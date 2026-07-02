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


def test_is_synthetic_observability_agents():
    """G1.1: vm-* / prometheus-kube-prometheus-* / legal-pages — observability,
    никогда не имеют edges. Без флага засчитывались как pure_orphan."""
    for name in (
        "vm-node-exporter",
        "vm-kube-state-metrics",
        "vmagent-vm-victoria-metrics-k8s-stack",
        "vm-victoria-metrics-k8s-stack-kube-controller-manager",
        "vm-victoria-metrics-k8s-stack-kube-etcd",
        "prometheus-kube-prometheus-kubelet",
        "prometheus-kube-prometheus-kube-controller-manager",
        "prometheus-kube-prometheus-kube-etcd",
        "prometheus-kube-prometheus-kube-scheduler",
        "legal-pages",
    ):
        assert _is_synthetic_service(name), f"{name} должен быть synthetic"


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
    """sre-ai/default — не WO-формат → None.

    monitoring/kube-system теперь канонизируются в "platform" (единая
    prefix-таблица из ownership_suggester, fix 2026-06-05).
    """
    assert _derive_team_owner("sre-ai") is None
    assert _derive_team_owner("default") is None
    assert _derive_team_owner("monitoring") == "platform"
    assert _derive_team_owner("kube-system") == "platform"


def test_team_owner_squad_realms():
    """squad-N-realm принадлежит самому squad-N (а не суффиксу realm).

    Унифицировано с suggest_owner_multi_signal (fix 2026-06-05): раньше
    отдавали суффикс "shared"/"kingdomN" — отсюда ~456 squad-сервисов без
    осмысленного owner.
    """
    assert _derive_team_owner("squad-3-shared") == "squad-3"
    assert _derive_team_owner("squad-19-kingdom2") == "squad-19"
    assert _derive_team_owner("squad-1-payments") == "squad-1"
    assert _derive_team_owner("squad-gd-kingdom1") == "squad-gd"


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
         patch("app.knowledge_graph.kg_sync._upsert_service_pg") as mock_upsert_svc, \
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
    # _kubectl_get_deployments мокаем на успешный-пустой fetch: иначе на машине
    # без кластера он raise'ит KubectlFetchError (fix #1) и sync_namespace для
    # ns не вызывается — тест же проверяет только приоритет источника namespaces.
    with patch("app.knowledge_graph.kg_sync.sync_namespace") as mock_sync_ns, \
         patch("app.knowledge_graph.kg_sync._kubectl_get_deployments", return_value=[]), \
         patch("app.knowledge_graph.kg_sync._discover_namespaces") as mock_disc:
        mock_sync_ns.return_value = {"services": 0, "edges": 0, "skipped": 0}
        sync_topology(db=MagicMock(), namespaces=["a", "b"])
    mock_disc.assert_not_called()
    assert mock_sync_ns.call_count == 2


def test_sync_topology_reads_settings_when_arg_none():
    """namespaces=None → берём из settings.KG_SCAN_NAMESPACES (env-driven)."""
    with patch("app.knowledge_graph.kg_sync.sync_namespace") as mock_sync_ns, \
         patch("app.knowledge_graph.kg_sync._kubectl_get_deployments", return_value=[]), \
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
         patch("app.knowledge_graph.kg_sync._kubectl_get_deployments", return_value=[]), \
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


# ── A2: _extract_db_targets (DSN sniffing) ─────────────────────────────────

def _mk_deploy_with_env(envs: dict) -> dict:
    return {"spec": {"template": {"spec": {"containers": [
        {"env": [{"name": k, "value": v} for k, v in envs.items()]}
    ]}}}}


def test_db_targets_postgres_with_explicit_ns():
    from app.knowledge_graph.kg_sync import _extract_db_targets
    out = _extract_db_targets(
        _mk_deploy_with_env({"DSN": "postgres://u:p@finance-db.prod-shared:5432/f"}),
        own_namespace="prod-kingdom1",
    )
    assert out == [("db:postgres:finance-db", "prod-shared", "postgres", "dsn_env")]


def test_db_targets_redis_bare_host_uses_own_ns():
    from app.knowledge_graph.kg_sync import _extract_db_targets
    out = _extract_db_targets(
        _mk_deploy_with_env({"CACHE_URL": "redis://redis-cluster:6379/0"}),
        own_namespace="prod-kingdom1",
    )
    assert out == [("db:redis:redis-cluster", "prod-kingdom1", "redis", "dsn_env")]


def test_db_targets_driver_canonization():
    """postgresql→postgres, rediss→redis, mariadb→mysql, amqps→amqp."""
    from app.knowledge_graph.kg_sync import _extract_db_targets
    cases = [
        ({"X": "postgresql://u@finance-db:5432/d"}, "postgres"),
        ({"X": "rediss://secure-redis:6380/0"}, "redis"),
        ({"X": "mariadb://app@mysql-payments:3306/db"}, "mysql"),
        ({"X": "amqps://rabbit-broker:5671/"}, "amqp"),
    ]
    for envs, want_driver in cases:
        out = _extract_db_targets(_mk_deploy_with_env(envs), "ns")
        assert len(out) == 1 and out[0][2] == want_driver, (envs, out)


