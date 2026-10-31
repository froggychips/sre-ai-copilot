"""Детерминированный мост: правила → v2-playbook → карточка с кнопкой.

До моста remediation-слой (matcher, binding, gate, executor, verify) был
достижим только из LLM-пайплайна (`pipeline._candidate_playbooks` →
FixAgent), а он на проде выключен. Прод-путь алертов — `/store` и
`/enrich-and-forward` — считал факты правилами (`enrich_alert`), но дальше
описания дело не шло. Мост соединяет их без модели:

    enrich_alert (rule_facts) + K8sFacts (terminated-state, live events)
        → match_playbooks(alertname, classification, facts)
        → render_plan: intent = первый мутирующий шаг plan.steps (сервер, не LLM)
        → build_match_snapshot + bind_server_params (тот же снимок, что у pipeline)
        → evaluate_intent_gate(require_binding=True)
              ├─ block   → строка «playbook X: заблокирован policy (tier)» в
              │            обычной карточке + решение в kg_remediation_decisions
              └─ approve → снимок цели (uid) → дедуп (playbook, uid, incarnation)
                           → server dry-run → IncidentRecord(APPROVAL_PENDING)
                           + kg_remediation_decisions → карточка с кнопками
                           `approve:/decline:` в канал DISCORD_INCIDENT_CHANNEL_ID

Дальше путь общий с LLM-веткой: клик → `_is_authorized_approver`
(DISCORD_APPROVERS_USER_IDS, fail-closed) → kg_action_approvals →
`apply_intent` (подпись, свежесть, gate со снимком, пере-dry-run, uid цели)
→ verify; итог verify дописывается в ту же карточку
(`annotate_verify_outcome`).

Сквады. Их алерты идут в `/store` и в канал по решению владельца не
попадают. Мост — узкое исключение: сквадовый алерт даёт карточку ТОЛЬКО
когда по нему сматчился playbook с policy approve, и не чаще одной на
(playbook, uid цели, incarnation) за REMEDIATION_BRIDGE_DEDUP_SECONDS.
Всё остальное по сквадам — как раньше, только KG.

Флаг — REMEDIATION_BRIDGE_ENABLED (default False). Выключен — ни один путь
сюда не заходит.
"""
from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Dict, FrozenSet, List, Optional, Set

import structlog

from app.config import settings

if TYPE_CHECKING:
    from app.core.execution_dsl import ExecutionIntent
    from app.diagnostics.facts import Fact, FactStore
    from app.models.incident import Incident
    from app.services.alert_enrichment import EnrichedContext

log = structlog.get_logger()

__all__ = [
    "BRIDGE_SOURCE",
    "Proposal",
    "annotate_verify_outcome",
    "bridge_enabled",
    "is_bridge_analysis",
    "plan_proposals",
    "run_bridge",
    "run_bridge_for_incident",
    "schedule_bridge_for_incident",
    "wants_bridge",
]

# Метка в analysis инцидента, созданного мостом. По ней apply-путь включает
# сверку intent-а со снимком независимо от REMEDIATION_PLAYBOOK_BINDING_ENABLED.
BRIDGE_SOURCE = "remediation_bridge"
_DEDUP_PREFIX = "copilot:remediation_bridge:"
_IDEM_PREFIX = "bridge:"
_BLOCK_IDEM_PREFIX = "bridge-block:"

# Исходы одного предложения — для лога/аудита и тестов.
OUTCOME_PROPOSED = "proposed"
OUTCOME_BLOCKED = "blocked"
OUTCOME_DEDUPLICATED = "deduplicated"
OUTCOME_EXECUTOR_DISABLED = "executor_disabled"
OUTCOME_TARGET_UNAVAILABLE = "target_unavailable"
OUTCOME_TARGET_REINCARNATED = "target_reincarnated"
OUTCOME_DRY_RUN_FAILED = "dry_run_failed"
OUTCOME_POST_FAILED = "post_failed"


def bridge_enabled() -> bool:
    return bool(getattr(settings, "REMEDIATION_BRIDGE_ENABLED", False))


