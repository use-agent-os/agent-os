from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from agentos.provider import (
    ChatConfig,
    ContentBlockToolResult,
    ContentBlockToolUse,
    DoneEvent,
    ErrorEvent,
    Message,
    ToolDefinition,
    ToolInputSchema,
    ToolUseEndEvent,
)
from agentos.provider.failures import ProviderFailureKind, classify_provider_error
from agentos.provider.ollama import OllamaProvider, _stream_timeout


def _patch_transport(
    monkeypatch: pytest.MonkeyPatch,
    captured: dict[str, Any],
    response_body: str | bytes,
    *,
    status_code: int = 200,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        if request.content:
            captured["payload"] = json.loads(request.content.decode("utf-8"))
        if isinstance(response_body, bytes):
            return httpx.Response(status_code, content=response_body)
        return httpx.Response(status_code, text=response_body)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("agentos.provider.ollama.httpx.AsyncClient", patched_async_client)


def _tool() -> ToolDefinition:
    return ToolDefinition(
        name="web_search",
        description="Search the web.",
        input_schema=ToolInputSchema(
            properties={"query": {"type": "string"}},
            required=["query"],
        ),
    )


def test_ollama_preserves_multiturn_tool_history(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(
        monkeypatch,
        captured,
        (
            '{"model":"qwen2.5:7b","message":{"role":"assistant",'
            '"content":"Final answer"},"done":false}\n'
            '{"model":"qwen2.5:7b","message":{"role":"assistant",'
            '"content":""},"done":true,"done_reason":"stop",'
            '"prompt_eval_count":12,"eval_count":2}\n'
        ),
    )
    provider = OllamaProvider(model="qwen2.5:7b")
    messages = [
        Message(role="user", content="Find local news"),
        Message(
            role="assistant",
            content=[
                ContentBlockToolUse(
                    id="call_search",
                    name="web_search",
                    input={"query": "local news"},
                )
            ],
        ),
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id="call_search",
                    content='{"results":["one"]}',
                )
            ],
        ),
    ]

    async def _run() -> list[Any]:
        return [event async for event in provider.chat(messages, tools=[_tool()])]

    asyncio.run(_run())

    assert captured["url"] == "http://localhost:11434/api/chat"
    assert captured["payload"]["messages"] == [
        {"role": "user", "content": "Find local news"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_search",
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "arguments": {"query": "local news"},
                    },
                }
            ],
        },
        {
            "role": "tool",
            "content": '{"results":["one"]}',
            "tool_name": "web_search",
        },
    ]


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": "local news"},
        '{"query":"local news"}',
    ],
)
def test_ollama_normalizes_native_tool_arguments_and_done_semantics(
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, str] | str,
) -> None:
    captured: dict[str, Any] = {}
    tool_chunk = {
        "model": "qwen2.5:7b",
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_search",
                    "type": "function",
                    "function": {"name": "web_search", "arguments": arguments},
                }
            ],
        },
        "done": False,
    }
    done_chunk = {
        "model": "qwen2.5:7b",
        "message": {"role": "assistant", "content": ""},
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 8,
        "eval_count": 3,
    }
    _patch_transport(
        monkeypatch,
        captured,
        f"{json.dumps(tool_chunk)}\n{json.dumps(done_chunk)}\n",
    )
    provider = OllamaProvider(model="configured-model")

    async def _run() -> list[Any]:
        return [
            event
            async for event in provider.chat(
                [Message(role="user", content="Search")],
                tools=[_tool()],
            )
        ]

    events = asyncio.run(_run())

    tool_end = next(event for event in events if isinstance(event, ToolUseEndEvent))
    assert tool_end.arguments == {"query": "local news"}
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.stop_reason == "tool_use"
    assert done.model == "qwen2.5:7b"
    assert done.input_tokens == 8
    assert done.output_tokens == 3


def test_ollama_preserves_non_tool_done_reason_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(
        monkeypatch,
        captured,
        (
            '{"model":"qwen2.5:7b","message":{"role":"assistant",'
            '"content":"partial"},"done":false}\n'
            '{"model":"qwen2.5:7b","message":{"role":"assistant",'
            '"content":""},"done":true,"done_reason":"length",'
            '"prompt_eval_count":4,"eval_count":5}\n'
        ),
    )
    provider = OllamaProvider(model="configured-model")

    async def _run() -> list[Any]:
        return [
            event
            async for event in provider.chat(
                [Message(role="user", content="Write")],
                config=ChatConfig(max_tokens=5),
            )
        ]

    events = asyncio.run(_run())

    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.stop_reason == "length"
    assert done.model == "qwen2.5:7b"


