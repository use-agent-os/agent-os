from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from agentos.engine.types import ThinkingLevel
from agentos.provider.openai import OpenAIProvider
from agentos.provider.types import (
    ChatConfig,
    ContentBlockToolResult,
    ContentBlockToolUse,
    DoneEvent,
    ErrorEvent,
    Message,
    ModelCapabilities,
    ProviderHeartbeatEvent,
    TextDeltaEvent,
    ToolDefinition,
    ToolInputSchema,
    ToolUseEndEvent,
)


def _sse_body(model: str = "test-model") -> bytes:
    chunks = [
        {
            "model": model,
            "choices": [{"delta": {"content": "ok"}, "finish_reason": None}],
        },
        {
            "model": model,
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        },
    ]
    body = b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks)
    return body + b"data: [DONE]\n\n"


def _patch_transport(monkeypatch: Any, captured: dict[str, Any]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse_body(),
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("agentos.provider.openai.httpx.AsyncClient", patched_async_client)


def _patch_transport_body(monkeypatch: Any, captured: dict[str, Any], body: bytes) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body,
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("agentos.provider.openai.httpx.AsyncClient", patched_async_client)


def _patch_transport_response(
    monkeypatch: Any,
    captured: dict[str, Any],
    response: httpx.Response,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return response

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("agentos.provider.openai.httpx.AsyncClient", patched_async_client)


def _patch_get_transport_response(
    monkeypatch: Any,
    captured: dict[str, Any],
    response: httpx.Response,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        return response

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("agentos.provider.openai.httpx.AsyncClient", patched_async_client)


def _collect(provider: OpenAIProvider, cfg: ChatConfig) -> DoneEvent:
    async def _run() -> DoneEvent:
        done: DoneEvent | None = None
        async for event in provider.chat([Message(role="user", content="hi")], config=cfg):
            if isinstance(event, DoneEvent):
                done = event
        assert done is not None
        return done

    return asyncio.run(_run())


def test_openrouter_stream_timeout_emits_heartbeat_before_non_stream_fallback(
    monkeypatch: Any,
) -> None:
    class TimeoutStream:
        async def __aenter__(self) -> Any:
            raise httpx.ReadTimeout("stream idle")

        async def __aexit__(self, *_exc: Any) -> None:
            return None

    class TimeoutClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> TimeoutClient:
            return self

        async def __aexit__(self, *_exc: Any) -> None:
            return None

        def stream(self, *args: Any, **kwargs: Any) -> TimeoutStream:
            return TimeoutStream()

    class SlowFallbackProvider(OpenAIProvider):
        async def _complete_non_stream(self, **kwargs: Any):
            await asyncio.sleep(0.05)
            yield ErrorEvent(message="fallback finished", code="timeout")

    monkeypatch.setattr("agentos.provider.openai.httpx.AsyncClient", TimeoutClient)
    provider = SlowFallbackProvider(
        api_key="test",
        model="deepseek/deepseek-v4-flash",
        base_url="https://openrouter.ai/api/v1",
        provider_kind="openrouter",
    )

    async def _first_event() -> Any:
        events = provider.chat(
            [Message(role="user", content="hi")],
            config=ChatConfig(timeout=1.0),
        )
        return await asyncio.wait_for(anext(events), timeout=0.02)

    event = asyncio.run(_first_event())

    assert isinstance(event, ProviderHeartbeatEvent)
    assert event.phase == "llm_fallback"


def test_openrouter_list_models_reports_openrouter_provider(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_get_transport_response(
        monkeypatch,
        captured,
        httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "deepseek/deepseek-v4-flash",
                        "name": "DeepSeek V4 Flash",
                        "context_length": 128000,
                        "top_provider": {"max_completion_tokens": 8192},
                    }
                ]
            },
            request=httpx.Request("GET", "https://openrouter.ai/api/v1/models"),
        ),
    )
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek/deepseek-v4-flash",
        base_url="https://openrouter.ai/api/v1",
        provider_kind="openrouter",
    )

    rows = asyncio.run(provider.list_models())

    assert captured["url"] == "https://openrouter.ai/api/v1/models"
    assert rows[0].provider == "openrouter"
    assert rows[0].model_id == "deepseek/deepseek-v4-flash"


def test_openrouter_http_error_names_provider_request(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport_response(
        monkeypatch,
        captured,
        httpx.Response(
            500,
            content=b"Internal Server Error",
            request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
        ),
    )
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek/deepseek-v4-flash",
        base_url="https://openrouter.ai/api/v1",
        provider_kind="openrouter",
    )

    events = _collect_events(provider, ChatConfig())

    error = next(event for event in events if isinstance(event, ErrorEvent))
    assert error.code == "500"
    assert error.message == "OpenRouter chat request failed (HTTP 500): Internal Server Error"


