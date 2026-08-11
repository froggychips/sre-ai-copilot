"""Тесты KG Coverage #1: k8s Job + CronJob sync.

Покрытие:
- pure helpers: _parse_k8s_time, _extract_job_status, _extract_cronjob_status,
  _extract_pod_template_labels, _resolve_owner_service_name
- sync_all_jobs: upsert + exit-code resolve у failed
- sync_all_cronjobs: upsert + runs_as_job через owner_service_id в metadata
- _link_jobs_to_cronjob_owners: transitive linkage Job → CronJob → Service
- idempotency: повторный sync не плодит дублей, обновляет counters
- kubectl failure → пустой результат, не raise
- skip-кейсы: нет owner label, нет matching Service в KG
"""
from datetime import datetime
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.k8s_jobs_sync import (
    _extract_cronjob_status, _extract_job_status,
    _extract_pod_template_labels, _kubectl_get_all, _parse_k8s_time,
    _resolve_owner, _resolve_owner_service_name,
    _resolve_owner_via_name_pattern, _strip_name_suffix, _upsert_k8s_job,
    sync_all_cronjobs, sync_all_jobs, sync_k8s_jobs,
)
from app.knowledge_graph.populator import upsert_service
from app.knowledge_graph.schema import K8sJob, Service  # noqa: F401


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _mk_job(
    name: str,
    namespace: str,
    *,
    succeeded: int = 0,
    failed: int = 0,
    active: int = 0,
    completion_time=None,
    start_time=None,
    labels=None,
    pod_labels=None,
    owner_refs=None,
):
    return {
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": labels or {},
            "ownerReferences": owner_refs or [],
        },
        "spec": {
            "template": {
                "metadata": {"labels": pod_labels or {}},
                "spec": {"containers": [{}]},
            },
        },
        "status": {
            "succeeded": succeeded,
            "failed": failed,
            "active": active,
            "startTime": start_time,
            "completionTime": completion_time,
        },
    }


def _mk_cronjob(
    name: str,
    namespace: str,
    *,
    schedule: str = "0 */1 * * *",
    suspend: bool = False,
    last_schedule: str = None,
    last_success: str = None,
    labels=None,
    pod_labels=None,
    active=None,
):
    return {
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": labels or {},
        },
        "spec": {
            "schedule": schedule,
            "suspend": suspend,
            "concurrencyPolicy": "Forbid",
            "jobTemplate": {
                "spec": {
                    "template": {
                        "metadata": {"labels": pod_labels or {}},
                        "spec": {"containers": [{}]},
                    },
                },
            },
        },
        "status": {
            "lastScheduleTime": last_schedule,
            "lastSuccessfulTime": last_success,
            "active": active or [],
        },
    }


# ── pure helpers ────────────────────────────────────────────────────────────


def test_parse_k8s_time_naive_utc():
    ts = _parse_k8s_time("2026-05-24T10:30:00Z")
    assert ts == datetime(2026, 5, 24, 10, 30, 0)
    assert ts.tzinfo is None


def test_parse_k8s_time_none_and_invalid():
    assert _parse_k8s_time(None) is None
    assert _parse_k8s_time("") is None
    assert _parse_k8s_time("not-a-date") is None


def test_extract_job_status_succeeded():
    job = _mk_job(
        "migrate", "sre-ai", succeeded=1,
        completion_time="2026-05-24T10:00:00Z",
        start_time="2026-05-24T09:55:00Z",
    )
    s = _extract_job_status(job)
    assert s["succeeded_count"] == 1
    assert s["failed_count"] == 0
    assert s["active_count"] == 0
    assert s["completion_time"] == datetime(2026, 5, 24, 10, 0, 0)
    assert s["start_time"] == datetime(2026, 5, 24, 9, 55, 0)


def test_extract_job_status_failed_no_completion():
    job = _mk_job("migrate", "sre-ai", failed=3, start_time="2026-05-24T09:55:00Z")
    s = _extract_job_status(job)
    assert s["failed_count"] == 3
    assert s["completion_time"] is None


