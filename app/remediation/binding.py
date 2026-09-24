"""Серверный снимок совпадения playbook-ов и привязка к нему intent-а.

До этого модуля привязка держалась на имени: FixAgent писал в intent
`"playbook": "<name>"`, pipeline сверял имя со списком кандидатов, а gate на
apply-пути — только что playbook существует и действие входит в его план.
Имя — вывод модели. Между отбором кандидатов и кликом Approve могли
измениться YAML playbook-а (новый образ), а сам intent мог отличаться от
плана параметрами (`replicas`), которых в плане нет.

Теперь в момент отбора сервер фиксирует СНИМОК: какие playbook-и совпали,
digest их YAML, вердикты preconditions на тот момент, шаги плана и список
verify. У каждой записи снимка есть свой hash (`binding`); intent несёт этот
hash в поле `playbook_match`, и hash входит в подпись intent-а — одобрение
человека покрывает ровно этот снимок. Gate на apply-пути сверяет intent со
снимком из analysis, а не с именем, которое написала модель.

    match_playbooks → build_match_snapshot → analysis["playbook_match"]
                               │
                  intent.playbook_match = entry["binding"]  (pipeline, не LLM)
                               │
      evaluate_intent_gate(intent, match_snapshot=analysis["playbook_match"])
                               └── check_intent_binding
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Mapping, Optional

from app.remediation.matcher import check_preconditions
from app.remediation.playbook import Playbook

if TYPE_CHECKING:
    from app.core.execution_dsl import ExecutionIntent
    from app.diagnostics.facts import FactStore

__all__ = [
    "SNAPSHOT_VERSION",
    "BindingViolation",
    "build_match_snapshot",
    "check_intent_binding",
    "entry_binding",
    "find_entry",
    "playbook_digest",
]

SNAPSHOT_VERSION = "playbook-match/v1"

_TEMPLATE_PREFIX = "{"


class BindingViolation(ValueError):
    """Intent не соответствует серверному снимку. `reason` — код для audit."""

    def __init__(self, reason: str, **extra: Any) -> None:
        super().__init__(reason)
        self.reason = reason
        self.extra = extra


def _digest(obj: Any, length: int) -> str:
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]


def playbook_digest(playbook: Playbook) -> str:
    """Digest содержимого playbook-а: меняется при любой правке YAML.

    Нужен, чтобы intent, одобренный под одну редакцию playbook-а, не
    исполнился под другую (policy ослабили, шаг добавили) после выката
    нового образа.
    """
    return _digest(playbook.model_dump(mode="json"), 16)


def _plan_steps(playbook: Playbook) -> List[Dict[str, Any]]:
    return [
        {
            "action": step.action,
            "resource_type": step.resource_type,
            "params": dict(step.params),
        }
        for step in playbook.plan.steps or ()
    ]


def entry_binding(entry: Mapping[str, Any]) -> str:
    """Hash записи снимка без самого поля `binding` (12 hex, как подпись)."""
    return _digest({k: v for k, v in entry.items() if k != "binding"}, 12)


def build_match_snapshot(
    candidates: Iterable[Playbook],
    *,
    facts: Optional["FactStore"],
    namespace: str,
    alertname: Optional[str],
    classification: Optional[str],
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Снимок отбора — JSON-совместимый dict для analysis.

    `preconditions` — те же checks, что вернул matcher: по ним видно, на
    каких вердиктах держалось совпадение, даже когда факты инцидента
    позже перезапишет повторный прогон.
    """
    entries: List[Dict[str, Any]] = []
    for pb in candidates:
        _, checks = check_preconditions(pb, facts)
        entry: Dict[str, Any] = {
            "playbook": pb.name,
            "playbook_digest": playbook_digest(pb),
            "namespace": namespace,
            "preconditions": checks,
            "plan": _plan_steps(pb),
            "verify": list(pb.verify or ()),
        }
        entry["binding"] = entry_binding(entry)
        entries.append(entry)
    return {
        "version": SNAPSHOT_VERSION,
        "matched_at": (now or datetime.now(timezone.utc)).isoformat(),
        "namespace": namespace,
        "alertname": alertname,
        "classification": classification,
        "entries": entries,
    }


