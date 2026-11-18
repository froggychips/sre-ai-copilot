"""API не должен вставать из-за БД.

Дважды (08.08 и 31.10.2026) оба пода API ложились одинаково: синхронная
работа с БД шла прямо в event loop обработчика вебхука, запрос вставал за
чужим row-lock, `/healthz` переставал отвечать, kubelet убивал под, а
Alertmanager терял вебхуки. Здесь — guard-ы на четыре части исправления:

* `lock_timeout` / `statement_timeout` на соединении, разные по роли процесса;
* таймаут БД в вебхуке — это 503 (Alertmanager повторит), а не 202 с потерей;
* запись батча идёт в потоке, event loop свободен;
* тело и размер батча ограничены (413), in-memory буфера сырых батчей нет.

Большая часть — без живого Postgres. Что без него не проверить (таймаут
реально действует и переживает возврат соединения в пул) — под
`requires_postgres`.
"""
from __future__ import annotations

import asyncio
import importlib.util
import inspect
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import psycopg2
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

import app.database as db_mod
from app.api import webhooks
from app.models.incident import AlertManagerWebhook
from tests.conftest import requires_postgres

_CFG = SimpleNamespace(
    DB_API_LOCK_TIMEOUT_MS=3000,
    DB_API_STATEMENT_TIMEOUT_MS=15000,
    DB_WORKER_LOCK_TIMEOUT_MS=180000,
    DB_WORKER_STATEMENT_TIMEOUT_MS=600000,
)


@pytest.fixture
def role():
    """Сменить роль процесса на время теста и вернуть прежнюю."""
    previous = db_mod.process_role()

    def _set(value: str) -> None:
        db_mod.set_process_role(value)

    yield _set
    db_mod.set_process_role(previous)


def _payload(n_alerts: int = 2, status: str = "firing") -> AlertManagerWebhook:
    return AlertManagerWebhook.model_validate({
        "version": "4",
        "groupKey": "{}:{alertname=\"KubePodCrashLooping\"}",
        "status": status,
        "receiver": "sre-ai-preprod",
        "groupLabels": {"alertname": "KubePodCrashLooping"},
        "alerts": [
            {
                "status": status,
                "labels": {
                    "alertname": "KubePodCrashLooping",
                    "severity": "warning",
                    "namespace": "squad-1",
                    "pod": f"svc-{i}",
                },
                "annotations": {"summary": "stub"},
                "startsAt": "2026-11-18T00:00:00Z",
                "fingerprint": f"dbt-{i}",
            }
            for i in range(n_alerts)
        ],
    })


def _pg_error(pgcode: str | None) -> psycopg2.Error:
    """Ошибка psycopg2 с заданным SQLSTATE (у настоящих pgcode — read-only)."""
    cls = type("FakePgError", (psycopg2.OperationalError,), {"pgcode": pgcode})
    return cls("simulated")


def _sa_error(pgcode: str | None) -> OperationalError:
    return OperationalError("UPDATE kg_services …", {}, _pg_error(pgcode))


# ── роль процесса и таймауты сессии ───────────────────────────────────────


def test_default_role_is_script_without_limits():
    """Скрипты, pytest и всё, что роль не объявило, ограничений не получают:
    разовый бэкфилл на полчаса не должен умереть на таймауте воркера."""
    assert db_mod.process_role() == db_mod.ROLE_SCRIPT
    assert db_mod.session_timeouts_ms(db_mod.ROLE_SCRIPT, cfg=_CFG) == (0, 0)


def test_timeouts_differ_by_role():
    api = db_mod.session_timeouts_ms(db_mod.ROLE_API, cfg=_CFG)
    worker = db_mod.session_timeouts_ms(db_mod.ROLE_WORKER, cfg=_CFG)
    assert api == (3000, 15000)
    assert worker == (180000, 600000)


