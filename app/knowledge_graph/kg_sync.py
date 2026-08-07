"""KG topology sync — строит ServiceEdge графа из k8s deployment env vars.

Сканирует deployment'ы в заданных namespace'ах, ищет env vars с URL-паттернами
вида `http://{service}.{namespace}.svc.cluster.local:{port}` или
`http://{service}:{port}` (same-namespace), создаёт `calls`-рёбра.

Запуск:
    python -m app.knowledge_graph.kg_sync          # все namespace из конфига
    python -m app.knowledge_graph.kg_sync preprod  # только preprod-*

Можно также импортировать и вызвать из кода: sync_topology(db, namespaces).
"""
from __future__ import annotations

import logging
import re
import subprocess
import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple, cast

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.knowledge_graph.populator import upsert_edge
from app.knowledge_graph.schema import NODE_KIND_SERVICE, Deployment, Service, ServiceEdge
from app.knowledge_graph.stale_classifier import (
    classify_stale_with_deploys,
)

logger = logging.getLogger(__name__)

# Edge decay config: edges с last_seen_at старше N дней — DELETE.
# Между 7 и N днями — мягко помечаются `inactive=true` в extras (не удаляем,
# нужны историчные для корреляций). 7 — порог soft-mark, не настраивается
# (привязан к классическому SLA-окну инцидентов). N — конфигурируемо.
EDGE_DECAY_DELETE_AFTER_DAYS = 30
EDGE_DECAY_INACTIVE_AFTER_DAYS = 7

# Deadman для edge-decay: не удаляем stale-edges, если удаление затронуло бы
# > этого % всех edges. Зеркалит threshold-abort из
# `drift_cleanup.run_drift_cleanup` (там 20% по namespace). В норме decay
# удаляет единицы рёбер; массовое удаление = симптом сбоя kubectl/кластера
# (часть last_seen_at не обновилась на этом проходе), а не реальной убыли
# топологии. Вместе с fetch-errors guard это защита от wipe графа.
EDGE_DECAY_MAX_DELETE_PCT = 25.0

# ── Per-source freshness guard для edge-decay ───────────────────────────────
#
# last_seen_at рёбер освежают РАЗНЫЕ модули: kg_sync (calls/uses_db/uses_nats
# из env), k8s_topology_resources_sync (serves_traffic/routes_to),
# k8s_ingress_sync (ingress-calls), nats_subjects_sync (subject-edges). Все
# кроме kg_sync — отдельные beat-таски, и они намеренно глотают свои сбои
# (kubectl timeout → return []). Отсюда реальный инцидент: `kubectl get
# services -A` (42МБ JSON) таймаутил каждый тик, serves_traffic тихо
# старели, и decay (видевший только СВОИ fetch-ошибки) стирал целые классы
# топологии без единого алерта. 25%-cap не спасал: рёбра стареют постепенно.
#
# Сигнал «источник свежести жив» выводим из самих данных: если модуль
# последние KG_EDGE_SOURCE_FRESH_HOURS часов не освежил НИ ОДНОГО своего
# ребра (max(last_seen_at) по группе старше окна) — источник считается
# сломанным (упал или рапортует zero fetches), и decay для его рёбер
# пропускается с громким warning. Окно должно быть больше максимального
# интервала beat-тасков (nats_subjects — 6ч), default 24ч.
_EDGE_SOURCE_FRESH_HOURS_DEFAULT = 24

# discovered_by → источник свежести (какой sync-модуль обновляет
# last_seen_at этих рёбер). Неизвестный/пустой discovered_by группируется
# fallback-ом по kind (см. _edge_freshness_source) — kind-aware защита
# работает и для legacy-рёбер.
_EDGE_FRESHNESS_SOURCE_BY_DISCOVERED_BY = {
    "kg_sync/env_vars": "kg_sync",
    "kg_sync/env_url_v2": "kg_sync",
    "kg_sync/nats_env": "kg_sync",
    "kg_sync/dsn_env": "kg_sync",
    "kg_sync/secret_hint": "kg_sync",
    "kg_sync/ingress": "k8s_ingress_sync",
    "kg_sync/nats_subjects_parser": "nats_subjects_sync",
    "k8s_topology_resources/service": "k8s_topology_resources_sync",
    "k8s_topology_resources/ingress": "k8s_topology_resources_sync",
}


def _edge_freshness_source(kind: Optional[str], discovered_by: Optional[str]) -> str:
    """Источник свежести ребра: по discovered_by, fallback — по kind."""
    src = _EDGE_FRESHNESS_SOURCE_BY_DISCOVERED_BY.get(discovered_by or "")
    if src:
        return src
    return f"kind:{kind or 'unknown'}"


def _stale_freshness_sources(db: Session, now: datetime) -> set[str]:
    """Множество источников, не освеживших ни одного ребра за окно.

    Для каждой группы (kind, discovered_by) берём max(last_seen_at),
    сворачиваем в источники через `_edge_freshness_source`. Источник со
    свежим максимумом — жив (он успешно прошёлся и обновил то, что видит);
    источник, у которого ВСЕ рёбра старые — сломан или рапортует zero
    fetches → его рёбра decay-ить нельзя (их возраст — артефакт сбоя).
    """
    from sqlalchemy import func

    from app.config import settings

    fresh_hours = int(getattr(
        settings, "KG_EDGE_SOURCE_FRESH_HOURS", _EDGE_SOURCE_FRESH_HOURS_DEFAULT,
    ))
    cutoff = now - timedelta(hours=fresh_hours)

    rows = (
        db.query(
            ServiceEdge.kind,
            ServiceEdge.discovered_by,
            func.max(ServiceEdge.last_seen_at),
        )
        .group_by(ServiceEdge.kind, ServiceEdge.discovered_by)
        .all()
    )
    latest: Dict[str, Optional[datetime]] = {}
    for kind, dby, max_seen in rows:
        src = _edge_freshness_source(kind, dby)
        cur = latest.get(src)
        if src not in latest or (
            max_seen is not None and (cur is None or max_seen > cur)
        ):
            latest[src] = max_seen
    return {
        src for src, seen in latest.items()
        if seen is None or seen < cutoff
    }

# URL-паттерн: http(s)://service-name(.namespace)?(.svc.cluster.local)?(:port)?
_SVC_URL_RE = re.compile(
    r"https?://([a-z0-9][a-z0-9-]*)(?:\.([a-z0-9][a-z0-9-]*))?(?:\.svc\.cluster\.local)?(?::\d+)?/?$",
    re.IGNORECASE,
)

# Env-name hint: после расширенного scan-а считаем `*_URL` / `*_HOST` /
# `*_ENDPOINT` / `*_ADDR` / `*_SERVICE_HOST` / `*_DSN` сильным сигналом что
# value содержит target host — даже без явной http-схемы.
_ENV_URL_HINT_RE = re.compile(
    r"(?:^|_)(URL|HOST|ENDPOINT|ADDR|SERVICE_HOST|DSN)$",
    re.IGNORECASE,
)

# k8s service-name pattern: lowercase letters/digits/hyphens, длина ≥3.
_SVC_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{2,}$")

# NATS env-pattern: SHARED_NATS_CONNECTION, KINGDOM_NATS_CLIENT_CONNECTION,
# NATS_FOR_CLIENT_SERVICE_CREDS, etc. Значения обычно подставляются runtime
# из ConfigMap-from-file и в env-dump пустые — но НАЛИЧИЕ переменной
# уже сигнал зависимости от NATS-cluster. На графе это синтетический
# узел `nats-shared` / `nats-kingdom`, edge_kind=`uses_nats`.
_NATS_ENV_RE = re.compile(
    r"^(?P<scope>SHARED|KINGDOM)_NATS(?:_CLIENT)?_(?:CONNECTION|CREDS)$"
    r"|^NATS_FOR_(?P<purpose>[A-Z_]+)_(?:CREDS|CONNECTION)$",
    re.IGNORECASE,
)

