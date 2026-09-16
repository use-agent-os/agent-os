"""write_file must calculate and report exact UTF-8 byte count, not character length.

Regression test for #2478 (same defect class as #2036, at the site it missed).
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
async def test_write_file_reports_exact_bytes_for_multibyte_utf8(tmp_path: Path) -> None:
    """Non-ASCII multibyte UTF-8 characters must report actual byte count, not character length."""
    with tool_context(tmp_path):
        target = tmp_path / "emojis.txt"
        content = "🌟" * 10
        char_count = len(content)
        byte_count = len(content.encode("utf-8"))
        assert char_count == 10
        assert byte_count == 40

        result = await write_file(str(target), content)

        assert f"Written {byte_count} bytes to {target}" == result
        assert target.stat().st_size == byte_count


@pytest.mark.asyncio
async def test_write_file_reports_exact_bytes_for_ascii(tmp_path: Path) -> None:
    """ASCII content bytes match character length."""
    with tool_context(tmp_path):
        target = tmp_path / "ascii.txt"
        content = "hello world\n"
        byte_count = len(content.encode("utf-8"))

        result = await write_file(str(target), content)

        assert f"Written {byte_count} bytes to {target}" == result
        assert target.stat().st_size == byte_count


@pytest.mark.asyncio
async def test_write_file_reports_exact_bytes_for_mixed_cjk_and_accents(tmp_path: Path) -> None:
    """CJK and accented strings report their true encoded UTF-8 byte count."""
    with tool_context(tmp_path):
        target = tmp_path / "cjk_accent.txt"
        content = "你好，世界！Café ☕\n"
        char_count = len(content)
        byte_count = len(content.encode("utf-8"))
        assert char_count != byte_count

        result = await write_file(str(target), content)

        assert f"Written {byte_count} bytes to {target}" == result
        assert target.stat().st_size == byte_count


@pytest.mark.asyncio
async def test_write_file_overwrite_reports_new_byte_count(tmp_path: Path) -> None:
    """Overwriting an existing file reports the newly written byte size."""
    with tool_context(tmp_path):
        target = tmp_path / "target.txt"
        first = "short"
        second = "🚀 longer content with multibyte symbols: 12345"

        await write_file(str(target), first)
        result = await write_file(str(target), second)

        expected_bytes = len(second.encode("utf-8"))
        assert f"Written {expected_bytes} bytes to {target}" == result
        assert target.stat().st_size == expected_bytes
