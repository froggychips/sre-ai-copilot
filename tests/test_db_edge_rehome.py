"""Перевес рёбер `uses_db` с баз удалённых окружений на живые.

Контекст в docstring `app.knowledge_graph.db_edge_rehome`: отключённый
`phantom_db_cleanup` схлопывал разные физические базы в одну, выбирая
канонической ту, что в лексикографически минимальном namespace. На проде
20.08.2026 это дало 3676 рёбер «живой сервис → база удалённого
preprod-kingdom1».
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.db_edge_rehome import rehome_db_edges, undo_rehome
from app.knowledge_graph.schema import Namespace, Service, ServiceEdge


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


def _ns(db, name, state="active"):
    db.add(Namespace(namespace=name, state=state))
    db.flush()


def _svc(db, name, ns):
    s = Service(name=name, namespace=ns)
    db.add(s)
    db.flush()
    return s


def _edge(db, src, dst, kind="uses_db", weight=1):
    e = ServiceEdge(src_id=src.id, dst_id=dst.id, kind=kind, weight=weight)
    db.add(e)
    db.flush()
    return e


def _scene(db):
    """Прод-сервис, ссылающийся на базу удалённого окружения, и живая база."""
    _ns(db, "prod-shared", "active")
    _ns(db, "prod-kingdom1", "active")
    _ns(db, "preprod-kingdom1", "missing")
    dead_db = _svc(db, "db:postgres:town", "preprod-kingdom1")
    live_db = _svc(db, "db:postgres:town", "prod-shared")
    client = _svc(db, "town-service", "prod-kingdom1")
    edge = _edge(db, client, dead_db)
    db.commit()
    return client, dead_db, live_db, edge


def test_dry_run_writes_nothing(db):
    client, dead_db, live_db, edge = _scene(db)
    stats = rehome_db_edges(db, apply=False)
    assert stats["applied"] is False
    assert stats["stale_edges"] == 1
    assert stats["repointed"] == 1      # приёмник найден
    assert stats["no_target"] == 0
    db.refresh(edge)
    assert edge.dst_id == dead_db.id, "dry-run изменил граф"


def test_edge_moves_to_shared_of_source_realm(db):
    """Ребро уходит в `<realm>-shared` окружения ИСТОЧНИКА, а не куда попало."""
    client, dead_db, live_db, edge = _scene(db)
    stats = rehome_db_edges(db, apply=True)
    assert stats["applied"] is True
    assert stats["repointed"] == 1
    db.refresh(edge)
    assert edge.dst_id == live_db.id


def test_second_run_is_a_noop(db):
    """Идемпотентность: повторный прогон на исправленном графе ничего не делает."""
    _scene(db)
    rehome_db_edges(db, apply=True)
    again = rehome_db_edges(db, apply=True)
    assert again["stale_edges"] == 0
    assert again["repointed"] == 0


def test_merges_into_existing_correct_edge_keeping_max_weight(db):
    """Если правильное ребро уже создано синком — сливаем, вес не понижаем."""
    client, dead_db, live_db, stale = _scene(db)
    correct = _edge(db, client, live_db, weight=3)
    stale.weight = 7
    db.commit()

    stats = rehome_db_edges(db, apply=True)
    assert stats["merged"] == 1
    assert stats["repointed"] == 0
    db.refresh(correct)
    assert correct.weight == 7, "вес понизился при слиянии"
    assert db.query(ServiceEdge).filter(ServiceEdge.id == stale.id).first() is None


def test_edge_without_target_is_left_alone(db):
    """Нет узла-приёмника — ребро не трогаем и не выдумываем узел.

    Выдуманный узел хуже устаревшего: устаревший видит `graph_integrity`,
    выдуманный неотличим от настоящего.
    """
    _ns(db, "prod-kingdom1", "active")
    _ns(db, "preprod-kingdom1", "missing")
    dead_db = _svc(db, "db:postgres:lonely", "preprod-kingdom1")
    client = _svc(db, "town-service", "prod-kingdom1")
    edge = _edge(db, client, dead_db)
    db.commit()

    stats = rehome_db_edges(db, apply=True)
    assert stats["no_target"] == 1
    assert stats["repointed"] == 0
    db.refresh(edge)
    assert edge.dst_id == dead_db.id


def test_dead_to_dead_edges_are_not_touched(db):
    """Снесённый сквад ссылается на свою же базу — законная история.

    Её убирает retention в `namespace_lifecycle`, а не этот модуль: перенос
    сделал бы вид, будто мёртвое окружение ходит в живую базу.
    """
    _ns(db, "squad-23-shared", "missing")
    _ns(db, "prod-shared", "active")
    _svc(db, "db:postgres:town", "prod-shared")
    dead_db = _svc(db, "db:postgres:town", "squad-23-shared")
    dead_client = _svc(db, "town-service", "squad-23-shared")
    edge = _edge(db, dead_client, dead_db)
    db.commit()

    stats = rehome_db_edges(db, apply=True)
    assert stats["stale_edges"] == 0
    db.refresh(edge)
    assert edge.dst_id == dead_db.id


def test_non_db_edges_are_out_of_scope(db):
    """Модуль трогает только `db:%`-узлы и только виды рёбер из DB_EDGE_KINDS."""
    _ns(db, "prod-kingdom1", "active")
    _ns(db, "preprod-kingdom1", "missing")
    dead_svc = _svc(db, "town-service", "preprod-kingdom1")
    client = _svc(db, "gateway", "prod-kingdom1")
    edge = _edge(db, client, dead_svc, kind="calls")
    db.commit()

    stats = rehome_db_edges(db, apply=True)
    assert stats["stale_edges"] == 0
    db.refresh(edge)
    assert edge.dst_id == dead_svc.id


def test_retired_collapse_refuses_to_run():
    """Старый backfill обязан падать с объяснением, а не тихо портить граф.

    Он доступен из CLI с `--apply`; его dry-run тоже отключён, потому что
    отчёт называл дублями 56 законных узлов и подталкивал применить перенос.
    """
    from app.knowledge_graph.phantom_db_cleanup import (
        PhantomDbCleanupRetired, collapse_phantom_db_nodes)

    for apply in (False, True):
        with pytest.raises(PhantomDbCleanupRetired) as exc:
            collapse_phantom_db_nodes(None, apply=apply)  # type: ignore[arg-type]
        assert "db_edge_rehome" in str(exc.value)


# ── откат ─────────────────────────────────────────────────────────────────
#
# Перенос меняет dst_id на месте, и без журнала прежнего адреса он был бы
# необратим. Поэтому в extras пишется rehomed_from — на проде эта операция
# затрагивает 3612 рёбер, и «откатить нечем» там неприемлемый ответ.


def test_repointed_edge_records_where_it_came_from(db):
    client, dead_db, live_db, edge = _scene(db)
    rehome_db_edges(db, apply=True)
    db.refresh(edge)
    assert edge.extras["rehomed_from"] == "preprod-kingdom1"
    assert edge.extras.get("rehomed_at")


def test_round_trip_returns_the_graph_to_its_original_state(db):
    """Перенёс → вернул → ребро смотрит туда же, куда до начала."""
    client, dead_db, live_db, edge = _scene(db)
    before = edge.dst_id

    rehome_db_edges(db, apply=True)
    db.refresh(edge)
    assert edge.dst_id == live_db.id

    stats = undo_rehome(db, apply=True)
    assert stats["restored"] == 1
    db.refresh(edge)
    assert edge.dst_id == before
    assert "rehomed_from" not in (edge.extras or {}), "журнал не убран за собой"


def test_undo_dry_run_writes_nothing(db):
    client, dead_db, live_db, edge = _scene(db)
    rehome_db_edges(db, apply=True)
    stats = undo_rehome(db, apply=False)
    assert stats["marked_edges"] == 1
    assert stats["restored"] == 0
    db.refresh(edge)
    assert edge.dst_id == live_db.id, "dry-run отката изменил граф"


def test_undo_leaves_edge_alone_when_origin_node_is_gone(db):
    """Retention снёс мёртвое окружение — возвращать некуда.

    Выдумывать узел ради отката значило бы делать ровно то, чего избегает
    сам перенос.
    """
    client, dead_db, live_db, edge = _scene(db)
    rehome_db_edges(db, apply=True)
    db.delete(dead_db)
    db.commit()

    stats = undo_rehome(db, apply=True)
    assert stats["target_gone"] == 1
    assert stats["restored"] == 0
    db.refresh(edge)
    assert edge.dst_id == live_db.id


def test_undo_skips_when_old_address_is_occupied(db):
    """На прежнем адресе уже есть ребро — возврат нарушил бы UNIQUE."""
    client, dead_db, live_db, edge = _scene(db)
    rehome_db_edges(db, apply=True)
    _edge(db, client, dead_db)     # кто-то создал ребро на старый узел
    db.commit()

    stats = undo_rehome(db, apply=True)
    assert stats["conflict"] == 1
    assert stats["restored"] == 0


def test_undo_is_a_noop_without_a_journal(db):
    """Рёбра, которых перенос не касался, откат не трогает."""
    _scene(db)
    stats = undo_rehome(db, apply=True)
    assert stats["marked_edges"] == 0
    assert stats["restored"] == 0


# ── чужое окружение, но ЖИВОЕ ─────────────────────────────────────────────
#
# Первый прогон переноса 21.08.2026 закрыл только рёбра в удалённые
# namespace. После него в графе осталось 1900 кросс-окруженческих рёбер,
# включая двенадцать вида «прод-сервис ходит в базу препрода»: все семь
# bot-service из prod-kingdom1..7 указывали на
# preprod-kingdom2/db:postgres:map-coordinator, хотя
# prod-shared/db:postgres:map-coordinator существует. Живой
# namespace-получатель делал эту ложь незаметной для проверки, которая
# смотрела только на state='missing'.


def test_prod_service_pointing_at_preprod_db_is_rehomed(db):
    """Прод не ходит в базу препрода — даже если препрод жив и здоров."""
    _ns(db, "prod-kingdom1", "active")
    _ns(db, "prod-shared", "active")
    _ns(db, "preprod-kingdom2", "active")
    wrong = _svc(db, "db:postgres:map-coordinator", "preprod-kingdom2")
    right = _svc(db, "db:postgres:map-coordinator", "prod-shared")
    bot = _svc(db, "bot-service", "prod-kingdom1")
    edge = _edge(db, bot, wrong)
    db.commit()

    stats = rehome_db_edges(db, apply=True)
    assert stats["repointed"] == 1
    db.refresh(edge)
    assert edge.dst_id == right.id
    assert edge.extras["rehomed_from"] == "preprod-kingdom2"


def test_edge_within_the_same_realm_is_left_alone(db):
    """Своё окружение — не трогаем: kingdom и shared одного realm это одно."""
    _ns(db, "prod-kingdom1", "active")
    _ns(db, "prod-shared", "active")
    own_db = _svc(db, "db:postgres:town", "prod-shared")
    svc = _svc(db, "town-service", "prod-kingdom1")
    edge = _edge(db, svc, own_db)
    db.commit()

    stats = rehome_db_edges(db, apply=True)
    assert stats["stale_edges"] == 0
    db.refresh(edge)
    assert edge.dst_id == own_db.id


def test_dead_squad_without_its_own_db_is_left_alone(db):
    """Снесённый сквад без своих БД: переносить некуда, и это не ошибка.

    Замер 21.08.2026: 100 таких рёбер, все из squad-20-shared — namespace
    в кластере отсутствует, а db-узлов у него в графе нет ни одного (280
    узлов, из них db: 0). Их уберёт retention вместе с самим сквадом.
    """
    _ns(db, "squad-20-shared", "missing")
    _ns(db, "preprod-shared", "active")
    shared_db = _svc(db, "db:postgres:config", "preprod-shared")
    dead_svc = _svc(db, "config-service", "squad-20-shared")
    edge = _edge(db, dead_svc, shared_db)
    db.commit()

    stats = rehome_db_edges(db, apply=True)
    assert stats["no_target"] == 1
    assert stats["repointed"] == 0
    db.refresh(edge)
    assert edge.dst_id == shared_db.id


def test_namespace_without_a_realm_is_out_of_scope(db):
    """`sre-ai`, `monitoring` — судить о «своём окружении» для них не на чем."""
    _ns(db, "sre-ai", "active")
    _ns(db, "prod-shared", "active")
    prod_db = _svc(db, "db:postgres:town", "prod-shared")
    own = _svc(db, "copilot-worker", "sre-ai")
    edge = _edge(db, own, prod_db)
    db.commit()

    stats = rehome_db_edges(db, apply=True)
    assert stats["stale_edges"] == 0
    db.refresh(edge)
    assert edge.dst_id == prod_db.id


# ── периодическая задача ──────────────────────────────────────────────────


def test_task_is_scheduled_after_namespace_lifecycle():
    """Отбор опирается на состояние namespace — считать его надо по свежей таблице.

    `kg_namespace_lifecycle` идёт на :3,13,23,33,43,53, перенос — на :07.
    Порядок важен: 21.08.2026 в отборе появились 64 ребра только потому, что
    lifecycle перевёл пересозданный `squad-10-kingdom2` из missing в active.
    """
    from app.workers.tasks import celery_app

    sched = celery_app.conf.beat_schedule
    assert "kg-db-edge-rehome" in sched
    entry = sched["kg-db-edge-rehome"]
    assert entry["task"] == "kg_db_edge_rehome"
    # crontab(minute="7") — раз в час, заведомо позже ближайшего :3
    assert 7 in entry["schedule"].minute
    lifecycle_minutes = sched["kg-namespace-lifecycle"]["schedule"].minute
    assert min(m for m in (7,)) > min(lifecycle_minutes)


def test_task_respects_the_disable_flag(monkeypatch):
    """Задача пишет в граф — выключатель на такое обязателен."""
    from app.config import settings
    from app.workers.tasks import kg_db_edge_rehome_task

    monkeypatch.setattr(settings, "KG_DB_EDGE_REHOME_ENABLED", False)
    assert kg_db_edge_rehome_task() == {"status": "disabled"}
