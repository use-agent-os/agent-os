"""The gateway, not the client, decides which connections are an agent's."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentos.gateway.access import ConnectionSurface
from agentos.gateway.agent_surface import (
    AGENT_TOKEN_ENV,
    OPERATOR_SECRET_ENV,
    OPERATOR_SECRET_FILE_ENV,
    OPERATOR_SECRET_FILENAME,
    AgentBinding,
    AgentSurface,
    agent_binding,
    default_operator_secret_path,
)
from agentos.gateway.auth import AccessContext
from agentos.gateway.rpc import RpcContext


@pytest.fixture
def surface() -> AgentSurface:
    return AgentSurface()


def _access() -> AccessContext:
    return AccessContext(surface=ConnectionSurface.CONTROL, admitted=True, credential_verified=True)


@pytest.fixture
def foreign_peers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The test peers are other machines, whatever addresses the CI host holds."""
    from agentos.gateway import agent_surface as agent_surface_mod

    monkeypatch.setattr(agent_surface_mod, "is_own_address", lambda addr: False)


class TestTokens:
    def test_minted_token_binds_to_the_session(self, surface: AgentSurface) -> None:
        token = surface.mint_token("agent:trading:webchat:x", "trading")
        binding = surface.bind({"agentToken": token})
        assert binding == AgentBinding(
            via="token", session_key="agent:trading:webchat:x", agent_id="trading"
        )

    def test_unknown_token_is_still_an_agent(self, surface: AgentSurface) -> None:
        assert surface.resolve_token("nope") is None
        assert surface.resolve_token(None) is None
        # Not a token this gateway minted (one from before a restart, or made
        # up), but the client says it is an agent: never the operator, and
        # recognized as an agent where human decisions are refused.
        assert surface.bind({"agentToken": "nope"}) == AgentBinding(via="token")
        surface.add_operator_secret("op")
        assert surface.bind({"agentToken": "nope", "operatorSecret": "op"}) == AgentBinding(
            via="token"
        )

    def test_tokens_are_bounded(self, surface: AgentSurface) -> None:
        first = surface.mint_token("s0", "a")
        for i in range(5000):
            surface.mint_token(f"s{i}", "a")
        assert surface.resolve_token(first) is None


class TestWindows:
    def test_new_connection_during_a_shell_is_the_agents(self, surface: AgentSurface) -> None:
        assert surface.bind({}).via == "unbound"
        with surface.exec_window("agent:main:chat"):
            assert surface.active_windows() == 1
            assert surface.bind({}) == AgentBinding(via="window", session_key="agent:main:chat")
            # Dropping the token gains nothing: still the agent's.
            assert surface.bind({"agentToken": "stripped"}).via == "window"
        assert surface.active_windows() == 0
        assert surface.bind({}).via == "unbound"

    def test_two_sessions_at_once_bind_without_a_session(self, surface: AgentSurface) -> None:
        a = surface.begin_window("agent:a")
        b = surface.begin_window("agent:b")
        assert surface.bind({}) == AgentBinding(via="window", session_key=None)
        surface.end_window(a)
        assert surface.bind({}).session_key == "agent:b"
        surface.end_window(b)

    def test_token_beats_window_for_attribution(self, surface: AgentSurface) -> None:
        token = surface.mint_token("agent:x", "trading")
        with surface.exec_window("agent:y"):
            assert surface.bind({"agentToken": token}).session_key == "agent:x"


class TestUnbound:
    """Presenting nothing never makes a connection the operator's."""

    def test_nothing_presented_is_unbound(self, surface: AgentSurface) -> None:
        binding = surface.bind({})
        assert binding == AgentBinding(via="unbound", session_key=None, agent_id=None)
        assert surface.bind(None).via == "unbound"
        assert binding.to_dict() == {"via": "unbound", "sessionKey": None, "agentId": None}

    def test_unbound_even_with_a_secret_configured(self, surface: AgentSurface) -> None:
        surface.set_operator_secret("s3cret")
        assert surface.bind({}).via == "unbound"
        assert surface.bind({"operatorSecret": "wrong"}).via == "unbound"

    def test_attach_marks_an_unbound_connection(self, surface: AgentSurface) -> None:
        access = surface.attach(_access(), {})
        assert access.agent is not None and access.agent.via == "unbound"
        # The trading handlers test ``binding is not None``: agent rules.
        assert agent_binding(RpcContext(conn_id="c", access=access)) is not None


