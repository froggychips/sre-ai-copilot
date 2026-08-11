"""External probe: транзакционная дисциплина, порядок Discord, отбор целей, лок.

Класс инцидента 08–10.08.2026 (idle-in-transaction). До правки прогон выглядел
так: SELECT целей открывал транзакцию, дальше весь последовательный probe-цикл
(DNS+TCP+HTTPS, таймаут 5с × N хостов) шёл без единого SQL. На ~25+ хостах
соединение убивал idle_in_transaction_session_timeout=120s
(app/database.py) → commit падал, state (consecutive_failures/firing) и
AlertEvent откатывались, а Discord-embed'ы к тому моменту УЖЕ улетели. Итог:
каждый следующий тик слал те же алерты заново и не мог их зарезолвить.

Здесь закреплены четыре свойства нового цикла:
  * probe идёт без открытой транзакции (проверяем `Session.in_transaction()`
    в момент probe — как guard на порядок операций в
    test_idle_transaction_guard);
  * Discord-отправка строго ПОСЛЕ commit результатов;
  * `ingress:<resource-name>` от sync_topology_resources не считается
    hostname'ом (M2 ревью) — скип с логом, без ложного ExternalProbeDown;
  * второй прогон при живом первом скипается по redis-локу.

Всё на in-memory SQLite; сеть и Discord замоканы.
"""
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.knowledge_graph import external_probe_sync as eps
from app.knowledge_graph.populator import upsert_service
from app.knowledge_graph.schema import AlertEvent


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


class _FakeRedis:
    """Минимум операций, которые использует лок: SET NX EX / GET / DEL."""

    def __init__(self) -> None:
        self.store: Dict[str, Any] = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)
        return 1


class _FakeDiscord:
    """Собирает отправки; порядок относительно commit проверяем через `order`."""

    def __init__(self, order: Optional[List[str]] = None) -> None:
        self.sent: List[Dict[str, Any]] = []
        self._order = order

    async def send_external_probe_alert(self, *, host, status, snapshot, resolved):
        self.sent.append(
            {"host": host, "status": status, "resolved": resolved, "snapshot": snapshot},
        )
        if self._order is not None:
            self._order.append(f"discord:{host}:{'resolved' if resolved else 'fired'}")


def _probe_stub(status: str, order: Optional[List[str]] = None, seen=None):
    """Подмена `_probe_endpoint`: без сети, с фиксированным статусом."""

    async def _fake(host: str, timeout: float) -> Dict[str, Any]:
        if order is not None:
            order.append(f"probe:{host}")
        if seen is not None:
            seen.append(host)
        return {
            "host": host,
            "last_at": "2026-08-10T12:00:00Z",
            "ips": ["10.0.0.1"] if status != "down" else [],
            "tcp_results": [],
            "http_result": {},
            "dns_error": None,
            "status": status,
        }

    return _fake


def _enabled(threshold: int = 1):
    """Контекст: фича включена, порог алерта опущен до `threshold`."""
    return (
        patch.object(settings, "EXTERNAL_PROBE_ENABLED", True),
        patch.object(settings, "EXTERNAL_PROBE_FAIL_THRESHOLD", threshold),
        patch.object(settings, "EXTERNAL_PROBE_TIMEOUT_SECONDS", 0.01),
    )


def _mk_host_node(db, host: str, ns: str = "prod-shared"):
    return upsert_service(
        db, ns, f"ingress:{host}", team_owner="external", synthetic=True,
    )


# ── (а) probe-фаза не держит транзакцию ─────────────────────────────────────


