import structlog
import json
from datetime import datetime
from app.config import settings

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer()
    ],
    logger_factory=structlog.WriteLoggerFactory(
        file=open(settings.AUDIT_LOG_PATH, "a")
    ),
)

audit_logger = structlog.get_logger("sre_audit")

class AuditService:
    def log_event(self, event_type: str, details: dict):
        audit_logger.info(
            event_type=event_type,
            timestamp=datetime.utcnow().isoformat(),
            **details
        )

audit_service = AuditService()
