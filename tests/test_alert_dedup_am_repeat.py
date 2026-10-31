"""Повтор горящего алерта от Alertmanager ≠ возврат после затишья.

Инцидент 31.10.2026 11:52 UTC: в #infra-error пачкой пришли 7 карточек
«🌀 RESURFACED» (NodeDiskIOSaturation dev-32, TooManyScrapeErrors, TooManyLogs,
ScrapePoolHasNoTargets, CnpgDrReplicaMissing, KubeVersionMismatch,
DevNodeDiskPressureImminent dev-4). Все горели непрерывно днями — AM просто
повторил группу через repeat_interval (4 ч), а L2 принял «4 ч без fire» за
quiet >2h.

Повтор отличается от возврата эпизодом: строка kg_alerts по fingerprint
открыта и эпизод (fired_at в KG и startsAt из AM) начался ДО прошлого раза,
когда ключ видели.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.knowledge_graph.schema import AlertEvent
from app.metrics import ALERTS_SUPPRESSED
from app.services.alert_dedup import Decision, decide_send

_ALERT = "NodeDiskIOSaturation"
_SVC = "node-exporter"
_KEY_CNT = f"enrich:lastsent:{_ALERT}:{_SVC}:cnt"
_KEY_LAST = f"enrich:lastsent:{_ALERT}:{_SVC}:last"


@pytest.fixture
def fake_redis():
    store: dict[str, str] = {}

    class FakeRedis:
        async def get(self, key):
            await asyncio.sleep(0)
            return store.get(key)

        async def set(self, key, value, ex=None, nx=False):
            await asyncio.sleep(0)
            if nx and key in store:
                return None
            store[key] = str(value)
            return True

        async def incr(self, key):
            new = int(store.get(key, "0")) + 1
            store[key] = str(new)
            return new

        async def decr(self, key):
            new = int(store.get(key, "0")) - 1
            store[key] = str(new)
            return new

        async def delete(self, key):
            store.pop(key, None)

    with patch("app.services.alert_dedup._get_client", return_value=FakeRedis()):
        yield store


@pytest.fixture
def db():
    # Проверка идёт через asyncio.to_thread, а sqlite in-memory — это одно
    # соединение: StaticPool отдаёт его и из рабочего потока.
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(db, fp: str, fired_at: datetime, resolved_at=None, notified=None):
    ev = AlertEvent(
        alertname=_ALERT,
        severity="warning",
        fingerprint=fp,
        fired_at=fired_at.replace(tzinfo=None),
        last_notified_at=(notified or fired_at).replace(tzinfo=None),
        resolved_at=resolved_at.replace(tzinfo=None) if resolved_at else None,
    )
    db.add(ev)
    db.commit()
    return ev


def _seen(store, ts: datetime, count: int = 1):
    store[_KEY_LAST] = str(int(ts.timestamp()))
    store[_KEY_CNT] = str(count)


def _suppressed_repeat() -> float:
    return ALERTS_SUPPRESSED.labels(reason="repeat", alertname=_ALERT)._value.get()


async def _decide(db, now, episodes, severity="warning"):
    return await decide_send(
        _ALERT, "monitoring", _SVC, severity, db, fire_at=now, episodes=episodes,
    )


# ── решение ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_continuous_repeat_after_4h_is_suppressed(fake_redis, db, now):
    """Горит 10 ч, карточку видели 4 ч назад, AM повторил → SUPPRESS_REPEAT."""
    started = now - timedelta(hours=10)
    _row(db, "fp-dev32", started)
    _seen(fake_redis, now - timedelta(hours=4))
    before = _suppressed_repeat()

    d = await _decide(db, now, [("fp-dev32", _iso(started))])

    assert d == Decision.SUPPRESS_REPEAT
    assert _suppressed_repeat() == before + 1
    # «Последний раз видели» сдвинут → следующий повтор через 4 ч снова
    # повтор, а счётчик хроники не тронут (карточки не было).
    assert fake_redis[_KEY_LAST] == str(int(now.timestamp()))
    assert fake_redis[_KEY_CNT] == "1"


@pytest.mark.asyncio
async def test_suppress_repeat_logs_alertname_and_fingerprint(fake_redis, db, now):
    started = now - timedelta(days=3)
    _row(db, "fp-kvm", started)
    _seen(fake_redis, now - timedelta(hours=4))

    with patch("app.services.alert_dedup.log") as log:
        d = await _decide(db, now, [("fp-kvm", _iso(started))])

    assert d == Decision.SUPPRESS_REPEAT
    events = {c.args[0]: c.kwargs for c in log.info.call_args_list}
    assert events["alert_dedup.suppress_repeat"]["alertname"] == _ALERT
    assert events["alert_dedup.suppress_repeat"]["fingerprints"] == ["fp-kvm"]


@pytest.mark.asyncio
async def test_resolve_then_refire_after_3h_is_resurfaced(fake_redis, db, now):
    """Был resolve, через 3 ч снова горит → SEND_RESURFACED.

    populate_from_incident на re-fire резолвнутой строки сбрасывает
    resolved_at и берёт новый fired_at — его и видит decide_send.
    """
    refired = now - timedelta(minutes=2)
    _row(db, "fp-disk", refired)
    _seen(fake_redis, now - timedelta(hours=3))

    d = await _decide(db, now, [("fp-disk", _iso(refired))])

    assert d == Decision.SEND_RESURFACED


@pytest.mark.asyncio
async def test_missed_resolve_but_new_starts_at_is_resurfaced(fake_redis, db, now):
    """resolve-sync не успел закрыть строку (fired_at старый), но AM прислал
    новый эпизод — startsAt позже прошлого раза → это возврат."""
    _row(db, "fp-scrape", now - timedelta(hours=10))
    _seen(fake_redis, now - timedelta(hours=3))

    d = await _decide(db, now, [("fp-scrape", _iso(now - timedelta(minutes=1)))])

    assert d == Decision.SEND_RESURFACED


@pytest.mark.asyncio
async def test_resolved_row_is_resurfaced(fake_redis, db, now):
    started = now - timedelta(hours=10)
    _row(db, "fp-res", started, resolved_at=now - timedelta(hours=1))
    _seen(fake_redis, now - timedelta(hours=4))

    d = await _decide(db, now, [("fp-res", _iso(started))])

    assert d == Decision.SEND_RESURFACED


@pytest.mark.asyncio
async def test_unknown_fingerprint_is_resurfaced(fake_redis, db, now):
    """Строки в KG нет (populate упал) — при сомнении шлём."""
    _seen(fake_redis, now - timedelta(hours=4))

    d = await _decide(db, now, [("fp-none", _iso(now - timedelta(hours=10)))])

    assert d == Decision.SEND_RESURFACED


@pytest.mark.asyncio
async def test_group_with_new_episode_is_resurfaced(fake_redis, db, now):
    """В группе один горящий и один новый fingerprint — карточка нужна."""
    old = now - timedelta(hours=10)
    new = now - timedelta(minutes=3)
    _row(db, "fp-old", old)
    _row(db, "fp-new", new)
    _seen(fake_redis, now - timedelta(hours=4))

    d = await _decide(db, now, [("fp-old", _iso(old)), ("fp-new", _iso(new))])

    assert d == Decision.SEND_RESURFACED


@pytest.mark.asyncio
async def test_first_fire_is_send(fake_redis, db, now):
    started = now - timedelta(minutes=1)
    _row(db, "fp-first", started)

    d = await _decide(db, now, [("fp-first", _iso(started))])

    assert d == Decision.SEND


@pytest.mark.asyncio
async def test_flag_off_restores_1_0_30_behaviour(fake_redis, db, now, monkeypatch):
    from app.services import alert_dedup

    monkeypatch.setattr(alert_dedup.settings, "ALERT_SUPPRESS_AM_REPEATS", False)
    started = now - timedelta(hours=10)
    _row(db, "fp-flag", started)
    _seen(fake_redis, now - timedelta(hours=4))

    d = await _decide(db, now, [("fp-flag", _iso(started))])

    assert d == Decision.SEND_RESURFACED


@pytest.mark.asyncio
async def test_without_episodes_behaviour_unchanged(fake_redis, db, now):
    """Caller без episodes (старые вызовы) — прежний L2."""
    _seen(fake_redis, now - timedelta(hours=4))

    d = await decide_send(_ALERT, "monitoring", _SVC, "warning", db, fire_at=now)

    assert d == Decision.SEND_RESURFACED


@pytest.mark.asyncio
async def test_critical_repeat_stays_reminder_without_resurfaced_mark(
    fake_redis, db, now,
):
    """Critical до фикса приходил каждые 4 ч — напоминание сохраняем, но
    обычной карточкой (SEND), без ложного 🌀: алерт не возвращался."""
    started = now - timedelta(hours=10)
    _row(db, "fp-crit", started)
    _seen(fake_redis, now - timedelta(hours=4), count=2)

    d = await _decide(db, now, [("fp-crit", _iso(started))], severity="critical")

    assert d == Decision.SEND
    # Окно заякорено как у resurface: откат недоставки снимет ровно этот fire.
    assert fake_redis[_KEY_CNT] == "1"


@pytest.mark.asyncio
async def test_kg_failure_falls_back_to_resurfaced(fake_redis, now):
    """Сломанная БД не глушит карточку: fail-open в сторону отправки."""
    db = MagicMock()
    db.query.side_effect = RuntimeError("pg down")
    _seen(fake_redis, now - timedelta(hours=4))

    d = await _decide(db, now, [("fp-x", _iso(now - timedelta(hours=10)))])

    assert d == Decision.SEND_RESURFACED


@pytest.mark.asyncio
async def test_in_window_path_untouched(fake_redis, db, now):
    """Fire в пределах 2 ч — L2a не участвует, обычный счётчик."""
    started = now - timedelta(hours=10)
    _row(db, "fp-win", started)
    _seen(fake_redis, now - timedelta(minutes=30))

    d = await _decide(db, now, [("fp-win", _iso(started))])

    assert d == Decision.SEND
    assert fake_redis[_KEY_CNT] == "2"


# ── хендлер enrich-and-forward ───────────────────────────────────────


def _payload(fp: str, starts_at: str, severity: str = "warning"):
    from app.models.incident import AlertManagerWebhook

    return AlertManagerWebhook(
        version="4",
        groupKey=f"grp-{fp}",
        status="firing",
        alerts=[{
            "status": "firing",
            "labels": {
                "alertname": _ALERT,
                "severity": severity,
                "namespace": "monitoring",
                "service": _SVC,
            },
            "annotations": {"summary": "s", "description": "d"},
            "startsAt": starts_at,
            "endsAt": None,
            "generatorURL": "https://prom.local",
            "fingerprint": fp,
        }],
    )


@pytest.mark.asyncio
async def test_handler_repeat_posts_no_card_and_touches_kg(
    fake_redis, db, now, monkeypatch,
):
    import app.api.webhooks as webhooks
    import app.knowledge_graph.auto_populator as populator_mod
    import app.services.alert_enrichment as enrichment_mod
    import app.services.discord_service as discord_mod

    started = now - timedelta(days=2)
    _row(db, "fp-handler", started, notified=now - timedelta(hours=4))
    _seen(fake_redis, now - timedelta(hours=4))

    def _populate(session, inc):
        # Как record_alert_event на открытой строке: last_notified_at = now,
        # fired_at/resolved_at не трогаются.
        row = session.query(AlertEvent).filter_by(fingerprint=inc.incident_id).one()
        row.last_notified_at = datetime.now(timezone.utc).replace(tzinfo=None)
        session.flush()
        return {}

    sent: list = []

    async def _send(self, ctxs, env=None, resurfaced=False):
        sent.append(resurfaced)

    monkeypatch.setattr(webhooks.settings, "DISCORD_ENRICH_ENABLED", True)
    monkeypatch.setattr(populator_mod, "populate_from_incident", _populate)
    monkeypatch.setattr(enrichment_mod, "enrich_alert", lambda s, inc: MagicMock())
    monkeypatch.setattr(enrichment_mod, "resolve_store_service",
                        lambda labels, legacy_default=None, db=None: legacy_default)
    monkeypatch.setattr(discord_mod.DiscordService, "send_enriched_alert", _send)

    res = await webhooks.alertmanager_webhook_enrich_and_forward(
        _payload("fp-handler", _iso(started)), db=db,
    )

    assert sent == [], "повтор горящего алерта не постит карточку"
    assert res["suppressed_repeat"] == 1
    assert res["enriched_groups"] == 0
    row = db.query(AlertEvent).filter_by(fingerprint="fp-handler").one()
    assert row.last_notified_at >= (now - timedelta(minutes=1)).replace(tzinfo=None)
    assert row.resolved_at is None
