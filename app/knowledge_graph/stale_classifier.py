"""Классификатор «stale-class» для kg_services.

Три значения:

* ``active`` — был deploy **самого сервиса** за последние
  ``ACTIVE_WINDOW_DAYS`` (default 30d). ns-broadcast (деплой любого сервиса
  namespace, разосланный всем — см. ``is_ns_broadcast_deploy``) таким
  доказательством НЕ является.
* ``expected_stale`` — давно не катился, но это норма: backup/cron/system
  (`*-backup`, `*-cron`, ns `kube-system`, `monitoring`, …) либо infra/platform
  owner вне ``ACTIVE_WINDOW_DAYS``.
* ``suspicious_stale`` — нет доказанных deploys сервиса за 30d, не
  expected_stale.

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
from typing import Any, Mapping, Optional

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


# ── Атрибуция деплоя: чей это деплой ────────────────────────────────────────
#
# `tc_deploys_to_kg` (app/workers/tasks.py) пишет ОДНУ И ТУ ЖЕ запись
# kg_deployments всем non-synthetic узлам namespace и помечает её
# `extras.namespace_scope = True` — это единственный признак в данных,
# который отличает «в ns что-то каталось» от «катился ЭТОТ сервис». Записи
# БЕЗ маркера (auto_populator по TC-контексту инцидента) привязаны к
# конкретному сервису.
#
# Отсюда исходная беда: `max(started_at)` по kg_deployments сервиса — это
# почти всегда ns-broadcast, поэтому в активно деплоящемся namespace ВСЕ
# сервисы вечно `active`, и классификатор отвечал на вопрос «был ли деплой в
# ns за 30d», а не «катился ли этот сервис». `suspicious_stale` при таком
# входе физически не мог сработать.
NS_BROADCAST_EXTRAS_KEY = "namespace_scope"


def is_ns_broadcast_deploy(extras: Optional[Mapping[str, Any]]) -> bool:
    """Запись kg_deployments — ns-broadcast, а не деплой ЭТОГО сервиса?

    Чистая функция над `Deployment.extras` — чтобы вызывающий (тот, кто
    считает `max(started_at)`) мог разделить доказательства и передать их
    сюда двумя параметрами: `last_service_deploy_at` / `last_ns_deploy_at`.
    """
    if not isinstance(extras, Mapping):
        return False
    return bool(extras.get(NS_BROADCAST_EXTRAS_KEY))


def classify_stale_with_deploys(
    name: str,
    namespace: str,
    last_deploy_at: Optional[datetime],
    *,
    team_owner: Optional[str] = None,
    now: Optional[datetime] = None,
    active_window_days: int = ACTIVE_WINDOW_DAYS,
    infra_expected_days: int = INFRA_EXPECTED_DAYS,
    last_service_deploy_at: Optional[datetime] = None,
    last_ns_deploy_at: Optional[datetime] = None,
) -> str:
    """3-классная классификация.

    Доказательства деплоя приходят одним из двух способов:

    * **разделённые** — ``last_service_deploy_at`` (max ``started_at`` по
      записям БЕЗ ``extras.namespace_scope``, то есть привязанным к самому
      сервису) и/или ``last_ns_deploy_at`` (ns-broadcast). Так надо, и так
      классификатор отвечает именно на «катился ли ЭТОТ сервис»;
    * **слитые** (legacy) — только ``last_deploy_at``: сырой
      ``max(kg_deployments.started_at)`` по service_id, где ns-broadcast и
      свой деплой неразличимы. Это вход, который сегодня даёт
      ``kg_sync._refresh_stale_class_for_namespace`` и
      ``scripts/backfill_ownership``.

    Решение:

    1. ``last_service_deploy_at`` свежее ``active_window_days`` → ``active``.
       Это ЕДИНСТВЕННОЕ доказательство «сервис катился».
    2. name/ns матчат legacy ``_classify_stale`` == ``expected`` → ``expected_stale``.
    3. ``team_owner`` infra/platform/data **и** последний известный deploy
       (любой атрибуции) свежее ``infra_expected_days`` либо его нет вовсе →
       ``expected_stale``.
    4. иначе → ``suspicious_stale``.

    ЯВНАЯ ДЕГРАДАЦИЯ (важно для чтения результата):

    * ns-broadcast **сам по себе `active` не даёт** — он говорит только «в
      namespace что-то каталось». Для сервиса с ns-broadcast'ом и без своих
      записей ответ уходит в правила 2-4, то есть `suspicious_stale` здесь
      читается как «нет ДОКАЗАННО своего деплоя», а не как «мёртв». Ложных
      обвинений в дайджест это не приносит: все его секции
      (``stats_digest._suspicious_stale_action_items`` и drill-down'ы)
      дополнительно требуют ``NOT EXISTS kg_deployments`` за 60d, а
      ns-broadcast-запись такому сервису эту проверку не пройдёт.
    * Завести четвёртое значение (``unknown``) нельзя: enum зафиксирован в
      ``contract.STALE_CLASS_VALUES`` + миграции колонки, а у соседей
      ``expected_stale`` вырезает сервис из app-scope орфан-метрики
      (``contract.compute_orphan_stats``) — «неизвестно» туда мапить нельзя,
      это испортило бы метрику качества.
    * Слитый (legacy) вход трактуется как раньше — свежий timestamp даёт
      ``active``. Это осознанный компромисс: разделить доказательства может
      только вызывающий (у него на руках ``Deployment.extras``, см.
      ``is_ns_broadcast_deploy``), а молча объявить все сервисы активно
      деплоящихся namespace подозрительными — обвинение на масштабе. Тот,
      кто пишет колонку, обязан перейти на разделённые параметры.
    """
    now_dt = _ensure_naive(now) if now is not None else datetime.utcnow()
    active_cutoff = now_dt - timedelta(days=active_window_days)
    infra_cutoff = now_dt - timedelta(days=infra_expected_days)

    svc_naive = (
        _ensure_naive(last_service_deploy_at)
        if last_service_deploy_at is not None else None
    )
    ns_naive = (
        _ensure_naive(last_ns_deploy_at) if last_ns_deploy_at is not None else None
    )
    merged_naive = (
        _ensure_naive(last_deploy_at) if last_deploy_at is not None else None
    )
    # Caller разделил атрибуцию → сырому `last_deploy_at` больше не верим
    # как доказательству «катился сервис».
    attribution_known = svc_naive is not None or ns_naive is not None

    if svc_naive is not None and svc_naive >= active_cutoff:
        return STALE_CLASS_ACTIVE
    if (
        not attribution_known
        and merged_naive is not None
        and merged_naive >= active_cutoff
    ):
        return STALE_CLASS_ACTIVE

    name_class = _classify_stale(name, namespace)
    if name_class == "expected":
        return STALE_CLASS_EXPECTED

    if team_owner and team_owner.lower() in _INFRA_OWNER_TOKENS:
        # infra-сервис: даже без свежего deploy в окне 30d — это норма,
        # если есть какие-то deploys за infra_expected_days. Здесь годится
        # доказательство любой атрибуции: правило и так о снисхождении.
        known = [t for t in (svc_naive, ns_naive, merged_naive) if t is not None]
        last_any = max(known) if known else None
        if last_any is None or last_any >= infra_cutoff:
            return STALE_CLASS_EXPECTED

    return STALE_CLASS_SUSPICIOUS
