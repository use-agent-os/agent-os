"""Part B — bind posture (host/port) is CLI-only: read-only via config RPC.

`config.set`/`config.patch` must not change `host`/`port`, and `config.apply`
(full replace) must preserve the RUNNING host/port instead of applying a
submitted one, so the UI can never persist an unsafe public-bind config.
"""

from __future__ import annotations

import tomllib

import pytest

import agentos.gateway.rpc_config  # noqa: F401  ensures registration
from agentos.gateway.auth import Principal
from agentos.gateway.config import GatewayConfig
from agentos.gateway.rpc import RpcContext, get_dispatcher


def _admin_ctx(config: GatewayConfig) -> RpcContext:
    return RpcContext(
        conn_id="t",
        config=config,
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.admin"}),
            is_owner=True,
            authenticated=True,
        ),
    )


@pytest.mark.asyncio
async def test_config_set_host_is_read_only(tmp_path):
    cfg = GatewayConfig(config_path=str(tmp_path / "c.toml"))
    res = await get_dispatcher().dispatch(
        "r1",
        "config.set",
        {"path": "host", "value": "0.0.0.0"},
        _admin_ctx(cfg),
    )

    assert res.error is not None
    assert "read-only" in res.error.message
    assert cfg.host == "127.0.0.1"


@pytest.mark.asyncio
async def test_config_set_port_is_read_only(tmp_path):
    cfg = GatewayConfig(config_path=str(tmp_path / "c.toml"))
    res = await get_dispatcher().dispatch(
        "r1",
        "config.set",
        {"path": "port", "value": 1},
        _admin_ctx(cfg),
    )

    assert res.error is not None
    assert "read-only" in res.error.message
    assert cfg.port == 18791


@pytest.mark.asyncio
async def test_config_patch_skips_host_and_port_but_applies_other_paths(tmp_path):
    cfg = GatewayConfig(config_path=str(tmp_path / "c.toml"))
    res = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {"host": "0.0.0.0", "port": 1, "debug": True}},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    # host/port are read-only paths: skipped, not applied.
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 18791
    # Other patches in the same call still apply.
    assert cfg.debug is True


@pytest.mark.asyncio
async def test_config_patch_merge_cannot_change_host_or_port(tmp_path):
    cfg = GatewayConfig(config_path=str(tmp_path / "c.toml"))
    res = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patch": {"host": "0.0.0.0", "port": 1, "debug": True}},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 18791
    assert cfg.debug is True


@pytest.mark.asyncio
async def test_config_apply_preserves_running_host_and_port(tmp_path):
    config_path = tmp_path / "c.toml"
    cfg = GatewayConfig(config_path=str(config_path))
    res = await get_dispatcher().dispatch(
        "r1",
        "config.apply",
        {"config": {"host": "0.0.0.0", "port": 9999, "debug": True}},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    # Full replace preserves (not rejects) the running bind posture.
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 18791
    assert cfg.debug is True

    persisted = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert persisted.get("host", "127.0.0.1") == "127.0.0.1"
    assert persisted.get("port", 18791) == 18791
