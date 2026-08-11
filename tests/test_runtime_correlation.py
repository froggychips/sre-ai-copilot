"""Тесты для kg_runtime_correlation_sync — co-occurrence warning-событий
подтверждает existing edges через новый discovery_source.

Покрытие:
- Базовый кейс: 3 события src + 3 dst в окне → confirm срабатывает
- Отсутствие совпадений по окну → не confirm
- Edge без существующего KG-record → не создаём (только update existing)
- Synthetic source/dst исключён
- Idempotency: повторный run не дублирует discovery_sources
- Порог считает ЭПИЗОДЫ: одна авария (сотни пар событий) не подтверждает ребро
- Подтверждение НЕ трогает `last_seen_at` (это признак свежести чужого
  источника, по нему работает decay-guard)
- Нет N+1: src/dst грузятся eager, а не по ребру
- Два указателя считают то же, что полный перебор
"""

from __future__ import annotations

import asyncio
import random
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import List, Optional

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.knowledge_graph import schema  # noqa: F401 — register models
from app.knowledge_graph.runtime_correlation import (
    MIN_CORRELATION_EPISODES,
    RUNTIME_CORRELATION_SOURCE,
    _count_co_occurrences,
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


# ── порог: эпизоды, а не пары событий ───────────────────────────────────────


def test_single_burst_does_not_confirm(db):
    """Одна авария: 10×10 событий в одном окне = 100 «пар», но 1 эпизод.

    Регрессия: порог считал ПАРЫ, поэтому одно совместное падение проходило
    `min_correlation_count=2` с двадцатикратным запасом и навешивало на ребро
    tier-1 source с precedence 0.95.
    """
    now = datetime.utcnow()
    src = _mk_service(db, "auth", "prod-kingdom1")
    dst = _mk_service(db, "town", "prod-kingdom1")
    edge = _mk_edge(db, src, dst)

    base = now - timedelta(hours=3)
    for i in range(10):
        _mk_pod_event(db, src.id, "BackOff", base + timedelta(seconds=i * 10))
        _mk_pod_event(db, dst.id, "Unhealthy", base + timedelta(seconds=i * 10 + 5))
    db.commit()

    stats = asyncio.run(
        run_runtime_correlation_sync(
            db, window_minutes=15, min_correlation_count=2, lookback_days=7, now=now
        )
    )

    assert stats["candidates"] == 0
    assert stats["newly_confirmed"] == 0
    assert stats["min_episodes"] == MIN_CORRELATION_EPISODES
    db.refresh(edge)
    assert RUNTIME_CORRELATION_SOURCE not in (edge.extras or {}).get(
        "discovery_sources", []
    )


def test_three_episodes_confirm_and_record_audit(db):
    """Три РАЗНЫХ эпизода за неделю — уже паттерн, ребро подтверждаем."""
    now = datetime.utcnow()
    src = _mk_service(db, "auth", "prod-kingdom1")
    dst = _mk_service(db, "town", "prod-kingdom1")
    edge = _mk_edge(db, src, dst)

    base = now - timedelta(days=3)
    for i in range(3):
        t = base + timedelta(hours=i * 8)
        _mk_pod_event(db, src.id, "BackOff", t)
        _mk_pod_event(db, dst.id, "OOMKilled", t + timedelta(minutes=2))
    db.commit()

    stats = asyncio.run(
        run_runtime_correlation_sync(
            db, window_minutes=15, min_correlation_count=2, lookback_days=7, now=now
        )
    )
    assert stats["newly_confirmed"] == 1

    db.refresh(edge)
    audit = (edge.extras or {}).get("runtime_correlation") or {}
    assert audit["episodes"] == 3
    assert audit["count"] == 3          # пары остаются в audit-trail
    assert audit["confirmed_at"]        # своё поле вместо last_seen_at
    assert audit["reasons"] == ["BackOff", "OOMKilled"]


# ── подтверждение не подменяет признак свежести источника ───────────────────


def test_confirmation_does_not_refresh_last_seen_at(db):
    """`last_seen_at` — про то, что ребро увидел ЕГО источник, не корреляция.

    Регрессия: корреляция ставила `last_seen_at = now` любому ребру, из-за
    чего env-inferred ребро, которого штатный синк больше не видит, вечно
    оставалось «свежим» и не децаилось, а `edge_decay_guard` видел живой
    источник при мёртвом синке.
    """
    now = datetime.utcnow()
    src = _mk_service(db, "auth", "prod-kingdom1")
    dst = _mk_service(db, "town", "prod-kingdom1")
    edge = _mk_edge(db, src, dst)
    stale_last_seen = edge.last_seen_at

    base = now - timedelta(days=2)
    for i in range(4):
        t = base + timedelta(hours=i * 6)
        _mk_pod_event(db, src.id, "BackOff", t)
        _mk_pod_event(db, dst.id, "Unhealthy", t + timedelta(minutes=1))
    db.commit()

    stats = asyncio.run(run_runtime_correlation_sync(db, now=now))
    assert stats["newly_confirmed"] == 1

    db.refresh(edge)
    assert edge.last_seen_at == stale_last_seen, (
        "runtime-корреляция освежила last_seen_at чужого источника — "
        "ребро больше не децаится"
    )
    assert RUNTIME_CORRELATION_SOURCE in (edge.extras or {}).get(
        "discovery_sources", []
    )


# ── N+1 ─────────────────────────────────────────────────────────────────────


@contextmanager
def _capture_statements(db):
    """Все SQL-запросы сессии за блок."""
    statements: List[str] = []
    engine = db.get_bind()

    def _on_exec(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _on_exec)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", _on_exec)


def test_no_n_plus_one_on_edge_endpoints(db):
    """src/dst грузятся вместе с рёбрами, а не отдельным SELECT'ом на ребро."""
    now = datetime.utcnow()
    hub = _mk_service(db, "auth", "prod-kingdom1")
    spokes = [_mk_service(db, f"svc-{i}", "prod-kingdom1") for i in range(3)]
    for spoke in spokes:
        _mk_edge(db, hub, spoke)

    base = now - timedelta(days=2)
    for i in range(4):
        t = base + timedelta(hours=i * 6)
        _mk_pod_event(db, hub.id, "BackOff", t)
        for spoke in spokes:
            _mk_pod_event(db, spoke.id, "Unhealthy", t + timedelta(minutes=1))
    db.commit()

    with _capture_statements(db) as statements:
        stats = asyncio.run(run_runtime_correlation_sync(db, now=now))
    assert stats["newly_confirmed"] == 3

    # Любое обращение к kg_services ВНЕ джойна с рёбрами = lazy-load конца
    # ребра (было ~2×edges лишних SELECT'ов каждые 30 минут).
    per_row_loads = [
        s for s in statements
        if "FROM kg_services" in s and "kg_service_edges" not in s
    ]
    assert not per_row_loads, (
        f"lazy-load концов ребра: {len(per_row_loads)} лишних SELECT'ов"
    )
    # Ожидаемый профиль: 1 запрос рёбер (+joined src/dst), 4 запроса
    # pod_events (по сервису, с кэшом), 1 count(edges), 1 батч кандидатов,
    # 3 UPDATE'а подтверждённых рёбер.
    assert len(statements) <= 10, statements


# ── два указателя == полный перебор ─────────────────────────────────────────


def _brute_force(src_events, dst_events, window):
    window_sec = window.total_seconds()
    count = 0
    last_at = None
    reasons = set()
    for s_ev in src_events:
        for d_ev in dst_events:
            if abs((d_ev.first_seen - s_ev.first_seen).total_seconds()) < window_sec:
                count += 1
                latest = max(s_ev.first_seen, d_ev.first_seen)
                if last_at is None or latest > last_at:
                    last_at = latest
                reasons.add(s_ev.reason)
                reasons.add(d_ev.reason)
    return count, last_at, sorted(reasons)


def test_two_pointer_equals_bruteforce():
    """Оптимизация не меняет результат: count/last_at/reasons совпадают."""
    rnd = random.Random(20260810)
    base = datetime(2026, 8, 3, 0, 0, 0)
    reasons_pool = ["BackOff", "Unhealthy", "OOMKilled", "ImagePullBackOff"]

    def _events(n):
        evs = [
            schema.PodEvent(
                namespace="prod-kingdom1",
                pod_name="p",
                reason=rnd.choice(reasons_pool),
                event_uid=uuid.uuid4().hex,
                first_seen=base + timedelta(minutes=rnd.randint(0, 7 * 24 * 60)),
            )
            for _ in range(n)
        ]
        return sorted(evs, key=lambda e: e.first_seen)

    window = timedelta(minutes=15)
    for n_src, n_dst in ((7, 11), (40, 3), (1, 1), (25, 25)):
        src_events, dst_events = _events(n_src), _events(n_dst)
        count, last_at, ev_reasons, episodes = _count_co_occurrences(
            src_events, dst_events, window,
        )
        assert (count, last_at, ev_reasons) == _brute_force(
            src_events, dst_events, window,
        )
        assert 0 <= episodes <= count