def test_opencap_http_error_names_provider_and_preserves_detail(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport_response(
        monkeypatch,
        captured,
        httpx.Response(
            400,
            json={"error": {"message": "unsupported_provider"}},
            request=httpx.Request(
                "POST", "https://gw.capminal.ai/api/inference/v1/chat/completions"
            ),
        ),
    )
    provider = OpenAIProvider(
        api_key="test",
        model="glm-5.2",
        base_url="https://gw.capminal.ai/api/inference/v1",
        provider_kind="opencap",
    )

    events = _collect_events(provider, ChatConfig())

    error = next(event for event in events if isinstance(event, ErrorEvent))
    assert error.code == "400"
    assert error.message == "OpenCAP chat request failed (HTTP 400): unsupported_provider"


def test_openrouter_deepseek_v4_returns_reasoning_content_from_details(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    chunks = [
        {
            "model": "deepseek/deepseek-v4-flash",
            "choices": [
                {
                    "delta": {
                        "reasoning_details": [
                            {"type": "reasoning.text", "text": "I considered the request."}
                        ],
                    },
                    "finish_reason": None,
                }
            ],
        },
        {
            "model": "deepseek/deepseek-v4-flash",
            "choices": [{"delta": {"content": "ok"}, "finish_reason": None}],
        },
        {
            "model": "deepseek/deepseek-v4-flash",
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        },
    ]
    body = b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks)
    body += b"data: [DONE]\n\n"
    _patch_transport_body(monkeypatch, captured, body)
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek/deepseek-v4-flash",
        base_url="https://openrouter.ai/api/v1",
        provider_kind="openrouter",
        provider_routing={"deepseek/deepseek-v4-flash": "deepseek"},
    )
    cfg = ChatConfig(
        thinking=True,
        thinking_level=ThinkingLevel.HIGH,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="openrouter",
        ),
    )

    done = _collect(provider, cfg)

    assert captured["payload"]["provider"] == {
        "order": ["deepseek"],
        "allow_fallbacks": True,
    }
    assert captured["payload"]["reasoning"] == {"effort": "high"}
    assert done.reasoning_content == "I considered the request."


def test_opencap_default_request_omits_provider_allow_list(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="glm-5.2",
        base_url="https://gw.capminal.ai/api/inference/v1",
        provider_kind="opencap",
    )

    _collect(provider, ChatConfig())

    assert captured["url"] == "https://gw.capminal.ai/api/inference/v1/chat/completions"
    assert "provider" not in captured["payload"]


def test_opencap_request_emits_provider_allow_list(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="glm-5.2",
        base_url="https://gw.capminal.ai/api/inference/v1",
        provider_kind="opencap",
        provider_routing={"glm-5.2": "surplus"},
    )

    _collect(provider, ChatConfig())

    assert captured["payload"]["provider"] == ["surplus"]


def test_opencap_reuses_streaming_and_tool_request_contract(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="glm-5.2",
        base_url="https://gw.capminal.ai/api/inference/v1",
        provider_kind="opencap",
    )
    tool = ToolDefinition(
        name="lookup",
        description="Lookup a value.",
        input_schema=ToolInputSchema(properties={"q": {"type": "string"}}, required=["q"]),
    )

    events = _collect_events(
        provider,
        ChatConfig(tool_choice="required"),
        tools=[tool],
    )

    assert captured["payload"]["stream"] is True
    assert captured["payload"]["stream_options"] == {"include_usage": True}
    assert captured["payload"]["tool_choice"] == "required"
    assert captured["payload"]["tools"][0]["function"]["name"] == "lookup"
    assert any(isinstance(event, DoneEvent) for event in events)


def _collect_events(
    provider: OpenAIProvider,
    cfg: ChatConfig,
    tools: list[ToolDefinition] | None = None,
    messages: list[Message] | None = None,
) -> list[Any]:
    async def _run() -> list[Any]:
        return [
            event
            async for event in provider.chat(
                messages or [Message(role="user", content="hi")],
                config=cfg,
                tools=tools,
            )
        ]

    return asyncio.run(_run())


def test_deepseek_thinking_uses_provider_thinking_field_not_openai_reasoning_effort(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek-chat",
        base_url="https://api.deepseek.com",
        provider_kind="deepseek",
    )
    cfg = ChatConfig(
        thinking=True,
        thinking_level=ThinkingLevel.HIGH,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="deepseek",
        ),
    )

    _collect(provider, cfg)

    assert captured["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert captured["payload"]["thinking"] == {"type": "enabled"}
    assert captured["payload"]["reasoning_effort"] == "high"


def test_deepseek_non_thinking_sends_provider_disabled_for_default_thinking_model(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek-v4-flash",
        base_url="https://api.deepseek.com",
        provider_kind="deepseek",
    )
    cfg = ChatConfig(
        thinking=False,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="deepseek",
        ),
    )

    _collect(provider, cfg)

    assert captured["payload"]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in captured["payload"]


def test_deepseek_tool_replay_preserves_reasoning_content_in_payload(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek-v4-pro",
        base_url="https://api.deepseek.com",
        provider_kind="deepseek",
    )
    messages = [
        Message(
            role="assistant",
            content=[
                ContentBlockToolUse(
                    id="call_lookup",
                    name="lookup",
                    input={"q": "cache"},
                )
            ],
            reasoning_content="I need to inspect the cache state before answering.",
        ),
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id="call_lookup",
                    content="cache is warm",
                )
            ],
        ),
        Message(role="user", content="continue"),
    ]
    cfg = ChatConfig(
        thinking=True,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="deepseek",
        ),
    )

    async def _run() -> None:
        async for _ in provider.chat(messages, config=cfg):
            pass

    asyncio.run(_run())

    assert captured["payload"]["messages"][0]["role"] == "assistant"
    assert captured["payload"]["messages"][0]["tool_calls"][0]["id"] == "call_lookup"
    assert (
        captured["payload"]["messages"][0]["reasoning_content"]
        == "I need to inspect the cache state before answering."
    )
    assert captured["payload"]["messages"][1] == {
        "role": "tool",
        "tool_call_id": "call_lookup",
        "content": "cache is warm",
    }


