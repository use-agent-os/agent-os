"""Streaming thinking/reasoning delta extraction across providers.

Covers the ``ThinkingDeltaEvent`` contract: Anthropic ``thinking_delta`` SSE
blocks, OpenAI-compatible ``reasoning_content`` deltas, the streaming
``<think>`` tag splitter for models whose reasoning rides inside content, and
the Ollama native ``thinking`` channel. Reasoning must reach subscribers as
typed events and must never leak into user-visible text deltas.
"""

from __future__ import annotations

import asyncio
import json
import random
from typing import Any

import httpx
import pytest

from agentos.provider import (
    ChatConfig,
    DoneEvent,
    Message,
    TextDeltaEvent,
    ThinkingDeltaEvent,
)
from agentos.provider.anthropic import AnthropicProvider
from agentos.provider.ollama import OllamaProvider
from agentos.provider.openai import OpenAIProvider
from agentos.provider.reasoning import ThinkTagStreamSplitter
from agentos.provider.types import ModelCapabilities

# ---------------------------------------------------------------------------
# ThinkTagStreamSplitter
# ---------------------------------------------------------------------------


def _run_splitter(chunks: list[str]) -> tuple[str, str]:
    splitter = ThinkTagStreamSplitter()
    text_parts: list[str] = []
    think_parts: list[str] = []
    for chunk in chunks:
        text, think = splitter.feed(chunk)
        text_parts.append(text)
        think_parts.append(think)
    text, think = splitter.flush()
    text_parts.append(text)
    think_parts.append(think)
    return "".join(text_parts), "".join(think_parts)


def test_splitter_passes_plain_text_through() -> None:
    text, think = _run_splitter(["plain ", "text < not a tag"])
    assert text == "plain text < not a tag"
    assert think == ""


def test_splitter_separates_single_block() -> None:
    text, think = _run_splitter(["hello <think>secret</think> world"])
    assert text == "hello  world"
    assert think == "secret"


def test_splitter_handles_tags_cut_across_chunks() -> None:
    text, think = _run_splitter(["<thi", "nk>only think", "</th", "ink>"])
    assert text == ""
    assert think == "only think"


def test_splitter_routes_unclosed_trailing_think_to_reasoning() -> None:
    text, think = _run_splitter(["before <think>never closed"])
    assert text == "before "
    assert think == "never closed"


def test_splitter_flushes_pending_partial_tag_as_text() -> None:
    # A lone "<th" that never becomes a tag must not be swallowed.
    text, think = _run_splitter(["tail <th"])
    assert text == "tail <th"
    assert think == ""


def test_splitter_is_chunking_invariant() -> None:
    full = "ab<think>c d</think>ef<think>gh</think> tail"
    rng = random.Random(7)
    for _ in range(200):
        cut_count = rng.randint(0, min(9, len(full) - 1))
        cuts = sorted(rng.sample(range(1, len(full)), cut_count))
        chunks, prev = [], 0
        for cut in [*cuts, len(full)]:
            chunks.append(full[prev:cut])
            prev = cut
        text, think = _run_splitter(chunks)
        assert text == "abef tail", chunks
        assert think == "c dgh", chunks


# ---------------------------------------------------------------------------
# Provider streams
# ---------------------------------------------------------------------------


def _patch_transport(
    monkeypatch: pytest.MonkeyPatch,
    module_path: str,
    response_body: bytes,
    *,
    content_type: str = "text/event-stream",
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": content_type},
            content=response_body,
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(f"{module_path}.httpx.AsyncClient", patched_async_client)


def _collect(provider: Any, config: ChatConfig) -> list[Any]:
    async def _run() -> list[Any]:
        return [
            event
            async for event in provider.chat(
                [Message(role="user", content="hi")],
                config=config,
            )
        ]

    return asyncio.run(_run())


def _anthropic_sse(events: list[dict]) -> bytes:
    parts = []
    for ev in events:
        parts.append(f"event: {ev['type']}\n".encode())
        parts.append(f"data: {json.dumps(ev)}\n\n".encode())
    return b"".join(parts)


def test_anthropic_stream_yields_thinking_deltas(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _anthropic_sse(
        [
            {
                "type": "message_start",
                "message": {"id": "msg_1", "model": "claude-fable-5", "usage": {}},
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking"},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "pondering "},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "deeply"},
            },
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "text"},
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "answer"},
            },
            {"type": "message_stop"},
        ]
    )
    _patch_transport(monkeypatch, "agentos.provider.anthropic", body)
    provider = AnthropicProvider(api_key="test-key", model="claude-fable-5")

    events = _collect(provider, ChatConfig())

    thinking = [e.text for e in events if isinstance(e, ThinkingDeltaEvent)]
    text = [e.text for e in events if isinstance(e, TextDeltaEvent)]
    done = next(e for e in events if isinstance(e, DoneEvent))
    assert thinking == ["pondering ", "deeply"]
    assert text == ["answer"]
    assert done.reasoning_content == "pondering deeply"


