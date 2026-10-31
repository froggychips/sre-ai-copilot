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
from datetime import datetime, timedelta
from typing import Any, Dict, List, Set

from sqlalchemy.orm import Session

from app.knowledge_graph.edge_decay_guard import (
    CLEANUP_SKIP_DELETE_PCT, CLEANUP_SKIP_EMPTY_FETCH,
    CLEANUP_SKIP_FETCH_FAILED, CLEANUP_SKIP_NO_BASELINE,
    SOURCE_INGRESS_SYNC, record_source_run)
from app.knowledge_graph.populator import (delete_service_node, upsert_edge,
                                           upsert_service)
from app.knowledge_graph.kubectl_breaker import run_kubectl
from app.knowledge_graph.schema import (NODE_KIND_INGRESS, NODE_KIND_SERVICE,
                                        Service)

log = logging.getLogger(__name__)


_KUBECTL_TIMEOUT_S = 30

# ── Чистка узлов исчезнувших хостов ─────────────────────────────────────────
#
# Синк только добавлял: `ingress:<host>` заводился при первом появлении
# Ingress-а и жил вечно. Пересозданный стенд или снятый Ingress оставляли
# узел в графе, и тот продолжал выдавать себя за точку входа. Замер
# 31.10.2026: 354 ingress-узла в живых namespace при 223 Ingress-ах в
# кластере, 184 (52%) не освежались больше суток — `check_node_freshness`
# висел в warn, а blast-radius считал трафик с хостов, которых нет.
#
# `updated_at` у ingress-узла — честный маркер свежести: его трогает только
# этот синк (health-пересчёт синтетику обходит), так что «не обновлялся N
# дней» и значит «N дней не виден в кластере».

#: Через сколько дней молчания узел считается исчезнувшим. Синк идёт раз в
#: час — неделя это 168 пропущенных тиков. Сутки-трое дали бы ложные
#: удаления на длинном сбое самого синка (kubectl-таймауты, упавший beat на
#: выходных): после них узлы живых хостов освежатся первым же удачным
#: прогоном, но до него успели бы попасть под нож. Неделю такой сбой не
#: длится, а пересозданный стенд получает тот же host обратно за часы.
INGRESS_NODE_STALE_DAYS = 7

#: Окно опоры: узлы, освежённые за эти часы ДО прогона, — это хосты, которые
#: синк недавно видел живыми. 24 тика.
_INGRESS_RECENTLY_SEEN_HOURS = 24

#: Усадка живого набора против опоры, после которой снимку не верим.
#: Тот же порог, что у `check_node_freshness`/`check_edges_freshness`.
_INGRESS_MAX_SHRINK_PCT = 30.0

#: Потолок удалений за проход: накопленное за месяцы разбирается порциями,
#: и ошибка видна на первой сотне строк, а не на всём графе.
_INGRESS_MAX_DELETE_PER_RUN = 100


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

    legacy.node_kind = NODE_KIND_INGRESS
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


def _recently_refreshed_ingress_nodes(db: Session, now: datetime) -> int:
    """Сколько ingress-узлов синк освежал за последние сутки. Считать ДО
    апсертов текущего прогона — иначе опора включит в себя сам проверяемый
    снимок и любая усадка станет нулевой (та же оговорка, что у
    `k8s_storage_sync._recently_seen_count`)."""
    since = now - timedelta(hours=_INGRESS_RECENTLY_SEEN_HOURS)
    return int(
        db.query(Service.id)
        .filter(Service.node_kind == NODE_KIND_INGRESS,
                Service.updated_at >= since)
        .count()
        or 0
    )


