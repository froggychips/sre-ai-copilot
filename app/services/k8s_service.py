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

import logging
import subprocess
from typing import Any, Dict, Optional, Tuple

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


def _intent_to_operation(intent: ExecutionIntent) -> K8sOperation:
    """Преобразовать структурный ExecutionIntent в K8sOperation для guard-а."""
    if intent.action not in _ACTION_OP_MAP:
        raise ValueError(f"Unsupported action type for guard: {intent.action!r}")
    verb, default_resource = _ACTION_OP_MAP[intent.action]
    # DESCRIBE_RESOURCE может покрывать pod/deployment/service/ingress —
    # берём resource_type из intent (уже валидирован pydantic-схемой:
    # ^(deployment|pod|service|ingress)$).
    resource = (default_resource or intent.resource_type.lower() + "s")
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

        command = DSLTranslator.to_kubectl(intent)
        return self._run_kubectl(
            command,
            dry_run=dry_run,
            post_approval=post_approval,
            risk=intent.risk,
        )

    def _run_kubectl(
        self,
        command: str,
        dry_run: bool,
        post_approval: bool,
        risk: str = "medium",
    ) -> Dict[str, Any]:
        """Запустить kubectl-команду (с возможным --dry-run=server)."""
        if not command.startswith("kubectl"):
            return {"success": False, "error": "Invalid command. Must be kubectl."}

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

        full_cmd = command.split()
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

    # ── Backward compat: старая run_command-обёртка ────────────────────────
    # Используется в discord_service.send_approval_request URL-генерации; ничего
    # критичного в коде её больше не вызывает. Оставляем заглушкой на случай
    # если внешние интеграции дёрнут.
    def run_command(
        self,
        command: str,
        risk_level: str = "MEDIUM",
        dry_run: bool = True,
        body: Optional[dict] = None,
    ) -> Dict[str, Any]:
        logging.warning(
            "K8sService.run_command — deprecated; use execute_intent(intent) instead"
        )
        return self._run_kubectl(
            command, dry_run=dry_run, post_approval=False, risk=risk_level
        )


k8s_service = K8sService()
