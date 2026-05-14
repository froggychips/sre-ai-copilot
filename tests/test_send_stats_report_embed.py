"""send_stats_report теперь оборачивает content в Discord embed.

Daily digest ~2800 chars > 2000 (Discord content-limit) → embed.description
(limit 4096) обходит проблему. Этот тест проверяет:
  - payload содержит embeds (не content)
  - title вытащен из первой строки
  - description обрезается на 4000 chars (с trailing маркером)
  - DISCORD_DRY_RUN → POST не вызывается
  - missing webhook URL → POST не вызывается
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.discord_service import DiscordService


@pytest.mark.asyncio
async def test_send_stats_report_wraps_in_embed_with_title():
    """Первая строка → embed.title; остаток → embed.description."""
    fake_client = MagicMock()
    fake_client.post = AsyncMock(return_value=MagicMock(status_code=200, text=""))
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch("app.services.discord_service.settings") as mock_settings, \
         patch("app.services.discord_service.httpx.AsyncClient", return_value=fake_client):
        mock_settings.DISCORD_WEBHOOK_STATS_URL = "https://discord.com/api/webhooks/x/y"
        mock_settings.DISCORD_DRY_RUN = False
        svc = DiscordService()
        await svc.send_stats_report(
            "📊 **Cluster Daily Digest** · 2026-05-15 09:00 UTC\n\n"
            "**🛡️ Cluster Health**\n  Nodes: 16, Crashloops: 9"
        )

    fake_client.post.assert_awaited_once()
    payload = fake_client.post.call_args.kwargs["json"]
    assert "embeds" in payload
    assert "content" not in payload
    embed = payload["embeds"][0]
    assert "Cluster Daily Digest" in embed["title"]
    assert "Cluster Health" in embed["description"]
    assert "color" in embed


@pytest.mark.asyncio
async def test_send_stats_report_truncates_oversized_description():
    """description > 4000 chars → обрезается + маркер '…truncated'."""
    fake_client = MagicMock()
    fake_client.post = AsyncMock(return_value=MagicMock(status_code=200, text=""))
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    long_content = "Title line\n" + ("X" * 5000)
    with patch("app.services.discord_service.settings") as mock_settings, \
         patch("app.services.discord_service.httpx.AsyncClient", return_value=fake_client):
        mock_settings.DISCORD_WEBHOOK_STATS_URL = "https://x"
        mock_settings.DISCORD_DRY_RUN = False
        svc = DiscordService()
        await svc.send_stats_report(long_content)

    payload = fake_client.post.call_args.kwargs["json"]
    desc = payload["embeds"][0]["description"]
    # 3990 (truncate point) + "\n_…truncated_" (~13 chars) ≈ 4003. Под лимит 4096 точно.
    assert len(desc) <= 4096
    assert "_…truncated_" in desc


@pytest.mark.asyncio
async def test_send_stats_report_no_url_skips_post():
    with patch("app.services.discord_service.settings") as mock_settings, \
         patch("app.services.discord_service.httpx.AsyncClient") as mock_client_cls:
        mock_settings.DISCORD_WEBHOOK_STATS_URL = None
        mock_settings.DISCORD_DRY_RUN = False
        svc = DiscordService()
        await svc.send_stats_report("any content")
    mock_client_cls.assert_not_called()


@pytest.mark.asyncio
async def test_send_stats_report_dry_run_skips_post():
    """DISCORD_DRY_RUN=True → POST не вызывается, только log.info."""
    with patch("app.services.discord_service.settings") as mock_settings, \
         patch("app.services.discord_service.httpx.AsyncClient") as mock_client_cls:
        mock_settings.DISCORD_WEBHOOK_STATS_URL = "https://x"
        mock_settings.DISCORD_DRY_RUN = True
        svc = DiscordService()
        await svc.send_stats_report("Test content")
    mock_client_cls.assert_not_called()


@pytest.mark.asyncio
async def test_send_stats_report_single_line_uses_default_title():
    """Single-line content без новой строки → default title 'Stats digest'."""
    fake_client = MagicMock()
    fake_client.post = AsyncMock(return_value=MagicMock(status_code=200, text=""))
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch("app.services.discord_service.settings") as mock_settings, \
         patch("app.services.discord_service.httpx.AsyncClient", return_value=fake_client):
        mock_settings.DISCORD_WEBHOOK_STATS_URL = "https://x"
        mock_settings.DISCORD_DRY_RUN = False
        svc = DiscordService()
        await svc.send_stats_report("оne-liner content без переносов")

    payload = fake_client.post.call_args.kwargs["json"]
    assert payload["embeds"][0]["title"] == "Stats digest"
