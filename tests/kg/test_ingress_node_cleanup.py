"""k8s_ingress_sync убирает узлы хостов, которых в кластере больше нет.

Синк только добавлял: `ingress:<host>` заводился при первом появлении
Ingress-а и жил вечно. Замер 31.10.2026 — 354 ingress-узла в живых namespace
при 223 Ingress-ах в кластере, 184 (52%) не освежались больше суток:
`check_node_freshness` висел в warn, а граф выдавал снесённые стенды за
точки входа.

Что фиксируют тесты:
  * узел, не виденный дольше порога, удаляется вместе с рёбрами; свежий и
    узел живого host-а (даже с устаревшим `updated_at`) остаются;
  * предохранители — сбой fetch-а, пустой снимок, отсутствие опоры и
    усадка живого набора — чистку отменяют, и отмена видна в
    `stats["cleanup"]` и дальше в `check_source_coverage`;
  * узел с NOT NULL-зависимостью пропускается с причиной, а не исключением;
  * `delete_service_node` отвязывает nullable FK и не трогает чужие таблицы.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.knowledge_graph.k8s_ingress_sync as mod
from app.database import Base
from app.knowledge_graph.edge_decay_guard import (ALL_EDGE_SOURCES,
                                                  SOURCE_INGRESS_SYNC,
                                                  get_source_report,
                                                  record_source_run)
from app.knowledge_graph.populator import (DELETE_BLOCKED_HARD_DEPENDENCIES,
                                           delete_service_node, upsert_edge)
from app.knowledge_graph.schema import (NODE_KIND_INGRESS, NODE_KIND_SERVICE,
                                        AlertEvent, Deployment,
                                        IngressObservation, Service,
                                        ServiceEdge, ServiceHealth)

NS = "squad-7"
BACKEND = "auth-service"


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    session.add(Service(namespace=NS, name=BACKEND, node_kind=NODE_KIND_SERVICE))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _backend(db) -> Service:
    return db.query(Service).filter_by(namespace=NS, name=BACKEND).one()


def _ingress_node(db, host: str, *, age: timedelta, with_edge: bool = True) -> Service:
    """Synthetic-узел host-а, освежённый `age` назад (как после прогона синка)."""
    stamp = datetime.utcnow() - age
    node = Service(
        namespace=NS, name=f"ingress:{host}", node_kind=NODE_KIND_INGRESS,
        team_owner="external", synthetic=True,
        created_at=stamp, updated_at=stamp,
    )
    db.add(node)
    db.flush()
    if with_edge:
        upsert_edge(db, src=node, dst=_backend(db), kind="calls",
                    discovered_by="kg_sync/ingress")
    db.commit()
    return node


def _ingress(host: str, backend: str = BACKEND, ns: str = NS) -> dict:
    return {
        "metadata": {"name": f"ing-{host}", "namespace": ns},
        "spec": {"rules": [
            {"host": host, "http": {"paths": [
                {"path": "/", "backend": {"service": {"name": backend}}},
            ]}},
        ]},
    }


def _run(db, snapshot):
    with patch.object(mod, "_kubectl_get_ingresses_all", return_value=snapshot):
        return mod.sync_all_ingresses(db)


def _exists(db, host: str) -> bool:
    return db.query(Service).filter_by(name=f"ingress:{host}").count() == 1


STALE = timedelta(days=mod.INGRESS_NODE_STALE_DAYS + 1)
LIVE = timedelta(hours=1)


# ── Основной сценарий ─────────────────────────────────────────────────────


def test_vanished_node_is_deleted_with_its_edges(db):
    """Хост не виден 8 дней и его нет в снимке — узел и его рёбра уходят."""
    gone = _ingress_node(db, "gone.example.com", age=STALE)
    _ingress_node(db, "live.example.com", age=LIVE)
    gone_id = gone.id

    stats = _run(db, [_ingress("live.example.com")])

    assert not _exists(db, "gone.example.com")
    assert db.query(ServiceEdge).filter(
        (ServiceEdge.src_id == gone_id) | (ServiceEdge.dst_id == gone_id)
    ).count() == 0, "ребро без конца — не ребро, оно уходит с узлом"
    assert _exists(db, "live.example.com")
    assert stats["nodes_deleted"] == 1
    assert stats["nodes_delete_skipped"] == 0
    assert stats["cleanup"]["skipped"] == ""
    assert stats["cleanup"]["edges_deleted"] == 1
    # Сам backend и его сторона графа не задеты.
    assert db.query(Service).filter_by(name=BACKEND).count() == 1


def test_recently_refreshed_node_survives_even_if_absent_from_snapshot(db):
    """Обновлённый сегодня, но не попавший в этот снимок, — не кандидат:
    один пропуск (route упал под savepoint, Ingress мигнул) не повод."""
    _ingress_node(db, "today.example.com", age=timedelta(hours=3))
    _ingress_node(db, "live.example.com", age=LIVE)

    stats = _run(db, [_ingress("live.example.com")])

    assert _exists(db, "today.example.com")
    assert stats["nodes_deleted"] == 0
    assert stats["cleanup"]["candidates"] == 0


def test_host_present_in_snapshot_survives_despite_stale_updated_at(db):
    """Host в снимке — узел живой, какой бы старой ни была метка.

    На sqlite `upsert_service` метку не освежает, если в строке ничего не
    изменилось, — поэтому здесь защищает именно сверка со снимком. Она же
    прикрывает route без backend-а в графе: узел не освежается, а Ingress жив.
    """
    _ingress_node(db, "seen.example.com", age=STALE)
    _ingress_node(db, "orphan.example.com", age=STALE)
    _ingress_node(db, "live.example.com", age=LIVE)

    stats = _run(db, [
        _ingress("live.example.com"),
        _ingress("seen.example.com"),
        _ingress("orphan.example.com", backend="no-such-backend"),
    ])

    assert _exists(db, "seen.example.com")
    assert _exists(db, "orphan.example.com"), (
        "skipped_no_backend_match не освежает узел, но Ingress-то жив"
    )
    assert stats["skipped_no_backend_match"] == 1
    assert stats["nodes_deleted"] == 0


def test_non_ingress_stale_nodes_are_not_candidates(db):
    """Чистка — только про `node_kind='ingress'`: сервис с такой же старой
    меткой живёт по своим правилам (его `updated_at` трогает health)."""
    stale = datetime.utcnow() - STALE
    db.add(Service(namespace=NS, name="old-service", node_kind=NODE_KIND_SERVICE,
                   created_at=stale, updated_at=stale))
    db.commit()
    _ingress_node(db, "live.example.com", age=LIVE)

    stats = _run(db, [_ingress("live.example.com")])

    assert db.query(Service).filter_by(name="old-service").count() == 1
    assert stats["cleanup"]["candidates"] == 0


# ── Предохранители ────────────────────────────────────────────────────────


def test_fetch_failure_skips_cleanup(db):
    """kubectl упал — снимок неполный, по нему не чистят."""
    _ingress_node(db, "gone.example.com", age=STALE)
    _ingress_node(db, "live.example.com", age=LIVE)

    with patch.object(
        mod, "_kubectl_get_ingresses_all",
        side_effect=mod.IngressFetchError("timeout"),
    ):
        stats = mod.sync_all_ingresses(db)

    assert _exists(db, "gone.example.com")
    assert stats["errors"] == 1
    assert stats["nodes_deleted"] == 0
    assert stats["cleanup"]["skipped"] == "fetch_failed"


def test_empty_snapshot_skips_cleanup(db):
    """0 Ingress-ов в ответе неотличимы от пустого кластера — не чистим."""
    _ingress_node(db, "gone.example.com", age=STALE)
    _ingress_node(db, "live.example.com", age=LIVE)

    stats = _run(db, [])

    assert _exists(db, "gone.example.com")
    assert stats["cleanup"]["skipped"] == "empty_fetch"


def test_shrunken_live_set_blocks_cleanup_and_is_visible_outside(db):
    """Снимок отдал 1 host там, где за сутки видели 10, — усадка 90% > 30%:
    чистка отменяется, и это видно в отчёте источника и в self-health.

    Порог — про усадку ЖИВОГО набора, а не про долю кандидатов от всех
    узлов: вторая формула блокировала бы сама себя (мусор копится оттого,
    что чистка не идёт), и на сегодняшних 52% устаревших узлов не отпустила
    бы чистку никогда.
    """
    for i in range(10):
        _ingress_node(db, f"live-{i}.example.com", age=LIVE)
    _ingress_node(db, "gone.example.com", age=STALE)
    for source in ALL_EDGE_SOURCES:
        record_source_run(source, {"errors": 0})

    stats = _run(db, [_ingress("live-0.example.com")])

    assert _exists(db, "gone.example.com")
    cleanup = stats["cleanup"]
    assert cleanup["skipped"] == "delete_pct"
    assert cleanup["candidates"] == 1
    # Процент без чисел не читается — рядом опора, снимок и размер графа.
    assert cleanup["baseline"] == 10
    assert cleanup["snapshot"] == 1
    assert cleanup["rows_total"] == 11
    assert cleanup["shrink_pct"] == 90.0

    report = get_source_report(SOURCE_INGRESS_SYNC)
    assert report is not None
    assert report.cleanup["skipped"] == "delete_pct"
    assert report.cleanup["baseline"] == 10

    from app.knowledge_graph.self_health import check_source_coverage
    r = check_source_coverage(db)
    assert SOURCE_INGRESS_SYNC in r.detail["cleanup_blocked"]
    assert SOURCE_INGRESS_SYNC not in r.detail["unhealthy"], (
        "источник отработал штатно и сам себя притормозил — это не поломка"
    )


def test_no_baseline_blocks_cleanup(db):
    """За сутки синк не освежил ни одного узла (вернулся после простоя):
    сравнивать снимок не с чем — fail-closed до следующего прогона."""
    _ingress_node(db, "gone.example.com", age=STALE)
    _ingress_node(db, "also-old.example.com", age=timedelta(days=2))

    stats = _run(db, [_ingress("brand-new.example.com")])

    assert _exists(db, "gone.example.com")
    cleanup = stats["cleanup"]
    assert cleanup["skipped"] == "no_baseline"
    assert cleanup["snapshot"] == 1
    assert cleanup["rows_total"] == 3, "два старых + заведённый этим прогоном"
    # Узел нового host-а заведён и станет опорой следующему прогону.
    assert _exists(db, "brand-new.example.com")


def test_cleanup_resumes_once_baseline_exists(db):
    """Второй прогон после простоя уже имеет опору — чистка идёт."""
    _ingress_node(db, "gone.example.com", age=STALE)
    first = _run(db, [_ingress("live.example.com")])
    assert first["cleanup"]["skipped"] == "no_baseline"

    second = _run(db, [_ingress("live.example.com")])

    assert second["cleanup"]["skipped"] == ""
    assert second["nodes_deleted"] == 1
    assert not _exists(db, "gone.example.com")


def test_deletes_are_capped_per_run(db, monkeypatch):
    """Накопленное разбирается порциями: ошибка видна на первой порции."""
    monkeypatch.setattr(mod, "_INGRESS_MAX_DELETE_PER_RUN", 2)
    for i in range(3):
        _ingress_node(db, f"gone-{i}.example.com", age=STALE)
    _ingress_node(db, "live.example.com", age=LIVE)

    first = _run(db, [_ingress("live.example.com")])
    assert first["nodes_deleted"] == 2
    assert first["cleanup"]["capped_to"] == 2
    assert first["cleanup"]["candidates"] == 3

    second = _run(db, [_ingress("live.example.com")])
    assert second["nodes_deleted"] == 1
    assert "capped_to" not in second["cleanup"]


def test_node_with_hard_dependency_is_skipped_without_exception(db):
    """NOT NULL `service_id` (health) — узел остаётся с причиной, а соседний
    кандидат без зависимостей удаляется тем же прогоном."""
    pinned = _ingress_node(db, "pinned.example.com", age=STALE)
    db.add(ServiceHealth(service_id=pinned.id, ts=datetime.utcnow(), cpu_pct=1.0))
    db.commit()
    _ingress_node(db, "gone.example.com", age=STALE)
    _ingress_node(db, "live.example.com", age=LIVE)

    stats = _run(db, [_ingress("live.example.com")])

    assert _exists(db, "pinned.example.com")
    assert not _exists(db, "gone.example.com")
    assert stats["nodes_deleted"] == 1
    assert stats["nodes_delete_skipped"] == 1
    assert stats["cleanup"]["skip_reasons"] == {DELETE_BLOCKED_HARD_DEPENDENCIES: 1}
    # Рёбра защищённого узла не тронуты: чистка либо целиком, либо никак.
    assert db.query(ServiceEdge).filter(ServiceEdge.src_id == pinned.id).count() == 1


def test_cleanup_crash_does_not_lose_the_run_report(db):
    """Сбой самой чистки — ошибка источника за цикл, но не потеря тика:
    апсерты закоммичены, отчёт guard'у ушёл."""
    _ingress_node(db, "live.example.com", age=LIVE)

    with patch.object(mod, "_cleanup_vanished_ingress_nodes",
                      side_effect=RuntimeError("boom")):
        stats = _run(db, [_ingress("live.example.com"),
                          _ingress("fresh.example.com")])

    assert stats["errors"] == 1
    assert stats["cleanup"]["skipped"] == "error"
    assert _exists(db, "fresh.example.com"), "работа синка до чистки сохранена"
    report = get_source_report(SOURCE_INGRESS_SYNC)
    assert report is not None and report.errors == 1


