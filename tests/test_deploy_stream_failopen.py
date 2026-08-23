"""Мёртвый поток деплоев больше не выглядит как «деплоев не было».

Инцидент 2026-08-11. `tc_deploys_to_kg` перестал пополнять kg_deployments
10.08 в 07:47. Через сутки прошёл прод-релиз (мерж preupdate→prod, раскатка
по 7 королевствам + shared), и спустя 20 секунд после открытия прода прилетел
ProdRestartsSpike по 8 namespace — штатный шум роллинга. В карточке алерта
копилот написал: «Деплоев в prod-kingdom1… за 60м до алерта не было — вряд ли
связано с деплоем». Деплой был. Триаж получил ложноотрицательный вердикт.

Три fail-open ветки, которые это допустили:
  1. `recent_deploys()` тихо возвращал [] при незаданном TC_URL/TOKEN/проектах;
  2. `check_deploy_stream_ingestion` отвечал ok и при недоступном TC, и при
     нулевом ответе — «это вотчина отдельного мониторинга» (которого нет);
  3. рендер Discord трактовал пустой kg_deployments как «деплоя не было».

Тесты фиксируют новое поведение: молчание источника = «не знаю», а не «нет».
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.queries import deploy_stream_freshness
from app.knowledge_graph.schema import Deployment, Service
from app.knowledge_graph.self_health import check_deploy_stream_ingestion


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


def _mk_service(db, name="auth", ns="preprod-shared") -> Service:
    s = Service(name=name, namespace=ns, team_owner="platform")
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _patch_tc(monkeypatch, builds, *, configured: bool):
    import app.services.teamcity_service as tc

    async def _fake_recent(**_kw):
        return builds

    monkeypatch.setattr(tc, "recent_deploys", _fake_recent)
    monkeypatch.setattr(
        tc, "branch_for_namespace",
        lambda ns: "preprod" if ns == "preprod-shared" else None,
    )
    monkeypatch.setattr(
        tc, "tc_sync_config_status",
        lambda: {"configured": configured,
                 "reason": "" if configured else "TC_TOKEN пуст"},
    )


# ── watchdog: молчание настроенного источника = fail ─────────────────────

def test_quiet_period_without_deploys_is_not_a_failure(db, monkeypatch):
    """TC ответил, деплоев в окне нет — это факт, а не слепота синка.

    Раньше здесь стоял fail: `recent_deploys` отдавала пустой список и при
    отказе источника, и при тишине, так что чек вынужденно считал отказом
    любой ноль. Цена приближения — 23.08.2026 самопроверка подняла «ослепший
    синк: проверь TC_TOKEN», хотя TC отвечал, а последний деплой был 21.08 в
    16:14, за 36 часов до срабатывания. У проекта бывают выходные.

    Отказ источника теперь приходит исключением и проверяется отдельно
    (`test_ingestion_fails_when_source_is_unavailable`).
    """
    _mk_service(db)
    _patch_tc(monkeypatch, [], configured=True)
    r = check_deploy_stream_ingestion(db)
    assert r.status == "ok", r.detail
    assert r.detail["should_ingest"] == 0


def test_ingestion_fails_when_source_is_unavailable(db, monkeypatch):
    """А вот если TC не ответил — состояние деплоев неизвестно, это fail.

    Защита от инцидента 11.08.2026 сохранена: тогда поток деплоев стоял
    сутки, а чек докладывал ok, и Discord писал «деплоев не было — вряд ли
    связано с деплоем» на алерте через 20 секунд после прод-раскатки.
    """
    from app.services.teamcity_service import TeamCitySourceUnavailable

    _mk_service(db)

    async def _boom(*a, **kw):
        raise TeamCitySourceUnavailable("все проекты TC не ответили: Wo_Backend")

    monkeypatch.setattr("app.services.teamcity_service.recent_deploys", _boom)
    monkeypatch.setattr(
        "app.services.teamcity_service.tc_sync_config_status",
        lambda: {"configured": True, "reason": ""},
    )
    r = check_deploy_stream_ingestion(db)
    assert r.status == "fail"
    assert "TeamCitySourceUnavailable" in r.detail["error"]


def test_ingestion_warns_when_tc_not_configured(db, monkeypatch):
    """Выключенную интеграцию не путаем с поломкой: warn, а не fail и не ok."""
    _mk_service(db)
    _patch_tc(monkeypatch, [], configured=False)
    r = check_deploy_stream_ingestion(db)
    assert r.status == "warn"
    assert "TC_TOKEN пуст" in r.detail["reason"]


def test_ingestion_works_inside_running_event_loop(db, monkeypatch):
    """Чек вызывается из синхронного run_self_health_checks, а тот — изнутри
    asyncio.run(_kg_self_health_logic()). Раньше внутренний asyncio.run падал
    RuntimeError, чек уходил в except и рапортовал ok: проверка мёртвого
    ingestion сама была мертва всё время своего существования.
    """
    import asyncio

    svc = _mk_service(db)
    db.add(Deployment(service_id=svc.id, buildtype_id="BT", build_number="100",
                      started_at=datetime.now(timezone.utc).replace(tzinfo=None)))
    db.commit()
    _patch_tc(
        monkeypatch,
        [{"buildtype_id": "BT", "number": "100", "branch": "preprod"}],
        configured=True,
    )

    async def _inside_loop():
        return check_deploy_stream_ingestion(db)

    r = asyncio.run(_inside_loop())
    assert r.status == "ok"
    assert r.detail["present_in_kg"] == 1
    # Ключевое: не «TC unavailable: RuntimeError», а реальный вердикт.
    assert "error" not in r.detail


def test_ingestion_fails_when_tc_raises_and_configured(db, monkeypatch):
    """Протухший токен даёт 401 → исключение. Раньше это был ok/skip."""
    import app.services.teamcity_service as tc

    async def _boom(**_kw):
        raise RuntimeError("401 Unauthorized")

    _mk_service(db)
    _patch_tc(monkeypatch, [], configured=True)
    monkeypatch.setattr(tc, "recent_deploys", _boom)
    r = check_deploy_stream_ingestion(db)
    assert r.status == "fail"
    assert "401" in r.detail["error"]


# ── freshness: «не знаю» отличается от «не было» ─────────────────────────

def test_freshness_stale_when_stream_silent_for_a_day(db):
    svc = _mk_service(db)
    now = datetime.now(timezone.utc)
    db.add(Deployment(
        service_id=svc.id, buildtype_id="BT", build_number="1",
        started_at=(now - timedelta(hours=30)).replace(tzinfo=None),
    ))
    db.commit()
    out = deploy_stream_freshness(db, before=now)
    assert out["stale"] is True
    assert out["age_hours"] >= 29


def test_freshness_not_stale_at_night_gap(db):
    """Порог щедрый: 4 часа без деплоев — норма, ложный stale хуже молчания."""
    svc = _mk_service(db)
    now = datetime.now(timezone.utc)
    db.add(Deployment(
        service_id=svc.id, buildtype_id="BT", build_number="2",
        started_at=(now - timedelta(hours=4)).replace(tzinfo=None),
    ))
    db.commit()
    assert deploy_stream_freshness(db, before=now)["stale"] is False


def test_freshness_stale_when_table_empty(db):
    out = deploy_stream_freshness(db, before=datetime.now(timezone.utc))
    assert out["stale"] is True
    assert out["last_at"] is None


# ── атрибуция прод-джоб: окружение задаёт проект, а не ветка ────────────

def test_prod_buildtype_detected_by_project_prefix():
    from app.services.teamcity_service import is_prod_buildtype

    assert is_prod_buildtype("Wo_Backend_K8sNewCluster_Prod_BackupAllDb")
    assert is_prod_buildtype("Wo_StaticsNewCluster_Prod_DeployStatics")
    assert is_prod_buildtype("Wo_Backend_K8sNewCluster_Prod")
    # preprod-конфиги не должны утечь в prod-ветку атрибуции
    assert not is_prod_buildtype("Wo_Backend_K8sNewCluster_BuildAndUpdate")
    assert not is_prod_buildtype("Wo_StaticsNewCluster_Preprod_DeployStatics")
    assert not is_prod_buildtype(None)


def test_prod_build_on_preprod_branch_attributed_to_prod(db, monkeypatch):
    """Prod_BackupAllDb #40 идёт с refs/heads/prepod — но это прод-деплой.

    Инцидент 2026-08-11: бэкап прод-БД перед релизом осел на squad-gd-shared/*
    (у squad-gd правило ветки — preprod, как и у самого билда), а prod-* остался
    без deploy-истории. Чек должен считать такой билд prod-ветковым.
    """
    _mk_service(db, "town-service", "prod-kingdom1")
    db.commit()
    import app.services.teamcity_service as tc

    async def _fake_recent(**_kw):
        return [{
            "buildtype_id": "Wo_Backend_K8sNewCluster_Prod_BackupAllDb",
            "number": "40", "branch": "refs/heads/preprod",
        }]

    monkeypatch.setattr(tc, "recent_deploys", _fake_recent)
    monkeypatch.setattr(
        tc, "tc_sync_config_status", lambda: {"configured": True, "reason": ""},
    )
    r = check_deploy_stream_ingestion(db)
    # Билд считается «должен быть в KG» для prod-ns, а его там нет → fail.
    assert r.status == "fail"
    assert r.detail["should_ingest"] == 1


# ── рендер: формулировка при мёртвом источнике ──────────────────────────

def _dep_field(fields: dict) -> str:
    return next(v for k, v in fields.items() if k.startswith("Deploy-связь"))


def test_embed_says_no_data_when_stream_stale():
    from tests.test_ns_deploy_attribution import _build_fields, _ns_ctx

    ctx = _ns_ctx("prod-kingdom1", [])
    ctx.deploy_stream = {
        "last_at": datetime(2026, 8, 10, 7, 47),
        "age_hours": 31.9,
        "stale": True,
    }
    value = _dep_field(_build_fields([ctx]))
    assert "Данных о деплоях нет" in value
    assert "10.08 07:47" in value
    # Главное: запрещённый вердикт не проскакивает.
    assert "вряд ли связано" not in value


def test_embed_keeps_negative_verdict_when_stream_healthy():
    """Живой поток + пусто в ns = честное «деплоя не было». Регресс-гарантия."""
    from tests.test_ns_deploy_attribution import _build_fields, _ns_ctx

    ctx = _ns_ctx("prod-kingdom1", [])
    ctx.deploy_stream = {"last_at": datetime.utcnow(), "age_hours": 0.5,
                         "stale": False}
    value = _dep_field(_build_fields([ctx]))
    assert "вряд ли связано" in value
