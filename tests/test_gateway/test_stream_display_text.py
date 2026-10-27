"""Streamed assistant text leaves the gateway display-clean.

The console used to strip directives, artifact markers and leaked tool
protocol from every frame itself; desktop, CLI and channels each needed their
own copy, and a marker split across two deltas reached the screen half-shown.
"""

from __future__ import annotations

from typing import Any

from agentos.engine.types import DoneEvent, TextDeltaEvent, ToolUseStartEvent
from agentos.gateway.boot import _emit_task_runtime_stream_events


async def _emitted(events: list[Any]) -> list[tuple[str, dict[str, Any]]]:
    async def _stream():
        for event in events:
            yield event

    seen: list[tuple[str, dict[str, Any]]] = []

    async def _emit(session_key: str, event_name: str, payload: dict[str, Any]) -> None:
        seen.append((event_name, payload))

    await _emit_task_runtime_stream_events(
        _stream(), "agent:main:test", _emit, idle_timeout=None, heartbeat_interval=None
    )
    return seen


def _text(seen: list[tuple[str, dict[str, Any]]]) -> str:
    return "".join(p["text"] for name, p in seen if name == "session.event.text_delta")


async def test_a_directive_split_across_deltas_never_reaches_a_frame() -> None:
    seen = await _emitted(
        [
            TextDeltaEvent(text="Sure. [[reply_"),
            TextDeltaEvent(text="to_current]] Here it is."),
            DoneEvent(text="Sure. [[reply_to_current]] Here it is."),
        ]
    )

    assert "[[" not in _text(seen)
    assert _text(seen).replace("  ", " ") == "Sure. Here it is."
    done = next(p for name, p in seen if name == "session.event.done")
    assert done["text"] == "Sure. Here it is."


async def test_leaked_tool_scaffold_is_dropped_at_the_segment_end() -> None:
    seen = await _emitted(
        [
            TextDeltaEvent(text="Checking.\n"),
            TextDeltaEvent(text="<details><summary>View areas "),
            TextDeltaEvent(text="around line 12</summary>"),
            ToolUseStartEvent(tool_use_id="t1", tool_name="bash"),
            TextDeltaEvent(text="Done."),
            DoneEvent(text="Done."),
        ]
    )

    names = [name for name, _ in seen]
    assert "<details" not in _text(seen)
    assert _text(seen) == "Checking.Done."
    # The first segment's text went out before the tool call, not after it.
    first_tool = names.index("session.event.tool_use_start")
    before = "".join(p["text"] for n, p in seen[:first_tool] if n == "session.event.text_delta")
    assert before == "Checking."


async def test_tool_call_markup_that_reaches_display_is_shown() -> None:
    # The engine cuts calls it ran; any markup still in the text never ran.
    markup = '<invoke name="exec_command"><parameter name="command">pwd</parameter></invoke>'
    seen = await _emitted(
        [
            TextDeltaEvent(text="Checking.\n"),
            TextDeltaEvent(text=markup),
            DoneEvent(text="Checking.\n" + markup),
        ]
    )

    assert _text(seen) == "Checking.\n" + markup


async def test_ordinary_angle_brackets_are_released_at_the_segment_end() -> None:
    seen = await _emitted(
        [
            TextDeltaEvent(text="Use <details> for "),
            TextDeltaEvent(text="optional prose."),
            DoneEvent(text="Use <details> for optional prose."),
        ]
    )

    assert _text(seen) == "Use <details> for optional prose."
    names = [name for name, _ in seen]
    # Held text is released before the terminal frame, never after it.
    assert names[-1] == "session.event.done"
