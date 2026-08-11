"""Окна и атрибуция в KG-аналитике (`knowledge_graph/queries.py`).

Три класса регрессий, все — «сигнал есть, а запрос его не видит»:

1. **Pod trail пустой у хронического crashloop.** k8s агрегирует повторы
   события в ОДНУ строку: растёт `count`/`lastTimestamp`, а `firstTimestamp`
   остаётся моментом первого падения. Фильтр по `first_seen` выбрасывал
   BackOff сервиса, крашащегося неделю — секция «🕒 Pod trail» показывала
   `total=0` ровно там, где нужнее всего.

2. **Один TC-билд считался K×2 раза.** `tc_deploys_to_kg` броадкастит билд на
   каждый non-synthetic узел ns (включая workload-дубль одноимённого
   сервиса), а ns-level атрибуция джойнила по namespace без `node_kind` и без
   дедупа: `limit=5` съедали копии одного билда, `total_deploys` врал в разы.

3. **Aware-datetime с не-UTC offset сдвигал окно.** `_ensure_aware(x)
   .replace(tzinfo=None)` корректен только для UTC; `startsAt` от
   AlertManager приходит с `+03:00`, и окно уезжало на 3 часа.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.queries import (cluster_deploy_activity,
                                         pod_event_summary_for,
                                         recent_deploys_for_namespaces,
                                         recent_pod_events_for)
from app.knowledge_graph.schema import (NODE_KIND_SERVICE, NODE_KIND_WORKLOAD,
                                        Deployment, PodEvent, Service)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


_NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)


def _svc(db, name="town-service", ns="prod-kingdom1", *, node_kind=NODE_KIND_SERVICE):
    s = Service(name=name, namespace=ns, synthetic=False, node_kind=node_kind)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _pod_event(db, svc, reason, *, first_seen, last_seen=None, count=1):
    ev = PodEvent(
        service_id=svc.id,
        namespace=svc.namespace,
        pod_name=f"{svc.name}-abc123",
        reason=reason,
        message=f"Back-off restarting failed container ({reason})",
        type="Warning",
        event_uid=uuid.uuid4().hex,
        first_seen=first_seen,
        last_seen=last_seen,
        count=count,
    )
    db.add(ev)
    db.commit()
    return ev


def _deploy(db, svc, *, minutes_before, number="728", buildtype="Bt_BuildAndUpdate"):
    started = (_NOW - timedelta(minutes=minutes_before)).replace(tzinfo=None)
    d = Deployment(
        service_id=svc.id, sha="872a8dd", repo="new-wo/wo-k8s",
        buildtype_id=buildtype, build_number=number, started_at=started,
        finished_at=started + timedelta(minutes=2), status="SUCCESS",
        triggered_by="ybobryashov",
        # Маркер ns-broadcast — так пишет tc_deploys_to_kg.
        extras={"buildtype_name": "Build and update", "namespace_scope": True},
    )
    db.add(d)
    db.commit()
    return d


# ── 1. Pod trail видит хронический crashloop ────────────────────────────────


def test_pod_trail_sees_chronic_backoff_with_old_first_seen(db):
    """BackOff идёт неделю: first_seen вне окна, last_seen живой → total>0."""
    svc = _svc(db)
    naive_now = _NOW.replace(tzinfo=None)
    _pod_event(
        db, svc, "BackOff",
        first_seen=naive_now - timedelta(days=7),
        last_seen=naive_now - timedelta(minutes=2),
        count=1841,
    )

    trail = pod_event_summary_for(db, svc.namespace, svc.name, around=_NOW,
                                  window_minutes=60)
    assert trail["total"] == 1841, "хронический BackOff не попал в pod trail"
    assert trail["by_reason"] == [("BackOff", 1841)]


def test_pod_trail_ignores_event_that_ended_before_window(db):
    """Событие, закончившееся ДО окна, в окно не попадает (не ослабили фильтр)."""
    svc = _svc(db)
    naive_now = _NOW.replace(tzinfo=None)
    _pod_event(
        db, svc, "OOMKilled",
        first_seen=naive_now - timedelta(days=3),
        last_seen=naive_now - timedelta(days=2),
        count=5,
    )

    trail = pod_event_summary_for(db, svc.namespace, svc.name, around=_NOW,
                                  window_minutes=60)
    assert trail == {"total": 0, "by_reason": []}


def test_pod_trail_without_last_seen_falls_back_to_first_seen(db):
    """last_seen NULL (строки до k8s_events_sync) — поведение как раньше."""
    svc = _svc(db)
    naive_now = _NOW.replace(tzinfo=None)
    _pod_event(db, svc, "Unhealthy",
               first_seen=naive_now - timedelta(minutes=10), last_seen=None, count=3)
    _pod_event(db, svc, "FailedMount",
               first_seen=naive_now - timedelta(days=2), last_seen=None, count=2)

    trail = pod_event_summary_for(db, svc.namespace, svc.name, around=_NOW,
                                  window_minutes=60)
    assert trail["total"] == 3
    assert trail["by_reason"] == [("Unhealthy", 3)]


def test_recent_pod_events_ranks_chronic_by_last_activity(db):
    """Хроник с живым last_seen обязан попасть в limit и встать первым."""
    svc = _svc(db)
    naive_now = _NOW.replace(tzinfo=None)
    _pod_event(
        db, svc, "BackOff",
        first_seen=naive_now - timedelta(days=7),
        last_seen=naive_now - timedelta(minutes=1),
        count=1841,
    )
    _pod_event(
        db, svc, "Unhealthy",
        first_seen=naive_now - timedelta(minutes=30),
        last_seen=naive_now - timedelta(minutes=30),
        count=1,
    )

    events = recent_pod_events_for(db, svc.namespace, svc.name, around=_NOW,
                                   window_minutes=60, limit=1)
    assert len(events) == 1
    assert events[0]["reason"] == "BackOff"
    # minutes_before — от НАЧАЛА события (7 дней), minutes_since_last — от
    # последней активности (минута назад).
    assert events[0]["minutes_before"] == 7 * 24 * 60
    assert events[0]["minutes_since_last"] == 1


# ── 2. Один TC-билд не двоится в ns-level атрибуции ─────────────────────────


def test_recent_deploys_for_namespaces_collapses_ns_broadcast(db):
    """30 сервисов ns + workload-дубли, один билд → одна запись, не 60."""
    for i in range(30):
        svc = _svc(db, name=f"svc-{i}", ns="preprod-shared")
        _deploy(db, svc, minutes_before=8, number="728")
        wl = _svc(db, name=f"svc-{i}", ns="preprod-shared",
                  node_kind=NODE_KIND_WORKLOAD)
        _deploy(db, wl, minutes_before=8, number="728")

    out = recent_deploys_for_namespaces(
        db, ["preprod-shared"], before=_NOW, lookback_minutes=60, limit=5,
    )
    assert len(out) == 1, f"один билд вернулся {len(out)} раз"
    assert out[0]["number"] == "728"
    assert out[0]["namespace"] == "preprod-shared"
    assert out[0]["minutes_before_incident"] == 8


def test_recent_deploys_for_namespaces_limit_counts_distinct_builds(db):
    """`limit` тратится на РАЗНЫЕ билды, а не на копии одного."""
    services = [_svc(db, name=f"svc-{i}", ns="preprod-shared") for i in range(10)]
    for number, minutes in (("801", 30), ("802", 20), ("803", 10)):
        for svc in services:
            _deploy(db, svc, minutes_before=minutes, number=number)

    out = recent_deploys_for_namespaces(
        db, ["preprod-shared"], before=_NOW, lookback_minutes=60, limit=2,
    )
    assert [r["number"] for r in out] == ["803", "802"]


def test_recent_deploys_for_namespaces_keeps_deploys_without_build_info(db):
    """Записи без buildtype/number (record_deployment без TC) не схлопываются."""
    svc = _svc(db, ns="preprod-shared")
    naive_now = _NOW.replace(tzinfo=None)
    for minutes in (10, 20):
        db.add(Deployment(
            service_id=svc.id,
            started_at=naive_now - timedelta(minutes=minutes),
            status="SUCCESS",
        ))
    db.commit()

    out = recent_deploys_for_namespaces(
        db, ["preprod-shared"], before=_NOW, lookback_minutes=60, limit=5,
    )
    assert len(out) == 2


def test_cluster_deploy_activity_ignores_workload_duplicate(db):
    """Workload-дубль одноимённого сервиса не удваивает соседнюю активность."""
    svc = _svc(db, name="mv-service", ns="squad-gd-shared")
    _deploy(db, svc, minutes_before=9, number="900")
    wl = _svc(db, name="mv-service", ns="squad-gd-shared",
              node_kind=NODE_KIND_WORKLOAD)
    _deploy(db, wl, minutes_before=9, number="900")

    act = cluster_deploy_activity(
        db, sibling_prefixes=["squad-gd-", "prod-"],
        exclude_namespace="prod-shared", before=_NOW, lookback_minutes=60,
    )
    assert act["total_deploys"] == 1
    assert act["distinct_builds"] == 1
    assert act["namespaces"] == [{"namespace": "squad-gd-shared", "deploys": 1}]


# ── 3. Aware-datetime с не-UTC offset ───────────────────────────────────────

#: Тот же момент, что `_NOW`, но в +03:00 — так его отдаёт AlertManager.
_NOW_MSK = _NOW.astimezone(timezone(timedelta(hours=3)))


def test_ns_deploys_window_correct_for_non_utc_aware_before(db):
    """`before` с offset +03:00 не должен сдвигать окно на 3 часа.

    Раньше `.replace(tzinfo=None)` превращал 15:00+03:00 в naive 15:00, окно
    уезжало в будущее, и деплой за 8 минут до алерта выпадал из атрибуции —
    эмбед честно писал «деплоев не было».
    """
    svc = _svc(db, ns="preprod-shared")
    _deploy(db, svc, minutes_before=8, number="728")

    out = recent_deploys_for_namespaces(
        db, ["preprod-shared"], before=_NOW_MSK, lookback_minutes=60, limit=5,
    )
    assert len(out) == 1
    assert out[0]["minutes_before_incident"] == 8

    # Тот же деплой не должен «находиться» вне окна: сдвиг на 3 часа наружу.
    out_far = recent_deploys_for_namespaces(
        db, ["preprod-shared"],
        before=_NOW_MSK + timedelta(hours=4), lookback_minutes=60, limit=5,
    )
    assert out_far == []


def test_pod_trail_window_correct_for_non_utc_aware_around(db):
    """То же для pod trail: окно ±60м вокруг aware-времени с offset +03:00."""
    svc = _svc(db)
    naive_now = _NOW.replace(tzinfo=None)
    _pod_event(db, svc, "OOMKilled",
               first_seen=naive_now - timedelta(minutes=5),
               last_seen=naive_now - timedelta(minutes=5), count=2)

    trail = pod_event_summary_for(db, svc.namespace, svc.name, around=_NOW_MSK,
                                  window_minutes=60)
    assert trail["total"] == 2

    far = pod_event_summary_for(
        db, svc.namespace, svc.name,
        around=_NOW_MSK + timedelta(hours=4), window_minutes=60,
    )
    assert far == {"total": 0, "by_reason": []}


def test_cluster_activity_window_correct_for_non_utc_aware_before(db):
    svc = _svc(db, name="mv-service", ns="squad-gd-shared")
    _deploy(db, svc, minutes_before=9, number="900")

    act = cluster_deploy_activity(
        db, sibling_prefixes=["squad-gd-"], exclude_namespace="prod-shared",
        before=_NOW_MSK, lookback_minutes=60,
    )
    assert act["total_deploys"] == 1
    assert act["earliest_minutes_before"] == 9
