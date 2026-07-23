"""Mouse-driven transcript selection + clipboard copy (issue: chat text could
not be selected or copied while the full-screen surface held mouse capture).

Covers:
  * plain-text extraction across single- and multi-line selections,
  * ANSI escape stripping before slicing,
  * CJK (double-width) column math,
  * reverse-drag normalization,
  * fragment highlighting (reverse video on the selected span),
  * mouse event plumbing on ``_TranscriptControl``,
  * clipboard helper dispatch (with the actual write stubbed out),
  * selection clearing when new transcript content arrives.
"""

from __future__ import annotations

import io

import pytest
from prompt_toolkit.data_structures import Point, Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from prompt_toolkit.output.vt100 import Vt100_Output

from agentos.cli.repl.app import ChatApplication
from agentos.cli.tui.terminal import clipboard as clipboard_module
from agentos.cli.tui.terminal.selection import (
    Selection,
    extract_selection_text,
    highlight_fragments,
)
from agentos.engine.commands import Surface


@pytest.fixture
def fullscreen_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTOS_CHAT_FULLSCREEN", "1")


def _build(pipe) -> ChatApplication:  # type: ignore[no-untyped-def]
    return ChatApplication(
        surface=Surface.CLI_GATEWAY,
        toolbar_context={
            "model": "m",
            "session_id": "k",
            "suppress": None,
            "status": None,
        },
        bottom_toolbar=lambda: "title . model . [tier:c1]",
        style=None,
        input=pipe,
        output=Vt100_Output(io.StringIO(), lambda: Size(rows=14, columns=54)),
    )


# ---------------------------------------------------------------------------
# extract_selection_text
# ---------------------------------------------------------------------------


def test_extract_single_line_span() -> None:
    transcript = "hello world\nline 2\n"
    sel = Selection(anchor=(0, 0), cursor=(0, 5))
    assert extract_selection_text(transcript, sel) == "hello"


def test_extract_multi_line_span() -> None:
    transcript = "hello world\nline 2\nthird line\n"
    sel = Selection(anchor=(0, 6), cursor=(2, 5))
    assert extract_selection_text(transcript, sel) == "world\nline 2\nthird"


def test_extract_normalizes_reverse_drag() -> None:
    transcript = "hello world\n"
    sel = Selection(anchor=(0, 5), cursor=(0, 0))
    assert extract_selection_text(transcript, sel) == "hello"


def test_extract_strips_ansi_codes() -> None:
    transcript = "\x1b[32mhello\x1b[0m world\n"
    sel = Selection(anchor=(0, 0), cursor=(0, 5))
    assert extract_selection_text(transcript, sel) == "hello"


def test_extract_cjk_width_aware() -> None:
    # 世界 occupies columns 5..9 (two double-width chars).
    transcript = "hello世界world\n"
    sel = Selection(anchor=(0, 5), cursor=(0, 9))
    assert extract_selection_text(transcript, sel) == "世界"
    # Partial overlap at the left edge still picks the first wide char only
    # when the window fully contains it.
    sel = Selection(anchor=(0, 0), cursor=(0, 7))
    assert extract_selection_text(transcript, sel) == "hello世"


def test_extract_empty_when_row_out_of_range() -> None:
    transcript = "one line\n"
    sel = Selection(anchor=(5, 0), cursor=(5, 3))
    assert extract_selection_text(transcript, sel) == ""


def test_extract_multi_line_clamps_to_last_line() -> None:
    transcript = "aaa\nbbb\n"
    sel = Selection(anchor=(0, 0), cursor=(99, 2))
    assert extract_selection_text(transcript, sel) == "aaa\nbbb"


# ---------------------------------------------------------------------------
# highlight_fragments
# ---------------------------------------------------------------------------


def _styles(frags) -> list[tuple[str, str]]:  # type: ignore[no-untyped-def]
    return [(item[0], item[1]) for item in frags]


def test_highlight_single_line_applies_reverse_to_span_only() -> None:
    transcript = "hello world\n"
    frags = highlight_fragments(transcript, Selection(anchor=(0, 0), cursor=(0, 5)))
    styled = _styles(frags)
    # First five chars highlighted, rest untouched.
    assert styled[:5] == [(" reverse", ch) for ch in "hello"]
    assert styled[5:11] == [("", ch) for ch in " world"]


def test_highlight_multi_line_covers_full_middle_lines() -> None:
    transcript = "aaa\nbbb\nccc\n"
    frags = highlight_fragments(transcript, Selection(anchor=(0, 1), cursor=(2, 2)))
    # Filter out the synthetic newline and empty EOL marker fragments.
    styled = [
        (style, text)
        for style, text in _styles(frags)
        if text not in {"\n", ""}
    ]
    assert styled == [
        ("", "a"),
        (" reverse", "a"),
        (" reverse", "a"),
        (" reverse", "b"),
        (" reverse", "b"),
        (" reverse", "b"),
        (" reverse", "c"),
        (" reverse", "c"),
        ("", "c"),
    ]


