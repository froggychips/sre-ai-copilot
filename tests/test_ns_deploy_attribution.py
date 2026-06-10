"""NS-level deploy attribution для service-less алертов.

Запрос on-call 2026-06-10 (#infra-error): «понимать, когда алерт связан
с деплоем, а когда нет». Namespace-агрегаты (PreprodRestartsSpike,
PreprodEndpointDown) не резолвятся в kg_services → сервисный
recent_deploys_for бессилен. Fallback: деплои всего namespace + явный
вердикт в embed, включая негативный («деплоев не было»).
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from app.models.incident import Incident
from app.services.alert_enrichment import (
    EnrichedContext,
    _ns_deploy_fallback_applies,
    enrich_alert,
)


def _make_incident(alertname: str, namespace: str, labels: dict | None = None) -> Incident:
    return Incident(
        incident_id="ns-attr-test",
        severity="critical",
        status="firing",
        summary="test",
        namespace=namespace,
        labels={"alertname": alertname, "severity": "critical", **(labels or {})},
        annotations={},
        starts_at="2026-06-10T13:16:00Z",
    )


# ── гейт по префиксам ns ─────────────────────────────────────────────────

def test_fallback_applies_to_app_namespaces():
    assert _ns_deploy_fallback_applies("preprod-kingdom2")
    assert _ns_deploy_fallback_applies("prod-shared")
    assert _ns_deploy_fallback_applies("preupdate-shared")
    assert _ns_deploy_fallback_applies("squad-13-shared")


def test_fallback_skips_infra_namespaces():
    assert not _ns_deploy_fallback_applies("monitoring")
    assert not _ns_deploy_fallback_applies("kube-system")
    assert not _ns_deploy_fallback_applies("sre-ai")


# ── enrich_alert: ns-fallback при нерезолвленном сервисе ─────────────────

@patch("app.services.alert_enrichment.recent_deploys_for_namespaces")
def test_serviceless_alert_in_app_ns_gets_ns_deploys(mock_ns_deploys):
    mock_ns_deploys.return_value = [
        {
            "name": "core-service", "namespace": "preprod-kingdom2",
            "buildtype_id": "Bt1", "buildtype_name": "Build and update",
            "number": "2385", "triggered_by": "user1",
            "minutes_before_incident": 24, "sha": None, "repo": None,
            "status": "SUCCESS", "url": None, "ts": datetime.now(timezone.utc),
        },
    ]
    inc = _make_incident("PreprodRestartsSpike", "preprod-kingdom2")
    ctx = enrich_alert(MagicMock(), inc)
    assert ctx.deploy_scope == "namespace"
    assert ctx.ns_deploy_window_min is not None
    assert len(ctx.recent_deploys) == 1
    assert ctx.recent_deploys[0]["namespace"] == "preprod-kingdom2"
    mock_ns_deploys.assert_called_once()


@patch("app.services.alert_enrichment.recent_deploys_for_namespaces", return_value=[])
def test_serviceless_alert_empty_window_keeps_scope(mock_ns_deploys):
    """Пустое окно — скоуп namespace всё равно выставлен: embed рендерит
    негативный вердикт «деплоев не было», а не молчит."""
    inc = _make_incident("PreprodEndpointDown", "preprod-shared")
    ctx = enrich_alert(MagicMock(), inc)
    assert ctx.deploy_scope == "namespace"
    assert ctx.recent_deploys == []


@patch("app.services.alert_enrichment.recent_deploys_for_namespaces")
def test_serviceless_alert_in_infra_ns_no_fallback(mock_ns_deploys):
    inc = _make_incident("NodeDiskIOSaturation", "monitoring")
    ctx = enrich_alert(MagicMock(), inc)
    assert ctx.deploy_scope == "service"
    assert ctx.recent_deploys == []
    mock_ns_deploys.assert_not_called()


@patch("app.services.alert_enrichment.recent_deploys_for", return_value=[])
@patch("app.services.alert_enrichment.nearby_alerts", return_value=[])
@patch("app.services.alert_enrichment.incidents_on", return_value=[])
@patch("app.services.alert_enrichment._downstream_count_by_kind", return_value={})
@patch("app.services.alert_enrichment.upstream_of", return_value=[])
@patch("app.services.alert_enrichment.recent_pod_events_for", return_value=[])
def test_resolved_service_keeps_service_scope(*_mocks):
    """Алерт с резолвом сервиса идёт обычным путём — scope=service."""
    inc = _make_incident(
        "KubeDeploymentReplicasMismatch", "monitoring",
        labels={"namespace": "preprod-shared", "deployment": "auth-service"},
    )
    db = MagicMock()
    svc_row = MagicMock()
    svc_row.team_owner = "platform"
    svc_row.synthetic = False
    svc_row.updated_at = datetime(2026, 6, 10, 9, 0, tzinfo=timezone.utc)
    db.query.return_value.filter.return_value.filter.return_value.first.return_value = svc_row
    db.query.return_value.filter.return_value.first.return_value = svc_row
    ctx = enrich_alert(db, inc)
    assert ctx.deploy_scope == "service"


# ── embed: поле «Deploy-связь» ───────────────────────────────────────────

def _ns_ctx(namespace: str, deploys: list) -> EnrichedContext:
    inc = _make_incident("PreprodRestartsSpike", namespace)
    ctx = EnrichedContext(incident=inc)
    ctx.deploy_scope = "namespace"
    ctx.ns_deploy_window_min = 60
    ctx.recent_deploys = deploys
    return ctx


def _build_fields(contexts):
    """Прогнать send_enriched_alert в DRY_RUN недоступно без сети —
    дёргаем сборку полей через публичный путь: собираем embed и ловим
    payload на _post_or_patch_enriched."""
    import asyncio

    from app.services.discord.service import DiscordService

    captured = {}

    async def _capture(self, **kwargs):
        captured["payload"] = kwargs.get("payload")

    svc = DiscordService()
    with (
        patch.object(DiscordService, "_post_or_patch_enriched", _capture),
        patch("app.services.discord.service.settings.DISCORD_WEBHOOK_URL", "https://example.com/wh"),
        patch("app.services.discord.service.settings.DISCORD_DRY_RUN", False),
    ):
        asyncio.run(svc.send_enriched_alert(contexts))
    embeds = (captured.get("payload") or {}).get("embeds") or [{}]
    return {f["name"]: f["value"] for f in embeds[0].get("fields", [])}


def test_embed_positive_deploy_verdict():
    deploys = [{
        "name": "core-service", "namespace": "preprod-kingdom2",
        "buildtype_id": "Bt1", "buildtype_name": "Build and update",
        "number": "2385", "triggered_by": "user1",
        "minutes_before_incident": 24, "sha": None, "repo": None,
        "status": "SUCCESS", "url": None, "ts": None,
    }]
    fields = _build_fields([_ns_ctx("preprod-kingdom2", deploys)])
    dep_field = next((v for k, v in fields.items() if k.startswith("Deploy-связь")), None)
    assert dep_field is not None
    assert "Возможно связано с деплоем" in dep_field
    assert "Build and update #2385" in dep_field
    assert "user1" in dep_field
    # Сервисный блок Recent deploys не дублирует ns-деплои.
    assert not any(k.startswith("Recent deploys") for k in fields)


def test_embed_negative_deploy_verdict():
    fields = _build_fields([_ns_ctx("preprod-shared", [])])
    dep_field = next((v for k, v in fields.items() if k.startswith("Deploy-связь")), None)
    assert dep_field is not None
    assert "не было" in dep_field
    assert "вряд ли связано" in dep_field


def test_embed_multi_ns_dedupes_same_build():
    """Один TC-билд катит десяток сервисов одного ns — в поле он один раз."""
    d = {
        "name": "core-service", "namespace": "preprod-kingdom2",
        "buildtype_id": "Bt1", "buildtype_name": "Build and full deploy",
        "number": "694", "triggered_by": "user2",
        "minutes_before_incident": 10, "sha": None, "repo": None,
        "status": "SUCCESS", "url": None, "ts": None,
    }
    d2 = dict(d, name="map-service")
    ctx1 = _ns_ctx("preprod-kingdom2", [d, d2])
    ctx2 = _ns_ctx("preprod-shared", [dict(d, namespace="preprod-shared")])
    fields = _build_fields([ctx1, ctx2])
    dep_field = next((v for k, v in fields.items() if k.startswith("Deploy-связь")), None)
    assert dep_field is not None
    assert dep_field.count("#694") == 1
