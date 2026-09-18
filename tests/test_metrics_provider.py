"""`MetricsProvider`: интерфейс к метрикам и реализация поверх VictoriaMetrics.

Главное здесь — то же, что у логов: граница между «серий нет» и «источник
не ответил». У метрик она стоит дороже. `metrics_sync` собирает пять
показателей на namespace, и до этой работы отказ глотался внутри клиента:
счётчик ошибок оставался нулевым, сервисы получали None по всем метрикам и
уходили в `skipped_empty`. При полностью недоступной VictoriaMetrics
прогон выглядел образцовым — errors=0 и правдоподобная цифра пропусков.
"""
from unittest.mock import AsyncMock, patch

import pytest

from app.context.vm_client import VMQueryError
from app.providers.measurement import Measurement
from app.providers.metrics import MetricsProvider
from app.providers.vm_metrics import VictoriaMetricsProvider, make_metrics_provider


def _provider() -> VictoriaMetricsProvider:
    return VictoriaMetricsProvider(name="vm", base_url="http://vm:8428")


# --- by_label -------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_result_is_measured_silence():
    """Источник ответил, серий нет — это факт, на котором можно делать вывод."""
    with patch.object(
        _provider()._client.__class__, "query_instant_by_strict",
        new=AsyncMock(return_value={}),
    ):
        result = await _provider().by_label("up", "pod")

    assert result.measured
    assert result.value == {}


@pytest.mark.asyncio
async def test_source_failure_is_unknown_not_empty():
    """Отказ — не пустой словарь.

    Именно это склеивание делало недоступную VictoriaMetrics неотличимой
    от namespace, где никто не экспортирует метрик.
    """
    with patch.object(
        _provider()._client.__class__, "query_instant_by_strict",
        new=AsyncMock(side_effect=VMQueryError("connection refused")),
    ):
        result = await _provider().by_label("up", "pod")

    assert not result.measured
    assert result.value is None
    assert "vm_unavailable" in result.reason


@pytest.mark.asyncio
async def test_values_pass_through():
    with patch.object(
        _provider()._client.__class__, "query_instant_by_strict",
        new=AsyncMock(return_value={"pod-a": 1.5, "pod-b": 0.0}),
    ):
        result = await _provider().by_label("up", "pod")

    assert result.value == {"pod-a": 1.5, "pod-b": 0.0}
    assert result.value["pod-b"] == 0.0, "настоящий ноль обязан доехать нулём"


# --- by_labels ------------------------------------------------------------

@pytest.mark.asyncio
async def test_composite_keys_survive():
    with patch.object(
        _provider()._client.__class__, "query_instant_by_labels_strict",
        new=AsyncMock(return_value={("host-a", "/api"): 2.0}),
    ):
        result = await _provider().by_labels("rate(x)", ("host", "path"))

    assert result.measured
    assert result.value[("host-a", "/api")] == 2.0


@pytest.mark.asyncio
async def test_composite_failure_is_unknown():
    with patch.object(
        _provider()._client.__class__, "query_instant_by_labels_strict",
        new=AsyncMock(side_effect=VMQueryError("timeout")),
    ):
        result = await _provider().by_labels("rate(x)", ("host", "path"))

    assert not result.measured


# --- scalar ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_scalar_zero_is_measured():
    """Ноль — значение, а не отсутствие данных."""
    with patch.object(
        _provider()._client.__class__, "query_instant",
        new=AsyncMock(return_value=0.0),
    ):
        result = await _provider().scalar("up")

    assert result.measured
    assert result.value == 0.0


@pytest.mark.asyncio
async def test_scalar_none_is_unknown():
    """None у query_instant означает «нет данных» — трактуем как незнание."""
    with patch.object(
        _provider()._client.__class__, "query_instant",
        new=AsyncMock(return_value=None),
    ):
        result = await _provider().scalar("up")

    assert not result.measured


# --- строгие методы клиента -----------------------------------------------

@pytest.mark.asyncio
async def test_strict_client_raises_instead_of_returning_empty():
    """Отличие strict-вариантов от прежних: отказ виден вызывающему."""
    from unittest.mock import MagicMock

    from app.context.vm_client import VMClient

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=Exception("connection refused"))

    with patch("httpx.AsyncClient", MagicMock(return_value=mock_client)):
        client = VMClient(base_url="http://vm:8428")
        # Прежний метод по-прежнему отдаёт пустоту — у него есть
        # потребители, которым это подходит (stats_digest).
        assert await client.query_instant_by("up", "pod") == {}
        # Строгий — поднимает.
        with pytest.raises(VMQueryError):
            await client.query_instant_by_strict("up", "pod")


# --- фабрика --------------------------------------------------------------

def test_factory_builds_vm_provider():
    provider = make_metrics_provider(url="http://vm:8428")
    assert isinstance(provider, VictoriaMetricsProvider)
    assert isinstance(provider, MetricsProvider)


def test_unknown_backend_is_an_error():
    """Опечатка в настройке не должна выглядеть как рабочая система."""
    with pytest.raises(ValueError, match="prometheus"):
        make_metrics_provider(url="u", backend="prometheus")


def test_measurement_type_is_shared_with_logs():
    """Один вопрос — один тип: у логов и метрик он общий."""
    from app.providers.logs import Measurement as LogMeasurement

    assert LogMeasurement is Measurement
