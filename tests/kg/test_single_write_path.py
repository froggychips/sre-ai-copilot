"""У kg_services ровно один путь записи, и имя констрейнта живёт в одном месте.

Прецедент #245: копий `INSERT … ON CONFLICT` по kg_services было две —
`populator._upsert_service_pg` и `kg_sync._upsert_service_pg`, каждая со своим
литералом имени констрейнта и своей «зеркальной» merge-политикой. При переходе
на трёхколоночный ключ (миграция 20260807_0200) вторую копию пропустили:
`ON CONFLICT` сослался на удалённое имя, kg_topology_sync падал на КАЖДОМ
namespace — 79 ошибок за тик, services=0, граф по сервисам не обновлялся сутки.

Тесты ниже структурные: они ловят возврат второй копии до того, как она
доедет до прода, — а не проверяют, что SQL «работает».
"""
import inspect
import re
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph import kg_sync, populator
from app.knowledge_graph.contract import UQ_KG_SERVICE_NS_NAME_KIND
from app.knowledge_graph.schema import (NODE_KIND_SERVICE, NODE_KIND_WORKLOAD,
                                        Service)

KG_DIR = Path(__file__).parent.parent.parent / "app" / "knowledge_graph"


def test_constraint_name_declared_once():
    """Литерал имени констрейнта не размножается по модулям."""
    literal = re.compile(rf'["\']{UQ_KG_SERVICE_NS_NAME_KIND}["\']')
    offenders = []
    for path in KG_DIR.glob("*.py"):
        if path.name == "contract.py":       # единственное легальное объявление
            continue
        if literal.search(path.read_text(encoding="utf-8")):
            offenders.append(path.name)
    assert not offenders, (
        f"имя констрейнта захардкожено в {offenders} — брать из "
        "contract.UQ_KG_SERVICE_NS_NAME_KIND, иначе переименование снова "
        "разъедется с ON CONFLICT (#245)"
    )


def test_schema_uses_the_declared_constraint_name():
    """UNIQUE в модели и константа контракта — одно и то же имя."""
    names = {
        c.name for c in Service.__table__.constraints if getattr(c, "name", None)
    }
    assert UQ_KG_SERVICE_NS_NAME_KIND in names, (
        f"в модели нет констрейнта {UQ_KG_SERVICE_NS_NAME_KIND}: {sorted(names)}"
    )


def test_only_one_on_conflict_targets_kg_services():
    """Второй `ON CONFLICT` по узлам графа = возврат дефекта #245."""
    hits = []
    for path in KG_DIR.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"on_conflict_do_update\(", text):
            window = text[match.start(): match.start() + 400]
            if "Service.__table__" in text[max(0, match.start() - 300): match.start()] or \
               UQ_KG_SERVICE_NS_NAME_KIND in window or "constraint=UQ_KG_SERVICE" in window:
                hits.append(f"{path.name}:{text[:match.start()].count(chr(10)) + 1}")
    assert len(hits) <= 1, f"несколько upsert-ов по kg_services: {hits}"


def test_kg_sync_delegates_to_the_single_writer(monkeypatch):
    """kg_sync не пишет узел сам — он зовёт populator.upsert_service."""
    calls = []

    def fake(db, **kwargs):
        calls.append(kwargs)
        return "svc"

    monkeypatch.setattr(kg_sync, "upsert_service", fake)
    kg_sync._upsert_service_pg(
        db=None, namespace="squad-1", name="api", team_owner="gr-wo",
        metadata={"app": "api"}, synthetic=False, stale_class="expected_stale",
    )

    assert len(calls) == 1, "kg_sync снова пишет узел мимо единого writer-а"
    assert calls[0]["node_kind"] == NODE_KIND_SERVICE, (
        "этот путь обязан заводить только логические сервисы: workload-узлы "
        "создаёт k8s_topology_resources_sync"
    )
    assert calls[0]["stale_class"] == "expected_stale"


def test_writer_signature_covers_both_callers():
    """У единого writer-а есть все поля, ради которых существовала вторая копия."""
    params = inspect.signature(populator.upsert_service).parameters
    for field in ("node_kind", "stale_class", "team_owner", "metadata", "synthetic"):
        assert field in params, f"в upsert_service нет {field} — вторая копия вернётся"


# --- поведение на живой сессии (SQLite-путь) ------------------------------


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_service_and_workload_are_different_nodes(db):
    """Ключ трёхколоночный: одно имя в одном namespace — два разных узла."""
    populator.upsert_service(db, "squad-1", "api", node_kind=NODE_KIND_SERVICE)
    populator.upsert_service(db, "squad-1", "api", node_kind=NODE_KIND_WORKLOAD)
    db.commit()

    assert db.query(Service).filter_by(namespace="squad-1", name="api").count() == 2, (
        "service и workload схлопнулись в один узел — serves_traffic снова "
        "выродится в self-loop (contract 2.4)"
    )


def test_stale_class_none_does_not_reset_existing(db):
    """None = «не считал», а не «сбросить»: иначе sync стирал бы expected_stale."""
    populator.upsert_service(db, "squad-1", "api", stale_class="expected_stale")
    db.commit()
    populator.upsert_service(db, "squad-1", "api", team_owner="gr-wo")
    db.commit()

    svc = db.query(Service).filter_by(namespace="squad-1", name="api").one()
    assert svc.stale_class == "expected_stale"
    assert svc.team_owner == "gr-wo"


def test_metadata_is_merged_not_overwritten(db):
    """Каждый источник владеет своими ключами — чужие переживают upsert."""
    populator.upsert_service(db, "squad-1", "api", metadata={"app": "api"})
    db.commit()
    populator.upsert_service(db, "squad-1", "api", metadata={"k8s_service": True})
    db.commit()

    svc = db.query(Service).filter_by(namespace="squad-1", name="api").one()
    assert svc.metadata_json == {"app": "api", "k8s_service": True}
