"""Endpoints: подтверждение того, что за Service реально стоят поды.

До этого синка граф знал только объявления. `serves_traffic` строится по
совпадению `Service.spec.selector` с labels workload'а — то есть отвечает на
вопрос «должен ли этот Service маршрутизировать трафик туда», но не «делает
ли он это сейчас». Endpoints отвечают на второй вопрос: контроллер kubernetes
записывает в них адреса реально готовых подов.

Замер 15.08.2026: **4732 Service с адресами и 83 без**. Состав пустых
проверен отдельно — ни одного headless (`clusterIP: None`), ни одного
`ExternalName`, ни одного без селектора. То есть все 83 — обычные Service,
которые обязаны кого-то обслуживать и не обслуживают никого. Раньше они
выглядели в графе обычными узлами.

Что делает синк:

  * подтверждает `serves_traffic` источником `k8s_endpoints/ready` — это
    corroboration к ребру от топологического синка, а не новое ребро:
    два независимых источника на одном ребре поднимают его достоверность;
  * записывает в metadata узла число готовых адресов и время проверки,
    чтобы «Service без подов» стал видимым фактом, а не отсутствием данных.

Чего НЕ делает: не строит рёбра Service → Pod. Поды эфемерны, узлов на них
граф не заводит, а owner-chain (Pod → ReplicaSet → Deployment) уже разобран
топологическим синком по селекторам.

Deadman: пустой ответ kubectl трактуется как сбой, а не как «во всём кластере
не осталось endpoints». Без этого один неудачный тик пометил бы все Service
кластера как мёртвые — ровно тот класс аварии, от которого в этом проекте
защищаются `edge_decay_guard` и `namespace_lifecycle`.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.knowledge_graph.kubectl_breaker import (record_failure,
                                                 record_success,
                                                 run_kubectl)
from app.knowledge_graph.populator import upsert_edge
from app.knowledge_graph.schema import NODE_KIND_SERVICE, Service, ServiceEdge

log = logging.getLogger(__name__)

__all__ = ["sync_endpoints", "EndpointsFetchError", "DISCOVERED_BY_ENDPOINTS"]

#: Источник для corroboration `serves_traffic`. Вес — в
#: `confidence._SOURCE_PRECEDENCE`.
DISCOVERED_BY_ENDPOINTS = "k8s_endpoints/ready"

_KUBECTL_TIMEOUT_S = 30

#: Узлов между коммитами. 200 — компромисс: транзакция живёт секунды, а не
#: весь проход, и при этом коммитов не тысячи. Значение того же порядка, что
#: `_COMMIT_BATCH` в k8s_topology_resources_sync.
_COMMIT_BATCH = 200


class EndpointsFetchError(RuntimeError):
    """kubectl не отдал endpoints — состояние кластера неизвестно.

    Отличается от «endpoints честно пусты»: при этой ошибке НИЧЕГО не
    помечается. Пустой список трактуется так же — кластер без единого
    endpoints невозможен, у kube-system они есть всегда.
    """


def _fetch_endpoints() -> List[Dict[str, Any]]:
    """`kubectl get endpoints -A -o json` → items. Бросает при сбое.

    Перед вызовом спрашиваем circuit breaker: если apiserver уже не отвечал
    подряд, идти к нему снова незачем — тридцать задач из расписания только
    добавят нагрузки больному API и займут форки ожиданием таймаутов.
    """
    try:
        out = run_kubectl(
            ["kubectl", "get", "endpoints", "-A", "-o", "json"],
            timeout=_KUBECTL_TIMEOUT_S,
        )
    except Exception as e:  # noqa: BLE001
        record_failure("get endpoints")
        raise EndpointsFetchError(f"kubectl get endpoints: {e}") from e
    if out.returncode != 0:
        record_failure("get endpoints")
        raise EndpointsFetchError(
            f"kubectl get endpoints rc={out.returncode}: {out.stderr.strip()[:200]}"
        )
    try:
        items = json.loads(out.stdout).get("items") or []
    except Exception as e:  # noqa: BLE001
        raise EndpointsFetchError(f"невалидный JSON от kubectl: {e}") from e
    if not items:
        raise EndpointsFetchError("kubectl вернул ноль endpoints — это сбой, а не факт")
    record_success("get endpoints")
    return items


def _ready_addresses(endpoint: Dict[str, Any]) -> int:
    """Сколько готовых адресов за этим Service.

    `notReadyAddresses` намеренно НЕ считаем: под, не прошедший readiness,
    трафик не получает, и для вопроса «обслуживает ли Service кого-нибудь»
    он всё равно что отсутствует.
    """
    total = 0
    for subset in endpoint.get("subsets") or []:
        total += len(subset.get("addresses") or [])
    return total


def _index_endpoints(items: List[Dict[str, Any]]) -> Dict[Tuple[str, str], int]:
    """{(namespace, name): число готовых адресов}."""
    idx: Dict[Tuple[str, str], int] = {}
    for item in items:
        meta = item.get("metadata") or {}
        ns, name = meta.get("namespace"), meta.get("name")
        if not ns or not name:
            continue
        idx[(ns, name)] = _ready_addresses(item)
    return idx


def sync_endpoints(db: Session, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Сверить Service-узлы графа с реальными endpoints кластера.

    Возвращает статистику: сколько узлов сопоставлено, у скольких есть поды,
    сколько оказалось пустыми, сколько рёбер подтверждено.

    Узлы, которых нет в ответе kubectl, не трогаются: их Service мог быть
    только что удалён, и это забота `namespace_lifecycle`/`drift_cleanup`, а
    не этого синка.
    """
    endpoints = _fetch_endpoints()          # бросает при сбое — до всякой записи
    idx = _index_endpoints(endpoints)
    stamp = (now or datetime.utcnow()).isoformat()

    stats: Dict[str, Any] = {
        "endpoints_seen": len(idx),
        "matched": 0,
        "with_pods": 0,
        "empty": 0,
        "edges_corroborated": 0,
        "empty_services": [],
    }

    # Обход батчами по id вместо `.all()` на 6441 узел: держать их всех в
    # identity map незачем, каждый обрабатывается независимо. Пик самого синка
    # невелик (122 МБ), но worker запущен с `--concurrency=4`, и такие
    # «невеликие» пики складываются — 11 OOMKill за 19 часов при лимите 3Gi.
    #
    # Почему НЕ `yield_per`: внутри цикла идёт `commit()`, а он закрывает
    # серверный курсор — `psycopg2.ProgrammingError: named cursor isn't valid
    # anymore` на первом же батче. Проверено на живых данных 16.08.2026;
    # unit-тесты этого не ловят, потому что в SQLite серверных курсоров нет.
    #
    # Список id — это ~6 тысяч int, порядка 50 КБ: цена пренебрежимая.
    node_ids: List[int] = [
        nid for (nid,) in
        db.query(Service.id)
        .filter(Service.node_kind == NODE_KIND_SERVICE, Service.synthetic.is_(False))
        # Детерминированный порядок: два писателя, идущие по строкам в разном
        # порядке, блокируют друг друга крест-накрест. `id` монотонен и
        # одинаков для всех — этого достаточно, чтобы взаимной блокировки не
        # возникало по вине самого обхода.
        .order_by(Service.id)
        .all()
    ]

    for offset in range(0, len(node_ids), _COMMIT_BATCH):
        chunk = node_ids[offset:offset + _COMMIT_BATCH]
        nodes = (
            db.query(Service)
            .filter(Service.id.in_(chunk))
            .order_by(Service.id)
            .all()
        )
        _process_batch(db, nodes, idx, stamp, stats)
        # Коммит на батч, а не на весь проход. Первый плановый прогон
        # (15.08.2026, 11:15) упал с `DeadlockDetected`: транзакция держала
        # блокировки на тысячах строк, пока рядом писал соседний синк.
        # Правило не новое — `kg_sync` коммитит после КАЖДОГО namespace по
        # следам инцидента 08.08.2026.
        db.commit()

    db.commit()
    log.info(
        "endpoints_sync.done matched=%s with_pods=%s empty=%s corroborated=%s",
        stats["matched"], stats["with_pods"], stats["empty"],
        stats["edges_corroborated"],
    )
    if stats["empty"]:
        log.warning(
            "endpoints_sync.services_without_pods count=%s sample=%s",
            stats["empty"], stats["empty_services"][:5],
        )
    return stats


