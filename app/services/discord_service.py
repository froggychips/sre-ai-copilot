import logging

import httpx

from app.config import settings


class DiscordService:
    async def send_report(self, report_text: str):
        payload = {"content": report_text}
        async with httpx.AsyncClient() as client:
            await client.post(settings.DISCORD_WEBHOOK_URL, json=payload)

    async def send_approval_request(self, approval_id: str, details: dict):
        """Отправляет Rich Embed с информацией об опасной операции и ссылками для подтверждения."""
        payload = {
            "embeds": [{
                "title": "⚠️ ACTION APPROVAL REQUIRED",
                "description": "AI Agent requested a high-risk operation on Kubernetes.",
                "color": 16711680,
                "fields": [
                    {"name": "Command", "value": f"`{details.get('command')}`", "inline": False},
                    {"name": "Risk Level", "value": details.get("risk", "HIGH"), "inline": True},
                    {"name": "Approval ID", "value": approval_id, "inline": True},
                    {"name": "Action", "value": f"[APPROVE](https://your-api.com/approvals/{approval_id}/approve) | [REJECT](https://your-api.com/approvals/{approval_id}/reject)", "inline": False},
                ],
            }]
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(settings.DISCORD_WEBHOOK_URL, json=payload)
            if response.status_code >= 400:
                logging.error("discord_approval_request_failed", extra={"status_code": response.status_code})


discord_service = DiscordService()