# Env-prefix к namespace для определения "shared"-кластера NATS.
# prod-kingdom1 → prod-shared, preprod-kingdom2 → preprod-shared, etc.
_NAMESPACE_ENV_PREFIX_RE = re.compile(r"^(prod|preprod|preupdate)(?:-|$)")

# Synthetic-сервисы — по дизайну никогда не имеют edges (backup-cron'ы,
# nats-tools, observability-exporters). Раньше засчитывались в Orphan %
# и раздували её до 33%. Теперь помечаются `synthetic=true` и исключаются
# из `kg_quality_section`. Паттерны намеренно узкие.
_SYNTHETIC_EXACT_NAMES = frozenset({
    "nats-box",
    "nats-client-box",
    "nats-exporter-prometheus-nats-exporter",
    "seq",
    "redis-exporter",
    # G1.1: observability-агенты — никогда не имеют edges (мониторят кластер,
    # не бизнес-сервисы). Без флага засчитывались как pure_orphan и раздували
    # orphan-метрику до 10%.
    "vm-node-exporter",
    "vm-kube-state-metrics",
    "vmagent-vm-victoria-metrics-k8s-stack",
    "vm-victoria-metrics-k8s-stack-kube-controller-manager",
    "vm-victoria-metrics-k8s-stack-kube-etcd",
    "prometheus-kube-prometheus-kubelet",
    "prometheus-kube-prometheus-kube-controller-manager",
    "prometheus-kube-prometheus-kube-etcd",
    "prometheus-kube-prometheus-kube-scheduler",
    # legal-pages — статический сайт без deps (leaf endpoint).
    "legal-pages",
})
_SYNTHETIC_SUFFIXES = ("-db-backup", "-cron")
# G1.1: префиксные паттерны observability (vm-* / prometheus-kube-prometheus-*
# с любым suffix). Удобнее точечно — exact_names выше; здесь только generic
# семейства которые редко имеют edges.


def _is_synthetic_service(name: str) -> bool:
    """True если service по имени попадает под synthetic-паттерн.

    Изолированные cron-backups / NATS-tools / observability-exporters —
    они НЕ ожидают edges, не должны считаться orphan.
    """
    if not name:
        return False
    if name in _SYNTHETIC_EXACT_NAMES:
        return True
    return any(name.endswith(s) for s in _SYNTHETIC_SUFFIXES)


# ── Edge metadata: confidence + semantics ──────────────────────────────────
#
# Все edges из env-parsing помечаем `confidence="inferred_env"` — это
# config-derived, не подтверждено runtime-trace'ами. Будущие L7-источники
# (OTEL spans, VM client-request метрики) будут писать "runtime_seen"
# (через JSON merge — наш upsert_edge сохранит обе пометки если они идут
# из разных passes).
#
# `semantics` (sync|async) выводим из kind:
#   - calls (HTTP/gRPC env URLs)        → sync
#   - uses_nats (NATS pub/sub)          → async
# Когда добавятся reads_from / consumes_kafka — расширим map.

_KIND_TO_SEMANTICS = {
    "calls": "sync",
    "uses_nats": "async",
    "consumes_kafka": "async",
    "reads_from": "sync",
    "uses_db": "sync",  # A2: DSN-targets (postgres/redis/clickhouse/mysql/mongodb)
}

# A2: DSN-схема в начале env-value → driver. Имя driver канонизируется в
# _DSN_DRIVER_CANON (postgresql → postgres; clickhouse+native → clickhouse).
_DSN_RE = re.compile(
    r"^(?P<scheme>postgres(?:ql)?|mysql|mariadb|mongodb|redis|rediss|clickhouse|"
    r"amqp|amqps|elasticsearch)[+\w]*://",
    re.IGNORECASE,
)
_DSN_DRIVER_CANON = {
    "postgresql": "postgres",
    "rediss": "redis",
    "amqps": "amqp",
    "mariadb": "mysql",
    "mongo": "mongodb",
    "rabbit": "amqp",
    "rabbitmq": "amqp",
}

# A2-v2: эвристика по имени Secret. Большинство prod DSN сидят в secretKeyRef,
# не в plain values — RBAC у sre-ai SA на secrets отсутствует (намеренно).
# Парсим имя secret-а (видно через valueFrom без чтения содержимого).
_SECRET_DB_DRIVER_RE = re.compile(
    r"(?<![a-z])(postgres(?:ql)?|mysql|mariadb|mongo(?:db)?|redis|clickhouse|"
    r"amqp|rabbit(?:mq)?|elasticsearch)(?![a-z])",
    re.IGNORECASE,
)
_SECRET_NAME_NOISE = frozenset({
    "secret", "secrets", "creds", "credentials", "credential", "config",
    "url", "dsn", "conn", "connection", "uri", "auth", "password", "passwords",
    "db", "database", "main", "primary", "rw", "ro",
})

# A2-v2: эвристика по `secretKeyRef.key`. Реальные prod-секреты в WO
# называются обобщённо (`database`, `infrastructure`) — driver/host
# по name не угадать. А ключи говорящие: `MV_POSTGRES_DB_CONNECTION`,
# `ANALYTICS_DB_CLICKHOUSE_CONNECTION`, `TOWN_DB_CONNECTION`.
_KEY_DB_DRIVER_TOKEN = re.compile(
    r"^(POSTGRES|PG|MYSQL|MARIADB|MONGO(?:DB)?|REDIS|CLICKHOUSE|AMQP|RABBIT(?:MQ)?)$",
    re.IGNORECASE,
)
_KEY_DB_GENERIC_TOKEN = re.compile(r"^(DB|DATABASE)$", re.IGNORECASE)
_KEY_DB_ENDPOINT_TOKEN = frozenset({"CONNECTION", "CONN", "URI", "URL", "DSN"})
_KEY_DB_NOISE = frozenset({
    "additional", "primary", "main", "rw", "ro", "client", "server",
    "user", "username", "password", "passwd", "host", "port", "name", "id",
    "ssl", "tls", "secret", "key",
})

# Для DB-host hint ослабляем фильтр имени (min 2 chars): в WO ключах
# встречаются короткие хосты типа `mv` (Materialized Views) — это валидно.
_DB_HOST_HINT_RE = re.compile(r"^[a-z][a-z0-9-]+$")


def _inferred_extras(kind: str) -> Dict[str, Any]:
    """Default extras для edge, созданного из env-parsing (config-derived).

    Используется во всех точках где kg_sync создаёт ребро. Будущие
    runtime-источники должны передавать `{"confidence": "runtime_seen", ...}`
    — merge сохранит обе пометки в JSON через upsert_edge.
    """
    return {
        "confidence": "inferred_env",
        "semantics": _KIND_TO_SEMANTICS.get(kind, "unknown"),
    }

# Пропускаем явно внешние/нерелевантные сервисы.
# Это blacklist для фильтрации значений из env-переменных подов,
# а не bind-адрес сервера — B104 здесь ложно-положительный.
_SKIP_SERVICE_NAMES = frozenset({
    "localhost", "127.0.0.1", "0.0.0.0",  # nosec B104 — blacklist value, not a bind call
})
_SKIP_VALUE_FRAGMENTS = ("azure", "google", "openai", "gpt", "amazonaws", "redis", "postgres", "nats")

# Namespace-list для KG-sync: тянется из settings.KG_SCAN_NAMESPACES
# (env-var `KG_SCAN_NAMESPACES`, comma-separated). Если пусто — sync_topology
# делает auto-discovery всех non-system namespaces через `kubectl get ns`.
# Раньше тут был хардкоженный список — выпилили чтобы не утекали в репо
# конкретные имена клиентской инфры.


