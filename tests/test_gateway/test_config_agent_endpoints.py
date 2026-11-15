"""An agent's connection cannot re-point model calls, raise budgets or redefine agents.

``llm.base_url`` (and every other endpoint a provider key or a prompt is sent
to), the spend ceilings and the agent definitions are the operator's: a
connection the gateway recognizes as an agent's (token, process, window) is
refused them through every config writer, and ``agents.create`` /
``agents.update`` / ``agents.delete`` -- which write ``agents`` through the
registry, not ``commit_config`` -- apply the same gate. The user's console
(``unbound``) and the operator keep them, and the operator's own file writes
(``agentos providers configure``, ``agentos agents add``) are not held.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.testclient import TestClient

from agentos.agents.registry import AgentRegistry
from agentos.config_roots import (
    AGENT_REFUSED_CONFIG_ROOTS,
    OPERATOR_ONLY_CONFIG_ROOTS,
    OPERATOR_ONLY_ENTRY_FIELDS,
)
from agentos.gateway.access import ConnectionSurface
from agentos.gateway.agent_surface import AgentBinding, get_agent_surface
from agentos.gateway.app import create_gateway_app
from agentos.gateway.auth import AccessContext
from agentos.gateway.config import ChannelsConfig, GatewayConfig, ProviderProfileConfig
from agentos.gateway.config_commit import commit_config
from agentos.gateway.config_persist import persist_config, set_runtime_overrides
from agentos.gateway.rpc import RpcContext, get_dispatcher

EVIL = "https://collector.invalid/v1"
RECOGNIZED = ("token", "process", "window")
_HEADERS = {"host": "127.0.0.1:18791"}


@pytest.fixture(autouse=True)
def _clean() -> Any:
    set_runtime_overrides({})
    get_agent_surface().reset()
    yield
    get_agent_surface().reset()
    set_runtime_overrides({})


@pytest.fixture
def config(tmp_path: Path) -> GatewayConfig:
    cfg = GatewayConfig(state_dir=str(tmp_path / "state"))
    cfg.config_path = str(tmp_path / "cfg.toml")
    persist_config(cfg)
    return cfg


def _ctx(config: GatewayConfig, via: str | None, **extra: Any) -> Any:
    access = AccessContext(
        surface=ConnectionSurface.CONTROL,
        admitted=True,
        credential_verified=via is None,
        agent=AgentBinding(via=via) if via else None,
    )
    return SimpleNamespace(config=config, access=access, provider_selector=None, **extra)


def _set(target: Any, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    for part in parts[:-1]:
        target = getattr(target, part)
    setattr(target, parts[-1], value)


def _get(target: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        target = getattr(target, part)
    return target


ROOT_EDITS = [
    ("llm.base_url", EVIL),
    ("llm.proxy", "http://collector.invalid:8080"),
    ("agentos_router.judge_base_url", EVIL),
    ("agentos_router.jev.base_url", EVIL),
    ("memory.embedding.base_url", EVIL),
    ("memory.embedding.remote.base_url", EVIL),
    ("memory.embedding.ollama.base_url", EVIL),
    ("memory.provider.mem0.llm_base_url", EVIL),
    ("memory.provider.mem0.embedder_base_url", EVIL),
    ("image_generation.providers.openai.base_url", EVIL),
    ("image_generation.providers.openrouter.base_url", EVIL),
    ("image_generation.providers.surplus.base_url", EVIL),
    ("audio.providers.elevenlabs.base_url", EVIL),
    ("x_search.base_url", EVIL),
    ("budgets.daily_limit", 1e9),
    ("budgets.session_limit", 1e9),
    ("budgets.enabled", False),
]


@pytest.mark.parametrize("via", RECOGNIZED)
@pytest.mark.parametrize(("path", "value"), ROOT_EDITS)
def test_an_agent_cannot_commit_an_endpoint_or_budget(
    config: GatewayConfig, via: str, path: str, value: Any
) -> None:
    before = _get(config, path)
    candidate = config.model_copy(deep=True)
    _set(candidate, path, value)
    with pytest.raises(ValueError, match="user's to change"):
        # An under-reported changed path is still caught by the diff.
        commit_config(_ctx(config, via), candidate, changed_paths={"heartbeat.enabled"})
    assert _get(config, path) == before


@pytest.mark.parametrize("via", ["unbound", None])
@pytest.mark.parametrize(("path", "value"), ROOT_EDITS)
def test_the_console_and_the_operator_still_can(
    config: GatewayConfig, via: str | None, path: str, value: Any
) -> None:
    candidate = config.model_copy(deep=True)
    _set(candidate, path, value)
    commit_config(_ctx(config, via), candidate, changed_paths={path})
    assert _get(config, path) == value


def _with_channels(config: GatewayConfig, *entries: dict[str, Any]) -> GatewayConfig:
    candidate = config.model_copy(deep=True)
    candidate.channels = ChannelsConfig.model_validate({"channels": list(entries)})
    return candidate


TELEGRAM = {"name": "tg", "type": "telegram", "token": "t"}
DISCORD = {"name": "dc", "type": "discord", "token": "t"}
EMAIL = {
    "name": "mail",
    "type": "email",
    "imap_host": "imap.example.com",
    "imap_username": "me",
    "smtp_host": "smtp.example.com",
    "from_address": "me@example.com",
    "allowed_senders": ["me@example.com"],
}
SLACK = {"name": "sl", "type": "slack", "token": "t"}

CHANNEL_EDITS = [
    (TELEGRAM, "api_base", EVIL),
    (TELEGRAM, "webhook_url", EVIL),
    (DISCORD, "gateway_url", "wss://collector.invalid/"),
    (SLACK, "command_request_url", EVIL),
    (EMAIL, "smtp_host", "smtp.collector.invalid"),
    (EMAIL, "imap_host", "imap.collector.invalid"),
    (EMAIL, "smtp_port", 2525),
]


@pytest.mark.parametrize("via", RECOGNIZED)
@pytest.mark.parametrize(("entry", "field", "value"), CHANNEL_EDITS)
def test_an_agent_cannot_repoint_a_channel(
    config: GatewayConfig, via: str, entry: dict[str, Any], field: str, value: Any
) -> None:
    config.channels = _with_channels(config, entry).channels
    persist_config(config)
    with pytest.raises(ValueError, match=rf"channels\.channels\.{entry['name']}\.{field}"):
        commit_config(_ctx(config, via), _with_channels(config, {**entry, field: value}))
    assert getattr(config.channels.channels[0], field) != value
    # Renaming the channel to carry the new endpoint is the same change.
    renamed = {**entry, "name": "other", field: value}
    with pytest.raises(ValueError, match="user's to change"):
        commit_config(_ctx(config, via), _with_channels(config, renamed))
    # Unrelated edits and removal stay allowed.
    commit_config(_ctx(config, via), _with_channels(config, {**entry, "enabled": False}))
    commit_config(_ctx(config, via), _with_channels(config))
    assert config.channels.channels == []


@pytest.mark.parametrize("via", ["unbound", None])
@pytest.mark.parametrize(("entry", "field", "value"), CHANNEL_EDITS)
def test_the_console_can_repoint_a_channel(
    config: GatewayConfig, via: str | None, entry: dict[str, Any], field: str, value: Any
) -> None:
    commit_config(_ctx(config, via), _with_channels(config, {**entry, field: value}))
    assert getattr(config.channels.channels[0], field) == value


@pytest.mark.parametrize("via", RECOGNIZED)
@pytest.mark.parametrize("field", ["base_url", "proxy", "router.judge_base_url"])
def test_an_agent_cannot_repoint_a_provider_profile(
    config: GatewayConfig, via: str, field: str
) -> None:
    config.provider_profiles = {"openai": ProviderProfileConfig(base_url="https://api.openai.com")}
    persist_config(config)
    for key in ("openai", "deepseek"):
        candidate = config.model_copy(deep=True)
        profile = candidate.provider_profiles.get(key) or ProviderProfileConfig()
        _set(profile, field, EVIL)
        candidate.provider_profiles[key] = profile
        with pytest.raises(ValueError, match=rf"provider_profiles\.{key}\.{field}"):
            commit_config(_ctx(config, via), candidate)
    # A profile change that keeps the endpoints is fine.
    candidate = config.model_copy(deep=True)
    candidate.provider_profiles["openai"].model = "gpt-x"
    commit_config(_ctx(config, via), candidate)
    commit_config(_ctx(config, "unbound"), _with_profile_url(config, EVIL))
    assert config.provider_profiles["openai"].base_url == EVIL


def _with_profile_url(config: GatewayConfig, url: str) -> GatewayConfig:
    candidate = config.model_copy(deep=True)
    candidate.provider_profiles["openai"].base_url = url
    return candidate


def test_every_endpoint_field_in_the_config_is_operator_only() -> None:
    """A new ``*base_url``/``*proxy``/``api_base`` field must join ``config_roots``."""
    import typing

    from pydantic import BaseModel

    found: set[str] = set()

    def walk(model: type[BaseModel], prefix: str) -> None:
        for name, info in model.model_fields.items():
            path = f"{prefix}{name}"
            if name.endswith(("base_url", "proxy", "api_base")) or name == "gateway_url":
                found.add(path)
            for arg in [info.annotation, *typing.get_args(info.annotation)]:
                if isinstance(arg, type) and issubclass(arg, BaseModel):
                    walk(arg, f"{path}.")

    walk(GatewayConfig, "")

    def covered(path: str) -> bool:
        roots = (*OPERATOR_ONLY_CONFIG_ROOTS, *AGENT_REFUSED_CONFIG_ROOTS)
        if any(path == r or path.startswith(f"{r}.") for r in roots):
            return True
        return any(
            path.startswith(f"{collection}.") and path[len(collection) + 1 :] in fields
            for collection, fields in OPERATOR_ONLY_ENTRY_FIELDS.items()
        )

    assert sorted(p for p in found if not covered(p)) == []
    assert "api_base" in OPERATOR_ONLY_ENTRY_FIELDS["channels.channels"]


# -- the real WebSocket ---------------------------------------------------------


def _rpc(ws: Any, req_id: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
    ws.send_json({"type": "req", "id": req_id, "method": method, "params": params})
    while True:
        frame = ws.receive_json()
        if frame.get("type") == "res" and frame.get("id") == req_id:
            return frame


def _connect(config: GatewayConfig, auth: dict[str, str]) -> Any:
    client = TestClient(create_gateway_app(config), client=("127.0.0.1", 50000))
    ws = client.websocket_connect("/ws", headers=_HEADERS)
    return ws


def test_config_set_of_llm_base_url_over_the_socket(config: GatewayConfig) -> None:
    token = get_agent_surface().mint_token("agent:main:main", "main")
    original = config.llm.base_url
    for auth, ok in (({"agentToken": token}, False), ({}, True)):
        with _connect(config, auth) as ws:
            ws.receive_json()
            params = {"minProtocol": 3, "maxProtocol": 3, "auth": auth}
            ws.send_json({"type": "req", "id": "c", "method": "connect", "params": params})
            assert ws.receive_json()["type"] == "hello-ok"
            res = _rpc(ws, "s", "config.set", {"path": "llm.base_url", "value": EVIL})
        assert res["ok"] is ok, res
        if not ok:
            assert config.llm.base_url == original
    assert config.llm.base_url == EVIL


# -- agents.create / update / delete -----------------------------------------


async def _agents_rpc(config: GatewayConfig, via: str | None, method: str, params: dict) -> Any:
    registry = AgentRegistry(config, persist_changes=False)
    ctx = RpcContext(conn_id="c", access=_ctx(config, via).access, agent_registry=registry)
    return await get_dispatcher().dispatch("r", method, params, ctx)


CREATE = {"id": "helper", "name": "Helper", "tools": ["*"], "workspace": "/"}


@pytest.mark.asyncio
@pytest.mark.parametrize("via", RECOGNIZED)
async def test_an_agent_cannot_create_update_or_delete_agents(
    config: GatewayConfig, via: str
) -> None:
    res = await _agents_rpc(config, via, "agents.create", CREATE)
    assert not res.ok and res.error.code == "agent.operator_required"
    assert config.agents == []
    await _agents_rpc(config, None, "agents.create", {"id": "helper", "name": "Helper"})
    res = await _agents_rpc(config, via, "agents.update", {"id": "helper", "tools": ["*"]})
    assert not res.ok and res.error.code == "agent.operator_required"
    assert config.agents[0].tools is None
    res = await _agents_rpc(config, via, "agents.delete", {"id": "helper"})
    assert not res.ok and res.error.code == "agent.operator_required"
    assert [a.id for a in config.agents] == ["helper"]


@pytest.mark.asyncio
@pytest.mark.parametrize("via", ["unbound", None])
async def test_the_console_and_the_operator_manage_agents(
    config: GatewayConfig, via: str | None
) -> None:
    res = await _agents_rpc(config, via, "agents.create", {"id": "helper", "name": "Helper"})
    assert res.ok, res.error
    res = await _agents_rpc(config, via, "agents.update", {"id": "helper", "model": "m"})
    assert res.ok, res.error
    assert config.agents[0].model == "m"
    res = await _agents_rpc(config, via, "agents.delete", {"id": "helper"})
    assert res.ok, res.error
    assert config.agents == []


def test_the_operators_file_writes_of_them_are_not_held() -> None:
    """``agentos providers configure`` / ``agentos agents add`` write the file
    while a gateway runs; those writes apply as before (no ``config reload``)."""
    from agentos.gateway.config_commit import operator_only_paths

    assert operator_only_paths(["llm.base_url", "budgets.daily_limit", "agents"]) == []
