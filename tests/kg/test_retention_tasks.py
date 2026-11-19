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
        "три ночные уборки не делят одно окно: свёртка, ретеншен, gone-узлы"
    )
    assert {"kg_health_downsample", "kg_gone_nodes_purge"} <= SYNC_QUEUE_TASKS


# --- сырьё health: кто удаляет при включённой свёртке (ревью PR #567) -------


def _raw_point(db, days):
    from app.knowledge_graph.schema import Service, ServiceHealth

    if db.query(Service).count() == 0:
        db.add(Service(id=1, namespace="prod-a", name="town", node_kind="service"))
    db.add(ServiceHealth(service_id=1, ts=datetime.utcnow() - timedelta(days=days), cpu_pct=1.0))
    db.commit()


@pytest.mark.parametrize("mode,expected", [
    ("apply", {"kg_service_health": "owned_by_downsample"}),
    ("APPLY", {"kg_service_health": "owned_by_downsample"}),
    ("dry_run", {}), ("off", {}), ("", {}), ("aply", {}),
])
def test_raw_health_owner_by_mode(mode, expected):
    from app.knowledge_graph.health_retention import raw_health_owner

    assert raw_health_owner(mode) == expected


def test_retention_never_deletes_unfolded_raw_when_downsample_applies(db, monkeypatch):
    """KG_HEALTH_RAW_RETENTION_DAYS=45 при KG_HEALTH_RETENTION_DAYS=30: точка
    35-дневной давности ещё не свёрнута (свёртка возьмёт её на 45-й день), и
    срочная чистка не вправе удалить её по старому порогу."""
    from app.config import settings
    from app.knowledge_graph.schema import ServiceHealth, ServiceHealthHourly
    from app.workers import tasks

    _raw_point(db, 35)
    monkeypatch.setattr(settings, "KG_HEALTH_DOWNSAMPLE_MODE", "apply")
    monkeypatch.setattr(settings, "KG_HEALTH_RAW_RETENTION_DAYS", 45)
    monkeypatch.setattr(settings, "KG_HEALTH_RETENTION_DAYS", 30)
    with _no_lock(), patch.object(tasks, "SessionLocal", return_value=db):
        tasks.kg_health_downsample_task.run()
        res = tasks.kg_health_retention_task.run()

    assert res["per_table"]["kg_service_health"]["skipped"] == "owned_by_downsample"
    assert res["per_table"]["kg_service_health"]["deleted"] == 0
    assert db.query(ServiceHealth).count() == 1, "несвёрнутое сырьё цело"
    assert db.query(ServiceHealthHourly).count() == 0
    assert "kg_anomaly_observations" in res["per_table"], "остальные таблицы чистятся как раньше"
    assert res["per_table"]["kg_anomaly_observations"]["skipped"] is None


def test_lagging_downsample_does_not_lose_raw_to_retention(db, monkeypatch):
    """Свёртка отстала (или упала): точке 40 дней, порог чистки 30 — она ждёт свёртку."""
    from app.config import settings
    from app.knowledge_graph.schema import ServiceHealth, ServiceHealthHourly
    from app.workers import tasks

    _raw_point(db, 40)
    monkeypatch.setattr(settings, "KG_HEALTH_DOWNSAMPLE_MODE", "apply")
    with _no_lock(), patch.object(tasks, "SessionLocal", return_value=db):
        tasks.kg_health_retention_task.run()          # свёртка в эту ночь не отработала
        assert db.query(ServiceHealth).count() == 1
        tasks.kg_health_downsample_task.run()

    assert db.query(ServiceHealth).count() == 0
    assert db.query(ServiceHealthHourly).one().samples == 1, "точка дошла до агрегата"


@pytest.mark.parametrize("mode", ["off", "dry_run"])
def test_retention_of_raw_is_unchanged_without_apply(db, monkeypatch, mode):
    """off и dry_run свёртки ничего не удаляют — срочная чистка работает как раньше,
    иначе в dry_run таблица росла бы без ограничения."""
    from app.config import settings
    from app.knowledge_graph.schema import ServiceHealth, ServiceHealthHourly
    from app.workers import tasks

    _raw_point(db, 35)
    _raw_point(db, 10)
    monkeypatch.setattr(settings, "KG_HEALTH_DOWNSAMPLE_MODE", mode)
    monkeypatch.setattr(settings, "KG_HEALTH_RETENTION_DAYS", 30)
    with _no_lock(), patch.object(tasks, "SessionLocal", return_value=db):
        tasks.kg_health_downsample_task.run()
        res = tasks.kg_health_retention_task.run()

    assert res["per_table"]["kg_service_health"]["deleted"] == 1
    assert res["per_table"]["kg_service_health"]["skipped"] is None
    assert db.query(ServiceHealth).count() == 1, "10-дневная точка цела и в dry_run"
    assert db.query(ServiceHealthHourly).count() == 0
