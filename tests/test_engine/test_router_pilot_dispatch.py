"""Dispatch + cache-key surfaces for the ``pilot-v1`` strategy.

These exercise the real engine seam (``_get_strategy`` / ``_strategy_cache_key``)
— not a mock of it — proving that a config with ``strategy="pilot-v1"`` builds a
``PilotStrategy`` (and that v4/judge still build their own strategies), and that
hot edits to the pilot thresholds rebuild the cached strategy.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentos.agentos_router.pilot.strategy import PilotStrategy
from agentos.engine.steps import agentos_router as step
from agentos.gateway.config import AgentOSRouterConfig

FIXTURE_DIR = (
    Path(__file__).parent.parent
    / "test_agentos_router"
    / "data"
    / "pilot_fixture"
)


@pytest.fixture(autouse=True)
def _clear_strategy_cache() -> None:
    # The dispatch cache is module-global; reset it around each test so a prior
    # strategy build never leaks into the next.
    step._strategy = None
    step._strategy_key = None
    yield
    step._strategy = None
    step._strategy_key = None


def test_dispatch_builds_pilot_strategy_for_pilot_v1() -> None:
    # Non-default confidence_threshold: the builder must forward the LIVE
    # config value (spec §4.1 t_eff coupling), not its own 0.5 fallback —
    # otherwise a raised router.confidence_threshold silently undoes fired
    # safety-net bumps at the engine confidence gate.
    cfg = AgentOSRouterConfig(
        strategy="pilot-v1",
        confidence_threshold=0.7,
        pilot={"pilot_artifact_dir": str(FIXTURE_DIR), "safety_net_threshold": 0.6},
    )

    strategy = step._get_strategy(cfg)

    assert isinstance(strategy, PilotStrategy)
    assert strategy._safety_net_threshold == 0.6
    assert strategy._confidence_threshold == 0.7
    assert strategy.artifact_dir == FIXTURE_DIR


def test_dispatch_still_builds_v4_and_judge() -> None:
    from agentos.agentos_router.v4_phase3 import V4Phase3Strategy

    v4 = step._get_strategy(AgentOSRouterConfig(strategy="v4_phase3"))
    assert isinstance(v4, V4Phase3Strategy)

    # A fresh cache key is required for the judge to build a distinct instance.
    step._strategy = None
    step._strategy_key = None
    judge = step._get_strategy(
        AgentOSRouterConfig(strategy="llm_judge"), llm_cfg=None
    )
    # The judge builder may return an unavailable stand-in without credentials,
    # but it must NOT be a Pilot/V4 strategy.
    assert not isinstance(judge, PilotStrategy | V4Phase3Strategy)


def test_cache_key_includes_pilot_thresholds() -> None:
    base = AgentOSRouterConfig(
        strategy="pilot-v1", pilot={"safety_net_threshold": 0.5}
    )
    key_base = step._strategy_cache_key(base)

    # A safety_net_threshold edit must perturb the key.
    edited = AgentOSRouterConfig(
        strategy="pilot-v1", pilot={"safety_net_threshold": 0.8}
    )
    assert step._strategy_cache_key(edited) != key_base

    # A confidence_threshold edit must perturb the key.
    conf = AgentOSRouterConfig(
        strategy="pilot-v1",
        pilot={"safety_net_threshold": 0.5},
        confidence_threshold=0.9,
    )
    assert step._strategy_cache_key(conf) != key_base


def test_cache_key_stable_for_unrelated_edit() -> None:
    base = AgentOSRouterConfig(
        strategy="pilot-v1", pilot={"safety_net_threshold": 0.5}
    )
    key_base = step._strategy_cache_key(base)

    # An unrelated (judge-only) field must NOT perturb the pilot cache key.
    unrelated = AgentOSRouterConfig(
        strategy="pilot-v1",
        pilot={"safety_net_threshold": 0.5},
        judge_input_max_chars=9999,
    )
    assert step._strategy_cache_key(unrelated) == key_base


def test_cache_key_rebuild_swaps_cached_pilot_instance() -> None:
    cfg_a = AgentOSRouterConfig(
        strategy="pilot-v1",
        pilot={"pilot_artifact_dir": str(FIXTURE_DIR), "safety_net_threshold": 0.5},
    )
    first = step._get_strategy(cfg_a)

    cfg_b = AgentOSRouterConfig(
        strategy="pilot-v1",
        pilot={"pilot_artifact_dir": str(FIXTURE_DIR), "safety_net_threshold": 0.9},
    )
    second = step._get_strategy(cfg_b)

    assert first is not second
    assert second._safety_net_threshold == 0.9