def is_bridge_analysis(analysis: Any) -> bool:
    return isinstance(analysis, dict) and analysis.get("source") == BRIDGE_SOURCE


@lru_cache(maxsize=1)
def _candidate_alertnames() -> FrozenSet[str]:
    """alertnames всех исполнимых playbook-ов: дешёвый фильтр до сбора фактов."""
    from app.remediation.matcher import default_registry
    names: set = set()
    for pb in default_registry().values():
        if pb.executable:
            names.update(pb.match.alertnames or ())
    return frozenset(names)


def wants_bridge(labels: Optional[Dict[str, Any]]) -> bool:
    """Стоит ли гнать алерт через мост: флаг + его alertname есть в реестре.

    Только по alertname: playbook-ам с одним `match.classification` нужен
    классификатор с сигналами, которых на прод-пути нет, — их мост пока не
    берёт. Ошибка реестра — False (fail-closed, алерт идёт старым путём).
    """
    if not bridge_enabled():
        return False
    try:
        return (labels or {}).get("alertname") in _candidate_alertnames()
    except Exception as e:
        log.warning("remediation_bridge.registry_failed", error=type(e).__name__)
        return False


# ── факты ─────────────────────────────────────────────────────────────────

def pod_state_facts(
    ctx: "EnrichedContext", snap: Any,
) -> List["Fact"]:
    """Факты по terminated-state и live-событиям пода из снимка K8sFacts.

    `enrich_alert` строит факты по графу (деплои, события из kg_pod_events),
    но не по состоянию контейнера: `oom_killed` там бывает только FOUND, а
    `process_crash` (с `exit_code`) не появляется вовсе — и preconditions
    `restart_crashloop_deployment` не выполнились бы никогда. Здесь те же
    правила, что гоняет пайплайн по K8sFacts (OOMKilledRule,
    ProcessCrashRule, PodEventsRule), без нового источника. Снимок не снят
    (None/error) — источники помечаются в source_status, и ABSENT
    понижается до UNKNOWN: «OOM не было» без данных — пробел, не факт.
    """
    from app.diagnostics.rules.oom import OOMKilledRule
    from app.diagnostics.rules.pod_events import PodEventsRule
    from app.diagnostics.rules.process_crash import ProcessCrashRule

    incident = ctx.incident
    labels = incident.labels or {}
    source_status: Dict[str, str] = {}
    if snap is None or getattr(snap, "error", None):
        reason = f"failed: {getattr(snap, 'error', None) or 'k8s_facts_unavailable'}"
        for fld in ("k8s_pod_state", "logs_summary", "k8s_events"):
            source_status[fld] = reason
    rule_ctx: Dict[str, Any] = {
        "incident": incident.model_dump(),
        "namespace": incident.namespace or labels.get("namespace"),
        "service": ctx.service,
        "pod": ctx.pod_name or labels.get("pod") or ctx.service,
        "alertname": labels.get("alertname", ""),
        "description": incident.description or "",
        "k8s_pod_state": dict(getattr(snap, "container_terminated", None) or {}),
        "logs_summary": getattr(snap, "text", None) or "",
        "k8s_events": list(getattr(snap, "pod_events", None) or []),
        "source_status": source_status,
    }
    facts: List["Fact"] = []
    for rule in (OOMKilledRule(), ProcessCrashRule(), PodEventsRule()):
        try:
            facts.extend(rule.run(rule_ctx))
        except Exception as e:
            log.warning("remediation_bridge.rule_failed", rule=rule.name, error=type(e).__name__)
    return facts


async def collect_facts(ctx: "EnrichedContext") -> "FactStore":
    """rule_facts карточки + факты состояния пода (см. pod_state_facts)."""
    from app.context.k8s_facts import K8sFacts
    from app.diagnostics.facts import FactStore

    store = FactStore(list(ctx.rule_facts or []))
    labels = ctx.incident.labels or {}
    namespace = ctx.incident.namespace or labels.get("namespace")
    snap: Any = None
    if namespace:
        try:
            snap = await K8sFacts.collect_snapshot(
                namespace, ctx.pod_name or labels.get("pod"),
            )
        except Exception as e:
            log.warning("remediation_bridge.k8s_facts_failed", error=type(e).__name__)
    store.extend(pod_state_facts(ctx, snap))
    return store


