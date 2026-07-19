"""Engine-interaction tests for ``PilotStrategy`` through ``apply_agentos_router``.

The engine's dispatch/config does not know ``pilot-v1`` yet (a later task), so
these tests inject a real fixture-backed ``PilotStrategy`` through the step's
existing seam — monkeypatching ``_get_strategy`` — exactly as the v4/judge
engine tests do. Every assertion goes through the FULL ``apply_agentos_router``
step, never ``_finalize_decision`` directly, proving that:

* the ``requires_history=True`` branch is taken for Pilot (guards run, history
  accumulates);
* the deterministic confidence gate can fall back a low-confidence Pilot route;
* a fired safety-net bump survives the confidence gate (spec §4.1 coupling —
  the bumped confidence is the escalation mass ``m >= t_eff >= threshold``);
* anti-downgrade is the engine's, not the strategy's (Pilot output is
  history-invariant; the sticky tier comes from the engine's history walk).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from agentos.agentos_router.pilot import PilotStrategy
from agentos.agentos_router.pilot.features import EMBED_DIM
from agentos.engine.pipeline import TurnContext
from agentos.engine.steps import agentos_router as agentos_router_step
from agentos.engine.steps.agentos_router import apply_agentos_router
from agentos.gateway.config import GatewayConfig

FIXTURE_DIR = Path(__file__).parent.parent / "test_agentos_router" / "data" / "pilot_fixture"


class _ProbEncoder:
    """Deterministic ``PilotEncoder`` returning a fixed raw embedding."""

    def __init__(self, vector: np.ndarray) -> None:
        self._vector = vector

    def encode_sync(self, texts: list[str]) -> np.ndarray:
        return np.asarray([self._vector for _ in texts], dtype=np.float32)

    def count_tokens_pretrunc(self, text: str) -> int:
        return len(text.split())


def _seed_vector_for_argmax(target_argmax: int) -> np.ndarray:
    """A 384-d embedding whose fixture prediction argmaxes to ``target``.

    Only R1/R2 are reachable through the L2-normalised feature path against the
    synthetic fixture (see the strategy contract suite).
    """
    from agentos.agentos_router.pilot.features import build_features
    from agentos.agentos_router.pilot.model import PilotModel

    model = PilotModel(FIXTURE_DIR)
    assert model.available
    for seed in range(400):
        rng = np.random.default_rng(seed)
        vector = rng.standard_normal(EMBED_DIM).astype(np.float32)
        feats = build_features(
            "probe message",
            encoder=_ProbEncoder(vector),
            token_count_pretrunc_8k=3,
        )
        if int(np.argmax(model.predict_proba(feats.reshape(1, -1))[0])) == target_argmax:
            return vector
    raise AssertionError(f"no seed produced argmax {target_argmax}")


@pytest.fixture(autouse=True)
def reset_agentos_router_state(monkeypatch: pytest.MonkeyPatch) -> None:
    agentos_router_step._history_store.clear()
    agentos_router_step._strategy = None
    agentos_router_step._strategy_key = None
    yield
    agentos_router_step._history_store.clear()
    agentos_router_step._strategy = None
    agentos_router_step._strategy_key = None
    monkeypatch.undo()


def _make_context(message: str, *, session_key: str = "test-pilot-session") -> TurnContext:
    config = GatewayConfig()
    config.agentos_router.rollout_phase = "full"
    return TurnContext(
        message=message,
        session_key=session_key,
        config=config,
        provider=None,
        model=config.llm.model,
        tool_defs=[],
        system_prompt="system",
    )


def _inject_pilot(
    monkeypatch: pytest.MonkeyPatch,
    *,
    argmax: int,
    safety_net_threshold: float = 0.5,
    confidence_threshold: float = 0.5,
) -> PilotStrategy:
    strategy = PilotStrategy(
        artifact_dir=FIXTURE_DIR,
        encoder=_ProbEncoder(_seed_vector_for_argmax(argmax)),
        safety_net_threshold=safety_net_threshold,
        confidence_threshold=confidence_threshold,
    )
    assert strategy.source == "pilot_v1"
    monkeypatch.setattr(
        agentos_router_step, "_get_strategy", lambda _config, _llm_cfg=None: strategy
    )
    return strategy


@pytest.mark.asyncio
async def test_pilot_routes_through_full_step(monkeypatch: pytest.MonkeyPatch) -> None:
    """The healthy Pilot route reaches routing metadata via the full step."""
    _inject_pilot(monkeypatch, argmax=2, safety_net_threshold=1.0, confidence_threshold=0.0)
    ctx = _make_context("probe message")
    # Keep the engine's own confidence gate inert so the raw c2 route survives to
    # metadata unmodified (this test is about the route reaching the step, not the
    # gate — the gate has its own test below).
    ctx.config.agentos_router.confidence_threshold = 0.0
    routed = await apply_agentos_router(ctx)

    assert routed.metadata["routing_source"] == "pilot_v1"
    assert routed.metadata["routed_tier"] == "c2"
    # requires_history branch ran: the engine populated final_* and history.
    extra = routed.metadata["routing_extra"]
    assert extra["final_tier"] == "c2"
    assert routed.metadata["routing_history"], "history must accumulate for Pilot"


@pytest.mark.asyncio
async def test_confidence_gate_falls_back_low_confidence_pilot_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A low-confidence R2 route (net not fired) is gated back to default_tier.

    Proves the confidence gate — a ``requires_history`` guard — genuinely runs
    for Pilot: R2 argmax with P(R2) ~0.42 < 0.5 and tier c2 != default c1, so the
    engine falls the final tier back to c1.
    """
    # High safety-net threshold so the net cannot fire and lift confidence to the
    # escalation mass; confidence stays the sub-threshold P(R2).
    _inject_pilot(monkeypatch, argmax=2, safety_net_threshold=1.0, confidence_threshold=0.5)
    ctx = _make_context("probe message")
    ctx.config.agentos_router.default_tier = "c1"

    routed = await apply_agentos_router(ctx)
    extra = routed.metadata["routing_extra"]

    assert routed.metadata["routing_source"] == "pilot_v1"
    assert extra["route_class"] == "R2"
    assert extra["base_tier"] == "c2"
    assert extra["confidence_gate_applied"] is True
    assert extra["final_tier"] == "c1"
    assert routed.metadata["routed_tier"] == "c1"


