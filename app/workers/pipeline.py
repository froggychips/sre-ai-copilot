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
from datetime import datetime, timedelta
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
from app.core.execution_dsl import ExecutionIntent
from app.core.intelligence.similar_incidents import SimilarIncidentEngine
from app.core.state_machine import IncidentState, StateMachine
from app.core.tracing import StageTimer
from app.database import IncidentRecord
from app.diagnostics import default_engine as diag_engine
from app.diagnostics.facts import FactStore
from app.diagnostics.incident_ctx import build_diagnostics_ctx
from app.knowledge_graph.auto_populator import populate_from_incident
from app.knowledge_graph.schema import Deployment, Service
from app.models.incident import Incident
from app.rca.deploy_correlator import correlate_deploy_to_incident
from app.observability.ai_metrics import (
    track_disagreement,
    track_execution_intent,
    track_executor_status,
    track_fact_conflict,
    track_hypotheses_count,
    track_incident_flapping,
    track_incident_recurrence,
    track_no_survivor,
    track_resolution_quality,
    track_survivors_count,
)
from app.services.audit_logger import audit_service
from app.services.clickhouse_service import get_blast_radius
from app.services.discord_service import discord_service
from app.services.gitlab_service import enrich_with_gitlab, gitlab_context_to_prompt
from app.services.statics_service import check_statics_for_error
from app.services.teamcity_service import incident_teamcity_context, teamcity_context_to_prompt

logger = structlog.get_logger()

# Per-stage timeout: каждая LLM/enrichment-стадия оборачивается в
# asyncio.wait_for(..., timeout=_STAGE_TIMEOUT). При TimeoutError исключение
# пробрасывается наверх — вызывающий код переводит инцидент в FAILED.
# getattr с дефолтом, чтобы не трогать config.py (отдельный batch/миграция).
_STAGE_TIMEOUT = float(getattr(settings, "PIPELINE_STAGE_TIMEOUT_SECONDS", 240.0))


async def _staged(coro):
    """Обернуть стадию в per-stage timeout. TimeoutError НЕ глушим."""
    return await asyncio.wait_for(coro, timeout=_STAGE_TIMEOUT)

_LEGACY_STATUS_ALIAS: dict[str, IncidentState] = {
    "PENDING": IncidentState.OPEN,
    "COMPLETED": IncidentState.RESOLVED,
}

# Root cause #2: alerts, которые срабатывают как побочка rolling-deploy'а,
# а не как реальная проблема. KubeDeploymentGenerationMismatch ловится 131
# раз/неделю с median TTR ~11 мин — это таймер rollout'а, а не инцидент.
ROLLOUT_NOISE_ALERTNAMES = frozenset({
    "KubeDeploymentGenerationMismatch",
    "KubeDeploymentReplicasMismatch",
    "KubeContainerWaiting",
})

