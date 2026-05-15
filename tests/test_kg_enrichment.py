"""Тесты на kg_sync enrichment — team_owner + NATS-cluster edges
плюс namespace discovery (после scrub-а DEFAULT_SCAN_NAMESPACES).

Проверяем pure-функции вычленения (без работы с БД), плюс smoke на
sync_namespace через моки upsert_service / upsert_edge / kubectl.
"""
from unittest.mock import MagicMock, patch

from app.knowledge_graph.kg_sync import (
    _derive_team_owner,
    _discover_namespaces,
    _extract_nats_clusters,
    _extract_upstreams_extended,
    _inferred_extras,
    _is_synthetic_service,
    _parse_host_from_value,
    sync_namespace,
    sync_topology,
)


# ── synthetic-service detection ─────────────────────────────────────────────

def test_is_synthetic_db_backup_suffix():
    assert _is_synthetic_service("chat-db-backup")
    assert _is_synthetic_service("config-worker-db-backup")
    assert _is_synthetic_service("chat-messages-global-db-backup")


def test_is_synthetic_cron_suffix():
    assert _is_synthetic_service("nightly-cleanup-cron")
    assert _is_synthetic_service("backup-cron")


def test_is_synthetic_exact_names():
    assert _is_synthetic_service("nats-box")
    assert _is_synthetic_service("nats-client-box")
    assert _is_synthetic_service("nats-exporter-prometheus-nats-exporter")
    assert _is_synthetic_service("seq")
    assert _is_synthetic_service("redis-exporter")


def test_is_synthetic_rejects_real_services():
    """Backup-related НЕ-cron сервисы — не synthetic."""
    assert not _is_synthetic_service("backup-service")  # это API, не cron
    assert not _is_synthetic_service("auth-service")
    assert not _is_synthetic_service("town-service")
    assert not _is_synthetic_service("nats-streaming")  # не точное совпадение
    assert not _is_synthetic_service("")
    assert not _is_synthetic_service("redis")  # не -exporter


# ── _parse_host_from_value (PR B — extended env scan) ───────────────────────


def test_parse_host_http_url_with_port_and_path():
    assert _parse_host_from_value("http://auth-service:8080/api/v1", allow_no_scheme=False) == ("auth-service", None)


def test_parse_host_http_url_with_namespace():
    assert _parse_host_from_value(
        "https://auth-service.prod-shared.svc.cluster.local:8080", allow_no_scheme=False
    ) == ("auth-service", "prod-shared")


def test_parse_host_http_url_with_short_ns():
    assert _parse_host_from_value("http://payment.prod-kingdom1:9000", allow_no_scheme=False) == ("payment", "prod-kingdom1")


def test_parse_host_bare_host_requires_url_hint():
    """`svc-name` без http-схемы — только когда env-name намекает (URL/HOST/etc)."""
    assert _parse_host_from_value("auth-service", allow_no_scheme=True) == ("auth-service", None)
    assert _parse_host_from_value("auth-service", allow_no_scheme=False) is None


def test_parse_host_dsn_style():
    """DSN-style: postgres://user:pass@host:port/db → host."""
    assert _parse_host_from_value(
        "postgres://user:secret@finance-db.prod-shared:5432/finance",
        allow_no_scheme=True,
    ) == ("finance-db", "prod-shared")


def test_parse_host_rejects_cloud_fragments():
    """Cloud / external — пропускаем рано (есть skip-fragments list)."""
    assert _parse_host_from_value("https://api.openai.com/v1/chat", allow_no_scheme=False) is None
    assert _parse_host_from_value("https://prod.amazonaws.com/...", allow_no_scheme=False) is None


def test_parse_host_rejects_localhost_and_short_names():
    assert _parse_host_from_value("http://localhost:3000", allow_no_scheme=False) is None
    assert _parse_host_from_value("http://x:80", allow_no_scheme=False) is None  # name <3 chars
    assert _parse_host_from_value("", allow_no_scheme=True) is None
    assert _parse_host_from_value("http://127.0.0.1:8080", allow_no_scheme=False) is None  # invalid SVC name


