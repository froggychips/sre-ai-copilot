"""Apply-service: канонический code-path для выполнения утверждённой ExecutionIntent.

Вызывается из Discord interaction handler (Ed25519-trusted) после
двухшагового подтверждения. Может также быть вызван из /approvals/{id}/approve
в будущем (JWT-trusted) — функция возвращает result-dict, не FastAPI Response.

Контракт:
  apply_intent(incident_id, applied_by) -> dict
    ineligibility → {"ok": False, "reason": "<reason>"}
    success       → {"ok": True, "result": <k8s execution dict>, "intent": ...}
    error         → {"ok": False, "reason": "execute_error", "error": "<msg>"}

Каждый apply:
  1. Загружает IncidentRecord, читает analysis.execution_intent + executor_result.
  2. Проверяет eligibility:
       - запись существует
       - executor_result.status == "dry_run_ok"
       - execution_intent распарсен, risk in {"low", "medium"}
       - executor_applied отсутствует (идемпотентность)
  3. Вызывает k8s_service.execute_intent(intent, dry_run=False, post_approval=True).
  4. Записывает результат в record.analysis.executor_applied с timestamp + user.
  5. Audit-event EXECUTOR_APPLIED (или EXECUTOR_APPLY_REFUSED при отказе).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

import structlog
from sqlalchemy.orm.attributes import flag_modified

from app.core.execution_dsl import ExecutionIntent
from app.database import IncidentRecord, SessionLocal
from app.observability.ai_metrics import track_executor_applied
from app.services.audit_logger import audit_service
from app.services.k8s_service import k8s_service

log = structlog.get_logger()

_ELIGIBLE_RISKS = {"low", "medium"}


def _refuse(incident_id: str, reason: str, applied_by: str) -> Dict[str, Any]:
    audit_service.log_event(
        "EXECUTOR_APPLY_REFUSED",
        {"incident_id": incident_id, "reason": reason, "applied_by": applied_by},
    )
    log.info("executor_apply.refused", incident_id=incident_id, reason=reason)
    return {"ok": False, "reason": reason}


def apply_intent(
    incident_id: str, applied_by: str, expected_signature: Optional[str] = None
) -> Dict[str, Any]:
    """Запустить kubectl для утверждённой ExecutionIntent. См. модульный docstring.

    expected_signature: если передан (из approve-кнопки custom_id), сверяем его с
    compute_signature(loaded_intent) и отказываем при расхождении — закрывает TOCTOU
    «оператор подтвердил intent A, в БД лежит intent B». None = проверка пропускается.
    """
    db = SessionLocal()
    try:
        record = (
            db.query(IncidentRecord)
            .filter(IncidentRecord.incident_id == incident_id)
            .first()
        )
        if record is None:
            return _refuse(incident_id, "incident_not_found", applied_by)

        analysis: Dict[str, Any] = record.analysis or {}

        # ── Идемпотентность: уже применили? ────────────────────────────
        if analysis.get("executor_applied"):
            log.info("executor_apply.already_applied", incident_id=incident_id)
            return {"ok": False, "reason": "already_applied"}

        intent_data = analysis.get("execution_intent")
        if not intent_data:
            return _refuse(incident_id, "no_intent", applied_by)

        try:
            intent = ExecutionIntent.model_validate(intent_data)
        except Exception as e:
            return _refuse(incident_id, f"intent_invalid:{type(e).__name__}", applied_by)

        if intent.risk.lower() not in _ELIGIBLE_RISKS:
            return _refuse(incident_id, f"risk_too_high:{intent.risk}", applied_by)

        # Integrity-gate: intent, который видел оператор в embed, должен совпадать
        # с тем, что сейчас в БД (защита от подмены записи между показом и кликом).
        if expected_signature is not None:
            from app.services.intent_signature import compute_signature

            if compute_signature(intent) != expected_signature:
                return _refuse(incident_id, "signature_mismatch", applied_by)

        executor_result: Dict[str, Any] = analysis.get("executor_result") or {}
        if executor_result.get("status") != "dry_run_ok":
            return _refuse(
                incident_id,
                f"dry_run_not_ok:{executor_result.get('status', 'missing')}",
                applied_by,
            )

        # ── Выполнение ─────────────────────────────────────────────────
        result = k8s_service.execute_intent(intent, dry_run=False, post_approval=True)

        # ── Persist ────────────────────────────────────────────────────
        applied_entry = {
            "applied_at": datetime.now(timezone.utc).isoformat(),
            "applied_by": applied_by,
            "result": {
                "success": result.get("success", False),
                "command": result.get("command"),
                "stdout": (result.get("stdout") or "")[:2048],
                "stderr": (result.get("stderr") or "")[:2048],
                "exit_code": result.get("exit_code"),
                "error": result.get("error"),
            },
        }
        analysis["executor_applied"] = applied_entry
        record.analysis = analysis
        # SQLAlchemy не видит in-place изменения внутри JSON Column без явного флага.
        flag_modified(record, "analysis")
        db.commit()

        audit_service.log_event(
            "EXECUTOR_APPLIED",
            {
                "incident_id": incident_id,
                "applied_by": applied_by,
                "command": result.get("command"),
                "success": result.get("success", False),
            },
        )
        track_executor_applied(bool(result.get("success", False)))
        log.info(
            "executor_apply.done",
            incident_id=incident_id,
            success=result.get("success", False),
            applied_by=applied_by,
        )
        return {"ok": True, "result": result, "intent": intent_data}
    except Exception as e:
        # Не должны падать наружу из апровал-хэндлера.
        log.error("executor_apply.exception", incident_id=incident_id, error=str(e))
        audit_service.log_event(
            "EXECUTOR_APPLY_EXCEPTION",
            {"incident_id": incident_id, "error": type(e).__name__},
        )
        try:
            db.rollback()
        except Exception:
            pass
        return {"ok": False, "reason": "execute_error", "error": str(e)}
    finally:
        db.close()
