"""KG sync reliability-under-failure regressions.

Покрываем 4 фикса устойчивости KG-sync к сбоям:

  #1 SILENT-FAILURE: kubectl-fetch упал → `sync_topology` инкрементит errors
     и НЕ рапортует success-with-0-services (fetch отличим от empty-ns).
  #2 EDGE-DECAY DEADMAN: decay пропускается если были fetch-ошибки ИЛИ если
     удаление затронуло бы > EDGE_DECAY_MAX_DELETE_PCT% edges (зеркалит
     drift_cleanup threshold-abort). Guard `_edge_decay_should_skip` — чистая
     функция, тестируется в изоляции.
  #3 UPSERT RACES: _upsert_k8s_job / _upsert_volume / _upsert_volume_edge
     переживают IntegrityError от параллельного tick'а через begin_nested
     SAVEPOINT + re-query-победителя (не теряют весь tick).
  #4 SESSION POISONING: flush-ошибка в одном namespace откатывается
     savepoint'ом и не роняет соседние ns + терминальный commit.

Всё на in-memory SQLite (поддерживает SAVEPOINT; та же семантика, что у PG).
"""
from datetime import datetime, timedelta

import pytest
import sqlalchemy.orm as orm
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.knowledge_graph.kg_sync as kg_sync
from app.database import Base
from app.knowledge_graph.k8s_jobs_sync import _upsert_k8s_job
from app.knowledge_graph.k8s_storage_sync import _upsert_volume
from app.knowledge_graph.kg_sync import (
    EDGE_DECAY_MAX_DELETE_PCT, KubectlFetchError, _edge_decay_should_skip,
    sync_topology,
)
from app.knowledge_graph.populator import upsert_edge, upsert_service
from app.knowledge_graph.schema import K8sJob, Service, ServiceEdge, StorageVolume


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


# ── helpers ──────────────────────────────────────────────────────────────────


def _seed_edges(db, fresh: int, old_days_each) -> int:
    """Создать `fresh` свежих edges + по одному old-edge на каждый elem
    old_days_each (возраст в днях). Возвращает итоговое число edges.

    Все edges от общего src к отдельным dst — уникальные (src_id,dst_id,kind).
    """
    src = upsert_service(db, "squad-1", "src")
    n = 0
    for i in range(fresh):
        dst = upsert_service(db, "squad-1", f"fresh-{i}")
        upsert_edge(db, src, dst, kind="calls")
        n += 1
    for j, age in enumerate(old_days_each):
        dst = upsert_service(db, "squad-1", f"old-{j}")
        e = upsert_edge(db, src, dst, kind="calls")
        e.last_seen_at = datetime.utcnow() - timedelta(days=age)
        n += 1
    db.commit()
    return n


# ── #2 guard в изоляции ──────────────────────────────────────────────────────


def test_edge_decay_guard_skips_on_fetch_errors():
    skip, reason = _edge_decay_should_skip(
        total_edges=100, to_delete=1, has_fetch_errors=True,
    )
    assert skip is True
    assert reason == "fetch_errors"


def test_edge_decay_guard_skips_when_over_threshold():
    # 30/100 = 30% > 25% default → deadman.
    skip, reason = _edge_decay_should_skip(
        total_edges=100, to_delete=30, has_fetch_errors=False,
    )
    assert skip is True
    assert "delete_pct" in reason


def test_edge_decay_guard_allows_small_decay():
    # 5/100 = 5% < 25% → OK.
    skip, reason = _edge_decay_should_skip(
        total_edges=100, to_delete=5, has_fetch_errors=False,
    )
    assert skip is False
    assert reason == ""


def test_edge_decay_guard_allows_when_nothing_to_delete():
    skip, _ = _edge_decay_should_skip(
        total_edges=100, to_delete=0, has_fetch_errors=False,
    )
    assert skip is False


def test_edge_decay_guard_empty_graph_no_skip():
    skip, _ = _edge_decay_should_skip(
        total_edges=0, to_delete=0, has_fetch_errors=False,
    )
    assert skip is False


def test_edge_decay_guard_threshold_boundary_not_skipped():
    # Ровно на пороге (25%) — НЕ skip (skip только строго выше).
    skip, _ = _edge_decay_should_skip(
        total_edges=100, to_delete=25, has_fetch_errors=False,
        max_delete_pct=EDGE_DECAY_MAX_DELETE_PCT,
    )
    assert skip is False


# ── #1 fetch-failure → errors + (a) decay skipped ───────────────────────────


