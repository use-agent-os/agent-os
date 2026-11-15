"""``agentos trade`` CLI surface tests.

The gateway round-trip is stubbed at ``run_gateway_sync``: these pin the RPC
names and params the CLI sends, symbol → address resolution, the
agent/manual initiator rule, ``--wait`` polling, and rendered output. The
guardrails themselves live in the engine and are tested there.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from click.exceptions import BadParameter
from typer.testing import CliRunner

from agentos.cli import trade_cmd
from agentos.cli.wallet_cmd import NATIVE_ADDRESS, PASSWORD_ENV

runner = CliRunner()

WALLET = "0x1111111111111111111111111111111111111111"
WALLET_B = "0x2222222222222222222222222222222222222222"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
WETH = "0x4200000000000000000000000000000000000006"
FAKE_AAPL = "0x7e86381a763f0ecca2bdf27c54eac403ddd48123"
REAL_AAPL = "0x1b0e319c6a659f002271b69db8a7df2f911c153e"

TOKENS = {
    "USDC": {"symbol": "USDC", "address": USDC, "verified": True, "name": "USD Coin"},
    "WETH": {"symbol": "WETH", "address": WETH, "verified": True, "name": "Wrapped Ether"},
}


def _order(**extra: Any) -> dict[str, Any]:
    base = {
        "orderId": "ord-1",
        "chainId": 8453,
        "wallet": WALLET,
        "tokenIn": TOKENS["USDC"],
        "tokenOut": TOKENS["WETH"],
        "amountIn": "10",
        "expectedOut": "0.0025",
        "valueUsd": 10.0,
        "status": "submitted",
        "txHash": "0xabc",
        "initiator": "manual",
    }
    base.update(extra)
    return base


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.search_results: dict[str, list[dict[str, Any]]] = {
            "usdc": [TOKENS["USDC"]],
            "weth": [TOKENS["WETH"]],
            "aapl": [
                {"symbol": "AAPL", "address": REAL_AAPL, "verified": True, "name": "Apple"},
                {"symbol": "AAPL", "address": FAKE_AAPL, "verified": False, "name": "Apple"},
            ],
            "gme": [
                {"symbol": "GME", "address": FAKE_AAPL, "verified": False},
            ],
            "dup": [
                {"symbol": "DUP", "address": USDC, "verified": True},
                {"symbol": "DUP", "address": WETH, "verified": True},
            ],
        }
        self.payloads: dict[str, Any] = {
            "config.set": {"restartRequired": False},
            "trading.status": {
                "enabled": True,
                "provider": "aggregator",
                "providers": [
                    {
                        "id": "aggregator",
                        "label": "AgentOS Aggregator",
                        "needsKey": False,
                        "keyConfigured": True,
                        "healthy": True,
                    },
                    {
                        "id": "uniswap",
                        "label": "Uniswap",
                        "needsKey": True,
                        "keyConfigured": True,
                        "healthy": None,
                    },
                ],
                "apiKeyConfigured": True,
                "unlocked": True,
                "unlockMode": "auto",
                "limits": {
                    "approvalThresholdUsd": 100,
                    "dailyCapUsd": 1000,
                    "approvalTtlSeconds": 900,
                },
                "chains": [
                    {
                        "chainId": 8453,
                        "key": "base",
                        "name": "Base",
                        "rpcUrl": "https://mainnet.base.org",
                        "healthy": True,
                    }
                ],
                "syncing": False,
            },
            "trading.probe": {"ok": True, "latencyMs": 120, "error": None},
            "trading.quote": {
                "quoteId": "q1",
                "routing": "CLASSIC",
                "tokenIn": TOKENS["USDC"],
                "tokenOut": TOKENS["WETH"],
                "amountIn": "10",
                "amountOut": "0.0025",
                "minOut": "0.00248",
                "rate": "0.00025",
                "valueUsd": 10.0,
                "priceImpactPct": 0.01,
                "gasUsd": 0.02,
                "slippagePct": 0.5,
                "expiresAt": 999,
                "guard": {
                    "decision": "allow",
                    "spentTodayUsd": 0,
                    "dailyCapUsd": 1000,
                    "thresholdUsd": 100,
                },
            },
            "trading.swap": {"orders": [_order()]},
            "trading.orders.wait": {"order": _order(status="confirmed")},
            "trading.orders.list": {"orders": [_order()], "pendingApprovals": 0},
            "trading.orders.get": {"order": _order()},
            "trading.orders.approve": {"order": _order(status="approved")},
            "trading.orders.reject": {"order": _order(status="rejected", reason="user")},
            "trading.history": {
                "entries": [
                    {
                        "id": 1,
                        "ts": 1700000000,
                        "chainId": 8453,
                        "wallet": WALLET,
                        "kind": "swap",
                        "txHash": "0xabc",
                        "tokenIn": TOKENS["USDC"],
                        "amountIn": "10",
                        "tokenOut": TOKENS["WETH"],
                        "amountOut": "0.0025",
                        "valueUsd": 10,
                        "gasUsd": 0.02,
                        "initiator": "agent",
                    }
                ],
                "nextBefore": None,
            },
            "trading.portfolio": {
                "totals": {
                    "valueUsd": 100,
                    "costUsd": 90,
                    "unrealizedUsd": 10,
                    "realizedUsd": 2,
                    "gasUsd": 0.5,
                    "change24hUsd": 1,
                    "change24hPct": 1.0,
                },
                "holdings": [
                    {
                        "chainId": 8453,
                        "token": TOKENS["WETH"],
                        "amount": "0.05",
                        "priceUsd": 2000,
                        "valueUsd": 100,
                        "avgCostUsd": 1800,
                        "unrealizedUsd": 10,
                        "unrealizedPct": 11.1,
                        "realizedUsd": 2,
                        "allocationPct": 100,
                    }
                ],
                "hiddenCount": 2,
                "syncing": True,
            },
            "trading.tokens.hide": {"token": {**TOKENS["USDC"], "hidden": True}},
            "trading.sync": {"started": True},
            "trading.limits": {
                "dailyCapUsd": 1000,
                "spentTodayUsd": 25,
                # The engine's key (service._limits_dict); a fixture faking a
                # different name once hid a CLI that printed "—" for it.
                "approvalThresholdUsd": 100,
                "approvalTtlSeconds": 900,
            },
        }

    async def call(self, method: str, params: dict | None = None) -> Any:
        params = dict(params or {})
        self.calls.append((method, params))
        if method == "trading.tokens.search":
            return {"tokens": self.search_results.get(str(params["query"]).lower(), [])}
        return self.payloads[method]

    def calls_to(self, method: str) -> list[dict[str, Any]]:
        return [params for name, params in self.calls if name == method]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    fake = _FakeClient()

    def _run(action, **kwargs):
        return asyncio.run(action(fake))

    monkeypatch.setattr(trade_cmd, "run_gateway_sync", _run)
    for name in (*trade_cmd.AGENT_ENV_MARKERS, PASSWORD_ENV):
        monkeypatch.delenv(name, raising=False)
    # Rich wraps tables at 80 columns under CliRunner (and an earlier test may
    # have pinned the shared console's width); addresses must survive intact.
    monkeypatch.setenv("COLUMNS", "220")
    monkeypatch.setattr(trade_cmd.console, "_width", 220)
    return fake


def test_status_renders(client: _FakeClient) -> None:
    result = runner.invoke(trade_cmd.app, ["status"])
    assert result.exit_code == 0, result.output
    assert "configured" in result.output
    assert "$100.00" in result.output
    assert "mainnet.base.org" in result.output
    assert client.calls == [("trading.status", {})]


def test_status_shows_provider_and_provider_table(client: _FakeClient) -> None:
    result = runner.invoke(trade_cmd.app, ["status"])
    assert result.exit_code == 0, result.output
    assert "AgentOS Aggregator" in result.output
    assert "Uniswap" in result.output
    assert "not needed" in result.output
    # The active provider gets the star, the other one does not.
    lines = result.output.splitlines()
    agg_line = next(line for line in lines if "Aggregator" in line and "not needed" in line)
    uniswap_line = next(
        line for line in lines if "Uniswap" in line and "API key" not in line and "★" not in line
    )
    assert "★" in agg_line and "★" not in uniswap_line


def test_probe_passes_key_and_reports(client: _FakeClient) -> None:
    ok = runner.invoke(trade_cmd.app, ["probe", "--api-key", "k1"])
    assert ok.exit_code == 0, ok.output
    assert client.calls == [("trading.probe", {"apiKey": "k1"})]
    assert "OK" in ok.output and "120 ms" in ok.output

    client.payloads["trading.probe"] = {"ok": False, "latencyMs": None, "error": "401"}
    bad = runner.invoke(trade_cmd.app, ["probe"])
    assert bad.exit_code == 1
    assert "401" in bad.output
    assert client.calls[-1] == ("trading.probe", {})


def test_probe_provider_flag(client: _FakeClient) -> None:
    client.payloads["trading.probe"] = {
        "ok": True,
        "provider": "aggregator",
        "latencyMs": 80,
        "error": None,
    }
    ok = runner.invoke(trade_cmd.app, ["probe", "--provider", "AGG"])
    assert ok.exit_code == 0, ok.output
    assert client.calls == [("trading.probe", {"provider": "aggregator"})]
    assert "AgentOS Aggregator OK" in ok.output

    client.payloads["trading.probe"] = {
        "ok": False,
        "provider": "aggregator",
        "latencyMs": None,
        "error": "Aggregator unreachable",
    }
    down = runner.invoke(trade_cmd.app, ["probe", "--provider", "aggregator"])
    assert down.exit_code == 1
    assert "Aggregator unreachable" in down.output

    as_json = runner.invoke(trade_cmd.app, ["probe", "--provider", "aggregator", "--json"])
    assert as_json.exit_code == 1
    assert json.loads(as_json.stdout)["ok"] is False

    unknown = runner.invoke(trade_cmd.app, ["probe", "--provider", "1inch"])
    assert unknown.exit_code != 0
    assert "unknown provider" in unknown.output


def test_provider_show_and_switch(client: _FakeClient) -> None:
    shown = runner.invoke(trade_cmd.app, ["provider"])
    assert shown.exit_code == 0, shown.output
    assert "AgentOS Aggregator" in shown.output
    assert client.calls == [("trading.status", {})]

    shown_json = runner.invoke(trade_cmd.app, ["provider", "--json"])
    payload = json.loads(shown_json.stdout)
    assert payload["provider"] == "aggregator"
    assert [p["id"] for p in payload["providers"]] == ["aggregator", "uniswap"]

    switched = runner.invoke(trade_cmd.app, ["provider", "uniswap"])
    assert switched.exit_code == 0, switched.output
    assert client.calls[-1] == ("config.set", {"path": "trading.provider", "value": "uniswap"})
    assert "Uniswap" in switched.output
    assert "needs an API key" in switched.output

    back = runner.invoke(trade_cmd.app, ["provider", "aggregator", "--json"])
    assert back.exit_code == 0, back.output
    assert json.loads(back.stdout) == {"provider": "aggregator", "restartRequired": False}

    bad = runner.invoke(trade_cmd.app, ["provider", "0x"])
    assert bad.exit_code != 0
    assert "unknown provider" in bad.output


def test_provider_helpers() -> None:
    assert trade_cmd.provider_id_from_arg("Uniswap") == "uniswap"
    assert trade_cmd.provider_id_from_arg("AGG") == "aggregator"
    assert trade_cmd.provider_id_from_arg("aggregator") == "aggregator"
    assert trade_cmd.provider_label("aggregator") == "AgentOS Aggregator"
    assert trade_cmd.provider_label(None) == "—"
    assert trade_cmd.provider_label("other") == "other"
    with pytest.raises(BadParameter):
        trade_cmd.provider_id_from_arg("kyber")


def test_tokens_search(client: _FakeClient) -> None:
    result = runner.invoke(trade_cmd.app, ["tokens", "--chain", "robinhood", "aapl"])
    assert result.exit_code == 0, result.output
    assert client.calls == [("trading.tokens.search", {"chainId": 4663, "query": "aapl"})]
    assert REAL_AAPL in result.output and "✓" in result.output


def test_quote_resolves_symbols_and_eth(client: _FakeClient) -> None:
    result = runner.invoke(
        trade_cmd.app,
        ["quote", "--chain", "base", "--in", "ETH", "--out", "usdc", "--amount", "0.01"],
    )
    assert result.exit_code == 0, result.output
    assert client.calls_to("trading.tokens.search") == [{"chainId": 8453, "query": "usdc"}]
    assert client.calls_to("trading.quote") == [
        {
            "chainId": 8453,
            "tokenIn": NATIVE_ADDRESS,
            "tokenOut": USDC,
            "amountIn": "0.01",
            "initiator": "manual",
        }
    ]
    assert "0.0025" in result.output
    assert "allow" in result.output


def test_usd_sizing_reaches_the_engine(client: _FakeClient) -> None:
    quote = runner.invoke(
        trade_cmd.app, ["quote", "--chain", "base", "--in", "ETH", "--out", USDC, "--usd", "5"]
    )
    swap = runner.invoke(
        trade_cmd.app,
        ["swap", "--chain", "base", "--in", "ETH", "--out", USDC, "--usd", "0.1", "--json"],
    )
    assert quote.exit_code == 0 and swap.exit_code == 0, quote.output + swap.output
    assert client.calls_to("trading.quote")[0]["amountUsd"] == 5
    assert "amountIn" not in client.calls_to("trading.quote")[0]
    assert client.calls_to("trading.swap")[0]["amountUsd"] == 0.1
    both = runner.invoke(
        trade_cmd.app,
        ["quote", "--chain", "base", "--in", "ETH", "--out", USDC, "--amount", "1", "--usd", "5"],
    )
    assert both.exit_code == 2 and "--amount or --usd" in both.output


def test_quote_passes_wallet_and_slippage_and_address_untouched(client: _FakeClient) -> None:
    result = runner.invoke(
        trade_cmd.app,
        [
            "quote",
            "--chain",
            "base",
            "--in",
            USDC,
            "--out",
            WETH,
            "--amount",
            "10",
            "--wallet",
            WALLET,
            "--slippage",
            "1.5",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert client.calls_to("trading.tokens.search") == []
    assert client.calls_to("trading.quote") == [
        {
            "chainId": 8453,
            "tokenIn": USDC,
            "tokenOut": WETH,
            "amountIn": "10",
            "initiator": "manual",
            "wallet": WALLET,
            "slippagePct": 1.5,
        }
    ]
    assert json.loads(result.stdout)["quoteId"] == "q1"


def test_symbol_resolution_prefers_the_verified_token(client: _FakeClient) -> None:
    result = runner.invoke(
        trade_cmd.app,
        ["quote", "--chain", "robinhood", "--in", "USDC", "--out", "AAPL", "--amount", "1"],
    )
    assert result.exit_code == 0, result.output
    assert client.calls_to("trading.quote")[0]["tokenOut"] == REAL_AAPL


@pytest.mark.parametrize(
    ("symbol", "code"),
    [("GME", "TOKEN_UNVERIFIED"), ("DUP", "TOKEN_AMBIGUOUS"), ("NOPE", "TOKEN_NOT_FOUND")],
)
def test_symbol_resolution_refuses_unverified_ambiguous_missing(
    client: _FakeClient, symbol: str, code: str
) -> None:
    result = runner.invoke(
        trade_cmd.app,
        [
            "quote",
            "--chain",
            "robinhood",
            "--in",
            "USDC",
            "--out",
            symbol,
            "--amount",
            "1",
            "--json",
        ],
    )
    assert result.exit_code == 2
    assert result.stdout == ""
    assert code in result.output
    assert client.calls_to("trading.quote") == []


def test_swap_manual_by_default_with_amount(client: _FakeClient) -> None:
    result = runner.invoke(
        trade_cmd.app,
        ["swap", "--chain", "base", "--in", "USDC", "--out", "WETH", "--amount", "10"],
    )
    assert result.exit_code == 0, result.output
    assert client.calls_to("trading.swap") == [
        {
            "chainId": 8453,
            "tokenIn": USDC,
            "tokenOut": WETH,
            "initiator": "manual",
            "amountIn": "10",
        }
    ]
    assert "ord-1" in result.output and "submitted" in result.output


def test_swap_as_agent_from_env_carries_session_key(
    client: _FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTOS_SESSION_KEY", "agent:main:webchat:abc")
    result = runner.invoke(
        trade_cmd.app,
        [
            "swap",
            "--chain",
            "base",
            "--in",
            "USDC",
            "--out",
            "WETH",
            "--pct",
            "50",
            "--wallet",
            WALLET,
            "--wallet",
            WALLET_B,
            "--slippage",
            "1",
            "--note",
            "DCA",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert client.calls_to("trading.swap") == [
        {
            "chainId": 8453,
            "tokenIn": USDC,
            "tokenOut": WETH,
            "initiator": "agent",
            "amountPct": 50.0,
            "wallets": [WALLET, WALLET_B],
            "slippagePct": 1.0,
            "note": "DCA",
            "sessionKey": "agent:main:webchat:abc",
        }
    ]


def test_swap_and_send_client_id_reach_the_gateway(client: _FakeClient) -> None:
    result = runner.invoke(
        trade_cmd.app,
        [
            "swap",
            "--chain",
            "base",
            "--in",
            "USDC",
            "--out",
            "WETH",
            "--amount",
            "10",
            "--client-id",
            " dca-2026-09-20 ",
        ],
    )
    assert result.exit_code == 0, result.output
    assert client.calls_to("trading.swap")[0]["clientOrderId"] == "dca-2026-09-20"
    # Absent or blank: the key is not sent at all.
    runner.invoke(
        trade_cmd.app,
        ["swap", "--chain", "base", "--in", "USDC", "--out", "WETH", "--amount", "10"],
    )
    assert "clientOrderId" not in client.calls_to("trading.swap")[1]
    client.payloads["trading.send"] = {"orders": [_send_order()], "batchId": None}
    result = runner.invoke(
        trade_cmd.app,
        [
            "send",
            "--chain",
            "base",
            "--token",
            "USDC",
            "--to",
            WALLET_B,
            "--amount",
            "1",
            "--client-id",
            "rent-09",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert client.calls_to("trading.send")[0]["clientOrderId"] == "rent-09"
    for command in ("swap", "send"):
        help_text = runner.invoke(trade_cmd.app, [command, "--help"]).output
        assert "--client-id" in help_text and "Idempotency" in help_text


def test_swap_as_agent_flag_and_all_wallets(client: _FakeClient) -> None:
    result = runner.invoke(
        trade_cmd.app,
        [
            "swap",
            "--chain",
            "robinhood",
            "--in",
            "ETH",
            "--out",
            USDC,
            "--amount",
            "0.1",
            "--all-wallets",
            "--as-agent",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    params = client.calls_to("trading.swap")[0]
    assert params["initiator"] == "agent"
    assert params["wallets"] == "all"
    assert params["chainId"] == 4663
    assert "sessionKey" not in params


def test_swap_agent_env_marker_without_session_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTOS_AGENT", "main")
    assert trade_cmd.initiator_for(False) == "agent"
    assert trade_cmd.initiator_for(False, {}) == "manual"
    assert trade_cmd.initiator_for(True, {}) == "agent"
    assert trade_cmd.initiator_for(False, {"AGENTOS_SESSION_KEY": "  "}) == "manual"


def test_swap_argument_validation(client: _FakeClient) -> None:
    common = ["swap", "--chain", "base", "--in", "USDC", "--out", "WETH"]
    assert runner.invoke(trade_cmd.app, common).exit_code != 0
    assert runner.invoke(trade_cmd.app, [*common, "--amount", "1", "--pct", "5"]).exit_code != 0
    assert runner.invoke(trade_cmd.app, [*common, "--pct", "150"]).exit_code != 0
    assert (
        runner.invoke(
            trade_cmd.app, [*common, "--amount", "1", "--wallet", WALLET, "--all-wallets"]
        ).exit_code
        != 0
    )
    assert client.calls == []


def test_swap_wait_polls_pending_orders_only(client: _FakeClient) -> None:
    client.payloads["trading.swap"] = {
        "orders": [
            _order(orderId="ord-1", status="awaiting_approval"),
            _order(orderId="ord-2", wallet=WALLET_B, status="rejected", reason="daily cap"),
        ]
    }
    result = runner.invoke(
        trade_cmd.app,
        [
            "swap",
            "--chain",
            "base",
            "--in",
            "USDC",
            "--out",
            "WETH",
            "--amount",
            "10",
            "--wait",
            "--wait-seconds",
            "30",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert client.calls_to("trading.orders.wait") == [{"orderId": "ord-1", "timeoutSeconds": 30}]
    payload = json.loads(result.stdout)
    assert [o["status"] for o in payload["orders"]] == ["confirmed", "rejected"]


def test_swap_prints_approval_hint(client: _FakeClient) -> None:
    client.payloads["trading.swap"] = {"orders": [_order(status="awaiting_approval")]}
    result = runner.invoke(
        trade_cmd.app,
        ["swap", "--chain", "base", "--in", "USDC", "--out", "WETH", "--amount", "500"],
    )
    assert result.exit_code == 0, result.output
    assert "agentos trade approve ord-1" in result.output


def test_orders_list_and_filters(client: _FakeClient) -> None:
    client.payloads["trading.orders.list"] = {
        "orders": [_order(status="awaiting_approval")],
        "pendingApprovals": 1,
    }
    result = runner.invoke(
        trade_cmd.app,
        ["orders", "--status", "awaiting_approval", "--wallet", WALLET, "--limit", "5"],
    )
    assert result.exit_code == 0, result.output
    assert client.calls == [
        ("trading.orders.list", {"limit": 5, "status": "awaiting_approval", "wallet": WALLET})
    ]
    assert "1 order(s) awaiting approval" in result.output


def test_order_get_and_wait(client: _FakeClient) -> None:
    shown = runner.invoke(trade_cmd.app, ["order", "ord-1"])
    waited = runner.invoke(trade_cmd.app, ["order", "ord-1", "--wait", "--wait-seconds", "10"])
    assert shown.exit_code == 0 and waited.exit_code == 0, waited.output
    assert client.calls == [
        ("trading.orders.get", {"orderId": "ord-1"}),
        ("trading.orders.wait", {"orderId": "ord-1", "timeoutSeconds": 10}),
    ]
    assert "0xabc" in shown.output
    assert "confirmed" in waited.output


def test_approve_and_reject(client: _FakeClient) -> None:
    approved = runner.invoke(trade_cmd.app, ["approve", "ord-1", "--json"])
    rejected = runner.invoke(trade_cmd.app, ["reject", "ord-1", "--reason", "too pricey"])
    assert approved.exit_code == 0 and rejected.exit_code == 0
    assert client.calls == [
        ("trading.orders.get", {"orderId": "ord-1"}),
        ("trading.orders.approve", {"orderId": "ord-1"}),
        ("trading.orders.reject", {"orderId": "ord-1", "reason": "too pricey"}),
    ]
    assert json.loads(approved.stdout)["order"]["status"] == "approved"
    assert "rejected" in rejected.output


def test_history_filters(client: _FakeClient) -> None:
    result = runner.invoke(
        trade_cmd.app,
        ["history", "--wallet", WALLET, "--chain", "base", "--kind", "swap", "--limit", "7"],
    )
    assert result.exit_code == 0, result.output
    assert client.calls == [
        ("trading.history", {"limit": 7, "wallet": WALLET, "chainId": 8453, "kind": "swap"})
    ]
    assert "USDC" in result.output and "WETH" in result.output and "agent" in result.output


def test_portfolio_renders_totals_and_holdings(client: _FakeClient) -> None:
    result = runner.invoke(trade_cmd.app, ["portfolio", "--wallet", WALLET])
    assert result.exit_code == 0, result.output
    assert client.calls == [("trading.portfolio", {"wallet": WALLET})]
    assert "$100.00" in result.output
    assert "+11.10%" in result.output
    assert "sync in progress" in result.output
    assert "2 junk tokens hidden" in result.output


def test_hidden_flags_and_hide_unhide(client: _FakeClient) -> None:
    shown = runner.invoke(trade_cmd.app, ["portfolio", "--hidden"])
    history = runner.invoke(trade_cmd.app, ["history", "--hidden"])
    hide = runner.invoke(trade_cmd.app, ["hide", USDC, "--chain", "base"])
    unhide = runner.invoke(trade_cmd.app, ["unhide", USDC, "--chain", "robinhood"])
    for result in (shown, history, hide, unhide):
        assert result.exit_code == 0, result.output
    assert client.calls == [
        ("trading.portfolio", {"includeHidden": True}),
        ("trading.history", {"limit": 100, "includeHidden": True}),
        ("trading.tokens.hide", {"chainId": 8453, "address": USDC, "hidden": True}),
        ("trading.tokens.hide", {"chainId": 4663, "address": USDC, "hidden": False}),
    ]
    assert "junk tokens hidden" not in shown.output
    assert "Hidden: USDC on base" in hide.output


def test_sync_and_limits(client: _FakeClient) -> None:
    sync = runner.invoke(trade_cmd.app, ["sync", "--full"])
    limits = runner.invoke(trade_cmd.app, ["limits", WALLET])
    assert sync.exit_code == 0 and limits.exit_code == 0
    assert client.calls == [
        ("trading.sync", {"full": True}),
        ("trading.limits", {"wallet": WALLET}),
    ]
    assert "Full rebuild" in sync.output
    assert "$25.00" in limits.output
    # The threshold must be printed, not a "—" from reading the wrong key.
    assert "$100.00" in limits.output


def test_every_command_supports_json(client: _FakeClient) -> None:
    invocations = [
        ["status"],
        ["probe"],
        ["provider"],
        ["provider", "uniswap"],
        ["tokens", "--chain", "base", "usdc"],
        ["orders"],
        ["order", "ord-1"],
        ["approve", "ord-1"],
        ["reject", "ord-1"],
        ["history"],
        ["portfolio"],
        ["sync"],
        ["limits", WALLET],
        ["hide", USDC, "--chain", "base"],
        ["unhide", USDC, "--chain", "base"],
    ]
    for args in invocations:
        result = runner.invoke(trade_cmd.app, [*args, "--json"])
        assert result.exit_code == 0, (args, result.output)
        json.loads(result.stdout)


def test_argument_errors_are_json_when_asked(client: _FakeClient) -> None:
    """An agent reading stderr for ``{"error": …}`` must never get a usage panel."""
    result = runner.invoke(
        trade_cmd.app,
        ["swap", "--chain", "base", "--in", "ETH", "--out", USDC, "--json"],
    )
    assert result.exit_code == 2 and result.stdout == ""
    payload = json.loads(result.stderr.strip().splitlines()[-1])
    assert payload["error"]["code"] == "INVALID_ARGUMENT"
    assert "--amount, --pct or --usd" in payload["error"]["message"]
    assert client.calls_to("trading.swap") == []
    result = runner.invoke(
        trade_cmd.app,
        ["swap", "--chain", "base", "--in", "ETH", "--out", USDC, "--pct", "0", "--json"],
    )
    assert result.exit_code == 2
    assert json.loads(result.stderr.strip().splitlines()[-1])["error"]["code"] == "INVALID_ARGUMENT"
    # Fractions of a percent are allowed.
    result = runner.invoke(
        trade_cmd.app,
        ["swap", "--chain", "base", "--in", "ETH", "--out", USDC, "--pct", "0.5", "--json"],
    )
    assert result.exit_code == 0, result.output
    assert client.calls_to("trading.swap")[0]["amountPct"] == 0.5


# ── send / allowances / revoke / decode / network ───────────────────────────


def _send_order(**extra: Any) -> dict[str, Any]:
    base = _order(
        kind="send",
        tokenOut=TOKENS["USDC"],
        recipient=WALLET_B,
        recipientLabel=None,
        batchId=None,
        expectedOut=None,
    )
    base.update(extra)
    return base


def test_send_single_recipient_manual(client: _FakeClient) -> None:
    client.payloads["trading.send"] = {"orders": [_send_order()], "batchId": None}
    result = runner.invoke(
        trade_cmd.app,
        [
            "send",
            "--chain",
            "base",
            "--token",
            "USDC",
            "--to",
            WALLET_B,
            "--amount",
            "10",
            "--note",
            "rent",
        ],
    )
    assert result.exit_code == 0, result.output
    assert client.calls_to("trading.send") == [
        {
            "chainId": 8453,
            "token": USDC,
            "recipients": [{"to": WALLET_B, "amount": "10"}],
            "initiator": "manual",
            "note": "rent",
        }
    ]
    assert "Send (manual)" in result.output
    assert "0x2222…2222" in result.output and "submitted" in result.output


def test_send_multisend_sizing_and_file(client: _FakeClient, tmp_path, monkeypatch) -> None:
    listing = tmp_path / "list.txt"
    listing.write_text(
        f"# payroll\n{WALLET}=1.5\n{WALLET_B}\n\n0x3333333333333333333333333333333333333333, 2\n"
    )
    monkeypatch.setenv("AGENTOS_SESSION_KEY", "agent:main:webchat:x")
    client.payloads["trading.send"] = {
        "orders": [
            _send_order(orderId="ord-1", status="awaiting_approval", batchId="bat_1"),
            _send_order(orderId="ord-2", status="awaiting_approval", batchId="bat_1"),
        ],
        "batchId": "bat_1",
    }
    result = runner.invoke(
        trade_cmd.app,
        [
            "send",
            "--chain",
            "base",
            "--token",
            "ETH",
            "--to",
            f"{FAKE_AAPL}=0.2",
            "--file",
            str(listing),
            "--amount",
            "0.1",
        ],
    )
    assert result.exit_code == 0, result.output
    params = client.calls_to("trading.send")[0]
    assert params["token"] == NATIVE_ADDRESS and params["initiator"] == "agent"
    assert params["sessionKey"] == "agent:main:webchat:x"
    assert params["recipients"] == [
        {"to": FAKE_AAPL, "amount": "0.2"},
        {"to": WALLET, "amount": "1.5"},
        {"to": WALLET_B, "amount": "0.1"},
        {"to": "0x3333333333333333333333333333333333333333", "amount": "2"},
    ]
    assert "Multisend (agent)" in result.output
    assert "one approval covers the whole batch" in result.output


def test_send_usd_sizing_and_wait(client: _FakeClient) -> None:
    client.payloads["trading.send"] = {
        "orders": [
            _send_order(orderId="ord-1", status="submitted"),
            _send_order(orderId="ord-2", status="failed", reason="x"),
        ],
        "batchId": "bat_1",
    }
    client.payloads["trading.orders.wait"] = {"order": _send_order(status="confirmed")}
    result = runner.invoke(
        trade_cmd.app,
        [
            "send",
            "--chain",
            "base",
            "--token",
            "USDC",
            "--to",
            WALLET_B,
            "--to",
            WALLET,
            "--usd",
            "5",
            "--wait",
            "--wait-seconds",
            "20",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert client.calls_to("trading.send")[0]["recipients"] == [
        {"to": WALLET_B, "amountUsd": 5.0},
        {"to": WALLET, "amountUsd": 5.0},
    ]
    assert client.calls_to("trading.orders.wait") == [{"orderId": "ord-1", "timeoutSeconds": 20}]
    payload = json.loads(result.stdout)
    assert [o["status"] for o in payload["orders"]] == ["confirmed", "failed"]
    assert payload["batchId"] == "bat_1"


@pytest.mark.parametrize(
    "args, message",
    [
        (["--to", WALLET_B], "has no amount"),
        (["--to", WALLET_B, "--amount", "1", "--usd", "2"], "not both"),
        (["--to", f"{WALLET_B}=1", "--usd", "2"], "cannot also size"),
        (["--to", "vitalik.eth", "--amount", "1"], "not an address"),
        (["--to", f"{WALLET_B}=abc"], "not an amount"),
        (["--amount", "1"], "at least one --to"),
        (["--file", "/nonexistent/list.txt", "--amount", "1"], "cannot read"),
    ],
)
def test_send_argument_validation(client: _FakeClient, args: list[str], message: str) -> None:
    result = runner.invoke(
        trade_cmd.app, ["send", "--chain", "base", "--token", "USDC", *args, "--json"]
    )
    assert result.exit_code == 2, result.output
    assert result.stdout == ""
    assert "INVALID_ARGUMENT" in result.output and message in result.output
    assert client.calls_to("trading.send") == []


def test_allowances_renders_and_warns(client: _FakeClient) -> None:
    client.payloads["trading.allowances.list"] = {
        "wallet": WALLET,
        "chainId": None,
        "count": 2,
        "unlimitedCount": 1,
        "allowances": [
            {
                "chainId": 8453,
                "token": TOKENS["USDC"],
                "spender": "0x000000000022D473030F116dDEE9F6B43aC78BA3",
                "spenderLabel": "Permit2",
                "allowance": "unlimited",
                "unlimited": True,
                "readFailed": False,
                "balance": "1000",
                "exposureUsd": 1000.0,
                "lastTxHash": "0xaaaa",
            },
            {
                "chainId": 4663,
                "token": TOKENS["WETH"],
                "spender": WALLET_B,
                "spenderLabel": None,
                "allowance": "0.5",
                "unlimited": False,
                "readFailed": False,
                "balance": "1",
                "exposureUsd": 1000.0,
                "lastTxHash": None,
            },
        ],
        "chains": [],
    }
    result = runner.invoke(trade_cmd.app, ["allowances", "--wallet", WALLET, "--full"])
    assert result.exit_code == 0, result.output
    assert client.calls == [("trading.allowances.list", {"wallet": WALLET, "full": True})]
    assert "Permit2" in result.output and "unlimited" in result.output
    assert "$1,000.00" in result.output and "robinhood" in result.output
    assert "1 unlimited allowance" in result.output and "agentos trade revoke" in result.output
    scoped = runner.invoke(trade_cmd.app, ["allowances", "--chain", "robinhood", "--json"])
    assert scoped.exit_code == 0
    assert client.calls[-1] == ("trading.allowances.list", {"chainId": 4663})


def test_allowances_polls_while_the_engine_scans(client: _FakeClient, monkeypatch) -> None:
    answers = iter(
        [
            {"wallet": WALLET, "allowances": [], "count": 0, "unlimitedCount": 0, "scanning": True},
            {"wallet": WALLET, "allowances": [], "count": 0, "unlimitedCount": 0, "scanning": True},
            {
                "wallet": WALLET,
                "allowances": [],
                "count": 0,
                "unlimitedCount": 0,
                "scanning": False,
            },
        ]
    )
    original = client.call

    async def call(method: str, params: dict | None = None):
        if method == "trading.allowances.list":
            client.calls.append((method, dict(params or {})))
            return next(answers)
        return await original(method, params)

    monkeypatch.setattr(client, "call", call)
    monkeypatch.setattr(trade_cmd.asyncio, "sleep", _no_sleep)
    result = runner.invoke(trade_cmd.app, ["allowances", "--full", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["scanning"] is False
    # The first call carries --full; the polls must not restart the scan.
    assert client.calls_to("trading.allowances.list") == [
        {"full": True},
        {},
        {},
    ]
    monkeypatch.setattr(client, "call", original)
    client.payloads["trading.allowances.list"] = {
        "wallet": WALLET,
        "allowances": [],
        "count": 0,
        "unlimitedCount": 0,
        "scanning": True,
    }
    partial = runner.invoke(trade_cmd.app, ["allowances", "--no-wait"])
    assert partial.exit_code == 0, partial.output
    assert client.calls[-1] == ("trading.allowances.list", {})
    assert "still scanning" in partial.output


def test_revoke_resolves_token_and_hints_approval(client: _FakeClient) -> None:
    client.payloads["trading.allowances.revoke"] = {
        "order": _order(
            kind="revoke",
            status="awaiting_approval",
            recipient=WALLET_B,
            recipientLabel="Permit2",
            amountIn="unlimited",
            expectedOut=None,
        )
    }
    result = runner.invoke(
        trade_cmd.app,
        ["revoke", "--chain", "base", "--token", "USDC", "--spender", WALLET_B, "--note", "x"],
    )
    assert result.exit_code == 0, result.output
    assert client.calls_to("trading.allowances.revoke") == [
        {"chainId": 8453, "token": USDC, "spender": WALLET_B, "initiator": "manual", "note": "x"}
    ]
    assert "revoke" in result.output and "Permit2" in result.output
    assert "agentos trade approve ord-1" in result.output
    bad = runner.invoke(
        trade_cmd.app, ["revoke", "--chain", "base", "--token", "USDC", "--spender", "nope"]
    )
    assert bad.exit_code == 2
    client.payloads["trading.orders.wait"] = {"order": _order(kind="revoke", status="confirmed")}
    waited = runner.invoke(
        trade_cmd.app,
        ["revoke", "--chain", "base", "--token", "USDC", "--spender", WALLET_B, "--wait", "--json"],
    )
    assert waited.exit_code == 0, waited.output
    assert json.loads(waited.stdout)["order"]["status"] == "confirmed"


def test_decode_hash_and_data(client: _FakeClient) -> None:
    client.payloads["trading.decode"] = {
        "chainId": 8453,
        "call": {"selector": "0xa9059cbb", "function": "transfer", "known": True, "args": []},
        "description": "transfer 10000000 units of usdc to 0x22",
        "to": USDC,
        "toLabel": None,
        "toToken": TOKENS["USDC"],
        "decoded": {
            "function": "transfer",
            "token": TOKENS["USDC"],
            "counterparty": WALLET_B,
            "counterpartyLabel": None,
            "amount": "10",
        },
        "tx": {
            "status": "success",
            "from": WALLET,
            "blockNumber": 7,
            "valueWei": "0",
            "gasUsed": 5,
        },
        "transfers": [{"token": TOKENS["USDC"], "amount": "10", "from": WALLET, "to": WALLET_B}],
        "approvals": [
            {
                "token": TOKENS["USDC"],
                "owner": WALLET,
                "spender": WALLET_B,
                "spenderLabel": "Permit2",
                "amount": "unlimited",
                "unlimited": True,
            }
        ],
        "wallets": [WALLET],
        "explorerUrl": "https://basescan.org/tx/0xabc",
    }
    result = runner.invoke(trade_cmd.app, ["decode", "--chain", "base", "0xabc"])
    assert result.exit_code == 0, result.output
    assert client.calls == [("trading.decode", {"chainId": 8453, "txHash": "0xabc"})]
    assert "transfer 10000000 units" in result.output and "success" in result.output
    assert (
        "Transfers" in result.output and "Approvals" in result.output and "Permit2" in result.output
    )
    assert "Involves your wallet" in result.output
    data = runner.invoke(
        trade_cmd.app, ["decode", "--chain", "base", "--data", "0xdead", "--to", USDC, "--json"]
    )
    assert data.exit_code == 0
    assert client.calls[-1] == ("trading.decode", {"chainId": 8453, "data": "0xdead", "to": USDC})
    neither = runner.invoke(trade_cmd.app, ["decode", "--chain", "base", "--json"])
    assert neither.exit_code == 2
    both = runner.invoke(trade_cmd.app, ["decode", "--chain", "base", "0xabc", "--data", "0x"])
    assert both.exit_code == 2


def test_network_renders(client: _FakeClient) -> None:
    client.payloads["trading.network"] = {
        "checkedAt": 1,
        "chains": [
            {
                "chainId": 8453,
                "name": "Base",
                "rpcUrl": "https://lb.drpc.org/…",
                "blockNumber": 123,
                "blockAgeS": 2,
                "baseFeeGwei": 0.0123,
                "priorityFeeGwei": 0.001,
                "latencyMs": 80,
                "healthy": True,
                "error": None,
            },
            {
                "chainId": 4663,
                "name": "Robinhood Chain",
                "rpcUrl": "https://rpc.mainnet.chain.robinhood.com",
                "blockNumber": None,
                "blockAgeS": None,
                "baseFeeGwei": None,
                "priorityFeeGwei": None,
                "latencyMs": None,
                "healthy": False,
                "error": "HTTP 502",
            },
        ],
    }
    result = runner.invoke(trade_cmd.app, ["network", "--fresh"])
    assert result.exit_code == 0, result.output
    assert client.calls == [("trading.network", {"fresh": True})]
    assert "123" in result.output and "80 ms" in result.output and "0.0123 gwei" in result.output
    assert "HTTP 502" in result.output
    plain = runner.invoke(trade_cmd.app, ["network", "--json"])
    assert plain.exit_code == 0 and client.calls[-1] == ("trading.network", {})


def test_orders_kind_filter(client: _FakeClient) -> None:
    result = runner.invoke(trade_cmd.app, ["orders", "--kind", "send", "--json"])
    assert result.exit_code == 0
    assert client.calls == [("trading.orders.list", {"limit": 50, "kind": "send"})]
    bad = runner.invoke(trade_cmd.app, ["orders", "--kind", "nope", "--json"])
    assert bad.exit_code == 2


async def _no_sleep(_seconds: float) -> None:
    return None


def test_swap_hidden_client_quote_options_reach_the_gateway(client: _FakeClient) -> None:
    result = runner.invoke(
        trade_cmd.app,
        [
            "swap",
            "--chain",
            "base",
            "--in",
            "USDC",
            "--out",
            "WETH",
            "--amount",
            "10",
            "--expected-out-raw",
            "5000000000000000",
            "--min-out-raw",
            " 4975000000000000 ",
        ],
    )
    assert result.exit_code == 0, result.output
    params = client.calls_to("trading.swap")[0]
    assert params["expectedOutRaw"] == "5000000000000000"
    assert params["minOutRaw"] == "4975000000000000"
    # Absent: not sent at all.
    runner.invoke(
        trade_cmd.app,
        ["swap", "--chain", "base", "--in", "USDC", "--out", "WETH", "--amount", "10"],
    )
    assert "expectedOutRaw" not in client.calls_to("trading.swap")[1]
    assert "minOutRaw" not in client.calls_to("trading.swap")[1]
    # Hidden from --help: they are for the desktop and scripts, not people.
    help_text = runner.invoke(trade_cmd.app, ["swap", "--help"]).output
    assert "--expected-out-raw" not in help_text and "--min-out-raw" not in help_text
    # And a value that is not a base-unit integer is refused before any call.
    bad = runner.invoke(
        trade_cmd.app,
        [
            "swap",
            "--chain",
            "base",
            "--in",
            "USDC",
            "--out",
            "WETH",
            "--amount",
            "10",
            "--expected-out-raw",
            "0.005",
        ],
    )
    assert bad.exit_code != 0
    assert "whole number" in bad.output
    assert len(client.calls_to("trading.swap")) == 2


def test_status_shows_a_pending_ledger_repair(client: _FakeClient) -> None:
    result = runner.invoke(trade_cmd.app, ["status"])
    assert result.exit_code == 0, result.output
    assert "full sync required" not in result.output
    client.payloads["trading.status"]["ledgerRepair"] = "full sync required"
    result = runner.invoke(trade_cmd.app, ["status"])
    assert result.exit_code == 0, result.output
    assert "ledger" in result.output and "full sync required" in result.output