class KubectlFetchError(RuntimeError):
    """kubectl-вызов реально упал (rc!=0 / timeout / exception / битый JSON) —
    в отличие от валидного пустого namespace.

    Раньше на любой сбой возвращался `[]`, из-за чего sync не мог отличить
    «kubectl упал» от «в namespace нет deployments» и рапортовал
    success-with-0-services (а edge-decay стирал живые рёбра, т.к. их
    last_seen_at не обновился). Теперь fetch-ошибка сигналится исключением,
    вызывающий (`sync_topology`) ловит его per-ns, инкрементит `errors` и
    включает edge-decay deadman. Зеркалит `drift_cleanup._k8s_live_namespaces`,
    который тоже raise'ит на kubectl-failure.
    """


def _kubectl_get_deployments(namespace: str) -> List[Dict[str, Any]]:
    """Вернуть список deployment spec'ов из kubectl.

    Raises `KubectlFetchError` при РЕАЛЬНОМ сбое (rc!=0 / timeout / exception /
    невалидный JSON) — чтобы сломанный fetch был отличим от genuinely-empty ns.
    Пустой список = namespace реально без deployments.
    """
    try:
        result = subprocess.run(
            ["kubectl", "get", "deployments", "-n", namespace, "-o", "json"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as e:
        logger.warning("kg_sync.kubectl_failed ns=%s: %s", namespace, e)
        raise KubectlFetchError(
            f"kubectl get deployments ns={namespace}: {e}"
        ) from e
    if result.returncode != 0:
        logger.warning(
            "kg_sync.kubectl_failed ns=%s rc=%d: %s",
            namespace, result.returncode, result.stderr[:200],
        )
        raise KubectlFetchError(
            f"kubectl get deployments ns={namespace} rc={result.returncode}"
        )
    try:
        return json.loads(result.stdout).get("items", [])
    except (json.JSONDecodeError, AttributeError) as e:
        logger.warning("kg_sync.kubectl_bad_json ns=%s: %s", namespace, e)
        raise KubectlFetchError(
            f"kubectl get deployments ns={namespace}: bad json: {e}"
        ) from e


def _extract_upstreams(
    deploy: Dict[str, Any],
    own_namespace: str,
) -> List[Tuple[str, str]]:
    """LEGACY URL-based extractor (только http(s)://-values).

    Сохранён для backward-compat и существующих тестов. Новый расширенный
    scan делает `_extract_upstreams_extended` с `_parse_host_from_value`.
    """
    envs: List[Dict[str, Any]] = []
    for c in deploy.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []):
        envs += c.get("env") or []

    upstreams: set[Tuple[str, str]] = set()
    for e in envs:
        v = str(e.get("value") or "")
        if not v.startswith("http"):
            continue
        if any(frag in v.lower() for frag in _SKIP_VALUE_FRAGMENTS):
            continue
        m = _SVC_URL_RE.match(v)
        if not m:
            continue
        svc_name = m.group(1).lower()
        ns = m.group(2) or own_namespace
        if svc_name in _SKIP_SERVICE_NAMES:
            continue
        # Игнорируем ссылки на себя же
        own_name = deploy.get("metadata", {}).get("name", "")
        if svc_name == own_name and ns == own_namespace:
            continue
        upstreams.add((svc_name, ns))

    return sorted(upstreams)


# ── Extended env-URL scan (PR B) ────────────────────────────────────────────


def _parse_host_from_value(value: str, allow_no_scheme: bool) -> Optional[Tuple[str, Optional[str]]]:
    """Достать (service_name, namespace_or_None) из env-value.

    Распознаваемые форматы:
      - `https?://svc(:port)?(/path)?`           — explicit scheme
      - `https?://svc.ns(.svc.cluster.local)?...` — explicit scheme + ns
      - `<driver>://user:pass@svc:port/db`        — DSN (postgres/mysql/redis/...)
      - `svc(:port)?` или `svc.ns(:port)?`        — bare host (`allow_no_scheme=True` only,
                                                    e.g. когда env-name=`*_HOST`)

    Возвращает None для:
      - пустой value
      - значений без host-токена (одни цифры, paths, etc.)
      - IP-адресов / localhost-fragments
      - external hostnames без k8s-shape (cloud providers и т.п.)

    Validation на service-name (k8s-shape) делается через `_SVC_NAME_RE` —
    отсекает шумовые слова длиной <3, заглавные, IP, etc.
    """
    v = (value or "").strip()
    if not v:
        return None

    # Срезаем схему:
    if "://" in v:
        _scheme, rest = v.split("://", 1)
        # user:pass@host для DSN-стиля
        if "@" in rest:
            rest = rest.split("@", 1)[1]
    elif allow_no_scheme:
        rest = v
    else:
        return None

    # Cut query: host?param=... → host
    rest = rest.split("?", 1)[0]
    # Cut path: host:port/path → host:port
    rest = rest.split("/", 1)[0]
    # Cut port: host:port → host
    rest = rest.split(":", 1)[0]
    # Cut k8s FQDN suffix
    rest = re.sub(r"\.svc\.cluster\.local$", "", rest)

    if not rest:
        return None

    # Cloud / external — проверяем по HOST части (не full value), чтобы
    # не отсекать legit k8s DSN типа `postgres://finance-db.prod-shared`.
    lower_host = rest.lower()
    if any(frag in lower_host for frag in _SKIP_VALUE_FRAGMENTS):
        return None

    parts = rest.split(".", 1)
    svc = parts[0].lower()
    ns = parts[1].lower() if len(parts) > 1 else None

    if not _SVC_NAME_RE.match(svc):
        return None
    if svc in _SKIP_SERVICE_NAMES:
        return None
    return (svc, ns)


def _extract_upstreams_extended(
    deploy: Dict[str, Any],
    own_namespace: str,
    known_index: Dict[str, set],
) -> List[Tuple[str, str]]:
    """Расширенный env-vars scan: возвращает (svc_name, namespace).

    Шаги для каждого env:
      1. Извлечь host из value (`_parse_host_from_value`). Без http-схемы
         только если env-name матчит `_ENV_URL_HINT_RE`.
      2. Резолвить namespace: если значение содержало `.ns` — берём его;
         иначе `own_namespace`.
      3. **Match only existing**: возвращаем только пары присутствующие в
         `known_index[ns]` — не создаём фейк-ноды для external hostnames.

    `known_index` — map ns → set of service-names в KG. Передаётся из
    `sync_topology` после Pass 1.
    """
    envs: List[Dict[str, Any]] = []
    for c in deploy.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []):
        envs += c.get("env") or []

    own_name = deploy.get("metadata", {}).get("name", "") or ""
    found: set[Tuple[str, str]] = set()
    for e in envs:
        v = str(e.get("value") or "")
        n = str(e.get("name") or "")
        if not v:
            continue
        is_url_hint = bool(_ENV_URL_HINT_RE.search(n))
        host = _parse_host_from_value(v, allow_no_scheme=is_url_hint)
        if host is None:
            continue
        svc, ns = host
        ns = ns or own_namespace
        if svc == own_name and ns == own_namespace:
            continue  # self-reference
        if svc not in known_index.get(ns, ()):
            continue  # match only existing services (фильтр external hosts)
        found.add((svc, ns))
    return sorted(found)


