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
