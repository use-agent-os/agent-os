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

import pytest
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.data_structures import Size
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.output.vt100 import Vt100_Output

from agentos.cli.repl.app import ChatApplication
from agentos.engine.commands import Surface


def _build_app(pipe, *, history=None) -> ChatApplication:  # type: ignore[no-untyped-def]
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
        history=history,
    )


def _has_buffer_control(window: object) -> bool:
    content = getattr(window, "content", None)
    return isinstance(content, BufferControl)


def test_input_buffer_height_grows_from_one_to_ten_lines() -> None:
    """The input grows with its draft but cannot exceed ten visible rows."""
    with create_pipe_input() as pipe:
        chat = _build_app(pipe)
        body = chat.application.layout.container.content
        input_row = next(c for c in body.children if getattr(c, "children", None))
        buffer_window = next(w for w in input_row.children if _has_buffer_control(w))
        height = buffer_window.height
        assert callable(height)
        assert height().preferred == 1

        chat._buffer.text = "\n".join(str(number) for number in range(12))
        assert height().preferred == 10


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


def test_buffer_is_multiline() -> None:
    with create_pipe_input() as pipe:
        chat = _build_app(pipe)
        body = chat.application.layout.container.content
        input_row = next(c for c in body.children if getattr(c, "children", None))
        buffer_window = next(w for w in input_row.children if _has_buffer_control(w))
        buf = buffer_window.content.buffer
        assert isinstance(buf, Buffer)
        assert buf.multiline() is True


def test_multiline_arrow_navigation_precedes_history_navigation() -> None:
    """Up/Down traverse draft lines and only enter history at boundaries."""
    async def _exercise() -> None:
        history = InMemoryHistory()
        history.append_string("previous prompt")

        with create_pipe_input() as pipe:
            chat = _build_app(pipe, history=history)
            app = chat.application

            async def _probe() -> None:
                await asyncio.sleep(0.05)
                try:
                    buf = chat._buffer
                    buf.text = "first line\nsecond line"
                    buf.cursor_position = len(buf.text)

                    buf.auto_up()
                    assert buf.text == "first line\nsecond line"
                    assert buf.document.cursor_position_row == 0

                    buf.auto_up()
                    assert buf.text == "previous prompt"

                    buf.auto_down()
                    assert buf.text == "first line\nsecond line"
                    assert buf.document.cursor_position_row == 1
                finally:
                    app.exit()

            app.create_background_task(_probe())
            await app.run_async()

    asyncio.run(_exercise())


@pytest.mark.parametrize(
    ("key", "expected_column"),
    [
        (Keys.Home, 0),
        ("c-a", 0),
        (Keys.End, len("middle text")),
        ("c-e", len("middle text")),
    ],
)
def test_line_boundary_shortcuts_stay_within_current_draft_line(
    key: Keys | str,
    expected_column: int,
) -> None:
    from agentos.cli.repl.app import _build_key_bindings

    bindings = _build_key_bindings()
    matching = [binding for binding in bindings.bindings if tuple(binding.keys) == (key,)]
    assert len(matching) == 1

    with create_pipe_input() as pipe:
        chat = _build_app(pipe)
        chat._buffer.text = "first\nmiddle text\nlast"
        chat._buffer.cursor_position = len("first\nmiddle")

        class _Event:
            app = chat.application

            @property
            def current_buffer(self) -> Buffer:
                return chat._buffer

        matching[0].handler(_Event())

        document = chat._buffer.document
        assert document.cursor_position_row == 1
        assert document.cursor_position_col == expected_column


def test_frame_with_more_than_ten_lines_keeps_toolbar_at_bottom() -> None:
    async def _render() -> list[str]:
        with create_pipe_input() as pipe:
            chat = _build_app(pipe)
            chat._buffer.text = "\n".join(f"line {number}" for number in range(12))
            app = chat.application
            captured: list[str] = []

            async def _probe() -> None:
                await asyncio.sleep(0.05)
                app.renderer._min_available_height = 15  # type: ignore[attr-defined]
                app._redraw()
                screen = app.renderer._last_screen  # type: ignore[attr-defined]
                assert screen is not None
                for y in range(screen.height):
                    row = screen.data_buffer[y]
                    captured.append("".join(row[x].char for x in sorted(row)).rstrip())
                app.exit()

            app.create_background_task(_probe())
            await app.run_async()
            return captured

    rows = asyncio.run(_render())

    assert "title . model . [tier:c1]" in rows[-1]
    rule_rows = [index for index, row in enumerate(rows) if row and set(row) == {"─"}]
    assert len(rule_rows) == 2
    assert rule_rows[1] == len(rows) - 2
    assert rule_rows[1] - rule_rows[0] == 11
