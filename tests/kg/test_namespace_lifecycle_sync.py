"""Детект инкарнаций и присутствия namespace (шаг B2).

Главное свойство: граф начинает понимать, что стенд под тем же именем — уже
другой. Прод 14.08.2026: `squad-1-shared` имеет узлы на 82 дня старше самого
namespace, к ним прилипло 39 775 health-точек прошлой инкарнации, и детектор
аномалий сравнивает новый стенд со старым.

Второе свойство: присутствие считается по ВРЕМЕНИ. Прежний guard абортил
уборку при `drift_pct > 20%` и на 29.8% перестал работать вовсе — то есть
заблокировался ровно тогда, когда мусора больше всего. Здесь исчезнувший
namespace лишь помечается `missing` с отметкой времени.

Удаления на этом шаге нет: сначала неделя наблюдения, потом действие.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph import namespace_lifecycle as nl
from app.knowledge_graph.namespace_lifecycle import (K8sNamespaceFetchError,
                                                     sync_namespace_lifecycle)
from app.knowledge_graph.schema import (NS_STATE_ACTIVE, NS_STATE_MISSING,
                                        Namespace)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


@pytest.fixture
def cluster(monkeypatch):
    """Подменяет ответ кластера: {name: (uid, created)}."""
    def _install(mapping):
        data = {
            name: {"uid": uid, "created_at": created}
            for name, (uid, created) in mapping.items()
        }
        monkeypatch.setattr(nl, "_fetch_namespaces", lambda: data)
    return _install


NOW = datetime(2026, 8, 14, 12, 0)


# --- первое знакомство ----------------------------------------------------


def test_new_namespace_is_created_as_active(db, cluster):
    cluster({"squad-1-shared": ("uid-a", NOW)})
    stats = sync_namespace_lifecycle(db)

    row = db.query(Namespace).one()
    assert (row.state, row.incarnation, row.k8s_uid) == (NS_STATE_ACTIVE, 1, "uid-a")
    assert stats["created"] == 1


def test_backfilled_row_without_uid_just_learns_it(db, cluster):
    """Строка из backfill не считается пересозданием — UID просто не знали."""
    db.add(Namespace(namespace="squad-2", k8s_uid=None, incarnation=1))
    db.commit()
    cluster({"squad-2": ("uid-first", NOW)})

    stats = sync_namespace_lifecycle(db)
    row = db.query(Namespace).one()
    assert row.k8s_uid == "uid-first"
    assert row.incarnation == 1, "запоминание UID — не пересоздание"
    assert stats["reincarnated"] == 0


# --- инкарнации -----------------------------------------------------------


def test_uid_change_counts_as_new_incarnation(db, cluster):
    """Сквад снесли и раскатали заново — под тем же именем это другой стенд."""
    db.add(Namespace(namespace="squad-34-shared", k8s_uid="uid-old", incarnation=1,
                     state=NS_STATE_ACTIVE))
    db.commit()

    cluster({"squad-34-shared": ("uid-new", NOW)})
    stats = sync_namespace_lifecycle(db)

    row = db.query(Namespace).one()
    assert row.incarnation == 2
    assert row.k8s_uid == "uid-new"
    assert stats["reincarnated"] == 1


def test_reincarnation_is_audited_without_purging_history(db, cluster, monkeypatch):
    """Факт фиксируется в audit, история НЕ трогается — это шаг B6."""
    events = []
    monkeypatch.setattr(
        "app.services.audit_logger.audit_service.log_event",
        lambda t, d: events.append((t, d)),
    )
    db.add(Namespace(namespace="squad-9", k8s_uid="uid-1", incarnation=1))
    db.commit()
    cluster({"squad-9": ("uid-2", NOW)})

    sync_namespace_lifecycle(db)

    assert events and events[0][0] == "KG_NAMESPACE_REINCARNATED"
    payload = events[0][1]
    assert payload["previous_uid"] == "uid-1" and payload["current_uid"] == "uid-2"
    assert payload["history_purged"] is False


def test_same_uid_is_not_a_reincarnation(db, cluster):
    db.add(Namespace(namespace="prod-kingdom1", k8s_uid="uid-stable", incarnation=1))
    db.commit()
    cluster({"prod-kingdom1": ("uid-stable", NOW)})

    stats = sync_namespace_lifecycle(db)
    assert db.query(Namespace).one().incarnation == 1
    assert stats["reincarnated"] == 0


# --- присутствие ----------------------------------------------------------


def test_disappeared_namespace_is_marked_missing(db, cluster):
    db.add(Namespace(namespace="squad-39-shared", k8s_uid="uid-x",
                     state=NS_STATE_ACTIVE))
    db.commit()
    cluster({"prod-kingdom1": ("uid-p", NOW)})

    stats = sync_namespace_lifecycle(db)
    row = db.query(Namespace).filter_by(namespace="squad-39-shared").one()
    assert row.state == NS_STATE_MISSING
    assert row.missing_since is not None
    assert stats["marked_missing"] == 1


def test_missing_since_is_not_reset_on_every_tick(db, cluster):
    """Отсчёт до забвения не должен обнуляться каждым тиком."""
    gone_at = datetime.utcnow() - timedelta(days=10)
    db.add(Namespace(namespace="squad-54-shared", state=NS_STATE_MISSING,
                     missing_since=gone_at, k8s_uid="uid-y"))
    db.commit()
    cluster({"prod-kingdom1": ("uid-p", NOW)})

    sync_namespace_lifecycle(db)
    row = db.query(Namespace).filter_by(namespace="squad-54-shared").one()
    assert row.missing_since == gone_at, "срок отсутствия пересчитали заново"


def test_returned_namespace_becomes_active_again(db, cluster):
    """Сеть моргнула — namespace вернулся с тем же UID, ничего не потеряно."""
    db.add(Namespace(namespace="squad-1", k8s_uid="uid-a", state=NS_STATE_MISSING,
                     missing_since=datetime.utcnow() - timedelta(hours=2),
                     incarnation=1))
    db.commit()
    cluster({"squad-1": ("uid-a", NOW)})

    stats = sync_namespace_lifecycle(db)
    row = db.query(Namespace).one()
    assert row.state == NS_STATE_ACTIVE
    assert row.missing_since is None
    assert row.incarnation == 1, "возврат с тем же UID — не новая инкарнация"
    assert stats["returned"] == 1


# --- отказ источника ------------------------------------------------------


def test_kubectl_failure_marks_nothing(db, monkeypatch):
    """Неизвестное состояние кластера — не повод объявлять стенды исчезнувшими.

    Ровно то, от чего защищал прежний guard по доле, но без его побочного
    эффекта: здесь блокируется только запись, а не уборка навсегда.
    """
    db.add(Namespace(namespace="squad-1", state=NS_STATE_ACTIVE, k8s_uid="uid-a"))
    db.commit()

    def boom():
        raise K8sNamespaceFetchError("kubectl недоступен")

    monkeypatch.setattr(nl, "_fetch_namespaces", boom)
    with pytest.raises(K8sNamespaceFetchError):
        sync_namespace_lifecycle(db)

    assert db.query(Namespace).one().state == NS_STATE_ACTIVE


def test_empty_cluster_response_is_treated_as_failure(monkeypatch):
    """Кластер без namespace невозможен — это сбой, а не «всё исчезло»."""
    class R:
        returncode = 0
        stdout = '{"items": []}'
        stderr = ""

    monkeypatch.setattr(nl, "run_kubectl", lambda *a, **k: R())
    with pytest.raises(K8sNamespaceFetchError):
        nl._fetch_namespaces()


def test_nonzero_rc_is_a_failure(monkeypatch):
    class R:
        returncode = 1
        stdout = ""
        stderr = "connection refused"

    monkeypatch.setattr(nl, "run_kubectl", lambda *a, **k: R())
    with pytest.raises(K8sNamespaceFetchError):
        nl._fetch_namespaces()
