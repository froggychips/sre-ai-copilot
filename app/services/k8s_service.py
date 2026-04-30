import subprocess
from typing import List, Dict, Any
import logging
from app.config import settings
from app.services.audit_logger import audit_service
from app.services.k8s_guard import k8s_guard, K8sOperation

class K8sService:
    def __init__(self):
        self.safe_mode = settings.SAFE_MODE

    def run_command(self, command: str, risk_level: str = "MEDIUM", dry_run: bool = True, body: dict = None) -> Dict[str, Any]:
        """
        Executes a kubectl command with mandatory safety checks and auditing.
        """
        if not command.startswith("kubectl"):
            return {"success": False, "error": "Invalid command. Must be kubectl."}

        # 1. Парсинг параметров для Guardrails
        cmd_parts = command.split()
        verb = cmd_parts[1] if len(cmd_parts) > 1 else "get"
        resource = cmd_parts[2] if len(cmd_parts) > 2 else "pods"
        namespace = "default"
        if "-n" in cmd_parts: namespace = cmd_parts[cmd_parts.index("-n") + 1]
        elif "--namespace" in cmd_parts: namespace = cmd_parts[cmd_parts.index("--namespace") + 1]

        # 2. ПРОВЕРКА БЕЗОПАСНОСТИ (Guardrails)
        try:
            k8s_guard.validate(K8sOperation(
                verb=verb, 
                resource=resource, 
                namespace=namespace, 
                body=body
            ))
        except PermissionError as e:
            return {"success": False, "error": f"GUARDRAIL_BLOCK: {str(e)}"}

        # Forced Dry-run logic
        destructive_keywords = ["delete", "patch", "apply", "scale", "restart"]
...

        
        full_cmd = command.split()
        
        if is_destructive or dry_run:
            if "--dry-run" not in command:
                full_cmd.append("--dry-run=server")
            logging.info(f"SAFETY: Forcing dry-run for command: {command}")

        # Audit before execution
        audit_service.log_event("K8S_COMMAND_ATTEMPT", {
            "command": command,
            "risk_level": risk_level,
            "is_destructive": is_destructive,
            "dry_run": dry_run
        })

        if not dry_run and self.safe_mode and settings.APPROVAL_REQUIRED:
            return {"success": False, "error": "SAFE_MODE: Manual approval required."}

        try:
            result = subprocess.run(full_cmd, capture_output=True, text=True, check=False)
            status = result.returncode == 0
            
            audit_service.log_event("K8S_COMMAND_RESULT", {
                "command": command,
                "success": status,
                "exit_code": result.returncode
            })

            return {
                "success": status,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "command": ' '.join(full_cmd)
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

k8s_service = K8sService()
