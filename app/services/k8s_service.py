import subprocess
from typing import List, Dict, Any
import logging
from app.config import settings

class K8sService:
    def __init__(self):
        self.safe_mode = settings.SAFE_MODE

    def run_command(self, command: str, dry_run: bool = True) -> Dict[str, Any]:
        """
        Executes a kubectl command safely.
        """
        if not command.startswith("kubectl"):
            return {"success": False, "error": "Invalid command. Must be kubectl."}

        full_cmd = command.split()
        
        if dry_run:
            full_cmd.append("--dry-run=server")
            logging.info(f"Executing DRY-RUN: {' '.join(full_cmd)}")
        else:
            if self.safe_mode and settings.APPROVAL_REQUIRED:
                return {"success": False, "error": "Safe mode active. Manual approval required for non-dry-run."}
            logging.info(f"Executing PRODUCTION command: {' '.join(full_cmd)}")

        try:
            result = subprocess.run(
                full_cmd, 
                capture_output=True, 
                text=True, 
                check=False
            )
            return {
                "success": result.returncode == 0,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "command": ' '.join(full_cmd)
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

k8s_service = K8sService()
