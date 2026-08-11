"""Тесты KG Coverage #4: ``kg_services.stale_class`` column.

Покрытие:

* ``stale_classifier._classify_stale`` — legacy 2-value эвристика (backup/cron/
  system/regular).
* ``stale_classifier.classify_stale_with_deploys`` — 3-классная (active/
  expected_stale/suspicious_stale) с разными комбинациями last_deploy_at,
  team_owner, name/ns.
* Атрибуция деплоя: ns-broadcast (`extras.namespace_scope`) сам по себе
  ``active`` НЕ даёт — иначе классификатор отвечает «был ли деплой в ns», а
  не «катился ли ЭТОТ сервис».
* ``kg_sync.sync_namespace`` populates ``stale_class`` on upsert (idempotent).
* ``queries.services_by_stale_class`` фильтрует по column.
* ``stats_digest.stale_deployments_section`` читает column как primary source
  и не зовёт runtime ``_classify_stale`` при заполненном column.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.queries import services_by_stale_class
from app.knowledge_graph.schema import Deployment, Service
from app.knowledge_graph.stale_classifier import (
    STALE_CLASS_ACTIVE,
    STALE_CLASS_EXPECTED,
    STALE_CLASS_SUSPICIOUS,
    _classify_stale,
    classify_stale_with_deploys,
    is_ns_broadcast_deploy,
)


# ── fixtures ────────────────────────────────────────────────────────────────


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


# ── stale_classifier._classify_stale (legacy 2-value) ───────────────────────


def test_classify_stale_backup_suffix_is_expected():
    assert _classify_stale("postgres-backup", "prod-kingdom1") == "expected"
    assert _classify_stale("town-db-backup", "prod-shared") == "expected"


def test_classify_stale_cron_suffix_is_expected():
    assert _classify_stale("etcd-snapshot-cronjob", "prod-shared") == "expected"
    assert _classify_stale("nightly-cron", "prod-kingdom1") == "expected"


def test_classify_stale_system_namespace_is_expected():
    assert _classify_stale("coredns", "kube-system") == "expected"
    assert _classify_stale("prometheus", "monitoring") == "expected"
    assert _classify_stale("cert-manager-webhook", "cert-manager") == "expected"


def test_classify_stale_cattle_namespace_is_expected():
    assert _classify_stale("rancher-agent", "cattle-system") == "expected"
    assert _classify_stale("anything", "cattle-fleet-local") == "expected"


def test_classify_stale_regular_app_is_suspicious():
    assert _classify_stale("town-service", "prod-kingdom1") == "suspicious"
    assert _classify_stale("auth", "preprod-shared") == "suspicious"


def test_classify_stale_backup_infix_is_expected():
    # `backup-postgresql`, `chat-messages-additional-backup-restore`
    assert _classify_stale("backup-postgresql", "prod-shared") == "expected"
    assert _classify_stale("chat-backup-restore", "prod-kingdom2") == "expected"


# ── classify_stale_with_deploys (3-class) ───────────────────────────────────


def test_classify_3class_active_recent_deploy():
    now = datetime(2026, 5, 24, 12, 0, 0)
    one_day_ago = now - timedelta(days=1)
    assert (
        classify_stale_with_deploys(
            "town-service", "prod-kingdom1", one_day_ago, now=now
        )
        == STALE_CLASS_ACTIVE
    )


def test_classify_3class_expected_backup_old_deploy():
    """Backup deployment не катился 45d → expected_stale (имя матчит)."""
    now = datetime(2026, 5, 24, 12, 0, 0)
    forty_five_days_ago = now - timedelta(days=45)
    assert (
        classify_stale_with_deploys(
            "town-db-backup", "prod-shared", forty_five_days_ago, now=now
        )
        == STALE_CLASS_EXPECTED
    )


def test_classify_3class_suspicious_old_deploy_app():
    """Регулярный сервис, 90d без деплоя → suspicious_stale."""
    now = datetime(2026, 5, 24, 12, 0, 0)
    ninety_days_ago = now - timedelta(days=90)
    assert (
        classify_stale_with_deploys(
            "town-service", "prod-kingdom1", ninety_days_ago, now=now
        )
        == STALE_CLASS_SUSPICIOUS
    )


def test_classify_3class_expected_system_namespace():
    """coredns в kube-system без deploy → expected_stale (ns матчит)."""
    now = datetime(2026, 5, 24, 12, 0, 0)
    old = now - timedelta(days=120)
    assert (
        classify_stale_with_deploys("coredns", "kube-system", old, now=now)
        == STALE_CLASS_EXPECTED
    )


def test_classify_3class_no_deploys_app_is_suspicious():
    """Сервис без deploy в KG, не backup/system → suspicious_stale."""
    assert (
        classify_stale_with_deploys(
            "town-service",
            "prod-kingdom1",
            None,
            now=datetime(2026, 5, 24, 12, 0, 0),
        )
        == STALE_CLASS_SUSPICIOUS
    )


def test_classify_3class_no_deploys_backup_is_expected():
    """Сервис без deploy, но имя backup → expected_stale."""
    assert (
        classify_stale_with_deploys(
            "town-db-backup",
            "prod-shared",
            None,
            now=datetime(2026, 5, 24, 12, 0, 0),
        )
        == STALE_CLASS_EXPECTED
    )


def test_classify_3class_infra_owner_old_deploy_is_expected():
    """team_owner=platform + 45d без деплоя → expected_stale (infra grace)."""
    now = datetime(2026, 5, 24, 12, 0, 0)
    forty_five_days_ago = now - timedelta(days=45)
    assert (
        classify_stale_with_deploys(
            "vmagent",
            "prod-kingdom1",
            forty_five_days_ago,
            team_owner="platform",
            now=now,
        )
        == STALE_CLASS_EXPECTED
    )


def test_classify_3class_aware_datetime_ok():
    """Если last_deploy_at — timezone-aware, всё равно норм сравнивается."""
    now = datetime(2026, 5, 24, 12, 0, 0)
    one_day_ago_aware = (now - timedelta(days=1)).replace(tzinfo=timezone.utc)
    assert (
        classify_stale_with_deploys(
            "town-service", "prod-kingdom1", one_day_ago_aware, now=now
        )
        == STALE_CLASS_ACTIVE
    )


# ── ns-broadcast vs свой деплой (атрибуция) ─────────────────────────────────


def test_is_ns_broadcast_deploy_marker():
    """Единственный признак в данных — `extras.namespace_scope`."""
    assert is_ns_broadcast_deploy({"namespace_scope": True, "branch": "preprod"})
    assert not is_ns_broadcast_deploy({"branch": "preprod"})
    assert not is_ns_broadcast_deploy(None)
    assert not is_ns_broadcast_deploy("не dict")


def test_ns_broadcast_alone_is_not_active():
    """Деплой соседа по ns не делает сервис `active`.

    Регрессия: `last_deploy_at` = max по kg_deployments, а ns-broadcast пишет
    билд ВСЕМ узлам ns → в активно деплоящемся namespace все сервисы вечно
    `active`, и классификатор отвечал «был ли деплой в ns», а не «катился ли
    ЭТОТ сервис». `suspicious_stale` при таком входе не мог сработать.
    """
    now = datetime(2026, 8, 10, 12, 0, 0)
    yesterday = now - timedelta(days=1)
    assert (
        classify_stale_with_deploys(
            "town-service", "prod-kingdom1", yesterday,
            now=now, last_ns_deploy_at=yesterday,
        )
        == STALE_CLASS_SUSPICIOUS
    )


def test_own_deploy_is_active_even_with_ns_broadcast():
    """Есть своя запись (без маркера ns_scope) → честный `active`."""
    now = datetime(2026, 8, 10, 12, 0, 0)
    yesterday = now - timedelta(days=1)
    assert (
        classify_stale_with_deploys(
            "town-service", "prod-kingdom1", yesterday,
            now=now,
            last_service_deploy_at=now - timedelta(days=3),
            last_ns_deploy_at=yesterday,
        )
        == STALE_CLASS_ACTIVE
    )


def test_own_deploy_stale_while_namespace_rolls_is_suspicious():
    """Свой деплой 90d назад, ns катится ежедневно → suspicious_stale."""
    now = datetime(2026, 8, 10, 12, 0, 0)
    assert (
        classify_stale_with_deploys(
            "town-service", "prod-kingdom1", now - timedelta(days=1),
            now=now,
            last_service_deploy_at=now - timedelta(days=90),
            last_ns_deploy_at=now - timedelta(days=1),
        )
        == STALE_CLASS_SUSPICIOUS
    )


def test_ns_broadcast_does_not_override_expected_shape():
    """Backup/system-форма остаётся expected_stale, не обвиняем её."""
    now = datetime(2026, 8, 10, 12, 0, 0)
    yesterday = now - timedelta(days=1)
    assert (
        classify_stale_with_deploys(
            "town-db-backup", "prod-shared", yesterday,
            now=now, last_ns_deploy_at=yesterday,
        )
        == STALE_CLASS_EXPECTED
    )
    # infra-owner тоже сохраняет снисхождение: ns-деплой свежее infra-окна.
    assert (
        classify_stale_with_deploys(
            "vmagent", "prod-kingdom1", yesterday,
            team_owner="platform", now=now, last_ns_deploy_at=yesterday,
        )
        == STALE_CLASS_EXPECTED
    )


def test_merged_attribution_keeps_legacy_active():
    """Слитый вход (только last_deploy_at) — прежнее поведение.

    Осознанная деградация: разделить доказательства может только вызывающий
    (у него на руках `Deployment.extras`), а молча объявить подозрительными
    все сервисы активно деплоящихся ns — обвинение на масштабе.
    """
    now = datetime(2026, 8, 10, 12, 0, 0)
    assert (
        classify_stale_with_deploys(
            "town-service", "prod-kingdom1", now - timedelta(days=1), now=now,
        )
        == STALE_CLASS_ACTIVE
    )


# ── kg_sync populate stale_class on upsert ──────────────────────────────────


def _make_deploy_doc(name: str, env_vars=None):
    """Минимальный k8s deployment JSON для sync_namespace."""
    return {
        "metadata": {
            "name": name,
            "labels": {},
        },
        "spec": {
            "template": {
                "spec": {
                    "containers": [{"env": env_vars or []}],
                },
            },
        },
        "status": {"readyReplicas": 1},
    }


def _add_deploy_record(db, svc: Service, started_at: datetime, *, ns_broadcast=False):
    """Добавить запись в kg_deployments.

    ``ns_broadcast=True`` ставит маркер ``extras.namespace_scope``, которым
    ``tc_deploys_to_kg`` помечает рассылку одного TC-билда на ВСЕ сервисы ns.
    """
    d = Deployment(
        service_id=svc.id,
        sha="abc123",
        started_at=started_at,
        status="SUCCESS",
        extras={"namespace_scope": True} if ns_broadcast else None,
    )
    db.add(d)
    db.flush()


def test_sync_namespace_populates_active_for_recent_deploy(db):
    """sync_namespace → stale_class='active' для свежего deploy."""
    from app.knowledge_graph import kg_sync

    # 1) Preseed: sync создаёт kg_services row
    deploy_doc = _make_deploy_doc("town-service")
    with patch.object(kg_sync, "_kubectl_get_deployments", return_value=[deploy_doc]):
        kg_sync.sync_namespace(db, "prod-kingdom1")

    svc = db.query(Service).filter_by(
        namespace="prod-kingdom1", name="town-service"
    ).one()

    # 2) Добавляем свежий deploy
    _add_deploy_record(db, svc, datetime.utcnow() - timedelta(days=1))

    # 3) Повторный sync — должен переписать stale_class='active'
    with patch.object(kg_sync, "_kubectl_get_deployments", return_value=[deploy_doc]):
        kg_sync.sync_namespace(db, "prod-kingdom1")

    db.refresh(svc)
    assert svc.stale_class == STALE_CLASS_ACTIVE


def test_sync_namespace_populates_expected_for_backup_old_deploy(db):
    """`*-backup` имя + старый deploy (45d) → expected_stale."""
    from app.knowledge_graph import kg_sync

    deploy_doc = _make_deploy_doc("town-db-backup")
    with patch.object(kg_sync, "_kubectl_get_deployments", return_value=[deploy_doc]):
        kg_sync.sync_namespace(db, "prod-shared")

    svc = db.query(Service).filter_by(
        namespace="prod-shared", name="town-db-backup"
    ).one()
    _add_deploy_record(db, svc, datetime.utcnow() - timedelta(days=45))

    with patch.object(kg_sync, "_kubectl_get_deployments", return_value=[deploy_doc]):
        kg_sync.sync_namespace(db, "prod-shared")

    db.refresh(svc)
    assert svc.stale_class == STALE_CLASS_EXPECTED


def test_sync_namespace_populates_suspicious_for_old_app_deploy(db):
    """Регулярный сервис + 90d без деплоя → suspicious_stale."""
    from app.knowledge_graph import kg_sync

    deploy_doc = _make_deploy_doc("auth")
    with patch.object(kg_sync, "_kubectl_get_deployments", return_value=[deploy_doc]):
        kg_sync.sync_namespace(db, "prod-kingdom1")

    svc = db.query(Service).filter_by(
        namespace="prod-kingdom1", name="auth"
    ).one()
    _add_deploy_record(db, svc, datetime.utcnow() - timedelta(days=90))

    with patch.object(kg_sync, "_kubectl_get_deployments", return_value=[deploy_doc]):
        kg_sync.sync_namespace(db, "prod-kingdom1")

    db.refresh(svc)
    assert svc.stale_class == STALE_CLASS_SUSPICIOUS


def test_sync_namespace_ns_broadcast_does_not_mask_stale_service(db):
    """ns-broadcast свежий + свой деплой 90d назад → suspicious_stale.

    Регрессия на разделение доказательств в
    ``kg_sync._refresh_stale_class_for_namespace``: раньше здесь считался
    ОДИН слитый max(started_at), поэтому рассылка TC-билда на все сервисы
    namespace делала каждый сервис активно деплоящегося ns вечно ``active``,
    и ``suspicious_stale`` не мог сработать именно там, где нужен.
    """
    from app.knowledge_graph import kg_sync

    deploy_doc = _make_deploy_doc("auth")
    with patch.object(kg_sync, "_kubectl_get_deployments", return_value=[deploy_doc]):
        kg_sync.sync_namespace(db, "prod-kingdom1")

    svc = db.query(Service).filter_by(namespace="prod-kingdom1", name="auth").one()
    # Свой деплой давно, а ns катится прямо сейчас (broadcast-запись свежая).
    _add_deploy_record(db, svc, datetime.utcnow() - timedelta(days=90))
    _add_deploy_record(
        db, svc, datetime.utcnow() - timedelta(hours=2), ns_broadcast=True
    )

    with patch.object(kg_sync, "_kubectl_get_deployments", return_value=[deploy_doc]):
        kg_sync.sync_namespace(db, "prod-kingdom1")

    db.refresh(svc)
    assert svc.stale_class == STALE_CLASS_SUSPICIOUS


def test_sync_namespace_own_deploy_wins_over_ns_broadcast(db):
    """Свой свежий деплой → active, даже если рядом есть ns-broadcast."""
    from app.knowledge_graph import kg_sync

    deploy_doc = _make_deploy_doc("auth")
    with patch.object(kg_sync, "_kubectl_get_deployments", return_value=[deploy_doc]):
        kg_sync.sync_namespace(db, "prod-kingdom1")

    svc = db.query(Service).filter_by(namespace="prod-kingdom1", name="auth").one()
    _add_deploy_record(db, svc, datetime.utcnow() - timedelta(hours=3))
    _add_deploy_record(
        db, svc, datetime.utcnow() - timedelta(hours=1), ns_broadcast=True
    )

    with patch.object(kg_sync, "_kubectl_get_deployments", return_value=[deploy_doc]):
        kg_sync.sync_namespace(db, "prod-kingdom1")

    db.refresh(svc)
    assert svc.stale_class == STALE_CLASS_ACTIVE


def test_sync_namespace_idempotent_reclassifies(db):
    """Повторный sync re-classifies (idempotent)."""
    from app.knowledge_graph import kg_sync

    deploy_doc = _make_deploy_doc("town-service")
    with patch.object(kg_sync, "_kubectl_get_deployments", return_value=[deploy_doc]):
        kg_sync.sync_namespace(db, "prod-kingdom1")
    svc = db.query(Service).filter_by(
        namespace="prod-kingdom1", name="town-service"
    ).one()

    # Сначала — старый deploy (90d) → suspicious
    _add_deploy_record(db, svc, datetime.utcnow() - timedelta(days=90))
    with patch.object(kg_sync, "_kubectl_get_deployments", return_value=[deploy_doc]):
        kg_sync.sync_namespace(db, "prod-kingdom1")
    db.refresh(svc)
    assert svc.stale_class == STALE_CLASS_SUSPICIOUS

    # Затем — свежий deploy → active
    _add_deploy_record(db, svc, datetime.utcnow() - timedelta(days=1))
    with patch.object(kg_sync, "_kubectl_get_deployments", return_value=[deploy_doc]):
        kg_sync.sync_namespace(db, "prod-kingdom1")
    db.refresh(svc)
    assert svc.stale_class == STALE_CLASS_ACTIVE


# ── queries.services_by_stale_class ─────────────────────────────────────────


def test_services_by_stale_class_filters(db):
    """services_by_stale_class возвращает только row-ы с матчингом."""
    s1 = Service(
        name="active-svc", namespace="prod-kingdom1",
        stale_class=STALE_CLASS_ACTIVE,
    )
    s2 = Service(
        name="suspicious-svc", namespace="prod-kingdom1",
        stale_class=STALE_CLASS_SUSPICIOUS,
    )
    s3 = Service(
        name="backup-svc", namespace="prod-shared",
        stale_class=STALE_CLASS_EXPECTED,
    )
    db.add_all([s1, s2, s3])
    db.flush()

    suspicious = services_by_stale_class(db, STALE_CLASS_SUSPICIOUS)
    assert len(suspicious) == 1
    assert suspicious[0].name == "suspicious-svc"

    active = services_by_stale_class(db, STALE_CLASS_ACTIVE)
    assert len(active) == 1
    assert active[0].name == "active-svc"

    expected = services_by_stale_class(db, STALE_CLASS_EXPECTED)
    assert len(expected) == 1
    assert expected[0].name == "backup-svc"


def test_services_by_stale_class_namespace_scope(db):
    s1 = Service(
        name="x", namespace="ns-a", stale_class=STALE_CLASS_SUSPICIOUS,
    )
    s2 = Service(
        name="x", namespace="ns-b", stale_class=STALE_CLASS_SUSPICIOUS,
    )
    db.add_all([s1, s2])
    db.flush()

    in_a = services_by_stale_class(db, STALE_CLASS_SUSPICIOUS, namespace="ns-a")
    assert len(in_a) == 1
    assert in_a[0].namespace == "ns-a"


# ── stats_digest reads kg_services.stale_class ──────────────────────────────


def test_stats_digest_uses_column_when_populated(db):
    """stats_digest.stale_deployments_section использует stale_class из DB,
    а не runtime _classify_stale.

    Тест-приём: создаём в kg_services row с именем `town-service` (нормальное)
    но с column ``stale_class='expected_stale'``. Если digest читает column —
    deployment скрывается. Если зовёт runtime `_classify_stale("town-service",
    "ns")` — тот вернёт `suspicious` и не скроет.
    """
    from app.services import stats_digest

    svc = Service(
        name="town-service",
        namespace="prod-kingdom1",
        stale_class=STALE_CLASS_EXPECTED,  # ← маркируем как expected явно
    )
    db.add(svc)
    db.flush()

    # kubectl_fn возвращает deployment 60d idle
    sixty_days_ago = (datetime.utcnow() - timedelta(days=60)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    deploy = {
        "metadata": {
            "name": "town-service",
            "annotations": {
                "deployment.kubernetes.io/revision-history": sixty_days_ago,
            },
            "creationTimestamp": sixty_days_ago,
        },
        "status": {
            "readyReplicas": 1,
            "conditions": [
                {
                    "type": "Progressing",
                    "lastUpdateTime": sixty_days_ago,
                },
            ],
        },
    }

    def fake_kubectl(ns):
        return [deploy] if ns == "prod-kingdom1" else []

    result = stats_digest.stale_deployments_section(
        db,
        ns_to_team={"prod-kingdom1": "squad-1"},
        threshold_days=30,
        kubectl_fn=fake_kubectl,
        hide_expected=True,
    )

    # Скрыт через column → должны увидеть «✅ ничего suspicious».
    assert "town-service" not in result
    assert "expected" in result.lower() or "✅" in result


def test_stats_digest_falls_back_to_legacy_when_column_null(db):
    """Если stale_class is NULL — fallback на legacy _classify_stale.

    Backup-deployment без stale_class column всё равно скрывается через
    legacy эвристику по суффиксу `-backup`.
    """
    from app.services import stats_digest

    svc = Service(
        name="town-db-backup",
        namespace="prod-shared",
        stale_class=None,  # ← column пуст (старая инсталляция)
    )
    db.add(svc)
    db.flush()

    sixty_days_ago = (datetime.utcnow() - timedelta(days=60)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    deploy = {
        "metadata": {
            "name": "town-db-backup",
            "creationTimestamp": sixty_days_ago,
        },
        "status": {
            "readyReplicas": 1,
            "conditions": [
                {
                    "type": "Progressing",
                    "lastUpdateTime": sixty_days_ago,
                },
            ],
        },
    }

    def fake_kubectl(ns):
        return [deploy] if ns == "prod-shared" else []

    result = stats_digest.stale_deployments_section(
        db,
        ns_to_team={"prod-shared": "platform"},
        threshold_days=30,
        kubectl_fn=fake_kubectl,
        hide_expected=True,
    )
    # legacy `_classify_stale` распознаёт `-backup` суффикс → скрыт.
    assert "town-db-backup" not in result
