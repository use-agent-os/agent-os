"""What a turn records when a tool hook rewrote a call's arguments.

Dispatch reports the arguments a call ran with in
``ToolResult.effective_arguments``. The agent uses them for what describes
the work done (the verification ledger) and tells the model about the rewrite
in the tool result; the ``tool_use`` it stores stays exactly as the model
emitted it, so the transcript still replays. Guards that must decide before
dispatch (failure loop, per-argument timeout, concurrency key) stay on the
model's arguments; the tests below pin that they do so in the safe direction.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from agentos.engine import Agent, AgentConfig
from agentos.engine.agent import HOOK_ADJUSTED_ARGUMENTS_KEY
from agentos.engine.hooks import Rewrite, ToolHookCall, ToolHookResult
from agentos.engine.tool_batch import ToolBatchExecutor
from agentos.engine.types import ToolCall, ToolResult, ToolResultEvent, WarningEvent
from agentos.provider import (
    ChatConfig,
    ContentBlockToolResult,
    ContentBlockToolUse,
    Message,
    ToolDefinition,
    ToolInputSchema,
)
from agentos.provider import DoneEvent as ProviderDone
from agentos.provider import TextDeltaEvent as ProviderTextDelta
from agentos.provider import ToolUseEndEvent as ProviderToolUseEnd
from agentos.provider import ToolUseStartEvent as ProviderToolUseStart
from agentos.tools.dispatch import build_tool_handler
from agentos.tools.registry import ToolRegistry
from agentos.tools.types import ToolContext, ToolSpec

SECRET = "sk-injected-by-hook"


class _ScriptedProvider:
    """Emits the same tool call ``rounds`` times, then answers in text."""

    provider_name = "fake"

    def __init__(self, tool_name: str, arguments: dict[str, Any], *, rounds: int = 1) -> None:
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
        self.calls.append(list(messages))
        return self._stream(len(self.calls))

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number > self.rounds:
            yield ProviderTextDelta(text="done")
            yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)
            return
        tool_use_id = f"tool-{call_number}"
        yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name=self.tool_name)
        yield ProviderToolUseEnd(
            tool_use_id=tool_use_id,
            tool_name=self.tool_name,
            arguments=dict(self.arguments),
            thought_signature=f"sig-{call_number}",
        )
        yield ProviderDone(stop_reason="tool_use", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


class _RewriteHook:
    name = "rewrite"

    def __init__(self, rewrite: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        self._rewrite = rewrite
        self.seen: list[dict[str, Any]] = []

    def before_tool(self, call: ToolHookCall) -> Any:
        self.seen.append(dict(call.tool_call.arguments))
        return Rewrite(self._rewrite(dict(call.tool_call.arguments)))

    def after_tool(self, call: ToolHookCall, outcome: ToolHookResult) -> Any:
        return None


def _write_tool(
    handler: Callable[..., Any],
    *,
    name: str = "write_file",
    extra: dict[str, Any] | None = None,
    **definition: Any,
) -> tuple[ToolRegistry, ToolDefinition]:
    params: dict[str, Any] = {"path": {"type": "string"}, "token": {"type": "string"}}
    params.update(extra or {})
    registry = ToolRegistry()
    registry.register(
        ToolSpec(name=name, description="Write.", parameters=params, required=["path"]),
        handler,
    )
    tool_def = ToolDefinition(
        name=name,
        description="Write.",
        input_schema=ToolInputSchema(properties=params, required=["path"]),
        **definition,
    )
    return registry, tool_def


def _agent(
    provider: _ScriptedProvider,
    registry: ToolRegistry,
    tool_def: ToolDefinition,
    hook: Any,
    **config: Any,
) -> Agent:
    return Agent(
        provider=provider,
        config=AgentConfig(max_iterations=provider.rounds + 1, **config),
        tool_definitions=[tool_def],
        tool_handler=build_tool_handler(registry, ToolContext(), tool_hooks=[hook]),
    )


def _blocks(messages: list[Message], kind: type) -> list[Any]:
    return [
        block
        for message in messages
        if isinstance(message.content, list)
        for block in message.content
        if isinstance(block, kind)
    ]


@pytest.mark.asyncio
async def test_the_verification_ledger_records_the_path_the_hook_wrote() -> None:
    written: list[str] = []

    async def write_file(path: str, token: str | None = None) -> str:
        written.append(path)
        return f"wrote {path}"

    registry, tool_def = _write_tool(write_file)
    # The model edits notes; the hook redirects the write to source code.
    hook = _RewriteHook(lambda args: {**args, "path": "src/app.py"})
    provider = _ScriptedProvider("write_file", {"path": "notes.md"})
    agent = _agent(provider, registry, tool_def, hook)

    events = [event async for event in agent.run_turn("update the notes")]

    assert written == ["src/app.py"]
    warnings = [e for e in events if isinstance(e, WarningEvent)]
    # Code changed and nothing checked it: the nudge fires. Recording the
    # model's "notes.md" instead would have stayed silent.
    assert [w.code for w in warnings] == ["unverified_code_changes"]
    assert "src/app.py" in warnings[0].message


@pytest.mark.asyncio
async def test_the_stored_tool_use_is_the_models_and_the_result_names_the_rewrite() -> None:
    async def write_file(path: str, token: str | None = None) -> str:
        return f"wrote {path}"

    registry, tool_def = _write_tool(write_file)
    hook = _RewriteHook(lambda args: {**args, "path": "src/app.py", "token": SECRET})
    provider = _ScriptedProvider("write_file", {"path": "notes.md"})
    agent = _agent(provider, registry, tool_def, hook)

    events = [event async for event in agent.run_turn("update the notes")]

    replay = provider.calls[1]
    tool_use = _blocks(replay, ContentBlockToolUse)
    tool_result = _blocks(replay, ContentBlockToolResult)
    # The tool_use is replayed as the model emitted it, signature included,
    # and still pairs with its result.
    assert [(b.id, b.input, b.thought_signature) for b in tool_use] == [
        ("tool-1", {"path": "notes.md"}, "sig-1")
    ]
    assert [b.tool_use_id for b in tool_result] == ["tool-1"]
    content = str(tool_result[0].content)
    assert content.startswith("wrote src/app.py")
    assert "a tool hook adjusted the arguments of this call" in content
    assert "(adjusted: path, token)" in content
    # Only names: the value a hook put in never reaches the model.
    assert SECRET not in content
    delivered = [e for e in events if isinstance(e, ToolResultEvent)]
    assert all(SECRET not in e.result for e in delivered)
    assert all(SECRET not in json.dumps(m.model_dump(mode="json")) for m in replay)


@pytest.mark.asyncio
async def test_a_json_result_stays_json_with_the_rewrite_noted() -> None:
    async def write_file(path: str, token: str | None = None) -> str:
        return json.dumps({"status": "ok", "path": path})

    registry, tool_def = _write_tool(write_file)
    hook = _RewriteHook(lambda args: {**args, "path": "src/app.py"})
    provider = _ScriptedProvider("write_file", {"path": "notes.md"})
    agent = _agent(provider, registry, tool_def, hook)

    _ = [event async for event in agent.run_turn("update the notes")]

    (result,) = _blocks(provider.calls[1], ContentBlockToolResult)
    payload = json.loads(str(result.content))
    assert payload == {"status": "ok", "path": "src/app.py", HOOK_ADJUSTED_ARGUMENTS_KEY: ["path"]}


@pytest.mark.asyncio
async def test_a_rewrite_that_changes_nothing_adds_no_note() -> None:
    async def write_file(path: str, token: str | None = None) -> str:
        return f"wrote {path}"

    registry, tool_def = _write_tool(write_file)
    hook = _RewriteHook(lambda args: dict(args))
    provider = _ScriptedProvider("write_file", {"path": "notes.md"})
    agent = _agent(provider, registry, tool_def, hook)

    _ = [event async for event in agent.run_turn("update the notes")]

    (result,) = _blocks(provider.calls[1], ContentBlockToolResult)
    assert result.content == "wrote notes.md"


@pytest.mark.asyncio
async def test_the_failure_loop_still_stops_the_models_repeat_of_a_rewritten_call() -> None:
    ran: list[str] = []

    async def write_file(path: str, token: str | None = None) -> str:
        ran.append(path)
        raise RuntimeError("disk full")

    registry, tool_def = _write_tool(write_file)
    hook = _RewriteHook(lambda args: {**args, "path": "src/app.py"})
    provider = _ScriptedProvider("write_file", {"path": "notes.md"}, rounds=3)
    agent = _agent(provider, registry, tool_def, hook, tool_failure_loop_block_threshold=2)

    events = [event async for event in agent.run_turn("update the notes")]

    # Keyed on the model's call, which is what repeats: the second identical
    # call is refused before dispatch, so the tool ran once.
    assert ran == ["src/app.py"]
    results = [e.result for e in events if isinstance(e, ToolResultEvent)]
    assert any("has already failed repeatedly" in result for result in results)


@pytest.mark.asyncio
async def test_a_rewritten_timeout_is_bounded_by_the_models_ask() -> None:
    # The per-call timeout is computed before dispatch from the model's
    # arguments. A hook raising the timeout does not extend it: the call is
    # cut off at the bound the model asked for (the conservative direction).
    async def slow(path: str, token: str | None = None, timeout: float = 0) -> str:
        await asyncio.sleep(timeout)
        return "finished"

    registry, tool_def = _write_tool(
        slow,
        name="slow_tool",
        extra={"timeout": {"type": "number"}},
        execution_timeout_argument="timeout",
    )
    hook = _RewriteHook(lambda args: {**args, "timeout": 5})
    provider = _ScriptedProvider("slow_tool", {"path": "x", "timeout": 0.05})
    agent = _agent(provider, registry, tool_def, hook, tool_timeout=0.05)

    events = [event async for event in agent.run_turn("go")]

    (result,) = [e for e in events if isinstance(e, ToolResultEvent)]
    assert result.is_error
    assert "timed out" in result.result


def test_the_concurrency_key_is_derived_from_the_arguments_as_emitted() -> None:
    # Decided before dispatch, so a hook that retargets a keyed call does not
    # move it to another key. ``parallel`` and ``exclusive`` follow the tool's
    # name alone, which a hook cannot change.
    spec = ToolSpec(
        name="send",
        description="Send.",
        parameters={"session_key": {"type": "string"}},
        concurrency="keyed",
        concurrency_key=lambda args, _session: ("send", args.get("session_key")),
    )

    async def _never(_call: ToolCall) -> ToolResult:  # pragma: no cover - not run
        raise AssertionError

    executor = ToolBatchExecutor(
        execute=_never,
        project=_never,
        timeout_for=lambda _call: 1.0,
        spec_for=lambda name: spec if name == "send" else None,
        deadline=0.0,
        iteration_timeout=1.0,
    )
    call = ToolCall(tool_use_id="t", tool_name="send", arguments={"session_key": "a"})
    assert executor.policy_for(call).key == ("send", "a")
