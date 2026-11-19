"""Статическое правило шума: алерт × класс namespace × контейнер.

Зачем. Замер 18.11.2026 по `kg_incidents` за 7 дней: 3525 из ~3900
инцидентов — одиночный `CPUThrottlingHigh`, ещё 4759 переоткрытий на них же.
3081 инцидент — стенды (`squad-*`), почти всегда контейнер `metrics`
(сайдкар-экспортер Postgres с крошечным CPU-лимитом). Действия по такому
алерту нет: стенд живёт на общей dev-ноде, лимит сайдкара задан чартом.
Полезный сигнал стендов (KubePodCrashLooping) в этом потоке тонул.

В отличие от остальных видов шума (rollout, meta, gen-mismatch — решение
обогащения по живому состоянию кластера) это правило ЧИСТОЕ: только лейблы
алерта и настройки. Ни SQL, ни kubectl — его можно звать на пути вебхука.

Потребители:
  * `auto_populator.populate_from_incident` — пишет `raw.noise_rule` в
    kg_alerts и передаёт вид шума в `incidents.attach_alert`;
  * читатели kg_alerts (stuck-alerts, дайджесты) — фильтр `raw.noise_rule`;
  * мост remediation может спросить `noise_rule_kind` до сбора фактов.
"""
from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Any, Iterable, Mapping, Optional

from app.config import settings

#: Вид шума в `kg_incidents.extras["noise_fingerprints"]` и `kg_alerts.raw`.
NOISE_KIND_RULE = "static_rule"
#: Ключ в `kg_alerts.raw`: значение — вид шума (сейчас всегда NOISE_KIND_RULE).
RAW_NOISE_KEY = "noise_rule"


def _matches(value: str, patterns: Iterable[str]) -> bool:
    return any(fnmatchcase(value, p) for p in patterns if p)


def noise_rule_kind(
    labels: Mapping[str, Any],
    namespace: Optional[str] = None,
    *,
    service: Optional[str] = None,
) -> Optional[str]:
    """Вид шума по статическому правилу или None.

    Совпадение = alertname из списка И (namespace по шаблону ИЛИ контейнер из
    списка «шум в любом namespace»). Внутри совпавшего namespace список
    `ALERT_NOISE_RULE_CONTAINERS` сужает правило; пустой — любой контейнер.
    Шаблоны — fnmatch, с учётом регистра. Контейнер берётся из лейбла
    `container`; если его нет — сравнивается имя сервиса.
    """
    if not getattr(settings, "ALERT_NOISE_RULE_ENABLED", False):
        return None
    alertname = str(labels.get("alertname") or "")
    if not alertname or alertname not in set(settings.ALERT_NOISE_RULE_ALERTNAMES or []):
        return None
    ns = str(namespace or labels.get("namespace") or "")
    subject = str(labels.get("container") or service or "")

    any_ns_containers = settings.ALERT_NOISE_RULE_CONTAINERS_ANY_NAMESPACE or []
    if subject and _matches(subject, any_ns_containers):
        return NOISE_KIND_RULE
    if not ns or not _matches(ns, settings.ALERT_NOISE_RULE_NAMESPACES or []):
        return None
    containers = settings.ALERT_NOISE_RULE_CONTAINERS or []
    if containers and not (subject and _matches(subject, containers)):
        return None
    return NOISE_KIND_RULE


def is_rule_noise_raw(raw: Any) -> bool:
    """Строка kg_alerts помечена статическим правилом (см. RAW_NOISE_KEY)."""
    return isinstance(raw, dict) and bool(raw.get(RAW_NOISE_KEY))


def reports_exclude_rule_noise() -> bool:
    """Скрывать ли помеченные алерты из stuck-alerts и дайджестов."""
    return bool(getattr(settings, "ALERT_NOISE_RULE_EXCLUDE_FROM_REPORTS", True))
