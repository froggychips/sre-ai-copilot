"""MigrationFailedRule и DbPermissionRule: found / absent / unknown / молчание.

Формулировки — реальные строки postgres, golang-migrate, alembic и Orleans
ADO.NET-хранилища; имена сервисов, таблиц и ролей вымышлены.
"""
from __future__ import annotations

from app.diagnostics.engine import DiagnosticEngine
from app.diagnostics.facts import FactKind, Verdict
from app.diagnostics.rules import DEFAULT_RULES
from app.diagnostics.rules.db_permission import DbPermissionRule
from app.diagnostics.rules.migration_failed import MigrationFailedRule

MIG = MigrationFailedRule()
DBP = DbPermissionRule()


def _ctx(**kw):
    base = {"namespace": "squad-alpha", "service": "bravo-service", "alertname": "KubeDeploymentGenerationMismatch"}
    base.update(kw)
    return base


def _one(rule, ctx):
    facts = rule.run(ctx)
    assert len(facts) == 1, facts
    return facts[0]


# ── MigrationFailedRule ─────────────────────────────────────────────────


def test_golang_migrate_dirty_version_found():
    f = _one(MIG, _ctx(logs_summary="error: Dirty database version 16. Fix and force version."))
    assert f.kind == FactKind.MIGRATION_FAILED
    assert f.verdict == Verdict.FOUND.value
    assert f.confidence == 0.95
    assert f.evidence["dirty"] is True and f.evidence["version"] == "16"
    assert "dirty" in f.evidence["signals"]


def test_schema_migrations_dirty_flag_found():
    f = _one(MIG, _ctx(k8s_summary="schema_migrations: version=42 dirty=true"))
    assert f.verdict == Verdict.FOUND.value
    assert f.evidence["dirty"] is True
    assert "version" not in f.evidence


def test_failed_migrate_job_event_found():
    events = [{"reason": "BackoffLimitExceeded", "message": "Job has reached the specified backoff limit",
               "object": "bravo-migrate"}]
    # bravo-migrate ↔ bravo-service — один workload.
    f = _one(MIG, _ctx(k8s_events=events))
    assert f.evidence["signals"] == ["migrate_job"]
    assert f.evidence["job"] == "bravo-migrate"
    assert "related" not in f.evidence


def test_migrate_job_without_image_cross_refs_image_pull():
    events = [{"reason": "BackOff", "message": 'Back-off pulling image "registry/bravo-migrations:1.2"',
               "object": "bravo-migrate-x7k2p"},
              {"reason": "Failed", "message": "Error: ImagePullBackOff", "object": "bravo-migrate-x7k2p"}]
    f = _one(MIG, _ctx(k8s_events=events))
    assert f.evidence["related"] == "image_pull"


def test_image_pull_of_non_migrator_is_not_migration():
    events = [{"reason": "Failed", "message": "Error: ImagePullBackOff", "object": "bravo-service-5d9f-abcde"}]
    f = _one(MIG, _ctx(k8s_events=events))
    assert f.verdict == Verdict.ABSENT.value


def test_orleans_field_not_found_is_missing_migration():
    text = "Orleans.Storage.StorageException: Field not found in row: LastGatheringPushTimestamp"
    f = _one(MIG, _ctx(logs_summary=text))
    assert f.evidence["signals"] == ["orleans_column"]
    assert f.evidence["missing_fields"] == ["lastgatheringpushtimestamp"]
    assert f.confidence == 0.85


def test_missing_column_is_weak_without_deploy_and_stronger_after():
    text = 'ERROR: column p.binarydata does not exist at character 42'
    weak = _one(MIG, _ctx(logs_summary=text))
    strong = _one(MIG, _ctx(logs_summary=text, recent_deployments=[{"name": "bravo", "ts": "x"}]))
    assert weak.confidence < 0.6 < strong.confidence
    assert weak.evidence["after_recent_deploy"] is False
    assert strong.evidence["missing_objects"] == ["column p.binarydata"]


def test_alembic_error_found():
    f = _one(MIG, _ctx(logs_summary="alembic.util.exc.CommandError: Can't locate revision identified by 'abc123'"))
    assert "migration_error" in f.evidence["signals"]


def test_several_signals_take_strongest():
    text = "migration failed: Dirty database version 7"
    f = _one(MIG, _ctx(logs_summary=text))
    assert set(f.evidence["signals"]) == {"dirty", "migration_error"}
    assert f.confidence == 0.95


def test_scanned_logs_without_signals_is_absent():
    f = _one(MIG, _ctx(logs_summary="GET /health 200 OK"))
    assert f.verdict == Verdict.ABSENT.value
    assert f.confidence == 0.6


def test_nothing_to_scan_emits_no_fact():
    assert MIG.run(_ctx()) == []


def test_failed_logs_source_turns_absent_into_unknown():
    ctx = _ctx(logs_summary="GET /health 200 OK", source_status={"logs_summary": "failed: OperationalError"})
    f = _one(MIG, ctx)
    assert f.verdict == Verdict.UNKNOWN.value


def test_found_survives_failed_neighbour_source():
    ctx = _ctx(logs_summary="Dirty database version 3", source_status={"k8s_events": "failed: timeout"})
    assert _one(MIG, ctx).verdict == Verdict.FOUND.value


# ── DbPermissionRule ───────────────────────────────────────────────────


def test_permission_denied_for_table_is_grants():
    text = 'ERROR: permission denied for table player_state (SQLSTATE 42501)'
    f = _one(DBP, _ctx(logs_summary=text))
    assert f.kind == FactKind.DB_PERMISSION
    assert f.evidence["subtype"] == "grants"
    assert f.evidence["objects"] == ["table player_state"]
    assert f.confidence == 0.9


