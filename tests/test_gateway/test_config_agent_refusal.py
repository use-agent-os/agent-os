"""No config writer lets an agent's connection set the operator's settings.

``config.set`` and ``config.patch`` refused ``trading.*`` to an agent-bound
connection; ``config.apply``, the full-replace sibling on the same WebSocket,
did not, so an agent's shell raised the approval threshold and daily cap and
its next swap skipped approval. Reproduced over the real ``/ws`` handshake.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.testclient import TestClient

from agentos.gateway.access import ConnectionSurface
from agentos.gateway.agent_surface import AgentBinding, get_agent_surface
from agentos.gateway.app import create_gateway_app
from agentos.gateway.auth import AccessContext
from agentos.gateway.config import ChannelsConfig, GatewayConfig
from agentos.gateway.config_commit import commit_config
from agentos.gateway.config_persist import persist_config, set_runtime_overrides

_HEADERS = {"host": "127.0.0.1:18791"}


@pytest.fixture(autouse=True)
def _clean() -> Any:
    set_runtime_overrides({})
    get_agent_surface().reset()
    yield
    get_agent_surface().reset()
    set_runtime_overrides({})


@pytest.fixture
def gateway(tmp_path: Path) -> tuple[GatewayConfig, Path]:
    path = tmp_path / "cfg.toml"
    config = GatewayConfig(state_dir=str(tmp_path / "state"))
    config.trading.approval_threshold_usd = 100.0
    config.trading.daily_cap_usd = 1000.0
    config.config_path = str(path)
    persist_config(config)
    return config, path


def _rpc(ws: Any, req_id: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
    ws.send_json({"type": "req", "id": req_id, "method": method, "params": params})
    while True:
        frame = ws.receive_json()
        if frame.get("type") == "res" and frame.get("id") == req_id:
            return frame


def _session(config: GatewayConfig, _auth: dict[str, str]) -> Any:
    client = TestClient(create_gateway_app(config), client=("127.0.0.1", 50000))
    ws = client.websocket_connect("/ws", headers=_HEADERS)
    return ws


def _connect(ws: Any, auth: dict[str, str]) -> None:
    ws.receive_json()  # challenge
    params = {"minProtocol": 3, "maxProtocol": 3, "auth": auth}
    ws.send_json({"type": "req", "id": "c", "method": "connect", "params": params})
    assert ws.receive_json()["type"] == "hello-ok"


def _raised(config: GatewayConfig) -> dict[str, Any]:
    payload = config.model_dump(mode="json")
    payload["trading"]["approval_threshold_usd"] = 1e9
    payload["trading"]["daily_cap_usd"] = 1e9
    return payload


@pytest.mark.parametrize("binding", ["unbound", "token"])
def test_every_config_writer_refuses_an_agent_the_trading_limits(
    gateway: tuple[GatewayConfig, Path], binding: str
) -> None:
    config, path = gateway
    before = path.read_bytes()
    auth = {}
    if binding == "token":
        auth = {"agentToken": get_agent_surface().mint_token("agent:main:main", "main")}
    with _session(config, auth) as ws:
        _connect(ws, auth)
        set_res = _rpc(
            ws, "s", "config.set", {"path": "trading.approval_threshold_usd", "value": 1e9}
        )
        patch_res = _rpc(ws, "p", "config.patch", {"patches": {"trading.daily_cap_usd": 1e9}})
        apply_res = _rpc(ws, "a", "config.apply", {"config": _raised(config)})
    for res in (set_res, patch_res, apply_res):
        assert res["ok"] is False, res
        assert "trading settings are the user's" in res["error"]["message"]
    assert config.trading.approval_threshold_usd == 100.0
    assert config.trading.daily_cap_usd == 1000.0
    assert path.read_bytes() == before
    on_disk = tomllib.loads(path.read_text())
    assert on_disk["trading"]["daily_cap_usd"] == 1000.0


def test_config_apply_refuses_other_operator_only_roots_to_a_recognized_agent(
    gateway: tuple[GatewayConfig, Path],
) -> None:
    config, _ = gateway
    token = get_agent_surface().mint_token("agent:main:main", "main")
    payload = config.model_dump(mode="json")
    payload["permissions"]["default_mode"] = "full"
    payload["sandbox"]["extra_rw_mounts"] = ["/"]
    with _session(config, {"agentToken": token}) as ws:
        _connect(ws, {"agentToken": token})
        res = _rpc(ws, "a", "config.apply", {"config": payload})
    assert res["ok"] is False
    assert "permissions.default_mode" in res["error"]["message"]
    assert config.permissions.default_mode == "bypass"
    assert config.sandbox.extra_rw_mounts == []


def test_the_operator_can_still_apply(gateway: tuple[GatewayConfig, Path]) -> None:
    config, path = gateway
    get_agent_surface().set_operator_secret("desktop-secret")
    with _session(config, {"operatorSecret": "desktop-secret"}) as ws:
        _connect(ws, {"operatorSecret": "desktop-secret"})
        res = _rpc(ws, "a", "config.apply", {"config": _raised(config)})
    assert res["ok"] is True, res
    assert config.trading.daily_cap_usd == 1e9
    assert tomllib.loads(path.read_text())["trading"]["daily_cap_usd"] == 1e9


def _ctx(config: GatewayConfig, via: str | None) -> SimpleNamespace:
    access = AccessContext(
        surface=ConnectionSurface.CONTROL,
        admitted=True,
        credential_verified=False,
        agent=AgentBinding(via=via) if via else None,
    )
    return SimpleNamespace(config=config, access=access, provider_selector=None)


@pytest.mark.parametrize("via", ["token", "process", "window", "unbound"])
def test_commit_config_is_the_backstop_for_any_writer(
    gateway: tuple[GatewayConfig, Path], via: str
) -> None:
    """A writer that under-reports its changed paths (or reports none) is still caught."""
    config, _ = gateway
    candidate = config.model_copy(deep=True)
    candidate.trading.daily_cap_usd = 1e9
    with pytest.raises(ValueError, match="trading settings"):
        commit_config(_ctx(config, via), candidate, changed_paths={"heartbeat.enabled"})
    assert config.trading.daily_cap_usd == 1000.0


@pytest.mark.parametrize("via", ["token", "process", "window"])
def test_recognized_agents_cannot_commit_any_operator_only_root(
    gateway: tuple[GatewayConfig, Path], via: str
) -> None:
    config, _ = gateway
    candidate = config.model_copy(deep=True)
    candidate.tools.invoke.allow = ["exec_command"]
    with pytest.raises(ValueError, match="tools.invoke.allow"):
        commit_config(_ctx(config, via), candidate)
    assert config.tools.invoke.allow == []


def test_the_users_unbound_console_keeps_its_settings_page(
    gateway: tuple[GatewayConfig, Path],
) -> None:
    """Unbound (the web console on loopback) may still change non-trading settings."""
    config, _ = gateway
    candidate = config.model_copy(deep=True)
    candidate.permissions.default_mode = "on"
    commit_config(_ctx(config, "unbound"), candidate, changed_paths={"permissions.default_mode"})
    assert config.permissions.default_mode == "on"


def _with_discord(config: GatewayConfig, allowed: list[str]) -> GatewayConfig:
    candidate = config.model_copy(deep=True)
    candidate.channels = ChannelsConfig.model_validate(
        {
            "channels": [
                {"name": "dc", "type": "discord", "token": "t", "allowed_user_ids": allowed},
            ]
        }
    )
    return candidate


@pytest.mark.parametrize("via", ["token", "process", "window"])
def test_recognized_agents_cannot_admit_a_channel_sender_through_config(
    gateway: tuple[GatewayConfig, Path], via: str
) -> None:
    """AOS-01 x AOS-08: pairing.approve refuses an agent; ``allowed_user_ids`` must too.

    The channel list diffs as one path, so no root check sees the new id.
    """
    config, _ = gateway
    config.channels = _with_discord(config, ["111"]).channels
    persist_config(config)
    with pytest.raises(ValueError, match=r"channels\.dc\.allowed_user_ids"):
        commit_config(_ctx(config, via), _with_discord(config, ["111", "666"]))
    assert config.channels.channels[0].allowed_user_ids == ["111"]
    # Narrowing (like deny / revoke) stays allowed.
    commit_config(_ctx(config, via), _with_discord(config, []))
    assert config.channels.channels[0].allowed_user_ids == []


def test_an_agent_cannot_upsert_a_channel_that_admits_its_sender(
    gateway: tuple[GatewayConfig, Path],
) -> None:
    config, _ = gateway
    token = get_agent_surface().mint_token("agent:main:main", "main")
    entry = {"name": "dc", "type": "discord", "token": "t", "allowed_user_ids": ["666"]}
    with _session(config, {"agentToken": token}) as ws:
        _connect(ws, {"agentToken": token})
        res = _rpc(ws, "u", "onboarding.channel.upsert", {"entry": entry})
    assert res["ok"] is False, res
    assert "admitting a channel sender" in res["error"]["message"]
    assert config.channels.channels == []


def test_the_users_console_can_still_admit_a_channel_sender(
    gateway: tuple[GatewayConfig, Path],
) -> None:
    config, _ = gateway
    commit_config(_ctx(config, "unbound"), _with_discord(config, ["111"]))
    assert config.channels.channels[0].allowed_user_ids == ["111"]
