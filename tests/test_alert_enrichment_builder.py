"""Unit-тесты на app.services.alert_enrichment + builder в discord_service.

KG-функции мокаются: enrich_alert не должен звонить в реальную БД,
проверяем что собирает структуру EnrichedContext и что builder
формирует ожидаемый embed payload.
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.incident import Incident
from app.services.alert_enrichment import (EnrichedContext,
                                           _detect_rollout_noise,
                                           enrich_alert)
from app.services.discord_service import DiscordService


def _make_incident(
    alertname: str = "KubePodCrashLooping",
    namespace: str = "preprod-kingdom2",
    service: str = "bot-service",
    pod: str = "bot-service-abc",
    severity: str = "critical",
    starts_at: str = "2026-05-15T12:57:00Z",
) -> Incident:
    return Incident(
        incident_id="fp-123",
        severity=severity,
        status="firing",
        summary="x",
        description="Pod is crash looping.",
        namespace=namespace,
        labels={
            "alertname": alertname,
            "severity": severity,
            "namespace": namespace,
            "service": service,
            "pod": pod,
        },
        annotations={"description": "Pod is crash looping."},
        starts_at=starts_at,
    )


# ── rollout-noise heuristic ──────────────────────────────────────────────


def test_rollout_noise_triggers_on_recent_deploy_for_mismatch_alert():
    inc = _make_incident(alertname="KubeDeploymentGenerationMismatch", severity="warning")
    deploys = [{"minutes_before_incident": 3, "sha": "abc1234"}]
    assert _detect_rollout_noise(inc, deploys) is True


def test_rollout_noise_false_when_deploy_old():
    inc = _make_incident(alertname="KubeDeploymentGenerationMismatch", severity="warning")
    deploys = [{"minutes_before_incident": 30}]
    assert _detect_rollout_noise(inc, deploys) is False


def test_rollout_noise_false_for_other_alertname():
    inc = _make_incident(alertname="KubePodCrashLooping")
    deploys = [{"minutes_before_incident": 1}]
    assert _detect_rollout_noise(inc, deploys) is False


# ── enrich_alert: ничего не звонит когда service пуст ────────────────────


def test_enrich_alert_returns_empty_when_no_service():
    inc = _make_incident(service="")
    inc.labels.pop("service")
    inc.labels.pop("deployment", None)
    db = MagicMock()
    ctx = enrich_alert(db, inc)
    assert ctx.service is None
    assert ctx.in_kg is False
    assert ctx.recent_deploys == []
    assert ctx.upstream_alerts == []
    # 0 SQL вызовов — раньше выходим
    db.query.assert_not_called()


# ── enrich_alert: всё подхватывается из KG-моков ─────────────────────────


@patch("app.services.alert_enrichment.recent_deploys_for")
@patch("app.services.alert_enrichment.nearby_alerts")
@patch("app.services.alert_enrichment.incidents_on")
@patch("app.services.alert_enrichment._downstream_count_by_kind")
def test_enrich_alert_builds_full_context(
    mock_downstream, mock_incidents, mock_nearby, mock_recent
):
    inc = _make_incident()
    mock_recent.return_value = [{
        "name": "bot-service",
        "ts": datetime(2026, 5, 15, 12, 53, tzinfo=timezone.utc),
        "sha": "deadbeefcafe",
        "number": "2841",
        "buildtype_id": "WO_Build_Bot",
        "status": "SUCCESS",
        "minutes_before_incident": 4,
    }]
    mock_nearby.return_value = [{
        "service": "town-db-postgresql",
        "namespace": "preprod-kingdom2",
        "alertname": "DBHighReplicationLag",
        "severity": "warning",
        "fired_at": datetime(2026, 5, 15, 12, 54, tzinfo=timezone.utc),
        "minutes_before": 3,
        "edge_kind": "calls",
    }]
    mock_incidents.return_value = [{"alertname": "KubePodCrashLooping"}] * 3
    mock_downstream.return_value = {"calls": 18, "uses_nats": 4}

    db = MagicMock()
    svc_row = MagicMock()
    svc_row.team_owner = "gameplay-team"
    svc_row.synthetic = False
    svc_row.updated_at = datetime(2026, 5, 15, 12, 50, tzinfo=timezone.utc)
    # Backwards-compat: legacy mock уровень для _downstream_count_by_kind
    # и т.п., если будут.
    db.query.return_value.filter.return_value.one_or_none.return_value = svc_row
    # New resolver path: filter(ns, name).filter(synthetic==False).first()
    db.query.return_value.filter.return_value.filter.return_value.first.return_value = svc_row

    ctx = enrich_alert(db, inc)
    assert ctx.in_kg is True
    assert ctx.team_owner == "gameplay-team"
    assert len(ctx.recent_deploys) == 1
    assert ctx.recent_deploys[0]["number"] == "2841"
    assert len(ctx.upstream_alerts) == 1
    assert ctx.upstream_alerts[0]["service"] == "town-db-postgresql"
    assert ctx.inbound_count_by_kind == {"calls": 18, "uses_nats": 4}
    assert len(ctx.recurrence_24h) == 3

    # Hypothesis из RecentDeployRule должен быть top-1
    hyp = ctx.primary_hypothesis()
    assert hyp is not None
    assert "Deploy" in hyp and "4 мин назад" in hyp


# ── builder: send_enriched_alert payload ─────────────────────────────────


@pytest.mark.asyncio
async def test_send_enriched_alert_builds_grouped_embed():
    """3 ns в одной group → один embed, namespaces в field."""
    ctxs = []
    for i, ns in enumerate(["preprod-kingdom1", "preprod-kingdom2", "preprod-kingdom3"]):
        inc = _make_incident(namespace=ns, pod=f"bot-service-{i}")
        ctxs.append(EnrichedContext(
            incident=inc,
            service="bot-service",
            pod=inc.labels["pod"],
            team_owner="gameplay-team",
            in_kg=True,
            recent_deploys=[],
            upstream_alerts=[],
            recurrence_24h=[],
            inbound_count_by_kind={},
            rule_facts=[],
        ))

    sent = {}

    async def fake_post(self, url, json=None, **_):
        sent["url"] = url
        sent["payload"] = json
        resp = MagicMock()
        resp.status_code = 204
        return resp

    svc = DiscordService()
    with patch("app.services.discord_service.settings.DISCORD_DRY_RUN", False), \
         patch("app.services.discord_service.settings.DISCORD_WEBHOOK_URL",
               "https://example.com/wh"), \
         patch("httpx.AsyncClient.post", new=fake_post):
        await svc.send_enriched_alert(ctxs, env="preprod")

    embed = sent["payload"]["embeds"][0]
    assert "KubePodCrashLooping" in embed["title"]
    assert "PREPROD" in embed["title"]
    assert "bot-service" in embed["title"]
    assert "3 ns" in embed["title"]
    # Namespaces field есть
    ns_field = next(f for f in embed["fields"] if f["name"] == "Namespaces")
    assert "preprod-kingdom1" in ns_field["value"]
    assert "preprod-kingdom2" in ns_field["value"]
    assert "preprod-kingdom3" in ns_field["value"]
    # Owner field
    owner = next(f for f in embed["fields"] if f["name"] == "Owner")
    assert "gameplay-team" in owner["value"]
    # Цвет critical
    assert embed["color"] == 0xE53935
    # Без mention payload
    assert sent["payload"]["allowed_mentions"] == {"parse": []}


@pytest.mark.asyncio
async def test_send_enriched_alert_rollout_noise_tagged_and_dimmed():
    """ROLLOUT-NORMAL → серый цвет, тэг в title."""
    inc = _make_incident(alertname="KubeDeploymentGenerationMismatch", severity="warning")
    ctx = EnrichedContext(
        incident=inc,
        service="bot-service",
        pod=inc.labels["pod"],
        in_kg=True,
        recent_deploys=[{
            "number": "2841",
            "sha": "deadbeef",
            "minutes_before_incident": 2,
            "status": "SUCCESS",
        }],
        rollout_noise=True,
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
    assert "ROLLOUT-NORMAL" in embed["title"]
    assert embed["color"] == 0x9E9E9E  # grey


@pytest.mark.asyncio
async def test_send_enriched_alert_recurrence_tag():
    inc = _make_incident()
    ctx = EnrichedContext(
        incident=inc,
        service="bot-service",
        recurrence_24h=[{"alertname": "KubePodCrashLooping"}] * 5,
        in_kg=True,
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
    assert "🔁 ×5" in embed["title"]


@pytest.mark.asyncio
async def test_send_enriched_alert_dry_run_does_not_call_http():
    inc = _make_incident()
    ctx = EnrichedContext(incident=inc, service="bot-service", in_kg=True)

    with patch("app.services.discord_service.settings.DISCORD_DRY_RUN", True), \
         patch("httpx.AsyncClient.post", new=AsyncMock()) as mock_post:
        await DiscordService().send_enriched_alert([ctx], env="preprod")

    mock_post.assert_not_called()
