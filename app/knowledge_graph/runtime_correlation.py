"""PodEvent runtime correlation — cheap OTEL-substitute для подтверждения edges.

Если у src сервиса и dst сервиса warning-события (BackOff/Unhealthy/
OOMKilled/FailedScheduling/CrashLoopBackOff/FailedMount/ImagePullBackOff)
сваливаются в одном временном окне N+ раз за неделю — это **runtime сигнал**
что A действительно зависит от B (когда B плохо, A тоже плохо).

Это новый высокоприоритетный discovery_source для уже существующих edges.
**Новые edges из ничего не создаём** — это слишком noisy без direction-инфо.
Симметрия (A↔B co-fail) не определяет направление, поэтому подтверждаем
только если edge уже существует в KG со своим direction.

Beat-task `kg_runtime_correlation_sync` каждые 30 мин. Sliding window 7 дней —
дорогой запрос, чаще нет смысла. Idempotent: повторный run не дублирует
`runtime_correlation` в discovery_sources (см. populator.upsert_edge merge).

CLI: `python -m app.knowledge_graph.runtime_correlation`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple, cast

from sqlalchemy.orm import Session

from app.knowledge_graph.schema import PodEvent, Service, ServiceEdge

log = logging.getLogger(__name__)


# Reasons которые считаем «runtime degradation signal». Совпадают с
# diagnostic-subset из k8s_events_sync._WARN_REASONS, но более узкий — только
# самые информативные для зависимостей. Информационный шум типа NodeNotReady
# (cluster-wide) исключаем: он бьёт ВСЕ pods на ноде и даст лживые
# co-occurrences для каждой пары сервисов в ns.
DEFAULT_CORRELATION_REASONS = frozenset({
    "BackOff",
    "Unhealthy",
    "OOMKilled",
    "FailedScheduling",
    "CrashLoopBackOff",
    "FailedMount",
    "ImagePullBackOff",
})

# Source-marker который пишется в edges.extras.discovery_sources.
# Совпадает с ключом в confidence._SOURCE_PRECEDENCE (0.95 — tier 1).
RUNTIME_CORRELATION_SOURCE = "kg_sync/runtime_corr"


@dataclass
class CoOccurrence:
    """Результат корреляции пары (src_service, dst_service)."""

    edge_id: int
    src_id: int
    dst_id: int
    src_name: str
    dst_name: str
    count: int
    last_window_at: datetime
    reasons: List[str]

    def as_dict(self) -> Dict[str, object]:
        return {
            "edge_id": self.edge_id,
            "src_id": self.src_id,
            "dst_id": self.dst_id,
            "src": self.src_name,
            "dst": self.dst_name,
            "count": self.count,
            "last_window_at": self.last_window_at.isoformat(),
            "reasons": self.reasons,
        }


def _count_co_occurrences(
    src_events: List[PodEvent],
    dst_events: List[PodEvent],
    window: timedelta,
) -> Tuple[int, Optional[datetime], List[str]]:
    """Сколько пар (src_event, dst_event) с |t_src - t_dst| < window.

    Each pair считается один раз. Возвращает count, last window timestamp
    (max из всех matched t), и distinct reasons (union по обоим сторонам).

    Алгоритм O(N+M) с двумя указателями: события уже отсортированы по
    first_seen. Для типичного volume (за 7d на пару сервисов <100 events
    с каждой стороны) запас комфортный.
    """
    if not src_events or not dst_events:
        return 0, None, []

    window_sec = window.total_seconds()
    # Гарантируем сортировку (defensive — БД-запрос ORDER BY уже даёт).
    src_sorted = sorted(src_events, key=lambda e: cast(datetime, e.first_seen))
    dst_sorted = sorted(dst_events, key=lambda e: cast(datetime, e.first_seen))

    count = 0
    last_at: Optional[datetime] = None
    reasons_set: set[str] = set()

    # Two-pointer: для каждого src event находим dst events в окне.
    # Не уникальный pairing (одно dst может матчить несколько src) — это
    # ОК, потому что мы хотим знать «сколько раз они сваливались вместе».
    for s_ev in src_sorted:
        s_ts = cast(datetime, s_ev.first_seen)
        for d_ev in dst_sorted:
            d_ts = cast(datetime, d_ev.first_seen)
            delta = abs((d_ts - s_ts).total_seconds())
            if delta < window_sec:
                count += 1
                latest = max(s_ts, d_ts)
                if last_at is None or latest > last_at:
                    last_at = latest
                if s_ev.reason:
                    reasons_set.add(cast(str, s_ev.reason))
                if d_ev.reason:
                    reasons_set.add(cast(str, d_ev.reason))

    return count, last_at, sorted(reasons_set)


def _load_pod_events(
    db: Session,
    service_id: int,
    since: datetime,
    reasons: frozenset[str],
) -> List[PodEvent]:
    """PodEvent для сервиса в окне since..now, только runtime-degradation
    reasons. Сортировка по first_seen asc — нужна для two-pointer корреляции.
    """
    return (
        db.query(PodEvent)
        .filter(
            PodEvent.service_id == service_id,
            PodEvent.first_seen >= since,
            PodEvent.reason.in_(reasons),
        )
        .order_by(PodEvent.first_seen.asc())
        .all()
    )


def _is_synthetic(service: Service) -> bool:
    """Synthetic services (vm-*, *-backup, observability-exporters) исключены
    из корреляции — у них либо нет edges (synthetic flag), либо edges
    случайные и не несут runtime-смысла.
    """
    return bool(service.synthetic)


async def find_co_occurring_pod_events(
    db: Session,
    window_minutes: int = 15,
    min_correlation_count: int = 2,
    lookback_days: int = 7,
    *,
    now: Optional[datetime] = None,
) -> List[CoOccurrence]:
    """Найти пары (service_a, service_b) где warning-события сваливались в окне.

    Алгоритм:
    1. Для каждой существующей edge (src, dst): найти все warning events
       для src и dst в `lookback_days`.
    2. Пары событий с |t_src - t_dst| < window_minutes считаются co-occurring.
    3. Если co-occurrence count >= min_correlation_count → возвращаем
       CoOccurrence (kandidat на edge-confirmation).

    Async-сигнатура для совместимости с tasks.py — внутри pure SQL/CPU,
    awaitable surface — будущая совместимость с async DB-драйвером.
    """
    now = now or datetime.utcnow()
    since = now - timedelta(days=lookback_days)
    window = timedelta(minutes=window_minutes)
    reasons = DEFAULT_CORRELATION_REASONS

    # Берём ТОЛЬКО существующие edges. Не создаём новые из ничего —
    # это слишком noisy без direction-инфо (см. модуль docstring).
    edges: List[ServiceEdge] = db.query(ServiceEdge).all()
    if not edges:
        return []

    # Cache PodEvent-листов per service — одна edge часто шарит сервис с
    # другой (звезда: A → B, A → C, A → D). Без cache N×M запросов в БД.
    # Per-run cache — не глобальный, чтобы свежий beat-tick всегда читал
    # актуальное состояние.
    events_cache: Dict[int, List[PodEvent]] = {}

    def _get_events(service_id: int) -> List[PodEvent]:
        if service_id not in events_cache:
            events_cache[service_id] = _load_pod_events(
                db, service_id, since, reasons,
            )
        return events_cache[service_id]

    results: List[CoOccurrence] = []
    for edge in edges:
        src = edge.src
        dst = edge.dst
        if src is None or dst is None:
            continue
        # Synthetic сервисы исключаем — у них либо нет edges, либо edges
        # observability-стороны (не настоящая dependency).
        if _is_synthetic(src) or _is_synthetic(dst):
            continue

        src_events = _get_events(cast(int, src.id))
        if not src_events:
            continue
        dst_events = _get_events(cast(int, dst.id))
        if not dst_events:
            continue

        count, last_at, ev_reasons = _count_co_occurrences(
            src_events, dst_events, window,
        )
        if count < min_correlation_count or last_at is None:
            continue

        results.append(CoOccurrence(
            edge_id=cast(int, edge.id),
            src_id=cast(int, src.id),
            dst_id=cast(int, dst.id),
            src_name=cast(str, src.name),
            dst_name=cast(str, dst.name),
            count=count,
            last_window_at=last_at,
            reasons=ev_reasons,
        ))

    return results


def _apply_confirmation(edge: ServiceEdge, co: CoOccurrence, now: datetime) -> bool:
    """Помечает edge как runtime-correlation-confirmed.

    Возвращает True если что-то изменилось (для метрики «confirmed_edges»
    в результате задачи). Idempotent: если source уже в discovery_sources,
    мы лишь обновляем `runtime_correlation` extras (count/last_window_at)
    и last_seen_at — повторных дубликатов в массиве не плодим.
    """
    extras: Dict[str, object] = dict(edge.extras or {})
    sources_raw = extras.get("discovery_sources") or []
    sources: List[str] = list(sources_raw) if isinstance(sources_raw, list) else []

    newly_added = RUNTIME_CORRELATION_SOURCE not in sources
    if newly_added:
        sources.append(RUNTIME_CORRELATION_SOURCE)
        extras["discovery_sources"] = sources

    # Audit-trail для post-mortem: какой count, когда последний window,
    # какие reasons. Перезаписывается с каждым новым run-ом — это OK,
    # это «текущее состояние корреляции», не история.
    extras["runtime_correlation"] = {
        "count": co.count,
        "last_window_at": co.last_window_at.isoformat(),
        "reasons": co.reasons,
    }
    # cast(Any, ...) — SQLAlchemy Column[T] vs T долг, см. PR #66 паттерн.
    edge.extras = cast(Any, extras)
    edge.last_seen_at = cast(Any, now)
    return newly_added


async def run_runtime_correlation_sync(
    db: Session,
    *,
    window_minutes: int = 15,
    min_correlation_count: int = 2,
    lookback_days: int = 7,
    now: Optional[datetime] = None,
) -> Dict[str, object]:
    """Beat-task entry: найти co-occurrences и проапдейтить edges.

    Returns dict со счётчиками для логов/audit:
        edges_total      — сколько edges смотрели
        candidates       — сколько прошли min_count threshold
        newly_confirmed  — у скольких источник добавлен впервые
        refreshed        — у скольких только extras обновлены (idempotent)
    """
    now = now or datetime.utcnow()

    co_list = await find_co_occurring_pod_events(
        db,
        window_minutes=window_minutes,
        min_correlation_count=min_correlation_count,
        lookback_days=lookback_days,
        now=now,
    )

    # Точный count edges (без synthetic-фильтра) — для метрики покрытия.
    edges_total = db.query(ServiceEdge).count()

    newly_confirmed = 0
    refreshed = 0
    for co in co_list:
        edge = db.get(ServiceEdge, co.edge_id)
        if edge is None:
            continue
        if _apply_confirmation(edge, co, now):
            newly_confirmed += 1
        else:
            refreshed += 1

    db.commit()

    stats = {
        "edges_total": edges_total,
        "candidates": len(co_list),
        "newly_confirmed": newly_confirmed,
        "refreshed": refreshed,
        "window_minutes": window_minutes,
        "min_correlation_count": min_correlation_count,
        "lookback_days": lookback_days,
        "now": now.isoformat(),
    }
    log.info(
        "runtime_correlation.done edges=%d candidates=%d newly=%d refreshed=%d "
        "window=%dm min=%d lookback=%dd",
        edges_total, len(co_list), newly_confirmed, refreshed,
        window_minutes, min_correlation_count, lookback_days,
    )
    return stats


if __name__ == "__main__":
    import asyncio

    from app.database import SessionLocal

    db = SessionLocal()
    try:
        print(asyncio.run(run_runtime_correlation_sync(db)))
    finally:
        db.close()
