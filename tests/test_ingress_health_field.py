"""Тесты _build_ingress_health_field — 🌐 Endpoint health (ingress) embed-поле."""
from app.services.discord.embed_builder import _build_ingress_health_field


def _ih(**kw):
    base = {
        "endpoints_total": 1,
        "max_5xx_rate": 0.0,
        "max_p95_ms": 0.0,
        "top_endpoints": [],
        "is_ingress_derived": True,
    }
    base.update(kw)
    return base


def test_none_and_empty_skip():
    assert _build_ingress_health_field(None) is None
    assert _build_ingress_health_field({}) is None


def test_no_endpoints_skip():
    assert _build_ingress_health_field(_ih(endpoints_total=0)) is None


def test_all_zero_skip():
    assert _build_ingress_health_field(_ih(max_5xx_rate=0.0, max_p95_ms=0.0)) is None


def test_renders_5xx_with_endpoint_and_wo_note():
    field = _build_ingress_health_field(_ih(
        max_5xx_rate=0.4, max_p95_ms=320.0,
        top_endpoints=[{"host": "wo-api.x.com", "path": "/town",
                        "error_5xx_rate": 0.4, "p95_latency_ms": 320.0, "rps": 12}],
    ))
    assert field is not None
    assert "🌐" in field["name"]
    assert "5xx: 0.4 rps" in field["value"]
    assert "wo-api.x.com/town" in field["value"]
    assert "p95: 320 ms" in field["value"]
    assert "WO-12483" in field["value"]


def test_renders_p95_only_when_no_5xx():
    field = _build_ingress_health_field(_ih(max_5xx_rate=0.0, max_p95_ms=210.0))
    assert field is not None
    assert "p95: 210 ms" in field["value"]
    assert "5xx" not in field["value"]
