import httpx
from app.config import settings
import logging

class DiscordService:
    async def send_report(self, report_text: str):
        # ... существующий код ...

    async def send_approval_request(self, approval_id: str, details: dict):
        """
        Отправляет Rich Embed с информацией об опасной операции и ссылками для подтверждения.
        """
        payload = {
            "embeds": [{
                "title": "⚠️ ACTION APPROVAL REQUIRED",
                "description": f"AI Agent requested a high-risk operation on Kubernetes.",
                "color": 16711680, # Red
                "fields": [
                    {"name": "Command", "value": f"`{details.get('command')}`", "inline": False},
                    {"name": "Risk Level", "value": details.get("risk", "HIGH"), "inline": True},
                    {"name": "Approval ID", "value": approval_id, "inline": True},
                    {"name": "Action", "value": f"[APPROVE](https://your-api.com/approvals/{approval_id}/approve) | [REJECT](https://your-api.com/approvals/{approval_id}/reject)", "inline": False}
                ]
            }]
        }
        async with httpx.AsyncClient() as client:
            await client.post(settings.DISCORD_WEBHOOK_URL, json=payload)
