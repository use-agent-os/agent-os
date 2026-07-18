"""Router single-model degrade for local providers.

When ``llm.provider`` is local (ollama / lm_studio / ovms) the runtime always
sends requests through that one configured provider — it never builds a
per-tier provider client. So a routed tier whose ``model`` belongs to a
*different* provider (the baked-in cloud defaults point at openrouter models)
hands the local server a model name it does not have → 404.

Degrade rule: when the chosen tier's ``provider`` differs from the configured
local provider, pin the model to ``llm.model`` instead of the tier's model.
The router still runs (classification, thinking-level, prompt policy) — only
the unusable model string is neutralized. A local user who intentionally sets
per-tier models to their *own* provider is honored unchanged.
"""

from __future__ import annotations

import pytest

from agentos.engine.pipeline import TurnContext
from agentos.engine.steps import agentos_router as agentos_router_step
from agentos.engine.steps.agentos_router import apply_agentos_router
from agentos.gateway.config import GatewayConfig


@pytest.fixture(autouse=True)
def reset_agentos_router_state(monkeypatch: pytest.MonkeyPatch):
    agentos_router_step._history_store.clear()
    agentos_router_step._strategy = None
    agentos_router_step._strategy_key = None
    yield
    agentos_router_step._history_store.clear()
    agentos_router_step._strategy = None
    agentos_router_step._strategy_key = None
    monkeypatch.undo()


class _FixedStrategy:
    requires_history = False

    def __init__(self, tier: str) -> None:
        self._tier = tier

    async def classify(self, message, valid_tiers, routing_history=None):
        return self._tier, 0.99, "llm_judge", {}


def _pin_strategy(monkeypatch: pytest.MonkeyPatch, tier: str) -> None:
    monkeypatch.setattr(
        agentos_router_step,
        "_get_strategy",
        lambda _config, _llm_cfg=None: _FixedStrategy(tier),
    )


def _make_context(config: GatewayConfig, message: str = "Do something.") -> TurnContext:
    config.agentos_router.rollout_phase = "full"
    return TurnContext(
        message=message,
        session_key="degrade-session",
        config=config,
        provider=None,
        model=config.llm.model,
        tool_defs=[],
        system_prompt="system",
        raw_message=None,
        attachments=[],
    )


def _ollama_config_with_cloud_tiers() -> GatewayConfig:
    # Local provider, but tiers still carry the baked-in cloud defaults.
    return GatewayConfig(llm={"provider": "ollama", "model": "qwen3.5:2b", "api_key": ""})


@pytest.mark.asyncio
async def test_local_provider_degrades_cloud_tier_model_to_llm_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _ollama_config_with_cloud_tiers()
    # Default c1 tier points at "minimax/minimax-m3" via provider="openrouter".
    _pin_strategy(monkeypatch, "c1")
    ctx = _make_context(config)

    routed = await apply_agentos_router(ctx)

    # Degrade: the local server only knows "qwen3.5:2b", never the cloud model.
    assert routed.model == "qwen3.5:2b"
    assert routed.metadata["applied_model"] == "qwen3.5:2b"
    assert routed.metadata["routing_degraded"] is True
    # The tier is still recorded so thinking/prompt policy for c1 still applies.
    assert routed.metadata["routed_tier"] == "c1"


@pytest.mark.asyncio
async def test_local_provider_honors_matching_provider_tier_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # User intentionally configured real per-tier Ollama models.
    config = GatewayConfig(
        llm={"provider": "ollama", "model": "qwen3.5:2b", "api_key": ""},
        agentos_router={
            "enabled": True,
            "tiers": {
                "c0": {"provider": "ollama", "model": "qwen3.5:2b"},
                "c1": {"provider": "ollama", "model": "qwen3.5:9b"},
                "c2": {"provider": "ollama", "model": "qwen3.5:9b"},
                "c3": {"provider": "ollama", "model": "qwen3.5:latest"},
            },
        },
    )
    _pin_strategy(monkeypatch, "c1")
    ctx = _make_context(config)

    routed = await apply_agentos_router(ctx)

    # Honored: matching-provider tier keeps its own model, no degrade.
    assert routed.model == "qwen3.5:9b"
    assert routed.metadata.get("routing_degraded") is not True
    assert routed.metadata["routed_tier"] == "c1"


