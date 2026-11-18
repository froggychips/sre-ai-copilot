"""Ретеншен таблиц процесса remediation, dry-run и ссылки между таблицами.

До 18.11.2026 срока не было у `kg_remediation_decisions`,
`kg_action_approvals`, `kg_remediation_attempts` и у мостовых строк
`incidents` (`rb-…`), а чистка `kg_incidents` полагалась на порядок списка
там, где нужен был запрет удалять строку со ссылкой.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import app.remediation.attempts  # noqa: F401 — регистрация kg_remediation_attempts
import app.remediation.models  # noqa: F401 — регистрация kg_remediation_decisions
from app.database import Base, IncidentRecord
from app.knowledge_graph.health_retention import (
    DEFAULT_WORKFLOW_RETENTION_DAYS, MIN_EVENT_RETENTION_DAYS, MODE_APPLY,
    MODE_DRY_RUN, MODE_OFF, WORKFLOW_RETENTION_TARGETS, purge_events,
    purge_table, purge_workflow, retention_mode)
from app.knowledge_graph.schema import (ActionApproval, KGIncident,
                                        KGRemediationEvent)
from app.remediation.attempts import RemediationAttempt
from app.remediation.models import RemediationDecision
from tests.conftest import requires_postgres

NOW = datetime(2026, 11, 18, 3, 40)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _ago(days):
    return NOW - timedelta(days=days)


def _bridge(db, incident_id, days, **kw):
    db.add(IncidentRecord(incident_id=incident_id, status="APPROVAL_PENDING",
                          created_at=_ago(days), **kw))


def _decision(db, incident_id, days):
    db.add(RemediationDecision(incident_id=incident_id, idempotency_key=f"k-{incident_id}-{days}",
                               created_at=_ago(days)))


def _approval(db, incident_id, days):
    db.add(ActionApproval(incident_id=incident_id, intent_signature=f"s-{days}",
                          status="approved", created_at=_ago(days)))


def _attempt(db, incident_id, days, status="verified"):
    db.add(RemediationAttempt(incident_id=incident_id, signature=f"s-{days}", status=status,
                              created_at=_ago(days), updated_at=_ago(days)))


# --- режимы -----------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("off", MODE_OFF), ("", MODE_OFF), (None, MODE_OFF), ("OFF", MODE_OFF),
    ("dry_run", MODE_DRY_RUN), ("dry-run", MODE_DRY_RUN), (" Dry_Run ", MODE_DRY_RUN),
    ("apply", MODE_APPLY), ("APPLY", MODE_APPLY),
    ("true", MODE_OFF), ("on", MODE_OFF), ("aply", MODE_OFF), ("1", MODE_OFF),
])
def test_unknown_mode_never_deletes(raw, expected):
    """Опечатка в настройке читается как off: непонятное не должно удалять."""
    assert retention_mode(raw) == expected


def test_workflow_settings_default_off():
    from app.config import Settings

    fields = Settings.model_fields
    assert fields["KG_WORKFLOW_RETENTION_MODE"].default == "off"
    assert fields["KG_WORKFLOW_RETENTION_DAYS"].default == DEFAULT_WORKFLOW_RETENTION_DAYS
    assert DEFAULT_WORKFLOW_RETENTION_DAYS >= MIN_EVENT_RETENTION_DAYS


def test_workflow_registry_lists_children_before_parent():
    tables = [t for t, _, _ in WORKFLOW_RETENTION_TARGETS]
    assert set(tables) == {
        "kg_remediation_decisions", "kg_action_approvals",
        "kg_remediation_attempts", "incidents",
    }
    assert tables[-1] == "incidents", "родитель уходит последним"


# --- что удаляется ----------------------------------------------------------


def test_old_workflow_rows_are_deleted_and_recent_survive(db):
    for days in (200, 30):
        _decision(db, f"rb-{days}", days)
        _approval(db, f"rb-{days}", days)
        _attempt(db, f"rb-{days}", days)
        _bridge(db, f"rb-{days}", days)
    db.commit()

    stats = purge_workflow(db, retention_days=180, dry_run=False, now=NOW)

    assert stats["deleted"] == 4
    assert [d.incident_id for d in db.query(RemediationDecision)] == ["rb-30"]
    assert [a.incident_id for a in db.query(ActionApproval)] == ["rb-30"]
    assert [a.incident_id for a in db.query(RemediationAttempt)] == ["rb-30"]
    assert [i.incident_id for i in db.query(IncidentRecord)] == ["rb-30"]


def test_only_bridge_rows_of_incidents_are_purged(db):
    """Разборы LLM-пайплайна в той же таблице срока не получают."""
    _bridge(db, "rb-abc-1-ff", 400)
    db.add(IncidentRecord(incident_id="fp-deadbeef", status="RESOLVED", created_at=_ago(400)))
    db.add(IncidentRecord(incident_id="rbx-not-bridge", status="RESOLVED", created_at=_ago(400)))
    db.commit()

    purge_workflow(db, retention_days=180, dry_run=False, now=NOW)

    left = sorted(i.incident_id for i in db.query(IncidentRecord))
    assert left == ["fp-deadbeef", "rbx-not-bridge"]


def test_bridge_row_waits_for_its_children(db):
    """Одобрение обновлено позже карточки — карточка ждёт, пока уйдёт оно."""
    _bridge(db, "rb-parent", 200)
    _attempt(db, "rb-parent", 100)          # попытку верифицировали позже
    _bridge(db, "rb-lonely", 200)
    db.commit()

    purge_workflow(db, retention_days=180, dry_run=False, now=NOW)

    assert [i.incident_id for i in db.query(IncidentRecord)] == ["rb-parent"]
    assert db.query(RemediationAttempt).count() == 1


def test_bridge_row_with_live_executor_claim_survives(db):
    _bridge(db, "rb-busy", 200, executor_state="in_flight")
    _bridge(db, "rb-done", 200, executor_state="applied")
    db.commit()

    purge_workflow(db, retention_days=180, dry_run=False, now=NOW)

    assert [i.incident_id for i in db.query(IncidentRecord)] == ["rb-busy"]


def test_workflow_dry_run_is_default_and_counts(db):
    _decision(db, "rb-1", 200)
    _bridge(db, "rb-2", 200)
    _bridge(db, "rb-3", 10)
    db.commit()

    stats = purge_workflow(db, retention_days=180, now=NOW)

    assert stats["dry_run"] is True and stats["deleted"] == 0
    assert stats["would_delete"] == 2
    assert stats["per_table"]["incidents"]["would_delete"] == 1
    assert db.query(RemediationDecision).count() == 1
    assert db.query(IncidentRecord).count() == 2


@pytest.mark.parametrize("days", [0, 30, MIN_EVENT_RETENTION_DAYS - 1])
def test_workflow_retention_below_floor_is_refused(db, days):
    _decision(db, "rb-1", 400)
    db.commit()

    stats = purge_workflow(db, retention_days=days, dry_run=False, now=NOW)

    assert stats["deleted"] == 0
    assert stats["per_table"]["kg_remediation_decisions"]["skipped"]
    assert db.query(RemediationDecision).count() == 1


def test_purge_table_dry_run_reports_without_deleting(db):
    _decision(db, "rb-1", 400)
    _decision(db, "rb-2", 400)
    db.commit()

    stats = purge_table(db, table="kg_remediation_decisions", ts_column="created_at",
                        retention_days=180, min_days=MIN_EVENT_RETENTION_DAYS,
                        dry_run=True, now=NOW)

    assert stats["would_delete"] == 2 and stats["deleted"] == 0 and stats["dry_run"] is True
    assert db.query(RemediationDecision).count() == 2


# --- FK kg_remediation_events → kg_incidents --------------------------------


def _incident(db, key, resolved_days):
    inc = KGIncident(incident_key=key, namespace="squad-1-shared", service_name=key,
                     status="resolved", opened_at=_ago(resolved_days + 1),
                     last_alert_at=_ago(resolved_days + 1), resolved_at=_ago(resolved_days))
    db.add(inc)
    db.flush()
    return inc


def _event(db, incident, created_days, run_id):
    db.add(KGRemediationEvent(actor="squad-medic", run_id=run_id, namespace="squad-1-shared",
                              started_at=_ago(created_days), outcome="fixed",
                              incident_id=incident.id, created_at=_ago(created_days)))


def test_incident_still_referenced_by_event_is_skipped(db):
    """Событие записано позже закрытия инцидента (медик дописал прогон):
    инцидент пересёк границу первым и ждёт, пока уйдёт событие."""
    referenced = _incident(db, "ref", 366)
    _event(db, referenced, 364, "run-late")
    _incident(db, "free", 366)
    db.commit()

    stats = purge_events(db, now=NOW)

    assert "error" not in stats["per_table"]["kg_incidents"]
    assert stats["per_table"]["kg_incidents"]["deleted"] == 1
    assert [i.incident_key for i in db.query(KGIncident)] == ["ref"]
    assert db.query(KGRemediationEvent).count() == 1

    # Через двое суток событие тоже за границей: уходят оба, инцидент — следом.
    later = purge_events(db, now=NOW + timedelta(days=2))
    assert later["per_table"]["kg_remediation_events"]["deleted"] == 1
    assert later["per_table"]["kg_incidents"]["deleted"] == 1
    assert db.query(KGIncident).count() == 0


@pytest.fixture
def pg_session():
    import app.knowledge_graph.schema  # noqa: F401
    from app.database import SessionLocal, engine

    Base.metadata.create_all(engine)
    session = SessionLocal()

    def _cleanup():
        session.rollback()
        session.execute(text(
            "DELETE FROM kg_remediation_events WHERE actor = 'retention-fk-test'"))
        session.execute(text(
            "DELETE FROM kg_incidents WHERE namespace = 'retention-fk-test-ns'"))
        session.execute(text("DELETE FROM incidents WHERE incident_id LIKE 'rb-retention-test-%'"))
        session.execute(text("DELETE FROM incidents WHERE incident_id = 'rbx-retention-test'"))
        session.commit()

    _cleanup()
    try:
        yield session
    finally:
        _cleanup()
        session.close()


@requires_postgres
def test_event_fk_does_not_abort_incident_purge_on_postgres(pg_session):
    """На живом FK: без условия отбора DELETE батча падал бы ForeignKeyViolation
    и откатывал удаление ВСЕХ инцидентов батча."""
    db = pg_session
    # Далёкое прошлое — под границу попадают только строки теста.
    now = datetime(2003, 6, 1)

    def inc(key, resolved_days):
        row = KGIncident(incident_key=f"retention-fk-test/{key}", namespace="retention-fk-test-ns",
                         service_name=key, status="resolved",
                         opened_at=now - timedelta(days=resolved_days + 1),
                         last_alert_at=now - timedelta(days=resolved_days + 1),
                         resolved_at=now - timedelta(days=resolved_days))
        db.add(row)
        db.flush()
        return row

    referenced = inc("ref", 366)
    inc("free", 366)
    db.add(KGRemediationEvent(actor="retention-fk-test", run_id="r1",
                              namespace="retention-fk-test-ns",
                              started_at=now - timedelta(days=364), outcome="fixed",
                              incident_id=referenced.id,
                              created_at=now - timedelta(days=364)))
    db.commit()

    stats = purge_events(db, now=now)

    assert "error" not in stats["per_table"]["kg_incidents"], stats["per_table"]["kg_incidents"]
    assert stats["per_table"]["kg_incidents"]["deleted"] == 1
    left = [i.service_name for i in
            db.query(KGIncident).filter_by(namespace="retention-fk-test-ns")]
    assert left == ["ref"]


@requires_postgres
def test_bridge_filter_on_postgres(pg_session):
    """LIKE 'rb-%' в сыром SQL: знак % не должен съедаться драйвером."""
    db = pg_session
    now = datetime(2003, 6, 1)
    old = now - timedelta(days=400)
    db.add(IncidentRecord(incident_id="rb-retention-test-1", status="APPROVAL_PENDING", created_at=old))
    db.add(IncidentRecord(incident_id="rbx-retention-test", status="RESOLVED", created_at=old))
    db.commit()

    dry = purge_workflow(db, retention_days=180, now=now)
    assert dry["per_table"]["incidents"]["would_delete"] == 1

    stats = purge_workflow(db, retention_days=180, dry_run=False, now=now)

    assert stats["per_table"]["incidents"]["deleted"] == 1
    left = [i.incident_id for i in db.query(IncidentRecord)
            .filter(IncidentRecord.incident_id.in_(["rb-retention-test-1", "rbx-retention-test"]))]
    assert left == ["rbx-retention-test"]
