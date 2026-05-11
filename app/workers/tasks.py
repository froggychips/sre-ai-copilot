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
from app.core.state_machine import StateMachine, IncidentState
import asyncio


# Legacy `status` strings that were used before IncidentState was hooked
# up to the worker pipeline. Rows created by old code (or by webhooks
# before the OPEN-on-create change) map onto the closest enum member so
# `transition_to` doesn't fail on them.
_LEGACY_STATUS_ALIAS: dict[str, IncidentState] = {
    "PENDING": IncidentState.OPEN,
    "COMPLETED": IncidentState.RESOLVED,
}


def _current_state(record) -> IncidentState:
    raw = record.status or ""
    try:
        return IncidentState(raw)
    except ValueError:
        return _LEGACY_STATUS_ALIAS.get(raw, IncidentState.OPEN)


def transition_to(record, new_state: IncidentState, db) -> None:
    """Validate and apply an IncidentState transition.

    Raises ValueError if the transition isn't allowed by StateMachine
    (e.g. trying to go RESOLVED→OPEN). Writes record.status and commits
    so the new state is visible to other workers immediately.

    Legacy `status` values ("PENDING"/"COMPLETED") are mapped onto the
    enum before validation — see _LEGACY_STATUS_ALIAS.
    """
    current = _current_state(record)
    if not StateMachine.validate_transition(current, new_state):
        raise ValueError(
            f"Invalid state transition: {current.value} → {new_state.value}"
        )
    record.status = new_state.value
    db.commit()

celery_app = Celery(
    "sre_tasks",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL
)

if settings.CELERY_TASK_ALWAYS_EAGER:
    # Inline-режим для локального e2e: process_incident_task.delay(...)
    # выполняется синхронно в текущем процессе, без Redis/worker-а.
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True


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

    # Load the persisted row once and drive the StateMachine through it.
    # `record` may be None if the webhook didn't pre-create it (e.g. tests
    # that bypass the HTTP layer) — we fall back to a no-op for state
    # transitions in that case but still collect trace locally.
    record = db.query(IncidentRecord).filter(IncidentRecord.incident_id == incident_id).first()

    def safe_transition(new_state: IncidentState, stage_trace: dict | None = None) -> None:
        """Apply a state transition + reflect it back into the current stage_trace."""
        if record is None:
            return
        transition_to(record, new_state, db)
        if stage_trace is not None:
            stage_trace["state_after"] = new_state.value

    try:
        incident = Incident(**incident_data)

        # OPEN → INVESTIGATING (pipeline really started doing work).
        async with StageTimer("analyzer") as t:
            analyzer = AnalyzerAgent()
            analysis = await analyzer.analyze(incident)
        snap = t.snapshot().to_dict()
        safe_transition(IncidentState.INVESTIGATING, snap)
        traces.append(snap)

        # Stage 2: Hypothesis — augmented with up to 3 past ACCEPTED resolutions
        # matching by service/cause/namespace (deterministic scoring inside
        # SimilarIncidentEngine — no LLM call here).
        similar_past = SimilarIncidentEngine.find(current_incident=incident_data, limit=3)
        async with StageTimer("hypothesis") as t:
            hypo = HypothesisAgent()
            hypotheses = await hypo.generate(analysis, similar_past=similar_past)
        snap = t.snapshot().to_dict()
        safe_transition(IncidentState.HYPOTHESIS_GENERATED, snap)
        traces.append(snap)

        # Stage 3: Critic — no state transition; still in HYPOTHESIS_GENERATED.
        async with StageTimer("critic") as t:
            critic = CriticAgent()
            final_cause = await critic.audit(analysis, hypotheses, namespace=incident.namespace)
        traces.append(t.snapshot().to_dict())

        # Stage 4: Fix → FIX_PROPOSED.
        async with StageTimer("fix") as t:
            fixer = FixAgent()
            fix_suggestion = await fixer.suggest(final_cause)
        snap = t.snapshot().to_dict()
        safe_transition(IncidentState.FIX_PROPOSED, snap)
        traces.append(snap)

        # Stage 5: Risk — no state transition; still in FIX_PROPOSED.
        async with StageTimer("risk") as t:
            risker = RiskAgent()
            risk_report = await risker.assess(fix_suggestion)
        traces.append(t.snapshot().to_dict())

        # Stage 6: Synthesis — sees all 5 outputs simultaneously (two-level
        # reasoning). Final transition to RESOLVED happens after the synth
        # is stored so the row carries the synthesis text at the same time
        # the state flips.
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
        synth_snap = t.snapshot().to_dict()

        # Persistence — analysis bundle plus the per-stage trace and the
        # similar-past matches that fed into Hypothesis. Both fields are
        # JSON columns; existing rows without them stay backward-compatible.
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
            # Transition FIX_PROPOSED → APPROVAL_PENDING → ... is the
            # "with human-approval" path; the always-on Synthesis-as-stage-6
            # path treats Synthesis output as the final report and goes
            # straight to RESOLVED. APPROVAL_PENDING / EXECUTING / are
            # entered later by the approvals API endpoint.
            safe_transition(IncidentState.RESOLVED, synth_snap)
            record.trace = traces + [synth_snap]
            db.commit()
        else:
            # Tests that bypass the DB still get the in-memory trace.
            traces.append(synth_snap)

        # Notification — send synthesized report, not raw stage output
        await discord_service.send_report(
            f"**Incident {incident_id} Analysis Complete.**\n\n{synthesis}"
        )

    except Exception as e:
        # Any agent / synthesis failure marks the incident FAILED.
        # validate_transition allows X → FAILED from every non-terminal state.
        if record is not None:
            try:
                transition_to(record, IncidentState.FAILED, db)
            except ValueError:
                # Already in a terminal state (RESOLVED/FAILED) — leave as is.
                db.rollback()
        else:
            db.rollback()
        raise e
    finally:
        db.close()
