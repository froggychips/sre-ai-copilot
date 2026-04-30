import httpx
from app.config import settings
import logging

class DiscordService:
    async def send_report(self, report_text: str):
        payload = {"content": report_text}
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(settings.DISCORD_WEBHOOK_URL, json=payload)
                response.raise_for_status()
            except Exception as e:
                logging.error(f"Failed to send discord notification: {e}")

discord_service = DiscordService()