def _parse_db_hint_from_secret_key(
    secret_key: str,
) -> Optional[Tuple[str, str]]:
    """A2-v2: эвристика по `secretKeyRef.key` (более информативна чем name
    в WO-инфре, где имена секретов обобщённые: `database`, `infrastructure`).

    Требует endpoint-marker (CONNECTION / CONN / URI / URL / DSN) — иначе
    это креденшл (PG_USER, ACCESS_TOKEN), не endpoint.

    Driver: первый driver-токен (POSTGRES/PG/MYSQL/MARIADB/MONGO/REDIS/
    CLICKHOUSE/AMQP/RABBIT). Если нет driver-токена, но есть generic
    DB-токен — default postgres (WO в основном Postgres, см. PG_USER/PG_PASSWORD
    в secret/database).

    Host hint — оставшиеся токены key, без noise.

    Examples:
      MV_POSTGRES_DB_CONNECTION         → ("postgres",   "mv")
      ANALYTICS_DB_CLICKHOUSE_CONNECTION → ("clickhouse", "analytics")
      TOWN_DB_CONNECTION                → ("postgres",   "town")   # default driver
      CHAT_MESSAGE_ADDITIONAL_DB_CONNECTION → ("postgres", "chat-message")
      PG_USER                           → None  (нет endpoint-marker)
      ACCESS_TOKEN_SECRET               → None  (нет ни driver, ни DB)
    """
    if not secret_key:
        return None
    tokens_raw = re.split(r"[_.]", secret_key)
    tokens = [t for t in tokens_raw if t]

    # Phase 1: endpoint-marker обязателен (отсекаем PG_USER и т.п.).
    if not any(t.upper() in _KEY_DB_ENDPOINT_TOKEN for t in tokens):
        return None

    driver: Optional[str] = None
    is_dsn_like = False
    host_parts: List[str] = []
    for tok in tokens:
        upper = tok.upper()
        if _KEY_DB_DRIVER_TOKEN.match(upper):
            low = upper.lower()
            if low == "pg":
                driver = "postgres"
            else:
                driver = _DSN_DRIVER_CANON.get(low, low)
            continue
        if _KEY_DB_GENERIC_TOKEN.match(upper):
            is_dsn_like = True
            continue
        if upper in _KEY_DB_ENDPOINT_TOKEN:
            continue
        if tok.lower() in _KEY_DB_NOISE:
            continue
        host_parts.append(tok.lower())

    if driver is None:
        if not is_dsn_like:
            return None
        driver = "postgres"  # WO default — большинство DB Postgres

    if not host_parts:
        return None
    host_hint = "-".join(host_parts)
    if not _DB_HOST_HINT_RE.match(host_hint):
        return None
    return (driver, host_hint)


def _parse_db_hint_from_secret_ref(
    secret_name: str,
    secret_key: str,
) -> Optional[Tuple[str, str]]:
    """Combined heuristic: сначала key (точнее), fallback на name."""
    by_key = _parse_db_hint_from_secret_key(secret_key)
    if by_key is not None:
        return by_key
    return _parse_db_hint_from_secret_name(secret_name)


def _parse_db_hint_from_secret_name(
    secret_name: str,
) -> Optional[Tuple[str, str]]:
    """A2-v2: эвристика по имени Secret для DB-цели.

    БЕЗ чтения содержимого Secret. Имя содержит driver-токен (postgres /
    redis / mongo / mysql / clickhouse / amqp / elasticsearch) — extract
    его + "host hint" из оставшейся части имени (удалив driver и общие
    noise-токены: secret / creds / config / url / dsn / db / ...).

    Примеры:
      postgres-finance-secret    → ("postgres", "finance")
      redis-cache-creds          → ("redis",    "cache")
      mongo-sessions-config      → ("mongodb",  "sessions")
      app-db-credentials         → None  (нет driver-токена)
      finance-postgres-rw-creds  → ("postgres", "finance-rw")
    """
    if not secret_name:
        return None
    m = _SECRET_DB_DRIVER_RE.search(secret_name)
    if not m:
        return None
    raw_driver = m.group(1).lower()
    driver = _DSN_DRIVER_CANON.get(raw_driver, raw_driver)

    # Разбиваем имя на токены и убираем driver + noise. Оставшиеся —
    # host hint (joined обратно через '-' чтобы сохранить порядок).
    parts = re.split(r"[-_.]", secret_name.lower())
    host_parts = [
        p for p in parts
        if p and p != raw_driver and p != driver and p not in _SECRET_NAME_NOISE
    ]
    if not host_parts:
        return None
    host_hint = "-".join(host_parts)
    if not _SVC_NAME_RE.match(host_hint):
        return None
    return (driver, host_hint)


def _extract_db_targets(
    deploy: Dict[str, Any],
    own_namespace: str,
) -> List[Tuple[str, str, str, str]]:
    """A2: возвращает [(db_node, namespace, driver, source)] для DB-edges.

    Два источника (`source`):

    1. `dsn_env` — plain `value` с DSN-схемой (postgres:// / redis:// /
       clickhouse:// / mysql:// / mongodb:// / amqp(s):// / elasticsearch://).
       Точный host из value.

    2. `secret_hint` — `valueFrom.secretKeyRef.name` распознан как DB-secret
       по regex (A2-v2). Host выводится эвристически из имени secret-а
       (без чтения содержимого — RBAC у sre-ai SA на secrets отсутствует
       намеренно). Менее точно: ns = own_namespace, host = derived от имени.

    Канонический формат `db_node`: `db:<driver>:<host>` — отделяет узел
    БД от одноимённого application-deployment.

    External cloud-БД (RDS / CloudSQL / Atlas) исключаются по hostname
    в dsn_env пути; для secret_hint cloud-фильтр не применим (имя
    secret-а не отражает endpoint).
    """
    envs: List[Dict[str, Any]] = []
    for c in deploy.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []):
        envs += c.get("env") or []

    found: set[Tuple[str, str, str, str]] = set()
    for e in envs:
        # === 1. Plain DSN value ===
        v = str(e.get("value") or "").strip()
        if v:
            m = _DSN_RE.match(v)
            if m:
                driver = m.group("scheme").lower()
                driver = _DSN_DRIVER_CANON.get(driver, driver)

                # _parse_host_from_value умеет вырезать user:pass@, порт,
                # path и .svc.cluster.local. Обходим только
                # _SKIP_VALUE_FRAGMENTS-фильтр (там 'redis'/'postgres'
                # помечены как noise для HTTP, но это не наш случай).
                host = _parse_host_from_value(v, allow_no_scheme=False)
                if host is None:
                    rest = v.split("://", 1)[1]
                    if "@" in rest:
                        rest = rest.split("@", 1)[1]
                    rest = rest.split("?", 1)[0].split("/", 1)[0].split(":", 1)[0]
                    rest = re.sub(r"\.svc\.cluster\.local$", "", rest)
                    if rest:
                        lower_host = rest.lower()
                        if not any(frag in lower_host for frag in (
                            "amazonaws", "azure", "googleapis", "cloud.google",
                        )):
                            parts = rest.split(".", 1)
                            svc = parts[0].lower()
                            ns = parts[1].lower() if len(parts) > 1 else None
                            if _SVC_NAME_RE.match(svc) and svc not in _SKIP_SERVICE_NAMES:
                                found.add((
                                    f"db:{driver}:{svc}",
                                    ns or own_namespace,
                                    driver,
                                    "dsn_env",
                                ))
                else:
                    svc, ns = host
                    found.add((
                        f"db:{driver}:{svc}",
                        ns or own_namespace,
                        driver,
                        "dsn_env",
                    ))
                continue  # plain DSN исключает дальнейший parse этого env

        # === 2. secretKeyRef heuristic (A2-v2) ===
        # Сначала key (информативен в WO: MV_POSTGRES_DB_CONNECTION),
        # fallback на name (для других конвенций именования).
        vf = e.get("valueFrom") or {}
        sref = vf.get("secretKeyRef") or {}
        secret_name = sref.get("name") or ""
        secret_key = sref.get("key") or ""
        if not (secret_name or secret_key):
            continue
        hint = _parse_db_hint_from_secret_ref(secret_name, secret_key)
        if hint is None:
            continue
        driver, host_hint = hint
        found.add((
            f"db:{driver}:{host_hint}",
            own_namespace,  # secret может ссылаться на DB в любом ns
            driver,
            "secret_hint",
        ))

    return sorted(found)


