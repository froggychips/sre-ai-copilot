"""Timeline инцидента: одна лента событий сервиса вокруг инцидента.

Главный объект расследования. Цепочка

    деплой → смена шаблона → пересоздание подов → аномалия → ошибки в логах
    → алерт → резолв

не вычисляется — она ПРОЯВЛЯЕТСЯ, когда события пяти таблиц графа положить
на одну ось времени. Каждое событие несёт Evidence-метки: `epistemic`
(наблюдали / вывели) и `provenance` (из какой таблицы), чтобы потребитель —
человек в Discord, будущий детерминированный RCA, LLM в конце цепочки —
видел, чему здесь можно верить.

Known Unknowns — часть ответа, а не его отсутствие. Если у инцидента нет
`service_id` (сервис не нашёлся в графе), деплои, события подов, аномалии и
логи опросить негде: в `unknowns` это сказано явно, вместо пустой ленты,
которая читалась бы как «ничего не происходило».
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, cast

from sqlalchemy.orm import Session

from app.core.timeutil import ensure_naive
from app.knowledge_graph.epistemic import Epistemic
from app.knowledge_graph.incidents import incident_to_dict
from app.knowledge_graph.queries import deploy_attribution_scope
from app.knowledge_graph.schema import (AlertEvent, AnomalyObservation,
                                        Deployment, KGIncident, LogObservation,
                                        PodEvent)

#: Сколько смотреть ДО первого алерта: деплой, который его вызвал, обычно
#: в пределах часа (RecentDeployRule живёт тем же окном).
LOOKBACK_MIN = 60
#: Сколько смотреть ПОСЛЕ резолва: последствия и повторные события.
LOOKAHEAD_MIN = 30

#: Порядок событий с одинаковым ts: причина раньше следствия.
_KIND_ORDER = {
    "incident.opened": 0, "deploy": 1, "pod_event": 2, "anomaly": 3,
    "log_errors": 4, "alert.fired": 5, "alert.resolved": 6, "incident.resolved": 7,
}

_LOG_LEVELS_OF_INTEREST = ("error", "fatal", "critical")


def _ev(ts: Any, kind: str, title: str, *, epistemic: Epistemic,
        provenance: str, details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "ts": ts,
        "kind": kind,
        "title": title,
        "details": details or {},
        "evidence": {"epistemic": epistemic.value, "provenance": provenance},
    }


def build_timeline(
    db: Session, incident: KGIncident, *, now: Optional[datetime] = None,
) -> Dict[str, Any]:
    now = ensure_naive(now or datetime.utcnow())
    opened_at = cast(datetime, incident.opened_at)
    resolved_at = cast(Optional[datetime], incident.resolved_at)
    start = opened_at - timedelta(minutes=LOOKBACK_MIN)
    end_anchor = resolved_at or now
    end = min(end_anchor + timedelta(minutes=LOOKAHEAD_MIN), now)

    events: List[Dict[str, Any]] = []
    unknowns: List[Dict[str, str]] = []

    events.append(_ev(
        incident.opened_at, "incident.opened",
        f"Инцидент открыт: {incident.service_name} ({incident.namespace})",
        epistemic=Epistemic.OBSERVED, provenance="kg_incidents",
        details={"severity": incident.severity, "incident_key": incident.incident_key},
    ))

    # ── алерты инцидента ─────────────────────────────────────────────────
    fps = list(incident.fingerprints or [])
    alerts: List[AlertEvent] = []
    if fps:
        alerts = (
            db.query(AlertEvent)
            .filter(AlertEvent.fingerprint.in_(fps))
            .order_by(AlertEvent.fired_at)
            .all()
        )
    for a in alerts:
        events.append(_ev(
            a.fired_at, "alert.fired", f"{a.alertname} [{a.severity or '?'}]",
            epistemic=Epistemic.OBSERVED, provenance="kg_alerts",
            details={"alertname": a.alertname, "severity": a.severity,
                     "fingerprint": a.fingerprint},
        ))
        if a.resolved_at is not None:
            events.append(_ev(
                a.resolved_at, "alert.resolved", f"{a.alertname} resolved",
                epistemic=Epistemic.OBSERVED, provenance="kg_alerts",
                details={"alertname": a.alertname, "fingerprint": a.fingerprint,
                         "duration_min": int((a.resolved_at - a.fired_at).total_seconds() // 60)},
            ))

    sid = cast(Optional[int], incident.service_id)
    if sid is None:
        unknowns.append({
            "scope": "deploy,pod_event,anomaly,log_errors",
            "reason": "сервис не найден в графе (service_id пуст) — источники не опрошены",
        })
    else:
        events.extend(_deploy_events(db, sid, start, end))
        events.extend(_pod_events(db, sid, start, end))
        events.extend(_anomaly_events(db, sid, start, end))
        events.extend(_log_events(db, sid, start, end))

    if resolved_at is not None:
        events.append(_ev(
            resolved_at, "incident.resolved",
            f"Инцидент закрыт: {incident.resolve_reason or ''}".strip(),
            epistemic=Epistemic.OBSERVED, provenance="kg_incidents",
            details={"reason": incident.resolve_reason,
                     "duration_min": int((resolved_at - opened_at).total_seconds() // 60)},
        ))

    events.sort(key=lambda e: (e["ts"], _KIND_ORDER.get(e["kind"], 99)))
    counts: Dict[str, int] = defaultdict(int)
    for e in events:
        counts[e["kind"]] += 1

    return {
        "incident": incident_to_dict(incident),
        "window": {"start": start, "end": end,
                   "lookback_min": LOOKBACK_MIN, "lookahead_min": LOOKAHEAD_MIN},
        "events": events,
        "counts": dict(counts),
        "unknowns": unknowns,
    }


def _deploy_events(db: Session, sid: int, start: datetime, end: datetime) -> List[Dict[str, Any]]:
    rows = (
        db.query(Deployment)
        .filter(Deployment.service_id == sid,
                Deployment.started_at >= start, Deployment.started_at <= end)
        .order_by(Deployment.started_at)
        .all()
    )
    out: List[Dict[str, Any]] = []
    for d in rows:
        extras: Dict[str, Any] = d.extras if isinstance(d.extras, dict) else {}
        scope = deploy_attribution_scope(extras)
        # Точная запись — наблюдение выката этого сервиса; ns-broadcast —
        # вывод «в namespace катили», к сервису не привязанный.
        epistemic = Epistemic.OBSERVED if scope == "service" else Epistemic.INFERRED
        attribution = extras.get("attribution") or d.buildtype_id or "deploy"
        if extras.get("attribution") == "k8s_rollout":
            title = f"Выкат в кластере ({extras.get('rollout_reason', '?')})"
        else:
            title = f"Деплой {d.buildtype_id or ''} #{d.build_number or '?'}".strip()
        if scope != "service":
            title += " — по namespace, привязка к сервису не подтверждена"
        out.append(_ev(
            d.started_at, "deploy", title,
            epistemic=epistemic, provenance=f"kg_deployments/{scope}",
            details={
                "attribution": attribution, "scope": scope,
                "buildtype_id": d.buildtype_id, "build_number": d.build_number,
                "status": d.status, "triggered_by": d.triggered_by, "sha": d.sha,
                "rollout_reason": extras.get("rollout_reason"),
                "images": extras.get("images"),
                "previous_images": extras.get("previous_images"),
            },
        ))
    return out


def _pod_events(db: Session, sid: int, start: datetime, end: datetime) -> List[Dict[str, Any]]:
    rows = (
        db.query(PodEvent)
        .filter(PodEvent.service_id == sid,
                PodEvent.last_seen >= start, PodEvent.first_seen <= end)
        .order_by(PodEvent.first_seen)
        .all()
    )
    return [
        _ev(
            p.first_seen, "pod_event", f"{p.reason} × {p.count or 1} — {p.pod_name}",
            epistemic=Epistemic.OBSERVED, provenance="kg_pod_events",
            details={"reason": p.reason, "pod": p.pod_name, "count": p.count,
                     "type": p.type, "message": (p.message or "")[:160],
                     "last_seen": p.last_seen},
        )
        for p in rows
    ]


def _anomaly_events(db: Session, sid: int, start: datetime, end: datetime) -> List[Dict[str, Any]]:
    """Аномалии схлопываются по (метрика, час): volume guard и так режет до
    трёх наблюдений в час, лента из сотни точек нечитаема."""
    rows = (
        db.query(AnomalyObservation)
        .filter(AnomalyObservation.service_id == sid,
                AnomalyObservation.ts >= start, AnomalyObservation.ts <= end)
        .order_by(AnomalyObservation.ts)
        .all()
    )
    buckets: Dict[tuple, Dict[str, Any]] = {}
    for a in rows:
        hour = a.ts.replace(minute=0, second=0, microsecond=0)
        b = buckets.setdefault((a.metric, hour), {
            "first_ts": a.ts, "count": 0, "max_abs_z": 0.0, "severity": None,
            "value": a.value, "baseline": a.baseline_mean,
        })
        b["count"] += 1
        z = abs(a.z_score or 0.0)
        if z > b["max_abs_z"]:
            b["max_abs_z"], b["value"], b["baseline"] = z, a.value, a.baseline_mean
        if a.severity == "critical" or b["severity"] is None:
            b["severity"] = a.severity
    out: List[Dict[str, Any]] = []
    for (metric, _hour), b in sorted(buckets.items(), key=lambda kv: kv[1]["first_ts"]):
        out.append(_ev(
            b["first_ts"], "anomaly",
            f"Аномалия {metric}: {b['value']:.2f} при baseline {b['baseline']:.2f} "
            f"(|z|≤{b['max_abs_z']:.1f}, {b['count']} набл./ч)",
            # Аномалия — вывод детектора из метрик, а не наблюдение события.
            epistemic=Epistemic.INFERRED, provenance="kg_anomaly_observations",
            details={"metric": metric, "count": b["count"], "max_abs_z": round(b["max_abs_z"], 1),
                     "severity": b["severity"], "value": b["value"], "baseline": b["baseline"]},
        ))
    return out


def _log_events(db: Session, sid: int, start: datetime, end: datetime) -> List[Dict[str, Any]]:
    rows = (
        db.query(LogObservation)
        .filter(LogObservation.service_id == sid,
                LogObservation.ts >= start, LogObservation.ts <= end)
        .order_by(LogObservation.ts)
        .all()
    )
    return [
        _ev(
            r.ts, "log_errors", f"Логи: {r.level} × {r.count}",
            epistemic=Epistemic.OBSERVED, provenance="kg_log_observations",
            details={"level": r.level, "count": r.count},
        )
        for r in rows
        if (r.level or "").lower() in _LOG_LEVELS_OF_INTEREST and (r.count or 0) > 0
    ]
