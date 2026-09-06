"""Recent deploy detector — самый ценный фактор корреляции для инцидентов.

«Деплой за ≤60 минут до alert-а» — критический сигнал, который любой
on-call SRE проверяет первым. Делаем его structured fact, чтобы LLM не
гонял эту проверку самостоятельно.

Три исхода, и все три честные:

  * FOUND  — деплой в окне есть. Сила зависит от привязки записи:
    `attribution_scope="service"` (точная запись: build_param TeamCity или
    k8s_rollout самого workload-а) → OBSERVED, confidence 0.95;
    `"namespace"` (ns-broadcast: билд раскатывал namespace целиком, к этому
    сервису запись не привязана) или без маркера → INFERRED, 0.6. За 30
    дней до 06.09.2026 все топ-8 источников kg_deployments были
    ns-broadcast — прежний единый 0.95 приписывал регресс сервису, который
    билд мог и не трогать.
  * ABSENT — источник жив, окно проверено, деплоев нет. 0.95: на это можно
    опираться.
  * UNKNOWN — проверить нечем: kg_deployments упал или не пополняется,
    сервиса нет в графе, у алерта нет времени. Инцидент 2026-08-11: поток
    деплоев стоял сутки, и пустой список рендерился как «деплоев не было —
    вряд ли связано». Теперь это ?, а не ✗.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List

from app.core.timeutil import parse_ts
from app.diagnostics.facts import Fact, FactKind, Verdict
from app.diagnostics.rules.base import Rule
from app.knowledge_graph.epistemic import Epistemic

_LOOKBACK_MINUTES = 60
_SOURCE = "recent_deployments"
_DEFAULT_PROVENANCE = "kg_deployments"

# Точная привязка деплоя к сервису.
_EXACT_CONFIDENCE = 0.95
# Деплой был в namespace, но запись к сервису не привязана. Выше soft-зоны
# критика (0.5), чтобы гипотеза «регресс от деплоя» жила, но ниже точной,
# чтобы уступала любому прямому наблюдению.
_BROADCAST_CONFIDENCE = 0.6

_SCOPE_SERVICE = "service"
_SCOPE_NAMESPACE = "namespace"
_SCOPE_UNKNOWN = "unknown"

_DEPLOY_KEYS = ("name", "repo", "sha", "buildtype_id", "number", "triggered_by", "url")


class RecentDeployRule(Rule):
    name = "RecentDeployRule"
    sources = (_SOURCE,)

    def evaluate(self, ctx: Dict[str, Any]) -> List[Fact]:
        subject = ctx.get("service")
        provenance = ctx.get("deploy_provenance") or _DEFAULT_PROVENANCE

        problem = self.source_problem(ctx, _SOURCE)
        if problem:
            return [Fact.unknown(
                FactKind.RECENT_DEPLOY, problem,
                subject=subject, source_rule=self.name,
                provenance=provenance, window_min=_LOOKBACK_MINUTES,
            )]

        incident_at = parse_ts(ctx.get("incident_starts_at"))
        if incident_at is None:
            return [Fact.unknown(
                FactKind.RECENT_DEPLOY, "no_incident_timestamp",
                subject=subject, source_rule=self.name,
                provenance=provenance, window_min=_LOOKBACK_MINUTES,
            )]

        deploys = ctx.get("recent_deployments") or []
        window = timedelta(minutes=_LOOKBACK_MINUTES)
        nearby: List[Dict[str, Any]] = []
        for d in deploys:
            d_ts = parse_ts(d.get("ts") or d.get("finished_at") or d.get("at"))
            if d_ts is None:
                continue
            delta = incident_at - d_ts
            # Положительная дельта = деплой ДО инцидента. Окно ±60min,
            # но deploy ПОСЛЕ инцидента редко интересен.
            if timedelta(0) <= delta <= window:
                nearby.append({
                    **{k: v for k, v in d.items() if k in _DEPLOY_KEYS},
                    "scope": d.get("attribution_scope") or _SCOPE_UNKNOWN,
                    "minutes_before_incident": int(delta.total_seconds() // 60),
                })

        if nearby:
            exact = [d for d in nearby if d["scope"] == _SCOPE_SERVICE]
            # Точные записи вперёд: рендер берёт deploys[0].
            nearby = exact + [d for d in nearby if d["scope"] != _SCOPE_SERVICE]
            if exact:
                epistemic, confidence, attribution = (
                    Epistemic.OBSERVED, _EXACT_CONFIDENCE, _SCOPE_SERVICE,
                )
                note = None
            else:
                epistemic, confidence, attribution = (
                    Epistemic.INFERRED, _BROADCAST_CONFIDENCE, _SCOPE_NAMESPACE,
                )
                note = (
                    "deploy recorded for the namespace, not attributed to this "
                    "service — the build may not have touched it"
                )
            evidence: Dict[str, Any] = {
                "lookback_minutes": _LOOKBACK_MINUTES,
                "deploys": nearby,
                "attribution": attribution,
            }
            if note:
                evidence["note"] = note
            return [Fact(
                kind=FactKind.RECENT_DEPLOY,
                observed=True,
                confidence=confidence,
                subject=subject,
                evidence=evidence,
                source_rule=self.name,
                verdict=Verdict.FOUND.value,
                epistemic=epistemic.value,
                provenance=f"{provenance}/{attribution}",
                window_min=_LOOKBACK_MINUTES,
            )]

        return [Fact(
            kind=FactKind.RECENT_DEPLOY,
            observed=False,
            confidence=_EXACT_CONFIDENCE,
            subject=subject,
            evidence={
                "lookback_minutes": _LOOKBACK_MINUTES,
                "deploys_checked": len(deploys),
            },
            source_rule=self.name,
            verdict=Verdict.ABSENT.value,
            epistemic=Epistemic.OBSERVED.value,
            provenance=provenance,
            window_min=_LOOKBACK_MINUTES,
        )]