# ── _extract_upstreams_extended ─────────────────────────────────────────────


def test_extract_upstreams_extended_match_only_existing():
    """Если target в known_index — edge возвращается. Если нет — пропуск."""
    deploy = _make_deploy([])
    deploy["spec"]["template"]["spec"]["containers"][0]["env"] = [
        {"name": "AUTH_URL", "value": "http://auth-service.prod-shared:8080"},  # известен
        {"name": "PAYMENT_HOST", "value": "payment-svc"},                        # неизвестен
        {"name": "EXTERNAL_API", "value": "https://api.openai.com/v1"},          # cloud — skipped
    ]
    deploy["metadata"] = {"name": "town-service"}
    known = {
        "prod-shared": {"auth-service"},
        "prod-kingdom1": {"town-service"},  # тут только себя
    }
    out = _extract_upstreams_extended(deploy, "prod-kingdom1", known)
    assert out == [("auth-service", "prod-shared")]


def test_extract_upstreams_extended_skips_self_reference():
    """Если env указывает на собственный сервис в собственном ns — skip."""
    deploy = _make_deploy([])
    deploy["spec"]["template"]["spec"]["containers"][0]["env"] = [
        {"name": "SELF_URL", "value": "http://town-service:8080"},
    ]
    deploy["metadata"] = {"name": "town-service"}
    known = {"prod-kingdom1": {"town-service"}}
    assert _extract_upstreams_extended(deploy, "prod-kingdom1", known) == []


def test_extract_upstreams_extended_requires_url_hint_for_bare_host():
    """Bare host без env-name hint игнорируется (избежать false-positive)."""
    deploy = _make_deploy([])
    deploy["spec"]["template"]["spec"]["containers"][0]["env"] = [
        # Нет _URL/_HOST/_DSN/etc — value=svc-name, но parse skip-нёт
        {"name": "RANDOM_VAR", "value": "auth-service"},
    ]
    deploy["metadata"] = {"name": "town-service"}
    known = {"prod-kingdom1": {"auth-service"}}
    out = _extract_upstreams_extended(deploy, "prod-kingdom1", known)
    assert out == []


def test_extract_upstreams_extended_url_hint_unlocks_bare():
    """env-name `*_HOST` → bare host матчится."""
    deploy = _make_deploy([])
    deploy["spec"]["template"]["spec"]["containers"][0]["env"] = [
        {"name": "AUTH_HOST", "value": "auth-service"},
    ]
    deploy["metadata"] = {"name": "town-service"}
    known = {"prod-kingdom1": {"auth-service", "town-service"}}
    out = _extract_upstreams_extended(deploy, "prod-kingdom1", known)
    assert out == [("auth-service", "prod-kingdom1")]


# ── Edge confidence + semantics (extras scaffold для L7-источников) ─────────


def test_inferred_extras_calls_is_sync():
    """HTTP/gRPC calls — sync semantics."""
    e = _inferred_extras("calls")
    assert e == {"confidence": "inferred_env", "semantics": "sync"}


def test_inferred_extras_nats_is_async():
    """uses_nats — async semantics (pub/sub)."""
    e = _inferred_extras("uses_nats")
    assert e == {"confidence": "inferred_env", "semantics": "async"}


def test_inferred_extras_unknown_kind_falls_back_to_unknown_semantics():
    """Future edge kinds, не описанные в map → semantics='unknown'."""
    e = _inferred_extras("custom_future_kind")
    assert e["confidence"] == "inferred_env"
    assert e["semantics"] == "unknown"


def test_inferred_extras_reads_from_is_sync():
    """Future reads_from (DB queries) — sync."""
    assert _inferred_extras("reads_from")["semantics"] == "sync"


def test_inferred_extras_kafka_is_async():
    """Future consumes_kafka — async."""
    assert _inferred_extras("consumes_kafka")["semantics"] == "async"


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


# ── _discover_namespaces (после scrub DEFAULT_SCAN_NAMESPACES) ──────────────

