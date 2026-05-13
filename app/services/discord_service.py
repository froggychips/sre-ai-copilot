import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.config import settings

# Discord embed colour codes
_COLOR_CRITICAL = 0xE53935   # red
_COLOR_WARNING  = 0xFDD835   # yellow
_COLOR_RESOLVED = 0x43A047   # green
_COLOR_UNKNOWN  = 0x9E9E9E   # grey

_SEVERITY_COLORS = {
    "critical": _COLOR_CRITICAL,
    "warning":  _COLOR_WARNING,
    "info":     _COLOR_UNKNOWN,
}


class DiscordService:
    async def send_report(self, report_text: str):
        if settings.DISCORD_DRY_RUN:
            logging.info("[DISCORD_DRY_RUN] send_report:\n%s", report_text)
            return
        url = settings.DISCORD_WEBHOOK_URL
        if not url:
            logging.warning("DISCORD_WEBHOOK_URL not set, skipping send_report")
            return
        payload = {"content": report_text}
        async with httpx.AsyncClient() as client:
            await client.post(url, json=payload)

    async def send_stats_report(self, content: str) -> None:
        """Отправить текст в канал #stats (cluster health, daily report)."""
        url = settings.DISCORD_WEBHOOK_STATS_URL
        if not url:
            logging.warning("DISCORD_WEBHOOK_STATS_URL not set, skipping stats report")
            return
        if settings.DISCORD_DRY_RUN:
            logging.info("[DISCORD_DRY_RUN] send_stats_report:\n%s", content)
            return
        payload = {"content": content}
        async with httpx.AsyncClient() as client:
            r = await client.post(url, json=payload)
            if r.status_code >= 400:
                logging.error("discord_stats_report_failed", extra={"status": r.status_code})

    async def send_incident_report(
        self,
        incident_id: str,
        alertname: str,
        namespace: str,
        pod: Optional[str],
        service: Optional[str],
        node: Optional[str],
        severity: str,
        cause: Optional[str],
        resolution_quality: str,
        synthesis: str,
        is_recurrence: bool = False,
        flap_count: int = 0,
    ) -> None:
        """Единый embed-отчёт, заменяющий сырой алерт от Spidey Bot.

        Формат: заголовок алерта (что видел Spidey Bot) + root cause +
        краткий вывод пайплайна — всё в одном Discord-сообщении.
        """
        color = (
            _COLOR_RESOLVED if resolution_quality == "resolved"
            else _SEVERITY_COLORS.get(severity.lower(), _COLOR_UNKNOWN)
        )

        status_icon = "✅" if resolution_quality == "resolved" else "⚠️"
        recurrence_tag = " · 🔁 RECURRENCE" if is_recurrence else ""
        flap_tag = f" · 🔄 ×{flap_count}" if flap_count > 0 else ""
        ns_part = f" · {namespace}" if namespace else ""
        title = f"{status_icon} {alertname}{ns_part}{recurrence_tag}{flap_tag}"

        fields = []
        if service:
            fields.append({"name": "Service", "value": f"`{service}`", "inline": True})
        if pod:
            fields.append({"name": "Pod", "value": f"`{pod}`", "inline": True})
        if node and not pod:
            # Node-level alerts (Node* family) don't have pod/namespace context
            fields.append({"name": "Node", "value": f"`{node}`", "inline": True})
        fields.append({
            "name": "Root Cause",
            "value": (cause or "Manual triage required — no hypothesis survived")[:1024],
            "inline": False,
        })

        # Synthesis truncated — Discord limit 4096, но читаемость важнее.
        description = synthesis[:1200] + ("…" if len(synthesis) > 1200 else "")

        payload = {
            "embeds": [{
                "title": title,
                "color": color,
                "fields": fields,
                "description": description,
                "footer": {"text": f"incident/{incident_id}"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }],
            # Кнопки фидбека. 👍 сохраняется сразу; 👎 требует подтверждения
            # (защита от случайного клика — см. discord_interactions.py).
            "components": [{
                "type": 1,  # ACTION_ROW
                "components": [
                    {
                        "type": 2, "style": 3,  # BUTTON SUCCESS (green)
                        "label": "👍 Верный анализ",
                        "custom_id": f"feedback_pos_{incident_id}",
                    },
                    {
                        "type": 2, "style": 4,  # BUTTON DANGER (red)
                        "label": "👎 Анализ неверен",
                        "custom_id": f"feedback_neg_{incident_id}",
                    },
                ],
            }],
        }

        if settings.DISCORD_DRY_RUN:
            logging.info("[DISCORD_DRY_RUN] send_incident_report: %s | cause=%s | rq=%s",
                         title, cause, resolution_quality)
            return
        url = settings.DISCORD_WEBHOOK_URL
        if not url:
            logging.warning("DISCORD_WEBHOOK_URL not set, skipping incident report")
            return
        async with httpx.AsyncClient() as client:
            r = await client.post(url, json=payload)
            if r.status_code >= 400:
                logging.error("discord_incident_report_failed", extra={"status": r.status_code})

    async def send_approval_request(self, approval_id: str, details: dict):
        """Отправляет Rich Embed с информацией об опасной операции и ссылками для подтверждения."""
        payload = {
            "embeds": [
                {
                    "title": "⚠️ ACTION APPROVAL REQUIRED",
                    "description": "AI Agent requested a high-risk operation on Kubernetes.",
                    "color": 16711680,
                    "fields": [
                        {
                            "name": "Command",
                            "value": f"`{details.get('command')}`",
                            "inline": False,
                        },
                        {
                            "name": "Risk Level",
                            "value": details.get("risk", "HIGH"),
                            "inline": True,
                        },
                        {"name": "Approval ID", "value": approval_id, "inline": True},
                        {
                            "name": "Action",
                            "value": f"[APPROVE](https://your-api.com/approvals/{approval_id}/approve) | [REJECT](https://your-api.com/approvals/{approval_id}/reject)",
                            "inline": False,
                        },
                    ],
                }
            ]
        }
        url = settings.DISCORD_WEBHOOK_URL
        if not url:
            logging.warning("DISCORD_WEBHOOK_URL not set, skipping approval request")
            return
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload)
            if response.status_code >= 400:
                logging.error(
                    "discord_approval_request_failed",
                    extra={"status_code": response.status_code},
                )


discord_service = DiscordService()