class TestPrecedence:
    """A sign that an agent is asking outranks any proof an agent could have read."""

    def test_operator_file_does_not_beat_an_open_exec_window(
        self, surface: AgentSurface, tmp_path: Path
    ) -> None:
        # The audit's reproduction: a shell that read operator.secret (a glob,
        # a cd, a variable) presented it while its own window was open.
        path = surface.ensure_operator_file(tmp_path)
        stolen = path.read_text().strip()
        with surface.exec_window("agent:main:v316"):
            assert surface.bind({"operatorSecret": stolen}) == AgentBinding(
                via="window", session_key="agent:main:v316"
            )
        # The operator's own terminal, with no agent shell running.
        assert surface.bind({"operatorSecret": stolen}) is None

    def test_agent_token_beats_every_secret(self, surface: AgentSurface, tmp_path: Path) -> None:
        surface.set_operator_secret("desktop")
        file_secret = surface.ensure_operator_file(tmp_path).read_text().strip()
        token = surface.mint_token("agent:x", "trading")
        for secret in ("desktop", file_secret):
            binding = surface.bind({"agentToken": token, "operatorSecret": secret})
            assert binding == AgentBinding(via="token", session_key="agent:x", agent_id="trading")

    def test_an_agent_process_is_an_agent_whatever_it_presents(
        self, surface: AgentSurface, tmp_path: Path
    ) -> None:
        surface.set_operator_secret("desktop")
        file_secret = surface.ensure_operator_file(tmp_path).read_text().strip()
        for presented in ({}, {"operatorSecret": "desktop"}, {"operatorSecret": file_secret}):
            assert surface.bind(presented, agent_process=True) == AgentBinding(via="process")
        with surface.exec_window("agent:x"):
            assert surface.bind({}, agent_process=True) == AgentBinding(
                via="process", session_key="agent:x"
            )
        # Even with the gateway token proved from another machine.
        assert surface.bind({}, agent_process=True, remote_operator=True) is not None

    def test_desktop_secret_survives_an_open_window(self, surface: AgentSurface) -> None:
        # The app reconnecting while an agent's dev server runs in the
        # background is still the user: the secret never touched the disk.
        surface.set_operator_secret("desktop")
        with surface.exec_window("agent:x"):
            assert surface.bind({"operatorSecret": "desktop"}) is None

    def test_operator_file_is_not_accepted_over_http(
        self, surface: AgentSurface, tmp_path: Path
    ) -> None:
        file_secret = surface.ensure_operator_file(tmp_path).read_text().strip()
        surface.set_operator_secret("desktop")
        assert surface.bind({"operatorSecret": file_secret}, file_secret=False) == AgentBinding(
            via="unbound"
        )
        assert surface.bind({"operatorSecret": "desktop"}, file_secret=False) is None

    def test_recognized_is_everything_but_unbound(self) -> None:
        assert AgentBinding(via="token").recognized
        assert AgentBinding(via="process").recognized
        assert AgentBinding(via="window").recognized
        assert not AgentBinding(via="unbound").recognized


