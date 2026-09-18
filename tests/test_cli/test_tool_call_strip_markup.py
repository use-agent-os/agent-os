"""The chat's tool status line shows tool arguments literally.

``_ToolCallStrip`` interpolated the tool's argument summary (a shell command, a
path, a search query) and its error text into Rich markup. ``awk -F'[/]' …``
raised ``MarkupError`` out of the stream renderer, which nothing on the way up
catches, so the whole chat session ended; ``*.[ch]`` quietly printed as ``*.``.
"""

from __future__ import annotations

import asyncio
from io import StringIO
from typing import Any

import pytest
from rich.console import Console

from agentos.cli.repl import stream as stream_mod
from agentos.cli.repl.stream import StreamingRenderer
from agentos.cli.ui import ACCENT

AWK = "awk -F'[/]' '{print $NF}' paths.txt"


@pytest.fixture
def screen(monkeypatch: pytest.MonkeyPatch) -> StringIO:
    """Point the renderer's console at a plain, uncoloured buffer."""
    buf = StringIO()
    capture = Console(file=buf, highlight=False, force_terminal=False, no_color=True, width=200)
    monkeypatch.setattr(stream_mod, "console", capture)
    return buf


def _lines(buf: StringIO) -> list[str]:
    return [line for line in buf.getvalue().splitlines() if line.strip()]


@pytest.mark.parametrize(
    ("tool", "args", "shown"),
    [
        pytest.param("exec_command", {"command": AWK}, AWK, id="closing_tag_in_command"),
        pytest.param(
            "exec_command",
            {"command": "find . -name '*.[ch]' | xargs wc -l"},
            "find . -name '*.[ch]' | xargs wc -l",
            id="character_class",
        ),
        pytest.param(
            "read_file",
            {"path": "notes/[draft] plan.md"},
            "notes/[draft] plan.md",
            id="bracketed_path",
        ),
        pytest.param(
            "web_search",
            {"query": "llama [INST] and [/INST] tokens"},
            "llama [INST] and [/INST] tokens",
            id="query_with_tags",
        ),
    ],
)
def test_the_start_line_shows_the_argument_as_given(
    screen: StringIO, tool: str, args: dict[str, Any], shown: str
) -> None:
    StreamingRenderer().tool_start(tool, args, "t1")

    assert _lines(screen) == [f"▸ {tool} {shown}"]


def test_the_error_line_shows_the_error_as_given(screen: StringIO) -> None:
    renderer = StreamingRenderer()
    renderer.tool_start("read_file", {"path": "a.md"}, "t1")
    renderer.tool_finished("t1", success=False, error="Path not found: notes/[draft] [/] plan.md")

    assert _lines(screen)[-1] == "✗ read_file: Path not found: notes/[draft] [/] plan.md"


def test_a_coalesced_run_with_a_bracketed_command_still_counts(screen: StringIO) -> None:
    renderer = StreamingRenderer()
    for index in range(4):
        renderer.tool_start("exec_command", {"command": AWK}, f"t{index}")
    renderer._strip.flush()

    lines = _lines(screen)
    assert lines[:3] == [f"▸ exec_command {AWK}", f"▸ exec_command {AWK}", "▸ exec_command ×3"]
    assert lines[3].startswith("▸ exec_command ×4 total ")


class _Gateway:
    """Replays the events the gateway streams for a turn that runs AWK."""

    async def send_message(self, session_key: str, message: str, **kwargs: Any):
        yield {
            "event": "session.event.tool_use_start",
            "tool_name": "exec_command",
            "input": {"command": AWK},
            "tool_use_id": "t1",
        }
        yield {
            "event": "session.event.tool_result",
            "tool_use_id": "t1",
            "tool_name": "exec_command",
            "result": "README.md\nsetup.py",
            "execution_status": "success",
        }
        yield {"event": "session.event.text_delta", "text": "The files are README.md and setup.py."}
        yield {"event": "session.event.done"}

    async def abort_session(self, key: str) -> None:
        return None

    async def resolve_approval(self, *args: Any, **kwargs: Any) -> None:
        return None


def test_a_gateway_turn_that_runs_the_command_still_delivers_its_reply(
    screen: StringIO,
) -> None:
    """Through the entry point the REPL uses for every gateway turn."""
    from agentos.cli.tui.adapters.runtime_bridge import stream_response_gateway

    result = asyncio.run(
        stream_response_gateway(_Gateway(), "agent:main:main", "list the files", {"mode": None})
    )

    assert result.error is None
    assert result.text == "The files are README.md and setup.py."
    assert f"▸ exec_command {AWK}" in _lines(screen)


def test_the_status_line_keeps_its_colours(monkeypatch: pytest.MonkeyPatch) -> None:
    """For plain arguments the rows are byte-identical to the old markup rendering."""
    buf = StringIO()
    colour = Console(file=buf, force_terminal=True, color_system="truecolor", width=200)
    monkeypatch.setattr(stream_mod, "console", colour)

    renderer = StreamingRenderer()
    renderer.tool_start("exec_command", {"command": "ls -la"}, "t1")
    renderer.tool_finished("t1", success=False, error="denied")

    with colour.capture() as expected:
        colour.print(f"[{ACCENT}]▸[/] [dim]exec_command ls -la[/dim]")
        colour.print("[red]✗[/] [dim]exec_command: denied[/dim]")
    assert buf.getvalue() == expected.get()
