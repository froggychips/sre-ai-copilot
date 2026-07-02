"""Statics-aware restart attribution (инцидент 2026-07-02).

Накат ПРОД-статики (v10400-prod→v10401-prod) заставляет статикозависимые
сервисы (town-*/map-*/bot/dev/mv/notificator) по всем prod-kingdom+shared
детектить смену хеша и самим штатно рестартиться (graceful exit 0,
`Newer statics … Will shutdown to reload`). k8s Deployment при этом НЕ
меняется → deploy-атрибуция видит «деплоя не было» и ложно хватается за
cross-namespace collateral соседних ns.

Фикс: момент наката определяется version-delta через Redis (копайлот
наблюдает номер версии env — beat + on-demand — и фиксирует first_observed_at
при смене номера). Перед выдачей collateral-вердикта проверяем недавний bump
статики для env алерта. Если был в окне — приоритетный вердикт «накат статики
→ ожидаемый self-restart wave», collateral подавлен, @mention снят.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from app.models.incident import Incident
from app.services import statics_service
from app.services.alert_enrichment import (
    EnrichedContext,
    _detect_statics_bump,
    enrich_alert,
)
from app.services.statics_service import (
    observe_statics_version,
    statics_env_from_namespace,
)

_NOW = datetime.now(timezone.utc)


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


# ── observe_statics_version: version-delta через Redis ────────────────────

class _FakeRedis:
    def __init__(self, initial=None):
        self.store = dict(initial or {})
        self.set_calls = []

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.store[key] = value
        self.set_calls.append((key, value, ex))


def _key(env="prod"):
    return statics_service._STATICS_SEEN_KEY.format(env=env)


@patch("app.services.statics_service.get_latest_statics_version")
def test_observe_first_sighting_prev_none(mock_latest):
    mock_latest.return_value = {"version": 10400, "prev_version": 10399, "env": "prod"}
    fake = _FakeRedis()
    with patch("app.services.statics_service._get_redis", return_value=fake):
        state = observe_statics_version("prod")
    # Первое наблюдение env: prev_version None (нет «до»-снимка → не bump).
    assert state["version"] == 10400
    assert state["prev_version"] is None
    assert state["first_observed_at"]
    # Снимок записан в Redis.
    assert json.loads(fake.store[_key()])["version"] == 10400


@patch("app.services.statics_service.get_latest_statics_version")
def test_observe_version_change_records_prev_and_now(mock_latest):
    mock_latest.return_value = {"version": 10401, "prev_version": 10400, "env": "prod"}
    old_iso = (_NOW - timedelta(hours=3)).isoformat()
    fake = _FakeRedis({
        _key(): json.dumps({"version": 10400, "prev_version": None, "first_observed_at": old_iso})
    })
    with patch("app.services.statics_service._get_redis", return_value=fake):
        state = observe_statics_version("prod")
    # Смена версии: prev = прежний наблюдённый, first_observed_at ~ сейчас.
    assert state["version"] == 10401
    assert state["prev_version"] == 10400
    fo = datetime.fromisoformat(state["first_observed_at"])
    assert abs((datetime.now(timezone.utc) - fo).total_seconds()) < 60


@patch("app.services.statics_service.get_latest_statics_version")
def test_observe_same_version_keeps_first_observed_at(mock_latest):
    mock_latest.return_value = {"version": 10401, "prev_version": 10400, "env": "prod"}
    t = (_NOW - timedelta(minutes=8)).isoformat()
    fake = _FakeRedis({
        _key(): json.dumps({"version": 10401, "prev_version": 10400, "first_observed_at": t})
    })
    with patch("app.services.statics_service._get_redis", return_value=fake):
        state = observe_statics_version("prod")
    # Та же версия — момент первого появления НЕ двигается (вердикт стабилен).
    assert state["first_observed_at"] == t
    assert state["prev_version"] == 10400
    assert fake.set_calls == []  # снимок не переписывался


@patch("app.services.statics_service.get_latest_statics_version", return_value=None)
def test_observe_no_statics_config(mock_latest):
    assert observe_statics_version("prod") is None


@patch("app.services.statics_service.get_latest_statics_version")
def test_observe_redis_down_returns_none(mock_latest):
    mock_latest.return_value = {"version": 10401, "prev_version": 10400, "env": "prod"}
    with patch("app.services.statics_service._get_redis", return_value=None):
        assert observe_statics_version("prod") is None


# ── _detect_statics_bump: окно / prev_version / фичефлаг ──────────────────

def _state(minutes_ago, version=10401, prev=10400):
    return {
        "version": version,
        "prev_version": prev,
        "first_observed_at": (_NOW - timedelta(minutes=minutes_ago)).isoformat(),
        "env": "prod",
    }


@patch("app.services.alert_enrichment.observe_statics_version")
def test_recent_bump_within_window_detected(mock_obs):
    mock_obs.return_value = _state(10)
    bump = _detect_statics_bump("prod-shared", _NOW)
    assert bump
    assert bump["version"] == 10401
    assert bump["prev_version"] == 10400
    assert bump["env"] == "prod"
    assert bump["minutes_before"] == 10


@patch("app.services.alert_enrichment.observe_statics_version")
def test_old_bump_outside_window_ignored(mock_obs):
    mock_obs.return_value = _state(90)
    assert _detect_statics_bump("prod-shared", _NOW) == {}


@patch("app.services.alert_enrichment.observe_statics_version")
def test_first_sighting_no_prev_ignored(mock_obs):
    # prev_version None (первое наблюдение env, нет «до»-снимка) → не bump.
    mock_obs.return_value = {**_state(2), "prev_version": None}
    assert _detect_statics_bump("prod-shared", _NOW) == {}


@patch("app.services.alert_enrichment.observe_statics_version")
def test_killswitch_off_skips_lookup(mock_obs):
    with patch(
        "app.services.alert_enrichment.settings.STATICS_RESTART_ATTRIB_ENABLED",
        False,
    ):
        assert _detect_statics_bump("prod-shared", _NOW) == {}
    mock_obs.assert_not_called()


@patch("app.services.alert_enrichment.observe_statics_version")
def test_unmapped_namespace_skips_lookup(mock_obs):
    assert _detect_statics_bump("monitoring", _NOW) == {}
    mock_obs.assert_not_called()


# ── enrich_alert: ctx.statics_bump заполняется в ns-fallback ветке ─────────

@patch("app.services.alert_enrichment.cluster_deploy_activity", return_value={})
@patch("app.services.alert_enrichment.recent_deploys_for_namespaces", return_value=[])
@patch("app.services.alert_enrichment.observe_statics_version")
def test_enrich_populates_statics_bump(mock_obs, _mock_ns, _mock_cluster):
    mock_obs.return_value = _state(8)
    inc = _make_incident("ProdRestartsSpike", "prod-shared")
    ctx = enrich_alert(MagicMock(), inc)
    assert ctx.deploy_scope == "namespace"
    assert ctx.recent_deploys == []
    assert ctx.statics_bump["version"] == 10401
    assert ctx.statics_bump["env"] == "prod"


@patch("app.services.alert_enrichment.cluster_deploy_activity", return_value={})
@patch("app.services.alert_enrichment.recent_deploys_for_namespaces", return_value=[])
@patch("app.services.alert_enrichment.observe_statics_version", return_value=None)
def test_enrich_no_bump_leaves_empty(_mock_obs, _mock_ns, _mock_cluster):
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

_BUMP = {
    "version": 10401, "prev_version": 10400, "env": "prod",
    "first_observed_at": (_NOW - timedelta(minutes=8)).isoformat(), "minutes_before": 8,
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
