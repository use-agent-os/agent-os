"""A failed run publishes its error as the one terminal frame, never ``done``.

The engine ends every turn with a DoneEvent, after the ErrorEvent on a failed
turn. Before that contract, a failed turn that had already used tokens emitted
its DoneEvent *before* the runner yielded the held-back error, so the gateway
published ``session.event.done`` (state ``done``) as the run's terminal frame
and the error frame was swallowed by the exactly-once reservation: clients saw
a failed turn as a success.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from agentos.engine.outcome import completed_outcome, outcome_from_error
from agentos.engine.types import DoneEvent, ErrorEvent, TextDeltaEvent
from agentos.gateway.boot import TaskRuntimeStreamError, _emit_task_runtime_stream_events
from agentos.gateway.terminal_events import TerminalPublications


async def _stream(events: list[Any]) -> AsyncIterator[Any]:
    for event in events:
        yield event


async def _emit(events: list[Any], frames: list[tuple[str, dict]], *, reserve: bool) -> None:
    async def emitter(session_key: str, name: str, payload: dict) -> None:
        frames.append((name, payload))

    await _emit_task_runtime_stream_events(
        _stream(events),
        "agent:main:webchat:t",
        emitter,
        idle_timeout=None,
        run_id="run-1" if reserve else None,
        terminal_publications=TerminalPublications() if reserve else None,
    )


def _terminal(frames: list[tuple[str, dict]]) -> list[tuple[str, str]]:
    return [
        (name, payload.get("state"))
        for name, payload in frames
        if name in {"session.event.done", "session.event.error"}
    ]


@pytest.mark.parametrize("reserve", [True, False])
async def test_failed_turn_with_usage_publishes_only_its_error_frame(reserve: bool) -> None:
    error = ErrorEvent(message="upstream exploded", code="500")
    events = [
        TextDeltaEvent(text="partial"),
        error,
        DoneEvent(
            input_tokens=120,
            output_tokens=30,
            outcome=outcome_from_error(code=error.code, message=error.message),
        ),
    ]
    frames: list[tuple[str, dict]] = []

    with pytest.raises(TaskRuntimeStreamError):
        await _emit(events, frames, reserve=reserve)

    assert _terminal(frames) == [("session.event.error", "error")]


async def test_successful_turn_publishes_done_carrying_its_outcome() -> None:
    frames: list[tuple[str, dict]] = []

    await _emit(
        [TextDeltaEvent(text="hi"), DoneEvent(text="hi", outcome=completed_outcome())],
        frames,
        reserve=True,
    )

    assert _terminal(frames) == [("session.event.done", "done")]
    done_payload = next(payload for name, payload in frames if name == "session.event.done")
    assert done_payload["outcome"]["kind"] == "completed"
    # Presentation fields the runner adds stay on the payload, unchanged.
    for key in (
        "routed_tier",
        "routing_source",
        "savings_pct",
        "savings_usd",
        "total_savings_pct",
        "session_totals",
        "input_tokens",
        "cost_usd",
    ):
        assert key in done_payload, key
