"""Meta-aggregate / scrape-plumbing noise muting (прецедент ProdNewCriticalAlerts 2026-06-16).

В отличие от rollout-noise (демот severity→info, тихий канал) meta-noise НЕ
дропается и НЕ демотится: карточка остаётся видимой, но рендерится приглушённо
(grey + 🔇 META-AGGREGATE, без 🚨 и без @mention). Каждый реальный критикал,
который агрегат считает, и так приходит копайлоту отдельной карточкой.
"""
from __future__ import annotations

from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from app.models.incident import Incident
from app.services.alert_enrichment import EnrichedContext, _detect_meta_noise
from app.services.discord.service import _COLOR_UNKNOWN
from app.services.discord_service import DiscordService

_STARTS_AT = "2026-06-16T17:47:00+00:00"


def _incident(alertname: str, severity: str = "critical") -> Incident:
    return Incident(
        incident_id="fp-meta",
        severity=severity,
        status="firing",
        summary="x",
        description="",
        namespace="prod-shared",
        labels={"alertname": alertname},
        annotations={},
        starts_at=_STARTS_AT,
    )


# ── _detect_meta_noise ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "alertname",
    [
        "ProdNewCriticalAlerts",
        "etcdInsufficientMembers",
        "ScrapePoolHasNoTargets",
        "RecordingRulesNoData",
        "PreprodNewCriticalAlerts",  # семейство `<Env>NewCriticalAlerts`
        "Squad7NewCriticalAlerts",
    ],
)
def test_detect_meta_noise_true(alertname: str) -> None:
    assert _detect_meta_noise(_incident(alertname)) is True


@pytest.mark.parametrize(
    "alertname",
    [
        "KubePodCrashLooping",  # реальный сбой — НЕ глушим
        "KubeDeploymentGenerationMismatch",  # это rollout-noise, своя ветка
        "TargetDown",
        "",
    ],
)
def test_detect_meta_noise_false(alertname: str) -> None:
    assert _detect_meta_noise(_incident(alertname)) is False


# ── META_NOISE_ETCD_ENABLED toggle (реальная потеря кворума не должна глохнуть) ─


def test_etcd_muted_by_default() -> None:
    """Default True → etcdInsufficientMembers остаётся meta-noise (scrape-gap)."""
    assert _detect_meta_noise(_incident("etcdInsufficientMembers")) is True


def test_etcd_not_muted_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """META_NOISE_ETCD_ENABLED=False → etcd НЕ глушится (реальный кворум пейджит)."""
    monkeypatch.setattr(
        "app.services.alert_enrichment.settings.META_NOISE_ETCD_ENABLED", False
    )
    assert _detect_meta_noise(_incident("etcdInsufficientMembers")) is False


