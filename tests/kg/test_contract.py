"""Контракт KG: правила, на которые опираются консьюмеры графа.

`shared_namespace_of` отвечает на вопрос «где физически лежит БД этого
namespace». Вопрос выглядит служебным, но именно на нём граф однажды соврал
про прод — см. комментарий у первого теста.
"""
import pytest

# --- shared_namespace_of: где физически живёт БД realm'а ------------------
#
# Правило выведено из кластера 15.08.2026: 41 база `config-db-postgresql`,
# каждая в своём `*-shared`. До него db-узлы схлопывались по имени, и
# `db:postgres:config` собрал 1430 рёбер из 106 namespace в одном узле.


@pytest.mark.parametrize("ns,expected", [
    ("prod-kingdom1", "prod-shared"),
    ("prod-kingdom7", "prod-shared"),
    ("preprod-kingdom2", "preprod-shared"),
    ("preprod-qa-kingdom2", "preprod-qa-shared"),
    ("preupdate-kingdom5", "preupdate-shared"),
    ("preupdate-qa-kingdom5", "preupdate-qa-shared"),
    ("squad-37-kingdom2", "squad-37-shared"),
    ("squad-1-kingdom7", "squad-1-shared"),
])
def test_shared_namespace_resolves_realm(ns, expected):
    from app.knowledge_graph.contract import shared_namespace_of
    assert shared_namespace_of(ns) == expected


def test_shared_namespace_is_idempotent():
    """`*-shared` — сам себе realm-хранилище."""
    from app.knowledge_graph.contract import shared_namespace_of
    assert shared_namespace_of("prod-shared") == "prod-shared"
    assert shared_namespace_of("squad-37-shared") == "squad-37-shared"


@pytest.mark.parametrize("ns", ["sre-ai", "monitoring", "kube-system",
                                "prod-lo-legal", "", None])
def test_unrecognised_namespace_gets_no_shared_pair(ns):
    """Придумывать shared-пару для не-realm namespace не на чем.

    None здесь значит «не знаю» — вызывающий оставит узел в own_namespace и
    понизит confidence. Это честнее, чем сослаться на выдуманный namespace.
    """
    from app.knowledge_graph.contract import shared_namespace_of
    assert shared_namespace_of(ns) is None


def test_different_realms_never_share_a_db_namespace():
    """Свойство, ради которого всё затевалось: realm'ы не смешиваются."""
    from app.knowledge_graph.contract import shared_namespace_of
    targets = {shared_namespace_of(ns) for ns in
               ("prod-kingdom1", "preprod-kingdom2", "squad-37-kingdom2",
                "squad-38-kingdom2", "preupdate-kingdom5")}
    assert len(targets) == 5, "разные realm обязаны дать разные namespace БД"