def test_db_targets_skip_external_cloud():
    from app.knowledge_graph.kg_sync import _extract_db_targets
    cases = [
        {"X": "postgres://u@db.amazonaws.com:5432/d"},
        {"X": "mongodb://atlas.cloud.google:27017/d"},
        {"X": "redis://x.azure:6379/0"},
    ]
    for envs in cases:
        assert _extract_db_targets(_mk_deploy_with_env(envs), "ns") == []


def test_db_targets_skip_non_dsn():
    """HTTP URL, bare-host без DSN-схемы — не должны быть DB-targets."""
    from app.knowledge_graph.kg_sync import _extract_db_targets
    cases = [
        {"API_URL": "http://api-service:8080/v1"},  # HTTP — не DSN
        {"HOST": "finance-db.prod-shared:5432"},     # bare без схемы
    ]
    for envs in cases:
        assert _extract_db_targets(_mk_deploy_with_env(envs), "ns") == []


# ── A2-v2: secret-name heuristic ────────────────────────────────────────────

def _mk_deploy_with_secret_envs(envs: dict) -> dict:
    """envs: {env_name: secret_name} → deployment с valueFrom.secretKeyRef."""
    return {"spec": {"template": {"spec": {"containers": [{"env": [
        {"name": k, "valueFrom": {"secretKeyRef": {"name": v, "key": "url"}}}
        for k, v in envs.items()
    ]}]}}}}


def test_secret_hint_postgres():
    from app.knowledge_graph.kg_sync import _parse_db_hint_from_secret_name
    assert _parse_db_hint_from_secret_name("postgres-finance-secret") == ("postgres", "finance")
    # `rw`/`ro` (read-write / read-only) — noise; вырезаются, остаётся бизнес-имя.
    assert _parse_db_hint_from_secret_name("finance-postgres-rw-creds") == ("postgres", "finance")
    assert _parse_db_hint_from_secret_name("postgres-orders-ro-secret") == ("postgres", "orders")


def test_secret_hint_mongo_canonical():
    """`mongo` в имени → driver канонизируется в mongodb."""
    from app.knowledge_graph.kg_sync import _parse_db_hint_from_secret_name
    assert _parse_db_hint_from_secret_name("mongo-sessions-config") == ("mongodb", "sessions")


def test_secret_hint_rabbit_to_amqp():
    from app.knowledge_graph.kg_sync import _parse_db_hint_from_secret_name
    assert _parse_db_hint_from_secret_name("rabbit-events-creds") == ("amqp", "events")


def test_secret_hint_no_driver_token_returns_none():
    """Имя без driver-токена не парсится."""
    from app.knowledge_graph.kg_sync import _parse_db_hint_from_secret_name
    assert _parse_db_hint_from_secret_name("app-db-credentials") is None
    assert _parse_db_hint_from_secret_name("api-config") is None
    assert _parse_db_hint_from_secret_name("") is None
    assert _parse_db_hint_from_secret_name("random-string") is None


def test_secret_hint_all_noise_returns_none():
    """Только driver + noise-токены → нет host hint."""
    from app.knowledge_graph.kg_sync import _parse_db_hint_from_secret_name
    assert _parse_db_hint_from_secret_name("postgres-secret") is None
    assert _parse_db_hint_from_secret_name("redis-creds") is None
    assert _parse_db_hint_from_secret_name("mongo-config") is None


def test_db_targets_secret_hint_e2e():
    """A2-v2: valueFrom.secretKeyRef.name → uses_db edge со source=secret_hint."""
    from app.knowledge_graph.kg_sync import _extract_db_targets
    out = _extract_db_targets(
        _mk_deploy_with_secret_envs({
            "DB_URL": "postgres-finance-secret",
            "CACHE_URL": "redis-cache-creds",
        }),
        own_namespace="prod-kingdom1",
    )
    assert ("db:postgres:finance", "prod-kingdom1", "postgres", "secret_hint") in out
    assert ("db:redis:cache", "prod-kingdom1", "redis", "secret_hint") in out


# ── A2-v2: secret-key heuristic (важнее secret-name в WO) ──────────────────

def test_secret_key_postgres_explicit_driver():
    from app.knowledge_graph.kg_sync import _parse_db_hint_from_secret_key
    assert _parse_db_hint_from_secret_key("MV_POSTGRES_DB_CONNECTION") == ("postgres", "mv")
    assert _parse_db_hint_from_secret_key("ANALYTICS_DB_CLICKHOUSE_CONNECTION") == ("clickhouse", "analytics")


def test_secret_key_generic_db_defaults_to_postgres():
    """Generic *_DB_CONNECTION без driver-токена → postgres (WO default)."""
    from app.knowledge_graph.kg_sync import _parse_db_hint_from_secret_key
    assert _parse_db_hint_from_secret_key("TOWN_DB_CONNECTION") == ("postgres", "town")
    assert _parse_db_hint_from_secret_key("CONFIG_DB_CONNECTION") == ("postgres", "config")
    assert _parse_db_hint_from_secret_key("FINANCE_DB_CONNECTION") == ("postgres", "finance")


