"""Тесты на kg_sync enrichment — team_owner + NATS-cluster edges.

Проверяем pure-функции вычленения (без работы с БД), плюс smoke на
sync_namespace через моки upsert_service / upsert_edge / kubectl.
"""
from unittest.mock import MagicMock, patch

from app.knowledge_graph.kg_sync import (
    _derive_team_owner,
    _extract_nats_clusters,
    sync_namespace,
)


# ── team_owner ──────────────────────────────────────────────────────────────

def test_team_owner_prod_kingdom():
    assert _derive_team_owner("prod-kingdom1") == "kingdom1"
    assert _derive_team_owner("prod-kingdom5") == "kingdom5"


def test_team_owner_shared_tier():
    assert _derive_team_owner("prod-shared") == "shared"
    assert _derive_team_owner("preupdate-shared") == "shared"


def test_team_owner_preprod_and_preupdate():
    assert _derive_team_owner("preprod-kingdom3") == "kingdom3"
    assert _derive_team_owner("preupdate-kingdom5") == "kingdom5"


def test_team_owner_non_wo_namespace():
    """sre-ai, monitoring, kube-system — не WO-формат."""
    assert _derive_team_owner("sre-ai") is None
    assert _derive_team_owner("monitoring") is None
    assert _derive_team_owner("kube-system") is None
    assert _derive_team_owner("default") is None


# ── NATS cluster extraction ─────────────────────────────────────────────────

def _make_deploy(env_names):
    """Минимальный deployment-spec с заданными env-vars (значения = '')."""
    return {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {"env": [{"name": n, "value": ""} for n in env_names]}
                    ]
                }
            }
        }
    }


def test_nats_shared_creates_shared_cluster_edge():
    deploy = _make_deploy(["SHARED_NATS_CONNECTION"])
    out = _extract_nats_clusters(deploy, "prod-kingdom2")
    assert out == [("nats-shared", "prod-shared")]


def test_nats_kingdom_creates_local_cluster_edge():
    deploy = _make_deploy(["KINGDOM_NATS_CONNECTION"])
    out = _extract_nats_clusters(deploy, "preupdate-kingdom1")
    # kingdom-NATS живёт в собственном namespace
    assert out == [("nats-kingdom", "preupdate-kingdom1")]


def test_nats_for_purpose_goes_to_shared():
    """NATS_FOR_CLIENT_SERVICE_CREDS → nats-purpose в <env>-shared."""
    deploy = _make_deploy(["NATS_FOR_CLIENT_SERVICE_CREDS"])
    out = _extract_nats_clusters(deploy, "preprod-kingdom3")
    assert out == [("nats-purpose", "preprod-shared")]


def test_nats_combined_extraction():
    """Multiple NATS env-vars → deduped set of edges."""
    deploy = _make_deploy([
        "SHARED_NATS_CONNECTION",
        "SHARED_NATS_CLIENT_CONNECTION",
        "KINGDOM_NATS_CONNECTION",
        "NATS_FOR_CLIENT_SERVICE_CREDS",
        "UNRELATED_VAR",
    ])
    out = _extract_nats_clusters(deploy, "prod-kingdom1")
    assert sorted(out) == [
        ("nats-kingdom", "prod-kingdom1"),
        ("nats-purpose", "prod-shared"),
        ("nats-shared", "prod-shared"),
    ]


def test_nats_extraction_ignores_unrelated_env():
    deploy = _make_deploy(["DATABASE_URL", "REDIS_HOST", "API_KEY"])
    assert _extract_nats_clusters(deploy, "prod-kingdom1") == []


def test_nats_extraction_no_env_prefix_skips_shared():
    """Namespace без prod-/preprod-/preupdate- prefix → shared/purpose edges skip-аются."""
    deploy = _make_deploy(["SHARED_NATS_CONNECTION", "KINGDOM_NATS_CONNECTION"])
    out = _extract_nats_clusters(deploy, "sre-ai")
    # SHARED требует env_prefix → skipped. KINGDOM работает безусловно.
    assert out == [("nats-kingdom", "sre-ai")]


# ── sync_namespace integration smoke ────────────────────────────────────────

def test_sync_namespace_calls_upsert_with_team_owner_and_nats_edges():
    """End-to-end: deployment с URL + NATS-env → 2 upsert_service вызова +
    оба типа edges. Моки прерывают kubectl и БД."""
    fake_deploy = {
        "metadata": {"name": "town-service"},
        "spec": {
            "template": {
                "spec": {
                    "containers": [{
                        "env": [
                            {"name": "AUTH_URL", "value": "http://auth-service.prod-shared.svc.cluster.local:8080"},
                            {"name": "SHARED_NATS_CONNECTION", "value": ""},
                            {"name": "KINGDOM_NATS_CONNECTION", "value": ""},
                        ]
                    }]
                }
            }
        }
    }

    with patch("app.knowledge_graph.kg_sync._kubectl_get_deployments", return_value=[fake_deploy]), \
         patch("app.knowledge_graph.kg_sync.upsert_service") as mock_upsert_svc, \
         patch("app.knowledge_graph.kg_sync.upsert_edge") as mock_upsert_edge:
        mock_upsert_svc.return_value = MagicMock()  # каждая call возвращает разный obj
        stats = sync_namespace(db=MagicMock(), namespace="prod-kingdom1")

    # services: town-service (источник) + auth-service (URL upstream)
    #         + nats-shared (SHARED_NATS) + nats-kingdom (KINGDOM_NATS) = 4 upserts
    assert mock_upsert_svc.call_count >= 4
    # edges: 1 calls + 2 uses_nats = 3 edges
    edge_kinds = [c.kwargs.get("kind") for c in mock_upsert_edge.call_args_list]
    assert edge_kinds.count("calls") == 1
    assert edge_kinds.count("uses_nats") == 2

    # team_owner правильно проброшен для source-сервиса
    src_call = [c for c in mock_upsert_svc.call_args_list if c.kwargs.get("name") == "town-service"][0]
    assert src_call.kwargs.get("team_owner") == "kingdom1"

    # NATS-узлы получают team_owner="platform" (infra marker)
    nats_calls = [c for c in mock_upsert_svc.call_args_list if c.kwargs.get("name","").startswith("nats-")]
    assert len(nats_calls) == 2
    assert all(c.kwargs.get("team_owner") == "platform" for c in nats_calls)

    assert stats["services"] == 1
    assert stats["edges"] == 3
