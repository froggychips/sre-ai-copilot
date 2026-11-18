"""Beat-задачи уборки: режимы off / dry_run / apply и место в расписании.

Все три уборки удаляют данные, поэтому по умолчанию выключены, а опечатка в
настройке режима не должна ничего запускать.
"""
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.remediation.attempts  # noqa: F401 — регистрация kg_remediation_attempts
from app.database import Base
from app.remediation.models import RemediationDecision


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _decision(db, incident_id, days):
    db.add(RemediationDecision(incident_id=incident_id, idempotency_key=f"k-{incident_id}-{days}",
                               created_at=datetime.utcnow() - timedelta(days=days)))


def _no_lock():
    return patch("app.workers.task_lock._redis_client", return_value=None)


def test_new_tasks_are_noop_when_disabled(monkeypatch):
    from app.config import settings
    from app.workers import tasks

    monkeypatch.setattr(settings, "KG_HEALTH_DOWNSAMPLE_MODE", "off")
    monkeypatch.setattr(settings, "KG_GONE_NODES_TTL_MODE", "off")
    with _no_lock(), patch.object(tasks, "SessionLocal", side_effect=AssertionError("БД не нужна")):
        assert tasks.kg_health_downsample_task.run() == {"skipped": "disabled"}
        assert tasks.kg_gone_nodes_purge_task.run() == {"skipped": "disabled"}


def test_downsample_task_passes_dry_run_until_apply(db, monkeypatch):
    from app.config import settings
    from app.knowledge_graph import health_downsample
    from app.workers import tasks

    seen = []
    monkeypatch.setattr(health_downsample, "downsample_service_health",
                        lambda db_, **kw: seen.append(kw["dry_run"]) or {})
    monkeypatch.setattr(health_downsample, "purge_hourly",
                        lambda db_, **kw: seen.append(kw["dry_run"]) or {})
    with _no_lock(), patch.object(tasks, "SessionLocal", return_value=db):
        for mode in ("dry_run", "apply", "aply"):
            monkeypatch.setattr(settings, "KG_HEALTH_DOWNSAMPLE_MODE", mode)
            tasks.kg_health_downsample_task.run()

    assert seen == [True, True, False, False], "опечатка в режиме — задача не запускается"


def test_retention_task_runs_workflow_only_when_enabled(db, monkeypatch):
    from app.config import settings
    from app.workers import tasks

    _decision(db, "rb-1", 400)
    db.commit()
    with _no_lock(), patch.object(tasks, "SessionLocal", return_value=db):
        monkeypatch.setattr(settings, "KG_WORKFLOW_RETENTION_MODE", "off")
        assert "workflow" not in tasks.kg_health_retention_task.run()
        assert db.query(RemediationDecision).count() == 1

        monkeypatch.setattr(settings, "KG_WORKFLOW_RETENTION_MODE", "dry_run")
        res = tasks.kg_health_retention_task.run()
        assert res["workflow"]["would_delete"] == 1
        assert db.query(RemediationDecision).count() == 1

        monkeypatch.setattr(settings, "KG_WORKFLOW_RETENTION_MODE", "apply")
        res = tasks.kg_health_retention_task.run()
        assert res["workflow"]["deleted"] == 1
        assert db.query(RemediationDecision).count() == 0


def test_new_tasks_are_scheduled_before_and_after_retention():
    from app.workers.tasks import SYNC_QUEUE_TASKS, celery_app

    sched = celery_app.conf.beat_schedule
    down, keep, gone = (sched[k]["schedule"] for k in
                        ("kg-health-downsample", "kg-health-retention", "kg-gone-nodes-purge"))

    def minute_of_day(c):
        return min(c.hour) * 60 + min(c.minute)

    assert minute_of_day(down) < minute_of_day(keep) < minute_of_day(gone), (
        "свёртка — до ретеншена: иначе тот удалит несвёрнутое на 30-м дне"
    )
    assert {"kg_health_downsample", "kg_gone_nodes_purge"} <= SYNC_QUEUE_TASKS
