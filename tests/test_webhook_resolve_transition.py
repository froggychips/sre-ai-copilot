"""Регрессия на две находки глубокого ревью в app/api/webhooks.py.

ФИКС 1 — resolve-вебхук писал RESOLVED МИМО state machine.
Статус переводился в RESOLVED из ЛЮБОГО нетерминального состояния, включая
INVESTIGATING/HYPOTHESIS_GENERATED у работающего прямо сейчас пайплайна.
Короткоживущий алерт (median TTR ~11 мин против многоминутного пайплайна)
ронял in-flight пайплайн: следующий `_safe_transition` упирался в терминал,
отчёт не отправлялся. Теперь переход идёт через `StateMachine`: валидный —
выполняется, невалидный — откладывается маркером в `data`.

ФИКС 2 — `prev_status` в логе всегда врал: присваивание шло ДО лога, поэтому
`prev_status=existing.status` печатал уже НОВОЕ значение ("RESOLVED").

ФИКС 3 — enrich-эндпоинт звал синхронный `enrich_alert` прямо из event loop
(~15-17s блокировки worst-case). Теперь — `enrich_alert_async`
(asyncio.to_thread), последовательными await-ами (Session не потокобезопасна).
"""
import asyncio
import threading
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.state_machine import IncidentState, StateMachine
from app.database import Base, IncidentRecord


