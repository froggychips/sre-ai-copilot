"""Что вообще доходит до LLM-пайплайна.

Третье из трёх условий, при которых `LLM_PIPELINE_ENABLED` можно включить
(два других — E2E-тесты и потолок расхода): «severity-фильтр сужен до
critical + prod-*». Условие записано комментарием в `config.py` с августа,
а в коде фильтра нет — до пайплайна доходит всё, что прошло подавление
шума. Разница считается просто: 50 алертов в минуту против единиц.

Почему это отдельный предохранитель, а не строчка в вебхуке. В пайплайн
ведут два пути — Celery-задача и прямой вызов из вебхука
(`PIPELINE_DIRECT_INVOKE`), и фильтр в одном из них второй просто обходит.
Проверка живёт там же, где хард-гейт и бюджет: на входе в задачу, где
мимо неё не пройти.

Отношение к подавлению шума (`_filter_suppressed`) — ортогональное. То
отвечает на вопрос «этот алерт вообще осмысленный», это — «он достаточно
важен, чтобы тратить на него семь вызовов модели». Шумный critical в
prod-namespace отсеет первое; осмысленный warning в dev — второе.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.config import settings


#: Метка в полезной нагрузке задачи: область действия уже проверена тем,
#: кто задачу поставил. Значение приходит из нашей же очереди, не извне.
SCOPE_APPROVED_KEY = "_scope_approved"


@dataclass(frozen=True)
class ScopeVerdict:
    """`in_scope=False` — инцидент до пайплайна не доходит."""

    in_scope: bool
    reason: str
    severity: str
    namespace: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "in_scope": self.in_scope,
            "reason": self.reason,
            "severity": self.severity,
            "namespace": self.namespace,
        }


def _allowed_severities() -> List[str]:
    raw = getattr(settings, "PIPELINE_SEVERITY_ALLOWLIST", None) or []
    return [str(s).strip().lower() for s in raw if str(s).strip()]


def _allowed_prefixes() -> List[str]:
    raw = getattr(settings, "PIPELINE_NAMESPACE_PREFIXES", None) or []
    return [str(p).strip() for p in raw if str(p).strip()]


def _severity_of(incident_data: Dict[str, Any]) -> str:
    """Severity из инцидента, с оглядкой на labels.

    `Incident.severity` заполняется из `labels["severity"]` с дефолтом
    "unknown", но в пайплайн приходит и dict, собранный другим путём —
    у него поле может отсутствовать. Читаем оба места.
    """
    direct = incident_data.get("severity")
    if isinstance(direct, str) and direct.strip():
        return direct.strip().lower()
    labels = incident_data.get("labels")
    if isinstance(labels, dict):
        label_sev = labels.get("severity")
        if isinstance(label_sev, str) and label_sev.strip():
            return label_sev.strip().lower()
    return "unknown"


def _namespace_of(incident_data: Dict[str, Any]) -> str:
    direct = incident_data.get("namespace")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    labels = incident_data.get("labels")
    if isinstance(labels, dict):
        label_ns = labels.get("namespace")
        if isinstance(label_ns, str) and label_ns.strip():
            return label_ns.strip()
    return ""


def check_scope(incident_data: Optional[Dict[str, Any]]) -> ScopeVerdict:
    """Попадает ли инцидент в область действия пайплайна.

    Инцидент без severity или без namespace НЕ проходит при включённом
    фильтре по этому измерению. Это сознательно: «неизвестно» — не повод
    тратить бюджет, а повод увидеть, что метка не проставлена. Обратное
    решение (пропускать неизвестное) означало бы, что любой алерт без
    лейбла обходит фильтр целиком.
    """
    data = incident_data if isinstance(incident_data, dict) else {}
    severity = _severity_of(data)
    namespace = _namespace_of(data)

    severities = _allowed_severities()
    if severities and severity not in severities:
        return ScopeVerdict(False, "severity_out_of_scope", severity, namespace)

    prefixes = _allowed_prefixes()
    if prefixes and not any(namespace.startswith(p) for p in prefixes):
        return ScopeVerdict(False, "namespace_out_of_scope", severity, namespace)

    return ScopeVerdict(True, "in_scope", severity, namespace)