def test_extract_cronjob_status_suspended():
    cj = _mk_cronjob(
        "etcd-backup", "kube-system", schedule="0 * * * *", suspend=True,
        last_success="2026-05-23T22:00:00Z",
    )
    s = _extract_cronjob_status(cj)
    assert s["schedule"] == "0 * * * *"
    assert s["suspended"] is True
    assert s["last_successful_time"] == datetime(2026, 5, 23, 22, 0, 0)
    assert s["last_schedule_time"] is None


def test_extract_cronjob_status_active_count():
    cj = _mk_cronjob(
        "push-s3", "prod-shared",
        active=[{"name": "push-s3-1"}, {"name": "push-s3-2"}],
    )
    s = _extract_cronjob_status(cj)
    assert s["active_count"] == 2


def test_extract_pod_template_labels_job():
    job = _mk_job("m", "ns", pod_labels={"app": "migrator"})
    assert _extract_pod_template_labels(job, "job") == {"app": "migrator"}


def test_extract_pod_template_labels_cronjob():
    cj = _mk_cronjob("c", "ns", pod_labels={"app.kubernetes.io/part-of": "town"})
    assert _extract_pod_template_labels(cj, "cronjob") == {
        "app.kubernetes.io/part-of": "town",
    }


def test_resolve_owner_service_name_prefers_part_of():
    """part-of приоритетнее `app` (recommended k8s label)."""
    obj = {"app.kubernetes.io/part-of": "town", "app": "town-cron"}
    assert _resolve_owner_service_name(obj, {}) == "town"


def test_resolve_owner_service_name_pod_labels_fallback():
    """Если на Job/CronJob metadata нет — смотрим pod-template."""
    assert _resolve_owner_service_name({}, {"app": "auth"}) == "auth"


def test_resolve_owner_service_name_none():
    assert _resolve_owner_service_name({}, {}) is None
    assert _resolve_owner_service_name(
        {"unknown-key": "x"}, {"other": "y"},
    ) is None


# ── sync_all_jobs ───────────────────────────────────────────────────────────


def test_sync_all_jobs_creates_node(db):
    job = _mk_job(
        "alembic-migrate-1", "sre-ai",
        succeeded=1, completion_time="2026-05-24T10:00:00Z",
        labels={"app": "sre-ai"},
    )
    with patch(
        "app.knowledge_graph.k8s_jobs_sync._kubectl_get_all",
        return_value=[job],
    ):
        stats = sync_all_jobs(db)
    assert stats["jobs_fetched"] == 1
    assert stats["nodes_upserted"] == 1
    assert stats["exit_codes_resolved"] == 0  # success → no exit-code lookup

    node = db.query(K8sJob).one()
    assert node.kind == "job"
    assert node.namespace == "sre-ai"
    assert node.succeeded_count == 1
    assert node.failed_count == 0
    assert node.completion_time == datetime(2026, 5, 24, 10, 0, 0)
    assert node.owner_service_name == "sre-ai"


def test_sync_all_jobs_resolves_exit_code_on_failure(db):
    job = _mk_job("alembic-migrate-2", "sre-ai", failed=1)
    with patch(
        "app.knowledge_graph.k8s_jobs_sync._kubectl_get_all",
        return_value=[job],
    ), patch(
        "app.knowledge_graph.k8s_jobs_sync._kubectl_get_pod_exit_code",
        return_value=1,
    ):
        stats = sync_all_jobs(db)
    assert stats["exit_codes_resolved"] == 1
    node = db.query(K8sJob).one()
    assert node.failed_count == 1
    assert node.last_pod_exit_code == 1


def test_sync_all_jobs_idempotent_updates_counts(db):
    """Повторный sync с обновлёнными counters обновляет, не плодит."""
    job_v1 = _mk_job("j", "ns", active=1, succeeded=0)
    with patch(
        "app.knowledge_graph.k8s_jobs_sync._kubectl_get_all",
        return_value=[job_v1],
    ):
        sync_all_jobs(db)
    assert db.query(K8sJob).count() == 1

    job_v2 = _mk_job(
        "j", "ns", active=0, succeeded=1,
        completion_time="2026-05-24T11:00:00Z",
    )
    with patch(
        "app.knowledge_graph.k8s_jobs_sync._kubectl_get_all",
        return_value=[job_v2],
    ):
        sync_all_jobs(db)

    assert db.query(K8sJob).count() == 1
    node = db.query(K8sJob).one()
    assert node.succeeded_count == 1
    assert node.active_count == 0
    assert node.completion_time == datetime(2026, 5, 24, 11, 0, 0)


