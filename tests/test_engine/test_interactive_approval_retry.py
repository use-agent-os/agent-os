from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from agentos.engine import Agent, AgentConfig, ToolResult
from agentos.engine.types import ToolCall, ToolResultEvent
from agentos.gateway.approval_queue import get_approval_queue, reset_approval_queue
from agentos.provider import ChatConfig, Message, ToolDefinition, ToolInputSchema
from agentos.provider import DoneEvent as ProviderDone
from agentos.provider import TextDeltaEvent as ProviderTextDelta
from agentos.provider import ToolUseEndEvent as ProviderToolUseEnd
from agentos.provider import ToolUseStartEvent as ProviderToolUseStart


class _OneApprovalToolProvider:
    provider_name = "fake"

    def __init__(self) -> None:
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        call_number = len(self.calls)
        return self._stream(call_number)

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number > 1:
            yield ProviderTextDelta(text="done")
            yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)
            return
        yield ProviderToolUseStart(tool_use_id="tool-1", tool_name="exec_command")
        yield ProviderToolUseEnd(
            tool_use_id="tool-1",
            tool_name="exec_command",
            arguments={"command": "pip install demo"},
        )
        yield ProviderDone(stop_reason="tool_use", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


def _exec_definition() -> ToolDefinition:
    return ToolDefinition(
        name="exec_command",
        description="Execute command.",
        input_schema=ToolInputSchema(
            properties={
                "command": {"type": "string"},
                "approval_id": {"type": "string"},
            },
            required=["command"],
        ),
    )


@pytest.mark.asyncio
async def test_interactive_approval_result_is_waited_and_retried_before_model_continues() -> None:
    approval_prompt_seen = asyncio.Event()
    allow_retry = asyncio.Event()
    tool_calls: list[dict[str, Any]] = []

    async def _handler(call: ToolCall) -> ToolResult:
        tool_calls.append(dict(call.arguments))
        approval_id = call.arguments.get("approval_id")
        if approval_id is None:
            return ToolResult(
                tool_use_id=call.tool_use_id,
                tool_name=call.tool_name,
                content=json.dumps(
                    {
                        "status": "approval_required",
                        "approval_id": "approve-1",
                        "command": call.arguments["command"],
                        "warning": "command requires approval",
                    }
                ),
            )
        assert approval_id == "approve-1"
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="exit_code=0\ninstalled\n",
        )

    provider = _OneApprovalToolProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=2),
        tool_definitions=[_exec_definition()],
        tool_handler=_handler,
    )

    events: list[Any] = []

    async def _drive() -> None:
        async for event in agent.run_turn("install package"):
            events.append(event)
            if isinstance(event, ToolResultEvent) and "approval_required" in event.result:
                approval_prompt_seen.set()
                await allow_retry.wait()

    task = asyncio.create_task(_drive())
    await asyncio.wait_for(approval_prompt_seen.wait(), timeout=2.0)
    assert len(provider.calls) == 1
    assert tool_calls == [{"command": "pip install demo"}]

    allow_retry.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert tool_calls == [
        {"command": "pip install demo"},
        {"command": "pip install demo", "approval_id": "approve-1"},
    ]
    assert len(provider.calls) == 2
    assert any(
        isinstance(event, ToolResultEvent) and event.result.startswith("exit_code=0")
        for event in events
    )
    second_provider_request = provider.calls[1]
    tool_result_messages = [
        msg
        for msg in second_provider_request
        if any(getattr(block, "type", None) == "tool_result" for block in msg.content)
    ]
    assert len(tool_result_messages) == 1
    block = next(
        block
        for block in tool_result_messages[0].content
        if getattr(block, "type", None) == "tool_result"
    )
    assert block.content == "exit_code=0\ninstalled\n"
    assert "approval_required" not in block.content


@pytest.mark.asyncio
async def test_agent_waits_for_approval_resolution_before_retry_result_reaches_model() -> None:
    reset_approval_queue()
    approval_prompt_seen = asyncio.Event()
    tool_calls: list[dict[str, Any]] = []

    async def _handler(call: ToolCall) -> ToolResult:
        tool_calls.append(dict(call.arguments))
        approval_id = call.arguments.get("approval_id")
        if approval_id is None:
            approval_id = get_approval_queue().request(
                "exec",
                {
                    "toolName": call.tool_name,
                    "command": call.arguments["command"],
                    "args": dict(call.arguments),
                },
            )
            return ToolResult(
                tool_use_id=call.tool_use_id,
                tool_name=call.tool_name,
                content=json.dumps(
                    {
                        "status": "approval_required",
                        "approval_id": approval_id,
                        "command": call.arguments["command"],
                        "warning": "command requires approval",
                    }
                ),
            )

        entry = get_approval_queue().get(str(approval_id))
        if not entry.resolved:
            return ToolResult(
                tool_use_id=call.tool_use_id,
                tool_name=call.tool_name,
                content=json.dumps(
                    {
                        "status": "approval_pending",
                        "approval_id": approval_id,
                        "command": call.arguments["command"],
                        "warning": "command requires approval",
                    }
                ),
            )
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="exit_code=0\napproved\n",
        )

    provider = _OneApprovalToolProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=2,
            metadata={"approval_wait_timeout_seconds": 1.0},
        ),
        tool_definitions=[_exec_definition()],
        tool_handler=_handler,
    )

    events: list[Any] = []

    async def _drive() -> None:
        async for event in agent.run_turn("install package"):
            events.append(event)
            if isinstance(event, ToolResultEvent) and "approval_required" in event.result:
                approval_prompt_seen.set()

    try:
        task = asyncio.create_task(_drive())
        await asyncio.wait_for(approval_prompt_seen.wait(), timeout=2.0)
        await asyncio.sleep(0.05)

        assert len(provider.calls) == 1
        assert len(tool_calls) == 1

        approval_event = next(
            event
            for event in events
            if isinstance(event, ToolResultEvent) and "approval_required" in event.result
        )
        approval_id = json.loads(approval_event.result)["approval_id"]
        get_approval_queue().resolve(approval_id, True)
        await asyncio.wait_for(task, timeout=2.0)

        assert len(tool_calls) == 2
        second_provider_request = provider.calls[1]
        tool_result_blocks = [
            block
            for msg in second_provider_request
            for block in msg.content
            if getattr(block, "type", None) == "tool_result"
        ]
        assert [block.content for block in tool_result_blocks] == [
            "exit_code=0\napproved\n"
        ]
        assert all(
            "approval_pending" not in event.result
            for event in events
            if isinstance(event, ToolResultEvent)
        )
    finally:
        reset_approval_queue()
