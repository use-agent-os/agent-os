"""Ollama sends ``think`` and ``num_ctx`` only when they apply (spec P10)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from agentos.engine.types import ThinkingLevel
from agentos.provider import ChatConfig, Message, ModelCapabilities
from agentos.provider.ollama import OllamaProvider

_REAL_ASYNC_CLIENT = httpx.AsyncClient
_DONE = json.dumps({"message": {"role": "assistant", "content": "ok"}, "done": True}) + "\n"


def _payload(monkeypatch: pytest.MonkeyPatch, model: str, cfg: ChatConfig) -> dict[str, Any]:
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, text=_DONE)

    transport = httpx.MockTransport(handler)

    def patched(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr("agentos.provider.ollama.httpx.AsyncClient", patched)
    provider = OllamaProvider(model=model)

    async def run() -> None:
        async for _ in provider.chat([Message(role="user", content="hi")], config=cfg):
            pass

    asyncio.run(run())
    return captured[0]


def test_num_ctx_follows_the_context_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTOS_OLLAMA_NUM_CTX", raising=False)
    sent = _payload(monkeypatch, "llama3", ChatConfig(context_window_tokens=16384))
    unset = _payload(monkeypatch, "llama3", ChatConfig())

    assert sent["options"]["num_ctx"] == 16384
    assert "num_ctx" not in unset["options"]


def test_num_ctx_from_the_window_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """The engine's 200k fallback window must not make Ollama allocate 200k of KV."""
    monkeypatch.delenv("AGENTOS_OLLAMA_NUM_CTX", raising=False)

    sent = _payload(monkeypatch, "llama3", ChatConfig(context_window_tokens=200_000))

    assert sent["options"]["num_ctx"] == 32768


def test_num_ctx_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTOS_OLLAMA_NUM_CTX", "8192")
    smaller = _payload(monkeypatch, "llama3", ChatConfig(context_window_tokens=131072))
    monkeypatch.setenv("AGENTOS_OLLAMA_NUM_CTX", "65536")
    larger = _payload(monkeypatch, "llama3", ChatConfig(context_window_tokens=131072))

    assert smaller["options"]["num_ctx"] == 8192
    assert larger["options"]["num_ctx"] == 65536


@pytest.mark.asyncio
async def test_agent_sends_its_context_window_to_ollama_as_num_ctx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end through the engine: Agent -> ChatConfig -> Ollama payload."""
    from agentos.engine import Agent, AgentConfig

    monkeypatch.delenv("AGENTOS_OLLAMA_NUM_CTX", raising=False)
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, text=_DONE)

    transport = httpx.MockTransport(handler)

    def patched(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr("agentos.provider.ollama.httpx.AsyncClient", patched)
    agent = Agent(
        provider=OllamaProvider(model="llama3"),
        config=AgentConfig(context_window_tokens=24576),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert any(event.kind == "done" for event in events)
    assert captured
    assert captured[0]["options"]["num_ctx"] == 24576


def test_think_is_sent_only_to_thinking_models_with_thinking_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    on = ChatConfig(thinking=True)

    assert _payload(monkeypatch, "qwen3:8b", on)["think"] is True
    assert _payload(monkeypatch, "deepseek-r1:14b", on)["think"] is True
    # Thinking off: a thinking family is told not to think (its default is on).
    assert _payload(monkeypatch, "qwen3:8b", ChatConfig())["think"] is False
    assert _payload(monkeypatch, "qwen3.5:9b", ChatConfig())["think"] is False
    assert "think" not in _payload(monkeypatch, "llama3", ChatConfig())
    assert "think" not in _payload(monkeypatch, "qwen3-coder:30b", ChatConfig())
    # gpt-oss takes a level, not a boolean; leave its default.
    assert "think" not in _payload(monkeypatch, "gpt-oss:20b", ChatConfig())
    # Ollama answers ``think`` for a non-thinking model with a 400.
    assert "think" not in _payload(monkeypatch, "llama3", on)
    assert "think" not in _payload(monkeypatch, "qwen3-coder:30b", on)
    reasoning_caps = ChatConfig(
        thinking=True, model_capabilities=ModelCapabilities(supports_reasoning=True)
    )
    assert _payload(monkeypatch, "my-custom-reasoner", reasoning_caps)["think"] is True


def test_thinking_off_tells_a_capability_only_reasoner_not_to_think(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model that reasons by capability, not by a known family name, thinks by
    default too; with thinking off it must get ``think: false`` or it can answer
    a tool round with reasoning only."""
    off = ChatConfig(model_capabilities=ModelCapabilities(supports_reasoning=True))

    assert _payload(monkeypatch, "my-custom-reasoner", off)["think"] is False


def test_thinking_off_leaves_think_tag_models_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inline ``<think>`` models do not use the native channel either way."""
    tags = ModelCapabilities(supports_reasoning=True, reasoning_format="think_tags")

    assert "think" not in _payload(monkeypatch, "qwen3:8b", ChatConfig(model_capabilities=tags))
    assert "think" not in _payload(
        monkeypatch, "my-custom-reasoner", ChatConfig(model_capabilities=tags)
    )


def test_gpt_oss_takes_a_think_level(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = ChatConfig(thinking=True, thinking_level=ThinkingLevel.HIGH)

    assert _payload(monkeypatch, "gpt-oss:20b", cfg)["think"] == "high"
    assert _payload(monkeypatch, "gpt-oss:20b", ChatConfig(thinking=True))["think"] == "medium"
