"""Тесты для regression-фиксов stats_digest (live digest 25 мая 2026).

Покрытие двух багов которые НЕ покрыл PR #46 (MTTR/correlation/anomaly fix):

  1. **Topology growth first-run pill** — Redis snapshot empty после beat-pod
     restart → старая версия рисовала `+2909 services since yesterday`,
     потому что diff считался от 0. Fix: на first-run рисуем
     `(new baseline · counting starts now)` и сохраняем snapshot,
     следующий run даёт нормальный Δ.

  2. **Pipeline gauge via task.last_run** — старый gauge читал data-timestamp
     (max(ServiceHealth.ts)) и при VM-scrape-gap рисовал `vmsingle ⚠️ 2h gap`,
     хотя `kg_metrics_sync` ходил каждые 10 мин. Fix: two-tier — task.last_run
     из Redis heartbeat первичен (scheduled OK?), data lag — отдельный
     warning. См. `ref_wo_vm_scrape_gap` memory.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import stats_digest


# ── 1. Topology growth: first-run pill ─────────────────────────────────────


@pytest.mark.asyncio
async def test_topology_growth_first_run_renders_new_baseline_pill():
    """Redis empty → `(new baseline · counting starts now)`, snapshot записан.

    Защита от регрессии `+2909 services since yesterday`.
    """
    db = MagicMock()
    # _count_real_services → 2909, _count_edges → 1500
    db.execute.return_value.scalar.side_effect = [2909, 1500]
    db.execute.return_value.fetchall.return_value = []

    write_mock = AsyncMock()
    with patch.object(
        stats_digest, "_read_topology_snapshot",
        new=AsyncMock(return_value=None),
    ), patch.object(
        stats_digest, "_write_topology_snapshot",
        new=write_mock,
    ):
        text = await stats_digest.topology_growth_section(db)

    # First-run pill — НЕТ диффа, есть pill «new baseline».
    assert "new baseline" in text
    assert "counting starts now" in text
    # Δ-формат `+N since yesterday` отсутствует — главная регрессия.
    assert "+2909" not in text
    assert "since yesterday" not in text
    # Текущие counts всё-таки показаны (для контекста).
    assert "2909" in text
    assert "1500" in text
    # Snapshot записан — завтра нормальный Δ.
    write_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_topology_growth_with_snapshot_renders_normal_delta():
    """Snapshot есть → нормальный Δ (контрольный тест что fix не сломал happy path)."""
    db = MagicMock()
    db.execute.return_value.scalar.side_effect = [310, 1547]
    db.execute.return_value.fetchall.return_value = [("foo.bar",)]

    fake_prev = {
        "services": 300,
        "edges": 1500,
        "nats_subjects": [],
    }
    with patch.object(
        stats_digest, "_read_topology_snapshot",
        new=AsyncMock(return_value=fake_prev),
    ), patch.object(
        stats_digest, "_write_topology_snapshot",
        new=AsyncMock(),
    ):
        text = await stats_digest.topology_growth_section(db)

    assert "new baseline" not in text
    assert "`+10` services" in text
    assert "`+47` edges" in text


@pytest.mark.asyncio
async def test_topology_growth_empty_snapshot_dict_treated_as_first_run():
    """Snapshot {} (parse-error / unexpected shape) → first-run pill."""
    db = MagicMock()
    db.execute.return_value.scalar.side_effect = [100, 200]
    db.execute.return_value.fetchall.return_value = []
    with patch.object(
        stats_digest, "_read_topology_snapshot",
        new=AsyncMock(return_value={}),
    ), patch.object(
        stats_digest, "_write_topology_snapshot",
        new=AsyncMock(),
    ):
        text = await stats_digest.topology_growth_section(db)
    assert "new baseline" in text


def test_detect_first_run_helper():
    """`_detect_first_run` — symmetry-helper для других секций."""
    assert stats_digest._detect_first_run(None) is True
    assert stats_digest._detect_first_run({}) is True
    assert stats_digest._detect_first_run({"services": 100}) is False


# ── 2. Pipeline gauge: task.last_run (не data timestamp) ───────────────────


def _fake_check_result(per_task: dict):
    from app.knowledge_graph.self_health import CheckResult
    return CheckResult(name="sync_lag", status="ok", detail={"per_task": per_task})


def test_pipeline_gauge_task_healthy_when_last_run_within_interval():
    """task.last_run свежий → ✓, даже если data lag huge.

    Главная регрессия: kg_metrics_sync ходил каждые 10 мин, но
    max(ServiceHealth.ts) отставал 2h из-за VM scrape gap. Старая версия
    рисовала ⚠️ 2h gap. Fix: gauge показывает ✓ (task scheduled OK).
    """
    now = datetime.now(timezone.utc)
    db = MagicMock()
    # data lag 120 мин — больше stale_minutes=60, НО task.last_run свежий
    per_task = {
        "kg_metrics_sync": {
            "lag_minutes": 120.0, "last_ts": "2026-05-25T13:00:00",
            "expected_interval_minutes": 10, "status": "warn",
        },
    }
    with patch("app.knowledge_graph.self_health.check_sync_lag",
               return_value=_fake_check_result(per_task)), \
         patch.object(stats_digest, "_get_beat_last_run",
                      return_value=now - timedelta(minutes=3)):
        text = stats_digest.pipeline_health_section(db, stale_minutes=60)
    # Gauge ✓ — task running. Data lag попадает отдельным warning.
    assert "vmsingle ✓" in text
    assert "scheduled OK, data lag" in text
    # «2h gap» (старая ошибочная формулировка) — НЕТ в main gauge.
    assert "vmsingle ⚠️ 2h gap" not in text


def test_pipeline_gauge_task_stale_when_last_run_exceeds_interval():
    """task.last_run > expected*2 → ⚠️ N since last run."""
    now = datetime.now(timezone.utc)
    db = MagicMock()
    per_task = {
        "kg_metrics_sync": {
            "lag_minutes": 5.0, "last_ts": "2026-05-25T14:55:00",
            "expected_interval_minutes": 10, "status": "ok",
        },
    }
    with patch("app.knowledge_graph.self_health.check_sync_lag",
               return_value=_fake_check_result(per_task)), \
         patch.object(stats_digest, "_get_beat_last_run",
                      return_value=now - timedelta(minutes=45)):
        # 45 min > 10*2=20 → stale
        text = stats_digest.pipeline_health_section(db, stale_minutes=60)
    assert "vmsingle ⚠️" in text
    assert "since last run" in text


def test_pipeline_gauge_data_stale_but_task_fresh_renders_separate_warning():
    """Task healthy + data lag > stale_minutes → отдельная строка `data lag`.

    Точный смысл: «scheduled OK, data lag 2h» — это разделение «инфра жива,
    но source данных молчит» (= ref_wo_vm_scrape_gap).
    """
    now = datetime.now(timezone.utc)
    db = MagicMock()
    per_task = {
        "kg_metrics_sync": {
            "lag_minutes": 130.0, "last_ts": "2026-05-25T12:50:00",
            "expected_interval_minutes": 10, "status": "warn",
        },
    }
    with patch("app.knowledge_graph.self_health.check_sync_lag",
               return_value=_fake_check_result(per_task)), \
         patch.object(stats_digest, "_get_beat_last_run",
                      return_value=now - timedelta(minutes=5)):
        text = stats_digest.pipeline_health_section(db, stale_minutes=60)
    assert "vmsingle ✓" in text
    # Отдельный мессадж — italic block ниже main gauge.
    assert "data lag" in text
    assert "scheduled OK" in text


def test_pipeline_gauge_falls_back_to_data_timestamp_when_no_heartbeat():
    """Heartbeat не записан (fresh deploy) → fallback на data ts.

    Не падаем тихо — продолжаем рисовать gauge.
    """
    db = MagicMock()
    per_task = {
        "kg_metrics_sync": {
            "lag_minutes": 5.0, "last_ts": "2026-05-25T14:55:00",
            "expected_interval_minutes": 10, "status": "ok",
        },
        "kg_seq_logs_sync": {
            "lag_minutes": 130.0, "last_ts": "2026-05-25T12:50:00",
            "expected_interval_minutes": 10, "status": "warn",
        },
    }
    with patch("app.knowledge_graph.self_health.check_sync_lag",
               return_value=_fake_check_result(per_task)), \
         patch.object(stats_digest, "_get_beat_last_run",
                      return_value=None):
        text = stats_digest.pipeline_health_section(db, stale_minutes=60)
    assert "vmsingle ✓" in text       # data lag 5m < 60m → ✓
    assert "seq ⚠️" in text            # 130 min → ⚠️
    assert "gap" in text


def test_pipeline_gauge_handles_naive_datetime_heartbeat():
    """Защита от naive datetime (старый Redis-формат): не падает."""
    db = MagicMock()
    per_task = {
        "kg_metrics_sync": {
            "lag_minutes": 5.0, "last_ts": "2026-05-25T14:55:00",
            "expected_interval_minutes": 10, "status": "ok",
        },
    }
    # naive datetime, считается UTC.
    naive_recent = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=3)
    with patch("app.knowledge_graph.self_health.check_sync_lag",
               return_value=_fake_check_result(per_task)), \
         patch.object(stats_digest, "_get_beat_last_run",
                      return_value=naive_recent):
        text = stats_digest.pipeline_health_section(db, stale_minutes=60)
    assert "vmsingle ✓" in text


# ── Heartbeat helpers ──────────────────────────────────────────────────────


def test_record_and_get_beat_last_run_round_trip():
    """`_record_task_heartbeat` ставит ключ, `_get_beat_last_run` его читает.

    Используем фейковый redis-клиент: мокаем модуль `redis` целиком.
    """
    storage: dict = {}

    class _FakeRedis:
        @classmethod
        def from_url(cls, url, decode_responses=True):
            return cls()

        def set(self, key, value, ex=None):
            storage[key] = value

        def get(self, key):
            return storage.get(key)

    with patch.dict("sys.modules", {"redis": MagicMock(Redis=_FakeRedis)}):
        ts = datetime(2026, 5, 25, 14, 30, tzinfo=timezone.utc)
        stats_digest._record_task_heartbeat("kg_metrics_sync", ts=ts)
        read_back = stats_digest._get_beat_last_run("kg_metrics_sync")
    assert read_back == ts


def test_get_beat_last_run_returns_none_when_key_missing():
    class _FakeRedis:
        @classmethod
        def from_url(cls, url, decode_responses=True):
            return cls()

        def get(self, key):
            return None

    with patch.dict("sys.modules", {"redis": MagicMock(Redis=_FakeRedis)}):
        assert stats_digest._get_beat_last_run("kg_metrics_sync") is None


def test_record_task_heartbeat_fail_open_on_redis_error():
    """Redis down → warning в лог, exception НЕ пробрасывается.

    Это гарантия что celery task_postrun сигнал не уронит сам task.
    """
    class _BrokenRedis:
        @classmethod
        def from_url(cls, url, decode_responses=True):
            raise RuntimeError("Redis unreachable")

    with patch.dict("sys.modules", {"redis": MagicMock(Redis=_BrokenRedis)}):
        # Не должно бросить.
        stats_digest._record_task_heartbeat("kg_metrics_sync")
