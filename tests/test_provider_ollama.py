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
from agentos.provider.ollama import OllamaProvider


def _patch_transport(
    monkeypatch: pytest.MonkeyPatch,
    captured: dict[str, Any],
    response_body: str | bytes,
    *,
    status_code: int = 200,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
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
