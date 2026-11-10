"""Turn timing trace: where the wall-clock time of one turn goes.

Unit tests drive ``TurnTiming`` with a fake clock; the integration tests run a
real ``Agent`` turn and a real ``TurnRunner`` turn with scripted providers and
check that the breakdown reaches ``Agent.turn_timing``, the decision log entry
and the ``turn.timing`` log line.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from structlog.testing import capture_logs

from agentos.engine import Agent, AgentConfig, ToolResult
from agentos.engine.runtime import TurnRunner
from agentos.engine.turn_timing import (
    MAX_DETAIL_ENTRIES,
    NULL_TURN_TIMING,
    STAGE_NAMES,
    TurnTiming,
    emit_turn_timing_log,
)
from agentos.provider import DoneEvent as ProviderDone
from agentos.provider import ErrorEvent as ProviderError
from agentos.provider import ProviderHeartbeatEvent, ToolDefinition, ToolInputSchema
from agentos.provider import TextDeltaEvent as ProviderText
from agentos.provider import ToolUseEndEvent as ProviderToolUseEnd
from agentos.provider import ToolUseStartEvent as ProviderToolUseStart
from agentos.tools import ToolContext
from agentos.tools.types import CallerKind


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, ms: int) -> None:
        self.now += ms / 1000


async def _events(clock: _Clock, script: list[tuple[int, Any]]) -> AsyncIterator[Any]:
    """Yield each event after advancing the fake clock by its delay (ms)."""

    for delay_ms, event in script:
        clock.advance(delay_ms)
        yield event


# ── TurnTiming unit tests ────────────────────────────────────────────────


def test_stages_accumulate_and_unmatched_end_is_ignored() -> None:
    clock = _Clock()
    timing = TurnTiming(clock=clock)

    timing.stage_start("input")
    clock.advance(12)
    timing.stage_end("input")
    timing.stage_end("attachment")  # never started
    timing.stage_start("turn_finalizer")
    clock.advance(30)
    timing.stage_end("turn_finalizer")

    summary = timing.summary()
    assert summary["stages"] == {"input": 12, "turn_finalizer": 30}
    assert summary["finalize_ms"] == 30
    assert summary["total_ms"] == 42


@pytest.mark.asyncio
async def test_provider_stream_marks_first_event_first_text_done_and_tokens() -> None:
    clock = _Clock()
    timing = TurnTiming(clock=clock)
    clock.advance(250)  # pre-provider work

    script = [
        (40, ProviderHeartbeatEvent(phase="llm_wait")),  # not model output
        (60, ProviderText(text="")),  # first real event, but empty text
        (100, ProviderText(text="Hello")),
        (50, ProviderText(text=" world")),
        (30, ProviderDone(input_tokens=1200, output_tokens=34)),
    ]
    seen = [event async for event in timing.provider_stream(_events(clock, script))]

    assert len(seen) == 5
    summary = timing.summary()
    assert summary["pre_provider_ms"] == 250
    assert summary["iterations"] == 1
    assert summary["provider_ms"] == 280
    assert summary["first_text_ms"] == 250 + 200
    assert summary["provider_calls"] == [
        {
            "request_started_ms": 250,
            "first_event_ms": 100,
            "first_text_ms": 200,
            "stream_done_ms": 280,
            "input_tokens": 1200,
            "output_tokens": 34,
        }
    ]


@pytest.mark.asyncio
async def test_provider_call_closes_on_error_event_even_if_consumer_breaks() -> None:
    clock = _Clock()
    timing = TurnTiming(clock=clock)

    async for event in timing.provider_stream(
        _events(clock, [(70, ProviderError(message="boom")), (999, ProviderText(text="x"))])
    ):
        if event.kind == "error":
            break
    clock.advance(500)  # the abandoned wrapper is not finalized yet

    call = timing.summary()["provider_calls"][0]
    assert call["stream_done_ms"] == 70
    assert call["first_text_ms"] is None
    assert call["input_tokens"] is None
    assert timing.summary()["provider_ms"] == 70


@pytest.mark.asyncio
async def test_provider_call_closes_when_the_stream_raises() -> None:
    clock = _Clock()
    timing = TurnTiming(clock=clock)

    async def _boom() -> AsyncIterator[Any]:
        clock.advance(90)
        raise TimeoutError("iteration timeout")
        yield  # pragma: no cover

    with pytest.raises(TimeoutError):
        async for _ in timing.provider_stream(_boom()):
            pass

    assert timing.summary()["provider_ms"] == 90
    assert timing.summary()["provider_calls"][0]["first_event_ms"] is None


@pytest.mark.asyncio
async def test_tool_batch_and_concurrent_approvals_count_wall_time_once() -> None:
    clock = _Clock()
    timing = TurnTiming(clock=clock)
    first = asyncio.Event()
    second = asyncio.Event()

    async def wait(payload: dict[str, Any], *, timeout: float) -> str:
        await (first if payload["approval_id"] == "a" else second).wait()
        return payload["approval_id"]

    timed_wait = timing.timed_approval(wait)
    assert timed_wait is not None

    async def batch_events() -> AsyncIterator[str]:
        clock.advance(10)
        task_a = asyncio.ensure_future(timed_wait({"approval_id": "a"}, timeout=5))
        task_b = asyncio.ensure_future(timed_wait({"approval_id": "b"}, timeout=5))
        await asyncio.sleep(0)  # both waits are now outstanding
        clock.advance(300)
        first.set()
        assert await task_a == "a"
        clock.advance(200)  # b still pending: still approval time
        second.set()
        assert await task_b == "b"
        clock.advance(40)
        yield "result"

    events = [e async for e in timing.tool_batch(batch_events(), calls=2)]

    assert events == ["result"]
    summary = timing.summary()
    assert summary["approval_wait_ms"] == 500  # union, not 300 + 500
    assert summary["tool_ms"] == 550
    assert summary["tool_batches"] == 1
    assert summary["tool_calls"] == 2
    assert summary["tool_batch_detail"] == [
        {"started_ms": 0, "duration_ms": 550, "calls": 2, "approval_wait_ms": 500}
    ]


def test_detail_lists_are_capped_but_totals_keep_counting() -> None:
    clock = _Clock()
    timing = TurnTiming(clock=clock)

    async def run() -> None:
        for _ in range(MAX_DETAIL_ENTRIES + 5):
            async for _ in timing.provider_stream(_events(clock, [(1, ProviderDone())])):
                pass

    asyncio.run(run())

    summary = timing.summary()
    assert summary["iterations"] == MAX_DETAIL_ENTRIES + 5
    assert len(summary["provider_calls"]) == MAX_DETAIL_ENTRIES
    assert summary["provider_ms"] == MAX_DETAIL_ENTRIES + 5


def test_log_fields_are_flat_integers() -> None:
    clock = _Clock()
    timing = TurnTiming(clock=clock)
    timing.stage_start("input")
    clock.advance(5)
    timing.stage_end("input")

    fields = timing.log_fields()

    assert fields["stage_input_ms"] == 5
    assert not any(isinstance(v, dict | list) for v in fields.values())
    assert {"pre_provider_ms", "provider_ms", "tool_ms", "approval_wait_ms"} <= set(fields)
    assert {"finalize_ms", "total_ms", "iterations"} <= set(fields)


def test_turn_timing_log_is_emitted_once() -> None:
    timing = TurnTiming(clock=_Clock())
    with capture_logs() as logs:
        emit_turn_timing_log(timing, turn_id="t1")
        emit_turn_timing_log(timing, turn_id="t1")
    lines = [entry for entry in logs if entry["event"] == "turn.timing"]
    assert len(lines) == 1
    assert lines[0]["turn_id"] == "t1"
    assert lines[0]["total_ms"] == 0


@pytest.mark.asyncio
async def test_null_timing_passes_through_and_records_nothing() -> None:
    async def gen() -> AsyncIterator[int]:
        yield 1
        yield 2

    assert [e async for e in NULL_TURN_TIMING.provider_stream(gen())] == [1, 2]
    assert [e async for e in NULL_TURN_TIMING.tool_batch(gen(), calls=1)] == [1, 2]

    async def wait() -> None:
        return None

    assert NULL_TURN_TIMING.timed_approval(wait) is wait
    assert NULL_TURN_TIMING.provider_calls == []
    assert NULL_TURN_TIMING.tool_batches == []


# ── Agent integration ────────────────────────────────────────────────────


class _SequenceProvider:
    provider_name = "fake"

    def __init__(self, streams: list[list[Any]]) -> None:
        self.streams = streams
        self.calls = 0

    def chat(self, messages: Any, tools: Any = None, config: Any = None) -> AsyncIterator[Any]:
        events = self.streams[min(self.calls, len(self.streams) - 1)]
        self.calls += 1
        return self._stream(events)

    async def _stream(self, events: list[Any]) -> AsyncIterator[Any]:
        for event in events:
            yield event

    async def list_models(self) -> list[Any]:
        return []


@pytest.mark.asyncio
async def test_agent_turn_records_each_provider_call_and_tool_batch() -> None:
    provider = _SequenceProvider(
        [
            [
                ProviderToolUseStart(tool_use_id="tool-1", tool_name="echo"),
                ProviderToolUseEnd(tool_use_id="tool-1", tool_name="echo", arguments={}),
                ProviderDone(stop_reason="tool_use", input_tokens=50, output_tokens=5),
            ],
            [
                ProviderText(text="done"),
                ProviderDone(stop_reason="end_turn", input_tokens=60, output_tokens=2),
            ],
        ]
    )

    async def tool_handler(call: Any) -> ToolResult:
        return ToolResult(tool_use_id=call.tool_use_id, tool_name=call.tool_name, content="ok")

    agent = Agent(
        provider=provider,
        config=AgentConfig(retry_base_backoff_ms=0, retry_max_backoff_ms=0),
        tool_definitions=[
            ToolDefinition(name="echo", description="Echo.", input_schema=ToolInputSchema())
        ],
        tool_handler=tool_handler,
    )
    timing = TurnTiming()
    agent.turn_timing = timing

    [event async for event in agent.run_turn("hello")]

    summary = timing.summary()
    assert summary["iterations"] == 2
    assert summary["tool_batches"] == 1
    assert summary["tool_calls"] == 1
    calls = summary["provider_calls"]
    assert [(c["input_tokens"], c["output_tokens"]) for c in calls] == [(50, 5), (60, 2)]
    assert calls[0]["first_text_ms"] is None
    assert calls[1]["first_text_ms"] is not None
    assert all(c["stream_done_ms"] is not None for c in calls)


def test_agent_default_timing_is_the_shared_null_recorder() -> None:
    agent = Agent(provider=_SequenceProvider([[ProviderDone()]]))
    assert agent.turn_timing is NULL_TURN_TIMING


# ── TurnRunner integration ───────────────────────────────────────────────


class _Selector:
    def __init__(self, provider: Any) -> None:
        self.provider = provider
        self.current_config = SimpleNamespace(model="fake-model")

    def clone(self) -> _Selector:
        return self

    def resolve(self) -> Any:
        return self.provider

    def override_model(self, model: str) -> None:
        self.current_config.model = model


@pytest.mark.asyncio
async def test_run_turn_writes_timing_to_decision_log_and_logs_turn_timing(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path))
    entries: list[Any] = []
    monkeypatch.setattr("agentos.engine.runtime.write_decision_entry", entries.append)
    provider = _SequenceProvider(
        [[ProviderText(text="hi"), ProviderDone(input_tokens=7, output_tokens=1)]]
    )
    runner = TurnRunner(provider_selector=_Selector(provider))

    with capture_logs() as logs:
        events = [
            event
            async for event in runner.run(
                "hello",
                "agent:main:main",
                ToolContext(caller_kind=CallerKind.AGENT),
            )
        ]

    assert any(event.kind == "done" for event in events)
    assert len(entries) == 1
    timing = entries[0].timing
    assert entries[0].schema_version == 16
    assert set(timing["stages"]) == set(STAGE_NAMES)
    assert timing["iterations"] == 1
    assert timing["provider_calls"][0]["input_tokens"] == 7
    assert timing["total_ms"] >= timing["pre_provider_ms"]
    lines = [entry for entry in logs if entry["event"] == "turn.timing"]
    assert len(lines) == 1
    assert lines[0]["session_key"] == "agent:main:main"
    assert lines[0]["iterations"] == 1
    assert "stage_stream_consumer_ms" in lines[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("recorder", ["timed", "null"])
async def test_closing_the_tool_batch_closes_the_batch_stream_at_once(recorder: str) -> None:
    timing = TurnTiming(clock=_Clock()) if recorder == "timed" else NULL_TURN_TIMING
    closed: list[str] = []

    async def batch() -> AsyncIterator[str]:
        try:
            yield "first"
            yield "second"
        finally:
            closed.append("batch")

    events = timing.tool_batch(batch(), calls=1)
    assert await events.__anext__() == "first"
    # The turn is closed mid-batch: the batch's own cleanup (cancelling its
    # running tools) runs now, not when the generator is collected.
    await events.aclose()
    assert closed == ["batch"]
