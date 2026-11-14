"""The gateway-token operator rule reads the socket's peer, not ``X-Forwarded-For``.

A real uvicorn server with the gateway's protocol class: uvicorn's
proxy-header middleware (on by default) trusts loopback and rewrites
``scope["client"]`` from ``X-Forwarded-For``. A process on the gateway host
that claims a remote address that way must still get the agent's rules.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from collections.abc import AsyncIterator
from typing import Any

import uvicorn
import websockets

from agentos.gateway.app import create_gateway_app
from agentos.gateway.config import AuthConfig, GatewayConfig
from agentos.gateway.protocol import MAX_PAYLOAD_BYTES
from agentos.gateway.ws_transport import GatewayWebSocketProtocol

_TOKEN = "correct-horse-battery-staple-0123456789"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.asynccontextmanager
async def _gateway() -> AsyncIterator[str]:
    port = _free_port()
    gateway = GatewayConfig(auth=AuthConfig(mode="token", token=_TOKEN))
    config = uvicorn.Config(
        create_gateway_app(gateway),
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="off",
        ws=GatewayWebSocketProtocol,
        ws_max_size=MAX_PAYLOAD_BYTES,
        proxy_headers=True,  # uvicorn's default, as the gateway runs it
    )
    server = uvicorn.Server(config)
    setattr(server, "capture_signals", contextlib.nullcontext)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    try:
        yield f"ws://127.0.0.1:{port}/ws"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)


async def _rename_error(url: str, headers: dict[str, str]) -> dict[str, Any]:
    async with websockets.connect(url, additional_headers=headers) as ws:
        await ws.recv()  # challenge
        params = {"minProtocol": 3, "maxProtocol": 3, "auth": {"token": _TOKEN}}
        await ws.send(json.dumps({"type": "req", "id": "c", "method": "connect", "params": params}))
        assert json.loads(await ws.recv())["type"] == "hello-ok"
        await ws.send(json.dumps({"type": "req", "id": "r", "method": "wallet.rename"}))
        while True:
            frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            if frame.get("type") == "res" and frame.get("id") == "r":
                return dict(frame["error"])


async def test_loopback_peer_claiming_a_remote_address_is_still_an_agent() -> None:
    async with _gateway() as url:
        error = await _rename_error(url, {"X-Forwarded-For": "203.0.113.7"})
    assert error["code"] == "trading.operator_required"


async def test_loopback_peer_with_the_token_is_an_agent() -> None:
    async with _gateway() as url:
        error = await _rename_error(url, {})
    assert error["code"] == "trading.operator_required"


# ── the operator file, from a process the gateway spawned ────────────────

_CHILD_CLIENT = """
import asyncio, json, sys
import websockets

async def main(url, secret):
    async with websockets.connect(url) as ws:
        await ws.recv()
        params = {"minProtocol": 3, "maxProtocol": 3,
                  "auth": {"token": sys.argv[3], "operatorSecret": secret}}
        await ws.send(json.dumps({"type": "req", "id": "c", "method": "connect",
                                  "params": params}))
        assert json.loads(await ws.recv())["type"] == "hello-ok"
        await ws.send(json.dumps({"type": "req", "id": "r", "method": "wallet.rename"}))
        while True:
            frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            if frame.get("type") == "res" and frame.get("id") == "r":
                print(frame["error"]["code"])
                return

asyncio.run(main(sys.argv[1], sys.argv[2]))
"""


async def _rename_error_with_secret(url: str, secret: str) -> str:
    async with websockets.connect(url) as ws:
        await ws.recv()
        params = {
            "minProtocol": 3,
            "maxProtocol": 3,
            "auth": {"token": _TOKEN, "operatorSecret": secret},
        }
        await ws.send(json.dumps({"type": "req", "id": "c", "method": "connect", "params": params}))
        assert json.loads(await ws.recv())["type"] == "hello-ok"
        await ws.send(json.dumps({"type": "req", "id": "r", "method": "wallet.rename"}))
        while True:
            frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            if frame.get("type") == "res" and frame.get("id") == "r":
                return str(frame["error"]["code"])


async def test_operator_file_from_a_spawned_process_is_still_an_agent(tmp_path) -> None:
    """An agent's process that read operator.secret, with no exec window open.

    The gateway in this test is the pytest process; the client is its child,
    as every agent shell, ``execute_code`` run or ACP harness is the
    gateway's. The same secret from a process the gateway did not spawn (here:
    the test process itself) is the operator.
    """
    import sys

    import pytest

    from agentos.gateway import peer_process
    from agentos.gateway.agent_surface import get_agent_surface

    if not peer_process.supported():
        pytest.skip("needs Linux /proc to map sockets to processes")
    surface = get_agent_surface()
    surface.reset()
    secret = surface.ensure_operator_file(tmp_path).read_text().strip()
    try:
        async with _gateway() as url:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                _CHILD_CLIENT,
                url,
                secret,
                _TOKEN,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
            assert proc.returncode == 0, err.decode()
            assert out.decode().strip() == "trading.operator_required"
            # Control: the operator's own (non-descendant) connection.
            assert await _rename_error_with_secret(url, secret) == "INVALID_REQUEST"
    finally:
        surface.reset()
