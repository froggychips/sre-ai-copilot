"""Регрессии node_kind-фильтра в health_score (contract 2.4).

С миграцией node_kind у пары «k8s Service foo + Deployment foo» два
non-synthetic узла в kg_services. recompute_all_health без фильтра делал
двойную работу (~9k UPDATE вместо ~4.5k — ровно те row-локи, что кормили
deadlock'и 09-10.08), а top_unhealthy показывал два неразличимых ряда
на одну пару.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.health_score import recompute_all_health, top_unhealthy
from app.knowledge_graph.schema import NODE_KIND_WORKLOAD, Service


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


@pytest.fixture
def svc_and_workload(db):
    """Одноимённая пара после миграции node_kind."""
    svc = Service(name="auth", namespace="prod-shared", synthetic=False)
    twin = Service(name="auth", namespace="prod-shared", synthetic=False,
                   node_kind=NODE_KIND_WORKLOAD)
    db.add_all([svc, twin])
    db.commit()
    return svc, twin


def test_recompute_skips_workload_twin(svc_and_workload, db):
    """Пересчёт трогает только Service-узел: workload-двойник не пересчитывается
    (двойная работа = вдвое больше row-локов на kg_services)."""
    svc, twin = svc_and_workload
    stats = recompute_all_health(db)
    assert stats["real_services"] == 1
    assert stats["recomputed"] == 1
    db.refresh(svc)
    db.refresh(twin)
    assert svc.health_score is not None
    assert twin.health_score is None, (
        "workload-узел не должен получать health_score от recompute"
    )


def test_top_unhealthy_hides_legacy_workload_rows(svc_and_workload, db):
    """Legacy-строки: workload-узлы, пересчитанные ДО фикса, хранят
    health_score навсегда (его никто не чистит) — top_unhealthy не должен
    показывать пару как два неразличимых ряда."""
    svc, twin = svc_and_workload
    svc.health_score = 0.2
    twin.health_score = 0.2  # legacy-значение от пересчёта до фикса
    db.commit()

    rows = top_unhealthy(db, limit=10)
    assert len(rows) == 1
    assert (rows[0]["namespace"], rows[0]["name"]) == ("prod-shared", "auth")