def test_deepseek_v4_tool_replay_adds_empty_reasoning_content_when_missing(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek-v4-flash",
        base_url="https://api.deepseek.com",
        provider_kind="deepseek",
    )
    messages = [
        Message(
            role="assistant",
            content=[
                ContentBlockToolUse(
                    id="call_lookup",
                    name="lookup",
                    input={"q": "cache"},
                )
            ],
        ),
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id="call_lookup",
                    content="cache is warm",
                )
            ],
        ),
        Message(role="user", content="continue"),
    ]
    cfg = ChatConfig(
        thinking=True,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="deepseek",
        ),
    )

    async def _run() -> None:
        async for _ in provider.chat(messages, config=cfg):
            pass

    asyncio.run(_run())

    assert captured["payload"]["messages"][0]["reasoning_content"] == ""


def test_deepseek_v4_text_replay_adds_empty_reasoning_content_when_missing(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek-v4-flash",
        base_url="https://api.deepseek.com",
        provider_kind="deepseek",
    )
    messages = [
        Message(role="assistant", content="Prior non-thinking assistant turn."),
        Message(role="user", content="continue in thinking mode"),
    ]
    cfg = ChatConfig(
        thinking=True,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="deepseek",
        ),
    )

    async def _run() -> None:
        async for _ in provider.chat(messages, config=cfg):
            pass

    asyncio.run(_run())

    assert captured["payload"]["messages"][0] == {
        "role": "assistant",
        "content": "Prior non-thinking assistant turn.",
        "reasoning_content": "",
    }


def test_deepseek_v4_non_thinking_replays_prior_reasoning_content(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek-v4-flash",
        base_url="https://api.deepseek.com",
        provider_kind="deepseek",
    )
    messages = [
        Message(
            role="assistant",
            content="previous answer",
            reasoning_content="prior thinking from earlier deepseek turn",
        ),
        Message(role="user", content="continue"),
    ]
    cfg = ChatConfig(
        thinking=False,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="deepseek",
        ),
    )

    async def _run() -> None:
        async for _ in provider.chat(messages, config=cfg):
            pass

    asyncio.run(_run())

    assert captured["payload"]["thinking"] == {"type": "disabled"}
    assert (
        captured["payload"]["messages"][0]["reasoning_content"]
        == "prior thinking from earlier deepseek turn"
    )


def test_deepseek_v4_replays_reasoning_content_without_catalog_capabilities(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek-v4-flash",
        base_url="https://api.deepseek.com",
        provider_kind="deepseek",
    )
    messages = [
        Message(
            role="assistant",
            content="previous answer",
            reasoning_content="prior thinking from direct deepseek",
        ),
        Message(role="user", content="continue"),
    ]
    cfg = ChatConfig(thinking=True, model_capabilities=None)

    async def _run() -> None:
        async for _ in provider.chat(messages, config=cfg):
            pass

    asyncio.run(_run())

    assert captured["payload"]["thinking"] == {"type": "enabled"}
    assert captured["payload"]["reasoning_effort"] == "high"
    assert (
        captured["payload"]["messages"][0]["reasoning_content"]
        == "prior thinking from direct deepseek"
    )


def test_openrouter_reasoning_model_replays_reasoning_content_by_capability(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="anthropic/claude-sonnet-4.5",
        base_url="https://openrouter.ai/api/v1",
        provider_kind="openrouter",
    )
    messages = [
        Message(
            role="assistant",
            content="previous answer",
            reasoning_content="openrouter-native reasoning should be replayed",
        ),
        Message(role="user", content="continue"),
    ]
    cfg = ChatConfig(
        thinking=True,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="openrouter",
        ),
    )

    async def _run() -> None:
        async for _ in provider.chat(messages, config=cfg):
            pass

    asyncio.run(_run())

    assert (
        captured["payload"]["messages"][0]["reasoning_content"]
        == "openrouter-native reasoning should be replayed"
    )


def test_non_deepseek_reasoning_model_does_not_replay_reasoning_content(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="gemini-2.5-pro",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        provider_kind="gemini",
    )
    messages = [
        Message(
            role="assistant",
            content="previous answer",
            reasoning_content="provider-internal reasoning must not be replayed",
        ),
        Message(role="user", content="continue"),
    ]
    cfg = ChatConfig(
        thinking=True,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="gemini",
        ),
    )

    async def _run() -> None:
        async for _ in provider.chat(messages, config=cfg):
            pass

    asyncio.run(_run())

    assert "reasoning_content" not in captured["payload"]["messages"][0]


def test_deepseek_non_v4_model_does_not_replay_reasoning_content(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek-chat",
        base_url="https://api.deepseek.com/v1",
        provider_kind="deepseek",
    )
    messages = [
        Message(
            role="assistant",
            content="previous answer",
            reasoning_content="must not be replayed for non-v4 direct DeepSeek",
        ),
        Message(role="user", content="continue"),
    ]
    cfg = ChatConfig(
        thinking=True,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="deepseek",
        ),
    )

    async def _run() -> None:
        async for _ in provider.chat(messages, config=cfg):
            pass

    asyncio.run(_run())

    assert "reasoning_content" not in captured["payload"]["messages"][0]


