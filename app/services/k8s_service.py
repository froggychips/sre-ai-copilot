"""K8sService — executor для ExecutionIntent.

Архитектура (после PR #3 executor track):
  - execute_intent(intent, dry_run, post_approval) — единственная public-точка.
  - Guard валидирует НЕ распарсенную строку kubectl, а структурный
    ExecutionIntent: ActionType → (k8s-verb, k8s-resource). Это исправляет
    pre-existing баг с `kubectl rollout restart`, у которого
    cmd_parts[2]="restart" формально не в ALLOWED_RESOURCES, хотя сам
    патч аннотации deployment-а — валидный write на deployments.
  - dry_run=True → `kubectl ... --dry-run=server` (kube-apiserver
    валидирует команду, ничего не пишет). Безопасно для пайплайн-стадии.
  - dry_run=False — ТОЛЬКО из утверждённого approval-flow:
    post_approval=True bypass-ит SAFE_MODE-блокировку. Без этого
    флага и при SAFE_MODE=true APPROVAL_REQUIRED=true execute-вызов
    отказывает "Manual approval required."
"""
from __future__ import annotations

import subprocess
from typing import Any, Dict, List, Tuple

from app.config import settings
from app.core.execution_dsl import ActionType, DSLTranslator, ExecutionIntent
from app.services.audit_logger import audit_service
from app.services.k8s_guard import K8sOperation, k8s_guard


# ActionType → (verb, resource) для guard-валидации.
# Это семантическое отображение, а не парсинг kubectl-строки.
# `rollout restart` физически патчит аннотацию deployment-а — verb=patch
# с точки зрения политики и kube-API.
_ACTION_OP_MAP: Dict[ActionType, Tuple[str, str]] = {
    ActionType.RESTART_DEPLOYMENT: ("patch", "deployments"),
    ActionType.SCALE_DEPLOYMENT:   ("patch", "deployments"),
    ActionType.GET_LOGS:           ("get",   "pods"),
    ActionType.DESCRIBE_RESOURCE:  ("get",   ""),   # resource берём из intent.resource_type
    ActionType.GET_PODS:           ("list",  "pods"),
}


# resource_type (singular, из pydantic-схемы) → множественное имя ресурса для
# guard-а. Наивное `+ "s"` давало "ingresss" для ingress — такого имени нет в
# ALLOWED_RESOURCES, и весь describe-путь для ingress был молча заблокирован.
_RESOURCE_PLURAL: Dict[str, str] = {
    "deployment": "deployments",
    "pod":        "pods",
    "service":    "services",
    "ingress":    "ingresses",
}


def _intent_to_operation(intent: ExecutionIntent) -> K8sOperation:
    """Преобразовать структурный ExecutionIntent в K8sOperation для guard-а."""
    if intent.action not in _ACTION_OP_MAP:
        raise ValueError(f"Unsupported action type for guard: {intent.action!r}")
    verb, default_resource = _ACTION_OP_MAP[intent.action]
    # DESCRIBE_RESOURCE может покрывать pod/deployment/service/ingress —
    # берём resource_type из intent (уже валидирован pydantic-схемой:
    # ^(deployment|pod|service|ingress)$). Неизвестный тип → "" → guard
    # отклонит как resource-not-allowed (fail-closed).
    resource = (
        default_resource
        or _RESOURCE_PLURAL.get(intent.resource_type.lower(), "")
    )
    return K8sOperation(
        verb=verb,
        resource=resource,
        namespace=intent.namespace,
        name=intent.resource_name,
    )


