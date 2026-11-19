"""Обратная связь по remediation моста: карточка, verify, claim, срок жизни.

Проверяем:
  * B — verify ищет алерт по настоящему fingerprint (`analysis.alert_fingerprint`),
    а не по `rb-…`; неизвестный статус алерта у моста — не `verified`;
  * A — итог apply (успех / отказ / ошибка) дописывается в карточку моста
    ботом, без interaction token; повторный клик итог не перетирает;
  * C — claim дедупа короткий до конечного исхода: транзиентный сбой
    dry-run / исключение не глушат алерт на всё окно;
  * D — срок жизни предложения и погасший алерт: отказ на клике и в
    apply_intent, кнопки снимаются; сторож задачи apply;
  * E/F/G — отзыв одобрения при сбое постановки (см. также
    test_executor_isolation), sync-вызовы вне event loop, applied_by = user id.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import discord_interactions as di
from app.config import settings
from app.core.execution_dsl import ActionType, ExecutionIntent
from app.database import Base, IncidentRecord
from app.knowledge_graph.schema import ActionApproval, AlertEvent
from app.remediation import bridge
from app.remediation import verification as v
from app.services import executor_apply
from app.services.intent_signature import compute_signature
from app.workers import executor_tasks

NS = "squad-7-k5"
NAME = "town-service"
FP = "fp-crash-1"
INCIDENT_ID = "rb-abcdef123456-1700000000-a1b2c3"


# ── фикстуры ──────────────────────────────────────────────────────────────

class _FakeRedis:
    """SET NX EX / DELETE / EXISTS с запоминанием TTL."""

    def __init__(self) -> None:
        self.store: dict = {}
        self.ttl: dict = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        self.ttl[key] = ex
        return True

    def delete(self, key):
        self.store.pop(key, None)
        self.ttl.pop(key, None)

    def exists(self, key):
        return int(key in self.store)


@pytest.fixture
def fake_redis(monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr(bridge, "_redis", lambda: r)
    monkeypatch.setattr(executor_tasks, "_redis", lambda: r)
    return r


@pytest.fixture
def sessions(monkeypatch):
    """Одна in-memory БД на все нити; подменяет SessionLocal везде, где его
    открывают обработчик кнопок, apply_intent и отчёт в карточку."""
    from app.knowledge_graph import schema  # noqa: F401
    from app.remediation import attempts, models  # noqa: F401

    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(di, "SessionLocal", factory)
    monkeypatch.setattr(executor_apply, "SessionLocal", factory)
    monkeypatch.setattr("app.database.SessionLocal", factory)
    yield factory
    engine.dispose()


def _intent() -> ExecutionIntent:
    return ExecutionIntent(
        action=ActionType.RESTART_DEPLOYMENT, resource_type="deployment",
        resource_name=NAME, namespace=NS, risk="low",
    )


SIG = compute_signature(_intent())


def _seed_proposal(factory, *, age_seconds: int = 60, alert: str = "firing",
                   incident_id: str = INCIDENT_ID, applied: bool = False) -> None:
    """Строка инцидента моста, как её пишет `_propose`, + алерт в kg_alerts.

    alert: firing | resolved | missing.
    """
    born = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    analysis = {
        "source": bridge.BRIDGE_SOURCE,
        "alert_fingerprint": FP,
        "dedup_key": "dedupkey",
        "execution_intent": _intent().model_dump(mode="json"),
        "execution_intent_at": born.isoformat(),
        "executor_result": {"status": "dry_run_ok", "dry_run_at": born.isoformat()},
        "bridge_card": {"id": "m-1", "channel_id": "c-1"},
    }
    if applied:
        analysis["executor_applied"] = {
            "applied_at": datetime.now(timezone.utc).isoformat(),
            "target_before": {"uid": "uid-1", "generation": 5, "template_hash": "aaaa"},
        }
    with factory() as db:
        db.add(IncidentRecord(incident_id=incident_id, status="APPROVAL_PENDING",
                              data={"namespace": NS}, analysis=analysis))
        if alert != "missing":
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            db.add(AlertEvent(
                alertname="KubePodCrashLooping", severity="warning", fingerprint=FP,
                fired_at=now - timedelta(minutes=30),
                resolved_at=now if alert == "resolved" else None,
            ))
        db.commit()


class _Card:
    """Подмена httpx.Client: GET отдаёт карточку, PATCH её запоминает."""

    def __init__(self, fields=None):
        self.fields = list(fields or [{"name": "Команда", "value": "x"}])
        self.patches: list = []

    def __call__(self, **_):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None):
        return SimpleNamespace(status_code=200, json=lambda: {"embeds": [{
            "title": "t", "fields": list(self.fields),
            "footer": {"text": "incident/x · ✅ Approved by @operator"},
        }]})

    def patch(self, url, headers=None, json=None):
        self.patches.append(json)
        self.fields = list(json["embeds"][0]["fields"])
        return SimpleNamespace(status_code=200)

    def field(self, name):
        return next((f["value"] for f in self.fields if f["name"] == name), None)


@pytest.fixture
def bot(monkeypatch):
    monkeypatch.setattr(settings, "DISCORD_BOT_TOKEN", "bot-token")


# ── B: verify по настоящему fingerprint ───────────────────────────────────

def _snap(**kw) -> v.TargetSnapshot:
    base = dict(kind="deployment", namespace=NS, name=NAME, uid="uid-1", generation=6,
                observed_generation=6, template_hash="bbbb", replicas_desired=1,
                replicas_ready=1)
    base.update(kw)
    return v.TargetSnapshot(**base)


_BEFORE = {"uid": "uid-1", "generation": 5, "template_hash": "aaaa"}


def _assess(alert_resolved, attempt, **kw):
    return v.assess(intent=_intent(), before=_BEFORE, now_snap=_snap(),
                    alert_resolved=alert_resolved, new_crash_events=0,
                    attempt=attempt, max_attempts=2, **kw)


def test_assess_unknown_alert_blocks_verified_only_when_required():
    # Мост: статус алерта неизвестен — ждём, на последней попытке «не подтверждено».
    assert _assess(None, 1, require_alert_status=True)["outcome"] == "pending"
    last = _assess(None, 2, require_alert_status=True)
    assert last["outcome"] == "unknown"
    assert any("резолв не проверен" in r for r in last["reasons"])
    assert _assess(True, 1, require_alert_status=True)["outcome"] == "verified"
    # Остальные пути — как было: None не блокер.
    assert _assess(None, 1)["outcome"] == "verified"


def test_alert_fingerprint_for_bridge_and_plain_incident():
    assert v.alert_fingerprint_for("rb-x", {"source": bridge.BRIDGE_SOURCE,
                                           "alert_fingerprint": FP}) == FP
    assert v.alert_fingerprint_for("fp-plain", {"alert_fingerprint": FP}) == "fp-plain"
    assert v.alert_fingerprint_for("fp-plain", None) == "fp-plain"


def _runner(snap: v.TargetSnapshot):
    obj = {
        "metadata": {"namespace": NS, "name": NAME, "uid": snap.uid,
                     "generation": snap.generation},
        "spec": {"replicas": snap.replicas_desired, "template": {"x": "new"}},
        "status": {"observedGeneration": snap.observed_generation,
                   "readyReplicas": snap.replicas_ready},
    }
    return lambda argv, **_: SimpleNamespace(returncode=0, stdout=json.dumps(obj), stderr="")


@pytest.mark.parametrize("alert,attempt,expected,checked", [
    ("resolved", 1, "verified", True),
    ("firing", 1, "pending", False),
    ("firing", 2, "failed", False),
    ("missing", 1, "pending", None),
    ("missing", 2, "unknown", None),
])
def test_verify_bridge_incident_uses_real_fingerprint(
    sessions, monkeypatch, alert, attempt, expected, checked,
):
    """До правки `_alert_resolved` искал kg_alerts.fingerprint == 'rb-…',
    всегда получал None, и None не мешал исходу `verified`."""
    monkeypatch.setattr(settings, "REMEDIATION_VERIFY_DELAYS_SEC", "300,900")
    _seed_proposal(sessions, alert=alert, applied=True)
    with patch("app.services.audit_logger.audit_service.log_event"), \
         patch.object(bridge, "annotate_verify_outcome") as annotate:
        out = v.verify_remediation(INCIDENT_ID, attempt, db_factory=sessions,
                                   runner=_runner(_snap()))
    assert out["outcome"] == expected
    assert out["checks"]["alert_resolved"] is checked
    if expected == "pending":
        annotate.assert_not_called()
    else:
        assert annotate.call_args.args[1] == expected


def test_verify_card_says_not_confirmed_for_unknown(bot):
    card = _Card()
    analysis = {"source": bridge.BRIDGE_SOURCE, "bridge_card": {"id": "m-1", "channel_id": "c-1"}}
    assert bridge.annotate_verify_outcome(
        analysis, "unknown", ["алерта инцидента нет в kg_alerts — резолв не проверен"],
        client_factory=card,
    )
    assert card.field("Verify").startswith("❔ не подтверждено — алерта инцидента нет")


# ── A: итог apply → в карточку ────────────────────────────────────────────

def test_apply_success_is_written_to_card(sessions, bot, fake_redis):
    _seed_proposal(sessions)
    card = _Card()
    outcome = {"ok": True, "result": {"success": True, "stdout": "restarted",
                                      "command": "kubectl rollout restart deployment/x"}}
    assert bridge.report_apply_outcome(INCIDENT_ID, outcome, client_factory=card)
    text = card.field("Исполнение")
    assert text.startswith("✅ kubectl выполнен") and "rollout restart" in text
    assert "Проверка результата" in text
    # Прежние поля и пометка об одобрении на месте.
    assert card.field("Команда") == "x"
    assert card.patches[0]["embeds"][0]["footer"]["text"].endswith("Approved by @operator")
    assert "components" not in card.patches[0]


@pytest.mark.parametrize("reason,needle,released", [
    ("dry_run_recheck_failed:replicas mismatch", "Повторный dry-run", True),
    ("target_reincarnated:uid графа ≠ живой", "Цель пересоздана", False),
    ("approval_stale", "Одобрение протухло", True),
    ("intent_stale:90000s", "План устарел", True),
    ("proposal_expired:4000s", "просрочено", True),
    ("alert_resolved", "погас", True),
    ("policy_block:namespace_tier", "Policy-gate", False),
    ("execute_error", "Сбой исполнителя", False),
])
def test_apply_refusal_is_written_to_card(sessions, bot, fake_redis, reason, needle, released):
    _seed_proposal(sessions)
    fake_redis.set(bridge._DEDUP_PREFIX + "dedupkey", "1", ex=21600)
    card = _Card()
    assert bridge.report_apply_outcome(
        INCIDENT_ID, {"ok": False, "reason": reason, "error": "RuntimeError"},
        client_factory=card,
    )
    assert needle in card.field("Исполнение")
    # Предложение умерло без записи в кластер — claim дедупа снят, следующий
    # батч алерта даст свежую карточку. Детерминированный отказ claim держит.
    assert (bridge._DEDUP_PREFIX + "dedupkey" not in fake_redis.store) is released


def test_failed_kubectl_is_written_to_card(sessions, bot, fake_redis):
    _seed_proposal(sessions)
    card = _Card()
    outcome = {"ok": True, "result": {"success": False, "command": "kubectl scale",
                                      "stderr": "Error from server (Conflict)"}}
    bridge.report_apply_outcome(INCIDENT_ID, outcome, client_factory=card)
    text = card.field("Исполнение")
    assert text.startswith("❌ kubectl упал") and "Conflict" in text
    assert "Проверка результата" not in text


@pytest.mark.parametrize("reason", ["already_applied", "apply_in_flight"])
def test_duplicate_click_does_not_overwrite_card_outcome(sessions, bot, fake_redis, reason):
    """Двойной клик / повторная доставка: итог первого исполнения остаётся."""
    _seed_proposal(sessions)
    card = _Card(fields=[{"name": "Исполнение", "value": "✅ kubectl выполнен"}])
    assert bridge.report_apply_outcome(
        INCIDENT_ID, {"ok": False, "reason": reason}, client_factory=card,
    ) is False
    assert card.patches == []


def test_report_ignores_non_bridge_and_missing_incidents(sessions, bot):
    with sessions() as db:
        db.add(IncidentRecord(incident_id="fp-llm", status="COMPLETED", data={},
                              analysis={"bridge_card": {"id": "m", "channel_id": "c"}}))
        db.commit()
    card = _Card()
    assert bridge.report_apply_outcome("fp-llm", {"ok": True, "result": {}},
                                       client_factory=card) is False
    assert bridge.report_apply_outcome("nope", {"ok": True, "result": {}},
                                       client_factory=card) is False
    assert card.patches == []


def test_executor_task_reports_to_card_without_interaction_token(fake_redis):
    """Клик «Применить» interaction token не передаёт — итог идёт в карточку."""
    outcome = {"ok": False, "reason": "dry_run_recheck_failed:x"}
    with patch("app.services.executor_apply.apply_intent", return_value=outcome), \
         patch("app.api.discord_interactions._send_followup", new_callable=AsyncMock) as followup, \
         patch.object(bridge, "report_apply_outcome") as report:
        res = executor_tasks.executor_apply_task.run(INCIDENT_ID, "111", SIG)
    followup.assert_not_awaited()
    report.assert_called_once_with(INCIDENT_ID, outcome, only_if_absent=False)
    assert res["reason"] == "dry_run_recheck_failed:x"
    # Отметка «executor взял задачу» — для сторожа.
    assert executor_tasks._was_picked(INCIDENT_ID, SIG) is True


def test_executor_task_survives_card_report_failure(fake_redis):
    with patch("app.services.executor_apply.apply_intent",
               return_value={"ok": True, "result": {"success": True}}), \
         patch.object(bridge, "report_apply_outcome", side_effect=RuntimeError("discord")):
        res = executor_tasks.executor_apply_task.run(INCIDENT_ID, "111", SIG)
    assert res == {"ok": True, "success": True}


def test_inline_apply_reports_to_card():
    outcome = {"ok": True, "result": {"success": True}}
    with patch("app.services.executor_apply.apply_intent", return_value=outcome) as apply, \
         patch.object(bridge, "report_apply_outcome") as report:
        assert executor_tasks.apply_and_report(INCIDENT_ID, "111", SIG) is outcome
    apply.assert_called_once_with(INCIDENT_ID, "111", SIG)
    report.assert_called_once_with(INCIDENT_ID, outcome, only_if_absent=False)


# ── D: сторож задачи apply ────────────────────────────────────────────────

def test_watchdog_reports_task_that_was_never_picked(sessions, bot, fake_redis):
    _seed_proposal(sessions)
    fake_redis.set(bridge._DEDUP_PREFIX + "dedupkey", "1", ex=21600)
    card = _Card()
    with patch("app.services.audit_logger.audit_service.log_event") as audit, \
         patch("httpx.Client", card):
        res = executor_tasks.apply_watchdog_task.run(INCIDENT_ID, SIG)
    assert res == {"picked": False}
    assert audit.call_args.args[0] == "EXECUTOR_APPLY_NOT_PICKED_UP"
    assert "Executor не взял задачу" in card.field("Исполнение")
    assert bridge._DEDUP_PREFIX + "dedupkey" not in fake_redis.store


def test_watchdog_is_silent_when_task_was_picked_or_redis_is_down(sessions, bot, fake_redis,
                                                                 monkeypatch):
    _seed_proposal(sessions)
    executor_tasks._mark_picked(INCIDENT_ID, SIG)
    with patch.object(bridge, "report_apply_outcome") as report:
        assert executor_tasks.apply_watchdog_task.run(INCIDENT_ID, SIG) == {"picked": True}
        monkeypatch.setattr(executor_tasks, "_redis", lambda: None)
        assert executor_tasks.apply_watchdog_task.run(INCIDENT_ID, SIG) == {"picked": None}
    report.assert_not_called()


def test_watchdog_does_not_overwrite_real_outcome(sessions, bot, fake_redis):
    """Отметка потерялась (Redis моргнул у executor-а), а задача отработала:
    настоящий итог в карточке остаётся, claim дедупа не снимается."""
    _seed_proposal(sessions)
    fake_redis.set(bridge._DEDUP_PREFIX + "dedupkey", "1", ex=21600)
    card = _Card(fields=[{"name": "Исполнение", "value": "✅ kubectl выполнен"}])
    with patch("app.services.audit_logger.audit_service.log_event"), \
         patch("httpx.Client", card):
        executor_tasks.apply_watchdog_task.run(INCIDENT_ID, SIG)
    assert card.patches == []
    assert bridge._DEDUP_PREFIX + "dedupkey" in fake_redis.store


# ── D: срок жизни предложения и погасший алерт ────────────────────────────

def test_proposal_refusal_rules(sessions, monkeypatch):
    _seed_proposal(sessions, age_seconds=60, alert="firing")
    with sessions() as db:
        analysis = db.query(IncidentRecord).one().analysis
        assert bridge.proposal_refusal(db, analysis) is None

        monkeypatch.setattr(settings, "REMEDIATION_BRIDGE_PROPOSAL_TTL_SECONDS", 30)
        assert bridge.proposal_refusal(db, analysis).startswith("proposal_expired:")
        monkeypatch.setattr(settings, "REMEDIATION_BRIDGE_PROPOSAL_TTL_SECONDS", 0)
        assert bridge.proposal_refusal(db, analysis) is None

        db.query(AlertEvent).one().resolved_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()
        assert bridge.proposal_refusal(db, analysis) == "alert_resolved"
        monkeypatch.setattr(settings, "REMEDIATION_BRIDGE_REFUSE_RESOLVED", False)
        assert bridge.proposal_refusal(db, analysis) is None
        # Не мост — правило не действует вовсе.
        assert bridge.proposal_refusal(db, {"execution_intent_at": "2020-01-01T00:00:00Z"}) is None


def test_proposal_refusal_unknown_alert_is_not_a_refusal(sessions):
    _seed_proposal(sessions, alert="missing")
    with sessions() as db:
        assert bridge.proposal_refusal(db, db.query(IncidentRecord).one().analysis) is None


def _click(custom_id: str, user_id: str = "111") -> MagicMock:
    payload = {
        "type": 3,
        "data": {"custom_id": custom_id},
        "member": {"user": {"id": user_id, "username": "operator"}},
        "token": "tok",
        "message": {"id": "m-1", "channel_id": "c-1",
                    "embeds": [{"title": "x", "footer": {"text": "incident/x"}}]},
        "channel_id": "c-1",
    }
    request = MagicMock()
    request.body = AsyncMock(return_value=json.dumps(payload).encode())
    return request


class _Press:
    """Нажатие кнопки карточки моста с перехватом побочных эффектов."""

    def __init__(self, dispatch=None, dispatch_mode: str = "queue"):
        self.dispatch_mode = dispatch_mode
        self.audit: list = []
        self.edits: list = []
        self.dispatch = dispatch or MagicMock(return_value="task-1")

    async def __call__(self, custom_id: str, user_id: str = "111"):
        async def _edit(**kw):
            self.edits.append(kw)

        def _spawn(coro, **_):
            # edit-корутину исполняем сразу (она подменена), остальное закрываем.
            if getattr(coro, "__name__", "") == "_edit":
                try:
                    coro.send(None)
                except StopIteration:
                    pass
            else:
                coro.close()

        with patch.object(di, "_verify_signature", return_value=True), \
             patch.object(di.settings, "DISCORD_PUBLIC_KEY", "deadbeef"), \
             patch.object(di.settings, "EXECUTOR_ENABLED", True), \
             patch.object(di.settings, "EXECUTOR_APPROVAL_ENABLED", True), \
             patch.object(di.settings, "EXECUTOR_DISPATCH", self.dispatch_mode), \
             patch.object(di.settings, "DISCORD_APPROVERS_USER_IDS", "111"), \
             patch.object(di.settings, "DISCORD_APPROVAL_RATE_LIMIT_PER_HOUR", 0), \
             patch.object(di.audit_service, "log_event",
                          side_effect=lambda et, d: self.audit.append((et, d))), \
             patch("app.workers.executor_tasks.dispatch_apply", self.dispatch), \
             patch.object(di, "_edit_message_after_decision", _edit), \
             patch.object(di, "_spawn_background_task", side_effect=_spawn):
            return await di.discord_interactions(
                _click(custom_id, user_id), x_signature_ed25519="00" * 64,
                x_signature_timestamp="0",
            )


def _approvals(factory) -> list:
    with factory() as db:
        return [(r.status, r.approved_by) for r in db.query(ActionApproval).all()]


@pytest.mark.asyncio
@pytest.mark.parametrize("age,alert,needle,mark", [
    (7200, "firing", "просрочено", "⌛ Просрочено — не исполнено"),
    (60, "resolved", "погас", "ℹ️ Алерт погас — не исполнено"),
])
async def test_click_on_dead_card_is_refused_and_buttons_removed(
    sessions, fake_redis, age, alert, needle, mark,
):
    _seed_proposal(sessions, age_seconds=age, alert=alert)
    fake_redis.set(bridge._DEDUP_PREFIX + "dedupkey", "1", ex=21600)
    press = _Press()
    resp = await press(f"approve:{INCIDENT_ID}:{SIG}")

    assert needle in resp["data"]["content"] and "kubectl не запущен" in resp["data"]["content"]
    assert resp["data"]["flags"] == 64
    press.dispatch.assert_not_called()
    assert _approvals(sessions) == []            # одобрение не записано
    assert [e["mark"] for e in press.edits] == [mark]   # кнопки сняты, пометка в footer
    assert [e for e, _ in press.audit] == ["DISCORD_APPROVAL_DENIED_STALE_PROPOSAL"]
    # Claim дедупа снят: горящий алерт получит новую карточку.
    assert bridge._DEDUP_PREFIX + "dedupkey" not in fake_redis.store


@pytest.mark.asyncio
async def test_dead_card_can_still_be_declined(sessions, fake_redis):
    _seed_proposal(sessions, age_seconds=7200)
    press = _Press()
    resp = await press(f"decline:{INCIDENT_ID}:{SIG}")
    assert "Declined" in resp["data"]["content"]
    assert _approvals(sessions) == [("declined", "111")]


@pytest.mark.asyncio
async def test_live_card_click_dispatches_once_on_double_click(sessions, fake_redis):
    _seed_proposal(sessions)
    press = _Press()
    first = await press(f"approve:{INCIDENT_ID}:{SIG}")
    second = await press(f"approve:{INCIDENT_ID}:{SIG}")

    assert "Executor launched" in first["data"]["content"]
    assert "появится в карточке" in first["data"]["content"]
    assert "Already approved by <@111>" in second["data"]["content"]
    # applied_by — Discord user id; задача поставлена ровно один раз.
    press.dispatch.assert_called_once_with(INCIDENT_ID, "111", SIG)
    assert _approvals(sessions) == [("approved", "111")]
    assert len(press.edits) == 1 and press.edits[0]["mark"] is None


@pytest.mark.asyncio
async def test_stale_check_failure_does_not_break_click(sessions, fake_redis):
    """Быстрая проверка fail-open: рубеж перед записью — apply_intent."""
    _seed_proposal(sessions)
    press = _Press()
    with patch.object(bridge, "proposal_refusal", side_effect=RuntimeError("db")):
        resp = await press(f"approve:{INCIDENT_ID}:{SIG}")
    assert "Executor launched" in resp["data"]["content"]
    press.dispatch.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("dispatch_mode", ["queue", "inline"])
async def test_card_feedback_is_promised_only_for_bridge_cards(
    sessions, fake_redis, dispatch_mode,
):
    """`report_apply_outcome` правит только карточку моста: обычному (LLM)
    инциденту и мосту без записанной карточки ответ итог в карточке не
    обещает — на обоих путях исполнения (очередь executor и inline)."""
    _seed_proposal(sessions)                                   # мост с карточкой
    _seed_proposal(sessions, incident_id="rb-nocard", alert="missing")
    with sessions() as db:
        db.add(IncidentRecord(incident_id="fp-llm", status="COMPLETED",
                              data={"namespace": NS}, analysis={"root_cause": "x"}))
        nocard = db.query(IncidentRecord).filter_by(incident_id="rb-nocard").one()
        nocard.analysis = {k: val for k, val in nocard.analysis.items() if k != "bridge_card"}
        db.commit()

    press = _Press(dispatch_mode=dispatch_mode)
    replies = {}
    for incident_id in (INCIDENT_ID, "rb-nocard", "fp-llm", "fp-unknown"):
        resp = await press(f"approve:{incident_id}:{SIG}")
        replies[incident_id] = resp["data"]["content"]

    assert "появится в карточке" in replies[INCIDENT_ID]
    for incident_id in ("rb-nocard", "fp-llm", "fp-unknown"):
        assert "Executor launched" in replies[incident_id]
        assert "карточке" not in replies[incident_id]
        assert "audit-trail" in replies[incident_id]
    assert press.dispatch.call_count == (4 if dispatch_mode == "queue" else 0)


def _approve_in_db(factory) -> None:
    with factory() as db:
        db.add(ActionApproval(
            incident_id=INCIDENT_ID, intent_signature=SIG, status="approved",
            approved_by="111", decided_at=datetime.now(timezone.utc).replace(tzinfo=None),
        ))
        db.commit()


@pytest.mark.parametrize("age,alert,expected", [
    (7200, "firing", "proposal_expired"),
    (60, "resolved", "alert_resolved"),
])
def test_apply_intent_refuses_dead_proposal_before_any_kubectl(
    sessions, age, alert, expected,
):
    """Рубеж перед записью: задача могла простоять в очереди, а клик —
    пройти мимо быстрой проверки."""
    _seed_proposal(sessions, age_seconds=age, alert=alert)
    _approve_in_db(sessions)
    with patch.object(executor_apply.k8s_service, "execute_intent") as kubectl, \
         patch.object(executor_apply, "snapshot_target") as snapshot, \
         patch.object(executor_apply.audit_service, "log_event"):
        out = executor_apply.apply_intent(INCIDENT_ID, "111", SIG)
    assert out["ok"] is False and out["reason"].split(":")[0] == expected
    kubectl.assert_not_called()
    snapshot.assert_not_called()


# ── F: sync-вызовы обработчика — вне event loop ───────────────────────────

@pytest.mark.asyncio
async def test_blocking_calls_run_outside_event_loop(sessions, fake_redis):
    _seed_proposal(sessions)
    loop_thread = threading.get_ident()
    seen: dict = {}

    real_record, real_stale = di._record_decision, di._bridge_proposal_state

    def _record(*a, **kw):
        seen["record"] = threading.get_ident()
        return real_record(*a, **kw)

    def _stale(*a, **kw):
        seen["stale"] = threading.get_ident()
        return real_stale(*a, **kw)

    def _dispatch(*a, **kw):
        seen["dispatch"] = threading.get_ident()
        return "task-1"

    press = _Press(dispatch=MagicMock(side_effect=_dispatch))
    with patch.object(di, "_record_decision", _record), \
         patch.object(di, "_bridge_proposal_state", _stale):
        await press(f"approve:{INCIDENT_ID}:{SIG}")
    assert set(seen) == {"record", "stale", "dispatch"}
    assert loop_thread not in seen.values()


@pytest.mark.asyncio
async def test_feedback_store_runs_outside_event_loop(sessions):
    seen: dict = {}

    def _store(*a):
        seen["thread"] = threading.get_ident()
        return True

    request = _click("feedback_pos_fp-1")
    with patch.object(di, "_verify_signature", return_value=True), \
         patch.object(di.settings, "DISCORD_PUBLIC_KEY", "deadbeef"), \
         patch.object(di.settings, "DISCORD_APPROVERS_USER_IDS", "111"), \
         patch.object(di, "_store_feedback", _store):
        await di.discord_interactions(request, x_signature_ed25519="00" * 64,
                                      x_signature_timestamp="0")
    assert seen["thread"] != threading.get_ident()


# ── G: applied_by — один идентификатор на обоих путях ─────────────────────

@pytest.mark.asyncio
async def test_apply_confirm_and_approve_use_same_actor(sessions, fake_redis):
    _seed_proposal(sessions)
    press = _Press()
    await press(f"apply_confirm:{INCIDENT_ID}:{SIG}")
    assert _approvals(sessions) == [("approved", "111")]
    press.dispatch.assert_called_once_with(INCIDENT_ID, "111", SIG, "tok")
