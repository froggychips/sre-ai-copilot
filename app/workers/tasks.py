from celery import Celery
from app.config import settings
from app.agents.analyzer import AnalyzerAgent
from app.agents.hypothesis import HypothesisAgent
from app.agents.critic import CriticAgent
from app.agents.fix import FixAgent
from app.agents.risk import RiskAgent
from app.services.discord_service import discord_service
from app.services.audit_logger import audit_service
from app.database import SessionLocal, IncidentRecord
from app.models.incident import NewRelicIncident
import asyncio
import logging

celery_app = Celery(
    "sre_tasks",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL
)

@celery_app.task(name="process_incident", bind=True, max_retries=3)
def process_incident_task(self, incident_data: dict):
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(async_process_incident(incident_data))

async def async_process_incident(incident_data: dict):
    db = SessionLocal()
    incident_id = incident_data.get("incident_id")
    audit_service.log_event("CELERY_START", {"incident_id": incident_id})

    try:
        incident = NewRelicIncident(**incident_data)
        
        # Agents chain
        analyzer = AnalyzerAgent()
        analysis = await analyzer.analyze(incident)
        
        hypo = HypothesisAgent()
        hypotheses = await hypo.generate(analysis)
        
        critic = CriticAgent()
        final_cause = await critic.audit(analysis, hypotheses)
        
        fixer = FixAgent()
        fix_suggestion = await fixer.suggest(final_cause)
        
        risker = RiskAgent()
        risk_report = await risker.assess(fix_suggestion)

        # Persistence
        record = db.query(IncidentRecord).filter(IncidentRecord.incident_id == incident_id).first()
        if record:
            record.analysis = {
                "summary": analysis,
                "cause": final_cause,
                "fix": fix_suggestion,
                "risk": risk_report
            }
            record.status = "COMPLETED"
            db.commit()

        # Notification
        await discord_service.send_report(f"**Incident {incident_id} Analysis Complete.**\nRisk: {risk_report}")
        
    except Exception as e:
        db.rollback()
        raise e
    finally:
        db.close()
