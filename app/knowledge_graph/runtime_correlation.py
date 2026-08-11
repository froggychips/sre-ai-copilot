"""PodEvent runtime correlation — cheap OTEL-substitute для подтверждения edges.

Если у src сервиса и dst сервиса warning-события (BackOff/Unhealthy/
OOMKilled/FailedScheduling/CrashLoopBackOff/FailedMount/ImagePullBackOff)
сваливаются в одном временном окне в N+ РАЗНЫХ эпизодах за неделю — это
**runtime сигнал** что A действительно зависит от B (когда B плохо, A тоже
плохо). Эпизод, а не пара событий: одна авария на двух crashloop-сервисах
даёт сотни пар и ничего не доказывает (см. `MIN_CORRELATION_EPISODES`).

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

from sqlalchemy.orm import Session, joinedload

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
    #: Сколько РАЗНЫХ моментов ко-падения (см. `_count_co_occurrences`).
    #: Именно по нему решается «подтверждать или нет», а `count` (пары)
    #: остаётся в audit-trail: 100×100 событий одной аварии дают 10000 пар
    #: и 1 эпизод.
    episodes: int = 0

    def as_dict(self) -> Dict[str, object]:
        return {
            "edge_id": self.edge_id,
            "src_id": self.src_id,
            "dst_id": self.dst_id,
            "src": self.src_name,
            "dst": self.dst_name,
            "count": self.count,
            "episodes": self.episodes,
            "last_window_at": self.last_window_at.isoformat(),
            "reasons": self.reasons,
        }


# Минимум РАЗНЫХ эпизодов ко-падения для подтверждения ребра.
#
# Порог существовал и раньше (`RUNTIME_CORRELATION_MIN_COUNT=2`), но считал
# ПАРЫ событий: у двух crashloop-сервисов одна авария (по 10 BackOff с каждой
# стороны в одном окне) даёт 100 «co-occurrences» — то есть одно совместное
# падение проходило порог с двадцатикратным запасом и навешивало на ребро
# tier-1 source с precedence 0.95. Считаем эпизоды (моменты, разнесённые
# больше чем на window) и требуем минимум три за lookback: кластерная авария
# (нода, image-pull, статика) бьёт всех соседей разом и легко даёт два
# совпадения за неделю, а три независимых эпизода — уже паттерн. Порог из
# settings ниже этого пола не опускаем: конфиг несёт старое pair-значение.
MIN_CORRELATION_EPISODES = 3


def _count_co_occurrences(
    src_events: List[PodEvent],
    dst_events: List[PodEvent],
    window: timedelta,
) -> Tuple[int, Optional[datetime], List[str], int]:
    """Сколько пар (src_event, dst_event) с |t_src - t_dst| < window.

    Возвращает `(count, last_window_at, reasons, episodes)`:
      * `count` — все матчащиеся пары (audit-trail, как было);
      * `last_window_at` — max из всех matched t;
      * `reasons` — distinct reasons (union по обоим сторонам);
      * `episodes` — сколько РАЗНЫХ моментов ко-падения: подряд идущие
        совпадения, разнесённые меньше чем на `window`, считаются одним
        (одна авария = один эпизод, сколько бы событий в неё ни попало).

    Алгоритм — настоящие два указателя, O(N+M): докстринг обещал это и
    раньше, а тело было полным вложенным циклом (на паре crashloop-сервисов
    100×100 событий = 10k итераций на КАЖДОЕ ребро, каждые 30 минут).
    Оба списка отсортированы по first_seen, поэтому границы окна для
    возрастающего `s_ts` двигаются только вперёд.
    """
    if not src_events or not dst_events:
        return 0, None, [], 0

    window_sec = window.total_seconds()
    # Гарантируем сортировку (defensive — БД-запрос ORDER BY уже даёт).
    src_sorted = sorted(src_events, key=lambda e: cast(datetime, e.first_seen))
    dst_sorted = sorted(dst_events, key=lambda e: cast(datetime, e.first_seen))
    dst_ts = [cast(datetime, e.first_seen) for e in dst_sorted]
    n_dst = len(dst_sorted)

    count = 0
    last_at: Optional[datetime] = None
    reasons_set: set[str] = set()
    episodes = 0
    episode_last_at: Optional[datetime] = None

    lo = 0   # первый dst, который ещё не «слишком старый» для текущего src
    hi = 0   # первый dst ЗА окном текущего src
    reasons_marked = 0  # до какого dst reasons уже собраны (каждый — один раз)

    for s_ev in src_sorted:
        s_ts = cast(datetime, s_ev.first_seen)
        while lo < n_dst and (s_ts - dst_ts[lo]).total_seconds() >= window_sec:
            lo += 1
        if hi < lo:
            hi = lo
        while hi < n_dst and (dst_ts[hi] - s_ts).total_seconds() < window_sec:
            hi += 1
        matched = hi - lo
        if matched <= 0:
            continue

        count += matched
        latest = max(s_ts, dst_ts[hi - 1])
        if last_at is None or latest > last_at:
            last_at = latest
        if s_ev.reason:
            reasons_set.add(cast(str, s_ev.reason))
        # reasons каждого dst собираем один раз за весь проход — иначе union
        # снова стоил бы O(N×M) на плотных данных.
        start = max(lo, reasons_marked)
        for idx in range(start, hi):
            if dst_sorted[idx].reason:
                reasons_set.add(cast(str, dst_sorted[idx].reason))
        reasons_marked = max(reasons_marked, hi)

        # Эпизод: новый момент ко-падения, если от предыдущего прошло больше
        # окна. src_sorted идёт по возрастанию, поэтому сравнения хватает.
        if (
            episode_last_at is None
            or (s_ts - episode_last_at).total_seconds() >= window_sec
        ):
            episodes += 1
        episode_last_at = s_ts

    return count, last_at, sorted(reasons_set), episodes


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
    3. Если число РАЗНЫХ эпизодов ко-падения >= порога (не ниже
       `MIN_CORRELATION_EPISODES`) → возвращаем CoOccurrence (кандидат на
       edge-confirmation).

    Async-сигнатура для совместимости с tasks.py — внутри pure SQL/CPU,
    awaitable surface — будущая совместимость с async DB-драйвером.
    """
    now = now or datetime.utcnow()
    since = now - timedelta(days=lookback_days)
    window = timedelta(minutes=window_minutes)
    reasons = DEFAULT_CORRELATION_REASONS
    min_episodes = max(int(min_correlation_count or 0), MIN_CORRELATION_EPISODES)

    # Берём ТОЛЬКО существующие edges. Не создаём новые из ничего —
    # это слишком noisy без direction-инфо (см. модуль docstring).
    #
    # src/dst тянем joinedload'ом: обе relationship нужны КАЖДОМУ ребру
    # (synthetic-фильтр + имена), а ленивая загрузка давала ~2×edges лишних
    # SELECT'ов каждые 30 минут — на графе в тысячи рёбер это тысячи
    # round-trip'ов ради двух колонок.
    edges: List[ServiceEdge] = (
        db.query(ServiceEdge)
        .options(joinedload(ServiceEdge.src), joinedload(ServiceEdge.dst))
        .all()
    )
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

        count, last_at, ev_reasons, episodes = _count_co_occurrences(
            src_events, dst_events, window,
        )
        if episodes < min_episodes or last_at is None:
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
            episodes=episodes,
        ))

    return results