def _derive_team_owner(namespace: str) -> Optional[str]:
    """team_owner по namespace — каноничная prefix-таблица из ownership_suggester.

    prod-kingdom1      → "kingdom1"
    preprod-shared     → "shared"
    squad-3-shared     → "squad-3"     (realm принадлежит squad, не "shared")
    squad-19-kingdom2  → "squad-19"
    monitoring/logging → "platform"
    sre-ai             → None

    РАНЬШЕ (до 2026-06-05) тут был отдельный regex, который для squad-стендов
    отдавал суффикс ("shared"/"kingdomN") вместо самого squad-N — отсюда
    рассинхрон с suggest_owner_multi_signal и ~456 squad-сервисов без
    осмысленного owner. Делегируем в единый `_try_prefix_match`, чтобы
    периодический sync (он перезаписывает team_owner при каждом проходе)
    нормализовал все squad-сервисы автоматически.
    """
    # Локальный импорт — избегаем тяжёлого import-time графа зависимостей.
    from app.services.ownership_suggester import _try_prefix_match

    return _try_prefix_match(namespace)


def _env_prefix(namespace: str) -> Optional[str]:
    m = _NAMESPACE_ENV_PREFIX_RE.match(namespace)
    return m.group(1) if m else None


def _extract_nats_clusters(
    deploy: Dict[str, Any],
    own_namespace: str,
) -> List[Tuple[str, str]]:
    """Вернуть [(cluster_name, cluster_namespace)] из NATS-env-имён.

    SHARED_NATS_*    → synthetic-node `nats-shared`  в `<env>-shared` namespace
    KINGDOM_NATS_*   → synthetic-node `nats-kingdom` в собственном namespace
    NATS_FOR_*_*     → synthetic-node `nats-purpose` в `<env>-shared` (общий)

    Значения env-переменных не нужны — здесь регистрируется только сам факт
    зависимости от NATS-cluster.
    """
    envs: List[Dict[str, Any]] = []
    for c in deploy.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []):
        envs += c.get("env") or []

    env_prefix = _env_prefix(own_namespace)
    found: set[Tuple[str, str]] = set()
    for e in envs:
        n = str(e.get("name", "") or "")
        m = _NATS_ENV_RE.match(n)
        if not m:
            continue
        if m.group("scope"):
            scope = m.group("scope").lower()
            if scope == "shared" and env_prefix:
                found.add(("nats-shared", f"{env_prefix}-shared"))
            elif scope == "kingdom":
                # kingdom NATS живёт в этом же namespace
                found.add(("nats-kingdom", own_namespace))
        elif m.group("purpose") and env_prefix:
            # NATS_FOR_X_CREDS — general-purpose, относим в env-shared
            found.add(("nats-purpose", f"{env_prefix}-shared"))
    return sorted(found)


def _upsert_service_pg(
    db: Session,
    namespace: str,
    name: str,
    team_owner: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    synthetic: Optional[bool] = None,
    stale_class: Optional[str] = None,
) -> Service:
    """PG-нативный UPSERT для kg_services.

    INSERT IF NOT EXISTS из populator.upsert_service не апдейтил уже
    существующие строки — labels/team_owner/metadata дрейфили месяцами.
    Здесь делаем `INSERT ... ON CONFLICT (namespace, name) DO UPDATE` —
    обновляем team_owner / metadata_json / updated_at при каждом sync.

    Поля:
      - created_at: только на insert (server default), не апдейтим.
      - updated_at: всегда now() на update.
      - synthetic: обновляем ТОЛЬКО если новое значение False (сервис стал
        реальным — был synthetic, перестал). Обратной деградации не делаем,
        чтобы случайное упущение в `_is_synthetic_service` не стёрло флаг.

    Требует UNIQUE constraint `uq_kg_service_ns_name` на (namespace, name) —
    он есть в schema.py (см. Service.__table_args__).
    TODO: если констрейнта в БД нет (старая инсталляция без миграции) —
    добавить через:
        ALTER TABLE kg_services
        ADD CONSTRAINT uq_kg_service_ns_name UNIQUE (namespace, name);
    """
    now = datetime.utcnow()
    values = {
        "namespace": namespace,
        "name": name,
        "team_owner": team_owner,
        "metadata_json": metadata,
        "synthetic": bool(synthetic) if synthetic is not None else False,
        "stale_class": stale_class,
        "created_at": now,
        "updated_at": now,
    }

    # Set-клауза: обновляем только то что реально меняется в sync-е.
    # team_owner — берём новый если задан (COALESCE для случая когда вызов
    # передал None — оставить существующий).
    set_clause: Dict[str, Any] = {
        "updated_at": now,
    }
    if team_owner is not None:
        set_clause["team_owner"] = team_owner
    # metadata_json НЕ в set_clause: полный overwrite стирал ключи других
    # источников (auto_populator, drift_cleanup, topology-sync). Merge —
    # ниже в Python, зеркалит populator._upsert_service_pg.
    # synthetic: апдейтим только если новое значение False (стал реальным).
    if synthetic is False:
        set_clause["synthetic"] = False
    if stale_class is not None:
        set_clause["stale_class"] = stale_class

    stmt = (
        pg_insert(Service.__table__)
        .values(**values)
        .on_conflict_do_update(
            constraint="uq_kg_service_ns_name",
            set_=set_clause,
        )
        .returning(Service.__table__.c.id)
    )
    result = db.execute(stmt)
    row = result.first()
    # ORM-объект нужен для downstream upsert_edge (использует svc.id).
    # populate_existing — identity-map мог держать stale-инстанс после
    # Core-upsert-а.
    svc = (
        db.query(Service)
        .filter_by(namespace=namespace, name=name, node_kind=NODE_KIND_SERVICE)
        .populate_existing()
        .one()
    )
    if metadata is not None:
        existing_meta: Dict[str, Any] = (
            svc.metadata_json if isinstance(svc.metadata_json, dict) else {}
        )
        merged = dict(existing_meta)
        merged.update(metadata)
        if merged != existing_meta:
            svc.metadata_json = cast(Any, merged)
    if row is not None:
        # flush чтобы downstream видели обновления в этой транзакции.
        db.flush()
    return svc


def _known_db_node_namespaces(db: Session) -> Dict[str, str]:
    """C2: map `db:<driver>:<host>` node-name → namespace где он УЖЕ есть в KG.

    Реестр реальных db-узлов для дедупа secret_hint-таргетов. Физический
    кластер БД один (напр. town-db), но secret_hint строит host эвристикой и
    кладёт узел в own_namespace — отсюда per-namespace фантом-дубли одного
    кластера, раздувающие blast-radius.

    Если узел с тем же каноническим именем уже создан где-то (через точный
    dsn_env или предыдущий sync), переиспользуем тот namespace вместо
    плодения копии. При нескольких — берём «канонический»: lexicographically
    minimal (детерминизм; shared-кластеры обычно в *-shared).
    """
    idx: Dict[str, str] = {}
    rows = (
        db.query(Service.name, Service.namespace)
        .filter(Service.name.like("db:%"))
        .all()
    )
    for name, ns in rows:
        cur = idx.get(name)
        if cur is None or ns < cur:
            idx[name] = ns
    return idx


