"""Выкаты, замеченные в кластере, а не рассказанные TeamCity.

`kg_deployments` наполнялся из одного источника, и замер 05.09.2026 показал
цену такой монополии: 137 423 записи, все с маркером ns-broadcast, и лишь 36
с точной целью. На таком входе `stale_classifier` не мог выдать `active`
никому, а `RecentDeployRule` отвечала «деплой был» на любой алерт активного
стенда.

Главное в этом источнике — не то, что он пишет, а то, чего он не пишет:
ложный «деплой» здесь дороже пропущенного. Поэтому тестов на молчание
больше, чем на запись.
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph import k8s_deploy_watch as dw
from app.knowledge_graph.schema import (NODE_KIND_SERVICE, NODE_KIND_WORKLOAD,
                                        Deployment, Service)

NOW = datetime(2026, 9, 5, 12, 0)


class _FakeRedis:
    def __init__(self, initial=None):
        self.h = dict(initial or {})

    def hgetall(self, key):
        return dict(self.h)

    def hset(self, key, mapping=None):
        self.h.update(mapping or {})

    def delete(self, key):
        self.h.clear()

    def expire(self, key, ttl):
        return True


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    s.add(Service(namespace="squad-1-shared", name="town-service",
                  node_kind=NODE_KIND_SERVICE, synthetic=False))
    s.commit()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _workload(*, name="town-service", ns="squad-1-shared", uid="uid-a",
              generation=7, image="nexus/town:1.0.0", kind="Deployment"):
    return {
        "kind": kind,
        "metadata": {"name": name, "namespace": ns, "uid": uid,
                     "generation": generation},
        "spec": {"template": {"spec": {"containers": [{"image": image}]}}},
    }


def _snapshot_of(*objs):
    """Redis-состояние «мы это уже видели»."""
    out = {}
    for o in objs:
        st = dw._current_state(o)
        out[dw._workload_key(st["ns"], st["kind"], st["name"])] = json.dumps(st)
    return out


def _run(db, workloads, redis):
    import app.knowledge_graph.k8s_deploy_watch as mod
    orig = mod._redis
    mod._redis = lambda: redis
    try:
        return mod.watch_k8s_rollouts(db, workloads=workloads, now=NOW)
    finally:
        mod._redis = orig


# ── чего источник не делает ────────────────────────────────────────────────

def test_first_run_records_nothing(db):
    """Сравнивать не с чем — записать «деплой» всему кластеру было бы ложью."""
    stats = _run(db, [_workload()], _FakeRedis())

    assert stats["first_run"] is True
    assert stats["recorded"] == 0
    assert db.query(Deployment).count() == 0


def test_unchanged_workload_is_not_a_deploy(db):
    """Задача ходит каждые 5 минут — тишина должна оставаться тишиной."""
    obj = _workload()
    stats = _run(db, [obj], _FakeRedis(_snapshot_of(obj)))

    assert stats["recorded"] == 0
    assert db.query(Deployment).count() == 0


def test_empty_kubectl_response_does_not_wipe_the_snapshot(db):
    """Пустой ответ — сбой запроса, а не опустевший кластер.

    Затерев снимок, следующий прогон объявил бы выкатом каждый workload.
    """
    redis = _FakeRedis(_snapshot_of(_workload()))
    stats = _run(db, [], redis)

    assert stats["skipped"] == "empty_response"
    assert redis.h, "снимок должен уцелеть"


def test_new_workload_is_not_a_rollout(db):
    """Появление объекта — не выкат новой версии существующего сервиса."""
    stats = _run(db, [_workload(), _workload(name="other-service")],
                 _FakeRedis(_snapshot_of(_workload())))

    assert stats["recorded"] == 0


def test_recreated_object_is_not_a_rollout(db):
    """Другой uid — объект пересоздан, история прежнего к нему не относится."""
    stats = _run(
        db, [_workload(uid="uid-b", generation=1)],
        _FakeRedis(_snapshot_of(_workload(uid="uid-a", generation=9))),
    )

    assert stats["reincarnated"] == 1
    assert stats["recorded"] == 0


def test_scale_down_does_not_look_like_a_rollout(db):
    """generation убывать не может, но снимок мог протухнуть — не выдумываем."""
    stats = _run(db, [_workload(generation=3)],
                 _FakeRedis(_snapshot_of(_workload(generation=9))))

    assert stats["recorded"] == 0


# ── что источник записывает ────────────────────────────────────────────────

def test_image_change_is_recorded_as_a_deploy(db):
    stats = _run(
        db, [_workload(image="nexus/town:1.1.0", generation=8)],
        _FakeRedis(_snapshot_of(_workload(image="nexus/town:1.0.0"))),
    )

    assert stats["recorded"] == 1
    assert stats["by_reason"]["image"] == 1
    d = db.query(Deployment).one()
    assert d.extras["rollout_reason"] == "image"
    assert d.extras["previous_images"] == ["nexus/town:1.0.0"]
    assert d.extras["images"] == ["nexus/town:1.1.0"]


def test_generation_bump_alone_is_recorded_but_labelled(db):
    """`kubectl scale` тоже двигает generation — потребитель должен различать."""
    stats = _run(db, [_workload(generation=8)],
                 _FakeRedis(_snapshot_of(_workload(generation=7))))

    assert stats["by_reason"]["generation"] == 1
    assert db.query(Deployment).one().extras["rollout_reason"] == "generation"


def test_record_is_not_a_namespace_broadcast(db):
    """Ради этого источник и заводился.

    `namespace_scope=False` — единственное, что даёт `classify_stale_with_deploys`
    право назвать сервис `active`.
    """
    from app.knowledge_graph.stale_classifier import is_ns_broadcast_deploy

    _run(db, [_workload(image="nexus/town:2.0.0")],
         _FakeRedis(_snapshot_of(_workload())))

    d = db.query(Deployment).one()
    assert d.extras["namespace_scope"] is False
    assert is_ns_broadcast_deploy(d.extras) is False
    assert d.extras["attribution"] == "k8s_rollout"


def test_record_lands_on_the_node_recent_deploys_reads(db):
    """Запись на workload-узел RecentDeployRule не увидела бы."""
    db.add(Service(namespace="squad-1-shared", name="town-service",
                   node_kind=NODE_KIND_WORKLOAD, synthetic=False))
    db.commit()

    _run(db, [_workload(image="nexus/town:2.0.0")],
         _FakeRedis(_snapshot_of(_workload())))

    d = db.query(Deployment).one()
    assert db.get(Service, d.service_id).node_kind == NODE_KIND_SERVICE


def test_falls_back_to_workload_node_when_no_service_node(db):
    """Потерять событие хуже, чем записать его на соседний узел."""
    db.add(Service(namespace="squad-1-shared", name="db-only",
                   node_kind=NODE_KIND_WORKLOAD, synthetic=False))
    db.commit()

    _run(db, [_workload(name="db-only", image="nexus/db:2")],
         _FakeRedis(_snapshot_of(_workload(name="db-only", image="nexus/db:1"))))

    d = db.query(Deployment).one()
    assert db.get(Service, d.service_id).node_kind == NODE_KIND_WORKLOAD


def test_unknown_service_is_counted_not_guessed(db):
    """Узла нет — считаем и идём дальше, а не заводим его задним числом."""
    stats = _run(db, [_workload(name="ghost", image="x:2")],
                 _FakeRedis(_snapshot_of(_workload(name="ghost", image="x:1"))))

    assert stats["no_node"] == 1
    assert db.query(Deployment).count() == 0


def test_same_generation_is_not_recorded_twice(db):
    """Дедуп по (service, buildtype, build_number) — на случай повторов."""
    redis = _FakeRedis(_snapshot_of(_workload()))
    _run(db, [_workload(image="nexus/town:2.0.0", generation=8)], redis)
    # Снимок обновился, повторный прогон с тем же состоянием — тишина.
    _run(db, [_workload(image="nexus/town:2.0.0", generation=8)], redis)

    assert db.query(Deployment).count() == 1
