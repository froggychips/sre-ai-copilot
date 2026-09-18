"""Дыры в ownership графа: кто остаётся без владельца и без провенанса.

Замер 18.09.2026 по реальным (non-synthetic) сервисам: 426 без владельца,
но из них активных — только 19. Остальное expected_stale и suspicious_stale,
то есть записи стендов, которых уже нет; проставлять им владельца незачем.

Эти 19 распались на две группы. Семнадцать — базы `*-db-postgresql` и
`nats`/`nats-client` ВНУТРИ squad-стендов: в squad-28-kingdom5 двадцать шесть
сервисов получили `squad-28` по префиксу, а семь остались пустыми. Причина
в том, что владельца по префиксу проставляет `kg_sync`, который ходит по
`kubectl get deployments`, — а базы это StatefulSet'ы, и до них он не
доходит. Остальные два (`ai-reviewer-net`, `cnpg-cloudnative-pg`) честно
бесхозные, им место в манифесте.

Отдельно — провенанс. У 1982 реальных сервисов владелец есть, а источника
нет, и 135 из них живые. Контракт заводит шесть источников с весами
доверия ровно чтобы отличать догадку по префиксу (0.4) от лейбла (0.9), но
без заполненной колонки эти веса не работают.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.knowledge_graph.contract import (OWNER_SOURCE_K8S_LABELS,
                                          OWNER_SOURCE_MANUAL,
                                          OWNER_SOURCE_NAMESPACE_PREFIX)
from app.knowledge_graph.populator import upsert_service
from app.knowledge_graph.schema import Base, Service


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _get(db, namespace: str, name: str) -> Service:
    return (
        db.query(Service)
        .filter(Service.namespace == namespace, Service.name == name)
        .one()
    )


# --- дозаполнение пустого владельца ---------------------------------------

def test_fallback_fills_empty_owner(db):
    """Узел без владельца получает догадку по префиксу."""
    upsert_service(db, namespace="squad-28-kingdom5", name="map-db-postgresql")
    assert _get(db, "squad-28-kingdom5", "map-db-postgresql").team_owner is None

    upsert_service(
        db, namespace="squad-28-kingdom5", name="map-db-postgresql",
        owner_fallback="squad-28",
        owner_fallback_source=OWNER_SOURCE_NAMESPACE_PREFIX,
    )

    svc = _get(db, "squad-28-kingdom5", "map-db-postgresql")
    assert svc.team_owner == "squad-28"
    assert svc.owner_source == OWNER_SOURCE_NAMESPACE_PREFIX


def test_fallback_does_not_overwrite_stronger_source(db):
    """Догадка не перетирает лейбл.

    Префикс — самый слабый источник (вес 0.4 против 0.9 у лейбла).
    Присваивание вместо дозаполнения однажды уже обвалило owner-coverage
    с 99.97% до ~50%.
    """
    upsert_service(
        db, namespace="squad-28-kingdom5", name="town-service",
        team_owner="squad-7", owner_source=OWNER_SOURCE_K8S_LABELS,
    )

    upsert_service(
        db, namespace="squad-28-kingdom5", name="town-service",
        owner_fallback="squad-28",
        owner_fallback_source=OWNER_SOURCE_NAMESPACE_PREFIX,
    )

    svc = _get(db, "squad-28-kingdom5", "town-service")
    assert svc.team_owner == "squad-7", "лейбл сильнее префикса"
    assert svc.owner_source == OWNER_SOURCE_K8S_LABELS


def test_fallback_does_not_overwrite_manual(db):
    upsert_service(
        db, namespace="infra", name="ai-reviewer-net",
        team_owner="platform", owner_source=OWNER_SOURCE_MANUAL,
    )
    upsert_service(
        db, namespace="infra", name="ai-reviewer-net",
        owner_fallback="guessed",
        owner_fallback_source=OWNER_SOURCE_NAMESPACE_PREFIX,
    )
    assert _get(db, "infra", "ai-reviewer-net").team_owner == "platform"


def test_empty_string_owner_counts_as_absent(db):
    """Пустая строка — это отсутствие владельца, как и NULL.

    В графе есть и то и другое, а для потребителя состояние одно.
    """
    upsert_service(db, namespace="squad-9-shared", name="nats")
    svc = _get(db, "squad-9-shared", "nats")
    svc.team_owner = ""
    db.flush()

    upsert_service(
        db, namespace="squad-9-shared", name="nats",
        owner_fallback="squad-9",
        owner_fallback_source=OWNER_SOURCE_NAMESPACE_PREFIX,
    )

    assert _get(db, "squad-9-shared", "nats").team_owner == "squad-9"


def test_explicit_owner_still_wins_over_fallback(db):
    """Явный владелец сильнее догадки в том же вызове."""
    upsert_service(
        db, namespace="squad-3-shared", name="nats",
        team_owner="platform", owner_source=OWNER_SOURCE_K8S_LABELS,
        owner_fallback="squad-3",
        owner_fallback_source=OWNER_SOURCE_NAMESPACE_PREFIX,
    )
    svc = _get(db, "squad-3-shared", "nats")
    assert svc.team_owner == "platform"
    assert svc.owner_source == OWNER_SOURCE_K8S_LABELS


# --- провенанс без владельца ----------------------------------------------

def test_source_without_owner_is_cleaned(db):
    """Источник без владельца — противоречие, и оно чистится при проходе.

    В графе таких строк 33: провенанс описывает значение, которого нет.
    """
    upsert_service(db, namespace="squad-5-shared", name="orphan-node")
    svc = _get(db, "squad-5-shared", "orphan-node")
    svc.owner_source = OWNER_SOURCE_NAMESPACE_PREFIX
    db.flush()

    upsert_service(db, namespace="squad-5-shared", name="orphan-node")

    assert _get(db, "squad-5-shared", "orphan-node").owner_source is None


def test_incomplete_fallback_is_rejected(db):
    """Владелец без источника (и наоборот) не принимается.

    Провенанс должен приезжать вместе со значением — иначе мы снова
    получаем владельца, о котором неизвестно, откуда он.
    """
    upsert_service(
        db, namespace="squad-6-shared", name="nats", owner_fallback="squad-6",
    )
    assert _get(db, "squad-6-shared", "nats").team_owner is None

    upsert_service(
        db, namespace="squad-6-shared", name="nats",
        owner_fallback_source=OWNER_SOURCE_NAMESPACE_PREFIX,
    )
    assert _get(db, "squad-6-shared", "nats").owner_source is None


def test_unknown_fallback_source_is_rejected(db):
    """Неизвестный источник не должен заводить в графе седьмой вариант."""
    upsert_service(
        db, namespace="squad-8-shared", name="nats",
        owner_fallback="squad-8", owner_fallback_source="сочинённый_источник",
    )
    svc = _get(db, "squad-8-shared", "nats")
    assert svc.team_owner is None
    assert svc.owner_source is None


# --- наследование провенанса workload-узлом -------------------------------

def test_workload_inherits_owner_source(db):
    """Workload наследует от Service и владельца, И источник.

    Раньше передавался только владелец, а upsert переписывает источник
    всегда вместе со значением — то есть каждый проход затирал провенанс в
    NULL. Отсюда живые узлы с владельцем и без источника.
    """
    from app.knowledge_graph.schema import NODE_KIND_WORKLOAD

    upsert_service(
        db, namespace="squad-4-kingdom2", name="town-service",
        team_owner="squad-4", owner_source=OWNER_SOURCE_K8S_LABELS,
    )
    svc = _get(db, "squad-4-kingdom2", "town-service")

    upsert_service(
        db, namespace="squad-4-kingdom2", name="town-service",
        team_owner=str(svc.team_owner),
        owner_source=str(svc.owner_source),
        node_kind=NODE_KIND_WORKLOAD,
    )

    workload = (
        db.query(Service)
        .filter(
            Service.namespace == "squad-4-kingdom2",
            Service.name == "town-service",
            Service.node_kind == NODE_KIND_WORKLOAD,
        )
        .one()
    )
    assert workload.team_owner == "squad-4"
    assert workload.owner_source == OWNER_SOURCE_K8S_LABELS, (
        "провенанс обязан ехать вместе с владельцем"
    )


# --- правило префикса ------------------------------------------------------

@pytest.mark.parametrize("namespace,expected", [
    ("squad-28-kingdom5", "squad-28"),
    ("squad-19-shared", "squad-19"),
    ("prod-kingdom1", "kingdom1"),
    ("monitoring", "platform"),
    ("sre-ai", None),
])
def test_topology_uses_same_prefix_table_as_kg_sync(namespace, expected):
    """Источник правды один: та же префиксная таблица, что у kg_sync.

    Своя копия regex в этом месте уже расходилась с общей таблицей и
    оставляла ~456 squad-сервисов без осмысленного владельца.
    """
    from app.knowledge_graph.k8s_topology_resources_sync import \
        _derive_team_owner as topology_derive
    from app.knowledge_graph.kg_sync import _derive_team_owner as sync_derive

    assert topology_derive(namespace) == expected
    assert topology_derive(namespace) == sync_derive(namespace)


# --- слепая зона: namespace, о котором граф не знает ----------------------

def test_orphan_namespace_check_finds_invisible_records(db):
    """Сервисы без записи в kg_namespaces и без класса должны быть названы.

    Такие записи проваливаются между механизмами: классификатор их не
    трогает (он ходит обходом kg_sync), а drift_cleanup чистит только
    namespace со state='missing' — которого у них нет, потому что самой
    записи в kg_namespaces нет.
    """
    from app.knowledge_graph.self_health import check_orphan_namespaces

    # Живой namespace с классом — не должен попасть в находки.
    upsert_service(
        db, namespace="squad-7-kingdom2", name="town-service",
        team_owner="squad-7", owner_source=OWNER_SOURCE_NAMESPACE_PREFIX,
        stale_class="active",
    )
    # Записи о namespace, которого в графе нет вовсе.
    for i in range(3):
        upsert_service(db, namespace="squad-52-kingdom5", name=f"svc-{i}")
    db.flush()

    result = check_orphan_namespaces(db)

    assert result.status == "warn"
    assert result.detail["services"] == 3
    assert result.detail["namespaces"] == 1
    assert "squad-52-kingdom5" in result.detail["top"]


def test_orphan_namespace_check_is_ok_when_graph_is_consistent(db):
    """Нет таких записей — проверка молчит."""
    from app.knowledge_graph.schema import Namespace
    from app.knowledge_graph.self_health import check_orphan_namespaces

    db.add(Namespace(namespace="squad-7-kingdom2", state="active"))
    upsert_service(
        db, namespace="squad-7-kingdom2", name="town-service",
        stale_class="active",
    )
    db.flush()

    assert check_orphan_namespaces(db).status == "ok"


def test_classified_records_are_not_flagged(db):
    """Запись с классом видна отчётам — она не слепая зона.

    Даже если namespace в kg_namespaces отсутствует: класс означает, что
    классификатор её видел и отнёс к категории.
    """
    from app.knowledge_graph.self_health import check_orphan_namespaces

    upsert_service(
        db, namespace="squad-99-shared", name="svc", stale_class="gone",
    )
    db.flush()

    assert check_orphan_namespaces(db).status == "ok"


# --- наследование по силе источника ---------------------------------------

def test_strong_owner_propagates_to_workload_by_assignment(db):
    """Лейбл распространяется на workload присваиванием.

    Он описывает конкретный объект, поэтому им можно переписать старую
    догадку: иначе workload навсегда остался бы с префиксным владельцем,
    даже когда у Service появился лейбл.
    """
    from app.knowledge_graph.k8s_topology_resources_sync import _inherited_owner

    svc = type("N", (), {"team_owner": "squad-7", "owner_source": OWNER_SOURCE_K8S_LABELS})()

    assert _inherited_owner(svc) == {
        "team_owner": "squad-7", "owner_source": OWNER_SOURCE_K8S_LABELS,
    }


def test_weak_owner_propagates_to_workload_as_fallback(db):
    """Догадка по префиксу — только дозаполнением.

    Она описывает namespace целиком и ничего не знает про отдельный
    объект, поэтому не должна переписывать то, что поставили осознанно.
    """
    from app.knowledge_graph.k8s_topology_resources_sync import _inherited_owner

    svc = type("N", (), {
        "team_owner": "squad-28", "owner_source": OWNER_SOURCE_NAMESPACE_PREFIX,
    })()

    assert _inherited_owner(svc) == {
        "owner_fallback": "squad-28",
        "owner_fallback_source": OWNER_SOURCE_NAMESPACE_PREFIX,
    }


def test_manual_owner_propagates_by_assignment(db):
    from app.knowledge_graph.k8s_topology_resources_sync import _inherited_owner

    svc = type("N", (), {"team_owner": "platform", "owner_source": OWNER_SOURCE_MANUAL})()
    assert "team_owner" in _inherited_owner(svc)


def test_owner_without_provenance_propagates_as_fallback(db):
    """Владелец без источника доверия не заслуживает — только дозаполнение."""
    from app.knowledge_graph.k8s_topology_resources_sync import _inherited_owner

    svc = type("N", (), {"team_owner": "squad-3", "owner_source": None})()
    assert _inherited_owner(svc) == {
        "owner_fallback": "squad-3", "owner_fallback_source": None,
    }


def test_no_owner_propagates_nothing(db):
    from app.knowledge_graph.k8s_topology_resources_sync import _inherited_owner

    svc = type("N", (), {"team_owner": None, "owner_source": None})()
    assert _inherited_owner(svc) == {}


def test_weak_inheritance_does_not_overwrite_workload_label(db):
    """Сквозная проверка: догадка Service не сносит лейбл workload."""
    from app.knowledge_graph.k8s_topology_resources_sync import _inherited_owner
    from app.knowledge_graph.schema import NODE_KIND_WORKLOAD

    upsert_service(
        db, namespace="squad-28-kingdom5", name="town-service",
        team_owner="squad-7", owner_source=OWNER_SOURCE_K8S_LABELS,
        node_kind=NODE_KIND_WORKLOAD,
    )
    weak_service = type("N", (), {
        "team_owner": "squad-28", "owner_source": OWNER_SOURCE_NAMESPACE_PREFIX,
    })()

    upsert_service(
        db, namespace="squad-28-kingdom5", name="town-service",
        node_kind=NODE_KIND_WORKLOAD, **_inherited_owner(weak_service),
    )

    workload = (
        db.query(Service)
        .filter(
            Service.namespace == "squad-28-kingdom5",
            Service.name == "town-service",
            Service.node_kind == NODE_KIND_WORKLOAD,
        )
        .one()
    )
    assert workload.team_owner == "squad-7", "лейбл workload сильнее догадки Service"
