"""Recent deploy detector — самый ценный фактор корреляции для инцидентов.

«Деплой за ≤60 минут до alert-а» — критический сигнал, который любой
on-call SRE проверяет первым. Делаем его structured fact, чтобы LLM не
гонял эту проверку самостоятельно.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.diagnostics.facts import Fact, FactKind
from app.diagnostics.rules.base import Rule

_LOOKBACK_MINUTES = 60


def _parse_ts(raw: Any) -> Optional[datetime]:
    """Принимает datetime / ISO-строку / None. Возвращает aware-datetime в UTC."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, str):
        # Поддержка "Z" в конце (стандарт alertmanager).
        s = raw.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


class RecentDeployRule(Rule):
    name = "RecentDeployRule"

    def evaluate(self, ctx: Dict[str, Any]) -> List[Fact]:
        incident_at = _parse_ts(ctx.get("incident_starts_at"))
        deploys = ctx.get("recent_deployments") or []

        if not deploys or incident_at is None:
            return [
                Fact(
                    kind=FactKind.RECENT_DEPLOY,
                    observed=False,
                    confidence=0.7,
                    subject=ctx.get("service"),
                    evidence={"reason": "no_deploys_or_no_timestamp"},
                    source_rule=self.name,
                )
            ]

        window = timedelta(minutes=_LOOKBACK_MINUTES)
        nearby: List[Dict[str, Any]] = []
        for d in deploys:
            d_ts = _parse_ts(d.get("ts") or d.get("finished_at") or d.get("at"))
            if d_ts is None:
                continue
            delta = incident_at - d_ts
            # Положительная дельта = деплой ДО инцидента. Окно ±60min,
            # но deploy ПОСЛЕ инцидента редко интересен.
            if timedelta(0) <= delta <= window:
                nearby.append({
                    **{k: v for k, v in d.items() if k in ("name", "repo", "sha", "buildtype_id", "number")},
                    "minutes_before_incident": int(delta.total_seconds() // 60),
                })

        if nearby:
            return [
                Fact(
                    kind=FactKind.RECENT_DEPLOY,
                    observed=True,
                    confidence=0.95,
                    subject=ctx.get("service"),
                    evidence={
                        "lookback_minutes": _LOOKBACK_MINUTES,
                        "deploys": nearby,
                    },
                    source_rule=self.name,
                )
            ]

        return [
            Fact(
                kind=FactKind.RECENT_DEPLOY,
                observed=False,
                confidence=0.95,
                subject=ctx.get("service"),
                evidence={
                    "lookback_minutes": _LOOKBACK_MINUTES,
                    "deploys_checked": len(deploys),
                },
                source_rule=self.name,
            )
        ]