def test_default_settings_keep_api_in_seconds_and_worker_in_minutes():
    """Дефолты: API — секунды, воркер — минуты, и воркер укладывается в
    мягкий лимит задачи с запасом (иначе таймаут запроса бессмысленен)."""
    from app.config import settings

    api_lock, api_stmt = db_mod.session_timeouts_ms(db_mod.ROLE_API)
    worker_lock, worker_stmt = db_mod.session_timeouts_ms(db_mod.ROLE_WORKER)
    assert 0 < api_lock <= 5_000
    assert api_lock < api_stmt <= 30_000
    assert 60_000 <= worker_lock < worker_stmt
    assert worker_stmt < settings.CELERY_TASK_SOFT_TIME_LIMIT_SECONDS * 1000
    # Держателя, ушедшего во внешний вызов, PG снимает по
    # idle_in_transaction_session_timeout — ожидающий должен дожить до этого.
    assert worker_lock > db_mod._IDLE_TX_TIMEOUT_MS


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql):
        self._conn.executed.append((sql, self._conn.autocommit))

    def close(self):
        self._conn.cursor_closed = True


class _FakeDbapiConnection:
    def __init__(self):
        self.autocommit = False
        self.executed: list = []
        self.cursor_closed = False

    def cursor(self):
        return _FakeCursor(self)


def test_connect_hook_sets_both_timeouts_in_autocommit(role):
    """SET обязан идти в autocommit: иначе ROLLBACK пула при возврате
    соединения откатит его вместе с транзакцией, и таймауты исчезнут."""
    role(db_mod.ROLE_API)
    conn = _FakeDbapiConnection()
    with patch.object(db_mod, "settings", _CFG):
        db_mod._apply_session_timeouts(conn)
    assert conn.executed == [
        ("SET lock_timeout = 3000", True),
        ("SET statement_timeout = 15000", True),
    ]
    assert conn.autocommit is False, "autocommit не возвращён в прежнее состояние"
    assert conn.cursor_closed


def test_connect_hook_is_noop_for_script_role():
    conn = _FakeDbapiConnection()
    db_mod._apply_session_timeouts(conn)
    assert conn.executed == []


def test_zero_disables_a_single_timeout(role):
    role(db_mod.ROLE_WORKER)
    cfg = SimpleNamespace(**{**vars(_CFG), "DB_WORKER_LOCK_TIMEOUT_MS": 0})
    conn = _FakeDbapiConnection()
    with patch.object(db_mod, "settings", cfg):
        db_mod._apply_session_timeouts(conn)
    assert [sql for sql, _ in conn.executed] == ["SET statement_timeout = 600000"]


def test_set_process_role_disposes_pool_on_change(role):
    """Соединения, открытые до объявления роли, жили бы с чужими таймаутами."""
    with patch.object(db_mod.engine, "dispose") as dispose:
        role(db_mod.ROLE_API)
        assert dispose.call_count == 1
        role(db_mod.ROLE_API)  # повтор — без сброса
        assert dispose.call_count == 1
    assert db_mod.process_role() == db_mod.ROLE_API


def test_set_process_role_rejects_unknown():
    with pytest.raises(ValueError):
        db_mod.set_process_role("cron")


def test_celery_worker_and_beat_declare_worker_role(role):
    """Воркер/executor и beat узнаются по сигналам Celery — без нового env."""
    from celery.signals import beat_init, worker_init

    def _connected(signal, func) -> bool:
        return any(ref() is func for _key, ref in signal.receivers if callable(ref))

    assert _connected(worker_init, db_mod._declare_worker_role)
    assert _connected(beat_init, db_mod._declare_beat_role)
    with patch.object(db_mod.engine, "dispose"):
        db_mod._declare_worker_role(sender=None)
    assert db_mod.process_role() == db_mod.ROLE_WORKER


def test_api_declares_its_role_in_lifespan():
    """Без этой строки API молча остаётся без таймаутов (роль script)."""
    import app.main as main_mod

    source = inspect.getsource(main_mod.lifespan)
    assert "set_process_role(ROLE_API)" in source
    # До первой работы с БД на старте.
    assert source.index("set_process_role(ROLE_API)") < source.index("_run_startup_contract_check")


# ── какие ошибки БД — «повторить позже» ───────────────────────────────────


@pytest.mark.parametrize("pgcode", [
    "57014",  # statement_timeout
    "55P03",  # lock_timeout
    "25P03",  # idle_in_transaction_session_timeout
    "40P01",  # deadlock
    "53300",  # too_many_connections
    "57P01",  # admin_shutdown
    "08006",  # connection_failure
    None,     # «server closed the connection unexpectedly»
])
def test_retryable_db_errors(pgcode):
    assert db_mod.is_retryable_db_error(_sa_error(pgcode)) is True