# ── план и политика ───────────────────────────────────────────────────────

@dataclass
class Proposal:
    """Одно совпавшее предложение: playbook + intent + решение policy."""

    playbook: str
    namespace: str
    resource_name: str
    tier: str
    mode: str                         # approve | block | auto
    reasons: List[Dict[str, Any]]
    intent: "ExecutionIntent"
    snapshot: Dict[str, Any]
    target_ref: Dict[str, Any]
    classification: Optional[str]
    command_preview: str
    preconditions: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.mode == "block"

    def blocked_line(self) -> str:
        return f"playbook `{self.playbook}`: заблокирован policy ({self.tier})"


def _probe_current_replicas(intent: "ExecutionIntent") -> Optional[int]:
    """Живое spec.replicas цели — серверный параметр плана (как у pipeline)."""
    from app.remediation.verification import snapshot_target
    snap = snapshot_target(intent)
    return None if snap.unknown else snap.replicas_desired


def _target_name(ctx: "EnrichedContext") -> Optional[str]:
    labels = ctx.incident.labels or {}
    return labels.get("deployment") or ctx.service or None


def plan_proposals(
    ctx: "EnrichedContext", facts: "FactStore", db: Any = None,
) -> List[Proposal]:
    """Совпавшие v2-playbook-и → intent-ы и решения policy. Без сети, кроме
    `bind_server_params` (kubectl get, только если план ждёт current_replicas).

    Intent собирает сервер из plan.steps: первый мутирующий шаг (немутирующие
    describe/get_logs исполнять по кнопке незачем). Снимок и привязка — те же
    функции, что у pipeline для FixAgent (`build_match_snapshot`,
    `bind_server_params`); gate вызывается с require_binding=True.
    """
    from app.core.execution_dsl import DSLTranslator, action_spec
    from app.remediation.binding import (bind_server_params,
                                         build_match_snapshot, find_entry)
    from app.remediation.executor_gate import evaluate_intent_gate
    from app.remediation.matcher import (PlanRenderError, classify_alert,
                                         match_playbooks, render_plan)
    from app.remediation.risk_axes import _classify_namespace
    from app.remediation.target_resolver import resolve_target

    labels = dict(ctx.incident.labels or {})
    alertname = labels.get("alertname")
    namespace = ctx.incident.namespace or labels.get("namespace")
    name = _target_name(ctx)
    if not namespace or not name:
        return []
    classification = classify_alert(labels)
    try:
        candidates = match_playbooks(
            alertname=alertname, classification=classification, facts=facts,
        )
    except Exception as e:
        log.warning("remediation_bridge.match_failed", error=type(e).__name__)
        return []

    proposals: List[Proposal] = []
    for pb in candidates:
        try:
            rendered = render_plan(pb, namespace=namespace, resource_name=name)
        except (PlanRenderError, ValueError) as e:
            log.info("remediation_bridge.plan_render_failed", playbook=pb.name, error=str(e)[:160])
            continue
        mutating = [intent for intent, _ in rendered if action_spec(intent.action).mutating]
        if not mutating:
            continue
        snapshot = build_match_snapshot(
            [pb], facts=facts, namespace=namespace,
            alertname=alertname, classification=classification,
        )
        entry = find_entry(snapshot, pb.name)
        if entry is None:
            continue
        intent = mutating[0].model_copy(update={"playbook_match": entry.get("binding")})
        intent = bind_server_params(intent, snapshot, _probe_current_replicas)
        decision = evaluate_intent_gate(intent, match_snapshot=snapshot, require_binding=True)
        target = resolve_target(
            {"labels": labels},
            facts={"target": {"kind": "Deployment", "namespace": namespace, "name": name}},
            kg_session=db,
        )
        proposals.append(Proposal(
            playbook=pb.name,
            namespace=namespace,
            resource_name=name,
            tier=_classify_namespace(namespace).value,
            mode=decision.mode.value,
            reasons=list(decision.reasons),
            intent=intent,
            snapshot=snapshot,
            target_ref=target.to_dict(),
            classification=classification,
            command_preview=" ".join(DSLTranslator.to_argv(intent)),
            preconditions=list(entry.get("preconditions") or []),
        ))
    return proposals


