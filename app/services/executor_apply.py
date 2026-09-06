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
       - executor_applied отсутствует (идемпотентность)
       - нет свежего executor_in_flight-клейма (двухфазный apply, см. ниже)
       - execution_intent распарсен
       - expected_signature ОБЯЗАТЕЛЕН и совпадает с compute_signature(intent)
         (TOCTOU: intent в БД == тому, что видел оператор)
       - в kg_action_approvals есть терминальный APPROVED для (incident_id,
         signature) — независимая проверка одобрения человеком (H1)
       - approval СВЕЖИЙ: decided_at не старше EXECUTOR_APPROVAL_MAX_AGE_SECONDS
         (часовой давности апрув не должен авторизовывать повторный kubectl
         после re-fire, который стёр executor_applied)
       - сам INTENT свежий: возраст плана не старше
         EXECUTOR_INTENT_MAX_AGE_SECONDS (см. ниже, M1)
       - инцидент не помечен «состояние кластера неизвестно» (M2)
       - intent.namespace == namespace инцидента из record.data (server-side
         binding: галлюцинированный/инжектированный intent не может действовать
         в чужом namespace)
       - risk in {"low", "medium"} (advisory LLM-hint)
       - ДЕТЕРМИНИРОВАННЫЙ policy-gate (evaluate_intent_gate) != BLOCK —
         пересчёт риска из самого intent-а, не из LLM-`risk` (см. executor_gate)
       - executor_result.status == "dry_run_ok"
  3. Двухфазный claim: ПЕРЕД kubectl пишет и КОММИТИТ analysis.executor_in_flight
     (timestamp + user). Краш/таймаут между мутацией кластера и записью
     executor_applied больше не оставляет живую кнопку: свежий claim = отказ
     apply_in_flight; протухший (EXECUTOR_IN_FLIGHT_TTL_SECONDS) снимается, но
     БЕЗ повторного write (M2, см. ниже).
  4. Пере-dry-run: kubectl --dry-run=server ещё раз, непосредственно перед
     реальным write (M1в). Провал → отказ, write не выполняется.
  5. Вызывает k8s_service.execute_intent(intent, dry_run=False, post_approval=True).
  6. Записывает результат в record.analysis.executor_applied (claim снимается)
     с timestamp + user + якорем времени intent-а.
  7. Audit-event EXECUTOR_APPLIED (или EXECUTOR_APPLY_REFUSED при отказе).

Свежесть плана, а не только одобрения (M1):
  Approval-окно ограничивает время от клика Approve, но не возраст самого
  плана. Оператор мог нажать «Approve & Run» на эмбеде недельной давности:
  approve свежий, а kubectl посчитан по состоянию кластера, которого уже нет.
  Поэтому:
    - возраст intent-а сверяется с EXECUTOR_INTENT_MAX_AGE_SECONDS. Явного
      created_at пайплайн в analysis не пишет (app/workers/pipeline.py вне
      скоупа), поэтому якорь выбирается из доступных — см.
      intent_signature.intent_recorded_at; нет ни одного → отказ
      intent_age_unknown (fail-closed);
    - разрешение к записи подтверждается СВЕЖИМ server-side dry-run
      непосредственно перед write: kube-apiserver ещё раз валидирует ровно
      ту же команду по ТЕКУЩЕМУ состоянию кластера (ресурс мог быть удалён,
      переименован, замещён другим владельцем). Провал → отказ без write.
  Разрешённый возраст фиксируется в executor_applied (provenance apply-а).

Протухший in-flight claim (M2):
  Claim снимается по TTL, но «claim протух» ≠ «write не состоялся»: kubectl мог
  успеть мутировать кластер, а финализация — не закоммититься. Поэтому
  переклейм больше НЕ выполняет write молча: инцидент помечается
  analysis.executor_state_unknown (состояние кластера неизвестно → manual), и
  apply отказывает. Разблокировать может только одобрение, выданное ПОСЛЕ
  этой пометки (человек сходил в кластер, увидел факт и одобрил заново).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