@pytest.mark.asyncio
async def test_fired_safety_net_bump_survives_confidence_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fired safety-net bump (§4.1) is not undone by the confidence gate.

    R1 argmax with escalation mass m > t_eff: the net fires, route → R2 (c2), and
    confidence is m (>= t_eff >= threshold). So the gate (fires only when
    confidence < threshold) stays inert and the bumped c2 survives.
    """
    _inject_pilot(monkeypatch, argmax=1, safety_net_threshold=0.5, confidence_threshold=0.5)
    ctx = _make_context("probe message")
    ctx.config.agentos_router.default_tier = "c1"

    routed = await apply_agentos_router(ctx)
    extra = routed.metadata["routing_extra"]

    assert extra["route_class"] == "R2", "safety net must bump R1 → R2"
    assert extra["safety_net_applied"] is True
    assert routed.metadata["routing_confidence"] >= 0.5
    assert extra["confidence_gate_applied"] is False
    assert extra["final_tier"] == "c2"
    assert routed.metadata["routed_tier"] == "c2"


@pytest.mark.asyncio
async def test_anti_downgrade_is_engine_owned_not_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """History accumulates across calls and the engine holds the higher tier.

    The strategy output itself is history-invariant (same probs each call); the
    anti-downgrade sticky behavior is produced by the engine's history walk, not
    the strategy. Turn 1 lands c2; turn 2's raw route would also be c2 here, so
    to isolate the engine's sticky behavior we drive turn 2 onto a lower raw tier
    by swapping the injected strategy and assert the engine holds c2.
    """
    # Turn 1: R2 (c2), net off, establishes c2 in history. Keep the engine's own
    # confidence gate inert so this test isolates the anti-downgrade guard.
    _inject_pilot(monkeypatch, argmax=2, safety_net_threshold=1.0, confidence_threshold=0.0)
    session = "test-pilot-anti-downgrade"
    ctx1 = _make_context("probe message", session_key=session)
    ctx1.config.agentos_router.confidence_threshold = 0.0
    routed1 = await apply_agentos_router(ctx1)
    assert routed1.metadata["routed_tier"] == "c2"
    assert routed1.metadata["routing_history"], "turn 1 must record history"

    # Turn 2: inject a strategy whose raw route is the lower c1 (R1 with the net
    # off and a permissive confidence so no gate interferes). The engine's
    # kv-cache anti-downgrade must hold the tier at the previous c2.
    strategy_low = PilotStrategy(
        artifact_dir=FIXTURE_DIR,
        encoder=_ProbEncoder(_seed_vector_for_argmax(1)),
        safety_net_threshold=1.0,  # net off → R1 stays R1 → c1
        confidence_threshold=0.0,
    )
    monkeypatch.setattr(
        agentos_router_step, "_get_strategy", lambda _config, _llm_cfg=None: strategy_low
    )
    ctx2 = _make_context("probe message", session_key=session)
    ctx2.config.agentos_router.confidence_threshold = 0.0
    routed2 = await apply_agentos_router(ctx2)
    extra2 = routed2.metadata["routing_extra"]

    assert extra2["route_class"] == "R1", "turn 2 raw route is the lower R1"
    assert extra2["base_tier"] == "c1"
    assert extra2["anti_downgrade_applied"] is True
    assert extra2["previous_tier"] == "c2"
    assert routed2.metadata["routed_tier"] == "c2", "engine holds the higher prior tier"


@pytest.mark.asyncio
async def test_strategy_output_is_history_invariant_within_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same message across two turns yields identical strategy raw probabilities.

    The engine's history-driven overrides live in ``routing_extra`` (base_tier /
    final_tier), but the strategy's own ``probabilities`` must be identical turn
    to turn — proof the classifier ignores accumulated history.
    """
    _inject_pilot(monkeypatch, argmax=2, safety_net_threshold=1.0, confidence_threshold=0.0)
    session = "test-pilot-invariant"
    routed1 = await apply_agentos_router(_make_context("probe message", session_key=session))
    probs1 = routed1.metadata["routing_extra"]["probabilities"]

    routed2 = await apply_agentos_router(_make_context("probe message", session_key=session))
    probs2 = routed2.metadata["routing_extra"]["probabilities"]

    assert probs1 == probs2, "raw probabilities must not depend on routing history"
