"""Issue #2478: ``write_file`` reported code points as bytes.

``return f"Written {len(content)} bytes to {p}"`` -- ``content`` is a ``str``,
so ``len`` counts Unicode code points. Every multibyte character was
under-reported: ten emoji came back as "Written 10 bytes" for a 40-byte file.
Callers comparing that number against disk limits, ``--max-bytes`` or tool
budgets were reasoning from the wrong figure.

The content is now encoded once and written as bytes, and the number reported
is the length of what reached the disk.
"""

from __future__ import annotations

import os
import re
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from agentos.tools.builtin import filesystem as fs
from agentos.tools.types import CallerKind, ToolContext, current_tool_context


def _original_async(fn: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
    """Unwrap the @tool and @sandboxed decorators to reach the implementation."""
    return fn.__wrapped__.__wrapped__  # type: ignore[attr-defined, no-any-return]


write_file = _original_async(fs.write_file)
edit_file = _original_async(fs.edit_file)

REPORT = re.compile(r"^Written (\d+) bytes to ")


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


def reported(result: str) -> int:
    match = REPORT.match(result)
    assert match is not None, result
    return int(match.group(1))


# ── the issue ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ten_emoji_are_forty_bytes(tmp_path: Path) -> None:
    target = tmp_path / "test.txt"
    with tool_context(tmp_path):
        result = await write_file(str(target), "🌟" * 10)

    assert reported(result) == 40
    assert os.path.getsize(target) == 40


# ── the report is what reached the disk, whatever the script ────────────────


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("", 0),
        ("ok", 2),
        ("hello world\n", 12),
        ("é", 2),  # 2-byte sequence
        ("ü" * 5, 10),
        ("日本語", 9),  # 3-byte sequences
        ("한국어 테스트", 19),
        ("🌟", 4),  # 4-byte sequence
        ("🇯🇵", 8),  # two regional indicators
        ("👨‍👩‍👧", 18),  # ZWJ sequence: 3 emoji + 2 joiners
        ("é", 3),  # combining acute accent
        ("﻿bom", 6),  # a BOM the caller put there is content
        ("mixed: a é 日 🌟", 20),  # 7 + 1 + 1 + 2 + 1 + 3 + 1 + 4
    ],
)
@pytest.mark.asyncio
async def test_the_report_equals_the_encoded_length(
    tmp_path: Path, content: str, expected: int
) -> None:
    target = tmp_path / "f.txt"
    with tool_context(tmp_path):
        result = await write_file(str(target), content)

    assert reported(result) == expected
    assert reported(result) == len(content.encode("utf-8"))


@pytest.mark.parametrize(
    "content",
    [
        "",
        "ascii only",
        "日本語テキスト",
        pytest.param("🌟" * 100, id="100-emoji"),
        "a\r\nb\r\n",
        "a\nb\n",
        pytest.param("\r\n" * 50, id="50-crlf"),
        pytest.param("x" * 10_000 + "日" * 10_000, id="large-mixed"),
    ],
)
@pytest.mark.asyncio
async def test_the_report_equals_the_size_on_disk(tmp_path: Path, content: str) -> None:
    """``st_size`` is the ground truth; the report must never disagree with it."""
    target = tmp_path / "f.txt"
    with tool_context(tmp_path):
        result = await write_file(str(target), content)

    assert reported(result) == target.stat().st_size


@pytest.mark.asyncio
async def test_the_report_is_bytes_not_code_points_for_every_multibyte_case(tmp_path: Path) -> None:
    """Pins the direction of the old defect: wherever the two differ, bytes win."""
    for content in ("é", "日", "🌟", "👨‍👩‍👧"):
        target = tmp_path / "f.txt"
        with tool_context(tmp_path):
            result = await write_file(str(target), content)
        assert reported(result) > len(content), content


# ── the bytes themselves are unchanged by the rewrite ───────────────────────


@pytest.mark.parametrize("content", ["a\nb\n", "a\r\nb\r\n", "a\rb\r", "no newline at end"])
@pytest.mark.asyncio
async def test_line_endings_reach_the_disk_exactly_as_given(tmp_path: Path, content: str) -> None:
    """Writing bytes has no newline translation, which is the guarantee the
    old ``newline=""`` text write gave and must keep giving on Windows."""
    target = tmp_path / "f.txt"
    with tool_context(tmp_path):
        await write_file(str(target), content)

    assert target.read_bytes() == content.encode("utf-8")


@pytest.mark.asyncio
async def test_the_file_is_written_without_a_bom(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    with tool_context(tmp_path):
        await write_file(str(target), "日本語")

    assert not target.read_bytes().startswith(b"\xef\xbb\xbf")
    assert target.read_text(encoding="utf-8") == "日本語"


@pytest.mark.asyncio
async def test_an_existing_file_is_replaced_not_appended(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_bytes(b"x" * 1000)
    with tool_context(tmp_path):
        result = await write_file(str(target), "short")

    assert reported(result) == 5
    assert target.stat().st_size == 5


@pytest.mark.asyncio
async def test_parent_directories_are_still_created(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "c.txt"
    with tool_context(tmp_path):
        result = await write_file(str(target), "deep")

    assert reported(result) == 4
    assert target.read_text(encoding="utf-8") == "deep"


@pytest.mark.asyncio
async def test_a_lone_surrogate_still_fails_loudly_and_writes_nothing_new(tmp_path: Path) -> None:
    """Unencodable content raised inside the write before; it must still raise,
    and must not leave a truncated file where a good one was."""
    target = tmp_path / "f.txt"
    target.write_bytes(b"previous")
    with tool_context(tmp_path), pytest.raises(UnicodeEncodeError):
        await write_file(str(target), "bad \ud800 surrogate")

    assert target.read_bytes() == b"previous"


# ── the message shape other callers parse ───────────────────────────────────


@pytest.mark.asyncio
async def test_the_message_shape_is_unchanged(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    with tool_context(tmp_path):
        result = await write_file(str(target), "ok")

    assert result == f"Written 2 bytes to {target}"


@pytest.mark.asyncio
async def test_edit_file_reports_are_not_affected(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("hello 🌟\n", encoding="utf-8")
    with tool_context(tmp_path):
        result = await edit_file(str(target), "hello", "bye")

    assert result.startswith(f"Edited {target}")
    assert target.read_text(encoding="utf-8") == "bye 🌟\n"
