"""KG quality report — baseline snapshot для Phase A (remediation).

Идемпотентный read-only скрипт: считает 5 групп метрик из Postgres KG-БД
(``kg_services``, ``kg_service_edges``, ``kg_alerts``, ``kg_deploys``,
``kg_k8s_jobs``, ``kg_storage_volumes``, ``kg_volume_edges``, ``kg_pod_events``)
и выводит markdown (default) или JSON.

Использование::

    # markdown в stdout
    python -m app.scripts.quality_report

    # JSON в stdout
    python -m app.scripts.quality_report --json

    # сохранить в файл
    python -m app.scripts.quality_report --markdown --output baseline.md

Без записи в БД (никаких INSERT/UPDATE/DELETE). Использует ту же
``SessionLocal`` что и production-копилот; для unit-тестов
``build_report(db)`` принимает session-objект напрямую.

Контекст: после merge 17 PR (Wave 7 X/Y/Z + storage + jobs + owner
inference multi-signal + stale_class + contract v2.1 + Discord UX) нам
нужна точка отсчёта, чтобы Phase A (remediation) мог демонстрировать
improvement, а не угадывать.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, func, or_, text
from sqlalchemy.orm import Session

from app.knowledge_graph.contract import EDGE_KINDS, SYNTHETIC_KINDS
from app.knowledge_graph.schema import (AlertEvent, Deployment, K8sJob,
                                        PodEvent, Service, ServiceEdge,
                                        StorageVolume, VolumeEdge)
from app.knowledge_graph.stale_classifier import (STALE_CLASS_ACTIVE,
                                                  STALE_CLASS_EXPECTED,
                                                  STALE_CLASS_SUSPICIOUS)

log = logging.getLogger(__name__)


# ── --check mode default thresholds (см. CLI args + ENV) ─────────────────────
#
# Это **warning gate**, не blocker: проверки могут понадобиться recalibrated
# при росте графа. Поэтому пороги читаются из ENV/CLI overrides, а не
# хардкод. Если хочется временно отключить gate — `--max-orphan 1.0` и т.п.
DEFAULT_MAX_ORPHAN_PCT: float = 20.0    # services без edges > N% → fail
DEFAULT_MIN_OWNER_PCT: float = 50.0     # owner coverage < N% → fail
DEFAULT_MAX_STALE_NULL_PCT: float = 10.0  # stale_class IS NULL > N% → fail


# ── helpers ──────────────────────────────────────────────────────────────────


def _pct(n: int, d: int) -> Optional[float]:
    """``round(100.0 * n / d, 2)`` или ``None`` если ``d == 0``.

    Возвращаем ``None`` чтобы в JSON/markdown было ``null`` /
    ``n/a`` вместо ложного ``0.0`` (deceptive denominator).
    """
    if d <= 0:
        return None
    return round(100.0 * n / d, 2)


def _is_synthetic_filter() -> Any:
    """Фильтр SQLAlchemy: «synthetic-узел» — либо ``synthetic=True``,
    либо имя начинается на ``ingress:`` / ``subject:`` / ``db:`` / ``nats:``.

    Дублирует логику ``contract.is_synthetic`` (которая в Python-объекте);
    нам нужен SQL-вариант для агрегатов по миллионам строк.
    """
    name_prefixes = [Service.name.like(f"{prefix}:%") for prefix in SYNTHETIC_KINDS]
    return or_(Service.synthetic.is_(True), *name_prefixes)


def _is_real_filter() -> Any:
    """Обратный к ``_is_synthetic_filter`` — реальный сервис."""
    # NOT (synthetic OR name LIKE 'ingress:%' OR ...)
    real_prefix_filters = [Service.name.notlike(f"{prefix}:%")
                           for prefix in SYNTHETIC_KINDS]
    return and_(
        or_(Service.synthetic.is_(False), Service.synthetic.is_(None)),
        *real_prefix_filters,
    )


# ── data classes ─────────────────────────────────────────────────────────────


@dataclass
class MetricBlock:
    """Универсальный блок метрики: numerator/denominator/pct + extras."""
    name: str
    value: int
    total: Optional[int] = None
    pct: Optional[float] = None
    extras: Dict[str, Any] = field(default_factory=dict)


@dataclass
class QualityReport:
    """Полный quality-snapshot KG.

    Все секции содержат либо счётчики, либо проценты от соответствующих
    denominators. Phase A сравнит секцию-к-секции — diff = improvement.
    """
    generated_at: str

    # 1. Service ownership
    services_total_real: int
    services_total_synthetic: int
    owner_known_count: int
    owner_known_pct: Optional[float]
    owner_sources: Dict[str, int]  # breakdown по OWNER_SOURCES (best-effort)
    owner_sources_note: str

    # 2. Topology coverage
    edges_by_kind: Dict[str, int]
    jobs_total: int
    jobs_linked_to_service: int
    jobs_linked_pct: Optional[float]
    volume_edges_by_kind: Dict[str, int]
    storage_volumes_by_kind: Dict[str, int]
    services_without_http_edges: int
    services_without_nats_edges: int
    # orphan-by-app-topology: real-сервис без ЛЮБОГО meaningful edge
    # (calls/routes_to/uses_db/uses_nats), исключая expected_stale-инфру.
    # Это gate-метрика (см. evaluate_check / issue #2).
    services_orphan_app: int
    services_app_scope_total: int

    # 3. Stale classification
    stale_active: int
    stale_expected: int
    stale_suspicious: int
    stale_null: int  # сервисы где stale_class ещё не проставлен
    top_suspicious_stale: List[Dict[str, Any]]  # name/ns/last_deploy_at

    # 4. Alert enrichment quality (за last 24h)
    alerts_24h_total: int
    alerts_24h_with_service: int
    alerts_24h_with_service_pct: Optional[float]
    alerts_24h_with_owner: int
    alerts_24h_with_owner_pct: Optional[float]
    alerts_24h_with_blast_radius: int
    alerts_24h_with_blast_radius_pct: Optional[float]
    alerts_24h_with_nats_impact: int
    alerts_24h_with_nats_impact_pct: Optional[float]
    alerts_24h_with_pod_trail: int
    alerts_24h_with_pod_trail_pct: Optional[float]

    # 5. Deploy attribution
    deploys_30d_total: int
    deploys_30d_linked_to_service: int
    deploys_30d_linked_pct: Optional[float]
    deploys_30d_with_sha: int
    deploys_30d_with_sha_pct: Optional[float]


# ── builders ─────────────────────────────────────────────────────────────────


#: Bucket для сервисов с team_owner но без явного ``owner_source`` в
#: ``metadata_json``. До PR #97 такие сервисы (большая часть — owner
#: проставлен topology_sync через namespace-prefix эвристику в
#: ``kg_sync._derive_team_owner``) молча отбрасывались из breakdown:
#: numerator = 22, denominator = owner_known_count = 3309 → отчёт показывал
#: "owner_source: namespace_prefix → 21" при том что реально 3287 owners
#: были выведены без какой-либо разметки в metadata_json. Теперь
#: классифицируем их как ``inferred_no_source`` — сумма breakdown
#: совпадает с ``owner_known_count`` (invariant полезен для CI-gate
#: и diff-сравнения между snapshot-ами).
OWNER_SOURCE_UNTRACKED = "inferred_no_source"


def _section_ownership(db: Session) -> Dict[str, Any]:
    """Группа 1. Service ownership + breakdown по owner_source.

    OWNER_SOURCES сейчас в контракте — это семантическая константа
    (см. ``contract.OWNER_SOURCES``). Часть owner-ов материализуется
    как ``metadata_json["owner_source"]`` (через
    ``backfill_ownership._canonical_owner_source``), часть проставляется
    topology_sync без явного source-маркера. Считаем breakdown по обоим
    путям: явный source — точный value, неявные — bucket
    ``inferred_no_source``. Invariant: ``sum(breakdown) == owner_known_count``.

    Python-side aggregation для max-портабельности: PG-native
    ``metadata_json->>'owner_source'`` не работает на SQLite (Column(JSON)
    маппится в TEXT в SQLite, в JSON в PG), и обходить дифф-диалекты
    SQL-выражением сложнее чем загрузить ~3к dict-ов в Python.
    """
    services_real = db.query(func.count(Service.id)).filter(_is_real_filter()).scalar() or 0
    services_synthetic = (
        db.query(func.count(Service.id)).filter(_is_synthetic_filter()).scalar() or 0
    )

    unknown_owner_markers = {"unknown", "n/a", "-", "none"}
    owner_known_q = db.query(func.count(Service.id)).filter(
        _is_real_filter(),
        Service.team_owner.isnot(None),
        Service.team_owner != "",
        func.lower(func.coalesce(Service.team_owner, "")).notin_(
            list(unknown_owner_markers)
        ),
    )
    owner_known_count: int = owner_known_q.scalar() or 0

    # Owner-sources breakdown: проходим по real-сервисам с осмысленным
    # team_owner; если в metadata_json есть owner_source — используем его
    # значение, иначе bucket "inferred_no_source".
    owner_sources: Dict[str, int] = {}
    rows = (
        db.query(Service.team_owner, Service.metadata_json)
        .filter(
            _is_real_filter(),
            Service.team_owner.isnot(None),
            Service.team_owner != "",
        )
        .all()
    )
    for team_owner, md in rows:
        # фильтр "unknown"-маркеров — тот же что и в owner_known_count выше,
        # чтобы сумма breakdown сошлась с owner_known_count.
        if not team_owner or str(team_owner).strip().lower() in unknown_owner_markers:
            continue
        md_dict = _coerce_metadata_dict(md)
        src_raw = md_dict.get("owner_source") if md_dict else None
        if src_raw:
            src = str(src_raw)
        else:
            src = OWNER_SOURCE_UNTRACKED
        owner_sources[src] = owner_sources.get(src, 0) + 1

    explicit_sources = {k: v for k, v in owner_sources.items()
                        if k != OWNER_SOURCE_UNTRACKED}
    if not owner_sources:
        note = (
            "owner_source breakdown пуст — нет сервисов с team_owner. "
            "Заполняется multi-signal owner-inference syncs (см. PR #85)."
        )
    elif not explicit_sources:
        untracked = owner_sources.get(OWNER_SOURCE_UNTRACKED, 0)
        note = (
            f"все {untracked} owners проставлены без явного "
            f"owner_source-маркера в metadata_json — скорее всего topology_sync "
            f"(_derive_team_owner) или legacy backfill. См. backfill_ownership."
        )
    else:
        note = (
            "breakdown по metadata_json.owner_source у сервисов с team_owner; "
            f"bucket {OWNER_SOURCE_UNTRACKED} — owner проставлен без source-маркера "
            f"(typически topology_sync namespace-prefix эвристикой)."
        )

    return {
        "services_total_real": services_real,
        "services_total_synthetic": services_synthetic,
        "owner_known_count": owner_known_count,
        "owner_known_pct": _pct(owner_known_count, services_real),
        "owner_sources": owner_sources,
        "owner_sources_note": note,
    }


def _coerce_metadata_dict(md: Any) -> Optional[Dict[str, Any]]:
    """Привести ``Service.metadata_json`` к dict или None.

    SQLAlchemy ``Column(JSON)`` обычно возвращает уже decoded dict
    (PG через psycopg2 json-typecast; SQLite через JSON-type-affinity).
    Но при некоторых legacy-миграциях столбец мог остаться ``TEXT``
    в SQLite или ``json`` в PG с битым typecast — тогда придёт строка.
    Гарантируем dict-или-None, не молча отбрасывая non-dict.
    """
    if md is None:
        return None
    if isinstance(md, dict):
        return md
    if isinstance(md, (bytes, bytearray)):
        try:
            md = md.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(md, str):
        try:
            parsed = json.loads(md)
        except (json.JSONDecodeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _section_topology(db: Session) -> Dict[str, Any]:
    """Группа 2. Topology coverage — edges, jobs, storage, orphans."""
    edges_by_kind_rows = (
        db.query(ServiceEdge.kind, func.count(ServiceEdge.id))
        .group_by(ServiceEdge.kind)
        .all()
    )
    edges_by_kind: Dict[str, int] = {k: int(c) for k, c in edges_by_kind_rows if k}

    # Jobs sync: kg_k8s_jobs.owner_service_name IS NOT NULL → линкован.
    jobs_total: int = db.query(func.count(K8sJob.id)).scalar() or 0
    jobs_linked: int = (
        db.query(func.count(K8sJob.id))
        .filter(K8sJob.owner_service_name.isnot(None), K8sJob.owner_service_name != "")
        .scalar()
        or 0
    )

    # Volume edges — by kind (uses_volume, bound_to).
    vol_edge_rows = (
        db.query(VolumeEdge.kind, func.count(VolumeEdge.id))
        .group_by(VolumeEdge.kind)
        .all()
    )
    volume_edges_by_kind: Dict[str, int] = {k: int(c) for k, c in vol_edge_rows if k}

    storage_rows = (
        db.query(StorageVolume.kind, func.count(StorageVolume.id))
        .group_by(StorageVolume.kind)
        .all()
    )
    storage_volumes_by_kind: Dict[str, int] = {k: int(c) for k, c in storage_rows if k}

    # Orphan-by-network: real-сервис без HTTP-edge (calls/serves_traffic/routes_to)
    # ни как src, ни как dst.
    http_kinds = ("calls", "serves_traffic", "routes_to")
    http_svc_ids_q = (
        db.query(ServiceEdge.src_id).filter(ServiceEdge.kind.in_(http_kinds)).union(
            db.query(ServiceEdge.dst_id).filter(ServiceEdge.kind.in_(http_kinds))
        )
    )
    http_svc_ids = {r[0] for r in http_svc_ids_q.all() if r[0] is not None}

    real_services = (
        db.query(Service.id).filter(_is_real_filter()).all()
    )
    real_ids = {r[0] for r in real_services}
    no_http = len(real_ids - http_svc_ids)

    nats_kinds = ("uses_nats",)
    nats_svc_ids_q = (
        db.query(ServiceEdge.src_id).filter(ServiceEdge.kind.in_(nats_kinds)).union(
            db.query(ServiceEdge.dst_id).filter(ServiceEdge.kind.in_(nats_kinds))
        )
    )
    nats_svc_ids = {r[0] for r in nats_svc_ids_q.all() if r[0] is not None}
    no_nats = len(real_ids - nats_svc_ids)

    # Orphan-by-app-topology (gate-метрика, см. issue #2):
    # сервис считается connected, если у него есть ЛЮБОЙ meaningful edge
    # (calls/routes_to/uses_db/uses_nats) как src ИЛИ dst. WO-сервисы общаются
    # в основном через NATS/Orleans и БД, а не HTTP REST, поэтому учитывать
    # только HTTP-kinds = ложные orphan-ы. Дополнительно из знаменателя
    # исключаем expected_stale (инфра: DB/headless/system — безрёберны by design),
    # оставляя active + suspicious_stale (реальные app-сервисы).
    meaningful_kinds = ("calls", "routes_to", "uses_db", "uses_nats")
    connected_ids_q = (
        db.query(ServiceEdge.src_id).filter(ServiceEdge.kind.in_(meaningful_kinds)).union(
            db.query(ServiceEdge.dst_id).filter(ServiceEdge.kind.in_(meaningful_kinds))
        )
    )
    connected_ids = {r[0] for r in connected_ids_q.all() if r[0] is not None}

    app_scope_rows = (
        db.query(Service.id)
        .filter(
            _is_real_filter(),
            or_(
                Service.stale_class != STALE_CLASS_EXPECTED,
                Service.stale_class.is_(None),
            ),
        )
        .all()
    )
    app_scope_ids = {r[0] for r in app_scope_rows}
    orphan_app = len(app_scope_ids - connected_ids)

    return {
        "edges_by_kind": edges_by_kind,
        "jobs_total": jobs_total,
        "jobs_linked_to_service": jobs_linked,
        "jobs_linked_pct": _pct(jobs_linked, jobs_total),
        "volume_edges_by_kind": volume_edges_by_kind,
        "storage_volumes_by_kind": storage_volumes_by_kind,
        "services_without_http_edges": no_http,
        "services_without_nats_edges": no_nats,
        "services_orphan_app": orphan_app,
        "services_app_scope_total": len(app_scope_ids),
    }


def _section_stale(db: Session, top_n: int = 10) -> Dict[str, Any]:
    """Группа 3. Stale classification (после PR #86)."""

    def _count(value: Optional[str]) -> int:
        q = db.query(func.count(Service.id)).filter(_is_real_filter())
        if value is None:
            q = q.filter(Service.stale_class.is_(None))
        else:
            q = q.filter(Service.stale_class == value)
        return int(q.scalar() or 0)

    stale_active = _count(STALE_CLASS_ACTIVE)
    stale_expected = _count(STALE_CLASS_EXPECTED)
    stale_suspicious = _count(STALE_CLASS_SUSPICIOUS)
    stale_null = _count(None)

    # Top suspicious — добавляем last_deploy_at через LEFT JOIN
    # на максимум started_at в Deployment. Делается в подзапросе.
    last_deploy_sq = (
        db.query(
            Deployment.service_id.label("sid"),
            func.max(Deployment.started_at).label("last_at"),
        )
        .group_by(Deployment.service_id)
        .subquery()
    )

    top_rows = (
        db.query(Service.name, Service.namespace, Service.team_owner, last_deploy_sq.c.last_at)
        .outerjoin(last_deploy_sq, last_deploy_sq.c.sid == Service.id)
        .filter(_is_real_filter(), Service.stale_class == STALE_CLASS_SUSPICIOUS)
        .order_by(last_deploy_sq.c.last_at.is_(None).desc(), last_deploy_sq.c.last_at.asc())
        .limit(top_n)
        .all()
    )
    top_suspicious_stale: List[Dict[str, Any]] = [
        {
            "name": name,
            "namespace": ns,
            "team_owner": owner,
            "last_deploy_at": last_at.isoformat() if last_at else None,
        }
        for (name, ns, owner, last_at) in top_rows
    ]

    return {
        "stale_active": stale_active,
        "stale_expected": stale_expected,
        "stale_suspicious": stale_suspicious,
        "stale_null": stale_null,
        "top_suspicious_stale": top_suspicious_stale,
    }


def _section_alert_enrichment(
    db: Session, now: Optional[datetime] = None
) -> Dict[str, Any]:
    """Группа 4. Alert enrichment quality за last 24h.

    Замечание: kg_alerts не хранит ``hypothesis_text`` или ``likely_cause``
    как первоклассные поля — enrichment строится по запросу в
    ``alert_enrichment.py``. Для baseline мы считаем «потенциал
    enrichment-а»: какой fraction алёртов имеет необходимую структуру
    в KG чтобы соответствующая секция в embed появилась.

    Метрики:
      * with_service — alert привязан к kg_service (FK).
      * with_owner — service имеет team_owner.
      * with_blast_radius — service имеет ≥1 IN-edge типа
        ``serves_traffic`` или ``routes_to``.
      * with_nats_impact — service имеет ≥1 ``uses_nats`` edge.
      * with_pod_trail — есть ≥1 ``kg_pod_events`` за окно
        ±60м от fired_at для того же сервиса.
    """
    now_dt = now or datetime.utcnow()
    since = now_dt - timedelta(days=1)

    alerts_24h_total: int = (
        db.query(func.count(AlertEvent.id))
        .filter(AlertEvent.fired_at >= since)
        .scalar()
        or 0
    )

    if alerts_24h_total == 0:
        return {
            "alerts_24h_total": 0,
            "alerts_24h_with_service": 0,
            "alerts_24h_with_service_pct": None,
            "alerts_24h_with_owner": 0,
            "alerts_24h_with_owner_pct": None,
            "alerts_24h_with_blast_radius": 0,
            "alerts_24h_with_blast_radius_pct": None,
            "alerts_24h_with_nats_impact": 0,
            "alerts_24h_with_nats_impact_pct": None,
            "alerts_24h_with_pod_trail": 0,
            "alerts_24h_with_pod_trail_pct": None,
        }

    # Сервис-аттрибуция: FK к kg_services.
    with_service: int = (
        db.query(func.count(AlertEvent.id))
        .filter(AlertEvent.fired_at >= since, AlertEvent.service_id.isnot(None))
        .scalar()
        or 0
    )

    # Owner: JOIN на Service + owner_known. Делаем в Python чтобы не плодить
    # дубль фильтра _is_real_filter().
    alerts_with_owner_q = (
        db.query(AlertEvent.id, Service.team_owner)
        .join(Service, Service.id == AlertEvent.service_id)
        .filter(AlertEvent.fired_at >= since)
    )
    with_owner = sum(
        1
        for _, owner in alerts_with_owner_q.all()
        if owner and owner.strip().lower() not in {"unknown", "n/a", "-", "none"}
    )

    # Blast radius: service_id есть И существует IN-edge с serves_traffic/routes_to.
    blast_kinds = ("serves_traffic", "routes_to")
    blast_dst_ids_q = (
        db.query(ServiceEdge.dst_id)
        .filter(ServiceEdge.kind.in_(blast_kinds))
        .distinct()
    )
    blast_ids = {r[0] for r in blast_dst_ids_q.all() if r[0] is not None}
    if blast_ids:
        with_blast: int = (
            db.query(func.count(AlertEvent.id))
            .filter(
                AlertEvent.fired_at >= since,
                AlertEvent.service_id.in_(blast_ids),
            )
            .scalar()
            or 0
        )
    else:
        with_blast = 0

    # NATS impact: uses_nats edge как src.
    nats_src_ids_q = (
        db.query(ServiceEdge.src_id)
        .filter(ServiceEdge.kind == "uses_nats")
        .distinct()
    )
    nats_ids = {r[0] for r in nats_src_ids_q.all() if r[0] is not None}
    if nats_ids:
        with_nats: int = (
            db.query(func.count(AlertEvent.id))
            .filter(
                AlertEvent.fired_at >= since,
                AlertEvent.service_id.in_(nats_ids),
            )
            .scalar()
            or 0
        )
    else:
        with_nats = 0

    # Pod trail: считаем сколько алёртов имеет ≥1 PodEvent для того же
    # сервиса в окне ±60мин. Делаем агрегацию через correlated subquery:
    # выгоднее одним запросом GROUP BY alert + EXISTS-subquery.
    pod_trail_window = timedelta(minutes=60)
    # Если pod_events таблица пустая — короткое замыкание.
    pod_events_total: int = (
        db.query(func.count(PodEvent.id)).filter(PodEvent.first_seen >= since - pod_trail_window).scalar()
        or 0
    )
    with_pod_trail = 0
    if pod_events_total > 0:
        # Загружаем alerts с service_id за окно, для каждого считаем
        # EXISTS pod_event for that service within ±60м.
        alert_rows = (
            db.query(AlertEvent.id, AlertEvent.service_id, AlertEvent.fired_at)
            .filter(AlertEvent.fired_at >= since, AlertEvent.service_id.isnot(None))
            .all()
        )
        # Группируем pod-events по service_id единожды.
        from collections import defaultdict

        pe_by_svc: Dict[int, List[datetime]] = defaultdict(list)
        pe_rows = (
            db.query(PodEvent.service_id, PodEvent.first_seen)
            .filter(
                PodEvent.service_id.isnot(None),
                PodEvent.first_seen >= since - pod_trail_window,
            )
            .all()
        )
        for sid, fs in pe_rows:
            if sid is not None and fs is not None:
                pe_by_svc[int(sid)].append(fs)

        for _, sid, fired_at in alert_rows:
            if sid is None:
                continue
            evs = pe_by_svc.get(int(sid))
            if not evs:
                continue
            lo = fired_at - pod_trail_window
            hi = fired_at + pod_trail_window
            if any(lo <= ev <= hi for ev in evs):
                with_pod_trail += 1

    return {
        "alerts_24h_total": alerts_24h_total,
        "alerts_24h_with_service": with_service,
        "alerts_24h_with_service_pct": _pct(with_service, alerts_24h_total),
        "alerts_24h_with_owner": with_owner,
        "alerts_24h_with_owner_pct": _pct(with_owner, alerts_24h_total),
        "alerts_24h_with_blast_radius": with_blast,
        "alerts_24h_with_blast_radius_pct": _pct(with_blast, alerts_24h_total),
        "alerts_24h_with_nats_impact": with_nats,
        "alerts_24h_with_nats_impact_pct": _pct(with_nats, alerts_24h_total),
        "alerts_24h_with_pod_trail": with_pod_trail,
        "alerts_24h_with_pod_trail_pct": _pct(with_pod_trail, alerts_24h_total),
    }


def _section_deploys(db: Session, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Группа 5. Deploy attribution (last 30d)."""
    now_dt = now or datetime.utcnow()
    since = now_dt - timedelta(days=30)

    total: int = (
        db.query(func.count(Deployment.id))
        .filter(Deployment.started_at >= since)
        .scalar()
        or 0
    )
    linked: int = (
        db.query(func.count(Deployment.id))
        .filter(Deployment.started_at >= since, Deployment.service_id.isnot(None))
        .scalar()
        or 0
    )
    with_sha: int = (
        db.query(func.count(Deployment.id))
        .filter(
            Deployment.started_at >= since,
            Deployment.sha.isnot(None),
            Deployment.sha != "",
        )
        .scalar()
        or 0
    )
    return {
        "deploys_30d_total": total,
        "deploys_30d_linked_to_service": linked,
        "deploys_30d_linked_pct": _pct(linked, total),
        "deploys_30d_with_sha": with_sha,
        "deploys_30d_with_sha_pct": _pct(with_sha, total),
    }


def build_report(db: Session, now: Optional[datetime] = None) -> QualityReport:
    """Главный entry-point: собирает все 5 секций в один dataclass.

    Делает только SELECT-ы. Принимает ``now`` для детерминизма в тестах
    (alerts/deploys-окна).
    """
    now_dt = now or datetime.utcnow()
    sec1 = _section_ownership(db)
    sec2 = _section_topology(db)
    sec3 = _section_stale(db)
    sec4 = _section_alert_enrichment(db, now=now_dt)
    sec5 = _section_deploys(db, now=now_dt)

    return QualityReport(
        generated_at=now_dt.isoformat(),
        **sec1,
        **sec2,
        **sec3,
        **sec4,
        **sec5,
    )


# ── renderers ────────────────────────────────────────────────────────────────


def _fmt_pct(p: Optional[float]) -> str:
    return "n/a" if p is None else f"{p:.2f}%"


def render_markdown(report: QualityReport) -> str:
    """Markdown render — структурный baseline-документ.

    Все denominators явно прописаны (`X/Y = Z%`), чтобы Phase A
    мог по diff'у на cell-уровне сказать «было 17.2% → стало 24.8%».
    """
    r = report

    def _table_block(rows: List[Tuple[str, str]]) -> str:
        out = ["| Метрика | Значение |", "|---|---|"]
        for label, val in rows:
            out.append(f"| {label} | {val} |")
        return "\n".join(out)

    def _owner_sources_block() -> str:
        if not r.owner_sources:
            return f"_(пусто: {r.owner_sources_note})_"
        rows = sorted(r.owner_sources.items(), key=lambda kv: -kv[1])
        out = ["| owner_source | count |", "|---|---|"]
        for k, v in rows:
            out.append(f"| {k} | {v} |")
        # Note всегда печатаем, чтобы было видно семантику bucket-а
        # `inferred_no_source` (главный регрессионный кейс).
        out.append("")
        out.append(f"> {r.owner_sources_note}")
        return "\n".join(out)

    def _edges_block() -> str:
        if not r.edges_by_kind:
            return "_(нет edges)_"
        rows = sorted(r.edges_by_kind.items(), key=lambda kv: -kv[1])
        out = ["| kind | count |", "|---|---|"]
        for k, v in rows:
            out.append(f"| {k} | {v} |")
        return "\n".join(out)

    def _volume_edges_block() -> str:
        if not r.volume_edges_by_kind:
            return "_(пусто — k8s_storage_sync ещё не отработал)_"
        rows = sorted(r.volume_edges_by_kind.items(), key=lambda kv: -kv[1])
        out = ["| kind | count |", "|---|---|"]
        for k, v in rows:
            out.append(f"| {k} | {v} |")
        return "\n".join(out)

    def _storage_block() -> str:
        if not r.storage_volumes_by_kind:
            return "_(пусто — k8s_storage_sync ещё не отработал)_"
        rows = sorted(r.storage_volumes_by_kind.items(), key=lambda kv: -kv[1])
        out = ["| kind | count |", "|---|---|"]
        for k, v in rows:
            out.append(f"| {k} | {v} |")
        return "\n".join(out)

    def _stale_top_block() -> str:
        if not r.top_suspicious_stale:
            return "_(нет suspicious_stale)_"
        out = ["| namespace | name | team_owner | last_deploy_at |",
               "|---|---|---|---|"]
        for row in r.top_suspicious_stale:
            out.append(
                f"| {row['namespace']} | {row['name']} | "
                f"{row.get('team_owner') or '-'} | {row.get('last_deploy_at') or 'никогда'} |"
            )
        return "\n".join(out)

    services_total = r.services_total_real + r.services_total_synthetic

    return f"""# KG Quality Report — baseline

Generated: `{r.generated_at}`

Snapshot для Phase A (remediation). Все denominators прописаны явно — Phase A
сравнивает cell-к-cell. Скрипт: `app/scripts/quality_report.py` (read-only).

---

## 1. Service ownership

{_table_block([
    ("Total services (real)", str(r.services_total_real)),
    ("Total services (synthetic)", str(r.services_total_synthetic)),
    ("Total services (всего)", str(services_total)),
    ("owner_known", f"{r.owner_known_count}/{r.services_total_real} = {_fmt_pct(r.owner_known_pct)}"),
])}

### Owner sources breakdown

{_owner_sources_block()}

---

## 2. Topology coverage

### Edges (kg_service_edges) by kind

{_edges_block()}

### Jobs sync linkage (#82)

{_table_block([
    ("kg_k8s_jobs total", str(r.jobs_total)),
    ("jobs linked to service", f"{r.jobs_linked_to_service}/{r.jobs_total} = {_fmt_pct(r.jobs_linked_pct)}"),
])}

### Volume edges (kg_volume_edges, #84)

{_volume_edges_block()}

### Storage volumes (kg_storage_volumes, #84)

{_storage_block()}

### Orphans (среди real-сервисов)

{_table_block([
    ("app-orphan (без meaningful edge, excl expected_stale) — gate-метрика",
     f"{r.services_orphan_app}/{r.services_app_scope_total}"),
    ("без HTTP-edges (calls/serves_traffic/routes_to) — диагностика",
     str(r.services_without_http_edges)),
    ("без NATS-edges (uses_nats) — диагностика",
     str(r.services_without_nats_edges)),
])}

---

## 3. Stale classification (#86)

{_table_block([
    ("active", str(r.stale_active)),
    ("expected_stale", str(r.stale_expected)),
    ("suspicious_stale", str(r.stale_suspicious)),
    ("stale_class IS NULL (не пересчитано)", str(r.stale_null)),
])}

### Top-10 suspicious_stale

{_stale_top_block()}

---

## 4. Alert enrichment quality (last 24h)

{_table_block([
    ("Total alerts (24h)", str(r.alerts_24h_total)),
    ("with linked service", f"{r.alerts_24h_with_service}/{r.alerts_24h_total} = {_fmt_pct(r.alerts_24h_with_service_pct)}"),
    ("with owner", f"{r.alerts_24h_with_owner}/{r.alerts_24h_total} = {_fmt_pct(r.alerts_24h_with_owner_pct)}"),
    ("with blast_radius (serves_traffic/routes_to IN-edges)",
     f"{r.alerts_24h_with_blast_radius}/{r.alerts_24h_total} = {_fmt_pct(r.alerts_24h_with_blast_radius_pct)}"),
    ("with NATS impact (uses_nats edges)",
     f"{r.alerts_24h_with_nats_impact}/{r.alerts_24h_total} = {_fmt_pct(r.alerts_24h_with_nats_impact_pct)}"),
    ("with pod_trail (kg_pod_events ±60м)",
     f"{r.alerts_24h_with_pod_trail}/{r.alerts_24h_total} = {_fmt_pct(r.alerts_24h_with_pod_trail_pct)}"),
])}

> Замечание: ``kg_alerts`` не хранит ``hypothesis_text`` — enrichment строится
> по запросу. Метрики выше — *потенциал* (структура KG позволяет построить
> секцию embed-а), не actually-rendered count.

---

## 5. Deploy attribution (last 30d)

{_table_block([
    ("Total deploys (30d)", str(r.deploys_30d_total)),
    ("linked to service",
     f"{r.deploys_30d_linked_to_service}/{r.deploys_30d_total} = {_fmt_pct(r.deploys_30d_linked_pct)}"),
    ("with commit_sha",
     f"{r.deploys_30d_with_sha}/{r.deploys_30d_total} = {_fmt_pct(r.deploys_30d_with_sha_pct)}"),
])}
"""


def render_json(report: QualityReport) -> str:
    return json.dumps(asdict(report), indent=2, ensure_ascii=False, sort_keys=True)


# ── --check mode ─────────────────────────────────────────────────────────────


@dataclass
class CheckThresholds:
    """Пороги для CI gate (warning, не blocker).

    Все 4 проверки можно отключить, выставив порог в "сейф" значение
    (`max_orphan=1.0`, `min_owner=0.0`, `max_stale_null=1.0`, оставляя
    unknown_edge_kinds всегда строгим — это всегда drift).
    """
    max_orphan_pct: float = DEFAULT_MAX_ORPHAN_PCT
    min_owner_pct: float = DEFAULT_MIN_OWNER_PCT
    max_stale_null_pct: float = DEFAULT_MAX_STALE_NULL_PCT


@dataclass
class CheckResult:
    """Результат CI-gate проверки.

    `failures` — список tuple `(axis, actual_value, threshold, detail)`.
    `passed` — True если все 4 axes в пределах thresholds.
    """
    passed: bool
    failures: List[Dict[str, Any]] = field(default_factory=list)
    unknown_edge_kinds: List[str] = field(default_factory=list)
    orphan_pct: Optional[float] = None
    owner_pct: Optional[float] = None
    stale_null_pct: Optional[float] = None
    thresholds: Dict[str, float] = field(default_factory=dict)


def _query_unknown_edge_kinds(db: Session) -> List[str]:
    """Edge kinds в БД, которых нет в `contract.EDGE_KINDS`."""
    try:
        rows = db.execute(
            text("SELECT DISTINCT kind FROM kg_service_edges")
        ).fetchall()
    except Exception as exc:
        log.warning("quality_report.unknown_edge_kinds_query_failed",
                    extra={"error": str(exc)})
        return []
    db_kinds = {r[0] for r in rows if r and r[0]}
    known = set(EDGE_KINDS.keys())
    return sorted(db_kinds - known)


def evaluate_check(
    report: QualityReport,
    db: Session,
    thresholds: CheckThresholds,
) -> CheckResult:
    """Сравнивает quality-снимок с порогами и возвращает CheckResult.

    Что валидирует:
      * `unknown_edge_kinds` (запрос к kg_service_edges) — len > 0 → fail.
      * `orphan_pct` (app-сервисы без ЛЮБОГО meaningful edge
        calls/routes_to/uses_db/uses_nats, исключая expected_stale-инфру)
        > max_orphan → fail. Это app-topology completeness, а не HTTP-only:
        WO общается через NATS/Orleans/БД, а не REST (issue #2).
      * `owner_pct` (owner_known среди real) < min_owner → fail.
      * `stale_null_pct` (stale_class IS NULL среди real) > max_stale_null → fail.
    """
    unknown = _query_unknown_edge_kinds(db)
    failures: List[Dict[str, Any]] = []

    if unknown:
        failures.append({
            "axis": "unknown_edge_kinds",
            "actual": unknown,
            "threshold": 0,
            "detail": f"{len(unknown)} edge kind(s) в БД отсутствуют в contract.EDGE_KINDS",
        })

    # orphan_pct: app-topology completeness — сервисы без ЛЮБОГО meaningful
    # edge (calls/routes_to/uses_db/uses_nats), знаменатель без expected_stale
    # инфры (см. _section_topology + issue #2). _pct возвращает None если
    # denom = 0 (пустая БД — gate тихо проходит).
    orphan_pct = _pct(report.services_orphan_app, report.services_app_scope_total)
    if orphan_pct is not None and orphan_pct > thresholds.max_orphan_pct:
        failures.append({
            "axis": "orphan_pct",
            "actual": orphan_pct,
            "threshold": thresholds.max_orphan_pct,
            "detail": f"{report.services_orphan_app}/{report.services_app_scope_total} = {orphan_pct}% > {thresholds.max_orphan_pct}% (app без meaningful edge, excl expected_stale)",
        })

    owner_pct = report.owner_known_pct
    if owner_pct is not None and owner_pct < thresholds.min_owner_pct:
        failures.append({
            "axis": "owner_pct",
            "actual": owner_pct,
            "threshold": thresholds.min_owner_pct,
            "detail": f"{report.owner_known_count}/{report.services_total_real} = {owner_pct}% < {thresholds.min_owner_pct}%",
        })

    stale_real_total = (
        report.stale_active + report.stale_expected
        + report.stale_suspicious + report.stale_null
    )
    stale_null_pct = _pct(report.stale_null, stale_real_total)
    if stale_null_pct is not None and stale_null_pct > thresholds.max_stale_null_pct:
        failures.append({
            "axis": "stale_null_pct",
            "actual": stale_null_pct,
            "threshold": thresholds.max_stale_null_pct,
            "detail": f"{report.stale_null}/{stale_real_total} = {stale_null_pct}% > {thresholds.max_stale_null_pct}% (backfill отстал)",
        })

    return CheckResult(
        passed=len(failures) == 0,
        failures=failures,
        unknown_edge_kinds=unknown,
        orphan_pct=orphan_pct,
        owner_pct=owner_pct,
        stale_null_pct=stale_null_pct,
        thresholds={
            "max_orphan_pct": thresholds.max_orphan_pct,
            "min_owner_pct": thresholds.min_owner_pct,
            "max_stale_null_pct": thresholds.max_stale_null_pct,
        },
    )


def render_check_markdown(result: CheckResult) -> str:
    """Markdown-таблица для CI-вывода. Показывает все 4 axes + статус."""
    status = "PASS" if result.passed else "FAIL"
    lines = [
        f"# KG Contract Check — {status}",
        "",
        "| axis | actual | threshold | status |",
        "|---|---|---|---|",
    ]

    def _row(axis: str, actual: Any, threshold: Any, ok: bool) -> str:
        mark = "OK" if ok else "FAIL"
        return f"| {axis} | {actual} | {threshold} | {mark} |"

    unknown_ok = len(result.unknown_edge_kinds) == 0
    lines.append(_row(
        "unknown_edge_kinds",
        ", ".join(result.unknown_edge_kinds) or "(none)",
        "0",
        unknown_ok,
    ))

    orphan_thr = result.thresholds.get("max_orphan_pct", DEFAULT_MAX_ORPHAN_PCT)
    if result.orphan_pct is None:
        lines.append(_row("orphan_pct", "n/a (empty DB)", f"≤{orphan_thr}%", True))
    else:
        ok = result.orphan_pct <= orphan_thr
        lines.append(_row("orphan_pct", f"{result.orphan_pct}%", f"≤{orphan_thr}%", ok))

    owner_thr = result.thresholds.get("min_owner_pct", DEFAULT_MIN_OWNER_PCT)
    if result.owner_pct is None:
        lines.append(_row("owner_pct", "n/a", f"≥{owner_thr}%", True))
    else:
        ok = result.owner_pct >= owner_thr
        lines.append(_row("owner_pct", f"{result.owner_pct}%", f"≥{owner_thr}%", ok))

    stale_thr = result.thresholds.get("max_stale_null_pct", DEFAULT_MAX_STALE_NULL_PCT)
    if result.stale_null_pct is None:
        lines.append(_row("stale_null_pct", "n/a", f"≤{stale_thr}%", True))
    else:
        ok = result.stale_null_pct <= stale_thr
        lines.append(_row("stale_null_pct", f"{result.stale_null_pct}%", f"≤{stale_thr}%", ok))

    if result.failures:
        lines.append("")
        lines.append("## Failures")
        for f in result.failures:
            lines.append(f"- **{f['axis']}**: {f['detail']}")

    return "\n".join(lines) + "\n"


def render_check_json(result: CheckResult, report: QualityReport) -> str:
    """JSON-вывод для CI. Включает `check_passed`, `failures` плюс полный
    quality-snapshot, чтобы можно было пайпить в `jq` и одним проходом
    забрать обе вещи.
    """
    payload = asdict(report)
    payload["check_passed"] = result.passed
    payload["check_failures"] = result.failures
    payload["check_thresholds"] = result.thresholds
    payload["check_unknown_edge_kinds"] = result.unknown_edge_kinds
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)


def _env_float(name: str, default: float) -> float:
    """ENV-override для thresholds. Невалидное значение → default + warning."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("quality_report.invalid_env_threshold",
                    extra={"name": name, "value": raw})
        return default


# ── CLI ──────────────────────────────────────────────────────────────────────


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    fmt = parser.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true", help="JSON-вывод")
    fmt.add_argument("--markdown", action="store_true", help="Markdown-вывод (default)")
    parser.add_argument("--output", "-o", help="Файл-вывод (default: stdout)")

    # --check mode: CI gate. Exit 1 при провале одного из axes.
    parser.add_argument(
        "--check", action="store_true",
        help="CI gate mode: проверить thresholds, exit 1 при failure.",
    )
    parser.add_argument(
        "--max-orphan", type=float,
        default=_env_float("KG_CHECK_MAX_ORPHAN_PCT", DEFAULT_MAX_ORPHAN_PCT),
        help=f"Max orphan pct (default: {DEFAULT_MAX_ORPHAN_PCT}, ENV: KG_CHECK_MAX_ORPHAN_PCT)",
    )
    parser.add_argument(
        "--min-owner", type=float,
        default=_env_float("KG_CHECK_MIN_OWNER_PCT", DEFAULT_MIN_OWNER_PCT),
        help=f"Min owner pct (default: {DEFAULT_MIN_OWNER_PCT}, ENV: KG_CHECK_MIN_OWNER_PCT)",
    )
    parser.add_argument(
        "--max-stale-null", type=float,
        default=_env_float("KG_CHECK_MAX_STALE_NULL_PCT", DEFAULT_MAX_STALE_NULL_PCT),
        help=f"Max stale_null pct (default: {DEFAULT_MAX_STALE_NULL_PCT}, ENV: KG_CHECK_MAX_STALE_NULL_PCT)",
    )

    args = parser.parse_args(argv)

    # SessionLocal импортируется лениво — без него тесты могут импортировать
    # модуль на in-memory SQLite-сессии (см. tests/test_quality_report.py).
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        report = build_report(db)
        check_result: Optional[CheckResult] = None
        if args.check:
            thresholds = CheckThresholds(
                max_orphan_pct=args.max_orphan,
                min_owner_pct=args.min_owner,
                max_stale_null_pct=args.max_stale_null,
            )
            check_result = evaluate_check(report, db, thresholds)
    finally:
        db.close()

    if args.check:
        assert check_result is not None
        if args.json:
            out = render_check_json(check_result, report)
        else:
            out = render_check_markdown(check_result)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(out)
            log.info("quality_report.check_written", extra={"path": args.output})
        else:
            sys.stdout.write(out)
            if not out.endswith("\n"):
                sys.stdout.write("\n")
        return 0 if check_result.passed else 1

    if args.json:
        out = render_json(report)
    else:
        out = render_markdown(report)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(out)
        log.info("quality_report.written", extra={"path": args.output})
    else:
        sys.stdout.write(out)
        if not out.endswith("\n"):
            sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
