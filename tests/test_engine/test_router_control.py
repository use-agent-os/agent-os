from __future__ import annotations

import copy
import time
from types import SimpleNamespace

import pytest

from agentos.engine.pipeline import TurnContext
from agentos.engine.steps.agentos_router import apply_agentos_router
from agentos.gateway.config import (
    ROUTER_TIER_PROFILE_IDS,
    AgentOSRouterConfig,
    _router_tier_profile_defaults,
)
from agentos.router_control import (
    RouterControlHoldStore,
    RouterControlValidationError,
    build_router_control_targets,
    render_router_control_prompt_block,
    resolve_router_control_target,
)


def _router_cfg(tiers: dict) -> AgentOSRouterConfig:
    return AgentOSRouterConfig(
        enabled=True,
        rollout_phase="full",
        auto_thinking=False,
        tiers=tiers,
        default_tier="c1" if "c1" in tiers else next(iter(tiers)),
    )


def test_router_control_targets_generalize_to_every_profile() -> None:
    for profile in sorted(ROUTER_TIER_PROFILE_IDS):
        tiers = _router_tier_profile_defaults(profile)
        targets = build_router_control_targets(_router_cfg(tiers))
        target_ids = {target.target_id for target in targets}

        for tier_name, tier_cfg in tiers.items():
            if tier_cfg.get("image_only"):
                assert f"tier:{tier_name}" not in target_ids
                continue
            assert f"tier:{tier_name}" in target_ids


def test_model_targets_are_rejected_by_local_validation() -> None:
    cfg = _router_cfg(
        {
            "c0": {"provider": "openrouter", "model": "same/model", "supports_image": False},
            "c1": {"provider": "openrouter", "model": "other/model", "supports_image": False},
            "c3": {"provider": "openrouter", "model": "same/model", "supports_image": False},
        }
    )

    with pytest.raises(RouterControlValidationError):
        resolve_router_control_target(cfg, "model:same/model")


def test_natural_language_aliases_are_rejected_by_local_validation() -> None:
    cfg = _router_cfg(_router_tier_profile_defaults("openrouter"))

    with pytest.raises(RouterControlValidationError):
        resolve_router_control_target(cfg, "Claude Opus 4.7")


def test_legacy_tier_target_aliases_resolve_to_canonical_routes() -> None:
    cfg = _router_cfg(_router_tier_profile_defaults("openrouter"))

    target = resolve_router_control_target(cfg, "tier:t3")

    assert target.target_id == "tier:c3"
    assert target.tier == "c3"


def test_hold_store_expires_by_explicit_turn_count_and_sliding_idle_time() -> None:
    cfg = _router_cfg(_router_tier_profile_defaults("openrouter"))
    target = resolve_router_control_target(cfg, "tier:c3")
    store = RouterControlHoldStore()
    store.set_hold(
        "agent:main:test",
        target,
        evidence="use c3",
        now_monotonic=100.0,
        turns_remaining=1,
        ttl_seconds=10.0,
    )

    first = store.get_valid("agent:main:test", now_monotonic=101.0, decrement=True)
    assert first is not None
    assert first.tier == "c3"
    assert store.get_valid("agent:main:test", now_monotonic=102.0) is None

    store.set_hold(
        "agent:main:test",
        target,
        evidence="use c3 again",
        now_monotonic=100.0,
        ttl_seconds=10.0,
    )
    assert store.get_valid("agent:main:test", now_monotonic=109.0, decrement=True) is not None
    assert store.get_valid("agent:main:test", now_monotonic=118.0) is not None
    assert store.get_valid("agent:main:test", now_monotonic=119.0) is None