# ── sync_all_cronjobs + runs_as_job edge ────────────────────────────────────


def test_sync_all_cronjobs_creates_node_and_runs_as_job_edge(db):
    # Pre-create owner Service в KG.
    upsert_service(db, namespace="prod-shared", name="town")
    db.commit()

    cj = _mk_cronjob(
        "town-backup", "prod-shared",
        schedule="0 */6 * * *",
        last_success="2026-05-24T06:00:00Z",
        labels={"app.kubernetes.io/part-of": "town"},
    )
    with patch(
        "app.knowledge_graph.k8s_jobs_sync._kubectl_get_all",
        return_value=[cj],
    ):
        stats = sync_all_cronjobs(db)

    assert stats["cronjobs_fetched"] == 1
    assert stats["nodes_upserted"] == 1
    assert stats["edges_runs_as_job"] == 1
    assert stats["skipped_no_owner_label"] == 0
    assert stats["skipped_no_owner_match"] == 0

    node = db.query(K8sJob).one()
    assert node.kind == "cronjob"
    assert node.schedule == "0 */6 * * *"
    assert node.owner_service_name == "town"
    assert node.last_successful_time == datetime(2026, 5, 24, 6, 0, 0)
    town = db.query(Service).filter_by(name="town").one()
    assert node.metadata_json["owner_service_id"] == town.id


def test_sync_all_cronjobs_skip_no_owner_label(db):
    cj = _mk_cronjob("orphan-cron", "monitoring", schedule="0 * * * *")
    with patch(
        "app.knowledge_graph.k8s_jobs_sync._kubectl_get_all",
        return_value=[cj],
    ):
        stats = sync_all_cronjobs(db)
    assert stats["skipped_no_owner_label"] == 1
    assert stats["edges_runs_as_job"] == 0
    node = db.query(K8sJob).one()
    assert node.owner_service_name is None
    assert "owner_service_id" not in (node.metadata_json or {})


def test_sync_all_cronjobs_skip_no_owner_match(db):
    """Label есть, но в kg_services нет такого service — пропускаем edge."""
    cj = _mk_cronjob(
        "ghost-cron", "prod-shared",
        labels={"app": "nonexistent-service"},
    )
    with patch(
        "app.knowledge_graph.k8s_jobs_sync._kubectl_get_all",
        return_value=[cj],
    ):
        stats = sync_all_cronjobs(db)
    assert stats["skipped_no_owner_match"] == 1
    node = db.query(K8sJob).one()
    assert node.owner_service_name == "nonexistent-service"
    assert "owner_service_id" not in (node.metadata_json or {})


# ── transitive linkage Job → CronJob → Service ──────────────────────────────


def test_link_jobs_to_cronjob_owners_transitive(db):
    """Failed Job, созданный CronJob-ом, получает owner_service_id parent-а.

    Job-name намеренно НЕ соответствует name-pattern (`-unusual-suffix`),
    чтобы изолировать тест transitive-линкера от name-pattern fallback.
    """
    upsert_service(db, namespace="prod-shared", name="town")
    db.commit()

    cj = _mk_cronjob(
        "town-unusual-suffix", "prod-shared",
        labels={"app.kubernetes.io/part-of": "town"},
    )
    # Job без своих labels, без matching name-pattern, но с ownerReferences
    # на CronJob — линкуется только через transitive resolve.
    job = _mk_job(
        "town-unusual-suffix-1716542400", "prod-shared",
        failed=1,
        owner_refs=[{"kind": "CronJob", "name": "town-unusual-suffix"}],
    )
    with patch(
        "app.knowledge_graph.k8s_jobs_sync._kubectl_get_all",
        side_effect=[[cj], [job]],
    ), patch(
        "app.knowledge_graph.k8s_jobs_sync._kubectl_get_pod_exit_code",
        return_value=1,
    ):
        result = sync_k8s_jobs(db)

    assert result["transitive_linked"] == 1
    job_node = (
        db.query(K8sJob)
        .filter_by(name="town-unusual-suffix-1716542400", kind="job")
        .one()
    )
    town = db.query(Service).filter_by(name="town").one()
    assert job_node.metadata_json["owner_service_id"] == town.id


