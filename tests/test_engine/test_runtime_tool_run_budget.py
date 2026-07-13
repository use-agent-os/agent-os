from __future__ import annotations

import types

import pytest

from agentos.engine.runtime import TurnRunner
from agentos.tools.types import ToolContext


@pytest.mark.asyncio
async def test_runtime_assigns_fresh_tool_run_budget_key_per_turn() -> None:
    runner = TurnRunner(provider_selector=None, session_manager=None)
    source_context = ToolContext(session_key="agent:main:webchat:demo")
    captured: list[ToolContext] = []

    async def fake_run_turn(self, *args, **kwargs):
        captured.append(args[5])
        if False:
            yield None

    runner._run_turn = types.MethodType(fake_run_turn, runner)

    async for _ in runner.run(
        "first",
        session_key="agent:main:webchat:demo",
        tool_context=source_context,
    ):
        pass
    async for _ in runner.run(
        "second",
        session_key="agent:main:webchat:demo",
        tool_context=source_context,
    ):
        pass

    assert len(captured) == 2
    assert captured[0].session_key == "agent:main:webchat:demo"
    assert captured[1].session_key == "agent:main:webchat:demo"
    assert captured[0].tool_run_budget_key
    assert captured[1].tool_run_budget_key
    assert captured[0].tool_run_budget_key != captured[1].tool_run_budget_key
    assert source_context.tool_run_budget_key is None
