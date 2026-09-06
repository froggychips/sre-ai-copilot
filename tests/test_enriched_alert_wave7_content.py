"""Wave 7 контент в enriched Discord embed.

Покрытие трёх новых секций для critical severity:
  * 🎯 Blast radius (X, PR #71) — serves_traffic + routes_to IN-edges
  * 📨 NATS impact (Z, PR #72) — uses_nats OUT-edges + co-consumers
  * 🕒 Pod trail (Y, PR #70) — kg_pod_events агрегация по reason

Тесты не звонят в реальную БД: либо мокается `queries.*_for(...)`, либо
напрямую подаётся EnrichedContext с заполненными полями (для builder-level
тестов). Critical/warning gate проверяется отдельно.
"""
from unittest.mock import MagicMock, patch

import pytest

from app.models.incident import Incident
from app.services.alert_enrichment import EnrichedContext, enrich_alert
from app.services.discord.embed_builder import (_build_blast_radius_field,
                                                _build_nats_impact_field,
                                                _build_pod_trail_field)
from app.services.discord_service import DiscordService


def _make_incident(
    alertname: str = "KubePodCrashLooping",
    namespace: str = "preprod-kingdom2",
    service: str = "bot-service",
    pod: str = "bot-service-abc",
    severity: str = "critical",
    starts_at: str = "2026-05-23T12:57:00Z",
) -> Incident:
    return Incident(
        incident_id="fp-wave7",
        severity=severity,
        status="firing",
        summary="x",
        description="Wave 7 test.",
        namespace=namespace,
        labels={
            "alertname": alertname,
            "severity": severity,
            "namespace": namespace,
            "service": service,
            "pod": pod,
        },
        annotations={"description": "Wave 7 test."},
        starts_at=starts_at,
    )


# ── builder pure-function unit tests ─────────────────────────────────────────


def test_blast_radius_field_renders_services_and_urls():
    blast = {
        "services": ["foo-svc", "bar-svc"],
        "urls": ["api.foo.com", "admin.foo.com"],
        "services_total": 2,
        "urls_total": 2,
    }
    field = _build_blast_radius_field(blast)
    assert field is not None
    assert "Blast radius" in field["name"]
    assert "2 svc" in field["value"]
    assert "foo-svc" in field["value"]
    assert "2 URL" in field["value"]
    assert "api.foo.com" in field["value"]


def test_blast_radius_field_skip_when_empty():
    assert _build_blast_radius_field({"services": [], "urls": [], "services_total": 0, "urls_total": 0}) is None
    assert _build_blast_radius_field(None) is None
    assert _build_blast_radius_field({}) is None


def test_blast_radius_truncates_with_plus_suffix():
    blast = {
        "services": ["svc-1", "svc-2", "svc-3"],  # top_n=3 в queries
        "urls": [],
        "services_total": 5,  # есть ещё 2
        "urls_total": 0,
    }
    field = _build_blast_radius_field(blast)
    assert field is not None
    assert "5 svc" in field["value"]
    assert "(+2)" in field["value"]


def test_nats_impact_field_renders_pub_and_sub():
    impact = [
        {"subject": "leaderboardfinished", "direction": "pub", "impact_count": 2, "impact_others": []},
        {"subject": "events.>", "direction": "sub", "impact_count": 0, "impact_others": []},
    ]
    field = _build_nats_impact_field(impact)
    assert field is not None
    assert "NATS impact" in field["name"]
    assert "pub→" in field["value"]
    assert "leaderboardfinished" in field["value"]
    assert "sub←" in field["value"]
    assert "events.>" in field["value"]
    # impact_count → sub-консьюмеров
    assert "2 sub" in field["value"]


def test_nats_impact_field_skip_when_empty():
    assert _build_nats_impact_field([]) is None
    assert _build_nats_impact_field(None) is None


def test_nats_impact_strips_subject_prefix():
    impact = [{"subject": "nats-subject:foo.bar", "direction": "pub", "impact_count": 1, "impact_others": []}]
    field = _build_nats_impact_field(impact)
    assert field is not None
    # Префикс должен быть убран
    assert "nats-subject:" not in field["value"]
    assert "foo.bar" in field["value"]


