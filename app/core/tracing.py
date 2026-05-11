"""Per-stage execution trace for the SRE incident pipeline.

Adapted from the same pattern used in froggy-sre (Swift TaskLocal +
LLMTraceRecorder). The Python equivalent of TaskLocal is contextvars —
each Celery task gets its own copy of the recorder when wrapped in
`StageTimer`, so concurrently-processed incidents don't trample each
other's trace state.

Mechanism:

    async with StageTimer("hypothesis") as t:
        result = await agent.generate(...)
    traces.append(t.snapshot())

Inside the agent call, BaseAgent.ask() invokes `record_llm_call(...)`,
which appends to the timer-bound list via the contextvar. Code paths
that don't bind a timer (unit tests of bare agents, ad-hoc scripts) get
a silent no-op.

Source pattern: https://github.com/froggychips/froggy-sre/blob/main/Sources/FroggySRECore/Tracing.swift
"""

from __future__ import annotations

import contextvars
import time
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class LLMCallInfo:
    backend: str
    duration_ms: int
    error: str | None = None


@dataclass
class StageTrace:
    stage: str
    duration_ms: int
    llm_calls: list[LLMCallInfo] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "duration_ms": self.duration_ms,
            "llm_calls": [asdict(c) for c in self.llm_calls],
        }


# Per-task storage. `None` outside of any `StageTimer` scope — callers
# that record LLM calls in that state are silently dropped (matches the
# Swift `LLMRouter.recorder?.record(...)` semantics).
_current_recorder: contextvars.ContextVar[list[LLMCallInfo] | None] = contextvars.ContextVar(
    "llm_call_recorder", default=None
)


def record_llm_call(backend: str, duration_ms: int, error: str | None = None) -> None:
    """Push one LLM call's metadata into the active StageTimer.

    No-op if called outside of any `StageTimer` block (unit tests of
    bare agents, ad-hoc scripts). Backend should be a stable identifier
    like the model name (`"claude-haiku-4-5"`) or a routing label
    (`"local-ollama"` / `"anthropic-cloud"`).
    """
    bucket = _current_recorder.get()
    if bucket is None:
        return
    bucket.append(LLMCallInfo(backend=backend, duration_ms=duration_ms, error=error))


class StageTimer:
    """Async context manager that measures duration and collects LLM-call metadata.

    Usage:
        async with StageTimer("hypothesis") as t:
            result = await agent.generate(...)
        traces.append(t.snapshot())

    Reentry / nesting: each `StageTimer` rebinds the contextvar to its
    own fresh list, so nested timers don't see each other's calls. On
    exit the previous binding is restored.
    """

    def __init__(self, stage: str) -> None:
        self.stage = stage
        self._bucket: list[LLMCallInfo] = []
        self._token: contextvars.Token[list[LLMCallInfo] | None] | None = None
        self._start: float = 0.0
        self._duration_ms: int = 0

    async def __aenter__(self) -> "StageTimer":
        self._token = _current_recorder.set(self._bucket)
        self._start = time.monotonic()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self._duration_ms = int((time.monotonic() - self._start) * 1000)
        if self._token is not None:
            _current_recorder.reset(self._token)

    def snapshot(self) -> StageTrace:
        return StageTrace(
            stage=self.stage,
            duration_ms=self._duration_ms,
            llm_calls=list(self._bucket),
        )
