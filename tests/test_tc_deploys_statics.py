"""Сборки статики — одна строка на (билд, namespace), а не на каждый сервис.

Замер 31.10.2026: `Wo_StaticsNewCluster_*` (10–15 сборок в день) давали
963 тыс. строк kg_deployments из 1,24 млн при 1487 билдах — каждая
раскладывалась ns-broadcast-ом на все ~665 сервисов preprod. Теперь строка
одна, на синтетическом узле `statics` namespace; потребители ns-свидетельства
(`recent_deploys_for`, `recent_deploys_for_namespaces`, stale-класс) видят её
как прежний broadcast.
"""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.queries import (recent_deploys_for,
                                         recent_deploys_for_namespaces)
from app.knowledge_graph.schema import (STATICS_NODE_NAME, Deployment,
                                        Service)
from app.services.teamcity_service import is_statics_buildtype


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


def _svc(db, name, ns):
    s = Service(name=name, namespace=ns, synthetic=False)
    db.add(s)
    db.flush()
    return s


_STARTED = datetime.utcnow() - timedelta(hours=1)
_FINISHED = _STARTED + timedelta(minutes=4)


def _build(**over):
    b = {
        "id": 207001, "number": "4411", "status": "SUCCESS",
        "branch": "<default>",
        "buildtype_id": "Wo_StaticsNewCluster_Preprod_DeployPreprodTerrain",
        "buildtype_name": "Deploy preprod terrain",
        "started_at": _STARTED.strftime("%Y-%m-%dT%H:%M:%S"),
        "finished_at": _FINISHED.strftime("%Y-%m-%dT%H:%M:%S"),
        "triggered_by": "gd-user", "sha": "0ddba11",
        "all_revisions": [{"sha": "0ddba11", "root": "wo-statics"}],
        "url": None, "target_realm": "preprod", "target_service": None,
    }
    b.update(over)
    return b


def _run(db, builds):
    from app.workers import tasks as m

    db.close = lambda: None   # type: ignore[method-assign]
    with patch.object(m, "SessionLocal", side_effect=lambda: db), \
         patch("app.services.teamcity_service.recent_deploys",
               AsyncMock(return_value=builds)), \
         patch.object(m.settings, "TC_URL", "https://tc.example"), \
         patch.object(m.settings, "TC_TOKEN", "x"):
        import asyncio
        return asyncio.run(m._tc_deploys_to_kg_logic())


def test_statics_predicate():
    assert is_statics_buildtype("Wo_StaticsNewCluster_Preprod_DeployPreprodTerrain")
    assert is_statics_buildtype("Wo_StaticsNewCluster_Gd_TestRebuildAndUpdateExistingStaticsFromGoogleSheets")
    assert not is_statics_buildtype("Wo_Backend_K8sNewCluster_BuildAndDeploy")
    assert not is_statics_buildtype(None)


def test_statics_build_is_one_row_per_namespace_on_anchor(db):
    for ns in ("preprod-shared", "preprod-kingdom2"):
        _svc(db, "town-service", ns)
        _svc(db, "bot-service", ns)
    db.commit()

    result = _run(db, [_build()])

    rows = db.query(Deployment, Service).join(Service, Deployment.service_id == Service.id).all()
    assert {(s.namespace, s.name) for _, s in rows} == {
        ("preprod-shared", STATICS_NODE_NAME),
        ("preprod-kingdom2", STATICS_NODE_NAME),
    }
    assert len(rows) == 2 and result["statics_rows"] == 2
    for d, s in rows:
        assert s.synthetic is True
        assert d.extras["namespace_scope"] is True
        assert d.extras["attribution"] == "build_param"


def test_non_statics_build_still_broadcasts(db):
    a = _svc(db, "town-service", "preprod-shared")
    b = _svc(db, "bot-service", "preprod-shared")
    db.commit()

    result = _run(db, [_build(buildtype_id="Wo_Backend_K8sNewCluster_BuildAndDeploy")])

    got = {d.service_id for d in db.query(Deployment)}
    assert got == {a.id, b.id}
    assert result["statics_rows"] == 0
    assert db.query(Service).filter_by(name=STATICS_NODE_NAME).count() == 0


def test_recent_deploys_for_service_sees_namespace_statics(db):
    _svc(db, "town-service", "preprod-kingdom2")
    db.commit()
    _run(db, [_build()])

    out = recent_deploys_for(
        db, "preprod-kingdom2", "town-service",
        before=_STARTED + timedelta(minutes=30), lookback_minutes=60,
    )
    assert len(out) == 1
    assert out[0]["number"] == "4411"
    assert out[0]["attribution_scope"] == "namespace"


def test_recent_deploys_for_namespaces_sees_statics_anchor(db):
    _svc(db, "town-service", "preprod-kingdom2")
    db.commit()
    _run(db, [_build()])

    out = recent_deploys_for_namespaces(
        db, ["preprod-kingdom2"], before=_STARTED + timedelta(minutes=30),
    )
    assert [d["number"] for d in out] == ["4411"]
    assert out[0]["name"] == STATICS_NODE_NAME


def test_stale_refresh_treats_statics_as_namespace_evidence(db):
    """ns-broadcast — свойство namespace: строка на якоре считается всем."""
    from app.knowledge_graph.kg_sync import _refresh_stale_class_for_namespace
    from app.knowledge_graph.stale_classifier import classify_stale_with_deploys

    svc = _svc(db, "town-service", "preprod-kingdom2")
    db.commit()
    _run(db, [_build()])

    _refresh_stale_class_for_namespace(db, "preprod-kingdom2")
    db.refresh(svc)

    anchor_row = (
        db.query(Deployment).join(Service, Deployment.service_id == Service.id)
        .filter(Service.name == STATICS_NODE_NAME).one()
    )
    expected = classify_stale_with_deploys(
        name="town-service", namespace="preprod-kingdom2", last_deploy_at=None,
        last_service_deploy_at=None, last_ns_deploy_at=anchor_row.started_at,
        team_owner=None, live_service_names=set(),
    )
    assert svc.stale_class == expected
