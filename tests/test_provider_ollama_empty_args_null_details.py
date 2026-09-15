"""Regression tests for #1950: empty tool arguments and ``details: null``.

Ollama models emit parameterless tool calls with ``function.arguments`` set
to ``null``, ``""`` or ``"null"``. Those normalised to ``{"_raw": ...}`` and
reached the tool as an unexpected ``_raw`` keyword. Separately, an
``/api/tags`` entry whose ``details`` is ``null`` crashed ``list_models`` and
the blanket ``except`` turned every installed model into an empty list.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from agentos.provider import Message, ToolUseEndEvent
from agentos.provider.ollama import OllamaProvider, _normalize_tool_arguments


@pytest.mark.parametrize(
    "arguments",
    [None, "", "   ", "null", "{}", {}],
    ids=["none", "empty", "whitespace", "json-null", "json-empty-object", "dict"],
)
def test_empty_tool_arguments_normalize_to_empty_dict(arguments: Any) -> None:
    assert _normalize_tool_arguments(arguments) == {}


@pytest.mark.parametrize(
    ("arguments", "raw"),
    [
        ("not json", "not json"),
        ("[1, 2]", "[1, 2]"),
        ("42", "42"),
        (7, "7"),
        (["a"], '["a"]'),
        ([], "[]"),
    ],
    ids=["invalid-json", "json-list", "json-number", "int", "list", "empty-list"],
)
def test_non_object_tool_arguments_become_a_string_raw_marker(arguments: Any, raw: str) -> None:
    """Dispatch only refuses a *string* ``_raw``; a native list or number
    left in place would reach the tool as an unexpected keyword."""
    assert _normalize_tool_arguments(arguments) == {"_raw": raw}


def test_parsed_json_object_is_returned() -> None:
    assert _normalize_tool_arguments('{"query": "x"}') == {"query": "x"}


def _patch_transport(monkeypatch: pytest.MonkeyPatch, body: str, *, status_code: int = 200) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text=body)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("agentos.provider.ollama.httpx.AsyncClient", patched_async_client)


@pytest.mark.parametrize("arguments", [None, ""], ids=["null", "empty"])
def test_parameterless_tool_call_streams_empty_arguments(
    monkeypatch: pytest.MonkeyPatch, arguments: Any
) -> None:
    tool_chunk = {
        "model": "qwen2.5:7b",
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_1", "function": {"name": "get_time", "arguments": arguments}}
            ],
        },
        "done": False,
    }
    done_chunk = {
        "model": "qwen2.5:7b",
        "message": {"role": "assistant", "content": ""},
        "done": True,
        "done_reason": "stop",
    }
    _patch_transport(monkeypatch, f"{json.dumps(tool_chunk)}\n{json.dumps(done_chunk)}\n")
    provider = OllamaProvider(model="qwen2.5:7b")

    async def _run() -> list[Any]:
        return [event async for event in provider.chat([Message(role="user", content="time?")])]

    events = asyncio.run(_run())
    tool_end = next(event for event in events if isinstance(event, ToolUseEndEvent))
    assert tool_end.arguments == {}


def test_list_models_tolerates_null_details(monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps(
        {
            "models": [
                {"name": "no-details", "details": None},
                {"name": "with-window", "details": {"context_length": 8192}},
                {"name": "missing-details"},
                {"name": "null-window", "details": {"context_length": None}},
                {"name": "string-window", "details": {"context_length": "4096"}},
            ]
        }
    )
    _patch_transport(monkeypatch, body)
    provider = OllamaProvider(model="no-details")

    models = asyncio.run(provider.list_models())

    assert [m.model_id for m in models] == [
        "no-details",
        "with-window",
        "missing-details",
        "null-window",
        "string-window",
    ]
    assert [m.context_window for m in models] == [0, 8192, 0, 0, 4096]