def test_pool_exhaustion_is_retryable():
    assert db_mod.is_retryable_db_error(PoolTimeoutError("QueuePool limit reached")) is True


@pytest.mark.parametrize("pgcode", ["23505", "22P02", "42P01"])
def test_data_errors_are_not_retryable(pgcode):
    """Битые данные повтором не лечатся — их обработчик помечает failed."""
    assert db_mod.is_retryable_db_error(_sa_error(pgcode)) is False


def test_non_postgres_and_plain_errors_are_not_retryable():
    import sqlite3

    sqlite_err = OperationalError("SELECT 1", {}, sqlite3.OperationalError("no such table"))
    assert db_mod.is_retryable_db_error(sqlite_err) is False
    assert db_mod.is_retryable_db_error(ValueError("boom")) is False
    assert db_mod.is_retryable_db_error(None) is False
    assert db_mod.is_retryable_db_error(
        IntegrityError("INSERT", {}, _pg_error("23505"))
    ) is False


def _error_context(exc):
    return SimpleNamespace(sqlalchemy_exception=exc, original_exception=getattr(exc, "orig", exc))


def test_watch_sees_swallowed_timeout_and_ignores_data_errors():
    with db_mod.watch_retryable_db_errors() as watch:
        db_mod._note_retryable_db_error(_error_context(_sa_error("23505")))
        assert watch.error is None
        first = _sa_error("55P03")
        db_mod._note_retryable_db_error(_error_context(first))
        db_mod._note_retryable_db_error(_error_context(_sa_error("57014")))
        assert watch.error is first, "держим первую ошибку, а не последнюю"


def test_watch_is_scoped_to_its_thread_and_block():
    db_mod._note_retryable_db_error(_error_context(_sa_error("55P03")))  # вне блока — no-op
    seen_in_other_thread = []
    with db_mod.watch_retryable_db_errors() as watch:
        t = threading.Thread(
            target=lambda: (
                db_mod._note_retryable_db_error(_error_context(_sa_error("55P03"))),
                seen_in_other_thread.append(True),
            ),
        )
        t.start()
        t.join()
        assert seen_in_other_thread and watch.error is None
    assert getattr(db_mod._db_error_watch, "current", None) is None


# ── /store: таймаут БД → 503, работа — в потоке ───────────────────────────


def _run_store(payload, db, populate):
    with patch(
        "app.knowledge_graph.auto_populator.populate_from_incident", side_effect=populate,
    ):
        return asyncio.run(webhooks.alertmanager_webhook_store_only(payload, db=db))


def test_store_timeout_swallowed_by_populate_becomes_503():
    """Запись в граф best-effort: lock_timeout она проглатывает и возвращает
    пустую статистику. Раньше это был 202, и Alertmanager алерт не повторял."""
    db = MagicMock()
    calls = []

    def _populate(_db, incident):
        calls.append(incident.incident_id)
        db_mod._note_retryable_db_error(_error_context(_sa_error("55P03")))
        return {"services_touched": 0, "deploys_added": 0, "alerts_added": 0}

    with pytest.raises(HTTPException) as exc:
        _run_store(_payload(n_alerts=3), db, _populate)
    assert exc.value.status_code == 503
    assert exc.value.headers == {"Retry-After": str(webhooks._DB_BUSY_RETRY_AFTER_SEC)}
    assert len(calls) == 1, "батч прерывается на первом таймауте, а не добивается до конца"
    db.rollback.assert_called()
    db.commit.assert_not_called()


def test_store_timeout_raised_by_populate_becomes_503():
    db = MagicMock()

    def _populate(_db, _incident):
        raise _sa_error("57014")

    with pytest.raises(HTTPException) as exc:
        _run_store(_payload(), db, _populate)
    assert exc.value.status_code == 503
    db.commit.assert_not_called()


def test_store_data_error_still_marks_failed_and_accepts():
    """Не-таймаут ведёт себя как раньше: алерт failed, батч принят."""
    db = MagicMock()

    def _populate(_db, incident):
        if incident.incident_id == "dbt-0":
            raise ValueError("bad row")
        return {"alerts_added": 1}

    result = _run_store(_payload(n_alerts=2), db, _populate)
    assert [a["result"] for a in result["alerts"]] == ["failed", "stored"]
    db.commit.assert_called_once()


