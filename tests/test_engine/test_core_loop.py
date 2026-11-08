"""``agentos.engine.core.run_agent_loop``: step order and stop rules (#3817)."""

from __future__ import annotations

import ast
import inspect
import textwrap
from collections.abc import AsyncGenerator

import pytest

from agentos.engine.agent import Agent
from agentos.engine.core.loop import LoopConfig, LoopState, run_agent_loop
from agentos.engine.types import AgentEvent, TextDeltaEvent, ToolCall

#: ``Agent._turn_generator`` only prepares, runs and settles the turn; the
#: loop's work lives in ``engine/core`` and the ``_turn_*`` steps.
TURN_GENERATOR_MAX_LINES = 300


class _State(LoopState):
    def __init__(self, *, rounds_with_tools: int, steer_once: bool = False) -> None:
        super().__init__()
        self.trace: list[str] = []
        self.rounds_with_tools = rounds_with_tools
        self.steer_once = steer_once


def _step(name: str):  # noqa: ANN202 - test helper returning a Step
    async def run(state: _State) -> AsyncGenerator[AgentEvent, None]:
        state.trace.append(f"{name}:{state.iterations}")
        if name == "before_request":
            state.iterations += 1
        if name == "commit_response":
            state.tool_calls = (
                [ToolCall(tool_use_id="t", tool_name="read", arguments={})]
                if state.iterations <= state.rounds_with_tools
                else []
            )
        if name == "on_final_answer" and state.steer_once:
            state.steer_once = False
            state.next_iteration = True
        yield TextDeltaEvent(text=name)

    return run


def _config(**overrides) -> LoopConfig[_State]:  # noqa: ANN003
    steps = {
        name: _step(name)
        for name in (
            "before_request",
            "stream",
            "commit_response",
            "on_final_answer",
            "execute_tools",
            "after_tools",
        )
    }
    steps.update(overrides)
    return LoopConfig(**steps)


async def _collect(state: _State, config: LoopConfig[_State]) -> list[str]:
    return [e.text async for e in run_agent_loop(state, config) if isinstance(e, TextDeltaEvent)]


@pytest.mark.asyncio
async def test_tool_rounds_repeat_until_a_response_calls_no_tools() -> None:
    state = _State(rounds_with_tools=1)
    events = await _collect(state, _config())

    assert events == [
        "before_request",
        "stream",
        "commit_response",
        "execute_tools",
        "after_tools",
        "before_request",
        "stream",
        "commit_response",
        "on_final_answer",
    ]
    assert state.iterations == 2


@pytest.mark.asyncio
async def test_a_final_answer_can_ask_for_another_iteration() -> None:
    state = _State(rounds_with_tools=0, steer_once=True)
    await _collect(state, _config())

    assert [s for s in state.trace if s.startswith("on_final_answer")] == [
        "on_final_answer:1",
        "on_final_answer:2",
    ]


@pytest.mark.asyncio
async def test_a_step_that_sets_stop_ends_the_loop_after_it() -> None:
    async def failing_stream(state: _State) -> AsyncGenerator[AgentEvent, None]:
        state.stop = True
        yield TextDeltaEvent(text="error")

    state = _State(rounds_with_tools=5)
    events = await _collect(state, _config(stream=failing_stream))

    assert events == ["before_request", "error"]


@pytest.mark.asyncio
async def test_closing_the_loop_closes_the_running_step_at_once() -> None:
    closed: list[str] = []

    async def slow_tools(state: _State) -> AsyncGenerator[AgentEvent, None]:
        try:
            yield TextDeltaEvent(text="tool running")
            yield TextDeltaEvent(text="never")
        finally:
            closed.append("execute_tools")

    loop = run_agent_loop(_State(rounds_with_tools=1), _config(execute_tools=slow_tools))
    async for event in loop:
        if isinstance(event, TextDeltaEvent) and event.text == "tool running":
            break
    await loop.aclose()

    assert closed == ["execute_tools"]


def test_turn_generator_stays_a_thin_driver() -> None:
    source = textwrap.dedent(inspect.getsource(Agent._turn_generator))
    node = ast.parse(source).body[0]
    assert isinstance(node, ast.AsyncFunctionDef)
    length = (node.end_lineno or node.lineno) - node.lineno + 1

    assert length < TURN_GENERATOR_MAX_LINES, (
        f"Agent._turn_generator is {length} lines; move the work into a "
        f"_turn_* step or engine/core instead (limit {TURN_GENERATOR_MAX_LINES})."
    )
    called = {
        n.func.id
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "run_agent_loop" in called