def test_ollama_bounds_non_utf8_http_error_body(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(
        monkeypatch,
        captured,
        b"\xff" + b"x" * 3000,
        status_code=502,
    )
    provider = OllamaProvider(model="configured-model")

    async def _run() -> list[Any]:
        return [event async for event in provider.chat([Message(role="user", content="Hi")])]

    events = asyncio.run(_run())

    assert len(events) == 1
    error = events[0]
    assert isinstance(error, ErrorEvent)
    assert error.code == "502"
    assert error.message.startswith("HTTP 502: �")
    assert error.message.endswith("…")
    assert len(error.message) <= 2010


def test_ollama_ignores_malformed_tool_call_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    chunks = (
        '{"message":{"content":"ok","tool_calls":[null,{"function":null},'
        '{"function":{"name":""}}]},"done":false}\n'
        '{"message":null,"done":true,"done_reason":"stop"}\n'
    )
    _patch_transport(monkeypatch, captured, chunks)
    provider = OllamaProvider(model="configured-model")

    async def _run() -> list[Any]:
        return [event async for event in provider.chat([Message(role="user", content="Hi")])]

    events = asyncio.run(_run())

    assert not any(isinstance(event, ToolUseEndEvent) for event in events)
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.stop_reason == "stop"


def _patch_raising_transport(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("agentos.provider.ollama.httpx.AsyncClient", patched_async_client)


def _first_event(provider: OllamaProvider) -> Any:
    async def _run() -> list[Any]:
        return [event async for event in provider.chat([Message(role="user", content="Hi")])]

    events = asyncio.run(_run())
    assert len(events) == 1
    return events[0]


def test_ollama_connect_timeout_is_bounded_below_the_request_timeout() -> None:
    timeout = _stream_timeout(120.0)

    assert timeout.connect == 5.0
    assert timeout.read == 120.0


def test_ollama_connect_timeout_never_exceeds_a_short_request_timeout() -> None:
    assert _stream_timeout(2.0).connect == 2.0


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("All connection attempts failed"),
        httpx.ConnectTimeout("timed out"),
    ],
)
def test_ollama_server_down_names_the_cause(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    _patch_raising_transport(monkeypatch, exc)
    provider = OllamaProvider(model="llama3.1:8b")

    error = _first_event(provider)

    assert isinstance(error, ErrorEvent)
    assert error.code == "connection_error"
    assert "Ollama is not reachable at http://localhost:11434" in error.message
    assert "ollama serve" in error.message
    # The ollama branch of classify_provider_error keys on this wording.
    assert (
        classify_provider_error(
            provider_name="ollama",
            status_code=None,
            raw_code=error.code,
            message=error.message,
        )
        is ProviderFailureKind.TRANSPORT_TRANSIENT
    )


def test_ollama_missing_model_404_says_how_to_pull_it(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(
        monkeypatch,
        captured,
        '{"error":"model \\"llama3.1:8b\\" not found, try pulling it first"}',
        status_code=404,
    )
    provider = OllamaProvider(model="llama3.1:8b")

    error = _first_event(provider)

    assert isinstance(error, ErrorEvent)
    assert error.code == "404"
    assert "ollama pull llama3.1:8b" in error.message
    assert "not found, try pulling it first" in error.message
    assert (
        classify_provider_error(
            provider_name="ollama",
            status_code=404,
            raw_code=error.code,
            message=error.message,
        )
        is ProviderFailureKind.MODEL_NOT_FOUND
    )


def test_ollama_non_model_404_keeps_the_plain_http_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured, "404 page not found", status_code=404)
    provider = OllamaProvider(model="llama3.1:8b")

    error = _first_event(provider)

    assert isinstance(error, ErrorEvent)
    assert error.message == "HTTP 404: 404 page not found"


def test_ollama_list_models_handles_null_details_and_model_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    response_payload = {
        "models": [
            {
                "name": "llama3.2:latest",
                "model": "llama3.2:latest",
                "details": {
                    "parent_model": "",
                    "format": "gguf",
                    "family": "llama",
                    "context_length": 8192,
                },
            },
            {
                # Custom imported or manifest with null details
                "name": "custom-model:latest",
                "details": None,
            },
            {
                # Model using only "model" field and missing "details"
                "model": "qwen2.5:3b",
            },
            {
                # Empty entry that should be skipped
                "name": "",
                "model": "",
                "details": {},
            },
        ]
    }
    _patch_transport(monkeypatch, captured, json.dumps(response_payload))
    provider = OllamaProvider()

    models = asyncio.run(provider.list_models())

    assert len(models) == 3
    assert models[0].model_id == "llama3.2:latest"
    assert models[0].display_name == "llama3.2:latest"
    assert models[0].context_window == 8192
    assert models[0].provider == "ollama"

    assert models[1].model_id == "custom-model:latest"
    assert models[1].display_name == "custom-model:latest"
    assert models[1].context_window == 0
    assert models[1].provider == "ollama"

    assert models[2].model_id == "qwen2.5:3b"
    assert models[2].display_name == "qwen2.5:3b"
    assert models[2].context_window == 0
    assert models[2].provider == "ollama"


def test_ollama_list_models_returns_empty_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_raising_transport(monkeypatch, httpx.ConnectError("Connection refused"))
    provider = OllamaProvider()

    models = asyncio.run(provider.list_models())
    assert models == []
