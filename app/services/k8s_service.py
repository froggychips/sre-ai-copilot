import logging
import subprocess
from typing import Any, Dict, Optional

from app.config import settings
from app.core.execution_dsl import DSLTranslator, ExecutionIntent
from app.services.audit_logger import audit_service
from app.services.k8s_guard import K8sOperation, k8s_guard


class K8sService:
    def __init__(self):
        self.safe_mode = settings.SAFE_MODE

    def execute_intent(self, intent: ExecutionIntent, dry_run: bool = True) -> Dict[str, Any]:
        """Translate execution intent to kubectl command and execute it safely."""
        command = DSLTranslator.to_kubectl(intent)
        return self.run_command(command, risk_level=intent.risk, dry_run=dry_run)

    def run_command(
        self,
        command: str,
        risk_level: str = "MEDIUM",
        dry_run: bool = True,
        body: Optional[dict] = None,
    ) -> Dict[str, Any]:
        """Execute kubectl command with guardrails, audit logging and forced dry-run for risky actions."""
        if not command.startswith("kubectl"):
            return {"success": False, "error": "Invalid command. Must be kubectl."}

        cmd_parts = command.split()
        verb = cmd_parts[1] if len(cmd_parts) > 1 else "get"
        resource = cmd_parts[2] if len(cmd_parts) > 2 else "pods"
        namespace = "default"
        if "-n" in cmd_parts and len(cmd_parts) > cmd_parts.index("-n") + 1:
            namespace = cmd_parts[cmd_parts.index("-n") + 1]
        elif "--namespace" in cmd_parts and len(cmd_parts) > cmd_parts.index("--namespace") + 1:
            namespace = cmd_parts[cmd_parts.index("--namespace") + 1]

        try:
            k8s_guard.validate(K8sOperation(verb=verb, resource=resource, namespace=namespace, body=body))
        except PermissionError as e:
            return {"success": False, "error": f"GUARDRAIL_BLOCK: {str(e)}"}

        destructive_keywords = ["delete", "patch", "apply", "scale", "restart"]
        is_destructive = any(k in cmd_parts for k in destructive_keywords)

        full_cmd = cmd_parts.copy()
        if is_destructive or dry_run:
            if not any(flag.startswith("--dry-run") for flag in full_cmd):
                full_cmd.append("--dry-run=server")
            logging.info("SAFETY: Forcing dry-run for command: %s", command)

        audit_service.log_event(
            "K8S_COMMAND_ATTEMPT",
            {
                "command": command,
                "risk_level": risk_level,
                "is_destructive": is_destructive,
                "dry_run": dry_run,
            },
        )

        if not dry_run and self.safe_mode and settings.APPROVAL_REQUIRED:
            return {"success": False, "error": "SAFE_MODE: Manual approval required."}

        try:
            result = subprocess.run(full_cmd, capture_output=True, text=True, check=False)
            status = result.returncode == 0
            audit_service.log_event(
                "K8S_COMMAND_RESULT",
                {
                    "command": command,
                    "success": status,
                    "exit_code": result.returncode,
                },
            )
            return {
                "success": status,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "command": " ".join(full_cmd),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


k8s_service = K8sService()
