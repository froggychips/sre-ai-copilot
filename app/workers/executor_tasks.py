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
TASK_APPLY_WATCHDOG = "remediation_apply_watchdog"


def queue_dispatch_enabled() -> bool:
    """True — dry-run и apply уходят в очередь copilot-executor."""
    return settings.EXECUTOR_DISPATCH == "queue"


# ── отметка «executor взял задачу apply» ─────────────────────────────────
# Нужна сторожу `remediation_apply_watchdog`: задача с истёкшим `expires`
# отбрасывается Celery без единой строки нашего кода, а потерянная
# (acks_late=False, смерть executor-а до старта) не оставляет следа вовсе.
_PICKED_PREFIX = "copilot:executor_apply:picked:"
_PICKED_TTL_SECONDS = 86400
#: Запас сторожа поверх `expires`: задача, взятая на последней секунде,
#: успевает поставить отметку.
_WATCHDOG_GRACE_SECONDS = 30


def _redis() -> Any:
    try:
        import redis
        return redis.from_url(settings.REDIS_URL, socket_timeout=2,
                              socket_connect_timeout=2)
    except Exception:
        return None


def _mark_picked(incident_id: str, intent_sig: str) -> None:
    r = _redis()
    if r is None:
        return
    try:
        r.set(f"{_PICKED_PREFIX}{incident_id}:{intent_sig}", "1", ex=_PICKED_TTL_SECONDS)
    except Exception as e:
        logger.warning("executor_apply_task.mark_picked_failed error=%s", type(e).__name__)


def _was_picked(incident_id: str, intent_sig: str) -> Optional[bool]:
    """True/False — отметка есть/нет; None — Redis недоступен, не знаем."""
    r = _redis()
    if r is None:
        return None
    try:
        return bool(r.exists(f"{_PICKED_PREFIX}{incident_id}:{intent_sig}"))
    except Exception:
        return None


def apply_expires_seconds() -> int:
    """Срок годности задачи apply в очереди, сек. 0 — без срока."""
    try:
        return max(0, int(getattr(settings, "EXECUTOR_APPLY_TASK_EXPIRES_SECONDS", 600)))
    except (TypeError, ValueError):
        return 600


def _report_to_card(
    incident_id: str, outcome: Dict[str, Any], *, only_if_absent: bool = False,
) -> None:
    """Итог apply → в карточку моста (bridge.report_apply_outcome).

    Для инцидентов не из моста — no-op. Наружу не бросает: сбой правки
    карточки не должен менять результат задачи, kubectl уже отработал.
    """
    try:
        from app.remediation.bridge import report_apply_outcome
        report_apply_outcome(incident_id, outcome, only_if_absent=only_if_absent)
    except Exception as e:
        logger.warning(
            "executor_apply_task.card_report_failed incident=%s error=%s",
            incident_id, type(e).__name__,
        )


def apply_and_report(incident_id: str, applied_by: str, intent_sig: str) -> Dict[str, Any]:
    """`apply_intent` + итог в карточку моста — для inline-пути api.

    При EXECUTOR_DISPATCH=inline apply исполняет фоновая нить api
    (`asyncio.to_thread`), задачи executor-а нет — итог в карточку дописывает
    она же. Sync и с сетью: звать только из нити, не из event loop.
    """
    from app.services.executor_apply import apply_intent

    outcome = apply_intent(incident_id, applied_by, intent_sig)
    _report_to_card(incident_id, outcome)
    return outcome


# acks_late=False — намеренно, вопреки глобальному task_acks_late=True
# (см. «Late acknowledgement» в tasks.py). Для apply нужна семантика «не
# больше одного раза»: переотправка после смерти executor-а посреди kubectl
# повторила бы запись в кластер. claim в apply_intent такой повтор тоже
# отобьёт (свежий — apply_in_flight, протухший — unknown без второй записи),
# но полагаться на второй рубеж там, где первый ставится одной строкой,
# незачем. Потерянная задача видна оператору: сторож
# `remediation_apply_watchdog` допишет в карточку моста, что executor задачу
# не взял, а в audit-логе не будет EXECUTOR_APPLIED.
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
    """Выполнить утверждённый intent и сообщить итог оператору.

    Итог уходит двумя путями: followup по interaction token (если он есть —
    путь Apply → «Да, запустить kubectl») и правкой карточки моста ботом
    (если инцидент создан мостом; токен клика для этого не нужен).
    """
    from app.services.audit_logger import audit_service
    from app.services.executor_apply import apply_intent

    _mark_picked(incident_id, intent_sig)
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
    _report_to_card(incident_id, outcome)
    return _json_safe_outcome(outcome)


@celery_app.task(name=TASK_APPLY_WATCHDOG, ignore_result=True)
def apply_watchdog_task(incident_id: str, intent_sig: str) -> Dict[str, Any]:
    """Сторож: взял ли executor задачу apply до истечения её `expires`.

    Ставится `dispatch_apply` в обычную очередь (`celery`, copilot-worker) с
    задержкой `expires + запас`. Отметки «взял» нет — задача протухла в
    очереди или потеряна: kubectl не запускался, и оператор должен узнать
    об этом из карточки, а не из тишины. Только чтение Redis и правка
    карточки; в кластер не пишет. Redis недоступен — не знаем, молчим.
    """
    picked = _was_picked(incident_id, intent_sig)
    if picked is None:
        return {"picked": None}
    if picked:
        return {"picked": True}
    from app.services.audit_logger import audit_service
    audit_service.log_event(
        "EXECUTOR_APPLY_NOT_PICKED_UP",
        {"incident_id": incident_id, "intent_signature": intent_sig},
    )
    logger.warning("executor_apply.not_picked_up incident=%s", incident_id)
    # only_if_absent: отметка могла потеряться (Redis моргнул у executor-а),
    # а задача — отработать и записать настоящий итог; его не перетираем.
    _report_to_card(incident_id, {"ok": False, "reason": "task_expired"}, only_if_absent=True)
    return {"picked": False}


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
    """Поставить apply в очередь executor. Возвращает id задачи.

    Sync publish в Redis-брокер: из async-обработчика звать через
    `run_in_threadpool`. Задача несёт `expires`
    (EXECUTOR_APPLY_TASK_EXPIRES_SECONDS): простоявшую дольше Celery
    отбросит, а сторож (`apply_watchdog_task`) сообщит об этом в карточку.
    Сторож ставится best-effort: его сбой не отменяет уже поставленный apply.
    """
    options: Dict[str, Any] = {"queue": settings.EXECUTOR_QUEUE_NAME}
    expires = apply_expires_seconds()
    if expires > 0:
        options["expires"] = expires
    res = executor_apply_task.apply_async(
        args=[incident_id, applied_by, intent_sig, interaction_token], **options,
    )
    if expires > 0:
        try:
            # retry=False: apply уже в очереди, а зовут нас из обработчика
            # клика (threadpool) — не висим на повторах publish ради сторожа.
            apply_watchdog_task.apply_async(
                args=[incident_id, intent_sig],
                countdown=expires + _WATCHDOG_GRACE_SECONDS,
                retry=False,
            )
        except Exception as e:
            logger.warning(
                "executor_apply.watchdog_schedule_failed incident=%s error=%s",
                incident_id, type(e).__name__,
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