def test_deepseek_reasoning_format_without_deepseek_model_does_not_replay_reasoning_content(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="custom-reasoning-model",
        base_url="https://api.deepseek.com/v1",
        provider_kind="deepseek",
    )
    messages = [
        Message(
            role="assistant",
            content="previous answer",
            reasoning_content="must not be replayed for a non-DeepSeek model",
        ),
        Message(role="user", content="continue"),
    ]
    cfg = ChatConfig(
        thinking=True,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="deepseek",
        ),
    )

    async def _run() -> None:
        async for _ in provider.chat(messages, config=cfg):
            pass

    asyncio.run(_run())

    assert "reasoning_content" not in captured["payload"]["messages"][0]


def test_gemini_reasoning_uses_openai_compatible_reasoning_effort(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="gemini-2.5-flash",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        provider_kind="gemini",
    )
    cfg = ChatConfig(
        thinking=True,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="gemini",
        ),
    )

    _collect(provider, cfg)

    assert captured["payload"]["reasoning_effort"] == "medium"
    assert "thinking" not in captured["payload"]


def test_gemini_25_flash_lite_non_thinking_uses_reasoning_effort_none(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="gemini-2.5-flash-lite",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        provider_kind="gemini",
    )
    cfg = ChatConfig(
        thinking=False,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="gemini",
        ),
    )

    _collect(provider, cfg)

    assert captured["payload"]["reasoning_effort"] == "none"
    assert "thinking" not in captured["payload"]


def test_gemini_31_flash_lite_non_thinking_uses_reasoning_effort_none(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="gemini-3.1-flash-lite",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        provider_kind="gemini",
    )
    cfg = ChatConfig(
        thinking=False,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="gemini",
        ),
    )

    _collect(provider, cfg)

    assert captured["payload"]["reasoning_effort"] == "none"
    assert "thinking" not in captured["payload"]


def test_openrouter_glm_5_2_non_thinking_disables_reasoning_by_default(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="z-ai/glm-5.2",
        base_url="https://openrouter.ai/api/v1",
        provider_kind="openrouter",
    )
    cfg = ChatConfig(
        thinking=False,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="openrouter",
        ),
    )

    _collect(provider, cfg)

    assert captured["payload"]["reasoning"] == {"enabled": False}


def test_zai_thinking_uses_provider_thinking_object(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="glm-4.5",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        provider_kind="zhipu",
    )
    cfg = ChatConfig(
        thinking=True,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="zai",
        ),
    )

    _collect(provider, cfg)

    assert captured["payload"]["thinking"] == {"type": "enabled"}
    assert "reasoning_effort" not in captured["payload"]


def test_glm_5_1_thinking_uses_provider_thinking_object(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="glm-5.1",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        provider_kind="zhipu",
    )
    cfg = ChatConfig(
        thinking=True,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="zai",
        ),
    )

    _collect(provider, cfg)

    assert captured["payload"]["thinking"] == {"type": "enabled"}
    assert "reasoning_effort" not in captured["payload"]


def test_zai_non_thinking_sends_provider_disabled_for_default_thinking_model(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="glm-5.1",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        provider_kind="zhipu",
    )
    cfg = ChatConfig(
        thinking=False,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="zai",
        ),
    )

    _collect(provider, cfg)

    assert captured["payload"]["thinking"] == {"type": "disabled"}


def test_dashscope_thinking_uses_enable_thinking_and_budget(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-plus",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )
    cfg = ChatConfig(
        thinking=True,
        thinking_budget_tokens=4096,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="dashscope",
        ),
    )

    _collect(provider, cfg)

    assert captured["payload"]["enable_thinking"] is True
    assert captured["payload"]["thinking_budget"] == 4096
    assert "reasoning_effort" not in captured["payload"]


def test_dashscope_non_thinking_sends_enable_thinking_false(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )
    cfg = ChatConfig(
        thinking=False,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="dashscope",
        ),
    )

    _collect(provider, cfg)

    assert captured["payload"]["enable_thinking"] is False
    assert "thinking_budget" not in captured["payload"]


def test_moonshot_kimi_thinking_uses_provider_thinking_object(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="kimi-k2.5",
        base_url="https://api.moonshot.cn/v1",
        provider_kind="moonshot",
    )
    cfg = ChatConfig(
        thinking=True,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="moonshot",
        ),
    )

    _collect(provider, cfg)

    assert captured["payload"]["thinking"] == {"type": "enabled"}


def test_moonshot_kimi_non_thinking_sends_provider_disabled(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="kimi-k2.5",
        base_url="https://api.moonshot.cn/v1",
        provider_kind="moonshot",
    )
    cfg = ChatConfig(
        thinking=False,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="moonshot",
        ),
    )

    _collect(provider, cfg)

    assert captured["payload"]["thinking"] == {"type": "disabled"}


def test_volcengine_thinking_uses_provider_thinking_object(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="doubao-seed-1-6-thinking-250715",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        provider_kind="volcengine",
    )
    cfg = ChatConfig(
        thinking=True,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="volcengine",
        ),
    )

    _collect(provider, cfg)

    assert captured["payload"]["thinking"] == {"type": "enabled"}


