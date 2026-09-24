"""Orleans membership: мёртвые записи силосов — хронический фон, а не причина.

Записи мёртвых силосов (`Status = 6`, Dead) в таблице membership остаются
после каждого пересоздания grainhost-а и живут, пока их не вычистят. На живых
данных 24.09.2026 фраза «Orleans membership: есть записи мёртвых силосов»
стояла в 28 из 31 инцидента, которые починил squad-medic, а причиной была в 2.
Без отдельного правила она доходила до модели как обычное наблюдение и тянула
её в гипотезу «сломан кластер Orleans» там, где ломалось совсем другое.

Поэтому само наличие мёртвых записей — хронический сигнал (`chronic=True`):
он есть почти всегда и о текущем инциденте ничего не говорит. Причиной он
становится только со СВЕЖИМ признаком в окне инцидента:

  * текст: силос не активен / вычищен из membership, отказ доставки на
    мёртвый силос (`SyncMapSourceEffects` на вычищенный силос — dev-17
    21.09.2026), падение кворума (только в строке про Orleans);
  * `ctx["orleans_membership"]`: мёртвых записей стало больше, чем было
    до инцидента (`dead_now > dead_before`);
  * `ctx["orleans_health"]` (форма `queries.orleans_health_for`): промахи
    пингов / сбои доставки / таймауты выше порога шума и на +50 % от
    суточной базы — пороги те же, что у детектора аномалий.

Исходы:
  * есть свежий признак → FOUND (`orleans_membership_degraded`);
  * мёртвые записи есть, свежего признака нет → ABSENT с `chronic=True`:
    деградации membership СЕЙЧАС нет, записи — фон. В промпт уходит с
    пометкой «фон, наблюдается постоянно» (см. FactStore.to_prompt_context);
  * нет ни того, ни другого → фактов нет: правило не сообщает об Orleans там,
    где о нём никто не говорил;
  * источник текста помечен в source_status → ABSENT понижается до UNKNOWN
    базовым Rule.run(): «свежих признаков нет» при упавших логах — не факт.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from app.diagnostics.facts import Fact, FactKind
from app.diagnostics.rules.base import Rule
from app.knowledge_graph.epistemic import Epistemic

# Упоминание мёртвых записей. Нужны ОБА признака в одной строке: «Orleans»
# без «мёртвых» — это просто имя фреймворка, «dead» без Orleans — что угодно.
_ORLEANS_RE = re.compile(r"orleans|membership|силос|silo", re.IGNORECASE)
_DEAD_RE = re.compile(
    r"status\s*=\s*6|\bdead\b|defunct|zombie|мёртв\w*|мертв\w*",
    re.IGNORECASE,
)

# Свежие признаки в тексте. Каждый — про текущую работу кластера, а не про
# накопленную историю таблицы.
_FRESH_TEXT_RE = re.compile(
    r"silo\s+\S*\s*(?:is\s+)?not\s+active"
    r"|siloUnavailable"
    r"|(?:not|no longer)\s+(?:found\s+)?in\s+(?:the\s+)?membership"
    r"|вычищен\w*\s+из\s+membership"
    r"|syncmapsourceeffects"
    r"|orleansmessagerejection"
    r"|target silo .* (?:dead|unavailable)",
    re.IGNORECASE,
)
# Кворум — только в строке про Orleans: «quorum» встречается и у Postgres
# (CNPG, синхронные реплики), и у NATS, и там к membership отношения не имеет.
_QUORUM_RE = re.compile(r"quorum|кворум", re.IGNORECASE)

# Пороги шума и относительного роста — из anomaly_detection (_MIN_ABS и
# +50 % рендера orleans_health). Держим их рядом с метриками, которые реально
# говорят о membership: промахи пингов, сбои доставки, таймауты.
_HEALTH_FLOORS: Dict[str, float] = {
    "orleans_pings_missed_rate": 0.5,
    "orleans_messaging_fault_rate": 1.0,
    "orleans_timedout_rate": 0.5,
}
_HEALTH_MIN_DELTA_PCT = 50.0

_PROVENANCE = "orleans_membership"


def _dead_record_lines(text: str) -> List[str]:
    """Строки, где одновременно Orleans/membership и мёртвые записи."""
    return [
        line.strip() for line in text.splitlines()
        if _ORLEANS_RE.search(line) and _DEAD_RE.search(line)
    ]


def _fresh_in(text: str) -> List[str]:
    hits = [m.group(0) for m in _FRESH_TEXT_RE.finditer(text)]
    for line in text.splitlines():
        if _ORLEANS_RE.search(line):
            hits.extend(m.group(0) for m in _QUORUM_RE.finditer(line))
    return hits


def _fresh_text_hits(text: str, events: List[Dict[str, Any]]) -> List[str]:
    hits = _fresh_in(text)
    for e in events:
        if not isinstance(e, dict):
            continue
        hits.extend(_fresh_in(f"{e.get('reason') or ''} {e.get('message') or ''}"))
    return hits


def _membership_growth(ctx: Dict[str, Any]) -> Optional[Dict[str, int]]:
    """Рост числа мёртвых записей за окно инцидента, если его кто-то измерил."""
    m = ctx.get("orleans_membership")
    if not isinstance(m, dict):
        return None
    now, before = m.get("dead_now"), m.get("dead_before")
    if isinstance(now, bool) or isinstance(before, bool):
        return None
    if isinstance(now, int) and isinstance(before, int) and now > before:
        return {"dead_now": now, "dead_before": before}
    return None


def _health_spikes(ctx: Dict[str, Any]) -> Dict[str, float]:
    """Метрики силоса, которые выше шума И заметно выше суточной базы."""
    health = ctx.get("orleans_health")
    if not isinstance(health, dict) or not health.get("present"):
        return {}
    latest = health.get("latest") or {}
    deltas = health.get("deltas_pct") or {}
    baseline = health.get("baseline") or {}
    out: Dict[str, float] = {}
    for metric, floor in _HEALTH_FLOORS.items():
        value = latest.get(metric)
        if not isinstance(value, (int, float)) or value < floor:
            continue
        delta = deltas.get(metric)
        base = baseline.get(metric)
        # База нулевая или её нет — рост от нуля до уровня выше шума и есть
        # всплеск; иначе нужен относительный рост.
        if not base or (isinstance(delta, (int, float)) and delta >= _HEALTH_MIN_DELTA_PCT):
            out[metric] = float(value)
    return out


class OrleansMembershipRule(Rule):
    name = "OrleansMembershipRule"
    sources = ("k8s_summary", "logs_summary", "k8s_events")

    def evaluate(self, ctx: Dict[str, Any]) -> List[Fact]:
        text = self.text_haystack(ctx)
        events = ctx.get("k8s_events") or []
        subject = ctx.get("service")

        dead_lines = _dead_record_lines(text)
        fresh_text = _fresh_text_hits(text, events)
        growth = _membership_growth(ctx)
        spikes = _health_spikes(ctx)

        if fresh_text or growth or spikes:
            return [Fact(
                kind=FactKind.ORLEANS_MEMBERSHIP_DEGRADED,
                observed=True,
                confidence=0.8,
                subject=subject,
                evidence={
                    "fresh_signals": sorted({h.lower() for h in fresh_text})[:5],
                    "dead_records_growth": growth,
                    "health_spikes": spikes,
                    "dead_records_mentioned": bool(dead_lines),
                },
                source_rule=self.name,
                epistemic=Epistemic.OBSERVED.value,
                provenance=_PROVENANCE,
            )]
        if dead_lines:
            return [Fact(
                kind=FactKind.ORLEANS_MEMBERSHIP_DEGRADED,
                observed=False,
                confidence=0.7,
                subject=subject,
                evidence={
                    "chronic": True,
                    "background": "dead_silo_records",
                    "note": (
                        "записи мёртвых силосов есть почти всегда; свежих "
                        "признаков деградации membership в окне нет"
                    ),
                },
                source_rule=self.name,
                epistemic=Epistemic.OBSERVED.value,
                provenance=_PROVENANCE,
            )]
        return []
