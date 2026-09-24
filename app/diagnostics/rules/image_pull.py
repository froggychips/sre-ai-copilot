"""ImagePullRule — образ контейнера не вытягивается.

До этого правила ни одно правило не говорило про ImagePullBackOff: на
реальных инцидентах сквадов («тег без образа», retention реестра) движок
выдавал ноль фактов, гипотез не появлялось, и пайплайн честно отказывался
отвечать. Хуже того, событие «Back-off pulling image» идёт под reason
`BackOff` и засчитывалось как crashloop — процесс, который ни разу не
стартовал, выглядел падающим.

Источники:
  1. k8s_events — `ErrImagePull` / `ImagePullBackOff`, либо `Failed` /
     `BackOff` с «pull image» в сообщении. Привязка к target-workload как в
     PodEventsRule: чужой workload — не наблюдение (✗ с пометкой), без
     привязки — наблюдение со срезанной вдвое уверенностью.
  2. Текст снапшота (`k8s_summary` / `logs_summary`): waiting-state
     контейнера и фразы kubelet. Слабее событий: в тексте нет involvedObject.

Причина различается по сообщению реестра — это и есть та развилка, по которой
оператор решает, что делать:
  * `not_found` — тега/манифеста нет в registry (опечатка, retention, образ не
    собрался): чинится пересборкой/пушем, не сетью;
  * `auth` — registry отказал в доступе (imagePullSecret, права);
  * `network` — registry недоступен (DNS, таймаут, TLS);
  * `unknown` — сообщение не распознано.

Имя образа кладётся в evidence как есть — это координата, а не секрет.
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

_PULL_REASONS = frozenset({"errimagepull", "imagepullbackoff", "errimageneverpull",
                           "invalidimagename"})
_PULL_MESSAGE_RE = re.compile(
    r"(failed to pull image|back-?off pulling image|pulling image .* failed|"
    r"errimagepull|imagepullbackoff)",
    re.IGNORECASE,
)
_TEXT_RE = re.compile(
    r"(imagepullbackoff|errimagepull|errimageneverpull|failed to pull image|"
    r"back-?off pulling image)",
    re.IGNORECASE,
)
_IMAGE_RE = re.compile(r'(?:pull(?:ing)? image|image)\s+"([^"\s]+)"', re.IGNORECASE)

# Порядок важен: «not found» встречается и в сетевых ошибках («no such host»
# — нет), но «403 Forbidden ... not found» бывает у прокси реестров; auth
# проверяем раньше not_found.
_CAUSES = (
    ("auth", re.compile(
        r"(unauthorized|authentication required|access denied|denied:|"
        r"forbidden|no basic auth credentials|\b401\b|\b403\b)", re.IGNORECASE)),
    ("network", re.compile(
        r"(i/o timeout|connection refused|no such host|tls handshake timeout|"
        r"context deadline exceeded|dial tcp|network is unreachable|"
        r"connection reset)", re.IGNORECASE)),
    ("not_found", re.compile(
        r"(manifest unknown|manifest for .* not found|not found|"
        r"no such manifest|repository does not exist|name unknown)", re.IGNORECASE)),
)

_EVENT_CONFIDENCE = 0.95
_TEXT_CONFIDENCE = 0.8
_ABSENT_CONFIDENCE = 0.7
_FOREIGN_CONFIDENCE = 0.5
_MESSAGE_LEN = 160


def _is_pull_event(ev: Dict[str, Any]) -> bool:
    reason = (ev.get("reason") or "").lower()
    if reason in _PULL_REASONS:
        return True
    return reason in ("failed", "backoff") and bool(
        _PULL_MESSAGE_RE.search(ev.get("message") or "")
    )


def pull_cause(text: str) -> str:
    for cause, pattern in _CAUSES:
        if pattern.search(text):
            return cause
    return "unknown"


def _image(text: str) -> Optional[str]:
    m = _IMAGE_RE.search(text)
    return m.group(1) if m else None


class ImagePullRule(Rule):
    name = "ImagePullRule"
    sources = ("k8s_events", "k8s_summary", "logs_summary")

    def evaluate(self, ctx: Dict[str, Any]) -> List[Fact]:
        target = ctx.get("pod") or ctx.get("service")
        subject = target or ctx.get("namespace")

        best: Dict[str, Dict[str, Any]] = {}
        for attribution, ev in classify_events(ctx.get("k8s_events") or [], target):
            if not _is_pull_event(ev):
                continue
            prev = best.get(attribution)
            count = int(ev.get("count") or 1)
            if prev is None or count > prev["count"]:
                message = ev.get("message") or ""
                best[attribution] = {
                    "source": "k8s_event",
                    "reason": ev.get("reason") or "",
                    "count": count,
                    "object": event_object(ev),
                    "cause": pull_cause(message),
                    "image": _image(message),
                    "message": message[:_MESSAGE_LEN],
                }

        if SCOPED in best or UNVERIFIED in best:
            scoped = SCOPED in best
            evidence = dict(best[SCOPED] if scoped else best[UNVERIFIED])
            confidence = _EVENT_CONFIDENCE
            if not scoped:
                confidence = round(confidence * UNVERIFIED_CONFIDENCE_FACTOR, 4)
                evidence["attribution"] = "unverified"
            return [Fact(
                kind=FactKind.IMAGE_PULL, observed=True, confidence=confidence,
                subject=evidence.get("object") or subject, evidence=evidence,
                source_rule=self.name, epistemic=Epistemic.OBSERVED.value,
                provenance="k8s_event",
            )]

        text = self.text_haystack(ctx)
        hits = self.count_matches(text, _TEXT_RE)
        if hits:
            return [Fact(
                kind=FactKind.IMAGE_PULL, observed=True,
                confidence=_TEXT_CONFIDENCE if target else round(
                    _TEXT_CONFIDENCE * UNVERIFIED_CONFIDENCE_FACTOR, 4),
                subject=subject,
                evidence={
                    "source": "k8s_text", "phrase_hits": hits,
                    "cause": pull_cause(text), "image": _image(text),
                },
                source_rule=self.name,
            )]

        if FOREIGN in best:
            # Образ не тянется у СОСЕДНЕГО workload-а: про target это ✗ —
            # наблюдение, а не пробел; сигнал сохраняем в evidence.
            return [Fact(
                kind=FactKind.IMAGE_PULL, observed=False,
                confidence=_FOREIGN_CONFIDENCE, subject=subject,
                evidence={**best[FOREIGN], "attribution": "foreign"},
                source_rule=self.name,
            )]

        return [Fact(
            kind=FactKind.IMAGE_PULL, observed=False,
            confidence=_ABSENT_CONFIDENCE, subject=subject,
            evidence={"events_seen": len(ctx.get("k8s_events") or [])},
            source_rule=self.name,
        )]
