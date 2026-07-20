"""Full-screen transcript-pane surface (issue #46, issue 1).

Opt-in via ``AGENTOS_CHAT_FULLSCREEN``: the assistant transcript renders
inside a scrollable prompt-toolkit pane above a permanently-pinned input
frame, so the frame stays visible while tokens stream (instead of the
native-scrollback surface that hides the app via ``in_terminal()``).

These pin:
  * the flag toggles ``full_screen`` + the transcript pane,
  * ``append_transcript`` accumulates and re-follows the tail,
  * a streamed turn appends into the pane (not ``console.file``) with the
    input frame pinned to the bottom and the partial (newline-less) tail
    visible immediately.
"""

from __future__ import annotations

import asyncio
import io

import pytest
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output

from agentos.cli.repl.app import ChatApplication
from agentos.engine.commands import Surface


@pytest.fixture
def fullscreen_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTOS_CHAT_FULLSCREEN", "1")


def _build(pipe, rows: int = 14) -> ChatApplication:  # type: ignore[no-untyped-def]
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
        output=Vt100_Output(io.StringIO(), lambda: Size(rows=rows, columns=54)),
    )


def test_flag_enables_fullscreen_and_pane(fullscreen_env: None) -> None:
    with create_pipe_input() as pipe:
        chat = _build(pipe)
        assert chat.fullscreen is True
        assert chat.application.full_screen is True
        assert chat._transcript_window is not None


def test_flag_off_by_default_keeps_native_scrollback() -> None:
    with create_pipe_input() as pipe:
        chat = _build(pipe)
        assert chat.fullscreen is False
        assert chat.application.full_screen is False
        assert chat._transcript_window is None


def test_append_transcript_accumulates_and_follows(fullscreen_env: None) -> None:
    with create_pipe_input() as pipe:
        chat = _build(pipe)
        chat._transcript_follow = False
        chat._transcript_scroll = 3
        chat.append_transcript("hello ")
        chat.append_transcript("world\n")
        assert chat._transcript == "hello world\n"
        # New output re-pins to the bottom.
        assert chat._transcript_follow is True
        assert chat._transcript_scroll == 0


def test_streamed_turn_renders_in_pane_with_frame_pinned(fullscreen_env: None) -> None:
    async def _render() -> list[str]:
        with create_pipe_input() as pipe:
            chat = _build(pipe)
            app = chat.application
            console_buffer = io.StringIO()
            captured: list[str] = []

            async def _probe() -> None:
                await asyncio.sleep(0.05)
                async with chat.stream_output() as write:
                    write("first line\n")
                    for i in range(1, 25):
                        write(f"line {i}\n")
                    write("partial tail no newline")
                app._redraw()
                screen = app.renderer._last_screen  # type: ignore[attr-defined]
                for y in range(screen.height):
                    row = screen.data_buffer[y]
                    captured.append("".join(row[x].char for x in sorted(row)).rstrip())
                app.exit()

            app.create_background_task(_probe())
            await app.run_async()
            # Streaming went to the pane, not the Rich console file.
            assert console_buffer.getvalue() == ""
            return captured

    rows = asyncio.run(_render())

    # Input frame is pinned to the bottom four rows.
    assert "title . model . [tier:c1]" in rows[-1]
    assert set(rows[-2]) == {"─"}
    assert "you" in rows[-3]
    assert set(rows[-4]) == {"─"}
    # The partial (newline-less) tail is visible immediately, just above the
    # frame — no line-buffering lag.
    assert "partial tail no newline" in rows[-5]
    # Auto-scrolled to the tail: the earliest lines have scrolled off.
    joined = "\n".join(rows)
    assert "line 24" in joined
    assert "first line" not in joined
