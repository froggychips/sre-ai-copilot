"""Резолв узла по (namespace, name) обязан оставаться однозначным.

Контекст: с contract 2.4 в `kg_services` два типа узлов, и Service `auth`
и workload `auth` живут в одной таблице как разные строки. Любой запрос вида
`.filter(namespace=…, name=…).one_or_none()` без фильтра по `node_kind`
превращается в `MultipleResultsFound` — причём не в синке топологии, а в
обогащении алертов, blast-radius и Discord-эмбедах, то есть на горячем пути.

Первый вариант этой правки такие места пропустил: греп искал форму
`filter_by(namespace=…, name=…)`, а половина кода использует
`.filter(Service.namespace == …, Service.name == …)`. Поэтому здесь два
теста: поведенческий (резолверы работают при одноимённом workload) и
структурный (в коде не появилось нового поиска без node_kind).
"""
from __future__ import annotations

import pathlib
import re

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.populator import upsert_service
from app.knowledge_graph.queries import _service_by_namespace_name
from app.knowledge_graph.schema import (NODE_KIND_SERVICE, NODE_KIND_WORKLOAD,
                                        Service)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def svc_and_workload(db):
    """Одноимённая пара Service/workload — ровно та ситуация, что ломала .one()."""
    svc = upsert_service(db, "prod-shared", "auth", team_owner="auth-team")
    upsert_service(db, "prod-shared", "auth", team_owner="auth-team",
                   node_kind=NODE_KIND_WORKLOAD)
    db.commit()
    return svc


def test_two_nodes_coexist_with_same_name(svc_and_workload, db):
    rows = db.query(Service).filter_by(namespace="prod-shared", name="auth").all()
    assert {r.node_kind for r in rows} == {NODE_KIND_SERVICE, NODE_KIND_WORKLOAD}


def test_queries_resolver_returns_service_node(svc_and_workload, db):
    """`.one_or_none()` не падает и отдаёт именно Service-узел."""
    resolved = _service_by_namespace_name(db, "prod-shared", "auth")
    assert resolved is not None, "резолвер вернул None при существующем сервисе"
    assert resolved.id == svc_and_workload.id
    assert resolved.node_kind == NODE_KIND_SERVICE


def test_blast_radius_survives_ambiguous_name(svc_and_workload, db):
    """blast_radius_for идёт тем же резолвером — проверяем, что не бросает.

    Это горячий путь: секция «🎯 Blast radius» в Discord-эмбеде critical-алерта.
    """
    from app.knowledge_graph.queries import blast_radius_for

    result = blast_radius_for(db, "prod-shared", "auth")
    assert set(result) >= {"services", "urls"}


# Формы поиска узла, которые обязаны нести node_kind. Ищем именно пару
# (namespace, name): поиск по одному namespace — легальный сценарий (список).
#
# `query(Service)` в паттерне обязателен: колонки namespace+name есть и у
# K8sJob, и у StorageVolume, и требовать от них node_kind — ложное
# срабатывание (у этих моделей такой колонки просто нет).
_LOOKUP_PATTERNS = (
    # db.query(Service)…filter_by(namespace=…, name=…)
    re.compile(r"query\(Service\)[^;]{0,200}?filter_by\(\s*namespace\s*=[^)]*?\bname\s*=", re.S),
    # .filter(Service.namespace == …, Service.name == …)
    re.compile(r"Service\.namespace\s*==[^)]*?Service\.name\s*==", re.S),
)

#: Конец SQLAlchemy-запроса: дальше этого искать node_kind бессмысленно.
_QUERY_TERMINATORS = (
    ".one_or_none()", ".one()", ".first()", ".all()", ".count()",
    ".scalar()", ".delete()", ".update(",
)

# Файлы, где Service-модель не участвует в лукапе по имени (сам populator
# делает upsert и передаёт node_kind параметром).
_SKIP_FILES = {"app/knowledge_graph/populator.py"}


def test_no_new_node_lookup_without_node_kind():
    """Ни один резолвер узла не ищет по (namespace, name) без node_kind.

    Структурная проверка: поведенческий тест выше покрывает известные точки,
    а этот не даёт добавить новую. Если тест упал на вашем коде — добавьте
    `node_kind` в фильтр, а не файл в _SKIP_FILES.
    """
    offenders = []
    for path in sorted(pathlib.Path("app").rglob("*.py")):
        rel = path.as_posix()
        if rel in _SKIP_FILES:
            continue
        src = path.read_text(encoding="utf-8")
        if "Service" not in src:
            continue
        for pattern in _LOOKUP_PATTERNS:
            for match in pattern.finditer(src):
                # Окно — до конца запроса, т.е. до первого терминатора
                # (.one_or_none() / .all() / …). Обрезать по первой ")" нельзя:
                # её закрывает сам `query(Service)`, и окно выходило пустым.
                tail = src[match.start():match.start() + 500]
                cut = min(
                    (tail.find(t) for t in _QUERY_TERMINATORS if tail.find(t) > 0),
                    default=len(tail),
                )
                if "node_kind" not in tail[:cut]:
                    line = src[:match.start()].count("\n") + 1
                    offenders.append(f"{rel}:{line}")
    assert not offenders, (
        "поиск узла без node_kind (MultipleResultsFound при одноимённом "
        f"workload): {offenders}"
    )