def test_scrape_gap_still_muted_when_etcd_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Флаг etcd не трогает чистые scrape-gap производные — те глушатся всегда."""
    monkeypatch.setattr(
        "app.services.alert_enrichment.settings.META_NOISE_ETCD_ENABLED", False
    )
    assert _detect_meta_noise(_incident("ScrapePoolHasNoTargets")) is True
    assert _detect_meta_noise(_incident("RecordingRulesNoData")) is True
    # Агрегаты тоже не затронуты.
    assert _detect_meta_noise(_incident("ProdNewCriticalAlerts")) is True


# ── render: приглушённый embed ────────────────────────────────────────────────


async def _capture_payload(ctx: EnrichedContext, env: str = "prod") -> Dict[str, Any]:
    sent: Dict[str, Any] = {}

    async def fake_post(self, url, json=None, **_):
        if "payload" not in sent:
            sent["payload"] = json
        resp = MagicMock()
        resp.status_code = 200
        resp.text = ""
        resp.json = MagicMock(return_value={"id": "snapshot-msg-id"})
        return resp

    from app.services.discord import dedup as dedup_mod

    with dedup_mod._dedup_lock:
        dedup_mod._recent_enriched.clear()

    with patch("app.services.discord_service.settings.DISCORD_DRY_RUN", False), \
         patch(
             "app.services.discord_service.settings.DISCORD_WEBHOOK_URL",
             "https://discord.com/api/webhooks/test/hook",
         ), \
         patch("httpx.AsyncClient.post", new=fake_post):
        await DiscordService().send_enriched_alert([ctx], env=env)
    return sent["payload"]


@pytest.mark.asyncio
async def test_meta_noise_embed_is_muted() -> None:
    """ProdNewCriticalAlerts (critical) → grey + 🔇, без 🚨, без @mention."""
    ctx = EnrichedContext(
        incident=_incident("ProdNewCriticalAlerts"),
        in_kg=False,
        meta_noise=True,
    )
    payload = await _capture_payload(ctx)
    embed = payload["embeds"][0]

    # Видимая карточка дошла (severity не демотился → routed to error).
    assert embed is not None
    # 🔇 вместо 🚨, тег META-AGGREGATE.
    assert embed["title"].startswith("🔇 ")
    assert "🚨" not in embed["title"]
    assert "META-AGGREGATE" in embed["title"]
    # Серый цвет (не красный critical).
    assert embed["color"] == _COLOR_UNKNOWN
    # Без @here/@mention-пинга.
    assert not payload.get("content")


@pytest.mark.asyncio
async def test_real_critical_still_loud() -> None:
    """Контроль: обычный critical (не meta) остаётся 🚨, без META-тега."""
    ctx = EnrichedContext(
        incident=_incident("KubePodCrashLooping"),
        service="clickhouse",
        in_kg=True,
        meta_noise=False,
    )
    payload = await _capture_payload(ctx)
    embed = payload["embeds"][0]
    assert embed["title"].startswith("🚨 ")
    assert "META-AGGREGATE" not in embed["title"]
    assert embed["color"] != _COLOR_UNKNOWN


# ── _detect_cp_down_noise (health-gated, прецедент 19.08.2026) ────────────────
#
# Kube{API,Scheduler,ControllerManager}Down = absent(up{job=...}): фаерится и на
# падении компонента, и на слепоте мониторинга. Подавляем ТОЛЬКО при доказанной
# живости control-plane; «не знаю» оставляет алёрт громким.


@pytest.mark.parametrize(
    "alertname,component",
    [
        ("KubeAPIDown", "apiserver"),
        ("KubeSchedulerDown", "kube-scheduler"),
        ("KubeControllerManagerDown", "kube-controller-manager"),
    ],
)
def test_cp_down_noise_true_when_component_alive(
    alertname: str, component: str
) -> None:
    """Компонент жив → это scrape-gap, глушим пинг (карточка остаётся)."""
    from app.services import alert_enrichment as ae

    with patch(
        "app.context.deployments.control_plane_component_alive", return_value=True
    ) as probe:
        assert ae._detect_cp_down_noise(_incident(alertname)) is True
    assert probe.call_args.args[0] == component


@pytest.mark.parametrize("alive", [False, None])
def test_cp_down_noise_false_when_not_proven_alive(alive: Any) -> None:
    """Компонент мёртв ИЛИ живость неизвестна → alert остаётся громким."""
    from app.services import alert_enrichment as ae

    with patch(
        "app.context.deployments.control_plane_component_alive", return_value=alive
    ):
        assert ae._detect_cp_down_noise(_incident("KubeAPIDown")) is False


def test_cp_down_noise_false_on_probe_exception() -> None:
    """Проба упала — fail-safe loud, а не тихое подавление."""
    from app.services import alert_enrichment as ae

    with patch(
        "app.context.deployments.control_plane_component_alive",
        side_effect=RuntimeError("kube unreachable"),
    ):
        assert ae._detect_cp_down_noise(_incident("KubeAPIDown")) is False


def test_cp_down_noise_respects_kill_switch() -> None:
    """META_NOISE_CP_DOWN_ENABLED=False → не подавляем даже при живом API."""
    from app.services import alert_enrichment as ae

    with patch.object(ae.settings, "META_NOISE_CP_DOWN_ENABLED", False), patch(
        "app.context.deployments.control_plane_component_alive", return_value=True
    ) as probe:
        assert ae._detect_cp_down_noise(_incident("KubeAPIDown")) is False
    probe.assert_not_called()


def test_cp_down_noise_ignores_other_alerts() -> None:
    """Чужие alertname сюда не попадают и пробу не дёргают."""
    from app.services import alert_enrichment as ae

    with patch(
        "app.context.deployments.control_plane_component_alive", return_value=True
    ) as probe:
        assert ae._detect_cp_down_noise(_incident("KubePodCrashLooping")) is False
    probe.assert_not_called()
