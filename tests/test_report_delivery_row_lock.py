"""Запись маркера идёт под row-lock и не затирает чужие ключи в analysis.

Замечание внешнего ревью, которое подтвердилось: запись маркера — классический
read-modify-write по JSON-колонке (прочитали `analysis` целиком, дописали свой
ключ, записали целиком). Второй писатель, прочитавший ту же версию, затирает
работу первого. Теряется при этом не «лишний» ключ, а `executor_applied` —
единственный guard от повторного реального `kubectl`.

Тот же приём (`SELECT … FOR UPDATE`) по тем же причинам уже применён в
`executor_apply.py` и `discord/dedup_store.py`; в доставке отчёта его не было.
"""
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.database import Base, IncidentRecord
from app.workers.report_delivery import ReportDelivery


@pytest.fixture
def db_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _record(db, analysis=None):
    rec = IncidentRecord(
        incident_id="LOCK-1", status="RESOLVED",
        analysis=analysis if analysis is not None else {},
    )
    db.add(rec)
    db.commit()
    return rec


def test_marker_write_reloads_row_under_lock(db_factory):
    """Перед мутацией строка перечитывается — иначе пишем поверх устаревшей копии."""
    db = db_factory()
    rec = _record(db, {"report_pending": {"args": {}, "attempts": 0}})

    selects = []

    @event.listens_for(db.bind, "before_cursor_execute")
    def capture(conn, cursor, statement, params, context, executemany):
        if statement.strip().upper().startswith("SELECT"):
            selects.append(" ".join(statement.split()))

    ReportDelivery("LOCK-1", db, rec).mark_sent(1)
    assert any("FROM incidents" in s for s in selects), (
        "строка не перечитывалась перед записью маркера — read-modify-write "
        "идёт по копии из identity map"
    )


def test_concurrent_writer_key_is_not_lost(db_factory):
    """Ключ, дописанный ДРУГОЙ сессией, переживает нашу запись маркера.

    Это и есть lost update из ревью: без перечитывания под локом мы бы записали
    свою старую копию analysis и стёрли executor_applied.
    """
    db = db_factory()
    rec = _record(db, {"report_pending": {"args": {}, "attempts": 0}})

    # Материализуем analysis В НАШЕЙ сессии до чужой записи. Без этой строки
    # гонка не воспроизводится: expire_on_commit=True помечает объект
    # устаревшим после commit в _record, и следующее обращение молча
    # перечитало бы строку — то есть тест «проходил» бы и без лока.
    assert "report_pending" in rec.analysis

    # Другой воркер дописал apply-provenance уже ПОСЛЕ того, как наша сессия
    # прочитала запись.
    other = db_factory()
    other_rec = other.query(IncidentRecord).filter_by(incident_id="LOCK-1").first()
    other_rec.analysis = {
        **other_rec.analysis,
        "executor_applied": {"applied_by": "yaroslav", "at": "2026-08-14T09:00:00+00:00"},
    }
    other.commit()

    ReportDelivery("LOCK-1", db, rec).mark_sent(1)

    fresh = db_factory().query(IncidentRecord).filter_by(incident_id="LOCK-1").first()
    assert fresh.analysis["executor_applied"]["applied_by"] == "yaroslav", (
        "маркер доставки затёр executor_applied — guard от повторного kubectl потерян"
    )
    assert fresh.analysis["report_sent"]["attempts"] == 1
    assert "report_pending" not in fresh.analysis


def test_lock_failure_does_not_lose_the_marker(db_factory, monkeypatch):
    """Лок best-effort: если refresh упал, маркер всё равно записывается.

    Обратный приоритет был бы хуже: потерять `report_sent` из-за недоступного
    `SELECT … FOR UPDATE` значит вечно ретраить уже доставленный отчёт.
    """
    db = db_factory()
    rec = _record(db, {"report_pending": {"args": {}, "attempts": 0}})

    def boom(*a, **kw):
        raise RuntimeError("FOR UPDATE не поддерживается")

    monkeypatch.setattr(db, "refresh", boom)
    ReportDelivery("LOCK-1", db, rec).mark_sent(2)

    fresh = db_factory().query(IncidentRecord).filter_by(incident_id="LOCK-1").first()
    assert fresh.analysis["report_sent"]["attempts"] == 2


def test_lock_is_requested_with_for_update(db_factory, monkeypatch):
    """refresh вызывается именно с with_for_update — иначе это просто перечитывание."""
    db = db_factory()
    rec = _record(db)
    calls = []
    original = db.refresh

    def spy(instance, **kwargs):
        calls.append(kwargs)
        return original(instance, **{k: v for k, v in kwargs.items() if k != "with_for_update"})

    monkeypatch.setattr(db, "refresh", spy)
    ReportDelivery("LOCK-1", db, rec).mark_failed(1, "severity_gate_skip")

    assert calls and calls[0].get("with_for_update") is True
