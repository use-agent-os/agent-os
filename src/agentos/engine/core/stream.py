"""Turn one provider stream into agent events and one ``AttemptResult``.

``stream_attempt`` consumes the events a provider yields for one request. It
re-emits what the user should see (text, reasoning, tool-use starts,
heartbeats) as agent events, assembles tool calls from their streamed
argument fragments, and ends with exactly one :class:`AttemptResult`
describing how the attempt went: the done event, the provider error (if
any), whether the per-iteration stream budget ran out, or whether the
repetition guard cut the response.

Classified failures never raise: a provider error, an iteration timeout and
a repetition trip all end up on the result, and the caller decides what to
do next. Cancellation, the total turn deadline and a cooperative abort are
not attempt outcomes; they still propagate.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from agentos.engine.repetition_guard import RepetitionTrip, StreamRepetitionGuard
from agentos.engine.text_tool_call_detect import TextToolCallStreamGate
from agentos.engine.types import (
    AgentEvent,
    RunHeartbeatEvent,
    TextDeltaEvent,
    ThinkingEvent,
    ToolCall,
    ToolUseStartEvent,
)
from agentos.provider import DoneEvent as ProviderDoneEvent
from agentos.provider import ErrorEvent as ProviderErrorEvent
from agentos.provider import ProviderHeartbeatEvent, ToolUseEndEvent
from agentos.provider import TextDeltaEvent as ProviderTextDelta
from agentos.provider import ThinkingDeltaEvent as ProviderThinkingDelta
from agentos.provider import ToolUseStartEvent as ProviderToolUseStart

#: Streamed tool-argument characters between two ``llm_tool_arguments``
#: heartbeats, so a long argument payload does not look like a stalled call.
TOOL_ARGUMENT_HEARTBEAT_CHARS = 4096


class IterationStreamTimeoutError(TimeoutError):
    """Raised when provider streaming exceeds the active Agent iteration budget."""


@dataclass
class StreamAccumulator:
    """Accumulates streaming fragments for a single tool call."""

    tool_use_id: str
    tool_name: str
    synthetic_from_text: bool = False
    json_buf: list[str] = field(default_factory=list)
    json_chars: int = 0

    def finish(self) -> dict[str, Any]:
        raw = "".join(self.json_buf)
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)  # type: ignore[no-any-return]
        except json.JSONDecodeError:
            return {"_raw": raw}


@dataclass
class AttemptResult:
    """How one provider request ended.

    ``text_parts`` is the list the caller passed in, so text streamed before
    a cancellation is still visible to the caller that seals the turn.
    """

    text_parts: list[str]
    tool_calls: list[ToolCall] = field(default_factory=list)
    pending_tools: dict[str, StreamAccumulator] = field(default_factory=dict)
    reasoning_parts: list[str] = field(default_factory=list)
    #: The provider's done event, when the stream got that far.
    done: ProviderDoneEvent | None = None
    #: The provider error event as received (also set for a thinking fallback).
    error_event: ProviderErrorEvent | None = None
    #: The provider error the caller has to recover from; ``None`` when the
    #: error only asks to retry without thinking (``thinking_fallback``).
    provider_error: ProviderErrorEvent | None = None
    #: The stream stopped on a provider error (``provider_error`` or a
    #: thinking fallback).
    got_error: bool = False
    #: The error mentioned thinking/reasoning and a fallback was allowed:
    #: retry the same request with thinking disabled.
    thinking_fallback: bool = False
    #: The per-iteration stream budget (``iteration_timeout``) ran out.
    iteration_timeout: bool = False
    #: The repetition guard cut the response; the stream was left open and
    #: must be closed by the caller.
    repetition_trip: RepetitionTrip | None = None
    #: Text or a tool-use start reached the consumer during this attempt.
    user_visible_emitted: bool = False
    #: Reasoning deltas were shown to the consumer during this attempt.
    showed_thinking: bool = False
    #: A tool call arrived while tools were off for this request (a final
    #: response after artifact delivery or after the iteration limit).
    ignored_tool_use: bool = False


async def stream_attempt(
    events: AsyncIterator[Any],
    *,
    text_parts: list[str],
    tools_enabled: bool,
    finalizing: bool,
    text_gate: TextToolCallStreamGate | None,
    repetition_guard: StreamRepetitionGuard | None,
    show_thinking: bool,
    thinking_fallback_allowed: bool,
    canonical_tool_name: Callable[..., str],
    coerce_tool_arguments: Callable[[str, Any], Any],
    on_done: Callable[[ProviderDoneEvent], None],
    started_at: float | None = None,
) -> AsyncIterator[AgentEvent | AttemptResult]:
    """Consume one provider stream; yield agent events, then one result.

    ``tools_enabled`` is false when the request offered no tools: tool-use
    events are then dropped (and noted on the result when ``finalizing``).
    ``on_done`` runs for the done event as it arrives, so usage accounting
    happens at the same point of the stream as before the split.
    """
    result = AttemptResult(text_parts=text_parts)
    call_started_at = time.monotonic() if started_at is None else started_at
    heartbeat_chars: dict[str, int] = {}
    try:
        async for raw_ev in events:
            if isinstance(raw_ev, ProviderTextDelta):
                text_parts.append(raw_ev.text)
                if raw_ev.text:
                    result.user_visible_emitted = True
                if text_gate is None:
                    yield TextDeltaEvent(text=raw_ev.text)
                elif shown_text := text_gate.push(raw_ev.text):
                    yield TextDeltaEvent(text=shown_text)
                if repetition_guard is not None and repetition_guard.feed_text(raw_ev.text):
                    result.repetition_trip = repetition_guard.trip
                    break

            elif isinstance(raw_ev, ProviderThinkingDelta):
                # Reasoning is display-only: never mixed into the reply text,
                # never counted as user-visible output.
                if raw_ev.text and show_thinking:
                    result.showed_thinking = True
                    yield ThinkingEvent(text=raw_ev.text)
                result.reasoning_parts.append(raw_ev.text)
                if repetition_guard is not None and repetition_guard.feed_reasoning(raw_ev.text):
                    result.repetition_trip = repetition_guard.trip
                    break

            elif isinstance(raw_ev, ProviderToolUseStart):
                if not tools_enabled:
                    if finalizing:
                        result.ignored_tool_use = True
                    continue
                if text_gate is not None and (held_text := text_gate.release()):
                    yield TextDeltaEvent(text=held_text)
                start_tool_name = canonical_tool_name(raw_ev.tool_name)
                result.pending_tools[raw_ev.tool_use_id] = StreamAccumulator(
                    tool_use_id=raw_ev.tool_use_id,
                    tool_name=start_tool_name,
                    synthetic_from_text=raw_ev.synthetic_from_text,
                )
                heartbeat_chars[raw_ev.tool_use_id] = 0
                result.user_visible_emitted = True
                yield ToolUseStartEvent(
                    tool_use_id=raw_ev.tool_use_id,
                    tool_name=start_tool_name,
                    synthetic_from_text=raw_ev.synthetic_from_text,
                )

            elif raw_ev.kind == "tool_use_delta":
                if not tools_enabled:
                    continue
                acc = result.pending_tools.get(raw_ev.tool_use_id)
                if acc:
                    json_fragment = raw_ev.json_fragment
                    acc.json_buf.append(json_fragment)
                    acc.json_chars += len(json_fragment)
                    last_heartbeat_chars = heartbeat_chars.get(raw_ev.tool_use_id, 0)
                    if acc.json_chars - last_heartbeat_chars >= TOOL_ARGUMENT_HEARTBEAT_CHARS:
                        heartbeat_chars[raw_ev.tool_use_id] = acc.json_chars
                        yield RunHeartbeatEvent(
                            phase="llm_tool_arguments",
                            elapsed_ms=int((time.monotonic() - call_started_at) * 1000),
                            idle_ms=0,
                            message=f"Receiving {acc.tool_name} arguments",
                        )

            elif isinstance(raw_ev, ToolUseEndEvent):
                if not tools_enabled:
                    if finalizing:
                        result.ignored_tool_use = True
                    continue
                acc = result.pending_tools.pop(raw_ev.tool_use_id, None)
                heartbeat_chars.pop(raw_ev.tool_use_id, None)
                if acc and acc.json_buf:
                    arguments = acc.finish()
                else:
                    arguments = raw_ev.arguments
                synthetic_from_text = (
                    acc.synthetic_from_text if acc is not None else raw_ev.synthetic_from_text
                )
                end_tool_name = canonical_tool_name(
                    raw_ev.tool_name,
                    tool_use_id=raw_ev.tool_use_id,
                )
                result.tool_calls.append(
                    ToolCall(
                        tool_use_id=raw_ev.tool_use_id,
                        tool_name=end_tool_name,
                        arguments=coerce_tool_arguments(end_tool_name, arguments),
                        synthetic_from_text=synthetic_from_text,
                        thought_signature=raw_ev.thought_signature,
                    )
                )

            elif isinstance(raw_ev, ProviderDoneEvent):
                result.done = raw_ev
                on_done(raw_ev)

            elif isinstance(raw_ev, ProviderErrorEvent):
                result.error_event = raw_ev
                result.got_error = True
                err_lower = raw_ev.message.lower()
                if thinking_fallback_allowed and (
                    "thinking" in err_lower or "reasoning" in err_lower
                ):
                    result.thinking_fallback = True
                else:
                    result.provider_error = raw_ev
                break

            elif isinstance(raw_ev, ProviderHeartbeatEvent):
                yield RunHeartbeatEvent(phase=raw_ev.phase, message=raw_ev.message)
    except IterationStreamTimeoutError:
        if text_gate is not None and (held_text := text_gate.release()):
            yield TextDeltaEvent(text=held_text)
        result.iteration_timeout = True
    yield result


__all__ = [
    "TOOL_ARGUMENT_HEARTBEAT_CHARS",
    "AttemptResult",
    "IterationStreamTimeoutError",
    "StreamAccumulator",
    "stream_attempt",
]
