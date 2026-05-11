from celery import Celery
from app.config import settings
from app.agents.analyzer import AnalyzerAgent
from app.agents.hypothesis import HypothesisAgent
from app.agents.critic import CriticAgent
from app.agents.fix import FixAgent
from app.agents.risk import RiskAgent
from app.agents.synthesis import SynthesisAgent
from app.services.discord_service import discord_service
from app.services.audit_logger import audit_service
from app.database import SessionLocal, IncidentRecord
from app.models.incident import Incident
from app.core.tracing import StageTimer
from app.core.intelligence.similar_incidents import SimilarIncidentEngine
import asyncio

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

    # Per-stage execution trace accumulated across the pipeline; written to
    # IncidentRecord.trace at the end so post-mortem reads timings and LLM
    # backends straight from the row.
    traces: list[dict] = []

    try:
        incident = Incident(**incident_data)

        # Stage 1: Analyzer
        async with StageTimer("analyzer") as t:
            analyzer = AnalyzerAgent()
            analysis = await analyzer.analyze(incident)
        traces.append(t.snapshot().to_dict())

        # Stage 2: Hypothesis — augmented with up to 3 past ACCEPTED resolutions
        # matching by service/cause/namespace (deterministic scoring inside
        # SimilarIncidentEngine — no LLM call here).
        similar_past = SimilarIncidentEngine.find(current_incident=incident_data, limit=3)
        async with StageTimer("hypothesis") as t:
            hypo = HypothesisAgent()
            hypotheses = await hypo.generate(analysis, similar_past=similar_past)
        traces.append(t.snapshot().to_dict())

        # Stage 3: Critic
        async with StageTimer("critic") as t:
            critic = CriticAgent()
            final_cause = await critic.audit(analysis, hypotheses, namespace=incident.namespace)
        traces.append(t.snapshot().to_dict())

        # Stage 4: Fix
        async with StageTimer("fix") as t:
            fixer = FixAgent()
            fix_suggestion = await fixer.suggest(final_cause)
        traces.append(t.snapshot().to_dict())

        # Stage 5: Risk
        async with StageTimer("risk") as t:
            risker = RiskAgent()
            risk_report = await risker.assess(fix_suggestion)
        traces.append(t.snapshot().to_dict())

        # Stage 6: Synthesis — sees all 5 outputs simultaneously (two-level reasoning)
        async with StageTimer("synthesis") as t:
            synthesizer = SynthesisAgent()
            synthesis = await synthesizer.synthesize(
                incident_id=incident_id,
                analysis=analysis,
                hypotheses=hypotheses,
                final_cause=final_cause,
                fix_suggestion=fix_suggestion,
                risk_report=risk_report,
            )
        traces.append(t.snapshot().to_dict())

        # Persistence — analysis bundle plus the per-stage trace and the
        # similar-past matches that fed into Hypothesis. Both fields are
        # JSON columns; existing rows without them stay backward-compatible.
        record = db.query(IncidentRecord).filter(IncidentRecord.incident_id == incident_id).first()
        if record:
            record.analysis = {
                "summary": analysis,
                "hypotheses": hypotheses,
                "cause": final_cause,
                "fix": fix_suggestion,
                "risk": risk_report,
                "synthesis": synthesis,
                "similar_past_count": len(similar_past),
            }
            record.trace = traces
            record.status = "COMPLETED"
            db.commit()

        # Notification — send synthesized report, not raw stage output
        await discord_service.send_report(
            f"**Incident {incident_id} Analysis Complete.**\n\n{synthesis}"
        )

    except Exception as e:
        db.rollback()
        raise e
    finally:
        db.close()
