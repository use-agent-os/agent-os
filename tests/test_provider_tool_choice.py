"""Forced tool_choice must be translated for Anthropic and OpenAI Responses.

The LLM router judge pins ``tool_choice`` to force its ``emit_route`` tool.
The OpenAI Chat-Completions provider forwards the nested forced-tool dict
verbatim; Anthropic and the OpenAI Responses API each use a different native
shape and must translate the OpenAI-style forced-tool dict.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from agentos.provider.anthropic import AnthropicProvider, _build_tool_choice_payload
from agentos.provider.openai_responses import (
    OpenAIResponsesProvider,
)
from agentos.provider.openai_responses import (
    _build_tool_choice_payload as _responses_tool_choice,
)
from agentos.provider.types import ChatConfig, Message, ToolDefinition, ToolInputSchema

# The forced-tool dict the LLM router judge pins on ChatConfig.tool_choice to
# guarantee a structured ``emit_route`` tool call.
_JUDGE_FORCED_TOOL_CHOICE = {"type": "function", "function": {"name": "emit_route"}}


def _capture_payload(monkeypatch: Any, module: str, response: httpx.Response) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = (
            json.loads(request.content.decode("utf-8")) if request.content else None
        )
        return response

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(f"{module}.httpx.AsyncClient", patched_async_client)
    return captured


_EMIT_ROUTE_TOOL = ToolDefinition(
    name="emit_route",
    description="Emit the routing decision.",
    input_schema=ToolInputSchema(
        properties={"route_class": {"type": "string"}}, required=["route_class"]
    ),
)


def test_openai_style_forced_tool_translates_to_anthropic_tool() -> None:
    payload = _build_tool_choice_payload(
        {"type": "function", "function": {"name": "emit_route"}}
    )
    assert payload == {"type": "tool", "name": "emit_route"}


def test_anthropic_native_tool_choice_passthrough() -> None:
    assert _build_tool_choice_payload({"type": "tool", "name": "x"}) == {
        "type": "tool",
        "name": "x",
    }
    assert _build_tool_choice_payload({"type": "any"}) == {"type": "any"}
    assert _build_tool_choice_payload({"type": "auto"}) == {"type": "auto"}


def test_string_tool_choice_maps() -> None:
    assert _build_tool_choice_payload("auto") == {"type": "auto"}
    assert _build_tool_choice_payload("any") == {"type": "any"}
    assert _build_tool_choice_payload("none") == {"type": "none"}
    # Unknown string degrades to auto rather than sending an invalid value.
    assert _build_tool_choice_payload("bogus") == {"type": "auto"}


def test_none_and_malformed_return_none_or_safe_default() -> None:
    assert _build_tool_choice_payload(None) is None
    assert _build_tool_choice_payload(123) is None
    # A forced-function dict missing a name falls back to "any" (force some tool).
    assert _build_tool_choice_payload({"type": "function", "function": {}}) == {"type": "any"}


def test_responses_flattens_nested_forced_tool() -> None:
    # The judge pins the nested Chat-Completions shape; the Responses API needs
    # the flat forced-function form ``{"type": "function", "name": ...}``.
    assert _responses_tool_choice(
        {"type": "function", "function": {"name": "emit_route"}}
    ) == {"type": "function", "name": "emit_route"}


def test_responses_flat_forced_tool_passthrough() -> None:
    assert _responses_tool_choice({"type": "function", "name": "emit_route"}) == {
        "type": "function",
        "name": "emit_route",
    }


def test_responses_string_and_none_defaults() -> None:
    assert _responses_tool_choice("auto") == "auto"
    assert _responses_tool_choice("required") == "required"
    assert _responses_tool_choice(None) == "auto"
    assert _responses_tool_choice(123) == "auto"


def test_responses_forced_tool_missing_name_degrades_to_auto() -> None:
    assert _responses_tool_choice({"type": "function", "function": {}}) == "auto"


# --- Behavior-level wiring: the translated tool_choice must reach the request ---
# These drive the real providers (not just the translator helper) so that deleting
# the tool_choice wiring in anthropic.py / openai_responses.py fails a test instead
# of silently regressing the judge to no forced tool call.


def test_anthropic_forwards_translated_forced_tool_choice_in_payload(
    monkeypatch: Any,
) -> None:
    captured = _capture_payload(
        monkeypatch,
        "agentos.provider.anthropic",
        httpx.Response(200, text=""),
    )
    provider = AnthropicProvider(api_key="test", model="claude-test")

    async def _run() -> None:
        async for _ in provider.chat(
            [Message(role="user", content="classify")],
            config=ChatConfig(tool_choice=_JUDGE_FORCED_TOOL_CHOICE, max_tokens=16),
            tools=[_EMIT_ROUTE_TOOL],
        ):
            pass

    asyncio.run(_run())

    assert captured["payload"]["tool_choice"] == {"type": "tool", "name": "emit_route"}


def test_openai_responses_forwards_translated_forced_tool_choice_in_payload(
    monkeypatch: Any,
) -> None:
    captured = _capture_payload(
        monkeypatch,
        "agentos.provider.openai_responses",
        httpx.Response(
            200,
            json={
                "id": "resp_test",
                "model": "gpt-5.4",
                "output": [],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        ),
    )
    provider = OpenAIResponsesProvider(api_key="test", model="gpt-5.4")

    async def _run() -> None:
        async for _ in provider.chat(
            [Message(role="user", content="classify")],
            config=ChatConfig(tool_choice=_JUDGE_FORCED_TOOL_CHOICE, max_tokens=16),
            tools=[_EMIT_ROUTE_TOOL],
        ):
            pass

    asyncio.run(_run())

    assert captured["payload"]["tool_choice"] == {"type": "function", "name": "emit_route"}


# --- forced-tool presence guard (agent.py) --------------------------------
# Now that the direct Anthropic provider honors cfg.tool_choice, forcing a tool
# absent from the request's tool list is a hard 400 on Anthropic. The agent
# guards this by only applying a forced tool_choice when its target tool is
# actually present in the tools passed to the provider.


def test_forced_tool_name_extracts_openai_and_anthropic_shapes() -> None:
    from agentos.engine.agent import _forced_tool_name

    assert (
        _forced_tool_name({"type": "function", "function": {"name": "memory_search"}})
        == "memory_search"
    )
    assert _forced_tool_name({"type": "tool", "name": "memory_search"}) == "memory_search"
    # Non-specific / malformed choices target no tool.
    assert _forced_tool_name({"type": "auto"}) is None
    assert _forced_tool_name({"type": "any"}) is None
    assert _forced_tool_name({"type": "function", "function": {}}) is None
    assert _forced_tool_name("auto") is None
    assert _forced_tool_name(None) is None


def test_forced_tool_present_true_when_target_in_tools() -> None:
    from agentos.engine.agent import _forced_tool_present

    meta = ToolDefinition(
        name="memory_search",
        description="",
        input_schema=ToolInputSchema(properties={}, required=[]),
    )
    assert _forced_tool_present(
        {"type": "function", "function": {"name": "memory_search"}}, [meta]
    )
    assert _forced_tool_present({"type": "tool", "name": "memory_search"}, [meta])


def test_forced_tool_present_false_when_target_absent() -> None:
    from agentos.engine.agent import _forced_tool_present

    # The forced tool was filtered out of this call — applying the forced choice
    # would 400 on Anthropic, so the guard reports absence and the caller keeps
    # tool_choice at its default rather than sending it.
    assert not _forced_tool_present(
        {"type": "function", "function": {"name": "memory_search"}}, [_EMIT_ROUTE_TOOL]
    )
    assert not _forced_tool_present(
        {"type": "function", "function": {"name": "memory_search"}}, []
    )
    assert not _forced_tool_present(
        {"type": "function", "function": {"name": "memory_search"}}, None
    )


def test_forced_tool_present_passes_non_specific_choices() -> None:
    from agentos.engine.agent import _forced_tool_present

    # A choice that targets no specific tool never needs a presence check.
    assert _forced_tool_present({"type": "auto"}, [])
    assert _forced_tool_present({"type": "any"}, None)
