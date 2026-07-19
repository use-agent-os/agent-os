"""Config-surface tests: the registry-driven strategy validation + PilotConfig.

``agentos_router.strategy`` is validated against the router strategy registry,
so ``pilot-v1`` is accepted and unknown ids are rejected. Pilot settings live in
the ``[agentos_router.pilot]`` sub-table backed by a typed ``PilotConfig``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentos.gateway.config import AgentOSRouterConfig, GatewayConfig, PilotConfig
from agentos.router_tiers import DEFAULT_ROUTER_STRATEGY


def test_router_strategy_accepts_pilot_v1() -> None:
    cfg = AgentOSRouterConfig(strategy="pilot-v1")
    assert cfg.strategy == "pilot-v1"


def test_router_strategy_rejects_unknown_id() -> None:
    with pytest.raises(ValidationError, match="agentos_router.strategy must be one of"):
        AgentOSRouterConfig(strategy="totally-made-up")


def test_router_strategy_default_is_pilot_v1() -> None:
    assert AgentOSRouterConfig().strategy == DEFAULT_ROUTER_STRATEGY == "pilot-v1"


def test_pilot_config_default_safety_net_threshold() -> None:
    cfg = AgentOSRouterConfig()
    assert isinstance(cfg.pilot, PilotConfig)
    assert cfg.pilot.safety_net_threshold == 0.5
    assert cfg.pilot.pilot_artifact_dir is None


def test_pilot_config_reads_sub_table() -> None:
    gw = GatewayConfig(
        agentos_router={
            "strategy": "pilot-v1",
            "pilot": {"safety_net_threshold": 0.7, "pilot_artifact_dir": "/tmp/pilot"},
        }
    )
    assert gw.agentos_router.strategy == "pilot-v1"
    assert gw.agentos_router.pilot.safety_net_threshold == 0.7
    assert gw.agentos_router.pilot.pilot_artifact_dir == "/tmp/pilot"


def test_pilot_config_rejects_out_of_range_threshold() -> None:
    with pytest.raises(ValidationError):
        PilotConfig(safety_net_threshold=1.5)