@pytest.mark.asyncio
async def test_probe_phase_holds_no_open_transaction(db):
    """В момент каждого probe транзакция закрыта.

    Ровно тот guard, что в test_topology_sync_reads_k8s_before_touching_db,
    но на состоянии сессии: пока `in_transaction()` в probe-фазе False,
    соединение не висит в `idle in transaction` все 5с × N хостов и
    idle_in_transaction_session_timeout его не обрывает.
    """
    for h in ("a.example.com", "b.example.com", "c.example.com"):
        _mk_host_node(db, h)
    db.commit()

    tx_during_probe: List[bool] = []

    async def _probe(host: str, timeout: float) -> Dict[str, Any]:
        tx_during_probe.append(db.in_transaction())
        return await _probe_stub("up")(host, timeout)

    e1, e2, e3 = _enabled()
    with e1, e2, e3, \
         patch.object(eps, "_get_redis", lambda: _FakeRedis()), \
         patch.object(eps, "_probe_endpoint", _probe), \
         patch("app.services.discord.service.DiscordService", lambda: _FakeDiscord()):
        stats = await eps.run_external_probe(db)

    assert stats["probed"] == 3
    assert tx_during_probe == [False, False, False], (
        f"транзакция открыта во время probe: {tx_during_probe} — вернулся "
        "idle-in-transaction 08–10.08.2026"
    )


@pytest.mark.asyncio
async def test_write_phase_rereads_rows_by_id(db):
    """Write-фаза перечитывает узлы по id, а не пишет по снапшоту фазы 1.

    Узел, удалённый за время probe (drift cleanup), не должен ронять tick.
    """
    svc = _mk_host_node(db, "gone.example.com")
    _mk_host_node(db, "alive.example.com")
    db.commit()
    gone_id = svc.id

    async def _probe(host: str, timeout: float) -> Dict[str, Any]:
        if host == "gone.example.com":
            # Имитируем удаление узла между snapshot и write.
            db.query(type(svc)).filter_by(id=gone_id).delete()
            db.commit()
        return await _probe_stub("up")(host, timeout)

    e1, e2, e3 = _enabled()
    with e1, e2, e3, \
         patch.object(eps, "_get_redis", lambda: _FakeRedis()), \
         patch.object(eps, "_probe_endpoint", _probe), \
         patch("app.services.discord.service.DiscordService", lambda: _FakeDiscord()):
        stats = await eps.run_external_probe(db)

    assert stats["probed"] == 2  # оба пробовались
    survivor = _mk_host_node(db, "alive.example.com")
    assert "external_probe" in (survivor.metadata_json or {})


# ── (б) Discord — только после commit ───────────────────────────────────────


@pytest.mark.asyncio
async def test_discord_alert_is_sent_after_commit(db):
    """Embed уходит ПОСЛЕ фиксации state.

    Прежний порядок (send → commit) на упавшем commit давал «алерт улетел,
    state откатился»: следующий тик слал тот же embed заново и не мог его
    зарезолвить.
    """
    _mk_host_node(db, "down.example.com")
    db.commit()

    order: List[str] = []
    event.listen(db, "after_commit", lambda s: order.append("commit"))
    discord = _FakeDiscord(order)

    e1, e2, e3 = _enabled(threshold=1)
    with e1, e2, e3, \
         patch.object(eps, "_get_redis", lambda: _FakeRedis()), \
         patch.object(eps, "_probe_endpoint", _probe_stub("down", order)), \
         patch("app.services.discord.service.DiscordService", lambda: discord):
        stats = await eps.run_external_probe(db)

    assert stats["alerts_fired"] == 1
    assert len(discord.sent) == 1 and discord.sent[0]["resolved"] is False
    sends = [i for i, x in enumerate(order) if x.startswith("discord:")]
    commits = [i for i, x in enumerate(order) if x == "commit"]
    assert commits and sends
    assert max(commits) < min(sends), f"send до commit: {order}"
    # И state, и AlertEvent зафиксированы.
    svc = _mk_host_node(db, "down.example.com")
    assert svc.metadata_json["external_probe"]["firing"] is True
    assert (
        db.query(AlertEvent)
        .filter(AlertEvent.fingerprint == "external_probe:down.example.com")
        .one()
        .resolved_at
    ) is None


