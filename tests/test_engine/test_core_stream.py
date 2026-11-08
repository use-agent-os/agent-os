"""``agentos.engine.core.stream_attempt``: one provider stream, one result."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, cast

import pytest

from agentos.engine.core.stream import (
    TOOL_ARGUMENT_HEARTBEAT_CHARS,
    AttemptResult,
    IterationStreamTimeoutError,
    stream_attempt,
)
from agentos.engine.repetition_guard import RepetitionTrip, StreamRepetitionGuard
from agentos.engine.text_tool_call_detect import TextToolCallStreamGate
from agentos.engine.types import (
    AgentEvent,
    RunHeartbeatEvent,
    TextDeltaEvent,
    ThinkingEvent,
    ToolUseStartEvent,
)
from agentos.provider import DoneEvent, ErrorEvent, ProviderHeartbeatEvent
from agentos.provider import TextDeltaEvent as ProviderText
from agentos.provider import ThinkingDeltaEvent as ProviderThinking
from agentos.provider import ToolUseStartEvent as ProviderToolStart
from agentos.provider.types import ToolUseDeltaEvent, ToolUseEndEvent


async def _events(*items: Any, raise_at_end: BaseException | None = None) -> AsyncIterator[Any]:
    for item in items:
        yield item
    if raise_at_end is not None:
        raise raise_at_end


async def _run(
    events: AsyncIterator[Any], **overrides: Any
) -> tuple[list[AgentEvent], AttemptResult, list[DoneEvent]]:
    done_seen: list[DoneEvent] = []
    kwargs: dict[str, Any] = {
        "text_parts": [],
        "tools_enabled": True,
        "finalizing": False,
        "text_gate": None,
        "repetition_guard": None,
        "show_thinking": True,
        "thinking_fallback_allowed": False,
        "canonical_tool_name": lambda name, **_: name,
        "coerce_tool_arguments": lambda _name, arguments: arguments,
        "on_done": done_seen.append,
    }
    kwargs.update(overrides)
    emitted: list[AgentEvent] = []
    results: list[AttemptResult] = []
    async for item in stream_attempt(events, **kwargs):
        if isinstance(item, AttemptResult):
            results.append(item)
        else:
            emitted.append(item)
    assert len(results) == 1, "exactly one AttemptResult ends the stream"
    return emitted, results[0], done_seen


@pytest.mark.asyncio
async def test_text_and_a_streamed_tool_call_end_with_one_done_result() -> None:
    text_parts: list[str] = []
    done = DoneEvent(input_tokens=10, output_tokens=5)
    emitted, result, done_seen = await _run(
        _events(
            ProviderText(text="Hi "),
            ProviderToolStart(tool_use_id="t1", tool_name="read"),
            ToolUseDeltaEvent(tool_use_id="t1", json_fragment='{"path": '),
            ToolUseDeltaEvent(tool_use_id="t1", json_fragment='"a.txt"}'),
            ToolUseEndEvent(tool_use_id="t1", tool_name="read"),
            done,
        ),
        text_parts=text_parts,
        canonical_tool_name=lambda name, **_: f"canon_{name}",
    )

    assert [type(e) for e in emitted] == [TextDeltaEvent, ToolUseStartEvent]
    assert result.text_parts is text_parts and text_parts == ["Hi "]
    assert [(c.tool_name, c.arguments) for c in result.tool_calls] == [
        ("canon_read", {"path": "a.txt"})
    ]
    assert result.pending_tools == {}
    assert result.done is done and done_seen == [done]
    assert result.user_visible_emitted
    assert not result.got_error and result.provider_error is None


@pytest.mark.asyncio
async def test_long_tool_arguments_emit_a_heartbeat() -> None:
    chunk = "x" * TOOL_ARGUMENT_HEARTBEAT_CHARS
    emitted, result, _ = await _run(
        _events(
            ProviderToolStart(tool_use_id="t1", tool_name="write"),
            ToolUseDeltaEvent(tool_use_id="t1", json_fragment=chunk),
        )
    )

    heartbeats = [e for e in emitted if isinstance(e, RunHeartbeatEvent)]
    assert [h.phase for h in heartbeats] == ["llm_tool_arguments"]
    # No end event and no done event: the call is still pending.
    assert list(result.pending_tools) == ["t1"] and result.done is None


@pytest.mark.asyncio
async def test_a_provider_error_stops_the_stream_and_is_returned() -> None:
    error = ErrorEvent(message="overloaded", code="529")
    emitted, result, _ = await _run(
        _events(ProviderText(text="a"), error, ProviderText(text="never"))
    )

    assert [e.text for e in emitted if isinstance(e, TextDeltaEvent)] == ["a"]
    assert result.got_error and result.provider_error is error and result.error_event is error
    assert not result.thinking_fallback


@pytest.mark.asyncio
async def test_a_thinking_error_asks_for_the_thinking_fallback_when_allowed() -> None:
    error = ErrorEvent(message="Thinking is not supported", code="400")
    _, allowed, _ = await _run(_events(error), thinking_fallback_allowed=True)
    _, spent, _ = await _run(_events(error), thinking_fallback_allowed=False)

    assert allowed.thinking_fallback and allowed.provider_error is None and allowed.got_error
    assert not spent.thinking_fallback and spent.provider_error is error


@pytest.mark.asyncio
async def test_an_iteration_timeout_is_a_result_and_releases_held_text() -> None:
    gate = TextToolCallStreamGate(frozenset({"read"}))
    emitted, result, _ = await _run(
        _events(ProviderText(text="<tool_call>"), raise_at_end=IterationStreamTimeoutError()),
        text_gate=gate,
    )

    assert result.iteration_timeout
    assert "".join(e.text for e in emitted if isinstance(e, TextDeltaEvent)) == "<tool_call>"


@pytest.mark.asyncio
async def test_other_failures_still_propagate() -> None:
    with pytest.raises(TimeoutError):
        await _run(_events(raise_at_end=TimeoutError("total")))


@pytest.mark.asyncio
async def test_a_repetition_trip_cuts_the_stream() -> None:
    trip = RepetitionTrip(
        signal="short_period",
        period=4,
        repeats=9,
        coverage_chars=36,
        loop_start=2,
        keep_chars=6,
        checked_chars=38,
    )

    class _Guard:
        def __init__(self) -> None:
            self.trip: RepetitionTrip | None = None

        def feed_text(self, text: str) -> bool:
            if text == "loop":
                self.trip = trip
                return True
            return False

        def feed_reasoning(self, text: str) -> bool:
            return False

    _, result, _ = await _run(
        _events(ProviderText(text="ok"), ProviderText(text="loop"), DoneEvent()),
        repetition_guard=cast(StreamRepetitionGuard, _Guard()),
    )

    assert result.repetition_trip is trip and result.done is None


@pytest.mark.asyncio
async def test_tool_use_is_dropped_when_tools_are_off() -> None:
    events = (
        ProviderToolStart(tool_use_id="t1", tool_name="read"),
        ToolUseEndEvent(tool_use_id="t1", tool_name="read"),
        DoneEvent(),
    )
    emitted, finalizing, _ = await _run(_events(*events), tools_enabled=False, finalizing=True)
    _, plain, _ = await _run(_events(*events), tools_enabled=False, finalizing=False)

    assert emitted == [] and finalizing.tool_calls == [] and finalizing.ignored_tool_use
    assert not plain.ignored_tool_use


@pytest.mark.asyncio
async def test_thinking_is_shown_only_when_asked_and_always_kept() -> None:
    events = (ProviderThinking(text="hmm"), ProviderHeartbeatEvent(phase="provider"), DoneEvent())
    shown, shown_result, _ = await _run(_events(*events), show_thinking=True)
    hidden, hidden_result, _ = await _run(_events(*events), show_thinking=False)

    assert [type(e) for e in shown] == [ThinkingEvent, RunHeartbeatEvent]
    assert shown_result.showed_thinking and shown_result.reasoning_parts == ["hmm"]
    assert [type(e) for e in hidden] == [RunHeartbeatEvent]
    assert not hidden_result.showed_thinking and hidden_result.reasoning_parts == ["hmm"]
    assert not shown_result.user_visible_emitted
