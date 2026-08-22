"""Деплой привязывается к тому, что он реально задеплоил.

Атрибуция шла по VCS-ветке, и она врала. Замер на проде 22.08.2026:

  * `BuildAndDeploy #2917` деплоил squad-1 (NAMESPACE=squad-1), а по ветке
    `<default>`→preprod осел на preprod-* и squad-gd-*: 596 записей, среди
    которых сервисов squad-1 не было НИ ОДНОГО;
  * `MigrateAndUpdateService #103` деплоил chat-message-service в squad-27 —
    и разошёлся по тем же 596 сервисам чужих окружений.

Итого 441 билд давал 124 911 записей, и в тех окружениях, куда деплоили,
деплоев не было видно вообще. Ровно так выглядит жалоба «пайплайн не
пополняет KG»: он пополнял, но не там.

Точный ответ лежал в параметрах билда: NAMESPACE есть у всех
deploy-конфигов, SERVICE_NAME — у тех, что деплоят один сервис.
"""
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.schema import Deployment, Service


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


def _build(**over):
    b = {
        "id": 179660, "number": "2917", "status": "SUCCESS",
        "branch": "<default>",
        "buildtype_id": "Wo_Backend_K8sNewCluster_BuildAndDeploy",
        "buildtype_name": "Build and update",
        "started_at": "2026-08-21T08:41:20",
        "finished_at": "2026-08-21T08:50:40",
        "triggered_by": "sgrozov", "sha": "a3124ff5",
        "all_revisions": [{"sha": "a3124ff5", "root": "wo-backend"}],
        "url": None, "target_realm": None, "target_service": None,
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


def test_deploy_lands_on_the_realm_it_targeted(db):
    """squad-1 задеплоили — записи должны быть у squad-1, а не у препрода."""
    mine = _svc(db, "town-service", "squad-1-shared")
    other = _svc(db, "town-service", "preprod-shared")
    db.commit()

    _run(db, [_build(target_realm="squad-1")])

    got = {
        (s.namespace, s.name)
        for s in db.query(Service).join(Deployment, Deployment.service_id == Service.id)
    }
    assert ("squad-1-shared", "town-service") in got
    assert ("preprod-shared", "town-service") not in got
    assert mine.id and other.id


def test_single_service_build_touches_only_that_service(db):
    """Конфиг деплоит один сервис — писать на все значит утверждать небылицы."""
    _svc(db, "chat-message-service", "squad-27-shared")
    _svc(db, "town-service", "squad-27-shared")
    db.commit()

    _run(db, [_build(
        buildtype_id="Wo_Backend_K8sNewCluster_MigrateAndUpdateService",
        number="103", target_realm="squad-27",
        target_service="chat-message-service",
    )])

    names = [
        s.name for s in
        db.query(Service).join(Deployment, Deployment.service_id == Service.id)
    ]
    assert names == ["chat-message-service"]


def test_realm_absent_from_graph_is_skipped_not_guessed(db):
    """Стенд снесён или не отсканирован — лучше ни одной записи, чем чужая.

    Падать на ветку здесь нельзя: она приписала бы деплой другому окружению,
    а неверный факт хуже отсутствующего.
    """
    _svc(db, "town-service", "preprod-shared")
    db.commit()

    stats = _run(db, [_build(target_realm="squad-99")])

    assert db.query(Deployment).count() == 0
    assert stats["skipped_no_realm"] == 1


def test_falls_back_to_branch_when_build_has_no_target(db):
    """Старые билды без параметров — прежнее поведение, чтобы не терять их."""
    _svc(db, "town-service", "preprod-shared")
    db.commit()

    stats = _run(db, [_build(branch="preprod", target_realm=None)])

    assert db.query(Deployment).count() == 1
    assert stats["by_attribution"]["vcs_branch"] == 1
    assert stats["by_attribution"]["build_param"] == 0


def test_kingdoms_of_the_realm_are_included(db):
    """Реальм разворачивается во все свои namespace, а не только в -shared."""
    _svc(db, "town-service", "squad-27-shared")
    _svc(db, "bot-service", "squad-27-kingdom2")
    _svc(db, "town-service", "squad-270-shared")   # чужой, похожий по префиксу
    db.commit()

    _run(db, [_build(target_realm="squad-27")])

    got = {
        s.namespace for s in
        db.query(Service).join(Deployment, Deployment.service_id == Service.id)
    }
    assert got == {"squad-27-shared", "squad-27-kingdom2"}


def test_rows_written_is_named_honestly(db):
    """Счётчик отражает записанные строки, а не «добавленные деплои».

    record_deployment делает on_conflict_do_update: на неизменном наборе
    билдов старый ключ показывал одно и то же большое число каждые 15 минут
    (4768 при восьми билдах), и по нему нельзя было понять, пополняется граф
    или просто перезаписывается.
    """
    _svc(db, "town-service", "squad-1-shared")
    db.commit()

    first = _run(db, [_build(target_realm="squad-1")])
    second = _run(db, [_build(target_realm="squad-1")])

    assert first["rows_written"] == 1
    assert second["rows_written"] == 1          # столько же — это перезапись
    assert db.query(Deployment).count() == 1    # а строк по-прежнему одна


def test_record_carries_where_its_attribution_came_from(db):
    """По данным должно быть видно, точная это привязка или догадка.

    Счётчик `by_attribution` живёт только в логах прогона. Без маркера в
    самой записи потребителю пришлось бы читать код, чтобы понять, можно ли
    ей доверять: `build_param` — цель из параметров билда, `vcs_branch` —
    вывод из ветки, которая у deploy-конфигов литеральный `<default>`.
    """
    _svc(db, "town-service", "squad-1-shared")
    _svc(db, "town-service", "preprod-shared")
    db.commit()

    _run(db, [
        _build(target_realm="squad-1"),
        _build(number="995", branch="preprod", target_realm=None),
    ])

    rows = db.query(Deployment).all()
    by_marker = {}
    for d in rows:
        svc = db.get(Service, d.service_id)
        by_marker[(svc.namespace, d.build_number)] = d.extras.get("attribution")

    assert by_marker[("squad-1-shared", "2917")] == "build_param"
    assert by_marker[("preprod-shared", "995")] == "vcs_branch"


def test_target_is_recorded_for_later_verification(db):
    """Цель сохраняется в записи — иначе проверить привязку нечем."""
    _svc(db, "chat-message-service", "squad-27-shared")
    db.commit()

    _run(db, [_build(
        buildtype_id="Wo_Backend_K8sNewCluster_MigrateAndUpdateService",
        number="103", target_realm="squad-27",
        target_service="chat-message-service",
    )])

    d = db.query(Deployment).one()
    assert d.extras["target_realm"] == "squad-27"
    assert d.extras["target_service"] == "chat-message-service"
