from typing import List, Dict

class NextStepsGenerator:
    MAPPING = {
        "OOM": [
            "Inspect pod memory limits and requests",
            "Check application heap usage / GC logs",
            "Verify if a recent deploy increased memory footprint"
        ],
        "App Crash": [
            "Inspect container logs for panic or uncaught exceptions",
            "Verify environment variables and secrets",
            "Check liveness/readiness probe configurations"
        ],
        "ImagePullBackOff": [
            "Verify container registry credentials",
            "Check if the image tag exists in the registry",
            "Inspect node connectivity to the registry"
        ]
    }

    @classmethod
    def generate(cls, root_cause: str) -> List[str]:
        """
        Возвращает список действий на основе детерминированного маппинга причин.
        """
        return cls.MAPPING.get(root_cause, ["Consult SRE playbooks", "Investigate service logs"])
