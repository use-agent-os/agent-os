"""SessionManager.get_agent_config returns the AgentRegistry entry, not NotImplementedError."""

from __future__ import annotations

import pytest

from agentos.session.manager import SessionManager


class _StubRegistry:
    def __init__(self, entries: list[dict]) -> None:
        self._entries = entries

    async def list_agents(self, *, include_builtin: bool = True) -> list[dict]:
        return list(self._entries)


class _StubStorage:
    """Minimal storage stub — get_agent_config does not touch storage."""


@pytest.mark.asyncio
async def test_get_agent_config_returns_entry_for_known_agent() -> None:
    registry = _StubRegistry(
        [
            {"id": "main", "name": "Main"},
            {"id": "research", "name": "Research", "model": "haiku"},
        ]
    )
    mgr = SessionManager(_StubStorage(), agent_registry=registry)  # type: ignore[arg-type]
    entry = await mgr.get_agent_config("research")
    assert entry is not None
    assert entry.get("id") == "research"


@pytest.mark.asyncio
async def test_get_agent_config_returns_none_for_unknown_agent() -> None:
    registry = _StubRegistry([{"id": "main", "name": "Main"}])
    mgr = SessionManager(_StubStorage(), agent_registry=registry)  # type: ignore[arg-type]
    entry = await mgr.get_agent_config("ghost")
    assert entry is None


@pytest.mark.asyncio
async def test_get_agent_config_returns_none_when_registry_missing() -> None:
    mgr = SessionManager(_StubStorage())  # type: ignore[arg-type]
    entry = await mgr.get_agent_config("anything")
    assert entry is None
