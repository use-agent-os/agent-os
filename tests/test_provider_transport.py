"""Provider transport: pooled client, split timeouts, stream-idle watchdog,
retry of a stream cut inside a tool call. All offline (httpx.MockTransport)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from agentos.provider import (
    ChatConfig,
    DoneEvent,
    ErrorEvent,
    Message,
    ProviderHeartbeatEvent,
    TextDeltaEvent,
    ToolDefinition,
    ToolInputSchema,
    ToolUseDeltaEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
)
from agentos.provider.anthropic import AnthropicProvider
from agentos.provider.failures import (
    ProviderFailureKind,
    ProviderRecoveryAction,
    classify_provider_error,
    decide_recovery_action,
    is_transport_timeout,
)
from agentos.provider.ollama import OllamaProvider
from agentos.provider.openai import OpenAIProvider
from agentos.provider.transport import (
    INCOMPLETE_TOOL_STREAM_CODE,
    STREAM_IDLE_TIMEOUT_CODE,
    STREAM_INTERRUPTED_AFTER_OUTPUT_CODE,
    resolve_stream_idle_timeout,
    stream_timeouts,
)

Body = bytes | Callable[[], AsyncIterator[bytes]]


def _sse(*events: dict[str, Any]) -> bytes:
    return b"".join(f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n".encode() for ev in events)


_START = {"type": "message_start", "message": {"usage": {"input_tokens": 3}}}
_TOOL_START = {
    "type": "content_block_start",
    "index": 1,
    "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {}},
}


def _json_delta(fragment: str) -> dict[str, Any]:
    return {
        "type": "content_block_delta",
        "index": 1,
        "delta": {"type": "input_json_delta", "partial_json": fragment},
    }


_TEXT = (
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Let me"}},
    {"type": "content_block_stop", "index": 0},
)
_CUT_TOOL = _sse(_START, _TOOL_START, _json_delta('{"pa'))
_CUT_TOOL_AFTER_TEXT = _sse(_START, *_TEXT, _TOOL_START, _json_delta('{"pa'))
_FULL_TOOL = _sse(
    _START,
    _TOOL_START,
    _json_delta('{"path": '),
    _json_delta('"a"}'),
    {"type": "content_block_stop", "index": 1},
    {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 5}},
    {"type": "message_stop"},
)
_END_TURN = _sse(
    _START,
    *_TEXT,
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
    {"type": "message_stop"},
)

_TOOLS = [
    ToolDefinition(
        name="read_file",
        description="Read.",
        input_schema=ToolInputSchema(properties={"path": {"type": "string"}}),
    )
]


class _Server:
    """Serves queued bodies and counts requests and client constructions."""

    def __init__(self, bodies: list[Body], default: bytes = _END_TURN) -> None:
        self._bodies = list(bodies)
        self._default = default
        self.requests: list[httpx.Request] = []
        self.clients_built = 0
        self.timeouts: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.timeouts.append(dict(request.extensions.get("timeout") or {}))
        body = self._bodies.pop(0) if self._bodies else self._default
        content: Any = body() if callable(body) else body
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        transport = httpx.MockTransport(self.handler)
        real_async_client = httpx.AsyncClient

        def patched(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            self.clients_built += 1
            kwargs["transport"] = transport
            return real_async_client(*args, **kwargs)

        monkeypatch.setattr("agentos.provider.transport.httpx.AsyncClient", patched)


async def _events(provider: Any, cfg: ChatConfig | None = None, tools: Any = _TOOLS) -> list[Any]:
    return [
        ev
        async for ev in provider.chat(
            [Message(role="user", content="go")], tools=tools, config=cfg or ChatConfig()
        )
    ]


# --- pooled client --------------------------------------------------------


def test_pooled_client_is_reused_across_requests_and_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _Server([])
    server.install(monkeypatch)
    provider = AnthropicProvider(api_key="k", model="claude-opus-5-5")

    async def run() -> None:
        await _events(provider)
        await _events(provider)
        await asyncio.gather(_events(provider), _events(provider), _events(provider))
        await provider.aclose()

    asyncio.run(run())

    assert len(server.requests) == 5
    assert server.clients_built == 1


def test_pooled_client_is_rebuilt_for_another_event_loop_and_after_aclose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _Server([])
    server.install(monkeypatch)
    provider = OpenAIProvider(api_key="k", model="m", base_url="https://api.example.com/v1")

    asyncio.run(_events(provider))
    asyncio.run(_events(provider))

    async def close_then_use() -> None:
        await provider.aclose()
        await _events(provider)
        async with provider:
            await _events(provider)

    asyncio.run(close_then_use())

    assert server.clients_built == 3


def test_requests_carry_split_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _Server([])
    server.install(monkeypatch)
    provider = AnthropicProvider(api_key="k", model="claude-opus-5-5")

    asyncio.run(_events(provider, ChatConfig(timeout=120.0)))

    assert server.timeouts[0] == {"connect": 12.0, "read": 120.0, "write": 30.0, "pool": 30.0}


def test_stream_timeouts_env_overrides_and_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTOS_LLM_STREAM_CONNECT_TIMEOUT_SECONDS", "3")
    monkeypatch.setenv("AGENTOS_LLM_STREAM_WRITE_TIMEOUT_SECONDS", "45")

    assert stream_timeouts(120.0).as_dict() == {
        "connect": 3.0,
        "read": 120.0,
        "write": 45.0,
        "pool": 30.0,
    }
    short = stream_timeouts(5.0)
    assert (short.connect, short.write, short.pool) == (3.0, 5.0, 5.0)


def test_stream_idle_timeout_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTOS_LLM_STREAM_IDLE_TIMEOUT_SECONDS", raising=False)
    assert resolve_stream_idle_timeout(None) == 120.0
    monkeypatch.setenv("AGENTOS_LLM_STREAM_IDLE_TIMEOUT_SECONDS", "0")
    assert resolve_stream_idle_timeout(None) is None
    assert resolve_stream_idle_timeout(5) == 5.0


# --- stream-idle watchdog -------------------------------------------------


def _stalling(first: bytes, *, stall_s: float = 5.0) -> Callable[[], AsyncIterator[bytes]]:
    async def body() -> AsyncIterator[bytes]:
        yield first
        # A gateway keep-alive comment is not progress.
        yield b": keep-alive\n\n"
        await asyncio.sleep(stall_s)
        yield b""

    return body


def test_stream_idle_timeout_raises_a_retriable_classified_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _Server([_stalling(_sse(_START))])
    server.install(monkeypatch)
    provider = AnthropicProvider(api_key="k", model="claude-opus-5-5")

    events = asyncio.run(_events(provider, ChatConfig(stream_idle_timeout=0.05)))

    error = events[-1]
    assert isinstance(error, ErrorEvent)
    assert error.code == STREAM_IDLE_TIMEOUT_CODE
    kind = classify_provider_error("anthropic", None, error.code, error.message)
    assert kind is ProviderFailureKind.TRANSPORT_TRANSIENT
    assert decide_recovery_action(kind) is ProviderRecoveryAction.RETRY_THEN_FALLBACK
    # Not a hard request timeout: the engine's tighter timeout-retry cap does
    # not apply to a stall that happened mid-stream.
    assert not is_transport_timeout(error.code, error.message)
    for provider_name in ("openrouter", "openai", "ollama"):
        assert (
            classify_provider_error(provider_name, None, error.code, error.message)
            is ProviderFailureKind.TRANSPORT_TRANSIENT
        )


def test_stream_idle_timeout_after_text_is_surfaced_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _Server([_stalling(_sse(_START, *_TEXT))])
    server.install(monkeypatch)
    provider = AnthropicProvider(api_key="k", model="claude-opus-5-5")

    events = asyncio.run(_events(provider, ChatConfig(stream_idle_timeout=0.05)))

    assert [e.text for e in events if isinstance(e, TextDeltaEvent)] == ["Let me"]
    error = events[-1]
    assert isinstance(error, ErrorEvent)
    assert error.code == STREAM_INTERRUPTED_AFTER_OUTPUT_CODE
    kind = classify_provider_error("anthropic", None, error.code, error.message)
    assert decide_recovery_action(kind) is ProviderRecoveryAction.SURFACE


def test_idle_watchdog_waits_for_the_first_data_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """A slow first token (prefill, silent reasoning) is not an idle stream."""

    async def slow_first_token() -> AsyncIterator[bytes]:
        yield b": OPENROUTER PROCESSING\n\n"
        await asyncio.sleep(0.15)
        yield _END_TURN

    server = _Server([slow_first_token])
    server.install(monkeypatch)
    provider = AnthropicProvider(api_key="k", model="claude-opus-5-5")

    events = asyncio.run(_events(provider, ChatConfig(stream_idle_timeout=0.05)))

    assert isinstance(events[-1], DoneEvent)


def test_ollama_stream_idle_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    line = json.dumps({"message": {"role": "assistant", "content": ""}, "done": False})
    server = _Server([_stalling(line.encode() + b"\n")])
    server.install(monkeypatch)
    provider = OllamaProvider(model="llama3")

    events = asyncio.run(_events(provider, ChatConfig(stream_idle_timeout=0.05), tools=None))

    assert isinstance(events[-1], ErrorEvent)
    assert events[-1].code == STREAM_IDLE_TIMEOUT_CODE


# --- incomplete tool stream -----------------------------------------------


def test_incomplete_tool_stream_is_retried_once_transparently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _Server([_CUT_TOOL, _FULL_TOOL])
    server.install(monkeypatch)
    provider = AnthropicProvider(api_key="k", model="claude-opus-5-5")

    events = asyncio.run(_events(provider))

    assert len(server.requests) == 2
    tool_events = [
        e for e in events if isinstance(e, ToolUseStartEvent | ToolUseDeltaEvent | ToolUseEndEvent)
    ]
    # Only the complete attempt's call reaches the caller, once.
    assert [type(e).__name__ for e in tool_events] == [
        "ToolUseStartEvent",
        "ToolUseDeltaEvent",
        "ToolUseDeltaEvent",
        "ToolUseEndEvent",
    ]
    assert tool_events[-1].arguments == {"path": "a"}
    assert isinstance(events[-1], DoneEvent)
    phases = [e.phase for e in events if isinstance(e, ProviderHeartbeatEvent)]
    assert "llm_retry" in phases
    assert "llm_tool_arguments" in phases


def test_incomplete_tool_stream_retries_only_once(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _Server([_CUT_TOOL, _CUT_TOOL, _FULL_TOOL])
    server.install(monkeypatch)
    provider = AnthropicProvider(api_key="k", model="claude-opus-5-5")

    events = asyncio.run(_events(provider))

    assert len(server.requests) == 2
    assert not any(isinstance(e, ToolUseStartEvent) for e in events)
    error = events[-1]
    assert isinstance(error, ErrorEvent)
    assert error.code == INCOMPLETE_TOOL_STREAM_CODE
    # Tool names are model/MCP-supplied text: logged, never in the error text.
    assert "read_file" not in error.message
    kind = classify_provider_error("anthropic", None, error.code, error.message)
    assert decide_recovery_action(kind) is ProviderRecoveryAction.SURFACE


def test_incomplete_tool_stream_after_text_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _Server([_CUT_TOOL_AFTER_TEXT, _FULL_TOOL])
    server.install(monkeypatch)
    provider = AnthropicProvider(api_key="k", model="claude-opus-5-5")

    events = asyncio.run(_events(provider))

    assert len(server.requests) == 1
    assert [e.text for e in events if isinstance(e, TextDeltaEvent)] == ["Let me"]
    assert not any(isinstance(e, ToolUseStartEvent) for e in events)
    assert isinstance(events[-1], ErrorEvent)
    assert events[-1].code == STREAM_INTERRUPTED_AFTER_OUTPUT_CODE


def _openai_sse(chunks: list[dict[str, Any]], *, done: bool) -> bytes:
    body = b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks)
    return body + (b"data: [DONE]\n\n" if done else b"")


def _openai_tool_chunk(arguments: str) -> dict[str, Any]:
    return {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "function": {"name": "read_file", "arguments": arguments},
                        }
                    ]
                },
                "finish_reason": None,
            }
        ]
    }


def test_openai_stream_cut_inside_tool_arguments_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cut = _openai_sse([_openai_tool_chunk('{"pa')], done=False)
    full = _openai_sse(
        [
            _openai_tool_chunk('{"path": "a"}'),
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ],
        done=True,
    )
    server = _Server([cut, full])
    server.install(monkeypatch)
    provider = OpenAIProvider(api_key="k", model="m", base_url="https://api.example.com/v1")

    events = asyncio.run(_events(provider))

    assert len(server.requests) == 2
    ends = [e for e in events if isinstance(e, ToolUseEndEvent)]
    assert len(ends) == 1
    assert ends[0].arguments == {"path": "a"}
    assert isinstance(events[-1], DoneEvent)


def test_openai_complete_stream_without_done_marker_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid arguments and no [DONE]: the call runs as before, no retry."""
    body = _openai_sse([_openai_tool_chunk('{"path": "a"}')], done=False)
    server = _Server([body])
    server.install(monkeypatch)
    provider = OpenAIProvider(api_key="k", model="m", base_url="https://api.example.com/v1")

    events = asyncio.run(_events(provider))

    assert len(server.requests) == 1
    assert [e.arguments for e in events if isinstance(e, ToolUseEndEvent)] == [{"path": "a"}]
    assert isinstance(events[-1], DoneEvent)


