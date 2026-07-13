from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from agentos.engine import Agent, AgentConfig, ToolResult
from agentos.engine.types import ToolCall
from agentos.provider import (
    ChatConfig,
    Message,
    ToolDefinition,
    ToolInputSchema,
)
from agentos.provider import (
    DoneEvent as ProviderDone,
)
from agentos.provider import (
    TextDeltaEvent as ProviderText,
)
from agentos.provider import (
    ToolUseEndEvent as ProviderToolUseEnd,
)
from agentos.provider import (
    ToolUseStartEvent as ProviderToolUseStart,
)

pytestmark = pytest.mark.local_golden

CASES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "agent_chains" / "synthetic_cases"


class _SyntheticCaseProvider:
    provider_name = "synthetic"

    def __init__(self, turns: list[dict[str, Any]]) -> None:
        self._turns = turns
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
        turn = self._turns[call_number - 1]
        tool_calls = turn.get("tool_calls") or []
        for tool_call in tool_calls:
            yield ProviderToolUseStart(
                tool_use_id=tool_call["id"],
                tool_name=tool_call["name"],
            )
            yield ProviderToolUseEnd(
                tool_use_id=tool_call["id"],
                tool_name=tool_call["name"],
                arguments=tool_call["arguments"],
            )
        if tool_calls:
            yield ProviderDone(stop_reason="tool_use", input_tokens=10, output_tokens=2)
            return

        yield ProviderText(text=turn["final_text"])
        yield ProviderDone(stop_reason="stop", input_tokens=12, output_tokens=3)

    async def list_models(self) -> list[Any]:
        return []


def _tool_def(name: str, properties: dict[str, Any]) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"Synthetic {name}.",
        input_schema=ToolInputSchema(
            properties=properties,
            required=list(properties),
        ),
    )


def _case_paths() -> list[Path]:
    return sorted(CASES_DIR.glob("*.json"))


def _load_case(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _result_content(raw: Any) -> str:
    if isinstance(raw, str):
        return raw
    return json.dumps(raw, sort_keys=True)


def _message_contains_tool_use(messages: list[Message], tool_use_id: str) -> bool:
    return any(
        message.role == "assistant"
        and any(getattr(block, "id", "") == tool_use_id for block in message.content)
        for message in messages
    )


def _message_contains_tool_result(messages: list[Message], tool_use_id: str) -> bool:
    return any(
        message.role == "user"
        and any(getattr(block, "tool_use_id", "") == tool_use_id for block in message.content)
        for message in messages
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("case_path", _case_paths(), ids=lambda path: path.stem)
async def test_synthetic_complex_agent_golden(case_path: Path) -> None:
    if os.environ.get("AGENTOS_RUN_LOCAL_GOLDENS") != "1":
        pytest.skip("set AGENTOS_RUN_LOCAL_GOLDENS=1 to run synthetic local goldens")

    case = _load_case(case_path)
    provider = _SyntheticCaseProvider(case["turns"])
    handled: list[tuple[str, str, dict[str, Any], bool]] = []

    async def tool_handler(call: ToolCall) -> ToolResult:
        payload = case["tool_results"][call.tool_use_id]
        is_error = not bool(payload["ok"])
        handled.append((call.tool_use_id, call.tool_name, dict(call.arguments), is_error))
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content=_result_content(payload["content"]),
            is_error=is_error,
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=int(case["max_iterations"])),
        tool_definitions=[
            _tool_def(tool["name"], tool["properties"])
            for tool in case["tools"]
        ],
        tool_handler=tool_handler,
    )

    events = [event async for event in agent.run_turn(case["prompt"])]
    expect = case["expect"]

    assert [tool_name for _tool_id, tool_name, _args, _is_error in handled] == expect[
        "tool_order"
    ]
    assert [tool_id for tool_id, _tool_name, _args, _is_error in handled] == expect["tool_ids"]
    assert [
        tool_id for tool_id, _tool_name, _args, is_error in handled if is_error
    ] == expect["error_tool_ids"]
    assert len(provider.calls) == int(expect["iterations"])
    assert not any(event.kind == "error" for event in events)
    assert any(
        event.kind == "done" and event.text == expect["final_text"]
        for event in events
    )

    tool_ids_by_turn = [
        [tool_call["id"] for tool_call in turn.get("tool_calls") or []]
        for turn in case["turns"]
    ]
    for turn_index, tool_ids in enumerate(tool_ids_by_turn):
        if not tool_ids:
            continue
        replay_call = provider.calls[turn_index + 1]
        for tool_id in tool_ids:
            assert _message_contains_tool_use(replay_call, tool_id)
            assert _message_contains_tool_result(replay_call, tool_id)
