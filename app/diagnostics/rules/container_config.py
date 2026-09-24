"""ContainerConfigRule — kubelet не собрал окружение контейнера.

`CreateContainerConfigError`: контейнер ссылается на ключ Secret/ConfigMap,
которого нет, или на сам объект, которого нет. Процесс не запускается, логов
нет, restart-счётчик не растёт — ни одно из прежних правил такой под не
видело, и на инцидентах «в Secret нет ключей» пайплайн оставался без фактов.

Источники — те же, что у ImagePullRule (k8s_events + текст снапшота), и та
же привязка к target-workload.

Evidence: вид объекта (`secret` / `configmap`), его имя и отсутствующие ключи.
Ключи — имена переменных окружения, а не значения; значения kubelet в
сообщение не пишет, и правило их ниоткуда не читает.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from app.diagnostics.facts import Fact, FactKind
from app.diagnostics.rules._container_start import (
    FOREIGN, SCOPED, UNVERIFIED, UNVERIFIED_CONFIDENCE_FACTOR, classify_events,
    event_object)
from app.diagnostics.rules.base import Rule
from app.knowledge_graph.epistemic import Epistemic

# couldn't find key FOO in Secret ns/name ; couldn't find key FOO in ConfigMap ns/name
_MISSING_KEY_RE = re.compile(
    r"couldn'?t find key\s+(\S+)\s+in\s+(secret|configmap)\s+([\w.\-/]+)",
    re.IGNORECASE,
)
# secret "name" not found ; configmap "name" not found ; configmaps "name" not found
_MISSING_OBJECT_RE = re.compile(
    r"\b(secrets?|configmaps?)\s+\"([\w.\-]+)\"\s+not found", re.IGNORECASE,
)
_CONFIG_REASONS = frozenset({"createcontainerconfigerror"})
_TEXT_RE = re.compile(
    r"(createcontainerconfigerror|couldn'?t find key\s+\S+\s+in\s+(?:secret|configmap)|"
    r"\b(?:secrets?|configmaps?)\s+\"[\w.\-]+\"\s+not found)",
    re.IGNORECASE,
)

_EVENT_CONFIDENCE = 0.95
_TEXT_CONFIDENCE = 0.85
_ABSENT_CONFIDENCE = 0.7
_FOREIGN_CONFIDENCE = 0.5
_MAX_KEYS = 10


def _is_config_event(ev: Dict[str, Any]) -> bool:
    reason = (ev.get("reason") or "").lower()
    if reason in _CONFIG_REASONS:
        return True
    message = ev.get("message") or ""
    return reason == "failed" and bool(
        _MISSING_KEY_RE.search(message) or _MISSING_OBJECT_RE.search(message)
        or "createcontainerconfigerror" in message.lower()
    )


def _object_kind(raw: str) -> str:
    return "configmap" if raw.lower().startswith("configmap") else "secret"


def parse_config_error(text: str) -> Dict[str, Any]:
    """{object_kind, object_name, missing_keys, missing_object} из текста kubelet."""
    keys: List[str] = []
    kind: Optional[str] = None
    name: Optional[str] = None
    for key, obj_kind, obj in _MISSING_KEY_RE.findall(text):
        if key not in keys:
            keys.append(key)
        kind = kind or _object_kind(obj_kind)
        # ns/name → name: namespace у инцидента и так есть.
        name = name or obj.rsplit("/", 1)[-1]
    missing_object = False
    m = _MISSING_OBJECT_RE.search(text)
    if m:
        missing_object = True
        kind = kind or _object_kind(m.group(1))
        name = name or m.group(2)
    return {
        "object_kind": kind,
        "object_name": name,
        "missing_keys": keys[:_MAX_KEYS],
        "missing_object": missing_object,
    }


class ContainerConfigRule(Rule):
    name = "ContainerConfigRule"
    sources = ("k8s_events", "k8s_summary", "logs_summary")

    def evaluate(self, ctx: Dict[str, Any]) -> List[Fact]:
        target = ctx.get("pod") or ctx.get("service")
        subject = target or ctx.get("namespace")

        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for attribution, ev in classify_events(ctx.get("k8s_events") or [], target):
            if _is_config_event(ev):
                grouped.setdefault(attribution, []).append(ev)

        for attribution in (SCOPED, UNVERIFIED):
            events = grouped.get(attribution)
            if not events:
                continue
            joined = "\n".join(e.get("message") or "" for e in events)
            evidence: Dict[str, Any] = {
                "source": "k8s_event",
                "reason": events[0].get("reason") or "",
                "count": sum(int(e.get("count") or 1) for e in events),
                "object": event_object(events[0]),
                **parse_config_error(joined),
            }
            confidence = _EVENT_CONFIDENCE
            if attribution == UNVERIFIED:
                confidence = round(confidence * UNVERIFIED_CONFIDENCE_FACTOR, 4)
                evidence["attribution"] = "unverified"
            return [Fact(
                kind=FactKind.CONTAINER_CONFIG, observed=True, confidence=confidence,
                subject=evidence.get("object") or subject, evidence=evidence,
                source_rule=self.name, epistemic=Epistemic.OBSERVED.value,
                provenance="k8s_event",
            )]

        text = self.text_haystack(ctx)
        hits = self.count_matches(text, _TEXT_RE)
        if hits:
            # parse — по исходному регистру: имена ключей регистрозависимы.
            raw = "\n".join(
                str(ctx.get(k) or "") for k in ("description", "k8s_summary", "logs_summary")
            )
            return [Fact(
                kind=FactKind.CONTAINER_CONFIG, observed=True,
                confidence=_TEXT_CONFIDENCE if target else round(
                    _TEXT_CONFIDENCE * UNVERIFIED_CONFIDENCE_FACTOR, 4),
                subject=subject,
                evidence={"source": "k8s_text", "phrase_hits": hits,
                          **parse_config_error(raw)},
                source_rule=self.name,
            )]

        if grouped.get(FOREIGN):
            ev = grouped[FOREIGN][0]
            return [Fact(
                kind=FactKind.CONTAINER_CONFIG, observed=False,
                confidence=_FOREIGN_CONFIDENCE, subject=subject,
                evidence={"source": "k8s_event", "attribution": "foreign",
                          "object": event_object(ev),
                          **parse_config_error(ev.get("message") or "")},
                source_rule=self.name,
            )]

        return [Fact(
            kind=FactKind.CONTAINER_CONFIG, observed=False,
            confidence=_ABSENT_CONFIDENCE, subject=subject,
            evidence={"events_seen": len(ctx.get("k8s_events") or [])},
            source_rule=self.name,
        )]