def test_pod_trail_field_renders_aggregation():
    trail = {"total": 5, "by_reason": [("OOMKilled", 3), ("CrashLoopBackOff", 2)]}
    field = _build_pod_trail_field(trail)
    assert field is not None
    assert "Pod trail" in field["name"]
    assert "5 evts" in field["value"]
    assert "3 OOMKilled" in field["value"]
    assert "2 CrashLoopBackOff" in field["value"]


def test_pod_trail_field_skip_when_empty():
    assert _build_pod_trail_field({"total": 0, "by_reason": []}) is None
    assert _build_pod_trail_field({}) is None
    assert _build_pod_trail_field(None) is None


# ── integration: builder в send_enriched_alert payload ───────────────────────


@pytest.mark.asyncio
async def test_send_enriched_alert_critical_includes_wave7_sections():
    """Critical embed с заполненным Wave 7 — три секции в payload."""
    inc = _make_incident(severity="critical")
    ctx = EnrichedContext(
        incident=inc,
        service="bot-service",
        pod=inc.labels["pod"],
        in_kg=True,
        team_owner="gameplay",
        blast_radius={
            "services": ["foo-svc", "bar-svc", "baz-svc"],
            "urls": ["api.foo.com", "admin.foo.com"],
            "services_total": 3,
            "urls_total": 2,
        },
        nats_impact=[
            {"subject": "leaderboardfinished", "direction": "pub", "impact_count": 2, "impact_others": []},
        ],
        pod_trail={"total": 5, "by_reason": [("OOMKilled", 3), ("CrashLoopBackOff", 2)]},
    )
    sent = {}

    async def fake_post(self, url, json=None, **_):
        sent["payload"] = json
        resp = MagicMock()
        resp.status_code = 204
        return resp

    svc = DiscordService()
    with patch("app.services.discord_service.settings.DISCORD_DRY_RUN", False), \
         patch("app.services.discord_service.settings.DISCORD_WEBHOOK_URL",
               "https://example.com/wh"), \
         patch("httpx.AsyncClient.post", new=fake_post):
        await svc.send_enriched_alert([ctx], env="preprod")

    embed = sent["payload"]["embeds"][0]
    field_names = {f["name"] for f in embed["fields"]}
    assert any("Blast radius" in n for n in field_names), f"missing blast in {field_names}"
    assert any("NATS impact" in n for n in field_names), f"missing nats in {field_names}"
    assert any("Pod trail" in n for n in field_names), f"missing trail in {field_names}"


@pytest.mark.asyncio
async def test_send_enriched_alert_warning_skips_wave7_sections():
    """Warning compact mode не должен показывать Wave 7 (gate is_critical)."""
    inc = _make_incident(severity="warning")
    ctx = EnrichedContext(
        incident=inc,
        service="bot-service",
        in_kg=True,
        blast_radius={"services": ["foo"], "urls": ["x.com"], "services_total": 1, "urls_total": 1},
        nats_impact=[{"subject": "a.b", "direction": "pub", "impact_count": 1, "impact_others": []}],
        pod_trail={"total": 3, "by_reason": [("OOMKilled", 3)]},
    )
    sent = {}

    async def fake_post(self, url, json=None, **_):
        sent["payload"] = json
        resp = MagicMock()
        resp.status_code = 204
        return resp

    svc = DiscordService()
    with patch("app.services.discord_service.settings.DISCORD_DRY_RUN", False), \
         patch("app.services.discord_service.settings.DISCORD_WEBHOOK_URL",
               "https://example.com/wh"), \
         patch("httpx.AsyncClient.post", new=fake_post):
        await svc.send_enriched_alert([ctx], env="preprod")

    embed = sent["payload"]["embeds"][0]
    field_names = {f["name"] for f in embed["fields"]}
    # Ни одной Wave 7 секции в warning compact mode
    assert not any("Blast radius" in n for n in field_names)
    assert not any("NATS impact" in n for n in field_names)
    assert not any("Pod trail" in n for n in field_names)


