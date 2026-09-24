"""kg_remediation_attempts: claim = INSERT, жизненный цикл, совместимость с JSON.

Здесь настоящая БД (SQLite in-memory), а не mock-сессия: весь смысл таблицы —
в том, что гонку ловит UNIQUE самой базы, и проверить это на MagicMock
нельзя. Порядок проверок eligibility и их отказы — в tests/test_executor_apply.py.

Сценарии:
  - happy path: строка applied с результатом, JSON executor_applied тоже есть;
    повтор → already_applied по таблице, даже если JSON стёрт;
  - гонка: второй apply во время записи первого → apply_in_flight;
  - гонка мимо проверок (оба прошли до вставки) → конфликт UNIQUE на commit
    → apply_in_flight, второго write нет;
  - протухший claim в таблице → unknown, write НЕ выполняется, повтор тем же
    одобрением тоже отказан;
  - unknown + одобрение позже пометки → CAS unknown→claimed → applied;
  - legacy: executor_applied в JSON без строки → already_applied, строка не
    создаётся;
  - kubectl вернул ошибку → failed, повтор запрещён;
  - верификация пишет исход в строку попытки;
  - миграция up/down на SQLite и совпадение её колонок с моделью.
"""
from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.execution_dsl import ExecutionIntent
from app.database import Base, IncidentRecord
from app.knowledge_graph.schema import ActionApproval
from app.remediation import attempts as attempts_store
from app.remediation import verification as v
from app.remediation.attempts import RemediationAttempt
from app.remediation.verification import TargetSnapshot
from app.services import executor_apply
from app.services.intent_signature import compute_signature

INCIDENT = "inc-attempt-1"
NS = "squad-1"

INTENT = {
    "action": "restart_deployment",
    "resource_type": "deployment",
    "resource_name": "town-service",
    "namespace": NS,
    "params": {},
    "risk": "low",
}
SIG = compute_signature(ExecutionIntent.model_validate(INTENT))


