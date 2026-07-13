from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest

from agentos.engine.pipeline import TurnContext
from agentos.engine.runtime import TurnRunner
from agentos.engine.steps import agentos_router as agentos_router_step
from agentos.gateway.config import AgentOSRouterConfig, GatewayConfig
from agentos.provider import ChatConfig, Message


class _Provider:
    provider_name = "fake"

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        raise AssertionError("pipeline test should not start provider chat")

    async def list_models(self) -> list[Any]:
        return []


class _SlowHistoryStrategy:
    requires_history = True

    async def classify(
        self,
        message: str,
        valid_tiers: list[str],
        routing_history: list[dict] | None = None,
        **kwargs: object,
    ) -> tuple[str, float, str, dict]:
        time.sleep(0.08)
        return (
            "c2",
            0.95,
            "llm_judge",
            {
                "route_class": "R2",
                "thinking_mode": "T2",
                "prompt_policy": "P1",
            },
        )


class _MutatingHistoryStrategy:
    requires_history = True

    async def classify(
        self,
        message: str,
        valid_tiers: list[str],
        routing_history: list[dict] | None = None,
        **kwargs: object,
    ) -> tuple[str, float, str, dict]:
        time.sleep(0.08)
        assert routing_history
        routing_history[0]["final_tier"] = "poisoned"
        return (
            "c2",
            0.95,
            "llm_judge",
            {
                "route_class": "R2",
                "thinking_mode": "T2",
                "prompt_policy": "P1",
            },
        )


def _config_with_router_timeout() -> GatewayConfig:
    return GatewayConfig(
        agentos_router=AgentOSRouterConfig(routing_timeout_seconds=0.01)
    )


@pytest.mark.asyncio
async def test_agentos_router_timeout_fails_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def slow_router(ctx: TurnContext) -> TurnContext:
        await asyncio.sleep(1.0)
        ctx.model = "should-not-route"
        return ctx

    slow_router.__name__ = "apply_agentos_router"
    monkeypatch.setattr("agentos.engine.steps.apply_agentos_router", slow_router)
    runner = TurnRunner(
        provider_selector=None,
        config=_config_with_router_timeout(),
    )
    provider = _Provider()

    turn, resolved_provider = await asyncio.wait_for(
        runner._run_pipeline(
            "hello",
            "agent:main:test",
            provider,
            None,
            [],
            "system prompt",
            [],
        ),
        timeout=0.25,
    )

    assert resolved_provider is provider
    assert turn.model != "should-not-route"
    router_record = next(
        record
        for record in turn.metadata["pipeline_steps"]
        if record.step_name == "apply_agentos_router"
    )
    assert router_record.applied is False
    assert "timed out" in (router_record.fallback_reason or "")


@pytest.mark.asyncio
async def test_agentos_router_timeout_does_not_late_append_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_key = "agent:main:test-router-timeout-history"
    agentos_router_step._history_store.clear()
    monkeypatch.setattr(
        agentos_router_step,
        "_get_strategy",
        lambda _config, _llm_cfg=None: _SlowHistoryStrategy(),
    )
    runner = TurnRunner(
        provider_selector=None,
        config=_config_with_router_timeout(),
    )
    provider = _Provider()

    turn, resolved_provider = await asyncio.wait_for(
        runner._run_pipeline(
            "hello",
            session_key,
            provider,
            None,
            [],
            "system prompt",
            [],
        ),
        timeout=0.25,
    )
    await asyncio.sleep(0.1)

    assert resolved_provider is provider
    assert agentos_router_step._history_store.get(session_key) is None
    router_record = next(
        record
        for record in turn.metadata["pipeline_steps"]
        if record.step_name == "apply_agentos_router"
    )
    assert router_record.applied is False
    assert "timed out" in (router_record.fallback_reason or "")


@pytest.mark.asyncio
async def test_agentos_router_timeout_does_not_late_mutate_history_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_key = "agent:main:test-router-timeout-history-mutation"
    original_history = [
        {
            "turn_index": 0,
            "_ts": time.monotonic(),
            "text": "previous",
            "final_tier": "c1",
        }
    ]
    agentos_router_step._history_store.clear()
    agentos_router_step._history_store.set(session_key, original_history)
    monkeypatch.setattr(
        agentos_router_step,
        "_get_strategy",
        lambda _config, _llm_cfg=None: _MutatingHistoryStrategy(),
    )
    runner = TurnRunner(
        provider_selector=None,
        config=_config_with_router_timeout(),
    )
    provider = _Provider()

    turn, resolved_provider = await asyncio.wait_for(
        runner._run_pipeline(
            "hello",
            session_key,
            provider,
            None,
            [],
            "system prompt",
            [],
        ),
        timeout=0.25,
    )
    await asyncio.sleep(0.1)

    assert resolved_provider is provider
    stored_history = agentos_router_step._history_store.get(session_key)
    assert stored_history == original_history
    assert stored_history is not None
    assert stored_history[0]["final_tier"] == "c1"
    router_record = next(
        record
        for record in turn.metadata["pipeline_steps"]
        if record.step_name == "apply_agentos_router"
    )
    assert router_record.applied is False
    assert "timed out" in (router_record.fallback_reason or "")


@pytest.mark.asyncio
async def test_agentos_router_timeout_fails_open_for_blocking_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def blocking_router(ctx: TurnContext) -> TurnContext:
        time.sleep(0.08)
        ctx.model = "should-not-route"
        return ctx

    blocking_router.__name__ = "apply_agentos_router"
    monkeypatch.setattr("agentos.engine.steps.apply_agentos_router", blocking_router)
    runner = TurnRunner(
        provider_selector=None,
        config=_config_with_router_timeout(),
    )
    provider = _Provider()
    started = time.monotonic()

    turn, resolved_provider = await asyncio.wait_for(
        runner._run_pipeline(
            "hello",
            "agent:main:test",
            provider,
            None,
            [],
            "system prompt",
            [],
        ),
        timeout=0.25,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 0.075
    assert resolved_provider is provider
    assert turn.model != "should-not-route"
    await asyncio.sleep(0.1)
    assert turn.model != "should-not-route"
    router_record = next(
        record
        for record in turn.metadata["pipeline_steps"]
        if record.step_name == "apply_agentos_router"
    )
    assert router_record.applied is False
    assert "timed out" in (router_record.fallback_reason or "")
