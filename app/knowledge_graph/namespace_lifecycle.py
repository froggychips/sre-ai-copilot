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
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Set, cast

import structlog
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core.timeutil import parse_ts
from app.knowledge_graph.contract import STALE_CLASS_GONE
from app.knowledge_graph.kubectl_breaker import run_kubectl
from app.knowledge_graph.schema import (NS_STATE_ACTIVE, NS_STATE_MISSING,
                                        Namespace, Service,
                                        ServiceEdge, ServiceHealth)
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
        labels = meta.get("labels") or {}
        result[name] = {
            "uid": meta.get("uid"),
            "created_at": parse_ts(meta.get("creationTimestamp")),
            # Сырьё для владельца стенда (kg_namespaces.deployed_by/_branch):
            # лейблы ставит TeamCity при раскатке сквада.
            "deployed_by": labels.get("deployed-by") or None,
            "deployed_branch": labels.get("deployed-branch") or None,
        }
    if not result:
        # Кластер без namespace невозможен: это сбой, а не «всё исчезло».
        raise K8sNamespaceFetchError("kubectl вернул пустой список namespace")
    return result




#: Сколько ждать после пересоздания стенда, прежде чем считать рёбра
#: устаревшими. Смысл в том, что рёбра подтверждают синки, и самый редкий из
#: них ходит раз в час (`kg_topology_sync`, `kg_ingress_sync`). Удалять
#: сразу после смены инкарнации значило бы снести то, что синк ещё не успел
#: увидеть, — и граф терял бы связи живого стенда.
#:
#: Два часа = два прогона самого редкого синка. Одного мало: тик может
#: совпасть с окном пересоздания и пройти по полупустому namespace.
_REINCARNATION_GRACE = timedelta(hours=2)

#: Доля рёбер namespace, выше которой чистка НЕ выполняется. Если после
#: grace-периода неподтверждённым оказалось почти всё, объяснение скорее в
#: сломанном синке, чем в том, что стенд действительно потерял все связи, —
#: а массовое удаление по такой причине уже случалось в этом проекте
#: (`edge_decay_guard` заведён ровно после него).
_REINCARNATION_MAX_PURGE_SHARE = 0.9


def purge_stale_edges_after_reincarnation(
    db: Session, now: Optional[datetime] = None, apply: bool = False,
) -> Dict[str, Any]:
    """Убрать рёбра, не подтверждённые после пересоздания namespace.

    Зачем. Пересоздание стенда не трогало ни узлы, ни рёбра — шаг B2
    намеренно только наблюдал. Из-за этого рёбра прежнего воплощения
    оживали вместе с именем: замер 21.08.2026 показал 1033 таких ребра в 22
    пересозданных namespace, и 636 из них — `uses_db`.

    Самый показательный случай: `squad-10-kingdom2` пересоздали (incarnation
    3), и его рёбра от 08.08 на базы удалённого `preprod-kingdom1` снова
    стали утверждением о работающем окружении. Их подчищал перенос
    `db_edge_rehome`, но это лечение симптома: рёбра `routes_to` и
    `uses_nats` того же происхождения (388 и 374) не подчищает никто.

    Критерий — `last_seen_at < first_seen_at` текущего воплощения: ребро не
    подтверждено ни одним синком после того, как стенд появился заново.

    Возвращает статистику; при `apply=False` ничего не пишет.
    """
    stamp = now or datetime.utcnow()
    stats: Dict[str, Any] = {
        "namespaces_checked": 0, "namespaces_purged": 0,
        "edges_deleted": 0, "skipped_in_grace": 0, "skipped_guard": 0,
        "applied": False,
    }

    rows = (
        db.query(Namespace)
        .filter(Namespace.incarnation > 1,
                Namespace.state == NS_STATE_ACTIVE,
                Namespace.first_seen_at.isnot(None))
        .all()
    )
    for ns_row in rows:
        stats["namespaces_checked"] += 1
        born = cast(datetime, ns_row.first_seen_at)
        if stamp - born < _REINCARNATION_GRACE:
            stats["skipped_in_grace"] += 1
            continue

        node_ids = [
            nid for (nid,) in
            db.query(Service.id).filter(Service.namespace == ns_row.namespace).all()
        ]
        if not node_ids:
            continue

        total = (
            db.query(func.count(ServiceEdge.id))
            .filter(or_(ServiceEdge.src_id.in_(node_ids),
                        ServiceEdge.dst_id.in_(node_ids)))
            .scalar()
        ) or 0
        stale_ids = [
            eid for (eid,) in
            db.query(ServiceEdge.id)
            .filter(or_(ServiceEdge.src_id.in_(node_ids),
                        ServiceEdge.dst_id.in_(node_ids)),
                    ServiceEdge.last_seen_at < born)
            .all()
        ]
        if not stale_ids:
            continue
        if total and len(stale_ids) / total > _REINCARNATION_MAX_PURGE_SHARE:
            stats["skipped_guard"] += 1
            log.warning(
                "kg_namespace.purge_skipped_guard",
                namespace=ns_row.namespace,
                stale=len(stale_ids), total=total,
            )
            continue

        stats["namespaces_purged"] += 1
        stats["edges_deleted"] += len(stale_ids)
        if not apply:
            continue

        # Батчами: удаление тысяч строк одной транзакцией держит блокировки,
        # пока рядом пишут синки (авария 15.08.2026 с DeadlockDetected).
        for offset in range(0, len(stale_ids), 200):
            chunk = stale_ids[offset:offset + 200]
            db.query(ServiceEdge).filter(ServiceEdge.id.in_(chunk)).delete(
                synchronize_session=False
            )
            db.commit()
        audit_service.log_event("KG_NAMESPACE_EDGES_PURGED", {
            "namespace": ns_row.namespace,
            "incarnation": ns_row.incarnation,
            "edges_deleted": len(stale_ids),
            "edges_total": total,
        })

    if apply:
        db.commit()
        stats["applied"] = True
    log.info("kg_namespace.reincarnation_purge", **stats)
    return stats


