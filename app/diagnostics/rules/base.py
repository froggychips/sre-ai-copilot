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
        """Конкатенированный lower-cased текст всех текстовых полей.

        Используется для regex-сканов: alertname, description, k8s_summary.
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
