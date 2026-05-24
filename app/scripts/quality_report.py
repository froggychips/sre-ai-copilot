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
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.knowledge_graph.contract import SYNTHETIC_KINDS
from app.knowledge_graph.schema import (AlertEvent, Deployment, K8sJob,
                                        PodEvent, Service, ServiceEdge,
                                        StorageVolume, VolumeEdge)
from app.knowledge_graph.stale_classifier import (STALE_CLASS_ACTIVE,
                                                  STALE_CLASS_EXPECTED,
                                                  STALE_CLASS_SUSPICIOUS)

log = logging.getLogger(__name__)


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


def _section_ownership(db: Session) -> Dict[str, Any]:
    """Группа 1. Service ownership + breakdown по owner_source.

    OWNER_SOURCES сейчас в контракте — это семантическая константа
    (см. ``contract.OWNER_SOURCES``). В БД поле ``owner_source`` ещё не
    материализовано (он живёт как metadata-key в ``Service.metadata_json``
    после wave-ов owner inference). Делаем best-effort: разбираем
    JSON-поле и считаем сколько строк имеют каждое значение.
    """
    services_real = db.query(func.count(Service.id)).filter(_is_real_filter()).scalar() or 0
    services_synthetic = (
        db.query(func.count(Service.id)).filter(_is_synthetic_filter()).scalar() or 0
    )

    owner_known_q = db.query(func.count(Service.id)).filter(
        _is_real_filter(),
        Service.team_owner.isnot(None),
        Service.team_owner != "",
        func.lower(func.coalesce(Service.team_owner, "")).notin_(
            ["unknown", "n/a", "-", "none"]
        ),
    )
    owner_known_count: int = owner_known_q.scalar() or 0

    # Owner-sources breakdown: вытаскиваем metadata_json у real сервисов с
    # owner, считаем по полю owner_source. Python-side aggregation — JSON-ops
    # переносимо не для всех бэкендов (PG/SQLite разные).
    owner_sources: Dict[str, int] = {}
    rows = (
        db.query(Service.metadata_json)
        .filter(
            _is_real_filter(),
            Service.team_owner.isnot(None),
            Service.team_owner != "",
        )
        .all()
    )
    for (md,) in rows:
        if not isinstance(md, dict):
            continue
        src = md.get("owner_source")
        if not src:
            continue
        owner_sources[str(src)] = owner_sources.get(str(src), 0) + 1

    note = (
        "owner_source breakdown пуст — поле ещё не материализовано в metadata_json. "
        "Заполняется multi-signal owner-inference syncs (planned post-#85)."
        if not owner_sources
        else "breakdown по metadata_json.owner_source у сервисов с team_owner."
    )

    return {
        "services_total_real": services_real,
        "services_total_synthetic": services_synthetic,
        "owner_known_count": owner_known_count,
        "owner_known_pct": _pct(owner_known_count, services_real),
        "owner_sources": owner_sources,
        "owner_sources_note": note,
    }


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

    return {
        "edges_by_kind": edges_by_kind,
        "jobs_total": jobs_total,
        "jobs_linked_to_service": jobs_linked,
        "jobs_linked_pct": _pct(jobs_linked, jobs_total),
        "volume_edges_by_kind": volume_edges_by_kind,
        "storage_volumes_by_kind": storage_volumes_by_kind,
        "services_without_http_edges": no_http,
        "services_without_nats_edges": no_nats,
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
    ("без HTTP-edges (calls/serves_traffic/routes_to)", str(r.services_without_http_edges)),
    ("без NATS-edges (uses_nats)", str(r.services_without_nats_edges)),
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


# ── CLI ──────────────────────────────────────────────────────────────────────


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    fmt = parser.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true", help="JSON-вывод")
    fmt.add_argument("--markdown", action="store_true", help="Markdown-вывод (default)")
    parser.add_argument("--output", "-o", help="Файл-вывод (default: stdout)")
    args = parser.parse_args(argv)

    # SessionLocal импортируется лениво — без него тесты могут импортировать
    # модуль на in-memory SQLite-сессии (см. tests/test_quality_report.py).
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        report = build_report(db)
    finally:
        db.close()

    if args.json:
        text = render_json(report)
    else:
        text = render_markdown(report)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
        log.info("quality_report.written", extra={"path": args.output})
    else:
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