def test_secret_key_multi_word_host():
    """CHAT_MESSAGE_ADDITIONAL_DB_CONNECTION → chat-message (additional — noise)."""
    from app.knowledge_graph.kg_sync import _parse_db_hint_from_secret_key
    assert _parse_db_hint_from_secret_key("CHAT_MESSAGE_ADDITIONAL_DB_CONNECTION") == ("postgres", "chat-message")


def test_secret_key_requires_endpoint_marker():
    """Без CONNECTION/CONN/URI/URL/DSN — не endpoint, skip (creds, тех значения)."""
    from app.knowledge_graph.kg_sync import _parse_db_hint_from_secret_key
    assert _parse_db_hint_from_secret_key("PG_USER") is None
    assert _parse_db_hint_from_secret_key("PG_PASSWORD") is None
    assert _parse_db_hint_from_secret_key("ACCESS_TOKEN_SECRET") is None
    assert _parse_db_hint_from_secret_key("S3_REGION") is None


def test_secret_key_no_db_token_returns_none():
    from app.knowledge_graph.kg_sync import _parse_db_hint_from_secret_key
    # endpoint-marker есть, но нет ни DB, ни driver-токена → не DB endpoint.
    assert _parse_db_hint_from_secret_key("CDN_BASE_URI") is None
    assert _parse_db_hint_from_secret_key("UPDATE_SERVICE_REST_ENDPOINT") is None
    assert _parse_db_hint_from_secret_key("") is None


def test_secret_ref_combined_prefers_key():
    """secret_name тоже передаётся, но key информативнее → его и используем."""
    from app.knowledge_graph.kg_sync import _parse_db_hint_from_secret_ref
    # name='database' (generic), key точный → берём key.
    assert _parse_db_hint_from_secret_ref("database", "TOWN_DB_CONNECTION") == ("postgres", "town")


def test_secret_ref_falls_back_to_name():
    """Если key не парсится, проверяем name (для других конвенций)."""
    from app.knowledge_graph.kg_sync import _parse_db_hint_from_secret_ref
    assert _parse_db_hint_from_secret_ref("postgres-finance-secret", "url") == ("postgres", "finance")


def test_db_targets_dsn_env_wins_over_secret_hint():
    """Если у env есть и value, и valueFrom, priority — plain DSN."""
    from app.knowledge_graph.kg_sync import _extract_db_targets
    deploy = {"spec": {"template": {"spec": {"containers": [{"env": [
        {
            "name": "DB_URL",
            "value": "postgres://u@real-host:5432/d",
            "valueFrom": {"secretKeyRef": {"name": "postgres-fake-secret", "key": "x"}},
        }
    ]}]}}}}
    out = _extract_db_targets(deploy, "ns")
    assert out == [("db:postgres:real-host", "ns", "postgres", "dsn_env")]


# ── C2: phantom db-node фильтрация (secret_hint host угадан) ────────────────


def _mk_db_deploy(name: str, secret_envs: dict | None = None, dsn_envs: dict | None = None) -> dict:
    """Deployment с DB-env: secret_envs={env: (secret_name, key)}, dsn_envs={env: value}."""
    env = []
    for k, (sname, skey) in (secret_envs or {}).items():
        env.append({"name": k, "valueFrom": {"secretKeyRef": {"name": sname, "key": skey}}})
    for k, v in (dsn_envs or {}).items():
        env.append({"name": k, "value": v})
    return {
        "metadata": {"name": name},
        "spec": {"template": {"spec": {"containers": [{"env": env}]}}},
    }


def _run_sync_namespace_db(deploy, namespace, known_db):
    """Прогон sync_namespace с замоканными upsert/kubectl/known_db.
    Возвращает (svc_calls, edge_calls)."""
    with patch("app.knowledge_graph.kg_sync._kubectl_get_deployments", return_value=[deploy]), \
         patch("app.knowledge_graph.kg_sync._upsert_service_pg") as mock_svc, \
         patch("app.knowledge_graph.kg_sync.upsert_edge") as mock_edge, \
         patch("app.knowledge_graph.kg_sync._known_db_node_namespaces", return_value=known_db) as mock_known, \
         patch("app.knowledge_graph.kg_sync._refresh_stale_class_for_namespace", return_value=0):
        mock_svc.return_value = MagicMock()
        sync_namespace(db=MagicMock(), namespace=namespace)
    return mock_svc.call_args_list, mock_edge.call_args_list, mock_known