def test_permission_denied_to_operation_and_owner():
    text = ('pq: permission denied to create extension "pg_trgm"\n'
            'ERROR: must be owner of table kingdom_events')
    f = _one(DBP, _ctx(logs_summary=text))
    assert f.evidence["operations"] == ["create"]
    assert f.evidence["objects"] == ["table kingdom_events"]


def test_sequence_and_schema_objects():
    text = 'permission denied for sequence events_id_seq; permission denied for schema public'
    f = _one(DBP, _ctx(k8s_summary=text))
    assert f.evidence["objects"] == ["schema public", "sequence events_id_seq"]


def test_role_missing_subtype():
    f = _one(DBP, _ctx(logs_summary='FATAL: role "charlie_rw" does not exist'))
    assert f.evidence["subtype"] == "role_missing"
    assert f.evidence["roles"] == ["charlie_rw"]


def test_password_auth_failed_is_auth_not_grants():
    f = _one(DBP, _ctx(logs_summary='FATAL: password authentication failed for user "delta_app"'))
    assert f.evidence["subtype"] == "auth"
    assert f.evidence["users"] == ["delta_app"]
    assert f.confidence == 0.85


def test_grants_outranks_auth_when_both():
    text = ('password authentication failed for user "delta_app"\n'
            'permission denied for table foo')
    f = _one(DBP, _ctx(logs_summary=text))
    assert f.evidence["subtype"] == "grants"
    assert f.evidence["subtypes"] == ["auth", "grants"]


def test_evidence_never_carries_raw_line():
    text = 'connect postgres://user:s3cret@db:5432/app: permission denied for table foo'
    f = _one(DBP, _ctx(logs_summary=text))
    assert "s3cret" not in repr(f.evidence)


def test_db_scanned_without_errors_is_absent_and_silent_without_material():
    assert _one(DBP, _ctx(logs_summary="ready")).verdict == Verdict.ABSENT.value
    assert DBP.run(_ctx()) == []


def test_db_failed_source_turns_absent_into_unknown():
    ctx = _ctx(logs_summary="ready", source_status={"logs_summary": "stale: 27ч без записей"})
    assert _one(DBP, ctx).verdict == Verdict.UNKNOWN.value


# ── Регистрация и путь до гипотез ───────────────────────────────────────


def test_rules_registered_and_kinds_are_anchorable():
    names = {r.name for r in DEFAULT_RULES}
    assert {"MigrationFailedRule", "DbPermissionRule"} <= names
    assert {FactKind.MIGRATION_FAILED, FactKind.DB_PERMISSION} <= FactKind.ALL


def test_facts_reach_hypothesis_prompt():
    """Факт попадает в <facts> и в <allowed_anchors> промпта perspective-агента."""
    from app.agents.multi_hypothesis import _prompt_for_perspective

    store = DiagnosticEngine().run(_ctx(logs_summary=(
        "Dirty database version 16. Fix and force version.\n"
        "ERROR: permission denied for table player_state")))
    assert store.has_observed(FactKind.MIGRATION_FAILED)
    assert store.has_observed(FactKind.DB_PERMISSION)
    user_context, _ = _prompt_for_perspective("app", "incident", store)
    assert "✓ migration_failed" in user_context
    assert "✓ db_permission" in user_context
    anchors = user_context.split("<allowed_anchors>")[1]
    assert "migration_failed" in anchors and "db_permission" in anchors


# ── Привязка к target (ревью #448, P1) ──────────────────────────────────


def test_namespace_wide_migration_is_soft_without_target():
    ctx = {"namespace": "squad-alpha", "alertname": "KubeDeploymentGenerationMismatch",
           "k8s_events": [{"reason": "BackoffLimitExceeded", "object": "charlie-migrate"}],
           "logs_summary": "Dirty database version 9"}
    f = _one(MIG, ctx)
    assert f.verdict == Verdict.FOUND.value
    assert f.confidence == 0.45
    assert f.evidence["attribution"] == "unverified"


def test_foreign_migrate_job_is_soft_with_known_target():
    events = [{"reason": "BackoffLimitExceeded", "object": "charlie-migrate"}]
    f = _one(MIG, _ctx(k8s_events=events))
    assert f.confidence == 0.45
    assert f.evidence["attribution"] == "foreign"


def test_scoped_text_signal_keeps_full_confidence_even_with_foreign_job():
    events = [{"reason": "BackoffLimitExceeded", "object": "charlie-migrate"}]
    f = _one(MIG, _ctx(k8s_events=events, logs_summary="Dirty database version 9"))
    assert f.confidence == 0.95
    assert "attribution" not in f.evidence


def test_db_permission_without_target_is_soft():
    ctx = {"namespace": "squad-alpha", "logs_summary": "permission denied for table foo"}
    f = _one(DBP, ctx)
    assert f.confidence == 0.45 and f.evidence["attribution"] == "unverified"


# ── Упавший источник без данных → явный ? (ревью #448, P2) ──────────────


def test_failed_sources_with_empty_fields_emit_unknown():
    ctx = _ctx(source_status={"logs_summary": "failed: timeout", "k8s_events": "failed: 403"})
    m = _one(MIG, ctx)
    d = _one(DBP, ctx)
    assert m.verdict == Verdict.UNKNOWN.value and "logs_summary" in m.unknown_reason
    assert d.verdict == Verdict.UNKNOWN.value