# ── orchestrator + kubectl failure ──────────────────────────────────────────


def test_sync_k8s_jobs_kubectl_failure_returns_empty(db):
    """kubectl падает → пустой результат, не raise."""
    with patch(
        "app.knowledge_graph.k8s_jobs_sync._kubectl_get_all",
        return_value=[],
    ):
        result = sync_k8s_jobs(db)
    assert result["cronjobs"]["cronjobs_fetched"] == 0
    assert result["jobs"]["jobs_fetched"] == 0
    assert result["transitive_linked"] == 0


def test_kubectl_get_all_handles_nonzero_rc():
    """Прямой тест wrapper-а: rc!=0 → [], не raise."""
    import subprocess as sp
    with patch.object(sp, "run") as m:
        m.return_value = sp.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="forbidden",
        )
        assert _kubectl_get_all("jobs") == []


def test_kubectl_get_all_handles_timeout():
    import subprocess as sp
    with patch.object(sp, "run", side_effect=sp.TimeoutExpired(cmd="kubectl", timeout=30)):
        assert _kubectl_get_all("jobs") == []


def test_upsert_k8s_job_unique_per_kind(db):
    """Тот же name в том же ns с разным kind — две записи."""
    _upsert_k8s_job(
        db, namespace="ns", name="shared-name", kind="job",
        fields={"succeeded_count": 1},
    )
    _upsert_k8s_job(
        db, namespace="ns", name="shared-name", kind="cronjob",
        fields={"schedule": "0 * * * *"},
    )
    db.commit()
    assert db.query(K8sJob).count() == 2


# ── name-pattern fallback resolver ──────────────────────────────────────────


@pytest.mark.parametrize("name,expected", [
    ("foo-backup", "foo"),
    ("foo-backup-20240101120000", "foo"),
    ("foo-backup-2024-01-01", "foo"),
    ("foo-backup-abc12", "foo"),
    ("auth-cron", "auth"),
    ("auth-cronjob", "auth"),
    ("billing-migration", "billing"),
    ("billing-migration-20240601", "billing"),
    ("billing-migration-2024-06-01", "billing"),
    ("alembic-migrate", "alembic"),
    ("alembic-migrate-1716542400", "alembic"),
    ("redis-init", "redis"),
    ("redis-init-job", "redis"),
    ("logs-cleanup", "logs"),
    ("logs-cleanup-20240101", "logs"),
    ("logs-cleanup-2024-01-01", "logs"),
    ("pg-restore", "pg"),
    ("pg-restore-from-snap", "pg"),
    ("elastic-reindex", "elastic"),
    ("elastic-reindex-20240105", "elastic"),
])
def test_strip_name_suffix_known_patterns(name, expected):
    assert _strip_name_suffix(name) == expected


@pytest.mark.parametrize("name", [
    "",
    "service-without-known-suffix",
    "random-thing",
    "-backup",                # patalogical: suffix без префикса
    "foo-backupcheck",        # не якорится: -backup в середине
    "foo-cronified",          # mid-substring, не суффикс
])
def test_strip_name_suffix_unknown_returns_none(name):
    assert _strip_name_suffix(name) is None


def test_resolve_owner_via_name_pattern_finds_service(db):
    """`foo-backup` без labels + Service `foo` в том же ns → matched."""
    upsert_service(db, namespace="prod-shared", name="foo")
    db.commit()
    result = _resolve_owner_via_name_pattern(db, "prod-shared", "foo-backup")
    assert result is not None
    name, svc_id = result
    assert name == "foo"
    svc = db.query(Service).filter_by(name="foo", namespace="prod-shared").one()
    assert svc_id == svc.id


