"""Tests for exposing provider-routed memory tools (TODO B4/B5)."""

from __future__ import annotations

from typing import Any

import pytest

from agentos.tools.builtin.memory_tools import (
    create_memory_tools,
    register_provider_memory_tools,
)
from agentos.tools.registry import ToolRegistry
from agentos.tools.types import ToolContext, current_tool_context


class DummyMemoryProviderManager:
    def __init__(self, schemas: list[dict[str, Any]], responses: dict[str, str] | None = None):
        self._schemas = schemas
        self._responses = responses or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return self._schemas

    def has_tool(self, name: str) -> bool:
        return any(s.get("name") == name for s in self._schemas)

    async def handle_tool_call(self, name: str, args: dict[str, Any], **kwargs: Any) -> str:
        self.calls.append((name, args))
        return self._responses.get(name, '{"status": "ok"}')


@pytest.mark.asyncio
async def test_register_provider_memory_tools_exposes_schemas():
    registry = ToolRegistry()
    pm = DummyMemoryProviderManager(
        schemas=[
            {
                "name": "ext_memory_search",
                "description": "External memory search tool",
                "parameters": {"query": {"type": "string"}},
                "required": ["query"],
            }
        ]
    )

    names = register_provider_memory_tools({"main": pm}, registry=registry)
    assert names == ["ext_memory_search"]

    registered = registry.get("ext_memory_search")
    assert registered is not None
    assert registered.spec.description == "External memory search tool"


@pytest.mark.asyncio
async def test_provider_memory_tools_skips_reserved_names():
    registry = ToolRegistry()
    create_memory_tools(
        stores={},
        retrievers={},
        memory_dir="/tmp",
        registry=registry,
    )
    assert "memory_search" in registry.list_names()

    pm = DummyMemoryProviderManager(
        schemas=[
            {"name": "memory_search", "description": "Colliding name"},
            {"name": "ext_memory_custom", "description": "Custom name"},
        ]
    )

    names = register_provider_memory_tools({"main": pm}, registry=registry)
    assert names == ["ext_memory_custom"]
    assert "memory_search" not in names


@pytest.mark.asyncio
async def test_provider_memory_tool_invocation_routes_by_agent_id():
    registry = ToolRegistry()
    pm_main = DummyMemoryProviderManager(
        schemas=[{"name": "custom_mem_tool", "description": "Main tool"}],
        responses={"custom_mem_tool": "main_response"},
    )
    pm_agent_b = DummyMemoryProviderManager(
        schemas=[{"name": "custom_mem_tool", "description": "Agent B tool"}],
        responses={"custom_mem_tool": "agent_b_response"},
    )

    provider_managers = {"main": pm_main, "agent_b": pm_agent_b}
    register_provider_memory_tools(provider_managers, registry=registry)

    rt = registry.get("custom_mem_tool")
    assert rt is not None

    token = current_tool_context.set(ToolContext(agent_id="main"))
    try:
        res = await rt.handler(key="value")
        assert res == "main_response"
        assert pm_main.calls == [("custom_mem_tool", {"key": "value"})]
    finally:
        current_tool_context.reset(token)

    token = current_tool_context.set(ToolContext(agent_id="agent_b"))
    try:
        res = await rt.handler(key="value2")
        assert res == "agent_b_response"
        assert pm_agent_b.calls == [("custom_mem_tool", {"key": "value2"})]
    finally:
        current_tool_context.reset(token)
