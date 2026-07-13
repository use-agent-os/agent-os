"""agents_list tool returns entries from the injected AgentRegistry."""

from __future__ import annotations

import json

import pytest

from agentos.tools.builtin import agents as agents_tool
from agentos.tools.types import ToolError


class _StubRegistry:
    def __init__(self, entries: list[dict]) -> None:
        self._entries = entries

    async def list_agents(self, *, include_builtin: bool = True) -> list[dict]:
        return list(self._entries)


@pytest.mark.asyncio
async def test_agents_list_uses_injected_registry() -> None:
    registry = _StubRegistry(
        [
            {"id": "main", "name": "Main Agent", "isBuiltin": True, "model": None},
            {"id": "research", "name": "Research", "isBuiltin": False, "model": "haiku"},
        ]
    )
    agents_tool.set_agent_registry(registry)
    try:
        out = await agents_tool.agents_list()
    finally:
        agents_tool.set_agent_registry(None)

    parsed = json.loads(out)
    assert isinstance(parsed, list)
    ids = [entry.get("id") for entry in parsed]
    assert "main" in ids
    assert "research" in ids


@pytest.mark.asyncio
async def test_agents_list_fails_when_registry_missing() -> None:
    agents_tool.set_agent_registry(None)
    with pytest.raises(ToolError):
        await agents_tool.agents_list()
