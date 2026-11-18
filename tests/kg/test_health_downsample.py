"""Свёртка kg_service_health в часовые агрегаты.

Замер 18.11.2026: таблица — 2,2 ГБ из 3,8 ГБ базы, а сырьё старше семи
суток не читает никто. Под тестом два необратимых свойства: свёртка не
трогает окно baseline детектора аномалий, и точка не теряется и не
считается дважды — ни при обрыве прогона, ни при повторном запуске.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.health_downsample import (DEFAULT_RAW_DAYS,
                                                   downsample_service_health,
                                                   purge_hourly)
from app.knowledge_graph.health_retention import MIN_RETENTION_DAYS
from app.knowledge_graph.schema import (HEALTH_HOURLY_METRICS, Service,
                                        ServiceHealth, ServiceHealthHourly)
from tests.conftest import requires_postgres

NOW = datetime(2026, 11, 18, 12, 30)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(Service(id=1, namespace="prod-kingdom1", name="town", node_kind="service"))
    s.add(Service(id=2, namespace="prod-kingdom1", name="map", node_kind="service"))
    s.commit()
    return s


def _point(db, ts, service_id=1, **metrics):
    db.add(ServiceHealth(service_id=service_id, ts=ts, source="vm", **metrics))


def _hour(days_ago, hour=10):
    return (NOW - timedelta(days=days_ago)).replace(hour=hour, minute=0, second=0, microsecond=0)


def _run(db, **kw):
    kw.setdefault("dry_run", False)
    kw.setdefault("now", NOW)
    return downsample_service_health(db, **kw)


# --- что сворачивается ------------------------------------------------------


def test_old_hour_becomes_one_row_with_avg_max_and_count(db):
    base = _hour(10)
    for i, cpu in enumerate([10.0, 20.0, 60.0]):
        _point(db, base + timedelta(minutes=10 * i), cpu_pct=cpu, mem_pct=50.0)
    db.commit()

    stats = _run(db)

    assert stats["folded"] == 3 and stats["hourly_upserted"] == 1
    assert db.query(ServiceHealth).count() == 0, "сырьё свёрнутого часа удалено"
    row = db.query(ServiceHealthHourly).one()
    assert (row.service_id, row.hour, row.samples) == (1, base, 3)
    assert row.cpu_pct_avg == pytest.approx(30.0) and row.cpu_pct_max == 60.0
    assert row.mem_pct_avg == 50.0 and row.mem_pct_max == 50.0


def test_services_and_hours_are_kept_apart(db):
    base = _hour(10)
    _point(db, base, service_id=1, cpu_pct=1.0)
    _point(db, base + timedelta(minutes=10), service_id=2, cpu_pct=2.0)
    _point(db, base + timedelta(hours=1), service_id=1, cpu_pct=3.0)
    db.commit()

    _run(db)

    got = {(r.service_id, r.hour): r.cpu_pct_avg for r in db.query(ServiceHealthHourly)}
    assert got == {
        (1, base): 1.0, (2, base): 2.0, (1, base + timedelta(hours=1)): 3.0,
    }


def test_null_metric_stays_null_not_zero(db):
    """NULL у http_5xx/p95 — «источника нет», а не «ошибок нет»; в агрегате так же."""
    _point(db, _hour(10), cpu_pct=5.0)
    _point(db, _hour(10) + timedelta(minutes=10), cpu_pct=None)
    db.commit()

    _run(db)

    row = db.query(ServiceHealthHourly).one()
    assert row.samples == 2
    assert row.cpu_pct_avg == 5.0, "среднее — по не-NULL точкам"
    assert row.http_5xx_rate_avg is None and row.http_5xx_rate_max is None
    assert row.orleans_latency_avg_ms_avg is None


# --- что НЕ трогается -------------------------------------------------------


def test_baseline_window_is_never_touched(db):
    """Семь суток сырья — baseline детектора аномалий; свёртка в него не заходит."""
    for age in (0, 1, 3, 6):
        _point(db, _hour(age), cpu_pct=1.0)
    _point(db, NOW - timedelta(days=7) + timedelta(minutes=1), cpu_pct=1.0)
    db.commit()

    stats = _run(db)

    assert stats["folded"] == 0
    assert db.query(ServiceHealth).count() == 5
    assert db.query(ServiceHealthHourly).count() == 0


def test_cutoff_is_a_whole_hour(db):
    """Час на границе остаётся сырьём целиком: агрегат не дописывается потом."""
    edge = (NOW - timedelta(days=7)).replace(minute=0, second=0, microsecond=0)   # 12:00
    _point(db, edge + timedelta(minutes=5), cpu_pct=1.0)    # старше границы-времени,
    _point(db, edge + timedelta(minutes=45), cpu_pct=2.0)   # но в пограничном часе
    _point(db, edge - timedelta(minutes=5), cpu_pct=3.0)    # предыдущий час — уходит
    db.commit()

    stats = _run(db)

    assert stats["folded"] == 1 and stats["cutoff"] == edge.isoformat()
    assert db.query(ServiceHealth).count() == 2


@pytest.mark.parametrize("days", [0, 1, 6, -3])
def test_raw_retention_below_floor_is_refused(db, days):
    _point(db, _hour(40), cpu_pct=1.0)
    db.commit()

    stats = _run(db, raw_days=days)

    assert stats["folded"] == 0 and "минимума" in stats["skipped"]
    assert db.query(ServiceHealth).count() == 1
    assert db.query(ServiceHealthHourly).count() == 0


def test_default_raw_days_cover_every_raw_reader():
    """Дефолт не короче самого глубокого читателя сырья."""
    from app.knowledge_graph.anomaly_detection import BASELINE_DAYS
    from app.knowledge_graph.health_score import _METRIC_BASELINE_WINDOW_DAYS

    assert DEFAULT_RAW_DAYS >= MIN_RETENTION_DAYS >= BASELINE_DAYS
    assert DEFAULT_RAW_DAYS >= _METRIC_BASELINE_WINDOW_DAYS


def test_settings_defaults_are_off_and_safe():
    from app.config import Settings

    fields = Settings.model_fields
    assert fields["KG_HEALTH_DOWNSAMPLE_MODE"].default == "off"
    assert fields["KG_HEALTH_RAW_RETENTION_DAYS"].default >= MIN_RETENTION_DAYS
    assert (fields["KG_HEALTH_HOURLY_RETENTION_DAYS"].default
            > fields["KG_HEALTH_RAW_RETENTION_DAYS"].default)


# --- dry-run ----------------------------------------------------------------


def test_dry_run_counts_and_writes_nothing(db):
    for i in range(4):
        _point(db, _hour(10) + timedelta(minutes=10 * i), cpu_pct=1.0)
    _point(db, _hour(9), cpu_pct=1.0)
    _point(db, _hour(1), cpu_pct=1.0)
    db.commit()

    stats = downsample_service_health(db, now=NOW)      # dry_run — дефолт

    assert stats["dry_run"] is True
    assert stats["would_fold"] == 5 and stats["would_create_hourly"] == 2
    assert stats["folded"] == 0
    assert db.query(ServiceHealth).count() == 6
    assert db.query(ServiceHealthHourly).count() == 0


# --- идемпотентность и чанки ------------------------------------------------


def test_second_run_is_a_noop(db):
    for i in range(3):
        _point(db, _hour(10) + timedelta(minutes=10 * i), cpu_pct=float(i))
    db.commit()
    _run(db)
    before = [(r.samples, r.cpu_pct_avg, r.cpu_pct_max) for r in db.query(ServiceHealthHourly)]

    stats = _run(db)

    assert stats["folded"] == 0 and stats["chunks"] == 0
    after = [(r.samples, r.cpu_pct_avg, r.cpu_pct_max) for r in db.query(ServiceHealthHourly)]
    assert after == before, "повторный прогон не меняет агрегаты"


def test_run_is_capped_and_resumes_oldest_first(db):
    for age in (30, 20, 10):
        _point(db, _hour(age), cpu_pct=float(age))
    db.commit()

    first = _run(db, max_chunks=2)

    assert first["truncated"] is True and first["chunks"] == 2 and first["folded"] == 2
    left = db.query(ServiceHealth).one()
    assert left.ts == _hour(10), "самое старое ушло первым"

    second = _run(db, max_chunks=2)
    assert second["truncated"] is False and second["folded"] == 1
    assert db.query(ServiceHealth).count() == 0
    assert db.query(ServiceHealthHourly).count() == 3


def test_gap_in_data_does_not_burn_chunks(db):
    """Между точками 20 суток пустоты — это два окна, а не восемьдесят."""
    _point(db, _hour(30), cpu_pct=1.0)
    _point(db, _hour(10), cpu_pct=1.0)
    db.commit()

    stats = _run(db, max_chunks=2)

    assert stats["chunks"] == 2 and stats["folded"] == 2 and stats["truncated"] is False


def test_late_point_merges_into_existing_hour(db):
    """Точка, записанная в уже свёрнутый час, вливается, а не затирает агрегат."""
    base = _hour(10)
    _point(db, base, cpu_pct=10.0)
    _point(db, base + timedelta(minutes=10), cpu_pct=20.0)
    db.commit()
    _run(db)

    _point(db, base + timedelta(minutes=20), cpu_pct=60.0)
    db.commit()
    _run(db)

    row = db.query(ServiceHealthHourly).one()
    assert row.samples == 3
    assert row.cpu_pct_avg == pytest.approx(30.0), "среднее взвешено по числу точек"
    assert row.cpu_pct_max == 60.0
    assert row.mem_pct_avg is None


def test_failed_chunk_leaves_raw_and_aggregates_consistent(db, monkeypatch):
    """Обрыв между INSERT и DELETE откатывает оба: точка не теряется и не двоится."""
    from app.knowledge_graph import health_downsample as mod

    _point(db, _hour(10), cpu_pct=1.0)
    db.commit()

    real_execute = db.execute

    def boom(stmt, *a, **kw):
        if str(stmt).startswith("DELETE FROM kg_service_health"):
            raise RuntimeError("connection lost")
        return real_execute(stmt, *a, **kw)

    monkeypatch.setattr(db, "execute", boom)
    with pytest.raises(RuntimeError):
        mod.downsample_service_health(db, dry_run=False, now=NOW)
    monkeypatch.undo()

    assert db.query(ServiceHealth).count() == 1
    assert db.query(ServiceHealthHourly).count() == 0, "INSERT откатился вместе с DELETE"


# --- схема ------------------------------------------------------------------


def test_every_raw_metric_is_aggregated():
    """Новая метрика в сырье без пары колонок в агрегатах исчезала бы на 8-й день."""
    not_metrics = {"id", "service_id", "ts", "source"}
    raw = {c.name for c in ServiceHealth.__table__.columns} - not_metrics
    assert raw == set(HEALTH_HOURLY_METRICS)
    hourly = {c.name for c in ServiceHealthHourly.__table__.columns}
    for m in HEALTH_HOURLY_METRICS:
        assert {f"{m}_avg", f"{m}_max"} <= hourly


def test_migration_and_model_agree_on_columns():
    """Миграция держит свой литерал списка метрик — он обязан совпадать с моделью."""
    import importlib.util
    from pathlib import Path

    path = (Path(__file__).resolve().parents[2] / "alembic" / "versions"
            / "20261118_0100_kg_service_health_hourly.py")
    spec = importlib.util.spec_from_file_location("mig_hourly", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    assert tuple(mig._METRICS) == tuple(HEALTH_HOURLY_METRICS)


# --- ретеншен агрегатов -----------------------------------------------------


def _hourly(db, days_ago, service_id=1):
    db.add(ServiceHealthHourly(service_id=service_id, hour=_hour(days_ago), samples=6))


def test_hourly_older_than_retention_is_deleted(db):
    for age in (120, 95, 89, 10):
        _hourly(db, age)
    db.commit()

    stats = purge_hourly(db, retention_days=90, dry_run=False, now=NOW)

    assert stats["deleted"] == 2
    left = sorted((NOW - r.hour).days for r in db.query(ServiceHealthHourly))
    assert left == [10, 89]


def test_hourly_dry_run_deletes_nothing(db):
    _hourly(db, 120)
    db.commit()

    stats = purge_hourly(db, retention_days=90, now=NOW)

    assert stats["would_delete"] == 1 and stats["deleted"] == 0
    assert db.query(ServiceHealthHourly).count() == 1


def test_hourly_retention_shorter_than_raw_is_refused(db):
    """Агрегат не может жить меньше сырья — он удалялся бы раньше, чем появлялся."""
    _hourly(db, 120)
    db.commit()

    stats = purge_hourly(db, retention_days=10, raw_days=14, dry_run=False, now=NOW)

    assert stats["deleted"] == 0 and "минимума" in stats["skipped"]
    assert db.query(ServiceHealthHourly).count() == 1


# --- PostgreSQL: date_trunc и ON CONFLICT настоящего диалекта ---------------

# Далёкое прошлое: в общей CI-базе под границу попадают только строки теста.
PG_NOW = datetime(2001, 3, 1, 12, 30)


@pytest.fixture
def pg_session():
    import app.knowledge_graph.schema  # noqa: F401
    from app.database import SessionLocal, engine

    Base.metadata.create_all(engine)
    session = SessionLocal()
    svc = Service(namespace="downsample-test-ns", name="svc", node_kind="service")
    session.add(svc)
    session.commit()
    sid = svc.id
    try:
        yield session, sid
    finally:
        session.rollback()
        for table in ("kg_service_health", "kg_service_health_hourly"):
            session.execute(text(f"DELETE FROM {table} WHERE service_id = :s"), {"s": sid})
        session.execute(text("DELETE FROM kg_services WHERE id = :s"), {"s": sid})
        session.commit()
        session.close()


@requires_postgres
def test_fold_on_postgres_is_exact_and_idempotent(pg_session):
    db, sid = pg_session
    base = (PG_NOW - timedelta(days=10)).replace(minute=0, second=0, microsecond=0)
    for i, cpu in enumerate([10.0, 20.0, 60.0]):
        db.add(ServiceHealth(service_id=sid, ts=base + timedelta(minutes=10 * i),
                             cpu_pct=cpu, source="vm"))
    db.add(ServiceHealth(service_id=sid, ts=PG_NOW - timedelta(days=1), cpu_pct=1.0, source="vm"))
    db.commit()

    dry = downsample_service_health(db, now=PG_NOW)
    assert dry["would_fold"] == 3 and dry["would_create_hourly"] == 1

    stats = downsample_service_health(db, dry_run=False, now=PG_NOW)
    assert stats["folded"] == 3 and stats["hourly_upserted"] == 1
    again = downsample_service_health(db, dry_run=False, now=PG_NOW)
    assert again["folded"] == 0

    row = db.query(ServiceHealthHourly).filter_by(service_id=sid).one()
    assert (row.hour, row.samples) == (base, 3)
    assert row.cpu_pct_avg == pytest.approx(30.0) and row.cpu_pct_max == 60.0
    assert row.mem_pct_avg is None
    assert db.query(ServiceHealth).filter_by(service_id=sid).count() == 1, "свежая точка цела"

    # Поздняя точка в свёрнутый час — слияние через ON CONFLICT.
    db.add(ServiceHealth(service_id=sid, ts=base + timedelta(minutes=30), cpu_pct=110.0, source="vm"))
    db.commit()
    downsample_service_health(db, dry_run=False, now=PG_NOW)
    db.expire_all()
    row = db.query(ServiceHealthHourly).filter_by(service_id=sid).one()
    assert row.samples == 4 and row.cpu_pct_avg == pytest.approx(50.0) and row.cpu_pct_max == 110.0

    purged = purge_hourly(db, retention_days=7, raw_days=7, dry_run=False,
                          now=PG_NOW + timedelta(days=30))
    assert purged["deleted"] == 1
