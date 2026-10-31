"""Еженедельная сверка копилота с кластером (weekly_reconciliation).

Каждая строка — на sqlite с моками VM/Redis; недоступный источник даёт
«не измерено», а не ноль; формат укладывается в лимит Discord.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.context.vm_client import VMQueryError
from app.database import Base
from app.knowledge_graph.schema import AlertEvent, KGRemediationEvent, Service
from app.knowledge_graph.source_status import SourceStatus, status_of
from app.remediation.attempts import RemediationAttempt
from app.services import weekly_reconciliation as wr

NOW = datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)
IN_WINDOW = datetime(2026, 10, 30, 12, 0)
OLD = datetime(2026, 10, 1, 12, 0)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


class FakeVM:
    def __init__(self, pairs=None, backup_ts=None, fail=False):
        self.pairs = pairs or set()
        self.backup_ts = backup_ts
        self.fail = fail

    async def query_label_sets_strict(self, query, labels):
        if self.fail:
            raise VMQueryError("vm: ConnectError")
        return set(self.pairs)

    async def query_instant_by_labels_strict(self, query, labels):
        if self.fail:
            raise VMQueryError("vm: ConnectError")
        return {(): self.backup_ts} if self.backup_ts is not None else {}


class FakeRedis:
    def __init__(self, initial=None, fail=False):
        self.store = dict(initial or {})
        self.fail = fail
        self.ttl = None

    def get(self, key):
        if self.fail:
            raise ConnectionError("redis down")
        return self.store.get(key)

    def set(self, key, value, ex=None):
        if self.fail:
            raise ConnectionError("redis down")
        self.store[key] = value
        self.ttl = ex


def _svc(db, ns, name, synthetic=False):
    s = Service(namespace=ns, name=name, synthetic=synthetic)
    db.add(s)
    db.flush()
    return s


def _alert(db, fp, alertname, svc=None, severity="warning", fired=IN_WINDOW, raw=None):
    db.add(AlertEvent(
        service_id=svc.id if svc else None, alertname=alertname, severity=severity,
        fingerprint=fp, fired_at=fired, raw=raw,
    ))
    db.flush()


def _run(coro):
    return asyncio.run(coro)


# ── 1. Покрытие ──────────────────────────────────────────────────────────────

def test_coverage_counts_pairs_and_marks_bad_below_90(db):
    town = _svc(db, "prod-k5", "town-service")
    _alert(db, "a1", "HighLatency", town)
    _alert(db, "a2", "KubeNodeNotReady", None, raw={"namespace": "monitoring"})
    _alert(db, "a3", "Ancient", town, fired=OLD)  # вне окна — не считается
    vm = FakeVM(pairs={("HighLatency", "prod-k5"), ("KubeNodeNotReady", "monitoring"),
                       ("Ancient", "prod-k5"), ("Other", "squad-1")})
    line = _run(wr.coverage_line(db, vm, NOW))
    assert line.status is SourceStatus.SUCCESS
    assert line.value == 50.0
    assert line.bad
    assert "2 из 4" in line.text


def test_coverage_ok_at_full(db):
    town = _svc(db, "prod-k5", "town-service")
    _alert(db, "a1", "HighLatency", town)
    _alert(db, "a2", "NodeDiskFull", None, raw={"namespace": "monitoring"})
    vm = FakeVM(pairs={("HighLatency", "prod-k5"), ("NodeDiskFull", "monitoring")})
    line = _run(wr.coverage_line(db, vm, NOW))
    assert line.value == 100.0 and not line.bad


def test_coverage_does_not_credit_rows_without_namespace(db):
    """Одна строка без namespace не «покрывает» все пары с тем же именем —
    иначе прячется ровно усечение группы (живой замер: 1 строка → 112 пар)."""
    _alert(db, "a1", "CPUThrottlingHigh", None)
    vm = FakeVM(pairs={("CPUThrottlingHigh", f"squad-{i}") for i in range(10)})
    line = _run(wr.coverage_line(db, vm, NOW))
    assert line.value == 0.0 and line.bad
    assert "ещё 10 совпали только по имени" in line.text


def test_coverage_excludes_severity_none_on_kg_side(db):
    town = _svc(db, "prod-k5", "town-service")
    _alert(db, "a1", "Watchdog", town, severity="none")
    line = _run(wr.coverage_line(db, FakeVM(pairs={("Watchdog", "prod-k5")}), NOW))
    assert line.value == 0.0 and line.bad


def test_coverage_unmeasured_when_vm_down(db):
    line = _run(wr.coverage_line(db, FakeVM(fail=True), NOW))
    assert line.status is SourceStatus.UNAVAILABLE
    assert line.render().startswith("⚪") and "не измерено" in line.render()
    assert line.value is None


def test_coverage_unmeasured_without_vm_url_and_on_empty_answer(db):
    assert _run(wr.coverage_line(db, None, NOW)).status is SourceStatus.UNAVAILABLE
    assert _run(wr.coverage_line(db, FakeVM(pairs=set()), NOW)).status is SourceStatus.EMPTY


# ── 2. Рост таблиц ───────────────────────────────────────────────────────────

MB = 1024 * 1024


def _pg(db):
    """sqlite делает вид, что он PostgreSQL: размеры подменяются моком."""
    bind = MagicMock()
    bind.dialect.name = "postgresql"
    return patch.object(db, "get_bind", return_value=bind)


def test_table_growth_first_run_is_baseline(db):
    redis = FakeRedis()
    with _pg(db), patch.object(wr, "_read_table_sizes",
                               return_value=(100 * MB, {"kg_alerts": 50 * MB})):
        line = wr.table_growth_line(db, NOW, redis)
    assert line.status is SourceStatus.SUCCESS and not line.bad
    assert "база заведена" in line.text
    saved = json.loads(redis.store[wr.SIZE_SNAPSHOT_REDIS_KEY])
    assert saved["tables"] == {"kg_alerts": 50 * MB}
    assert redis.ttl == 30 * 24 * 3600


def test_table_growth_marks_bad_over_30_pct(db):
    prev = {"ts": (NOW - timedelta(days=7)).isoformat(), "db": 100 * MB,
            "tables": {"kg_deployments": 100 * MB, "kg_alerts": 50 * MB}}
    redis = FakeRedis({wr.SIZE_SNAPSHOT_REDIS_KEY: json.dumps(prev)})
    with _pg(db), patch.object(wr, "_read_table_sizes", return_value=(
            200 * MB, {"kg_deployments": 140 * MB, "kg_alerts": 51 * MB})):
        line = wr.table_growth_line(db, NOW, redis)
    assert line.bad
    assert "`kg_deployments` +40.0 МБ (+40%)" in line.text
    assert "за 7 дн." in line.text


def test_table_growth_ok_small_growth_and_tiny_tables_ignored(db):
    prev = {"ts": NOW.isoformat(), "tables": {"big": 100 * MB, "tiny": 8192}}
    redis = FakeRedis({wr.SIZE_SNAPSHOT_REDIS_KEY: json.dumps(prev)})
    with _pg(db), patch.object(wr, "_read_table_sizes",
                               return_value=(200 * MB, {"big": 110 * MB, "tiny": 65536})):
        line = wr.table_growth_line(db, NOW, redis)
    assert not line.bad


def test_table_growth_bad_when_db_over_5gb(db):
    with _pg(db), patch.object(wr, "_read_table_sizes",
                               return_value=(6 * 1024 ** 3, {"t": MB})):
        line = wr.table_growth_line(db, NOW, FakeRedis())
    assert line.bad


def test_table_growth_partial_when_redis_down(db):
    with _pg(db), patch.object(wr, "_read_table_sizes", return_value=(MB, {"t": MB})):
        line = wr.table_growth_line(db, NOW, FakeRedis(fail=True))
    assert line.status is SourceStatus.PARTIAL
    assert "рост не посчитан" in line.text


def test_table_growth_unmeasured_on_sqlite(db):
    line = wr.table_growth_line(db, NOW, FakeRedis())
    assert line.status is SourceStatus.UNAVAILABLE


# ── 3. Бэкап ─────────────────────────────────────────────────────────────────

def test_backup_ok_and_bad_by_age():
    fresh = _run(wr.backup_line(FakeVM(backup_ts=NOW.timestamp() - 2 * 3600), NOW))
    assert fresh.status is SourceStatus.SUCCESS and not fresh.bad
    stale = _run(wr.backup_line(FakeVM(backup_ts=NOW.timestamp() - 30 * 3600), NOW))
    assert stale.bad and "30.0 ч" in stale.text


def test_backup_unmeasured_when_metric_missing_or_vm_down():
    assert _run(wr.backup_line(FakeVM(), NOW)).status is SourceStatus.EMPTY
    assert _run(wr.backup_line(FakeVM(fail=True), NOW)).status is SourceStatus.UNAVAILABLE


# ── 4. Атрибуция ─────────────────────────────────────────────────────────────

def test_attribution_counts_no_service_and_exporters(db):
    town = _svc(db, "prod-k5", "town-service")
    ksm = _svc(db, "squad-1", "vm-kube-state-metrics")
    seq = _svc(db, "monitoring", "seq", synthetic=True)
    _alert(db, "a1", "X", town)
    _alert(db, "a2", "X", town)
    _alert(db, "a3", "X", ksm)
    _alert(db, "a4", "X", seq)
    _alert(db, "a5", "X", None)
    _alert(db, "a6", "X", None, fired=OLD)  # вне окна
    line = wr.attribution_line(db, NOW)
    assert line.value == 60.0 and line.bad
    assert "без сервиса 1, на экспортере/synthetic 2" in line.text


def test_attribution_ok_and_empty(db):
    assert wr.attribution_line(db, NOW).status is SourceStatus.EMPTY
    town = _svc(db, "prod-k5", "town-service")
    for i in range(4):
        _alert(db, f"a{i}", "X", town)
    _alert(db, "a9", "X", None)
    line = wr.attribution_line(db, NOW)
    assert line.value == 20.0 and not line.bad


# ── 5. Починки ───────────────────────────────────────────────────────────────

def test_remediation_zero_is_bad(db):
    db.add(KGRemediationEvent(actor="squad-medic", run_id="r1", namespace="squad-1-shared",
                              started_at=IN_WINDOW, outcome="unresolved", fixed=False))
    db.flush()
    line = wr.remediation_line(db, NOW)
    assert line.status is SourceStatus.SUCCESS and line.bad
    assert "из 1 прогонов" in line.text


def test_remediation_counts_verified_and_fixed(db):
    db.add(RemediationAttempt(incident_id="i1", signature="s", status="verified",
                              updated_at=IN_WINDOW, created_at=IN_WINDOW))
    db.add(RemediationAttempt(incident_id="i2", signature="s", status="verified",
                              updated_at=OLD, created_at=OLD))
    db.add(KGRemediationEvent(actor="squad-medic", run_id="r1", namespace="ns",
                              started_at=IN_WINDOW, outcome="fixed", fixed=True))
    db.flush()
    line = wr.remediation_line(db, NOW)
    assert line.value == 2.0 and not line.bad


# ── Формат и задача ──────────────────────────────────────────────────────────

def test_report_format_header_five_lines_and_limit(db):
    content, lines = _run(wr.build_report(db, vm=FakeVM(fail=True), redis_client=FakeRedis(), now=NOW))
    rows = content.split("\n")
    assert len(rows) == 6
    assert "2026-W45" in rows[0] and "26.10–02.11" in rows[0]
    assert len(content) <= 2000
    # VM лежит: две строки VM — «не измерено», а не ноль.
    assert rows[1].startswith("⚪") and rows[3].startswith("⚪")


def test_report_truncated_to_2000():
    long = wr.ReconLine("t", SourceStatus.SUCCESS, "x" * 3000)
    assert len(wr.format_report([long], NOW)) == 2000


def test_run_sends_to_stats_channel_and_marks_status(db):
    sent = {}

    async def fake_send(content):
        sent["content"] = content
        return True

    with patch.object(wr.settings, "VICTORIA_METRICS_URL", ""), \
         patch.object(wr, "_redis", return_value=FakeRedis()), \
         patch("app.services.discord_service.discord_service.send_stats_report",
               side_effect=fake_send):
        res = _run(wr.run_weekly_reconciliation(db))
    assert res["status"] == "sent"
    assert "Сверка копилота" in sent["content"]
    assert status_of(res) is SourceStatus.PARTIAL


def test_run_disabled_by_flag(db):
    with patch.object(wr.settings, "WEEKLY_RECONCILIATION_ENABLED", False):
        res = _run(wr.run_weekly_reconciliation(db))
    assert res["status"] == "skipped"
    assert status_of(res) is SourceStatus.UNAVAILABLE


def test_task_is_scheduled_on_default_queue():
    from app.workers.tasks import celery_app

    cfg = celery_app.conf.beat_schedule["weekly-reconciliation"]
    assert cfg["task"] == "weekly_reconciliation"
    assert "weekly_reconciliation" not in (celery_app.conf.task_routes or {})


def test_no_llm_imports():
    import inspect
    src = inspect.getsource(wr)
    for forbidden in ("llm_client", "app.llm", "anthropic", "agents"):
        assert forbidden not in src
