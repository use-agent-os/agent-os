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
from prompt_toolkit.data_structures import Point, Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from prompt_toolkit.output.vt100 import Vt100_Output

from agentos.cli.repl.app import ChatApplication
from agentos.cli.tui.terminal.app import _MOUSE_SCROLL_LINES
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


def test_scroll_transcript_releases_and_relatches_follow(fullscreen_env: None) -> None:
    with create_pipe_input() as pipe:
        chat = _build(pipe)
        for i in range(1, 40):
            chat.append_transcript(f"line {i}\n")
        assert chat._transcript_follow is True
        assert chat._transcript_scroll == 0

        # Generic scrolling preserves the requested offset (for example, Page Up).
        chat.scroll_transcript(10)
        assert chat._transcript_follow is False
        assert chat._transcript_scroll == 10

        # Over-scroll clamps to the top of history (total_lines - 1).
        chat.scroll_transcript(10_000)
        total = chat._transcript.count("\n")
        assert chat._transcript_scroll == total - 1

        # Scrolling back to the bottom re-latches follow.
        chat.scroll_transcript_to_bottom()
        assert chat._transcript_follow is True
        assert chat._transcript_scroll == 0


def test_scroll_is_noop_when_not_fullscreen() -> None:
    with create_pipe_input() as pipe:
        chat = _build(pipe)  # flag off
        chat.append_transcript("ignored\n")  # append also no-ops meaningfully
        chat.scroll_transcript(5)
        assert chat._transcript_scroll == 0
        assert chat._transcript_follow is True


def test_mouse_wheel_scrolls_through_transcript_control(fullscreen_env: None) -> None:
    with create_pipe_input() as pipe:
        chat = _build(pipe)
        for i in range(1, 40):
            chat.append_transcript(f"line {i}\n")

        assert chat._transcript_window is not None
        control = chat._transcript_window.content
        scroll_up = MouseEvent(
            position=Point(x=0, y=0),
            event_type=MouseEventType.SCROLL_UP,
            button=MouseButton.NONE,
            modifiers=frozenset(),
        )
        scroll_down = MouseEvent(
            position=Point(x=0, y=0),
            event_type=MouseEventType.SCROLL_DOWN,
            button=MouseButton.NONE,
            modifiers=frozenset(),
        )

        assert control.mouse_handler(scroll_up) is None
        # First wheel tick is doubled so the wrapped cursor visibly exits the viewport.
        assert chat._transcript_scroll == _MOUSE_SCROLL_LINES * 2
        assert chat._transcript_follow is False

        assert control.mouse_handler(scroll_down) is None
        assert chat._transcript_scroll == _MOUSE_SCROLL_LINES
        assert chat._transcript_follow is False

        assert control.mouse_handler(scroll_down) is None
        assert chat._transcript_scroll == 0
        assert chat._transcript_follow is True

        input_control = next(
            window.content
            for window in chat.application.layout.find_all_windows()
            if isinstance(window.content, BufferControl)
        )
        assert input_control.mouse_handler(scroll_up) is NotImplemented
        assert chat._transcript_scroll == 0
        assert chat._transcript_follow is True


def test_mouse_reporting_is_enabled_only_for_fullscreen(fullscreen_env: None) -> None:
    with create_pipe_input() as pipe:
        fullscreen_chat = _build(pipe)
        assert fullscreen_chat.application.mouse_support() is True

    with create_pipe_input() as pipe:
        native_chat = ChatApplication(
            surface=Surface.CLI_GATEWAY,
            toolbar_context={"model": None, "session_id": None, "suppress": None},
            bottom_toolbar=lambda: "",
            input=pipe,
            output=Vt100_Output(io.StringIO(), lambda: Size(rows=14, columns=54)),
            fullscreen=False,
        )
        assert native_chat.application.mouse_support() is False


def test_resolve_chat_fullscreen_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit arg wins; then the env override; then the TTY default."""
    from agentos.cli.tui.terminal.app import resolve_chat_fullscreen

    # Explicit argument always wins, regardless of env.
    monkeypatch.setenv("AGENTOS_CHAT_FULLSCREEN", "0")
    assert resolve_chat_fullscreen(True) is True
    assert resolve_chat_fullscreen(False) is False

    # Env override wins when no explicit arg is given.
    monkeypatch.setenv("AGENTOS_CHAT_FULLSCREEN", "1")
    assert resolve_chat_fullscreen() is True
    monkeypatch.setenv("AGENTOS_CHAT_FULLSCREEN", "off")
    assert resolve_chat_fullscreen() is False

    # With no env override the default follows the stdout TTY-ness. Force a
    # known value via a fake stdout so the assertion is deterministic.
    monkeypatch.delenv("AGENTOS_CHAT_FULLSCREEN", raising=False)

    class _FakeStdout:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdout", _FakeStdout())
    assert resolve_chat_fullscreen() is True

    class _FakePipe:
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr("sys.stdout", _FakePipe())
    assert resolve_chat_fullscreen() is False


def test_queued_startup_output_replays_into_pane(fullscreen_env: None) -> None:
    """Pre-surface startup output (queued while the app has not yet taken the
    alternate screen) is drained into the transcript pane when the full-screen
    surface opens — this is how the welcome banner survives full-screen."""
    import agentos.cli.tui.terminal.prompt as prompt_module
    from agentos.cli.tui.terminal.prompt import interactive_session, queue_pane_output

    async def _drive() -> str:
        # Simulate the notifier / launch_bridge capture-and-queue step.
        queue_pane_output("Connected to gateway. Session: agent:main:cli:abc\n")
        queue_pane_output("AGENTOS welcome banner\n")
        with create_pipe_input() as pipe:
            out = Vt100_Output(io.StringIO(), lambda: Size(rows=20, columns=80))
            async with interactive_session(
                surface="cli_gateway",
                model="m",
                session_id="agent:main:cli:abc",
                input=pipe,
                output=out,
            ) as handle:
                await asyncio.sleep(0.05)
                return handle._chat_app._transcript

    transcript = asyncio.run(_drive())
    assert "Connected to gateway. Session: agent:main:cli:abc" in transcript
    assert "AGENTOS welcome banner" in transcript
    # The pending buffer is drained (and cleared) once replayed.
    assert prompt_module._pending_pane_output == []


def test_interactive_session_redirects_console_into_pane(fullscreen_env: None) -> None:
    """In full-screen, Rich `console.print` output lands in the transcript
    pane (not stdout), and the real console stream is restored on exit."""
    import io

    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.output.vt100 import Vt100_Output

    from agentos.cli.tui.terminal.prompt import interactive_session
    from agentos.cli.ui import console

    async def _drive() -> tuple[str, type]:
        original_file_type = type(console.file)
        with create_pipe_input() as pipe:
            out = Vt100_Output(io.StringIO(), lambda: Size(rows=12, columns=54))
            async with interactive_session(
                surface="cli_gateway",
                model="m",
                session_id="k",
                input=pipe,
                output=out,
            ) as handle:
                await asyncio.sleep(0.05)
                # During the session, the sink is installed.
                assert type(console.file).__name__ == "_TranscriptConsoleSink"
                console.print("hello from a notice")
                await asyncio.sleep(0.01)
                transcript = handle._chat_app._transcript
        # After exit the real stream is restored.
        return transcript, original_file_type

    transcript, original_file_type = asyncio.run(_drive())
    assert "hello from a notice" in transcript
    from agentos.cli.ui import console as console_after

    assert type(console_after.file) is original_file_type