def sync_namespace(
    db: Session,
    namespace: str,
    deploys: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, int]:
    """Синхронизировать топологию одного namespace.

    `deploys` — опциональный pre-fetched список (для двухпроходного
    sync_topology). Если None, тянем через `_kubectl_get_deployments` сами.
    """
    stats = {"services": 0, "edges": 0, "skipped": 0}
    if deploys is None:
        deploys = _kubectl_get_deployments(namespace)

    src_team = _derive_team_owner(namespace)

    # C2: реестр уже существующих db-узлов — лениво, только когда встретится
    # secret_hint-таргет (host угадан) и нужна дедупликация против реальных
    # узлов. None = ещё не загружали (избегаем лишнего запроса для ns без БД).
    known_db: Optional[Dict[str, str]] = None

    for deploy in deploys:
        name = deploy.get("metadata", {}).get("name", "")
        if not name:
            continue

        src = _upsert_service_pg(
            db, namespace=namespace, name=name, team_owner=src_team,
            synthetic=_is_synthetic_service(name),
        )
        stats["services"] += 1

        # URL-based edges (existing flow)
        upstreams = _extract_upstreams(deploy, namespace)
        for up_svc, up_ns in upstreams:
            dst = _upsert_service_pg(
                db,
                namespace=up_ns,
                name=up_svc,
                team_owner=_derive_team_owner(up_ns),
            )
            upsert_edge(
                db, src=src, dst=dst, kind="calls",
                discovered_by="kg_sync/env_vars",
                extras=_inferred_extras("calls"),
            )
            stats["edges"] += 1

        # NATS-cluster edges (PR — KG enrichment).
        # Synthetic nodes — представляют общий NATS-кластер в `<env>-shared`
        # либо local-kingdom NATS в текущем namespace. team_owner="platform" —
        # явный маркер что это инфра-узел, а не business-service.
        for nats_name, nats_ns in _extract_nats_clusters(deploy, namespace):
            dst = _upsert_service_pg(
                db,
                namespace=nats_ns,
                name=nats_name,
                team_owner="platform",
            )
            upsert_edge(
                db, src=src, dst=dst,
                kind="uses_nats",
                discovered_by="kg_sync/nats_env",
                extras=_inferred_extras("uses_nats"),
            )
            stats["edges"] += 1

        # A2: DB-edges из DSN-схем (plain value) + эвристика по secret-name
        # (A2-v2, когда DSN в valueFrom.secretKeyRef и RBAC не даёт читать).
        # Synthetic-узел `db:<driver>:<host>`, team_owner="data".
        # confidence отражает точность источника: dsn_env — точно (host из
        # реального значения), secret_hint — нестрого (host угадан из имени).
        for db_node, db_ns, driver, source in _extract_db_targets(deploy, namespace):
            edge_extras: Dict[str, Any] = {
                "driver": driver,
                "semantics": _KIND_TO_SEMANTICS["uses_db"],
            }
            if source == "dsn_env":
                # Точный host из реального DSN-значения — доверяем как есть.
                edge_extras["confidence"] = "inferred_env"
            else:
                # C2: secret_hint — host УГАДАН regex'ом из имени secret/ключа,
                # а db_ns всегда == own_namespace. Без проверки это плодит
                # per-namespace фантом-дубли одного физического кластера
                # (напр. db:postgres:town в каждом ns town-db) → раздутый
                # blast-radius. Сверяемся с реестром реальных db-узлов:
                #   - матч → переиспользуем канонический namespace узла
                #     (не плодим копию), confidence остаётся inferred_secret_name;
                #   - нет матча → host не подтверждён ничем; всё равно создаём
                #     (не теряем сигнал), но помечаем confidence='unverified_host'
                #     + unverified_host=True, чтобы консьюмеры (blast-radius)
                #     могли отфильтровать.
                if known_db is None:
                    known_db = _known_db_node_namespaces(db)
                canonical_ns = known_db.get(db_node)
                if canonical_ns is not None:
                    db_ns = canonical_ns
                    edge_extras["confidence"] = "inferred_secret_name"
                else:
                    edge_extras["confidence"] = "unverified_host"
                    edge_extras["unverified_host"] = True
            dst = _upsert_service_pg(
                db,
                namespace=db_ns,
                name=db_node,
                team_owner="data",
                synthetic=True,
            )
            upsert_edge(
                db, src=src, dst=dst,
                kind="uses_db",
                discovered_by=f"kg_sync/{source}",
                extras=edge_extras,
            )
            stats["edges"] += 1

    # KG Coverage #4: пересчитать stale_class для всех сервисов в этом ns.
    # Делаем здесь (а не в _upsert_service_pg), чтобы один SQL-проход по
    # kg_deployments вместо N×запросов.
    _refresh_stale_class_for_namespace(db, namespace)

    return stats


def _refresh_stale_class_for_namespace(db: Session, namespace: str) -> int:
    """Пересчитать ``kg_services.stale_class`` для всех сервисов в namespace.

    Используем max(``kg_deployments.started_at``) per service как «последний
    deploy». Сервисы без deploy → ``last_deploy_at = None``.

    Возвращает количество обновлённых строк.
    """
    from sqlalchemy import func

    services = db.query(Service).filter(Service.namespace == namespace).all()
    if not services:
        return 0

    svc_ids = [s.id for s in services]
    rows = (
        db.query(Deployment.service_id, func.max(Deployment.started_at))
        .filter(Deployment.service_id.in_(svc_ids))
        .group_by(Deployment.service_id)
        .all()
    )
    last_deploy_by_svc: Dict[int, datetime] = {sid: ts for sid, ts in rows}

    updated = 0
    for svc in services:
        svc_id_int: int = cast(int, svc.id)
        last = last_deploy_by_svc.get(svc_id_int)
        new_class = classify_stale_with_deploys(
            name=cast(str, svc.name),
            namespace=cast(str, svc.namespace),
            last_deploy_at=last,
            team_owner=cast(Optional[str], svc.team_owner),
        )
        if svc.stale_class != new_class:
            svc.stale_class = new_class  # type: ignore[assignment]
            updated += 1
    if updated:
        db.flush()
    return updated


_SYSTEM_NAMESPACE_PREFIXES = ("kube-", "openshift-", "cattle-")
_SYSTEM_NAMESPACES = frozenset({"default", "monitoring", "ingress-nginx", "cert-manager"})


