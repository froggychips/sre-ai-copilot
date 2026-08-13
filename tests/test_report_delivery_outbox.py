"""Outbox доставки Discord-отчёта: инцидент больше не теряет разбор.

Баг: терминальный state (RESOLVED/TRIAGE_REQUIRED) коммитился ДО
`discord_service.send_incident_report`, отправка была одноразовой и не бросала.
Отсюда две тихие потери отчёта с разбором и approve-кнопками:
  (а) транзиентный фейл POST-а — таск завершался «успешно», отчёта нет;
  (б) смерть воркера в окне «commit → send → ack» (task_acks_late) —
      redelivery видела терминальный start_state и уходила в
      `_record_resolved_early`, отчёт не отправлялся никогда.

Что проверяем:
  * delivered=False → терминальный state закоммичен, `report_pending` жив,
    поднят ReportDeliveryPending (Celery-ретрай);
  * повторный run() на терминальной строке с `report_pending` шлёт ТОЛЬКО
    отчёт (LLM-стадии не трогает), при True ставит `report_sent`;
  * успешный путь сразу ставит `report_sent`;
  * исчерпание попыток → `report_failed`, без вечного ретрая;
  * терминальный state БЕЗ маркера → прежнее поведение (resolved_early).

Моки discord_service — по образцу tests/test_stage_executor.py и
tests/test_pipeline_trace.py.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.models.hypothesis import Hypothesis, HypothesisSet
from app.core.execution_dsl import ActionType, ExecutionIntent
from app.core.state_machine import IncidentState
from app.diagnostics.facts import Fact, FactKind, FactStore
from app.workers.pipeline import IncidentPipeline
from app.workers.report_delivery import ReportDelivery, ReportDeliveryPending

# Маркеры и лимит попыток переехали в ReportDelivery вместе с механикой
# доставки; тест по-прежнему смотрит на них снаружи, через контракт класса.
_REPORT_PENDING_KEY = ReportDelivery.PENDING_KEY
_REPORT_SENT_KEY = ReportDelivery.SENT_KEY
_REPORT_FAILED_KEY = ReportDelivery.FAILED_KEY
_REPORT_MAX_ATTEMPTS = ReportDelivery.MAX_ATTEMPTS


# ── Helpers ────────────────────────────────────────────────────────────────

@pytest.fixture
def incident_data() -> dict:
    return {
        "incident_id": "OUTBOX-1",
        "severity": "warning",
        "status": "firing",
        "summary": "High error rate on api-7d8f",
        "description": "Sustained 5xx rate.",
        "namespace": "squad-1",
        # Без service/app в labels: `_resolve_team_owner` выходит сразу и не
        # тащит MagicMock-сервис в поля embed.
        "labels": {"alertname": "HighErrorRate", "pod": "api-7d8f"},
        "annotations": {},
        "starts_at": "2026-05-08T12:00:00Z",
        "ends_at": None,
        "generator_url": None,
        "raw": {},
    }


def _hyp_set() -> HypothesisSet:
    return HypothesisSet(items=[
        Hypothesis(cause="OOM on api-7d8f", anchored_facts=[FactKind.OOM_KILLED],
                   confidence=0.9, perspective="infra"),
    ])


def _fact_store() -> FactStore:
    return FactStore([Fact(kind=FactKind.OOM_KILLED, observed=True, confidence=0.95)])


def _record(status: str = IncidentState.OPEN.value, analysis=None):
    rec = MagicMock()
    rec.status = status
    rec.analysis = analysis
    rec.trace = None
    return rec


def _pipeline(incident_data: dict, record) -> IncidentPipeline:
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = record
    return IncidentPipeline(incident_data, db, record, MagicMock())


@pytest.fixture
def mocked_agents(mocker):
    """LLM-стадии + KG замокированы; discord-доставка настраивается в тесте."""
    analyze = mocker.patch("app.workers.pipeline.AnalyzerAgent.analyze",
                           new_callable=AsyncMock, return_value="a")
    mocker.patch("app.workers.pipeline.MultiHypothesisAgent.generate",
                 new_callable=AsyncMock, return_value=_hyp_set())
    mocker.patch("app.workers.pipeline.FactCriticAgent.critique_all",
                 new_callable=AsyncMock, return_value=_hyp_set())
    mocker.patch("app.workers.pipeline.FixAgent.suggest",
                 new_callable=AsyncMock, return_value=("f", None))
    mocker.patch("app.workers.pipeline.RiskAgent.assess",
                 new_callable=AsyncMock, return_value="r")
    synth = mocker.patch("app.workers.pipeline.SynthesisAgent.synthesize",
                         new_callable=AsyncMock, return_value="synth text")
    mocker.patch("app.workers.pipeline.SimilarIncidentEngine.find", return_value=[])
    mocker.patch("app.workers.pipeline.diag_engine.run", return_value=_fact_store())
    mocker.patch("app.workers.pipeline.populate_from_incident", MagicMock())
    mocker.patch("app.workers.pipeline.audit_service.log_event")
    return {"analyze": analyze, "synthesize": synth}


def _patch_send(mocker, *, delivered):
    return mocker.patch(
        "app.workers.report_delivery.discord_service.send_incident_report",
        new_callable=AsyncMock,
        return_value=delivered,
    )


# ── (а) delivered=False: state терминальный, маркер жив, исключение ────────

@pytest.mark.asyncio
async def test_failed_delivery_keeps_terminal_state_and_pending_marker(
    mocker, mocked_agents, incident_data
):
    send = _patch_send(mocker, delivered=False)
    record = _record()
    pl = _pipeline(incident_data, record)

    with pytest.raises(ReportDeliveryPending):
        await pl.run()

    # Терминальный state закоммичен ДО броска — ретрай не переигрывает pipeline.
    assert record.status == IncidentState.RESOLVED.value
    send.assert_awaited_once()

    analysis = record.analysis
    pending = analysis[_REPORT_PENDING_KEY]
    assert pending["attempts"] == 1
    assert _REPORT_SENT_KEY not in analysis
    # В маркере лежат ГОТОВЫЕ поля embed — повторная отправка не требует
    # ни LLM-стадий, ни пересборки из checkpoint-а.
    args = pending["args"]
    assert args["incident_id"] == "OUTBOX-1"
    assert args["synthesis"] == "synth text"
    assert args["cause"] == "OOM on api-7d8f"
    assert args["resolution_quality"] == "resolved"
    assert args["alertname"] == "HighErrorRate"
    assert args["pod"] == "api-7d8f"
    assert args["severity"] == "warning"


@pytest.mark.asyncio
async def test_failed_delivery_marker_survives_analysis_merge(
    mocker, mocked_agents, incident_data
):
    """Маркер мержится в analysis, а не затирает чужие ключи (executor_applied)."""
    _patch_send(mocker, delivered=False)
    record = _record(analysis={"executor_applied": {"applied_by": "yaroslav"}})
    pl = _pipeline(incident_data, record)

    with pytest.raises(ReportDeliveryPending):
        await pl.run()

    assert record.analysis["executor_applied"] == {"applied_by": "yaroslav"}
    assert _REPORT_PENDING_KEY in record.analysis


# ── (б) повторный run(): только отправка, без LLM-стадий ──────────────────

def _pending_analysis(attempts: int = 1) -> dict:
    intent = ExecutionIntent(
        action=ActionType.RESTART_DEPLOYMENT,
        resource_type="deployment",
        resource_name="town-service",
        namespace="squad-1",
        params={},
        risk="low",
    )
    return {
        "summary": "a",
        "synthesis": "synth text",
        _REPORT_PENDING_KEY: {
            "attempts": attempts,
            "queued_at": "2026-08-10T10:00:00+00:00",
            "args": {
                "incident_id": "OUTBOX-1",
                "alertname": "HighErrorRate",
                "namespace": "squad-1",
                "pod": "api-7d8f",
                "service": None,
                "node": None,
                "severity": "warning",
                "cause": "OOM on api-7d8f",
                "resolution_quality": "resolved",
                "synthesis": "synth text",
                "is_recurrence": False,
                "flap_count": 0,
                "execution_intent": intent.model_dump(mode="json"),
                "executor_result": {"status": "dry_run_ok"},
                "deploy_correlation": None,
                "team_owner": "squad-1",
                "recurrence_count_24h": 2,
                "recurrence_count_7d": 5,
                "incident_ts": "2026-05-08T12:00:00+00:00",
            },
        },
    }


@pytest.mark.asyncio
async def test_redelivery_on_terminal_row_sends_report_without_llm(
    mocker, mocked_agents, incident_data
):
    send = _patch_send(mocker, delivered=True)
    record = _record(status=IncidentState.RESOLVED.value, analysis=_pending_analysis())
    pl = _pipeline(incident_data, record)

    await pl.run()

    # LLM-стадии не запускались — только доставка.
    mocked_agents["analyze"].assert_not_awaited()
    mocked_agents["synthesize"].assert_not_awaited()
    send.assert_awaited_once()

    kwargs = send.await_args.kwargs
    assert kwargs["incident_id"] == "OUTBOX-1"
    assert kwargs["synthesis"] == "synth text"
    assert kwargs["recurrence_count_24h"] == 2
    # Сериализованные поля разворачиваются обратно в доменные типы.
    assert isinstance(kwargs["execution_intent"], ExecutionIntent)
    assert kwargs["execution_intent"].resource_name == "town-service"
    assert kwargs["incident_ts"] is not None and kwargs["incident_ts"].year == 2026

    # delivered=True → pending снят, report_sent зафиксирован (attempts += 1).
    assert _REPORT_PENDING_KEY not in record.analysis
    assert record.analysis[_REPORT_SENT_KEY]["attempts"] == 2
    # Терминальный статус не тронут, resolved_early не пишется.
    assert record.status == IncidentState.RESOLVED.value
    assert "resolved_early" not in record.analysis


@pytest.mark.asyncio
async def test_redelivery_failure_raises_and_bumps_attempts(
    mocker, mocked_agents, incident_data
):
    send = _patch_send(mocker, delivered=False)
    record = _record(status=IncidentState.TRIAGE_REQUIRED.value,
                     analysis=_pending_analysis(attempts=1))
    pl = _pipeline(incident_data, record)

    with pytest.raises(ReportDeliveryPending):
        await pl.run()

    send.assert_awaited_once()
    mocked_agents["analyze"].assert_not_awaited()
    assert record.analysis[_REPORT_PENDING_KEY]["attempts"] == 2
    assert record.status == IncidentState.TRIAGE_REQUIRED.value


@pytest.mark.asyncio
async def test_redelivery_gives_up_after_max_attempts(
    mocker, mocked_agents, incident_data
):
    """Последняя попытка не бросает: иначе Celery-ретраи крутились бы вечно."""
    send = _patch_send(mocker, delivered=False)
    record = _record(
        status=IncidentState.RESOLVED.value,
        analysis=_pending_analysis(attempts=_REPORT_MAX_ATTEMPTS - 1),
    )
    pl = _pipeline(incident_data, record)

    await pl.run()  # без исключения

    send.assert_awaited_once()
    assert _REPORT_PENDING_KEY not in record.analysis
    failed = record.analysis[_REPORT_FAILED_KEY]
    assert failed["attempts"] == _REPORT_MAX_ATTEMPTS
    assert failed["reason"] == "discord_delivery_failed"


@pytest.mark.asyncio
async def test_terminal_row_without_marker_still_records_resolved_early(
    mocker, mocked_agents, incident_data
):
    """Прежний путь (алерт погас до старта) не задет: отчёт не шлём."""
    send = _patch_send(mocker, delivered=True)
    record = _record(status=IncidentState.RESOLVED.value, analysis={})
    pl = _pipeline(incident_data, record)

    await pl.run()

    send.assert_not_awaited()
    assert record.analysis["resolved_early"]["reason"] == "terminal_before_start"


@pytest.mark.asyncio
async def test_already_sent_marker_is_not_redelivered(
    mocker, mocked_agents, incident_data
):
    """report_sent от прошлого прогона не воскрешает pending-хвост."""
    send = _patch_send(mocker, delivered=True)
    analysis = _pending_analysis()
    analysis[_REPORT_SENT_KEY] = {"sent_at": "2026-08-10T10:01:00+00:00", "attempts": 1}
    record = _record(status=IncidentState.RESOLVED.value, analysis=analysis)
    pl = _pipeline(incident_data, record)

    await pl.run()

    send.assert_not_awaited()
    assert record.analysis["resolved_early"]["reason"] == "terminal_before_start"


# ── (в) успешный путь: сразу report_sent ──────────────────────────────────

@pytest.mark.asyncio
async def test_successful_run_marks_report_sent(mocker, mocked_agents, incident_data):
    send = _patch_send(mocker, delivered=True)
    record = _record()
    pl = _pipeline(incident_data, record)

    await pl.run()

    send.assert_awaited_once()
    assert record.status == IncidentState.RESOLVED.value
    assert _REPORT_PENDING_KEY not in record.analysis
    assert record.analysis[_REPORT_SENT_KEY]["attempts"] == 1
    # Прогон завершён — checkpoint-ключ зачищен (регрессия на мерж analysis).
    assert "pipeline_checkpoint" not in record.analysis


@pytest.mark.asyncio
async def test_low_severity_report_is_not_retried(mocker, mocked_agents, incident_data):
    """severity-gate discord-сервиса отдаёт False намеренно — это не потеря."""
    send = _patch_send(mocker, delivered=False)
    incident_data = {**incident_data, "severity": "info"}
    record = _record()
    pl = _pipeline(incident_data, record)

    await pl.run()  # без ReportDeliveryPending

    send.assert_awaited_once()
    assert record.analysis[_REPORT_FAILED_KEY]["reason"] == "severity_gate_skip"
    assert _REPORT_PENDING_KEY not in record.analysis


@pytest.mark.asyncio
async def test_send_exception_is_swallowed_into_retry(
    mocker, mocked_agents, incident_data
):
    """Контракт «сервис не бросает» подстрахован: исключение = недоставка."""
    send = mocker.patch(
        "app.workers.report_delivery.discord_service.send_incident_report",
        new_callable=AsyncMock,
        side_effect=RuntimeError("boom"),
    )
    record = _record()
    pl = _pipeline(incident_data, record)

    with pytest.raises(ReportDeliveryPending):
        await pl.run()

    send.assert_awaited_once()
    assert record.status == IncidentState.RESOLVED.value
    assert record.analysis[_REPORT_PENDING_KEY]["attempts"] == 1
