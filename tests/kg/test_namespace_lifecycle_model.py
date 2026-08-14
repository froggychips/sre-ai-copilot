"""kg_namespaces: namespace как объект, а не строка-текст в kg_services.

Шаг B1 жизненного цикла — только модель и backfill, без переходов состояний.
Порядок намеренный: сначала наблюдение, потом действие. Пока таблица лишь
фиксирует, что граф видел, — удалять по ней ничего нельзя.

Три поломки, ради которых она заводится (замер прода 14.08.2026):
  * выход не отслеживался — 198 namespace в графе против 139 живых;
  * пересоздание невидимо — `squad-1-shared` имеет узлы на 82 дня старше
    самого namespace, к ним прилипло 39 775 health-точек прошлой жизни;
  * уборка блокировала сама себя — guard по доле (29.8% при пороге 20%).
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.schema import (NS_STATE_ACTIVE, NS_STATE_MISSING,
                                        NS_STATE_RETIRED, NS_STATES, Namespace,
                                        Service)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_states_are_exactly_three():
    """Состояний ровно три: живой, исчез, забыт. Четвёртого смысла нет."""
    assert NS_STATES == (NS_STATE_ACTIVE, NS_STATE_MISSING, NS_STATE_RETIRED)


def test_defaults_are_active_and_first_incarnation(db):
    db.add(Namespace(namespace="squad-1-shared"))
    db.commit()

    ns = db.query(Namespace).one()
    assert ns.state == NS_STATE_ACTIVE
    assert ns.incarnation == 1
    assert ns.missing_since is None, "у живого namespace нет времени исчезновения"


def test_uid_is_nullable_for_legacy_rows(db):
    """UID неизвестен у строк, заведённых до того, как синк начал его читать.

    NULL честнее выдумки: инкарнацию таких namespace определить нечем.
    """
    db.add(Namespace(namespace="squad-2", k8s_uid=None))
    db.commit()
    assert db.query(Namespace).one().k8s_uid is None


def test_incarnation_and_uid_travel_together(db):
    """Смена UID при том же имени — это другой стенд, а не тот же самый."""
    db.add(Namespace(namespace="squad-34-shared", k8s_uid="uid-first", incarnation=1))
    db.commit()

    ns = db.query(Namespace).one()
    ns.k8s_uid = "uid-second"
    ns.incarnation = 2
    db.commit()

    fresh = db.query(Namespace).one()
    assert (fresh.k8s_uid, fresh.incarnation) == ("uid-second", 2)


def test_missing_since_enables_ttl_by_time(db):
    """TTL считается по времени, а не по доле — в этом суть замены guard'а.

    Прежний предохранитель (`drift_pct > 20%`) блокировал уборку ровно тогда,
    когда мусора накопилось больше всего. Время такой петли не создаёт:
    для забвения нужны сотни подтверждений за месяц.
    """
    gone_at = datetime.utcnow() - timedelta(days=31)
    db.add(Namespace(namespace="squad-39-shared", state=NS_STATE_MISSING,
                     missing_since=gone_at))
    db.commit()

    ns = db.query(Namespace).one()
    age_days = (datetime.utcnow() - ns.missing_since).days
    assert age_days >= 30, "по missing_since считается срок до retired"


def test_k8s_created_at_is_independent_of_node_age(db):
    """Возраст namespace и возраст узлов — разные вещи.

    `squad-1-shared`: namespace создан 05.08, а узлы в графе — с 15.05.
    Пока возраст брали из узлов, пересозданный стенд выглядел трёхмесячным.
    """
    ns_created = datetime(2026, 8, 5, 12, 58)
    node_created = datetime(2026, 5, 15, 18, 22)

    db.add(Namespace(namespace="squad-1-shared", k8s_created_at=ns_created))
    db.add(Service(namespace="squad-1-shared", name="api", node_kind="service",
                   created_at=node_created))
    db.commit()

    ns = db.query(Namespace).one()
    svc = db.query(Service).one()
    assert ns.k8s_created_at > svc.created_at, (
        "namespace моложе своих узлов — признак пересоздания, и он должен "
        "быть виден"
    )


def test_namespace_is_primary_key(db):
    """Ключ — имя: по нему идут все существующие джойны графа."""
    db.add(Namespace(namespace="squad-1"))
    db.commit()
    assert db.query(Namespace).filter_by(namespace="squad-1").one() is not None
