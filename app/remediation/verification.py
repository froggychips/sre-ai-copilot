"""Верификация remediation: та же ли цель, сработало ли, стало ли лучше.

До 06.09.2026 `apply_intent` заканчивался на exit code kubectl: «команда
прошла» считалось результатом. Но команда проходит и над Deployment,
который пересоздали час назад под тем же именем, и над тем, который после
рестарта так и крашится. Оператор видел «✓ applied» — и всё.

Здесь три вопроса, каждый со своим ответом, а не одним флагом:

  1. **Та же ли цель.** Перед записью снимаем живой объект (`uid`,
     `generation`, hash `spec.template`, реплики). Если у инцидента есть
     `target_ref` из графа (`kg_remediation_decisions`, uid из
     `kg_services.k8s_uid`) и его uid не совпадает с живым — объект
     пересоздан, действие отказано: «мы успешно починили Deployment,
     которого уже нет» — худший исход, потому что он выглядит успехом.
  2. **Сработало ли.** После записи — второй снимок: у `rollout restart`
     меняется hash шаблона и растёт generation; у `scale` — replicas.
  3. **Стало ли лучше.** Отложенная проверка (Celery, через 5 и 15 минут):
     тот же uid, rollout сошёлся (`observedGeneration == generation`),
     `ready == desired`, алерт инцидента resolved, новых
     CrashLoop/OOM-событий у подов нет.

Исход — `verified` / `failed` / `pending` / `unknown`, и `unknown` — честный
ответ, а не сбой: kubectl недоступен, uid сменился после действия, алерта
в графе нет. Все проверки лежат в `analysis["executor_verification"]` с
причинами, состояние исполнителя переходит в `verified` или
`verification_failed` (`incident_state`).

Снимок берётся через `kubectl_breaker.run_kubectl` — единственный
разрешённый путь к kubectl (тест `test_no_direct_kubectl_calls`), read-only
`get -o json`. Никаких мутаций здесь нет и не должно появиться.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import structlog

from app.config import settings
from app.core.execution_dsl import ActionType, ExecutionIntent

log = structlog.get_logger("remediation.verification")

OUTCOME_VERIFIED = "verified"
OUTCOME_FAILED = "failed"
OUTCOME_PENDING = "pending"
OUTCOME_UNKNOWN = "unknown"

#: Причины событий подов, которые после действия означают «не помогло».
_CRASH_REASONS = ("oomkill", "crashloopbackoff", "backoff")

_RESOURCE_KIND = {
    "deployment": "deployment", "pod": "pod", "service": "service", "ingress": "ingress",
}


@dataclass
class TargetSnapshot:
    """Состояние живого объекта в момент наблюдения. `unknown=True` — снять
    не удалось; `reason` говорит почему. Отсутствие снимка — отсутствие
    доказательств, а не доказательство отсутствия."""

    kind: Optional[str] = None
    namespace: Optional[str] = None
    name: Optional[str] = None
    uid: Optional[str] = None
    resource_version: Optional[str] = None
    generation: Optional[int] = None
    observed_generation: Optional[int] = None
    template_hash: Optional[str] = None
    replicas_desired: Optional[int] = None
    replicas_ready: Optional[int] = None
    phase: Optional[str] = None
    observed_at: Optional[str] = None
    unknown: bool = False
    reason: Optional[str] = None
    extras: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def unavailable(cls, reason: str, intent: Optional[ExecutionIntent] = None) -> "TargetSnapshot":
        return cls(
            kind=intent.resource_type if intent else None,
            namespace=intent.namespace if intent else None,
            name=intent.resource_name if intent else None,
            observed_at=_now_iso(), unknown=True, reason=reason,
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def converged(self) -> Optional[bool]:
        if self.unknown or self.generation is None or self.observed_generation is None:
            return None
        return self.observed_generation >= self.generation

    @property
    def healthy(self) -> Optional[bool]:
        if self.unknown or self.replicas_desired is None:
            return None
        if self.replicas_desired == 0:
            return None      # отскейлен в ноль — «здоров» бессмысленно
        return (self.replicas_ready or 0) >= self.replicas_desired


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _template_hash(spec: Dict[str, Any]) -> Optional[str]:
    """Тот же hash, что у kg_deploy_watch: sha256 от spec.template, без replicas."""
    template = spec.get("template")
    if not isinstance(template, dict):
        return None
    payload = json.dumps(template, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def parse_snapshot(obj: Dict[str, Any], intent: ExecutionIntent) -> TargetSnapshot:
    """Снимок из JSON-объекта kubectl. Чистая функция — тестируется без кластера."""
    meta = obj.get("metadata") or {}
    spec = obj.get("spec") or {}
    status = obj.get("status") or {}
    snap = TargetSnapshot(
        kind=intent.resource_type,
        namespace=meta.get("namespace") or intent.namespace,
        name=meta.get("name") or intent.resource_name,
        uid=meta.get("uid"),
        resource_version=meta.get("resourceVersion"),
        generation=meta.get("generation"),
        observed_generation=status.get("observedGeneration"),
        observed_at=_now_iso(),
    )
    if intent.resource_type == "deployment":
        snap.template_hash = _template_hash(spec)
        snap.replicas_desired = spec.get("replicas")
        snap.replicas_ready = status.get("readyReplicas") or 0
    elif intent.resource_type == "pod":
        snap.phase = status.get("phase")
    return snap


def snapshot_target(
    intent: ExecutionIntent, *, runner: Optional[Callable[..., Any]] = None, timeout: float = 15.0,
) -> TargetSnapshot:
    """`kubectl get <kind> <name> -n <ns> -o json` → TargetSnapshot. Fail-open в
    `unknown`: верификация никогда не должна ронять действие."""
    if runner is None:
        from app.knowledge_graph.kubectl_breaker import run_kubectl
        runner = run_kubectl
    kind = _RESOURCE_KIND.get((intent.resource_type or "").lower())
    if kind is None:
        return TargetSnapshot.unavailable(f"unsupported_resource_type:{intent.resource_type}", intent)
    argv = ["kubectl", "get", kind, intent.resource_name, "-n", intent.namespace, "-o", "json"]
    try:
        proc = runner(argv, timeout=timeout, operation="remediation_snapshot", respect_breaker=True)
    except Exception as e:  # kubectl нет, брейкер открыт, таймаут
        return TargetSnapshot.unavailable(f"kubectl_failed:{type(e).__name__}", intent)
    if getattr(proc, "returncode", 1) != 0:
        err = (getattr(proc, "stderr", "") or "").strip()[:160]
        if "NotFound" in err or "not found" in err:
            return TargetSnapshot.unavailable("target_not_found", intent)
        return TargetSnapshot.unavailable(f"kubectl_exit_{getattr(proc, 'returncode', '?')}:{err}", intent)
    try:
        obj = json.loads(getattr(proc, "stdout", "") or "{}")
    except ValueError:
        return TargetSnapshot.unavailable("kubectl_output_not_json", intent)
    if not isinstance(obj, dict) or not obj.get("metadata"):
        return TargetSnapshot.unavailable("kubectl_output_empty", intent)
    return parse_snapshot(obj, intent)


# ── идентичность ──────────────────────────────────────────────────────────

def expected_identity(db: Any, incident_id: str) -> Optional[Dict[str, Any]]:
    """`target_ref` последнего решения по инциденту (kg_remediation_decisions):
    uid/incarnation из графа на момент разбора. None — решения нет или в нём
    нет uid (источник не сообщил): тогда сверять нечем, и это Known Unknown,
    а не отказ."""
    try:
        from app.remediation.models import RemediationDecision
        row = (
            db.query(RemediationDecision)
            .filter(RemediationDecision.incident_id == incident_id)
            .order_by(RemediationDecision.created_at.desc())
            .first()
        )
    except Exception as e:
        log.debug("verification.expected_identity_failed", error=type(e).__name__)
        return None
    ref = getattr(row, "target_ref", None) if row is not None else None
    if not isinstance(ref, dict):
        return None
    return {
        "uid": ref.get("uid"), "incarnation": ref.get("incarnation"),
        "kind": ref.get("kind"), "namespace": ref.get("namespace"), "name": ref.get("name"),
    }


def identity_mismatch(expected: Optional[Dict[str, Any]], live: TargetSnapshot) -> Optional[str]:
    """Причина отказа, если живой объект — не тот, о котором инцидент. None —
    совпадает или сверить нечем (об этом скажет identity_check='unknown')."""
    if not expected or not expected.get("uid") or live.unknown or not live.uid:
        return None
    if str(expected["uid"]) != str(live.uid):
        return (
            f"uid графа {str(expected['uid'])[:8]}… ≠ живой {str(live.uid)[:8]}… — "
            f"объект пересоздан после инцидента"
        )
    return None


def identity_check(expected: Optional[Dict[str, Any]], live: TargetSnapshot) -> str:
    if live.unknown:
        return "unknown:no_live_snapshot"
    if not expected or not expected.get("uid"):
        return "unknown:no_expected_uid"
    return "same" if str(expected["uid"]) == str(live.uid) else "mismatch"


# ── отложенная проверка ──────────────────────────────────────────────────

def verify_delays() -> List[int]:
    raw = str(getattr(settings, "REMEDIATION_VERIFY_DELAYS_SEC", "300,900") or "")
    out: List[int] = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit() and int(part) > 0:
            out.append(int(part))
    return out or [300, 900]


def schedule_verification(incident_id: str, *, attempt: int = 1) -> Dict[str, Any]:
    """Поставить отложенную проверку. Брокер недоступен — не роняем apply:
    исход честно `scheduled=False`, действие уже сделано."""
    if not getattr(settings, "REMEDIATION_VERIFY_ENABLED", True):
        return {"scheduled": False, "reason": "disabled"}
    delays = verify_delays()
    if attempt > len(delays):
        return {"scheduled": False, "reason": "no_more_attempts"}
    delay = delays[attempt - 1]
    try:
        from app.workers.tasks import remediation_verify_task
        remediation_verify_task.apply_async(args=[incident_id, attempt], countdown=delay)
    except Exception as e:
        log.warning("verification.schedule_failed", incident_id=incident_id, error=type(e).__name__)
        return {"scheduled": False, "reason": f"broker:{type(e).__name__}", "delay_sec": delay}
    return {"scheduled": True, "attempt": attempt, "delay_sec": delay}


def _parse_iso(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _alert_resolved(db: Any, incident_id: str) -> Optional[bool]:
    """Алерт инцидента resolved? None — строки в kg_alerts нет (LLM-путь пишет
    fingerprint == incident_id)."""
    try:
        from app.knowledge_graph.schema import AlertEvent
        row = db.query(AlertEvent).filter(AlertEvent.fingerprint == incident_id).first()
    except Exception:
        return None
    if row is None:
        return None
    return getattr(row, "resolved_at", None) is not None


def _new_crash_events(db: Any, intent: ExecutionIntent, since: datetime) -> int:
    try:
        from app.knowledge_graph.schema import NODE_KIND_SERVICE, PodEvent, Service
        svc_ids = [
            sid for (sid,) in db.query(Service.id).filter(
                Service.namespace == intent.namespace,
                Service.name == intent.resource_name,
                Service.node_kind == NODE_KIND_SERVICE,
            ).all()
        ]
        if not svc_ids:
            return 0
        rows = (
            db.query(PodEvent.reason)
            .filter(PodEvent.service_id.in_(svc_ids),
                    PodEvent.first_seen >= since.replace(tzinfo=None))
            .all()
        )
    except Exception:
        return 0
    return sum(1 for (r,) in rows if any(c in (r or "").lower() for c in _CRASH_REASONS))


def assess(
    *,
    intent: ExecutionIntent,
    before: Optional[Dict[str, Any]],
    now_snap: TargetSnapshot,
    alert_resolved: Optional[bool],
    new_crash_events: int,
    attempt: int,
    max_attempts: int,
) -> Dict[str, Any]:
    """Чистая оценка исхода по собранным фактам. Возвращает
    {outcome, checks, reasons}; каждая проверка — True/False/None(unknown)."""
    checks: Dict[str, Any] = {}
    reasons: List[str] = []
    before = before or {}

    if now_snap.unknown:
        return {"outcome": OUTCOME_UNKNOWN,
                "checks": {"snapshot": None},
                "reasons": [f"живой снимок недоступен: {now_snap.reason}"]}

    # 1. та же цель
    if before.get("uid") and now_snap.uid:
        same = before["uid"] == now_snap.uid
        checks["same_identity"] = same
        if not same:
            return {"outcome": OUTCOME_UNKNOWN, "checks": checks,
                    "reasons": ["uid объекта сменился после действия — результат "
                                "относится к другому объекту"]}
    else:
        checks["same_identity"] = None
        reasons.append("uid до действия неизвестен — идентичность не сверена")

    # 2. сработало ли
    if intent.action == ActionType.RESTART_DEPLOYMENT:
        took = None
        if before.get("template_hash") and now_snap.template_hash:
            took = before["template_hash"] != now_snap.template_hash
        elif before.get("generation") is not None and now_snap.generation is not None:
            took = now_snap.generation > before["generation"]
        checks["action_took_effect"] = took
        if took is False:
            reasons.append("шаблон и generation не изменились — рестарт не применился")
    elif intent.action == ActionType.SCALE_DEPLOYMENT:
        want = intent.params.get("replicas")
        took = None if want is None or now_snap.replicas_desired is None else int(want) == now_snap.replicas_desired
        checks["action_took_effect"] = took
        if took is False:
            reasons.append(f"replicas={now_snap.replicas_desired}, ожидалось {want}")
    else:
        checks["action_took_effect"] = None

    # 3. стало ли лучше
    checks["converged"] = now_snap.converged
    checks["healthy"] = now_snap.healthy
    checks["alert_resolved"] = alert_resolved
    checks["new_crash_events"] = new_crash_events
    if alert_resolved is None:
        reasons.append("алерта инцидента нет в kg_alerts — резолв не проверен")

    if new_crash_events > 0:
        reasons.append(f"после действия {new_crash_events} новых CrashLoop/OOM-событий")
        return {"outcome": OUTCOME_FAILED, "checks": checks, "reasons": reasons}
    if checks.get("action_took_effect") is False:
        return {"outcome": OUTCOME_FAILED, "checks": checks, "reasons": reasons}
    if now_snap.converged is False:
        reasons.append("rollout ещё не сошёлся (observedGeneration < generation)")
        return {"outcome": OUTCOME_PENDING if attempt < max_attempts else OUTCOME_FAILED,
                "checks": checks, "reasons": reasons}
    if now_snap.healthy is False:
        reasons.append(f"ready {now_snap.replicas_ready}/{now_snap.replicas_desired}")
        return {"outcome": OUTCOME_PENDING if attempt < max_attempts else OUTCOME_FAILED,
                "checks": checks, "reasons": reasons}
    if alert_resolved is False:
        reasons.append("алерт всё ещё firing")
        return {"outcome": OUTCOME_PENDING if attempt < max_attempts else OUTCOME_FAILED,
                "checks": checks, "reasons": reasons}
    return {"outcome": OUTCOME_VERIFIED, "checks": checks, "reasons": reasons}


def verify_remediation(
    incident_id: str,
    attempt: int = 1,
    *,
    db_factory: Optional[Callable[[], Any]] = None,
    runner: Optional[Callable[..., Any]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Отложенная проверка исхода применённого действия. См. докстринг модуля."""
    from sqlalchemy.orm.attributes import flag_modified

    from app.database import IncidentRecord, SessionLocal
    from app.observability.ai_metrics import track_remediation_verification
    from app.services.audit_logger import audit_service
    from app.services.incident_state import (EXECUTOR_VERIFIED,
                                             EXECUTOR_VERIFY_FAILED,
                                             set_executor_state)

    max_attempts = len(verify_delays())
    db = (db_factory or SessionLocal)()
    try:
        record = db.query(IncidentRecord).filter(IncidentRecord.incident_id == incident_id).first()
        if record is None:
            return {"outcome": OUTCOME_UNKNOWN, "reason": "incident_not_found"}
        analysis: Dict[str, Any] = dict(record.analysis or {})
        applied = analysis.get("executor_applied") or {}
        intent_data = analysis.get("execution_intent")
        if not applied or not intent_data:
            return {"outcome": OUTCOME_UNKNOWN, "reason": "nothing_applied"}
        try:
            intent = ExecutionIntent.model_validate(intent_data)
        except Exception as e:
            return {"outcome": OUTCOME_UNKNOWN, "reason": f"intent_invalid:{type(e).__name__}"}

        applied_at = _parse_iso(applied.get("applied_at")) or (now or datetime.now(timezone.utc))
        now_snap = snapshot_target(intent, runner=runner)
        result = assess(
            intent=intent,
            before=applied.get("target_before"),
            now_snap=now_snap,
            alert_resolved=_alert_resolved(db, incident_id),
            new_crash_events=_new_crash_events(db, intent, applied_at),
            attempt=attempt,
            max_attempts=max_attempts,
        )
        entry = {
            "attempt": attempt,
            "max_attempts": max_attempts,
            "checked_at": _now_iso(),
            "outcome": result["outcome"],
            "checks": result["checks"],
            "reasons": result["reasons"],
            "snapshot": now_snap.to_dict(),
        }
        history = list(analysis.get("executor_verification_history") or [])
        history.append({k: entry[k] for k in ("attempt", "checked_at", "outcome")})
        analysis["executor_verification"] = entry
        analysis["executor_verification_history"] = history[-10:]
        record.analysis = analysis
        flag_modified(record, "analysis")
        if result["outcome"] == OUTCOME_VERIFIED:
            set_executor_state(record, EXECUTOR_VERIFIED)
        elif result["outcome"] == OUTCOME_FAILED:
            set_executor_state(record, EXECUTOR_VERIFY_FAILED)
        db.commit()

        audit_service.log_event(
            f"EXECUTOR_VERIFY_{result['outcome'].upper()}",
            {"incident_id": incident_id, "attempt": attempt,
             "checks": result["checks"], "reasons": result["reasons"]},
        )
        track_remediation_verification(result["outcome"])
        log.info("verification.done", incident_id=incident_id, attempt=attempt,
                 outcome=result["outcome"], reasons=result["reasons"])

        next_delay: Optional[int] = None
        if result["outcome"] == OUTCOME_PENDING and attempt < max_attempts:
            next_delay = verify_delays()[attempt]
        return {**result, "attempt": attempt, "next_delay_sec": next_delay}
    finally:
        db.close()