def test_volcengine_non_thinking_sends_provider_disabled(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="doubao-seed-1-6-thinking-250715",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        provider_kind="volcengine",
    )
    cfg = ChatConfig(
        thinking=False,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="volcengine",
        ),
    )

    _collect(provider, cfg)

    assert captured["payload"]["thinking"] == {"type": "disabled"}


def test_moonshot_kimi_k2_6_omits_temperature_for_fixed_sampling(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="kimi-k2.6",
        base_url="https://api.moonshot.cn/v1",
        provider_kind="moonshot",
    )

    _collect(provider, ChatConfig(temperature=0))

    assert "temperature" not in captured["payload"]


def test_moonshot_v1_still_sends_temperature(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="moonshot-v1-8k",
        base_url="https://api.moonshot.cn/v1",
        provider_kind="moonshot",
    )

    _collect(provider, ChatConfig(temperature=0))

    assert captured["payload"]["temperature"] == 0


def test_direct_openai_gpt_5_5_reasoning_uses_max_completion_tokens_without_temperature(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="gpt-5.5",
        base_url="https://api.openai.com/v1",
        provider_kind="openai",
    )
    cfg = ChatConfig(
        thinking=True,
        temperature=0,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="openai",
        ),
    )

    _collect(provider, cfg)

    assert captured["payload"]["max_completion_tokens"] == cfg.max_tokens
    assert "max_tokens" not in captured["payload"]
    assert captured["payload"]["reasoning_effort"] == "medium"
    assert "temperature" not in captured["payload"]


def test_openrouter_still_sends_temperature(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="openai/gpt-4o-mini",
        base_url="https://openrouter.ai/api/v1",
        provider_kind="openrouter",
    )

    _collect(provider, ChatConfig(temperature=0))

    assert captured["payload"]["temperature"] == 0


def test_siliconflow_baseline_payload_does_not_enable_provider_thinking_by_default(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek-ai/DeepSeek-V3",
        base_url="https://api.siliconflow.cn/v1",
        provider_kind="siliconflow",
    )

    _collect(provider, ChatConfig())

    assert "enable_thinking" not in captured["payload"]
    assert "thinking_budget" not in captured["payload"]
    assert "thinking" not in captured["payload"]


def test_ovms_v3_base_url_posts_to_v3_chat_completions(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="unused",
        model="llama3",
        base_url="http://localhost:8000/v3",
        provider_kind="ovms",
    )

    _collect(provider, ChatConfig())

    assert captured["url"] == "http://localhost:8000/v3/chat/completions"


def test_qianfan_v2_base_url_posts_to_v2_chat_completions(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="unused",
        model="ernie-4.0-turbo-8k",
        base_url="https://qianfan.baidubce.com/v2",
        provider_kind="qianfan",
    )

    _collect(provider, ChatConfig())

    assert captured["url"] == "https://qianfan.baidubce.com/v2/chat/completions"


def test_zai_v4_base_url_posts_to_v4_chat_completions(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="unused",
        model="glm-4.5",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        provider_kind="zhipu",
    )

    _collect(provider, ChatConfig())

    assert captured["url"] == "https://open.bigmodel.cn/api/paas/v4/chat/completions"