def _naive_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture
def Session(monkeypatch):
    """Общий in-memory engine: apply_intent открывает свою сессию, тест — свою."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False,
                           expire_on_commit=False)
    monkeypatch.setattr(executor_apply, "SessionLocal", factory)
    # Кластер и брокер недоступны в тестах: снимок «неизвестен», uid графа
    # не записан, верификация не планируется.
    monkeypatch.setattr(
        executor_apply, "snapshot_target",
        lambda intent, **kw: TargetSnapshot.unavailable("test", intent),
    )
    monkeypatch.setattr(executor_apply, "expected_identity", lambda db, incident_id: None)
    monkeypatch.setattr(
        executor_apply, "schedule_verification",
        lambda incident_id, **kw: {"scheduled": False, "reason": "test"},
    )
    monkeypatch.setattr(executor_apply.audit_service, "log_event", lambda *a, **kw: None)
    yield factory
    engine.dispose()


def _seed(Session, analysis_extra=None, approval_decided_at=None) -> None:
    db = Session()
    analysis = {"execution_intent": INTENT, "executor_result": {"status": "dry_run_ok"}}
    analysis.update(analysis_extra or {})
    db.add(IncidentRecord(incident_id=INCIDENT, status="COMPLETED",
                          data={"namespace": NS}, analysis=analysis))
    db.add(ActionApproval(incident_id=INCIDENT, intent_signature=SIG, status="approved",
                          approved_by="tester",
                          decided_at=approval_decided_at or _naive_now()))
    db.commit()
    db.close()


class _Exec:
    """Заглушка k8s_service.execute_intent: считает реальные write."""

    def __init__(self, write_result=None, during_write=None):
        self.writes = 0
        self.write_result = write_result or {"success": True, "command": "kubectl rollout restart"}
        self.during_write = during_write

    def __call__(self, intent, dry_run=True, post_approval=False, **kw):
        if dry_run:
            return {"success": True, "command": "kubectl … --dry-run=server", "exit_code": 0}
        self.writes += 1
        if self.during_write is not None:
            self.during_write()
        return self.write_result


def _rows(Session):
    db = Session()
    try:
        return db.query(RemediationAttempt).all()
    finally:
        db.close()


def _analysis(Session):
    db = Session()
    try:
        return db.query(IncidentRecord).filter_by(incident_id=INCIDENT).one().analysis
    finally:
        db.close()


def test_happy_path_writes_applied_row_and_json(Session):
    _seed(Session)
    fake = _Exec()
    with patch.object(executor_apply.k8s_service, "execute_intent", fake):
        out = executor_apply.apply_intent(INCIDENT, "tester", SIG)
    assert out["ok"] is True and fake.writes == 1
    [row] = _rows(Session)
    assert row.status == attempts_store.STATUS_APPLIED
    assert row.signature == SIG and row.namespace == NS and row.action == "restart_deployment"
    assert row.applied_at is not None and row.claimed_at is not None
    assert row.result["result"]["command"] == "kubectl rollout restart"
    # dual-write: embed/timeline продолжают читать JSON
    analysis = _analysis(Session)
    assert analysis["executor_applied"]["result"]["success"] is True
    assert "executor_in_flight" not in analysis


def test_repeat_is_refused_by_table_even_if_json_marker_lost(Session):
    """Ровно тот класс бага, от которого таблица: analysis перезаписан, маркер
    executor_applied пропал — повторной записи в кластер всё равно нет."""
    _seed(Session)
    fake = _Exec()
    with patch.object(executor_apply.k8s_service, "execute_intent", fake):
        assert executor_apply.apply_intent(INCIDENT, "tester", SIG)["ok"] is True
        db = Session()
        rec = db.query(IncidentRecord).filter_by(incident_id=INCIDENT).one()
        rec.analysis = {"execution_intent": INTENT, "executor_result": {"status": "dry_run_ok"}}
        db.commit()
        db.close()
        out = executor_apply.apply_intent(INCIDENT, "tester", SIG)
    assert out == {"ok": False, "reason": "already_applied"}
    assert fake.writes == 1


def test_concurrent_apply_during_write_gets_in_flight(Session):
    _seed(Session)
    inner: dict = {}
    fake = _Exec(during_write=lambda: inner.setdefault(
        "out", executor_apply.apply_intent(INCIDENT, "racer", SIG)))
    with patch.object(executor_apply.k8s_service, "execute_intent", fake):
        out = executor_apply.apply_intent(INCIDENT, "tester", SIG)
    assert out["ok"] is True
    # JSON-claim проверяется раньше таблицы и отвечает тем же кодом; важно,
    # что второй write не состоялся.
    assert inner["out"]["reason"] == "apply_in_flight"
    assert fake.writes == 1


def test_race_past_checks_is_caught_by_unique_constraint(Session, monkeypatch):
    """Оба претендента прошли проверки до вставки (на SQLite FOR UPDATE нет):
    победителя определяет UNIQUE(incident_id, signature) на commit."""
    _seed(Session)
    db = Session()
    db.add(attempts_store.new_claim(INCIDENT, SIG, INTENT, "winner"))
    db.commit()
    db.close()
    # Проигравший «не видел» строку победителя — она появилась после его чтения.
    monkeypatch.setattr(executor_apply, "_load_attempt", lambda db, incident_id: None)
    fake = _Exec()
    with patch.object(executor_apply.k8s_service, "execute_intent", fake):
        out = executor_apply.apply_intent(INCIDENT, "loser", SIG)
    assert out == {"ok": False, "reason": "apply_in_flight"}
    assert fake.writes == 0
    [row] = _rows(Session)
    assert row.applied_by == "winner" and row.status == attempts_store.STATUS_CLAIMED
    # Откат затронул и JSON-claim проигравшего: чужой in-flight не повис.
    assert "executor_in_flight" not in _analysis(Session)


def test_stale_claim_goes_unknown_without_second_write(Session, monkeypatch):
    monkeypatch.setattr(executor_apply.settings, "EXECUTOR_IN_FLIGHT_TTL_SECONDS", 600,
                        raising=False)
    _seed(Session)
    db = Session()
    row = attempts_store.new_claim(INCIDENT, SIG, INTENT, "crashed-worker")
    row.claimed_at = _naive_now() - timedelta(hours=1)
    db.add(row)
    db.commit()
    db.close()
    fake = _Exec()
    with patch.object(executor_apply.k8s_service, "execute_intent", fake):
        out = executor_apply.apply_intent(INCIDENT, "tester", SIG)
        again = executor_apply.apply_intent(INCIDENT, "tester", SIG)
    assert out["reason"] == "cluster_state_unknown:manual_verify_then_reapprove"
    # То же одобрение старше пометки — само себя не разблокирует.
    assert again["reason"] == "cluster_state_unknown:manual_verify_then_reapprove"
    assert fake.writes == 0
    [row] = _rows(Session)
    assert row.status == attempts_store.STATUS_UNKNOWN and row.error == "stale_claim"
    assert _analysis(Session)["executor_state_unknown"]["resolution"] == "manual"


def test_fresh_claim_row_refuses_in_flight(Session):
    _seed(Session)
    db = Session()
    db.add(attempts_store.new_claim(INCIDENT, SIG, INTENT, "other-worker"))
    db.commit()
    db.close()
    fake = _Exec()
    with patch.object(executor_apply.k8s_service, "execute_intent", fake):
        out = executor_apply.apply_intent(INCIDENT, "tester", SIG)
    assert out["reason"] == "apply_in_flight" and fake.writes == 0


def test_unknown_reclaimed_by_approval_after_mark(Session):
    """Выход из unknown: человек проверил кластер и одобрил ПОСЛЕ пометки.
    JSON-пометку при этом могли потерять — момент берётся из строки."""
    _seed(Session, approval_decided_at=_naive_now())
    db = Session()
    row = attempts_store.new_claim(INCIDENT, SIG, INTENT, "crashed-worker")
    row.status = attempts_store.STATUS_UNKNOWN
    row.updated_at = _naive_now() - timedelta(minutes=10)
    db.add(row)
    db.commit()
    db.close()
    fake = _Exec()
    with patch.object(executor_apply.k8s_service, "execute_intent", fake):
        out = executor_apply.apply_intent(INCIDENT, "tester", SIG)
    assert out["ok"] is True and fake.writes == 1
    [row] = _rows(Session)
    assert row.status == attempts_store.STATUS_APPLIED and row.applied_by == "tester"


def test_unknown_row_for_other_intent_gets_new_attempt(Session):
    """Re-fire после пометки принёс другой intent: старая строка остаётся
    unknown со своим intent-ом, новая команда получает свою строку."""
    _seed(Session, approval_decided_at=_naive_now())
    old_intent = {**INTENT, "resource_name": "other-service"}
    old_sig = compute_signature(ExecutionIntent.model_validate(old_intent))
    db = Session()
    row = attempts_store.new_claim(INCIDENT, old_sig, old_intent, "crashed-worker")
    row.status = attempts_store.STATUS_UNKNOWN
    row.updated_at = _naive_now() - timedelta(minutes=10)
    db.add(row)
    db.commit()
    db.close()
    fake = _Exec()
    with patch.object(executor_apply.k8s_service, "execute_intent", fake):
        out = executor_apply.apply_intent(INCIDENT, "tester", SIG)
    assert out["ok"] is True and fake.writes == 1
    by_sig = {r.signature: r for r in _rows(Session)}
    assert by_sig[old_sig].status == attempts_store.STATUS_UNKNOWN
    assert by_sig[old_sig].resource_name == "other-service"
    assert by_sig[SIG].status == attempts_store.STATUS_APPLIED
    assert by_sig[SIG].resource_name == "town-service"


def test_unknown_row_without_later_approval_refused(Session):
    _seed(Session, approval_decided_at=_naive_now() - timedelta(minutes=10))
    db = Session()
    row = attempts_store.new_claim(INCIDENT, SIG, INTENT, "crashed-worker")
    row.status = attempts_store.STATUS_UNKNOWN
    row.updated_at = _naive_now()
    db.add(row)
    db.commit()
    db.close()
    fake = _Exec()
    with patch.object(executor_apply.k8s_service, "execute_intent", fake):
        out = executor_apply.apply_intent(INCIDENT, "tester", SIG)
    assert out["reason"] == "cluster_state_unknown:manual_verify_then_reapprove"
    assert fake.writes == 0


def test_legacy_json_applied_without_row_is_already_applied(Session):
    """Запись от версии до таблицы: executor_applied есть, строки нет."""
    _seed(Session, analysis_extra={"executor_applied": {"applied_at": "2026-09-20T10:00:00+00:00"}})
    fake = _Exec()
    with patch.object(executor_apply.k8s_service, "execute_intent", fake):
        out = executor_apply.apply_intent(INCIDENT, "tester", SIG)
    assert out == {"ok": False, "reason": "already_applied"}
    assert fake.writes == 0 and _rows(Session) == []


def test_failed_write_is_terminal(Session):
    _seed(Session)
    fake = _Exec(write_result={"success": False, "command": "kubectl …",
                               "stderr": "forbidden", "exit_code": 1})
    with patch.object(executor_apply.k8s_service, "execute_intent", fake):
        assert executor_apply.apply_intent(INCIDENT, "tester", SIG)["ok"] is True
        db = Session()
        rec = db.query(IncidentRecord).filter_by(incident_id=INCIDENT).one()
        analysis = dict(rec.analysis)
        analysis.pop("executor_applied")
        rec.analysis = analysis
        db.commit()
        db.close()
        again = executor_apply.apply_intent(INCIDENT, "tester", SIG)
    [row] = _rows(Session)
    assert row.status == attempts_store.STATUS_FAILED and row.error == "forbidden"
    assert again["reason"] == "already_applied" and fake.writes == 1


def _deploy_runner(stdout: str):
    class _Proc:
        returncode = 0
        stderr = ""

        def __init__(self):
            self.stdout = stdout

    return lambda argv, **kw: _Proc()


def test_verification_records_outcome_on_attempt(Session, monkeypatch):
    monkeypatch.setattr(v.settings, "REMEDIATION_VERIFY_DELAYS_SEC", "300,900", raising=False)
    _seed(Session)
    with patch.object(executor_apply.k8s_service, "execute_intent", _Exec()):
        assert executor_apply.apply_intent(INCIDENT, "tester", SIG)["ok"] is True
    import json
    deploy = json.dumps({
        "metadata": {"name": "town-service", "namespace": NS, "uid": "u1", "generation": 6},
        "spec": {"replicas": 2, "template": {"spec": {"containers": [{"image": "svc:2"}]}}},
        "status": {"observedGeneration": 6, "readyReplicas": 2},
    })
    with patch("app.services.audit_logger.audit_service.log_event"):
        out = v.verify_remediation(INCIDENT, 1, db_factory=Session,
                                   runner=_deploy_runner(deploy))
    assert out["outcome"] == "verified"
    [row] = _rows(Session)
    assert row.status == attempts_store.STATUS_VERIFIED
    assert row.verification["outcome"] == "verified" and row.verification["attempt"] == 1
    assert _analysis(Session)["executor_verification"]["outcome"] == "verified"


def test_verification_falls_back_to_row_when_json_lost(Session, monkeypatch):
    """analysis потерял executor_applied — проверка всё равно находит, что
    проверять, по строке попытки."""
    monkeypatch.setattr(v.settings, "REMEDIATION_VERIFY_DELAYS_SEC", "300,900", raising=False)
    _seed(Session)
    with patch.object(executor_apply.k8s_service, "execute_intent", _Exec()):
        assert executor_apply.apply_intent(INCIDENT, "tester", SIG)["ok"] is True
    db = Session()
    rec = db.query(IncidentRecord).filter_by(incident_id=INCIDENT).one()
    rec.analysis = {}
    db.commit()
    db.close()
    with patch("app.services.audit_logger.audit_service.log_event"):
        out = v.verify_remediation(INCIDENT, 1, db_factory=Session,
                                   runner=_deploy_runner("{}"))
    # снимок пуст → unknown, но не «nothing_applied»: попытка найдена
    assert out["outcome"] == "unknown" and out.get("reason") != "nothing_applied"
    [row] = _rows(Session)
    assert row.status == attempts_store.STATUS_APPLIED       # unknown статус не меняет
    assert row.verification["outcome"] == "unknown"


# ── миграция ──────────────────────────────────────────────────────────────

_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic" / "versions" / "20260924_0100_kg_remediation_attempts.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("m_20260924_0100", _MIGRATION)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_upgrade_downgrade_matches_model():
    mod = _load_migration()
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            mod.upgrade()
        insp = inspect(conn)
        assert "kg_remediation_attempts" in insp.get_table_names()
        cols = {c["name"] for c in insp.get_columns("kg_remediation_attempts")}
        assert cols == {c.name for c in RemediationAttempt.__table__.columns}
        uniques = {tuple(u["column_names"]) for u in insp.get_unique_constraints(
            "kg_remediation_attempts")}
        assert ("incident_id", "signature") in uniques
        idx = {i["name"] for i in insp.get_indexes("kg_remediation_attempts")}
        assert "ix_kg_remediation_attempts_status_updated" in idx
        with Operations.context(ctx):
            mod.downgrade()
        assert "kg_remediation_attempts" not in inspect(conn).get_table_names()
    engine.dispose()
