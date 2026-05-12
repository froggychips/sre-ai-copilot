import asyncio

from celery import Celery

from app.agents.analyzer import AnalyzerAgent
from app.agents.fact_critic import (FactCriticAgent, best_candidate, refuted,
                                    survivors)
from app.agents.fix import FixAgent
from app.agents.multi_hypothesis import MultiHypothesisAgent
from app.agents.risk import RiskAgent
from app.agents.synthesis import SynthesisAgent
from app.config import settings
from app.core.intelligence.similar_incidents import SimilarIncidentEngine
from app.core.state_machine import IncidentState, StateMachine
from app.core.tracing import StageTimer
from app.database import IncidentRecord, SessionLocal
from app.diagnostics import default_engine as diag_engine
from app.context.k8s_facts import K8sFacts
from app.context.vm_client import VMClient
from app.diagnostics.facts import FactStore
from app.diagnostics.incident_ctx import build_diagnostics_ctx
from app.services.teamcity_service import incident_teamcity_context
from app.knowledge_graph.auto_populator import populate_from_incident
from app.models.incident import Incident
from app.observability.ai_metrics import (track_disagreement,
                                          track_no_survivor)
from app.services.audit_logger import audit_service
from app.services.discord_service import discord_service


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


celery_app = Celery("sre_tasks", broker=settings.REDIS_URL, backend=settings.REDIS_URL)

if settings.CELERY_TASK_ALWAYS_EAGER:
    # Inline-режим для локального e2e: process_incident_task.delay(...)
    # выполняется синхронно в текущем процессе, без Redis/worker-а.
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True


@celery_app.task(name="process_incident", bind=True, max_retries=3)
def process_incident_task(self, incident_data: dict):
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(async_process_incident(incident_data))