# --- tool names never steer the engine's error-text fallbacks ---------------


def _cut_named_tool(name: str, *, after_text: bool) -> bytes:
    start = {
        "type": "content_block_start",
        "index": 1,
        "content_block": {"type": "tool_use", "id": "toolu_x", "name": name, "input": {}},
    }
    prefix = _TEXT if after_text else ()
    return _sse(_START, *prefix, start, _json_delta('{"thou'))


@pytest.mark.parametrize("after_text", [True, False])
def test_stream_guard_error_text_never_carries_tool_names(after_text: bool) -> None:
    from agentos.provider.transport import guard_tool_stream

    async def cut() -> AsyncIterator[Any]:
        if after_text:
            yield TextDeltaEvent(text="Let me think")
        yield ToolUseStartEvent(tool_use_id="t1", tool_name="mcp_reasoning_sequentialthinking")
        yield ToolUseDeltaEvent(tool_use_id="t1", json_fragment='{"thou')

    async def run() -> list[Any]:
        return [e async for e in guard_tool_stream(cut, provider="p", model="m")]

    error = asyncio.run(run())[-1]

    assert isinstance(error, ErrorEvent)
    assert "thinking" not in error.message.lower()
    assert "reasoning" not in error.message.lower()


@pytest.mark.asyncio
async def test_cut_thinking_named_tool_does_not_trigger_the_thinking_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An MCP tool called ``sequentialthinking`` cut mid-stream after text must
    not make the engine disable thinking and re-stream text already shown."""
    from agentos.engine import Agent, AgentConfig

    server = _Server(
        [_cut_named_tool("sequentialthinking", after_text=True)] * 3, default=_END_TURN
    )
    server.install(monkeypatch)
    agent = Agent(
        provider=AnthropicProvider(api_key="k", model="claude-opus-5-5"),
        config=AgentConfig(
            thinking=True, max_provider_retries=2, retry_base_backoff_ms=0, retry_max_backoff_ms=0
        ),
    )
    tools = [
        ToolDefinition(
            name="sequentialthinking",
            description="Think step by step.",
            input_schema=ToolInputSchema(properties={"thought": {"type": "string"}}),
        )
    ]
    agent.tool_definitions = tools  # type: ignore[misc]

    events = [event async for event in agent.run_turn("hello")]

    assert len(server.requests) == 1
    texts = [e.text for e in events if getattr(e, "kind", "") == "text_delta"]
    assert texts == ["Let me"]
    assert not any(
        getattr(e, "kind", "") == "warning" and "thinking" in (getattr(e, "code", "") or "")
        for e in events
    )


# --- router judge: per-call loops must not leak sockets ---------------------


def _judge_sse() -> bytes:
    arguments = json.dumps({"route_class": "R1", "confidence": 0.9, "reason": "test"})
    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "function": {"name": "emit_route", "arguments": arguments},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    # No ``[DONE]``: the body is read to its end, so the connection goes back
    # to the pool open (keep-alive) instead of being closed by an early break.
    return b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks)


def test_router_judge_on_per_call_loops_leaks_no_sockets() -> None:
    """The judge pattern: one provider, a fresh ``asyncio.run`` per turn, real
    sockets. Every client must be closed on its own loop: no ResourceWarning."""
    import gc
    import threading
    import warnings
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from types import SimpleNamespace

    from agentos.agentos_router.llm_judge import LLMJudgeStrategy

    body = _judge_sse()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            length = int(self.headers.get("content-length") or 0)
            self.rfile.read(length)
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: Any) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    try:
        router_cfg = SimpleNamespace(
            tiers={},
            default_tier="c1",
            judge_model="local-judge",
            judge_base_url=base_url,
            judge_api_key="local",
            judge_short_circuit_enabled=False,
            routing_timeout_seconds=15.0,
        )
        llm_cfg = SimpleNamespace(provider="", api_key="", api_key_env="", base_url="")
        strategy = LLMJudgeStrategy(
            router_cfg=router_cfg,
            llm_cfg=llm_cfg,
            provider_factory=lambda _p, model, key, url: OpenAIProvider(
                api_key=key or "local", model=model, base_url=url or base_url
            ),
        )
        # Collect garbage that earlier tests in this process left behind first;
        # the gc.collect() below would otherwise report their unclosed objects
        # (e.g. anyio memory streams) as this test's leaks.
        gc.collect()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            results = [
                asyncio.run(strategy.classify("refactor the parser", ["c0", "c1", "c2", "c3"]))
                for _ in range(3)
            ]
            del strategy
            gc.collect()
    finally:
        server.shutdown()
        server.server_close()

    assert [source for _tier, _conf, source, _extra in results] == ["llm_judge"] * 3
    leaks = [w for w in caught if issubclass(w.category, ResourceWarning)]
    assert leaks == [], [str(w.message) for w in leaks]