class TestOperator:
    def test_operator_secret_is_never_an_agent(self, surface: AgentSurface) -> None:
        surface.set_operator_secret("s3cret")
        with surface.exec_window("agent:x"):
            assert surface.bind({"operatorSecret": "s3cret"}) is None
            assert surface.bind({"operatorSecret": "wrong"}) is not None
            assert surface.bind({}) is not None

    def test_both_secrets_are_accepted(self, surface: AgentSurface) -> None:
        surface.set_operator_secret("from-desktop")
        surface.add_operator_secret("from-file")
        assert surface.has_operator_secret
        assert surface.is_operator("from-desktop") and surface.is_operator("from-file")
        assert surface.bind({"operatorSecret": "from-desktop"}) is None
        assert surface.bind({"operatorSecret": "from-file"}) is None
        assert not surface.is_operator("neither")
        # Replacing the desktop slot leaves the other one alone.
        surface.set_operator_secret("desktop-2")
        assert not surface.is_operator("from-desktop")
        assert surface.is_operator("desktop-2") and surface.is_operator("from-file")
        surface.set_operator_secret(None)
        assert not surface.is_operator("desktop-2") and surface.is_operator("from-file")

    def test_no_secret_configured_admits_nobody_as_operator_by_secret(
        self, surface: AgentSurface
    ) -> None:
        assert surface.is_operator("anything") is False
        assert surface.has_operator_secret is False

    def test_secret_file_is_read_once_and_deleted(
        self, surface: AgentSurface, tmp_path: Path
    ) -> None:
        secret_file = tmp_path / "operator.secret"
        secret_file.write_text("from-file\n")
        env = {OPERATOR_SECRET_FILE_ENV: str(secret_file), "OTHER": "kept"}
        assert surface.load_operator_secret_from_env(env) is True
        assert surface.is_operator("from-file")
        assert not secret_file.exists()
        # Scrubbed: a child of this process cannot find it again.
        assert OPERATOR_SECRET_FILE_ENV not in env and env == {"OTHER": "kept"}

    def test_bare_env_secret_is_scrubbed(self, surface: AgentSurface) -> None:
        env = {OPERATOR_SECRET_ENV: "bare"}
        assert surface.load_operator_secret_from_env(env) is True
        assert surface.is_operator("bare") and env == {}

    def test_missing_file_is_not_fatal(self, surface: AgentSurface, tmp_path: Path) -> None:
        env = {OPERATOR_SECRET_FILE_ENV: str(tmp_path / "gone")}
        assert surface.load_operator_secret_from_env(env) is False
        assert surface.has_operator_secret is False


class TestAttach:
    def test_attach_marks_the_access_context(self, surface: AgentSurface) -> None:
        token = surface.mint_token("agent:x", "trading")
        access = surface.attach(_access(), {"agentToken": token})
        assert access.agent is not None and access.agent.session_key == "agent:x"
        assert access.admitted is True
        ctx = RpcContext(conn_id="c", access=access)
        assert agent_binding(ctx) == access.agent

    def test_operator_access_is_left_alone(self, surface: AgentSurface) -> None:
        surface.set_operator_secret("s3cret")
        access = _access()
        assert surface.attach(access, {"operatorSecret": "s3cret"}) is access
        assert agent_binding(RpcContext(conn_id="c", access=access)) is None
        assert agent_binding(object()) is None


