"""Ретеншен kg_service_health.

Таблица росла с первого дня без всякой политики: 18.2 млн строк и 4.2 ГБ из
5.3 ГБ базы к 15.08.2026, при том что самый глубокий потребитель — baseline
детектора аномалий — смотрит на 7 дней.

Главное свойство под тестом не «удаляет», а «не удаляет лишнего»: ошибка в
cutoff необратима, per-table бэкапов у этой БД нет.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.health_retention import (DEFAULT_RETENTION_DAYS,
                                                  MIN_RETENTION_DAYS,
                                                  purge_old_health)
from app.knowledge_graph.schema import Service, ServiceHealth

NOW = datetime(2026, 8, 15, 12, 0)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(Service(id=1, namespace="prod-kingdom1", name="town", node_kind="service"))
    s.commit()
    return s


def _add_points(db, ages_days):
    """Точки заданного возраста. Минуты разводятся: у таблицы UNIQUE(service_id, ts)."""
    for i, age in enumerate(ages_days):
        db.add(ServiceHealth(
            service_id=1,
            ts=NOW - timedelta(days=age) - timedelta(minutes=i),
            cpu_pct=1.0,
        ))
    db.commit()


# --- что удаляется ---------------------------------------------------------


def test_old_points_are_deleted(db):
    _add_points(db, [40, 35, 31])
    stats = purge_old_health(db, retention_days=30, now=NOW)

    assert stats["deleted"] == 3
    assert db.query(ServiceHealth).count() == 0


def test_recent_points_survive(db):
    """Граница — по времени, и всё внутри окна обязано уцелеть."""
    _add_points(db, [0, 1, 6, 29])
    stats = purge_old_health(db, retention_days=30, now=NOW)

    assert stats["deleted"] == 0
    assert db.query(ServiceHealth).count() == 4


def test_only_points_past_cutoff_go(db):
    _add_points(db, [45, 31, 29, 2])
    purge_old_health(db, retention_days=30, now=NOW)

    survived = sorted(int((NOW - h.ts).days) for h in db.query(ServiceHealth).all())
    assert survived == [2, 29], "удалено ровно то, что старше границы"


def test_baseline_window_is_never_touched(db):
    """7 дней — окно baseline детектора аномалий; на дефолте оно неприкосновенно."""
    _add_points(db, [0, 3, 7])
    purge_old_health(db, retention_days=DEFAULT_RETENTION_DAYS, now=NOW)
    assert db.query(ServiceHealth).count() == 3


# --- защита от собственной ошибки -----------------------------------------


@pytest.mark.parametrize("days", [0, 1, 6, -5])
def test_retention_below_floor_is_refused(db, days):
    """Слишком короткий срок — почти наверняка опечатка, а удаление необратимо."""
    _add_points(db, [100, 50, 1])
    stats = purge_old_health(db, retention_days=days, now=NOW)

    assert stats["deleted"] == 0
    assert stats["skipped"] and "минимума" in stats["skipped"]
    assert db.query(ServiceHealth).count() == 3, "при отказе не трогаем ничего"


def test_floor_equals_anomaly_baseline():
    """Пол ретеншена завязан на baseline не случайно — если тот вырастет,
    этот тест напомнит поднять и пол."""
    from app.knowledge_graph.anomaly_detection import BASELINE_DAYS
    assert MIN_RETENTION_DAYS >= BASELINE_DAYS


def test_empty_table_is_a_noop(db):
    stats = purge_old_health(db, retention_days=30, now=NOW)
    assert stats["deleted"] == 0 and stats["batches"] == 1


# --- батчи ----------------------------------------------------------------


def test_deletion_is_batched(db):
    """Один DELETE на миллионы строк раздул бы WAL — режем на батчи."""
    _add_points(db, [40] * 25)
    stats = purge_old_health(db, retention_days=30, batch_size=10, now=NOW)

    assert stats["deleted"] == 25
    assert stats["batches"] == 3, "25 строк по 10 = 3 батча"


def test_run_is_capped_and_says_so(db):
    """Упёрлись в лимит прогона — это должно быть видно, а не выглядеть как «всё убрано»."""
    _add_points(db, [40] * 30)
    stats = purge_old_health(db, retention_days=30, batch_size=10, max_batches=2, now=NOW)

    assert stats["deleted"] == 20
    assert stats["truncated"] is True
    assert db.query(ServiceHealth).count() == 10, "остаток ждёт следующего тика"


def test_second_run_finishes_the_tail(db):
    """Хвост дочищается следующим прогоном — идемпотентность важнее скорости."""
    _add_points(db, [40] * 30)
    purge_old_health(db, retention_days=30, batch_size=10, max_batches=2, now=NOW)
    stats = purge_old_health(db, retention_days=30, batch_size=10, max_batches=2, now=NOW)

    assert stats["deleted"] == 10
    assert db.query(ServiceHealth).count() == 0


# --- политика распространяется на все наблюдательные таблицы ---------------
#
# Политику написали для kg_service_health и на ней же оставили, хотя рядом
# росли ещё пять таблиц той же природы. Замер 05.09.2026, строк старше 30
# дней: kg_anomaly_observations 908 148 (68% таблицы), kg_signal_aggregates
# 412 167, kg_ingress_observations 350 196, kg_log_observations 106 141,
# kg_cluster_observations 21 724.

def test_every_sampling_table_has_a_retention_target():
    """Наблюдательная таблица без срока хранения растёт вечно.

    Список сверяется явно: новая таблица сэмплов должна попасть в реестр
    осознанно, а не быть забытой до следующего разбора «почему база 6 ГБ».
    """
    from app.knowledge_graph.health_retention import RETENTION_TARGETS

    covered = {t for t, _, _ in RETENTION_TARGETS}
    assert covered == {
        "kg_service_health",
        "kg_anomaly_observations",
        "kg_signal_aggregates",
        "kg_ingress_observations",
        "kg_log_observations",
        "kg_cluster_observations",
    }


def test_event_tables_are_not_in_retention():
    """События со смыслом сроком жизни метрик не чистятся.

    Деплой полугодовой давности отвечает на вопрос «когда это сломалось»,
    и его срок — отдельное решение, а не побочный эффект уборки сэмплов.
    """
    from app.knowledge_graph.health_retention import RETENTION_TARGETS

    covered = {t for t, _, _ in RETENTION_TARGETS}
    for table in ("kg_deployments", "kg_alerts", "kg_pod_events",
                  "kg_k8s_jobs", "kg_services", "kg_service_edges"):
        assert table not in covered, f"{table} — события, а не сэмплы"


def test_unknown_table_is_refused(db):
    """Имя таблицы подставляется в SQL текстом — брать его снаружи нельзя."""
    from app.knowledge_graph.health_retention import purge_table

    with pytest.raises(ValueError, match="RETENTION_TARGETS"):
        purge_table(db, table="kg_services", ts_column="updated_at", now=NOW)


def test_column_must_match_the_registry(db):
    """Колонка тоже из реестра: по чужой колонке cutoff означал бы другое."""
    from app.knowledge_graph.health_retention import purge_table

    with pytest.raises(ValueError):
        purge_table(db, table="kg_service_health", ts_column="id", now=NOW)


def test_purge_observations_cleans_a_second_table(db):
    """Уборка доходит до соседних таблиц, а не только до метрик."""
    from app.knowledge_graph.health_retention import purge_observations
    from app.knowledge_graph.schema import AnomalyObservation

    _add_points(db, [40, 1])
    for i, age in enumerate([40, 35, 1]):
        db.add(AnomalyObservation(
            service_id=1, ts=NOW - timedelta(days=age) - timedelta(minutes=i),
            metric="cpu_pct", severity="warning",
        ))
    db.commit()

    stats = purge_observations(db, now=NOW)

    assert stats["per_table"]["kg_anomaly_observations"]["deleted"] == 2
    assert stats["per_table"]["kg_service_health"]["deleted"] == 1
    assert db.query(AnomalyObservation).count() == 1


def test_one_broken_table_does_not_stop_the_rest(db, monkeypatch):
    """Отказ на одной таблице не отменяет уборку остальных.

    Иначе одна кривая настройка останавливает политику целиком — а именно
    так эти таблицы и выросли.
    """
    from app.knowledge_graph import health_retention as hr

    _add_points(db, [40])
    real = hr.purge_table

    def boom(db_, *, table, **kw):
        if table == "kg_anomaly_observations":
            raise RuntimeError("нет такой колонки")
        return real(db_, table=table, **kw)

    monkeypatch.setattr(hr, "purge_table", boom)
    stats = hr.purge_observations(db, now=NOW)

    assert "error" in stats["per_table"]["kg_anomaly_observations"]
    assert stats["per_table"]["kg_service_health"]["deleted"] == 1


def test_override_still_respects_the_floor(db):
    """Настройка из конфига не отменяет пол ретеншена."""
    from app.knowledge_graph.health_retention import purge_observations

    _add_points(db, [40])
    stats = purge_observations(
        db, overrides={"kg_service_health": 1}, now=NOW,
    )

    assert stats["per_table"]["kg_service_health"]["deleted"] == 0
    assert stats["per_table"]["kg_service_health"]["skipped"]
    assert db.query(ServiceHealth).count() == 1