# ── дедуп ─────────────────────────────────────────────────────────────────

def dedup_key(playbook: str, uid: str, incarnation: Any) -> str:
    raw = f"{playbook}|{uid}|{incarnation if incarnation is not None else '-'}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _redis() -> Any:
    try:
        import redis
        return redis.from_url(settings.REDIS_URL, socket_timeout=2)
    except Exception:
        return None


def _window_seconds() -> int:
    try:
        return max(1, int(getattr(settings, "REMEDIATION_BRIDGE_DEDUP_SECONDS", 21600)))
    except (TypeError, ValueError):
        return 21600


def _recent_decision_exists(db: Any, key: str) -> bool:
    """Фолбэк дедупа без Redis: решение моста по ключу в окне уже записано."""
    from app.remediation.models import RemediationDecision
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=_window_seconds())
    row = (
        db.query(RemediationDecision.id)
        .filter(
            RemediationDecision.idempotency_key == _IDEM_PREFIX + key,
            RemediationDecision.created_at >= since,
        )
        .first()
    )
    return row is not None


def claim_proposal(db: Any, key: str) -> bool:
    """Атомарно занять ключ на окно. False — предложение уже было.

    Redis `SET NX EX`: два батча одного алерта, пришедшие одновременно, не
    выпустят две карточки. Без Redis — по kg_remediation_decisions (окно то
    же; гонка двух одновременных батчей тогда возможна, лишняя карточка —
    не лишний kubectl: исполнение всё равно одно на клик и на подпись).
    БД тоже недоступна — False: не уверены, что не слали, — не шлём.
    """
    r = _redis()
    if r is not None:
        try:
            return bool(r.set(_DEDUP_PREFIX + key, "1", nx=True, ex=_window_seconds()))
        except Exception as e:
            log.debug("remediation_bridge.redis_claim_failed", error=type(e).__name__)
    try:
        return not _recent_decision_exists(db, key)
    except Exception as e:
        log.warning("remediation_bridge.dedup_db_failed", error=type(e).__name__)
        return False


def release_proposal(key: str) -> None:
    """Снять claim, если карточка не ушла: следующий батч попробует снова."""
    r = _redis()
    if r is None:
        return
    try:
        r.delete(_DEDUP_PREFIX + key)
    except Exception as e:
        log.debug("remediation_bridge.redis_release_failed", error=type(e).__name__)


# ── dry-run, запись, карточка ─────────────────────────────────────────────

def _dry_run(intent: "ExecutionIntent") -> Dict[str, Any]:
    """Server-side dry-run тем же путём, что stage_executor пайплайна."""
    from app.workers.executor_tasks import (queue_dispatch_enabled,
                                            run_dry_run_via_queue)
    try:
        if queue_dispatch_enabled():
            result = run_dry_run_via_queue(intent.model_dump(mode="json"))
        else:
            from app.services.k8s_service import k8s_service
            result = k8s_service.execute_intent(intent, True)
    except Exception as e:
        return {"status": "error", "error_type": type(e).__name__,
                "dry_run_at": datetime.now(timezone.utc).isoformat()}
    status = "dry_run_ok" if result.get("success") else "dry_run_failed"
    if (result.get("error") or "").startswith("GUARDRAIL_BLOCK"):
        status = "guardrail_blocked"
    return {
        "status": status,
        "command": result.get("command"),
        "stdout": (result.get("stdout") or "")[:2048],
        "stderr": (result.get("stderr") or "")[:2048],
        "exit_code": result.get("exit_code"),
        # Якорь свежести плана для apply_intent (intent_recorded_at).
        "dry_run_at": datetime.now(timezone.utc).isoformat(),
    }


