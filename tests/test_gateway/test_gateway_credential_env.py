"""The gateway's credentials never reach a process an agent runs.

A compose deployment hands the gateway its token as ``AGENTOS_AUTH_TOKEN``.
Every agent shell used to inherit it, and a token-verified connection from a
non-loopback address is the operator's (``agent_surface`` rule 1b), so an
agent that dialled its own container address with it became the operator.
"""

from __future__ import annotations

import ipaddress
import os

import pytest

from agentos import env_policy
from agentos.gateway import agent_surface as agent_surface_mod
from agentos.gateway import credential_env
from agentos.gateway.access import ConnectionSurface
from agentos.gateway.agent_surface import AgentBinding, AgentSurface, is_own_address
from agentos.gateway.auth import AccessContext, resolve_auth
from agentos.gateway.config import AuthConfig, GatewayConfig
from agentos.gateway.lifecycle_hooks import _CREDENTIAL_ENV
from agentos.tools.builtin import shell
from agentos.tools.env_passthrough import build_subprocess_env
from agentos.tools.types import CallerKind, ToolContext, current_tool_context

_COMPOSE_TOKEN = "DUMMY-COMPOSE-AUTH-TOKEN-0123456789abcdef"

_CREDENTIALS = {
    "AGENTOS_AUTH_TOKEN": _COMPOSE_TOKEN,
    "AGENTOS_GATEWAY_AUTH__TOKEN": "nested-token",
    "AGENTOS_AUTH_PASSWORD": "pw",
    "AGENTOS_GATEWAY_AUTH__PASSWORD": "nested-pw",
    "AGENTOS_GATEWAY_HOOKS__TOKEN": "DUMMY-HOOKS-TOKEN",
    "AGENTOS_GATEWAY_TOKEN": "client-token",
}


@pytest.fixture
def compose_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _CREDENTIALS.items():
        monkeypatch.setenv(name, value)


def test_one_list_covers_every_name_the_gateway_reads_a_credential_from() -> None:
    assert set(_CREDENTIALS) <= env_policy.GATEWAY_CREDENTIAL_ENV
    # The lifecycle filter and the child filter are the same list (plus the
    # per-call agent token), so they cannot drift apart again.
    assert env_policy.GATEWAY_CREDENTIAL_ENV <= _CREDENTIAL_ENV
    assert "AGENTOS_AGENT_TOKEN" in _CREDENTIAL_ENV
    # And no AgentOS surface may write one into ``.env`` for the next boot.
    for name in env_policy.GATEWAY_CREDENTIAL_ENV:
        assert not env_policy.is_writable(name)


def test_child_env_drops_inherited_and_planted_credentials(compose_env: None) -> None:
    env = build_subprocess_env(extra=dict(_CREDENTIALS, AGENTOS_AGENT_TOKEN="planted"))
    assert not set(_CREDENTIALS) & set(env)
    assert "AGENTOS_AGENT_TOKEN" not in env


