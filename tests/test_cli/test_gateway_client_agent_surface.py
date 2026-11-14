"""What the CLI presents so the gateway can tell the operator from an agent.

Exactly one of ``agentToken`` (inside an agent's shell) or ``operatorSecret``
(the operator's own terminal, local gateway, file readable) — or neither, in
which case the gateway binds the connection as the agent's.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from agentos.cli import gateway_client

_LOCAL = "ws://127.0.0.1:18791/ws"
_REMOTE = "ws://10.0.0.5:18791/ws"


@pytest.fixture
def secret_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "wallets" / "operator.secret"
    monkeypatch.setenv(gateway_client.OPERATOR_SECRET_FILE_ENV, str(path))
    for name in gateway_client.AGENT_SESSION_ENV:
        monkeypatch.delenv(name, raising=False)
    # Whoever runs the suite (a CI runner, or an agent's own shell) is not
    # the subject here: the operator's terminal has no gateway ancestor.
    monkeypatch.setattr(gateway_client, "_ancestors", lambda: [])
    return path


class TestAgentSurfaceAuth:
    def test_token_only_inside_an_agent_shell(
        self, secret_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        secret_file.parent.mkdir()
        secret_file.write_text("op-secret\n")
        monkeypatch.setenv(gateway_client.AGENT_TOKEN_ENV, "minted")
        # The file is readable, and still never presented alongside the token.
        assert gateway_client.agent_surface_auth(_LOCAL) == {"agentToken": "minted"}
        assert gateway_client.agent_surface_auth(_REMOTE) == {"agentToken": "minted"}

    def test_file_only_for_the_local_operator(self, secret_file: Path) -> None:
        secret_file.parent.mkdir()
        secret_file.write_text("  op-secret \n")
        assert gateway_client.agent_surface_auth(_LOCAL) == {"operatorSecret": "op-secret"}
        assert gateway_client.agent_surface_auth("ws://localhost:1/ws") == {
            "operatorSecret": "op-secret"
        }

    def test_remote_gateway_gets_neither(self, secret_file: Path) -> None:
        secret_file.parent.mkdir()
        secret_file.write_text("op-secret\n")
        assert gateway_client.agent_surface_auth(_REMOTE) == {}
        assert gateway_client.agent_surface_auth(None) == {}

    @pytest.mark.parametrize(
        "marker",
        ["AGENTOS_AGENT_TOKEN", "AGENTOS_SESSION_KEY", "AGENTOS_AGENT", "AGENTOS_ACP_LEASE"],
    )
    def test_an_agent_session_never_presents_the_file(
        self, secret_file: Path, monkeypatch: pytest.MonkeyPatch, marker: str
    ) -> None:
        # ``AGENTOS_AGENT_TOKEN= agentos trade approve …``: the token emptied,
        # the session markers (or just the variable's presence) left behind.
        secret_file.parent.mkdir()
        secret_file.write_text("op-secret\n")
        monkeypatch.setenv(marker, "")
        assert gateway_client.agent_surface_auth(_LOCAL) == {}

    def test_a_gateway_ancestor_never_presents_the_file(
        self, secret_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``env -i agentos …`` strips every variable; the parent stays.
        secret_file.parent.mkdir()
        secret_file.write_text("op-secret\n")
        monkeypatch.setattr(
            gateway_client,
            "_ancestors",
            lambda: [(4242, ["/bin/sh", "-c", "agentos trade approve x"]), (4100, ["zsh"])],
        )
        monkeypatch.setattr(gateway_client, "_gateway_pid_candidates", lambda: {4100})
        assert gateway_client.agent_surface_auth(_LOCAL) == {}
        monkeypatch.setattr(gateway_client, "_gateway_pid_candidates", lambda: set())
        assert gateway_client.agent_surface_auth(_LOCAL) == {"operatorSecret": "op-secret"}
        monkeypatch.setattr(
            gateway_client,
            "_ancestors",
            lambda: [
                (4242, ["sh"]),
                (
                    4100,
                    [
                        "/opt/agentos/bin/python",
                        "-m",
                        "agentos.cli.main",
                        "gateway",
                        "run",
                        "--port",
                        "1",
                    ],
                ),
            ],
        )
        assert gateway_client.agent_surface_auth(_LOCAL) == {}

    @pytest.mark.parametrize(
        "argv",
        [
            ["agentos", "gateway", "run"],
            ["/Users/u/.local/bin/agentos", "gateway", "run", "--bind", "127.0.0.1"],
            ["/usr/bin/python3", "-m", "agentos.cli.main", "gateway", "run"],
            ["/home/u/.local/bin/gateway", "run"],
        ],
    )
    def test_gateway_command_lines(self, argv: list[str]) -> None:
        assert gateway_client._looks_like_gateway(argv)

    @pytest.mark.parametrize(
        "argv",
        [["zsh"], ["agentos", "chat"], ["pytest", "tests/test_gateway"], ["vim", "gateway", "run"]],
    )
    def test_other_command_lines(self, argv: list[str]) -> None:
        assert not gateway_client._looks_like_gateway(argv)

    def test_missing_or_empty_file_gets_neither(self, secret_file: Path) -> None:
        assert gateway_client.agent_surface_auth(_LOCAL) == {}
        secret_file.parent.mkdir()
        secret_file.write_text("\n")
        assert gateway_client.agent_surface_auth(_LOCAL) == {}

    def test_default_path_is_the_gateways(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv(gateway_client.OPERATOR_SECRET_FILE_ENV, raising=False)
        monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path))
        assert gateway_client._operator_secret_path() == (tmp_path / "wallets" / "operator.secret")


class _Ws:
    def __init__(self, sent: list[dict[str, Any]]) -> None:
        self._sent = sent
        self.frames = iter(
            [
                json.dumps({"type": "event", "event": "connect.challenge"}),
                json.dumps({"type": "hello-ok", "server": {"version": "x"}}),
            ]
        )

    async def recv(self) -> str:
        return next(self.frames)

    async def send(self, text: str) -> None:
        self._sent.append(json.loads(text))

    async def close(self) -> None:
        pass


@pytest.fixture
def handshakes(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    class _Websockets:
        @staticmethod
        async def connect(url: str) -> _Ws:
            return _Ws(sent)

    monkeypatch.setitem(sys.modules, "websockets", _Websockets)
    return sent


async def _connect(url: str, token: str | None) -> None:
    client = gateway_client.GatewayClient()
    await client.connect(url, token=token)
    await client.close()


class TestConnectFrame:
    @pytest.mark.asyncio
    async def test_token_only(
        self, handshakes: list[dict[str, Any]], secret_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        secret_file.parent.mkdir()
        secret_file.write_text("op-secret\n")
        monkeypatch.setenv(gateway_client.AGENT_TOKEN_ENV, "minted")
        await _connect(_LOCAL, "gw-token")
        assert handshakes[0]["params"]["auth"] == {"token": "gw-token", "agentToken": "minted"}

    @pytest.mark.asyncio
    async def test_file_only(self, handshakes: list[dict[str, Any]], secret_file: Path) -> None:
        secret_file.parent.mkdir()
        secret_file.write_text("op-secret\n")
        await _connect(_LOCAL, None)
        assert handshakes[0]["params"]["auth"] == {"operatorSecret": "op-secret"}

    @pytest.mark.asyncio
    async def test_neither(self, handshakes: list[dict[str, Any]], secret_file: Path) -> None:
        await _connect(_LOCAL, None)
        assert "auth" not in handshakes[0]["params"]
        handshakes.clear()
        secret_file.parent.mkdir()
        secret_file.write_text("op-secret\n")
        await _connect(_REMOTE, "gw-token")
        assert handshakes[0]["params"]["auth"] == {"token": "gw-token"}