def test_resolve_owner_via_name_pattern_namespace_isolated(db):
    """Cross-namespace matching запрещён: `foo` в dev-shared ≠ owner foo-backup в prod."""
    upsert_service(db, namespace="dev-shared", name="foo")
    db.commit()
    result = _resolve_owner_via_name_pattern(db, "prod-shared", "foo-backup")
    assert result is None


def test_resolve_owner_via_name_pattern_no_matching_service(db):
    """Strip даёт candidate, но в kg_services этого сервиса нет → None."""
    # Никакого `qux` сервиса не создаём.
    result = _resolve_owner_via_name_pattern(db, "prod-shared", "qux-migration")
    assert result is None


# ── _resolve_owner с priority ───────────────────────────────────────────────


def test_resolve_owner_label_priority_over_name_pattern(db):
    """Label `app.kubernetes.io/part-of` приоритетнее name-pattern fallback'а.

    Даже если name-pattern strip даст другой результат — label выигрывает.
    """
    upsert_service(db, namespace="prod-shared", name="canonical")
    upsert_service(db, namespace="prod-shared", name="other")
    db.commit()
    # name → `other-backup` strip → `other`. Но part-of label → `canonical`.
    owner, owner_id, via = _resolve_owner(
        db, namespace="prod-shared", name="other-backup",
        obj_labels={"app.kubernetes.io/part-of": "canonical"},
        pod_labels={},
    )
    assert owner == "canonical"
    # label-path не делает SELECT, owner_id остаётся None — оригинальный
    # pipeline догоняет резолв через kg_services-lookup ниже по стеку.
    assert owner_id is None
    assert via == "part-of_label"


def test_resolve_owner_app_label_priority_over_name_pattern(db):
    upsert_service(db, namespace="prod-shared", name="myapp")
    upsert_service(db, namespace="prod-shared", name="foo")
    db.commit()
    owner, owner_id, via = _resolve_owner(
        db, namespace="prod-shared", name="foo-cron",
        obj_labels={"app": "myapp"}, pod_labels={},
    )
    assert owner == "myapp"
    assert owner_id is None
    assert via == "app_label"


def test_resolve_owner_falls_back_to_name_pattern_without_labels(db):
    upsert_service(db, namespace="prod-shared", name="foo")
    db.commit()
    owner, owner_id, via = _resolve_owner(
        db, namespace="prod-shared", name="foo-backup",
        obj_labels={}, pod_labels={},
    )
    assert owner == "foo"
    assert owner_id is not None
    assert via == "name_pattern"


def test_resolve_owner_returns_none_when_no_signal(db):
    owner, owner_id, via = _resolve_owner(
        db, namespace="kube-system", name="cleanup-old-things",
        obj_labels={}, pod_labels={},
    )
    # `cleanup-old-things` → strip `-things`? нет такого suffix-а в нашем
    # списке. Возвращает None по всем веткам.
    assert owner is None
    assert owner_id is None
    assert via == "none"


def test_resolve_owner_name_pattern_skipped_when_no_kg_service_match(db):
    """Pattern даёт candidate, но в kg_services его нет → resolved_via='none'."""
    # `qux-migration-2024-01-01` → strip `-migration-2024-01-01` → `qux`.
    # Но Service `qux` отсутствует.
    owner, owner_id, via = _resolve_owner(
        db, namespace="prod-shared", name="qux-migration-2024-01-01",
        obj_labels={}, pod_labels={},
    )
    assert owner is None
    assert owner_id is None
    assert via == "none"


# ── end-to-end sync с name-pattern fallback ─────────────────────────────────


def test_sync_all_jobs_links_via_name_pattern_without_labels(db):
    """Job без labels, name=foo-backup, Service foo в ns → linked + provenance."""
    upsert_service(db, namespace="prod-shared", name="foo")
    db.commit()

    job = _mk_job("foo-backup", "prod-shared", succeeded=1)
    with patch(
        "app.knowledge_graph.k8s_jobs_sync._kubectl_get_all",
        return_value=[job],
    ):
        stats = sync_all_jobs(db)
    assert stats["linked_via_name_pattern"] == 1

    node = db.query(K8sJob).one()
    assert node.owner_service_name == "foo"
    foo = db.query(Service).filter_by(name="foo").one()
    assert node.metadata_json["owner_service_id"] == foo.id
    assert node.metadata_json["owner_resolved_via"] == "name_pattern"


