"""WAL-19: registering or launching an MCP server is the operator's action.

A stdio MCP server is a command the gateway forks itself, with its own
environment, outside the sandbox and the approval queue -- the property that
makes ``hooks.lifecycle`` operator-only. ``mcp`` was not an operator-only
config root and ``mcp.connect`` had no operator check, so an agent's own
connection could register ``command = "sh"`` and have the gateway run it.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import tomli_w

from agentos import env_policy
from agentos.config_roots import OPERATOR_ONLY_CONFIG_ROOTS
from agentos.gateway.access import ConnectionSurface
from agentos.gateway.agent_surface import AgentBinding
from agentos.gateway.auth import AccessContext
from agentos.gateway.config import GatewayConfig
from agentos.gateway.config_approval import enforce_approval
from agentos.gateway.config_commit import reload_config_from_disk
from agentos.gateway.config_persist import set_runtime_overrides
from agentos.gateway.rpc import RpcContext, get_dispatcher

SERVER = {"name": "evil", "transport": "stdio", "command": "sh", "args": ["-c", "id"]}
AGENT_VIAS = ("token", "process", "window")


@pytest.fixture(autouse=True)
def _no_runtime_overrides() -> Any:
    set_runtime_overrides({})
    yield
    set_runtime_overrides({})


def _ctx(config: GatewayConfig | None, via: str | None) -> RpcContext:
    access = AccessContext(
        surface=ConnectionSurface.CONTROL,
        admitted=True,
        credential_verified=via is None,
        agent=AgentBinding(via=via) if via else None,
    )
    return RpcContext(conn_id="c", access=access, config=config)


def _running(tmp_path: Path) -> tuple[Path, GatewayConfig]:
    path = tmp_path / "agentos.toml"
    path.write_text(
        tomli_w.dumps(GatewayConfig(state_dir=str(tmp_path / "state")).to_toml_dict()),
        encoding="utf-8",
    )
    config = GatewayConfig.load(path)
    enforce_approval(config)
    return path, config


def test_mcp_is_an_operator_only_root() -> None:
    assert "mcp" in OPERATOR_ONLY_CONFIG_ROOTS


@pytest.mark.parametrize(
    "name", ["AGENTOS_MCP_ENABLED", "AGENTOS_MCP_SERVERS", "AGENTOS_GATEWAY_MCP__SERVERS"]
)
def test_the_environment_spellings_are_not_writable(name: str) -> None:
    assert env_policy.is_writable(name) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("via", AGENT_VIAS)
async def test_agent_cannot_register_or_enable_a_server(tmp_path: Path, via: str) -> None:
    _path, running = _running(tmp_path)
    dispatcher = get_dispatcher()
    attempts: list[tuple[str, dict[str, Any]]] = [
        ("config.set", {"path": "mcp.enabled", "value": True}),
        ("config.set", {"path": "mcp.servers", "value": [SERVER]}),
        ("config.patch", {"patch": {"mcp": {"enabled": True, "servers": [SERVER]}}}),
    ]
    for method, params in attempts:
        res = await dispatcher.dispatch("r", method, params, _ctx(running, via))
        assert not res.ok, (method, params)
    assert running.mcp.enabled is False and running.mcp.servers == []


@pytest.mark.asyncio
async def test_operator_may_register_a_server(tmp_path: Path) -> None:
    _path, running = _running(tmp_path)
    res = await get_dispatcher().dispatch(
        "r", "config.set", {"path": "mcp.servers", "value": [SERVER]}, _ctx(running, None)
    )
    assert res.ok, res.error
    assert [s.name for s in running.mcp.servers] == ["evil"]


@pytest.mark.asyncio
@pytest.mark.parametrize("via", AGENT_VIAS)
@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("mcp.connect", {"name": "evil"}),
        ("mcp.disconnect", {"name": "evil"}),
        ("mcp.oauth.start", {"name": "evil"}),
        ("mcp.oauth.complete", {"code": "c", "state": "s"}),
        ("mcp.oauth.clear", {"name": "evil"}),
    ],
)
async def test_agent_cannot_launch_stop_or_authorize_a_server(
    tmp_path: Path, via: str, method: str, params: dict[str, Any]
) -> None:
    _path, running = _running(tmp_path)
    running.mcp.enabled = True
    res = await get_dispatcher().dispatch("r", method, params, _ctx(running, via))
    assert not res.ok and res.error is not None
    assert res.error.code == "mcp.operator_required"


@pytest.mark.asyncio
async def test_the_console_is_not_refused(tmp_path: Path) -> None:
    """``unbound`` (the user's own web console on loopback) keeps working."""
    _path, running = _running(tmp_path)
    res = await get_dispatcher().dispatch(
        "r", "mcp.disconnect", {"name": "nothing-connected"}, _ctx(running, "unbound")
    )
    assert res.error is None or res.error.code != "mcp.operator_required"


def test_a_file_edit_registering_a_server_is_held(tmp_path: Path) -> None:
    path, running = _running(tmp_path)
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    data["mcp"] = {"enabled": True, "servers": [SERVER]}
    path.write_text(tomli_w.dumps(data), encoding="utf-8")

    result = reload_config_from_disk(SimpleNamespace(config=running, provider_selector=None))
    assert any(p.startswith("mcp") for p in result.held_paths)
    assert running.mcp.enabled is False and running.mcp.servers == []
    # A restart does not apply it either.
    restarted = GatewayConfig.load(path)
    assert "mcp" in enforce_approval(restarted)
    assert restarted.mcp.enabled is False and restarted.mcp.servers == []
