"""Срок жизни узлов `stale_class='gone'`: снесённый стенд не живёт в графе вечно.

Замер 18.11.2026 (прод): из 24 811 узлов `kg_services` 12 342 — `gone`,
половина графа. Класс ставит `namespace_lifecycle`, когда namespace узла
исчез из кластера, и на этом жизненный цикл обрывался: состояние
`kg_namespaces.state='retired'` («забыт: узлы и история удалены») в схеме
есть с 14.08.2026, но не выставлял его никто. 167 namespace числятся
`missing`, самый старый — с 15.08.2026; 79 из них (4853 узла) отсутствуют
дольше тридцати дней.

**Чем `gone` отличается от соседних классов.** `expected_stale` и
`suspicious_stale` — ответ классификатора давности деплоя про СУЩЕСТВУЮЩИЙ
сервис («давно не катился»): узел жив, его читают дайджест и отчёты, и
удалять его нельзя ни по какому сроку. `gone` — ответ lifecycle про
существование: namespace узла нет в кластере. Под срок попадает только он.

**Что значит «вернуться».** Сквады пересоздают под тем же именем. Вернувшийся
namespace lifecycle переводит в `active` тем же тиком (каждые десять минут),
обнуляет `missing_since` и пересчитывает класс узлов. Поэтому отсчёт ведётся
не от узла, а от `kg_namespaces.missing_since`: вернулся хоть на тик — срок
начинается заново. Удаляются только узлы, у которых одновременно
`stale_class='gone'`, namespace не `active` и отсутствует дольше TTL.
Если стенд вернётся ПОСЛЕ удаления, синк топологии заведёт узлы заново —
чистыми, без истории прежнего воплощения (ровно то, чего добивается
`purge_stale_*_after_reincarnation`); строка `kg_namespaces` остаётся, и
счётчик инкарнаций не теряется.

**Защита от слепого lifecycle.** Всё решение стоит на том, что lifecycle
жив и снимает отметку с вернувшихся. Если он не ходил (воркер лежит, kubectl
не отвечает), `missing_since` может врать: стенд давно вернулся, а отметка
осталась. Поэтому прогон требует свежего свидетельства — хотя бы один
namespace с `last_seen_at` за последние `LIFECYCLE_FRESHNESS`; нет — отказ.

**Ссылки на узел.** В проде FK на kg_services есть только у
`kg_incidents.service_id` (таблицы наблюдений созданы до появления FK в
миграциях), в свежей базе — у всех. Код не полагается ни на то, ни на
другое и убирает ссылки сам, одной транзакцией с узлом:

  * наблюдения и состояние узла удаляются: health (сырьё и часовые
    агрегаты), аномалии, агрегаты сигналов, деплои, рёбра с любого конца,
    рёбра томов;
  * история, у которой есть собственный адрес, остаётся и теряет только
    ссылку (`service_id = NULL`): инциденты (хранят namespace и
    service_name), алерты, события подов, наблюдения ingress и логов. NULL
    у этих колонок — штатное состояние «узел в графе не найден».

Узел, на который ссылается открытый инцидент или не закрытый алерт,
пропускается: история ещё пишется.

**Гонка с записью истории.** Вебхук пишет `kg_alerts` / `kg_incidents` с
`service_id` параллельно, поэтому занятость проверяется дважды: дёшево до
блокировки (отсеять батч без транзакции) и ещё раз ПОСЛЕ взятия блокировок
строк узлов, в той же транзакции, что и удаление. Что это даёт:

  * строка истории закоммичена до блокировки — вторая проверка её видит,
    узел пропускается;
  * таблица с настоящим FK на kg_services (в проде — `kg_incidents`, в
    свежей базе — все): INSERT потомка держит на строке узла FOR KEY SHARE.
    Незакоммиченная вставка → `SKIP LOCKED` узел не берёт; вставка после
    нашей блокировки ждёт коммита и получает нарушение FK — ссылку на
    удалённый узел база не примет;
  * таблица без FK (в проде — `kg_alerts`, `kg_pod_events`, наблюдения):
    блокировка узла писателя не останавливает, и строка, закоммиченная
    между второй проверкой и коммитом удаления, осталась бы с висячим
    `service_id`. Её подбирает проход после коммита (`_sweep_late_refs`):
    ссылка снимается, как у остальной истории. Узла уже нет — сохранить
    связь нельзя, можно только не оставить её висячей.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, cast

from sqlalchemy import CursorResult, bindparam, func, text
from sqlalchemy.orm import Session

from app.knowledge_graph.contract import STALE_CLASS_GONE
from app.knowledge_graph.health_retention import RETENTION_ROWS
from app.knowledge_graph.k8s_storage_sync import NODE_SERVICE
from app.knowledge_graph.schema import (NS_STATE_ACTIVE, NS_STATE_RETIRED,
                                        Namespace, Service)
from app.services.audit_logger import audit_service

logger = logging.getLogger(__name__)

__all__ = [
    "purge_gone_nodes", "DEFAULT_GONE_TTL_DAYS", "MIN_GONE_TTL_DAYS",
    "LIFECYCLE_FRESHNESS",
]

#: Через сколько суток отсутствия namespace удалять его узлы.
DEFAULT_GONE_TTL_DAYS = 30

#: Пол TTL. Неделя — заведомо дольше любого пересоздания стенда и любой
#: аварии lifecycle, которую не заметили бы; короче — почти наверняка
#: опечатка, а удаление необратимо.
MIN_GONE_TTL_DAYS = 7

#: Насколько свежим должно быть последнее наблюдение lifecycle. Он ходит
#: каждые десять минут; два часа — запас на деплой и рестарт воркеров.
LIFECYCLE_FRESHNESS = timedelta(hours=2)

#: Узлов на транзакцию. 200 — как у чистки рёбер после реинкарнации:
#: удаление тысяч строк одной транзакцией держит блокировки, пока рядом
#: пишут синки (авария 15.08.2026 с DeadlockDetected).
DEFAULT_NODE_BATCH = 200
#: Узлов за прогон. Первое включение на проде — около пяти тысяч.
DEFAULT_MAX_NODES = 10_000
DEFAULT_TIME_BUDGET_SECONDS = 900.0

#: Таблицы, строки которых удаляются вместе с узлом: (таблица, условие).
#: Порядок не важен — всё в одной транзакции. Литералы, не ввод.
_DELETE_WITH_NODE: tuple[tuple[str, str], ...] = (
    ("kg_service_health", "service_id IN :ids"),
    ("kg_service_health_hourly", "service_id IN :ids"),
    ("kg_anomaly_observations", "service_id IN :ids"),
    ("kg_signal_aggregates", "service_id IN :ids"),
    ("kg_deployments", "service_id IN :ids"),
    # Два запроса вместо OR: каждый идёт по своему индексу (src_id / dst_id).
    ("kg_service_edges", "src_id IN :ids"),
    ("kg_service_edges", "dst_id IN :ids"),
    # У рёбер томов id «тегированный»: на kg_services он указывает только
    # у концов с типом NODE_SERVICE (bind :svc), у pvc/pv это другая таблица.
    ("kg_volume_edges", "src_kind = :svc AND src_id IN :ids"),
    ("kg_volume_edges", "dst_kind = :svc AND dst_id IN :ids"),
)

#: Таблицы, строки которых остаются и теряют только ссылку на узел.
_DETACH_FROM_NODE: tuple[str, ...] = (
    "kg_incidents",
    "kg_alerts",
    "kg_pod_events",
    "kg_ingress_observations",
    "kg_log_observations",
)


def _ids_stmt(sql: str):
    return text(sql).bindparams(bindparam("ids", expanding=True))


def _params(ids: Sequence[int], sql: str) -> Dict[str, Any]:
    params: Dict[str, Any] = {"ids": list(ids)}
    if ":svc" in sql:
        params["svc"] = NODE_SERVICE
    return params


def _lifecycle_is_fresh(db: Session, now: datetime) -> bool:
    last = db.query(func.max(Namespace.last_seen_at)).scalar()
    return last is not None and now - last <= LIFECYCLE_FRESHNESS


def _busy_node_ids(db: Session, ids: Sequence[int]) -> set[int]:
    """Узлы, по которым история ещё пишется: открытый инцидент или алерт."""
    busy: set[int] = set()
    for sql in (
        "SELECT DISTINCT service_id FROM kg_incidents"
        " WHERE status = 'open' AND service_id IN :ids",
        "SELECT DISTINCT service_id FROM kg_alerts"
        " WHERE resolved_at IS NULL AND service_id IN :ids",
    ):
        busy.update(int(r[0]) for r in db.execute(_ids_stmt(sql), {"ids": list(ids)}))
    return busy


def _count_refs(db: Session, ids: Sequence[int]) -> Dict[str, int]:
    """Сколько строк ушло бы вместе с узлами (для dry_run)."""
    counts: Dict[str, int] = {}
    for table, cond in _DELETE_WITH_NODE:
        n = db.execute(
            _ids_stmt(f"SELECT count(*) FROM {table} WHERE {cond}"),  # nosec B608
            _params(ids, cond),
        ).scalar() or 0
        counts[table] = counts.get(table, 0) + int(n)
    for table in _DETACH_FROM_NODE:
        n = db.execute(
            _ids_stmt(f"SELECT count(*) FROM {table} WHERE service_id IN :ids"),  # nosec B608
            {"ids": list(ids)},
        ).scalar() or 0
        counts[f"{table}.detached"] = int(n)
    return counts


def _lock_gone(db: Session, ids: Sequence[int]) -> List[int]:
    """Перечитать узлы батча под блокировкой: только те, что всё ещё `gone`.

    SKIP LOCKED: узел, который прямо сейчас пишет синк (namespace вернулся
    между отбором и удалением), не ждём и не трогаем — он уйдёт следующим
    прогоном или не уйдёт вовсе. SQLite блокировку игнорирует.
    """
    rows = (
        db.query(Service.id)
        .filter(Service.id.in_(list(ids)), Service.stale_class == STALE_CLASS_GONE)
        .with_for_update(skip_locked=True)
        .all()
    )
    return [int(r[0]) for r in rows]


def _delete_batch(db: Session, ids: Sequence[int]) -> Dict[str, int]:
    """Удалить узлы и всё, что на них ссылается. Вызывающий коммитит."""
    counts: Dict[str, int] = {}
    params = {"ids": list(ids)}
    for table in _DETACH_FROM_NODE:
        res = cast(CursorResult, db.execute(
            _ids_stmt(f"UPDATE {table} SET service_id = NULL WHERE service_id IN :ids"),  # nosec B608
            params,
        ))
        counts[f"{table}.detached"] = int(res.rowcount or 0)
    for table, cond in _DELETE_WITH_NODE:
        res = cast(CursorResult, db.execute(
            _ids_stmt(f"DELETE FROM {table} WHERE {cond}"),  # nosec B608
            _params(ids, cond),
        ))
        counts[table] = counts.get(table, 0) + int(res.rowcount or 0)
    res = cast(CursorResult, db.execute(
        _ids_stmt("DELETE FROM kg_services WHERE id IN :ids"), params,
    ))
    counts["kg_services"] = int(res.rowcount or 0)
    return counts


def _sweep_late_refs(db: Session, ids: Sequence[int]) -> Dict[str, int]:
    """Снять ссылки на уже удалённые узлы, появившиеся во время удаления.

    Нужен только там, где FK на kg_services нет (см. «Гонка с записью
    истории» в докстринге модуля): писатель мог закоммитить строку с
    `service_id` узла между проверкой занятости и коммитом удаления.
    Возвращает только ненулевые счётчики; коммитит сам.
    """
    counts: Dict[str, int] = {}
    for table in _DETACH_FROM_NODE:
        res = cast(CursorResult, db.execute(
            _ids_stmt(f"UPDATE {table} SET service_id = NULL WHERE service_id IN :ids"),  # nosec B608
            _params(ids, ""),
        ))
        if res.rowcount:
            counts[f"{table}.detached"] = int(res.rowcount)
    for table, cond in _DELETE_WITH_NODE:
        res = cast(CursorResult, db.execute(
            _ids_stmt(f"DELETE FROM {table} WHERE {cond}"),  # nosec B608
            _params(ids, cond),
        ))
        if res.rowcount:
            counts[table] = counts.get(table, 0) + int(res.rowcount)
    db.commit()
    return counts


def _merge(total: Dict[str, int], part: Dict[str, int]) -> None:
    for key, value in part.items():
        total[key] = total.get(key, 0) + value


def purge_gone_nodes(
    db: Session,
    *,
    ttl_days: int = DEFAULT_GONE_TTL_DAYS,
    dry_run: bool = True,
    node_batch: int = DEFAULT_NODE_BATCH,
    max_nodes: int = DEFAULT_MAX_NODES,
    time_budget_seconds: float = DEFAULT_TIME_BUDGET_SECONDS,
    only_namespaces: Optional[Sequence[str]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Удалить узлы `gone`, чей namespace отсутствует дольше `ttl_days`.

    `dry_run=True` (дефолт) ничего не пишет и возвращает, сколько узлов и
    связанных строк ушло бы (`would_delete_nodes`, `rows`).

    `only_namespaces` — ограничить прогон перечисленными namespace (первое
    включение на одном-двух стендах). Список только сужает: условия срока и
    класса действуют и на них. Пустой/None — все подходящие.

    Статистика:
      * `namespaces_eligible` / `nodes_eligible` — что попало под срок;
      * `nodes_deleted`, `rows` — удалено узлов и строк по таблицам
        (`<таблица>.detached` — строк, у которых снята ссылка);
      * `nodes_skipped_busy` — узлы с открытым инцидентом или алертом
        (проверка до блокировки и повторная под ней);
      * `late_refs` — строк, записанных на узел уже во время его удаления
        (таблицы без FK); ссылки с них сняты проходом после коммита;
      * `namespaces_retired` — namespace без единого узла переведены в
        `retired`;
      * `truncated` — упёрлись в `max_nodes` или бюджет времени;
      * `skipped` — причина отказа, если ничего не делали.
    """
    stamp = now or datetime.utcnow()
    stats: Dict[str, Any] = {
        "dry_run": dry_run, "ttl_days": ttl_days, "cutoff": None,
        "namespaces_eligible": 0, "nodes_eligible": 0,
        "nodes_deleted": 0, "would_delete_nodes": 0, "nodes_skipped_busy": 0,
        "namespaces_retired": 0, "late_refs": 0, "rows": {},
        "truncated": False, "skipped": None,
    }
    if ttl_days < MIN_GONE_TTL_DAYS:
        logger.error("gone_retention.refused ttl_days=%s < min=%s", ttl_days, MIN_GONE_TTL_DAYS)
        stats["skipped"] = (
            f"ttl_days={ttl_days} меньше минимума {MIN_GONE_TTL_DAYS}: стенд "
            "могут пересоздать, а удаление необратимо"
        )
        return stats
    if not _lifecycle_is_fresh(db, stamp):
        logger.warning(
            "gone_retention.lifecycle_stale — namespace_lifecycle не отмечал "
            "живых namespace дольше %s, отметкам missing_since верить нельзя",
            LIFECYCLE_FRESHNESS,
        )
        stats["skipped"] = "lifecycle_stale"
        db.rollback()
        return stats

    cutoff = stamp - timedelta(days=ttl_days)
    stats["cutoff"] = cutoff.isoformat()
    ns_query = (
        db.query(Namespace.namespace, Namespace.missing_since)
        .filter(Namespace.state != NS_STATE_ACTIVE,
                Namespace.missing_since.isnot(None),
                Namespace.missing_since < cutoff)
    )
    only = sorted({str(n).strip() for n in (only_namespaces or []) if str(n).strip()})
    if only:
        ns_query = ns_query.filter(Namespace.namespace.in_(only))
        stats["only_namespaces"] = only
    namespaces = [
        (str(ns), since) for ns, since in
        ns_query.order_by(Namespace.missing_since).all()
    ]
    started = time.monotonic()
    processed = 0

    for ns_name, since in namespaces:
        if processed >= max_nodes or time.monotonic() - started > time_budget_seconds:
            stats["truncated"] = True
            break
        node_ids = [
            int(r[0]) for r in
            db.query(Service.id)
            .filter(Service.namespace == ns_name,
                    Service.stale_class == STALE_CLASS_GONE)
            .order_by(Service.id)
            .all()
        ]
        if not node_ids:
            if not dry_run:
                stats["namespaces_retired"] += _retire_if_empty(db, ns_name)
            continue
        stats["namespaces_eligible"] += 1
        stats["nodes_eligible"] += len(node_ids)
        ns_rows: Dict[str, int] = {}
        ns_deleted = 0

        for offset in range(0, len(node_ids), node_batch):
            if processed >= max_nodes or time.monotonic() - started > time_budget_seconds:
                stats["truncated"] = True
                break
            chunk = node_ids[offset:offset + node_batch]
            processed += len(chunk)
            busy = _busy_node_ids(db, chunk)
            stats["nodes_skipped_busy"] += len(busy)
            chunk = [i for i in chunk if i not in busy]
            if not chunk:
                continue
            if dry_run:
                _merge(ns_rows, _count_refs(db, chunk))
                stats["would_delete_nodes"] += len(chunk)
                continue
            try:
                # Состояние namespace перечитывается под той же транзакцией,
                # что и удаление: вернулся между отбором и батчем — выходим.
                state = (
                    db.query(Namespace.state)
                    .filter(Namespace.namespace == ns_name)
                    .scalar()
                )
                if state is None or state == NS_STATE_ACTIVE:
                    db.rollback()
                    logger.info("gone_retention.namespace_returned namespace=%s", ns_name)
                    break
                locked = _lock_gone(db, chunk)
                # Занятость перепроверяется ПОД блокировкой: инцидент или
                # алерт, закоммиченный между первой проверкой и этим местом,
                # иначе ушёл бы вместе с узлом как «закрытая история».
                late_busy = _busy_node_ids(db, locked) if locked else set()
                if late_busy:
                    stats["nodes_skipped_busy"] += len(late_busy)
                    locked = [i for i in locked if i not in late_busy]
                if locked:
                    counts = _delete_batch(db, locked)
                    _merge(ns_rows, counts)
                    ns_deleted += counts.get("kg_services", 0)
                db.commit()
                if locked:
                    late = _sweep_late_refs(db, locked)
                    if late:
                        logger.warning(
                            "gone_retention.late_refs namespace=%s %s — строки "
                            "записаны во время удаления узлов, ссылки сняты",
                            ns_name, late,
                        )
                        stats["late_refs"] += sum(late.values())
                        _merge(ns_rows, late)
            except Exception:
                db.rollback()
                raise

        _merge(stats["rows"], ns_rows)
        if dry_run:
            continue
        stats["nodes_deleted"] += ns_deleted
        if ns_deleted:
            audit_service.log_event("KG_GONE_NODES_PURGED", {
                "namespace": ns_name,
                "missing_since": since.isoformat() if since else None,
                "ttl_days": ttl_days,
                "nodes_deleted": ns_deleted,
                "rows": ns_rows,
            })
        stats["namespaces_retired"] += _retire_if_empty(db, ns_name)

    db.rollback()   # закрыть транзакцию последнего чтения
    action = "would_delete" if dry_run else "deleted"
    nodes = stats["would_delete_nodes"] if dry_run else stats["nodes_deleted"]
    RETENTION_ROWS.labels(target="kg_services", action=action).inc(nodes)
    for key, value in stats["rows"].items():
        if key == "kg_services" or not value:
            continue
        if key.endswith(".detached"):
            RETENTION_ROWS.labels(
                target=key.removesuffix(".detached"),
                action="would_detach" if dry_run else "detached",
            ).inc(value)
        else:
            RETENTION_ROWS.labels(target=key, action=action).inc(value)
    if stats["truncated"]:
        logger.warning(
            "gone_retention.truncated nodes=%s — остаток уйдёт следующим прогоном", nodes,
        )
    logger.info("gone_retention.done %s", stats)
    return stats


def _retire_if_empty(db: Session, namespace: str) -> int:
    """Перевести namespace в `retired`, если узлов в графе у него не осталось.

    `retired` — «забыт: узлы и история удалены» (schema.NS_STATES). Строка
    остаётся: по ней lifecycle при возврате стенда увидит смену UID и
    поднимет номер инкарнации. Условие `state != active` в самом UPDATE —
    чтобы не затереть возврат, случившийся между чтением и записью.
    """
    left = (
        db.query(func.count(Service.id))
        .filter(Service.namespace == namespace)
        .scalar()
    ) or 0
    if left:
        db.rollback()
        return 0
    updated = (
        db.query(Namespace)
        .filter(Namespace.namespace == namespace,
                Namespace.state != NS_STATE_ACTIVE,
                Namespace.state != NS_STATE_RETIRED)
        .update({Namespace.state: NS_STATE_RETIRED}, synchronize_session=False)
    )
    db.commit()
    return int(updated or 0)
