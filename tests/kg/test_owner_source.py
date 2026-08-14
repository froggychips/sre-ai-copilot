"""Провенанс владельца: откуда взялся team_owner.

До contract 2.6 в графе было 12 577 узлов с `team_owner` и ни одного способа
отличить владельца, поставленного человеком, от угаданного по префиксу
namespace. Для эскалации это разные вещи: префиксная эвристика первой ломается
на переименовании сквада, а звать людей ночью по угаданному владельцу — худший
момент это выяснить.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.contract import (OWNER_SOURCE_ALIASES, OWNER_SOURCE_TRUST,
                                          OWNER_SOURCES, owner_source_valid)
from app.knowledge_graph.populator import upsert_service
from app.knowledge_graph.schema import Service


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


# --- контракт -------------------------------------------------------------


def test_trust_covers_every_source():
    """Новый источник обязан получить оценку доверия — иначе он «никакой»."""
    assert set(OWNER_SOURCE_TRUST) == OWNER_SOURCES


def test_trust_orders_manual_above_heuristics():
    """Порядок доверия — суть таблицы, а не украшение."""
    assert OWNER_SOURCE_TRUST["manual"] > OWNER_SOURCE_TRUST["k8s_labels"]
    assert OWNER_SOURCE_TRUST["k8s_labels"] > OWNER_SOURCE_TRUST["deploy_history"]
    assert OWNER_SOURCE_TRUST["deploy_history"] > OWNER_SOURCE_TRUST["namespace_prefix"]
    assert OWNER_SOURCE_TRUST["namespace_prefix"] > OWNER_SOURCE_TRUST["suggested"]


def test_aliases_resolve_to_known_sources():
    """Короткие алиасы suggester-а не должны разъезжаться с канонами."""
    for alias, canonical in OWNER_SOURCE_ALIASES.items():
        assert canonical in OWNER_SOURCES, f"алиас {alias} ведёт в никуда"


@pytest.mark.parametrize("value,ok", [
    ("manual", True), ("k8s_labels", True), ("namespace_prefix", True),
    (None, True),          # провенанс неизвестен — легальное состояние
    ("labels", False),     # это алиас, а не канон
    ("выдумка", False),
])
def test_owner_source_validation(value, ok):
    assert owner_source_valid(value) is ok


# --- запись ---------------------------------------------------------------


def test_owner_source_is_persisted(db):
    upsert_service(db, "squad-1", "api", team_owner="squad-1",
                   owner_source="k8s_labels")
    db.commit()

    svc = db.query(Service).filter_by(namespace="squad-1", name="api").one()
    assert svc.team_owner == "squad-1"
    assert svc.owner_source == "k8s_labels"


def test_unknown_source_is_dropped_not_stored(db):
    """Опечатка не должна заводить в графе седьмой «источник»."""
    upsert_service(db, "squad-1", "api", team_owner="squad-1",
                   owner_source="labels")   # алиас, не канон
    db.commit()

    svc = db.query(Service).filter_by(namespace="squad-1", name="api").one()
    assert svc.team_owner == "squad-1", "владелец обязан сохраниться"
    assert svc.owner_source is None, "неизвестный источник пишется как «неизвестно»"


def test_source_follows_the_owner_it_describes(db):
    """Смена владельца переписывает и провенанс — иначе он описывал бы прошлого."""
    upsert_service(db, "squad-1", "api", team_owner="squad-1",
                   owner_source="namespace_prefix")
    db.commit()
    upsert_service(db, "squad-1", "api", team_owner="gr-wo",
                   owner_source="manual")
    db.commit()

    svc = db.query(Service).filter_by(namespace="squad-1", name="api").one()
    assert (svc.team_owner, svc.owner_source) == ("gr-wo", "manual")


def test_upsert_without_owner_keeps_existing_provenance(db):
    """Синк, не трогающий владельца, не должен обнулять его источник."""
    upsert_service(db, "squad-1", "api", team_owner="squad-1",
                   owner_source="manual")
    db.commit()
    upsert_service(db, "squad-1", "api", metadata={"app": "api"})
    db.commit()

    svc = db.query(Service).filter_by(namespace="squad-1", name="api").one()
    assert svc.owner_source == "manual"


def test_legacy_rows_have_null_provenance(db):
    """Существующие строки остаются с NULL — выдумывать им источник нечестно."""
    upsert_service(db, "squad-1", "api", team_owner="squad-1")
    db.commit()

    svc = db.query(Service).filter_by(namespace="squad-1", name="api").one()
    assert svc.team_owner == "squad-1"
    assert svc.owner_source is None
