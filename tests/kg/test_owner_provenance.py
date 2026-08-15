"""Владелец узла должен нести провенанс.

Замер прода 15.08.2026: `owner_source` = NULL у **всех 6441** узлов. Колонка
заведена миграцией 20260814_0100, валидация `owner_source_valid` написана,
реестр `OWNER_SOURCES` заполнен, контракт бампнут до 2.6 — а ни один вызов
`upsert_service` значение не передавал.

Это второй случай того же класса за сутки: `node_kind='ingress'` был объявлен
в контракте и не проставлялся ни разу (559 узлов, исправлено в #284). Поле
существует в модели, контракт его обещает, продюсер молчит — и снаружи это
неотличимо от «данных просто нет».

Отсюда последний тест файла: он ищет такие поля механически, а не по памяти.
"""
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph import kg_sync
from app.knowledge_graph.contract import (OWNER_SOURCE_NAMESPACE_PREFIX,
                                          OWNER_SOURCE_PLATFORM_STATIC,
                                          OWNER_SOURCES, OWNER_SOURCE_TRUST)
from app.knowledge_graph.schema import Service


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _sync(db, namespace="prod-kingdom1"):
    deploy = {
        "metadata": {"name": "town-service"},
        "spec": {"template": {"spec": {"containers": [{"env": [
            {"name": "DB_URL", "valueFrom": {"secretKeyRef": {
                "name": "postgres-town-secret", "key": "url"}}},
        ]}]}}},
    }
    with patch.object(kg_sync, "_kubectl_get_deployments", return_value=[deploy]), \
         patch.object(kg_sync, "_refresh_stale_class_for_namespace", return_value=0):
        kg_sync.sync_namespace(db, namespace)
    db.commit()


def test_service_owner_records_its_provenance(db):
    """Владелец выведен из префикса namespace — так и должно быть записано."""
    _sync(db)
    svc = db.query(Service).filter_by(name="town-service").one()
    assert svc.team_owner == "kingdom1"
    assert svc.owner_source == OWNER_SOURCE_NAMESPACE_PREFIX


def test_synthetic_node_owner_is_marked_static(db):
    """У `db:*` владелец `data` не выведен, а назначен — это другой источник."""
    _sync(db)
    node = db.query(Service).filter(Service.name.like("db:%")).one()
    assert node.team_owner == "data"
    assert node.owner_source == OWNER_SOURCE_PLATFORM_STATIC


def test_no_node_is_left_without_provenance(db):
    """Ровно то, чего не хватало: ни одного NULL после синка."""
    _sync(db)
    missing = [s.name for s in db.query(Service).all() if s.owner_source is None]
    assert not missing, f"узлы без провенанса владельца: {missing}"


def test_written_sources_are_known_to_the_contract(db):
    """Источник вне реестра ломает `owner_source_valid` и шкалу доверия."""
    _sync(db)
    for svc in db.query(Service).all():
        assert svc.owner_source in OWNER_SOURCES


def test_prefix_owner_is_the_least_trusted(db):
    """Провенанс нужен ради этого: угаданный владелец слабее лейбла.

    Если различие перестанет быть видно, звать людей ночью будут по догадке.
    """
    _sync(db)
    svc = db.query(Service).filter_by(name="town-service").one()
    assert OWNER_SOURCE_TRUST[svc.owner_source] < OWNER_SOURCE_TRUST["k8s_labels"]


# --- поиск полей, которые контракт обещает, а продюсер не пишет ------------


def test_declared_semantic_fields_have_a_producer():
    """Механическая проверка вместо памяти.

    Ищет семантические поля узла в аргументах `upsert_service` и требует, чтобы
    хотя бы один вызов в app/ их передавал. Так `owner_source` (NULL у 6441
    узла) и `node_kind='ingress'` (0 узлов из 559) были бы пойманы в день
    появления, а не через месяцы.
    """
    import inspect
    import pathlib
    import re

    from app.knowledge_graph.populator import upsert_service

    # Поля, за которыми стоит смысл контракта, а не механика записи.
    semantic = {"owner_source", "stale_class", "node_kind", "synthetic"}
    params = set(inspect.signature(upsert_service).parameters) & semantic

    app_dir = pathlib.Path(__file__).parent.parent.parent / "app"
    sources = "\n".join(p.read_text(encoding="utf-8") for p in app_dir.rglob("*.py"))

    unpopulated = [
        field for field in sorted(params)
        # Ищем передачу с непустым значением: `field=None` не считается.
        if not re.search(rf"\b{field}=(?!None\b)\S", sources)
    ]
    assert not unpopulated, (
        f"контракт объявляет поля, но ни один продюсер их не пишет: "
        f"{unpopulated}. Снаружи это неотличимо от «данных нет»."
    )
