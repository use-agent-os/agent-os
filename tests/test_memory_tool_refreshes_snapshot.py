"""The curated ``memory`` tool notifies ``on_memory_write`` like its siblings do.

``TurnRunner`` freezes MEMORY.md/USER.md into a per-session ``MemorySnapshot``
and only rebuilds it through the ``on_memory_write`` callback (see
``refresh_memory_snapshot``). ``memory_save`` and ``memory_delete`` (#1806)
both fire it. ``memory`` -- the primary, default-exposed tool the model is
told to use for durable facts, via add/replace/remove and the batch
``operations`` shape -- wrote to the same curated store but never notified:
a committed write landed on disk, yet the prompt kept injecting the
pre-write memory_md for the rest of the session, invisibly to the model.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentos.tools.builtin.memory_tools import create_memory_tools
from agentos.tools.registry import ToolRegistry
from agentos.tools.types import CallerKind, ToolContext, current_tool_context


class _FakeVectorStore:
    async def index_file(self, *, path: str, content: str, source) -> int:
        return 1 if content else 0

    async def remove_file(self, path: str) -> None:
        return None

    async def total_size(self) -> int:
        return 0


@pytest.fixture()
def tools(tmp_path):
    refreshed: list[str] = []
    registry = ToolRegistry()
    create_memory_tools(
        stores=_FakeVectorStore(),
        retrievers=SimpleNamespace(),
        memory_dir=str(tmp_path),
        registry=registry,
        on_memory_write=refreshed.append,
    )
    handlers = {name: registry.get(name).handler for name in registry.list_names()}
    return SimpleNamespace(handlers=handlers, refreshed=refreshed, root=tmp_path)


async def test_memory_add_notifies_snapshot_refresh(tools) -> None:
    result = await tools.handlers["memory"](action="add", content="deployed to prod")

    assert '"success": true' in result.lower()
    assert tools.refreshed == ["main"]


async def test_memory_replace_notifies_snapshot_refresh(tools) -> None:
    await tools.handlers["memory"](action="add", content="deployed to staging")
    tools.refreshed.clear()

    result = await tools.handlers["memory"](
        action="replace", old_text="staging", content="deployed to prod"
    )

    assert '"success": true' in result.lower()
    assert tools.refreshed == ["main"]


async def test_memory_remove_notifies_snapshot_refresh(tools) -> None:
    await tools.handlers["memory"](action="add", content="stale fact")
    tools.refreshed.clear()

    result = await tools.handlers["memory"](action="remove", old_text="stale fact")

    assert '"success": true' in result.lower()
    assert tools.refreshed == ["main"]


async def test_memory_batch_notifies_snapshot_refresh_once(tools) -> None:
    result = await tools.handlers["memory"](
        operations=[
            {"action": "add", "content": "fact one"},
            {"action": "add", "content": "fact two"},
        ]
    )

    assert '"success": true' in result.lower()
    # One batch call is one refresh, not one per operation inside it.
    assert tools.refreshed == ["main"]


async def test_memory_user_target_notifies_snapshot_refresh(tools) -> None:
    """USER.md is a separate curated target from MEMORY.md but rebuilds
    through the same per-agent snapshot the model's prompt reads from."""
    result = await tools.handlers["memory"](
        action="add", target="user", content="prefers concise answers"
    )

    assert '"success": true' in result.lower()
    assert tools.refreshed == ["main"]


async def test_memory_does_not_notify_when_the_write_fails(tools) -> None:
    missing = await tools.handlers["memory"](action="replace", old_text="nope", content="new")
    assert '"success": false' in missing.lower()

    empty_add = await tools.handlers["memory"](action="add", content="")
    assert '"success": false' in empty_add.lower()

    assert tools.refreshed == []


async def test_memory_notifies_for_the_calling_agent(tools) -> None:
    token = current_tool_context.set(
        ToolContext(
            caller_kind=CallerKind.CLI,
            channel_kind="cli",
            channel_id="cli:test",
            agent_id="research",
        )
    )
    try:
        await tools.handlers["memory"](action="add", content="agent-scoped fact")
    finally:
        current_tool_context.reset(token)

    assert tools.refreshed == ["research"]
