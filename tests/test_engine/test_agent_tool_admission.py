"""A turn sees resolved names and coerced arguments; rejections count as failures."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from typing import Any

from agentos.engine import Agent, AgentConfig, DoneEvent, ToolCall
from agentos.engine.progress_watchdog import (
    ProgressObservation,
    ProgressWatchdog,
    tool_call_signature,
)
from agentos.provider import ChatConfig, Message
from agentos.provider import DoneEvent as ProviderDone
from agentos.provider import TextDeltaEvent as ProviderText
from agentos.provider import ToolUseEndEvent as ProviderToolUseEnd
from agentos.provider import ToolUseStartEvent as ProviderToolUseStart
from agentos.tools.dispatch import build_tool_handler
from agentos.tools.registry import ToolRegistry
from agentos.tools.types import ToolContext, ToolSpec


class _ScriptedToolProvider:
    """Emits the same tool call ``rounds`` times, then answers in text."""

    provider_name = "fake"

    def __init__(self, *, tool_name: str, arguments: dict[str, Any], rounds: int) -> None:
        self.tool_name = tool_name
        self.arguments = arguments
        self.rounds = rounds
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        return self._stream(len(self.calls))

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number > self.rounds:
            yield ProviderText(text="done")
            yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)
            return
        tool_use_id = f"call-{call_number}"
        yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name=self.tool_name)
        yield ProviderToolUseEnd(
            tool_use_id=tool_use_id,
            tool_name=self.tool_name,
            arguments=dict(self.arguments),
        )
        yield ProviderDone(stop_reason="tool_calls", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


def _registry(calls: list[dict[str, Any]]) -> ToolRegistry:
    async def _count(**kwargs: Any) -> str:
        calls.append(kwargs)
        return f"counted {kwargs['n']}"

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="count_things",
            description="Count things.",
            parameters={"n": {"type": "integer"}},
            required=["n"],
        ),
        _count,
    )
    return registry


def _tool_results(events: list[Any]) -> list[Any]:
    return [event for event in events if getattr(event, "kind", None) == "tool_result"]


async def test_turn_runs_a_misnamed_call_with_stringified_arguments() -> None:
    calls: list[dict[str, Any]] = []
    provider = _ScriptedToolProvider(
        tool_name="functions.Count_Things:0", arguments={"n": "4"}, rounds=1
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=3),
        tool_handler=build_tool_handler(_registry(calls), ToolContext()),
    )

    events = [event async for event in agent.run_turn("count them")]

    assert calls == [{"n": 4}]
    assert any(isinstance(event, DoneEvent) for event in events)
    results = _tool_results(events)
    assert len(results) == 1
    assert getattr(results[0], "is_error", False) is False


async def test_repeated_rejections_reach_the_failure_loop_guard() -> None:
    calls: list[dict[str, Any]] = []
    provider = _ScriptedToolProvider(tool_name="count_things", arguments={"n": "many"}, rounds=3)
    agent = Agent(
        provider=provider,
        config=AgentConfig(tool_failure_loop_block_threshold=3, max_iterations=5),
        tool_handler=build_tool_handler(_registry(calls), ToolContext()),
    )

    events = [event async for event in agent.run_turn("count them")]

    reasons = [
        (getattr(event, "execution_status", None) or {}).get("reason")
        for event in _tool_results(events)
    ]
    assert calls == []
    assert reasons == ["invalid_arguments", "invalid_arguments", "tool_failure_loop_exhausted"]


async def test_rejected_call_is_recorded_like_any_failed_call() -> None:
    agent = Agent(
        provider=_ScriptedToolProvider(tool_name="count_things", arguments={}, rounds=0),
        config=AgentConfig(tool_failure_loop_block_threshold=0),
        tool_handler=build_tool_handler(_registry([]), ToolContext()),
    )
    watchdog = ProgressWatchdog(repeated_tool_error_threshold=2)
    call = ToolCall(tool_use_id="w1", tool_name="count_things", arguments={"n": "many"})

    decisions = []
    for iteration in (1, 2):
        result = await agent._execute_tool(call)
        assert result.is_error is True
        assert json.loads(result.content)["error_class"] == "ToolArgumentsRejected"
        decisions.append(
            watchdog.observe(
                ProgressObservation(
                    iteration=iteration,
                    tool_error_signature=f"{result.tool_name}:{result.content[:160]}",
                    tool_calls=(
                        tool_call_signature(
                            result.tool_name,
                            call.arguments,
                            result.content,
                            is_error=True,
                        ),
                    ),
                )
            )
        )

    assert agent._tool_failure_loop_counts[("count_things", _args_hash(call.arguments))] == 2
    assert [decision.reason for decision in decisions] == ["no_signal", "repeated_tool_error"]


def _args_hash(arguments: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


class _BatchProvider:
    """Emits one batch of tool calls, then answers in text."""

    provider_name = "fake"

    def __init__(self, calls: list[tuple[str, str, dict[str, Any]]]) -> None:
        self.batch = calls
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        return self._stream(len(self.calls))

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number > 1:
            yield ProviderText(text="done")
            yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)
            return
        for tool_use_id, name, arguments in self.batch:
            yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name=name)
            yield ProviderToolUseEnd(tool_use_id=tool_use_id, tool_name=name, arguments=arguments)
        yield ProviderDone(stop_reason="tool_calls", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


async def test_misnamed_calls_are_scheduled_and_approved_under_their_real_names(
    monkeypatch: Any,
) -> None:
    import asyncio

    import agentos.engine.agent as agent_module
    from agentos.engine.tool_batch import registry_spec_lookup
    from agentos.engine.types import ToolUseStartEvent

    active: set[str] = set()
    peak_reads = 0
    exec_runs: list[dict[str, Any]] = []

    async def _read(path: str) -> str:
        nonlocal peak_reads
        active.add(path)
        peak_reads = max(peak_reads, len(active))
        await asyncio.sleep(0.05)
        active.discard(path)
        return f"read {path}"

    async def _exec(command: str, approval_id: str | None = None) -> str:
        exec_runs.append({"command": command, "approval_id": approval_id, "others": set(active)})
        if approval_id is None:
            return json.dumps(
                {"status": "approval_required", "approval_id": "ap-1", "command": command}
            )
        return "exit_code=0"

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="read_file",
            description="Read.",
            parameters={"path": {"type": "string"}},
            required=["path"],
            concurrency="parallel",
            read_only=True,
        ),
        _read,
    )
    registry.register(
        ToolSpec(
            name="exec_command",
            description="Run.",
            parameters={"command": {"type": "string"}, "approval_id": {"type": "string"}},
            required=["command"],
            concurrency="exclusive",
            requires_approval=True,
            early_approval=True,
        ),
        _exec,
    )
    looked_up: list[str] = []

    def _spying_lookup(source: Any) -> Any:
        inner = registry_spec_lookup(source)

        def _lookup(name: str) -> Any:
            looked_up.append(name)
            return inner(name)

        return _lookup

    monkeypatch.setattr(agent_module, "registry_spec_lookup", _spying_lookup)
    provider = _BatchProvider(
        [
            ("r1", "functions.Read_File:0", {"path": "a"}),
            ("r2", "READ_FILE", {"path": "b"}),
            ("x1", "functions.Exec_Command", {"command": "ls"}),
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=3),
        tool_definitions=registry.to_tool_definitions(),
        tool_registry=registry,
        tool_handler=build_tool_handler(registry, ToolContext()),
    )

    events = [event async for event in agent.run_turn("look and run")]

    # Spec lookups (concurrency, early approval) only ever saw canonical names.
    assert looked_up
    assert set(looked_up) <= {"read_file", "exec_command"}
    # Parallel reads overlapped; the exclusive exec ran alone, after approval.
    assert peak_reads == 2
    assert exec_runs == [
        {"command": "ls", "approval_id": None, "others": set()},
        {"command": "ls", "approval_id": "ap-1", "others": set()},
    ]
    # Stream events and the persisted tool_use blocks carry the real names.
    starts = [event.tool_name for event in events if isinstance(event, ToolUseStartEvent)]
    assert starts == ["read_file", "read_file", "exec_command"]
    replayed = provider.calls[1]
    tool_uses = [
        (block.id, block.name)
        for message in replayed
        if not isinstance(message.content, str)
        for block in message.content
        if getattr(block, "type", None) == "tool_use"
    ]
    tool_results = [
        block.tool_use_id
        for message in replayed
        if not isinstance(message.content, str)
        for block in message.content
        if getattr(block, "type", None) == "tool_result"
    ]
    assert tool_uses == [("r1", "read_file"), ("r2", "read_file"), ("x1", "exec_command")]
    assert sorted(tool_results) == ["r1", "r2", "x1"]


async def test_unresolvable_names_reach_dispatch_unchanged() -> None:
    from agentos.engine.types import ToolUseStartEvent

    registry = _registry([])
    registry.register(ToolSpec(name="Count_Things", description="Twin.", parameters={}), _noop)
    provider = _BatchProvider([("u1", "COUNT_THINGS", {"n": 1})])
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=2),
        tool_definitions=registry.to_tool_definitions(),
        tool_registry=registry,
        tool_handler=build_tool_handler(registry, ToolContext()),
    )

    events = [event async for event in agent.run_turn("count")]

    starts = [event.tool_name for event in events if isinstance(event, ToolUseStartEvent)]
    assert starts == ["COUNT_THINGS"]
    results = _tool_results(events)
    assert json.loads(results[0].result)["error_class"] == "ToolNotFound"


async def _noop(**_kwargs: Any) -> str:
    return "noop"