def test_secret_hint_unverified_when_no_known_db_node():
    """secret_hint host не найден среди реальных db-узлов → confidence=unverified_host
    + unverified_host=True, namespace остаётся own (узел всё равно создаём)."""
    deploy = _mk_db_deploy("town-service", secret_envs={
        "DB_URL": ("postgres-town-secret", "url"),
    })
    svc_calls, edge_calls, mock_known = _run_sync_namespace_db(deploy, "prod-kingdom1", {})
    mock_known.assert_called_once()  # реестр загружен лениво
    db_edge = [c for c in edge_calls if c.kwargs.get("kind") == "uses_db"]
    assert len(db_edge) == 1
    ex = db_edge[0].kwargs["extras"]
    assert ex["confidence"] == "unverified_host"
    assert ex["unverified_host"] is True
    # db-узел создан в own_namespace
    db_svc = [c for c in svc_calls if str(c.kwargs.get("name", "")).startswith("db:")]
    assert db_svc[0].kwargs["namespace"] == "prod-kingdom1"


def test_secret_hint_reuses_canonical_ns_when_known():
    """secret_hint host совпал с уже существующим db-узлом → переиспользуем его
    канонический namespace (не плодим per-ns дубль), confidence inferred_secret_name."""
    deploy = _mk_db_deploy("town-service", secret_envs={
        "DB_URL": ("postgres-town-secret", "url"),
    })
    known = {"db:postgres:town": "prod-shared"}  # реальный узел живёт в shared
    svc_calls, edge_calls, _ = _run_sync_namespace_db(deploy, "prod-kingdom1", known)
    db_svc = [c for c in svc_calls if str(c.kwargs.get("name", "")).startswith("db:")]
    assert db_svc[0].kwargs["namespace"] == "prod-shared"  # канонический, не own
    db_edge = [c for c in edge_calls if c.kwargs.get("kind") == "uses_db"]
    ex = db_edge[0].kwargs["extras"]
    assert ex["confidence"] == "inferred_secret_name"
    assert "unverified_host" not in ex


def test_dsn_env_target_not_marked_unverified():
    """dsn_env host точный (из реального значения) → не трогаем, реестр не нужен."""
    deploy = _mk_db_deploy("town-service", dsn_envs={
        "DB_URL": "postgres://u@finance-db.prod-shared:5432/d",
    })
    svc_calls, edge_calls, mock_known = _run_sync_namespace_db(deploy, "prod-kingdom1", {})
    mock_known.assert_not_called()  # dsn_env не лезет в реестр
    db_edge = [c for c in edge_calls if c.kwargs.get("kind") == "uses_db"]
    ex = db_edge[0].kwargs["extras"]
    assert ex["confidence"] == "inferred_env"
    assert "unverified_host" not in ex


def test_known_db_node_namespaces_picks_lexicographically_minimal():
    """При нескольких namespace с одним db-узлом — детерминированно minimal."""
    from app.knowledge_graph.kg_sync import _known_db_node_namespaces
    fake_db = MagicMock()
    query = fake_db.query.return_value
    query.filter.return_value.all.return_value = [
        ("db:postgres:town", "prod-shared"),
        ("db:postgres:town", "prod-kingdom1"),
        ("db:redis:cache", "prod-kingdom2"),
    ]
    out = _known_db_node_namespaces(fake_db)
    assert out == {"db:postgres:town": "prod-kingdom1", "db:redis:cache": "prod-kingdom2"}


# ── A4: k8s_events_sync ─────────────────────────────────────────────────────


def test_deployment_from_pod_name_standard_deployment():
    from app.knowledge_graph.k8s_events_sync import _deployment_from_pod_name
    assert _deployment_from_pod_name("bot-service-5476d85d74-f626c") == "bot-service"
    # 8-char RS hash + 5-char pod hash — стандарт k8s.
    assert _deployment_from_pod_name("town-service-abc12345-9wxyz") == "town-service"


def test_deployment_from_pod_name_multipart():
    from app.knowledge_graph.k8s_events_sync import _deployment_from_pod_name
    # Многословные имена deployment'ов корректно отделяются
    assert _deployment_from_pod_name("chat-message-service-7d4fdbb455-l6md8") == "chat-message-service"


def test_deployment_from_pod_name_statefulset_pattern():
    """StatefulSet `pg-cluster-0` — не наш кейс (нет 2 hash-суффиксов)."""
    from app.knowledge_graph.k8s_events_sync import _deployment_from_pod_name
    assert _deployment_from_pod_name("pg-cluster-0") is None
    assert _deployment_from_pod_name("redis-0") is None
    assert _deployment_from_pod_name("") is None


def test_parse_k8s_timestamp():
    from app.knowledge_graph.k8s_events_sync import _parse_k8s_timestamp
    dt = _parse_k8s_timestamp("2026-05-16T07:30:03Z")
    assert dt is not None and dt.year == 2026 and dt.minute == 30
    assert _parse_k8s_timestamp(None) is None
    assert _parse_k8s_timestamp("invalid") is None


def test_warn_reasons_includes_critical_diagnostic_events():
    """OOMKilled / FailedScheduling / ImagePullBackOff / Unhealthy — must-have."""
    from app.knowledge_graph.k8s_events_sync import _WARN_REASONS
    for r in ("OOMKilled", "FailedScheduling", "ImagePullBackOff",
              "FailedMount", "BackOff", "CrashLoopBackOff", "Unhealthy",
              "Evicted", "NodeNotReady"):
        assert r in _WARN_REASONS, f"missing diagnostic reason: {r}"