def find_entry(
    snapshot: Optional[Mapping[str, Any]], playbook: Optional[str],
) -> Optional[Mapping[str, Any]]:
    """Запись снимка для playbook-а или None (снимка нет / playbook не совпал)."""
    if not isinstance(snapshot, Mapping) or not playbook:
        return None
    if snapshot.get("version") != SNAPSHOT_VERSION:
        return None
    for entry in snapshot.get("entries") or ():
        if isinstance(entry, Mapping) and entry.get("playbook") == playbook:
            return entry
    return None


def _is_template(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(_TEMPLATE_PREFIX)


def _step_matches(step: Mapping[str, Any], intent: "ExecutionIntent") -> bool:
    """Intent — ровно этот шаг плана: действие, тип ресурса и параметры.

    Литеральный параметр шага обязан совпасть; шаблон `{name}` принимает
    значение intent-а (его диапазон уже проверил ExecutionIntent). Лишний
    параметр в intent-е — не совпадение: план его не предусматривал.
    """
    from app.core.execution_dsl import action_spec

    if step.get("action") != intent.action.value:
        return False
    spec = action_spec(intent.action)
    want_type = step.get("resource_type") or spec.requires_resource_type or "deployment"
    if intent.resource_type.lower() != str(want_type).lower():
        return False
    step_params = step.get("params") or {}
    intent_params = intent.params or {}
    if set(intent_params) != set(step_params):
        return False
    for key, value in step_params.items():
        if _is_template(value):
            continue
        if intent_params.get(key) != value:
            return False
    return True


def check_intent_binding(
    intent: "ExecutionIntent",
    snapshot: Optional[Mapping[str, Any]],
    registry: Mapping[str, Playbook],
) -> Playbook:
    """Сверить intent с серверным снимком. Возвращает playbook из реестра.

    Любое расхождение → BindingViolation с кодом причины. Порядок проверок —
    от «нечего сверять» к «сверили и не сошлось», чтобы код в audit сразу
    говорил, где разрыв.
    """
    if not intent.playbook:
        raise BindingViolation("playbook_missing")
    if not isinstance(snapshot, Mapping) or snapshot.get("version") != SNAPSHOT_VERSION:
        raise BindingViolation("match_snapshot_missing", playbook=intent.playbook)
    if not intent.playbook_match:
        raise BindingViolation("binding_missing", playbook=intent.playbook)
    entry = find_entry(snapshot, intent.playbook)
    if entry is None:
        raise BindingViolation(
            "playbook_not_matched",
            playbook=intent.playbook,
            matched=sorted(
                str(e.get("playbook")) for e in snapshot.get("entries") or ()
                if isinstance(e, Mapping)
            ),
        )
    # Снимок пишет сервер, но analysis переписывают многие — пересчёт hash
    # ловит правку записи задним числом, а не только подмену в intent-е.
    if entry_binding(entry) != entry.get("binding"):
        raise BindingViolation("snapshot_tampered", playbook=intent.playbook)
    if intent.playbook_match != entry.get("binding"):
        raise BindingViolation("binding_mismatch", playbook=intent.playbook)
    if entry.get("namespace") != intent.namespace:
        raise BindingViolation(
            "namespace_mismatch",
            playbook=intent.playbook,
            snapshot_namespace=entry.get("namespace"),
        )
    pb = registry.get(intent.playbook)
    if pb is None:
        raise BindingViolation("playbook_unknown", playbook=intent.playbook)
    if not pb.executable:
        raise BindingViolation("playbook_not_executable", playbook=pb.name)
    if playbook_digest(pb) != entry.get("playbook_digest"):
        raise BindingViolation("playbook_changed_since_match", playbook=pb.name)
    if not any(
        isinstance(step, Mapping) and _step_matches(step, intent)
        for step in entry.get("plan") or ()
    ):
        raise BindingViolation(
            "intent_not_in_plan",
            playbook=pb.name,
            action=intent.action.value,
            plan=sorted(str(s.get("action")) for s in entry.get("plan") or ()
                        if isinstance(s, Mapping)),
        )
    return pb