def test_store_pool_exhaustion_becomes_503_before_any_write():
    db = MagicMock()
    db.connection.side_effect = PoolTimeoutError("QueuePool limit of size 10 overflow 20 reached")
    populate = MagicMock()
    with pytest.raises(HTTPException) as exc:
        _run_store(_payload(), db, populate)
    assert exc.value.status_code == 503
    populate.assert_not_called()


def test_store_commit_timeout_becomes_503():
    db = MagicMock()
    db.commit.side_effect = _sa_error(None)
    with pytest.raises(HTTPException) as exc:
        _run_store(_payload(), db, lambda _db, _inc: {})
    assert exc.value.status_code == 503


def test_store_resolved_only_batch_does_not_touch_db_connection():
    db = MagicMock()
    result = _run_store(_payload(status="resolved"), db, MagicMock())
    assert [a["result"] for a in result["alerts"]] == ["resolved-skipped", "resolved-skipped"]
    db.connection.assert_not_called()


def _run_with_ticker(coro_factory):
    """Выполнить корутину рядом с «пульсом» loop-а; вернуть (результат,
    максимальную паузу между ударами пульса в секундах)."""
    async def _main():
        gaps = []
        stop = asyncio.Event()

        async def _ticker():
            last = time.monotonic()
            while not stop.is_set():
                await asyncio.sleep(0.01)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        ticker = asyncio.create_task(_ticker())
        try:
            result = await coro_factory()
        finally:
            stop.set()
            await ticker
        return result, max(gaps)

    return asyncio.run(_main())


def test_store_db_work_runs_off_the_event_loop():
    """Пока запись ждёт БД, loop продолжает крутиться — `/healthz` отвечает.

    0,4 с в populate — это «запрос встал за чужим локом». До исправления
    пауза пульса равнялась бы этим 0,4 с.
    """
    db = MagicMock()
    threads = []

    def _slow_populate(_db, _incident):
        threads.append(threading.current_thread())
        time.sleep(0.2)
        return {}

    def _call():
        return webhooks.alertmanager_webhook_store_only(_payload(n_alerts=2), db=db)

    with patch(
        "app.knowledge_graph.auto_populator.populate_from_incident", side_effect=_slow_populate,
    ):
        result, max_gap = _run_with_ticker(_call)
    assert result["status"] == "stored"
    assert all(t is not threading.main_thread() for t in threads)
    # Весь батч — в ОДНОМ потоке: сессия не переходит из рук в руки.
    assert len(set(threads)) == 1
    assert max_gap < 0.15, f"event loop стоял {max_gap:.2f} с"


def test_enrich_store_phase_runs_off_the_event_loop_and_maps_timeout(monkeypatch):
    monkeypatch.setattr(webhooks.settings, "DISCORD_ENRICH_ENABLED", False)
    db = MagicMock()
    threads = []

    def _slow_populate(_db, _incident):
        threads.append(threading.current_thread())
        time.sleep(0.2)
        return {}

    async def _no_node(_labels):
        return None

    def _call():
        return webhooks.alertmanager_webhook_enrich_and_forward(_payload(n_alerts=2), db=db)

    with patch("app.services.node_resolver.annotate_node_label", _no_node), patch(
        "app.knowledge_graph.auto_populator.populate_from_incident", side_effect=_slow_populate,
    ):
        result, max_gap = _run_with_ticker(_call)
    assert [a["result"] for a in result["alerts"]] == ["stored", "stored"]
    assert all(t is not threading.main_thread() for t in threads)
    assert max_gap < 0.15, f"event loop стоял {max_gap:.2f} с"
    db.commit.assert_called_once()

    def _timeout(_db, _incident):
        raise _sa_error("55P03")

    with patch("app.services.node_resolver.annotate_node_label", _no_node), patch(
        "app.knowledge_graph.auto_populator.populate_from_incident", side_effect=_timeout,
    ), pytest.raises(HTTPException) as exc:
        asyncio.run(webhooks.alertmanager_webhook_enrich_and_forward(_payload(), db=db))
    assert exc.value.status_code == 503


