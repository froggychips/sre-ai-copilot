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

Операционная память (roadmap п.7, 07.09.2026). Та же лента продолжается
тем, что копилот СДЕЛАЛ с инцидентом: собранные свидетельства
(`incidents.analysis.facts`), диагноз (`cause` / `triage_note`), решения
плейбуков (`kg_remediation_decisions`), применённое действие
(`executor_applied` со снимками идентичности) и верификация исхода
(`executor_verification`). Это не новая таблица — цепочка
Incident → Evidence → Diagnosis → Decision → Action → Verification уже
записана в трёх местах, ей не хватало одной оси времени и связи с
`kg_incidents` (через fingerprint: LLM-путь пишет `incident_id ==
fingerprint`). Сводка — в `memory`. Если разбор по алертам инцидента не
запускался (записей в `incidents` нет), это названо в `unknowns`: «действий
не было» и «путь выключен» — разные ответы.
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
from app.remediation.models import RemediationDecision

#: Сколько смотреть ДО первого алерта: деплой, который его вызвал, обычно
#: в пределах часа (RecentDeployRule живёт тем же окном).
LOOKBACK_MIN = 60
#: Сколько смотреть ПОСЛЕ резолва: последствия и повторные события.
LOOKAHEAD_MIN = 30

#: Порядок событий с одинаковым ts: причина раньше следствия.
_KIND_ORDER = {
    "incident.opened": 0, "deploy": 1, "pod_event": 2, "anomaly": 3,
    "log_errors": 4, "alert.fired": 5,
    # операционная память: что копилот сделал — после того, что он увидел
    "evidence": 6, "diagnosis": 7, "decision": 8, "action.applied": 9, "verification": 10,
    "alert.resolved": 11, "incident.resolved": 12,
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

    memory_events, memory, memory_unknown = _operational_memory(db, incident)
    events.extend(memory_events)
    if memory_unknown:
        unknowns.append(memory_unknown)

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

    memory["outcome"] = _outcome(memory, incident)
    return {
        "incident": incident_to_dict(incident),
        "window": {"start": start, "end": end,
                   "lookback_min": LOOKBACK_MIN, "lookahead_min": LOOKAHEAD_MIN},
        "events": events,
        "counts": dict(counts),
        "unknowns": unknowns,
        "memory": memory,
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


# ── операционная память ─────────────────────────────────────────────────

def _naive(dt: Any) -> Optional[datetime]:
    """ISO-строка или datetime → naive UTC (ось времени ленты — naive UTC, §10)."""
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except ValueError:
            return None
    if not isinstance(dt, datetime):
        return None
    return ensure_naive(dt)


def _operational_memory(db: Session, incident: KGIncident):
    """События «что копилот сделал» + сводка + Known Unknown, если разбора не было."""
    from app.database import IncidentRecord

    fps = list(incident.fingerprints or [])
    memory: Dict[str, Any] = {
        "records": 0, "diagnosis": None, "resolution_quality": None,
        "decisions": 0, "actions": 0, "verification": None, "identity_check": None,
    }
    events: List[Dict[str, Any]] = []
    if not fps:
        return events, memory, None

    records: List[Any] = (
        db.query(IncidentRecord).filter(IncidentRecord.incident_id.in_(fps)).all()
    )
    decisions: List[RemediationDecision] = (
        db.query(RemediationDecision)
        .filter(RemediationDecision.incident_id.in_(fps))
        .order_by(RemediationDecision.created_at)
        .all()
    )
    memory["records"] = len(records)
    memory["decisions"] = len(decisions)

    for rec in records:
        analysis: Dict[str, Any] = rec.analysis if isinstance(rec.analysis, dict) else {}
        base_ts = _naive(getattr(rec, "created_at", None)) or incident.opened_at

        facts = [f for f in (analysis.get("facts") or []) if isinstance(f, dict)]
        if facts:
            by_verdict: Dict[str, int] = defaultdict(int)
            for f in facts:
                verdict = f.get("verdict") or ("found" if f.get("observed") else "absent")
                by_verdict[verdict] += 1
            found = [f for f in facts if f.get("observed")]
            found.sort(key=lambda f: -(f.get("confidence") or 0))
            events.append(_ev(
                base_ts, "evidence",
                "Свидетельства: " + ", ".join(f"{k} {v}" for k, v in sorted(by_verdict.items())),
                epistemic=Epistemic.OBSERVED if found else Epistemic.INFERRED,
                provenance="incidents.analysis.facts",
                details={
                    "by_verdict": dict(by_verdict),
                    "found": [{"kind": f.get("kind"), "confidence": f.get("confidence"),
                               "epistemic": f.get("epistemic"), "subject": f.get("subject")}
                              for f in found[:5]],
                    "unknown": [{"kind": f.get("kind"), "reason": f.get("unknown_reason")}
                                for f in facts if f.get("verdict") == "unknown"][:5],
                },
            ))

        cause = analysis.get("cause")
        triage = analysis.get("triage_note")
        quality = analysis.get("resolution_quality")
        if cause or triage:
            memory["diagnosis"] = cause or triage
            memory["resolution_quality"] = quality
            events.append(_ev(
                base_ts, "diagnosis",
                f"Диагноз: {cause}" if cause else f"Диагноз не поставлен: {triage}",
                # Вывод гипотез над свидетельствами — заключение, не наблюдение.
                epistemic=Epistemic.INFERRED, provenance="incidents.analysis",
                details={"cause": cause, "triage_note": triage, "resolution_quality": quality,
                         "fact_conflicts": analysis.get("fact_conflicts") or [],
                         "is_recurrence": analysis.get("is_recurrence")},
            ))

        applied = analysis.get("executor_applied")
        if isinstance(applied, dict):
            intent = analysis.get("execution_intent") or {}
            result = applied.get("result") or {}
            before = applied.get("target_before") or {}
            after = applied.get("target_after") or {}
            memory["actions"] += 1
            memory["identity_check"] = applied.get("identity_check")
            events.append(_ev(
                _naive(applied.get("applied_at")) or base_ts, "action.applied",
                f"Действие: {intent.get('action', '?')} {intent.get('resource_type', '')}/"
                f"{intent.get('resource_name', '?')} — "
                f"{'ok' if result.get('success') else 'ошибка'}",
                epistemic=Epistemic.OBSERVED, provenance="incidents.analysis.executor_applied",
                details={
                    "action": intent.get("action"), "resource": intent.get("resource_name"),
                    "applied_by": applied.get("applied_by"), "success": result.get("success"),
                    "command": result.get("command"), "identity_check": applied.get("identity_check"),
                    "uid_before": before.get("uid"), "uid_after": after.get("uid"),
                    "generation_before": before.get("generation"),
                    "generation_after": after.get("generation"),
                    "verification_scheduled": (applied.get("verification") or {}).get("scheduled"),
                },
            ))

        ver = analysis.get("executor_verification")
        if isinstance(ver, dict):
            outcome = ver.get("outcome")
            memory["verification"] = outcome
            events.append(_ev(
                _naive(ver.get("checked_at")) or base_ts, "verification",
                f"Верификация: {outcome}" + (f" — {ver['reasons'][0]}" if ver.get("reasons") else ""),
                epistemic=(Epistemic.OBSERVED if outcome in ("verified", "failed")
                           else Epistemic.UNKNOWN),
                provenance="incidents.analysis.executor_verification",
                details={"outcome": outcome, "attempt": ver.get("attempt"),
                         "checks": ver.get("checks") or {}, "reasons": ver.get("reasons") or []},
            ))

    for d in decisions:
        events.append(_ev(
            _naive(getattr(d, "created_at", None)) or incident.opened_at, "decision",
            f"Решение: {d.decision or '?'}" + (f" · {d.selected_playbook}" if d.selected_playbook else ""),
            # Политика применена к осям риска — объявленный результат правил.
            epistemic=Epistemic.DECLARED, provenance="kg_remediation_decisions",
            details={"decision": d.decision, "playbook": d.selected_playbook,
                     "classification": d.classification,
                     "reasons": d.decision_reasons or [],
                     "command_preview": d.command_preview,
                     "target_uid": (d.target_ref or {}).get("uid") if isinstance(d.target_ref, dict) else None},
        ))

    unknown = None
    if not records and not decisions:
        unknown = {
            "scope": "evidence,diagnosis,decision,action,verification",
            "reason": "разбор по алертам инцидента не запускался (записей в incidents и "
                      "kg_remediation_decisions нет) — действий не было не потому, что "
                      "нечего было делать, а потому, что путь не включён",
        }
    return events, memory, unknown


def _outcome(memory: Dict[str, Any], incident: KGIncident) -> str:
    """Итог операционной памяти одной строкой."""
    if memory.get("verification"):
        return f"action_{memory['verification']}"
    if memory.get("actions"):
        return "action_applied_unverified"
    if incident.status == "resolved":
        return "resolved_without_action" if memory.get("records") else "resolved_without_analysis"
    return "open_without_action" if memory.get("records") else "open_without_analysis"
