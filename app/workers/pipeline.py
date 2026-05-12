"""IncidentPipeline — staged orchestrator for the incident analysis pipeline.

Extracted from the monolithic async_process_incident function in tasks.py.
Each stage is a discrete async method, making the pipeline independently
testable and easy to extend without touching adjacent stages.

Stage order:
  0. _populate_kg       — KG auto-population (best-effort)
  1. stage_analyze      — AnalyzerAgent summary
  2. stage_diagnose     — DiagnosticsEngine + k8s/VM/TC enrichment → FactStore
  3. stage_hypothesize  — MultiHypothesisAgent fan-out + similar past context
  4. stage_critique     — FactCriticAgent adversarial grounding → best candidate
  5. stage_jira_enrich  — Atlassian Jira open/resolved tickets (best-effort)
  6. stage_fix          — FixAgent ExecutionIntent (recurrence + Jira aware)
  7. stage_risk         — RiskAgent assessment
  8. stage_synthesize   — SynthesisAgent final report + DB persist + Discord
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional

import structlog

from app.agents.analyzer import AnalyzerAgent
from app.agents.fact_critic import (FactCriticAgent, best_candidate, refuted,
                                    survivors)
from app.agents.fix import FixAgent
from app.agents.multi_hypothesis import MultiHypothesisAgent
from app.agents.risk import RiskAgent
from app.agents.synthesis import SynthesisAgent
from app.config import settings
from app.context.jira_client import JiraClient, build_jira_context
from app.context.k8s_facts import K8sFacts
from app.context.vm_client import VMClient
from app.core.intelligence.similar_incidents import SimilarIncidentEngine
from app.core.state_machine import IncidentState, StateMachine
from app.core.tracing import StageTimer
from app.database import IncidentRecord
from app.diagnostics import default_engine as diag_engine
from app.diagnostics.facts import FactStore
from app.diagnostics.incident_ctx import build_diagnostics_ctx
from app.knowledge_graph.auto_populator import populate_from_incident
from app.models.incident import Incident
from app.observability.ai_metrics import track_disagreement, track_no_survivor
from app.services.audit_logger import audit_service
from app.services.discord_service import discord_service
from app.services.gitlab_service import enrich_with_gitlab, gitlab_context_to_prompt
from app.services.teamcity_service import incident_teamcity_context

logger = structlog.get_logger()

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
    current = _current_state(record)
    if not StateMachine.validate_transition(current, new_state):
        raise ValueError(
            f"Invalid state transition: {current.value} → {new_state.value}"
        )
    record.status = new_state.value
    db.commit()


def _serialize_hypotheses(critiqued, facts: FactStore) -> str:
    lines = ["=== HYPOTHESES (fact-anchored multi-perspective) ==="]
    surv = survivors(critiqued).items
    ref = refuted(critiqued).items
    if not surv and not ref:
        lines.append("(no hypotheses produced — facts may be insufficient)")
    for h in surv:
        lines.append(f"[SURVIVED] perspective={h.perspective} conf={h.confidence:.2f}")
        lines.append(f"  cause: {h.cause}")
        if h.detail:
            lines.append(f"  detail: {h.detail}")
        lines.append(f"  anchored_facts: {h.anchored_facts}")
    for h in ref:
        lines.append(f"[REFUTED]  perspective={h.perspective} conf={h.confidence:.2f}")
        lines.append(f"  cause: {h.cause}")
        lines.append(f"  refuted_by: {h.refutations}")
    lines.append("")
    lines.append(facts.to_prompt_context())
    return "\n".join(lines)


class IncidentPipeline:
    """Stateful pipeline for a single incident. Call `await pipeline.run()`."""

    def __init__(self, incident_data: Dict[str, Any], db, record, root_span):
        self.incident_data = incident_data
        self.incident_id: str = incident_data.get("incident_id", "")
        self.db = db
        self.record = record
        self.root_span = root_span
        self.traces: List[dict] = []

        # Stage outputs populated as the pipeline advances.
        self.incident: Optional[Incident] = None
        self.analysis: Optional[str] = None
        self.fact_store: Optional[FactStore] = None
        self.similar_past: List[dict] = []
        self.is_recurrence: bool = False
        self.flap_count: int = incident_data.get("flap_count", 0)
        self.critiqued = None
        self.best = None
        self.final_cause: str = ""
        self.hypotheses_text: str = ""
        self.jira_context: Optional[Dict[str, Any]] = None
        self.fix_suggestion: Optional[str] = None
        self.risk_report: Optional[str] = None
        self.synthesis: Optional[str] = None
        self.cluster_health_context: Optional[str] = None
        self.gitlab_context: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    # State machine helpers
    # ------------------------------------------------------------------

    def _safe_transition(self, new_state: IncidentState, stage_trace: Optional[dict] = None) -> None:
        if self.record is None:
            return
        transition_to(self.record, new_state, self.db)
        if stage_trace is not None:
            stage_trace["state_after"] = new_state.value

    # ------------------------------------------------------------------
    # Stage 0 — Knowledge Graph population (best-effort)
    # ------------------------------------------------------------------

    async def _populate_kg(self) -> None:
        try:
            populate_from_incident(self.db, self.incident)
            self.db.commit()
        except Exception as e:
            audit_service.log_event(
                "KG_POPULATE_FAILED",
                {"incident_id": self.incident_id, "error": type(e).__name__},
            )
            self.db.rollback()

    # ------------------------------------------------------------------
    # Stage 1 — Analyzer
    # ------------------------------------------------------------------

    async def stage_analyze(self) -> None:
        async with StageTimer("analyzer") as t:
            self.analysis = await AnalyzerAgent().analyze(self.incident)
        snap = t.snapshot().to_dict()
        self._safe_transition(IncidentState.INVESTIGATING, snap)
        self.traces.append(snap)

    # ------------------------------------------------------------------
    # Stage 2 — Diagnostics + enrichment (k8s / VM / TC)
    # ------------------------------------------------------------------

    async def stage_diagnose(self) -> None:
        async with StageTimer("diagnostics") as t:
            await self._enrich_teamcity()
            await self._enrich_gitlab()
            diag_ctx = build_diagnostics_ctx(
                incident=self.incident,
                analyzer_summary=self.analysis,
                kg_session=self.db,
            )
            await self._enrich_k8s(diag_ctx)
            await self._enrich_vm(diag_ctx)
            self.fact_store = diag_engine.run(diag_ctx)
        snap = t.snapshot().to_dict()
        self._safe_transition(IncidentState.FACTS_COLLECTED, snap)
        self.traces.append(snap)

    async def _enrich_gitlab(self) -> None:
        try:
            gl_ctx = await enrich_with_gitlab(self.incident.teamcity_context)
            if gl_ctx:
                self.gitlab_context = gl_ctx
                audit_service.log_event(
                    "GITLAB_ENRICHED",
                    {"mr_count": len(gl_ctx.get("mrs", []))},
                )
        except Exception as e:
            audit_service.log_event("GITLAB_ENRICH_FAILED", {"error": str(e)})

    async def _enrich_teamcity(self) -> None:
        if self.incident.teamcity_context is not None:
            return
        try:
            tc_ctx = await incident_teamcity_context(
                namespace=self.incident.namespace,
                incident_starts_at=self.incident.starts_at,
            )
            if tc_ctx:
                self.incident.teamcity_context = tc_ctx
        except Exception as e:
            audit_service.log_event(
                "TC_ENRICHMENT_FAILED",
                {"incident_id": self.incident_id, "error": type(e).__name__},
            )

    async def _enrich_k8s(self, diag_ctx: dict) -> None:
        if not self.incident.namespace:
            return
        try:
            snap = await K8sFacts.collect_snapshot(
                namespace=self.incident.namespace,
                pod=self.incident.labels.get("pod"),
            )
            diag_ctx["logs_summary"] = snap.text
            diag_ctx["k8s_pod_state"] = snap.container_terminated
            diag_ctx["k8s_events"] = snap.pod_events
            if snap.core_dump_node:
                diag_ctx["core_dump_node"] = snap.core_dump_node
        except Exception as e:
            audit_service.log_event(
                "K8S_ENRICHMENT_FAILED",
                {"incident_id": self.incident_id, "error": type(e).__name__},
            )

    async def _enrich_vm(self, diag_ctx: dict) -> None:
        if not settings.VICTORIA_METRICS_URL:
            return
        vm = VMClient(settings.VICTORIA_METRICS_URL, timeout=10.0)
        await asyncio.gather(
            self._enrich_pod_metrics(diag_ctx, vm),
            self._enrich_cluster_health(diag_ctx, vm),
            return_exceptions=True,
        )

    async def _enrich_pod_metrics(self, diag_ctx: dict, vm: VMClient) -> None:
        if not self.incident.namespace:
            return
        try:
            incident_ts = None
            if self.incident.starts_at:
                try:
                    incident_ts = datetime.fromisoformat(
                        self.incident.starts_at.replace("Z", "+00:00")
                    )
                except ValueError:
                    pass
            metrics = await vm.get_pod_metrics(
                namespace=self.incident.namespace,
                pod=self.incident.labels.get("pod", ""),
                window_minutes=settings.VICTORIA_METRICS_WINDOW_MINUTES,
                incident_time=incident_ts,
            )
            diag_ctx["metrics_summary"] = metrics
        except Exception as e:
            audit_service.log_event(
                "VM_POD_ENRICHMENT_FAILED",
                {"incident_id": self.incident_id, "error": type(e).__name__},
            )

    async def _enrich_cluster_health(self, diag_ctx: dict, vm: VMClient) -> None:
        try:
            health = await vm.get_cluster_health()
            diag_ctx["cluster_health"] = health.to_dict()
            self.cluster_health_context = health.to_prompt_context()
            diag_ctx["cluster_health_context"] = self.cluster_health_context
        except Exception as e:
            audit_service.log_event(
                "VM_CLUSTER_HEALTH_FAILED",
                {"incident_id": self.incident_id, "error": type(e).__name__},
            )

    # ------------------------------------------------------------------
    # Stage 3 — Multi-hypothesis fan-out
    # ------------------------------------------------------------------

    async def stage_hypothesize(self) -> None:
        self.similar_past = SimilarIncidentEngine.find(
            current_incident=self.incident_data, limit=3
        )
        self.is_recurrence = any(p.get("recurrence") for p in self.similar_past)

        summary = self.analysis

        # Flapping context: same alert fired→resolved→fired again.
        # RESOLVED between cycles may be misleading — root cause not fixed.
        if self.flap_count > 0:
            summary = (
                f"⚠️ FLAPPING ALERT — cycle #{self.flap_count}. "
                f"This alert has fired, resolved, and re-fired {self.flap_count} time(s). "
                f"The RESOLVED signal between cycles was likely premature — "
                f"the underlying cause was not fully remediated. "
                f"Focus hypotheses on why the fix did not hold.\n\n{summary}"
            )

        # Cluster-wide health snapshot: lets hypotheses distinguish "isolated
        # pod issue" from "cluster-wide pressure" (e.g. 8 crashloops + disk 92%).
        if self.cluster_health_context:
            summary = f"{summary}\n\n{self.cluster_health_context}"

        gl_prompt = gitlab_context_to_prompt(self.gitlab_context)
        if gl_prompt:
            summary = f"{summary}\n\n{gl_prompt}"

        if self.similar_past:
            bullets = []
            for p in self.similar_past:
                line = f"- score={p.get('score', '?')} cause={(p.get('root_cause') or '?')[:200]}"
                if p.get("recurrence"):
                    days = p.get("days_ago")
                    line += f" [RECURRENCE: {days}d ago]" if days is not None else " [RECURRENCE]"
                bullets.append(line)
            summary = (
                f"{summary}\n\nSimilar past resolutions (consider as patterns, "
                f"verify against current facts):\n" + "\n".join(bullets)
            )

        async with StageTimer("hypothesis") as t:
            hypothesis_set = await MultiHypothesisAgent().generate(
                incident_summary=summary, facts=self.fact_store
            )
        self.traces.append(t.snapshot().to_dict())
        self._hypothesis_set = hypothesis_set

    # ------------------------------------------------------------------
    # Stage 4 — FactCritic adversarial grounding
    # ------------------------------------------------------------------

    async def stage_critique(self) -> None:
        async with StageTimer("critic") as t:
            self.critiqued = await FactCriticAgent().critique_all(
                self._hypothesis_set, self.fact_store
            )
        self.traces.append(t.snapshot().to_dict())

        if self.critiqued.disagreement_signal() is not None:
            track_disagreement()

        self.best = best_candidate(self.critiqued)
        if self.best is None:
            track_no_survivor()
            self.final_cause = (
                "No hypothesis survived adversarial critique. "
                f"Observed facts: {sorted(self.fact_store.observed_kinds())}. "
                "Manual triage required."
            )
        else:
            self.final_cause = self.best.cause + (
                f" — {self.best.detail}" if self.best.detail else ""
            )
        self.hypotheses_text = _serialize_hypotheses(self.critiqued, self.fact_store)

        if self.traces:
            self.traces[-1]["state_after"] = IncidentState.HYPOTHESIS_GENERATED.value
        if self.record is not None:
            transition_to(self.record, IncidentState.HYPOTHESIS_GENERATED, self.db)

    # ------------------------------------------------------------------
    # Stage 5 — Jira enrichment (best-effort)
    # ------------------------------------------------------------------

    async def stage_jira_enrich(self) -> None:
        if not (settings.JIRA_BASE_URL and settings.JIRA_API_TOKEN):
            return
        try:
            jira = JiraClient(
                base_url=settings.JIRA_BASE_URL,
                email=settings.JIRA_EMAIL,
                api_token=settings.JIRA_API_TOKEN,
                project_key=settings.JIRA_PROJECT_KEY,
                backend_label=settings.JIRA_BACKEND_LABEL,
            )
            service = (
                self.incident.labels.get("service")
                or self.incident.labels.get("app", "")
            )
            if service:
                issues = await jira.search_by_service(
                    service=service,
                    namespace=self.incident.namespace,
                    days=settings.JIRA_SEARCH_DAYS,
                )
                self.jira_context = build_jira_context(issues)
        except Exception as e:
            audit_service.log_event(
                "JIRA_ENRICHMENT_FAILED",
                {"incident_id": self.incident_id, "error": type(e).__name__},
            )

    # ------------------------------------------------------------------
    # Stage 6 — FixAgent
    # ------------------------------------------------------------------

    async def stage_fix(self) -> None:
        async with StageTimer("fix") as t:
            self.fix_suggestion = await FixAgent().suggest(
                self.final_cause,
                is_recurrence=self.is_recurrence,
                jira_context=self.jira_context,
            )
        snap = t.snapshot().to_dict()
        self._safe_transition(IncidentState.FIX_PROPOSED, snap)
        self.traces.append(snap)

    # ------------------------------------------------------------------
    # Stage 7 — RiskAgent
    # ------------------------------------------------------------------

    async def stage_risk(self) -> None:
        async with StageTimer("risk") as t:
            self.risk_report = await RiskAgent().assess(self.fix_suggestion)
        self.traces.append(t.snapshot().to_dict())

    # ------------------------------------------------------------------
    # Stage 8 — Synthesis + persist + Discord
    # ------------------------------------------------------------------

    async def stage_synthesize(self) -> None:
        async with StageTimer("synthesis") as t:
            self.synthesis = await SynthesisAgent().synthesize(
                incident_id=self.incident_id,
                analysis=self.analysis,
                hypotheses=self.hypotheses_text,
                final_cause=self.final_cause,
                fix_suggestion=self.fix_suggestion,
                risk_report=self.risk_report,
            )
        synth_snap = t.snapshot().to_dict()
        await self._persist(synth_snap)

    async def _persist(self, synth_snap: dict) -> None:
        record = (
            self.db.query(IncidentRecord)
            .filter(IncidentRecord.incident_id == self.incident_id)
            .first()
        )
        resolution_quality = "resolved" if self.best else "unresolved"
        self.root_span.set_attribute("sre.incident.resolution_quality", resolution_quality)
        self.root_span.set_attribute("sre.incident.is_recurrence", self.is_recurrence)
        if self.best:
            self.root_span.set_attribute("sre.incident.cause", self.best.cause[:500])

        if record:
            record.analysis = {
                "summary": self.analysis,
                "hypotheses": self.hypotheses_text,
                "cause": self.best.cause if self.best else None,
                "triage_note": self.final_cause if self.best is None else None,
                "resolution_quality": resolution_quality,
                "fix": self.fix_suggestion,
                "risk": self.risk_report,
                "synthesis": self.synthesis,
                "similar_past_count": len(self.similar_past),
                "is_recurrence": self.is_recurrence,
                "jira_context": self.jira_context,
                "facts": self.fact_store.to_dict()["facts"],
                "fact_conflicts": [
                    {"a": a.kind, "b": b.kind,
                     "a_conf": a.confidence, "b_conf": b.confidence}
                    for a, b in self.fact_store.conflicts()
                ],
                "hypothesis_set": [h.model_dump() for h in self.critiqued.items],
                "best_candidate": self.best.model_dump() if self.best else None,
                "disagreement_signal": self.critiqued.disagreement_signal(),
                "consensus_kinds": self.critiqued.consensus_kinds(),
                "cluster_health_context": self.cluster_health_context,
                "gitlab_context": self.gitlab_context,
            }
            self._safe_transition(IncidentState.RESOLVED, synth_snap)
            record.trace = self.traces + [synth_snap]
            self.db.commit()
        else:
            self.traces.append(synth_snap)

        labels = self.incident.labels or {}
        await discord_service.send_incident_report(
            incident_id=self.incident_id,
            alertname=labels.get("alertname", "UnknownAlert"),
            namespace=self.incident.namespace or "",
            pod=labels.get("pod"),
            service=labels.get("service") or labels.get("app"),
            # Node* alerts carry instance/node label instead of pod/namespace
            node=labels.get("node") or labels.get("instance"),
            severity=self.incident.severity or "warning",
            cause=self.best.cause if self.best else None,
            resolution_quality="resolved" if self.best else "unresolved",
            synthesis=self.synthesis or "",
            is_recurrence=self.is_recurrence,
            flap_count=self.flap_count,
        )

    # ------------------------------------------------------------------
    # Orchestrator
    # ------------------------------------------------------------------

    async def run(self) -> None:
        self.incident = Incident(**self.incident_data)
        await self._populate_kg()
        await self.stage_analyze()
        await self.stage_diagnose()
        await self.stage_hypothesize()
        await self.stage_critique()
        await self.stage_jira_enrich()
        await self.stage_fix()
        await self.stage_risk()
        await self.stage_synthesize()