def test_highlight_preserves_original_style_and_appends_reverse() -> None:
    transcript = "\x1b[32mhello\x1b[0m\n"
    frags = highlight_fragments(transcript, Selection(anchor=(0, 0), cursor=(0, 2)))
    styled = _styles(frags)
    assert styled[0] == ("ansigreen reverse", "h")
    assert styled[1] == ("ansigreen reverse", "e")
    # Untouched fragments keep their original style.
    assert styled[2] == ("ansigreen", "l")


# ---------------------------------------------------------------------------
# _TranscriptControl mouse plumbing
# ---------------------------------------------------------------------------


def test_mouse_drag_builds_selection_and_copies(
    fullscreen_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    copied: list[str] = []
    monkeypatch.setattr(
        "agentos.cli.tui.terminal.app.copy_to_system_clipboard",
        lambda text: copied.append(text),
    )
    with create_pipe_input() as pipe:
        chat = _build(pipe)
        chat.append_transcript("hello world\nline 2\n")

        control = chat._transcript_window.content  # type: ignore[union-attr]

        down = MouseEvent(
            position=Point(x=0, y=0),
            event_type=MouseEventType.MOUSE_DOWN,
            button=MouseButton.LEFT,
            modifiers=frozenset(),
        )
        move = MouseEvent(
            position=Point(x=5, y=0),
            event_type=MouseEventType.MOUSE_MOVE,
            button=MouseButton.LEFT,
            modifiers=frozenset(),
        )
        up = MouseEvent(
            position=Point(x=5, y=0),
            event_type=MouseEventType.MOUSE_UP,
            button=MouseButton.LEFT,
            modifiers=frozenset(),
        )

        control.mouse_handler(down)
        # Anchor recorded; no selection visible yet.
        assert chat._transcript_selection is None

        control.mouse_handler(move)
        assert chat._transcript_selection == Selection(anchor=(0, 0), cursor=(0, 5))

        control.mouse_handler(up)
        assert copied == ["hello"]
        # Highlight stays after release.
        assert chat._transcript_selection == Selection(anchor=(0, 0), cursor=(0, 5))


def test_mouse_down_clears_prior_selection(fullscreen_env: None) -> None:
    with create_pipe_input() as pipe:
        chat = _build(pipe)
        chat.append_transcript("hello\n")
        chat._transcript_selection = Selection(anchor=(0, 0), cursor=(0, 3))

        control = chat._transcript_window.content  # type: ignore[union-attr]
        down = MouseEvent(
            position=Point(x=1, y=0),
            event_type=MouseEventType.MOUSE_DOWN,
            button=MouseButton.LEFT,
            modifiers=frozenset(),
        )
        control.mouse_handler(down)
        assert chat._transcript_selection is None


def test_append_transcript_clears_selection(fullscreen_env: None) -> None:
    with create_pipe_input() as pipe:
        chat = _build(pipe)
        chat.append_transcript("hello\n")
        chat._transcript_selection = Selection(anchor=(0, 0), cursor=(0, 3))
        chat.append_transcript("world\n")
        assert chat._transcript_selection is None


def test_right_click_does_not_select(fullscreen_env: None) -> None:
    with create_pipe_input() as pipe:
        chat = _build(pipe)
        chat.append_transcript("hello\n")
        control = chat._transcript_window.content  # type: ignore[union-attr]
        down = MouseEvent(
            position=Point(x=0, y=0),
            event_type=MouseEventType.MOUSE_DOWN,
            button=MouseButton.RIGHT,
            modifiers=frozenset(),
        )
        result = control.mouse_handler(down)
        assert chat._transcript_selection is None
        # Falls through to the parent handler (no fragment handlers here).
        assert result is NotImplemented


# ---------------------------------------------------------------------------
# Clipboard helper
# ---------------------------------------------------------------------------


def test_copy_to_system_clipboard_noop_on_empty_text() -> None:
    assert clipboard_module.copy_to_system_clipboard("") is False


def test_copy_to_system_clipboard_uses_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []
    monkeypatch.setattr(clipboard_module, "_WRITER", lambda text: seen.append(text) or True)
    assert clipboard_module.copy_to_system_clipboard("payload") is True
    assert seen == ["payload"]


def test_copy_to_system_clipboard_swallows_writer_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(text: str) -> bool:  # noqa: ARG001
        raise RuntimeError("no clipboard daemon")

    monkeypatch.setattr(clipboard_module, "_WRITER", _boom)
    assert clipboard_module.copy_to_system_clipboard("x") is False


def test_copy_to_system_clipboard_returns_false_when_no_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(clipboard_module, "_WRITER", None)
    assert clipboard_module.copy_to_system_clipboard("x") is False