@pytest.fixture
def db():
    """In-memory SQLite сессия с таблицей incidents."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _payload(fingerprint: str, *, status: str = "resolved", ends_at=None):
    from app.models.incident import AlertManagerWebhook

    return AlertManagerWebhook(
        version="4",
        groupKey=f"grp-{fingerprint}",
        status=status,
        alerts=[
            {
                "status": status,
                "labels": {
                    "alertname": "KubePodCrashLooping",
                    "severity": "critical",
                    "namespace": "prod-kingdom1",
                    "service": "town-service",
                },
                "annotations": {"summary": "s", "description": "d"},
                "startsAt": "2026-08-07T10:00:00Z",
                "endsAt": ends_at,
                "generatorURL": "https://prom.local",
                "fingerprint": fingerprint,
            }
        ],
    )


def _run_resolve(webhooks, db, fingerprint: str, **kw):
    return asyncio.run(webhooks.alertmanager_webhook(_payload(fingerprint, **kw), db=db))


@pytest.fixture
def webhooks(monkeypatch):
    """Модуль вебхуков с заглушённым raw_collector и перехваченным логом."""
    import app.api.webhooks as webhooks_mod

    monkeypatch.setattr(webhooks_mod.raw_collector, "ingest", MagicMock())
    monkeypatch.setattr(webhooks_mod, "log", MagicMock())
    return webhooks_mod


def _log_events(webhooks) -> dict:
    """{event_name: kwargs} по всем вызовам log.info."""
    return {
        call.args[0]: call.kwargs
        for call in webhooks.log.info.call_args_list
        if call.args
    }


# ── ФИКС 1: resolve во время активного пайплайна ─────────────────────────
@pytest.mark.parametrize(
    "in_flight",
    [
        # OPEN сюда НЕ входит: это «принят, работа не начиналась», и
        # OPEN → RESOLVED разрешён специально (см. тест ниже про
        # короткоживущий алерт). Здесь — только состояния работающего
        # пайплайна, где форс-запись терминала убила бы прогон.
        IncidentState.INVESTIGATING.value,
        IncidentState.HYPOTHESIS_GENERATED.value,
        IncidentState.APPROVAL_PENDING.value,
    ],
)
def test_resolve_mid_pipeline_does_not_force_invalid_transition(
    webhooks, db, in_flight
):
    """Из состояний, откуда RESOLVED невалиден, статус НЕ перезаписывается."""
    # Предусловие находки: именно эти переходы state machine запрещает.
    assert not StateMachine.validate_transition(
        IncidentState(in_flight), IncidentState.RESOLVED
    )

    db.add(IncidentRecord(
        incident_id=f"FP-INFLIGHT-{in_flight}",
        status=in_flight,
        data={"flap_count": 2},
    ))
    db.commit()

    result = _run_resolve(webhooks, db, f"FP-INFLIGHT-{in_flight}")

    assert result["alerts"][0]["task_id"] == "resolve-deferred"

    db.expire_all()
    row = db.query(IncidentRecord).filter_by(
        incident_id=f"FP-INFLIGHT-{in_flight}").first()
    # Статус нетронут — пайплайн доедет до своего терминала штатно.
    assert row.status == in_flight
    # Резолв не потерян: маркер лежит в data (там же, где flap_count).
    assert row.data["resolve_pending"] is True
    assert row.data["resolve_pending_from"] == in_flight
    assert row.data["resolved_at"]
    assert row.data["flap_count"] == 2, "существующий data не затёрт"


def test_resolve_from_open_closes_incident(webhooks, db):
    """Короткоживущий алерт: погас до начала расследования → штатный RESOLVED.

    OPEN означает «принят, работа не начиналась», поэтому откладывать тут
    нечего. Это НЕ придирка к семантике: при LLM_PIPELINE_ENABLED=false —
    а это дефолт и продовый advisory-режим — пайплайн выходит раньше любого
    перехода, OPEN не покидается никогда. Если резолв из OPEN откладывать,
    строка зависает в OPEN навсегда, а повторный fire дедуплицируется вместо
    того, чтобы обработаться как flapping.

    Гонка «резолв пришёл, пока celery-задача стартует» безопасна: первый же
    _safe_transition увидит терминал и поднимет IncidentResolvedExternally.
    """
    assert StateMachine.validate_transition(
        IncidentState.OPEN, IncidentState.RESOLVED
    )

    db.add(IncidentRecord(
        incident_id="FP-OPEN-RESOLVE",
        status=IncidentState.OPEN.value,
        data={"flap_count": 1},
    ))
    db.commit()

    result = _run_resolve(webhooks, db, "FP-OPEN-RESOLVE")
    assert result["alerts"][0]["task_id"] == "resolved"

    db.expire_all()
    row = db.query(IncidentRecord).filter_by(
        incident_id="FP-OPEN-RESOLVE").first()
    assert row.status == IncidentState.RESOLVED.value
    # Откладывать было нечего — маркера отложенного резолва быть не должно.
    assert not row.data.get("resolve_pending")
    assert row.data["flap_count"] == 1, "существующий data не затёрт"


def test_resolve_mid_pipeline_keeps_pipeline_transitions_valid(webhooks, db):
    """После отложенного резолва следующий шаг пайплайна остаётся валидным."""
    db.add(IncidentRecord(
        incident_id="FP-NEXTSTEP",
        status=IncidentState.INVESTIGATING.value,
        data={},
    ))
    db.commit()

    _run_resolve(webhooks, db, "FP-NEXTSTEP")

    db.expire_all()
    row = db.query(IncidentRecord).filter_by(incident_id="FP-NEXTSTEP").first()
    # Раньше строка уезжала в RESOLVED и этот переход становился невалидным —
    # пайплайн падал, попытка уйти в FAILED тоже была невалидна.
    assert StateMachine.validate_transition(
        IncidentState(row.status), IncidentState.FACTS_COLLECTED
    )
    assert StateMachine.validate_transition(
        IncidentState(row.status), IncidentState.FAILED
    )


def test_resolve_deferred_is_logged_with_real_prev_status(webhooks, db):
    """Отложенный резолв логируется отдельным событием с реальным статусом."""
    db.add(IncidentRecord(
        incident_id="FP-DEFLOG",
        status=IncidentState.INVESTIGATING.value,
        data={},
    ))
    db.commit()

    _run_resolve(webhooks, db, "FP-DEFLOG")

    events = _log_events(webhooks)
    assert "webhook.alert_resolved" not in events
    assert events["webhook.alert_resolve_deferred"]["prev_status"] == (
        IncidentState.INVESTIGATING.value
    )


def test_resolve_uses_alert_ends_at_as_resolved_at(webhooks, db):
    """resolved_at берётся из endsAt алерта, если он есть."""
    db.add(IncidentRecord(
        incident_id="FP-ENDSAT",
        status=IncidentState.INVESTIGATING.value,
        data={},
    ))
    db.commit()

    _run_resolve(webhooks, db, "FP-ENDSAT", ends_at="2026-08-07T10:11:00Z")

    db.expire_all()
    row = db.query(IncidentRecord).filter_by(incident_id="FP-ENDSAT").first()
    assert row.data["resolved_at"] == "2026-08-07T10:11:00Z"


# ── ФИКС 1: валидные переходы по-прежнему выполняются ────────────────────
@pytest.mark.parametrize(
    "valid_from",
    [
        IncidentState.FACTS_COLLECTED.value,
        IncidentState.FIX_PROPOSED.value,
        IncidentState.EXECUTING.value,
    ],
)
def test_resolve_applies_when_transition_is_valid(webhooks, db, valid_from):
    """Из состояний, откуда RESOLVED валиден, статус пишется как и раньше."""
    assert StateMachine.validate_transition(
        IncidentState(valid_from), IncidentState.RESOLVED
    )

    db.add(IncidentRecord(
        incident_id=f"FP-VALID-{valid_from}",
        status=valid_from,
        data={},
    ))
    db.commit()

    result = _run_resolve(webhooks, db, f"FP-VALID-{valid_from}")

    assert result["alerts"][0]["task_id"] == "resolved"
    db.expire_all()
    row = db.query(IncidentRecord).filter_by(
        incident_id=f"FP-VALID-{valid_from}").first()
    assert row.status == IncidentState.RESOLVED.value
    assert "resolve_pending" not in (row.data or {})


# ── ФИКС 2: prev_status в логе — РЕАЛЬНЫЙ предыдущий статус ──────────────
def test_resolved_log_reports_real_prev_status(webhooks, db):
    """Раньше лог печатал уже новое значение и всегда врал 'RESOLVED'."""
    db.add(IncidentRecord(
        incident_id="FP-PREVLOG",
        status=IncidentState.FIX_PROPOSED.value,
        data={},
    ))
    db.commit()

    _run_resolve(webhooks, db, "FP-PREVLOG")

    kwargs = _log_events(webhooks)["webhook.alert_resolved"]
    assert kwargs["incident_id"] == "FP-PREVLOG"
    assert kwargs["prev_status"] == IncidentState.FIX_PROPOSED.value
    assert kwargs["prev_status"] != IncidentState.RESOLVED.value


# ── Терминальные статусы и неизвестный инцидент ──────────────────────────
@pytest.mark.parametrize(
    "terminal",
    [
        IncidentState.RESOLVED.value,
        IncidentState.TRIAGE_REQUIRED.value,
        IncidentState.FAILED.value,
    ],
)
def test_resolve_on_terminal_incident_is_noop(webhooks, db, terminal):
    """Терминальную строку резолв не трогает и маркером не пачкает."""
    db.add(IncidentRecord(
        incident_id=f"FP-TERM-{terminal}",
        status=terminal,
        data={"flap_count": 1},
    ))
    db.commit()

    result = _run_resolve(webhooks, db, f"FP-TERM-{terminal}")

    assert result["alerts"][0]["task_id"] == "resolved"
    db.expire_all()
    row = db.query(IncidentRecord).filter_by(
        incident_id=f"FP-TERM-{terminal}").first()
    assert row.status == terminal
    assert row.data == {"flap_count": 1}
    assert _log_events(webhooks) == {}


def test_resolve_for_unknown_incident_is_accepted(webhooks, db):
    """Резолв алерта, которого нет в БД, просто ACK-ается без записи."""
    result = _run_resolve(webhooks, db, "FP-UNKNOWN")

    assert result["alerts"][0]["task_id"] == "resolved"
    assert db.query(IncidentRecord).count() == 0


# ── ФИКС 3: enrich-путь не блокирует event loop ──────────────────────────
def _enrich_payload(fingerprint: str = "FP-ENRICH"):
    from app.models.incident import AlertManagerWebhook

    return AlertManagerWebhook(
        version="4",
        groupKey="grp-enrich",
        status="firing",
        alerts=[
            {
                "status": "firing",
                "labels": {
                    "alertname": "KubePodCrashLooping",
                    "severity": "critical",
                    "namespace": "prod-kingdom1",
                    "service": "town-service",
                    "pod": "town-service-6c6cd4df-8hx9c",
                },
                "annotations": {"summary": "s", "description": "d"},
                "startsAt": "2026-08-07T10:00:00Z",
                "endsAt": None,
                "generatorURL": "https://prom.local",
                "fingerprint": fingerprint,
            }
        ],
    )


@pytest.fixture
def enrich_env(webhooks, monkeypatch):
    """Мокает всё вокруг enrich-эндпоинта, кроме самого enrich_alert."""
    import app.knowledge_graph.auto_populator as populator_mod
    import app.services.alert_dedup as dedup_mod
    import app.services.discord_service as discord_mod

    monkeypatch.setattr(webhooks.settings, "DISCORD_ENRICH_ENABLED", True)
    monkeypatch.setattr(
        populator_mod, "populate_from_incident", lambda db, inc: {}
    )

    async def _send(**kw):
        return dedup_mod.Decision.SEND

    monkeypatch.setattr(dedup_mod, "decide_send", _send)
    monkeypatch.setattr(
        discord_mod.DiscordService, "send_enriched_alert", AsyncMock()
    )
    return webhooks


def test_enrich_runs_in_worker_thread_not_on_event_loop(enrich_env, monkeypatch):
    """enrich_alert исполняется в thread pool, а не в потоке event loop."""
    import app.services.alert_enrichment as enrichment_mod

    threads = []

    def fake_enrich(db, incident):
        threads.append(threading.get_ident())
        return MagicMock()

    monkeypatch.setattr(enrichment_mod, "enrich_alert", fake_enrich)

    loop_thread = threading.get_ident()
    result = asyncio.run(
        enrich_env.alertmanager_webhook_enrich_and_forward(
            _enrich_payload(), db=MagicMock()
        )
    )

    assert result["enriched_groups"] == 1
    assert len(threads) == 1
    # Прямой вызов синхронного enrich_alert исполнялся бы в потоке loop-а.
    assert threads[0] != loop_thread


def test_enrich_does_not_block_event_loop(enrich_env, monkeypatch):
    """Пока enrich «висит» 150 мс, loop продолжает крутить другие таски."""
    import app.services.alert_enrichment as enrichment_mod

    def slow_enrich(db, incident):
        time.sleep(0.15)
        return MagicMock()

    monkeypatch.setattr(enrichment_mod, "enrich_alert", slow_enrich)

    async def _scenario():
        ticks = {"n": 0}

        async def heartbeat():
            while True:
                await asyncio.sleep(0.01)
                ticks["n"] += 1

        hb = asyncio.create_task(heartbeat())
        try:
            await enrich_env.alertmanager_webhook_enrich_and_forward(
                _enrich_payload("FP-ENRICH-SLOW"), db=MagicMock()
            )
        finally:
            hb.cancel()
        return ticks["n"]

    ticks = asyncio.run(_scenario())
    # Синхронный вызов дал бы 0 тиков — loop стоял бы все 150 мс.
    assert ticks >= 3, f"event loop был заблокирован (тиков: {ticks})"


def test_enrich_awaits_sequentially_per_session(enrich_env, monkeypatch):
    """Обогащение группы идёт ПОСЛЕДОВАТЕЛЬНО: Session не потокобезопасна."""
    import app.services.alert_enrichment as enrichment_mod

    concurrent = {"now": 0, "max": 0}
    lock = threading.Lock()

    def tracking_enrich(db, incident):
        with lock:
            concurrent["now"] += 1
            concurrent["max"] = max(concurrent["max"], concurrent["now"])
        time.sleep(0.02)
        with lock:
            concurrent["now"] -= 1
        return MagicMock()

    monkeypatch.setattr(enrichment_mod, "enrich_alert", tracking_enrich)

    payload = _enrich_payload("FP-SEQ-1")
    second = payload.alerts[0].model_copy(update={"fingerprint": "FP-SEQ-2"})
    payload.alerts.append(second)

    asyncio.run(
        enrich_env.alertmanager_webhook_enrich_and_forward(payload, db=MagicMock())
    )

    assert concurrent["max"] == 1, "две задачи делили одну Session одновременно"
