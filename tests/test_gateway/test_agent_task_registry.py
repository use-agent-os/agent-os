"""Task registry cleanup must not remove a newer task for the same session."""

from __future__ import annotations

import asyncio

from agentos.gateway.agent_tasks import AgentTaskRegistry


async def test_cancelled_predecessor_callback_keeps_replacement_registered() -> None:
    registry = AgentTaskRegistry()
    blocker = asyncio.Event()
    predecessor = asyncio.create_task(blocker.wait())
    registry.register("agent:main:ws:dm:peer", predecessor)

    replacement = asyncio.create_task(blocker.wait())
    registry.register("agent:main:ws:dm:peer", replacement)
    await asyncio.gather(predecessor, return_exceptions=True)
    await asyncio.sleep(0)

    assert replacement.done() is False
    assert registry.get("agent:main:ws:dm:peer") is replacement
    assert registry.is_running("agent:main:ws:dm:peer") is True

    replacement.cancel()
    await asyncio.gather(replacement, return_exceptions=True)
    await asyncio.sleep(0)

    assert registry.get("agent:main:ws:dm:peer") is None
    assert registry.is_running("agent:main:ws:dm:peer") is False
