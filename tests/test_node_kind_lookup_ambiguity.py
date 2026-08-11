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
from app.knowledge_graph.populator import upsert_edge, upsert_service
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


def test_blast_radius_finds_service_entrypoint_via_workload_edge(
    svc_and_workload, db,
):
    """serves_traffic-ребро идёт src=Service-узел → dst=WORKLOAD-узел (так
    его пишет producer, k8s_topology_resources_sync._sync_one_service) —
    blast_radius обязан найти его и вернуть имя k8s Service.

    Регрессия: старый фильтр `dst_id == svc.id` сравнивал с id
    Service-узла (queries.py резолвит node_kind='service'), а producer в
    dst НИКОГДА не пишет Service-узел → секция «🎯 Blast radius» в
    critical-embed молча пустовала при живых рёбрах.
    """
    from app.knowledge_graph.queries import blast_radius_for

    workload = (
        db.query(Service)
        .filter_by(
            namespace="prod-shared", name="auth",
            node_kind=NODE_KIND_WORKLOAD,
        )
        .one()
    )
    # Ребро — по образцу producer'а (k8s_topology_resources_sync:523-535).
    upsert_edge(
        db,
        src=svc_and_workload,
        dst=workload,
        kind="serves_traffic",
        discovered_by="k8s_topology_resources/service",
        extras={
            "confidence": "declared_k8s",
            "semantics": "sync",
            "selector": {"app": "auth"},
            "service_type": "ClusterIP",
        },
    )
    db.commit()

    result = blast_radius_for(db, "prod-shared", "auth")
    assert result["services"] == ["auth"], (
        "blast_radius не нашёл Service-точку входа по serves_traffic-ребру "
        "на workload-узел"
    )
    assert result["services_total"] == 1


def test_ns_deploy_attribution_not_doubled_by_workload_node(svc_and_workload, db):
    """Один TC-билд не двоится из-за одноимённого workload-узла.

    `tc_deploys_to_kg` броадкастит билд на КАЖДЫЙ non-synthetic узел ns, то
    есть и на Service `auth`, и на workload `auth`. Ns-level атрибуция
    джойнила по namespace без `node_kind` — и один билд возвращался дважды
    (а с K сервисами в ns — K×2 раза).
    """
    from datetime import datetime, timedelta, timezone

    from app.knowledge_graph.queries import recent_deploys_for_namespaces
    from app.knowledge_graph.schema import Deployment

    before = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)
    workload = (
        db.query(Service)
        .filter_by(namespace="prod-shared", name="auth",
                   node_kind=NODE_KIND_WORKLOAD)
        .one()
    )
    for node in (svc_and_workload, workload):
        db.add(Deployment(
            service_id=node.id,
            buildtype_id="Bt_BuildAndUpdate", build_number="728",
            started_at=(before - timedelta(minutes=8)).replace(tzinfo=None),
            status="SUCCESS",
            extras={"namespace_scope": True},
        ))
    db.commit()

    out = recent_deploys_for_namespaces(
        db, ["prod-shared"], before=before, lookback_minutes=60, limit=5,
    )
    assert len(out) == 1, f"билд #728 вернулся {len(out)} раз"
    assert out[0]["number"] == "728"


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