def _serialize_hypotheses_for_synthesis(critiqued, facts: FactStore) -> str:
    """Превратить HypothesisSet в текст для SynthesisAgent.

    SynthesisAgent читает строку, не структуру (его контракт остался прежним).
    Чтобы synthesis-отчёт всё-таки опирался на anchor-структуру, мы
    рендерим в plain-text:
      * выживших с их anchors,
      * отказанных с их refutations,
      * сводный набор фактов.
    """
    lines = ["=== HYPOTHESES (fact-anchored multi-perspective) ==="]
    surv = survivors(critiqued).items
    ref = refuted(critiqued).items
    if not surv and not ref:
        lines.append("(no hypotheses produced — facts may be insufficient)")
    for h in surv:
        lines.append(
            f"[SURVIVED] perspective={h.perspective} conf={h.confidence:.2f}"
        )
        lines.append(f"  cause: {h.cause}")
        if h.detail:
            lines.append(f"  detail: {h.detail}")
        lines.append(f"  anchored_facts: {h.anchored_facts}")
    for h in ref:
        lines.append(
            f"[REFUTED]  perspective={h.perspective} conf={h.confidence:.2f}"
        )
        lines.append(f"  cause: {h.cause}")
        lines.append(f"  refuted_by: {h.refutations}")
    lines.append("")
    lines.append(facts.to_prompt_context())
    return "\n".join(lines)


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

        # Auto-populate knowledge graph (best-effort, не валит pipeline).
        # Каждый инцидент добавляет Service + AlertEvent + Deployments
        # из TC-context. Со временем граф наполняется без отдельной cron.
        try:
            populate_from_incident(db, incident)
            db.commit()
        except Exception as e:
            audit_service.log_event(
                "KG_POPULATE_FAILED",
                {"incident_id": incident_id, "error": type(e).__name__},
            )
            db.rollback()

        # Stage 1: Analyzer — читает alert + контекст, формирует summary
        # для дальнейших стадий. Транзит OPEN → INVESTIGATING.
        async with StageTimer("analyzer") as t:
            analyzer = AnalyzerAgent()
            analysis = await analyzer.analyze(incident)
        snap = t.snapshot().to_dict()
        safe_transition(IncidentState.INVESTIGATING, snap)
        traces.append(snap)

        # Stage 2: Diagnostics (deterministic) — выдаёт structured FactStore.
        # Это сердцевина fact-anchored архитектуры: LLM-стадии дальше
        # работают ПОВЕРХ фактов, а не сырого текста alert-а.
        async with StageTimer("diagnostics") as t:
            # TC context — enriches recent_deployments перед build_diagnostics_ctx.
            # Best-effort: squad-N namespaces не маппятся (branch_for_namespace=None),
            # prod/preprod/squad-gd получают список билдов за TC_LOOKBACK_MINUTES.
            if incident.teamcity_context is None:
                try:
                    tc_ctx = await incident_teamcity_context(
                        namespace=incident.namespace,
                        incident_starts_at=incident.starts_at,
                    )
                    if tc_ctx:
                        incident.teamcity_context = tc_ctx
                except Exception as _tc_err:
                    audit_service.log_event(
                        "TC_ENRICHMENT_FAILED",
                        {"incident_id": incident_id, "error": type(_tc_err).__name__},
                    )

            diag_ctx = build_diagnostics_ctx(
                incident=incident, analyzer_summary=analysis, kg_session=db
            )

            # K8s enrichment: pod logs (previous first, tail 200), terminated
            # reasons, events, core dump detection. Best-effort.
            if incident.namespace:
                try:
                    k8s_snap = await K8sFacts.collect_snapshot(
                        namespace=incident.namespace,
                        pod=incident.labels.get("pod"),
                    )
                    diag_ctx["logs_summary"] = k8s_snap.text
                    diag_ctx["k8s_pod_state"] = k8s_snap.container_terminated
                    diag_ctx["k8s_events"] = k8s_snap.pod_events
                    if k8s_snap.core_dump_node:
                        diag_ctx["core_dump_node"] = k8s_snap.core_dump_node
                except Exception as _k8s_err:
                    audit_service.log_event(
                        "K8S_ENRICHMENT_FAILED",
                        {"incident_id": incident_id, "error": type(_k8s_err).__name__},
                    )

            # VictoriaMetrics: memory/CPU trend за 15 мин до инцидента.
            # Best-effort: если URL не задан или VM недоступна — молчим.
            if settings.VICTORIA_METRICS_URL and incident.namespace:
                try:
                    from datetime import datetime, timezone
                    pod = incident.labels.get("pod", "")
                    incident_ts = None
                    if incident.starts_at:
                        try:
                            incident_ts = datetime.fromisoformat(
                                incident.starts_at.replace("Z", "+00:00")
                            )
                        except ValueError:
                            pass
                    vm = VMClient(
                        settings.VICTORIA_METRICS_URL,
                        timeout=10.0,
                    )
                    metrics = await vm.get_pod_metrics(
                        namespace=incident.namespace,
                        pod=pod,
                        window_minutes=settings.VICTORIA_METRICS_WINDOW_MINUTES,
                        incident_time=incident_ts,
                    )
                    diag_ctx["metrics_summary"] = metrics
                except Exception as _vm_err:
                    audit_service.log_event(
                        "VM_ENRICHMENT_FAILED",
                        {"incident_id": incident_id, "error": type(_vm_err).__name__},
                    )

            fact_store = diag_engine.run(diag_ctx)
        snap = t.snapshot().to_dict()
        safe_transition(IncidentState.FACTS_COLLECTED, snap)
        traces.append(snap)

        # Stage 3: Fan-out hypothesis (3 perspective-агента параллельно)
        # + anchor-валидация. SimilarIncidentEngine подмешивается в
        # incident_summary как мини-context из похожих ACCEPTED-инцидентов.
        # Не делает state transition — это часть рассуждения, формальное
        # HYPOTHESIS_GENERATED ставится после критика.
        similar_past = SimilarIncidentEngine.find(
            current_incident=incident_data, limit=3
        )
        if similar_past:
            past_bullets = "\n".join(
                f"- score={p.get('score', '?')} cause={(p.get('root_cause') or '?')[:200]}"
                for p in similar_past
            )
            multi_summary = (
                f"{analysis}\n\nSimilar past resolutions (consider as patterns, "
                f"verify against current facts):\n{past_bullets}"
            )
        else:
            multi_summary = analysis

        async with StageTimer("hypothesis") as t:
            mha = MultiHypothesisAgent()
            hypothesis_set = await mha.generate(
                incident_summary=multi_summary, facts=fact_store
            )
        traces.append(t.snapshot().to_dict())

        # Stage 4: Fact-based adversarial critic — отбраковывает гипотезы,
        # анchor-факты которых не observed (или anchor с низкой
        # уверенностью).
        async with StageTimer("critic") as t:
            critic = FactCriticAgent()
            critiqued = await critic.critique_all(hypothesis_set, fact_store)
        traces.append(t.snapshot().to_dict())

        # Стадии 3+4 в сумме = выбранная гипотеза. Это и есть момент
        # HYPOTHESIS_GENERATED. Текст для последующих агентов формируется
        # из best_candidate; если выживших нет — pipeline всё равно идёт
        # до конца с явной пометкой «manual triage».
        if critiqued.disagreement_signal() is not None:
            track_disagreement()

        best = best_candidate(critiqued)
        if best is None:
            track_no_survivor()
            final_cause = (
                "No hypothesis survived adversarial critique. "
                f"Observed facts: {sorted(fact_store.observed_kinds())}. "
                "Manual triage required."
            )
            hypotheses_text = _serialize_hypotheses_for_synthesis(
                critiqued, fact_store
            )
        else:
            final_cause = best.cause + (f" — {best.detail}" if best.detail else "")
            hypotheses_text = _serialize_hypotheses_for_synthesis(
                critiqued, fact_store
            )
        # safe_transition сюда, потому что critic уже отработал.
        if traces:
            traces[-1]["state_after"] = IncidentState.HYPOTHESIS_GENERATED.value
        if record is not None:
            transition_to(record, IncidentState.HYPOTHESIS_GENERATED, db)

        # Stage 5: Fix — поверх final_cause. Контракт FixAgent не менялся.
        async with StageTimer("fix") as t:
            fixer = FixAgent()
            fix_suggestion = await fixer.suggest(final_cause)
        snap = t.snapshot().to_dict()
        safe_transition(IncidentState.FIX_PROPOSED, snap)
        traces.append(snap)

        # Stage 6: Risk — оценка предложенного фикса.
        async with StageTimer("risk") as t:
            risker = RiskAgent()
            risk_report = await risker.assess(fix_suggestion)
        traces.append(t.snapshot().to_dict())

        # Stage 7: Synthesis — финальный отчёт. SynthesisAgent ничего не
        # знает про FactStore; ему передаётся подготовленный текст с
        # SURVIVED/REFUTED + facts-блок.
        async with StageTimer("synthesis") as t:
            synthesizer = SynthesisAgent()
            synthesis = await synthesizer.synthesize(
                incident_id=incident_id,
                analysis=analysis,
                hypotheses=hypotheses_text,
                final_cause=final_cause,
                fix_suggestion=fix_suggestion,
                risk_report=risk_report,
            )
        synth_snap = t.snapshot().to_dict()

        # Persistence — analysis bundle + структурированный fact-anchored
        # блок + полный per-stage trace. Все JSON-колонки опциональны,
        # старые строки остаются backward-совместимыми.
        record = (
            db.query(IncidentRecord)
            .filter(IncidentRecord.incident_id == incident_id)
            .first()
        )
        if record:
            record.analysis = {
                "summary": analysis,
                "hypotheses": hypotheses_text,
                "cause": final_cause,
                "fix": fix_suggestion,
                "risk": risk_report,
                "synthesis": synthesis,
                "similar_past_count": len(similar_past),
                # Fact-anchored details для post-mortem и regression-тестов:
                "facts": fact_store.to_dict()["facts"],
                "hypothesis_set": [h.model_dump() for h in critiqued.items],
                "best_candidate": best.model_dump() if best else None,
                "disagreement_signal": critiqued.disagreement_signal(),
                "consensus_kinds": critiqued.consensus_kinds(),
            }
            safe_transition(IncidentState.RESOLVED, synth_snap)
            record.trace = traces + [synth_snap]
            db.commit()
        else:
            # Tests that bypass the DB still get the in-memory trace.
            traces.append(synth_snap)

        await discord_service.send_report(
            f"**Incident {incident_id} Analysis Complete.**\n\n{synthesis}"
        )

    except Exception as e:
        # Любая ошибка агента/синтеза — FAILED.
        # validate_transition разрешает X → FAILED из любого non-terminal.
        if record is not None:
            try:
                transition_to(record, IncidentState.FAILED, db)
            except ValueError:
                # Already in terminal state — leave as is.
                db.rollback()
        else:
            db.rollback()
        raise e
    finally:
        db.close()
