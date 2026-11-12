"""Direct OpenAI gpt-5.4+ tool turns with thinking go to /v1/responses.

/v1/chat/completions refuses function tools with any ``reasoning_effort`` but
``none`` for these models (live API, 2026-11-12), so a tool turn that asks for
thinking is sent as a Responses request: reasoning config, encrypted reasoning
items replayed across tool calls (``store: false``), streamed summaries as
thinking, and usage with reasoning / cached tokens.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from agentos.engine.types import ThinkingLevel
from agentos.provider.openai import OpenAIProvider
from agentos.provider.openai_responses import (
    RESPONSES_REASONING_FORMAT,
    OpenAIResponsesProvider,
    responses_payload_from_chat,
)
from agentos.provider.replay import provenance_for_provider
from agentos.provider.types import (
    ChatConfig,
    ContentBlockImage,
    ContentBlockText,
    ContentBlockToolResult,
    ContentBlockToolUse,
    DoneEvent,
    ErrorEvent,
    Message,
    MessageProvenance,
    ModelCapabilities,
    ProviderHeartbeatEvent,
    StopReason,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolDefinition,
    ToolInputSchema,
    ToolUseDeltaEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
)

_CAPS = ModelCapabilities(supports_reasoning=True, supports_tools=True, reasoning_format="openai")
_ENCRYPTED = "gAAAAB-opaque-encrypted-reasoning"


def _tool() -> ToolDefinition:
    return ToolDefinition(
        name="get_weather",
        description="Get the weather.",
        input_schema=ToolInputSchema(
            properties={"city": {"type": "string"}, "unit": {"type": "string"}},
            required=["city"],
        ),
    )


def _sse(events: list[dict[str, Any]]) -> bytes:
    return b"".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode() for event in events
    )


def _tool_call_stream(model: str = "gpt-5.6-terra") -> list[dict[str, Any]]:
    """The event sequence the live API streamed for a reasoning + tool turn."""
    reasoning_item = {
        "id": "rs_1",
        "type": "reasoning",
        "content": [],
        "encrypted_content": _ENCRYPTED,
        "summary": [{"type": "summary_text", "text": "**Plan**\n\nLook it up."}],
    }
    call_item = {
        "id": "fc_1",
        "type": "function_call",
        "status": "completed",
        "arguments": '{"city":"Hanoi"}',
        "call_id": "call_abc",
        "name": "get_weather",
    }
    return [
        {"type": "response.created", "response": {"status": "in_progress"}},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {**reasoning_item, "summary": []},
        },
        {"type": "response.reasoning_summary_part.added", "item_id": "rs_1"},
        {"type": "response.reasoning_summary_text.delta", "item_id": "rs_1", "delta": "**Plan**"},
        {"type": "response.reasoning_summary_part.added", "item_id": "rs_1"},
        {
            "type": "response.reasoning_summary_text.delta",
            "item_id": "rs_1",
            "delta": "Look it up.",
        },
        {"type": "response.output_item.done", "output_index": 0, "item": reasoning_item},
        {
            "type": "response.output_item.added",
            "output_index": 1,
            "item": {**call_item, "status": "in_progress", "arguments": ""},
        },
        {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": '{"city":'},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": '"Hanoi"}'},
        {"type": "response.output_item.done", "output_index": 1, "item": call_item},
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "model": model,
                "usage": {
                    "input_tokens": 94,
                    "input_tokens_details": {"cached_tokens": 40, "cache_write_tokens": 3},
                    "output_tokens": 88,
                    "output_tokens_details": {"reasoning_tokens": 36},
                },
            },
        },
    ]


def _patch(
    monkeypatch: Any,
    captured: list[dict[str, Any]],
    body: bytes,
    *,
    content_type: str = "text/event-stream",
    status: int = 200,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            {"url": str(request.url), "payload": json.loads(request.content.decode("utf-8"))}
        )
        return httpx.Response(status, headers={"content-type": content_type}, content=body)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("agentos.provider.openai.httpx.AsyncClient", patched)


def _provider(model: str = "gpt-5.6-terra") -> OpenAIProvider:
    return OpenAIProvider(
        api_key="test", model=model, base_url="https://api.openai.com/v1", provider_kind="openai"
    )


def _run(
    provider: OpenAIProvider,
    cfg: ChatConfig,
    *,
    messages: list[Message] | None = None,
    tools: list[ToolDefinition] | None = None,
) -> list[Any]:
    async def _go() -> list[Any]:
        return [
            event
            async for event in provider.chat(
                messages or [Message(role="user", content="weather in Hanoi?")],
                tools=[_tool()] if tools is None else tools,
                config=cfg,
            )
        ]

    return asyncio.run(_go())


def _thinking_cfg(**overrides: Any) -> ChatConfig:
    values: dict[str, Any] = {
        "system": "You are helpful.",
        "max_tokens": 4000,
        "thinking": True,
        "thinking_level": ThinkingLevel.MEDIUM,
        "model_capabilities": _CAPS,
    }
    values.update(overrides)
    return ChatConfig(**values)


@pytest.mark.parametrize(
    "model", ["gpt-5.4", "gpt-5.5", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-6-luna"]
)
def test_thinking_tool_turn_posts_a_responses_request(monkeypatch: Any, model: str) -> None:
    captured: list[dict[str, Any]] = []
    _patch(monkeypatch, captured, _sse(_tool_call_stream(model)))

    _run(
        _provider(model),
        _thinking_cfg(temperature=0.3, stop_sequences=["END"], cache_mode="auto"),
    )

    assert captured[0]["url"] == "https://api.openai.com/v1/responses"
    payload = captured[0]["payload"]
    assert payload["model"] == model
    assert payload["reasoning"] == {"effort": "medium", "summary": "auto"}
    assert payload["include"] == ["reasoning.encrypted_content"]
    assert payload["store"] is False
    assert payload["stream"] is True
    assert payload["instructions"] == "You are helpful."
    assert payload["input"] == [{"role": "user", "content": "weather in Hanoi?"}]
    assert payload["max_output_tokens"] == 4000
    # Refused by /v1/responses for these models: unknown / unsupported.
    assert "stop" not in payload
    assert "temperature" not in payload
    assert "messages" not in payload
    assert "reasoning_effort" not in payload
    # strict defaults to true on Responses and would make `unit` required.
    assert payload["tools"] == [
        {
            "type": "function",
            "name": "get_weather",
            "description": "Get the weather.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}, "unit": {"type": "string"}},
                "required": ["city"],
            },
            "strict": False,
        }
    ]
    assert payload["prompt_cache_key"].startswith("agentos-")


def test_thinking_tool_turn_maps_stream_events_and_usage(monkeypatch: Any) -> None:
    captured: list[dict[str, Any]] = []
    _patch(monkeypatch, captured, _sse(_tool_call_stream()))

    events = _run(_provider(), _thinking_cfg())

    thinking = "".join(e.text for e in events if isinstance(e, ThinkingDeltaEvent))
    assert thinking == "**Plan**\n\nLook it up."
    # The tool-stream guard adds heartbeats while a call's arguments stream.
    skip = (ThinkingDeltaEvent, ProviderHeartbeatEvent)
    assert [type(e) for e in events if not isinstance(e, skip)] == [
        ToolUseStartEvent,
        ToolUseDeltaEvent,
        ToolUseDeltaEvent,
        ToolUseEndEvent,
        DoneEvent,
    ]
    end = next(e for e in events if isinstance(e, ToolUseEndEvent))
    assert (end.tool_use_id, end.tool_name, end.arguments) == (
        "call_abc",
        "get_weather",
        {"city": "Hanoi"},
    )
    done = events[-1]
    assert isinstance(done, DoneEvent)
    assert done.stop_reason is StopReason.TOOL_USE
    assert (done.input_tokens, done.output_tokens) == (94, 88)
    assert done.reasoning_tokens == 36
    assert (done.cached_tokens, done.cache_write_tokens) == (40, 3)
    assert done.cache_usage_reported is True
    assert done.reasoning_content == "**Plan**\n\nLook it up."
    assert done.model == "gpt-5.6-terra"
    assert done.reasoning_details == [
        {
            "type": "reasoning",
            "format": RESPONSES_REASONING_FORMAT,
            "encrypted_content": _ENCRYPTED,
            "summary": [{"type": "summary_text", "text": "**Plan**\n\nLook it up."}],
            "id": "rs_1",
        }
    ]


def _tool_loop_history(provenance: MessageProvenance | None) -> list[Message]:
    return [
        Message(
            role="user",
            content=[
                ContentBlockText(text="what is in this picture, and the weather?"),
                ContentBlockImage(media_type="image/png", data="iVBORw0KGgo="),
            ],
        ),
        Message(
            role="assistant",
            content=[
                ContentBlockText(text="Checking."),
                ContentBlockToolUse(id="call_abc", name="get_weather", input={"city": "Hanoi"}),
            ],
            reasoning_content="**Plan**",
            reasoning_details=[
                {
                    "type": "reasoning",
                    "format": RESPONSES_REASONING_FORMAT,
                    "id": "rs_1",
                    "encrypted_content": _ENCRYPTED,
                    "summary": [{"type": "summary_text", "text": "**Plan**"}],
                }
            ],
            provenance=provenance,
        ),
        Message(
            role="user",
            content=[ContentBlockToolResult(tool_use_id="call_abc", content="Sunny 30C")],
        ),
    ]


def test_tool_loop_replays_encrypted_reasoning_before_its_calls(monkeypatch: Any) -> None:
    captured: list[dict[str, Any]] = []
    _patch(monkeypatch, captured, _sse(_tool_call_stream()))
    provider = _provider()

    _run(provider, _thinking_cfg(), messages=_tool_loop_history(provenance_for_provider(provider)))

    assert captured[0]["payload"]["input"] == [
        {
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "what is in this picture, and the weather?"},
                {"type": "input_image", "image_url": "data:image/png;base64,iVBORw0KGgo="},
            ],
        },
        {
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": "**Plan**"}],
            "encrypted_content": _ENCRYPTED,
            "id": "rs_1",
        },
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Checking."}],
        },
        {
            "type": "function_call",
            "call_id": "call_abc",
            "name": "get_weather",
            "arguments": '{"city": "Hanoi"}',
        },
        {"type": "function_call_output", "call_id": "call_abc", "output": "Sunny 30C"},
    ]


def test_another_models_reasoning_is_not_replayed(monkeypatch: Any) -> None:
    captured: list[dict[str, Any]] = []
    _patch(monkeypatch, captured, _sse(_tool_call_stream()))
    foreign = MessageProvenance(provider="openai", model="gpt-5.5", api="openai")

    _run(_provider(), _thinking_cfg(), messages=_tool_loop_history(foreign))

    assert all(item.get("type") != "reasoning" for item in captured[0]["payload"]["input"])


@pytest.mark.parametrize(
    ("level", "effort"),
    [
        # gpt-5.6-terra refuses "minimal" (live API, 2026-11-12).
        (ThinkingLevel.MINIMAL, "low"),
        (ThinkingLevel.LOW, "low"),
        (ThinkingLevel.HIGH, "high"),
        (ThinkingLevel.XHIGH, "xhigh"),
    ],
)
def test_thinking_level_maps_to_a_supported_effort(
    monkeypatch: Any, level: ThinkingLevel, effort: str
) -> None:
    captured: list[dict[str, Any]] = []
    _patch(monkeypatch, captured, _sse(_tool_call_stream()))

    _run(_provider(), _thinking_cfg(thinking_level=level, max_tokens=4))

    assert captured[0]["payload"]["reasoning"]["effort"] == effort
    # /v1/responses rejects max_output_tokens below 16.
    assert captured[0]["payload"]["max_output_tokens"] == 16


@pytest.mark.parametrize(
    ("model", "tools", "thinking"),
    [
        ("gpt-5.6-terra", True, False),  # tools, no thinking: chat with "none"
        ("gpt-5.6-terra", False, True),  # thinking, no tools: chat takes effort
        ("gpt-5.2", True, True),  # older models accept effort with tools
    ],
)
def test_other_turns_stay_on_chat_completions(
    monkeypatch: Any, model: str, tools: bool, thinking: bool
) -> None:
    captured: list[dict[str, Any]] = []
    chat_body = (
        b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    )
    _patch(monkeypatch, captured, chat_body)

    _run(_provider(model), _thinking_cfg(thinking=thinking), tools=[_tool()] if tools else [])

    assert captured[0]["url"].endswith("/v1/chat/completions")


@pytest.mark.parametrize("thinking", [False, True])
def test_gpt_6_1_sol_tool_turn_never_sends_effort_none(monkeypatch: Any, thinking: bool) -> None:
    """gpt-6.1-sol has no ``none``, so a tool turn without thinking cannot take
    the Chat Completions escape its siblings use."""
    captured: list[dict[str, Any]] = []
    _patch(monkeypatch, captured, _sse(_tool_call_stream("gpt-6.1-sol")))

    _run(_provider("gpt-6.1-sol"), _thinking_cfg(thinking=thinking))

    assert captured[0]["url"] == "https://api.openai.com/v1/responses"
    effort = captured[0]["payload"]["reasoning"]["effort"]
    assert effort == ("medium" if thinking else "low")


def test_gateway_tool_turns_are_not_rerouted(monkeypatch: Any) -> None:
    captured: list[dict[str, Any]] = []
    _patch(monkeypatch, captured, b"data: [DONE]\n\n")
    provider = OpenAIProvider(
        api_key="test",
        model="gpt-5.6-terra",
        base_url="https://gw.capminal.ai/api/inference/v1",
        provider_kind="opencap",
    )

    _run(provider, _thinking_cfg())

    assert captured[0]["url"].endswith("/chat/completions")


def test_text_reply_streams_text_and_ends_the_turn(monkeypatch: Any) -> None:
    captured: list[dict[str, Any]] = []
    stream = [
        {"type": "response.output_text.delta", "delta": "Sunny "},
        {"type": "response.output_text.delta", "delta": "today."},
        {
            "type": "response.output_item.done",
            "item": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Sunny today."}],
            },
        },
        {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"input_tokens": 5, "output_tokens": 2}},
        },
    ]
    _patch(monkeypatch, captured, _sse(stream))

    events = _run(_provider(), _thinking_cfg())

    assert "".join(e.text for e in events if isinstance(e, TextDeltaEvent)) == "Sunny today."
    done = events[-1]
    assert isinstance(done, DoneEvent)
    assert done.stop_reason is StopReason.END_TURN
    assert done.reasoning_details is None
    assert done.cache_usage_reported is False


def test_incomplete_response_reports_max_tokens(monkeypatch: Any) -> None:
    captured: list[dict[str, Any]] = []
    stream = [
        {"type": "response.output_text.delta", "delta": "partial"},
        {
            "type": "response.incomplete",
            "response": {
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "usage": {"input_tokens": 5, "output_tokens": 16},
            },
        },
    ]
    _patch(monkeypatch, captured, _sse(stream))

    done = _run(_provider(), _thinking_cfg())[-1]

    assert isinstance(done, DoneEvent)
    assert done.stop_reason is StopReason.MAX_TOKENS
    assert done.raw_stop_reason == "incomplete:max_output_tokens"


def test_failed_response_is_an_error(monkeypatch: Any) -> None:
    captured: list[dict[str, Any]] = []
    stream = [
        {
            "type": "response.failed",
            "response": {
                "status": "failed",
                "error": {"code": "server_error", "message": "upstream failed"},
            },
        }
    ]
    _patch(monkeypatch, captured, _sse(stream))

    events = _run(_provider(), _thinking_cfg())

    assert len(events) == 1
    assert isinstance(events[0], ErrorEvent)
    assert events[0].code == "server_error"
    assert "upstream failed" in events[0].message


def test_http_error_keeps_status_and_body(monkeypatch: Any) -> None:
    captured: list[dict[str, Any]] = []
    body = json.dumps({"error": {"message": "Invalid 'input'", "type": "invalid_request_error"}})
    _patch(monkeypatch, captured, body.encode(), content_type="application/json", status=400)

    events = _run(_provider(), _thinking_cfg())

    assert len(events) == 1
    assert isinstance(events[0], ErrorEvent)
    assert events[0].code == "400"
    assert "Invalid 'input'" in events[0].message


def test_stream_cut_inside_a_tool_call_is_retried_then_reported(monkeypatch: Any) -> None:
    captured: list[dict[str, Any]] = []
    cut = [
        event
        for event in _tool_call_stream()
        if event["type"] not in {"response.output_item.done", "response.completed"}
    ]
    _patch(monkeypatch, captured, _sse(cut))

    events = _run(_provider(), _thinking_cfg())

    # The tool-stream guard re-runs a request cut inside a tool call once.
    assert len(captured) == 2
    assert not any(isinstance(e, (ToolUseEndEvent, DoneEvent)) for e in events)
    assert isinstance(events[-1], ErrorEvent)


def test_stream_ending_without_completion_is_an_error(monkeypatch: Any) -> None:
    captured: list[dict[str, Any]] = []
    _patch(monkeypatch, captured, _sse([{"type": "response.output_text.delta", "delta": "hi"}]))

    events = _run(_provider(), _thinking_cfg())

    assert isinstance(events[-1], ErrorEvent)
    assert events[-1].code == "stream_incomplete"


def test_chat_payload_conversion_keeps_tool_choice_and_parallel_calls() -> None:
    payload = responses_payload_from_chat(
        {
            "model": "gpt-5.6-terra",
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "hi"},
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": '{"q":"x"}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "found"},
            ],
            "max_completion_tokens": 100,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "parameters": {"type": "object"},
                        "strict": True,
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": "lookup"}},
            "parallel_tool_calls": True,
        },
        reasoning_effort="high",
    )

    assert payload["instructions"] == "sys"
    assert payload["tools"] == [
        {"type": "function", "name": "lookup", "parameters": {"type": "object"}, "strict": True}
    ]
    assert payload["tool_choice"] == {"type": "function", "name": "lookup"}
    assert payload["parallel_tool_calls"] is True
    assert payload["input"][1:] == [
        {"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": '{"q":"x"}'},
        {"type": "function_call_output", "call_id": "call_1", "output": "found"},
    ]


def test_responses_provider_drops_stop_and_sends_non_strict_tools(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200, json={"status": "completed", "output": [], "usage": {"input_tokens": 1}}
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("agentos.provider.openai_responses.httpx.AsyncClient", patched)
    provider = OpenAIResponsesProvider(api_key="test", model="gpt-5.4")

    async def _go() -> None:
        async for _ in provider.chat(
            [Message(role="user", content="hi")],
            tools=[_tool()],
            config=ChatConfig(stop_sequences=["END"], max_tokens=32),
        ):
            pass

    asyncio.run(_go())

    assert "stop" not in captured["payload"]
    assert captured["payload"]["tools"][0]["strict"] is False