class K8sService:
    def __init__(self) -> None:
        self.safe_mode = settings.SAFE_MODE

    def execute_intent(
        self,
        intent: ExecutionIntent,
        dry_run: bool = True,
        post_approval: bool = False,
    ) -> Dict[str, Any]:
        """Validate intent через guard и выполнить kubectl-команду.

        Возвращает:
          {"success": bool, "stdout": str, "stderr": str, "command": str,
           "exit_code": int, "dry_run": bool}
        либо ошибку:
          {"success": False, "error": "GUARDRAIL_BLOCK: <reason>" | "SAFE_MODE: ...",
           "dry_run": bool}
        """
        # Guard первым шагом — внутри испускается guardrail.blocked OTEL-event.
        try:
            op = _intent_to_operation(intent)
            k8s_guard.validate(op)
        except PermissionError as e:
            audit_service.log_event(
                "K8S_GUARDRAIL_BLOCK",
                {"intent": intent.model_dump(mode="json"), "error": str(e)},
            )
            return {
                "success": False,
                "error": f"GUARDRAIL_BLOCK: {e}",
                "dry_run": dry_run,
            }
        except ValueError as e:
            return {
                "success": False,
                "error": f"GUARDRAIL_BLOCK: {e}",
                "dry_run": dry_run,
            }

        # argv собирается из intent напрямую; строка идёт рядом только для
        # аудита и человекочитаемого вывода. Раньше строка была единственной
        # формой, и `.split()` восстанавливал аргументы наугад.
        return self._run_kubectl(
            DSLTranslator.to_argv(intent),
            dry_run=dry_run,
            post_approval=post_approval,
            risk=intent.risk,
        )

    def _run_kubectl(
        self,
        argv: List[str],
        dry_run: bool,
        post_approval: bool,
        risk: str = "medium",
    ) -> Dict[str, Any]:
        """Запустить kubectl (с возможным --dry-run=server). Принимает ТОЛЬКО argv.

        Строковой формы здесь больше нет намеренно. Раньше метод брал
        `command: str` и восстанавливал аргументы через `command.split()` —
        наивно, без понимания кавычек. Именно поэтому у полей
        `ExecutionIntent` стоят charset-регулярки: их докстринги прямо
        ссылаются на `split()` как на причину.

        Пока такой путь существует, он остаётся путём атаки — даже если
        сегодня по нему никто не ходит: достаточно одного нового вызывающего,
        собравшего команду строкой. Поэтому fallback убран целиком, а не
        помечен deprecated.

        `command` для аудита и вывода собирается здесь же из argv: одна форма
        порождает другую, разъехаться нечему.
        """
        if not argv or argv[0] != "kubectl":
            return {"success": False, "error": "Invalid command. Must be kubectl."}
        command = " ".join(argv)

        # SAFE_MODE-блок на реальный write без явного post_approval=True.
        # Это последний рубеж: даже если кто-то вызвал dry_run=False вне
        # approval-flow — отказываем.
        if not dry_run and self.safe_mode and settings.APPROVAL_REQUIRED and not post_approval:
            audit_service.log_event(
                "K8S_BLOCKED_NO_APPROVAL",
                {"command": command, "risk": risk},
            )
            return {
                "success": False,
                "error": "SAFE_MODE: Manual approval required.",
                "dry_run": False,
            }

        full_cmd = list(argv)
        if dry_run and not any(flag.startswith("--dry-run") for flag in full_cmd):
            full_cmd.append("--dry-run=server")

        audit_service.log_event(
            "K8S_COMMAND_ATTEMPT",
            {
                "command": command,
                "risk": risk,
                "dry_run": dry_run,
                "post_approval": post_approval,
            },
        )

        try:
            result = subprocess.run(  # nosec B603 — full_cmd начинается с "kubectl" и собирается из ExecutionIntent (pydantic-валидирован)
                full_cmd, capture_output=True, text=True, check=False, timeout=30
            )
            success = result.returncode == 0
            audit_service.log_event(
                "K8S_COMMAND_RESULT",
                {"command": command, "success": success, "exit_code": result.returncode},
            )
            return {
                "success": success,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "command": " ".join(full_cmd),
                "exit_code": result.returncode,
                "dry_run": dry_run,
            }
        except subprocess.TimeoutExpired as e:
            audit_service.log_event(
                "K8S_COMMAND_TIMEOUT",
                {"command": command, "timeout_s": getattr(e, "timeout", None)},
            )
            return {
                "success": False,
                "error": "kubectl_timeout",
                "dry_run": dry_run,
            }
        except FileNotFoundError:
            # kubectl binary не установлен — типично для local dev / тестов.
            return {
                "success": False,
                "error": "kubectl_binary_missing",
                "dry_run": dry_run,
            }
        except Exception as e:
            return {"success": False, "error": str(e), "dry_run": dry_run}

    # ── Удалено: legacy-обёртка run_command ────────────────────────────────
    # Была backward-compat заглушкой после выпила
    # discord_service.send_approval_request (chore/gc-legacy-discord-senders),
    # но вызывающих в репо не осталось (ни app/, ни app/scripts/, ни tests/) —
    # только устаревшее упоминание в докстринге stage_executor
    # (app/workers/pipeline.py), который утверждает, что guard вызывается
    # «внутри run_command». Фактически она шла СРАЗУ в _run_kubectl, т.е.
    # K8sSecurityGuard.validate не вызывался вообще: единственной защитой
    # оставался SAFE_MODE+APPROVAL_REQUIRED-гейт, а read-ветки (dry_run=True) и
    # write при SAFE_MODE=false проходили в кластер по произвольной
    # kubectl-строке. Мёртвый метод с такой поверхностью не «оставляем на
    # всякий случай»: единственная точка входа — execute_intent(intent), где
    # guard стоит первым шагом. Если внешней интеграции когда-нибудь
    # понадобится строковый вход — он обязан собирать ExecutionIntent.


k8s_service = K8sService()
