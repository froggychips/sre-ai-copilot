"""Tests for the StageTimer + contextvars-based LLM call recorder.

Mirrors froggy-sre's TracingTests.swift in intent: confirm the
side-band channel works (calls are captured inside a StageTimer scope),
the no-op fallback works (calls outside any scope are silently dropped),
and nesting / parallel timers don't trample each other.
"""

import asyncio

import pytest

from app.core.tracing import StageTimer, record_llm_call


@pytest.mark.asyncio
async def test_stage_timer_captures_calls_inside_scope() -> None:
    async with StageTimer("hypothesis") as t:
        record_llm_call(backend="m1", duration_ms=10)
        record_llm_call(backend="m1", duration_ms=15, error="timeout")

    snap = t.snapshot()
    assert snap.stage == "hypothesis"
    assert snap.duration_ms >= 0
    assert len(snap.llm_calls) == 2
    assert snap.llm_calls[0].backend == "m1"
    assert snap.llm_calls[1].error == "timeout"


@pytest.mark.asyncio
async def test_record_outside_scope_is_silent_noop() -> None:
    # No active StageTimer. record_llm_call must not raise.
    record_llm_call(backend="ghost", duration_ms=1)


@pytest.mark.asyncio
async def test_nested_timers_isolate_calls() -> None:
    async with StageTimer("outer") as outer:
        record_llm_call(backend="outer-pre", duration_ms=1)
        async with StageTimer("inner") as inner:
            record_llm_call(backend="inner-only", duration_ms=2)
        # After inner exits, outer's contextvar binding is restored.
        record_llm_call(backend="outer-post", duration_ms=3)

    inner_snap = inner.snapshot()
    outer_snap = outer.snapshot()
    assert [c.backend for c in inner_snap.llm_calls] == ["inner-only"]
    assert [c.backend for c in outer_snap.llm_calls] == ["outer-pre", "outer-post"]


@pytest.mark.asyncio
async def test_to_dict_shape_matches_db_contract() -> None:
    async with StageTimer("risk") as t:
        record_llm_call(backend="claude-haiku", duration_ms=42)

    d = t.snapshot().to_dict()
    assert d.keys() == {"stage", "duration_ms", "llm_calls"}
    assert d["stage"] == "risk"
    assert isinstance(d["duration_ms"], int)
    assert d["llm_calls"] == [
        {"backend": "claude-haiku", "duration_ms": 42, "error": None}
    ]


@pytest.mark.asyncio
async def test_parallel_timers_in_separate_tasks_dont_share_recorder() -> None:
    """Each asyncio Task gets its own copy of contextvars on creation."""

    async def stage_in_task(name: str, backend: str) -> dict:
        async with StageTimer(name) as t:
            await asyncio.sleep(0)  # let other tasks run
            record_llm_call(backend=backend, duration_ms=1)
        return t.snapshot().to_dict()

    a, b = await asyncio.gather(
        stage_in_task("a", "model-A"),
        stage_in_task("b", "model-B"),
    )
    assert [c["backend"] for c in a["llm_calls"]] == ["model-A"]
    assert [c["backend"] for c in b["llm_calls"]] == ["model-B"]
