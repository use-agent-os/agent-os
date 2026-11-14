"""AOS-17: http_request must not reach the gateway's own control API.

``http_request`` stays available during plan mode and sends a model-chosen
method and body. In ``auth.mode = "none"`` the gateway admits loopback HTTP
without credentials, so prompt-injected content could have the model POST
``plan.mode.set off`` to ``127.0.0.1:18791`` and lift plan mode itself.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
from collections.abc import Awaitable, Callable, Iterator
from typing import Any, cast

import httpcore
import pytest
from starlette.testclient import TestClient

from agentos.gateway.agent_surface import get_agent_surface
from agentos.gateway.app import create_gateway_app
from agentos.gateway.config import GatewayConfig
from agentos.plan_mode import get_plan_mode_store, reset_plan_mode_store
from agentos.sandbox.integration import configure_runtime
from agentos.sandbox.policy import SandboxSettings
from agentos.tools import ssrf
from agentos.tools.builtin import web
from agentos.tools.ssrf_client import (
    ValidatingNetworkBackend,
    validate_agent_http_address,
    validate_metadata_only_address,
)
from agentos.tools.types import SSRFBlockedError

HttpRequest = Callable[..., Awaitable[str]]
_KEY = "agent:main:main"
_PLAN_OFF = '{"method":"plan.mode.set","params":{"key":"agent:main:main","mode":"off"}}'


@pytest.fixture(autouse=True)
def _no_published_listener() -> Iterator[None]:
    ssrf.clear_gateway_listener()
    yield
    ssrf.clear_gateway_listener()


class _RecordingBackend(httpcore.AsyncNetworkBackend):
    def __init__(self) -> None:
        self.connections: list[tuple[str, int]] = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.connections.append((host, port))
        return object()

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        return object()

    async def sleep(self, seconds: float) -> None:
        return None


# ── the validator ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "127.8.9.10", "::1", "0.0.0.0", "::", "::ffff:127.0.0.1"],
)
def test_agent_validator_refuses_default_gateway_port_on_this_host(address: str) -> None:
    with pytest.raises(SSRFBlockedError, match="gateway"):
        validate_agent_http_address("h", ipaddress.ip_address(address), ssrf.DEFAULT_GATEWAY_PORT)


def test_agent_validator_follows_the_published_listener(monkeypatch) -> None:
    import agentos.gateway.access as access

    monkeypatch.setattr(access, "local_interface_addresses", lambda: frozenset({"192.168.1.20"}))
    ssrf.set_gateway_listener(["0.0.0.0"], 28000)
    for addr in ("127.0.0.1", "192.168.1.20", "0.0.0.0"):
        with pytest.raises(SSRFBlockedError):
            validate_agent_http_address("h", ipaddress.ip_address(addr), 28000)
    # The default port is no longer the gateway; other hosts on the port are.
    validate_agent_http_address("h", ipaddress.ip_address("127.0.0.1"), ssrf.DEFAULT_GATEWAY_PORT)
    validate_agent_http_address("h", ipaddress.ip_address("192.168.1.99"), 28000)


def test_agent_validator_keeps_local_dev_servers_reachable() -> None:
    for port in (80, 3000, 8080, 18790):
        validate_agent_http_address("localhost", ipaddress.ip_address("127.0.0.1"), port)


def test_metadata_only_validator_is_unchanged() -> None:
    """MCP servers and cron webhooks keep the plain metadata floor."""
    validate_metadata_only_address(
        "localhost", ipaddress.ip_address("127.0.0.1"), ssrf.DEFAULT_GATEWAY_PORT
    )


async def test_backend_threads_the_port_to_the_validator(monkeypatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))
        ],
    )
    inner = _RecordingBackend()
    backend = ValidatingNetworkBackend(validate_agent_http_address, inner)
    with pytest.raises(SSRFBlockedError):
        await backend.connect_tcp("rebind.example", ssrf.DEFAULT_GATEWAY_PORT)
    assert inner.connections == []
    await backend.connect_tcp("rebind.example", 3000)
    assert inner.connections == [("127.0.0.1", 3000)]


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:18791/api/v1/rpc",
        "http://localhost:18791/api/v1/rpc",
        "http://[::1]:18791/api/v1/rpc",
        "http://0.0.0.0:18791/api/v1/rpc",
    ],
)
def test_url_check_refuses_gateway_aliases(url: str) -> None:
    with pytest.raises(SSRFBlockedError):
        ssrf.assert_not_gateway_endpoint(url)


# ── the tool, against a real listener ──────────────────────────────────────


class _Listener:
    """A local TCP listener that records whether anything connected."""

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self._sock.settimeout(2.0)
        self.port = self._sock.getsockname()[1]
        self.connected = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        self.connected.set()
        with conn:
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")

    def close(self) -> None:
        self._sock.close()


@pytest.mark.asyncio
async def test_http_request_refuses_the_published_gateway_listener(tmp_path) -> None:
    configure_runtime(
        SandboxSettings(sandbox=False, security_grading=False, allow_legacy_mode=True),
        workspace=tmp_path,
    )
    handler = cast(HttpRequest, web.http_request.__wrapped__.__wrapped__)
    listener = _Listener()
    try:
        ssrf.set_gateway_listener(["127.0.0.1"], listener.port)
        for host in ("127.0.0.1", "localhost"):
            with pytest.raises(SSRFBlockedError):
                await handler(
                    url=f"http://{host}:{listener.port}/api/v1/rpc",
                    method="POST",
                    headers={"content-type": "application/json"},
                    body=_PLAN_OFF,
                )
        assert not listener.connected.is_set()

        # The same listener is an ordinary dev server once it is not the gateway.
        ssrf.clear_gateway_listener()
        result = await handler(url=f"http://127.0.0.1:{listener.port}/", method="GET")
        assert listener.connected.wait(2.0)
        assert '"status": 200' in result
    finally:
        listener.close()


# ── plan.mode.set off from an agent-bound connection ───────────────────────


@pytest.fixture
def _plan_client() -> Iterator[TestClient]:
    reset_plan_mode_store()
    get_agent_surface().reset()
    app = create_gateway_app(GatewayConfig())
    client = TestClient(app, client=("127.0.0.1", 50000), base_url="http://127.0.0.1:18791")
    get_plan_mode_store().enable(_KEY)
    yield client
    reset_plan_mode_store()
    get_agent_surface().reset()


def _plan_off(client: TestClient, headers: dict[str, str] | None = None) -> Any:
    return client.post(
        "/api/v1/rpc",
        content=_PLAN_OFF,
        headers={"content-type": "application/json", **(headers or {})},
    )


def test_agent_token_cannot_turn_plan_mode_off(_plan_client: TestClient) -> None:
    token = get_agent_surface().mint_token(_KEY, "main")
    resp = _plan_off(_plan_client, {"x-agentos-agent-token": token})
    assert "plan.operator_required" in resp.text
    assert get_plan_mode_store().is_enabled(_KEY)


def test_exec_window_connection_cannot_turn_plan_mode_off(_plan_client: TestClient) -> None:
    with get_agent_surface().exec_window("agent:main:other"):
        resp = _plan_off(_plan_client)
    assert "plan.operator_required" in resp.text
    assert get_plan_mode_store().is_enabled(_KEY)


def test_operator_can_still_turn_plan_mode_off(_plan_client: TestClient) -> None:
    surface = get_agent_surface()
    surface.set_operator_secret("operator-secret-aos17")
    with surface.exec_window("agent:main:other"):
        resp = _plan_off(_plan_client, {"x-agentos-operator-secret": "operator-secret-aos17"})
    assert resp.status_code == 200
    assert resp.json()["payload"]["planMode"] is False


def test_web_console_unbound_can_still_approve_a_plan(_plan_client: TestClient) -> None:
    resp = _plan_off(_plan_client)
    assert resp.status_code == 200
    assert resp.json()["payload"]["planMode"] is False


def test_agent_may_still_turn_plan_mode_on(_plan_client: TestClient) -> None:
    get_plan_mode_store().disable(_KEY)
    token = get_agent_surface().mint_token(_KEY, "main")
    resp = _plan_client.post(
        "/api/v1/rpc",
        json={"method": "plan.mode.set", "params": {"key": _KEY, "mode": "on"}},
        headers={"x-agentos-agent-token": token},
    )
    assert resp.status_code == 200
    assert get_plan_mode_store().is_enabled(_KEY)