def test_hold_store_get_valid_is_atomic_against_concurrent_set_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding #1: the router step runs inside ``asyncio.to_thread`` where
    ``get_valid(decrement=True)`` mutates the shared store (popping an expired /
    turn-exhausted hold), while the ``/c0``-``/c3`` slash commands call
    ``set_hold`` on the event-loop thread. ``__deepcopy__`` shares the identical
    instance across both threads, so ``get_valid``'s check-then-act (read the
    hold, decide it is stale, then pop) must run under the store's lock: a
    ``set_hold`` that lands *between* the read and the pop would otherwise be
    silently clobbered by the pop, losing the freshly installed tier pin.

    We widen the check-then-act window by making ``is_expired`` sleep, then
    install a durable pin from another thread while a decrementing read is mid
    check. With the lock, the pin survives every iteration; without it (verified
    empirically), the pop deletes the fresh pin and this asserts ``None``.
    """
    import threading

    from agentos import router_control

    cfg = _router_cfg(_router_tier_profile_defaults("openrouter"))
    stale_target = resolve_router_control_target(cfg, "tier:c0")
    pin_target = resolve_router_control_target(cfg, "tier:c3")

    real_is_expired = router_control.RouterControlHold.is_expired
    slow_ids: set[int] = set()

    def slow_is_expired(self: router_control.RouterControlHold, now: float):
        if id(self) in slow_ids:
            # Widen the read->pop window so a racing set_hold can interleave.
            time.sleep(0.01)
        return real_is_expired(self, now)

    monkeypatch.setattr(
        router_control.RouterControlHold, "is_expired", slow_is_expired
    )

    for _ in range(15):
        store = RouterControlHoldStore()
        # An already-expired, turn-exhausted hold: get_valid will decide to pop it.
        stale = store.set_hold(
            "agent:main:race",
            stale_target,
            evidence="stale",
            now_monotonic=0.0,
            turns_remaining=0,
            ttl_seconds=1.0,
        )
        slow_ids.clear()
        slow_ids.add(id(stale))

        def _worker() -> None:
            store.get_valid("agent:main:race", now_monotonic=100.0, decrement=True)

        worker = threading.Thread(target=_worker)
        worker.start()
        time.sleep(0.002)  # let the worker enter is_expired's slow window
        # Install a durable pin concurrently with the worker's pop.
        store.set_hold(
            "agent:main:race",
            pin_target,
            evidence="pin",
            now_monotonic=100.0,
            turns_remaining=5,
            ttl_seconds=1000.0,
        )
        worker.join()

        survived = store.get_valid("agent:main:race", now_monotonic=100.0)
        assert survived is not None, "concurrent set_hold pin was clobbered by a racing pop"
        assert survived.tier == "c3"


def test_hold_store_deepcopy_preserves_session_identity_for_ttl_refresh() -> None:
    cfg = _router_cfg(_router_tier_profile_defaults("openrouter"))
    target = resolve_router_control_target(cfg, "tier:c3")
    store = RouterControlHoldStore()
    store.set_hold(
        "agent:main:test",
        target,
        evidence="use c3",
        now_monotonic=100.0,
        ttl_seconds=10.0,
    )

    copied = copy.deepcopy(store)

    assert copied is store
    assert copied.get_valid("agent:main:test", now_monotonic=109.0, decrement=True) is not None
    assert store.get_valid("agent:main:test", now_monotonic=118.0) is not None
    assert store.get_valid("agent:main:test", now_monotonic=119.0) is None


@pytest.mark.asyncio
async def test_agentos_router_refreshes_hold_idle_ttl_through_copied_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _router_cfg(_router_tier_profile_defaults("openrouter"))
    target = resolve_router_control_target(cfg, "tier:c3")
    store = RouterControlHoldStore()
    store.set_hold(
        "agent:main:test-refresh",
        target,
        evidence="use c3",
        now_monotonic=100.0,
        ttl_seconds=10.0,
    )
    now = [109.0]
    monkeypatch.setattr("agentos.router_control.time.monotonic", lambda: now[0])

    metadata = copy.deepcopy({"router_control_hold_store": store})
    ctx = TurnContext(
        message="continue this",
        session_key="agent:main:test-refresh",
        config=SimpleNamespace(agentos_router=cfg),
        provider=None,
        model="default-model",
        tool_defs=[],
        system_prompt="system",
        metadata=metadata,
    )

    out = await apply_agentos_router(ctx)

    assert out.metadata["routing_source"] == "router_control_hold"
    now[0] = 118.0
    assert store.get_valid("agent:main:test-refresh") is not None
    now[0] = 119.0
    assert store.get_valid("agent:main:test-refresh") is None


@pytest.mark.asyncio
async def test_agentos_router_applies_hold_before_normal_classification(monkeypatch) -> None:
    cfg = _router_cfg(_router_tier_profile_defaults("openrouter"))
    target = resolve_router_control_target(cfg, "tier:c3")
    store = RouterControlHoldStore()
    store.set_hold("agent:main:test", target, evidence="use c3")

    def fail_strategy(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("router classification should not run while hold is valid")

    monkeypatch.setattr("agentos.engine.steps.agentos_router._get_strategy", fail_strategy)
    ctx = TurnContext(
        message="review this",
        session_key="agent:main:test",
        config=SimpleNamespace(agentos_router=cfg),
        provider=None,
        model="default-model",
        tool_defs=[],
        system_prompt="system",
        metadata={"router_control_hold_store": store},
    )

    out = await apply_agentos_router(ctx)

    assert out.model == "anthropic/claude-opus-4.8"
    assert out.metadata["routing_source"] == "router_control_hold"
    assert out.metadata["router_control_hold_applied"] is True
    assert out.metadata["router_control_target_tier"] == "c3"


@pytest.mark.asyncio
async def test_image_attachments_bypass_text_hold(monkeypatch) -> None:
    cfg = _router_cfg(_router_tier_profile_defaults("openrouter"))
    target = resolve_router_control_target(cfg, "tier:c3")
    store = RouterControlHoldStore()
    store.set_hold("agent:main:test-image", target, evidence="use c3")

    def fail_strategy(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("image route should not classify")

    monkeypatch.setattr("agentos.engine.steps.agentos_router._get_strategy", fail_strategy)
    ctx = TurnContext(
        message="what is in this image?",
        session_key="agent:main:test-image",
        config=SimpleNamespace(agentos_router=cfg),
        provider=None,
        model="default-model",
        tool_defs=[],
        system_prompt="system",
        attachments=[{"mime": "image/png"}],
        metadata={"router_control_hold_store": store},
    )

    out = await apply_agentos_router(ctx)

    assert out.metadata["routing_source"] == "image_route"
    assert out.metadata.get("router_control_hold_applied") is not True
    assert out.model == "minimax/minimax-m3"


@pytest.mark.asyncio
async def test_agentos_router_does_not_refresh_ttl_for_inapplicable_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hold to a tier that is no longer a valid text tier must not have its
    idle TTL refreshed — otherwise a hold that can never take effect is kept
    alive indefinitely while the session keeps sending turns, defeating
    TTL-based reclamation. Here c3 is turned ``image_only`` after the hold was
    pinned, so the hold falls through to normal classification and its
    ``last_activity`` must stay pinned to the set time.
    """
    tiers = _router_tier_profile_defaults("openrouter")
    # Pin the hold against a config where c3 is still a valid text tier.
    cfg_before = _router_cfg(tiers)
    target = resolve_router_control_target(cfg_before, "tier:c3")
    store = RouterControlHoldStore()
    store.set_hold(
        "agent:main:stale-hold",
        target,
        evidence="use c3",
        now_monotonic=100.0,
        ttl_seconds=10.0,
    )

    # c3 is subsequently removed from the valid text tiers (turned image_only),
    # so the hold can never be applied.
    image_only_tiers = copy.deepcopy(tiers)
    image_only_tiers["c3"]["image_only"] = True
    cfg = _router_cfg(image_only_tiers)

    class _StubStrategy:
        async def classify(self, *_args: object, **_kwargs: object):
            return "c1", 1.0, "llm_judge", {}

    monkeypatch.setattr(
        "agentos.engine.steps.agentos_router._get_strategy",
        lambda *_a, **_k: _StubStrategy(),
    )

    now = [105.0]
    monkeypatch.setattr("agentos.router_control.time.monotonic", lambda: now[0])

    ctx = TurnContext(
        message="continue this",
        session_key="agent:main:stale-hold",
        config=SimpleNamespace(agentos_router=cfg, llm=None),
        provider=None,
        model="default-model",
        tool_defs=[],
        system_prompt="system",
        metadata={"router_control_hold_store": store},
    )

    out = await apply_agentos_router(ctx)

    # Hold was NOT applied — classification took over.
    assert out.metadata["routing_source"] != "router_control_hold"
    assert out.metadata.get("router_control_hold_applied") is not True

    # The hold's idle TTL was not refreshed: last_activity stays at 100.0, so it
    # expires 10s after that (>= 110.0), NOT 10s after the turn at 105.0.
    now[0] = 110.0
    assert store.get_valid("agent:main:stale-hold") is None


def test_prompt_block_contains_canonical_targets_not_aliases() -> None:
    cfg = _router_cfg(_router_tier_profile_defaults("openrouter"))

    block = render_router_control_prompt_block(cfg)

    assert "router_control" in block
    assert "tier:c3" in block
    assert "tier:t3" not in block
    assert "model:anthropic/claude-opus-4.8" not in block
    assert "description" not in block
    assert "must choose one target_id exactly" in block
