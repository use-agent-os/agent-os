import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from agentos.tools.builtin.patch import _validate_path, apply_patch
from agentos.tools.types import ToolContext, current_tool_context


def _original_async(fn: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
    return fn.__wrapped__.__wrapped__  # type: ignore[attr-defined, no-any-return]


def test_validate_path_resolves_workspace_alias() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir).resolve()
        ctx = ToolContext(workspace_dir=str(root))
        token = current_tool_context.set(ctx)
        try:
            resolved = _validate_path("/workspace/src/app.py")
            assert resolved == root / "src" / "app.py"
        finally:
            current_tool_context.reset(token)


@pytest.mark.asyncio
async def test_apply_patch_add_file_with_workspace_alias() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir).resolve()
        ctx = ToolContext(workspace_dir=str(root))
        token = current_tool_context.set(ctx)
        try:
            patch_text = (
                "*** Begin Patch\n"
                "*** Add File: /workspace/test_alias.txt\n"
                "+hello world\n"
                "*** End Patch\n"
            )
            res = await _original_async(apply_patch)(patch_text)
            assert "Applied patch" in res
            target_file = root / "test_alias.txt"
            assert target_file.is_file()
            assert target_file.read_text(encoding="utf-8") == "hello world"
        finally:
            current_tool_context.reset(token)


@pytest.mark.asyncio
async def test_apply_patch_update_file_with_workspace_alias() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir).resolve()
        target_file = root / "existing.txt"
        target_file.write_text("line1\nline2\nline3\n", encoding="utf-8")

        ctx = ToolContext(workspace_dir=str(root))
        token = current_tool_context.set(ctx)
        try:
            patch_text = (
                "*** Begin Patch\n"
                "*** Update File: /workspace/existing.txt\n"
                "@@@ -1,3 +1,4 @@@\n"
                " line1\n"
                "+line1.5\n"
                " line2\n"
                " line3\n"
                "*** End Patch\n"
            )
            res = await _original_async(apply_patch)(patch_text)
            assert "Applied patch" in res
            assert target_file.read_text(encoding="utf-8") == "line1\nline1.5\nline2\nline3\n"
        finally:
            current_tool_context.reset(token)
