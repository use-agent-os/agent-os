"""LP entrypoints use the same scoped RPC transport as wallet reads (no live RPC)."""

from __future__ import annotations

import asyncio
import json
import traceback
import urllib.request
from typing import Any

import httpx
import pytest

from agentos.gateway.config import TradingConfig
from agentos.trading import lp
from agentos.trading.chains import BASE, ROBINHOOD
from agentos.trading.evm import EvmTransportError
from agentos.trading.service import TradingError, TradingService

SECRET = "offline-lp-test-credential"


def config() -> TradingConfig:
    return TradingConfig(
        rpc_urls={"4663": "https://rh.example/rpc"},
        rpc_header_env={"4663": {"x-api-key": "RH_TEST_RPC_KEY"}},
    )


@pytest.fixture(autouse=True)
def no_external_rpc(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("LP bypassed the engine RPC transport")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)


@pytest.mark.asyncio
async def test_lp_position_sends_header_on_initial_head_and_position_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RH_TEST_RPC_KEY", SECRET)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["x-api-key"] == SECRET
        body = json.loads(request.content)
        return httpx.Response(200, json={"id": body["id"], "result": "0x1237"})

    def position(env: lp.ChainEnv, token_id: int) -> dict[str, Any]:
        return {"head": env.block, "chain": env.client.chain_id(), "tokenId": token_id}

    monkeypatch.setattr(lp, "build_position", position)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        service = TradingService(config(), http=http, background=False)
        assert await service.evm(ROBINHOOD).chain_id() == 4663
        assert await lp.lp_position(service, chain=ROBINHOOD, token_id=1) == {
            "head": 4663,
            "chain": 4663,
            "tokenId": 1,
        }
    assert len(seen) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["redirect", "http", "rpc", "batch", "transport"])
async def test_lp_transport_refuses_redirects_and_redacts_errors(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    monkeypatch.setenv("RH_TEST_RPC_KEY", SECRET)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.host == "rh.example"
        assert request.headers["x-api-key"] == SECRET
        if kind == "redirect":
            return httpx.Response(302, headers={"location": "https://sink.example/"})
        if kind == "transport":
            raise httpx.ConnectError(f"rejected {SECRET}", request=request)
        error = {"code": -32000, "message": f"rejected {SECRET}", "data": {"echo": SECRET}}
        body = {"id": 0 if kind == "batch" else 1, "error": error}
        return httpx.Response(
            401 if kind == "http" else 200, json=[body] if kind == "batch" else body
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as http:
        service = TradingService(config(), http=http, background=False)
        env = lp._engine_env(service, ROBINHOOD, asyncio.get_running_loop())
        if kind == "batch":
            result = await asyncio.to_thread(env.client.batch, [{"method": "eth_chainId"}])
            assert SECRET not in repr(result)
            assert result[0]["error"]["data"]["echo"] == "[redacted]"
        else:
            with pytest.raises((TradingError, EvmTransportError, RuntimeError)) as exc:
                await lp.lp_position(service, chain=ROBINHOOD, token_id=1)
            assert SECRET not in "".join(traceback.format_exception(exc.value))
            if kind == "redirect":
                assert "302" in str(exc.value)
        assert len(seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, " leading", "ab\\c ", "bad\r\nvalue"])
async def test_lp_invalid_credentials_do_not_send(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    monkeypatch.delenv("RH_TEST_RPC_KEY", raising=False)
    if value is not None:
        monkeypatch.setenv("RH_TEST_RPC_KEY", value)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        raise AssertionError("invalid credentials reached HTTP")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        service = TradingService(config(), http=http, background=False)
        with pytest.raises((TradingError, EvmTransportError), match="missing or invalid"):
            await lp.lp_position(service, chain=ROBINHOOD, token_id=1)
    assert seen == []


@pytest.mark.asyncio
async def test_lp_batch_rotation_and_fallback_never_share_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RH_TEST_RPC_KEY", SECRET)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        if isinstance(body, list):
            return httpx.Response(200, json=[{"id": e["id"], "result": "0x1237"} for e in body])
        return httpx.Response(200, json={"id": body["id"], "result": "0x1237"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        service = TradingService(config(), http=http, background=False)
        env = lp._engine_env(service, ROBINHOOD, asyncio.get_running_loop())
        public = lp._engine_env(service, BASE, asyncio.get_running_loop())
        assert public.client.url == BASE.rpc_url
        assert env.log_client is not None and env.log_client.url == ROBINHOOD.rpc_url
        assert await asyncio.to_thread(env.client.batch, [{"method": "eth_chainId"}]) == ["0x1237"]
        monkeypatch.setenv("RH_TEST_RPC_KEY", "rotated-offline-lp-credential")
        assert await asyncio.to_thread(env.client.chain_id) == 4663
        assert seen[0].headers["x-api-key"] == SECRET
        assert seen[1].headers["x-api-key"] == "rotated-offline-lp-credential"
        assert "x-api-key" not in http.headers
        monkeypatch.delenv("RH_TEST_RPC_KEY")
        with pytest.raises(EvmTransportError):
            await asyncio.to_thread(env.client.chain_id)
        assert len(seen) == 2
