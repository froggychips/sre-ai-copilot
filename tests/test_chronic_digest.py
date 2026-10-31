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


# ── согласованность с daily-дайджестом ─────────────────────────────────────
#
# «Chronic» встречается в daily-дайджесте трижды с порогами 3/5/10, здесь —
# четвёртый раз со своим порогом из настроек. Пока ни одно из чисел не
# называло свой критерий, сверить их между собой было невозможно.


def test_format_header_states_threshold_and_grouping():
    """Заголовок сам объясняет, что именно посчитано."""
    out = _format([_row(fires=7)], window_hours=24, min_fires=5)
    assert "≥5 fires/24h" in out
    assert "ns+сервис+alertname" in out  # единица группировки


def test_format_header_follows_configured_threshold():
    out = _format([_row(fires=7)], window_hours=6, min_fires=3)
    assert "≥3 fires/6h" in out


def test_threshold_matches_daily_digest_constant():
    """Порог chronic-дайджеста и «chronic» в секции Changes — одно число.

    Оба сообщения летят в #stats, и число в Changes читается как длина
    списка, который придёт этим дайджестом. Разъедутся — дайджесты начнут
    противоречить друг другу.
    """
    from app.config import Settings
    from app.services.stats_digest import CHRONIC_TRACKED_MIN_FIRES

    assert (
        Settings.model_fields["CHRONIC_DIGEST_MIN_FIRES"].default
        == CHRONIC_TRACKED_MIN_FIRES
    )


@pytest.mark.asyncio
async def test_sent_content_states_the_configured_threshold():
    """Порог из настроек доезжает до текста, а не подставляется дефолтом."""
    db = MagicMock()
    with patch("app.services.chronic_digest.settings") as ms, \
         patch("app.services.chronic_digest._aggregate", return_value=[_row(fires=9)]), \
         patch("app.services.discord_service.DiscordService.send_stats_report",
               new_callable=AsyncMock) as mock_send:
        ms.CHRONIC_DIGEST_ENABLED = True
        ms.CHRONIC_DIGEST_WINDOW_HOURS = 12
        ms.CHRONIC_DIGEST_MIN_FIRES = 4
        await send_chronic_digest(db)

    assert "≥4 fires/12h" in mock_send.await_args.args[0]


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
    assert result == {"status": "sent", "rows": 1, "continuous": 0}
    assert mock_send.await_count == 1
    sent_content = mock_send.await_args.args[0]
    assert "bot-service" in sent_content
    assert "42 fires" in sent_content


# ── «горят непрерывно» ─────────────────────────────────────────────────────
#
# Непрерывно горящий алерт — одна строка kg_alerts, в счёт fires он не
# попадает, а его AM-повторы в #infra-error больше не постятся
# (SUPPRESS_REPEAT). Секция держит его в поле зрения.


@pytest.fixture
def sqlite_db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.database import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _alert(db, fp, fired_h, notified_h=1.0, resolved=False, raw=None, svc=None):
    from app.knowledge_graph.schema import AlertEvent

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db.add(AlertEvent(
        alertname=f"A-{fp}",
        severity="warning",
        fingerprint=fp,
        service_id=svc,
        fired_at=now - timedelta(hours=fired_h),
        last_notified_at=now - timedelta(hours=notified_h),
        resolved_at=now if resolved else None,
        raw=raw,
    ))
    db.commit()


def test_continuous_picks_open_long_firing_alerts(sqlite_db):
    from app.knowledge_graph.schema import Service
    from app.services.chronic_digest import _continuous

    svc = Service(name="cnpg", namespace="prod-shared")
    sqlite_db.add(svc)
    sqlite_db.commit()
    _alert(sqlite_db, "days", fired_h=72, svc=svc.id)               # горит 3 суток
    _alert(sqlite_db, "node", fired_h=30, raw={"node": "dev-32"})   # без сервиса
    _alert(sqlite_db, "fresh", fired_h=3)                           # меньше порога
    _alert(sqlite_db, "gone", fired_h=72, resolved=True)            # резолвнут
    _alert(sqlite_db, "phantom", fired_h=72, notified_h=48)         # AM молчит

    rows = _continuous(sqlite_db, 24)

    assert [r["alertname"] for r in rows] == ["A-days", "A-node"]
    assert rows[0]["where"] == "prod-shared/cnpg"
    assert rows[1]["where"] == "dev-32"


def test_format_continuous_section():
    from app.services.chronic_digest import _format_continuous

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    out = _format_continuous([{
        "alertname": "KubeVersionMismatch", "severity": "warning",
        "where": "—", "fired_at": now - timedelta(hours=50),
    }], 24)
    assert "Горят непрерывно > 24 ч" in out
    assert "`KubeVersionMismatch`" in out
    assert "горит 50 ч" in out
    assert _format_continuous([], 24) == ""


@pytest.mark.asyncio
async def test_sends_continuous_even_without_chronic_rows():
    """Хроники нет, но алерт горит сутками — дайджест всё равно уходит."""
    cont = [{
        "alertname": "CnpgDrReplicaMissing", "severity": "warning",
        "where": "prod-shared/cnpg",
        "fired_at": (datetime.now(timezone.utc) - timedelta(hours=40)).replace(tzinfo=None),
    }]
    with patch("app.services.chronic_digest.settings") as ms, \
         patch("app.services.chronic_digest._aggregate", return_value=[]), \
         patch("app.services.chronic_digest._continuous", return_value=cont), \
         patch("app.services.discord_service.DiscordService.send_stats_report",
               new_callable=AsyncMock) as mock_send:
        ms.CHRONIC_DIGEST_ENABLED = True
        ms.CHRONIC_DIGEST_WINDOW_HOURS = 24
        ms.CHRONIC_DIGEST_MIN_FIRES = 5
        ms.STUCK_ALERTS_MIN_DURATION_HOURS = 24
        result = await send_chronic_digest(MagicMock())
    assert result == {"status": "sent", "rows": 0, "continuous": 1}
    content = mock_send.await_args.args[0]
    assert "CnpgDrReplicaMissing" in content
    assert "Chronic alerts" not in content
