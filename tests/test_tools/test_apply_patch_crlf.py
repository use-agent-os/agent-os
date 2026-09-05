"""Regression tests for apply_patch line ending handling (CRLF / LF)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from agentos.tools.builtin import patch as patch_tool
from agentos.tools.types import ToolContext, current_tool_context


def _original_async(fn: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
    return fn.__wrapped__.__wrapped__  # type: ignore[attr-defined, no-any-return]


@pytest.mark.asyncio
async def test_apply_patch_updates_crlf_file(tmp_path: Path) -> None:
    target = tmp_path / "crlf_file.py"
    # Write file with explicit CRLF line endings
    target.write_bytes(b"def greet():\r\n    print('hello')\r\n    return True\r\n")

    token = current_tool_context.set(ToolContext(workspace_dir=str(tmp_path)))
    apply_patch = _original_async(patch_tool.apply_patch)
    patch_text = (
        "*** Begin Patch\n"
        "*** Update File: crlf_file.py\n"
        "@@@ -1,3 +1,3 @@@\n"
        " def greet():\n"
        "-    print('hello')\n"
        "+    print('world')\n"
        "     return True\n"
        "*** End Patch"
    )
    try:
        result = await apply_patch(patch_text)
    finally:
        current_tool_context.reset(token)

    assert "1 file(s) modified" in result
    raw_content = target.read_bytes()
    assert b"\r\n" in raw_content
    # Ensure no lone \n without \r
    assert raw_content.replace(b"\r\n", b"") == b"def greet():    print('world')    return True"
    assert raw_content == b"def greet():\r\n    print('world')\r\n    return True\r\n"


@pytest.mark.asyncio
async def test_apply_patch_preserves_lf_file(tmp_path: Path) -> None:
    target = tmp_path / "lf_file.py"
    # Write file with explicit LF line endings
    target.write_bytes(b"def greet():\n    print('hello')\n    return True\n")

    token = current_tool_context.set(ToolContext(workspace_dir=str(tmp_path)))
    apply_patch = _original_async(patch_tool.apply_patch)
    patch_text = (
        "*** Begin Patch\n"
        "*** Update File: lf_file.py\n"
        "@@@ -1,3 +1,3 @@@\n"
        " def greet():\n"
        "-    print('hello')\n"
        "+    print('world')\n"
        "     return True\n"
        "*** End Patch"
    )
    try:
        result = await apply_patch(patch_text)
    finally:
        current_tool_context.reset(token)

    assert "1 file(s) modified" in result
    raw_content = target.read_bytes()
    assert b"\r" not in raw_content
    assert raw_content == b"def greet():\n    print('world')\n    return True\n"


@pytest.mark.asyncio
async def test_apply_patch_crlf_addition_and_deletion(tmp_path: Path) -> None:
    target = tmp_path / "multi_line_crlf.py"
    target.write_bytes(b"line 1\r\nline 2\r\nline 3\r\nline 4\r\n")

    token = current_tool_context.set(ToolContext(workspace_dir=str(tmp_path)))
    apply_patch = _original_async(patch_tool.apply_patch)
    patch_text = (
        "*** Begin Patch\n"
        "*** Update File: multi_line_crlf.py\n"
        "@@@ -1,4 +1,4 @@@\n"
        " line 1\n"
        "-line 2\n"
        "-line 3\n"
        "+line 2 modified\n"
        "+line 2.5 inserted\n"
        " line 4\n"
        "*** End Patch"
    )
    try:
        result = await apply_patch(patch_text)
    finally:
        current_tool_context.reset(token)

    assert "1 file(s) modified" in result
    raw_content = target.read_bytes()
    assert raw_content == b"line 1\r\nline 2 modified\r\nline 2.5 inserted\r\nline 4\r\n"
