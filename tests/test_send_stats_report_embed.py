"""send_stats_report теперь оборачивает content в Discord embed.

Daily digest ~2800 chars > 2000 (Discord content-limit) → embed.description
(limit 4096) обходит проблему. Этот тест проверяет:
  - payload содержит embeds (не content)
  - title вытащен из первой строки
  - description обрезается до 4000 chars — ПО СЕКЦИЯМ, с сохранением
    критичного хвоста (самодиагностика / kg_quality / heartbeats)
  - DISCORD_DRY_RUN → POST не вызывается
  - missing webhook URL → POST не вызывается

Плюс контракт возврата bool (по нему stats_digest решает, писать ли
deadman-маркер — раньше маркер писался и при недоставке):
  - 2xx → True, HTTP>=400 → False, нет URL → False, DRY_RUN → True.
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
        delivered = await svc.send_stats_report(
            "📊 **Cluster Daily Digest** · 2026-05-15 09:00 UTC\n\n"
            "**🛡️ Cluster Health**\n  Nodes: 16, Crashloops: 9"
        )

    assert delivered is True  # 2xx → доставлено
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
    """Нет вебхука → POST не вызывается, возврат False (маркер НЕ пишем)."""
    with patch("app.services.discord_service.settings") as mock_settings, \
         patch("app.services.discord_service.httpx.AsyncClient") as mock_client_cls:
        mock_settings.DISCORD_WEBHOOK_STATS_URL = None
        mock_settings.DISCORD_DRY_RUN = False
        svc = DiscordService()
        delivered = await svc.send_stats_report("any content")
    assert delivered is False
    mock_client_cls.assert_not_called()


@pytest.mark.asyncio
async def test_send_stats_report_dry_run_skips_post():
    """DISCORD_DRY_RUN=True → POST не вызывается, но возврат True.

    Dry-run — намеренное подавление доставки: digest-цикл должен считаться
    успешным (deadman-маркер пишется), иначе dry-run стенды шумели бы
    ложным «digest не дошёл».
    """
    with patch("app.services.discord_service.settings") as mock_settings, \
         patch("app.services.discord_service.httpx.AsyncClient") as mock_client_cls:
        mock_settings.DISCORD_WEBHOOK_STATS_URL = "https://x"
        mock_settings.DISCORD_DRY_RUN = True
        svc = DiscordService()
        delivered = await svc.send_stats_report("Test content")
    assert delivered is True
    mock_client_cls.assert_not_called()


@pytest.mark.asyncio
async def test_send_stats_report_returns_false_on_http_error():
    """HTTP>=400 → False: раньше error только логировался, вызывающая
    сторона не отличала доставку от фейла и писала deadman-маркер."""
    fake_client = MagicMock()
    fake_client.post = AsyncMock(
        return_value=MagicMock(status_code=400, text="Invalid Webhook Token", headers={})
    )
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch("app.services.discord_service.settings") as mock_settings, \
         patch("app.services.discord_service.httpx.AsyncClient", return_value=fake_client):
        mock_settings.DISCORD_WEBHOOK_STATS_URL = "https://x"
        mock_settings.DISCORD_DRY_RUN = False
        svc = DiscordService()
        delivered = await svc.send_stats_report("Title\nbody")

    assert delivered is False
    fake_client.post.assert_awaited_once()  # 400 не ретраится


# ── обрезка по секциям: критичный хвост обязан выживать ─────────────────────
#
# Дайджест из ~20 секций регулярно перерастает лимит description, а слепой
# `description[:3990]` рубил именно хвост: сначала предупреждение «Секции
# недоступны» (ради которого механизм самодиагностики и делался после
# инцидента 07.08.2026), затем kg_quality и footer с heartbeat'ами синков.
# То есть первым исчезало ровно то, по чему читатель понимает, можно ли
# верить остальному тексту.

_FAILURES_LINE = (
    "⚠️ **Секции недоступны (2):** `kg_quality_section`, `mttr_section` — "
    "данные ниже неполные"
)
_KG_QUALITY = (
    "**🧬 KG quality**\n  Services: `5799` · Orphan: `47`/`302` (15%)\n"
    "  Edges: `4773` (calls=36, uses_nats=4737)"
)
_SYNCS = "_Syncs: metrics 14:45 · cluster 14:30 · topology 12:17 · 4/4 active_"


_DIGEST_TITLE = "📊 **Cluster Daily Digest** · 2026-08-10 09:00 UTC"


def _oversized_description(*, failures_first: bool) -> str:
    """То, что реально уходит в description: 20 «толстых» секций без title.

    Первая строка контента у `send_stats_report` становится embed.title и в
    description не попадает — поэтому в норме description НАЧИНАЕТСЯ строкой
    самодиагностики.
    """
    body = [
        f"**Секция {i}**\n" + "\n".join(f"  • строка {j} " + "x" * 60
                                        for j in range(5))
        for i in range(20)
    ]
    if failures_first:
        return "\n\n".join([_FAILURES_LINE] + body + [_KG_QUALITY, _SYNCS])
    # Легаси-порядок (жалоба в самом конце) — обрезка обязана спасти и его.
    return "\n\n".join(body + [_KG_QUALITY, _SYNCS, _FAILURES_LINE])


@pytest.mark.parametrize("failures_first", [True, False])
def test_truncate_keeps_self_diagnostics_and_tail(failures_first):
    from app.services.discord.service import (_STATS_DESC_LIMIT,
                                              _truncate_stats_description)

    raw = _oversized_description(failures_first=failures_first)
    assert len(raw) > _STATS_DESC_LIMIT  # стенд действительно переполнен

    out = _truncate_stats_description(raw)

    assert len(out) <= _STATS_DESC_LIMIT
    assert "Секции недоступны" in out, "самодиагностика съедена обрезкой"
    assert "🧬 KG quality" in out
    assert "_Syncs:" in out
    # Читатель должен видеть, что дайджест укорочен, а не «в кластере тихо».
    assert "вырезано секций" in out
    # Голова тоже частично сохранена — не только хвост.
    assert "**Секция 0**" in out
    if failures_first:
        # Порядок чтения сохранён: предупреждение по-прежнему первым блоком.
        assert out.startswith("⚠️ **Секции недоступны")


def test_truncate_leaves_short_digest_untouched():
    from app.services.discord.service import _truncate_stats_description

    short = "**A**\n  line\n\n**B**\n  line"
    assert _truncate_stats_description(short) == short


def test_truncate_cuts_single_oversized_block_with_marker():
    """Одна гигантская секция без хвоста — режем её саму, а не дропаем."""
    from app.services.discord.service import (_STATS_DESC_LIMIT,
                                              _truncate_stats_description)

    out = _truncate_stats_description("**Big**\n" + "X" * 9000)
    assert len(out) <= _STATS_DESC_LIMIT
    assert "_…truncated_" in out
    assert out.startswith("**Big**")


@pytest.mark.asyncio
async def test_send_stats_report_preserves_failures_line_in_payload():
    """Сквозной путь: то, что уходит в Discord, содержит предупреждение."""
    fake_client = MagicMock()
    fake_client.post = AsyncMock(return_value=MagicMock(status_code=200, text=""))
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch("app.services.discord_service.settings") as mock_settings, \
         patch("app.services.discord_service.httpx.AsyncClient", return_value=fake_client):
        mock_settings.DISCORD_WEBHOOK_STATS_URL = "https://x"
        mock_settings.DISCORD_DRY_RUN = False
        svc = DiscordService()
        await svc.send_stats_report(
            _DIGEST_TITLE + "\n\n" + _oversized_description(failures_first=True)
        )

    payload = fake_client.post.call_args.kwargs["json"]
    assert "Cluster Daily Digest" in payload["embeds"][0]["title"]
    desc = payload["embeds"][0]["description"]
    assert len(desc) <= 4096
    # Строка самодиагностики — первым, что читатель видит под заголовком.
    assert desc.startswith("⚠️ **Секции недоступны")
    assert "🧬 KG quality" in desc
    assert "_Syncs:" in desc


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
