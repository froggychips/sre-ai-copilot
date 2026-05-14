"""Тесты на OTEL graceful-degrade при unreachable collector.

Прод-кейс: `OTLP_EXPORTER_ENDPOINT=jaeger.monitoring:4317`, но jaeger
в кластере отсутствует → bg-thread `BatchSpanProcessor` каждые секунды
ретрайнет export и спамит логи `Transient error StatusCode.UNAVAILABLE`.

Решение: probe endpoint при startup; если недоступен — не подключать
exporter. Spans всё равно создаются (provider есть), просто не уходят.
"""
from unittest.mock import MagicMock, patch

from app import telemetry


# ── _parse_otlp_endpoint ────────────────────────────────────────────────────

def test_parse_endpoint_http_prefix():
    assert telemetry._parse_otlp_endpoint("http://jaeger.monitoring:4317") == ("jaeger.monitoring", 4317)


def test_parse_endpoint_bare_host_port():
    assert telemetry._parse_otlp_endpoint("tempo.observability:4318") == ("tempo.observability", 4318)


def test_parse_endpoint_default_port():
    assert telemetry._parse_otlp_endpoint("http://jaeger.monitoring") == ("jaeger.monitoring", 4317)
    assert telemetry._parse_otlp_endpoint("plain-host") == ("plain-host", 4317)


def test_parse_endpoint_empty_or_invalid_returns_none():
    assert telemetry._parse_otlp_endpoint("") is None
    assert telemetry._parse_otlp_endpoint("not_a_number:abc") is None


# ── _otlp_reachable ─────────────────────────────────────────────────────────

def test_reachable_true_when_socket_connects():
    with patch("app.telemetry.socket.create_connection") as mock_sock:
        mock_sock.return_value.__enter__ = MagicMock()
        mock_sock.return_value.__exit__ = MagicMock()
        assert telemetry._otlp_reachable("http://host:4317") is True


def test_reachable_false_on_connection_refused():
    with patch("app.telemetry.socket.create_connection", side_effect=ConnectionRefusedError):
        assert telemetry._otlp_reachable("http://host:4317") is False


def test_reachable_false_on_timeout():
    with patch("app.telemetry.socket.create_connection", side_effect=TimeoutError("connect timeout")):
        assert telemetry._otlp_reachable("http://host:4317") is False


def test_reachable_false_on_dns_failure():
    with patch("app.telemetry.socket.create_connection", side_effect=OSError("nodename nor servname")):
        assert telemetry._otlp_reachable("http://nonexistent.invalid:4317") is False


def test_reachable_false_on_empty_endpoint():
    assert telemetry._otlp_reachable("") is False


# ── setup_telemetry behaviour ───────────────────────────────────────────────

def test_setup_attaches_exporter_when_reachable():
    """Reachable collector → BatchSpanProcessor добавляется в provider."""
    fake_provider = MagicMock()
    with patch("app.telemetry._otlp_reachable", return_value=True), \
         patch("app.telemetry.TracerProvider", return_value=fake_provider), \
         patch("app.telemetry.OTLPSpanExporter") as mock_exporter, \
         patch("app.telemetry.BatchSpanProcessor") as mock_processor, \
         patch("app.telemetry.trace.set_tracer_provider"):
        telemetry.setup_telemetry(service_name="test")

    mock_exporter.assert_called_once()
    mock_processor.assert_called_once()
    fake_provider.add_span_processor.assert_called_once()


def test_setup_skips_exporter_when_unreachable():
    """Unreachable collector → exporter НЕ создаётся, span_processor НЕ добавляется."""
    fake_provider = MagicMock()
    with patch("app.telemetry._otlp_reachable", return_value=False), \
         patch("app.telemetry.TracerProvider", return_value=fake_provider), \
         patch("app.telemetry.OTLPSpanExporter") as mock_exporter, \
         patch("app.telemetry.BatchSpanProcessor") as mock_processor, \
         patch("app.telemetry.trace.set_tracer_provider") as mock_set_provider:
        telemetry.setup_telemetry(service_name="test")

    mock_exporter.assert_not_called()
    mock_processor.assert_not_called()
    fake_provider.add_span_processor.assert_not_called()
    # Provider всё равно установлен — spans создаются, просто не экспортируются.
    mock_set_provider.assert_called_once_with(fake_provider)
