"""Тесты для kg_runtime_correlation_sync — co-occurrence warning-событий
подтверждает existing edges через новый discovery_source.

Покрытие:
- Базовый кейс: 3 события src + 3 dst в окне → confirm срабатывает
- Отсутствие совпадений по окну → не confirm
- Edge без существующего KG-record → не создаём (только update existing)
- Synthetic source/dst исключён
- Idempotency: повторный run не дублирует discovery_sources
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta
from typing import Optional

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.knowledge_graph import schema  # noqa: F401 — register models
from app.knowledge_graph.runtime_correlation import (
    RUNTIME_CORRELATION_SOURCE,
    run_runtime_correlation_sync,
)


@pytest.fixture
def db():
    """SQLite in-memory с полной схемой KG."""
    eng = create_engine("sqlite:///:memory:")
    schema.Base.metadata.create_all(eng)
    SessionMaker = sessionmaker(bind=eng)
    s = SessionMaker()
    try:
        yield s
    finally:
        s.close()
        eng.dispose()


def _mk_service(db, name: str, ns: str, *, synthetic: bool = False, id: Optional[int] = None):
    svc = schema.Service(
        name=name,
        namespace=ns,
        team_owner=ns.split("-")[0] if "-" in ns else ns,
        synthetic=synthetic,
        metadata_json={},
    )
    db.add(svc)
    db.flush()
    return svc


def _mk_edge(db, src, dst, kind: str = "calls"):
    edge = schema.ServiceEdge(
        src_id=src.id,
        dst_id=dst.id,
        kind=kind,
        weight=1.0,
        extras={"discovery_sources": ["env_inferred"]},
        last_seen_at=datetime.utcnow() - timedelta(days=1),
    )
    db.add(edge)
    db.flush()
    return edge


def _mk_pod_event(db, service_id: int, reason: str, when: datetime, count: int = 1):
    ev = schema.PodEvent(
        service_id=service_id,
        namespace="ns",
        pod_name=f"pod-{reason}",
        reason=reason,
        message=f"{reason} happened",
        type="Warning",
        event_uid=uuid.uuid4().hex,
        first_seen=when,
        last_seen=when,
        count=count,
    )
    db.add(ev)
    db.flush()
    return ev


def test_co_occurrence_confirms_edge(db):
    """3+3 события в окне 5 мин → edge получает runtime_correlation source."""
    now = datetime.utcnow()
    src = _mk_service(db, "auth", "prod-kingdom1")
    dst = _mk_service(db, "town", "prod-kingdom1")
    edge = _mk_edge(db, src, dst)

    base = now - timedelta(hours=2)
    for i in range(3):
        t = base + timedelta(minutes=i * 30)
        _mk_pod_event(db, src.id, "BackOff", t)
        _mk_pod_event(db, dst.id, "Unhealthy", t + timedelta(minutes=2))

    db.commit()

    stats = asyncio.run(
        run_runtime_correlation_sync(
            db,
            window_minutes=15,
            min_correlation_count=2,
            lookback_days=7,
            now=now,
        )
    )

    assert stats["newly_confirmed"] == 1
    db.refresh(edge)
    sources = (edge.extras or {}).get("discovery_sources", [])
    assert RUNTIME_CORRELATION_SOURCE in sources


def test_no_overlap_no_confirm(db):
    """События src и dst в разное время (1+ час) → нет confirm."""
    now = datetime.utcnow()
    src = _mk_service(db, "auth", "prod-kingdom1")
    dst = _mk_service(db, "town", "prod-kingdom1")
    edge = _mk_edge(db, src, dst)

    base = now - timedelta(hours=4)
    for i in range(3):
        _mk_pod_event(db, src.id, "BackOff", base + timedelta(minutes=i * 10))
        _mk_pod_event(db, dst.id, "Unhealthy", base + timedelta(hours=2, minutes=i * 10))

    db.commit()

    stats = asyncio.run(
        run_runtime_correlation_sync(
            db, window_minutes=15, min_correlation_count=2, lookback_days=7, now=now
        )
    )

    assert stats["newly_confirmed"] == 0
    db.refresh(edge)
    sources = (edge.extras or {}).get("discovery_sources", [])
    assert RUNTIME_CORRELATION_SOURCE not in sources


def test_no_edge_no_confirmation(db):
    """Co-occurring события без edge → ничего не создаётся."""
    now = datetime.utcnow()
    src = _mk_service(db, "auth", "prod-kingdom1")
    dst = _mk_service(db, "town", "prod-kingdom1")

    base = now - timedelta(hours=1)
    for i in range(3):
        t = base + timedelta(minutes=i * 5)
        _mk_pod_event(db, src.id, "BackOff", t)
        _mk_pod_event(db, dst.id, "Unhealthy", t + timedelta(minutes=2))
    db.commit()

    stats = asyncio.run(
        run_runtime_correlation_sync(
            db, window_minutes=15, min_correlation_count=2, lookback_days=7, now=now
        )
    )

    # Нет edges → newly_confirmed == 0; corr-функция может вернуть кандидата
    # но _apply_confirmation на отсутствующем edge не сработает.
    assert stats["newly_confirmed"] == 0


def test_synthetic_excluded(db):
    """Synthetic services (vm-*) исключены из корреляции."""
    now = datetime.utcnow()
    src = _mk_service(db, "vm-kube-state-metrics", "monitoring", synthetic=True)
    dst = _mk_service(db, "town", "prod-kingdom1")
    edge = _mk_edge(db, src, dst)

    base = now - timedelta(hours=1)
    for i in range(3):
        t = base + timedelta(minutes=i * 5)
        _mk_pod_event(db, src.id, "BackOff", t)
        _mk_pod_event(db, dst.id, "Unhealthy", t + timedelta(minutes=2))
    db.commit()

    stats = asyncio.run(
        run_runtime_correlation_sync(
            db, window_minutes=15, min_correlation_count=2, lookback_days=7, now=now
        )
    )

    assert stats["newly_confirmed"] == 0
    db.refresh(edge)
    sources = (edge.extras or {}).get("discovery_sources", [])
    assert RUNTIME_CORRELATION_SOURCE not in sources


def test_idempotency_no_duplicate_source(db):
    """Повторный run на том же стейте не дублирует discovery_source."""
    now = datetime.utcnow()
    src = _mk_service(db, "auth", "prod-kingdom1")
    dst = _mk_service(db, "town", "prod-kingdom1")
    edge = _mk_edge(db, src, dst)

    base = now - timedelta(hours=2)
    for i in range(3):
        t = base + timedelta(minutes=i * 30)
        _mk_pod_event(db, src.id, "BackOff", t)
        _mk_pod_event(db, dst.id, "Unhealthy", t + timedelta(minutes=2))
    db.commit()

    asyncio.run(run_runtime_correlation_sync(db, now=now))
    asyncio.run(run_runtime_correlation_sync(db, now=now))

    db.refresh(edge)
    sources = (edge.extras or {}).get("discovery_sources", [])
    assert sources.count(RUNTIME_CORRELATION_SOURCE) == 1
