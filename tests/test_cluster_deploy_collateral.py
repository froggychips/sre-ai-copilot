"""Cross-namespace deploy collateral (инцидент ProdEndpointDown 2026-06-15).

Per-namespace deploy attribution слепа к bulk-rollout в СОСЕДНИХ ns одного
физического кластера: два конкурентных BuildAndUpdate уронили prod-shared
через image-pull/CRI pressure, хотя в prod-shared деплоя не было. Атрибуция
сказала «деплоев не было — вряд ли связано», что ложно.

queries.cluster_deploy_activity отвечает на «а каталось ли что-то рядом, на
том же железе» — агрегат по соседним ns кластера за окно.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.queries import cluster_deploy_activity
from app.knowledge_graph.schema import Deployment, Service


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


_INCIDENT_AT = datetime(2026, 6, 15, 13, 56, 0, tzinfo=timezone.utc)


def _svc(db, name, namespace):
    s = Service(name=name, namespace=namespace, synthetic=False)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _deploy(db, svc_id, *, minutes_before, buildtype="Bt_BuildAndUpdate",
            number="728", who="ybobryashov"):
    started = (_INCIDENT_AT - timedelta(minutes=minutes_before)).replace(tzinfo=None)
    d = Deployment(
        service_id=svc_id, sha="872a8dd", repo="new-wo/wo-k8s",
        buildtype_id=buildtype, build_number=number, started_at=started,
        finished_at=started + timedelta(minutes=2), status="SUCCESS",
        triggered_by=who, extras={"buildtype_name": "Build and update"},
    )
    db.add(d)
    db.commit()
    return d


_NEW_CLUSTER = ["prod-", "preprod-", "preupdate-", "squad-gd-"]


def test_empty_when_no_sibling_deploys(db):
    _svc(db, "auth-service", "prod-shared")  # свой ns, без деплоя
    act = cluster_deploy_activity(
        db, sibling_prefixes=_NEW_CLUSTER, exclude_namespace="prod-shared",
        before=_INCIDENT_AT, lookback_minutes=60,
    )
    assert act == {}


def test_empty_when_prefixes_empty(db):
    sib = _svc(db, "mv-service", "squad-gd-shared")
    _deploy(db, sib.id, minutes_before=8)
    assert cluster_deploy_activity(
        db, sibling_prefixes=[], exclude_namespace="prod-shared",
        before=_INCIDENT_AT, lookback_minutes=60,
    ) == {}


def test_excludes_own_namespace(db):
    own = _svc(db, "admin-service", "prod-shared")
    _deploy(db, own.id, minutes_before=8)  # деплой в СВОЁМ ns — не collateral
    act = cluster_deploy_activity(
        db, sibling_prefixes=_NEW_CLUSTER, exclude_namespace="prod-shared",
        before=_INCIDENT_AT, lookback_minutes=60,
    )
    assert act == {}


def test_aggregates_concurrent_sibling_rollout(db):
    # Воспроизводим инцидент: bulk-rollout в squad-gd-shared (#728) и
    # preprod-shared (#727) пока prod-shared (свой ns) без деплоя.
    # По 3 и 2 сервиса на билд — это ns-broadcast `tc_deploys_to_kg`, один и
    # тот же билд размножен по узлам ns, а не 5 разных деплоев.
    for i in range(3):
        s = _svc(db, f"svc-gd-{i}", "squad-gd-shared")
        _deploy(db, s.id, minutes_before=10, number="728")
    for i in range(2):
        s = _svc(db, f"svc-pp-{i}", "preprod-shared")
        _deploy(db, s.id, minutes_before=7, number="727")

    act = cluster_deploy_activity(
        db, sibling_prefixes=_NEW_CLUSTER, exclude_namespace="prod-shared",
        before=_INCIDENT_AT, lookback_minutes=60,
    )
    # Два ns-деплоя (#728 в squad-gd-shared, #727 в preprod-shared), а не 5
    # строк kg_deployments: ns-broadcast схлопнут по (ns, buildtype, number).
    assert act["total_deploys"] == 2
    assert act["distinct_builds"] == 2
    assert {"namespace": "squad-gd-shared", "deploys": 1} in act["namespaces"]
    assert {"namespace": "preprod-shared", "deploys": 1} in act["namespaces"]
    # ближайший деплой — preprod (7м) < gd (10м).
    assert act["earliest_minutes_before"] == 7
    # sample dedup по (buildtype, number): 2 уникальных билда.
    assert len(act["sample_builds"]) == 2
    assert act["sample_builds"][0]["minutes_before_incident"] == 7
    assert act["sample_builds"][0]["namespace"] == "preprod-shared"


def test_one_build_across_two_namespaces_is_two_ns_deploys(db):
    """Один билд, прокатившийся по двум соседним ns = 2 ns-деплоя, 1 билд.

    Регрессия: без дедупа по (ns, билд) `total_deploys` считал СТРОКИ
    kg_deployments и на ns-broadcast врал в разы («60 deploys» вместо
    «1 билд»); честен был только `distinct_builds`.
    """
    for i in range(10):
        s = _svc(db, f"svc-gd-{i}", "squad-gd-shared")
        _deploy(db, s.id, minutes_before=9, number="900")
    for i in range(10):
        s = _svc(db, f"svc-pp-{i}", "preprod-shared")
        _deploy(db, s.id, minutes_before=9, number="900")

    act = cluster_deploy_activity(
        db, sibling_prefixes=_NEW_CLUSTER, exclude_namespace="prod-shared",
        before=_INCIDENT_AT, lookback_minutes=60,
    )
    assert act["total_deploys"] == 2, "20 строк ns-broadcast = 2 ns-деплоя"
    assert act["distinct_builds"] == 1
    assert act["namespaces"] == [
        {"namespace": "squad-gd-shared", "deploys": 1},
        {"namespace": "preprod-shared", "deploys": 1},
    ] or act["namespaces"] == [
        {"namespace": "preprod-shared", "deploys": 1},
        {"namespace": "squad-gd-shared", "deploys": 1},
    ]
    # В примерах билд показываем один раз — он один и тот же.
    assert len(act["sample_builds"]) == 1


def test_two_different_builds_in_one_namespace_both_counted(db):
    """Дедуп не съедает РАЗНЫЕ билды в одном ns."""
    for i in range(3):
        s = _svc(db, f"svc-gd-{i}", "squad-gd-shared")
        _deploy(db, s.id, minutes_before=20, number="801")
        _deploy(db, s.id, minutes_before=5, number="802")

    act = cluster_deploy_activity(
        db, sibling_prefixes=_NEW_CLUSTER, exclude_namespace="prod-shared",
        before=_INCIDENT_AT, lookback_minutes=60,
    )
    assert act["total_deploys"] == 2
    assert act["distinct_builds"] == 2
    assert act["namespaces"] == [{"namespace": "squad-gd-shared", "deploys": 2}]
    assert act["earliest_minutes_before"] == 5


def test_window_excludes_old_deploys(db):
    s = _svc(db, "svc-gd", "squad-gd-shared")
    _deploy(db, s.id, minutes_before=120)  # вне 60м окна
    act = cluster_deploy_activity(
        db, sibling_prefixes=_NEW_CLUSTER, exclude_namespace="prod-shared",
        before=_INCIDENT_AT, lookback_minutes=60,
    )
    assert act == {}