def test_sync_all_jobs_migration_with_date_suffix_linked(db):
    """`qux-migration-2024-01-01` без labels → strip → линкуется к qux."""
    upsert_service(db, namespace="sre-ai", name="qux")
    db.commit()

    job = _mk_job("qux-migration-2024-01-01", "sre-ai", succeeded=1)
    with patch(
        "app.knowledge_graph.k8s_jobs_sync._kubectl_get_all",
        return_value=[job],
    ):
        stats = sync_all_jobs(db)
    assert stats["linked_via_name_pattern"] == 1

    node = db.query(K8sJob).one()
    assert node.owner_service_name == "qux"
    assert node.metadata_json["owner_resolved_via"] == "name_pattern"


def test_sync_all_jobs_no_matching_service_records_none_provenance(db):
    """`cleanup-old-things` в kube-system без matching kg_service → resolved_via=none."""
    job = _mk_job("cleanup-old-things", "kube-system", succeeded=1)
    with patch(
        "app.knowledge_graph.k8s_jobs_sync._kubectl_get_all",
        return_value=[job],
    ):
        stats = sync_all_jobs(db)
    assert stats["linked_via_name_pattern"] == 0

    node = db.query(K8sJob).one()
    assert node.owner_service_name is None
    assert node.metadata_json["owner_resolved_via"] == "none"
    assert "owner_service_id" not in node.metadata_json


def test_sync_all_cronjobs_links_via_name_pattern(db):
    """CronJob без labels + Service в ns → edge через name_pattern."""
    upsert_service(db, namespace="prod-shared", name="auth")
    db.commit()
    cj = _mk_cronjob("auth-cron", "prod-shared", schedule="0 * * * *")
    with patch(
        "app.knowledge_graph.k8s_jobs_sync._kubectl_get_all",
        return_value=[cj],
    ):
        stats = sync_all_cronjobs(db)
    assert stats["edges_runs_as_job"] == 1
    assert stats["linked_via_name_pattern"] == 1
    assert stats["skipped_no_owner_label"] == 0
    assert stats["skipped_no_owner_match"] == 0

    node = db.query(K8sJob).one()
    auth = db.query(Service).filter_by(name="auth").one()
    assert node.metadata_json["owner_service_id"] == auth.id
    assert node.metadata_json["owner_resolved_via"] == "name_pattern"


def test_sync_all_jobs_part_of_label_wins_over_pattern(db):
    """part-of label приоритетнее name-pattern, даже если оба сматчились бы."""
    upsert_service(db, namespace="prod-shared", name="canonical")
    upsert_service(db, namespace="prod-shared", name="foo")
    db.commit()

    job = _mk_job(
        "foo-backup", "prod-shared", succeeded=1,
        labels={"app.kubernetes.io/part-of": "canonical"},
    )
    with patch(
        "app.knowledge_graph.k8s_jobs_sync._kubectl_get_all",
        return_value=[job],
    ):
        stats = sync_all_jobs(db)
    # name_pattern path НЕ срабатывает — label выиграл.
    assert stats["linked_via_name_pattern"] == 0
    node = db.query(K8sJob).one()
    assert node.owner_service_name == "canonical"
    assert node.metadata_json["owner_resolved_via"] == "part-of_label"


# ── Ревью 2026-08-10: kubectl вне транзакции + O(1) запросов на линковку ────