def _apply_confirmation(edge: ServiceEdge, co: CoOccurrence, now: datetime) -> bool:
    """Помечает edge как runtime-correlation-confirmed.

    Возвращает True если что-то изменилось (для метрики «confirmed_edges»
    в результате задачи). Idempotent: если source уже в discovery_sources,
    мы лишь обновляем `runtime_correlation` extras (count/episodes/
    last_window_at/confirmed_at) — повторных дубликатов в массиве не плодим.

    `last_seen_at` НЕ трогаем. Это поле — признак «источник, отвечающий за
    свежесть ЭТОГО kind, снова увидел ребро»: по нему `kg_sync._decay_stale_edges`
    гасит и удаляет рёбра, а `edge_decay_guard` (fallback по данным,
    `max(last_seen_at)` в группе источника) решает, можно ли вообще децаить.
    Освежая его, корреляция делала две плохие вещи разом: env-inferred ребро,
    которого штатный синк больше НЕ видит, вечно оставалось «свежим» и не
    децаилось, а guard видел живой источник при мёртвом синке — то есть
    ровно та тихая эрозия, от которой guard и написан, только наоборот.
    Своё подтверждение пишем в свои поля: `discovery_sources` (tier-1
    precedence 0.95 в confidence-формуле) + `extras.runtime_correlation`.
    """
    extras: Dict[str, object] = dict(edge.extras or {})
    sources_raw = extras.get("discovery_sources") or []
    sources: List[str] = list(sources_raw) if isinstance(sources_raw, list) else []

    newly_added = RUNTIME_CORRELATION_SOURCE not in sources
    if newly_added:
        sources.append(RUNTIME_CORRELATION_SOURCE)
        extras["discovery_sources"] = sources

    # Audit-trail для post-mortem: сколько пар/эпизодов, когда последний
    # window, какие reasons, когда подтверждали. Перезаписывается с каждым
    # новым run-ом — это OK, это «текущее состояние корреляции», не история.
    # `confirmed_at` живёт здесь, а не в `last_seen_at`, именно чтобы не
    # подменять признак свежести чужого источника (см. докстринг).
    extras["runtime_correlation"] = {
        "count": co.count,
        "episodes": co.episodes,
        "last_window_at": co.last_window_at.isoformat(),
        "confirmed_at": now.isoformat(),
        "reasons": co.reasons,
    }
    # cast(Any, ...) — SQLAlchemy Column[T] vs T долг, см. PR #66 паттерн.
    edge.extras = cast(Any, extras)
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

    # Кандидатов забираем ОДНИМ запросом. `db.get` в цикле давал SELECT на
    # каждого: identity map держит инстансы слабой ссылкой, и рёбра,
    # загруженные внутри find_co_occurring_pod_events, к этому моменту уже
    # собраны GC (в CoOccurrence лежат только id).
    edges_by_id: Dict[int, ServiceEdge] = {}
    if co_list:
        edges_by_id = {
            cast(int, e.id): e
            for e in db.query(ServiceEdge)
            .filter(ServiceEdge.id.in_([co.edge_id for co in co_list]))
            .all()
        }

    newly_confirmed = 0
    refreshed = 0
    for co in co_list:
        edge = edges_by_id.get(co.edge_id)
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
        # Фактический порог: параметр не может опуститься ниже пола
        # MIN_CORRELATION_EPISODES (в settings лежит старое pair-значение).
        "min_episodes": max(
            int(min_correlation_count or 0), MIN_CORRELATION_EPISODES,
        ),
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
