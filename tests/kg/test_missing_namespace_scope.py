"""Обходы «по всем реальным сервисам» не видят узлы снесённых namespace.

Кейс squad-42 (08.09.2026): namespace удалены накануне, а у 31 узла были
health_computed_at «сегодня» и свежие health-точки — metrics_sync и
health_score шли по kg_services, не глядя в kg_namespaces, и VM по
несуществующему namespace отдавал пустоту, которая записывалась как
измерение.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.health_score import recompute_all_health
from app.knowledge_graph.schema import (NS_STATE_ACTIVE, NS_STATE_MISSING,
                                        Namespace, Service)


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


def test_health_recompute_skips_services_of_missing_namespace(db):
    db.add_all([
        Namespace(namespace="squad-42-shared", state=NS_STATE_MISSING),
        Namespace(namespace="prod-shared", state=NS_STATE_ACTIVE),
        Service(name="town-service", namespace="squad-42-shared", synthetic=False),
        Service(name="auth", namespace="prod-shared", synthetic=False),
    ])
    db.commit()

    stats = recompute_all_health(db)

    assert stats["real_services"] == 1
    assert stats["skipped_missing_ns"] == 1
    gone = db.query(Service).filter_by(namespace="squad-42-shared").one()
    alive = db.query(Service).filter_by(namespace="prod-shared").one()
    assert gone.health_score is None, "снесённый стенд не должен получать свежий health_score"
    assert alive.health_score is not None


def test_unknown_namespace_is_not_treated_as_missing(db):
    """Namespace, которого нет в kg_namespaces вовсе (строка ещё не заведена
    lifecycle'ом), — не «пропавший»: обход его не пропускает."""
    db.add(Service(name="auth", namespace="brand-new-ns", synthetic=False))
    db.commit()

    stats = recompute_all_health(db)
    assert stats["real_services"] == 1
    assert stats["skipped_missing_ns"] == 0
