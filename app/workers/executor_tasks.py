"""Задачи executor-а, которым нужны write-права в кластере.

Отдельный модуль и отдельная очередь (`settings.EXECUTOR_QUEUE_NAME`) — чтобы
write-роль Kubernetes можно было выдать ОДНОМУ процессу. До 24.09.2026 apply
шёл прямо из api-пода (обработчик Discord-кнопки, `asyncio.to_thread`), а
server-side dry-run — из worker-а; оба бегали под общим SA `sre-ai`. Значит
write-роль, привязанная к нему, доставалась и api, принимающему вебхуки из
интернета, и worker-у, который гоняет LLM по логам и тексту алертов.

Теперь при `EXECUTOR_DISPATCH=queue`:
  * api и worker только КЛАДУТ задачу в очередь executor;
  * слушает её deployment `copilot-executor`
    (`celery ... worker -Q executor`) под SA `sre-ai-executor`;
  * write-роль `sre-ai-remediate` привязана только к этому SA.

Все проверки допустимости apply остаются внутри `apply_intent` — задача их
не дублирует и не обходит: подпись, одобрение, свежесть, namespace-binding,
детерминированный gate, пере-dry-run, claim.

Регистрация — импортом из конца `app.workers.tasks`, на том же celery_app:
иначе worker, запущенный с `-A app.workers.tasks.celery_app`, этих задач не
знает.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from app.config import settings
from app.workers.tasks import celery_app

logger = logging.getLogger(__name__)

TASK_EXECUTOR_APPLY = "executor_apply"
TASK_EXECUTOR_DRY_RUN = "executor_dry_run"


def queue_dispatch_enabled() -> bool:
    """True — dry-run и apply уходят в очередь copilot-executor."""
    return settings.EXECUTOR_DISPATCH == "queue"


# acks_late=False — намеренно, вопреки глобальному task_acks_late=True
# (см. «Late acknowledgement» в tasks.py). Для apply нужна семантика «не
# больше одного раза»: переотправка после смерти executor-а посреди kubectl
# повторила бы запись в кластер. claim в apply_intent такой повтор тоже
# отобьёт (свежий — apply_in_flight, протухший — unknown без второй записи),
# но полагаться на второй рубеж там, где первый ставится одной строкой,
# незачем. Потерянная задача видна оператору: followup не придёт, а в
# audit-логе не будет EXECUTOR_APPLIED.
@celery_app.task(
    name=TASK_EXECUTOR_APPLY,
    acks_late=False,
    reject_on_worker_lost=False,
)
def executor_apply_task(
    incident_id: str,
    applied_by: str,
    intent_sig: str,
    interaction_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Выполнить утверждённый intent и, если есть token, ответить в Discord."""
    from app.services.audit_logger import audit_service
    from app.services.executor_apply import apply_intent

    try:
        outcome = apply_intent(incident_id, applied_by, intent_sig)
    except Exception as e:
        # Тот же след, что оставлял done-callback фоновой таски в api:
        # упавший apply не должен исчезать молча.
        logger.error(
            "executor_apply_task.failed incident=%s error=%s",
            incident_id, repr(e), exc_info=True,
        )
        audit_service.log_event(
            "DISCORD_BACKGROUND_TASK_FAILED",
            {"kind": "executor_apply_task", "incident_id": incident_id,
             "error": type(e).__name__},
        )
        outcome = {"ok": False, "reason": "execute_error", "error": type(e).__name__}

    if interaction_token:
        from app.api.discord_interactions import (_format_apply_outcome,
                                                  _send_followup)
        asyncio.run(_send_followup(
            interaction_token, _format_apply_outcome(incident_id, outcome),
        ))
    return _json_safe_outcome(outcome)


@celery_app.task(name=TASK_EXECUTOR_DRY_RUN)
def executor_dry_run_task(intent_json: Dict[str, Any]) -> Dict[str, Any]:
    """`kubectl ... --dry-run=server` для intent-а из стадии пайплайна.

    Intent приходит JSON-ом и валидируется заново: доверять форме, собранной
    в другом процессе, незачем. Guard (K8sSecurityGuard) стоит первым шагом
    внутри execute_intent — как и при inline-вызове.
    """
    from app.core.execution_dsl import ExecutionIntent
    from app.services.k8s_service import k8s_service

    intent = ExecutionIntent.model_validate(intent_json)
    return k8s_service.execute_intent(intent, True)


def run_dry_run_via_queue(intent_json: Dict[str, Any]) -> Dict[str, Any]:
    """Поставить dry-run в очередь executor и дождаться результата (sync).

    Зовётся из стадии пайплайна через asyncio.to_thread. Таймаут
    (EXECUTOR_DRY_RUN_TIMEOUT_SECONDS) бросает исключение — стадия
    превращает его в executor_result.status='error', и кнопки Apply нет.

    `disable_sync_subtasks=False`: ожидание результата ИЗ задачи Celery по
    умолчанию запрещено из-за дедлока, когда обе задачи делят один пул.
    Здесь пулы разные — pipeline в copilot-worker, dry-run в
    copilot-executor, — а не поднятый executor упирается в таймаут, а не в
    вечное ожидание.
    """
    result = executor_dry_run_task.apply_async(
        args=[intent_json], queue=settings.EXECUTOR_QUEUE_NAME,
    )
    try:
        return result.get(
            timeout=settings.EXECUTOR_DRY_RUN_TIMEOUT_SECONDS,
            disable_sync_subtasks=False,
        )
    finally:
        # Результат нужен ровно один раз; без forget он висит в Redis
        # result-backend до result_expires.
        result.forget()


def dispatch_apply(
    incident_id: str,
    applied_by: str,
    intent_sig: str,
    interaction_token: Optional[str] = None,
) -> str:
    """Поставить apply в очередь executor. Возвращает id задачи."""
    res = executor_apply_task.apply_async(
        args=[incident_id, applied_by, intent_sig, interaction_token],
        queue=settings.EXECUTOR_QUEUE_NAME,
    )
    return str(res.id)


def _json_safe_outcome(outcome: Dict[str, Any]) -> Dict[str, Any]:
    """Результат задачи уходит в Redis JSON-ом — берём только сериализуемое."""
    safe: Dict[str, Any] = {"ok": bool(outcome.get("ok"))}
    for key in ("reason", "error"):
        if outcome.get(key) is not None:
            safe[key] = str(outcome[key])
    result = outcome.get("result")
    if isinstance(result, dict):
        safe["success"] = bool(result.get("success"))
        if result.get("command") is not None:
            safe["command"] = str(result["command"])
    return safe
