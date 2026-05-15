"""Тесты на app.services.chronic_digest (L5).

Покрывает:
  - Если CHRONIC_DIGEST_ENABLED=False → skipped, send не вызван
  - Пустая БД → status=empty, send не вызван
  - С данными (3 сервиса × >=5 fires) → markdown + send_stats_report 1 вызов
  - Формат markdown содержит fires count + firing-duration
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.chronic_digest import _format, send_chronic_digest


def _row(svc="bot-service", ns="preprod-kingdom1", fires=12, hours_ago=24, last_min_ago=5):
    now = datetime.now(timezone.utc)
    return {
        "namespace": ns,
        "service": svc,
        "alertname": "KubePodCrashLooping",
        "fires": fires,
        "first_fired": (now - timedelta(hours=hours_ago)).replace(tzinfo=None),
        "last_fired": (now - timedelta(minutes=last_min_ago)).replace(tzinfo=None),
    }


def test_format_returns_empty_when_no_rows():
    assert _format([], 24) == ""


def test_format_includes_fires_and_firing_duration():
    rows = [_row(fires=12, hours_ago=24)]
    out = _format(rows, window_hours=24)
    assert "bot-service" in out
    assert "**12 fires**" in out
    assert "firing 24h" in out
    assert "Chronic alerts" in out


def test_format_truncates_at_15_rows_with_marker():
    rows = [_row(svc=f"svc-{i}") for i in range(20)]
    out = _format(rows, 24)
    assert "(+5 ещё)" in out


@pytest.mark.asyncio
async def test_skipped_when_disabled():
    db = MagicMock()
    with patch("app.services.chronic_digest.settings") as ms:
        ms.CHRONIC_DIGEST_ENABLED = False
        result = await send_chronic_digest(db)
    assert result == {"status": "skipped", "reason": "CHRONIC_DIGEST_ENABLED=false"}


@pytest.mark.asyncio
async def test_empty_when_no_data():
    db = MagicMock()
    with patch("app.services.chronic_digest.settings") as ms, \
         patch("app.services.chronic_digest._aggregate", return_value=[]), \
         patch("app.services.discord_service.DiscordService.send_stats_report",
               new_callable=AsyncMock) as mock_send:
        ms.CHRONIC_DIGEST_ENABLED = True
        ms.CHRONIC_DIGEST_WINDOW_HOURS = 24
        ms.CHRONIC_DIGEST_MIN_FIRES = 5
        result = await send_chronic_digest(db)
    assert result["status"] == "empty"
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_sends_to_stats_channel_with_data():
    db = MagicMock()
    rows = [_row(svc="bot-service", fires=42, hours_ago=24, last_min_ago=15)]
    with patch("app.services.chronic_digest.settings") as ms, \
         patch("app.services.chronic_digest._aggregate", return_value=rows), \
         patch("app.services.discord_service.DiscordService.send_stats_report",
               new_callable=AsyncMock) as mock_send:
        ms.CHRONIC_DIGEST_ENABLED = True
        ms.CHRONIC_DIGEST_WINDOW_HOURS = 24
        ms.CHRONIC_DIGEST_MIN_FIRES = 5
        result = await send_chronic_digest(db)
    assert result == {"status": "sent", "rows": 1}
    assert mock_send.await_count == 1
    sent_content = mock_send.await_args.args[0]
    assert "bot-service" in sent_content
    assert "42 fires" in sent_content
