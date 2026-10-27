"""The loop guard acts on repeated no-progress tool calls inside a real turn."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from agentos.engine import Agent, AgentConfig, DoneEvent, ErrorEvent, SubagentSpec, ToolResult
from agentos.engine.outcome import outcome_from_error
from agentos.engine.runtime import TurnRunner
from agentos.engine.turn_runner.harness import _TurnRunnerAgentConfigBuilderAdapter
from agentos.gateway.config import ToolsConfig
from agentos.provider import ChatConfig, Message
from agentos.provider import DoneEvent as ProviderDone
from agentos.provider import TextDeltaEvent as ProviderText
from agentos.provider import ToolUseEndEvent as ProviderToolUseEnd
from agentos.provider import ToolUseStartEvent as ProviderToolUseStart

Step = list[tuple[str, dict[str, Any]]] | None


class _ScriptedProvider:
    """Replays one tool batch per provider call; ``None`` ends with text."""

    provider_name = "fake"

    def __init__(self, script: Callable[[int], Step]) -> None:
        self.script = script
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append([message.model_copy(deep=True) for message in messages])
        return self._stream(len(self.calls))

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        step = self.script(call_number)
        if step is None:
            yield ProviderText(text="done")
            yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)
            return
        for index, (name, arguments) in enumerate(step):
            tool_use_id = f"call-{call_number}-{index}"
            yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name=name)
            yield ProviderToolUseEnd(tool_use_id=tool_use_id, tool_name=name, arguments=arguments)
        yield ProviderDone(stop_reason="tool_calls", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []

    def tool_results(self, call_number: int) -> list[str]:
        """Every tool result text the model had in front of it on that call."""

        texts: list[str] = []
        for message in self.calls[call_number - 1]:
            if isinstance(message.content, str):
                continue
            for block in message.content:
                if getattr(block, "type", None) == "tool_result":
                    content = block.content
                    texts.append(content if isinstance(content, str) else str(content))
        return texts


class _Handler:
    def __init__(self, result: Callable[[Any], str] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._result = result or (lambda _call: "same contents")

    async def __call__(self, call: Any) -> ToolResult:
        self.calls.append((call.tool_name, dict(call.arguments)))
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content=self._result(call),
        )


_READ = ("read_file", {"path": "notes.md"})


def _agent(provider: _ScriptedProvider, handler: _Handler, **config: Any) -> Agent:
    config.setdefault("max_iterations", 20)
    return Agent(provider=provider, config=AgentConfig(**config), tool_handler=handler)


async def _run(agent: Agent, message: str = "summarize notes.md") -> list[Any]:
    return [event async for event in agent.run_turn(message)]


@pytest.mark.asyncio
async def test_warn_guidance_reaches_the_model_with_the_repeated_result() -> None:
    provider = _ScriptedProvider(lambda n: [_READ] if n <= 3 else None)
    handler = _Handler()

    events = await _run(_agent(provider, handler))

    assert any(isinstance(event, DoneEvent) for event in events)
    assert len(handler.calls) == 3
    # Calls 2 and 3 saw one and two plain results; call 4 sees the warning on
    # the third, attached to the result that triggered it.
    assert not any("[Loop check]" in text for text in provider.tool_results(3))
    last = provider.tool_results(4)
    assert last[-1].startswith("same contents")
    assert "[Loop check]" in last[-1]
    assert "read_file has now returned the same result 3 times" in last[-1]


@pytest.mark.asyncio
async def test_first_critical_blocks_the_batch_and_second_ends_the_turn() -> None:
    other = ("glob_search", {"pattern": "*.md"})
    # Four identical reads, then the same read with a second call in the
    # batch, then the same read again.
    provider = _ScriptedProvider(lambda n: [_READ] if n <= 4 or n == 6 else [_READ, other])
    handler = _Handler()

    events = await _run(_agent(provider, handler))

    # The fifth and sixth batches never reached the handler.
    assert len(handler.calls) == 4
    blocked = provider.tool_results(6)[-2:]
    assert "Not run: this exact call has stopped producing anything new" in blocked[0]
    assert "Do not repeat it unchanged" in blocked[0]
    assert "Not run: another call in this batch" in blocked[1]

    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert [error.code for error in errors] == ["loop_detected"]
    assert "Stopped this turn" in errors[0].message
    assert "read_file" in errors[0].message
    assert len(provider.calls) == 6
    assert outcome_from_error(code="loop_detected").kind == "partial"


@pytest.mark.asyncio
async def test_a_block_lets_the_model_change_course() -> None:
    def script(n: int) -> Step:
        if n <= 5:
            return [_READ]
        if n == 6:
            return [("read_file", {"path": "notes.md", "offset": 40})]
        return None

    provider = _ScriptedProvider(script)
    handler = _Handler()

    events = await _run(_agent(provider, handler))

    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert any(isinstance(event, DoneEvent) for event in events)
    # Four reads ran, the fifth was refused, the changed read ran.
    assert len(handler.calls) == 5
    assert handler.calls[-1][1] == {"path": "notes.md", "offset": 40}


@pytest.mark.asyncio
async def test_a_poll_with_changing_results_is_never_blocked() -> None:
    poll = ("process", {"action": "poll", "session_id": "bg-1"})
    provider = _ScriptedProvider(lambda n: [poll] if n <= 15 else None)
    counter = iter(range(1000))
    handler = _Handler(lambda _call: f'{{"output_chars": {next(counter)}}}')

    events = await _run(_agent(provider, handler))

    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert len(handler.calls) == 15
    assert not any("[Loop check]" in text for text in provider.tool_results(16))


@pytest.mark.asyncio
async def test_an_unchanged_poll_gets_a_larger_allowance() -> None:
    poll = ("process", {"action": "poll", "session_id": "bg-1"})
    provider = _ScriptedProvider(lambda n: [poll] if n <= 8 else None)
    handler = _Handler(lambda _call: '{"status": "running"}')

    events = await _run(_agent(provider, handler))

    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert len(handler.calls) == 8


@pytest.mark.asyncio
async def test_ping_pong_between_two_calls_is_warned_then_blocked() -> None:
    a = ("read_file", {"path": "a.py"})
    b = ("grep_search", {"pattern": "TODO"})
    provider = _ScriptedProvider(lambda n: [a] if n % 2 else [b])
    handler = _Handler(lambda call: f"output of {call.tool_name}")

    events = await _run(_agent(provider, handler))

    seen = provider.tool_results(7)
    assert "alternated between read_file and grep_search" in seen[-1]
    # Seven calls ran; the eighth would have stretched the alternation, so it
    # was answered without running, and the ninth (another repeat) ended it.
    assert len(handler.calls) == 7
    refused = provider.tool_results(9)[-1]
    assert "alternated between grep_search and read_file" in refused
    assert "Not run" in refused
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert [error.code for error in errors] == ["loop_detected"]
    assert len(provider.calls) == 9


@pytest.mark.asyncio
async def test_observe_mode_only_logs() -> None:
    provider = _ScriptedProvider(lambda n: [_READ] if n <= 7 else None)
    handler = _Handler()

    events = await _run(_agent(provider, handler, loop_guard="observe"))

    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert len(handler.calls) == 7
    assert not any("[Loop check]" in text for text in provider.tool_results(8))


@pytest.mark.asyncio
async def test_a_new_turn_starts_the_loop_guard_from_zero() -> None:
    def script(n: int) -> Step:
        # Turn one: reads until it is stopped (calls 1-6). Turn two: one read.
        if n <= 6:
            return [_READ]
        return [_READ] if n == 7 else None

    provider = _ScriptedProvider(script)
    handler = _Handler()
    agent = _agent(provider, handler)

    first = await _run(agent)
    second = await _run(agent, "read notes.md again please")

    assert any(isinstance(e, ErrorEvent) and e.code == "loop_detected" for e in first)
    assert not any(isinstance(e, ErrorEvent) for e in second)
    assert len(handler.calls) == 5


@pytest.mark.asyncio
async def test_the_failure_streak_resets_when_a_new_turn_starts() -> None:
    failing = ("exec_command", {"command": "make deploy"})

    async def _fail(call: Any) -> ToolResult:
        attempts.append(call.tool_use_id)
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="deploy failed: token expired",
            is_error=True,
        )

    attempts: list[str] = []
    # Turn one tries twice, then answers; turn two (after the user fixed the
    # token) tries once more.
    script = {1: [failing], 2: [failing], 3: None, 4: [failing], 5: None}
    provider = _ScriptedProvider(lambda n: script.get(n))
    agent = Agent(
        provider=provider,
        config=AgentConfig(tool_failure_loop_block_threshold=3, max_iterations=10),
        tool_handler=_fail,
    )

    await _run(agent, "deploy")
    await _run(agent, "I refreshed the token, deploy again")

    # Without the reset the third attempt would be refused unrun.
    assert len(attempts) == 3


def test_tools_loop_guard_config_reaches_agent_config() -> None:
    assert ToolsConfig().loop_guard == "enforce"
    with pytest.raises(ValidationError):
        ToolsConfig(loop_guard="off")
    for value in ("enforce", "observe"):
        runner = TurnRunner(
            provider_selector=None,
            config=SimpleNamespace(tools=ToolsConfig(loop_guard=value)),
        )
        aux = _TurnRunnerAgentConfigBuilderAdapter(runner).build_auxiliaries(
            agent_id="main",
            session_key="agent:main:webchat:t",
            session_id_for_log=None,
            turn=SimpleNamespace(metadata={}),
        )
        assert aux.loop_guard == value


def test_subagent_inherits_the_loop_guard_mode() -> None:
    parent = _agent(_ScriptedProvider(lambda _n: None), _Handler(), loop_guard="observe")
    child = parent._make_child_agent(SubagentSpec(task="t"), depth=1)

    assert child.config.loop_guard == "observe"
