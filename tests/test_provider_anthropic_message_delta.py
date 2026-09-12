"""``message_delta`` handling in ``AnthropicProvider._stream`` (issue #1724).

A ``message_delta`` that omits ``usage`` or ``delta.stop_reason`` must not
regress state already accumulated from an earlier delta, and an explicit
``null`` ``cache_read_input_tokens`` must not crash the stream.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from agentos.provider import ChatConfig, DoneEvent, ErrorEvent, Message
from agentos.provider.anthropic import AnthropicProvider

_MESSAGE_START = {
    "type": "message_start",
    "message": {
        "id": "msg_1",
        "model": "claude-opus-4-7",
        "usage": {"input_tokens": 10, "cache_read_input_tokens": 100},
    },
}
_TOOL_BLOCK = [
    {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read_file"},
    },
    {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "input_json_delta", "partial_json": '{"path": "a"}'},
    },
    {"type": "content_block_stop", "index": 0},
]


def _sse_body(events: list[dict]) -> bytes:
    parts = []
    for ev in events:
        parts.append(f"event: {ev['type']}\n".encode())
        parts.append(f"data: {json.dumps(ev)}\n\n".encode())
    return b"".join(parts)


def _run(monkeypatch: pytest.MonkeyPatch, events: list[dict]) -> DoneEvent:
    body = _sse_body(events)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("agentos.provider.anthropic.httpx.AsyncClient", patched_async_client)
    provider = AnthropicProvider(api_key="test", model="claude-opus-4-7")

    async def _collect() -> DoneEvent:
        done: DoneEvent | None = None
        async for ev in provider.chat([Message(role="user", content="hi")], config=ChatConfig()):
            assert not isinstance(ev, ErrorEvent), ev.message
            if isinstance(ev, DoneEvent):
                done = ev
        assert done is not None
        return done

    return asyncio.run(_collect())


def test_trailing_empty_delta_keeps_tool_use_stop_reason_and_output_tokens(monkeypatch) -> None:
    done = _run(
        monkeypatch,
        [
            _MESSAGE_START,
            *_TOOL_BLOCK,
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 42},
            },
            {"type": "message_delta", "delta": {}},
            {"type": "message_stop"},
        ],
    )
    assert done.stop_reason == "tool_use"
    assert done.output_tokens == 42
    assert done.cached_tokens == 100
    assert done.input_tokens == 110


def test_trailing_usage_only_delta_updates_tokens_but_not_stop_reason(monkeypatch) -> None:
    done = _run(
        monkeypatch,
        [
            _MESSAGE_START,
            *_TOOL_BLOCK,
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 42},
            },
            {"type": "message_delta", "usage": {"output_tokens": 45}},
            {"type": "message_stop"},
        ],
    )
    assert done.stop_reason == "tool_use"
    assert done.output_tokens == 45


def test_explicit_null_stop_reason_and_null_usage_do_not_regress_state(monkeypatch) -> None:
    done = _run(
        monkeypatch,
        [
            _MESSAGE_START,
            *_TOOL_BLOCK,
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 42},
            },
            {"type": "message_delta", "delta": {"stop_reason": None}, "usage": None},
            {"type": "message_stop"},
        ],
    )
    assert done.stop_reason == "tool_use"
    assert done.output_tokens == 42


def test_null_cache_read_input_tokens_does_not_crash_the_stream(monkeypatch) -> None:
    """The Messages API schema types ``cache_read_input_tokens`` as ``integer | null``."""
    done = _run(
        monkeypatch,
        [
            _MESSAGE_START,
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 7, "cache_read_input_tokens": None},
            },
            {"type": "message_stop"},
        ],
    )
    assert done.stop_reason == "end_turn"
    assert done.output_tokens == 7
    assert done.cached_tokens == 100


def test_single_delta_last_write_wins_is_unchanged(monkeypatch) -> None:
    """The normal one-delta stream keeps its exact previous behaviour."""
    done = _run(
        monkeypatch,
        [
            _MESSAGE_START,
            {
                "type": "message_delta",
                "delta": {"stop_reason": "max_tokens"},
                "usage": {"output_tokens": 9, "cache_read_input_tokens": 250},
            },
            {"type": "message_stop"},
        ],
    )
    assert done.stop_reason == "max_tokens"
    assert done.output_tokens == 9
    assert done.cached_tokens == 250
    assert done.input_tokens == 260