def test_gemini_stream_tool_call_without_index_is_tolerated(monkeypatch: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        chunks = [
            {
                "model": "gemini-2.5-flash",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "id": "call_lookup",
                                    "type": "function",
                                    "function": {
                                        "name": "lookup",
                                        "arguments": '{"q":"hi"}',
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "model": "gemini-2.5-flash",
                "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2},
            },
        ]
        body = b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body + b"data: [DONE]\n\n",
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("agentos.provider.openai.httpx.AsyncClient", patched_async_client)
    provider = OpenAIProvider(
        api_key="test",
        model="gemini-2.5-flash",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        provider_kind="gemini",
    )
    tool = ToolDefinition(
        name="lookup",
        description="Lookup a value.",
        input_schema=ToolInputSchema(properties={"q": {"type": "string"}}, required=["q"]),
    )

    events = _collect_events(provider, ChatConfig(), tools=[tool])

    tool_end = next(event for event in events if isinstance(event, ToolUseEndEvent))
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert tool_end.tool_use_id == "call_lookup"
    assert tool_end.tool_name == "lookup"
    assert tool_end.arguments == {"q": "hi"}
    assert done.model == "gemini-2.5-flash"


def test_openai_compat_sends_required_tool_choice_when_configured(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="gpt-test",
        base_url="https://openrouter.ai/api/v1",
        provider_kind="openrouter",
    )
    tool = ToolDefinition(
        name="memory_search",
        description="Search memory.",
        input_schema=ToolInputSchema(properties={"name": {"type": "string"}}, required=["name"]),
    )

    _collect_events(provider, ChatConfig(tool_choice="required"), tools=[tool])

    assert captured["payload"]["tool_choice"] == "required"


def test_openai_compat_sends_named_function_tool_choice_when_configured(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="gpt-test",
        base_url="https://openrouter.ai/api/v1",
        provider_kind="openrouter",
    )
    tool = ToolDefinition(
        name="memory_search",
        description="Search memory.",
        input_schema=ToolInputSchema(properties={"name": {"type": "string"}}, required=["name"]),
    )
    tool_choice = {"type": "function", "function": {"name": "memory_search"}}

    _collect_events(provider, ChatConfig(tool_choice=tool_choice), tools=[tool])

    assert captured["payload"]["tool_choice"] == tool_choice


def test_gemini_stream_multiple_tool_calls_without_indexes_stay_separate(
    monkeypatch: Any,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        chunks = [
            {
                "model": "gemini-2.5-flash",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "id": "call_lookup",
                                    "type": "function",
                                    "function": {
                                        "name": "lookup",
                                        "arguments": '{"q":"hi"}',
                                    },
                                },
                                {
                                    "id": "call_save",
                                    "type": "function",
                                    "function": {
                                        "name": "save",
                                        "arguments": '{"value":1}',
                                    },
                                },
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "model": "gemini-2.5-flash",
                "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2},
            },
        ]
        body = b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body + b"data: [DONE]\n\n",
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("agentos.provider.openai.httpx.AsyncClient", patched_async_client)
    provider = OpenAIProvider(
        api_key="test",
        model="gemini-2.5-flash",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        provider_kind="gemini",
    )
    tools = [
        ToolDefinition(
            name="lookup",
            description="Lookup a value.",
            input_schema=ToolInputSchema(properties={"q": {"type": "string"}}, required=["q"]),
        ),
        ToolDefinition(
            name="save",
            description="Save a value.",
            input_schema=ToolInputSchema(
                properties={"value": {"type": "number"}},
                required=["value"],
            ),
        ),
    ]

    events = _collect_events(provider, ChatConfig(), tools=tools)

    tool_ends = [event for event in events if isinstance(event, ToolUseEndEvent)]
    assert [(event.tool_use_id, event.tool_name, event.arguments) for event in tool_ends] == [
        ("call_lookup", "lookup", {"q": "hi"}),
        ("call_save", "save", {"value": 1}),
    ]


# --- Null-valued streaming fields -------------------------------------------
#
# ``dict.get(key, default)`` only returns ``default`` when the key is ABSENT.
# Several OpenAI-compatible gateways (glm-* via capminal/opencap among them)
# emit the key with an explicit ``null`` instead of omitting it, so the stream
# loop must treat null and missing identically or the whole turn dies with
# ``TypeError: 'NoneType' object is not iterable``.


def _null_field_body(chunks: list[dict[str, Any]]) -> bytes:
    body = b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks)
    return body + b"data: [DONE]\n\n"


def _streamed_text(events: list[Any]) -> str:
    return "".join(e.text for e in events if isinstance(e, TextDeltaEvent))


def _glm_provider() -> OpenAIProvider:
    return OpenAIProvider(
        api_key="test",
        model="glm-5.2",
        base_url="https://gw.capminal.ai/api/inference/v1",
        provider_kind="opencap",
    )


def test_stream_survives_an_explicit_null_tool_calls_in_a_delta(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport_body(
        monkeypatch,
        captured,
        _null_field_body(
            [
                {
                    "model": "glm-5.2",
                    "choices": [
                        {
                            "delta": {"content": "hello", "tool_calls": None},
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "model": "glm-5.2",
                    "choices": [{"delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 1},
                },
            ]
        ),
    )

    events = _collect_events(_glm_provider(), ChatConfig())

    assert not [event for event in events if isinstance(event, ErrorEvent)]
    assert _streamed_text(events) == "hello"


def test_stream_survives_an_explicit_null_choices_on_the_usage_chunk(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport_body(
        monkeypatch,
        captured,
        _null_field_body(
            [
                {
                    "model": "glm-5.2",
                    "choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}],
                },
                {
                    "model": "glm-5.2",
                    "choices": None,
                    "usage": {"prompt_tokens": 2, "completion_tokens": 1},
                },
            ]
        ),
    )

    events = _collect_events(_glm_provider(), ChatConfig())

    assert not [event for event in events if isinstance(event, ErrorEvent)]
    assert _streamed_text(events) == "hi"
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.input_tokens == 2


def test_stream_survives_an_explicit_null_delta(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport_body(
        monkeypatch,
        captured,
        _null_field_body(
            [
                {"model": "glm-5.2", "choices": [{"delta": None, "finish_reason": None}]},
                {
                    "model": "glm-5.2",
                    "choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}],
                },
            ]
        ),
    )

    events = _collect_events(_glm_provider(), ChatConfig())

    assert not [event for event in events if isinstance(event, ErrorEvent)]
    assert _streamed_text(events) == "hi"


def test_stream_survives_a_null_function_object_inside_a_tool_call(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport_body(
        monkeypatch,
        captured,
        _null_field_body(
            [
                {
                    "model": "glm-5.2",
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {"index": 0, "id": "call_a", "function": None},
                                    {
                                        "index": 0,
                                        "function": {
                                            "name": "lookup",
                                            "arguments": '{"q": "hi"}',
                                        },
                                    },
                                ]
                            },
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "model": "glm-5.2",
                    "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
                },
            ]
        ),
    )

    events = _collect_events(_glm_provider(), ChatConfig())

    assert not [event for event in events if isinstance(event, ErrorEvent)]
    tool_ends = [event for event in events if isinstance(event, ToolUseEndEvent)]
    assert [(event.tool_use_id, event.tool_name, event.arguments) for event in tool_ends] == [
        ("call_a", "lookup", {"q": "hi"}),
    ]


def test_gemini_stream_tool_call_extracts_thought_signature(monkeypatch: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        chunks = [
            {
                "model": "gemini-3.5-flash",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "id": "call_list_dir",
                                    "type": "function",
                                    "function": {
                                        "name": "default_api:list_dir",
                                        "arguments": '{"DirectoryPath": "/tmp"}',
                                    },
                                    "thought_signature": "gemini_encrypted_signature_abc123",
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "model": "gemini-3.5-flash",
                "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        ]
        body = b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body + b"data: [DONE]\n\n",
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("agentos.provider.openai.httpx.AsyncClient", patched_async_client)
    provider = OpenAIProvider(
        api_key="test",
        model="gemini-3.5-flash",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        provider_kind="gemini",
    )
    tool = ToolDefinition(
        name="default_api:list_dir",
        description="List dir.",
        input_schema=ToolInputSchema(
            properties={"DirectoryPath": {"type": "string"}},
            required=["DirectoryPath"],
        ),
    )

    events = _collect_events(provider, ChatConfig(), tools=[tool])

    tool_end = next(event for event in events if isinstance(event, ToolUseEndEvent))
    assert tool_end.tool_use_id == "call_list_dir"
    assert tool_end.tool_name == "default_api:list_dir"
    assert tool_end.arguments == {"DirectoryPath": "/tmp"}
    assert tool_end.thought_signature == "gemini_encrypted_signature_abc123"


def test_gemini_build_messages_preserves_thought_signature() -> None:
    from agentos.provider.openai import _build_openai_messages

    msg = Message(
        role="assistant",
        content=[
            ContentBlockToolUse(
                id="call_list_dir",
                name="default_api:list_dir",
                input={"DirectoryPath": "/tmp"},
                thought_signature="gemini_encrypted_signature_abc123",
            )
        ],
    )

    formatted = _build_openai_messages(msg, provider_kind="gemini")
    assert len(formatted) == 1
    assert formatted[0]["role"] == "assistant"
    tool_calls = formatted[0]["tool_calls"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["id"] == "call_list_dir"
    assert tool_calls[0]["thought_signature"] == "gemini_encrypted_signature_abc123"


def test_gemini_build_messages_synthetic_adds_skip_validator() -> None:
    from agentos.provider.openai import _build_openai_messages

    msg = Message(
        role="assistant",
        content=[
            ContentBlockToolUse(
                id="call_list_dir",
                name="default_api:list_dir",
                input={"DirectoryPath": "/tmp"},
                thought_signature=None,
            )
        ],
    )

    formatted = _build_openai_messages(msg, provider_kind="gemini")
    assert len(formatted) == 1
    assert formatted[0]["role"] == "assistant"
    tool_calls = formatted[0]["tool_calls"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["id"] == "call_list_dir"
    assert tool_calls[0]["thought_signature"] == "skip_thought_signature_validator"


def test_gemini_outbound_chat_payload_includes_thought_signature(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)

    provider = OpenAIProvider(
        api_key="test",
        model="gemini-3.5-flash",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        provider_kind="gemini",
    )

    messages = [
        Message(role="user", content="List the directory"),
        Message(
            role="assistant",
            content=[
                ContentBlockToolUse(
                    id="call_list_dir",
                    name="default_api:list_dir",
                    input={"DirectoryPath": "/tmp"},
                    thought_signature="sig_encrypted_999",
                )
            ],
        ),
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id="call_list_dir",
                    content='["file1.txt"]',
                )
            ],
        ),
    ]

    _collect_events(provider, ChatConfig(), messages=messages)

    outbound_messages = captured["payload"]["messages"]
    assistant_msg = next(m for m in outbound_messages if m["role"] == "assistant")
    assert assistant_msg["tool_calls"][0]["thought_signature"] == "sig_encrypted_999"


def test_stream_consumer_stage_persists_thought_signature_and_history_rebuilds_it() -> None:
    from agentos.engine.history import reconstruct_messages_from_entry
    from agentos.engine.turn_runner.stream_consumer_stage import (
        _StreamState,
        _ToolResultHandler,
        _ToolUseStartHandler,
    )
    from agentos.engine.types import ToolResultEvent, ToolUseStartEvent

    state = _StreamState()
    start_handler = _ToolUseStartHandler()
    result_handler = _ToolResultHandler()

    # 1. ToolUseStart arrives
    start_handler.handle(
        ToolUseStartEvent(
            tool_use_id="call_test_123",
            tool_name="test_tool",
        ),
        state,
    )

    # 2. ToolResult arrives carrying thought_signature
    result_handler.handle(
        ToolResultEvent(
            tool_use_id="call_test_123",
            tool_name="test_tool",
            arguments={"query": "hello"},
            result="success",
            thought_signature="gemini_sig_transcript_persist",
        ),
        state,
    )

    # Verify tool_use segment in turn_segments has thought_signature
    tool_use_seg = next(s for s in state.turn_segments if s.get("type") == "tool_use")
    assert tool_use_seg["thought_signature"] == "gemini_sig_transcript_persist"

    # Rebuild history from transcript segments
    messages = reconstruct_messages_from_entry(
        role="assistant",
        content="",
        tool_calls=state.turn_segments,
    )
    assert len(messages) >= 1
    assistant_msg = messages[0]
    tool_use_block = next(b for b in assistant_msg.content if b.type == "tool_use")
    assert tool_use_block.thought_signature == "gemini_sig_transcript_persist"


# ── Mid-stream `error` chunk (unclaimed sibling of #2118's Anthropic fix) ──


def test_openrouter_mid_stream_error_yields_error_event_and_keeps_partial_text(
    monkeypatch: Any,
) -> None:
    """OpenRouter's documented mid-stream failure shape: HTTP already 200, and
    -- deliberately, so a naive OpenAI-spec client doesn't crash on
    choices[0].delta -- a *non-empty* choices entry (finish_reason="error",
    empty delta) alongside the top-level `error` object. Before this fix,
    `chunk.get("error")` was never read: the loop just processed the empty
    delta as a normal (silent) chunk and fell through to an unconditional
    DoneEvent, discarding the error and reporting the turn as a clean
    success. https://openrouter.ai/docs/api_reference/streaming
    """
    captured: dict[str, Any] = {}
    chunks = [
        {"model": "openai/gpt-4o", "choices": [{"delta": {"content": "Hel"}}]},
        {"model": "openai/gpt-4o", "choices": [{"delta": {"content": "lo"}}]},
        {
            "id": "cmpl-abc123",
            "object": "chat.completion.chunk",
            "model": "openai/gpt-4o",
            "provider": "openai",
            "error": {"code": "server_error", "message": "Provider disconnected unexpectedly"},
            "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "error"}],
        },
    ]
    body = b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks)
    _patch_transport_body(monkeypatch, captured, body)
    provider = OpenAIProvider(
        api_key="test",
        model="openai/gpt-4o",
        base_url="https://openrouter.ai/api/v1",
        provider_kind="openrouter",
    )

    events = _collect_events(provider, ChatConfig())

    text = "".join(e.text for e in events if isinstance(e, TextDeltaEvent))
    assert text == "Hello"
    assert len(events) == 3
    error = events[-1]
    assert isinstance(error, ErrorEvent)
    assert error.code == "server_error"
    assert error.message == "Provider disconnected unexpectedly"
    assert not any(isinstance(e, DoneEvent) for e in events)


def test_mid_stream_error_with_empty_choices_still_yields_error_event(
    monkeypatch: Any,
) -> None:
    """Plain OpenAI's own spec (per OpenRouter's docs, contrasting their own
    deliberate deviation) emits a mid-stream error chunk with an *empty*
    choices array rather than OpenRouter's populated one. The check must not
    assume OpenRouter's choices-populated shape -- it has to work whether or
    not choices carries anything at all."""
    captured: dict[str, Any] = {}
    chunks = [
        {"model": "gpt-5.6-luna", "choices": [{"delta": {"content": "Hi"}}]},
        {
            "model": "gpt-5.6-luna",
            "error": {"message": "the server had an error processing your request"},
            "choices": [],
        },
    ]
    body = b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks)
    _patch_transport_body(monkeypatch, captured, body)
    provider = OpenAIProvider(api_key="test", model="gpt-5.6-luna")

    events = _collect_events(provider, ChatConfig())

    assert len(events) == 2
    error = events[-1]
    assert isinstance(error, ErrorEvent)
    assert error.code == ""
    assert error.message == "the server had an error processing your request"
    assert not any(isinstance(e, DoneEvent) for e in events)


def test_malformed_error_chunk_still_yields_error_event(monkeypatch: Any) -> None:
    """A truthy `error` value with an unexpected shape (a bare string here,
    instead of the documented {"code", "message"} object) must still surface
    as a failure -- a truthy `error` is the failure signal regardless of the
    shape of its content, though a falsy one (see the "error": null test
    below) reads as no error at all. Matches #2118's Anthropic convention:
    never silently drop, fall back to a generic message when nothing usable
    is present."""
    captured: dict[str, Any] = {}
    chunks = [{"model": "test-model", "error": "rate limited"}]
    body = b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks)
    _patch_transport_body(monkeypatch, captured, body)
    provider = OpenAIProvider(api_key="test", model="test-model")

    events = _collect_events(provider, ChatConfig())

    assert len(events) == 1
    error = events[0]
    assert isinstance(error, ErrorEvent)
    assert error.code == ""
    assert error.message == "rate limited"


def test_explicit_null_error_on_every_chunk_is_not_misread_as_a_failure(
    monkeypatch: Any,
) -> None:
    """Overshoot guard: several OpenAI-compatible gateways send `"error":
    null` on every chunk of a perfectly normal, successful stream rather
    than omitting the key -- the same explicit-null convention this file's
    own `or []` / `or {}` idiom already exists to handle for `choices` and
    `usage`. A truthiness check must not misread that as a failure, or every
    successful call on such a gateway would be reported as an error."""
    captured: dict[str, Any] = {}
    chunks = [
        {"model": "test-model", "error": None, "choices": [{"delta": {"content": "Hel"}}]},
        {"model": "test-model", "error": None, "choices": [{"delta": {"content": "lo"}}]},
        {
            "model": "test-model",
            "error": None,
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        },
    ]
    body = b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks)
    body += b"data: [DONE]\n\n"
    _patch_transport_body(monkeypatch, captured, body)
    provider = OpenAIProvider(api_key="test", model="test-model")

    events = _collect_events(provider, ChatConfig())

    text = "".join(e.text for e in events if isinstance(e, TextDeltaEvent))
    assert text == "Hello"
    assert not any(isinstance(e, ErrorEvent) for e in events)
    done = next(e for e in events if isinstance(e, DoneEvent))
    assert done.stop_reason == "stop"
