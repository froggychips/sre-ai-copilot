"""Отказ базы по правам или по аутентификации.

В live-RCA датасете (24.09.2026) 3 из 31 реального инцидента — выданные не
тем гранты: сервис или мигратор после пересоздания роли/схемы упирается в
«permission denied». Правила на это не было, и в логах такая строка
оставалась невидимой для гипотез.

Подтипы (evidence.subtype):

  * `grants`       — postgres «permission denied for table/schema/…»,
                     «permission denied to <op>», «must be owner of …»,
                     SQLSTATE 42501 «insufficient privilege».
  * `role_missing` — «role "X" does not exist»: роль не создана или снесена.
  * `auth`         — «password authentication failed for user "X"»: это не
                     гранты, а неверный/протухший пароль в Secret — другой
                     фикс, поэтому отдельный подтип.

В evidence — только объект и операция (тип объекта, его имя, имя роли).
Пароли в таких строках postgres не печатает, но строку целиком мы всё равно
не копируем: сообщение драйвера может нести DSN.

Исходы и привязка как у MigrationFailedRule: FOUND по сигналу (без
pod/service — в soft-зоне), ABSENT только если было что просмотреть, без
материала — ни одного факта, упавший источник — ?.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from app.diagnostics.facts import Fact, FactKind
from app.diagnostics.rules.base import Rule

_DENIED_FOR = re.compile(
    r"permission denied for (table|schema|sequence|relation|database|function|view)"
    r"\s+\"?([\w.]+)\"?",
    re.IGNORECASE,
)
_DENIED_TO = re.compile(r"permission denied to (\w+)", re.IGNORECASE)
_MUST_BE_OWNER = re.compile(
    r"must be owner of (table|relation|schema|sequence|database|function|view)"
    r"\s+\"?([\w.]+)\"?",
    re.IGNORECASE,
)
_INSUFFICIENT = re.compile(r"insufficient[_ ]privilege|sqlstate\s*[:=]?\s*42501", re.IGNORECASE)
_ROLE_MISSING = re.compile(r"role\s+\"?([\w.-]+)\"?\s+does not exist", re.IGNORECASE)
_AUTH_FAILED = re.compile(
    r"password authentication failed for user\s+\"?([\w.-]+)\"?", re.IGNORECASE,
)

# Порядок = приоритет подтипа при нескольких сигналах сразу.
_SUBTYPE_CONF = (
    ("grants", 0.9),
    ("role_missing", 0.9),
    ("auth", 0.85),
)
_ABSENT_CONFIDENCE = 0.6
# См. MigrationFailedRule: soft-зона fact_critic для непривязанной находки.
_UNATTRIBUTED_CONFIDENCE = 0.45
_MAX_OBJECTS = 3


class DbPermissionRule(Rule):
    name = "DbPermissionRule"
    sources = ("k8s_summary", "logs_summary")

    def evaluate(self, ctx: Dict[str, Any]) -> List[Fact]:
        text = self.text_haystack(ctx)
        subject = ctx.get("service") or ctx.get("pod") or ctx.get("namespace")

        found: Dict[str, Dict[str, Any]] = {}

        objects = {f"{k.lower()} {n}" for k, n in _DENIED_FOR.findall(text)}
        objects |= {f"{k.lower()} {n}" for k, n in _MUST_BE_OWNER.findall(text)}
        operations = sorted({op.lower() for op in _DENIED_TO.findall(text)})
        if objects or operations or _INSUFFICIENT.search(text):
            found["grants"] = {
                "objects": sorted(objects)[:_MAX_OBJECTS],
                "operations": operations[:_MAX_OBJECTS],
            }

        roles = sorted(set(_ROLE_MISSING.findall(text)))
        if roles:
            found["role_missing"] = {"roles": roles[:_MAX_OBJECTS]}

        users = sorted(set(_AUTH_FAILED.findall(text)))
        if users:
            found["auth"] = {"users": users[:_MAX_OBJECTS]}

        if found:
            subtype, confidence = next((s, c) for s, c in _SUBTYPE_CONF if s in found)
            evidence: Dict[str, Any] = {"subtype": subtype, "subtypes": sorted(found)}
            # Без pod/service логи и снимок — со всего namespace-а: отказ базы
            # у соседа не должен стать жёстким якорем этого инцидента.
            if not (ctx.get("pod") or ctx.get("service")):
                confidence = min(confidence, _UNATTRIBUTED_CONFIDENCE)
                evidence["attribution"] = "unverified"
            for details in found.values():
                evidence.update({k: v for k, v in details.items() if v})
            return [Fact(
                kind=FactKind.DB_PERMISSION,
                observed=True,
                confidence=confidence,
                subject=subject,
                evidence=evidence,
                source_rule=self.name,
            )]

        if not (ctx.get("logs_summary") or ctx.get("k8s_summary")):
            failed = self.failed_sources(ctx)
            if failed:
                return [Fact.unknown(
                    FactKind.DB_PERMISSION,
                    "; ".join(f"{src}: {why}" for src, why in failed.items()),
                    subject=subject, source_rule=self.name,
                )]
            return []
        return [Fact(
            kind=FactKind.DB_PERMISSION,
            observed=False,
            confidence=_ABSENT_CONFIDENCE,
            subject=subject,
            evidence={"note": "no permission/auth errors in scanned logs/snapshot"},
            source_rule=self.name,
        )]