def _cleanup_vanished_ingress_nodes(
    db: Session,
    *,
    seen_hosts: Set[str],
    fetch_ok: bool,
    baseline: int,
    now: datetime,
) -> Dict[str, Any]:
    """Удалить `ingress:<host>`-узлы, которых синк не видел INGRESS_NODE_STALE_DAYS.

    Кандидат — узел `node_kind='ingress'` с `updated_at` старше порога, чьего
    host-а НЕТ в текущем снимке. Второе условие страхует первое: route без
    backend-а в графе (`skipped_no_backend_match`) узел не освежает, хотя
    Ingress жив, — такой узел по одному `updated_at` выглядел бы исчезнувшим.

    Предохранители — по дисциплине `k8s_storage_sync._cleanup_absent_volumes`:
      * fetch сбойнул → снимок неполный, не чистим (`fetch_failed`);
      * в снимке 0 Ingress-ов → сбой неотличим от пустого кластера (`empty_fetch`);
      * опоры нет — за сутки синк не освежил ни одного узла (вернулся после
        простоя) → сравнивать снимок не с чем, fail-closed (`no_baseline`);
      * живой набор ужался больше чем на `_INGRESS_MAX_SHRINK_PCT`% против
        опоры → снимок подозрительно мал, не чистим (`delete_pct`).

    Порог намеренно про усадку ЖИВОГО набора, а не про долю кандидатов от
    всех узлов: вторая формула блокирует сама себя — мусор копится именно
    оттого, что чистка не идёт, доля растёт, порог держит крепче. На
    сегодняшних 52% устаревших узлов такой порог не отпустил бы чистку
    никогда (ровно эту ловушку уже разбирали в storage-синке и drift_cleanup).

    Удаление — через `populator.delete_service_node`, по узлу под SAVEPOINT:
    узел с NOT NULL-зависимостями (health, деплои) пропускается с причиной,
    а не роняет проход.
    """
    cleanup: Dict[str, Any] = {
        "candidates": 0, "nodes_deleted": 0, "nodes_delete_skipped": 0,
        "edges_deleted": 0, "skipped": "", "skip_reasons": {},
    }
    if not fetch_ok:
        cleanup["skipped"] = CLEANUP_SKIP_FETCH_FAILED
        log.warning(
            "ingress_sync.node_cleanup_skipped reason=%s — снимок неполный, "
            "узлы не чистим", CLEANUP_SKIP_FETCH_FAILED,
        )
        return cleanup
    if not seen_hosts:
        cleanup["skipped"] = CLEANUP_SKIP_EMPTY_FETCH
        log.warning(
            "ingress_sync.node_cleanup_skipped reason=%s — 0 Ingress-ов в "
            "снимке (сбой неотличим от пустого кластера)",
            CLEANUP_SKIP_EMPTY_FETCH,
        )
        return cleanup

    cutoff = now - timedelta(days=INGRESS_NODE_STALE_DAYS)
    rows_total = int(
        db.query(Service.id)
        .filter(Service.node_kind == NODE_KIND_INGRESS)
        .count() or 0
    )
    # `updated_at IS NULL` под `<` не попадает — и правильно: без метки
    # доказать молчание нечем, а недоказанное не удаляют.
    seen_names = {f"ingress:{host}" for host in seen_hosts}
    candidates = [
        (int(node_id), str(ns), str(name))
        for node_id, ns, name in (
            db.query(Service.id, Service.namespace, Service.name)
            .filter(Service.node_kind == NODE_KIND_INGRESS,
                    Service.updated_at < cutoff)
            .order_by(Service.updated_at, Service.id)
            .all()
        )
        if name not in seen_names
    ]
    cleanup["candidates"] = len(candidates)
    if not candidates:
        return cleanup

    if baseline <= 0:
        cleanup["skipped"] = CLEANUP_SKIP_NO_BASELINE
        cleanup["snapshot"] = len(seen_hosts)
        cleanup["rows_total"] = rows_total
        log.warning(
            "ingress_sync.node_cleanup_skipped reason=%s candidates=%d — за "
            "последние %dч синк не освежил ни одного ingress-узла, сравнивать "
            "снимок (%d хостов при %d узлах) не с чем",
            CLEANUP_SKIP_NO_BASELINE, len(candidates),
            _INGRESS_RECENTLY_SEEN_HOURS, len(seen_hosts), rows_total,
        )
        return cleanup

    shrink_pct = max(0.0, 100.0 * (baseline - len(seen_hosts)) / baseline)
    if shrink_pct > _INGRESS_MAX_SHRINK_PCT:
        cleanup["skipped"] = CLEANUP_SKIP_DELETE_PCT
        # Процент без обоих чисел не читается: «ужалось на 75%» — это 300
        # против 75 или 4 против 1? Решение принимает человек по отчёту
        # источника, поэтому рядом опора, снимок и размер графа.
        cleanup["shrink_pct"] = round(shrink_pct, 1)
        cleanup["baseline"] = baseline
        cleanup["snapshot"] = len(seen_hosts)
        cleanup["rows_total"] = rows_total
        log.warning(
            "ingress_sync.node_cleanup_skipped reason=%s candidates=%d "
            "live_set_shrank=%.1f%% > %.1f%% (хостов сейчас %d, за сутки "
            "видели %d) — снимок подозрительно мал, узлы не чистим",
            CLEANUP_SKIP_DELETE_PCT, len(candidates), shrink_pct,
            _INGRESS_MAX_SHRINK_PCT, len(seen_hosts), baseline,
        )
        return cleanup

    if len(candidates) > _INGRESS_MAX_DELETE_PER_RUN:
        cleanup["capped_to"] = _INGRESS_MAX_DELETE_PER_RUN
        candidates = candidates[:_INGRESS_MAX_DELETE_PER_RUN]

    reasons: Dict[str, int] = {}
    deleted_names: List[str] = []
    for node_id, ns, name in candidates:
        try:
            # SAVEPOINT на узел: сбой на одном (гонка с параллельным
            # писателем, неожиданный FK) откатывает только его.
            with db.begin_nested():
                outcome = delete_service_node(db, node_id)
        except Exception as e:  # noqa: BLE001 — savepoint откатил узел, идём дальше
            cleanup["nodes_delete_skipped"] += 1
            reasons["error"] = reasons.get("error", 0) + 1
            log.warning(
                "ingress_sync.node_delete_failed ns=%s name=%s err=%s",
                ns, name, e,
            )
            continue
        if outcome["deleted"]:
            cleanup["nodes_deleted"] += 1
            cleanup["edges_deleted"] += int(outcome["edges_deleted"])
            deleted_names.append(f"{ns}/{name}")
        else:
            cleanup["nodes_delete_skipped"] += 1
            why = str(outcome["reason"] or "unknown")
            reasons[why] = reasons.get(why, 0) + 1
            log.info(
                "ingress_sync.node_delete_skipped ns=%s name=%s reason=%s "
                "blockers=%s", ns, name, why, outcome.get("blockers"),
            )
    cleanup["skip_reasons"] = reasons
    log.info(
        "ingress_sync.node_cleanup candidates=%d deleted=%d skipped=%d "
        "reasons=%s edges_deleted=%d stale_after=%dd of total=%d sample=%s",
        cleanup["candidates"], cleanup["nodes_deleted"],
        cleanup["nodes_delete_skipped"], reasons, cleanup["edges_deleted"],
        INGRESS_NODE_STALE_DAYS, rows_total, deleted_names[:10],
    )
    return cleanup