def _select_spy(session):
    """Счётчик SELECT-ов на engine сессии. Возвращает (counter, detach)."""
    from sqlalchemy import event

    counter = {"n": 0}
    engine = session.get_bind()

    def _before(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            counter["n"] += 1

    event.listen(engine, "before_cursor_execute", _before)
    return counter, lambda: event.remove(engine, "before_cursor_execute", _before)


def test_exit_codes_fetched_before_transaction_opens(db):
    """Все kubectl-фетчи exit-кодов — ДО первого SAVEPOINT-а тика.

    Раньше `_kubectl_get_pod_exit_code` вызывался из `_sync_one_job`, то есть
    внутри открытой транзакции: волна failed-джоб (накат миграций) держала
    локи на kg_k8s_jobs всё время N внешних вызовов по 30с.
    """
    jobs = [
        _mk_job("m-1", "sre-ai", failed=1),
        _mk_job("m-2", "sre-ai", failed=2),
        _mk_job("ok", "sre-ai", succeeded=1),
    ]
    order = []

    real_begin_nested = db.begin_nested

    def spy_begin_nested():
        order.append("tx")
        return real_begin_nested()

    with patch(
        "app.knowledge_graph.k8s_jobs_sync._kubectl_get_all",
        return_value=jobs,
    ), patch(
        "app.knowledge_graph.k8s_jobs_sync._kubectl_get_pod_exit_code",
        side_effect=lambda ns, name: order.append(f"kubectl:{name}") or 137,
    ), patch.object(db, "begin_nested", spy_begin_nested):
        stats = sync_all_jobs(db)

    # SAVEPOINT на каждую джобу (+ вложенный на INSERT в _upsert_k8s_job).
    assert order.count("tx") >= 3
    kubectl_calls = [o for o in order if o.startswith("kubectl:")]
    assert kubectl_calls == ["kubectl:m-1", "kubectl:m-2"], (
        "exit-код должен фетчиться только у failed-джоб"
    )
    # Ключевое: ни одного kubectl ПОСЛЕ открытия первой транзакции.
    assert order.index("tx") > order.index("kubectl:m-2"), (
        f"kubectl уехал внутрь транзакции: {order}"
    )
    # Семантика счётчиков и записи сохранена.
    assert stats["exit_codes_resolved"] == 2
    assert stats["errors"] == 0
    assert {
        (n.name, n.last_pod_exit_code)
        for n in db.query(K8sJob).all()
    } == {("m-1", 137), ("m-2", 137), ("ok", None)}


def test_exit_code_prefetch_deduplicates_same_job(db):
    """Дубль (ns, name) в выдаче kubectl → один фетч exit-кода."""
    jobs = [_mk_job("dup", "ns", failed=1), _mk_job("dup", "ns", failed=1)]
    calls = []
    with patch(
        "app.knowledge_graph.k8s_jobs_sync._kubectl_get_all",
        return_value=jobs,
    ), patch(
        "app.knowledge_graph.k8s_jobs_sync._kubectl_get_pod_exit_code",
        side_effect=lambda ns, name: calls.append(name) or 1,
    ):
        stats = sync_all_jobs(db)
    assert calls == ["dup"]
    assert stats["exit_codes_resolved"] == 1


def test_broken_status_in_prefetch_does_not_kill_tick(db):
    """Битый status в префетче не роняет тик — джобу разберёт SAVEPOINT."""
    bad = _mk_job("bad", "ns")
    bad["status"] = {"failed": "not-a-number"}
    good = _mk_job("good", "ns", succeeded=1)
    with patch(
        "app.knowledge_graph.k8s_jobs_sync._kubectl_get_all",
        return_value=[bad, good],
    ), patch(
        "app.knowledge_graph.k8s_jobs_sync._kubectl_get_pod_exit_code",
        return_value=1,
    ):
        stats = sync_all_jobs(db)
    assert stats["errors"] == 1              # битая джоба откатана per-item
    assert stats["nodes_upserted"] == 1      # соседняя записана
    assert db.query(K8sJob).one().name == "good"


def test_sync_all_jobs_commits_in_batches(db, monkeypatch):
    """Транзакция дробится: не один commit на весь тик."""
    monkeypatch.setattr(
        "app.knowledge_graph.k8s_jobs_sync._COMMIT_BATCH", 2,
    )
    jobs = [_mk_job(f"j-{i}", "ns", succeeded=1) for i in range(5)]

    commits = {"n": 0}
    real_commit = db.commit

    def spy_commit():
        commits["n"] += 1
        return real_commit()

    with patch(
        "app.knowledge_graph.k8s_jobs_sync._kubectl_get_all",
        return_value=jobs,
    ), patch.object(db, "commit", spy_commit):
        sync_all_jobs(db)

    # 2 батчевых (после 2-й и 4-й джобы) + финальный.
    assert commits["n"] == 3
    assert db.query(K8sJob).count() == 5


def _prepare_cronjob_owned_jobs(db, n_jobs: int) -> None:
    """1 CronJob с owner_service_id + n_jobs дочерних Job-ов."""
    from app.knowledge_graph.k8s_jobs_sync import _upsert_k8s_job

    svc = upsert_service(db, namespace="prod-shared", name="town")
    db.flush()
    _upsert_k8s_job(
        db, namespace="prod-shared", name="town-unusual", kind="cronjob",
        fields={"schedule": "0 * * * *"},
        metadata={"owner_service_id": svc.id},
    )
    for i in range(n_jobs):
        _upsert_k8s_job(
            db, namespace="prod-shared", name=f"town-unusual-{i}", kind="job",
            fields={"failed_count": 1},
            metadata={
                "owner_references": [
                    {"kind": "CronJob", "name": "town-unusual"},
                ],
            },
        )
    db.commit()


def _link_selects_for(n_jobs: int) -> tuple:
    """(linked, кол-во SELECT-ов) для `_link_jobs_to_cronjob_owners` на n джоб."""
    from app.knowledge_graph.k8s_jobs_sync import _link_jobs_to_cronjob_owners

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    try:
        _prepare_cronjob_owned_jobs(session, n_jobs)
        counter, detach = _select_spy(session)
        try:
            linked = _link_jobs_to_cronjob_owners(session)
        finally:
            detach()
        return linked, counter["n"]
    finally:
        session.close()
        engine.dispose()


def test_link_jobs_to_cronjob_owners_is_constant_query_count():
    """O(1) запросов вместо N+1: SELECT-родитель больше не per-job.

    На сотнях джоб это были сотни SELECT-ов каждые 15 минут внутри одной
    транзакции.
    """
    linked_small, selects_small = _link_selects_for(3)
    linked_big, selects_big = _link_selects_for(30)

    assert linked_small == 3 and linked_big == 30
    # Два запроса: сами джобы + пачка родителей.
    assert selects_small == 2, f"неожиданный план запросов: {selects_small}"
    assert selects_big == selects_small, (
        f"число SELECT-ов растёт с числом джоб: {selects_small} → {selects_big}"
    )


def test_link_jobs_to_cronjob_owners_ignores_foreign_namespace(db):
    """Родитель ищется по паре (ns, name) — чужой ns не линкуется.

    IN×IN может принести лишние пары; выборка по точному ключу это ловит.
    """
    from app.knowledge_graph.k8s_jobs_sync import (
        _link_jobs_to_cronjob_owners, _upsert_k8s_job)

    svc = upsert_service(db, namespace="dev-shared", name="town")
    db.flush()
    # CronJob с owner-ом живёт в dev-shared…
    _upsert_k8s_job(
        db, namespace="dev-shared", name="town-unusual", kind="cronjob",
        fields={"schedule": "0 * * * *"},
        metadata={"owner_service_id": svc.id},
    )
    # …а CronJob того же имени в prod-shared owner-а не имеет.
    _upsert_k8s_job(
        db, namespace="prod-shared", name="town-unusual", kind="cronjob",
        fields={"schedule": "0 * * * *"}, metadata={},
    )
    _upsert_k8s_job(
        db, namespace="prod-shared", name="town-unusual-1", kind="job",
        fields={"failed_count": 1},
        metadata={
            "owner_references": [{"kind": "CronJob", "name": "town-unusual"}],
        },
    )
    db.commit()

    assert _link_jobs_to_cronjob_owners(db) == 0
    job_node = db.query(K8sJob).filter_by(kind="job").one()
    assert "owner_service_id" not in (job_node.metadata_json or {})
