"""Blackbox-probe namespace resolution (инцидент ProdEndpointDown 2026-06-30).

ProdEndpointDown ставит `namespace: prod-shared` СТАТИЧЕСКИ (AM-роут), а
реальный затронутый realm зашит в URL пробы (`instance`):
`https://wo-api4-prod.lastoasisgame.com/town/health/ready` → prod-kingdom4.

Полный прод-релиз prod-280 ролльнул prod-kingdom4 и на время раскатки уронил
пробу wo-api4. Атрибуция смотрела в prod-shared, деплоя там не нашла → ложный
«795 деплоев в соседях» + не подавила @here. Фикс: резолвим namespace из
instance-URL, после чего существующая NS-fallback атрибуция и mention-
подавление по окну деплоя работают правильно.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.models.incident import Incident
from app.services.alert_enrichment import (
    _resolve_probe_namespace,
    enrich_alert,
)


def _probe_incident(instance: str | None, namespace: str = "prod-shared") -> Incident:
    labels = {
        "alertname": "ProdEndpointDown",
        "severity": "critical",
        "namespace": namespace,
        "job": "prod-blackbox",
    }
    if instance is not None:
        labels["instance"] = instance
    return Incident(
        incident_id="probe-test",
        severity="critical",
        status="firing",
        summary="PROD endpoint не отвечает",
        namespace=namespace,
        labels=labels,
        annotations={},
        starts_at="2026-06-30T12:01:00Z",
    )


# ── unit: _resolve_probe_namespace ──────────────────────────────────────────

@pytest.mark.parametrize("instance,expected", [
    # kingdom-проба town-service — главный кейс инцидента
    ("https://wo-api4-prod.lastoasisgame.com/town/health/ready", "prod-kingdom4"),
    ("https://wo-api7-prod.lastoasisgame.com/town/health/ready", "prod-kingdom7"),
    ("https://wo-api1-prod.lastoasisgame.com/town/health/ready", "prod-kingdom1"),
    # shared-проба auth-service (без номера) → prod-shared (он же и был)
    ("https://wo-api-prod.lastoasisgame.com/auth/health/ready", "prod-shared"),
    # env-generic: preprod/preupdate переиспользуют тот же паттерн
    ("https://wo-api2-preprod.lastoasisgame.com/town/health/ready", "preprod-kingdom2"),
    ("https://wo-api5-preupdate.lastoasisgame.com/town/health/ready", "preupdate-kingdom5"),
    # голый host без схемы + с портом — тоже резолвим
    ("wo-api4-prod.lastoasisgame.com/town/health/ready", "prod-kingdom4"),
    ("https://wo-api4-prod.lastoasisgame.com:443/town/health/ready", "prod-kingdom4"),
])
def test_resolve_probe_namespace_maps_known_hosts(instance, expected):
    assert _resolve_probe_namespace({"instance": instance}) == expected


@pytest.mark.parametrize("instance", [
    "10.244.1.5:9100",                       # node-exporter target — не проба
    "https://grafana.lastoasisgame.com/",    # другой host, не wo-api
    "https://wo-cdn-prod.lastoasisgame.com/",  # не api-паттерн
    "garbage",
    "",
])
def test_resolve_probe_namespace_returns_none_for_non_probe(instance):
    assert _resolve_probe_namespace({"instance": instance}) is None


def test_resolve_probe_namespace_none_without_instance():
    assert _resolve_probe_namespace({}) is None
    assert _resolve_probe_namespace({"alertname": "ProdEndpointDown"}) is None
    # fallback на `target`-label, если instance нет
    assert _resolve_probe_namespace(
        {"target": "https://wo-api3-prod.lastoasisgame.com/town/health/ready"}
    ) == "prod-kingdom3"


# ── integration: enrich_alert переопределяет namespace и атрибутит деплой ────

def _deploy(ns: str, minutes_before: int) -> dict:
    return {
        "name": "town-service", "namespace": ns,
        "buildtype_id": "Bt1", "buildtype_name": "Build and full deploy",
        "number": "834", "triggered_by": "ybobryashov",
        "minutes_before_incident": minutes_before, "sha": None, "repo": None,
        "status": "SUCCESS", "url": None, "ts": None,
    }


@patch("app.services.alert_enrichment.recent_deploys_for_namespaces")
def test_probe_alert_attributes_to_kingdom_namespace(mock_ns_deploys):
    """wo-api4 → деплой ищется в prod-kingdom4, не в статическом prod-shared."""
    mock_ns_deploys.return_value = [_deploy("prod-kingdom4", 8)]
    inc = _probe_incident(
        "https://wo-api4-prod.lastoasisgame.com/town/health/ready"
    )
    ctx = enrich_alert(MagicMock(), inc)

    assert ctx.deploy_scope == "namespace"
    assert ctx.recent_deploys and ctx.recent_deploys[0]["namespace"] == "prod-kingdom4"
    # запрос ушёл в prod-kingdom4, а НЕ в статический prod-shared
    args, _ = mock_ns_deploys.call_args
    assert args[1] == ["prod-kingdom4"]
    # debug-метки в extras
    assert ctx.extras["probe_ns_resolved"] == "prod-kingdom4"
    assert ctx.extras["probe_ns_static_label"] == "prod-shared"


@patch("app.services.alert_enrichment.cluster_deploy_activity")
@patch("app.services.alert_enrichment.recent_deploys_for_namespaces", return_value=[])
def test_probe_alert_kill_switch_keeps_static_namespace(mock_ns_deploys, mock_cluster):
    """ENRICH_PROBE_NS_RESOLVE_ENABLED=False → старое поведение (prod-shared)."""
    mock_cluster.return_value = {}
    inc = _probe_incident(
        "https://wo-api4-prod.lastoasisgame.com/town/health/ready"
    )
    with patch(
        "app.services.alert_enrichment.settings.ENRICH_PROBE_NS_RESOLVE_ENABLED",
        False,
    ):
        ctx = enrich_alert(MagicMock(), inc)
    args, _ = mock_ns_deploys.call_args
    assert args[1] == ["prod-shared"]
    assert "probe_ns_resolved" not in ctx.extras


@patch("app.services.alert_enrichment.cluster_deploy_activity", return_value={})
@patch("app.services.alert_enrichment.recent_deploys_for_namespaces", return_value=[])
def test_shared_probe_stays_prod_shared(mock_ns_deploys, mock_cluster):
    """wo-api-prod (без номера) — это и есть prod-shared, override no-op."""
    inc = _probe_incident(
        "https://wo-api-prod.lastoasisgame.com/auth/health/ready"
    )
    ctx = enrich_alert(MagicMock(), inc)
    args, _ = mock_ns_deploys.call_args
    assert args[1] == ["prod-shared"]
    # namespace не менялся → extras-метки не выставлены
    assert "probe_ns_resolved" not in ctx.extras


# ── end-to-end: ложный @here на штатной раскатке больше не шлётся ────────────

def _build_payload(contexts):
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
    return captured.get("payload") or {}


@patch("app.services.alert_enrichment.recent_deploys_for_namespaces")
def test_probe_alert_during_release_suppresses_mention(mock_ns_deploys):
    """Полный сценарий инцидента 2026-06-30: деплой prod-kingdom4 8м назад →
    карточка показывает deploy-связь и НЕ пингует @here."""
    mock_ns_deploys.return_value = [_deploy("prod-kingdom4", 8)]
    inc = _probe_incident(
        "https://wo-api4-prod.lastoasisgame.com/town/health/ready"
    )
    ctx = enrich_alert(MagicMock(), inc)
    payload = _build_payload([ctx])

    # @here не отправлен (был бы при cross-ns collateral до фикса)
    assert "content" not in payload
    assert payload["allowed_mentions"] == {"parse": []}
    fields = {f["name"]: f["value"] for f in payload["embeds"][0]["fields"]}
    dep_field = next(v for k, v in fields.items() if k.startswith("Deploy-связь"))
    assert "Возможно связано с деплоем" in dep_field
    assert "Mention подавлен" in dep_field
    # ложная collateral-гипотеза «795 деплоев в соседях» не выводится
    assert "соседних ns" not in dep_field