def test_enrich_store_phase_keeps_batch_order():
    """resolved и firing остаются в `alerts` в порядке батча."""
    prepared = [
        (SimpleNamespace(incident_id="a"), False),
        (SimpleNamespace(incident_id="b"), True),
        (SimpleNamespace(incident_id="c"), False),
    ]
    order = []

    def _populate(_db, incident):
        order.append(incident.incident_id)
        return {}

    with patch(
        "app.knowledge_graph.auto_populator.populate_from_incident", side_effect=_populate,
    ):
        stored = webhooks._enrich_store_batch_sync(MagicMock(), prepared)
    assert order == ["a", "c"]
    assert [(s["incident_id"], s["result"]) for s in stored] == [
        ("a", "stored"), ("b", "resolved-skipped"), ("c", "stored"),
    ]


def test_db_thread_limit_comes_from_settings(monkeypatch):
    """Свой лимит потоков, а не общий threadpool: шторм вебхуков не должен
    занять потоки, в которых отвечает /readyz."""
    monkeypatch.setattr(webhooks.settings, "WEBHOOK_DB_THREADS", 3)

    async def _probe():
        limiter = webhooks._db_limiter()
        assert webhooks._db_limiter() is limiter
        return limiter.total_tokens

    assert asyncio.run(_probe()) == 3


def test_db_threads_fit_into_connection_pool():
    from app.config import settings

    assert settings.WEBHOOK_DB_THREADS < settings.DB_POOL_SIZE + settings.DB_MAX_OVERFLOW


def test_remediation_webhook_timeout_becomes_503():
    db = MagicMock()
    with patch.object(webhooks, "record_external_remediation", side_effect=_sa_error("55P03")), \
            pytest.raises(HTTPException) as exc:
        asyncio.run(webhooks.remediation_event_webhook(MagicMock(), db=db))
    assert exc.value.status_code == 503


# ── предел размера тела и батча ───────────────────────────────────────────


@pytest.fixture
def limited_client(monkeypatch):
    monkeypatch.setattr(webhooks.settings, "WEBHOOK_MAX_BODY_BYTES", 1024)
    monkeypatch.setattr(webhooks.settings, "ALERTMANAGER_WEBHOOK_SECRET", "body-limit-secret")
    app = FastAPI()
    app.include_router(webhooks.router, prefix="/webhooks")
    return TestClient(app)


