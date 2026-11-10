"""A stopped turn is bounded like a completed one before it becomes history.

``_cap_turn_for_history`` caps a finished turn's tool payloads once, at the turn
boundary; later turns treat it as already capped. A turn ended by
``request_abort`` or a hard cancel skipped the cap (and the seal of its last
round), so a 30k-char argument and a 24k-char result stayed in history, and
were replayed on every later request, at full size.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

from agentos.engine import Agent, AgentConfig
from agentos.engine.agent import TURN_INTERRUPTED_NOTE
from agentos.engine.turn_progress import TurnStopSignal, bind_turn_stop_signal
from agentos.engine.types import DoneEvent
from agentos.provider import ContentBlockToolResult, ContentBlockToolUse, Message
from tests.fakes import FakeTool, FakeToolbox, ScriptedProvider, done, text, tool_call

_rng = random.Random(1)
# Varied lines, so the result cannot be squeezed by a repetition reducer.
BIG_RESULT = "\n".join(
    "".join(_rng.choice("abcdefghij klmnop") for _ in range(60)) for _ in range(500)
)
BIG_ARGUMENT = "y" * 30_000


def _payload_sizes(messages: list[Message]) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for message in messages:
        if not isinstance(message.content, list):
            continue
        for block in message.content:
            if isinstance(block, ContentBlockToolUse):
                sizes[f"use:{block.id}"] = sum(len(str(v)) for v in block.input.values())
            elif isinstance(block, ContentBlockToolResult):
                sizes[f"result:{block.tool_use_id}"] = len(str(block.content))
    return sizes


def _agent() -> tuple[Agent, FakeToolbox]:
    toolbox = FakeToolbox(
        FakeTool("big", result=BIG_RESULT), FakeTool("slow", delay=0.5, result="slow")
    )
    provider = ScriptedProvider(
        [
            [tool_call("big", {"q": BIG_ARGUMENT}, tool_use_id="B"), done("tool_use")],
            [tool_call("slow", tool_use_id="T"), done("tool_use")],
            [text("never"), done()],
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=8),
        session_key="agent:main:webchat:cap",
        **toolbox.agent_kwargs(),
    )
    return agent, toolbox


async def _wait_for_start(toolbox: FakeToolbox, tool_use_id: str) -> None:
    while "start" not in toolbox.events_for(tool_use_id):
        await asyncio.sleep(0.002)


def _assert_capped(agent: Agent) -> None:
    cap = agent._projection._history_tool_payload_cap()
    sizes = _payload_sizes(agent._history)
    assert sizes["use:B"] <= cap
    assert sizes["result:B"] <= cap
    # The running call is paired with a cancelled result.
    assert "result:T" in sizes
    assert agent._turn_record is not None
    recorded = _payload_sizes(agent._turn_record.messages)
    assert recorded["use:B"] <= cap
    assert recorded["result:B"] <= cap


async def test_an_aborted_turn_is_capped_for_history() -> None:
    agent, toolbox = _agent()
    signal = TurnStopSignal()
    events: list[Any] = []

    async def consume() -> None:
        bind_turn_stop_signal(signal)
        async for event in agent.run_turn("go"):
            events.append(event)

    task = asyncio.create_task(consume())
    await _wait_for_start(toolbox, "T")
    signal.request_abort("cancelled")
    await task

    assert isinstance(events[-1], DoneEvent)
    assert events[-1].outcome.reason == "cancelled"
    _assert_capped(agent)


async def test_a_hard_cancelled_turn_is_capped_for_history() -> None:
    agent, toolbox = _agent()

    async def consume() -> None:
        async for _ in agent.run_turn("go"):
            pass

    task = asyncio.create_task(consume())
    await _wait_for_start(toolbox, "T")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    _assert_capped(agent)


async def test_the_stopped_turn_still_ends_on_the_interrupted_note() -> None:
    agent, toolbox = _agent()

    async def consume() -> None:
        async for _ in agent.run_turn("go"):
            pass

    task = asyncio.create_task(consume())
    await _wait_for_start(toolbox, "T")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    last = agent._history[-1]
    assert last.role == "assistant"
    assert TURN_INTERRUPTED_NOTE in str(last.content)
