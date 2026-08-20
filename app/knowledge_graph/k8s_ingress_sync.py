"""Phase 3-B: sync k8s Ingress resources в kg_service_edges.

Каждый Ingress даёт **external entrypoint** для одного-нескольких internal
services. Это первый источник, который раскрывает «откуда приходит трафик»
вне cluster-internal env-scan.

Модель:
- synthetic-узел `ingress:<host>` (team_owner="external") — ОДИН на
  hostname, независимо от namespace. Если узел с этим именем уже есть
  где-то в KG — переиспользуем канонический (лексикографически минимальный
  ns, детерминизм как у db:-узлов в kg_sync); иначе создаём в ns Ingress-а.
  Раньше узел плодился per-namespace: один host в N ns давал N узлов,
  деливших один external_probe:{host}-fingerprint.
- Edge `ingress:<host>` → `<backend_svc>` (тот же ns у Ingress), kind=`calls`,
  discovered_by=`kg_sync/ingress`.

Result в embed:
- `upstream_of(auth-service)` начинает возвращать `ingress:auth.lastoasisgame.com`
- В secции «Inbound callers» в #error embed: 1 через `calls` (external)
- В blast radius / why-matters эвристиках узел будет учтён.

CLI:
    python -m app.knowledge_graph.k8s_ingress_sync             # все ns
    python -m app.knowledge_graph.k8s_ingress_sync preprod-kingdom1
"""
from __future__ import annotations

import json
import logging
import subprocess
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from app.knowledge_graph.edge_decay_guard import (
    SOURCE_INGRESS_SYNC, record_source_run)
from app.knowledge_graph.populator import upsert_edge, upsert_service
from app.knowledge_graph.kubectl_breaker import run_kubectl
from app.knowledge_graph.schema import (NODE_KIND_INGRESS, NODE_KIND_SERVICE,
                                        Service)

log = logging.getLogger(__name__)


_KUBECTL_TIMEOUT_S = 30


class IngressFetchError(RuntimeError):
    """kubectl реально упал (timeout / rc!=0 / битый JSON) — в отличие от
    валидного «в кластере нет ingress-ов».

    Аналог `kg_sync.KubectlFetchError`. Нужен, чтобы вызывающий отличал сбой
    fetch-а от пустого кластера: по пустому тику edge-decay guard не должен
    легализовать удаление ingress-овых `calls` (их last_seen_at освежает
    ТОЛЬКО этот синк).
    """


