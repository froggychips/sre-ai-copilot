"""Базовый интерфейс правила и общие хелперы для текстового анализа.

Enriched context — это dict со следующими полями (все optional, кроме
incident):
    incident:               dict — оригинальный alert payload
    namespace:              str
    service:                str | None
    pod:                    str | None
    description:            str  — alert annotations.description
    alertname:              str  — alert labels.alertname
    k8s_summary:            str | None — текст из K8sFacts.collect()
    logs_summary:           str | None — наблюдаемый текст (k8s snapshot/логи)
    analyzer_summary:       str | None — LLM-вывод AnalyzerAgent. НЕ входит в
                            text_haystack: проза модели не должна фабриковать
                            «наблюдаемые» факты (circular fact contamination)
    recent_deployments:     list[dict] — [{name, ts, repo, sha}, ...]
    metrics_summary:        dict | None — namespace health
    incident_starts_at:     datetime | None
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any, Dict, List

from app.diagnostics.facts import Fact


class Rule(ABC):
    """Контракт правила. evaluate() — чистая функция от context → факты."""

    name: str = "abstract"

    @abstractmethod
    def evaluate(self, ctx: Dict[str, Any]) -> List[Fact]:
        ...

    # --- Хелперы для подклассов ----------------------------------------

    @staticmethod
    def text_haystack(ctx: Dict[str, Any]) -> str:
        """Конкатенированный lower-cased текст НАБЛЮДАЕМЫХ текстовых полей.

        Используется для regex-сканов: alertname, description, k8s_summary,
        logs_summary. `analyzer_summary` (LLM-проза) сюда НЕ входит намеренно —
        см. app/diagnostics/incident_ctx.py про circular fact contamination.
        Регистр свёрнут, чтобы не дублировать паттерны вроде `Oom|OOM`.
        """
        parts = [
            ctx.get("alertname", ""),
            ctx.get("description", ""),
            ctx.get("k8s_summary") or "",
            ctx.get("logs_summary") or "",
        ]
        return "\n".join(parts).lower()

    @staticmethod
    def count_matches(haystack: str, pattern: re.Pattern[str]) -> int:
        return len(pattern.findall(haystack))


# Суффиксы k8s-подов: `<workload>-<rs-hash>-<pod-hash>` (Deployment) или
# `<workload>-<ordinal>` (StatefulSet). Сравниваем воркллоады, срезая до
# двух хвостовых токенов.
_MAX_SUFFIX_STRIP = 2


def _base_names(name: str) -> list[str]:
    """[name, name без 1 суффикса, name без 2 суффиксов] — непустые уровни."""
    out = [name]
    parts = name.split("-")
    for depth in range(1, _MAX_SUFFIX_STRIP + 1):
        if len(parts) - depth < 1:
            break
        out.append("-".join(parts[: len(parts) - depth]))
    return out


def same_workload(pod_name: str, target: str) -> bool:
    """Эвристика «под принадлежит той же рабочей нагрузке, что и target».

    target — имя target-пода ИЛИ имя сервиса из label-ов инцидента. Нужна,
    чтобы terminated-state СОСЕДНЕГО сервиса в том же namespace не
    приписывался этому инциденту (false anchor на conf 0.98), но пересозданный
    под того же workload-а (другой rs-hash) по-прежнему матчился.
    """
    if not pod_name or not target:
        return False
    if pod_name == target or pod_name.startswith(target + "-"):
        return True
    pod_bases = _base_names(pod_name)
    target_bases = _base_names(target)
    for i, a in enumerate(pod_bases):
        for j, b in enumerate(target_bases):
            if a != b:
                continue
            # Глубокое (2 суффикса с обеих сторон) совпадение принимаем только
            # для составных имён: иначе town-db-0 и town-service-0 схлопнулись
            # бы в «town».
            if i + j >= 3 and "-" not in a:
                continue
            return True
    return False