def _openai_sse(deltas: list[dict], *, finish: str = "stop") -> bytes:
    lines = []
    for delta in deltas:
        chunk = {"choices": [{"delta": delta, "finish_reason": None}]}
        lines.append(f"data: {json.dumps(chunk)}\n\n")
    lines.append(f'data: {json.dumps({"choices": [{"delta": {}, "finish_reason": finish}]})}\n\n')
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def test_openai_compat_reasoning_content_deltas(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _openai_sse(
        [
            {"reasoning_content": "step one; "},
            {"reasoning_content": "step two"},
            {"content": "final answer"},
        ]
    )
    _patch_transport(monkeypatch, "agentos.provider.openai", body)
    provider = OpenAIProvider(api_key="test-key", model="deepseek-reasoner")

    events = _collect(provider, ChatConfig())

    thinking = [e.text for e in events if isinstance(e, ThinkingDeltaEvent)]
    text = [e.text for e in events if isinstance(e, TextDeltaEvent)]
    done = next(e for e in events if isinstance(e, DoneEvent))
    assert thinking == ["step one; ", "step two"]
    assert text == ["final answer"]
    assert done.reasoning_content == "step one; step two"


def test_openai_compat_think_tags_split_across_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _openai_sse(
        [
            {"content": "<thi"},
            {"content": "nk>hidden reasoning</th"},
            {"content": "ink>visible reply"},
        ]
    )
    _patch_transport(monkeypatch, "agentos.provider.openai", body)
    provider = OpenAIProvider(api_key="test-key", model="qwen3-local")
    config = ChatConfig(
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            reasoning_format="think_tags",
        )
    )

    events = _collect(provider, config)

    thinking = "".join(e.text for e in events if isinstance(e, ThinkingDeltaEvent))
    text = "".join(e.text for e in events if isinstance(e, TextDeltaEvent))
    done = next(e for e in events if isinstance(e, DoneEvent))
    assert thinking == "hidden reasoning"
    assert text == "visible reply"
    assert "<think>" not in text
    assert done.reasoning_content == "hidden reasoning"


def test_openai_compat_without_capability_leaves_content_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _openai_sse([{"content": "just <thinking about life> text"}])
    _patch_transport(monkeypatch, "agentos.provider.openai", body)
    provider = OpenAIProvider(api_key="test-key", model="gpt-4o")

    events = _collect(provider, ChatConfig())

    text = "".join(e.text for e in events if isinstance(e, TextDeltaEvent))
    assert text == "just <thinking about life> text"
    assert not any(isinstance(e, ThinkingDeltaEvent) for e in events)


def _ollama_ndjson(chunks: list[dict]) -> bytes:
    return "".join(json.dumps(chunk) + "\n" for chunk in chunks).encode()


def test_ollama_native_thinking_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _ollama_ndjson(
        [
            {"message": {"thinking": "mulling it over"}},
            {"message": {"content": "the reply"}},
            {"done": True, "prompt_eval_count": 3, "eval_count": 5, "done_reason": "stop"},
        ]
    )
    _patch_transport(
        monkeypatch,
        "agentos.provider.ollama",
        body,
        content_type="application/x-ndjson",
    )
    provider = OllamaProvider(model="qwen3")

    events = _collect(provider, ChatConfig())

    thinking = [e.text for e in events if isinstance(e, ThinkingDeltaEvent)]
    text = [e.text for e in events if isinstance(e, TextDeltaEvent)]
    done = next(e for e in events if isinstance(e, DoneEvent))
    assert thinking == ["mulling it over"]
    assert text == ["the reply"]
    assert done.reasoning_content == "mulling it over"


def test_ollama_think_tags_content_split(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _ollama_ndjson(
        [
            {"message": {"content": "<think>local reason"}},
            {"message": {"content": "ing</think>clean answer"}},
            {"done": True, "prompt_eval_count": 1, "eval_count": 2, "done_reason": "stop"},
        ]
    )
    _patch_transport(
        monkeypatch,
        "agentos.provider.ollama",
        body,
        content_type="application/x-ndjson",
    )
    provider = OllamaProvider(model="qwen3")
    config = ChatConfig(
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            reasoning_format="think_tags",
        )
    )

    events = _collect(provider, config)

    thinking = "".join(e.text for e in events if isinstance(e, ThinkingDeltaEvent))
    text = "".join(e.text for e in events if isinstance(e, TextDeltaEvent))
    done = next(e for e in events if isinstance(e, DoneEvent))
    assert thinking == "local reasoning"
    assert text == "clean answer"
    assert done.reasoning_content == "local reasoning"
