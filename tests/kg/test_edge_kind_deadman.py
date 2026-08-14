"""Deadman на источник каждого вида рёбер.

Агрегатная проверка `check_edges_freshness` считает долю просроченных рёбер по
ВСЕМУ графу с порогом 30% — и по арифметике не может заметить смерть отдельного
источника. Живой граф 14.08.2026:

    serves_traffic  5336  31.2%
    uses_db         4459  26.0%
    uses_nats       3560  20.8%
    calls           2096  12.2%
    routes_to       1672   9.8%

Полная остановка NATS-синка даёт 20.8% просроченных рёбер — ниже порога, то
есть агрегат промолчит и через сутки, и через неделю. Заметен ему только
тотальный отказ kg_sync целиком.

Это тот же класс слепоты, что стоил суток простоя deploy-stream: проверка
существовала и рапортовала `ok`, потому что смотрела не туда.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.self_health import (_EDGE_KIND_SOURCES,
                                             check_edge_kind_freshness,
                                             check_edges_freshness)
from app.knowledge_graph.schema import Service, ServiceEdge


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _seed_edges(db, kind: str, count: int, age_minutes: float) -> None:
    """count рёбер вида kind, последний раз виденных age_minutes назад."""
    last_seen = datetime.utcnow() - timedelta(minutes=age_minutes)
    for i in range(count):
        src = Service(namespace="squad-1", name=f"{kind}-src-{i}", node_kind="service")
        dst = Service(namespace="squad-1", name=f"{kind}-dst-{i}", node_kind="service")
        db.add_all([src, dst])
        db.flush()
        db.add(ServiceEdge(src_id=src.id, dst_id=dst.id, kind=kind, last_seen_at=last_seen))
    db.commit()


def test_fresh_graph_is_ok(db):
    for kind in _EDGE_KIND_SOURCES:
        _seed_edges(db, kind, 2, age_minutes=5)
    assert check_edge_kind_freshness(db).status == "ok"


def test_dead_nats_sync_is_caught(db):
    """Главный сценарий: NATS-синк умер, остальные живы."""
    _seed_edges(db, "uses_nats", 3560, age_minutes=60 * 24)
    for kind in ("calls", "uses_db", "serves_traffic", "routes_to"):
        _seed_edges(db, kind, 5, age_minutes=5)

    result = check_edge_kind_freshness(db)
    assert result.status == "fail"
    nats = result.detail["per_kind"]["uses_nats"]
    assert nats["status"] == "fail"
    assert nats["writer_task"] == "kg_nats_subjects_sync", (
        "алерт обязан называть таск, который чинить"
    )


def test_aggregate_check_stays_silent_on_the_same_data(db):
    """Ровно та слепота, ради которой написана per-kind проверка.

    Доли взяты из живого графа: uses_nats — 20.8% рёбер, то есть его полная
    смерть не поднимает агрегатный порог 30%.
    """
    _seed_edges(db, "uses_nats", 208, age_minutes=60 * 48)   # мёртв двое суток
    _seed_edges(db, "calls", 792, age_minutes=5)             # остальные свежие

    assert check_edges_freshness(db).status == "ok", (
        "агрегатная проверка не должна замечать — в этом и проблема"
    )
    assert check_edge_kind_freshness(db).status == "fail", (
        "а per-kind обязана"
    )


def test_warn_before_fail(db):
    """Между «чуть отстал» и «мёртв» есть разница: warn при >1×, fail при >2×."""
    threshold = _EDGE_KIND_SOURCES["uses_nats"]["stale_after_minutes"]
    _seed_edges(db, "uses_nats", 3, age_minutes=threshold * 1.5)

    result = check_edge_kind_freshness(db)
    assert result.detail["per_kind"]["uses_nats"]["status"] == "warn"
    assert result.status == "warn"


def test_missing_kind_is_not_a_failure(db):
    """Вида нет вовсе — не сбой: routes_to появился позже остальных."""
    _seed_edges(db, "calls", 2, age_minutes=5)

    result = check_edge_kind_freshness(db)
    assert result.status == "ok"
    assert result.detail["per_kind"]["routes_to"]["edges"] == 0


def test_null_last_seen_is_a_failure(db):
    """Рёбра есть, но время последнего наблюдения не пишется — тоже слепота."""
    src = Service(namespace="squad-1", name="a", node_kind="service")
    dst = Service(namespace="squad-1", name="b", node_kind="service")
    db.add_all([src, dst])
    db.flush()
    db.add(ServiceEdge(src_id=src.id, dst_id=dst.id, kind="uses_nats", last_seen_at=None))
    db.commit()

    result = check_edge_kind_freshness(db)
    assert result.detail["per_kind"]["uses_nats"]["status"] == "fail"


def test_new_edge_kind_is_reported_as_unmapped(db):
    """Завели kind, не описав источник — deadman его не покрывает, это видно."""
    _seed_edges(db, "depends_on_totally_new", 2, age_minutes=5)

    result = check_edge_kind_freshness(db)
    assert "depends_on_totally_new" in result.detail["unmapped_kinds"]


def test_every_mapped_kind_names_a_writer_task():
    """Карта источников бесполезна, если не говорит, что чинить."""
    for kind, cfg in _EDGE_KIND_SOURCES.items():
        assert cfg.get("task"), f"у {kind} не указан пишущий таск"
        assert cfg.get("stale_after_minutes", 0) > 0, f"у {kind} нет порога"


def test_check_is_registered_in_self_health():
    """Проверка, не включённая в набор, не выполняется никогда."""
    from app.knowledge_graph.self_health import _ALL_CHECKS

    assert check_edge_kind_freshness in _ALL_CHECKS