def test_kubectl_fetch_failure_increments_errors(db, monkeypatch):
    """kubectl-сбой → errors++, НЕ success-with-0-services, decay пропущен."""
    _seed_edges(db, fresh=0, old_days_each=[40])  # 1 старое ребро
    assert db.query(ServiceEdge).count() == 1

    def boom(ns):
        raise KubectlFetchError(f"kubectl down ns={ns}")

    monkeypatch.setattr(kg_sync, "_kubectl_get_deployments", boom)

    total = sync_topology(db, namespaces=["squad-1"])

    # Сломанный fetch посчитан как error, а не как пустой namespace.
    assert total["errors"] == 1
    assert total["services"] == 0
    assert total["namespaces"] == 0
    # Deadman: decay пропущен, старое ребро НЕ удалено (last_seen_at не
    # обновился из-за сбоя — удаление было бы ложным).
    assert total["edge_decay_skipped"] is True
    assert total["edges_deleted"] == 0
    assert db.query(ServiceEdge).count() == 1


def test_fetch_failure_does_not_wipe_other_namespaces_decay(db, monkeypatch):
    """Смешанный проход: один ns упал fetch'ем → errors>0 → decay всего
    прохода пропускается (даже для успешных ns)."""
    _seed_edges(db, fresh=3, old_days_each=[40, 40])  # 5 edges, 2 старых

    def flaky(ns):
        if ns == "squad-2":
            raise KubectlFetchError("kubectl down")
        return []  # успешный пустой fetch

    monkeypatch.setattr(kg_sync, "_kubectl_get_deployments", flaky)

    total = sync_topology(db, namespaces=["squad-1", "squad-2"])

    assert total["errors"] == 1
    assert total["namespaces"] == 1  # только squad-1 успешен
    assert total["edge_decay_skipped"] is True
    # Оба старых ребра целы — decay не сработал из-за fetch-ошибки.
    assert db.query(ServiceEdge).count() == 5


# ── #2 (b) decay skipped on threshold vs. runs below it ─────────────────────


def test_edge_decay_skipped_when_over_threshold(db, monkeypatch):
    """Без fetch-ошибок, но удаление > 25% всех edges → deadman → skip."""
    _seed_edges(db, fresh=6, old_days_each=[40, 40, 40, 40])  # 10 edges, 4 старых=40%
    monkeypatch.setattr(kg_sync, "_kubectl_get_deployments", lambda ns: [])

    total = sync_topology(db, namespaces=["squad-1"])

    assert total["errors"] == 0
    assert total["edge_decay_skipped"] is True
    assert total["edges_deleted"] == 0
    assert db.query(ServiceEdge).count() == 10  # ничего не удалено


def test_edge_decay_runs_below_threshold(db, monkeypatch):
    """Контроль: удаление ≤ 25% и нет fetch-ошибок → decay реально работает."""
    _seed_edges(db, fresh=9, old_days_each=[40])  # 10 edges, 1 старое = 10%
    monkeypatch.setattr(kg_sync, "_kubectl_get_deployments", lambda ns: [])

    total = sync_topology(db, namespaces=["squad-1"])

    assert total["errors"] == 0
    assert total["edge_decay_skipped"] is False
    assert total["edges_deleted"] == 1
    assert db.query(ServiceEdge).count() == 9  # старое ребро удалено


# ── #2 (c) kind/source-aware decay: сломанный источник не даёт стирать ──────
#
# Реальный инцидент: `kubectl get services -A` (42МБ) таймаутил каждый тик,
# k8s_topology_resources_sync возвращал [] и НЕ raise-ил → serves_traffic
# тихо старели, а decay в kg_sync (видевший только СВОИ fetch-ошибки)
# стирал целые классы топологии. Теперь ребро decay-ится только если его
# источник свежести освежал хоть что-то за окно KG_EDGE_SOURCE_FRESH_HOURS.


def _mk_edge(db, src, dst_name, kind, discovered_by, age_days):
    dst = upsert_service(db, "squad-1", dst_name)
    e = upsert_edge(db, src, dst, kind=kind, discovered_by=discovered_by)
    if age_days:
        e.last_seen_at = datetime.utcnow() - timedelta(days=age_days)
    return e