@pytest.mark.skipif(os.name != "posix", reason="POSIX shell expansion")
@pytest.mark.asyncio
async def test_exec_command_child_sees_no_gateway_credential(
    compose_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The audit's probe, through the real exec_command body."""
    monkeypatch.setattr(shell, "get_runtime", lambda: None)
    reset = current_tool_context.set(
        ToolContext(
            caller_kind=CallerKind.CLI,
            session_key="agent:main:main",
            workspace_dir=str(tmp_path),
            elevated="bypass",
        )
    )
    try:
        out = await shell.exec_command(
            "echo AUTH=${AGENTOS_AUTH_TOKEN:-unset} NESTED=${AGENTOS_GATEWAY_AUTH__TOKEN:-unset}"
            " GW=${AGENTOS_GATEWAY_TOKEN:-unset} HOOKS=${AGENTOS_GATEWAY_HOOKS__TOKEN:-unset}",
            workdir=str(tmp_path),
            env={"AGENTOS_AUTH_TOKEN": _COMPOSE_TOKEN},
        )
    finally:
        current_tool_context.reset(reset)
    assert "AUTH=unset NESTED=unset GW=unset HOOKS=unset" in out, out


class TestBootScrub:
    def test_scrub_removes_the_credentials_and_keeps_them_for_a_reexec(self) -> None:
        credential_env.reset_scrubbed_credential_env()
        environ = dict(_CREDENTIALS, PATH="/usr/bin", AGENTOS_OPERATOR_SECRET="desktop")
        removed = credential_env.scrub_credential_env(environ)
        assert set(removed) == set(_CREDENTIALS)
        assert environ == {"PATH": "/usr/bin", "AGENTOS_OPERATOR_SECRET": "desktop"}
        # The desktop's secret is the agent surface's to scrub and hand on.
        assert credential_env.credential_env_for_reexec() == _CREDENTIALS
        credential_env.reset_scrubbed_credential_env()

    def test_env_sourced_tokens_stay_out_of_toml_after_the_scrub(
        self, compose_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = GatewayConfig()
        config.mark_env_sourced_auth_secrets({})
        # The nested spelling wins when both are set; either is a credential.
        assert config.auth.token == "nested-token"
        assert config.hooks.token == "DUMMY-HOOKS-TOKEN"
        credential_env.scrub_credential_env()
        try:
            assert "AGENTOS_AUTH_TOKEN" not in os.environ
            data = config.to_toml_dict()
            assert "token" not in data.get("auth", {})
            assert "token" not in data.get("hooks", {})
        finally:
            credential_env.reset_scrubbed_credential_env()


def _verified_access(token: str) -> AccessContext:
    config = GatewayConfig(host="0.0.0.0", auth=AuthConfig(mode="token", token=_COMPOSE_TOKEN))
    access = resolve_auth(config, auth_params={"token": token}, client_kind="control")
    assert access is not None and access.credential_verified
    assert access.surface is ConnectionSurface.CONTROL
    return access


class TestSelfDialIsNotRemote:
    """Rule 1b: the gateway token from this host's own address is not the operator."""

    def test_token_from_the_containers_own_address_is_an_agent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        own = {ipaddress.ip_address("172.18.0.2")}
        monkeypatch.setattr(agent_surface_mod, "is_own_address", lambda addr: addr in own)
        surface = AgentSurface()
        access = _verified_access(_COMPOSE_TOKEN)
        with surface.exec_window("agent:main:main"):
            bound = surface.attach(access, {}, peer_ip="172.18.0.2")
            assert bound.agent == AgentBinding(via="window", session_key="agent:main:main")
            # The same token from another machine is still the operator.
            assert surface.attach(access, {}, peer_ip="172.18.0.1") is access
        assert surface.attach(access, {}, peer_ip="172.18.0.2").agent == AgentBinding(via="unbound")
        # An IPv4-mapped spelling of the own address is the same address.
        assert surface.attach(access, {}, peer_ip="::ffff:172.18.0.2").agent is not None

    @pytest.mark.parametrize("peer", ["fe80::1", "fe80::1%eth0", "169.254.10.1", "0.0.0.0"])
    def test_link_local_or_unspecified_peer_is_never_remote(self, peer: str) -> None:
        surface = AgentSurface()
        bound = surface.attach(_verified_access(_COMPOSE_TOKEN), {}, peer_ip=peer)
        assert bound.agent == AgentBinding(via="unbound")

    def test_own_address_probe_on_this_host(self) -> None:
        assert is_own_address(ipaddress.ip_address("127.0.0.1"))
        # TEST-NET-3 is never assigned to a host (unless the kernel accepts
        # non-local binds, in which case every address counts as ours).
        assert is_own_address(ipaddress.ip_address("203.0.113.77")) is is_own_address(
            ipaddress.ip_address("192.0.2.1")
        )