def _process_batch(
    db: Session,
    nodes: List[Service],
    idx: Dict[Tuple[str, str], int],
    stamp: str,
    stats: Dict[str, Any],
) -> None:
    """Обработать один батч узлов. Коммит — на вызывающей стороне."""
    for node in nodes:
        key = (str(node.namespace), str(node.name))
        if key not in idx:
            continue
        ready = idx[key]
        stats["matched"] += 1

        meta: Dict[str, Any] = dict(node.metadata_json or {})
        meta["endpoints_ready"] = ready
        meta["endpoints_checked_at"] = stamp
        node.metadata_json = meta  # type: ignore[assignment]
        # JSON-колонка: SQLAlchemy не видит мутацию словаря по значению, и без
        # явного флага UPDATE не уйдёт. Тот же приём в external_probe_sync.
        flag_modified(node, "metadata_json")

        if ready == 0:
            stats["empty"] += 1
            # Список нужен потребителю целиком: «83 пустых Service» без имён
            # — метрика, а не сигнал, по ней нельзя ничего сделать.
            stats["empty_services"].append(f"{node.namespace}/{node.name}")
            continue

        stats["with_pods"] += 1
        # Подтверждаем уже существующие serves_traffic вторым источником.
        # Новых рёбер не создаём: кого именно обслуживает Service, знает
        # топологический синк по селекторам, а endpoints говорят лишь «да,
        # за ним есть готовые поды».
        for edge in (
            db.query(ServiceEdge)
            .filter(ServiceEdge.src_id == node.id,
                    ServiceEdge.kind == "serves_traffic")
            .all()
        ):
            if edge.dst is None:
                continue
            upsert_edge(
                db, src=node, dst=edge.dst, kind="serves_traffic",
                discovered_by=DISCOVERED_BY_ENDPOINTS,
                extras={"endpoints_ready": ready},
            )
            stats["edges_corroborated"] += 1
