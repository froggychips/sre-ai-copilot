"""ReportDelivery напрямую — без пайплайна и без моков восьми агентов.

Смысл этого файла ровно в том, чего раньше не было: outbox-механику можно
проверить на `record` + `db`, потому что доставка больше не метод
IncidentPipeline. Интеграция «пайплайн доводит инцидент до отчёта» осталась в
tests/test_report_delivery_outbox.py — здесь проверяется сам механизм.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.workers.report_delivery import (ReportDelivery, ReportDeliveryPending,
                                         severity_routeable)


class FakeRecord:
    """Минимальная замена IncidentRecord: доставке нужен только analysis."""

    def __init__(self, analysis=None):
        self.analysis = analysis if analysis is not None else {}


@pytest.fixture
def db():
    return MagicMock()


@pytest.fixture
def record():
    return FakeRecord()


@pytest.fixture
def rd(db, record):
    return ReportDelivery("RD-1", db, record)


def _args(**over):
    base = {
        "incident_id": "RD-1",
        "alertname": "HighErrorRate",
        "namespace": "squad-1",
        "severity": "warning",
        "synthesis": "разбор",
    }
    base.update(over)
    return base


@pytest.fixture
def sent(monkeypatch):
    """Discord-сервис, отдающий заданный delivered и копящий вызовы."""
    def _install(delivered=True, raises=None):
        mock = AsyncMock()
        if raises is not None:
            mock.send_incident_report.side_effect = raises
        else:
            mock.send_incident_report.return_value = delivered
        monkeypatch.setattr(
            "app.workers.report_delivery.discord_service", mock
        )
        return mock
    return _install


# --- успешная доставка ----------------------------------------------------


@pytest.mark.asyncio
async def test_successful_delivery_marks_sent(rd, record, sent):
    mock = sent(delivered=True)
    rd.mark_outboxed()
    rd.stage(_args())
    await rd.flush()

    assert mock.send_incident_report.await_count == 1
    assert ReportDelivery.PENDING_KEY not in record.analysis
    assert record.analysis[ReportDelivery.SENT_KEY]["attempts"] == 1
    assert rd.pending is None


@pytest.mark.asyncio
async def test_none_return_counts_as_delivered(rd, record, sent):
    """Старый контракт сервиса ничего не возвращал — ретраить вслепую хуже."""
    sent(delivered=None)
    rd.mark_outboxed()
    rd.stage(_args())
    await rd.flush()

    assert record.analysis[ReportDelivery.SENT_KEY]["attempts"] == 1


@pytest.mark.asyncio
async def test_nothing_staged_is_a_noop(rd, sent):
    mock = sent(delivered=True)
    await rd.flush()
    assert mock.send_incident_report.await_count == 0


# --- недоставка и ретрай --------------------------------------------------


@pytest.mark.asyncio
async def test_failed_delivery_raises_and_keeps_marker(rd, record, sent):
    sent(delivered=False)
    rd.mark_outboxed()
    rd.stage(_args())

    with pytest.raises(ReportDeliveryPending):
        await rd.flush()

    pending = record.analysis[ReportDelivery.PENDING_KEY]
    assert pending["attempts"] == 1
    assert pending["args"]["alertname"] == "HighErrorRate"
    assert "last_error_at" in pending
    assert ReportDelivery.SENT_KEY not in record.analysis


@pytest.mark.asyncio
async def test_send_exception_is_swallowed_into_retry(rd, record, sent):
    """Сервис по контракту не бросает; если всё же бросил — это ретрай, не краш."""
    sent(raises=RuntimeError("webhook down"))
    rd.mark_outboxed()
    rd.stage(_args())

    with pytest.raises(ReportDeliveryPending):
        await rd.flush()

    assert record.analysis[ReportDelivery.PENDING_KEY]["attempts"] == 1


@pytest.mark.asyncio
async def test_last_attempt_gives_up_without_raising(rd, record, sent):
    """На MAX_ATTEMPTS пишем report_failed и НЕ бросаем: лишний прогон таска бесполезен."""
    sent(delivered=False)
    rd.mark_outboxed()
    await rd.resend({"args": _args(), "attempts": ReportDelivery.MAX_ATTEMPTS - 1})

    assert ReportDelivery.PENDING_KEY not in record.analysis
    failed = record.analysis[ReportDelivery.FAILED_KEY]
    assert failed["attempts"] == ReportDelivery.MAX_ATTEMPTS
    assert failed["reason"] == "discord_delivery_failed"


@pytest.mark.asyncio
async def test_no_record_means_no_retry(db, sent):
    """Ad-hoc прогон без строки в БД: переотправлять не с чего — молча уходим.

    Ретрай прогнал бы LLM-стадии заново, что дороже потерянного embed-а.
    """
    sent(delivered=False)
    rd = ReportDelivery("RD-adhoc", db, None)
    rd.stage(_args())

    await rd.flush()  # без ReportDeliveryPending


@pytest.mark.asyncio
async def test_low_severity_is_not_retried(rd, record, sent):
    """info/none сервис намеренно не шлёт в #infra-error — это не сбой доставки."""
    sent(delivered=False)
    rd.mark_outboxed()
    rd.stage(_args(severity="info"))

    await rd.flush()  # без ReportDeliveryPending

    assert record.analysis[ReportDelivery.FAILED_KEY]["reason"] == "severity_gate_skip"
    assert ReportDelivery.PENDING_KEY not in record.analysis