@pytest.mark.asyncio
async def test_send_enriched_alert_critical_skips_empty_wave7_sections():
    """Critical но Wave 7 поля пусты — секции не должны появляться (skip-if-empty)."""
    inc = _make_incident(severity="critical")
    ctx = EnrichedContext(
        incident=inc,
        service="bot-service",
        in_kg=True,
        # Все Wave 7 поля дефолтные / пустые
    )
    sent = {}

    async def fake_post(self, url, json=None, **_):
        sent["payload"] = json
        resp = MagicMock()
        resp.status_code = 204
        return resp

    svc = DiscordService()
    with patch("app.services.discord_service.settings.DISCORD_DRY_RUN", False), \
         patch("app.services.discord_service.settings.DISCORD_WEBHOOK_URL",
               "https://example.com/wh"), \
         patch("httpx.AsyncClient.post", new=fake_post):
        await svc.send_enriched_alert([ctx], env="preprod")

    embed = sent["payload"]["embeds"][0]
    field_names = {f["name"] for f in embed["fields"]}
    assert not any("Blast radius" in n for n in field_names)
    assert not any("NATS impact" in n for n in field_names)
    assert not any("Pod trail" in n for n in field_names)


# ── enrich_alert: wave7 lookup только при severity=critical ──────────────────


@patch("app.services.alert_enrichment.pod_event_summary_for")
@patch("app.services.alert_enrichment.nats_impact_for")
@patch("app.services.alert_enrichment.blast_radius_v2")
@patch("app.services.alert_enrichment.recent_pod_events_for", return_value=[])
@patch("app.services.alert_enrichment.recent_deploys_for", return_value=[])
@patch("app.services.alert_enrichment.nearby_alerts", return_value=[])
@patch("app.services.alert_enrichment.incidents_on", return_value=[])
@patch("app.services.alert_enrichment._downstream_count_by_kind", return_value={})
def test_enrich_alert_calls_wave7_queries_when_critical(
    _ic, _do, _ne, _rd, _rpe,
    mock_blast, mock_nats, mock_trail,
):
    mock_blast.return_value = {"services": ["a"], "urls": [], "services_total": 1, "urls_total": 0}
    mock_nats.return_value = [{"subject": "x", "direction": "pub", "impact_count": 1, "impact_others": []}]
    mock_trail.return_value = {"total": 2, "by_reason": [("OOMKilled", 2)]}

    inc = _make_incident(severity="critical")
    db = MagicMock()
    db.query.return_value.filter.return_value.filter.return_value.first.return_value = None

    ctx = enrich_alert(db, inc)
    # Wave 7 queries вызваны
    assert mock_blast.called
    assert mock_nats.called
    assert mock_trail.called
    # И поля заполнены
    assert ctx.blast_radius.get("services_total") == 1
    assert len(ctx.nats_impact) == 1
    assert ctx.pod_trail.get("total") == 2


@patch("app.services.alert_enrichment.pod_event_summary_for")
@patch("app.services.alert_enrichment.nats_impact_for")
@patch("app.services.alert_enrichment.blast_radius_v2")
@patch("app.services.alert_enrichment.recent_pod_events_for", return_value=[])
@patch("app.services.alert_enrichment.recent_deploys_for", return_value=[])
@patch("app.services.alert_enrichment.nearby_alerts", return_value=[])
@patch("app.services.alert_enrichment.incidents_on", return_value=[])
@patch("app.services.alert_enrichment._downstream_count_by_kind", return_value={})
def test_enrich_alert_skips_wave7_queries_when_warning(
    _ic, _do, _ne, _rd, _rpe,
    mock_blast, mock_nats, mock_trail,
):
    inc = _make_incident(severity="warning")
    db = MagicMock()
    db.query.return_value.filter.return_value.filter.return_value.first.return_value = None

    ctx = enrich_alert(db, inc)
    # Никаких Wave 7 запросов для warning — экономим 3 SQL hits.
    mock_blast.assert_not_called()
    mock_nats.assert_not_called()
    mock_trail.assert_not_called()
    assert ctx.blast_radius == {}
    assert ctx.nats_impact == []
    assert ctx.pod_trail == {}
