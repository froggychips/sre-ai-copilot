"""Детерминированный мост правила → playbook → карточка (app/remediation/bridge.py).

Проверяем:
  * факты состояния пода (K8sFacts) дают preconditions restart_crashloop_deployment;
    exit по сигналу / нет снимка — playbook не матчится (fail-closed);
  * squad → approve → intent из plan.steps → карточка с approve:/decline:
    в канал, подпись = подпись intent-а в БД, запись в kg_remediation_decisions;
  * prod → block → строка в обычной карточке, без кнопок и без карточки;
  * дедуп (playbook, uid, incarnation) на 6 ч — Redis и фолбэк по БД;
  * флаг выключен → мост не вызывается, /store как в 1.0.29;
  * клик по карточке моста: чужой — отказ + audit, intent не исполняется;
    из allowlist — executor получает задачу;
  * apply-путь сверяет intent моста со снимком без binding-флага;
  * итог verify дописывается в ту же карточку.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.diagnostics.facts import Fact, FactStore
from app.models.incident import Incident
from app.remediation import bridge
from app.remediation.matcher import match_playbooks
from app.remediation.verification import TargetSnapshot
from app.services.alert_enrichment import EnrichedContext

_PLAYBOOK = "restart_crashloop_deployment"


# ── фикстуры ──────────────────────────────────────────────────────────────

class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def delete(self, key):
        self.store.pop(key, None)


@pytest.fixture
def fake_redis(monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr(bridge, "_redis", lambda: r)
    return r


@pytest.fixture
def db():
    from app.database import Base
    from app.knowledge_graph import schema  # noqa: F401
    from app.remediation import models  # noqa: F401

    # run_bridge уводит plan_proposals в to_thread с той же сессией (как
    # enrich_alert_async) — SQLite должен это пережить.
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def bridge_on(monkeypatch):
    monkeypatch.setattr(settings, "REMEDIATION_BRIDGE_ENABLED", True)
    monkeypatch.setattr(settings, "EXECUTOR_ENABLED", True)
    monkeypatch.setattr(settings, "EXECUTOR_APPROVAL_ENABLED", True)
    monkeypatch.setattr(settings, "DISCORD_DRY_RUN", False)


def _incident(namespace: str = "squad-7-k5", fp: str = "fp-crash-1") -> Incident:
    return Incident(
        incident_id=fp,
        severity="warning",
        status="firing",
        summary="crashloop",
        description="Pod is in waiting state (reason: CrashLoopBackOff).",
        namespace=namespace,
        labels={
            "alertname": "KubePodCrashLooping",
            "namespace": namespace,
            "pod": "town-service-6c6cd4df-8hx9c",
            "container": "town-service",
        },
        annotations={},
        starts_at="2026-10-30T10:00:00Z",
    )


def _ctx(namespace: str = "squad-7-k5", fp: str = "fp-crash-1") -> EnrichedContext:
    return EnrichedContext(
        incident=_incident(namespace, fp),
        service="town-service",
        pod_name="town-service-6c6cd4df-8hx9c",
        rule_facts=[
            # Как отдаёт enrich_alert: crashloop из kg_pod_events, деплоев нет.
            Fact(kind="crashloop", observed=True, confidence=0.85,
                 evidence={"reason": "BackOff"}, subject="town-service-6c6cd4df-8hx9c"),
            Fact(kind="recent_deploy", observed=False, confidence=0.9),
        ],
    )


def _snap(exit_code: int = 1, reason: str = "Error", error=None):
    return SimpleNamespace(
        text="Unhealthy pods: none",
        container_terminated={
            "town-service-6c6cd4df-8hx9c": {
                "reason": reason, "exit_code": exit_code,
                "message": "", "container": "town-service",
            },
        },
        pod_events=[],
        error=error,
    )


def _facts(ctx: EnrichedContext, snap=None) -> FactStore:
    store = FactStore(list(ctx.rule_facts))
    store.extend(bridge.pod_state_facts(ctx, _snap() if snap is None else snap))
    return store


# ── факты → совпадение ────────────────────────────────────────────────────

def test_pod_state_facts_satisfy_restart_playbook():
    ctx = _ctx()
    facts = _facts(ctx)
    crash = [f for f in facts.by_kind("process_crash") if f.verdict == "found"]
    assert crash and crash[0].evidence["exit_code"] == 1
    assert {f.verdict for f in facts.by_kind("oom_killed")} == {"absent"}
    matched = match_playbooks(alertname="KubePodCrashLooping", facts=facts)
    assert [pb.name for pb in matched] == [_PLAYBOOK]


def test_rule_facts_alone_do_not_match():
    """Без состояния пода (как было на прод-пути) preconditions не выполнены."""
    ctx = _ctx()
    assert match_playbooks(alertname="KubePodCrashLooping",
                           facts=FactStore(list(ctx.rule_facts))) == []


@pytest.mark.parametrize("exit_code", [139, 134])
def test_signal_crash_excluded(exit_code):
    ctx = _ctx()
    facts = _facts(ctx, _snap(exit_code=exit_code))
    assert match_playbooks(alertname="KubePodCrashLooping", facts=facts) == []


def test_k8s_snapshot_failure_is_unknown_not_absent():
    ctx = _ctx()
    facts = _facts(ctx, SimpleNamespace(text="", container_terminated={},
                                        pod_events=[], error="ApiException"))
    assert "unknown" in {f.verdict for f in facts.by_kind("oom_killed")}
    assert match_playbooks(alertname="KubePodCrashLooping", facts=facts) == []


def test_k8s_facts_counts_running_crashloop_pod_as_unhealthy():
    from app.context.k8s_facts import _has_bad_waiting
    waiting = SimpleNamespace(state=SimpleNamespace(
        waiting=SimpleNamespace(reason="CrashLoopBackOff")))
    starting = SimpleNamespace(state=SimpleNamespace(
        waiting=SimpleNamespace(reason="ContainerCreating")))
    running = SimpleNamespace(state=SimpleNamespace(waiting=None))
    assert _has_bad_waiting([running, waiting]) is True
    assert _has_bad_waiting([running, starting]) is False
    assert _has_bad_waiting(None) is False


# ── план и политика ───────────────────────────────────────────────────────

def test_plan_squad_is_approve_with_server_built_intent(db):
    ctx = _ctx()
    [p] = bridge.plan_proposals(ctx, _facts(ctx), db)
    assert p.playbook == _PLAYBOOK
    assert p.mode == "approve"
    assert p.tier == "squad"
    assert p.intent.action.value == "restart_deployment"
    assert p.intent.resource_name == "town-service"
    assert p.intent.namespace == "squad-7-k5"
    assert p.intent.playbook == _PLAYBOOK
    assert p.intent.playbook_match == p.snapshot["entries"][0]["binding"]
    assert "rollout" in p.command_preview and "restart" in p.command_preview


def test_plan_prod_is_blocked(db):
    ctx = _ctx(namespace="prod-kingdom5")
    [p] = bridge.plan_proposals(ctx, _facts(ctx), db)
    assert p.mode == "block"
    assert p.blocked_line() == f"playbook `{_PLAYBOOK}`: заблокирован policy (prod)"


def test_gate_checks_bridge_snapshot_without_binding_flag(db, monkeypatch):
    from app.remediation.executor_gate import PolicyMode, evaluate_intent_gate
    monkeypatch.setattr(settings, "REMEDIATION_PLAYBOOK_BINDING_ENABLED", False)
    ctx = _ctx()
    [p] = bridge.plan_proposals(ctx, _facts(ctx), db)
    ok = evaluate_intent_gate(p.intent, match_snapshot=p.snapshot, require_binding=True)
    assert ok.mode == PolicyMode.APPROVE
    tampered = json.loads(json.dumps(p.snapshot))
    tampered["entries"][0]["plan"].append({"action": "scale_deployment", "params": {}})
    bad = evaluate_intent_gate(p.intent, match_snapshot=tampered, require_binding=True)
    assert bad.mode == PolicyMode.BLOCK
    assert bad.reasons[0]["reason"] == "snapshot_tampered"
    # Без require_binding (LLM-путь, флаг выключен) — прежнее поведение.
    legacy = evaluate_intent_gate(p.intent, match_snapshot=tampered)
    assert legacy.mode == PolicyMode.APPROVE


def test_apply_path_requires_binding_for_bridge_records():
    assert bridge.is_bridge_analysis({"source": bridge.BRIDGE_SOURCE}) is True
    assert bridge.is_bridge_analysis({"execution_intent": {}}) is False
    assert bridge.is_bridge_analysis(None) is False


# ── run_bridge: карточка / блок / дедуп ───────────────────────────────────

def _live(uid: str = "uid-1") -> TargetSnapshot:
    return TargetSnapshot(kind="deployment", namespace="squad-7-k5",
                          name="town-service", uid=uid, replicas_desired=1)


async def _run(db, ctx, *, send=None, live=None, dry_status="dry_run_ok"):
    send = send or AsyncMock(return_value={"id": "m-1", "channel_id": "c-1"})
    facts = _facts(ctx)
    with patch.object(bridge, "collect_facts", AsyncMock(return_value=facts)), \
         patch("app.remediation.verification.snapshot_target",
               return_value=live or _live()), \
         patch.object(bridge, "_dry_run",
                      return_value={"status": dry_status,
                                    "dry_run_at": datetime.now().isoformat()}), \
         patch("app.services.discord_service.DiscordService.send_remediation_proposal",
               send):
        outcomes = await bridge.run_bridge(db, ctx)
    return outcomes, send


@pytest.mark.asyncio
async def test_squad_match_posts_card_with_buttons(db, bridge_on, fake_redis):
    from app.database import IncidentRecord
    from app.remediation.models import RemediationDecision
    from app.services.intent_signature import compute_signature
    from app.core.execution_dsl import ExecutionIntent

    outcomes, send = await _run(db, _ctx())
    assert outcomes == [{"playbook": _PLAYBOOK, "mode": "approve",
                         "outcome": bridge.OUTCOME_PROPOSED}]
    send.assert_awaited_once()
    payload = send.await_args.args[0]
    buttons = payload["components"][0]["components"]
    approve_id, decline_id = buttons[0]["custom_id"], buttons[1]["custom_id"]
    _, incident_id, sig = approve_id.split(":", 2)
    assert decline_id == f"decline:{incident_id}:{sig}"
    assert payload["allowed_mentions"] == {"parse": []}

    record = db.query(IncidentRecord).filter_by(incident_id=incident_id).one()
    assert record.status == "APPROVAL_PENDING"
    assert record.analysis["source"] == bridge.BRIDGE_SOURCE
    assert record.analysis["executor_result"]["status"] == "dry_run_ok"
    assert record.analysis["bridge_card"] == {"id": "m-1", "channel_id": "c-1"}
    stored = ExecutionIntent.model_validate(record.analysis["execution_intent"])
    assert compute_signature(stored) == sig
    assert record.data["namespace"] == "squad-7-k5"

    decision = db.query(RemediationDecision).filter_by(incident_id=incident_id).one()
    assert decision.decision == "approve"
    assert decision.selected_playbook == _PLAYBOOK
    assert decision.target_ref["uid"] == "uid-1"


@pytest.mark.asyncio
async def test_prod_block_adds_card_line_without_buttons(db, bridge_on, fake_redis):
    ctx = _ctx(namespace="prod-kingdom5")
    outcomes, send = await _run(db, ctx)
    assert outcomes[0]["outcome"] == bridge.OUTCOME_BLOCKED
    send.assert_not_awaited()
    assert ctx.extras["remediation_lines"] == [
        f"playbook `{_PLAYBOOK}`: заблокирован policy (prod)"
    ]


@pytest.mark.asyncio
async def test_blocked_line_rendered_in_enriched_card(bridge_on):
    from app.services.discord_service import DiscordService
    ctx = _ctx(namespace="prod-kingdom5")
    ctx.extras["remediation_lines"] = [f"playbook `{_PLAYBOOK}`: заблокирован policy (prod)"]
    sent: dict = {}

    async def fake_post(self, url, json=None, **_):
        sent["payload"] = json
        resp = MagicMock()
        resp.status_code = 204
        return resp

    with patch("app.services.discord_service.settings.DISCORD_WEBHOOK_URL",
               "https://example.com/wh"), \
         patch("httpx.AsyncClient.post", new=fake_post):
        await DiscordService().send_enriched_alert([ctx], env="prod")
    payload = sent["payload"]
    assert "components" not in payload
    fields = {f["name"]: f["value"] for f in payload["embeds"][0]["fields"]}
    assert "заблокирован policy (prod)" in fields["Remediation"]


@pytest.mark.asyncio
async def test_enriched_card_has_no_remediation_field_without_bridge():
    from app.services.discord_service import DiscordService
    sent: dict = {}

    async def fake_post(self, url, json=None, **_):
        sent["payload"] = json
        resp = MagicMock()
        resp.status_code = 204
        return resp

    with patch("app.services.discord_service.settings.DISCORD_DRY_RUN", False), \
         patch("app.services.discord_service.settings.DISCORD_WEBHOOK_URL",
               "https://example.com/wh"), \
         patch("httpx.AsyncClient.post", new=fake_post):
        await DiscordService().send_enriched_alert([_ctx(namespace="prod-kingdom5")], env="prod")
    names = [f["name"] for f in sent["payload"]["embeds"][0]["fields"]]
    assert "Remediation" not in names


@pytest.mark.asyncio
async def test_repeat_within_window_is_silent(db, bridge_on, fake_redis):
    first, send1 = await _run(db, _ctx(fp="fp-a"))
    # Новый батч того же алерта (AM пере-шлёт через repeat_interval), даже
    # с другим fingerprint-ом — та же цель (uid) и тот же playbook.
    second, send2 = await _run(db, _ctx(fp="fp-b"))
    assert first[0]["outcome"] == bridge.OUTCOME_PROPOSED
    assert second[0]["outcome"] == bridge.OUTCOME_DEDUPLICATED
    send2.assert_not_awaited()
    # Пересозданная цель (другой uid) — новое предложение.
    third, send3 = await _run(db, _ctx(fp="fp-c"), live=_live(uid="uid-2"))
    assert third[0]["outcome"] == bridge.OUTCOME_PROPOSED
    send3.assert_awaited_once()


@pytest.mark.asyncio
async def test_dedup_falls_back_to_decisions_without_redis(db, bridge_on, monkeypatch):
    from app.remediation.models import RemediationDecision
    monkeypatch.setattr(bridge, "_redis", lambda: None)
    first, _ = await _run(db, _ctx(fp="fp-a"))
    second, send2 = await _run(db, _ctx(fp="fp-b"))
    assert first[0]["outcome"] == bridge.OUTCOME_PROPOSED
    assert second[0]["outcome"] == bridge.OUTCOME_DEDUPLICATED
    send2.assert_not_awaited()
    # Окно 6 ч прошло — снова предлагаем.
    for row in db.query(RemediationDecision).all():
        row.created_at = datetime.utcnow() - timedelta(hours=7)
    db.commit()
    third, send3 = await _run(db, _ctx(fp="fp-c"))
    assert third[0]["outcome"] == bridge.OUTCOME_PROPOSED
    send3.assert_awaited_once()


@pytest.mark.parametrize("redis_up", [True, False])
@pytest.mark.asyncio
async def test_failed_post_releases_claim(db, bridge_on, fake_redis, monkeypatch, redis_up):
    from app.database import IncidentRecord
    if not redis_up:
        monkeypatch.setattr(bridge, "_redis", lambda: None)
    first, _ = await _run(db, _ctx(fp="fp-a"), send=AsyncMock(return_value=None))
    assert db.query(IncidentRecord).count() == 0
    assert first[0]["outcome"] == bridge.OUTCOME_POST_FAILED
    second, send2 = await _run(db, _ctx(fp="fp-b"))
    assert second[0]["outcome"] == bridge.OUTCOME_PROPOSED
    send2.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_dry_run_posts_nothing(db, bridge_on, fake_redis):
    outcomes, send = await _run(db, _ctx(), dry_status="dry_run_failed")
    assert outcomes[0]["outcome"] == bridge.OUTCOME_DRY_RUN_FAILED
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_card_without_executor_flags(db, bridge_on, fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "EXECUTOR_APPROVAL_ENABLED", False)
    outcomes, send = await _run(db, _ctx())
    assert outcomes[0]["outcome"] == bridge.OUTCOME_EXECUTOR_DISABLED
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_flag_off_bridge_is_inert(db, monkeypatch):
    monkeypatch.setattr(settings, "REMEDIATION_BRIDGE_ENABLED", False)
    collect = AsyncMock()
    with patch.object(bridge, "collect_facts", collect):
        assert await bridge.run_bridge(db, _ctx()) == []
        assert await bridge.run_bridge_for_incident(_incident()) == []
    collect.assert_not_awaited()
    assert bridge.wants_bridge({"alertname": "KubePodCrashLooping"}) is False


@pytest.mark.asyncio
async def test_store_endpoint_schedules_bridge_only_with_flag(monkeypatch):
    from app.api import webhooks
    from app.models.incident import AlertManagerWebhook

    payload = AlertManagerWebhook.model_validate({
        "version": "4", "receiver": "sre-ai-info", "status": "firing", "groupKey": "g",
        "alerts": [{
            "status": "firing",
            "labels": {"alertname": "KubePodCrashLooping", "namespace": "squad-7-k5",
                       "pod": "town-service-6c6cd4df-8hx9c", "severity": "warning"},
            "annotations": {}, "startsAt": "2026-10-30T10:00:00Z",
            "fingerprint": "abc",
        }],
    })
    scheduled: list = []
    with patch("app.knowledge_graph.auto_populator.populate_from_incident",
               return_value={}), \
         patch.object(bridge, "schedule_bridge_for_incident",
                      side_effect=lambda inc: scheduled.append(inc)):
        monkeypatch.setattr(settings, "REMEDIATION_BRIDGE_ENABLED", False)
        await webhooks.alertmanager_webhook_store_only(payload, db=MagicMock())
        assert scheduled == []
        monkeypatch.setattr(settings, "REMEDIATION_BRIDGE_ENABLED", True)
        await webhooks.alertmanager_webhook_store_only(payload, db=MagicMock())
    assert len(scheduled) == 1
    assert scheduled[0].namespace == "squad-7-k5"


# ── клик по карточке моста ────────────────────────────────────────────────

def _click(custom_id: str, user_id: str) -> dict:
    return {
        "type": 3,
        "data": {"custom_id": custom_id},
        "member": {"user": {"id": user_id, "username": f"user-{user_id}"}},
        "token": "tok",
        "message": {"id": "m-1", "channel_id": "c-1",
                    "embeds": [{"title": "x", "footer": {"text": "incident/x"}}]},
        "channel_id": "c-1",
    }


async def _press(custom_id: str, user_id: str, approvals_db):
    from app.api import discord_interactions as di
    audit: list = []
    dispatched: list = []
    request = MagicMock()
    request.body = AsyncMock(return_value=json.dumps(_click(custom_id, user_id)).encode())
    with patch.object(di, "_verify_signature", return_value=True), \
         patch.object(di, "SessionLocal", approvals_db), \
         patch.object(di.settings, "DISCORD_PUBLIC_KEY", "deadbeef"), \
         patch.object(di.settings, "EXECUTOR_ENABLED", True), \
         patch.object(di.settings, "EXECUTOR_APPROVAL_ENABLED", True), \
         patch.object(di.settings, "EXECUTOR_DISPATCH", "queue"), \
         patch.object(di.settings, "DISCORD_APPROVERS_USER_IDS", "owner-1"), \
         patch.object(di.audit_service, "log_event",
                      side_effect=lambda et, d: audit.append((et, d))), \
         patch("app.workers.executor_tasks.dispatch_apply",
               side_effect=lambda *a, **k: dispatched.append(a)), \
         patch("app.services.executor_apply.apply_intent") as apply_intent, \
         patch.object(di, "_spawn_background_task",
                      side_effect=lambda coro, **_: coro.close()):
        resp = await di.discord_interactions(
            request, x_signature_ed25519="00" * 64, x_signature_timestamp="0",
        )
    return resp, audit, dispatched, apply_intent


@pytest.fixture
def approvals_db():
    from app.database import Base
    from app.knowledge_graph import schema  # noqa: F401
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, autocommit=False, autoflush=False)
    engine.dispose()


def _card_ids(db) -> tuple:
    ctx = _ctx()
    [p] = bridge.plan_proposals(ctx, _facts(ctx), db)
    from app.services.intent_signature import compute_signature
    payload = bridge.build_card(p, incident_id="rb-abc-1", signature=compute_signature(p.intent),
                                alertname="KubePodCrashLooping")
    buttons = payload["components"][0]["components"]
    return buttons[0]["custom_id"], buttons[1]["custom_id"]


@pytest.mark.asyncio
async def test_foreign_click_is_denied_and_audited(db, approvals_db):
    approve_id, _ = _card_ids(db)
    resp, audit, dispatched, apply_intent = await _press(approve_id, "stranger-9", approvals_db)
    assert "not authorized" in resp["data"]["content"]
    assert resp["data"]["flags"] == 64
    assert [e for e, _ in audit] == ["DISCORD_APPROVAL_DENIED_UNAUTHORIZED"]
    assert audit[0][1]["incident_id"] == "rb-abc-1"
    assert dispatched == []
    apply_intent.assert_not_called()
    from app.knowledge_graph.schema import ActionApproval
    with approvals_db() as s:
        assert s.query(ActionApproval).count() == 0


@pytest.mark.asyncio
async def test_owner_click_dispatches_executor(db, approvals_db):
    approve_id, _ = _card_ids(db)
    _, incident_id, sig = approve_id.split(":", 2)
    resp, audit, dispatched, _ = await _press(approve_id, "owner-1", approvals_db)
    assert "Executor launched" in resp["data"]["content"]
    assert dispatched == [(incident_id, "user-owner-1", sig)]
    assert "INCIDENT_ACTION_APPROVED" in [e for e, _ in audit]


# ── verify → карточка ─────────────────────────────────────────────────────

def test_verify_outcome_appended_to_card(monkeypatch):
    monkeypatch.setattr(settings, "DISCORD_BOT_TOKEN", "bot-token")
    calls: dict = {}

    class _Client:
        def __init__(self, **_):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None):
            calls["get"] = url
            return SimpleNamespace(status_code=200, json=lambda: {"embeds": [{
                "title": "t", "fields": [{"name": "Команда", "value": "x"}],
                "footer": {"text": "incident/x · ✅ Approved by @owner"},
            }]})

        def patch(self, url, headers=None, json=None):
            calls["patch"] = json
            return SimpleNamespace(status_code=200)

    analysis = {"source": bridge.BRIDGE_SOURCE,
                "bridge_card": {"id": "m-1", "channel_id": "c-1"}}
    assert bridge.annotate_verify_outcome(analysis, "verified", client_factory=_Client)
    assert calls["get"].endswith("/channels/c-1/messages/m-1")
    embed = calls["patch"]["embeds"][0]
    assert embed["footer"]["text"].endswith("Approved by @owner")
    assert embed["fields"][-1] == {"name": "Verify", "value": "✅ verified", "inline": False}
    # Не карточка моста — ничего не трогаем.
    assert bridge.annotate_verify_outcome({"bridge_card": {"id": "m"}}, "failed",
                                          client_factory=_Client) is False


# ── resolved-карточки ─────────────────────────────────────────────────────

@pytest.mark.parametrize("duration,threshold,expected", [
    (3, 30, False),
    (29, 30, False),
    (30, 30, True),
    (95, 30, True),
    (None, 30, True),   # длительность неизвестна — постим
    (3, 0, True),       # 0 — фильтр выключен (поведение 1.0.29)
])
def test_resolved_notice_policy(monkeypatch, duration, threshold, expected):
    from app.services.discord.service import resolved_notice_wanted
    monkeypatch.setattr(settings, "DISCORD_RESOLVED_NOTICE_MIN_MINUTES", threshold)
    assert resolved_notice_wanted(duration) is expected


@pytest.mark.asyncio
async def test_short_resolved_episode_not_posted(monkeypatch):
    from app.services.discord_service import DiscordService
    monkeypatch.setattr(settings, "DISCORD_DRY_RUN", False)
    monkeypatch.setattr(settings, "DISCORD_WEBHOOK_URL", "https://example.com/wh")
    monkeypatch.setattr(settings, "DISCORD_RESOLVED_NOTICE_MIN_MINUTES", 30)
    post = AsyncMock(return_value=True)
    with patch.object(DiscordService, "_post_enriched_raw", post):
        short = await DiscordService().send_resolved_notice(
            alertname="KubePodCrashLooping", namespace="prod-kingdom5", duration_min=4)
        long_ = await DiscordService().send_resolved_notice(
            alertname="KubePodCrashLooping", namespace="prod-kingdom5", duration_min=45)
    assert short is False
    assert long_ is True
    post.assert_awaited_once()
    assert "длился 45м" in post.await_args.args[1]["embeds"][0]["description"]
