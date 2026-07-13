from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest

from agentos.tools.builtin import filesystem
from agentos.tools.builtin import patch as patch_tool
from agentos.tools.types import ToolContext, current_tool_context


def _original_async(fn: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
    return fn.__wrapped__.__wrapped__  # type: ignore[attr-defined, no-any-return]


@pytest.mark.asyncio
async def test_filesystem_write_notifies_bootstrap_or_memory_sources(tmp_path) -> None:
    memory_calls: list[tuple[str, str]] = []
    bootstrap_calls: list[tuple[str, str]] = []
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            agent_id="main",
            workspace_dir=str(tmp_path),
            memory_source_dir=str(tmp_path),
            on_memory_source_write=lambda agent_id, path: memory_calls.append((agent_id, path)),
            on_bootstrap_source_write=lambda agent_id, path: bootstrap_calls.append(
                (agent_id, path)
            ),
        )
    )
    write_file = _original_async(filesystem.write_file)
    try:
        await write_file("USER.md", "Name: Alice\n")
        await write_file("MEMORY.md", "# MEMORY\n")
        await write_file("memory/USER.md", "not a bootstrap file\n")
    finally:
        current_tool_context.reset(token)

    assert bootstrap_calls == [("main", "USER.md")]
    assert ("main", "MEMORY.md") in memory_calls
    assert ("main", "memory/USER.md") in memory_calls


@pytest.mark.asyncio
async def test_patch_notifies_bootstrap_and_memory_sources(tmp_path) -> None:
    memory_calls: list[tuple[str, str]] = []
    bootstrap_calls: list[tuple[str, str]] = []
    (tmp_path / "USER.md").write_text("Name:\n", encoding="utf-8")
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "2026-05-01.md").write_text("old\n", encoding="utf-8")
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            agent_id="main",
            workspace_dir=str(tmp_path),
            memory_source_dir=str(tmp_path),
            on_memory_source_write=lambda agent_id, path: memory_calls.append((agent_id, path)),
            on_bootstrap_source_write=lambda agent_id, path: bootstrap_calls.append(
                (agent_id, path)
            ),
        )
    )
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        await apply_patch(
            """*** Begin Patch
*** Update File: USER.md
@@@ -1,1 +1,1 @@@
-Name:
+Name: Alice
*** Update File: memory/2026-05-01.md
@@@ -1,1 +1,1 @@@
-old
+new
*** End Patch"""
        )
    finally:
        current_tool_context.reset(token)

    assert bootstrap_calls == [("main", "USER.md")]
    assert memory_calls == [("main", "memory/2026-05-01.md")]
