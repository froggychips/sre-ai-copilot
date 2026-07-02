"""Statics-aware restart attribution (инцидент 2026-07-02).

Накат ПРОД-статики (v10400-prod→v10401-prod) заставляет статикозависимые
сервисы (town-*/map-*/bot/dev/mv/notificator) по всем prod-kingdom+shared
детектить смену хеша и самим штатно рестартиться (graceful exit 0,
`Newer statics … Will shutdown to reload`). k8s Deployment при этом НЕ
меняется → deploy-атрибуция видит «деплоя не было» и ложно хватается за
cross-namespace collateral соседних ns.

Фикс: перед выдачей collateral-вердикта проверяем недавний bump статики для
env алерта. Если был в окне — приоритетный вердикт «накат статики → ожидаемый
self-restart wave», collateral подавлен, @mention снят.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from app.models.incident import Incident
from app.services.alert_enrichment import (
    EnrichedContext,
    _detect_statics_bump,
    enrich_alert,
)
from app.services.statics_service import statics_env_from_namespace


def _make_incident(alertname: str, namespace: str) -> Incident:
    return Incident(
        incident_id="statics-attr-test",
        severity="critical",
        status="firing",
        summary="test",
        namespace=namespace,
        labels={"alertname": alertname, "severity": "critical"},
        annotations={},
        # старая дата → effective_at=now в enrich_alert (chronic-путь).
        starts_at="2026-06-10T13:16:00Z",
    )


# ── namespace → env суффикс version-БД ────────────────────────────────────

def test_env_mapping_prod_is_shared_across_kingdoms():
    assert statics_env_from_namespace("prod-shared") == "prod"
    assert statics_env_from_namespace("prod-kingdom4") == "prod"


def test_env_mapping_preprod_preupdate_squad():
    assert statics_env_from_namespace("preprod-shared") == "preprod"
    assert statics_env_from_namespace("preupdate-kingdom5") == "preupdate"
    assert statics_env_from_namespace("squad-12-shared") == "squad-12"
    assert statics_env_from_namespace("squad-gd-shared") == "squad-gd"


def test_env_mapping_infra_ns_is_none():
    assert statics_env_from_namespace("monitoring") is None
    assert statics_env_from_namespace("kube-system") is None
    assert statics_env_from_namespace("") is None
    assert statics_env_from_namespace(None) is None


# ── _detect_statics_bump: окно/фичефлаг/деградация ────────────────────────

_NOW = datetime.now(timezone.utc)


def _info(created_at, version=10401, prev=10400):
    return {
        "version": version,
        "prev_version": prev,
        "created_at": created_at,
        "datname": f"v{version}-prod",
        "env": "prod",
    }


@patch("app.services.alert_enrichment.get_latest_statics_version")
def test_recent_bump_within_window_detected(mock_latest):
    mock_latest.return_value = _info(_NOW - timedelta(minutes=10))
    bump = _detect_statics_bump("prod-shared", _NOW)
    assert bump
    assert bump["version"] == 10401
    assert bump["prev_version"] == 10400
    assert bump["env"] == "prod"
    assert bump["minutes_before"] == 10


@patch("app.services.alert_enrichment.get_latest_statics_version")
def test_old_bump_outside_window_ignored(mock_latest):
    mock_latest.return_value = _info(_NOW - timedelta(minutes=90))
    assert _detect_statics_bump("prod-shared", _NOW) == {}


@patch("app.services.alert_enrichment.get_latest_statics_version")
def test_future_bump_beyond_skew_ignored(mock_latest):
    # Версия «создана» сильно ПОЗЖЕ алерта — не может быть причиной.
    mock_latest.return_value = _info(_NOW + timedelta(minutes=15))
    assert _detect_statics_bump("prod-shared", _NOW) == {}


@patch("app.services.alert_enrichment.get_latest_statics_version")
def test_no_commit_timestamp_degrades_to_empty(mock_latest):
    # track_commit_timestamp off → created_at=None → окно не оценить → {}.
    mock_latest.return_value = _info(None)
    assert _detect_statics_bump("prod-shared", _NOW) == {}


@patch("app.services.alert_enrichment.get_latest_statics_version")
def test_killswitch_off_skips_lookup(mock_latest):
    with patch(
        "app.services.alert_enrichment.settings.STATICS_RESTART_ATTRIB_ENABLED",
        False,
    ):
        assert _detect_statics_bump("prod-shared", _NOW) == {}
    mock_latest.assert_not_called()


@patch("app.services.alert_enrichment.get_latest_statics_version")
def test_unmapped_namespace_skips_lookup(mock_latest):
    assert _detect_statics_bump("monitoring", _NOW) == {}
    mock_latest.assert_not_called()


# ── enrich_alert: ctx.statics_bump заполняется в ns-fallback ветке ─────────

@patch("app.services.alert_enrichment.cluster_deploy_activity", return_value={})
@patch("app.services.alert_enrichment.recent_deploys_for_namespaces", return_value=[])
@patch("app.services.alert_enrichment.get_latest_statics_version")
def test_enrich_populates_statics_bump(mock_latest, _mock_ns, _mock_cluster):
    mock_latest.return_value = _info(_NOW - timedelta(minutes=8))
    inc = _make_incident("ProdRestartsSpike", "prod-shared")
    ctx = enrich_alert(MagicMock(), inc)
    assert ctx.deploy_scope == "namespace"
    assert ctx.recent_deploys == []
    assert ctx.statics_bump["version"] == 10401
    assert ctx.statics_bump["env"] == "prod"


@patch("app.services.alert_enrichment.cluster_deploy_activity", return_value={})
@patch("app.services.alert_enrichment.recent_deploys_for_namespaces", return_value=[])
@patch("app.services.alert_enrichment.get_latest_statics_version", return_value=None)
def test_enrich_no_bump_leaves_empty(_mock_latest, _mock_ns, _mock_cluster):
    inc = _make_incident("ProdRestartsSpike", "prod-shared")
    ctx = enrich_alert(MagicMock(), inc)
    assert ctx.statics_bump == {}


# ── embed-рендер: statics-вердикт приоритетнее collateral + без пинга ─────

_CLUSTER_ACT = {
    "total_deploys": 530,
    "distinct_builds": 2,
    "earliest_minutes_before": 7,
    "namespaces": [
        {"namespace": "squad-gd-shared", "deploys": 60},
        {"namespace": "preprod-shared", "deploys": 44},
    ],
    "sample_builds": [
        {
            "namespace": "preprod-shared", "buildtype_id": "Bt1",
            "buildtype_name": "Build and update", "number": "727",
            "triggered_by": "ybobryashov", "minutes_before_incident": 7,
        },
    ],
}


def _ns_ctx(namespace, *, statics_bump=None, cluster=None) -> EnrichedContext:
    inc = _make_incident("ProdRestartsSpike", namespace)
    ctx = EnrichedContext(incident=inc)
    ctx.deploy_scope = "namespace"
    ctx.ns_deploy_window_min = 60
    ctx.recent_deploys = []
    ctx.statics_bump = statics_bump or {}
    ctx.cluster_deploy_activity = cluster or {}
    return ctx


def _build_payload(contexts):
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
    return captured.get("payload") or {}


def _dep_field(payload):
    fields = {f["name"]: f["value"] for f in payload["embeds"][0].get("fields", [])}
    return next((v for k, v in fields.items() if k.startswith("Deploy-связь")), None)


_BUMP = {
    "version": 10401, "prev_version": 10400, "env": "prod",
    "created_at": (_NOW - timedelta(minutes=8)).isoformat(), "minutes_before": 8,
}


def test_statics_verdict_replaces_collateral():
    # Статика катилась И рядом были деплои соседей — статика приоритетнее.
    payload = _build_payload([_ns_ctx("prod-shared", statics_bump=_BUMP, cluster=_CLUSTER_ACT)])
    field = _dep_field(payload)
    assert field is not None
    assert "Накат статики" in field
    assert "v10400-prod→v10401-prod" in field
    assert "self-restart" in field
    # Ложные вердикты НЕ выводятся.
    assert "cross-namespace rollout-collateral" not in field
    assert "вряд ли связано" not in field


def test_statics_verdict_suppresses_mention():
    payload = _build_payload([_ns_ctx("prod-shared", statics_bump=_BUMP)])
    assert "content" not in payload
    assert payload["allowed_mentions"] == {"parse": []}
    assert "Mention подавлен" in _dep_field(payload)


def test_no_statics_bump_keeps_collateral():
    # Без bump'а статики — прежняя collateral-логика не тронута.
    payload = _build_payload([_ns_ctx("prod-shared", cluster=_CLUSTER_ACT)])
    field = _dep_field(payload)
    assert "cross-namespace rollout-collateral" in field
    assert "Накат статики" not in field
    # collateral сам по себе НЕ подавляет пинг (только statics/deploy делают).
    assert payload.get("content", "").strip() == "@here"
