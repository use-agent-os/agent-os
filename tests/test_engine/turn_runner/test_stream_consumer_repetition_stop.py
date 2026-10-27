"""Stream consumer handling of the engine-internal ``RepetitionStopEvent``."""

from __future__ import annotations

from agentos.engine.turn_runner.stream_consumer_stage import (
    _SUPPRESS,
    _ErrorHandler,
    _RepetitionStopHandler,
    _StreamState,
    _TextDeltaHandler,
    _ToolUseStartHandler,
)
from agentos.engine.types import (
    ErrorEvent,
    RepetitionStopEvent,
    TextDeltaEvent,
    ToolUseStartEvent,
)


def _feed(state: _StreamState, *texts: str) -> None:
    handler = _TextDeltaHandler()
    for text in texts:
        handler.handle(TextDeltaEvent(text=text), state)


def test_removed_text_is_trimmed_from_the_reply_and_suppressed() -> None:
    state = _StreamState()
    state.final_text_parts.append("Earlier turn text. ")
    _feed(state, "Kept answer. ", "loop loop ", "loop loop ")

    result = _RepetitionStopHandler().handle(
        RepetitionStopEvent(signal="short_period", period=5, removed_text="loop loop loop loop "),
        state,
    )

    assert result is _SUPPRESS
    assert "".join(state.final_text_parts) == "Earlier turn text. Kept answer. "
    assert "".join(state.current_text_parts) == "Kept answer. "


def test_unpaired_tool_use_segments_from_the_cut_response_are_dropped() -> None:
    state = _StreamState()
    state.turn_segments.extend(
        [
            {"type": "tool_use", "tool_use_id": "done-1", "name": "echo", "input": "{}"},
            {"type": "tool_result", "tool_use_id": "done-1", "content": "ok"},
        ]
    )
    _feed(state, "Before the tool. ")
    _ToolUseStartHandler().handle(ToolUseStartEvent(tool_use_id="cut-1", tool_name="echo"), state)
    _feed(state, "again again again ")

    _RepetitionStopHandler().handle(
        RepetitionStopEvent(signal="short_period", period=6, removed_text="again again "),
        state,
    )

    ids = [segment.get("tool_use_id") for segment in state.turn_segments]
    assert "cut-1" not in ids
    assert ids.count("done-1") == 2
    assert "".join(state.current_text_parts) == "again "


def test_trim_is_skipped_when_the_reply_does_not_end_with_the_removed_text() -> None:
    state = _StreamState()
    _feed(state, "Answer that the protocol guard rewrote.")

    _RepetitionStopHandler().handle(
        RepetitionStopEvent(removed_text="something else entirely"),
        state,
    )

    assert "".join(state.final_text_parts) == "Answer that the protocol guard rewrote."


def test_repetition_error_drops_unpaired_tool_use() -> None:
    state = _StreamState()
    state.turn_segments.extend(
        [
            {"type": "text", "text": "partial"},
            {"type": "tool_use", "tool_use_id": "t1", "name": "x", "input": ""},
        ]
    )

    result = _ErrorHandler().handle(
        ErrorEvent(message="stopped", code="repetition_detected"),
        state,
    )

    assert result is _SUPPRESS
    assert state.turn_segments == [{"type": "text", "text": "partial"}]
