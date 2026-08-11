"""KG H2: sync_namespace_events изолирует битый event через SAVEPOINT.

Регресс на P1: per-event except внутри цикла не откатывал транзакцию, поэтому
первая IntegrityError/DataError на одном event переводила PG-сессию в
aborted-состояние, и финальный db.commit() этого namespace падал с
PendingRollbackError — терялись ВСЕ успешно записанные ранее события tick'а.

Фикс: record_pod_event обёрнут в db.begin_nested() (SAVEPOINT) per-event;
его контекст-менеджер откатывает только битый event. Проверяем на SQLite
(поддерживает SAVEPOINT; та же семантика, что у PG).
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.knowledge_graph.k8s_events_sync as k8s_events_sync
from app.database import Base
from app.knowledge_graph.schema import PodEvent, Service


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _warning_event(uid: str, pod: str, reason: str = "OOMKilled") -> dict:
    return {
        "metadata": {"uid": uid, "creationTimestamp": "2026-05-16T07:30:03Z"},
        "reason": reason,
        "type": "Warning",
        "involvedObject": {"kind": "Pod", "name": pod},
        "message": "container killed",
        "firstTimestamp": "2026-05-16T07:30:03Z",
        "lastTimestamp": "2026-05-16T07:31:03Z",
        "count": 1,
        "source": {"component": "kubelet", "host": "dev-1"},
    }


def test_one_bad_event_does_not_kill_namespace_sync(db, monkeypatch):
    """Битый event в середине НЕ роняет соседние, commit проходит."""
    # Сервис, чтобы pod резолвился (pod-name == service name → exact match).
    db.add(Service(namespace="squad-1", name="bot-service", synthetic=False))
    db.commit()

    events = [
        _warning_event("uid-1", "bot-service"),
        _warning_event("uid-2", "bot-service"),  # этот уроним
        _warning_event("uid-3", "bot-service"),
    ]
    monkeypatch.setattr(
        k8s_events_sync, "_kubectl_get_events_warning", lambda ns: events
    )

    real_record = k8s_events_sync.record_pod_event

    def flaky_record(db, *, event_uid, **kwargs):
        if event_uid == "uid-2":
            raise RuntimeError("simulated DataError on uid-2")
        return real_record(db, event_uid=event_uid, **kwargs)

    monkeypatch.setattr(k8s_events_sync, "record_pod_event", flaky_record)

    # Спай на begin_nested: доказываем, что изоляция реально через SAVEPOINT,
    # а не просто терпимость SQLite к ошибкам (на PG именно SAVEPOINT спасает
    # от aborted-транзакции — там этот тест и ловит регресс).
    nested_calls = {"n": 0}
    real_begin_nested = db.begin_nested

    def spy_begin_nested():
        nested_calls["n"] += 1
        return real_begin_nested()

    monkeypatch.setattr(db, "begin_nested", spy_begin_nested)

    stats = k8s_events_sync.sync_namespace_events(db, "squad-1")

    # 2 записаны, 1 в errors — соседи выжили.
    assert stats["added"] == 2
    assert stats["errors"] == 1
    # SAVEPOINT открывался на каждое из 3 валидных событий.
    assert nested_calls["n"] == 3

    # sync_namespace_events уже сделал db.commit() в конце без
    # PendingRollbackError (до фикса упал бы тут). Строки реально в БД.
    uids = {e.event_uid for e in db.query(PodEvent).all()}
    assert uids == {"uid-1", "uid-3"}


def test_clean_namespace_sync_commits_all(db, monkeypatch):
    """Контроль: без падений все события коммитятся."""
    db.add(Service(namespace="squad-1", name="bot-service", synthetic=False))
    db.commit()
    events = [_warning_event("uid-1", "bot-service"), _warning_event("uid-2", "bot-service")]
    monkeypatch.setattr(
        k8s_events_sync, "_kubectl_get_events_warning", lambda ns: events
    )
    stats = k8s_events_sync.sync_namespace_events(db, "squad-1")
    assert stats["added"] == 2
    assert stats["errors"] == 0
    assert db.query(PodEvent).count() == 2


# ── Ревью 2026-08-10: перебор ns — только живые, без синтетики ──────────────


def _seed_namespaces(db):
    """Живой ns, синтетический `nats-subjects` и мёртвый (весь synthetic)."""
    db.add_all([
        Service(namespace="squad-1", name="bot-service", synthetic=False),
        # ingress:<host> — synthetic, но стоит рядом с живым сервисом:
        # namespace обязан остаться в переборе.
        Service(namespace="squad-1", name="ingress:a.example.com", synthetic=True),
        # subject-узлы nats_subjects_sync: ns в k8s не существует вовсе.
        Service(namespace="nats-subjects", name="subject:ping", synthetic=True),
        # мёртвый ns после drift_cleanup: все узлы помечены synthetic=True.
        Service(namespace="squad-dead", name="old-service", synthetic=True),
    ])
    db.commit()


def test_sync_all_events_skips_synthetic_and_dead_namespaces(db, monkeypatch):
    """Пустой KG_SCAN_NAMESPACES → перебираем только живые ns.

    Раньше брались ВСЕ distinct namespace: синтетический `nats-subjects` и
    мёртвые ns давали по два kubectl-вызова и warning каждые 10 минут.
    """
    _seed_namespaces(db)
    monkeypatch.setattr(k8s_events_sync.settings, "KG_SCAN_NAMESPACES", "")

    seen = []
    monkeypatch.setattr(
        k8s_events_sync, "sync_namespace_events",
        lambda db, ns: seen.append(ns) or {
            "fetched": 0, "added": 0, "skipped": 0, "errors": 0,
        },
    )

    total = k8s_events_sync.sync_all_events(db)

    assert seen == ["squad-1"]
    assert "nats-subjects" not in seen
    assert "squad-dead" not in seen
    assert total["namespaces"] == 1


def test_scannable_namespaces_helper_filters_synthetic(db):
    """Прямой контракт хелпера (используется и как док-фиксация признака)."""
    _seed_namespaces(db)
    assert k8s_events_sync._scannable_kg_namespaces(db) == ["squad-1"]


def test_explicit_namespaces_argument_is_not_filtered(db, monkeypatch):
    """Явный список (CLI / whitelist) уважается как есть — фильтр не мешает."""
    _seed_namespaces(db)
    seen = []
    monkeypatch.setattr(
        k8s_events_sync, "sync_namespace_events",
        lambda db, ns: seen.append(ns) or {
            "fetched": 0, "added": 0, "skipped": 0, "errors": 0,
        },
    )
    k8s_events_sync.sync_all_events(db, namespaces=["squad-dead"])
    assert seen == ["squad-dead"]
