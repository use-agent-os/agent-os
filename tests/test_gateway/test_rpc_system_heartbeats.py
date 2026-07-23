"""Transactional persistence coverage for the heartbeat control RPC."""

from __future__ import annotations

from typing import Any

import pytest

import agentos.gateway.rpc_system  # noqa: F401  ensure registration
from agentos.gateway.auth import Principal
from agentos.gateway.config import GatewayConfig
from agentos.gateway.rpc import RpcContext, get_dispatcher


class _HeartbeatLoop:
    def __init__(self) -> None:
        self.nudges = 0

    def nudge(self) -> None:
        self.nudges += 1


def _ctx(config: GatewayConfig, heartbeat_loop: Any) -> RpcContext:
    return RpcContext(
        conn_id="heartbeat-transaction-test",
        config=config,
        heartbeat_loop=heartbeat_loop,
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.admin"}),
            is_owner=True,
            authenticated=True,
        ),
    )


@pytest.mark.asyncio
async def test_set_heartbeats_persists_before_runtime_nudge(tmp_path) -> None:
    target = tmp_path / "config.toml"
    config = GatewayConfig(config_path=str(target))
    heartbeat_loop = _HeartbeatLoop()

    result = await get_dispatcher().dispatch(
        "r1",
        "set-heartbeats",
        {"enabled": True, "intervalMs": 45_000},
        _ctx(config, heartbeat_loop),
    )

    assert result.error is None, result.error
    assert config.heartbeat.enabled is True
    assert config.heartbeat.interval_ms == 45_000
    assert target.exists()
    assert heartbeat_loop.nudges == 1


@pytest.mark.asyncio
async def test_set_heartbeats_persist_failure_has_no_partial_runtime_mutation(
    tmp_path, monkeypatch
) -> None:
    config = GatewayConfig(config_path=str(tmp_path / "config.toml"))
    original_enabled = config.heartbeat.enabled
    original_interval = config.heartbeat.interval_ms
    heartbeat_loop = _HeartbeatLoop()

    def fail_persist(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr("agentos.gateway.config_commit.persist_config", fail_persist)
    result = await get_dispatcher().dispatch(
        "r1",
        "set-heartbeats",
        {"enabled": not original_enabled, "intervalMs": original_interval + 1_000},
        _ctx(config, heartbeat_loop),
    )

    assert result.error is not None
    assert result.error.code == "INTERNAL_ERROR"
    assert config.heartbeat.enabled is original_enabled
    assert config.heartbeat.interval_ms == original_interval
    assert heartbeat_loop.nudges == 0
