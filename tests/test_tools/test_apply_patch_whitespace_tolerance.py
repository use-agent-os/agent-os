from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from agentos.tools.builtin import patch as patch_tool
from agentos.tools.types import ToolContext, current_tool_context


def _original_async(fn: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
    return fn.__wrapped__.__wrapped__  # type: ignore[attr-defined, no-any-return]


@pytest.mark.asyncio
async def test_apply_patch_whitespace_tolerant_context(tmp_path: Path) -> None:
    token = current_tool_context.set(ToolContext(workspace_dir=str(tmp_path)))
    file_path = tmp_path / "hello.py"
    # Target file has trailing spaces on context lines
    file_path.write_text(
        "def hello():   \n    greeting = 'hi'  \n    return greeting\n", encoding="utf-8"
    )

    # Patch context does not have trailing whitespace
    patch_text = (
        "*** Begin Patch\n"
        "*** Update File: hello.py\n"
        "@@@ -1,3 +1,3 @@@\n"
        " def hello():\n"
        "-    greeting = 'hi'\n"
        "+    greeting = 'hello world'\n"
        "     return greeting\n"
        "*** End Patch\n"
    )

    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        result = await apply_patch(patch_text)
    finally:
        current_tool_context.reset(token)

    assert "Applied patch: 1 file(s) modified" in result
    content = file_path.read_text(encoding="utf-8")
    assert "greeting = 'hello world'" in content
    # First line should retain original file format
    assert content.startswith("def hello():   \n")


@pytest.mark.asyncio
async def test_apply_patch_whitespace_tolerant_hunk_trailing_spaces(tmp_path: Path) -> None:
    token = current_tool_context.set(ToolContext(workspace_dir=str(tmp_path)))
    file_path = tmp_path / "calc.py"
    file_path.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    # Patch has trailing spaces on context and delete line
    patch_text = (
        "*** Begin Patch\n"
        "*** Update File: calc.py\n"
        "@@@ -1,2 +1,2 @@@\n"
        " def add(a, b):   \n"
        "-    return a + b  \t \n"
        "+    return a + b + 0\n"
        "*** End Patch\n"
    )

    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        result = await apply_patch(patch_text)
    finally:
        current_tool_context.reset(token)

    assert "Applied patch: 1 file(s) modified" in result
    content = file_path.read_text(encoding="utf-8")
    assert "return a + b + 0" in content


@pytest.mark.asyncio
async def test_apply_patch_rejects_actual_content_mismatch(tmp_path: Path) -> None:
    token = current_tool_context.set(ToolContext(workspace_dir=str(tmp_path)))
    file_path = tmp_path / "config.py"
    file_path.write_text("DEBUG = False\nPORT = 8080\n", encoding="utf-8")

    # Patch expects different variable name
    patch_text = (
        "*** Begin Patch\n"
        "*** Update File: config.py\n"
        "@@@ -1,2 +1,2 @@@\n"
        " DEBUG_MODE = False\n"
        "-PORT = 8080\n"
        "+PORT = 9090\n"
        "*** End Patch\n"
    )

    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        with pytest.raises(ValueError, match="Context mismatch at line 1"):
            await apply_patch(patch_text)
    finally:
        current_tool_context.reset(token)
