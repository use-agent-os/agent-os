"""``agentos trade bridge quote|create|status``: flags → RPC params, ``--wait``, rendering."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from typer.testing import CliRunner

from agentos.cli import trade_cmd
from agentos.cli.gateway_client import GatewayRPCError

runner = CliRunner()
WALLET = "0x1111111111111111111111111111111111111111"
SOLVER = "0x4cd00e387622c35bddb9b4c962c136462338bc31"
REQUEST = "0x" + "feed".rjust(64, "0")


def _order(status: str, **extra: Any) -> dict[str, Any]:
    return {
        "orderId": "ord-1",
        "kind": "bridge",
        "status": status,
        "chainId": 8453,
        "wallet": WALLET,
        "tokenIn": {"symbol": "USDC", "address": "0xusdc", "decimals": 6},
        "tokenOut": {"symbol": "USDG", "address": "0xusdg", "decimals": 6, "chainId": 4663},
        "amountIn": "25",
        "expectedOut": "24.93",
        "minOut": "24.68",
        "receivedOut": "24.93" if status == "confirmed" else None,
        "valueUsd": 25.0,
        "recipient": SOLVER,
        "recipientLabel": "Relay solver",
        "provider": "relay",
        "providerLabel": "Relay",
        "reason": None,
        "initiator": "manual",
        "txHash": "0xdep" if status in ("submitted", "confirmed") else None,
        "explorerUrl": "https://basescan.org/tx/0xdep" if status != "awaiting_approval" else None,
        "bridge": {
            "originChainId": 8453,
            "destinationChainId": 4663,
            "destinationChain": {"chainId": 4663, "name": "Robinhood Chain"},
            "asset": "USD",
            "requestId": REQUEST,
            "fillStatus": "success" if status == "confirmed" else None,
            "feesUsd": 0.03,
            "depositTxHash": "0xdep" if status != "awaiting_approval" else None,
            "destinationTxHash": "0xfill" if status == "confirmed" else None,
            "destinationExplorerUrl": (
                "https://robinhoodchain.blockscout.com/tx/0xfill" if status == "confirmed" else None
            ),
        },
        **extra,
    }


class _Client:
    def __init__(self, *, final: str = "confirmed", error: GatewayRPCError | None = None) -> None:
        self.final = final
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, params: dict | None = None) -> Any:
        self.calls.append((method, dict(params or {})))
        if self.error is not None:
            raise self.error
        if method == "trading.bridge.quote":
            return {
                "quote": {
                    "wallet": WALLET,
                    "originChainId": 8453,
                    "destinationChainId": 4663,
                    "route": {"from": "base", "to": "robinhood", "asset": "USD"},
                    "tokenIn": {"symbol": "USDC"},
                    "tokenOut": {"symbol": "USDG"},
                    "amountIn": "25",
                    "expectedOut": "24.93",
                    "minOut": "24.68",
                    "rate": 0.9972,
                    "feesUsd": 0.03,
                    "valueUsd": 25.0,
                    "timeEstimateSeconds": 1,
                    "balance": "1000",
                    "guard": {"decision": "needs_approval", "reason": "it waits for you"},
                    "requestId": REQUEST,
                }
            }
        if method == "trading.bridge.create":
            return {"order": _order("awaiting_approval")}
        if method == "trading.orders.wait":
            return {"order": _order(self.final)}
        if method == "trading.bridge.status":
            order = _order(self.final)
            return {"order": order, "bridge": order["bridge"]}
        if method == "trading.tokens.search":
            return {
                "tokens": [
                    {
                        "address": "0xabc0000000000000000000000000000000000001",
                        "symbol": "FOO",
                        "verified": True,
                    }
                ]
            }
        raise AssertionError(method)


def _use(monkeypatch: pytest.MonkeyPatch, client: _Client) -> _Client:
    monkeypatch.setattr(
        trade_cmd, "run_gateway_sync", lambda action, **kw: asyncio.run(action(client))
    )
    monkeypatch.setenv("COLUMNS", "220")
    monkeypatch.delenv("AGENTOS_SESSION_KEY", raising=False)
    monkeypatch.delenv("AGENTOS_AGENT", raising=False)
    return client


def test_quote_sends_the_chains_token_and_one_size(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _use(monkeypatch, _Client())
    result = runner.invoke(
        trade_cmd.app,
        [
            "bridge",
            "quote",
            "--chain",
            "base",
            "--to-chain",
            "robinhood",
            "--token",
            "usdc",
            "--amount",
            "25",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    method, params = client.calls[0]
    assert method == "trading.bridge.quote"
    assert params["chainId"] == 8453 and params["toChainId"] == 4663
    assert params["token"] == "USDC" and params["amount"] == "25"
    assert params["initiator"] == "manual" and "amountUsd" not in params
    assert json.loads(result.output)["quote"]["expectedOut"] == "24.93"


def test_quote_renders_a_table(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, _Client())
    result = runner.invoke(
        trade_cmd.app,
        [
            "bridge",
            "quote",
            "--chain",
            "base",
            "--to-chain",
            "robinhood",
            "--token",
            "USDC",
            "--usd",
            "25",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Bridge quote" in result.output and "USDG" in result.output
    assert "needs_approval" in result.output


def test_create_passes_note_client_id_and_waits_for_the_fill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _use(monkeypatch, _Client())
    monkeypatch.setenv("AGENTOS_SESSION_KEY", "agent:main:webchat:desk")
    result = runner.invoke(
        trade_cmd.app,
        [
            "bridge",
            "create",
            "--chain",
            "base",
            "--to-chain",
            "robinhood",
            "--token",
            "USDC",
            "--pct",
            "50",
            "--note",
            "user: move",
            "--client-id",
            "br-9",
            "--wait",
            "--wait-seconds",
            "120",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert [m for m, _ in client.calls] == ["trading.bridge.create", "trading.orders.wait"]
    params = client.calls[0][1]
    assert params["amountPct"] == 50.0 and params["note"] == "user: move"
    assert params["clientOrderId"] == "br-9" and params["initiator"] == "agent"
    assert params["sessionKey"] == "agent:main:webchat:desk"
    assert client.calls[1][1] == {"orderId": "ord-1", "timeoutSeconds": 120}
    order = json.loads(result.output)["order"]
    assert order["status"] == "confirmed" and order["bridge"]["destinationTxHash"] == "0xfill"


def test_create_without_wait_prints_the_order_and_the_approval_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _use(monkeypatch, _Client())
    result = runner.invoke(
        trade_cmd.app,
        [
            "bridge",
            "create",
            "--chain",
            "base",
            "--to-chain",
            "robinhood",
            "--token",
            "USDC",
            "--amount",
            "25",
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(client.calls) == 1
    assert "awaiting_approval" in result.output
    assert "agentos trade approve ord-1" in result.output
    assert "to chain" in result.output and "robinhood" in result.output


def test_an_address_token_is_resolved_and_a_symbol_is_not_searched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _use(monkeypatch, _Client())
    result = runner.invoke(
        trade_cmd.app,
        [
            "bridge",
            "quote",
            "--chain",
            "robinhood",
            "--to-chain",
            "base",
            "--token",
            "USDG",
            "--amount",
            "1",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert [m for m, _ in client.calls] == ["trading.bridge.quote"]
    assert client.calls[0][1]["token"] == "USDG"


@pytest.mark.parametrize(
    "args",
    [
        ["--amount", "1", "--usd", "5"],
        [],
        ["--pct", "0"],
        ["--usd", "-1"],
    ],
)
def test_sizing_flags_are_validated(monkeypatch: pytest.MonkeyPatch, args: list[str]) -> None:
    _use(monkeypatch, _Client())
    result = runner.invoke(
        trade_cmd.app,
        [
            "bridge",
            "quote",
            "--chain",
            "base",
            "--to-chain",
            "robinhood",
            "--token",
            "ETH",
            *args,
            "--json",
        ],
    )
    assert result.exit_code == 2, result.output
    assert json.loads(result.output.strip().splitlines()[-1])["error"]["code"] == "INVALID_ARGUMENT"


def test_an_unsupported_route_is_a_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    error = GatewayRPCError(
        "trading.bridge.create",
        code="trading.bridge.unsupported_route",
        message="WETH cannot be bridged from Base; only ETH, USDC can",
        data={
            "supportedRoutes": [
                {"from": "base", "to": "robinhood", "tokenIn": "ETH", "tokenOut": "ETH"}
            ]
        },
    )
    _use(monkeypatch, _Client(error=error))
    result = runner.invoke(
        trade_cmd.app,
        [
            "bridge",
            "create",
            "--chain",
            "base",
            "--to-chain",
            "robinhood",
            "--token",
            "ETH",
            "--amount",
            "1",
            "--json",
        ],
    )
    assert result.exit_code == 2, result.output
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["error"]["code"] == "trading.bridge.unsupported_route"
    assert payload["error"]["details"]["supportedRoutes"]


def test_status_picks_request_id_or_order_id(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _use(monkeypatch, _Client(final="submitted"))
    result = runner.invoke(trade_cmd.app, ["bridge", "status", REQUEST, "--json"])
    assert result.exit_code == 0, result.output
    assert client.calls[0] == ("trading.bridge.status", {"requestId": REQUEST})
    assert json.loads(result.output)["bridge"]["requestId"] == REQUEST

    client = _use(monkeypatch, _Client(final="confirmed"))
    result = runner.invoke(trade_cmd.app, ["bridge", "status", "ord-1"])
    assert result.exit_code == 0, result.output
    assert client.calls[0] == ("trading.bridge.status", {"orderId": "ord-1"})
    assert "0xfill" in result.output and "success" in result.output
    assert "24.93 USDG" in result.output


def test_orders_accepts_the_bridge_kind() -> None:
    assert "bridge" in trade_cmd.ORDER_KINDS
    legs = trade_cmd._order_legs(_order("confirmed"))
    assert legs == "25 USDC → robinhood (24.93 USDG)"
