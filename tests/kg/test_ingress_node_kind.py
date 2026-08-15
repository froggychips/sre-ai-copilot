"""Ingress-узлы носят собственный node_kind.

`NODE_KIND_INGRESS` был объявлен в контракте с самого введения поля, но не
проставлялся никогда: замер 15.08.2026 — 559 узлов `ingress:*`, все как
`service`, при нуле `ingress`. Пустая сущность в контракте хуже отсутствующей:
она обещает различение, которого нет.

Смысл терялся ровно там, где поле вводилось. `node_kind` появился, потому что
узел означал сразу две сущности — k8s Service и Deployment схлопывались в одну
строку. Внешняя точка входа — третья сущность, и она снова пряталась под
`service`.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph import k8s_ingress_sync as ing
from app.knowledge_graph.schema import (NODE_KIND_INGRESS, NODE_KIND_SERVICE,
                                        Service)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    # backend, на который ссылается Ingress: обычный k8s Service
    s.add(Service(namespace="prod-kingdom1", name="auth-service",
                  node_kind=NODE_KIND_SERVICE))
    s.commit()
    return s


def _route(host="api.example.com", backend="auth-service"):
    return {"host": host, "backend": backend, "path": "/"}


def _sync(db, ns="prod-kingdom1"):
    stats = {"nodes_created": 0, "edges_created": 0, "skipped_no_backend_match": 0}
    ing._sync_one_route(db, ns=ns, ing_name="api", route=_route(), stats=stats)
    db.commit()
    return stats


def test_ingress_node_gets_its_own_kind(db):
    _sync(db)
    node = db.query(Service).filter_by(name="ingress:api.example.com").one()
    assert node.node_kind == NODE_KIND_INGRESS


def test_backend_service_keeps_service_kind(db):
    """Сам сервис за Ingress остаётся k8s Service — меняется только точка входа."""
    _sync(db)
    backend = db.query(Service).filter_by(name="auth-service").one()
    assert backend.node_kind == NODE_KIND_SERVICE


def test_ingress_node_stays_synthetic(db):
    """synthetic не отменяется: узел по-прежнему вне метрик качества."""
    _sync(db)
    node = db.query(Service).filter_by(name="ingress:api.example.com").one()
    assert node.synthetic is True


def test_repeated_sync_does_not_duplicate(db):
    """Идемпотентность: второй тик не создаёт вторую копию узла."""
    _sync(db)
    _sync(db)
    assert db.query(Service).filter_by(name="ingress:api.example.com").count() == 1


def test_legacy_node_is_found_despite_old_kind(db):
    """Узел, оставшийся с node_kind='service', не должен раздваиваться.

    Поиск в `_canonical_host_node_ns` идёт по имени БЕЗ node_kind именно
    поэтому: миграция и код катятся не одновременно, и в промежутке синк
    обязан узнавать старую строку.
    """
    db.add(Service(namespace="preprod-shared", name="ingress:api.example.com",
                   node_kind=NODE_KIND_SERVICE, synthetic=True))
    db.commit()

    assert ing._canonical_host_node_ns(db, "api.example.com", "prod-kingdom1") \
        == "preprod-shared", "старая строка обязана находиться по имени"


def test_contract_declares_this_kind():
    """Проверка от обратного: если kind уберут из контракта, тест напомнит."""
    from app.knowledge_graph.contract import NODE_KIND_INGRESS as contract_kind
    from app.knowledge_graph.contract import NODE_KINDS
    assert contract_kind in NODE_KINDS


# --- гонка выката ---------------------------------------------------------
#
# Код и схема катятся не одновременно. Тест `test_ingress_host_node_reuses_
# existing_canonical` поймал это первым: строка с node_kind='service' для
# upsert'а выглядит другим узлом, и рядом рождается вторая копия — а 3218
# рёбер остаются на старой.


def test_legacy_service_row_is_adopted_not_duplicated(db):
    """Старая строка переводится на новый kind, а не дублируется."""
    db.add(Service(namespace="prod-kingdom1", name="ingress:api.example.com",
                   node_kind=NODE_KIND_SERVICE, synthetic=True))
    db.commit()
    legacy_id = db.query(Service).filter_by(name="ingress:api.example.com").one().id

    _sync(db)

    rows = db.query(Service).filter_by(name="ingress:api.example.com").all()
    assert len(rows) == 1, "дубль вместо усыновления — рёбра разъедутся"
    assert rows[0].id == legacy_id, "id обязан сохраниться: на нём висят рёбра"
    assert rows[0].node_kind == NODE_KIND_INGRESS


def test_adoption_skipped_when_new_row_already_exists(db):
    """Обе строки уже есть — старую не трогаем: UPDATE упёрся бы в UNIQUE."""
    for kind in (NODE_KIND_SERVICE, NODE_KIND_INGRESS):
        db.add(Service(namespace="prod-kingdom1", name="ingress:api.example.com",
                       node_kind=kind, synthetic=True))
    db.commit()

    ing._adopt_legacy_ingress_node(db, "prod-kingdom1", "ingress:api.example.com")
    db.commit()

    kinds = sorted(r.node_kind for r in
                   db.query(Service).filter_by(name="ingress:api.example.com").all())
    assert kinds == [NODE_KIND_INGRESS, NODE_KIND_SERVICE], "обе строки на месте"


def test_adoption_is_noop_without_legacy_row(db):
    """Нечего усыновлять — тихо выходим."""
    ing._adopt_legacy_ingress_node(db, "prod-kingdom1", "ingress:nope.example.com")
    assert db.query(Service).filter_by(name="ingress:nope.example.com").count() == 0
