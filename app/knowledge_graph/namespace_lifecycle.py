"""Жизненный цикл namespace: присутствие и идентичность инкарнации.

Шаг B2 проекта: граф начинает знать, **какой именно** стенд он видит, а не
только его имя. Удаления здесь нет намеренно — сначала неделя наблюдения,
потом действие (см. docstring `sync_namespace_lifecycle`).

Зачем нужен UID. Сквады сносят и раскатывают заново под тем же именем; для
кластера это разные объекты, а для графа — одна строка, потому что ключ узла
`(namespace, name, node_kind)`. В результате к новому стенду прилипает
история предыдущего: замер прода 14.08.2026 — `squad-1-shared` имеет узлы на
82 дня старше самого namespace и 39 775 health-точек прошлой инкарнации.
Детектор аномалий сравнивает текущие метрики с этой историей, то есть
сравнивает новый стенд со старым.

Почему присутствие считается по времени, а не по доле. Прежний
`drift_cleanup` абортился при `drift_pct > 20%` и на 29.8% перестал работать
вовсе — то есть заблокировался ровно тогда, когда мусора накопилось больше
всего. Здесь namespace, которого нет в кластере, лишь помечается `missing` с
отметкой времени; сколько его нет — считает `missing_since`. Любая
транзиентная ошибка самоисправляется возвратом в `active`, а для последствий
(шаг B5) потребуются сотни подтверждений за месяц.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, Optional

import structlog
from sqlalchemy.orm import Session

from app.knowledge_graph.kubectl_breaker import run_kubectl
from app.knowledge_graph.schema import (NS_STATE_ACTIVE, NS_STATE_MISSING,
                                        Namespace)
from app.services.audit_logger import audit_service

log = structlog.get_logger()

__all__ = ["sync_namespace_lifecycle", "K8sNamespaceFetchError"]


class K8sNamespaceFetchError(RuntimeError):
    """kubectl не отдал список namespace — состояние кластера неизвестно.

    Отличается от «namespace честно не стало»: при этой ошибке НИЧЕГО не
    помечается missing. Пустой ответ трактуется так же, потому что кластер без
    namespace физически невозможен — это признак сбоя, а не факт.
    """


def _fetch_namespaces() -> Dict[str, Dict[str, Any]]:
    """{name: {uid, created_at}} из кластера. Бросает при сбое."""
    try:
        out = run_kubectl(["kubectl", "get", "ns", "-o", "json"], timeout=30)
    except Exception as e:  # noqa: BLE001
        raise K8sNamespaceFetchError(f"kubectl get ns: {e}") from e
    if out.returncode != 0:
        raise K8sNamespaceFetchError(
            f"kubectl get ns rc={out.returncode}: {out.stderr.strip()[:200]}"
        )
    try:
        items = json.loads(out.stdout).get("items", [])
    except Exception as e:  # noqa: BLE001
        raise K8sNamespaceFetchError(f"невалидный JSON от kubectl: {e}") from e

    result: Dict[str, Dict[str, Any]] = {}
    for it in items:
        meta = it.get("metadata") or {}
        name = meta.get("name")
        if not name:
            continue
        result[name] = {
            "uid": meta.get("uid"),
            "created_at": _parse_ts(meta.get("creationTimestamp")),
        }
    if not result:
        # Кластер без namespace невозможен: это сбой, а не «всё исчезло».
        raise K8sNamespaceFetchError("kubectl вернул пустой список namespace")
    return result


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        # Все timestamp'ы в БД — naive UTC (см. self_health._now).
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, AttributeError):
        return None


def sync_namespace_lifecycle(db: Session) -> Dict[str, Any]:
    """Сверить kg_namespaces с кластером: присутствие и идентичность.

    Что делает:
      * новый namespace → строка с `incarnation=1`, `state=active`;
      * известный и живой → обновляет `last_seen_at`, снимает `missing`;
      * **сменился `k8s_uid`** → `incarnation += 1`, `first_seen_at=now`,
        событие `KG_NAMESPACE_REINCARNATED` в audit. Историю НЕ трогает;
      * пропал из кластера → `state=missing`, `missing_since=now` (однократно).

    Чего НЕ делает (шаги B5/B6): не удаляет узлы, рёбра и историю, не переводит
    в `retired`. Пока это чистое наблюдение — по нему за неделю станет видно,
    как часто стенды пересоздаются и бывают ли «намеренные» пересоздания, при
    которых историю терять нельзя.

    Сбой `kubectl` → K8sNamespaceFetchError и НИ ОДНОЙ пометки missing:
    неизвестное состояние кластера не повод объявлять стенды исчезнувшими.
    """
    live = _fetch_namespaces()          # бросает при сбое — до всякой записи
    now = datetime.utcnow()

    # str(): SQLAlchemy-колонка в ключе словаря сбивает вывод типов —
    # дальше по коду ключ сравнивается с именами из kubectl.
    known: Dict[str, Namespace] = {
        str(ns.namespace): ns for ns in db.query(Namespace).all()
    }
    stats = {
        "live": len(live), "known": len(known),
        "created": 0, "reincarnated": 0, "returned": 0, "marked_missing": 0,
    }

    for name, info in live.items():
        row = known.get(name)
        if row is None:
            db.add(Namespace(
                namespace=name, k8s_uid=info["uid"],
                k8s_created_at=info["created_at"], incarnation=1,
                state=NS_STATE_ACTIVE, first_seen_at=now, last_seen_at=now,
            ))
            stats["created"] += 1
            continue

        # Смена UID = другой объект в кластере под тем же именем.
        # row.k8s_uid is None — строка из backfill: инкарнацию не знали,
        # поэтому просто запоминаем текущий UID, не считая это пересозданием.
        if row.k8s_uid and info["uid"] and row.k8s_uid != info["uid"]:
            row.incarnation = (row.incarnation or 1) + 1  # type: ignore[assignment]
            row.first_seen_at = now  # type: ignore[assignment]
            stats["reincarnated"] += 1
            audit_service.log_event("KG_NAMESPACE_REINCARNATED", {
                "namespace": name,
                "incarnation": row.incarnation,
                "previous_uid": row.k8s_uid,
                "current_uid": info["uid"],
                # Историю на этом шаге НЕ удаляем — только фиксируем факт.
                "history_purged": False,
            })
            log.warning("kg_namespace.reincarnated", namespace=name,
                        incarnation=row.incarnation)

        if row.state == NS_STATE_MISSING:
            stats["returned"] += 1
            log.info("kg_namespace.returned", namespace=name)

        row.k8s_uid = info["uid"] or row.k8s_uid  # type: ignore[assignment]
        row.k8s_created_at = info["created_at"] or row.k8s_created_at  # type: ignore[assignment]
        row.state = NS_STATE_ACTIVE  # type: ignore[assignment]
        row.last_seen_at = now  # type: ignore[assignment]
        row.missing_since = None  # type: ignore[assignment]

    for name, row in known.items():
        if name in live or row.state != NS_STATE_ACTIVE:
            continue
        row.state = NS_STATE_MISSING  # type: ignore[assignment]
        # Ставится ОДИН раз: по нему считается срок до забвения, и повторная
        # запись обнуляла бы отсчёт на каждом тике.
        row.missing_since = now  # type: ignore[assignment]
        stats["marked_missing"] += 1
        log.info("kg_namespace.marked_missing", namespace=name)

    db.commit()
    log.info("kg_namespace.lifecycle_synced", **stats)
    return stats
