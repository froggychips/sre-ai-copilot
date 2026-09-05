"""Переатрибуция kg_deployments по реальной цели билда.

Записи, сделанные до фикса, привязаны к namespace по VCS-ветке — у
deploy-конфигов она литеральный `<default>`, и нормализация в `preprod` была
догадкой. Замер на проде 22.08.2026: 441 билд, 124 911 записей (≈596 на
билд), и `BuildAndDeploy #2917`, деплоивший squad-1, записан на preprod-* и
squad-gd-* без единого сервиса squad-1.
"""
from datetime import datetime
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.deploy_reattribution import reattribute_deployments
from app.knowledge_graph.schema import Deployment, Service

STARTED = datetime(2026, 8, 21, 8, 41, 20)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _svc(db, name, ns):
    s = Service(name=name, namespace=ns, synthetic=False)
    db.add(s)
    db.flush()
    return s


def _dep(db, svc, *, bt="Wo_Backend_K8sNewCluster_BuildAndDeploy", num="2917",
         scope=True):
    d = Deployment(
        service_id=svc.id, started_at=STARTED, buildtype_id=bt,
        build_number=num, status="SUCCESS", triggered_by="sgrozov",
        extras={"branch": "preprod", "namespace_scope": True} if scope
        else {"branch": "preprod"},
    )
    db.add(d)
    db.flush()
    return d


def _target(realm, service=None):
    return patch(
        "app.knowledge_graph.deploy_reattribution.fetch_build_target",
        return_value=(realm, service),
    )


def test_records_move_from_the_wrong_realm_to_the_right_one(db):
    """Деплой squad-1 должен оказаться у squad-1, а не у препрода."""
    wrong = _svc(db, "town-service", "preprod-shared")
    right = _svc(db, "town-service", "squad-1-shared")
    _dep(db, wrong)
    db.commit()

    with _target("squad-1"):
        stats = reattribute_deployments(db, apply=True)

    assert stats["rows_deleted"] == 1
    assert stats["rows_created"] == 1
    owners = {
        db.get(Service, d.service_id).namespace
        for d in db.query(Deployment).all()
    }
    assert owners == {"squad-1-shared"}
    assert right.id and wrong.id


def test_single_service_target_drops_the_rest(db):
    """SERVICE_NAME задан — остальные 595 записей были небылицами."""
    target = _svc(db, "chat-message-service", "squad-27-shared")
    other = _svc(db, "town-service", "squad-27-shared")
    _dep(db, target, bt="Wo_Backend_K8sNewCluster_MigrateAndUpdateService", num="103")
    _dep(db, other, bt="Wo_Backend_K8sNewCluster_MigrateAndUpdateService", num="103")
    db.commit()

    with _target("squad-27", "chat-message-service"):
        stats = reattribute_deployments(db, apply=True)

    assert stats["rows_deleted"] == 1
    names = {db.get(Service, d.service_id).name for d in db.query(Deployment).all()}
    assert names == {"chat-message-service"}


def test_build_unknown_to_teamcity_is_left_alone(db):
    """TC не ответил или билд выпал из retention — цель неизвестна.

    Удалять по незнанию нельзя: отсутствие записи — тоже утверждение, просто
    другое.
    """
    svc = _svc(db, "town-service", "preprod-shared")
    _dep(db, svc)
    db.commit()

    with patch("app.knowledge_graph.deploy_reattribution.fetch_build_target",
               return_value=None):
        stats = reattribute_deployments(db, apply=True)

    assert stats["builds_unknown"] == 1
    assert stats["rows_deleted"] == 0
    assert db.query(Deployment).count() == 1


def test_records_without_broadcast_marker_are_untouched(db):
    """Точечные записи переатрибутировать не за что."""
    svc = _svc(db, "town-service", "preprod-shared")
    _svc(db, "town-service", "squad-1-shared")
    _dep(db, svc, scope=False)
    db.commit()

    with _target("squad-1"):
        stats = reattribute_deployments(db, apply=True)

    assert stats["rows_deleted"] == 0
    assert db.query(Deployment).count() == 1


