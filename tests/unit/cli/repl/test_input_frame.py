"""Issue #46 §5 input-frame layout invariants.

Pins the two regressions reported after the frame landed:

  * The framed input row must stay compact — the input buffer is a
    single fixed line, so it can never balloon to fill the region the
    non-full-screen renderer reserves below the cursor (which is large on
    a fresh launch against a tall terminal). A greedy spacer at the top
    of the layout absorbs that slack and pins the frame + toolbar to the
    bottom.
"""

from __future__ import annotations

import asyncio
import io

from prompt_toolkit.buffer import Buffer
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.output.vt100 import Vt100_Output

from agentos.cli.repl.app import ChatApplication
from agentos.engine.commands import Surface


def _build_app(pipe) -> ChatApplication:  # type: ignore[no-untyped-def]
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
        output=Vt100_Output(io.StringIO(), lambda: Size(rows=30, columns=80)),
    )


def _has_buffer_control(window: object) -> bool:
    content = getattr(window, "content", None)
    return isinstance(content, BufferControl)


def test_input_buffer_height_is_fixed_single_line() -> None:
    """The buffer window is pinned to exactly one row.

    An unbounded ``Dimension(min=1)`` let the input balloon to fill the
    reserved region and pushed the bottom rule + toolbar far below the
    ``you`` row (issue: frame too tall on entry).
    """
    with create_pipe_input() as pipe:
        chat = _build_app(pipe)
        body = chat.application.layout.container.content
        input_row = next(c for c in body.children if getattr(c, "children", None))
        buffer_window = next(w for w in input_row.children if _has_buffer_control(w))
        height = buffer_window.height
        assert height is not None
        assert height.max == 1  # fixed single line, never grows


def test_first_layout_child_is_greedy_spacer() -> None:
    """A flexible spacer heads the layout so the frame pins to the bottom."""
    with create_pipe_input() as pipe:
        chat = _build_app(pipe)
        body = chat.application.layout.container.content
        spacer = body.children[0]
        # A plain Window (no buffer / no children) with a flexible height.
        assert not getattr(spacer, "children", None)
        assert not _has_buffer_control(spacer)
        assert spacer.height is None  # flexible → absorbs reserved slack


def test_frame_stays_compact_and_pinned_to_bottom() -> None:
    """Render with a large reserved height; the frame must not balloon.

    Expect the last four rows to be ``rule / ◢ you / rule / toolbar`` with
    the toolbar on the very last row and the slack rendered as blank rows
    above the frame (the greedy spacer), regardless of how many rows the
    renderer reserves.
    """

    async def _render() -> list[str]:
        with create_pipe_input() as pipe:
            chat = _build_app(pipe)
            app = chat.application
            captured: list[str] = []

            async def _probe() -> None:
                await asyncio.sleep(0.05)
                # Simulate a fresh launch against a tall terminal.
                app.renderer._min_available_height = 12  # type: ignore[attr-defined]
                app._redraw()
                screen = app.renderer._last_screen  # type: ignore[attr-defined]
                assert screen is not None
                for y in range(screen.height):
                    row = screen.data_buffer[y]
                    captured.append(
                        "".join(row[x].char for x in sorted(row)).rstrip()
                    )
                app.exit()

            app.create_background_task(_probe())
            await app.run_async()
            return captured

    rows = asyncio.run(_render())

    # Toolbar is on the very last row.
    assert "title . model . [tier:c1]" in rows[-1]
    # Directly above: bottom rule, the you-row, top rule (compact frame).
    assert set(rows[-2]) == {"─"}
    assert "you" in rows[-3]
    assert set(rows[-4]) == {"─"}
    # The reserved slack is blank rows above the frame (greedy spacer).
    assert rows[0] == ""


def test_buffer_is_single_line() -> None:
    """Sanity: the input buffer is single-line (so exact(1) can't clip)."""
    with create_pipe_input() as pipe:
        chat = _build_app(pipe)
        body = chat.application.layout.container.content
        input_row = next(c for c in body.children if getattr(c, "children", None))
        buffer_window = next(w for w in input_row.children if _has_buffer_control(w))
        buf = buffer_window.content.buffer
        assert isinstance(buf, Buffer)
        assert buf.multiline() is False