def test_decay_skips_kinds_whose_source_is_dead(db, monkeypatch):
    """Источник serves_traffic мёртв (ни одного свежего ребра) → его рёбра
    НЕ удаляются и НЕ помечаются inactive; здоровый kind decay-ится."""
    src = upsert_service(db, "squad-1", "src")
    # calls (kg_sync-семейство): 3 свежих + 1 старое (40д) → источник жив.
    for i in range(3):
        _mk_edge(db, src, f"c-fresh-{i}", "calls", "kg_sync/env_vars", 0)
    _mk_edge(db, src, "c-old", "calls", "kg_sync/env_vars", 40)
    # serves_traffic: ВСЕ рёбра старые → источник считается мёртвым.
    _mk_edge(db, src, "st-old", "serves_traffic",
             "k8s_topology_resources/service", 40)
    _mk_edge(db, src, "st-mid", "serves_traffic",
             "k8s_topology_resources/service", 10)
    db.commit()

    monkeypatch.setattr(kg_sync, "_kubectl_get_deployments", lambda ns: [])
    total = sync_topology(db, namespaces=["squad-1"])

    assert total["errors"] == 0
    assert total["edge_decay_skipped"] is False
    # Удалено только старое calls-ребро (его источник жив).
    assert total["edges_deleted"] == 1
    remaining = {
        (e.kind, e.dst.name) for e in db.query(ServiceEdge).all()
    }
    assert ("serves_traffic", "st-old") in remaining     # НЕ удалено
    assert ("serves_traffic", "st-mid") in remaining
    assert ("calls", "c-old") not in remaining            # удалено
    # st-mid (10д) НЕ помечен inactive — источник мёртв.
    st_mid = (
        db.query(ServiceEdge)
        .filter(ServiceEdge.kind == "serves_traffic")
        .all()
    )
    for e in st_mid:
        assert not (e.extras or {}).get("inactive")
    # Источник назван в отчёте.
    assert "k8s_topology_resources_sync" in total["edge_decay_stale_sources"]
    assert total["edge_decay_blocked_by_source"] == 2


def test_decay_runs_for_kind_with_healthy_source(db, monkeypatch):
    """Контроль: у serves_traffic есть свежее ребро (источник жив) →
    старое удаляется, среднее помечается inactive, как и раньше."""
    src = upsert_service(db, "squad-1", "src")
    for i in range(3):
        _mk_edge(db, src, f"c-fresh-{i}", "calls", "kg_sync/env_vars", 0)
    _mk_edge(db, src, "st-fresh", "serves_traffic",
             "k8s_topology_resources/service", 0)
    _mk_edge(db, src, "st-old", "serves_traffic",
             "k8s_topology_resources/service", 40)
    _mk_edge(db, src, "st-mid", "serves_traffic",
             "k8s_topology_resources/service", 10)
    db.commit()

    monkeypatch.setattr(kg_sync, "_kubectl_get_deployments", lambda ns: [])
    total = sync_topology(db, namespaces=["squad-1"])

    assert total["edge_decay_skipped"] is False
    assert total["edges_deleted"] == 1
    assert total["edges_marked_inactive"] == 1
    names = {e.dst.name for e in db.query(ServiceEdge).all()}
    assert "st-old" not in names
    st_mid = (
        db.query(ServiceEdge)
        .join(kg_sync.Service, ServiceEdge.dst_id == kg_sync.Service.id)
        .filter(kg_sync.Service.name == "st-mid")
        .one()
    )
    assert (st_mid.extras or {}).get("inactive") is True
    assert total["edge_decay_stale_sources"] == []


def test_decay_source_health_is_per_discovered_by_family(db, monkeypatch):
    """`calls` из env (kg_sync) и `calls` из ingress-синка — РАЗНЫЕ источники
    свежести: живой env-scan не легализует удаление ingress-рёбер, чей
    синк молчит."""
    src = upsert_service(db, "squad-1", "src")
    for i in range(3):
        _mk_edge(db, src, f"env-fresh-{i}", "calls", "kg_sync/env_vars", 0)
    _mk_edge(db, src, "env-old", "calls", "kg_sync/env_vars", 40)
    # ingress-calls: единственное ребро, старое → источник мёртв.
    _mk_edge(db, src, "ing-old", "calls", "kg_sync/ingress", 40)
    db.commit()

    monkeypatch.setattr(kg_sync, "_kubectl_get_deployments", lambda ns: [])
    total = sync_topology(db, namespaces=["squad-1"])

    names = {e.dst.name for e in db.query(ServiceEdge).all()}
    assert "env-old" not in names        # kg_sync жив → удалено
    assert "ing-old" in names            # k8s_ingress_sync мёртв → защищено
    assert "k8s_ingress_sync" in total["edge_decay_stale_sources"]


# ── #4 session poisoning: namespace-isolation через savepoint ───────────────


def test_pass1_namespace_isolation_via_savepoint(db, monkeypatch):
    """Flush-ошибка в одном ns откатывается savepoint'ом; соседи выживают,
    терминальный commit не падает с PendingRollbackError."""
    monkeypatch.setattr(
        kg_sync, "_kubectl_get_deployments",
        lambda ns: [{"metadata": {"name": "x"}}],
    )

    def fake_sync_ns(db_, ns, deploys=None):
        # Частичная реальная запись, затем падение на squad-2.
        db_.add(Service(namespace=ns, name=f"svc-{ns}", synthetic=False))
        db_.flush()
        if ns == "squad-2":
            raise RuntimeError("simulated flush error mid-namespace")
        return {"services": 1, "edges": 0, "skipped": 0}

    monkeypatch.setattr(kg_sync, "sync_namespace", fake_sync_ns)

    total = sync_topology(db, namespaces=["squad-1", "squad-2", "squad-3"])

    assert total["errors"] == 1
    assert total["namespaces"] == 2
    names = {s.name for s in db.query(Service).all()}
    # svc-squad-2 откатан savepoint'ом; соседи закоммичены (до фикса
    # PendingRollbackError потерял бы весь проход или сохранил бы svc-squad-2).
    assert names == {"svc-squad-1", "svc-squad-3"}


