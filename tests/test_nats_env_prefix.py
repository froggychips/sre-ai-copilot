"""NATS-рёбра должны строиться во всех контурах, а не только в prod.

`_env_prefix` определяет, в каком namespace искать общий NATS-кластер:
`prod-kingdom1` → `prod-shared`. Пока regex знал только
`prod|preprod|preupdate`, для squad-стендов он возвращал None — и в
`_extract_nats_clusters` рёбра на `SHARED_NATS_*` и `NATS_FOR_*_*` не
создавались вовсе. Работали только `KINGDOM_NATS_*`, которым префикс не нужен.

Замер на проде 08.08.2026: 112 рёбер `uses_nats` на 4874 squad-сервиса против
280 на 292 prod-сервиса. Плотность связей в squad была вчетверо ниже, и именно
squad давал 3154 orphan'а из 3578 по всему графу.

Второй случай — QA-окружения: `preprod-qa-kingdom2` живёт со своим
`preprod-qa-shared`, и без `-qa` в префиксе его ребро уходило бы в соседнее
окружение.
"""
from __future__ import annotations

import pytest

from app.knowledge_graph.kg_sync import _env_prefix, _extract_nats_clusters


@pytest.mark.parametrize("namespace,expected", [
    # исходное поведение не изменилось
    ("prod-kingdom1", "prod"),
    ("prod-shared", "prod"),
    ("preprod-shared", "preprod"),
    ("preupdate-kingdom5", "preupdate"),
    # squad: префикс включает номер — общий NATS у каждого стенда свой
    ("squad-14-shared", "squad-14"),
    ("squad-14-kingdom2", "squad-14"),
    ("squad-7-shared", "squad-7"),
    # QA-контуры: `-qa` часть префикса, иначе ребро уедет в соседний shared
    ("preprod-qa-shared", "preprod-qa"),
    ("preprod-qa-kingdom2", "preprod-qa"),
    ("preupdate-qa-shared", "preupdate-qa"),
    # инфраструктурные ns к контурам не относятся
    ("statics", None),
    ("sre-ai", None),
    ("monitoring", None),
])
def test_env_prefix(namespace, expected):
    assert _env_prefix(namespace) == expected


def _deploy_with_env(*env_names: str):
    return {
        "spec": {"template": {"spec": {"containers": [
            {"env": [{"name": n, "value": "x"} for n in env_names]},
        ]}}},
    }


def test_squad_gets_shared_nats_edge():
    """Регрессия: у squad-стенда появляется ребро на его собственный NATS."""
    deploy = _deploy_with_env("SHARED_NATS_CLIENT_CONNECTION")

    clusters = _extract_nats_clusters(deploy, "squad-14-kingdom2")

    assert ("nats-shared", "squad-14-shared") in clusters, (
        f"ожидалось ребро на squad-14-shared, получено {clusters} — "
        "SHARED_NATS в squad снова теряется"
    )


def test_squad_purpose_nats_edge():
    """`NATS_FOR_*_CREDS` тоже требует префикса — и тоже терялся в squad."""
    deploy = _deploy_with_env("NATS_FOR_CLIENT_SERVICE_CREDS")

    clusters = _extract_nats_clusters(deploy, "squad-14-shared")

    assert ("nats-purpose", "squad-14-shared") in clusters


def test_qa_env_does_not_leak_into_neighbour():
    """QA-стенд ссылается на свой shared, а не на соседний."""
    deploy = _deploy_with_env("SHARED_NATS_CLIENT_CONNECTION")

    clusters = _extract_nats_clusters(deploy, "preprod-qa-kingdom2")

    assert ("nats-shared", "preprod-qa-shared") in clusters
    assert ("nats-shared", "preprod-shared") not in clusters, (
        "ребро QA-стенда уехало в соседнее окружение"
    )


def test_kingdom_nats_stays_local():
    """KINGDOM_NATS указывает на свой же namespace — префикс тут ни при чём."""
    deploy = _deploy_with_env("KINGDOM_NATS_CONNECTION")

    assert _extract_nats_clusters(deploy, "squad-14-kingdom2") == [
        ("nats-kingdom", "squad-14-kingdom2"),
    ]


def test_namespace_without_contour_yields_no_shared_edge():
    """Для ns вне контуров shared-ребро не выдумывается."""
    deploy = _deploy_with_env("SHARED_NATS_CLIENT_CONNECTION")

    assert _extract_nats_clusters(deploy, "statics") == []