# Whitelist: эти алёрты всегда actionable — НЕ подавляем даже если есть
# active rollout. CrashLooping/JobFailed/TargetDown — реальные сбои.
ROLLOUT_NOISE_NEVER_SUPPRESS = frozenset({
    "KubePodCrashLooping",
    "KubeJobFailed",
    "TargetDown",
    "KubePodNotReady",
})


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
        # _hypothesis_set: вывод MultiHypothesisAgent (stage_hypothesize) →
        # вход FactCriticAgent (stage_critique). Объявляем в __init__, чтобы
        # mypy видел атрибут (иначе [attr-defined]/[has-type] на доступе в
        # stage_critique, где он раньше появлялся только как self._hypothesis_set=…).
        self._hypothesis_set: Optional[Any] = None
        self.critiqued: Optional[Any] = None
        self.best: Optional[Any] = None
        self.final_cause: str = ""
        self.hypotheses_text: str = ""
        self.jira_context: Optional[Dict[str, Any]] = None
        self.fix_suggestion: Optional[str] = None
        self.execution_intent: Optional[ExecutionIntent] = None
        self.executor_result: Optional[Dict[str, Any]] = None
        self.risk_report: Optional[str] = None
        self.synthesis: Optional[str] = None
        self.cluster_health_context: Optional[str] = None
        self.gitlab_context: Optional[Dict[str, Any]] = None
        self.blast_radius_context: Optional[str] = None
        self.statics_check_context: Optional[str] = None
        # Wave 3 #2: deploy correlation для проброса в Discord embed.
        # Заполняется в stage_diagnose._enrich_deploy_correlation.
        self.deploy_correlation: Optional[Dict[str, Any]] = None
        # Wave 3 #10: team_owner резолвится после KG populate; используется
        # в send_incident_report для per-team channel routing.
        self.team_owner: Optional[str] = None
        # Wave 3 #13: окно для recurrence-label «×N in 24h · M in 7d».
        self.recurrence_count_24h: int = 0
        self.recurrence_count_7d: int = 0

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
            await asyncio.gather(
                self._enrich_k8s(diag_ctx),
                self._enrich_vm(diag_ctx),
                self._enrich_clickhouse(diag_ctx),
                self._enrich_statics(diag_ctx),
                return_exceptions=True,
            )
            # Sync best-effort hook: ищем deploy в окне до инцидента и
            # сравниваем метрики до/после. Результат — отдельный сигнал для
            # RCA-агентов и финального синтеза (см. diag_ctx["deploy_correlation"]).
            self._enrich_deploy_correlation(diag_ctx)
            # Root cause #2: если в окне ROLLOUT_SUPPRESS_WINDOW_MINUTES
            # шёл rollout того же сервиса — `KubeDeploymentGenerationMismatch`
            # и компания являются rollout-noise. Демотим severity → "info",
            # Wave 3 severity-routing уже умеет пропускать info.
            self._filter_rollout_noise(diag_ctx)
            self.fact_store = diag_engine.run(diag_ctx)
        snap = t.snapshot().to_dict()
        self._safe_transition(IncidentState.FACTS_COLLECTED, snap)
        self.traces.append(snap)

    async def _enrich_clickhouse(self, diag_ctx: dict) -> None:
        if not self.incident.namespace or not self.incident.starts_at:
            return
        try:
            blast = await get_blast_radius(
                namespace=self.incident.namespace,
                starts_at=self.incident.starts_at,
            )
            if blast:
                diag_ctx["blast_radius"] = blast
                self.blast_radius_context = blast
                audit_service.log_event("CH_BLAST_RADIUS_ENRICHED", {"incident_id": self.incident_id})
        except Exception as e:
            audit_service.log_event("CH_ENRICH_FAILED", {"incident_id": self.incident_id, "error": str(e)})

    async def _enrich_statics(self, diag_ctx: dict) -> None:
        error_text = (
            (self.incident.annotations or {}).get("description")
            or self.incident.description
            or ""
        )
        if not error_text:
            return
        try:
            statics_check = await check_statics_for_error(error_text)
            if statics_check:
                diag_ctx["statics_check"] = statics_check
                self.statics_check_context = statics_check
                audit_service.log_event("STATICS_ENRICHED", {"incident_id": self.incident_id})
        except Exception as e:
            audit_service.log_event("STATICS_ENRICH_FAILED", {"incident_id": self.incident_id, "error": str(e)})

    def _enrich_deploy_correlation(self, diag_ctx: dict) -> None:
        """Связать инцидент с недавним deploy через kg_deployments+kg_service_health.

        Sync, best-effort. При любой ошибке пайплайн не падает: просто пишем
        событие в audit и идём дальше.

        TODO: глубже интегрировать в hypothesis prompt (app/workers/pipeline.py
        stage_hypothesize) и в SynthesisAgent — сейчас сигнал доступен через
        diag_ctx["deploy_correlation"], но не подмешан в текст для LLM.
        """
        if not self.incident.namespace:
            return
        service_name = (self.incident.labels or {}).get("service")
        if not service_name:
            return
        incident_starts_at = diag_ctx.get("incident_starts_at")
        if incident_starts_at is None:
            return
        try:
            svc = (
                self.db.query(Service)
                .filter(
                    Service.namespace == self.incident.namespace,
                    Service.name == service_name,
                )
                .one_or_none()
            )
            if svc is None:
                return
            result = correlate_deploy_to_incident(
                db=self.db,
                service_id=svc.id,
                incident_ts=incident_starts_at,
            )
            diag_ctx["deploy_correlation"] = result
            # Wave 3 #2: пробрасываем в discord embed через _persist.
            self.deploy_correlation = result
            # Wave 3 #10: подтягиваем team_owner с сервиса (best-effort).
            if svc.team_owner:
                self.team_owner = svc.team_owner
            if result.get("verdict") in ("likely", "suspect"):
                audit_service.log_event(
                    "DEPLOY_CORRELATION_SUSPECT",
                    {
                        "incident_id": self.incident_id,
                        "deploy_id": (result.get("deploy") or {}).get("id"),
                        "verdict": result.get("verdict"),
                        "confidence": result.get("confidence"),
                    },
                )
        except Exception as e:
            audit_service.log_event(
                "DEPLOY_CORRELATION_FAILED",
                {"incident_id": self.incident_id, "error": type(e).__name__},
            )

    def _filter_rollout_noise(self, diag_ctx: dict) -> None:
        """Подавить rollout-noise алёрты, если идёт активный rollout сервиса.

        Root cause #2 alert-quality: `KubeDeployment{Generation,Replicas}Mismatch`
        и `KubeContainerWaiting` срабатывают как side-effect rolling-update'а
        (median TTR ~11 мин). Если в окне `ROLLOUT_SUPPRESS_WINDOW_MINUTES`
        зафиксирован deploy того же сервиса → демотим severity до "info"
        (Wave 3 severity-routing уже умеет skip-ать info → канал тихий).

        Whitelist actionable алёртов (`KubePodCrashLooping`, `KubeJobFailed`,
        `TargetDown`, `KubePodNotReady`) — НЕ подавляются никогда; это
        реальные сбои, даже если совпадают с rollout-окном.

        Best-effort: при любой ошибке (KG holod, БД сорвалась) ничего не
        делаем — пайплайн идёт дальше с исходной severity.
        """
        if not settings.ROLLOUT_SUPPRESS_ENABLED:
            return
        if self.incident is None:
            return
        labels = self.incident.labels or {}
        alertname = labels.get("alertname", "")
        if alertname not in ROLLOUT_NOISE_ALERTNAMES:
            return
        if alertname in ROLLOUT_NOISE_NEVER_SUPPRESS:
            # Defensive: пересечение пустое (sets разные), но если кто-то
            # переставит alertname в обе группы — actionable приоритет.
            return

        # Резолвим target service из labels (тот же helper, что в alert_enrichment).
        try:
            from app.services.alert_enrichment import _resolve_target_service_from_labels
            resolved_ns, resolved_svc = _resolve_target_service_from_labels(labels)
        except Exception:
            resolved_ns, resolved_svc = None, None
        namespace = resolved_ns or self.incident.namespace
        service_name = resolved_svc or labels.get("service") or labels.get("app")
        if not namespace or not service_name:
            return

        window_min = max(1, int(settings.ROLLOUT_SUPPRESS_WINDOW_MINUTES))
        try:
            svc = (
                self.db.query(Service)
                .filter(Service.namespace == namespace, Service.name == service_name)
                .one_or_none()
            )
            if svc is None:
                return
            now = datetime.utcnow()
            cutoff = now - timedelta(minutes=window_min)
            # Активный rollout = started_at в окне И (finished_at IS NULL OR
            # finished_at >= started_at - 5 мин). Второе условие защищает от
            # rollouts, которые закончились прямо перед alert-ом — generation
            # mismatch может задержаться на ~1 мин после finish.
            deploy = (
                self.db.query(Deployment)
                .filter(
                    Deployment.service_id == svc.id,
                    Deployment.started_at >= cutoff,
                )
                .order_by(Deployment.started_at.desc())
                .first()
            )
            if deploy is None:
                return
            if deploy.finished_at is not None and deploy.finished_at < (
                deploy.started_at - timedelta(minutes=5)
            ):
                # finished_at сильно раньше started_at — это битая запись, не верим.
                return
            # Гасим: severity → info. Wave 3 routing.skip_info_in_error работает.
            previous_severity = self.incident.severity
            self.incident.severity = "info"
            # Подсветить в labels для downstream-render (audit/digest/digital pet).
            self.incident.labels["suppress_reason"] = "active_rollout"
            self.incident.labels["original_severity"] = previous_severity or ""
            diag_ctx["rollout_suppressed"] = True
            age_seconds = int((now - deploy.started_at).total_seconds())
            audit_service.log_event(
                "ALERT_SUPPRESSED_ROLLOUT_NOISE",
                {
                    "incident_id": self.incident_id,
                    "alertname": alertname,
                    "namespace": namespace,
                    "service": service_name,
                    "deploy_id": deploy.id,
                    "deploy_started_at": deploy.started_at.isoformat(),
                    "age_seconds": age_seconds,
                    "previous_severity": previous_severity,
                },
            )
        except Exception as e:
            audit_service.log_event(
                "ROLLOUT_NOISE_FILTER_FAILED",
                {"incident_id": self.incident_id, "error": type(e).__name__},
            )

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
        track_incident_recurrence(self.is_recurrence)
        if self.flap_count > 0:
            track_incident_flapping()

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

        tc_prompt = teamcity_context_to_prompt(self.incident.teamcity_context)
        if tc_prompt:
            summary = f"{summary}\n\n{tc_prompt}"

        gl_prompt = gitlab_context_to_prompt(self.gitlab_context)
        if gl_prompt:
            summary = f"{summary}\n\n{gl_prompt}"

        if self.blast_radius_context:
            summary = f"{summary}\n\n{self.blast_radius_context}"

        if self.statics_check_context:
            summary = f"{summary}\n\n{self.statics_check_context}"

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

        # Distribution number-of-hypotheses per run (cardinality сигнал
        # того, сколько даёт fan-out перед adversarial grounding).
        track_hypotheses_count(len(self.critiqued.items))
        # Survivors после critic-а — узкое место reasoning-цепочки.
        track_survivors_count(len(survivors(self.critiqued).items))

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
            self.fix_suggestion, self.execution_intent = await FixAgent().suggest(
                self.final_cause,
                is_recurrence=self.is_recurrence,
                jira_context=self.jira_context,
            )
        snap = t.snapshot().to_dict()
        # Метрика на root-span: смог ли LLM выдать structured-intent.
        self.root_span.set_attribute(
            "sre.incident.execution_intent_parsed",
            self.execution_intent is not None,
        )
        if self.execution_intent is not None:
            self.root_span.set_attribute(
                "sre.incident.execution_intent_action",
                self.execution_intent.action.value,
            )
            track_execution_intent(parsed=True, action=self.execution_intent.action.value)
        else:
            track_execution_intent(parsed=False)
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
    # Stage 7.5 — Executor (dry-run only, PR #2 executor track)
    # ------------------------------------------------------------------

    async def stage_executor(self) -> None:
        """Server-side dry-run of the proposed ExecutionIntent.

        Behaviour:
          - settings.EXECUTOR_ENABLED=False → стадия пропускается полностью.
          - self.execution_intent is None → пропускается с reason=no_intent.
          - K8sService.execute_intent(intent, dry_run=True) →
            kubectl ... --dry-run=server: kube-apiserver валидирует команду,
            ничего не применяя. K8sSecurityGuard.validate вызывается внутри
            K8sService.run_command первым шагом — guardrail.blocked event
            попадает на текущий OTEL-span.
          - Любая exception от kubectl/guard захватывается, executor_result
            помечается blocked=True; пайплайн не падает (advisory-fallback).

        Реальный write (dry_run=False) появится только в PR #3 после Discord
        approval. До тех пор стадия — это «выглядит ли команда валидной для
        кластера» проверка.
        """
        if not settings.EXECUTOR_ENABLED:
            self.executor_result = {"status": "skipped", "reason": "executor_disabled"}
            track_executor_status("skipped")
            return
        if self.execution_intent is None:
            self.executor_result = {"status": "skipped", "reason": "no_intent"}
            track_executor_status("skipped")
            return

        from app.services.k8s_service import k8s_service

        async with StageTimer("executor") as t:
            try:
                # K8sService.execute_intent — sync (subprocess), не блокируем loop.
                result = await asyncio.to_thread(
                    k8s_service.execute_intent,
                    self.execution_intent,
                    True,  # dry_run=True
                )
                self.executor_result = {
                    "status": "dry_run_ok" if result.get("success") else "dry_run_failed",
                    "command": result.get("command"),
                    "stdout": (result.get("stdout") or "")[:2048],
                    "stderr": (result.get("stderr") or "")[:2048],
                    "exit_code": None if "exit_code" not in result else int(result.get("exit_code") or 0),
                }
                if result.get("error", "").startswith("GUARDRAIL_BLOCK"):
                    self.executor_result["status"] = "guardrail_blocked"
                    self.executor_result["reason"] = result["error"]
            except Exception as e:
                self.executor_result = {
                    "status": "error",
                    "error_type": type(e).__name__,
                    "error": str(e)[:512],
                }
                audit_service.log_event(
                    "EXECUTOR_DRY_RUN_FAILED",
                    {"incident_id": self.incident_id, "error": type(e).__name__},
                )
        self.traces.append(t.snapshot().to_dict())
        self.root_span.set_attribute(
            "sre.incident.executor_status", self.executor_result["status"]
        )
        track_executor_status(self.executor_result["status"])

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

        # Quality metrics (Grok review #7) — финальный аккорд per-incident.
        track_resolution_quality(resolution_quality)
        if self.fact_store is not None:
            for a, b in self.fact_store.conflicts():
                track_fact_conflict(a.kind, b.kind)

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
                "blast_radius_context": self.blast_radius_context,
                "statics_check_context": self.statics_check_context,
                "execution_intent": (
                    self.execution_intent.model_dump(mode="json")
                    if self.execution_intent is not None
                    else None
                ),
                "executor_result": self.executor_result,
            }
            # Если ни одна гипотеза не пережила critique (self.best is None,
            # resolution_quality=="unresolved") — инцидент НЕ разрешён, уводим
            # его в TRIAGE_REQUIRED вместо RESOLVED. Это прекращает помечать
            # неразрешённые инциденты как RESOLVED.
            final_state = (
                IncidentState.RESOLVED if self.best else IncidentState.TRIAGE_REQUIRED
            )
            self._safe_transition(final_state, synth_snap)
            record.trace = self.traces + [synth_snap]
            self.db.commit()
        else:
            self.traces.append(synth_snap)

        labels = self.incident.labels or {}
        alertname = labels.get("alertname", "UnknownAlert")

        # Wave 3 #13: recurrence counts 24h/7d (best-effort).
        self._compute_recurrence_counts(alertname)
        # Wave 3 #10: если team_owner ещё не определён (например deploy
        # correlation не сработал), пробуем дорезолвить по сервису.
        if not self.team_owner:
            self._resolve_team_owner()

        # incident_ts для #8 log error rate window.
        incident_ts = None
        if self.incident.starts_at:
            try:
                incident_ts = datetime.fromisoformat(
                    self.incident.starts_at.replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                incident_ts = None

        await discord_service.send_incident_report(
            incident_id=self.incident_id,
            alertname=alertname,
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
            execution_intent=self.execution_intent,
            executor_result=self.executor_result,
            deploy_correlation=self.deploy_correlation,
            team_owner=self.team_owner,
            recurrence_count_24h=self.recurrence_count_24h,
            recurrence_count_7d=self.recurrence_count_7d,
            incident_ts=incident_ts,
        )

    def _compute_recurrence_counts(self, alertname: str) -> None:
        """Wave 3 #13: посчитать сколько раз alertname сработал за 24h/7d.

        Берём из kg_alerts: fired_at в окне OR resolved_at в окне (alert мог
        начаться раньше, разрешиться внутри). Best-effort: при любой ошибке
        оставляем 0.
        """
        try:
            from datetime import datetime as _dt, timedelta as _td

            from sqlalchemy import or_

            from app.knowledge_graph.schema import AlertEvent

            now = _dt.utcnow()
            cutoff_24h = now - _td(hours=24)
            cutoff_7d = now - _td(days=7)
            base = self.db.query(AlertEvent).filter(AlertEvent.alertname == alertname)
            cnt_24h = base.filter(
                or_(
                    AlertEvent.fired_at >= cutoff_24h,
                    AlertEvent.resolved_at >= cutoff_24h,
                )
            ).count()
            cnt_7d = base.filter(
                or_(
                    AlertEvent.fired_at >= cutoff_7d,
                    AlertEvent.resolved_at >= cutoff_7d,
                )
            ).count()
            # Защита от mock'нутых session (тесты): принимаем только int.
            self.recurrence_count_24h = int(cnt_24h) if isinstance(cnt_24h, int) else 0
            self.recurrence_count_7d = int(cnt_7d) if isinstance(cnt_7d, int) else 0
        except Exception as e:
            audit_service.log_event(
                "RECURRENCE_COUNT_FAILED",
                {"incident_id": self.incident_id, "error": type(e).__name__},
            )

    def _resolve_team_owner(self) -> None:
        """Wave 3 #10: подтянуть team_owner по (namespace, service)."""
        if not self.incident or not self.incident.namespace:
            return
        labels = self.incident.labels or {}
        service_name = labels.get("service") or labels.get("app")
        if not service_name:
            return
        try:
            svc = (
                self.db.query(Service)
                .filter(
                    Service.namespace == self.incident.namespace,
                    Service.name == service_name,
                )
                .one_or_none()
            )
            if svc and svc.team_owner:
                self.team_owner = svc.team_owner
        except Exception:
            # best-effort, без логирования (не критично для embed)
            return

    # ------------------------------------------------------------------
    # Orchestrator
    # ------------------------------------------------------------------

    async def run(self) -> None:
        self.incident = Incident(**self.incident_data)
        # _populate_kg — best-effort, оставляем без timeout-обёртки.
        await self._populate_kg()
        # LLM/enrichment-стадии — под per-stage timeout (_STAGE_TIMEOUT).
        # TimeoutError пробрасывается → вызывающий код переводит в FAILED.
        await _staged(self.stage_analyze())
        await _staged(self.stage_diagnose())
        await _staged(self.stage_hypothesize())
        await _staged(self.stage_critique())
        await _staged(self.stage_jira_enrich())
        await _staged(self.stage_fix())
        await _staged(self.stage_risk())
        await _staged(self.stage_executor())
        await _staged(self.stage_synthesize())