def sync_all_ingresses(db: Session) -> Dict[str, Any]:
    """Sync — главная entry point.

    Возвращает stats:
      ingresses_fetched / routes_seen / nodes_created / edges_created
      / skipped_no_backend_match (backend service не существует в KG yet)
      / errors (routes, откатившиеся per-item savepoint-ом, + сбой fetch-а:
        kubectl-timeout считается ошибкой, а не «в кластере 0 ingress-ов»)
      / nodes_deleted / nodes_delete_skipped / cleanup (чистка узлов
        исчезнувших хостов, см. `_cleanup_vanished_ingress_nodes`)
    """
    # Опора для чистки — ДО fetch-а и апсертов: сколько узлов синк освежал
    # за прошлые сутки без учёта текущего снимка.
    run_started = datetime.utcnow()
    baseline = _recently_refreshed_ingress_nodes(db, run_started)

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

    stats: Dict[str, Any] = {
        "ingresses_fetched": len(ingresses),
        "routes_seen": 0,
        "nodes_created": 0,
        "edges_created": 0,
        "skipped_no_backend_match": 0,
        "errors": fetch_errors,
        "nodes_deleted": 0,
        "nodes_delete_skipped": 0,
    }
    # Хосты текущего снимка — все, включая route без backend-а в графе: такой
    # Ingress жив, и его узел чистке не кандидат.
    seen_hosts: Set[str] = set()

    for ing in ingresses:
        meta = ing.get("metadata") or {}
        ns = meta.get("namespace") or "default"
        ing_name = meta.get("name") or "?"

        routes = _extract_routes(ing)
        for r in routes:
            stats["routes_seen"] += 1
            seen_hosts.add(r["host"])
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

    # Чистка — после коммита апсертов: освежённые узлы уже в базе, и сбой
    # чистки не откатит работу синка. Любая её ошибка считается в errors
    # (источник на этот цикл признаётся нездоровым — decay его рёбер
    # подождёт), а отчёт guard'у уходит в любом случае.
    try:
        cleanup = _cleanup_vanished_ingress_nodes(
            db, seen_hosts=seen_hosts, fetch_ok=fetch_errors == 0,
            baseline=baseline, now=run_started,
        )
        db.commit()
    except Exception as e:  # noqa: BLE001 — чистка не должна уносить тик и отчёт
        db.rollback()
        stats["errors"] += 1
        cleanup = {"skipped": "error", "error": type(e).__name__}
        log.warning("ingress_sync.node_cleanup_failed err=%s", e)
    stats["cleanup"] = cleanup
    stats["nodes_deleted"] = int(cleanup.get("nodes_deleted") or 0)
    stats["nodes_delete_skipped"] = int(cleanup.get("nodes_delete_skipped") or 0)

    log.info(
        "ingress_sync.done ingresses=%d routes=%d edges=%d skipped=%d errors=%d "
        "nodes_deleted=%d",
        stats["ingresses_fetched"], stats["routes_seen"],
        stats["edges_created"], stats["skipped_no_backend_match"],
        stats["errors"], stats["nodes_deleted"],
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
