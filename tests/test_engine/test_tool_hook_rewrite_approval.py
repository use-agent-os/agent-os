"""A tool hook's rewrite through a real turn: Agent, batch executor, approval retry.

The hook runs again on the approval retry. The approval the tool granted is
bound to the rewritten command, so the retry must run that command with the
same ``approval_id`` -- even when the hook builds fresh arguments.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from agentos.engine import Agent, AgentConfig
from agentos.engine.hooks import ReplaceResult, Rewrite, ToolHookCall, ToolHookResult
from agentos.engine.types import ToolResultEvent
from agentos.provider import ChatConfig, Message, ToolDefinition, ToolInputSchema
from agentos.provider import DoneEvent as ProviderDone
from agentos.provider import TextDeltaEvent as ProviderTextDelta
from agentos.provider import ToolUseEndEvent as ProviderToolUseEnd
from agentos.provider import ToolUseStartEvent as ProviderToolUseStart
from agentos.tools.dispatch import build_tool_handler
from agentos.tools.registry import ToolRegistry
from agentos.tools.types import ToolContext, ToolSpec


class _OneToolCallProvider:
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
        return self._stream(len(self.calls))

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number > 1:
            yield ProviderTextDelta(text="done")
            yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)
            return
        yield ProviderToolUseStart(tool_use_id="tool-1", tool_name="deploy")
        yield ProviderToolUseEnd(
            tool_use_id="tool-1",
            tool_name="deploy",
            arguments={"command": "deploy prod --key sk-live"},
        )
        yield ProviderDone(stop_reason="tool_use", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


class _SafeDeployHook:
    """Rewrites every deploy to staging with fresh arguments, and redacts keys."""

    name = "safe-deploy"

    def __init__(self) -> None:
        self.before: list[dict[str, Any]] = []
        self.after: list[str] = []

    def before_tool(self, call: ToolHookCall) -> Any:
        self.before.append(dict(call.tool_call.arguments))
        command = str(call.tool_call.arguments["command"])
        return Rewrite({"command": command.replace("prod", "staging")})

    def after_tool(self, call: ToolHookCall, outcome: ToolHookResult) -> Any:
        text = str(getattr(outcome.result, "content", "") or "")
        self.after.append(text)
        if "sk-" in text:
            return ReplaceResult(text.replace("sk-", "sk-[redacted]-"))
        return None


@pytest.mark.asyncio
async def test_rewrite_survives_the_approval_retry_with_its_approval_id() -> None:
    ran: list[dict[str, Any]] = []
    approvals: dict[str, str] = {}

    async def deploy(command: str, approval_id: str | None = None) -> str:
        ran.append({"command": command, "approval_id": approval_id})
        if approval_id is None:
            approvals["ap-1"] = command
            return json.dumps(
                {"status": "approval_required", "approval_id": "ap-1", "command": command}
            )
        # The approval is bound to the command it was granted for.
        assert approvals[approval_id] == command
        return f"exit_code=0\ndeployed: {command}\n"

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="deploy",
            description="Deploy.",
            parameters={"command": {"type": "string"}, "approval_id": {"type": "string"}},
            required=["command"],
        ),
        deploy,
    )
    hook = _SafeDeployHook()
    provider = _OneToolCallProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=2),
        tool_definitions=[
            ToolDefinition(
                name="deploy",
                description="Deploy.",
                input_schema=ToolInputSchema(
                    properties={
                        "command": {"type": "string"},
                        "approval_id": {"type": "string"},
                    },
                    required=["command"],
                ),
            )
        ],
        tool_handler=build_tool_handler(registry, ToolContext(), tool_hooks=[hook]),
    )

    events = [event async for event in agent.run_turn("deploy it")]

    assert ran == [
        {"command": "deploy staging --key sk-live", "approval_id": None},
        {"command": "deploy staging --key sk-live", "approval_id": "ap-1"},
    ]
    # The retry re-dispatches the model's call plus the approval id; the hook
    # runs again on it.
    assert hook.before == [
        {"command": "deploy prod --key sk-live"},
        {"command": "deploy prod --key sk-live", "approval_id": "ap-1"},
    ]
    # The redacting after_tool saw the pending result as JSON and it was still
    # recognised as pending, so the batch waited and retried.
    assert json.loads(hook.after[0])["status"] == "approval_required"
    results = [event.result for event in events if isinstance(event, ToolResultEvent)]
    assert any(result.startswith("exit_code=0") for result in results)
    assert all("sk-live" not in result for result in results)
    assert len(provider.calls) == 2