def purge_stale_health_after_reincarnation(
    db: Session, now: Optional[datetime] = None, apply: bool = False,
) -> Dict[str, Any]:
    """Убрать health-точки, снятые с прежнего воплощения стенда.

    Зачем это важнее, чем кажется. Детектор аномалий строит baseline по
    последним семи дням `kg_service_health`. Если стенд пересоздали, в это
    окно попадают замеры ПРЕЖНЕГО стенда — другого по составу и нагрузке, —
    и новый сравнивается со старым.

    Замер 21.08.2026: 262 657 точек прежних инкарнаций внутри baseline-окна
    (12% от 2 135 538 за семь дней), затронуто 797 сервисов. Всего таких
    точек в базе 1 393 632. Для сравнения: аномалий за сутки 21 303 на 859
    сервисов, и 133 из них аномальны больше двадцати часов из двадцати
    четырёх — то есть «аномалия» стала их постоянным состоянием, что для
    аномалии само по себе противоречие.

    Проблема названа ещё в docstring теста инкарнаций: «squad-1-shared имеет
    узлы на 82 дня старше самого namespace, к ним прилипло 39 775
    health-точек прошлой инкарнации, и детектор аномалий сравнивает новый
    стенд со старым». Здесь это наконец убирается.

    Защиты те же, что у чистки рёбер, и по тем же причинам: grace-период
    (метрики пишутся раз в десять минут, но `kg_metrics_sync` мог не успеть
    пройти) и guard на долю — если под удаление попало почти всё, объяснение
    скорее в сломанной таблице `kg_namespaces`, чем в реальности.
    """
    stamp = now or datetime.utcnow()
    stats: Dict[str, Any] = {
        "namespaces_checked": 0, "namespaces_purged": 0,
        "points_deleted": 0, "skipped_in_grace": 0, "skipped_guard": 0,
        "applied": False,
    }

    rows = (
        db.query(Namespace)
        .filter(Namespace.incarnation > 1,
                Namespace.state == NS_STATE_ACTIVE,
                Namespace.first_seen_at.isnot(None))
        .all()
    )
    for ns_row in rows:
        stats["namespaces_checked"] += 1
        born = cast(datetime, ns_row.first_seen_at)
        if stamp - born < _REINCARNATION_GRACE:
            stats["skipped_in_grace"] += 1
            continue

        node_ids = [
            nid for (nid,) in
            db.query(Service.id).filter(Service.namespace == ns_row.namespace).all()
        ]
        if not node_ids:
            continue

        total = (
            db.query(func.count(ServiceHealth.id))
            .filter(ServiceHealth.service_id.in_(node_ids))
            .scalar()
        ) or 0
        stale = (
            db.query(func.count(ServiceHealth.id))
            .filter(ServiceHealth.service_id.in_(node_ids),
                    ServiceHealth.ts < born)
            .scalar()
        ) or 0
        if not stale:
            continue

        # Guard здесь НЕ долевой, в отличие от чистки рёбер, и это разница
        # по существу. У пересозданного стенда естественно, что почти вся
        # история относится к прежней инкарнации: метрики пишутся раз в
        # десять минут, а история живёт тридцать дней. Долевой порог
        # (проба 21.08.2026 с ним) отсёк 11 namespace из 22 — ровно те, где
        # baseline загрязнён сильнее всего и чистка нужнее.
        #
        # Опасность здесь другая: неверный `first_seen_at`. Если lifecycle
        # ошибочно пометит стенд заново рождённым, под удаление уйдёт вся
        # история. Защита от этого — потребовать доказательство, что НОВЫЙ
        # стенд действительно пишет метрики: хотя бы одна точка новее
        # рождения. Нет таких — значит либо метрики не идут, либо
        # `first_seen_at` врёт, и в обоих случаях удалять нечего.
        fresh = (
            db.query(func.count(ServiceHealth.id))
            .filter(ServiceHealth.service_id.in_(node_ids),
                    ServiceHealth.ts >= born)
            .scalar()
        ) or 0
        if fresh == 0:
            stats["skipped_guard"] += 1
            log.warning(
                "kg_namespace.health_purge_skipped_no_fresh_points",
                namespace=ns_row.namespace, stale=stale, total=total,
            )
            continue

        stats["namespaces_purged"] += 1
        stats["points_deleted"] += stale
        if not apply:
            continue

        # Удаляем порциями по времени, а не по списку id: точек бывает
        # десятки тысяч на namespace, и держать их идентификаторы в памяти
        # незачем — воркер уже ловил OOM на списках такого порядка.
        deleted = 0
        while True:
            batch_ids = [
                hid for (hid,) in
                db.query(ServiceHealth.id)
                .filter(ServiceHealth.service_id.in_(node_ids),
                        ServiceHealth.ts < born)
                .limit(2000).all()
            ]
            if not batch_ids:
                break
            db.query(ServiceHealth).filter(
                ServiceHealth.id.in_(batch_ids)
            ).delete(synchronize_session=False)
            db.commit()
            deleted += len(batch_ids)
        audit_service.log_event("KG_NAMESPACE_HEALTH_PURGED", {
            "namespace": ns_row.namespace,
            "incarnation": ns_row.incarnation,
            "points_deleted": deleted,
            "points_total": total,
        })

    if apply:
        db.commit()
        stats["applied"] = True
    log.info("kg_namespace.health_purge", **stats)
    return stats


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
    returned_names: List[str] = []

    for name, info in live.items():
        row = known.get(name)
        if row is None:
            db.add(Namespace(
                namespace=name, k8s_uid=info["uid"],
                k8s_created_at=info["created_at"], incarnation=1,
                state=NS_STATE_ACTIVE, first_seen_at=now, last_seen_at=now,
                deployed_by=info.get("deployed_by"),
                deployed_branch=info.get("deployed_branch"),
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
            returned_names.append(name)
            log.info("kg_namespace.returned", namespace=name)

        row.k8s_uid = info["uid"] or row.k8s_uid  # type: ignore[assignment]
        row.k8s_created_at = info["created_at"] or row.k8s_created_at  # type: ignore[assignment]
        row.state = NS_STATE_ACTIVE  # type: ignore[assignment]
        row.last_seen_at = now  # type: ignore[assignment]
        row.missing_since = None  # type: ignore[assignment]
        # Лейблы — как есть, включая None: перераскатка стенда без лейбла
        # должна стирать прежнего деплойера, а не наследовать его.
        if "deployed_by" in info:
            row.deployed_by = info.get("deployed_by")  # type: ignore[assignment]
            row.deployed_branch = info.get("deployed_branch")  # type: ignore[assignment]

    for name, row in known.items():
        if name in live or row.state != NS_STATE_ACTIVE:
            continue
        row.state = NS_STATE_MISSING  # type: ignore[assignment]
        # Ставится ОДИН раз: по нему считается срок до забвения, и повторная
        # запись обнуляла бы отсчёт на каждом тике.
        row.missing_since = now  # type: ignore[assignment]
        stats["marked_missing"] += 1
        log.info("kg_namespace.marked_missing", namespace=name)

    # Узлы исчезнувших namespace получают stale_class='gone'. Sweep по ВСЕМ
    # не-active строкам, а не только по свежепомеченным: класс у узлов замирал
    # на значении момента сноса (ns-sync по отсутствующему namespace больше не
    # вызывается), и squad-42 утром 08.09.2026 держал 31 сервис в `active`
    # при namespace, удалённых накануне. Идемпотентно — повторный тик ничего
    # не перепишет.
    gone_names = [name for name, row in known.items()
                  if name not in live and row.state != NS_STATE_ACTIVE]
    stats["services_gone"] = mark_services_gone(db, gone_names)
    # Вернувшийся namespace: класс пересчитать сразу, не дожидаясь очередного
    # ns-sync — иначе живой стенд до часа читался бы как снесённый.
    stats["services_reclassified"] = _reclassify_returned(db, returned_names)

    db.commit()
    log.info("kg_namespace.lifecycle_synced", **stats)
    return stats


def missing_namespace_names(db: Session) -> Set[str]:
    """Namespace, которых сейчас нет в кластере (`state != active`).

    Единая точка для обходов «по всем реальным сервисам» (metrics_sync,
    health_score, детектор аномалий, агрегаты сигналов): без неё снесённый
    стенд продолжал жить в графе — VM по несуществующему namespace отдаёт
    пустоту, а пустота, записанная как измерение, давала свежие health-точки
    и health_score узлам squad-42 назавтра после сноса.
    """
    rows = (
        db.query(Namespace.namespace)
        .filter(Namespace.state != NS_STATE_ACTIVE)
        .all()
    )
    return {str(r[0]) for r in rows}


def mark_services_gone(db: Session, namespaces: Iterable[str]) -> int:
    """`stale_class='gone'` всем узлам перечисленных namespace. Идемпотентно.

    Возвращает число строк, у которых класс действительно сменился.
    """
    names = [str(n) for n in namespaces]
    if not names:
        return 0
    updated = (
        db.query(Service)
        .filter(
            Service.namespace.in_(names),
            or_(Service.stale_class.is_(None), Service.stale_class != STALE_CLASS_GONE),
        )
        .update({Service.stale_class: STALE_CLASS_GONE}, synchronize_session=False)
    )
    if updated:
        log.info("kg_namespace.services_marked_gone",
                 namespaces=len(names), services=updated)
    return int(updated or 0)


def _reclassify_returned(db: Session, namespaces: Iterable[str]) -> int:
    """Пересчитать stale_class узлов вернувшихся namespace обычным
    классификатором (`kg_sync._refresh_stale_class_for_namespace`).

    Ленивый импорт: kg_sync тяжёлый и в lifecycle нужен только здесь. Сбой
    пересчёта не роняет lifecycle — класс догонит очередной ns-sync.
    """
    names = [str(n) for n in namespaces]
    if not names:
        return 0
    from app.knowledge_graph.kg_sync import _refresh_stale_class_for_namespace

    # Пересчёт читает kg_services через ORM — отдать ему уже применённые
    # изменения состояния namespace.
    db.flush()
    total = 0
    for name in names:
        try:
            total += int(_refresh_stale_class_for_namespace(db, name) or 0)
        except Exception as e:  # noqa: BLE001 — один namespace не должен ронять тик
            log.warning("kg_namespace.reclassify_failed", namespace=name, error=str(e))
    return total