def test_pass2_namespace_isolation_via_savepoint(db, monkeypatch):
    """Pass 2 (extended env-scan) изолирован savepoint-ом так же, как Pass 1.

    Реальный триггер: конкурентный phantom_db_cleanup удаляет db:%-узел, к
    которому Pass 2 цепляет ребро → flush-ошибка. Без savepoint Session
    уходила в aborted-состояние: соседние ns падали PendingRollbackError,
    терминальный db.commit() убивал task, Pass 3 (revive/decay) не бежал.
    """
    # Существующий сервис — заготовка для IntegrityError на дубле.
    upsert_service(db, "squad-2", "dup")
    db.commit()

    monkeypatch.setattr(
        kg_sync, "_kubectl_get_deployments",
        lambda ns: [{"metadata": {"name": f"svc-{ns}"}}],
    )

    def fake_enrich(db_, ns, deploys, known_index):
        # Частичная запись + flush-ошибка на squad-2 (дубль UNIQUE-ключа —
        # реальный IntegrityError, переводящий Session в aborted).
        db_.add(Service(namespace=ns, name=f"p2-{ns}", synthetic=False))
        db_.flush()
        if ns == "squad-2":
            db_.add(Service(namespace="squad-2", name="dup", synthetic=False))
            db_.flush()  # IntegrityError (uq_kg_service_ns_name)
        return 0

    monkeypatch.setattr(kg_sync, "_enrich_calls_edges_for_ns", fake_enrich)

    total = sync_topology(db, namespaces=["squad-1", "squad-2", "squad-3"])

    # Ошибка squad-2 посчитана, но task дожил до конца (Pass 3 отработал).
    assert total["errors"] == 1
    assert "edge_decay_skipped" in total
    names = {s.name for s in db.query(Service).all()}
    # Частичная запись squad-2 откатана savepoint-ом, соседи закоммичены.
    assert "p2-squad-1" in names
    assert "p2-squad-3" in names
    assert "p2-squad-2" not in names


# ── #3 upsert races: recover from IntegrityError ────────────────────────────


def _stale_read_once(monkeypatch):
    """Заставить ПЕРВЫЙ Query.one_or_none() вернуть None (симуляция stale-read
    в гонке: наш SELECT не увидел строку, вставленную параллельным tick'ом).
    Последующие вызовы — реальные."""
    orig = orm.Query.one_or_none
    state = {"n": 0}

    def stale(self):
        state["n"] += 1
        if state["n"] == 1:
            return None
        return orig(self)

    monkeypatch.setattr(orm.Query, "one_or_none", stale)


def test_upsert_k8s_job_recovers_from_integrity_race(db, monkeypatch):
    """Строка уже есть (вставил чужой tick), наш one_or_none её не увидел →
    INSERT ловит IntegrityError → savepoint откат → re-query + update.
    Без дубля, tick не потерян."""
    _upsert_k8s_job(
        db, namespace="prod-shared", name="mig", kind="job",
        fields={"succeeded_count": 1, "failed_count": 0, "active_count": 0},
    )
    db.commit()
    assert db.query(K8sJob).count() == 1

    _stale_read_once(monkeypatch)
    node = _upsert_k8s_job(
        db, namespace="prod-shared", name="mig", kind="job",
        fields={"succeeded_count": 2, "failed_count": 0, "active_count": 0},
    )
    db.commit()

    assert db.query(K8sJob).count() == 1  # без дубля
    assert node.succeeded_count == 2      # апдейт применён (last-write-wins)


def test_upsert_volume_recovers_from_integrity_race(db, monkeypatch):
    """Тот же сценарий для _upsert_volume (UNIQUE kind,namespace,name)."""
    fields = {
        "kind": "pvc", "namespace": "prod-shared", "name": "data-0",
        "capacity_bytes": 1024, "storage_class": "local-path",
        "phase": "Bound", "access_modes": ["ReadWriteOnce"],
        "volume_name": None, "metadata_json": {},
    }
    _upsert_volume(db, dict(fields))
    db.commit()
    assert db.query(StorageVolume).count() == 1

    _stale_read_once(monkeypatch)
    updated = dict(fields, phase="Released")
    vol = _upsert_volume(db, updated)
    db.commit()

    assert db.query(StorageVolume).count() == 1   # без дубля
    assert vol.phase == "Released"                # апдейт применён