def _record_decision(
    db: Any, *, incident_id: str, fingerprint: str, proposal: Proposal,
    idempotency_key: str, reasons: List[Dict[str, Any]],
) -> None:
    """Строка kg_remediation_decisions. Повтор того же ключа — no-op."""
    from sqlalchemy.exc import IntegrityError

    from app.remediation.models import RemediationDecision
    try:
        with db.begin_nested():
            db.add(RemediationDecision(
                incident_id=incident_id,
                alert_fingerprint=fingerprint,
                target_ref=proposal.target_ref,
                classification=proposal.classification,
                classification_provenance={"source": BRIDGE_SOURCE},
                candidate_playbooks=[proposal.playbook],
                selected_playbook=proposal.playbook,
                decision=proposal.mode,
                decision_reasons=reasons,
                command_preview=proposal.command_preview,
                idempotency_key=idempotency_key,
            ))
    except IntegrityError:
        pass


def _check_mark(check: Dict[str, Any]) -> str:
    actual = ",".join(str(a) for a in check.get("actual") or [])
    return f"{'✓' if check.get('ok') else '✗'} {check.get('fact')}: {actual}"


def build_card(
    proposal: Proposal, *, incident_id: str, signature: str, alertname: str,
) -> Dict[str, Any]:
    """Карточка предложения: что сделаем, почему, и две кнопки."""
    why = "\n".join(_check_mark(c) for c in proposal.preconditions) or "—"
    embed: Dict[str, Any] = {
        "title": f"🛠 {proposal.playbook}: {proposal.namespace}/{proposal.resource_name}"[:256],
        "description": (
            f"`{alertname}` сматчил playbook по фактам правил, без LLM. "
            "Server dry-run прошёл. Исполнится только после нажатия, "
            "и только у владельца."
        ),
        "color": 0x5865F2,
        "fields": [
            {"name": "Команда", "value": f"`{proposal.command_preview}`"[:1024], "inline": False},
            {"name": "Почему (preconditions)", "value": why[:1024], "inline": False},
            {"name": "Policy", "value": f"{proposal.mode} ({proposal.tier})", "inline": True},
        ],
        "footer": {"text": f"incident/{incident_id} · bridge"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return {
        "embeds": [embed],
        "components": [{"type": 1, "components": [
            {"type": 2, "style": 3, "label": "Применить",
             "custom_id": f"approve:{incident_id}:{signature}"},
            {"type": 2, "style": 4, "label": "Отклонить",
             "custom_id": f"decline:{incident_id}:{signature}"},
        ]}],
        "allowed_mentions": {"parse": []},
    }


async def _propose(db: Any, incident: "Incident", proposal: Proposal) -> str:
    """approve-ветка: снимок цели → дедуп → dry-run → запись → карточка."""
    from sqlalchemy.orm.attributes import flag_modified

    from app.core.state_machine import IncidentState
    from app.database import IncidentRecord
    from app.remediation.verification import snapshot_target
    from app.services.audit_logger import audit_service
    from app.services.discord_service import DiscordService
    from app.services.intent_signature import compute_signature

    if not (settings.EXECUTOR_ENABLED and settings.EXECUTOR_APPROVAL_ENABLED):
        # Кнопка без исполнителя — обещание, которое клик не выполнит.
        return OUTCOME_EXECUTOR_DISABLED

    live = await asyncio.to_thread(snapshot_target, proposal.intent)
    if live.unknown:
        return OUTCOME_TARGET_UNAVAILABLE
    kg_uid = proposal.target_ref.get("uid")
    if kg_uid and live.uid and str(kg_uid) != str(live.uid):
        # Граф знает другой объект с этим именем — предлагать нечего, apply
        # всё равно отказал бы по target_reincarnated.
        return OUTCOME_TARGET_REINCARNATED
    uid = str(live.uid or kg_uid or f"{proposal.namespace}/{proposal.resource_name}")
    proposal.target_ref["uid"] = live.uid or kg_uid
    key = dedup_key(proposal.playbook, uid, proposal.target_ref.get("incarnation"))
    if not claim_proposal(db, key):
        return OUTCOME_DEDUPLICATED

    executor_result = await asyncio.to_thread(_dry_run, proposal.intent)
    if executor_result.get("status") != "dry_run_ok":
        # Claim не снимаем: детерминированный отказ dry-run-а повторится и на
        # следующем батче — не долбим apiserver каждые repeat_interval.
        audit_service.log_event("REMEDIATION_BRIDGE_DRY_RUN_FAILED", {
            "playbook": proposal.playbook, "namespace": proposal.namespace,
            "resource": proposal.resource_name, "status": executor_result.get("status"),
        })
        return OUTCOME_DRY_RUN_FAILED

    # Своя строка инцидента на каждое предложение: apply/attempts/approvals
    # ключуются incident_id, и одобрение одной карточки не должно покрывать
    # следующую. Случайный хвост — два предложения в одну секунду (повтор
    # после неудачной отправки) не сталкиваются.
    incident_id = f"rb-{key[:12]}-{int(time.time())}-{secrets.token_hex(3)}"
    signature = compute_signature(proposal.intent)
    now_iso = datetime.now(timezone.utc).isoformat()
    analysis: Dict[str, Any] = {
        "source": BRIDGE_SOURCE,
        "alert_fingerprint": incident.incident_id,
        "execution_intent": proposal.intent.model_dump(mode="json"),
        "execution_intent_at": now_iso,
        "executor_result": executor_result,
        "playbook_match": proposal.snapshot,
        "policy_decision": {"mode": proposal.mode, "reasons": proposal.reasons},
        "target_live": live.to_dict(),
    }
    record = IncidentRecord(
        incident_id=incident_id,
        status=IncidentState.APPROVAL_PENDING.value,
        data=incident.model_dump(mode="json"),
        analysis=analysis,
    )
    db.add(record)
    _record_decision(
        db, incident_id=incident_id, fingerprint=incident.incident_id,
        proposal=proposal, idempotency_key=_IDEM_PREFIX + key,
        reasons=proposal.reasons,
    )
    db.commit()

    payload = build_card(
        proposal, incident_id=incident_id, signature=signature,
        alertname=(incident.labels or {}).get("alertname", "?"),
    )
    msg = await DiscordService().send_remediation_proposal(payload)
    if msg is None:
        # Карточки нет — нечего одобрять: убираем и запись предложения, иначе
        # фолбэк-дедуп по БД заглушил бы следующую попытку на всё окно.
        from app.remediation.models import RemediationDecision
        db.query(RemediationDecision).filter(
            RemediationDecision.incident_id == incident_id,
        ).delete(synchronize_session=False)
        db.delete(record)
        db.commit()
        release_proposal(key)
        return OUTCOME_POST_FAILED
    analysis["bridge_card"] = msg
    record.analysis = analysis
    flag_modified(record, "analysis")
    db.commit()
    audit_service.log_event("REMEDIATION_BRIDGE_PROPOSED", {
        "incident_id": incident_id,
        "alert_fingerprint": incident.incident_id,
        "playbook": proposal.playbook,
        "namespace": proposal.namespace,
        "resource": proposal.resource_name,
        "intent_signature": signature,
    })
    return OUTCOME_PROPOSED


async def run_bridge(db: Any, ctx: "EnrichedContext") -> List[Dict[str, Any]]:
    """Мост для одного обогащённого алерта. Возвращает исходы по playbook-ам.

    block — строка в `ctx.extras["remediation_lines"]` (её рисует обычная
    карточка, если алерт вообще идёт в канал) и решение в БД. approve —
    отдельная карточка с кнопками (см. _propose). Наружу не бросает.
    """
    incident = ctx.incident
    if not wants_bridge(incident.labels):
        return []
    outcomes: List[Dict[str, Any]] = []
    try:
        facts = await collect_facts(ctx)
        proposals = await asyncio.to_thread(plan_proposals, ctx, facts, db)
    except Exception as e:
        log.warning("remediation_bridge.plan_failed", error=type(e).__name__)
        return []
    for proposal in proposals:
        try:
            if proposal.blocked:
                lines = ctx.extras.setdefault("remediation_lines", [])
                if proposal.blocked_line() not in lines:
                    lines.append(proposal.blocked_line())
                _record_decision(
                    db, incident_id=incident.incident_id, fingerprint=incident.incident_id,
                    proposal=proposal,
                    idempotency_key=_BLOCK_IDEM_PREFIX + proposal.playbook,
                    reasons=proposal.reasons,
                )
                db.commit()
                outcome = OUTCOME_BLOCKED
            else:
                outcome = await _propose(db, incident, proposal)
        except Exception as e:
            try:
                db.rollback()
            except Exception:
                pass
            log.warning("remediation_bridge.propose_failed",
                        playbook=proposal.playbook, error=type(e).__name__)
            outcome = f"error:{type(e).__name__}"
        log.info("remediation_bridge.outcome", playbook=proposal.playbook,
                 namespace=proposal.namespace, resource=proposal.resource_name,
                 mode=proposal.mode, outcome=outcome)
        outcomes.append({"playbook": proposal.playbook, "mode": proposal.mode,
                         "outcome": outcome})
    return outcomes


async def run_bridge_for_incident(incident: "Incident") -> List[Dict[str, Any]]:
    """Мост для алерта из `/store`: своя сессия, своё обогащение.

    /store обогащение не делает (сквады в канал не ходят), поэтому для
    кандидатов моста — тот же `enrich_alert`, что у enrich-and-forward.
    Запускается фоном после ответа AlertManager-у.
    """
    from app.database import SessionLocal
    from app.services.alert_enrichment import enrich_alert_async

    if not wants_bridge(incident.labels):
        return []
    db = SessionLocal()
    try:
        ctx = await enrich_alert_async(db, incident)
        return await run_bridge(db, ctx)
    except Exception as e:
        log.warning("remediation_bridge.store_failed", error=type(e).__name__)
        return []
    finally:
        db.close()


# Strong-ref на фоновые задачи: asyncio держит на задачу только weakref, и
# незапомненная задача может быть собрана GC до завершения.
_BACKGROUND: Set["asyncio.Task[Any]"] = set()


def schedule_bridge_for_incident(incident: "Incident") -> Optional["asyncio.Task[Any]"]:
    """Запустить run_bridge_for_incident фоном — ответ AlertManager-у не ждёт."""
    try:
        task = asyncio.get_running_loop().create_task(run_bridge_for_incident(incident))
    except RuntimeError:
        return None
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    return task


# ── итог verify → в ту же карточку ────────────────────────────────────────

_VERIFY_ICON = {"verified": "✅", "failed": "❌", "unknown": "❔"}


def annotate_verify_outcome(
    analysis: Any, outcome: str, reasons: Any = None,
    *, client_factory: Any = None,
) -> bool:
    """Дописать итог verify полем в карточку моста. Best-effort, не бросает.

    GET → PATCH, а не PATCH сохранённым embed-ом: после клика карточку уже
    отредактировал обработчик кнопки (кто одобрил, кнопки сняты), и старая
    копия эту пометку стёрла бы.
    """
    if not is_bridge_analysis(analysis):
        return False
    card = analysis.get("bridge_card") or {}
    message_id, channel_id = card.get("id"), card.get("channel_id")
    token = getattr(settings, "DISCORD_BOT_TOKEN", None)
    if not (message_id and channel_id and token):
        return False
    import httpx
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}"
    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    text = f"{_VERIFY_ICON.get(outcome, '•')} {outcome}"
    if reasons:
        text += " — " + "; ".join(str(r) for r in list(reasons)[:3])
    try:
        with (client_factory or httpx.Client)(timeout=10.0) as client:
            r = client.get(url, headers=headers)
            if r.status_code >= 400:
                return False
            embeds = list((r.json() or {}).get("embeds") or [])
            if not embeds:
                return False
            fields = [f for f in embeds[0].get("fields") or [] if f.get("name") != "Verify"]
            fields.append({"name": "Verify", "value": text[:1024], "inline": False})
            embeds[0]["fields"] = fields
            p = client.patch(url, headers=headers, json={"embeds": embeds})
            return p.status_code < 400
    except Exception as e:
        log.warning("remediation_bridge.annotate_failed", error=type(e).__name__)
        return False