def _discover_namespaces() -> List[str]:
    """Auto-discovery: `kubectl get ns` минус system-namespaces.

    Используется когда settings.KG_SCAN_NAMESPACES не выставлен.
    System-namespaces (kube-*, monitoring, cert-manager и т.п.) исключаются.
    """
    try:
        result = subprocess.run(
            ["kubectl", "get", "namespaces", "-o", "jsonpath={.items[*].metadata.name}"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            logger.warning("kg_sync.discover_failed: %s", result.stderr[:200])
            return []
        all_ns = result.stdout.strip().split()
        return [
            ns for ns in all_ns
            if ns not in _SYSTEM_NAMESPACES
            and not any(ns.startswith(p) for p in _SYSTEM_NAMESPACE_PREFIXES)
        ]
    except Exception as e:
        logger.warning("kg_sync.discover_exception: %s", e)
        return []


def _build_known_index(db: Session) -> Dict[str, set]:
    """Map namespace → set of service-names в KG.

    Используется в Pass 2 для фильтрации «match only existing». Без этого
    индекса extended-scan создавал бы фейк-ноды для external hostnames
    (cloud providers, BAS endpoints), которые не имеют отношения к WO-графу.
    """
    from app.knowledge_graph.schema import Service
    idx: Dict[str, set] = {}
    for name, ns in db.query(Service.name, Service.namespace).all():
        idx.setdefault(ns, set()).add(name)
    return idx


def _enrich_calls_edges_for_ns(
    db: Session,
    namespace: str,
    deploys: List[Dict[str, Any]],
    known_index: Dict[str, set],
) -> int:
    """Pass 2: extended env-scan + создание calls-edges только на existing.

    Возвращает count новых edges (попыток upsert — дубликаты идемпотентны).
    """
    from app.knowledge_graph.schema import Service
    edges_count = 0
    for deploy in deploys:
        name = deploy.get("metadata", {}).get("name", "")
        if not name:
            continue
        src = db.query(Service).filter_by(
            namespace=namespace, name=name, node_kind=NODE_KIND_SERVICE,
        ).one_or_none()
        if src is None:
            continue
        for up_svc, up_ns in _extract_upstreams_extended(deploy, namespace, known_index):
            dst = db.query(Service).filter_by(
                namespace=up_ns, name=up_svc, node_kind=NODE_KIND_SERVICE,
            ).one_or_none()
            if dst is None:
                continue
            upsert_edge(
                db, src=src, dst=dst, kind="calls",
                discovered_by="kg_sync/env_url_v2",
                extras=_inferred_extras("calls"),
            )
            edges_count += 1
    return edges_count


def _edge_decay_should_skip(
    total_edges: int,
    to_delete: int,
    has_fetch_errors: bool,
    max_delete_pct: float = EDGE_DECAY_MAX_DELETE_PCT,
) -> Tuple[bool, str]:
    """Deadman-guard для edge-decay. Возвращает (skip, reason).

    skip=True если:
      - has_fetch_errors: sync не полностью наблюдал граф (kubectl упал на
        части ns) → last_seen_at живых edges не обновился, decay удалил бы их
        ложно;
      - delete_pct > max_delete_pct: массовое удаление — симптом сбоя, а не
        реальной убыли топологии (та же логика, что drift_cleanup threshold).

    Чистая функция без БД — тестируется в изоляции.
    """
    if has_fetch_errors:
        return True, "fetch_errors"
    if total_edges <= 0 or to_delete <= 0:
        return False, ""
    delete_pct = 100.0 * to_delete / total_edges
    if delete_pct > max_delete_pct:
        return True, f"delete_pct={delete_pct:.1f}>max={max_delete_pct:.1f}"
    return False, ""


def _decay_stale_edges(
    db: Session,
    delete_after_days: int = EDGE_DECAY_DELETE_AFTER_DAYS,
    inactive_after_days: int = EDGE_DECAY_INACTIVE_AFTER_DAYS,
    has_fetch_errors: bool = False,
) -> Dict[str, int]:
    """Decay для kg_service_edges по last_seen_at.

    Логика:
      1. Edges с last_seen_at < now() - delete_after_days → DELETE.
         Конфигурируется через EDGE_DECAY_DELETE_AFTER_DAYS (default 30).
      2. Edges с last_seen_at < now() - inactive_after_days → soft-mark:
         extras['inactive'] = True, extras['inactivated_at'] = now().
         НЕ удаляем — нужны историчные для корреляций.
      3. Edges «воскресшие» (последний sync обновил last_seen_at — они
         уже свежие) — сюда не попадают; флаг `inactive` чистится в
         основном проходе через `upsert_edge` (см. ниже).

    Deadman: перед мутацией считаем объём удаления. Если sync имел
    fetch-ошибки ИЛИ удаление затронуло бы > EDGE_DECAY_MAX_DELETE_PCT% всех
    edges — весь decay-проход пропускается (skipped_decay=1), ничего не
    удаляем и не помечаем. Защита от wipe графа при kubectl-failure (зеркалит
    drift_cleanup threshold-abort).

    Kind/source-aware guard: рёбра, чей источник свежести (см.
    `_stale_freshness_sources`) не освежил ни одного своего ребра за окно,
    из decay ИСКЛЮЧАЮТСЯ — и из DELETE, и из soft-mark inactive. Их возраст
    — артефакт сбоя источника (kubectl timeout / упавший beat-task), а не
    реальная убыль топологии. Пропуск логируется громко (warning).

    Возвращает stats: {marked_inactive, deleted, skipped_decay,
    stale_sources, blocked_by_source}.
    Revived здесь не считаем — это делает основной проход (см. PR в
    upsert_edge: unset inactive при апдейте last_seen_at).
    """
    now = datetime.utcnow()
    inactive_cutoff = now - timedelta(days=inactive_after_days)
    delete_cutoff = now - timedelta(days=delete_after_days)

    stats: Dict[str, Any] = {
        "marked_inactive": 0, "deleted": 0, "skipped_decay": 0,
        "stale_sources": [], "blocked_by_source": 0,
    }

    # Deadman: считаем объём ПЕРЕД любой мутацией, чтобы решить безопасно ли
    # применять decay. `int(... or 0)` — count() всегда int в реальной БД,
    # обёртка лишь защищает от None экзотических бэкендов.
    total_edges = int(db.query(ServiceEdge).count() or 0)

    # Per-source health: источники, молчащие дольше окна свежести.
    stale_sources = _stale_freshness_sources(db, now)
    stats["stale_sources"] = sorted(stale_sources)

    delete_candidates = (
        db.query(ServiceEdge)
        .filter(ServiceEdge.last_seen_at < delete_cutoff)
        .all()
    )
    eligible_delete: List[ServiceEdge] = []
    blocked_kinds_by_source: Dict[str, set] = {}
    for edge in delete_candidates:
        src_name = _edge_freshness_source(
            cast(Optional[str], edge.kind),
            cast(Optional[str], edge.discovered_by),
        )
        if src_name in stale_sources:
            blocked_kinds_by_source.setdefault(src_name, set()).add(edge.kind)
            stats["blocked_by_source"] += 1
        else:
            eligible_delete.append(edge)

    skip, reason = _edge_decay_should_skip(
        total_edges, len(eligible_delete), has_fetch_errors,
    )
    if skip:
        stats["skipped_decay"] = 1
        logger.warning(
            "kg_sync.edge_decay_skipped reason=%s total_edges=%d would_delete=%d",
            reason, total_edges, len(eligible_delete),
        )
        return stats

    # 1) DELETE старых (>= delete_after_days) с живым источником. Делаем
    #    первым чтобы не помечать как inactive то, что сейчас удалим.
    if eligible_delete:
        ids = [e.id for e in eligible_delete]
        db.query(ServiceEdge).filter(ServiceEdge.id.in_(ids)).delete(
            synchronize_session=False,
        )
    stats["deleted"] = len(eligible_delete)

    # 2) Soft-mark inactive (между inactive_after_days и delete_after_days).
    #    Берём edges без `inactive=true` в extras чтобы не перетирать
    #    inactivated_at при каждом проходе. JSON-merge: сохраняем
    #    существующие ключи (discovery_sources / confidence / semantics).
    #    Тот же source-guard: рёбра сломанного источника не помечаем.
    candidates = (
        db.query(ServiceEdge)
        .filter(
            ServiceEdge.last_seen_at < inactive_cutoff,
            ServiceEdge.last_seen_at >= delete_cutoff,
        )
        .all()
    )
    marked_any = False
    for edge in candidates:
        src_name = _edge_freshness_source(
            cast(Optional[str], edge.kind),
            cast(Optional[str], edge.discovered_by),
        )
        if src_name in stale_sources:
            blocked_kinds_by_source.setdefault(src_name, set()).add(edge.kind)
            stats["blocked_by_source"] += 1
            continue
        ex: Dict[str, Any] = dict(edge.extras or {})
        if ex.get("inactive") is True:
            continue  # уже помечен в предыдущем decay-проходе
        ex["inactive"] = True
        ex["inactivated_at"] = now.isoformat()
        edge.extras = cast(Any, ex)
        stats["marked_inactive"] += 1
        marked_any = True
    if marked_any:
        db.flush()

    # Громкий лог: какие источники молчат и какие kinds из-за этого
    # защищены от decay. Это ЗАМЕТНЫЙ симптом сломанного синка — раньше
    # эрозия шла молча.
    if blocked_kinds_by_source:
        for src_name, kinds in sorted(blocked_kinds_by_source.items()):
            logger.warning(
                "kg_sync.edge_decay_source_stale source=%s kinds=%s "
                "blocked_edges=%d — источник не освежал рёбра дольше окна "
                "свежести, decay для этих kind'ов пропущен (защита от "
                "молчаливой эрозии графа)",
                src_name, sorted(kinds),
                sum(
                    1 for e in delete_candidates + candidates
                    if _edge_freshness_source(
                        cast(Optional[str], e.kind),
                        cast(Optional[str], e.discovered_by),
                    ) == src_name
                ),
            )
    return stats


def _revive_active_edges(db: Session) -> int:
    """Снимает флаг inactive с edges, у которых last_seen_at стал свежим.

    Основной sync-проход (upsert_edge) обновляет last_seen_at = now(),
    но не трогает extras['inactive']. Делаем это здесь: если edge свежее
    inactive-cutoff, но в extras висит inactive=true — снимаем флаг.
    """
    cutoff = datetime.utcnow() - timedelta(days=EDGE_DECAY_INACTIVE_AFTER_DAYS)
    edges = (
        db.query(ServiceEdge)
        .filter(ServiceEdge.last_seen_at >= cutoff)
        .all()
    )
    revived = 0
    for edge in edges:
        ex: Dict[str, Any] = cast(Dict[str, Any], edge.extras) or {}
        if ex.get("inactive") is True:
            new_ex = dict(ex)
            new_ex.pop("inactive", None)
            new_ex.pop("inactivated_at", None)
            edge.extras = cast(Any, new_ex or None)
            revived += 1
    if revived:
        db.flush()
    return revived


def sync_topology(
    db: Session,
    namespaces: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Синхронизировать топологию всех namespace'ов. Коммит — снаружи.

    Два прохода:
      Pass 1: per ns — services (+ synthetic flag) + NATS edges + legacy
              URL-based calls-edges (только http(s)://-values).
      Pass 2: после commit — расширенный env-scan (`*_HOST`/`*_DSN`/etc),
              match только с already-existing services. Это даёт значительно
              больше calls-edges и не создаёт фейк-нодов для external hosts.

    Приоритет источников списка namespaces:
      1. Аргумент `namespaces` (override).
      2. settings.KG_SCAN_NAMESPACES (env-var, comma-separated).
      3. Auto-discovery через `kubectl get ns` минус system-namespaces.
    """
    if namespaces is None:
        from app.config import settings
        raw = (getattr(settings, "KG_SCAN_NAMESPACES", "") or "").strip()
        namespaces = [n.strip() for n in raw.split(",") if n.strip()] if raw else _discover_namespaces()
    if not namespaces:
        logger.warning("kg_sync.no_namespaces_to_scan")
        return {"services": 0, "edges": 0, "namespaces": 0, "errors": 0}

    total: Dict[str, Any] = {
        "services": 0, "edges": 0, "edges_extended": 0,
        "namespaces": 0, "errors": 0,
        "edges_marked_inactive": 0, "edges_deleted": 0, "edges_revived": 0,
        "edge_decay_skipped": False,
    }
    deploys_cache: Dict[str, List[Dict[str, Any]]] = {}

    # ── Pass 1: services + NATS edges + legacy calls ───────────────────
    for ns in namespaces:
        try:
            # fetch-ошибка (KubectlFetchError) прилетает сюда → errors++, ns
            # НЕ кэшируется для Pass 2 и не считается success-with-0.
            deploys = _kubectl_get_deployments(ns)
            deploys_cache[ns] = deploys
            # SAVEPOINT на namespace: flush-ошибка внутри sync_namespace
            # (IntegrityError/DataError) откатывает только этот ns, не весь
            # проход. Без изоляции Session уходит в aborted-состояние и все
            # последующие ns + терминальный db.commit() падают с
            # PendingRollbackError, теряя весь pass. Зеркалит per-item
            # SAVEPOINT из k8s_events_sync.sync_namespace_events.
            with db.begin_nested():
                stats = sync_namespace(db, ns, deploys=deploys)
            total["services"] += stats["services"]
            total["edges"] += stats["edges"]
            total["namespaces"] += 1
            if stats["services"] > 0:
                logger.info("kg_sync.ns_pass1 ns=%s services=%d edges=%d",
                            ns, stats["services"], stats["edges"])
        except Exception as e:
            logger.warning("kg_sync.ns_failed_pass1 ns=%s: %s", ns, e)
            total["errors"] += 1
    db.commit()

    # ── Pass 2: extended env-scan ──────────────────────────────────────
    known_index = _build_known_index(db)
    for ns, deploys in deploys_cache.items():
        try:
            # SAVEPOINT на namespace — как в Pass 1. Без него flush-ошибка
            # (например, конкурентный phantom_db_cleanup удалил db:%-узел,
            # к которому мы цепляем ребро) оставляла Session в aborted-
            # состоянии: последующие ns падали с PendingRollbackError,
            # терминальный db.commit() убивал task, и Pass 3 (revive/decay)
            # не запускался вовсе.
            with db.begin_nested():
                extra = _enrich_calls_edges_for_ns(db, ns, deploys, known_index)
            total["edges_extended"] += extra
            total["edges"] += extra
            if extra > 0:
                logger.info("kg_sync.ns_pass2 ns=%s extended_edges=%d", ns, extra)
        except Exception as e:
            logger.warning("kg_sync.ns_failed_pass2 ns=%s: %s", ns, e)
            total["errors"] += 1
    db.commit()

    # ── Pass 3: edge decay — soft-mark inactive (>7д) + DELETE (>30д) ──
    # Сначала revive: снимаем `inactive` с edges, чьи last_seen_at
    # обновились в Pass 1/2. Затем decay: помечаем/удаляем stale-edges.
    try:
        revived = _revive_active_edges(db)
        # Deadman: если в Pass 1/2 были fetch-ошибки — decay пропускается
        # (last_seen_at части живых edges не обновился, удаление было бы ложным).
        decay_stats = _decay_stale_edges(db, has_fetch_errors=total["errors"] > 0)
        total["edges_revived"] = revived
        total["edges_marked_inactive"] = decay_stats["marked_inactive"]
        total["edges_deleted"] = decay_stats["deleted"]
        total["edge_decay_skipped"] = bool(decay_stats.get("skipped_decay"))
        total["edge_decay_stale_sources"] = decay_stats.get("stale_sources", [])
        total["edge_decay_blocked_by_source"] = decay_stats.get(
            "blocked_by_source", 0,
        )
        if decay_stats.get("skipped_decay"):
            logger.warning(
                "kg_sync.edge_decay_deadman errors=%d — decay пропущен (граф не тронут)",
                total["errors"],
            )
        elif revived or decay_stats["marked_inactive"] or decay_stats["deleted"]:
            logger.info(
                "kg_sync.edge_decay revived=%d marked_inactive=%d deleted=%d "
                "(inactive_after=%dd delete_after=%dd)",
                revived, decay_stats["marked_inactive"], decay_stats["deleted"],
                EDGE_DECAY_INACTIVE_AFTER_DAYS, EDGE_DECAY_DELETE_AFTER_DAYS,
            )
        db.commit()
    except Exception as e:
        logger.warning("kg_sync.edge_decay_failed: %s", e)
        total["errors"] += 1
        db.rollback()

    logger.info("kg_sync.done total=%s", total)
    return total


if __name__ == "__main__":
    import sys
    from app.database import SessionLocal

    # CLI: python -m app.knowledge_graph.kg_sync [prefix-filter]
    # Без аргумента — sync по settings.KG_SCAN_NAMESPACES / auto-discovery.
    # С prefix — фильтрует auto-discovery list по prefix-у.
    filter_prefix = sys.argv[1] if len(sys.argv) > 1 else None
    namespaces: Optional[List[str]] = None
    if filter_prefix:
        discovered = _discover_namespaces()
        namespaces = [ns for ns in discovered if ns.startswith(filter_prefix)]
    # else: sync_topology возьмёт из settings / auto-discovery

    db = SessionLocal()
    try:
        result = sync_topology(db, namespaces)
        print(f"Done: {result}")
    finally:
        db.close()