@pytest.mark.usefixtures("foreign_peers")
class TestRemoteToken:
    """The gateway token proved from another machine is the operator's."""

    @pytest.mark.parametrize("peer", ["192.168.1.20", "172.30.0.5", "203.0.113.7", "2001:db8::1"])
    def test_remote_token_is_the_operator(self, surface: AgentSurface, peer: str) -> None:
        access = _access()
        assert surface.attach(access, {}, peer_ip=peer) is access

    @pytest.mark.parametrize(
        "peer",
        ["127.0.0.1", "127.8.9.10", "::1", "[::1]", "::ffff:127.0.0.1", None, "", "testclient"],
    )
    def test_loopback_or_unknown_peer_stays_unbound(
        self, surface: AgentSurface, peer: str | None
    ) -> None:
        access = surface.attach(_access(), {}, peer_ip=peer)
        assert access.agent == AgentBinding(via="unbound")

    def test_unverified_or_node_access_is_not_the_operator(self, surface: AgentSurface) -> None:
        unverified = AccessContext(
            surface=ConnectionSurface.CONTROL, admitted=True, credential_verified=False
        )
        node = AccessContext(
            surface=ConnectionSurface.NODE, admitted=True, credential_verified=True
        )
        for access in (unverified, node):
            bound = surface.attach(access, {}, peer_ip="192.168.1.20")
            assert bound.agent == AgentBinding(via="unbound")

    def test_agent_token_still_wins(self, surface: AgentSurface) -> None:
        token = surface.mint_token("agent:x", "trading")
        access = surface.attach(_access(), {"agentToken": token}, peer_ip="192.168.1.20")
        assert access.agent is not None and access.agent.session_key == "agent:x"

    def test_remote_token_beats_an_open_window(self, surface: AgentSurface) -> None:
        # The app reconnecting while an agent shell runs is still the user.
        with surface.exec_window("agent:x"):
            access = _access()
            assert surface.attach(access, {}, peer_ip="192.168.1.20") is access
            local = surface.attach(_access(), {}, peer_ip="127.0.0.1")
            assert local.agent is not None and local.agent.via == "window"


@pytest.mark.usefixtures("foreign_peers")
class TestRemoteTokenOverWebSocket:
    """Through the real handshake, as the desktop app in External mode connects."""

    _TOKEN = "correct-horse-battery-staple-0123456789"

    def _rename(self, peer: str, headers: dict[str, str], auth: dict[str, str]) -> dict:
        from starlette.testclient import TestClient

        from agentos.gateway.app import create_gateway_app
        from agentos.gateway.config import AuthConfig, GatewayConfig

        config = GatewayConfig(host="0.0.0.0", auth=AuthConfig(mode="token", token=self._TOKEN))
        client = TestClient(create_gateway_app(config), client=(peer, 50000))
        with client.websocket_connect("/ws", headers=headers) as ws:
            ws.receive_json()
            params = {"minProtocol": 3, "maxProtocol": 3, "auth": {"token": self._TOKEN, **auth}}
            ws.send_json({"type": "req", "id": "c", "method": "connect", "params": params})
            assert ws.receive_json()["type"] == "hello-ok"
            # No params: the operator gets past the guard to the params check,
            # without touching a vault.
            ws.send_json({"type": "req", "id": "r", "method": "wallet.rename", "params": {}})
            while True:
                frame = ws.receive_json()
                if frame.get("type") == "res" and frame.get("id") == "r":
                    return frame["error"]

    _CONSOLE = {"origin": "http://192.168.1.20:18791", "host": "192.168.1.20:18791"}

    def test_remote_app_reaches_operator_only_rpcs(self) -> None:
        error = self._rename("192.168.1.20", self._CONSOLE, {})
        assert error["code"] == "INVALID_REQUEST"
        assert "params.address" in error["message"]

    def test_loopback_cli_with_the_token_is_still_an_agent(self) -> None:
        error = self._rename("127.0.0.1", {"host": "127.0.0.1:18791"}, {})
        assert error["code"] == "trading.operator_required"

    def test_remote_agent_token_is_still_an_agent(self) -> None:
        from agentos.gateway.agent_surface import get_agent_surface

        token = get_agent_surface().mint_token("agent:x", "trading")
        error = self._rename("192.168.1.20", self._CONSOLE, {"agentToken": token})
        assert error["code"] == "trading.operator_required"
        assert error["details"]["sessionKey"] == "agent:x"


