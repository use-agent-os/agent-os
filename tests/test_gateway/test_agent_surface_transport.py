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
