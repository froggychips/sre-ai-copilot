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
from typing import Any, Dict, List, Optional, Set, Tuple, cast

from sqlalchemy import cast as sa_cast
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.knowledge_graph.contract import shared_namespace_of
from app.knowledge_graph.edge_decay_guard import (
    REASON_UNMAPPED_KIND, SOURCE_KG_SYNC, edge_block_reason, record_source_run,
    unhealthy_sources,
)
from app.knowledge_graph.populator import upsert_edge, upsert_service
from app.knowledge_graph.schema import NODE_KIND_SERVICE, Deployment, Service, ServiceEdge
from app.knowledge_graph.stale_classifier import (
    classify_stale_with_deploys,
    is_ns_broadcast_deploy,
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
# Карта «kind ребра → синхронизатор, отвечающий за его свежесть», сбор
# per-source здоровья и решение «можно ли децаить это ребро» вынесены в
# `edge_decay_guard` — там же расписан инцидент, из-за которого guard
# появился. Здесь только применение.
#
# ВАЖНО: заводишь новый kind ребра — правишь
# `edge_decay_guard.EDGE_KIND_FRESHNESS_SOURCES`, иначе он не будет
# децаиться вовсе (fail-closed) и в логах повиснет `unmapped_kind`.

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
#
# Squad-стенды именуются иначе: `squad-<N>-shared` / `squad-<N>-kingdom<M>`, и
# общий NATS у каждого стенда свой — `squad-14-shared`, а не «squad-shared».
# Поэтому для них префикс включает номер. Пока squad не распознавался,
# `_env_prefix` возвращал None, и в `_extract_nats_clusters` рёбра на
# SHARED_NATS_* / NATS_FOR_*_* не создавались вовсе: у squad оставались только
# kingdom-рёбра (им префикс не нужен). Замер 08.08.2026 — 112 рёбер uses_nats
# на 4874 squad-сервиса против 280 на 292 prod-сервиса; из-за этого squad давал
# 3154 orphan'а из 3578 по всему графу.
# `-qa` — часть префикса, а не хвост: preprod-qa-kingdom2 живёт со своим
# preprod-qa-shared. Без этого NATS-ребро QA-стенда уходило бы в чужой
# preprod-shared, то есть в соседнее окружение.
_NAMESPACE_ENV_PREFIX_RE = re.compile(
    r"^((?:prod|preprod|preupdate|squad-\d+)(?:-qa)?)(?:-|$)",
)

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
    """Upsert логического сервиса — тонкая обёртка над единственным writer-ом.

    Раньше здесь лежала ВТОРАЯ копия `INSERT … ON CONFLICT` со своим литералом
    имени констрейнта. Копии разъехались при переходе на трёхколоночный ключ
    (миграция 20260807_0200, #245): здешний `ON CONFLICT` сослался на удалённое
    имя, и kg_topology_sync падал на КАЖДОМ namespace — 79 ошибок за тик,
    services=0, граф по сервисам не обновлялся сутки. Merge-политика metadata,
    правила synthetic и stale_class при этом всё время дублировались «зеркально»
    — то есть расхождение было вопросом времени и в остальных полях.

    Функция сохранена (её зовут пять мест в этом модуле) и фиксирует ровно одно:
    этот путь заводит ТОЛЬКО логические сервисы, node_kind=service. Workload-узлы
    создаёт k8s_topology_resources_sync своим вызовом того же writer-а.
    """
    return upsert_service(
        db,
        namespace=namespace,
        name=name,
        team_owner=team_owner,
        metadata=metadata,
        synthetic=synthetic,
        node_kind=NODE_KIND_SERVICE,
        stale_class=stale_class,
    )


#: Кэш «существует ли namespace» на время одного прохода sync_all: список
#: namespace за проход не меняется, а спрашивать БД на каждый env-ref дорого.
_EXISTING_NS_CACHE: Optional[Set[str]] = None


def _reset_existing_ns_cache() -> None:
    """Сбросить кэш существующих namespace (вызывается в начале прохода)."""
    global _EXISTING_NS_CACHE
    _EXISTING_NS_CACHE = None


def _db_namespace_for_client(db: Session, client_namespace: str) -> Optional[str]:
    """Namespace, где лежит БД клиента из `client_namespace`, или None.

    `<realm>-shared` по contract.shared_namespace_of, но **только если такой
    namespace в графе действительно есть**. Правило выведено из кластера
    (41 база `config-db-postgresql`, все в `*-shared`), однако имя namespace —
    это соглашение, а не гарантия: применить его вслепую значит сослаться на
    узел, которого может не существовать, и получить ту же ложь, только новую.

    None означает «не знаю» — вызывающий оставит узел в own_namespace и
    понизит confidence. Не знать честнее, чем угадать.
    """
    global _EXISTING_NS_CACHE
    target = shared_namespace_of(client_namespace)
    if target is None:
        return None
    if _EXISTING_NS_CACHE is None:
        _EXISTING_NS_CACHE = {
            ns for (ns,) in db.query(Service.namespace).distinct().all() if ns
        }
    return target if target in _EXISTING_NS_CACHE else None


# УДАЛЕНО 15.08.2026: `_known_db_node_namespaces` — дедуп db-узлов по
# лексикографически минимальному namespace.
#
# Приём сам по себе рабочий и живёт в `k8s_ingress_sync._canonical_host_node_ns`,
# но там он применяется к DNS-именам, а DNS-имя глобально: `api.example.com`
# в мире одно, и сводить его в один узел правильно.
#
# Для БД host — это service-name ВНУТРИ namespace, то есть имя локальное.
# `config` в каждом из 41 `*-shared` — разные физические базы. Сводя их по
# имени, дедуп собрал `db:postgres:config` в `preprod-kingdom1` с 1430 рёбрами
# из 106 namespace и заставил граф утверждать, что прод ходит в базу препрода.
#
# Различие стоит помнить: схлопывать по имени можно ровно тогда, когда имя
# глобально. Замена — `_db_namespace_for_client` (realm → `<realm>-shared`).


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
                # а db_ns приходит как own_namespace. Куда его отнести — вопрос
                # с историей, и оба прежних ответа были неверны.
                #
                # Оставлять в own_namespace нельзя: физический кластер один на
                # realm, и получались per-namespace фантом-копии.
                #
                # Дедуп по «каноническому» узлу (lexicographically minimal ns
                # среди уже существующих) оказался хуже: он сводил РАЗНЫЕ
                # физические базы в одну. Замер 15.08.2026 —
                # `db:postgres:config` жил в `preprod-kingdom1` и собрал 1430
                # рёбер из 106 namespace, тогда как таких баз в кластере 41,
                # по одной на `*-shared`. Граф утверждал, что прод ходит в базу
                # препрода: не «неточно», а неверный факт, причём ровно в
                # blast-radius.
                #
                # Правильный ответ — realm: БД живёт в `<realm>-shared`
                # (contract.shared_namespace_of). Правило выведено из кластера,
                # но вслепую не применяется — если такого namespace в графе нет,
                # остаёмся в own_namespace и честно понижаем confidence.
                resolved_ns = _db_namespace_for_client(db, namespace)
                if resolved_ns is not None:
                    db_ns = resolved_ns
                    edge_extras["confidence"] = "inferred_secret_name"
                    edge_extras["db_namespace_source"] = "realm_shared"
                else:
                    # Realm не распознан или его `*-shared` не существует: host
                    # не подтверждён ничем. Сигнал не теряем, но помечаем, чтобы
                    # консьюмеры (blast-radius) могли отфильтровать.
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

    Доказательства деплоя РАЗДЕЛЕНЫ на два max(``started_at``):

    * ``last_service_deploy_at`` — записи БЕЗ маркера ns-broadcast, т.е.
      «катился именно этот сервис»;
    * ``last_ns_deploy_at`` — записи с ``extras.namespace_scope=True``,
      которые ``tc_deploys_to_kg`` рассылает на ВСЕ сервисы namespace.

    Раньше здесь считался один слитый max, и ns-broadcast делал каждый сервис
    активно деплоящегося namespace вечно ``active`` — классификатор отвечал на
    вопрос «был ли деплой в ns», а не «катился ли этот сервис», из-за чего
    ``suspicious_stale`` физически не мог сработать там, где он и нужен.
    Разделение позволяет ``classify_stale_with_deploys`` выдавать ``active``
    только по собственному деплою сервиса (см. его докстринг).

    Возвращает количество обновлённых строк.
    """
    services = db.query(Service).filter(Service.namespace == namespace).all()
    if not services:
        return 0

    svc_ids = [s.id for s in services]
    # extras — JSON-колонка, и маркер приходится разбирать в Python: предикат
    # по JSON различается между PG и sqlite (тесты гоняются на sqlite), а
    # выборка ограничена сервисами одного namespace, так что это дёшево.
    deploy_rows = (
        db.query(Deployment.service_id, Deployment.started_at, Deployment.extras)
        .filter(Deployment.service_id.in_(svc_ids))
        .all()
    )
    last_service_deploy: Dict[int, datetime] = {}
    last_ns_deploy: Dict[int, datetime] = {}
    for sid, started_at, extras in deploy_rows:
        if started_at is None:
            continue
        bucket = last_ns_deploy if is_ns_broadcast_deploy(extras) else last_service_deploy
        known = bucket.get(sid)
        if known is None or started_at > known:
            bucket[sid] = started_at

    updated = 0
    for svc in services:
        svc_id_int: int = cast(int, svc.id)
        new_class = classify_stale_with_deploys(
            name=cast(str, svc.name),
            namespace=cast(str, svc.namespace),
            # Слитый вход больше не передаём: доказательства разделены ниже,
            # и классификатор при разделённой атрибуции сырому max'у не верит
            # (см. attribution_known в classify_stale_with_deploys).
            last_deploy_at=None,
            last_service_deploy_at=last_service_deploy.get(svc_id_int),
            last_ns_deploy_at=last_ns_deploy.get(svc_id_int),
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
) -> Dict[str, Any]:
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

    Kind/source-aware guard: рёбра, чей источник свежести в этом цикле не
    отработал (см. `edge_decay_guard.unhealthy_sources` — упал, отрапортовал
    fetch-ошибки, вернул подозрительный ноль или давно не освежал ни одного
    своего ребра), из decay ИСКЛЮЧАЮТСЯ — и из DELETE, и из soft-mark
    inactive. Их возраст — артефакт сбоя источника (kubectl timeout /
    упавший beat-task), а не реальная убыль топологии. Fail-closed: kind без
    сопоставленного источника не децаится вовсе. Любой пропуск логируется
    громко (warning со списком kind'ов и причиной).

    Возвращает stats: {marked_inactive, deleted, skipped_decay,
    stale_sources, blocked_by_source, blocked_kinds}.
    Revived здесь не считаем — это делает основной проход (см. PR в
    upsert_edge: unset inactive при апдейте last_seen_at).
    """
    now = datetime.utcnow()
    inactive_cutoff = now - timedelta(days=inactive_after_days)
    delete_cutoff = now - timedelta(days=delete_after_days)

    stats: Dict[str, Any] = {
        "marked_inactive": 0, "deleted": 0, "skipped_decay": 0,
        "stale_sources": [], "blocked_by_source": 0, "blocked_kinds": {},
    }

    # Deadman: считаем объём ПЕРЕД любой мутацией, чтобы решить безопасно ли
    # применять decay. `int(... or 0)` — count() всегда int в реальной БД,
    # обёртка лишь защищает от None экзотических бэкендов.
    total_edges = int(db.query(ServiceEdge).count() or 0)

    # Per-source health за цикл: stats-отчёты синков + фоллбэк по данным.
    unhealthy = unhealthy_sources(db, now)
    stats["stale_sources"] = sorted(unhealthy)

    # reason → {kind: количество заблокированных рёбер}. Копится и по
    # delete-, и по inactive-кандидатам; уходит в warning ниже.
    blocked: Dict[str, Dict[str, int]] = {}

    def _note_blocked(edge: ServiceEdge, why: str) -> None:
        by_kind = blocked.setdefault(why, {})
        kind_name = cast(Optional[str], edge.kind) or "unknown"
        by_kind[kind_name] = by_kind.get(kind_name, 0) + 1
        stats["blocked_by_source"] += 1

    def _log_blocked() -> None:
        """Громкий лог: какие kind'ы и почему выведены из decay. Зовётся на
        ВСЕХ выходах — молчаливый пропуск недопустим, исходная беда была
        именно в отсутствии сигнала."""
        stats["blocked_kinds"] = {r: dict(k) for r, k in blocked.items()}
        for why, by_kind in sorted(blocked.items()):
            if why == REASON_UNMAPPED_KIND:
                logger.warning(
                    "kg_sync.edge_decay_unmapped_kind kinds=%s blocked_edges=%d "
                    "— kind не сопоставлен НИ ОДНОМУ источнику свежести; decay "
                    "пропущен (fail-closed). Добавь kind в "
                    "edge_decay_guard.EDGE_KIND_FRESHNESS_SOURCES",
                    sorted(by_kind), sum(by_kind.values()),
                )
                continue
            logger.warning(
                "kg_sync.edge_decay_source_unhealthy reason=%s kinds=%s "
                "blocked_edges=%d sources=%s — синхронизатор, отвечающий за "
                "свежесть этих kind'ов, в этом цикле не отработал; decay для "
                "них пропущен (защита от молчаливой эрозии графа)",
                why, sorted(by_kind), sum(by_kind.values()),
                sorted(s for s, r in unhealthy.items() if r == why),
            )

    delete_candidates = (
        db.query(ServiceEdge)
        .filter(ServiceEdge.last_seen_at < delete_cutoff)
        .all()
    )
    eligible_delete: List[ServiceEdge] = []
    for edge in delete_candidates:
        block_reason = edge_block_reason(
            cast(Optional[str], edge.kind),
            cast(Optional[str], edge.discovered_by),
            unhealthy,
        )
        if block_reason:
            _note_blocked(edge, block_reason)
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
        # Общий deadman не отменяет per-kind сигнал: если какой-то класс
        # рёбер уже под защитой сломанного синка, это надо видеть и здесь.
        _log_blocked()
        return stats

    # 1) DELETE старых (>= delete_after_days) с живым источником. Делаем
    #    первым чтобы не помечать как inactive то, что сейчас удалим.
    #    Условие по last_seen_at ПОВТОРЯЕТСЯ в WHERE самого DELETE: между
    #    SELECT кандидатов и этим DELETE конкурентный синк (beat-таски
    #    раскиданы по forked-процессам) мог освежить ребро — удалять его
    #    нельзя. Иначе живое ребро исчезает и возвращается следующим тиком:
    #    лишний churn плюс завышенный decay-счётчик, по которому потом
    #    судят о здоровье графа. Считаем удалённое по rowcount, а не по
    #    длине списка кандидатов — иначе счётчик врёт ровно на эту гонку.
    if eligible_delete:
        ids = [cast(int, e.id) for e in eligible_delete]
        stats["deleted"] = int(
            db.query(ServiceEdge)
            .filter(
                ServiceEdge.id.in_(ids),
                ServiceEdge.last_seen_at < delete_cutoff,
            )
            .delete(synchronize_session=False)
            or 0
        )
        if stats["deleted"] != len(ids):
            logger.info(
                "kg_sync.edge_decay_delete_race candidates=%d deleted=%d — "
                "часть кандидатов освежена конкурентным синком между SELECT и "
                "DELETE, они оставлены живыми",
                len(ids), stats["deleted"],
            )

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
        block_reason = edge_block_reason(
            cast(Optional[str], edge.kind),
            cast(Optional[str], edge.discovered_by),
            unhealthy,
        )
        if block_reason:
            _note_blocked(edge, block_reason)
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

    _log_blocked()
    return stats


def _extras_inactive_filter(db: Session) -> Any:
    """SQL-условие «в extras висит inactive=true», диалект-зависимое.

    На PostgreSQL — JSONB-containment `extras @> '{"inactive": true}'`
    (колонка объявлена как json, поэтому нужен cast; оператор `@>` есть
    только у jsonb). На остальных диалектах (SQLite в тестах) —
    `json_extract(extras, '$.inactive')`. Тот же приём диалект-зависимой
    ветки, что в `populator._is_postgresql`.

    Точная проверка всё равно повторяется в Python (`ex.get("inactive") is
    True`) — SQL здесь нужен только чтобы не тянуть в ORM рёбра, которые
    гарантированно не при делах.
    """
    if db.get_bind().dialect.name == "postgresql":
        return sa_cast(ServiceEdge.extras, JSONB).contains({"inactive": True})
    return ServiceEdge.extras["inactive"].as_boolean().is_(True)


def _revive_active_edges(db: Session) -> int:
    """Снимает флаг inactive с edges, у которых last_seen_at стал свежим.

    Основной sync-проход (upsert_edge) обновляет last_seen_at = now(),
    но не трогает extras['inactive']. Делаем это здесь: если edge свежее
    inactive-cutoff, но в extras висит inactive=true — снимаем флаг.

    Отбор ДВУМЯ условиями сразу, оба в SQL: раньше грузились ВСЕ свежие
    рёбра (то есть почти весь граф — десятки тысяч ORM-объектов каждый час)
    ради проверки одного ключа в extras, при том что помеченных `inactive`
    в норме единицы.
    """
    cutoff = datetime.utcnow() - timedelta(days=EDGE_DECAY_INACTIVE_AFTER_DAYS)
    edges = (
        db.query(ServiceEdge)
        .filter(
            ServiceEdge.last_seen_at >= cutoff,
            _extras_inactive_filter(db),
        )
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
    """Синхронизировать топологию всех namespace'ов. Коммитит сам, per-ns.

    Порядок «весь kubectl-I/O → потом SQL» и короткие per-namespace
    транзакции — следствие инцидента 08.08.2026 (см. app/database.py и
    k8s_topology_resources_sync как образец): раньше Pass 1 был одной
    многоминутной транзакцией на ~80 kubectl-вызовов.

    Два прохода:
      Pass 1: per ns — services (+ synthetic flag) + NATS edges + legacy
              URL-based calls-edges (только http(s)://-values).
      Pass 2: после Pass 1 — расширенный env-scan (`*_HOST`/`*_DSN`/etc),
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
        empty = {"services": 0, "edges": 0, "namespaces": 0, "errors": 0}
        # Отчитываемся и о пустом прогоне: 0 просканированных namespace —
        # это ровно тот «подозрительный ноль», из-за которого нельзя
        # децаить наши kind'ы в этом цикле.
        record_source_run(SOURCE_KG_SYNC, empty)
        return empty

    total: Dict[str, Any] = {
        "services": 0, "edges": 0, "edges_extended": 0,
        "namespaces": 0, "errors": 0,
        "edges_marked_inactive": 0, "edges_deleted": 0, "edges_revived": 0,
        "edge_decay_skipped": False,
    }
    deploys_cache: Dict[str, List[Dict[str, Any]]] = {}

    # ── Fetch-фаза: ВЕСЬ kubectl-I/O до первого SQL ────────────────────
    # Инцидент 08.08.2026: Pass 1 был одной многоминутной транзакцией —
    # первый ns открывал её, дальше ~80 kubectl-вызовов (таймаут 15с
    # каждый) шли с открытой транзакцией, и row-locks жили до
    # единственного commit после цикла. idle_in_transaction_session_timeout
    # (app/database.py) такую транзакцию НЕ обрывает — между kubectl'ями
    # сессия успевала выполнить SQL и выглядела «живой». Поэтому внешний
    # I/O выносим ДО транзакции целиком, как в k8s_topology_resources_sync
    # (тест test_topology_sync_reads_k8s_before_touching_db).
    # fetch-ошибка (KubectlFetchError) → errors++, ns НЕ кэшируется для
    # Pass 1/2 и не считается success-with-0.
    for ns in namespaces:
        try:
            deploys_cache[ns] = _kubectl_get_deployments(ns)
        except Exception as e:
            logger.warning("kg_sync.ns_failed_pass1 ns=%s: %s", ns, e)
            total["errors"] += 1

    # ── Pass 1: services + NATS edges + legacy calls ───────────────────
    for ns, deploys in deploys_cache.items():
        try:
            # SAVEPOINT на namespace: flush-ошибка внутри sync_namespace
            # (IntegrityError/DataError) откатывает только этот ns, не весь
            # проход. Без изоляции Session уходит в aborted-состояние и все
            # последующие ns + per-ns db.commit() падают с
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
        # Коммит после КАЖДОГО ns, а не один на весь проход: транзакция
        # живёт ровно SQL-работу одного namespace, локи отпускаются, DDL и
        # соседние писатели не ждут конца всего синка (инцидент 08.08.2026).
        # Атомарность прохода не нужна: sync идемпотентен, повторный прогон
        # дописывает недописанное — образец k8s_topology_resources_sync
        # (_COMMIT_BATCH). После упавшего ns commit тоже безопасен:
        # savepoint уже откатил его работу, pending-изменений нет.
        db.commit()

    # ── Pass 2: extended env-scan ──────────────────────────────────────
    known_index = _build_known_index(db)
    for ns, deploys in deploys_cache.items():
        try:
            # SAVEPOINT на namespace — как в Pass 1. Без него flush-ошибка
            # (например, конкурентный phantom_db_cleanup удалил db:%-узел,
            # к которому мы цепляем ребро) оставляла Session в aborted-
            # состоянии: последующие ns падали с PendingRollbackError,
            # per-ns db.commit() убивал task, и Pass 3 (revive/decay)
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
        # Per-ns commit — те же соображения, что в Pass 1 (инцидент
        # 08.08.2026): kubectl тут уже не зовём (deploys_cache), но тысячи
        # upsert'ов под одним commit — это минуты удержания row-locks.
        db.commit()

    # ── Pass 3: edge decay — soft-mark inactive (>7д) + DELETE (>30д) ──
    # Сначала revive: снимаем `inactive` с edges, чьи last_seen_at
    # обновились в Pass 1/2. Затем decay: помечаем/удаляем stale-edges.
    # Свой stats-отчёт за цикл — до decay: guard решает по нему, можно ли
    # трогать kind'ы, за свежесть которых отвечает сам kg_sync
    # (calls/uses_db/uses_nats из env-scan).
    record_source_run(SOURCE_KG_SYNC, total)
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
        # {reason: {kind: edges}} — видно в логе `kg_sync.done`, какие классы
        # топологии сейчас под защитой и почему.
        total["edge_decay_blocked_kinds"] = decay_stats.get("blocked_kinds", {})
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
