"""Blast radius обязан говорить, насколько он уверен.

До 15.08.2026 это был единственный публичный запрос графа без достоверности:
`upstream_of` отдаёт `confidence_score` с июня, а здесь список имён выглядел
одинаково уверенно независимо от того, прочитано ребро из k8s-манифеста или
угадано по имени секрета.

Между тем «кого заденет» — самый дорогой вопрос к графу. Именно в нём ошибка с
`db:postgres:config` (1430 рёбер, прод показан клиентом базы препрода)
выглядела достоверной: источник ребра честно лежал в данных, но ответ его не
показывал.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.queries import blast_radius_for
from app.knowledge_graph.schema import (NODE_KIND_SERVICE, NODE_KIND_WORKLOAD,
                                        Service, ServiceEdge)

NS = "prod-kingdom1"
NAME = "town-service"


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(Service(id=1, namespace=NS, name=NAME, node_kind=NODE_KIND_SERVICE))
    s.add(Service(id=2, namespace=NS, name=NAME, node_kind=NODE_KIND_WORKLOAD))
    s.commit()
    return s


def _entry(db, src_name, sources, node_id):
    """Service-узел, маршрутизирующий трафик на workload упавшего сервиса."""
    db.add(Service(id=node_id, namespace=NS, name=src_name,
                   node_kind=NODE_KIND_SERVICE))
    db.add(ServiceEdge(src_id=node_id, dst_id=2, kind="serves_traffic",
                       last_seen_at=datetime.utcnow() - timedelta(minutes=5),
                       extras={"discovery_sources": sources}))
    db.commit()


# --- достоверность в ответе ------------------------------------------------


def test_result_carries_confidence_per_entry(db):
    _entry(db, "town-entry", ["k8s_topology_resources/service"], 10)

    out = blast_radius_for(db, NS, NAME)
    assert out["services"] == ["town-entry"], "плоский список не должен ломаться"

    detail = out["services_detailed"][0]
    assert detail["name"] == "town-entry"
    assert detail["confidence_label"] == "high"
    assert detail["confidence_score"] >= 0.85


def test_guessed_entry_is_labelled_lower(db):
    """Догадка обязана отличаться от прочитанного манифеста."""
    _entry(db, "guessed", ["kg_sync/env_vars"], 11)

    detail = blast_radius_for(db, NS, NAME)["services_detailed"][0]
    assert detail["confidence_label"] != "high"


def test_min_confidence_seen_reports_the_weakest(db):
    """Потребитель должен видеть худшее звено, а не среднее по больнице."""
    _entry(db, "solid", ["k8s_topology_resources/service"], 12)
    _entry(db, "weak", ["kg_sync/env_vars"], 13)

    out = blast_radius_for(db, NS, NAME)
    weakest = min(d["confidence_score"] for d in out["services_detailed"])
    assert out["min_confidence_seen"] == pytest.approx(weakest)


# --- фильтр ---------------------------------------------------------------


def test_threshold_keeps_only_observed(db):
    """«Что ТОЧНО заденет» — отдельный вопрос от «что может задеть»."""
    _entry(db, "from-k8s", ["k8s_topology_resources/service"], 14)
    _entry(db, "from-env", ["kg_sync/env_vars"], 15)

    strict = blast_radius_for(db, NS, NAME, min_confidence=0.85)
    assert strict["services"] == ["from-k8s"]
    assert strict["services_total"] == 1, "счётчик обязан учитывать фильтр"


def test_default_hides_nothing(db):
    """Без порога выдача полная — фильтр включают осознанно."""
    _entry(db, "from-k8s", ["k8s_topology_resources/service"], 16)
    _entry(db, "from-env", ["kg_sync/env_vars"], 17)

    assert blast_radius_for(db, NS, NAME)["services_total"] == 2


def test_threshold_can_empty_the_answer(db):
    """Пустой ответ честнее списка, которому нельзя верить."""
    _entry(db, "from-env", ["kg_sync/env_vars"], 18)

    out = blast_radius_for(db, NS, NAME, min_confidence=0.85)
    assert out["services"] == []
    assert out["min_confidence_seen"] is None


# --- совместимость --------------------------------------------------------


def test_unknown_service_returns_empty_shape(db):
    out = blast_radius_for(db, NS, "нет-такого")
    assert out["services"] == [] and out["urls"] == []


def test_flat_lists_still_present_for_discord_embed(db):
    """`_build_blast_radius_field` читает services/urls — формат не трогаем."""
    _entry(db, "town-entry", ["k8s_topology_resources/service"], 19)

    out = blast_radius_for(db, NS, NAME)
    for key in ("services", "urls", "services_total", "urls_total"):
        assert key in out