@pytest.mark.asyncio
async def test_cloud_provider_applies_tier_model_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # openrouter is not local: full multi-model routing, no degrade.
    config = GatewayConfig(llm={"provider": "openrouter", "api_key": "sk-x"})
    _pin_strategy(monkeypatch, "c1")
    ctx = _make_context(config)

    routed = await apply_agentos_router(ctx)

    assert routed.model == "minimax/minimax-m3"
    assert routed.metadata.get("routing_degraded") is not True


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["ollama", "lm_studio", "ovms", "vllm"])
async def test_every_local_provider_degrades_cloud_tier_model(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    # The full local set — not just ollama — must be covered by the degrade.
    config = GatewayConfig(llm={"provider": provider, "model": "local-model", "api_key": ""})
    _pin_strategy(monkeypatch, "c1")
    ctx = _make_context(config)

    routed = await apply_agentos_router(ctx)

    assert routed.model == "local-model"
    assert routed.metadata["routing_degraded"] is True


@pytest.mark.asyncio
async def test_empty_llm_model_keeps_route_and_does_not_lie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With no llm.model there is nothing safe to pin to: the route must stay
    # unchanged and routing_degraded must NOT claim a degrade that never happened.
    config = GatewayConfig(llm={"provider": "ollama", "model": "", "api_key": ""})
    config.llm.model = ""  # guard against default-model backfill
    _pin_strategy(monkeypatch, "c1")
    ctx = _make_context(config)

    routed = await apply_agentos_router(ctx)

    assert routed.model == "minimax/minimax-m3"
    assert routed.metadata.get("routing_degraded") is not True


@pytest.mark.asyncio
async def test_image_route_degrades_under_local_provider() -> None:
    # The image branch is a separate ctx.model write site with an early return;
    # a cloud image_model tier must degrade for a local provider too.
    config = _ollama_config_with_cloud_tiers()
    ctx = _make_context(config)
    ctx.attachments = [{"type": "image/png", "name": "shot.png"}]

    routed = await apply_agentos_router(ctx)

    assert routed.model == "qwen3.5:2b"
    assert routed.metadata["routing_degraded"] is True
    assert routed.metadata["routed_tier"] == "image_model"


@pytest.mark.asyncio
async def test_router_control_hold_degrades_under_local_provider() -> None:
    # The router-control-hold branch is the third ctx.model write site.
    from agentos.router_control import (
        RouterControlHoldStore,
        resolve_router_control_target,
    )

    config = _ollama_config_with_cloud_tiers()
    ctx = _make_context(config)
    store = RouterControlHoldStore()
    target = resolve_router_control_target(config.agentos_router, "tier:c3")
    store.set_hold(ctx.session_key, target, evidence="hold on c3")
    ctx.metadata["router_control_hold_store"] = store

    routed = await apply_agentos_router(ctx)

    assert routed.metadata["router_control_hold_applied"] is True
    assert routed.model == "qwen3.5:2b"
    assert routed.metadata["routing_degraded"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pinned_tier", "expected_model", "expect_degraded"),
    [
        ("c0", "qwen3.5:2b", False),  # matching-provider tier honored
        ("c1", "qwen3.5:2b", True),  # mismatched tier degraded to llm.model
    ],
)
async def test_mixed_tier_table_degrades_only_mismatched_tiers(
    monkeypatch: pytest.MonkeyPatch,
    pinned_tier: str,
    expected_model: str,
    expect_degraded: bool,
) -> None:
    # A hand-mixed table: one intentional local tier, one leftover cloud tier.
    config = GatewayConfig(
        llm={"provider": "ollama", "model": "qwen3.5:2b", "api_key": ""},
        agentos_router={
            "enabled": True,
            "tiers": {
                "c0": {"provider": "ollama", "model": "qwen3.5:2b"},
                "c1": {"provider": "openrouter", "model": "minimax/minimax-m3"},
            },
        },
    )
    _pin_strategy(monkeypatch, pinned_tier)
    ctx = _make_context(config)

    routed = await apply_agentos_router(ctx)

    assert routed.model == expected_model
    assert routed.metadata.get("routing_degraded", False) is expect_degraded
