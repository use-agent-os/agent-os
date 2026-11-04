"""RPC credentials stay on their configured chain, without live keys or wallets."""

from __future__ import annotations

import json
import traceback

import httpx
import pytest

from agentos.gateway.config import TradingConfig, redact_public_config
from agentos.trading.chains import BASE, ROBINHOOD
from agentos.trading.evm import EvmClient, EvmRpcError, EvmTransportError
from agentos.trading.service import TradingService

SECRET = "offline-rpc-test-credential"


@pytest.mark.asyncio
async def test_headers_are_chain_local_and_not_shared(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RH_TEST_RPC_KEY", SECRET)
    monkeypatch.setenv("BASE_TEST_RPC_KEY", "different-offline-credential")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        chain = "0x1237" if request.url.host == "rh.example" else "0x2105"
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": chain})

    config = TradingConfig(
        rpc_urls={"4663": "https://rh.example/rpc", "8453": "https://base.example/rpc"},
        rpc_header_env={
            "4663": {"x-api-key": "RH_TEST_RPC_KEY"},
            "8453": {"authorization": "BASE_TEST_RPC_KEY"},
        },
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        service = TradingService(config, http=http, background=False)
        assert await service.evm(ROBINHOOD).chain_id() == 4663
        assert await service.evm(BASE).chain_id() == 8453
        await http.get("https://relay.example/quote")
        assert seen[0].headers["x-api-key"] == SECRET
        assert "authorization" not in seen[0].headers
        assert seen[1].headers["authorization"] == "different-offline-credential"
        assert "x-api-key" not in seen[1].headers
        assert "authorization" not in seen[2].headers and "x-api-key" not in seen[2].headers
        assert "x-api-key" not in http.headers and "authorization" not in http.headers
    for view in (
        repr(config),
        config.model_dump_json(),
        json.dumps(redact_public_config({"trading": config.model_dump()})),
    ):
        assert SECRET not in view and "different-offline-credential" not in view


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value", [None, "", "  ", "bad\r\nheader", "non-ascii-密钥", " padded", "padded ", "ab\\c "]
)
async def test_missing_or_invalid_value_sends_nothing(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    monkeypatch.delenv("RH_TEST_RPC_KEY", raising=False)
    if value is not None:
        monkeypatch.setenv("RH_TEST_RPC_KEY", value)
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"result": "0x1237"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = EvmClient(
            "https://rh.example/rpc", http=http, header_env={"x-api-key": "RH_TEST_RPC_KEY"}
        )
        with pytest.raises(EvmTransportError):
            await client.chain_id()
    assert sent == []


@pytest.mark.parametrize(
    "name",
    [
        "host",
        "Content-Length",
        "Connection",
        "Transfer-Encoding",
        "accept",
        "USER-AGENT",
        "bad header",
        "x-key\r\n",
    ],
)
def test_protocol_header_overrides_are_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="header"):
        TradingConfig(rpc_header_env={"4663": {name: "RH_TEST_RPC_KEY"}})


def test_env_names_and_case_collisions_are_validated() -> None:
    with pytest.raises(ValueError):
        TradingConfig(rpc_header_env={"4663": {"x-api-key": "not an env name"}})
    with pytest.raises(ValueError):
        TradingConfig(
            rpc_header_env={
                "4663": {
                    "x-api-key": "RH_TEST_RPC_KEY",
                    "X-API-KEY": "OTHER_TEST_RPC_KEY",
                }
            }
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [None, "", "  "])
async def test_credentials_require_an_explicit_rpc_url(
    monkeypatch: pytest.MonkeyPatch, override: str | None
) -> None:
    monkeypatch.setenv("RH_TEST_RPC_KEY", SECRET)
    config = TradingConfig(
        rpc_header_env={"4663": {"x-api-key": "RH_TEST_RPC_KEY"}},
        rpc_urls={} if override is None else {"4663": override},
    )
    async with httpx.AsyncClient() as http:
        service = TradingService(config, http=http, background=False)
        with pytest.raises(EvmTransportError, match="rpc_urls"):
            service.evm(ROBINHOOD)


@pytest.mark.asyncio
async def test_redirects_cannot_forward_the_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RH_TEST_RPC_KEY", SECRET)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(307, headers={"location": "https://other.example/rpc"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as http:
        client = EvmClient(
            "https://rh.example/rpc", http=http, header_env={"x-api-key": "RH_TEST_RPC_KEY"}
        )
        with pytest.raises(EvmTransportError, match="307"):
            await client.chain_id()
    assert len(seen) == 1 and seen[0].url.host == "rh.example"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["http", "rpc", "batch", "transport"])
async def test_diagnostics_redact_echoed_credentials(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    monkeypatch.setenv("RH_TEST_RPC_KEY", SECRET)

    def handler(request: httpx.Request) -> httpx.Response:
        error = {"code": -32000, "message": f"rejected {SECRET}", "data": {"echo": SECRET}}
        if kind == "transport":
            raise httpx.ConnectError(f"connection rejected {SECRET}", request=request)
        if kind == "http":
            return httpx.Response(401, json={"error": error})
        body = {"jsonrpc": "2.0", "id": 1, "error": error}
        return httpx.Response(200, json=[body] if kind == "batch" else body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = EvmClient(
            "https://rh.example/rpc", http=http, header_env={"x-api-key": "RH_TEST_RPC_KEY"}
        )
        with pytest.raises((EvmTransportError, EvmRpcError)) as info:
            if kind == "batch":
                await client.batch([("eth_chainId", [])])
            else:
                await client.chain_id()
        assert SECRET not in "".join(traceback.format_exception(info.value))
        assert SECRET not in repr(getattr(info.value, "data", None))


def test_authenticated_rpc_requires_tls() -> None:
    with pytest.raises(EvmTransportError, match="https"):
        EvmClient("http://remote.example/rpc", header_env={"x-api-key": "RH_TEST_RPC_KEY"})


@pytest.mark.asyncio
async def test_batch_and_key_rotation_use_current_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RH_TEST_RPC_KEY", SECRET)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        if isinstance(body, list):
            return httpx.Response(
                200, json=[{"id": item["id"], "result": "0x1237"} for item in body]
            )
        return httpx.Response(200, json={"id": body["id"], "result": "0x1237"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = EvmClient(
            "https://rh.example/rpc", http=http, header_env={"x-api-key": "RH_TEST_RPC_KEY"}
        )
        assert await client.batch([("eth_chainId", []), ("eth_chainId", [])]) == [
            "0x1237",
            "0x1237",
        ]
        monkeypatch.setenv("RH_TEST_RPC_KEY", "rotated-offline-credential")
        assert await client.chain_id() == 4663
        assert seen[0].headers["x-api-key"] == SECRET
        assert seen[1].headers["x-api-key"] == "rotated-offline-credential"
        monkeypatch.delenv("RH_TEST_RPC_KEY")
        with pytest.raises(EvmTransportError):
            await client.chain_id()
        assert len(seen) == 2


@pytest.mark.asyncio
async def test_no_header_configuration_preserves_public_defaults() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": 1, "result": "0x1237"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        service = TradingService(TradingConfig(), http=http, background=False)
        assert await service.evm(ROBINHOOD).chain_id() == 4663
        assert str(seen[0].url) == ROBINHOOD.rpc_url
        assert "x-api-key" not in seen[0].headers and "authorization" not in seen[0].headers