def test_warn_reasons_excludes_info_events():
    """Pulled / Created / Scheduled — info noise, не нужны в KG."""
    from app.knowledge_graph.k8s_events_sync import _WARN_REASONS
    for r in ("Pulled", "Created", "Scheduled", "Started", "Killing"):
        assert r not in _WARN_REASONS


# ── C1+C3: last_seen_at refresh + discovery_sources merge ──────────────────

def test_upsert_edge_sets_last_seen_at_on_create():
    """C1: new edge получает last_seen_at = now."""
    from datetime import datetime
    from app.knowledge_graph.populator import upsert_edge
    from app.knowledge_graph.schema import Service, ServiceEdge

    # in-memory SQLite через fixture conftest.py?
    # Простой smoke через MagicMock + capture аргумента db.add.
    from unittest.mock import MagicMock
    db = MagicMock()
    db.query.return_value.filter.return_value.one_or_none.return_value = None
    src = Service(id=1, namespace="ns", name="src")
    dst = Service(id=2, namespace="ns", name="dst")

    before = datetime.utcnow()
    upsert_edge(db, src=src, dst=dst, kind="calls", discovered_by="kg_sync/env_vars")
    after = datetime.utcnow()

    add_call = db.add.call_args
    assert add_call is not None
    edge_arg = add_call.args[0]
    assert isinstance(edge_arg, ServiceEdge)
    assert edge_arg.last_seen_at is not None
    assert before <= edge_arg.last_seen_at <= after
    # C3: discovery_sources проинициализирован одним источником
    assert edge_arg.extras["discovery_sources"] == ["kg_sync/env_vars"]