def _kubectl_get_ingresses_all(*, strict: bool = False) -> List[Dict[str, Any]]:
    """kubectl get ingresses -A -o json → list.

    Раньше `subprocess.run` шёл здесь БЕЗ try/except (единственный такой
    kubectl-хелпер в синках): `TimeoutExpired` при тупящем apiserver-е
    вылетал наружу и убивал тик целиком — и `sync_all_ingresses` (не доходя
    до `record_source_run`, т.е. без отчёта edge-decay guard'у), и
    `ingress_observations_sync`, который импортирует этот же хелпер. Все
    соседи (`k8s_jobs_sync`, `k8s_topology_resources_sync`) в этом месте
    деградируют, а не падают.

    strict=True → сбой сигналится `IngressFetchError` (вызывающий считает
    его в `errors`). strict=False (default) → `[]` как раньше: контракт для
    `ingress_observations_sync`, который ждёт список и сам логирует
    `no_ingresses`.
    """
    try:
        out = run_kubectl(
            ["kubectl", "get", "ingresses", "-A", "-o", "json"],
            timeout=_KUBECTL_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as e:
        log.warning(
            "ingress_sync.kubectl_timeout timeout=%ds", _KUBECTL_TIMEOUT_S,
        )
        if strict:
            raise IngressFetchError(
                f"kubectl get ingresses: timeout {_KUBECTL_TIMEOUT_S}s"
            ) from e
        return []
    except OSError as e:
        # kubectl нет в PATH / fork не удался — тот же класс деградации.
        log.warning("ingress_sync.kubectl_exception err=%s", e)
        if strict:
            raise IngressFetchError(f"kubectl get ingresses: {e}") from e
        return []

    if out.returncode != 0:
        log.warning(
            "ingress_sync.kubectl_failed rc=%d stderr=%s",
            out.returncode, (out.stderr or "").strip()[:200],
        )
        if strict:
            raise IngressFetchError(
                f"kubectl get ingresses rc={out.returncode}"
            )
        return []
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError as e:
        log.warning("ingress_sync.json_decode_failed %s", e)
        if strict:
            raise IngressFetchError(f"kubectl get ingresses: bad json: {e}") from e
        return []
    items = data.get("items") or []
    return items if isinstance(items, list) else []


def _extract_routes(ing: Dict[str, Any]) -> List[Dict[str, str]]:
    """Из Ingress spec.rules вытащить (host, backend_svc, path).

    Поддерживаем networking.k8s.io/v1 form (`backend.service.name`).
    `defaultBackend` тоже учитываем (без host — `*` ловит весь трафик).
    """
    out: List[Dict[str, str]] = []
    spec = ing.get("spec") or {}

    # defaultBackend (если ловит unmatched)
    db = spec.get("defaultBackend") or {}
    db_svc = (db.get("service") or {}).get("name")
    if db_svc:
        out.append({"host": "*", "backend": db_svc, "path": "/*"})

    for rule in spec.get("rules") or []:
        host = rule.get("host") or "*"
        http = rule.get("http") or {}
        for path in http.get("paths") or []:
            backend = path.get("backend") or {}
            svc = (backend.get("service") or {}).get("name")
            if svc:
                out.append({
                    "host": host,
                    "backend": svc,
                    "path": path.get("path") or "/",
                })
    return out


def _canonical_host_node_ns(db: Session, host: str, fallback_ns: str) -> str:
    """Namespace для `ingress:<host>`-узла: host — глобальное имя, узел один.

    Если узел(ы) с этим именем уже есть — берём лексикографически
    минимальный namespace (детерминизм). Иначе — ns текущего Ingress-а.
    Так один host в N namespace-ах перестаёт плодить N узлов, деливших
    единственный `external_probe:{host}`-fingerprint.

    Схлопывание здесь законно именно потому, что host — **глобальное** имя:
    `api.lastoasisgame.com` в мире один, в каком бы namespace ни лежал его
    Ingress. Тот же приём применялся к `db:<driver>:<host>` и там оказался
    неверен — у БД host это service-name внутри namespace, то есть имя
    локальное, и 41 разная база схлопнулась в один узел (contract 2.7).
    Правило: сводить узлы по имени можно ровно тогда, когда имя глобально.

    Поиск идёт по имени БЕЗ `node_kind` намеренно: узлы, созданные до
    15.08.2026, лежат с `node_kind='service'`, и фильтр отсёк бы их,
    заставив синк создать вторую копию рядом.
    """
    rows = (
        db.query(Service.namespace)
        .filter(Service.name == f"ingress:{host}")
        .all()
    )
    if rows:
        return min(ns for (ns,) in rows)
    return fallback_ns


def _adopt_legacy_ingress_node(db: Session, namespace: str, name: str) -> None:
    """Перевести уже существующий `ingress:*`-узел на node_kind='ingress'.

    Ключ узла — (namespace, name, node_kind), поэтому строка, созданная до
    15.08.2026 с `node_kind='service'`, для upsert'а выглядит другим узлом:
    без этой правки синк создал бы ВТОРУЮ копию рядом, и 3218 рёбер
    разъехались бы между старой и новой.

    Миграция `20260815_0100` делает то же самое разом, но полагаться только на
    неё нельзя: код и схема катятся не одновременно, и в обе стороны есть
    окно. Самолечение здесь снимает вопрос порядка выката целиком.

    Если строка с `node_kind='ingress'` уже есть, старую не трогаем: UPDATE
    упёрся бы в UNIQUE, а рёбра на legacy-узел доживут своё через edge_decay.
    """
    already = (
        db.query(Service)
        .filter_by(namespace=namespace, name=name, node_kind=NODE_KIND_INGRESS)
        .first()
    )
    if already is not None:
        return

    legacy = (
        db.query(Service)
        .filter_by(namespace=namespace, name=name, node_kind=NODE_KIND_SERVICE)
        .first()
    )
    if legacy is None:
        return

    legacy.node_kind = NODE_KIND_INGRESS  # type: ignore[assignment]
    db.flush()
    log.info("ingress_sync.node_kind_adopted namespace=%s name=%s", namespace, name)


def _sync_one_route(
    db: Session,
    *,
    ns: str,
    ing_name: str,
    route: Dict[str, str],
    stats: Dict[str, int],
) -> None:
    """Обработать один route. Вызывается под per-item SAVEPOINT-ом."""
    host = route["host"]
    backend_name = route["backend"]

    # Backend должен существовать в KG (kg_topology_sync уже видел его
    # как Deployment). Если нет — пропускаем (избегаем фейк-узлов).
    backend = (
        db.query(Service)
        .filter_by(namespace=ns, name=backend_name, node_kind=NODE_KIND_SERVICE)
        .one_or_none()
    )
    if backend is None:
        stats["skipped_no_backend_match"] += 1
        return

    # synthetic-узел внешнего entrypoint — один на hostname (см.
    # _canonical_host_node_ns), не per-namespace.
    external_node_name = f"ingress:{host}"
    node_ns = _canonical_host_node_ns(db, host, ns)
    # До upsert'а: строка могла быть создана старым кодом как 'service', и
    # тогда ключ (namespace, name, node_kind) не совпадёт — родится дубль.
    _adopt_legacy_ingress_node(db, node_ns, external_node_name)
    ext = upsert_service(
        db,
        namespace=node_ns,
        name=external_node_name,
        team_owner="external",
        synthetic=True,
        # Внешняя точка входа — не k8s Service. Контракт объявлял этот
        # node_kind с самого введения поля, но никто его не проставлял: до
        # 15.08.2026 все 559 узлов `ingress:*` лежали как `service`, то есть
        # `node_kind='service'` снова означал две разные сущности — ровно ту
        # болезнь, ради лечения которой поле и вводили.
        node_kind=NODE_KIND_INGRESS,
    )
    stats["nodes_created"] += 1  # idempotent: upsert не дубль; считаем "touched"

    upsert_edge(
        db, src=ext, dst=backend, kind="calls",
        discovered_by="kg_sync/ingress",
        extras={
            "ingress_name": ing_name,
            "path": route["path"],
            "confidence": "declared_k8s",  # сильнее чем inferred_env
            "semantics": "sync",
        },
    )
    stats["edges_created"] += 1


def sync_all_ingresses(db: Session) -> Dict[str, int]:
    """Sync — главная entry point.

    Возвращает stats:
      ingresses_fetched / routes_seen / nodes_created / edges_created
      / skipped_no_backend_match (backend service не существует в KG yet)
      / errors (routes, откатившиеся per-item savepoint-ом, + сбой fetch-а:
        kubectl-timeout считается ошибкой, а не «в кластере 0 ingress-ов»)
    """
    # Fetch-сбой не роняет тик: считаем его в errors и идём до
    # record_source_run — иначе трейсбек уносил и отчёт guard'у (тик терялся
    # целиком), и тот бы судил свежесть ingress-рёбер по фоллбэку.
    try:
        ingresses = _kubectl_get_ingresses_all(strict=True)
        fetch_errors = 0
    except IngressFetchError as e:
        ingresses = []
        fetch_errors = 1
        log.warning(
            "ingress_sync.fetch_failed err=%s — тик деградирует, "
            "рёбра не освежаем", e,
        )

    stats = {
        "ingresses_fetched": len(ingresses),
        "routes_seen": 0,
        "nodes_created": 0,
        "edges_created": 0,
        "skipped_no_backend_match": 0,
        "errors": fetch_errors,
    }

    for ing in ingresses:
        meta = ing.get("metadata") or {}
        ns = meta.get("namespace") or "default"
        ing_name = meta.get("name") or "?"

        routes = _extract_routes(ing)
        for r in routes:
            stats["routes_seen"] += 1
            try:
                # SAVEPOINT на route: один DataError не переводит Session в
                # aborted-состояние и не роняет весь tick (зеркалит
                # per-item SAVEPOINT из k8s_events_sync).
                with db.begin_nested():
                    _sync_one_route(
                        db, ns=ns, ing_name=ing_name, route=r, stats=stats,
                    )
            except Exception as e:
                stats["errors"] += 1
                log.warning(
                    "ingress_sync.route_failed ns=%s ingress=%s host=%s err=%s",
                    ns, ing_name, r.get("host"), e,
                )

    db.commit()
    log.info(
        "ingress_sync.done ingresses=%d routes=%d edges=%d skipped=%d errors=%d",
        stats["ingresses_fetched"], stats["routes_seen"],
        stats["edges_created"], stats["skipped_no_backend_match"],
        stats["errors"],
    )
    # Отчёт для edge-decay guard: этот синк — единственный, кто освежает
    # ingress-овые `calls` (discovered_by='kg_sync/ingress'). Живой env-scan
    # в kg_sync их свежести не подтверждает и легализовать удаление не может.
    record_source_run(SOURCE_INGRESS_SYNC, stats)
    return stats


if __name__ == "__main__":
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        print(sync_all_ingresses(db))
    finally:
        db.close()