@pytest.mark.asyncio
async def test_resolve_notification_also_after_commit(db):
    """Resolve-путь тоже пишет сначала, шлёт потом."""
    svc = _mk_host_node(db, "flap.example.com")
    svc.metadata_json = {"external_probe": {"firing": True, "consecutive_failures": 3}}
    db.commit()
    from app.knowledge_graph.populator import record_alert_event
    from datetime import datetime
    record_alert_event(
        db, svc, "ExternalProbeDown", "critical",
        "external_probe:flap.example.com", datetime(2026, 8, 10, 11, 0),
    )
    db.commit()

    order: List[str] = []
    event.listen(db, "after_commit", lambda s: order.append("commit"))
    discord = _FakeDiscord(order)

    e1, e2, e3 = _enabled()
    with e1, e2, e3, \
         patch.object(eps, "_get_redis", lambda: _FakeRedis()), \
         patch.object(eps, "_probe_endpoint", _probe_stub("up", order)), \
         patch("app.services.discord.service.DiscordService", lambda: discord):
        stats = await eps.run_external_probe(db)

    assert stats["alerts_resolved"] == 1
    assert discord.sent[0]["resolved"] is True
    sends = [i for i, x in enumerate(order) if x.startswith("discord:")]
    commits = [i for i, x in enumerate(order) if x == "commit"]
    assert max(commits) < min(sends), f"send до commit: {order}"
    ae = (
        db.query(AlertEvent)
        .filter(AlertEvent.fingerprint == "external_probe:flap.example.com")
        .one()
    )
    assert ae.resolved_at is not None


# ── (в) коллизия имён ingress:* (M2) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_ingress_resource_node_is_skipped(db):
    """`ingress:<resource-name>` из sync_topology_resources — не цель probe.

    Топология создаёт узлы с теми же признаками (synthetic + external +
    'ingress:%'), но хвост там — имя k8s-Ingress-ресурса. Прежний селектор
    пробовал их как hostname → DNS-fail и ложный ExternalProbeDown.
    Отсекаем и по метке источника (metadata_json.k8s_ingress), и по форме
    хвоста (RFC-1123 + обязательная точка).
    """
    _mk_host_node(db, "real.example.com")          # цель
    _mk_host_node(db, "grafana-ingress")           # имя ресурса: точки нет
    _mk_host_node(db, "under_score.example.com")   # не RFC-1123
    _mk_host_node(db, "*")                         # wildcard catch-all
    # Узел ровно как его пишет k8s_topology_resources_sync._sync_one_ingress:
    # хвост похож на FQDN, но это имя ресурса — спасает только метка источника.
    upsert_service(
        db, "prod-shared", "ingress:grafana.ingress.v1",
        team_owner="external", synthetic=True,
        metadata={"k8s_ingress": {"ingress_name": "grafana.ingress.v1",
                                  "hosts": ["grafana.example.com"]}},
    )
    db.commit()

    probed: List[str] = []
    discord = _FakeDiscord()

    e1, e2, e3 = _enabled()
    with e1, e2, e3, \
         patch.object(eps, "_get_redis", lambda: _FakeRedis()), \
         patch.object(eps, "_probe_endpoint", _probe_stub("down", seen=probed)), \
         patch("app.services.discord.service.DiscordService", lambda: discord):
        stats = await eps.run_external_probe(db)

    assert probed == ["real.example.com"]
    assert stats["probed"] == 1
    assert stats["skipped_wildcard"] == 1
    assert stats["skipped_non_hostname"] == 3
    # Ни одного ложного алерта по узлам топологии.
    assert [s["host"] for s in discord.sent] == ["real.example.com"]


def test_hostname_regex_rejects_resource_names():
    """Форма хвоста: FQDN проходит, имя k8s-ресурса — нет."""
    ok = ["grafana.lastoasisgame.com", "a.b.co", "xn--80ak6aa92e.com", "A.Example.COM"]
    bad = ["grafana-ingress", "town-service", "", "-lead.example.com",
           "trail-.example.com", "no_underscore.example.com", "dot.at.end."]
    for h in ok:
        assert eps._HOSTNAME_RE.match(h), h
    for h in bad:
        assert not eps._HOSTNAME_RE.match(h), h