@pytest.mark.asyncio
async def test_unusable_marker_is_closed_not_retried(rd, record, sent):
    """Маркер без args доставить нечем — ретрай крутился бы вечно."""
    mock = sent(delivered=True)
    rd.mark_outboxed()
    await rd.deliver({"attempts": 0})

    assert mock.send_incident_report.await_count == 0
    assert record.analysis[ReportDelivery.FAILED_KEY]["reason"] == "marker_unusable"


# --- чтение маркера -------------------------------------------------------


def test_load_pending_returns_live_marker(rd, record):
    record.analysis = {ReportDelivery.PENDING_KEY: {"args": _args(), "attempts": 1}}
    assert rd.load_pending()["attempts"] == 1


@pytest.mark.parametrize("done_key", [ReportDelivery.SENT_KEY, ReportDelivery.FAILED_KEY])
def test_pending_tail_is_not_resurrected(rd, record, done_key):
    """Доставлено либо сдались — хвост pending от прошлого цикла не воскрешаем."""
    record.analysis = {
        ReportDelivery.PENDING_KEY: {"args": _args(), "attempts": 1},
        done_key: {"attempts": 1},
    }
    assert rd.load_pending() is None


def test_garbage_marker_is_ignored(rd, record):
    record.analysis = {ReportDelivery.PENDING_KEY: {"args": "не dict"}}
    assert rd.load_pending() is None


def test_no_record_has_no_pending(db):
    assert ReportDelivery("RD-x", db, None).load_pending() is None


# --- сериализация полей embed --------------------------------------------


def test_send_kwargs_revives_intent_and_timestamp():
    from app.core.execution_dsl import ActionType, ExecutionIntent

    intent = ExecutionIntent(
        action=ActionType.RESTART_DEPLOYMENT,
        resource_type="deployment",
        resource_name="api",
        namespace="squad-1",
        risk="low",
    )
    kwargs = ReportDelivery.send_kwargs({
        "execution_intent": intent.model_dump(mode="json"),
        "incident_ts": "2026-08-10T10:00:00+00:00",
    })
    assert isinstance(kwargs["execution_intent"], ExecutionIntent)
    assert kwargs["incident_ts"].year == 2026


def test_broken_intent_does_not_cost_the_whole_report():
    """Битый intent = отчёт без кнопок, а не потерянный отчёт."""
    kwargs = ReportDelivery.send_kwargs({"execution_intent": {"action": "нет такого"}})
    assert kwargs["execution_intent"] is None


def test_broken_timestamp_degrades_to_none():
    kwargs = ReportDelivery.send_kwargs({"incident_ts": "не дата"})
    assert kwargs["incident_ts"] is None


# --- запись маркеров устойчива к сорванной сессии -------------------------


def test_marker_write_failure_does_not_raise(rd, record, db):
    """Best-effort: упавший commit не должен ронять уже завершённый разбор."""
    db.commit.side_effect = RuntimeError("session is closed")
    rd.mark_sent(1)  # не бросает
    db.rollback.assert_called_once()


# --- severity-gate --------------------------------------------------------


@pytest.mark.parametrize("severity,expected", [("critical", True), ("warning", True)])
def test_routeable_severities(severity, expected):
    assert severity_routeable(severity) is expected


def test_silent_severities_are_not_routeable():
    """info/none сервис в #infra-error не шлёт — и доставка это знает."""
    assert severity_routeable("info") is False
    assert severity_routeable(None) is False


def test_broken_routing_helper_defaults_to_routeable(monkeypatch):
    """Хелпер сервиса недоступен → лишний ретрай лучше похороненного разбора."""
    import app.services.discord.routing as routing

    def boom(_severity):
        raise RuntimeError("routing module broken")

    monkeypatch.setattr(routing, "_should_route_to_error", boom)
    assert severity_routeable("info") is True