@pytest.mark.parametrize("path", [
    "/webhooks/alertmanager",
    "/webhooks/alertmanager/store",
    "/webhooks/alertmanager/enrich-and-forward",
    "/webhooks/remediation",
])
def test_oversized_body_rejected_with_413_before_auth(limited_client, path):
    resp = limited_client.post(
        path, content=b"x" * 2048, headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 413
    assert "too large" in resp.json()["detail"]


def test_oversized_chunked_body_rejected_with_413(limited_client):
    """Без Content-Length (chunked) предел считается по факту прочитанного."""
    def _chunks():
        for _ in range(8):
            yield b"x" * 512

    resp = limited_client.post(
        "/webhooks/alertmanager/store", content=_chunks(),
        headers={"Content-Type": "application/json"},
    )
    assert "content-length" not in {k.lower() for k in resp.request.headers}
    assert resp.status_code == 413


def test_body_within_limit_reaches_auth(limited_client):
    resp = limited_client.post(
        "/webhooks/alertmanager/store", content=b"{}",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code != 413


def test_body_limit_zero_disables_check(limited_client, monkeypatch):
    monkeypatch.setattr(webhooks.settings, "WEBHOOK_MAX_BODY_BYTES", 0)
    resp = limited_client.post(
        "/webhooks/alertmanager/store", content=b"x" * 4096,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code != 413


def test_too_many_alerts_rejected_with_413(monkeypatch):
    monkeypatch.setattr(webhooks.settings, "WEBHOOK_MAX_ALERTS", 2)
    db = MagicMock()
    with patch.object(webhooks.log, "warning") as warn, pytest.raises(HTTPException) as exc:
        asyncio.run(webhooks.alertmanager_webhook_store_only(_payload(n_alerts=3), db=db))
    assert exc.value.status_code == 413
    # Alertmanager 4xx не повторяет — отказ обязан остаться в логе.
    assert warn.call_args.args[0] == "webhook.body_too_large"
    assert warn.call_args.kwargs["alerts"] == 3
    db.connection.assert_not_called()

    monkeypatch.setattr(webhooks.settings, "WEBHOOK_MAX_ALERTS", 0)
    result = _run_store(_payload(n_alerts=3), MagicMock(), lambda _db, _inc: {})
    assert result["status"] == "stored"


def test_raw_collector_is_gone():
    """До 1000 сырых батчей на процесс при лимите пода 512Mi, читателей — ноль."""
    assert importlib.util.find_spec("app.ingestion") is None
    assert not hasattr(webhooks, "raw_collector")


# ── живой Postgres: таймауты действительно действуют ──────────────────────


@pytest.fixture
def api_role_engine(role, monkeypatch):
    """Модульный engine в роли API с коротким lock_timeout."""
    from app.config import settings

    monkeypatch.setattr(settings, "DB_API_LOCK_TIMEOUT_MS", 300)
    monkeypatch.setattr(settings, "DB_API_STATEMENT_TIMEOUT_MS", 1500)
    role(db_mod.ROLE_API)
    db_mod.Base.metadata.create_all(db_mod.engine)
    yield db_mod.engine
    db_mod.engine.dispose()


@requires_postgres
def test_pg_timeouts_applied_and_survive_pool_return(api_role_engine):
    for _ in range(2):  # второй проход — то же соединение после ROLLBACK пула
        with api_role_engine.connect() as conn:
            assert conn.execute(text("SHOW lock_timeout")).scalar() == "300ms"
            assert conn.execute(text("SHOW statement_timeout")).scalar() == "1500ms"
            assert conn.execute(text("SHOW timezone")).scalar() == "UTC"
            conn.rollback()


@requires_postgres
def test_pg_statement_timeout_is_retryable(api_role_engine):
    started = time.monotonic()
    with api_role_engine.connect() as conn, pytest.raises(OperationalError) as exc:
        conn.execute(text("SELECT pg_sleep(10)"))
    assert time.monotonic() - started < 5
    assert db_mod.is_retryable_db_error(exc.value)


@requires_postgres
def test_pg_lock_timeout_swallowed_in_savepoint_is_seen_by_watch(api_role_engine):
    """Тот самый сценарий: запись встала за чужим row-lock, код записи
    проглотил ошибку в своём SAVEPOINT — наблюдатель её всё равно видит."""
    incident_id = f"lock-{time.time_ns()}"
    with db_mod.SessionLocal() as setup:
        setup.add(db_mod.IncidentRecord(incident_id=incident_id, status="OPEN", data={}))
        setup.commit()

    holder = db_mod.SessionLocal()
    waiter = db_mod.SessionLocal()
    try:
        holder.query(db_mod.IncidentRecord).filter_by(incident_id=incident_id).with_for_update().one()
        started = time.monotonic()
        with db_mod.watch_retryable_db_errors() as watch:
            try:
                with waiter.begin_nested():
                    waiter.query(db_mod.IncidentRecord).filter_by(
                        incident_id=incident_id,
                    ).update({"status": "RESOLVED"})
            except Exception:
                pass  # best-effort запись: исключение проглочено
        assert time.monotonic() - started < 3, "lock_timeout не сработал"
        assert watch.error is not None
        assert watch.error.orig.pgcode == "55P03"
        # Внешняя транзакция цела — сессией можно пользоваться дальше.
        assert waiter.execute(text("SELECT 1")).scalar() == 1
    finally:
        waiter.rollback()
        waiter.close()
        holder.rollback()
        holder.close()
        with db_mod.SessionLocal() as cleanup:
            cleanup.query(db_mod.IncidentRecord).filter_by(incident_id=incident_id).delete()
            cleanup.commit()


@requires_postgres
def test_pg_script_role_has_no_limits(role):
    role(db_mod.ROLE_SCRIPT)
    db_mod.engine.dispose()
    with db_mod.engine.connect() as conn:
        assert conn.execute(text("SHOW lock_timeout")).scalar() == "0"
        assert conn.execute(text("SHOW statement_timeout")).scalar() == "0"