# ── (г) лок от перекрытия прогонов ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_second_run_skips_while_first_holds_lock(db):
    """Beat тикает раз в минуту, прогон может жить дольше — второй скипаем.

    Иначе два прогона гоняют read-modify-write по metadata_json наперегонки:
    потерянные consecutive_failures и двойные fire/resolve.
    """
    _mk_host_node(db, "slow.example.com")
    db.commit()
    fake_redis = _FakeRedis()
    inner: List[Dict[str, Any]] = []

    async def _probe(host: str, timeout: float) -> Dict[str, Any]:
        # Пока первый прогон в probe-фазе — второй тик уже стучится.
        e1, e2, e3 = _enabled()
        with e1, e2, e3:
            inner.append(await eps.run_external_probe(db))
        return await _probe_stub("up")(host, timeout)

    e1, e2, e3 = _enabled()
    with e1, e2, e3, \
         patch.object(eps, "_get_redis", lambda: fake_redis), \
         patch.object(eps, "_probe_endpoint", _probe), \
         patch("app.services.discord.service.DiscordService", lambda: _FakeDiscord()):
        stats = await eps.run_external_probe(db)

    assert inner == [{"skipped": "already_running"}]
    assert stats["probed"] == 1
    # Лок отпущен — следующий тик пройдёт.
    assert await eps._try_acquire_lock() not in (None,)


@pytest.mark.asyncio
async def test_lock_released_after_run(db):
    """После нормального прогона ключ лока не остаётся висеть."""
    fake_redis = _FakeRedis()
    e1, e2, e3 = _enabled()
    with e1, e2, e3, \
         patch.object(eps, "_get_redis", lambda: fake_redis), \
         patch.object(eps, "_probe_endpoint", _probe_stub("up")), \
         patch("app.services.discord.service.DiscordService", lambda: _FakeDiscord()):
        await eps.run_external_probe(db)
    assert eps._LOCK_KEY not in fake_redis.store


@pytest.mark.asyncio
async def test_lock_release_keeps_foreign_token(db):
    """Прогон дольше TTL не удаляет лок, перезахваченный другим прогоном."""
    fake_redis = _FakeRedis()
    with patch.object(eps, "_get_redis", lambda: fake_redis):
        fake_redis.store[eps._LOCK_KEY] = "other-worker-token"
        await eps._release_lock("my-token")
    assert fake_redis.store[eps._LOCK_KEY] == "other-worker-token"


@pytest.mark.asyncio
async def test_redis_down_is_fail_open(db):
    """Redis недоступен — probe всё равно работает (лок best-effort).

    Живой мониторинг внешних endpoint'ов важнее защиты от наложения;
    сбой redis не должен глушить probe целиком.
    """
    class _DeadRedis:
        async def set(self, *a, **kw):
            raise ConnectionError("redis down")

        async def get(self, *a, **kw):
            raise ConnectionError("redis down")

        async def delete(self, *a, **kw):
            raise ConnectionError("redis down")

    _mk_host_node(db, "still.example.com")
    db.commit()

    e1, e2, e3 = _enabled()
    with e1, e2, e3, \
         patch.object(eps, "_get_redis", lambda: _DeadRedis()), \
         patch.object(eps, "_probe_endpoint", _probe_stub("up")), \
         patch("app.services.discord.service.DiscordService", lambda: _FakeDiscord()):
        stats = await eps.run_external_probe(db)

    assert stats["probed"] == 1


@pytest.mark.asyncio
async def test_disabled_flag_short_circuits(db):
    """Флаг выключен — ни лока, ни SQL, ни probe (дефолт прода до включения)."""
    with patch.object(settings, "EXTERNAL_PROBE_ENABLED", False):
        assert await eps.run_external_probe(db) == {"skipped": "disabled"}