# ── delete_service_node ───────────────────────────────────────────────────


def test_delete_service_node_unlinks_nullable_dependencies(db):
    """Nullable FK обнуляется, строка-история остаётся; рёбра уходят."""
    node = _ingress_node(db, "x.example.com", age=STALE)
    db.add(AlertEvent(service_id=node.id, alertname="ProbeFailed",
                      fired_at=datetime.utcnow(), fingerprint="fp-1"))
    db.add(IngressObservation(ts=datetime.utcnow(), ingress_name="ing",
                              host="x.example.com", path="/",
                              service_id=node.id))
    db.commit()
    node_id = node.id

    outcome = delete_service_node(db, node_id)
    db.commit()

    assert outcome["deleted"] is True
    assert outcome["edges_deleted"] == 1
    assert outcome["unlinked"] == {"kg_alerts": 1, "kg_ingress_observations": 1}
    assert db.query(Service).filter_by(id=node_id).count() == 0
    alert = db.query(AlertEvent).filter_by(fingerprint="fp-1").one()
    assert alert.service_id is None, "алерт — история, она переживает узел"
    obs = db.query(IngressObservation).filter_by(host="x.example.com").one()
    assert obs.service_id is None


def test_delete_service_node_refuses_hard_dependency(db):
    """Деплой с NOT NULL `service_id` — узел не удаляется, ничего не тронуто."""
    node = _ingress_node(db, "y.example.com", age=STALE)
    db.add(Deployment(service_id=node.id, started_at=datetime.utcnow()))
    db.commit()

    outcome = delete_service_node(db, node.id)
    db.commit()

    assert outcome["deleted"] is False
    assert outcome["reason"] == DELETE_BLOCKED_HARD_DEPENDENCIES
    assert outcome["blockers"] == {"kg_deployments": 1}
    assert db.query(Service).filter_by(id=node.id).count() == 1
    assert db.query(ServiceEdge).filter(ServiceEdge.src_id == node.id).count() == 1


def test_delete_service_node_reports_missing_node(db):
    outcome = delete_service_node(db, 10**9)
    assert outcome["deleted"] is False
    assert outcome["reason"] == "not_found"