class TestOperatorFile:
    """The gateway writes its own secret for the local CLI, rotated each boot."""

    def test_written_0600_under_wallets_and_accepted(
        self, surface: AgentSurface, tmp_path: Path
    ) -> None:
        path = surface.ensure_operator_file(tmp_path)
        assert path == tmp_path / OPERATOR_SECRET_FILENAME
        assert path == default_operator_secret_path(tmp_path)
        assert path.parent.name == "wallets"
        if os.name == "posix":
            assert oct(path.stat().st_mode & 0o777) == "0o600"
            assert oct(path.parent.stat().st_mode & 0o777) == "0o700"
        secret = path.read_text().strip()
        assert len(secret) >= 32
        assert surface.has_operator_secret
        assert surface.bind({"operatorSecret": secret}) is None
        assert surface.operator_file == path

    def test_rotated_on_every_call(self, surface: AgentSurface, tmp_path: Path) -> None:
        first = surface.ensure_operator_file(tmp_path).read_text().strip()
        second = surface.ensure_operator_file(tmp_path).read_text().strip()
        assert first != second
        assert surface.is_operator(second)
        # The old one is worthless: a copy taken earlier no longer works.
        assert not surface.is_operator(first)
        assert surface.bind({"operatorSecret": first}).via == "unbound"

    def test_leftover_file_is_tightened(self, surface: AgentSurface, tmp_path: Path) -> None:
        path = default_operator_secret_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("stale")
        path.chmod(0o644)
        surface.ensure_operator_file(tmp_path)
        assert path.read_text().strip() != "stale"
        if os.name == "posix":
            assert oct(path.stat().st_mode & 0o777) == "0o600"

    def test_desktop_secret_survives_the_file(self, surface: AgentSurface, tmp_path: Path) -> None:
        surface.set_operator_secret("from-desktop")
        secret = surface.ensure_operator_file(tmp_path).read_text().strip()
        assert surface.is_operator("from-desktop") and surface.is_operator(secret)

    def test_remove_drops_file_and_secret(self, surface: AgentSurface, tmp_path: Path) -> None:
        path = surface.ensure_operator_file(tmp_path)
        secret = path.read_text().strip()
        surface.remove_operator_file()
        assert not path.exists()
        assert not surface.is_operator(secret)
        assert surface.operator_file is None
        surface.remove_operator_file()  # idempotent

    def test_default_path_is_under_the_agentos_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path / "home"))
        assert default_operator_secret_path() == tmp_path / "home" / "wallets" / "operator.secret"

    def test_wallets_dir_is_denied_to_agent_shells(self) -> None:
        from agentos.sandbox.sensitive_paths import is_sensitive_path

        assert is_sensitive_path("~/.agentos/wallets") == "~/.agentos/wallets"
        assert is_sensitive_path("~/.agentos/" + OPERATOR_SECRET_FILENAME) == "~/.agentos/wallets"


def test_env_names_are_the_documented_ones() -> None:
    assert AGENT_TOKEN_ENV == "AGENTOS_AGENT_TOKEN"
    assert OPERATOR_SECRET_ENV == "AGENTOS_OPERATOR_SECRET"
    assert os.environ.get(OPERATOR_SECRET_ENV) is None


def test_http_requests_never_honour_the_operator_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An agent's in-process HTTP tool must not turn a read secret into the operator."""
    from starlette.testclient import TestClient

    from agentos.gateway.agent_surface import get_agent_surface
    from agentos.gateway.app import create_gateway_app
    from agentos.gateway.config import GatewayConfig

    surface = get_agent_surface()
    surface.reset()
    surface.set_operator_secret("desktop")
    file_secret = surface.ensure_operator_file(tmp_path).read_text().strip()
    bound: list[AgentBinding | None] = []
    original = AgentSurface.attach

    def spy(self: AgentSurface, access: AccessContext, *args, **kwargs) -> AccessContext:
        result = original(self, access, *args, **kwargs)
        bound.append(result.agent)
        return result

    monkeypatch.setattr(AgentSurface, "attach", spy)
    try:
        client = TestClient(create_gateway_app(GatewayConfig()), client=("127.0.0.1", 50000))
        for secret in (file_secret, "desktop"):
            response = client.post(
                "/api/v1/rpc",
                json={"id": 1, "method": "health"},
                headers={"x-agentos-operator-secret": secret, "host": "127.0.0.1:18791"},
            )
            assert response.status_code == 200
    finally:
        surface.reset()
    assert bound == [AgentBinding(via="unbound"), None]
