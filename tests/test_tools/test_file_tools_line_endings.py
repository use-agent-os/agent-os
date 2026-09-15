"""``edit_file`` and ``write_file`` must round-trip line endings, not normalise them.

``apply_patch`` was given this property in #1124; the two general-purpose file
tools still read through ``Path.read_text`` (which folds every ending to ``\\n``)
and wrote back through ``Path.write_text`` (which re-emits ``os.linesep``), so a
one-line edit rewrote every line in the file.

Every assertion is on the file's *bytes*. A test that compares ``read_text()``
output passes against the unfixed code, because the translation hides itself.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from agentos.tools.builtin import filesystem as fs
from agentos.tools.types import CallerKind, ToolContext, current_tool_context


def _original_async(fn: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
    """Unwrap the @tool and @sandboxed decorators to reach the implementation."""

    return fn.__wrapped__.__wrapped__  # type: ignore[attr-defined, no-any-return]


edit_file = _original_async(fs.edit_file)
write_file = _original_async(fs.write_file)


@contextmanager
def tool_context(workspace: Path) -> Iterator[None]:
    token = current_tool_context.set(
        ToolContext(
            caller_kind=CallerKind.CLI,
            channel_kind="cli",
            channel_id="cli:test",
            workspace_dir=str(workspace),
        )
    )
    try:
        yield
    finally:
        current_tool_context.reset(token)


@pytest.mark.asyncio
async def test_edit_file_keeps_an_lf_file_on_lf(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    target.write_bytes(b"a = 1\nb = 2\nc = 3\n")

    with tool_context(tmp_path):
        await edit_file(str(target), "b = 2", "b = 22")

    assert target.read_bytes() == b"a = 1\nb = 22\nc = 3\n"


@pytest.mark.asyncio
async def test_edit_file_keeps_a_crlf_file_on_crlf(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    target.write_bytes(b"a = 1\r\nb = 2\r\nc = 3\r\n")

    with tool_context(tmp_path):
        await edit_file(str(target), "b = 2", "b = 22")

    assert target.read_bytes() == b"a = 1\r\nb = 22\r\nc = 3\r\n"


@pytest.mark.asyncio
async def test_edit_file_does_not_rewrite_untouched_lines(tmp_path: Path) -> None:
    """A one-line edit must not come back as a whole-file diff."""
    target = tmp_path / "sample.py"
    target.write_bytes(b"a = 1\nb = 2\nc = 3\nd = 4\ne = 5\n")

    with tool_context(tmp_path):
        await edit_file(str(target), "c = 3", "c = 33")

    assert target.read_bytes() == b"a = 1\nb = 2\nc = 33\nd = 4\ne = 5\n"


@pytest.mark.asyncio
async def test_edit_file_matches_multi_line_old_text_in_a_crlf_file(tmp_path: Path) -> None:
    """A model sends LF-separated ``old_text``; matching must not regress on CRLF."""
    target = tmp_path / "sample.py"
    target.write_bytes(b"def f():\r\n    a = 1\r\n    b = 2\r\n    return a\r\n")

    with tool_context(tmp_path):
        result = await edit_file(str(target), "    a = 1\n    b = 2", "    a = 10\n    b = 20")

    # Still the exact strategy: no fuzzy marker in the result line.
    assert "[match=" not in result
    assert target.read_bytes() == b"def f():\r\n    a = 10\r\n    b = 20\r\n    return a\r\n"


@pytest.mark.asyncio
async def test_edit_file_gives_an_inserted_line_the_files_own_ending(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    target.write_bytes(b"x = 1\r\ny = 2\r\n")

    with tool_context(tmp_path):
        await edit_file(str(target), "x = 1", "x = 1\nw = 0")

    assert target.read_bytes() == b"x = 1\r\nw = 0\r\ny = 2\r\n"


@pytest.mark.asyncio
async def test_write_file_writes_the_callers_endings_verbatim(tmp_path: Path) -> None:
    """The content is the authority, the same rule ``*** Add File`` follows."""
    target = tmp_path / "new.txt"

    with tool_context(tmp_path):
        await write_file(str(target), "one\ntwo\n")

    assert target.read_bytes() == b"one\ntwo\n"


@pytest.mark.asyncio
async def test_write_file_keeps_crlf_content_on_crlf(tmp_path: Path) -> None:
    target = tmp_path / "new.txt"

    with tool_context(tmp_path):
        await write_file(str(target), "one\r\ntwo\r\n")

    assert target.read_bytes() == b"one\r\ntwo\r\n"
