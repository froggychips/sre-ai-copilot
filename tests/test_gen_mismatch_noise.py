"""KubeDeploymentGenerationMismatch — условное (health-gated) приглушение.

Прецедент prod-kingdom7/town-service 2026-06-23: generation != observedGeneration
штатно флапает, когда внешний контроллер (Rancher/cattle-cluster-agent дописывает
publicEndpoints-аннотацию) бьёт metadata.generation, а deployment-контроллер на
миг отстаёт — накат при этом давно сошёлся (RESURFACED-флап). НО тот же alertname
сигналит и реальный зависший накат.

Различитель — здоровье реплик: приглушаем (grey + 🔇 GENERATION-CHURN, без
🚨/@mention) ТОЛЬКО при ready==desired (>=1). Любая неоднозначность
(ready<desired, "?/N", None) оставляет alert ГРОМКИМ — fail-safe loud, на проде
лучше лишний пинг, чем проспать зависший накат.
"""
from __future__ import annotations

from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

from app.models.incident import Incident
from app.services.alert_enrichment import (
    EnrichedContext,
    _detect_gen_mismatch_noise,
)
from app.services.discord.service import _COLOR_UNKNOWN
from app.services.discord_service import DiscordService

_STARTS_AT = "2026-06-23T16:20:00+00:00"
_GEN = "KubeDeploymentGenerationMismatch"


def _incident(alertname: str = _GEN, severity: str = "critical") -> Incident:
    return Incident(
        incident_id="fp-gen",
        severity=severity,
        status="firing",
        summary="Deployment generation mismatch",
        description="",
        namespace="prod-kingdom7",
        labels={"alertname": alertname, "deployment": "town-service"},
        annotations={},
        starts_at=_STARTS_AT,
    )


# ── _detect_gen_mismatch_noise ────────────────────────────────────────────────


@pytest.mark.parametrize("rrd", ["1/1", "3/3", "10/10"])
def test_healthy_replicas_muted(rrd: str) -> None:
    """ready==desired (>=1) → доброкачественный churn, приглушаем."""
    assert _detect_gen_mismatch_noise(_incident(), rrd) is True


@pytest.mark.parametrize("rrd", ["0/1", "2/3", "0/3"])
def test_unhealthy_replicas_loud(rrd: str) -> None:
    """ready<desired → реальный зависший накат, оставляем громким."""
    assert _detect_gen_mismatch_noise(_incident(), rrd) is False


@pytest.mark.parametrize("rrd", ["?/1", "?/3", "abc", "1", "", None])
def test_unknown_replicas_failsafe_loud(rrd: Optional[str]) -> None:
    """Нет/нераспарсиваемые данные о репликах → fail-safe loud."""
    assert _detect_gen_mismatch_noise(_incident(), rrd) is False


def test_zero_zero_loud() -> None:
    """0/0 (scaled-to-zero / нет реплик) — не «здоровый накат», громко."""
    assert _detect_gen_mismatch_noise(_incident(), "0/0") is False


@pytest.mark.parametrize(
    "alertname",
    ["KubePodCrashLooping", "ProdNewCriticalAlerts", "KubeDeploymentReplicasMismatch", ""],
)
def test_other_alertnames_never_gen_noise(alertname: str) -> None:
    """Детектор узкий — только KubeDeploymentGenerationMismatch, даже при 1/1."""
    assert _detect_gen_mismatch_noise(_incident(alertname), "1/1") is False


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
async def test_gen_churn_embed_is_muted() -> None:
    """healthy gen-mismatch → grey + 🔇 GENERATION-CHURN, без 🚨, без @mention."""
    ctx = EnrichedContext(
        incident=_incident(),
        service="town-service",
        in_kg=True,
        replicas_ready_desired="1/1",
        gen_mismatch_noise=True,
    )
    payload = await _capture_payload(ctx)
    embed = payload["embeds"][0]

    assert embed["title"].startswith("🔇 ")
    assert "🚨" not in embed["title"]
    assert "GENERATION-CHURN" in embed["title"]
    assert embed["color"] == _COLOR_UNKNOWN
    assert not payload.get("content")  # без @here-пинга


@pytest.mark.asyncio
async def test_failed_rollout_still_loud() -> None:
    """Контроль: тот же alertname при ready<desired (флаг не выставлен) → 🚨."""
    ctx = EnrichedContext(
        incident=_incident(),
        service="town-service",
        in_kg=True,
        replicas_ready_desired="0/1",
        gen_mismatch_noise=False,
    )
    payload = await _capture_payload(ctx)
    embed = payload["embeds"][0]
    assert embed["title"].startswith("🚨 ")
    assert "GENERATION-CHURN" not in embed["title"]
    assert embed["color"] != _COLOR_UNKNOWN
