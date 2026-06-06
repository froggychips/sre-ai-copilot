"""Классификатор «stale-class» для kg_services.

Три значения:

* ``active`` — был deploy за последние ``ACTIVE_WINDOW_DAYS`` (default 30d).
* ``expected_stale`` — давно не катился, но это норма: backup/cron/system
  (`*-backup`, `*-cron`, ns `kube-system`, `monitoring`, …) либо infra/platform
  owner вне ``ACTIVE_WINDOW_DAYS``.
* ``suspicious_stale`` — нет deploys за 30d, не expected_stale.

Используется в:
  * ``kg_sync.sync_namespace`` — переписывает ``kg_services.stale_class`` на
    каждом sync (idempotent).
  * ``stats_digest.stale_deployments_section`` — читает column как primary,
    fallback на legacy ``_classify_stale`` если column пуст (старая инсталляция
    без свежего sync).
  * dashboards / SQL-запросы — `WHERE stale_class = 'suspicious_stale'`.

Логика legacy ``_classify_stale(name, ns) -> 'expected'|'suspicious'``
осталась прежней (без timestamp-сигнала), вынесена сюда из ``stats_digest``
без изменений; новая 3-классная функция надстройка поверх.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

# ── Константы (вынесены из stats_digest.py) ──────────────────────────────────

# Suffix-эвристика: имя deployment'а заканчивается на `-backup` / `-cron` / …
# `-hl`/`-headless`/`-metrics`/`-replicas`/`-master` — k8s Service-shape под-сервисы
# bitnami-чартов (postgresql/redis): это не деплоящиеся workload'ы, у них никогда
# не будет deploy-recency → иначе ошибочно попадают в suspicious_stale.
_EXPECTED_STALE_NAME_SUFFIXES = (
    "-backup", "-cron", "-cronjob", "-job",
    "-hl", "-headless", "-metrics", "-replicas", "-master",
)

# Infix-эвристика: backup-postgresql, postgres-backup-restore, …
_EXPECTED_STALE_NAME_INFIXES = ("backup-", "-backup-", "-cron-")

# Системные namespace — deployments в них «не катились» по design'у.
# Расширено 2026-06-06: инфра/система/AI-ML namespaces (infra*/statics/sre-ai/
# ai-platform/ai-reviewer/mcp/jupyter/ambassador/tools/default) — их сервисы
# (БД, headless, экспортёры, MCP-серверы) не деплоятся через TC → ошибочно
# классифицировались suspicious_stale при NULL team_owner. cattle-* покрыт
# отдельной startswith-веткой ниже.
_EXPECTED_STALE_NAMESPACES = frozenset({
    "kube-system",
    "cattle-system",
    "monitoring",
    "logging",
    "cert-manager",
    "ingress-nginx",
    "metallb-system",
    "local-path-storage",
    "default",
    "infra",
    "infra-2",
    "statics",
    "sre-ai",
    "ai-platform",
    "ai-reviewer",
    "mcp",
    "jupyter",
    "ambassador",
    "tools",
})

# Infra/platform owner: для них допускается «не катился 60d» если запас
# свежее ``INFRA_EXPECTED_DAYS``. Иначе всё равно suspicious_stale.
_INFRA_OWNER_TOKENS = frozenset({"platform", "infra", "infrastructure", "data"})

# Окно для классификации.
ACTIVE_WINDOW_DAYS = 30
INFRA_EXPECTED_DAYS = 60

# Канонические значения хранятся в `contract.py` (источник истины для
# enum). Здесь ре-экспортируем под старыми именами ради backward-compat
# с импортёрами (`stats_digest`, тесты), которые жили до выноса в contract.
from app.knowledge_graph.contract import (  # noqa: E402 — re-export после module constants
    STALE_CLASS_ACTIVE,
    STALE_CLASS_EXPECTED_STALE as STALE_CLASS_EXPECTED,
    STALE_CLASS_SUSPICIOUS_STALE as STALE_CLASS_SUSPICIOUS,
)
from app.knowledge_graph.contract import STALE_CLASS_VALUES as _CONTRACT_STALE_CLASS_VALUES  # noqa: E402

# Для backward-compat сохраняем tuple-форму (старый API). В новых местах
# использовать `contract.STALE_CLASS_VALUES` (set).
STALE_CLASS_VALUES = tuple(sorted(_CONTRACT_STALE_CLASS_VALUES))


def _classify_stale(name: str, namespace: str) -> str:
    """Legacy 2-value эвристика: ``'expected' | 'suspicious'``.

    Не смотрит на timestamp; нужна для случаев когда последний deploy
    неизвестен (тогда fallback на name/ns). Используется в
    ``stats_digest.stale_deployments_section`` как fallback и в
    ``classify_stale_with_deploys`` как builder.
    """
    if namespace in _EXPECTED_STALE_NAMESPACES:
        return "expected"
    if namespace.startswith("cattle-"):
        return "expected"
    name_lower = name.lower()
    if any(name_lower.endswith(suf) for suf in _EXPECTED_STALE_NAME_SUFFIXES):
        return "expected"
    if any(inf in name_lower for inf in _EXPECTED_STALE_NAME_INFIXES):
        return "expected"
    return "suspicious"


def _ensure_naive(dt: datetime) -> datetime:
    """Сравнения должны быть в одном tz-режиме (naive UTC)."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def classify_stale_with_deploys(
    name: str,
    namespace: str,
    last_deploy_at: Optional[datetime],
    *,
    team_owner: Optional[str] = None,
    now: Optional[datetime] = None,
    active_window_days: int = ACTIVE_WINDOW_DAYS,
    infra_expected_days: int = INFRA_EXPECTED_DAYS,
) -> str:
    """3-классная классификация.

    Решение:

    1. ``last_deploy_at`` < ``active_window_days`` назад → ``active``.
    2. name/ns матчат legacy ``_classify_stale`` == ``expected`` → ``expected_stale``.
    3. ``team_owner`` infra/platform/data **и** ``last_deploy_at`` свежее
       ``infra_expected_days`` (или None — никогда не катился, но infra) →
       ``expected_stale``.
    4. иначе → ``suspicious_stale``.

    Если ``last_deploy_at`` is None (нет ни одного deployment в KG) — fallback:
    name/ns hint → expected_stale; иначе suspicious_stale.
    """
    now_dt = _ensure_naive(now) if now is not None else datetime.utcnow()
    active_cutoff = now_dt - timedelta(days=active_window_days)
    infra_cutoff = now_dt - timedelta(days=infra_expected_days)

    last_naive = _ensure_naive(last_deploy_at) if last_deploy_at is not None else None

    if last_naive is not None and last_naive >= active_cutoff:
        return STALE_CLASS_ACTIVE

    name_class = _classify_stale(name, namespace)
    if name_class == "expected":
        return STALE_CLASS_EXPECTED

    if team_owner and team_owner.lower() in _INFRA_OWNER_TOKENS:
        # infra-сервис: даже без свежего deploy в окне 30d — это норма,
        # если есть какие-то deploys за infra_expected_days.
        if last_naive is None or last_naive >= infra_cutoff:
            return STALE_CLASS_EXPECTED

    return STALE_CLASS_SUSPICIOUS
