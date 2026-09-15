"""edit_file and write_file must round-trip a file's line endings, not normalise them.

Every assertion here is on *bytes*: ``Path.read_text`` folds CRLF to LF, so a
``read_text()`` comparison passes against code that rewrote every line of the
file to ``os.linesep`` (#1909, the edit_file/write_file half of #1124).
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
async def test_edit_keeps_lf_on_every_platform(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    target.write_bytes(b"a = 1\nb = 2\nc = 3\n")

    with tool_context(tmp_path):
        await edit_file(str(target), "b = 2", "b = 22")

    assert target.read_bytes() == b"a = 1\nb = 22\nc = 3\n"


@pytest.mark.asyncio
async def test_edit_keeps_crlf_on_every_platform(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    target.write_bytes(b"a = 1\r\nb = 2\r\nc = 3\r\n")

    with tool_context(tmp_path):
        await edit_file(str(target), "b = 2", "b = 22")

    assert target.read_bytes() == b"a = 1\r\nb = 22\r\nc = 3\r\n"


@pytest.mark.asyncio
async def test_multi_line_old_text_matches_a_crlf_file(tmp_path: Path) -> None:
    """The model writes LF-separated old_text; it must still find CRLF lines."""
    target = tmp_path / "sample.py"
    target.write_bytes(b"a = 1\r\nb = 2\r\nc = 3\r\n")

    with tool_context(tmp_path):
        await edit_file(str(target), "b = 2\nc = 3", "b = 22\nc = 33")

    assert target.read_bytes() == b"a = 1\r\nb = 22\r\nc = 33\r\n"


@pytest.mark.asyncio
async def test_inserted_lines_take_the_file_convention(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    target.write_bytes(b"a = 1\r\nc = 3\r\n")

    with tool_context(tmp_path):
        await edit_file(str(target), "a = 1", "a = 1\nb = 2")

    assert target.read_bytes() == b"a = 1\r\nb = 2\r\nc = 3\r\n"


@pytest.mark.asyncio
async def test_crlf_in_new_text_does_not_double_up(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    target.write_bytes(b"a = 1\r\nc = 3\r\n")

    with tool_context(tmp_path):
        await edit_file(str(target), "a = 1", "a = 1\r\nb = 2")

    assert target.read_bytes() == b"a = 1\r\nb = 2\r\nc = 3\r\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("original", "expected"),
    [
        # CRLF majority: the inserted line follows it.
        (b"a\r\nb\r\nc\n", b"a\r\nX\r\nb\r\nc\n"),
        # LF majority.
        (b"a\nb\nc\r\n", b"a\nX\nb\nc\r\n"),
        # Tie: the first ending seen wins.
        (b"a\r\nb\n", b"a\r\nX\r\nb\n"),
        (b"a\nb\r\n", b"a\nX\nb\r\n"),
    ],
)
async def test_mixed_ending_file_picks_the_majority_for_new_lines(
    tmp_path: Path, original: bytes, expected: bytes
) -> None:
    target = tmp_path / "sample.txt"
    target.write_bytes(original)

    with tool_context(tmp_path):
        await edit_file(str(target), "a", "a\nX")

    assert target.read_bytes() == expected


@pytest.mark.asyncio
async def test_cr_only_file_still_matches_lf_old_text_and_keeps_cr(tmp_path: Path) -> None:
    """read_file shows a classic-Mac file LF-separated, so that is what the model sends back."""
    target = tmp_path / "sample.txt"
    target.write_bytes(b"a = 1\rb = 2\rc = 3\r")

    with tool_context(tmp_path):
        await edit_file(str(target), "b = 2\nc = 3", "b = 22\nb2 = 0\nc = 33")

    assert target.read_bytes() == b"a = 1\rb = 22\rb2 = 0\rc = 33\r"


@pytest.mark.asyncio
async def test_file_without_trailing_newline_keeps_its_shape(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_bytes(b"hello\r\nworld")

    with tool_context(tmp_path):
        await edit_file(str(target), "world", "WORLD")

    assert target.read_bytes() == b"hello\r\nWORLD"


@pytest.mark.asyncio
async def test_write_file_emits_lf_verbatim(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"

    with tool_context(tmp_path):
        await write_file(str(target), "one\ntwo\n")

    assert target.read_bytes() == b"one\ntwo\n"


@pytest.mark.asyncio
async def test_write_file_emits_crlf_verbatim(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"

    with tool_context(tmp_path):
        await write_file(str(target), "one\r\ntwo\r\n")

    assert target.read_bytes() == b"one\r\ntwo\r\n"