def test_upsert_edge_accumulates_discovery_sources():
    """C3: повторный upsert с другим discovered_by — accumulates."""
    from app.knowledge_graph.populator import upsert_edge
    from app.knowledge_graph.schema import Service, ServiceEdge
    from unittest.mock import MagicMock

    # Existing edge с одним источником
    existing = ServiceEdge(
        src_id=1, dst_id=2, kind="calls",
        weight=1,
        extras={"discovery_sources": ["kg_sync/env_vars"], "confidence": "inferred_env"},
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.one_or_none.return_value = existing
    src = Service(id=1, namespace="ns", name="src")
    dst = Service(id=2, namespace="ns", name="dst")

    upsert_edge(db, src=src, dst=dst, kind="calls", discovered_by="kg_sync/nats_env")

    assert sorted(existing.extras["discovery_sources"]) == [
        "kg_sync/env_vars", "kg_sync/nats_env",
    ]


def test_upsert_edge_idempotent_for_same_source():
    """C3: повтор того же источника — без дублирования в discovery_sources."""
    from app.knowledge_graph.populator import upsert_edge
    from app.knowledge_graph.schema import Service, ServiceEdge
    from unittest.mock import MagicMock

    existing = ServiceEdge(
        src_id=1, dst_id=2, kind="calls",
        weight=1,
        extras={"discovery_sources": ["kg_sync/env_vars"]},
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.one_or_none.return_value = existing
    src = Service(id=1, namespace="ns", name="src")
    dst = Service(id=2, namespace="ns", name="dst")

    upsert_edge(db, src=src, dst=dst, kind="calls", discovered_by="kg_sync/env_vars")

    # Не задублился
    assert existing.extras["discovery_sources"] == ["kg_sync/env_vars"]


# ── D2-auto: drift cleanup safety threshold ───────────────────────────────


def test_drift_cleanup_skipped_threshold():
    """Когда drift > max_drift_pct — no-op, защита от false-wipe."""
    from unittest.mock import patch
    from app.knowledge_graph.drift_cleanup import run_drift_cleanup

    # Mock: k8s has 1 ns, kg has 5 ns (4 drift = 80% > 20% threshold).
    with patch("app.knowledge_graph.drift_cleanup._k8s_live_namespaces",
               return_value={"only-this"}):
        db = MagicMock()
        db.query.return_value.distinct.return_value.all.return_value = [
            ("ns-a",), ("ns-b",), ("ns-c",), ("ns-d",), ("only-this",),
        ]
        result = run_drift_cleanup(db, max_drift_pct=20.0, apply=True)

    assert result["skipped_threshold"] is True
    assert result["drift_pct"] == 80.0
    assert result["marked_services"] == 0
    assert result["applied"] is False
    # UPDATE не должен быть вызван
    db.commit.assert_not_called()


def test_drift_cleanup_within_threshold_apply():
    """Когда drift ≤ threshold — UPDATE применяется."""
    from unittest.mock import patch, MagicMock
    from app.knowledge_graph.drift_cleanup import run_drift_cleanup
    from app.knowledge_graph.schema import Service

    # Mock: 5 ns в KG, 4 в k8s → drift 1/5 = 20% (на границе, проходит).
    svc = MagicMock(spec=Service)
    svc.synthetic = False
    svc.metadata_json = None

    with patch("app.knowledge_graph.drift_cleanup._k8s_live_namespaces",
               return_value={"a", "b", "c", "d"}):
        db = MagicMock()
        db.query.return_value.distinct.return_value.all.return_value = [
            ("a",), ("b",), ("c",), ("d",), ("drift-ns",),
        ]
        db.query.return_value.filter.return_value.all.return_value = [svc]
        result = run_drift_cleanup(db, max_drift_pct=25.0, apply=True)

    assert result["skipped_threshold"] is False
    assert result["drift_pct"] == 20.0
    assert result["applied"] is True
    assert result["marked_services"] == 1
    assert svc.synthetic is True
    assert svc.metadata_json["drift_reason"] == "ns_not_in_k8s"
    db.commit.assert_called_once()


def test_drift_cleanup_kubectl_failure_raises():
    """kubectl-failure → RuntimeError. Beat task ловит и логирует."""
    from unittest.mock import patch
    from app.knowledge_graph.drift_cleanup import run_drift_cleanup
    import pytest

    with patch("app.knowledge_graph.drift_cleanup.subprocess.run") as mr:
        mr.return_value = MagicMock(returncode=1, stderr="connection refused")
        with pytest.raises(RuntimeError, match="kubectl get ns failed"):
            run_drift_cleanup(MagicMock(), apply=True)


def test_drift_cleanup_already_marked_idempotent():
    """Services с drift_reason уже помечены — skip повтор."""
    from unittest.mock import patch, MagicMock
    from app.knowledge_graph.drift_cleanup import run_drift_cleanup
    from app.knowledge_graph.schema import Service

    already = MagicMock(spec=Service)
    already.synthetic = True
    already.metadata_json = {"drift_reason": "ns_not_in_k8s"}

    with patch("app.knowledge_graph.drift_cleanup._k8s_live_namespaces",
               return_value={"a", "b", "c", "d"}):
        db = MagicMock()
        db.query.return_value.distinct.return_value.all.return_value = [
            ("a",), ("b",), ("c",), ("d",), ("drift-ns",),
        ]
        db.query.return_value.filter.return_value.all.return_value = [already]
        result = run_drift_cleanup(db, max_drift_pct=25.0, apply=True)

    assert result["marked_services"] == 0  # уже помечен, skip


# ── ChatGPT review #3.1: precedence-based confidence_score ─────────────────


def test_confidence_runtime_beats_inferred():
    """runtime source (precedence 1.0) >> env (precedence 0.5)."""
    from datetime import datetime, timedelta
    from app.knowledge_graph.confidence import confidence_score
    fresh = datetime.utcnow() - timedelta(minutes=5)
    runtime = confidence_score({"discovery_sources": ["kg_sync/otel_runtime"]}, fresh)
    env_only = confidence_score({"discovery_sources": ["kg_sync/env_vars"]}, fresh)
    assert runtime >= 0.9
    assert env_only <= 0.55
    assert runtime > env_only


def test_confidence_ingress_higher_than_secret():
    """k8s manifest declared (0.85) > secret-key heuristic (0.65)."""
    from datetime import datetime, timedelta
    from app.knowledge_graph.confidence import confidence_score
    fresh = datetime.utcnow() - timedelta(minutes=5)
    ingress = confidence_score({"discovery_sources": ["kg_sync/ingress"]}, fresh)
    secret_only = confidence_score({"discovery_sources": ["kg_sync/secret_hint"]}, fresh)
    assert ingress > secret_only


def test_confidence_corroboration_bonus():
    """2 unique sources → +0.10 corroboration; 3+ → +0.20 cap."""
    from datetime import datetime, timedelta
    from app.knowledge_graph.confidence import confidence_score
    fresh = datetime.utcnow() - timedelta(minutes=5)
    single = confidence_score({"discovery_sources": ["kg_sync/env_vars"]}, fresh)
    two = confidence_score({"discovery_sources": ["kg_sync/env_vars", "kg_sync/env_url_v2"]}, fresh)
    three = confidence_score({"discovery_sources": ["kg_sync/env_vars", "kg_sync/env_url_v2", "kg_sync/nats_env"]}, fresh)
    assert two > single
    assert three > two
    # bonus capped at 0.20 — 4-й источник не должен поднимать дальше
    four = confidence_score({"discovery_sources": ["kg_sync/env_vars", "kg_sync/env_url_v2", "kg_sync/nats_env", "kg_sync/dsn_env"]}, fresh)
    # three и four содержат разные tier источников; bonus уже capped
    # → max precedence отвечает за рост (dsn_env = 0.65), не corroboration
    # Минимально требуем: four > three при добавлении более сильного source
    assert four >= three


def test_confidence_unknown_source_below_known():
    """unknown source — _SOURCE_PRECEDENCE_DEFAULT (0.40), ниже weakest known."""
    from datetime import datetime, timedelta
    from app.knowledge_graph.confidence import confidence_score
    fresh = datetime.utcnow() - timedelta(minutes=5)
    unknown = confidence_score({"discovery_sources": ["kg_sync/some_future_thing"]}, fresh)
    env_known = confidence_score({"discovery_sources": ["kg_sync/env_vars"]}, fresh)
    assert unknown < env_known


def test_confidence_stale_runtime_loses_to_fresh_env():
    """Stale runtime (>30d) теряет на freshness multiplier — fresh env-only выигрывает."""
    from datetime import datetime, timedelta
    from app.knowledge_graph.confidence import confidence_score
    stale = datetime.utcnow() - timedelta(days=60)
    fresh = datetime.utcnow() - timedelta(minutes=5)
    stale_runtime = confidence_score({"discovery_sources": ["kg_sync/otel_runtime"]}, stale)
    fresh_env = confidence_score({"discovery_sources": ["kg_sync/env_vars"]}, fresh)
    assert fresh_env > stale_runtime


# ── ChatGPT review #4.3: health score ─────────────────────────────────────


def test_health_perfect_when_no_signals():
    """Сервис без alerts / pod_events / recurrence → score 1.0."""
    from unittest.mock import MagicMock
    from app.knowledge_graph.health_score import compute_health_for_service
    from app.knowledge_graph.schema import Service
    svc = Service(id=1, namespace="ns", name="svc")
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = []
    db.query.return_value.filter.return_value.count.return_value = 0
    # SignalAggregate lookup — нет свежей записи → skip новых компонентов
    db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None
    score, signals = compute_health_for_service(db, svc)
    assert score == 1.0
    assert signals["open_critical"] == 0
    assert signals["open_warning"] == 0
    assert signals["chronic_pod_events"] == 0
    assert signals["recurrence_24h"] == 0
    # Новые компоненты — graceful skip → None
    assert signals["p95_drift_pct"] is None
    assert signals["http_5xx_rate"] is None
    assert signals["deploy_failure_pct"] is None
    assert signals["slo_burn_pct"] is None


def test_health_critical_alert_penalty():
    """1 open critical alert — score падает на 0.40."""
    from unittest.mock import MagicMock
    from app.knowledge_graph.health_score import compute_health_for_service
    from app.knowledge_graph.schema import AlertEvent, Service
    svc = Service(id=1, namespace="ns", name="svc")
    critical = MagicMock(spec=AlertEvent)
    critical.severity = "critical"
    db = MagicMock()
    # query.filter().all() возвращает open alerts (1 critical)
    # query.filter().count() возвращает 0 для pod_events и recurrence
    counts_returns = iter([0, 0])
    db.query.return_value.filter.return_value.all.return_value = [critical]
    db.query.return_value.filter.return_value.count.side_effect = lambda: next(counts_returns)
    db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None
    score, signals = compute_health_for_service(db, svc)
    assert score == 0.60
    assert signals["open_critical"] == 1


def test_health_chronic_pod_event_penalty():
    """Chronic pod_event (count > 1000) → -0.35."""
    from unittest.mock import MagicMock
    from app.knowledge_graph.health_score import compute_health_for_service
    from app.knowledge_graph.schema import Service
    svc = Service(id=1, namespace="ns", name="svc")
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = []
    counts_returns = iter([2, 0])  # 2 chronic events, 0 recurrence
    db.query.return_value.filter.return_value.count.side_effect = lambda: next(counts_returns)
    db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None
    score, signals = compute_health_for_service(db, svc)
    # 1.0 - 2*0.35 = 0.30
    assert abs(score - 0.30) < 0.01
    assert signals["chronic_pod_events"] == 2


def test_health_recurrence_penalty():
    """20 алертов в 24h → 4 group of 5 → -0.40."""
    from unittest.mock import MagicMock
    from app.knowledge_graph.health_score import compute_health_for_service
    from app.knowledge_graph.schema import Service
    svc = Service(id=1, namespace="ns", name="svc")
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = []
    counts_returns = iter([0, 20])  # 0 chronic, 20 recurrences
    db.query.return_value.filter.return_value.count.side_effect = lambda: next(counts_returns)
    db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None
    score, _ = compute_health_for_service(db, svc)
    # 1.0 - (20 // 5) * 0.10 = 1.0 - 4*0.10 = 0.60
    assert abs(score - 0.60) < 0.01


def test_health_score_clamped_to_zero():
    """Множественные penalty не уводят score ниже 0."""
    from unittest.mock import MagicMock
    from app.knowledge_graph.health_score import compute_health_for_service
    from app.knowledge_graph.schema import AlertEvent, Service
    svc = Service(id=1, namespace="ns", name="svc")
    # 5 critical alerts + 3 chronic + 20 recurrence — overflow penalty
    alerts = [MagicMock(spec=AlertEvent, severity="critical") for _ in range(5)]
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = alerts
    counts_returns = iter([3, 20])
    db.query.return_value.filter.return_value.count.side_effect = lambda: next(counts_returns)
    db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None
    score, _ = compute_health_for_service(db, svc)
    assert score == 0.0


# ── расширение health_score: p95/5xx/deploy/slo (2026-05-22) ────────────────


def _mk_clean_db():
    """db-mock без alerts/pod_events/recurrence — чистый baseline 1.0."""
    from unittest.mock import MagicMock
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = []
    db.query.return_value.filter.return_value.count.return_value = 0
    db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None
    return db


def test_health_penalty_high_5xx_rate():
    """5xx rate 3% (>1% trigger) — penalty в районе 0.10 (capped 0.25)."""
    from unittest.mock import patch
    from app.knowledge_graph.health_score import compute_health_for_service
    from app.knowledge_graph.schema import Service
    svc = Service(id=1, namespace="ns", name="svc")
    db = _mk_clean_db()
    # p95 None (нет drift), 5xx = 0.03 (3%), 12 точек (>= 6 min)
    with patch(
        "app.knowledge_graph.health_score._recent_p95_and_5xx",
        return_value=(None, 0.03, 12),
    ), patch(
        "app.knowledge_graph.health_score._baseline_p95",
        return_value=None,
    ), patch(
        "app.knowledge_graph.health_score._latest_signal_aggregate",
        return_value=None,
    ):
        score, signals = compute_health_for_service(db, svc)
    # penalty = (0.03 - 0.01) * 5.0 = 0.10
    assert abs(score - 0.90) < 0.01
    assert signals["http_5xx_rate"] == 0.03


def test_health_penalty_p95_drift():
    """p95 текущее в 2× от baseline (drift 100%, > 50% trigger) — penalty."""
    from unittest.mock import patch
    from app.knowledge_graph.health_score import compute_health_for_service
    from app.knowledge_graph.schema import Service
    svc = Service(id=1, namespace="ns", name="svc")
    db = _mk_clean_db()
    # current 400ms vs baseline 200ms → drift = +100%
    with patch(
        "app.knowledge_graph.health_score._recent_p95_and_5xx",
        return_value=(400.0, None, 10),
    ), patch(
        "app.knowledge_graph.health_score._baseline_p95",
        return_value=200.0,
    ), patch(
        "app.knowledge_graph.health_score._latest_signal_aggregate",
        return_value=None,
    ):
        score, signals = compute_health_for_service(db, svc)
    # penalty = (100 - 50) * 0.005 = 0.25 (на самой границе cap)
    assert abs(score - 0.75) < 0.01
    assert signals["p95_drift_pct"] == 100.0
    # cap проверка: ещё больший drift не должен увести ниже 0.75
    with patch(
        "app.knowledge_graph.health_score._recent_p95_and_5xx",
        return_value=(2000.0, None, 10),
    ), patch(
        "app.knowledge_graph.health_score._baseline_p95",
        return_value=200.0,
    ), patch(
        "app.knowledge_graph.health_score._latest_signal_aggregate",
        return_value=None,
    ):
        score_big, _ = compute_health_for_service(db, svc)
    assert abs(score_big - 0.75) < 0.01  # capped


def test_health_penalty_slo_burn():
    """slo_burn_pct 30% (> 10% trigger) — penalty из kg_signal_aggregates."""
    from unittest.mock import MagicMock, patch
    from app.knowledge_graph.health_score import compute_health_for_service
    from app.knowledge_graph.schema import Service, SignalAggregate
    svc = Service(id=1, namespace="ns", name="svc")
    db = _mk_clean_db()
    agg = MagicMock(spec=SignalAggregate)
    agg.deploy_failure_pct = 0.0    # ниже trigger 20% — no penalty
    agg.slo_burn_pct = 30.0
    with patch(
        "app.knowledge_graph.health_score._recent_p95_and_5xx",
        return_value=(None, None, 0),
    ), patch(
        "app.knowledge_graph.health_score._baseline_p95",
        return_value=None,
    ), patch(
        "app.knowledge_graph.health_score._latest_signal_aggregate",
        return_value=agg,
    ):
        score, signals = compute_health_for_service(db, svc)
    # penalty = (30 - 10) * 0.01 = 0.20 (под cap 0.25)
    assert abs(score - 0.80) < 0.01
    assert signals["slo_burn_pct"] == 30.0
    assert signals["deploy_failure_pct"] == 0.0


def test_health_skips_when_no_metric_data():
    """Меньше 6 точек в kg_service_health за час → graceful skip,
    нет penalty по p95/5xx; нет свежей SignalAggregate → нет penalty
    deploy/slo. Score остаётся 1.0."""
    from unittest.mock import patch
    from app.knowledge_graph.health_score import compute_health_for_service
    from app.knowledge_graph.schema import Service
    svc = Service(id=1, namespace="ns", name="svc")
    db = _mk_clean_db()
    # 3 точки (< 6 min) → helper возвращает (None, None, 3)
    with patch(
        "app.knowledge_graph.health_score._recent_p95_and_5xx",
        return_value=(None, None, 3),
    ), patch(
        "app.knowledge_graph.health_score._baseline_p95",
        return_value=None,
    ), patch(
        "app.knowledge_graph.health_score._latest_signal_aggregate",
        return_value=None,
    ):
        score, signals = compute_health_for_service(db, svc)
    assert score == 1.0
    assert signals["p95_drift_pct"] is None
    assert signals["http_5xx_rate"] is None
    assert signals["deploy_failure_pct"] is None
    assert signals["slo_burn_pct"] is None