import structlog
from sqlalchemy.orm.attributes import flag_modified

from app.config import settings
from app.core.execution_dsl import ExecutionIntent
from app.database import IncidentRecord, SessionLocal
from app.observability.ai_metrics import track_executor_applied
from app.remediation.verification import (expected_identity, identity_check,
                                          identity_mismatch,
                                          schedule_verification, snapshot_target)
from app.services.audit_logger import audit_service
from app.services.intent_signature import intent_recorded_at, parse_utc_ts
from app.services.k8s_service import k8s_service

log = structlog.get_logger()

_ELIGIBLE_RISKS = {"low", "medium"}


def _utcnow_naive() -> datetime:
    """Naive-UTC now — тот же формат, что у ActionApproval.decided_at."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _approval_is_stale(approval: Any, max_age_sec: int) -> bool:
    """True если approve старше окна (или decided_at отсутствует/битый).

    Fail-closed: недатированный approve не может авторизовать write.
    """
    decided_at = getattr(approval, "decided_at", None)
    if not isinstance(decided_at, datetime):
        return True
    if decided_at.tzinfo is not None:
        decided_at = decided_at.astimezone(timezone.utc).replace(tzinfo=None)
    return (_utcnow_naive() - decided_at).total_seconds() > max_age_sec


def _in_flight_is_fresh(entry: Dict[str, Any], ttl_sec: int) -> bool:
    """True если executor_in_flight-клейм ещё живой (отказ apply).

    Битый/непарсящийся claimed_at → считаем свежим (fail-closed): пока TTL
    определить нельзя, повторный kubectl запрещён.
    """
    raw = entry.get("claimed_at")
    try:
        claimed_at = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return True
    if claimed_at.tzinfo is None:
        claimed_at = claimed_at.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - claimed_at).total_seconds()
    return age <= ttl_sec


def _approved_after(approval: Any, state_unknown: Any) -> bool:
    """True если approve выдан ПОЗЖЕ пометки executor_state_unknown.

    Единственный легальный выход из состояния «кластер в неизвестном виде»:
    человек проверил кластер и одобрил действие уже после пометки. Одобрение
    того же прогона всегда старше пометки (сначала клик, потом протухший
    claim), поэтому само себя разблокировать не может. Любая неоднозначность
    (нет даты, битая дата, пометка не dict) → False, fail-closed.
    """
    if not isinstance(state_unknown, dict):
        return False
    detected_at = parse_utc_ts(state_unknown.get("detected_at"))
    decided_at = parse_utc_ts(getattr(approval, "decided_at", None))
    if detected_at is None or decided_at is None:
        return False
    return decided_at > detected_at


def _mark_state_unknown(
    db: Any,
    record: Any,
    analysis: Dict[str, Any],
    stale_claim: Any,
    applied_by: str,
) -> None:
    """Снять протухший claim и пометить инцидент «cluster state unknown».

    Claim не должен висеть вечно (иначе кнопка мертва навсегда), но и молча
    пускать второй write нельзя: kubectl прошлого apply мог выполниться, а
    финализация — нет. Пометка переживает мерж analysis в pipeline._persist,
    как executor_applied.
    """
    analysis.pop("executor_in_flight", None)
    analysis["executor_state_unknown"] = {
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "detected_by": applied_by,
        "stale_claim": stale_claim if isinstance(stale_claim, dict) else str(stale_claim),
        "resolution": "manual",
    }
    record.analysis = analysis
    flag_modified(record, "analysis")
    try:
        db.commit()
    except Exception as e:  # pragma: no cover — БД недоступна
        # Не смогли записать пометку — отказ всё равно отдаём (write не идёт).
        log.error(
            "executor_apply.state_unknown_persist_failed",
            error=str(e),
        )
        try:
            db.rollback()
        except Exception:
            pass


def _incident_namespace(record: Any) -> Optional[str]:
    """Namespace инцидента из сохранённого alert-payload (record.data).

    Основной источник — incident.namespace; fallback — labels.namespace.
    """
    data = record.data if isinstance(record.data, dict) else {}
    ns = data.get("namespace")
    if not ns:
        labels = data.get("labels")
        if isinstance(labels, dict):
            ns = labels.get("namespace")
    return str(ns).strip().lower() if ns else None


def _refuse(incident_id: str, reason: str, applied_by: str) -> Dict[str, Any]:
    audit_service.log_event(
        "EXECUTOR_APPLY_REFUSED",
        {"incident_id": incident_id, "reason": reason, "applied_by": applied_by},
    )
    log.info("executor_apply.refused", incident_id=incident_id, reason=reason)
    return {"ok": False, "reason": reason}


def _load_approval(db, incident_id: str, signature: str):
    """Вернуть ActionApproval-строку для (incident_id, signature) или None.

    Выделено отдельной функцией, чтобы тесты могли подменять источник
    одобрения, не поднимая БД.
    """
    from app.knowledge_graph.schema import ActionApproval

    return (
        db.query(ActionApproval)
        .filter(
            ActionApproval.incident_id == incident_id,
            ActionApproval.intent_signature == signature,
        )
        .first()
    )


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
        # with_for_update() берёт row-lock (SELECT ... FOR UPDATE) на строку
        # инцидента и держит его до commit/rollback в конце функции. Это
        # сериализует конкурентные apply (double-click / реплей / approve+apply):
        # второй вызов блокируется на этом SELECT, пока первый не закоммитит
        # executor_applied, и затем УВИДИТ его → отказ already_applied. Без лока
        # оба читают executor_applied=None и оба выполняют реальный kubectl дважды.
        record = (
            db.query(IncidentRecord)
            .filter(IncidentRecord.incident_id == incident_id)
            .with_for_update()
            .first()
        )
        if record is None:
            return _refuse(incident_id, "incident_not_found", applied_by)

        analysis: Dict[str, Any] = record.analysis or {}

        # ── Идемпотентность: уже применили? ────────────────────────────
        if analysis.get("executor_applied"):
            log.info("executor_apply.already_applied", incident_id=incident_id)
            return {"ok": False, "reason": "already_applied"}

        # ── Двухфазный claim: apply уже стартовал (и мог мутировать кластер,
        # не успев записать executor_applied — краш/таймаут в окне между
        # kubectl и commit)? Свежий claim = отказ.
        in_flight = analysis.get("executor_in_flight")
        in_flight_ttl = int(
            getattr(settings, "EXECUTOR_IN_FLIGHT_TTL_SECONDS", 600) or 600
        )
        if isinstance(in_flight, dict) and _in_flight_is_fresh(in_flight, in_flight_ttl):
            return _refuse(incident_id, "apply_in_flight", applied_by)
        if in_flight:
            # Протухший claim (M2). Раньше он просто переклеймивался, и apply
            # шёл дальше: TTL 600s < approval-окна 3600s, поэтому один approve
            # легально давал ВТОРОЙ реальный write, а факт первого никто не
            # проверял (kubectl мог выполниться и мутировать кластер — не
            # закоммитилась только финализация). Инвариант «≤1 write на
            # approve» держим так: claim снимаем (кнопка не мертва навсегда),
            # но write НЕ делаем — помечаем инцидент как «состояние кластера
            # неизвестно → manual» и отказываем.
            log.warning(
                "executor_apply.stale_in_flight_state_unknown",
                incident_id=incident_id,
                stale_claim=in_flight,
            )
            _mark_state_unknown(db, record, analysis, in_flight, applied_by)
            audit_service.log_event(
                "EXECUTOR_STATE_UNKNOWN",
                {
                    "incident_id": incident_id,
                    "applied_by": applied_by,
                    "stale_claim": in_flight,
                },
            )
            return _refuse(
                incident_id,
                "cluster_state_unknown:manual_verify_then_reapprove",
                applied_by,
            )

        intent_data = analysis.get("execution_intent")
        if not intent_data:
            return _refuse(incident_id, "no_intent", applied_by)

        try:
            intent = ExecutionIntent.model_validate(intent_data)
        except Exception as e:
            return _refuse(incident_id, f"intent_invalid:{type(e).__name__}", applied_by)

        # ── Authorization gate (#2 TOCTOU + H1) ────────────────────────
        # Реальный write требует ПРОВЕРЕННУЮ подпись — не доверяем тому, что
        # вызывающий хэндлер сделал авторизацию. expected_signature теперь
        # ОБЯЗАТЕЛЕН (раньше None молча пропускал проверку → apply-кнопка
        # обходила TOCTOU, а будущий вызывающий мог обойти SAFE_MODE).
        if not expected_signature:
            return _refuse(incident_id, "signature_required", applied_by)
        from app.services.intent_signature import compute_signature

        if compute_signature(intent) != expected_signature:
            # intent в БД ≠ тому, что видел оператор (подмена между показом и кликом).
            return _refuse(incident_id, "signature_mismatch", applied_by)

        # Независимая проверка: терминальный APPROVED в kg_action_approvals для
        # (incident_id, signature). Закрывает обход SAFE_MODE «будущим
        # вызывающим»: без записи об одобрении человеком write не выполняется.
        approval = _load_approval(db, incident_id, expected_signature)
        if approval is None or (approval.status or "").lower() != "approved":
            return _refuse(incident_id, "not_approved", applied_by)

        # Freshness-binding: терминальный APPROVED без границы по возрасту +
        # re-fire, стирающий executor_applied, = многочасовой approve
        # авторизует ВТОРОЙ реальный kubectl. Окно настраивается;
        # недатированный approve — отказ (см. _approval_is_stale).
        approval_max_age = int(
            getattr(settings, "EXECUTOR_APPROVAL_MAX_AGE_SECONDS", 3600) or 3600
        )
        if _approval_is_stale(approval, approval_max_age):
            return _refuse(incident_id, "approval_stale", applied_by)

        # ── Инцидент уже помечен «состояние кластера неизвестно» (M2) ───
        # Пометку ставит переклейм протухшего claim-а выше. Снять её может
        # только одобрение, выданное ПОСЛЕ пометки: человек сходил в кластер,
        # убедился, что первого write не было (или откатил его), и одобрил
        # заново. Одобрение того же прогона всегда старше пометки.
        state_unknown = analysis.get("executor_state_unknown")
        if state_unknown and not _approved_after(approval, state_unknown):
            return _refuse(
                incident_id,
                "cluster_state_unknown:manual_verify_then_reapprove",
                applied_by,
            )

        # ── Binding intent ↔ инцидент ──────────────────────────────────
        # Intent генерирует LLM из обогащённого промпта (логи/алерты =
        # untrusted input) — сверяем namespace intent-а с namespace самого
        # инцидента из record.data. Легитимного cross-namespace кейса в
        # кодовой базе нет → fail-closed.
        incident_ns = _incident_namespace(record)
        if not incident_ns:
            return _refuse(incident_id, "namespace_unbound", applied_by)
        if (intent.namespace or "").strip().lower() != incident_ns:
            return _refuse(incident_id, "namespace_mismatch", applied_by)

        if intent.risk.lower() not in _ELIGIBLE_RISKS:
            return _refuse(incident_id, f"risk_too_high:{intent.risk}", applied_by)

        executor_result: Dict[str, Any] = analysis.get("executor_result") or {}
        if executor_result.get("status") != "dry_run_ok":
            return _refuse(
                incident_id,
                f"dry_run_not_ok:{executor_result.get('status', 'missing')}",
                applied_by,
            )

        # ── Свежесть самого intent-а (M1) ──────────────────────────────
        # Approve-окно ограничивает время от клика, но не возраст плана:
        # «Approve & Run» на эмбеде недельной давности давал свежий approve на
        # kubectl, посчитанный по состоянию кластера, которого уже нет.
        # Якорь — время прогона пайплайна (какое именно поле взято, пишем в
        # audit/лог и в executor_applied, см. intent_recorded_at).
        intent_max_age = int(
            getattr(settings, "EXECUTOR_INTENT_MAX_AGE_SECONDS", 86400) or 86400
        )
        recorded_at, anchor = intent_recorded_at(
            analysis, getattr(record, "created_at", None)
        )
        if recorded_at is None:
            # Возраст плана неизвестен → write запрещён (fail-closed).
            return _refuse(incident_id, "intent_age_unknown", applied_by)
        intent_age = int((datetime.now(timezone.utc) - recorded_at).total_seconds())
        if intent_age > intent_max_age:
            log.info(
                "executor_apply.intent_stale",
                incident_id=incident_id,
                intent_age_seconds=intent_age,
                max_age_seconds=intent_max_age,
                time_anchor=anchor,
            )
            return _refuse(incident_id, f"intent_stale:{intent_age}s", applied_by)

        # ── Детерминированный policy-gate ──────────────────────────────
        # Финальный серверный рубеж: пересчитываем риск из самого intent-а
        # (namespace/kind/replicas — структурные поля, не свободный LLM-текст)
        # и блокируем prod/system/data-plane/необратимое. LLM-`risk` выше —
        # лишь advisory; этот gate модель обойти prompt-injection'ом не может.
        from app.remediation.executor_gate import (PolicyMode,
                                                   evaluate_intent_gate)

        gate = evaluate_intent_gate(intent)
        gate_dict = gate.to_dict()
        if gate.mode == PolicyMode.BLOCK:
            reason_code = "no_match"
            if gate.reasons:
                first = gate.reasons[0]
                reason_code = first.get("axis") or first.get("rule") or "no_match"
            audit_service.log_event(
                "EXECUTOR_APPLY_REFUSED_POLICY",
                {
                    "incident_id": incident_id,
                    "applied_by": applied_by,
                    "policy_decision": gate_dict,
                },
            )
            return _refuse(incident_id, f"policy_block:{reason_code}", applied_by)

        # ── Пере-dry-run по ТЕКУЩЕМУ состоянию кластера (M1в) ───────────
        # executor_result.dry_run_ok в analysis — снимок момента прогона
        # пайплайна. Между ним и кликом deployment мог быть удалён,
        # переименован, пересоздан другим владельцем или отскейлен вручную.
        # Прогоняем ровно ту же команду через kubectl --dry-run=server
        # (kube-apiserver валидирует, ничего не пишет) непосредственно перед
        # реальным write; провал = отказ, write не выполняется. Делаем это ДО
        # claim-а: read-only проверка не должна оставлять in-flight маркер.
        recheck = k8s_service.execute_intent(intent, dry_run=True)
        if not recheck.get("success"):
            detail = str(
                recheck.get("error")
                or (recheck.get("stderr") or "").strip()
                or f"exit_code={recheck.get('exit_code')}"
            )[:160]
            audit_service.log_event(
                "EXECUTOR_APPLY_DRY_RUN_RECHECK_FAILED",
                {
                    "incident_id": incident_id,
                    "applied_by": applied_by,
                    "command": recheck.get("command"),
                    "detail": detail,
                },
            )
            return _refuse(incident_id, f"dry_run_recheck_failed:{detail}", applied_by)

        # ── Фаза 1: claim ПЕРЕД kubectl ────────────────────────────────
        # Мутация кластера и запись о ней раньше были в одной транзакции:
        # краш/таймаут между execute_intent и commit оставлял кластер
        # мутированным, а executor_applied — незаписанным → живая кнопка
        # могла применить повторно. Коммитим claim ДО kubectl: любой
        # конкурент/ретрай в окне выполнения видит свежий executor_in_flight
        # и получает apply_in_flight. Commit также отпускает row-lock —
        # дальше от гонок защищает сам claim.
        # Верификация, шаг 1: та же ли цель. Снимок живого объекта до записи
        # и сверка uid с target_ref из графа (kg_remediation_decisions). Не
        # совпал — объект пересоздан после инцидента, действие отказано:
        # «починили Deployment, которого уже нет» выглядит успехом, и потому
        # хуже любого отказа. Сверить нечем (uid не записан, kubectl
        # недоступен) — идём дальше, но identity_check честно говорит unknown.
        expected_target = expected_identity(db, incident_id)
        target_before = snapshot_target(intent)
        mismatch = identity_mismatch(expected_target, target_before)
        if mismatch:
            audit_service.log_event(
                "EXECUTOR_APPLY_REFUSED_TARGET_REINCARNATED",
                {
                    "incident_id": incident_id,
                    "applied_by": applied_by,
                    "expected": expected_target,
                    "live": target_before.to_dict(),
                },
            )
            return _refuse(incident_id, f"target_reincarnated:{mismatch}", applied_by)
        analysis["executor_in_flight"] = {
            "claimed_at": datetime.now(timezone.utc).isoformat(),
            "claimed_by": applied_by,
            # Provenance: по какому плану и какой свежести идёт write.
            "intent_recorded_at": recorded_at.isoformat(),
            "intent_time_anchor": anchor,
        }
        record.analysis = analysis
        # SQLAlchemy не видит in-place изменения внутри JSON Column без явного флага.
        flag_modified(record, "analysis")
        db.commit()

        # ── Фаза 2: выполнение ─────────────────────────────────────────
        result = k8s_service.execute_intent(intent, dry_run=False, post_approval=True)
        # Верификация, шаг 2: сработало ли — снимок сразу после записи.
        target_after = snapshot_target(intent)

        # ── Финализация: persist результата, claim снимается ───────────
        applied_entry = {
            "applied_at": datetime.now(timezone.utc).isoformat(),
            "applied_by": applied_by,
            # Возраст плана на момент write + чем он был измерен: без этого
            # post-mortem не отличит «применили свежий разбор» от «применили
            # вчерашний» (явного created_at у intent-а в analysis нет).
            "intent_recorded_at": recorded_at.isoformat(),
            "intent_time_anchor": anchor,
            "intent_age_seconds": intent_age,
            "pre_write_dry_run": {
                "command": recheck.get("command"),
                "exit_code": recheck.get("exit_code"),
            },
            "result": {
                "success": result.get("success", False),
                "command": result.get("command"),
                "stdout": (result.get("stdout") or "")[:2048],
                "stderr": (result.get("stderr") or "")[:2048],
                "exit_code": result.get("exit_code"),
                "error": result.get("error"),
            },
            "policy_decision": gate_dict,
        }
        applied_entry["target_expected"] = expected_target
        applied_entry["target_before"] = target_before.to_dict()
        applied_entry["target_after"] = target_after.to_dict()
        applied_entry["identity_check"] = identity_check(expected_target, target_before)
        # Верификация, шаг 3: стало ли лучше — отложенная проверка (Celery,
        # через 5 и 15 минут). Ставится до финализирующего commit'а, чтобы
        # запись осталась двухфазной (claim → финал); задержка в сотни секунд
        # делает гонку с commit'ом теоретической, а если commit всё же упадёт,
        # задача честно ответит nothing_applied → unknown.
        applied_entry["verification"] = (
            schedule_verification(incident_id, attempt=1)
            if result.get("success", False)
            else {"scheduled": False, "reason": "apply_failed"}
        )
        analysis["executor_applied"] = applied_entry
        analysis.pop("executor_in_flight", None)
        record.analysis = analysis
        flag_modified(record, "analysis")
        db.commit()

        audit_service.log_event(
            "EXECUTOR_APPLIED",
            {
                "incident_id": incident_id,
                "applied_by": applied_by,
                "command": result.get("command"),
                "success": result.get("success", False),
                "policy_decision": gate_dict,
                # По какому по свежести плану сработал write (audit-трейл).
                "intent_age_seconds": intent_age,
                "intent_time_anchor": anchor,
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
        # Не должны падать наружу из апровал-хэндлера. NB: если исключение
        # случилось ПОСЛЕ commit-а claim-а (фаза 2) — rollback его не снимает,
        # это осознанно: состояние кластера неизвестно, повторный apply
        # блокируется apply_in_flight до истечения EXECUTOR_IN_FLIGHT_TTL.
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
