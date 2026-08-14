"""Orphan, разрезанный по средам, — и почему агрегат без разреза вводит в заблуждение.

Замер живого графа 14.08.2026:

    squad        4447 узлов  61.7% orphan
    preupdate     191        57.1%
    preprod       141        52.5%
    prod          160        13.8%
    infra/other    32        93.8%
    ─────────────────────────────────
    всего        4971        59.9%

Общая цифра почти целиком описывает связность эфемерных dev-сквадов: они дают
89% знаменателя. Цель «снизить orphan до 10%» на агрегате означает «дорисовать
рёбра стендам, живущим несколько дней», при том что на проде — там, где blast
radius действительно спрашивают, — orphan уже 13.8%.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.contract import (compute_orphan_stats,
                                          compute_orphan_stats_by_env,
                                          env_of_namespace)
from app.knowledge_graph.populator import upsert_edge, upsert_service
from app.knowledge_graph.schema import NODE_KIND_SERVICE


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


@pytest.mark.parametrize("namespace,env", [
    ("prod-kingdom1", "prod"),
    ("preprod-kingdom1", "preprod"),
    ("preupdate-kingdom5", "preupdate"),
    ("squad-4-kingdom2", "squad"),
    ("squad-1-shared", "squad"),
    ("mcp", "infra/other"),
    ("sre-ai", "infra/other"),
    ("", "infra/other"),
    (None, "infra/other"),
])
def test_env_classification(namespace, env):
    assert env_of_namespace(namespace) == env


def test_preupdate_is_not_swallowed_by_prod_prefix():
    """`preupdate-` и `preprod-` не должны попадать в `prod` по подстроке."""
    assert env_of_namespace("preupdate-kingdom5") != "prod"
    assert env_of_namespace("preprod-kingdom1") != "prod"


def _seed(db):
    """Мини-граф: prod связный, squad — нет."""
    prod_a = upsert_service(db, "prod-kingdom1", "api", node_kind=NODE_KIND_SERVICE)
    prod_b = upsert_service(db, "prod-kingdom1", "town", node_kind=NODE_KIND_SERVICE)
    upsert_edge(db, prod_a, prod_b, "calls")
    for i in range(3):
        upsert_service(db, f"squad-{i}", "api", node_kind=NODE_KIND_SERVICE)
    upsert_service(db, "mcp", "tools", node_kind=NODE_KIND_SERVICE)
    db.commit()


def test_split_matches_aggregate(db):
    """Сумма разреза равна агрегату — иначе одна из метрик врёт."""
    _seed(db)
    total = compute_orphan_stats(db)
    by_env = compute_orphan_stats_by_env(db)

    assert sum(s["app_scope"] for s in by_env.values()) == total["app_scope"]
    assert sum(s["orphan"] for s in by_env.values()) == total["orphan"]


def test_prod_and_squad_are_reported_separately(db):
    _seed(db)
    by_env = compute_orphan_stats_by_env(db)

    assert by_env["prod"]["app_scope"] == 2
    assert by_env["prod"]["orphan"] == 0, "связанные prod-узлы не должны быть orphan"
    assert by_env["squad"]["orphan"] == 3
    assert by_env["squad"]["orphan_pct"] == 100.0
    assert by_env["infra/other"]["orphan"] == 1


def test_aggregate_hides_healthy_prod(db):
    """Смысл разреза: агрегат выглядит плохо, хотя прод в порядке."""
    _seed(db)
    total = compute_orphan_stats(db)
    by_env = compute_orphan_stats_by_env(db)

    assert total["orphan_pct"] > 50, "агрегат утянут сквадами"
    assert by_env["prod"]["orphan_pct"] == 0.0, "а прод при этом чистый"


def test_empty_graph_gives_empty_split(db):
    assert compute_orphan_stats_by_env(db) == {}


def test_synthetic_and_expected_stale_excluded(db):
    """Знаменатель тот же, что у агрегата: synthetic и expected_stale вне scope.

    stale_class выставляем прямо в модели: этот PR не зависит от появления
    параметра в upsert_service (см. отдельный PR про единый write path).
    """
    upsert_service(db, "prod-kingdom1", "backup-cron", synthetic=True)
    pg = upsert_service(db, "prod-kingdom1", "postgres")
    pg.stale_class = "expected_stale"
    db.commit()

    assert compute_orphan_stats_by_env(db) == {}