def _fake_kubectl_ns(stdout: str, returncode: int = 0):
    """Helper для мока subprocess.run результата `kubectl get ns -o jsonpath=...`."""
    fake = MagicMock()
    fake.stdout = stdout
    fake.stderr = ""
    fake.returncode = returncode
    return fake


def test_discover_namespaces_excludes_system_prefixes():
    fake_out = "kube-system kube-public kube-node-lease default monitoring squad-a squad-b cert-manager"
    with patch("app.knowledge_graph.kg_sync.subprocess.run", return_value=_fake_kubectl_ns(fake_out)):
        result = _discover_namespaces()
    # System namespaces (kube-*, default, monitoring, cert-manager) исключены.
    assert set(result) == {"squad-a", "squad-b"}


def test_discover_namespaces_handles_kubectl_failure():
    with patch("app.knowledge_graph.kg_sync.subprocess.run", return_value=_fake_kubectl_ns("", returncode=1)):
        assert _discover_namespaces() == []


def test_discover_namespaces_handles_exception():
    with patch("app.knowledge_graph.kg_sync.subprocess.run", side_effect=OSError("no kubectl")):
        assert _discover_namespaces() == []


# ── sync_topology source-of-namespaces priority ─────────────────────────────

def test_sync_topology_uses_explicit_argument():
    """Аргумент `namespaces` — высший приоритет, ни settings ни discover не дёргаются."""
    with patch("app.knowledge_graph.kg_sync.sync_namespace") as mock_sync_ns, \
         patch("app.knowledge_graph.kg_sync._discover_namespaces") as mock_disc:
        mock_sync_ns.return_value = {"services": 0, "edges": 0, "skipped": 0}
        sync_topology(db=MagicMock(), namespaces=["a", "b"])
    mock_disc.assert_not_called()
    assert mock_sync_ns.call_count == 2


def test_sync_topology_reads_settings_when_arg_none():
    """namespaces=None → берём из settings.KG_SCAN_NAMESPACES (env-driven)."""
    with patch("app.knowledge_graph.kg_sync.sync_namespace") as mock_sync_ns, \
         patch("app.knowledge_graph.kg_sync._discover_namespaces") as mock_disc, \
         patch("app.config.settings") as mock_settings:
        mock_settings.KG_SCAN_NAMESPACES = "team-x, team-y , team-z"  # whitespace стрипается
        mock_sync_ns.return_value = {"services": 0, "edges": 0, "skipped": 0}
        sync_topology(db=MagicMock())
    mock_disc.assert_not_called()
    called_ns = [c.args[1] for c in mock_sync_ns.call_args_list]
    assert sorted(called_ns) == ["team-x", "team-y", "team-z"]


def test_sync_topology_falls_back_to_discovery_when_settings_empty():
    """settings.KG_SCAN_NAMESPACES пусто → _discover_namespaces."""
    with patch("app.knowledge_graph.kg_sync.sync_namespace") as mock_sync_ns, \
         patch("app.knowledge_graph.kg_sync._discover_namespaces", return_value=["auto-1", "auto-2"]) as mock_disc, \
         patch("app.config.settings") as mock_settings:
        mock_settings.KG_SCAN_NAMESPACES = ""
        mock_sync_ns.return_value = {"services": 0, "edges": 0, "skipped": 0}
        sync_topology(db=MagicMock())
    mock_disc.assert_called_once()
    assert mock_sync_ns.call_count == 2


def test_sync_topology_no_namespaces_returns_zero_stats():
    """Пустой settings + пустой discover → no-op return, не падаем."""
    with patch("app.knowledge_graph.kg_sync._discover_namespaces", return_value=[]), \
         patch("app.config.settings") as mock_settings, \
         patch("app.knowledge_graph.kg_sync.sync_namespace") as mock_sync_ns:
        mock_settings.KG_SCAN_NAMESPACES = ""
        result = sync_topology(db=MagicMock())
    assert result == {"services": 0, "edges": 0, "namespaces": 0, "errors": 0}
    mock_sync_ns.assert_not_called()
