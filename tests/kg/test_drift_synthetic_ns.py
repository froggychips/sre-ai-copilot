"""drift_cleanup: синтетический namespace не считается дрейфом.

Ревью 2026-08-10: `kg_ns` собирался как distinct по `kg_services.namespace`,
включая synthetic-ns `nats-subjects` (контейнер subject-узлов из
nats_subjects_sync). В k8s такого namespace нет по построению, поэтому он
попадал в drift КАЖДЫЙ прогон:

  * subject-узлы получали `drift_reason=ns_not_in_k8s` + `drift_marked_at`
    и метились synthetic (они и так synthetic, но метаданные врали о причине);
  * `drift_pct` был завышен на постоянную величину — а это тот самый
    процент, по которому срабатывает safety-threshold.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.drift_cleanup import run_drift_cleanup
from app.knowledge_graph.nats_subjects_sync import NATS_SUBJECTS_NAMESPACE
from app.knowledge_graph.schema import NS_STATE_MISSING, Namespace, Service


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


def _seed(db):
    db.add_all([
        Service(namespace="prod-shared", name="town", synthetic=False),
        Service(namespace="dev-shared", name="town", synthetic=False),
        Service(
            namespace=NATS_SUBJECTS_NAMESPACE, name="subject:ping",
            synthetic=True,
        ),
    ])
    db.commit()


def test_synthetic_ns_never_in_drift(db):
    """Все реальные ns живы → drift пуст, несмотря на `nats-subjects` в KG."""
    _seed(db)
    with patch(
        "app.knowledge_graph.drift_cleanup._k8s_live_namespaces",
        return_value={"prod-shared", "dev-shared"},
    ):
        stats = run_drift_cleanup(db, apply=True)

    assert stats["drift_ns"] == []
    assert stats["drift_pct"] == 0.0
    assert stats["kg_ns_count"] == 2, "синтетический ns не должен считаться"
    assert stats["marked_services"] == 0


def test_subject_nodes_do_not_get_drift_metadata(db):
    """Subject-узлы не получают drift_reason даже когда есть реальный дрейф."""
    _seed(db)
    db.add(Service(namespace="squad-gone", name="ghost", synthetic=False))
    # Пометка ставится только по подтверждённому отсутствию: ns должен
    # числиться `missing` в lifecycle дольше grace-периода.
    db.add(Namespace(
        namespace="squad-gone", state=NS_STATE_MISSING, incarnation=1,
        first_seen_at=datetime(2026, 8, 1), last_seen_at=datetime(2026, 8, 20),
        missing_since=datetime(2026, 8, 20),
    ))
    db.commit()

    with patch(
        "app.knowledge_graph.drift_cleanup._k8s_live_namespaces",
        return_value={"prod-shared", "dev-shared"},
    ):
        stats = run_drift_cleanup(db, max_drift_pct=40.0, apply=True)

    assert stats["drift_ns"] == ["squad-gone"]
    assert stats["marked_services"] == 1

    subject = (
        db.query(Service)
        .filter_by(namespace=NATS_SUBJECTS_NAMESPACE, name="subject:ping")
        .one()
    )
    assert "drift_reason" not in (subject.metadata_json or {})
    ghost = db.query(Service).filter_by(name="ghost").one()
    assert ghost.metadata_json["drift_reason"] == "ns_not_in_k8s"


def test_drift_pct_not_inflated_by_synthetic_ns(db):
    """Знаменатель drift_pct — только реальные ns.

    С учётом `nats-subjects` было бы 1/4=25% (и порог 20% срубил бы чистку),
    без него 1/3=33.33% — честная величина по реальным ns.
    """
    _seed(db)
    db.add(Service(namespace="squad-gone", name="ghost", synthetic=False))
    db.commit()

    with patch(
        "app.knowledge_graph.drift_cleanup._k8s_live_namespaces",
        return_value={"prod-shared", "dev-shared"},
    ):
        stats = run_drift_cleanup(db, max_drift_pct=40.0, apply=False)

    assert stats["kg_ns_count"] == 3
    assert stats["drift_pct"] == 33.33