def test_dry_run_writes_nothing(db):
    wrong = _svc(db, "town-service", "preprod-shared")
    _svc(db, "town-service", "squad-1-shared")
    _dep(db, wrong)
    db.commit()

    with _target("squad-1"):
        stats = reattribute_deployments(db, apply=False)

    assert stats["applied"] is False
    assert stats["rows_deleted"] == 1 and stats["rows_created"] == 1
    assert db.query(Deployment).count() == 1     # но ничего не переписано


def test_second_run_is_a_noop(db):
    """Идемпотентность: на исправленных данных менять нечего."""
    wrong = _svc(db, "town-service", "preprod-shared")
    _svc(db, "town-service", "squad-1-shared")
    _dep(db, wrong)
    db.commit()

    with _target("squad-1"):
        reattribute_deployments(db, apply=True)
        again = reattribute_deployments(db, apply=True)

    assert again["rows_deleted"] == 0
    assert again["rows_created"] == 0


def test_realm_absent_from_graph_leaves_history_as_is(db):
    """Реальма в графе нет — не превращаем неверную историю в пустую."""
    svc = _svc(db, "town-service", "preprod-shared")
    _dep(db, svc)
    db.commit()

    with _target("squad-99"):
        stats = reattribute_deployments(db, apply=True)

    assert stats["rows_deleted"] == 0
    assert db.query(Deployment).count() == 1


def test_realm_prefix_does_not_swallow_similar_names(db):
    """squad-270 не относится к squad-27."""
    near = _svc(db, "town-service", "squad-270-shared")
    right = _svc(db, "town-service", "squad-27-shared")
    _dep(db, near)
    db.commit()

    with _target("squad-27"):
        reattribute_deployments(db, apply=True)

    owners = {
        db.get(Service, d.service_id).namespace
        for d in db.query(Deployment).all()
    }
    assert owners == {"squad-27-shared"}
    assert right.id


def test_correct_row_of_single_service_build_loses_broadcast_marker(db):
    """Запись стоит на целевом сервисе — она доказательство его деплоя.

    Пока на ней висит `namespace_scope`, `stale_classifier` обязан читать её
    как «в namespace что-то каталось» и не может выдать `active`. Замер
    05.09.2026: 36 записей с известным `target_service`, и все с маркером.
    """
    svc = _svc(db, "chat-message-service", "squad-27-shared")
    _dep(db, svc, bt="Wo_Backend_K8sNewCluster_MigrateAndUpdateService",
         num="103")
    db.commit()

    with _target("squad-27", "chat-message-service"):
        stats = reattribute_deployments(db, apply=True)

    assert stats["rows_kept"] == 1
    assert stats["rows_unmarked"] == 1
    assert db.query(Deployment).one().extras["namespace_scope"] is False


def test_ns_wide_build_keeps_marker_on_its_rows(db):
    """Целевого сервиса у билда нет — записи остаются broadcast'ом."""
    a = _svc(db, "town-service", "squad-1-shared")
    b = _svc(db, "chat-message-service", "squad-1-shared")
    _dep(db, a)
    _dep(db, b)
    db.commit()

    with _target("squad-1"):
        stats = reattribute_deployments(db, apply=True)

    assert stats["rows_unmarked"] == 0
    assert all(d.extras["namespace_scope"] is True
               for d in db.query(Deployment))


def test_created_row_of_single_service_build_is_not_a_broadcast(db):
    """Дозаписанной строке маркер из broadcast-шаблона не копируется."""
    _svc(db, "chat-message-service", "preprod-shared")
    right = _svc(db, "chat-message-service", "squad-27-shared")
    _dep(db, db.query(Service).filter_by(namespace="preprod-shared").one(),
         bt="Wo_Backend_K8sNewCluster_MigrateAndUpdateService", num="103")
    db.commit()

    with _target("squad-27", "chat-message-service"):
        stats = reattribute_deployments(db, apply=True)

    assert stats["rows_created"] == 1
    created = db.query(Deployment).one()
    assert created.service_id == right.id
    assert created.extras["namespace_scope"] is False
