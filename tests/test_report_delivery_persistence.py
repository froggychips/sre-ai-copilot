"""Маркеры доставки долетают до БД — на настоящей сессии, не на MagicMock.

Пробел, вскрытый внешним ревью: и `test_report_delivery_outbox`, и
`test_report_delivery_unit` работают через `MagicMock` вместо сессии, то есть
проверяют, что метод ВЫЗВАН, но не что строка изменилась. Именно в этом зазоре
живёт классическая ловушка JSON-колонок: SQLAlchemy не отслеживает мутацию
словаря на месте, и `analysis["report_sent"] = ...` без `flag_modified`
молча не порождает UPDATE.

Сейчас `_update_analysis` присваивает НОВУЮ копию (`dict(current)` → мутация →
присваивание), поэтому UPDATE генерируется. Эти тесты фиксируют именно
наблюдаемый результат в базе, а не способ его достижения: если кто-то
перепишет хелпер на мутацию на месте, они покраснеют.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, IncidentRecord
from app.workers.report_delivery import ReportDelivery


@pytest.fixture
def db_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def db(db_factory):
    return db_factory()


def _record(db, analysis=None):
    rec = IncidentRecord(
        incident_id="PERSIST-1",
        status="RESOLVED",
        analysis=analysis if analysis is not None else {},
    )
    db.add(rec)
    db.commit()
    return rec


def _reread(db_factory, incident_id="PERSIST-1"):
    """Прочитать строку НОВОЙ сессией: identity map первой ничего не подскажет."""
    return (
        db_factory()
        .query(IncidentRecord)
        .filter(IncidentRecord.incident_id == incident_id)
        .first()
    )


def test_mark_sent_is_visible_to_another_session(db_factory, db):
    rec = _record(db, {"report_pending": {"args": {"severity": "warning"}, "attempts": 0}})
    ReportDelivery("PERSIST-1", db, rec).mark_sent(2)

    fresh = _reread(db_factory)
    assert fresh.analysis["report_sent"]["attempts"] == 2
    assert "report_pending" not in fresh.analysis, "pending обязан сниматься в БД, а не только в памяти"


def test_mark_failed_is_visible_to_another_session(db_factory, db):
    rec = _record(db, {"report_pending": {"args": {}, "attempts": 3}})
    ReportDelivery("PERSIST-1", db, rec).mark_failed(4, "discord_delivery_failed")

    fresh = _reread(db_factory)
    assert fresh.analysis["report_failed"]["reason"] == "discord_delivery_failed"
    assert fresh.analysis["report_failed"]["attempts"] == 4


def test_bump_attempts_is_visible_to_another_session(db_factory, db):
    """Счётчик попыток обязан пережить смерть воркера — иначе ретраи бесконечны."""
    rec = _record(db, {"report_pending": {"args": {"severity": "critical"}, "attempts": 0}})
    rd = ReportDelivery("PERSIST-1", db, rec)
    rd.stage({"severity": "critical"})
    rd.bump_attempts({"severity": "critical"}, 2)

    fresh = _reread(db_factory)
    pending = fresh.analysis["report_pending"]
    assert pending["attempts"] == 2
    assert "last_error_at" in pending


def test_refresh_args_keeps_other_marker_fields(db_factory, db):
    """Обогащённые поля дописываются, а queued_at/attempts не теряются."""
    rec = _record(db, {"report_pending": {
        "args": {"severity": "warning"},
        "attempts": 1,
        "queued_at": "2026-08-13T10:00:00+00:00",
    }})
    ReportDelivery("PERSIST-1", db, rec).refresh_args({"severity": "warning", "team_owner": "gr-wo"})

    pending = _reread(db_factory).analysis["report_pending"]
    assert pending["args"]["team_owner"] == "gr-wo"
    assert pending["attempts"] == 1
    assert pending["queued_at"] == "2026-08-13T10:00:00+00:00"


def test_markers_do_not_wipe_foreign_keys_in_analysis(db_factory, db):
    """executor_applied и прочая apply-provenance переживают запись маркеров.

    Это единственный guard от повторного реального kubectl: если доставка
    отчёта затрёт его, старая Discord-кнопка сможет прогнать вторую мутацию.
    """
    rec = _record(db, {
        "executor_applied": {"at": "2026-08-13T09:00:00+00:00", "by": "yaroslav"},
        "report_pending": {"args": {}, "attempts": 0},
    })
    ReportDelivery("PERSIST-1", db, rec).mark_sent(1)

    fresh = _reread(db_factory)
    assert fresh.analysis["executor_applied"]["by"] == "yaroslav"
    assert "report_sent" in fresh.analysis


def test_load_pending_reads_committed_state(db_factory, db):
    rec = _record(db, {"report_pending": {"args": {"severity": "critical"}, "attempts": 1}})
    db.commit()
    assert ReportDelivery("PERSIST-1", db, rec).load_pending()["attempts"] == 1


def test_marker_write_on_dead_session_does_not_raise(db_factory, db):
    """Best-effort: разорванная сессия не должна ронять уже завершённый разбор."""
    rec = _record(db, {"report_pending": {"args": {}, "attempts": 0}})
    rd = ReportDelivery("PERSIST-1", db, rec)
    db.close()
    db.bind.dispose()          # соединение больше не поднимется
    rd.mark_sent(1)            # не бросает
