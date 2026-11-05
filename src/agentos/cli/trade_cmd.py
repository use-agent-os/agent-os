"""``agentos trade`` — quotes, swaps, sends, allowances, orders, history and PnL.

A thin client over the gateway's ``trading.*`` RPCs. Every order goes through
the engine's guardrails, and the *gateway* decides who is asking: a shell
spawned by an agent turn carries ``AGENTOS_AGENT_TOKEN`` (minted by the shell
tool, presented at the handshake by ``gateway_rpc``), and any connection opened
while an agent shell is running counts as the agent's (see
``agentos.gateway.agent_surface``). An agent-bound connection is the agent
whatever it declares: its swaps obey the per-order approval threshold and the
per-wallet daily cap, and its sends and revokes always park for the user's
approval. ``--as-agent`` (or ``AGENTOS_SESSION_KEY``/``AGENTOS_AGENT`` in the
environment) only lets a person opt *into* the agent rules; it cannot opt an
agent out of them.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, tzinfo
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import click
import typer
from rich.panel import Panel
from rich.table import Table
from typer.core import TyperGroup

from agentos.cli.gateway_rpc import rpc_error_exit_code, run_gateway_sync
from agentos.cli.output import emit_error, print_json, print_text
from agentos.cli.ui import ACCENT, ACCENT_HEADER, console, markup_escape
from agentos.cli.wallet_cmd import (
    NATIVE_ADDRESS,
    amount_text,
    chain_id_from_arg,
    chain_label,
    is_address,
    money,
    percent,
    short_address,
    token_symbol,
)


class _JsonUsageGroup(TyperGroup):
    """A usage error under ``--json`` is a JSON error on stderr, exit 2.

    Click reports a bad or missing option with a Rich usage panel, which an
    agent reading stderr for ``{"error": …}`` cannot parse. The same error
    without ``--json`` still gets the panel.
    """

    def invoke(self, ctx: click.Context) -> Any:
        args = [*getattr(ctx, "_protected_args", []), *ctx.args]
        try:
            return super().invoke(ctx)
        except click.UsageError as exc:
            if "--json" not in args:
                raise
            _bad_argument(exc.format_message(), json_output=True)


app = typer.Typer(
    cls=_JsonUsageGroup,
    help=(
        "Swap and send tokens, review and revoke allowances, decode transactions, "
        "check the network, and track orders, history and PnL."
    ),
)

AGENT_ENV_MARKERS = ("AGENTOS_SESSION_KEY", "AGENTOS_AGENT")
PROVIDERS: dict[str, str] = {"aggregator": "AgentOS Aggregator", "uniswap": "Uniswap"}
# Statuses that still move on their own after ``trading.swap`` returns.
_PENDING_STATUSES = frozenset({"awaiting_approval", "approved", "submitted", "quoted"})


class TokenResolutionError(Exception):
    """A symbol did not resolve to exactly one verified token."""

    def __init__(self, message: str, *, code: str = "TOKEN_UNRESOLVED") -> None:
        super().__init__(message)
        self.code = code


def provider_id_from_arg(value: str) -> str:
    """Normalise ``aggregator``/``uniswap`` (case-insensitive; ``agg`` accepted)."""

    key = value.strip().lower()
    if key in ("agg", "404", "agentos"):
        key = "aggregator"
    if key not in PROVIDERS:
        choices = ", ".join(PROVIDERS)
        raise typer.BadParameter(f"unknown provider {value!r}; expected one of: {choices}")
    return key


def provider_label(value: Any) -> str:
    key = str(value or "").strip().lower()
    return PROVIDERS.get(key, key or "—")


def initiator_for(as_agent: bool, environ: dict[str, str] | None = None) -> str:
    """``agent`` when forced or when running inside an agent turn, else ``manual``."""

    env = os.environ if environ is None else environ
    if as_agent:
        return "agent"
    if any(env.get(name, "").strip() for name in AGENT_ENV_MARKERS):
        return "agent"
    return "manual"


async def resolve_token(client: Any, chain_id: int, value: str) -> str:
    """Turn ``ETH``, an address, or a symbol into a token address.

    Symbols must match exactly one *verified* token on the chain; anything
    else raises so the caller never swaps into a lookalike by accident. An
    address is passed through untouched — the engine resolves its metadata.
    """

    text = value.strip()
    if not text:
        raise TokenResolutionError("token cannot be empty")
    if text.upper() == "ETH":
        return NATIVE_ADDRESS
    if is_address(text):
        return text
    result = await client.call("trading.tokens.search", {"chainId": chain_id, "query": text})
    tokens = result.get("tokens", []) if isinstance(result, dict) else []
    matches = [
        t
        for t in tokens
        if isinstance(t, dict) and str(t.get("symbol") or "").upper() == text.upper()
    ]
    if not matches:
        raise TokenResolutionError(
            f"no token with symbol {text!r} on {chain_label(chain_id)}; pass its address",
            code="TOKEN_NOT_FOUND",
        )
    verified = [t for t in matches if t.get("verified")]
    if len(verified) == 1:
        return str(verified[0]["address"])
    if not verified:
        listed = ", ".join(str(t.get("address")) for t in matches[:5])
        raise TokenResolutionError(
            f"{text!r} matched only unverified tokens on {chain_label(chain_id)} ({listed}); "
            "pass the address explicitly if you mean one of them",
            code="TOKEN_UNVERIFIED",
        )
    listed = ", ".join(str(t.get("address")) for t in verified[:5])
    raise TokenResolutionError(
        f"{text!r} is ambiguous on {chain_label(chain_id)} ({listed}); pass the address",
        code="TOKEN_AMBIGUOUS",
    )


def _exit_token_error(exc: TokenResolutionError, *, json_output: bool) -> None:
    emit_error(str(exc), json_output=json_output, code=exc.code)
    raise typer.Exit(2)


def _bad_argument(message: str, *, json_output: bool) -> None:
    """An argument error an agent can parse: JSON on stderr, exit 2.

    ``typer.BadParameter`` prints a usage panel, which is right for a person
    and useless for ``--json`` callers reading stderr for ``{"error": …}``.
    """
    emit_error(message, json_output=json_output, code="INVALID_ARGUMENT")
    raise typer.Exit(2)


def _dict(value: Any) -> dict[str, Any]:
    """Narrow an RPC payload (or one of its fields) to a dict for rendering."""

    return value if isinstance(value, dict) else {}


def _order_rows(result: Any) -> list[dict[str, Any]]:
    orders = result.get("orders", []) if isinstance(result, dict) else []
    return [o for o in orders if isinstance(o, dict)]


def _order_legs(order: dict[str, Any]) -> str:
    """Legs in one phrase: "10 USDC → WETH", "10 USDC → 0x2222…2222", or the spender of a revoke."""
    kind = str(order.get("kind") or "swap")
    amount = f"{amount_text(order.get('amountIn'))} {token_symbol(order.get('tokenIn'))}"
    if kind == "send":
        return f"{amount} → {short_address(order.get('recipient'))}"
    if kind == "revoke":
        who = order.get("recipientLabel") or short_address(order.get("recipient"))
        return f"revoke {token_symbol(order.get('tokenIn'))} for {who}"
    if kind in LP_KINDS:
        return _lp_legs(order)
    if kind == "bridge":
        bridge = _dict(order.get("bridge"))
        dest = chain_label(bridge.get("destinationChainId"))
        out = order.get("receivedOut") or order.get("expectedOut")
        return f"{amount} → {dest} ({amount_text(out)} {token_symbol(order.get('tokenOut'))})"
    return f"{amount} → {token_symbol(order.get('tokenOut'))}"


#: Order kinds of the Uniswap V4 LP writes (``trade lp collect|remove|add``).
LP_KINDS = ("lp_collect", "lp_remove", "lp_add")
ORDER_KINDS = ("swap", "send", "revoke", "bridge", *LP_KINDS)


def _lp_pair(order: dict[str, Any]) -> str:
    return f"{token_symbol(order.get('tokenIn'))}/{token_symbol(order.get('tokenOut'))}"


def _lp_legs(order: dict[str, Any]) -> str:
    """The order in words: "Collect fees · #48213", "Remove liquidity · #48213 · 100%"."""
    kind = str(order.get("kind") or "")
    plan = _dict(order.get("plan"))
    token_id = order.get("tokenId") or plan.get("tokenId")
    where = f" · #{token_id}" if token_id else ""
    if kind == "lp_collect":
        return f"Collect fees{where}"
    if kind == "lp_remove":
        pct = plan.get("pct")
        return f"Remove liquidity{where}" + (f" · {float(pct):g}%" if pct is not None else "")
    return f"Add liquidity · {_lp_pair(order)}{where}"


def _lp_amounts(pair: Any) -> str | None:
    """``{base, quote}`` Amounts as "1.2 PEPE + 0.01 WETH"; None when both are zero."""
    legs = _dict(pair)
    parts = []
    for key in ("base", "quote"):
        leg = _dict(legs.get(key))
        if leg.get("raw") not in (None, "0"):
            parts.append(f"{leg.get('human')} {leg.get('symbol') or key}")
    return " + ".join(parts) or None


def _order_table(orders: list[dict[str, Any]], title: str = "Orders") -> Table:
    table = Table(title=title, show_header=True, header_style=ACCENT_HEADER)
    table.add_column("Order")
    table.add_column("Chain")
    table.add_column("Wallet")
    table.add_column("Legs")
    table.add_column("Value", justify="right")
    table.add_column("Status")
    table.add_column("Tx / reason")
    for order in orders:
        tail = order.get("txHash") or order.get("reason") or ""
        table.add_row(
            str(order.get("orderId") or ""),
            chain_label(order.get("chainId")),
            short_address(order.get("wallet")),
            markup_escape(_order_legs(order)),
            money(order.get("valueUsd")),
            str(order.get("status") or ""),
            markup_escape(str(tail)),
        )
    return table


def _print_order(order: dict[str, Any]) -> None:
    table = Table(title=f"Order {order.get('orderId') or ''}", show_header=False)
    table.add_column("Field", style=ACCENT)
    table.add_column("Value")
    kind = str(order.get("kind") or "swap")
    if kind in LP_KINDS:
        _print_lp_order(order, table)
        return
    recipient = order.get("recipient")
    if recipient and order.get("recipientLabel"):
        recipient = f"{recipient} ({order['recipientLabel']})"
    bridge = _dict(order.get("bridge")) if kind == "bridge" else {}
    out_amount = (
        order.get("receivedOut") if order.get("status") == "confirmed" else order.get("expectedOut")
    )
    for field, value in (
        ("kind", kind if kind != "swap" else None),
        ("batch", order.get("batchId")),
        ("status", order.get("status")),
        ("reason", order.get("reason")),
        ("chain", chain_label(order.get("chainId"))),
        ("to chain", chain_label(bridge.get("destinationChainId")) if bridge else None),
        ("wallet", order.get("wallet")),
        ("to" if kind == "send" else "spender", recipient if kind != "bridge" else None),
        (
            "allowance" if kind == "revoke" else "in",
            f"{amount_text(order.get('amountIn'))} {token_symbol(order.get('tokenIn'))}",
        ),
        (
            "out",
            f"{amount_text(out_amount)} {token_symbol(order.get('tokenOut'))}"
            if kind in ("swap", "bridge")
            else None,
        ),
        ("min out", order.get("minOut")),
        ("value", money(order.get("valueUsd"))),
        ("price impact", percent(order.get("priceImpactPct"))),
        ("gas", money(order.get("gasUsd"))),
        ("fees", money(bridge.get("feesUsd")) if bridge else None),
        ("initiator", order.get("initiator")),
        ("request", bridge.get("requestId")),
        ("fill", bridge.get("fillStatus")),
        ("tx", order.get("txHash")),
        ("explorer", order.get("explorerUrl")),
        ("destination tx", bridge.get("destinationTxHash")),
        ("destination explorer", bridge.get("destinationExplorerUrl")),
        ("expires", order.get("expiresAt")),
        ("note", order.get("note")),
    ):
        if value in (None, "", "—"):
            continue
        table.add_row(field, markup_escape(str(value)))
    console.print(table)


def _named(pair: Any, plan: dict[str, Any]) -> dict[str, Any]:
    """Amounts with their token symbols (the plan's ``token``/``quote``) attached."""
    out = {}
    for key, which in (("base", "token"), ("quote", "quote")):
        leg = dict(_dict(_dict(pair).get(key)))
        leg["symbol"] = _dict(plan.get(which)).get("symbol")
        out[key] = leg
    return out


def _print_lp_order(order: dict[str, Any], table: Table) -> None:
    plan = _dict(order.get("plan"))
    rng = _dict(plan.get("range"))
    pool = _dict(plan.get("pool"))
    expected = _dict(plan.get("expected"))
    sim = _dict(plan.get("simulation"))
    op = str(plan.get("op") or "")
    moves = "you receive" if op in ("collect", "remove") else "you deposit"
    bound = "minimum" if op == "remove" else ("maximum" if op == "add" else None)
    approvals = [
        f"{a.get('symbol')} {a.get('step')}" + (f" ({a['txHash']})" if a.get("txHash") else "")
        for a in plan.get("approvals") or []
        if isinstance(a, dict) and (a.get("needed") or a.get("txHash"))
    ]
    bounds = _dict(plan.get("bounds"))
    rows = (
        ("kind", _lp_legs(order)),
        ("status", order.get("status")),
        ("reason", order.get("reason")),
        ("chain", chain_label(order.get("chainId"))),
        ("wallet", order.get("wallet")),
        ("pool", f"{_lp_pair(order)} · {pool.get('feePct')} · {pool.get('poolId')}"),
        ("hook", pool.get("hook")),
        (
            "range",
            f"{rng.get('tickLower')} → {rng.get('tickUpper')} ({_range_text(rng)})"
            + (f" · {plan.get('rangeSpec')}" if plan.get("rangeSpec") else "")
            if rng
            else None,
        ),
        ("one-sided", plan.get("oneSided")),
        (moves, _lp_amounts(_named(expected, plan))),
        ("worth", _usd(expected.get("usd")) if expected else None),
        (
            "fees included" if op == "remove" else "fees",
            _lp_amounts(_named(plan.get("fees"), plan)),
        ),
        (
            bound,
            _lp_amounts(
                _named(
                    {k: {"raw": v, "human": v} for k, v in bounds.items()},
                    plan,
                )
            )
            if bound
            else None,
        ),
        ("approvals", ", ".join(approvals) or None),
        ("simulation", f"{sim.get('method')}: {'ok' if sim.get('ok') else sim.get('revert')}"),
        ("gas", _usd(order.get("gasUsd"))),
        ("received", _lp_amounts(_named(order.get("received"), plan))),
        ("spent", _lp_amounts(_named(order.get("spent"), plan))),
        ("initiator", order.get("initiator")),
        ("plan", plan.get("planHash")),
        ("tx", order.get("txHash")),
        ("explorer", order.get("explorerUrl")),
        ("expires", order.get("expiresAt")),
        ("note", order.get("note")),
    )
    for field, value in rows:
        if field is None or value in (None, "", "—"):
            continue
        table.add_row(field, markup_escape(str(value)))
    console.print(table)


# ── commands ────────────────────────────────────────────────────────────────


@app.command("status")
def trade_status(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show trading readiness: API key, chains, limits, vault state."""

    async def _run(client):
        return await client.call("trading.status", {})

    result = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(result)
        return
    result = _dict(result)
    limits = _dict(result.get("limits"))
    table = Table(title="Trading", show_header=False)
    table.add_column("Field", style=ACCENT)
    table.add_column("Value")
    table.add_row("enabled", "yes" if result.get("enabled") else "no")
    table.add_row("provider", provider_label(result.get("provider")))
    table.add_row("Uniswap API key", "configured" if result.get("apiKeyConfigured") else "missing")
    table.add_row("vault", "unlocked" if result.get("unlocked") else "locked")
    table.add_row("unlock mode", str(result.get("unlockMode") or "—"))
    table.add_row("approval threshold", money(limits.get("approvalThresholdUsd")))
    table.add_row("daily cap / wallet", money(limits.get("dailyCapUsd")))
    table.add_row("approval TTL", f"{limits.get('approvalTtlSeconds', '—')} s")
    table.add_row("syncing", "yes" if result.get("syncing") else "no")
    if result.get("ledgerRepair"):
        table.add_row("ledger", f"[bold red]{markup_escape(str(result['ledgerRepair']))}[/]")
    console.print(table)
    providers = result.get("providers", [])
    if isinstance(providers, list) and providers:
        pt = Table(title="Swap providers", show_header=True, header_style=ACCENT_HEADER)
        pt.add_column("Provider")
        pt.add_column("Active")
        pt.add_column("API key")
        pt.add_column("Reachable")
        for entry in providers:
            if not isinstance(entry, dict):
                continue
            healthy = entry.get("healthy")
            reachable = "—" if healthy is None else ("yes" if healthy else "no")
            if entry.get("needsKey"):
                key_state = "configured" if entry.get("keyConfigured") else "missing"
            else:
                key_state = "not needed"
            pt.add_row(
                str(entry.get("label") or entry.get("id") or ""),
                "★" if entry.get("id") == result.get("provider") else "",
                key_state,
                reachable,
            )
        console.print(pt)
    chains = result.get("chains", [])
    if isinstance(chains, list) and chains:
        ct = Table(title="Chains", show_header=True, header_style=ACCENT_HEADER)
        ct.add_column("Chain")
        ct.add_column("Id")
        ct.add_column("RPC")
        ct.add_column("Healthy")
        for chain in chains:
            if not isinstance(chain, dict):
                continue
            healthy = chain.get("healthy")
            ct.add_row(
                str(chain.get("name") or chain.get("key") or ""),
                str(chain.get("chainId") or ""),
                markup_escape(str(chain.get("rpcUrl") or "")),
                "—" if healthy is None else ("yes" if healthy else "no"),
            )
        console.print(ct)


@app.command("probe")
def trade_probe(
    provider: str | None = typer.Option(
        None, "--provider", help="aggregator or uniswap (default: the configured provider)"
    ),
    api_key: str | None = typer.Option(
        None, "--api-key", help="Test this Uniswap key instead of the configured one"
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Check that the swap provider is reachable (and, for Uniswap, that the key works)."""

    params: dict[str, Any] = {}
    if provider:
        params["provider"] = provider_id_from_arg(provider)
    if api_key:
        params["apiKey"] = api_key

    async def _run(client):
        return await client.call("trading.probe", params)

    result = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(result)
        if isinstance(result, dict) and not result.get("ok"):
            raise typer.Exit(1)
        return
    result = _dict(result)
    name = provider_label(result.get("provider") or params.get("provider"))
    if result.get("ok"):
        latency = result.get("latencyMs")
        suffix = f" ({latency} ms)" if latency is not None else ""
        console.print(f"[green]{name} OK[/]{suffix}")
        return
    console.print(f"[red]{name} failed:[/] {markup_escape(str(result.get('error')))}")
    raise typer.Exit(1)


@app.command("provider")
def trade_provider(
    name: str | None = typer.Argument(
        None, help="aggregator or uniswap (omit to show the current one)"
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show or switch the swap provider (the aggregator needs no key; Uniswap needs one)."""

    if name is None:

        async def _show(client):
            return await client.call("trading.status", {})

        result = _dict(run_gateway_sync(_show, json_output=json_output))
        current = result.get("provider")
        if json_output:
            print_json({"provider": current, "providers": result.get("providers", [])})
            return
        console.print(f"Swap provider: [{ACCENT}]{provider_label(current)}[/]")
        return

    provider = provider_id_from_arg(name)

    async def _set(client):
        return await client.call("config.set", {"path": "trading.provider", "value": provider})

    result = _dict(run_gateway_sync(_set, json_output=json_output))
    if json_output:
        print_json({"provider": provider, **result})
        return
    console.print(f"Swap provider is now [{ACCENT}]{provider_label(provider)}[/].")
    if provider == "uniswap":
        console.print(
            "Uniswap needs an API key in `trading.uniswap_api_key` (or UNISWAP_API_KEY). "
            "Run `agentos trade probe --provider uniswap` to check it from here."
        )
    elif result.get("restartRequired"):
        console.print("Restart the gateway to apply.")


@app.command("tokens")
def trade_tokens(
    query: str = typer.Argument(..., help="Symbol, name, or address"),
    chain: str = typer.Option(..., "--chain", help="base or robinhood"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Search tokens on a chain (verified Robinhood Stock Tokens are flagged)."""

    chain_id = chain_id_from_arg(chain)

    async def _run(client):
        return await client.call("trading.tokens.search", {"chainId": chain_id, "query": query})

    result = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(result)
        return
    rows = result.get("tokens", []) if isinstance(result, dict) else []
    table = Table(
        title=f"Tokens on {chain_label(chain_id)}", show_header=True, header_style=ACCENT_HEADER
    )
    table.add_column("Symbol")
    table.add_column("Name")
    table.add_column("Address")
    table.add_column("Price", justify="right")
    table.add_column("Liquidity", justify="right")
    table.add_column("Verified")
    for row in rows:
        if not isinstance(row, dict):
            continue
        table.add_row(
            markup_escape(str(row.get("symbol") or "")),
            markup_escape(str(row.get("name") or "")),
            str(row.get("address") or ""),
            money(row.get("priceUsd")),
            money(row.get("liquidityUsd")),
            "✓" if row.get("verified") else "",
        )
    console.print(table)


def _set_hidden(chain: str, address: str, hidden: bool, json_output: bool) -> None:
    chain_id = chain_id_from_arg(chain)

    async def _run(client):
        return await client.call(
            "trading.tokens.hide", {"chainId": chain_id, "address": address, "hidden": hidden}
        )

    result = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(result)
        return
    token = _dict(_dict(result).get("token"))
    symbol = markup_escape(str(token.get("symbol") or token.get("address") or address))
    verb = "Hidden" if token.get("hidden") else "Shown"
    console.print(f"{verb}: {symbol} on {chain_label(chain_id)}")


@app.command("hide")
def trade_hide(
    address: str = typer.Argument(..., help="Token address"),
    chain: str = typer.Option(..., "--chain", help="base or robinhood"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Hide a token from balances, portfolio and history (the ledger keeps it)."""

    _set_hidden(chain, address, True, json_output)


@app.command("unhide")
def trade_unhide(
    address: str = typer.Argument(..., help="Token address"),
    chain: str = typer.Option(..., "--chain", help="base or robinhood"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show a token again; the engine will not auto-hide it after this."""

    _set_hidden(chain, address, False, json_output)


@app.command("quote")
def trade_quote(
    chain: str = typer.Option(..., "--chain", help="base or robinhood"),
    token_in: str = typer.Option(..., "--in", help="Token to sell: symbol, address, or ETH"),
    token_out: str = typer.Option(..., "--out", help="Token to buy: symbol, address, or ETH"),
    amount: str | None = typer.Option(None, "--amount", help="Amount of --in to sell, human units"),
    usd: float | None = typer.Option(
        None, "--usd", help="Sell this many US dollars' worth of --in"
    ),
    wallet: str | None = typer.Option(None, "--wallet", help="Wallet address (default primary)"),
    slippage: float | None = typer.Option(None, "--slippage", help="Slippage %, default auto"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Get a swap quote without executing anything."""

    if (amount is None) == (usd is None):
        _bad_argument("Use exactly one of --amount or --usd", json_output=json_output)
    if usd is not None and usd <= 0:
        _bad_argument("--usd must be above 0", json_output=json_output)
    chain_id = chain_id_from_arg(chain)
    # The gateway decides who is asking from the connection itself; this is
    # the fallback declaration so a quote inside an agent turn carries the
    # agent's guard verdict (threshold, cap) instead of a manual "allow".
    initiator = initiator_for(False)

    async def _run(client):
        params: dict[str, Any] = {
            "chainId": chain_id,
            "tokenIn": await resolve_token(client, chain_id, token_in),
            "tokenOut": await resolve_token(client, chain_id, token_out),
            "initiator": initiator,
        }
        if amount is not None:
            params["amountIn"] = amount
        else:
            params["amountUsd"] = usd
        if wallet:
            params["wallet"] = wallet
        if slippage is not None:
            params["slippagePct"] = slippage
        return await client.call("trading.quote", params)

    try:
        result = run_gateway_sync(_run, json_output=json_output)
    except TokenResolutionError as exc:
        _exit_token_error(exc, json_output=json_output)
        return
    if json_output:
        print_json(result)
        return
    result = _dict(result)
    guard = _dict(result.get("guard"))
    table = Table(title="Quote", show_header=False)
    table.add_column("Field", style=ACCENT)
    table.add_column("Value")
    table.add_row(
        "sell", f"{amount_text(result.get('amountIn'))} {token_symbol(result.get('tokenIn'))}"
    )
    table.add_row(
        "receive", f"{amount_text(result.get('amountOut'))} {token_symbol(result.get('tokenOut'))}"
    )
    table.add_row("minimum", amount_text(result.get("minOut")))
    table.add_row("rate", amount_text(result.get("rate")))
    table.add_row("value", money(result.get("valueUsd")))
    table.add_row("price impact", percent(result.get("priceImpactPct")))
    table.add_row("gas", money(result.get("gasUsd")))
    table.add_row("slippage", percent(result.get("slippagePct")))
    table.add_row("routing", str(result.get("routing") or "—"))
    table.add_row("expires", str(result.get("expiresAt") or "—"))
    if guard:
        table.add_row(
            "guardrail",
            f"{guard.get('decision')} (spent today {money(guard.get('spentTodayUsd'))} "
            f"of {money(guard.get('dailyCapUsd'))}; threshold {money(guard.get('thresholdUsd'))})",
        )
    console.print(table)


@app.command("swap")
def trade_swap(
    chain: str = typer.Option(..., "--chain", help="base or robinhood"),
    token_in: str = typer.Option(..., "--in", help="Token to sell: symbol, address, or ETH"),
    token_out: str = typer.Option(..., "--out", help="Token to buy: symbol, address, or ETH"),
    amount: str | None = typer.Option(None, "--amount", help="Amount of --in, human units"),
    pct: float | None = typer.Option(
        None, "--pct", help="Percent of the --in balance (above 0, up to 100; fractions allowed)"
    ),
    usd: float | None = typer.Option(
        None, "--usd", help="Sell this many US dollars' worth of --in"
    ),
    wallets: list[str] | None = typer.Option(
        None, "--wallet", help="Wallet address (repeatable; default primary)"
    ),
    all_wallets: bool = typer.Option(False, "--all-wallets", help="Swap from every wallet"),
    slippage: float | None = typer.Option(None, "--slippage", help="Slippage %, default auto"),
    note: str | None = typer.Option(None, "--note", help="Why this swap (kept in history)"),
    client_id: str | None = typer.Option(
        None,
        "--client-id",
        help=(
            "Idempotency key; re-running with the same id returns the same order "
            "instead of trading twice"
        ),
    ),
    wait: bool = typer.Option(False, "--wait", help="Block until each order settles"),
    wait_seconds: int = typer.Option(
        300, "--wait-seconds", help="How long --wait blocks per order", min=1, max=900
    ),
    as_agent: bool = typer.Option(
        False, "--as-agent", help="Apply the agent guardrails (threshold, daily cap)"
    ),
    expected_out_raw: str | None = typer.Option(
        None,
        "--expected-out-raw",
        hidden=True,
        help="The quote's amountOutRaw you confirmed; refused if the price moved past it",
    ),
    min_out_raw: str | None = typer.Option(
        None, "--min-out-raw", hidden=True, help="The quote's minOutRaw you confirmed"
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Swap tokens from one, several, or all wallets."""

    for name, raw in (("--expected-out-raw", expected_out_raw), ("--min-out-raw", min_out_raw)):
        if raw is not None and not raw.strip().isdigit():
            _bad_argument(f"{name} must be a whole number in base units", json_output=json_output)
    if sum(v is not None for v in (amount, pct, usd)) != 1:
        _bad_argument("Use exactly one of --amount, --pct or --usd", json_output=json_output)
    if pct is not None and not 0 < pct <= 100:
        _bad_argument("--pct must be above 0 and at most 100", json_output=json_output)
    if usd is not None and usd <= 0:
        _bad_argument("--usd must be above 0", json_output=json_output)
    if all_wallets and wallets:
        _bad_argument("Use either --wallet or --all-wallets, not both", json_output=json_output)
    chain_id = chain_id_from_arg(chain)
    initiator = initiator_for(as_agent)
    session_key = os.environ.get("AGENTOS_SESSION_KEY", "").strip()

    async def _run(client):
        params: dict[str, Any] = {
            "chainId": chain_id,
            "tokenIn": await resolve_token(client, chain_id, token_in),
            "tokenOut": await resolve_token(client, chain_id, token_out),
            "initiator": initiator,
        }
        if amount is not None:
            params["amountIn"] = amount
        elif pct is not None:
            params["amountPct"] = pct
        else:
            params["amountUsd"] = usd
        if all_wallets:
            params["wallets"] = "all"
        elif wallets:
            params["wallets"] = list(wallets)
        if slippage is not None:
            params["slippagePct"] = slippage
        if note:
            params["note"] = note
        if client_id:
            params["clientOrderId"] = client_id.strip()
        if expected_out_raw is not None:
            params["expectedOutRaw"] = expected_out_raw.strip()
        if min_out_raw is not None:
            params["minOutRaw"] = min_out_raw.strip()
        if session_key:
            params["sessionKey"] = session_key
        result = await client.call("trading.swap", params)
        if not wait:
            return result
        settled: list[dict[str, Any]] = []
        for order in _order_rows(result):
            order_id = order.get("orderId")
            if order_id and order.get("status") in _PENDING_STATUSES:
                waited = await client.call(
                    "trading.orders.wait", {"orderId": order_id, "timeoutSeconds": wait_seconds}
                )
                waited_order = waited.get("order") if isinstance(waited, dict) else None
                settled.append(waited_order if isinstance(waited_order, dict) else order)
            else:
                settled.append(order)
        return {"orders": settled}

    try:
        result = run_gateway_sync(_run, json_output=json_output)
    except TokenResolutionError as exc:
        _exit_token_error(exc, json_output=json_output)
        return
    if json_output:
        print_json(result)
        return
    orders = _order_rows(result)
    console.print(_order_table(orders, title=f"Swap ({initiator})"))
    for order in orders:
        if order.get("status") == "awaiting_approval":
            console.print(
                f"Order [{ACCENT}]{order.get('orderId')}[/] is waiting for approval in the app "
                f"(or: agentos trade approve {order.get('orderId')})."
            )


def _parse_recipient(text: str) -> tuple[str, str | None]:
    """``0xabc`` or ``0xabc=10`` → (address, amount or None)."""
    raw = text.strip()
    if "=" in raw:
        address, amount = raw.split("=", 1)
    elif "," in raw:
        address, amount = raw.split(",", 1)
    elif len(raw.split()) == 2:
        address, amount = raw.split()
    else:
        address, amount = raw, ""
    address = address.strip()
    amount = amount.strip()
    if not is_address(address):
        raise ValueError(f"not an address: {address!r}")
    if amount and not amount.replace(".", "", 1).isdigit():
        raise ValueError(f"not an amount: {amount!r} for {address}")
    return address, amount or None


def _recipients_from(
    to: list[str] | None,
    path: str | None,
    amount: str | None,
    usd: float | None,
    *,
    json_output: bool,
) -> list[dict[str, Any]]:
    """Recipients from ``--to`` and/or a file, each sized by its own amount or the shared one."""
    entries: list[str] = list(to or [])
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    text = line.split("#", 1)[0].strip()
                    if text:
                        entries.append(text)
        except OSError as exc:
            _bad_argument(f"cannot read {path}: {exc}", json_output=json_output)
    if not entries:
        _bad_argument("Give at least one --to, or --file", json_output=json_output)
    recipients: list[dict[str, Any]] = []
    for entry in entries:
        try:
            address, own = _parse_recipient(entry)
        except ValueError as exc:
            _bad_argument(str(exc), json_output=json_output)
            return []
        item: dict[str, Any] = {"to": address}
        if own is not None:
            if usd is not None:
                _bad_argument(
                    f"{address} carries its own amount; --usd cannot also size it",
                    json_output=json_output,
                )
            item["amount"] = own
        elif amount is not None:
            item["amount"] = amount
        elif usd is not None:
            item["amountUsd"] = usd
        else:
            _bad_argument(
                f"{address} has no amount: add =<amount> to it, or pass --amount / --usd",
                json_output=json_output,
            )
        recipients.append(item)
    return recipients


@app.command("send")
def trade_send(
    chain: str = typer.Option(..., "--chain", help="base or robinhood"),
    token: str = typer.Option(..., "--token", help="Token to send: symbol, address, or ETH"),
    to: list[str] | None = typer.Option(
        None,
        "--to",
        help="Recipient address, optionally with its own amount as ADDR=AMOUNT (repeatable)",
    ),
    file: str | None = typer.Option(
        None, "--file", help="Recipients file: one 'ADDR' or 'ADDR,AMOUNT' per line; # comments"
    ),
    amount: str | None = typer.Option(
        None, "--amount", help="Amount for every recipient without its own, human units"
    ),
    usd: float | None = typer.Option(
        None, "--usd", help="US dollars' worth of --token for every recipient without its own"
    ),
    wallet: str | None = typer.Option(None, "--wallet", help="Wallet address (default primary)"),
    note: str | None = typer.Option(None, "--note", help="Why (kept in history)"),
    client_id: str | None = typer.Option(
        None,
        "--client-id",
        help=(
            "Idempotency key; re-running with the same id returns the same order "
            "instead of trading twice"
        ),
    ),
    wait: bool = typer.Option(False, "--wait", help="Block until every leg settles"),
    wait_seconds: int = typer.Option(
        300, "--wait-seconds", help="How long --wait blocks per leg", min=1, max=900
    ),
    as_agent: bool = typer.Option(
        False, "--as-agent", help="Apply the agent rules (every send waits for approval)"
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Send a token to one or many addresses. Several --to make one multisend, decided once."""

    if amount is not None and usd is not None:
        _bad_argument("Use one of --amount or --usd, not both", json_output=json_output)
    if usd is not None and usd <= 0:
        _bad_argument("--usd must be above 0", json_output=json_output)
    chain_id = chain_id_from_arg(chain)
    recipients = _recipients_from(to, file, amount, usd, json_output=json_output)
    initiator = initiator_for(as_agent)
    session_key = os.environ.get("AGENTOS_SESSION_KEY", "").strip()

    async def _run(client):
        params: dict[str, Any] = {
            "chainId": chain_id,
            "token": await resolve_token(client, chain_id, token),
            "recipients": recipients,
            "initiator": initiator,
        }
        if wallet:
            params["wallet"] = wallet
        if note:
            params["note"] = note
        if client_id:
            params["clientOrderId"] = client_id.strip()
        if session_key:
            params["sessionKey"] = session_key
        result = await client.call("trading.send", params)
        if not wait:
            return result
        settled: list[dict[str, Any]] = []
        for order in _order_rows(result):
            order_id = order.get("orderId")
            if order_id and order.get("status") in _PENDING_STATUSES:
                waited = await client.call(
                    "trading.orders.wait", {"orderId": order_id, "timeoutSeconds": wait_seconds}
                )
                waited_order = waited.get("order") if isinstance(waited, dict) else None
                settled.append(waited_order if isinstance(waited_order, dict) else order)
            else:
                settled.append(order)
        return {"orders": settled, "batchId": result.get("batchId")}

    try:
        result = run_gateway_sync(_run, json_output=json_output)
    except TokenResolutionError as exc:
        _exit_token_error(exc, json_output=json_output)
        return
    if json_output:
        print_json(result)
        return
    orders = _order_rows(result)
    title = f"Send ({initiator})" if len(orders) == 1 else f"Multisend ({initiator})"
    console.print(_order_table(orders, title=title))
    waiting = [o for o in orders if o.get("status") == "awaiting_approval"]
    if waiting:
        first = waiting[0]
        console.print(
            f"Waiting for approval in the app (or: agentos trade approve {first.get('orderId')}"
            + (" — one approval covers the whole batch)." if len(waiting) > 1 else ").")
        )


@app.command("allowances")
def trade_allowances(
    chain: str | None = typer.Option(None, "--chain", help="base or robinhood (default: both)"),
    wallet: str | None = typer.Option(None, "--wallet", help="Wallet address (default primary)"),
    full: bool = typer.Option(False, "--full", help="Rescan the chain from the wallet's start"),
    wait: bool = typer.Option(
        True,
        "--wait/--no-wait",
        help="Keep polling until the engine's log scan has caught up with the chain",
    ),
    wait_seconds: int = typer.Option(
        600, "--wait-seconds", help="How long --wait polls at most", min=1, max=3600
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """What this wallet has approved others to spend, with live amounts and exposure."""

    params: dict[str, Any] = {}
    if chain:
        params["chainId"] = chain_id_from_arg(chain)
    if wallet:
        params["wallet"] = wallet
    if full:
        params["full"] = True

    async def _run(client):
        # The engine scans in the background and answers at once with what it
        # has; short polls keep every RPC well inside its timeout, however
        # long a first pass over a 0.1 s chain takes.
        result = await client.call("trading.allowances.list", params)
        if not wait:
            return result
        deadline = time.monotonic() + wait_seconds
        again = {k: v for k, v in params.items() if k != "full"}
        while isinstance(result, dict) and result.get("scanning") and time.monotonic() < deadline:
            await asyncio.sleep(2.0)
            result = await client.call("trading.allowances.list", again)
        return result

    result = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(result)
        return
    result = _dict(result)
    rows = [a for a in result.get("allowances", []) if isinstance(a, dict)]
    table = Table(
        title=f"Allowances for {short_address(result.get('wallet'))}",
        show_header=True,
        header_style=ACCENT_HEADER,
    )
    table.add_column("Chain")
    table.add_column("Token")
    table.add_column("Spender")
    table.add_column("Allowance", justify="right")
    table.add_column("Held", justify="right")
    table.add_column("At stake", justify="right")
    table.add_column("Granted in")
    for row in rows:
        spender = row.get("spenderLabel") or short_address(row.get("spender"))
        allowance = row.get("allowance")
        if row.get("readFailed"):
            allowance = "?"
        table.add_row(
            chain_label(row.get("chainId")),
            markup_escape(token_symbol(row.get("token"))),
            markup_escape(str(spender)),
            f"[red]{allowance}[/red]" if row.get("unlimited") else amount_text(allowance),
            amount_text(row.get("balance")),
            money(row.get("exposureUsd")),
            short_address(row.get("lastTxHash")) if row.get("lastTxHash") else "",
        )
    console.print(table)
    if not rows:
        console.print("No live allowances.")
    if result.get("scanning"):
        console.print(
            "[dim]The engine is still scanning older blocks; run again in a moment "
            "for the complete list.[/dim]"
        )
    unlimited = int(result.get("unlimitedCount") or 0)
    if unlimited:
        console.print(
            f"[yellow]{unlimited} unlimited allowance(s).[/yellow] Revoke with: "
            "agentos trade revoke --chain <chain> --token <addr> --spender <addr>"
        )


@app.command("revoke")
def trade_revoke(
    chain: str = typer.Option(..., "--chain", help="base or robinhood"),
    token: str = typer.Option(..., "--token", help="Token: symbol or address"),
    spender: str = typer.Option(..., "--spender", help="Spender address to cut off"),
    wallet: str | None = typer.Option(None, "--wallet", help="Wallet address (default primary)"),
    note: str | None = typer.Option(None, "--note", help="Why (kept in history)"),
    wait: bool = typer.Option(False, "--wait", help="Block until the revoke settles"),
    wait_seconds: int = typer.Option(
        300, "--wait-seconds", help="How long --wait blocks", min=1, max=900
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Set an ERC-20 allowance to zero. From an agent turn this queues for the user's approval."""

    if not is_address(spender):
        _bad_argument(f"--spender must be an address, got {spender!r}", json_output=json_output)
    chain_id = chain_id_from_arg(chain)
    session_key = os.environ.get("AGENTOS_SESSION_KEY", "").strip()

    async def _run(client):
        params: dict[str, Any] = {
            "chainId": chain_id,
            "token": await resolve_token(client, chain_id, token),
            "spender": spender,
            "initiator": initiator_for(False),
        }
        if wallet:
            params["wallet"] = wallet
        if note:
            params["note"] = note
        if session_key:
            params["sessionKey"] = session_key
        result = await client.call("trading.allowances.revoke", params)
        order = result.get("order") if isinstance(result, dict) else None
        if wait and isinstance(order, dict) and order.get("status") in _PENDING_STATUSES:
            waited = await client.call(
                "trading.orders.wait",
                {"orderId": order.get("orderId"), "timeoutSeconds": wait_seconds},
            )
            return waited if isinstance(waited, dict) else result
        return result

    try:
        result = run_gateway_sync(_run, json_output=json_output)
    except TokenResolutionError as exc:
        _exit_token_error(exc, json_output=json_output)
        return
    if json_output:
        print_json(result)
        return
    order = result.get("order") if isinstance(result, dict) else None
    _print_order(order if isinstance(order, dict) else {})
    if isinstance(order, dict) and order.get("status") == "awaiting_approval":
        console.print(
            f"Waiting for approval in the app (or: agentos trade approve {order.get('orderId')})."
        )


@app.command("decode")
def trade_decode(
    tx_hash: str | None = typer.Argument(None, help="Transaction hash to explain"),
    chain: str = typer.Option(..., "--chain", help="base or robinhood"),
    data: str | None = typer.Option(None, "--data", help="Raw calldata instead of a hash"),
    to: str | None = typer.Option(None, "--to", help="Target contract for --data (optional)"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Explain a transaction (what it called, what moved) or raw calldata."""

    if (tx_hash is None) == (data is None):
        _bad_argument("Give a transaction hash, or --data", json_output=json_output)
    chain_id = chain_id_from_arg(chain)
    params: dict[str, Any] = {"chainId": chain_id}
    if tx_hash:
        params["txHash"] = tx_hash
    else:
        params["data"] = data
        if to:
            params["to"] = to

    async def _run(client):
        return await client.call("trading.decode", params)

    result = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(result)
        return
    result = _dict(result)
    call = _dict(result.get("call"))
    console.print(f"[{ACCENT}]{markup_escape(str(result.get('description') or ''))}[/]")
    table = Table(show_header=False)
    table.add_column("Field", style=ACCENT)
    table.add_column("Value")
    tx = _dict(result.get("tx"))
    decoded = _dict(result.get("decoded"))
    to_label = result.get("toLabel") or token_symbol(result.get("toToken")) or ""
    target = str(result.get("to") or "")
    for field, value in (
        ("function", call.get("function") or f"{call.get('selector')} (unknown)"),
        ("to", f"{target} ({to_label})" if to_label else target),
        ("status", tx.get("status")),
        ("from", tx.get("from")),
        ("block", tx.get("blockNumber")),
        ("value", f"{tx.get('valueWei')} wei" if tx.get("valueWei") not in (None, "0") else None),
        ("gas used", tx.get("gasUsed")),
        (
            decoded.get("function") or "",
            (
                f"{amount_text(decoded.get('amount'))} {token_symbol(decoded.get('token'))} "
                f"→ {decoded.get('counterparty')}"
                + (f" ({decoded['counterpartyLabel']})" if decoded.get("counterpartyLabel") else "")
            )
            if decoded
            else None,
        ),
        ("explorer", result.get("explorerUrl")),
    ):
        if value in (None, "", "—"):
            continue
        table.add_row(field, markup_escape(str(value)))
    console.print(table)
    transfers = [t for t in result.get("transfers", []) if isinstance(t, dict)]
    if transfers:
        moved = Table(title="Transfers", show_header=True, header_style=ACCENT_HEADER)
        moved.add_column("Token")
        moved.add_column("Amount", justify="right")
        moved.add_column("From")
        moved.add_column("To")
        for t in transfers:
            moved.add_row(
                markup_escape(token_symbol(t.get("token"))),
                amount_text(t.get("amount")),
                short_address(t.get("from")),
                short_address(t.get("to")),
            )
        console.print(moved)
    approvals = [a for a in result.get("approvals", []) if isinstance(a, dict)]
    if approvals:
        granted = Table(title="Approvals", show_header=True, header_style=ACCENT_HEADER)
        granted.add_column("Token")
        granted.add_column("Owner")
        granted.add_column("Spender")
        granted.add_column("Amount", justify="right")
        for a in approvals:
            granted.add_row(
                markup_escape(token_symbol(a.get("token"))),
                short_address(a.get("owner")),
                markup_escape(str(a.get("spenderLabel") or short_address(a.get("spender")))),
                f"[red]{a.get('amount')}[/red]"
                if a.get("unlimited")
                else amount_text(a.get("amount")),
            )
        console.print(granted)
    wallets = result.get("wallets") or []
    if wallets:
        console.print(f"Involves your wallet(s): {', '.join(short_address(w) for w in wallets)}")


@app.command("network")
def trade_network(
    fresh: bool = typer.Option(False, "--fresh", help="Skip the engine's short cache"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Head block, block age, gas and RPC latency for every chain."""

    params: dict[str, Any] = {"fresh": True} if fresh else {}

    async def _run(client):
        return await client.call("trading.network", params)

    result = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(result)
        return
    rows = [r for r in _dict(result).get("chains", []) if isinstance(r, dict)]
    table = Table(title="Network", show_header=True, header_style=ACCENT_HEADER)
    table.add_column("Chain")
    table.add_column("RPC")
    table.add_column("Head", justify="right")
    table.add_column("Age", justify="right")
    table.add_column("Base fee", justify="right")
    table.add_column("Tip", justify="right")
    table.add_column("Latency", justify="right")
    table.add_column("Healthy")
    for row in rows:
        age = row.get("blockAgeS")
        base_fee = row.get("baseFeeGwei")
        tip = row.get("priorityFeeGwei")
        latency = row.get("latencyMs")
        healthy = row.get("healthy")
        table.add_row(
            str(row.get("name") or row.get("key") or ""),
            markup_escape(str(row.get("rpcUrl") or "")),
            str(row.get("blockNumber") or "—"),
            f"{age} s" if age is not None else "—",
            f"{base_fee:.4f} gwei" if isinstance(base_fee, int | float) else "—",
            f"{tip:.4f} gwei" if isinstance(tip, int | float) else "—",
            f"{latency} ms" if latency is not None else "—",
            "[green]yes[/green]"
            if healthy
            else f"[red]no[/red] {markup_escape(str(row.get('error') or ''))}".strip(),
        )
    console.print(table)


@app.command("orders")
def trade_orders(
    status: str | None = typer.Option(None, "--status", help="Filter by status"),
    wallet: str | None = typer.Option(None, "--wallet", help="Filter by wallet address"),
    kind: str | None = typer.Option(
        None, "--kind", help="swap, send, revoke, lp_collect, lp_remove or lp_add"
    ),
    limit: int = typer.Option(50, "--limit", help="Max rows", min=1, max=500),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """List recent orders (pending approvals first)."""

    params: dict[str, Any] = {"limit": limit}
    if status:
        params["status"] = status
    if wallet:
        params["wallet"] = wallet
    if kind:
        if kind not in ORDER_KINDS:
            _bad_argument(
                f"--kind must be one of {', '.join(ORDER_KINDS)}", json_output=json_output
            )
        params["kind"] = kind

    async def _run(client):
        return await client.call("trading.orders.list", params)

    result = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(result)
        return
    orders = _order_rows(result)
    console.print(_order_table(orders))
    pending = result.get("pendingApprovals") if isinstance(result, dict) else None
    if pending:
        console.print(f"{pending} order(s) awaiting approval.")


@app.command("order")
def trade_order(
    order_id: str = typer.Argument(..., help="Order id"),
    wait: bool = typer.Option(False, "--wait", help="Block until the order settles"),
    wait_seconds: int = typer.Option(
        300, "--wait-seconds", help="How long --wait blocks", min=1, max=900
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show one order, optionally waiting for it to settle."""

    async def _run(client):
        if wait:
            return await client.call(
                "trading.orders.wait", {"orderId": order_id, "timeoutSeconds": wait_seconds}
            )
        return await client.call("trading.orders.get", {"orderId": order_id})

    result = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(result)
        return
    order = result.get("order") if isinstance(result, dict) else None
    _print_order(order if isinstance(order, dict) else {})


@app.command("approve")
def trade_approve(
    order_id: str = typer.Argument(..., help="Order id awaiting approval"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Approve a parked order.

    Agent swaps park above the approval threshold or the price-impact ceiling,
    or when the engine cannot price them; agent sends and revokes always park,
    and so does every LP write (``trade lp collect|remove|add``), whoever asked.
    """

    async def _run(client):
        return await client.call("trading.orders.approve", {"orderId": order_id})

    result = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(result)
        return
    order = result.get("order") if isinstance(result, dict) else None
    _print_order(order if isinstance(order, dict) else {})


@app.command("reject")
def trade_reject(
    order_id: str = typer.Argument(..., help="Order id awaiting approval"),
    reason: str | None = typer.Option(None, "--reason", help="Why (returned to the agent)"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Reject a queued order."""

    params: dict[str, Any] = {"orderId": order_id}
    if reason:
        params["reason"] = reason

    async def _run(client):
        return await client.call("trading.orders.reject", params)

    result = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(result)
        return
    order = result.get("order") if isinstance(result, dict) else None
    _print_order(order if isinstance(order, dict) else {})


@app.command("history")
def trade_history(
    wallet: str | None = typer.Option(None, "--wallet", help="Filter by wallet address"),
    chain: str | None = typer.Option(None, "--chain", help="base or robinhood"),
    kind: str | None = typer.Option(
        None,
        "--kind",
        help="swap, deposit, withdraw, gas, approval, unwrap, lp_add, lp_collect, lp_remove",
    ),
    limit: int = typer.Option(100, "--limit", help="Max rows", min=1, max=1000),
    hidden: bool = typer.Option(False, "--hidden", help="Include entries of hidden (junk) tokens"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show the ledger: swaps, deposits, withdrawals, approvals."""

    params: dict[str, Any] = {"limit": limit}
    if wallet:
        params["wallet"] = wallet
    if chain:
        params["chainId"] = chain_id_from_arg(chain)
    if kind:
        params["kind"] = kind
    if hidden:
        params["includeHidden"] = True

    async def _run(client):
        return await client.call("trading.history", params)

    result = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(result)
        return
    rows = result.get("entries", []) if isinstance(result, dict) else []
    table = Table(title="History", show_header=True, header_style=ACCENT_HEADER)
    table.add_column("When")
    table.add_column("Kind")
    table.add_column("Chain")
    table.add_column("Wallet")
    table.add_column("In")
    table.add_column("Out")
    table.add_column("Value", justify="right")
    table.add_column("Gas", justify="right")
    table.add_column("By")
    table.add_column("Tx")
    for row in rows:
        if not isinstance(row, dict):
            continue
        token_in = row.get("tokenIn")
        token_out = row.get("tokenOut")
        table.add_row(
            str(row.get("ts") or ""),
            str(row.get("kind") or ""),
            chain_label(row.get("chainId")),
            short_address(row.get("wallet")),
            markup_escape(
                f"{amount_text(row.get('amountIn'))} {token_symbol(token_in)}" if token_in else "—"
            ),
            markup_escape(
                f"{amount_text(row.get('amountOut'))} {token_symbol(token_out)}"
                if token_out
                else "—"
            ),
            money(row.get("valueUsd")),
            money(row.get("gasUsd")),
            str(row.get("initiator") or ""),
            short_address(row.get("txHash")) if row.get("txHash") else "",
        )
    console.print(table)


@app.command("portfolio")
def trade_portfolio(
    wallet: str | None = typer.Option(None, "--wallet", help="One wallet (default: all)"),
    hidden: bool = typer.Option(False, "--hidden", help="Include hidden (junk) tokens"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Holdings with cost basis, realized and unrealized PnL."""

    params: dict[str, Any] = {}
    if wallet:
        params["wallet"] = wallet
    if hidden:
        params["includeHidden"] = True

    async def _run(client):
        return await client.call("trading.portfolio", params)

    result = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(result)
        return
    result = _dict(result)
    totals = _dict(result.get("totals"))
    summary = Table(title="Portfolio", show_header=False)
    summary.add_column("Field", style=ACCENT)
    summary.add_column("Value", justify="right")
    summary.add_row("value", money(totals.get("valueUsd")))
    summary.add_row("cost", money(totals.get("costUsd")))
    summary.add_row("unrealized", money(totals.get("unrealizedUsd")))
    summary.add_row("realized", money(totals.get("realizedUsd")))
    summary.add_row("gas", money(totals.get("gasUsd")))
    summary.add_row(
        "24h",
        f"{money(totals.get('change24hUsd'))} ({percent(totals.get('change24hPct'))})",
    )
    console.print(summary)
    holdings = result.get("holdings", [])
    table = Table(title="Holdings", show_header=True, header_style=ACCENT_HEADER)
    table.add_column("Chain")
    table.add_column("Token")
    table.add_column("Amount", justify="right")
    table.add_column("Price", justify="right")
    table.add_column("Value", justify="right")
    table.add_column("Avg cost", justify="right")
    table.add_column("Unrealized", justify="right")
    table.add_column("Realized", justify="right")
    table.add_column("Alloc", justify="right")
    for row in holdings if isinstance(holdings, list) else []:
        if not isinstance(row, dict):
            continue
        symbol = markup_escape(token_symbol(row.get("token")))
        table.add_row(
            chain_label(row.get("chainId")),
            f"[dim]{symbol} (hidden)[/dim]" if row.get("hidden") else symbol,
            amount_text(row.get("amount")),
            money(row.get("priceUsd")),
            money(row.get("valueUsd")),
            money(row.get("avgCostUsd")),
            f"{money(row.get('unrealizedUsd'))} ({percent(row.get('unrealizedPct'))})",
            money(row.get("realizedUsd")),
            percent(row.get("allocationPct")),
        )
    console.print(table)
    hidden_count = int(result.get("hiddenCount") or 0)
    if hidden_count and not hidden:
        noun = "token" if hidden_count == 1 else "tokens"
        console.print(
            f"[dim]{hidden_count} junk {noun} hidden and not counted; add --hidden to list "
            "them, or `agentos trade unhide` to keep one.[/dim]"
        )
    if result.get("syncing"):
        console.print("Ledger sync in progress; numbers may still move.")


@app.command("sync")
def trade_sync(
    wallet: str | None = typer.Option(None, "--wallet", help="One wallet (default: all)"),
    full: bool = typer.Option(False, "--full", help="Rebuild lots and history from the chain"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Re-read the chain into the ledger."""

    params: dict[str, Any] = {"full": full}
    if wallet:
        params["wallet"] = wallet

    async def _run(client):
        return await client.call("trading.sync", params)

    result = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(result)
        return
    console.print("Sync started." + (" Full rebuild." if full else ""))


@app.command("limits")
def trade_limits(
    wallet: str | None = typer.Argument(None, help="Wallet address (default: the primary wallet)"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show the agent guardrails and today's spend for a wallet."""

    async def _run(client):
        target = wallet
        if not target:
            status = await client.call("wallet.status", {})
            target = (status or {}).get("primary") if isinstance(status, dict) else None
            if not target:
                _bad_argument("No primary wallet; pass a wallet address", json_output=json_output)
        return await client.call("trading.limits", {"wallet": target})

    result = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(result)
        return
    result = result if isinstance(result, dict) else {}
    shown = str(wallet or result.get("wallet") or "")
    table = Table(title=f"Limits for {short_address(shown)}", show_header=False)
    table.add_column("Field", style=ACCENT)
    table.add_column("Value", justify="right")
    table.add_row("daily cap", money(result.get("dailyCapUsd")))
    table.add_row("spent today", money(result.get("spentTodayUsd")))
    table.add_row("approval threshold", money(result.get("approvalThresholdUsd")))
    table.add_row("approval TTL", f"{result.get('approvalTtlSeconds', '—')} s")
    console.print(table)


# ── trade lp: Uniswap V4 liquidity read-outs (docs/lp-cards.md) ────────────

LP_MIME = "application/vnd.agentos.lp+json"
#: Where ``trade lp --json`` writes its card payloads, relative to the working
#: directory (an agent's shell runs in its workspace, which is what lets the
#: gateway publish the file).
LP_CARD_DIR = "lp-cards"
#: Card files kept in ``LP_CARD_DIR``; older ones are deleted after each write.
#: The gateway copies a published card into its artifact store, so the file in
#: the workspace is only needed until the command's output has been read.
LP_CARDS_KEPT = 20
_LP_SLUG = re.compile(r"[^A-Za-z0-9._-]+")
_LP_CARD_FILE = re.compile(r"^(pool|ranges|position|positions)-[A-Za-z0-9._-]*\.json$")
#: Gateway error codes that mean "change the input", not "something broke".
_LP_USAGE_CODES = frozenset(
    {"trading.invalid", "trading.lp.not_a_wallet", "trading.lp.pool_key_unknown"}
)


class _LpGroup(_JsonUsageGroup):
    """``trade lp``: a usage error under ``--json`` is a JSON error on stderr, exit 2."""


lp_app = typer.Typer(
    cls=_LpGroup,
    help=(
        "Uniswap V4 liquidity on Base and Robinhood Chain: read a token's pool, its "
        "liquidity ranges, one position or every position of your wallets; collect fees, "
        "remove or add liquidity (every write waits for your approval)."
    ),
)
app.add_typer(lp_app, name="lp")


# ── bridge (Base ⇄ Robinhood Chain through Relay; docs/bridge.md) ─────────────

bridge_app = typer.Typer(
    cls=_JsonUsageGroup,
    help=(
        "Bridge ETH or the USD stable (USDC ⇄ USDG) between Base and Robinhood Chain "
        "through Relay. From an agent a bridge always waits for your approval; the order "
        "is confirmed only once the funds have landed on the destination chain."
    ),
)
app.add_typer(bridge_app, name="bridge")

#: Engine refusals of a bridge that mean "fix the input", answered as exit 2.
_BRIDGE_USAGE_CODES = frozenset(
    {
        "trading.invalid",
        "trading.bridge.unsupported_route",
        "trading.unpriced",
        "trading.unknown_token",
        "trading.insufficient_balance",
        "trading.unsupported_chain",
    }
)
#: Registry symbols the engine resolves itself: never searched as a symbol
#: (bare ``USDC`` on Robinhood Chain resolves to lookalikes).
_BRIDGE_SYMBOLS = frozenset({"ETH", "USDC", "USDG"})


def _bridge_params(
    chain: str,
    to_chain: str,
    token: str,
    amount: str | None,
    usd: float | None,
    pct: float | None,
    wallet: str | None,
    *,
    json_output: bool,
) -> dict[str, Any]:
    given = [x for x in (amount, usd, pct) if x is not None]
    if len(given) != 1:
        _bad_argument("Use exactly one of --amount, --usd or --pct", json_output=json_output)
    if usd is not None and usd <= 0:
        _bad_argument("--usd must be above 0", json_output=json_output)
    if pct is not None and not 0 < pct <= 100:
        _bad_argument("--pct must be within (0, 100]", json_output=json_output)
    params: dict[str, Any] = {
        "chainId": chain_id_from_arg(chain),
        "toChainId": chain_id_from_arg(to_chain),
        "token": token.strip(),
    }
    if amount is not None:
        params["amount"] = amount
    if usd is not None:
        params["amountUsd"] = usd
    if pct is not None:
        params["amountPct"] = pct
    if wallet:
        params["wallet"] = wallet
    return params


async def _bridge_token(client: Any, params: dict[str, Any]) -> str:
    text = str(params["token"])
    if text.upper() in _BRIDGE_SYMBOLS:
        return text.upper()
    return await resolve_token(client, int(params["chainId"]), text)


def _bridge_call(
    method: str,
    params: dict[str, Any],
    *,
    wait: bool,
    wait_seconds: int,
    json_output: bool,
) -> dict[str, Any]:
    async def _run(client):
        from agentos.cli.gateway_client import GatewayRPCError

        params["token"] = await _bridge_token(client, params)
        try:
            result = await client.call(method, params)
        except GatewayRPCError as exc:
            if exc.code not in _BRIDGE_USAGE_CODES:
                raise
            emit_error(exc.message, json_output=json_output, code=exc.code, details=exc.data)
            raise typer.Exit(2) from exc
        order = _dict(result).get("order")
        if wait and isinstance(order, dict) and order.get("status") in _PENDING_STATUSES:
            waited = await client.call(
                "trading.orders.wait",
                {"orderId": order.get("orderId"), "timeoutSeconds": wait_seconds},
            )
            if isinstance(waited, dict) and isinstance(waited.get("order"), dict):
                result = waited
        return result

    try:
        return _dict(run_gateway_sync(_run, json_output=json_output))
    except TokenResolutionError as exc:
        _exit_token_error(exc, json_output=json_output)
        raise typer.Exit(2) from exc


def _print_bridge_quote(quote: dict[str, Any]) -> None:
    table = Table(title="Bridge quote", show_header=False)
    table.add_column("Field", style=ACCENT)
    table.add_column("Value")
    route = _dict(quote.get("route"))
    guard = _dict(quote.get("guard"))
    for field, value in (
        (
            "route",
            f"{chain_label(quote.get('originChainId'))} → "
            f"{chain_label(quote.get('destinationChainId'))} ({route.get('asset')})",
        ),
        ("wallet", quote.get("wallet")),
        ("in", f"{amount_text(quote.get('amountIn'))} {token_symbol(quote.get('tokenIn'))}"),
        ("out", f"≈ {amount_text(quote.get('expectedOut'))} {token_symbol(quote.get('tokenOut'))}"),
        ("min out", quote.get("minOut")),
        ("rate", quote.get("rate")),
        ("fees", money(quote.get("feesUsd"))),
        ("value", money(quote.get("valueUsd"))),
        (
            "time",
            f"~{quote.get('timeEstimateSeconds')} s" if quote.get("timeEstimateSeconds") else None,
        ),
        ("balance", f"{amount_text(quote.get('balance'))} {token_symbol(quote.get('tokenIn'))}"),
        ("guard", f"{guard.get('decision')} — {guard.get('reason')}" if guard else None),
        ("request", quote.get("requestId")),
    ):
        if value in (None, "", "—"):
            continue
        table.add_row(field, markup_escape(str(value)))
    console.print(table)


@bridge_app.command("quote")
def trade_bridge_quote(
    chain: str = typer.Option(..., "--chain", help="Origin chain: base or robinhood"),
    to_chain: str = typer.Option(..., "--to-chain", help="Destination chain: base or robinhood"),
    token: str = typer.Option(..., "--token", help="ETH, USDC, USDG, or an address on the origin"),
    amount: str | None = typer.Option(None, "--amount", help="Amount to bridge, human units"),
    usd: float | None = typer.Option(None, "--usd", help="US dollars' worth of --token"),
    pct: float | None = typer.Option(None, "--pct", help="Share of the balance, 0 < pct <= 100"),
    wallet: str | None = typer.Option(None, "--wallet", help="Wallet address (default primary)"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Price a bridge: what arrives on the other chain, the fees, and whether it would wait."""

    params = _bridge_params(
        chain, to_chain, token, amount, usd, pct, wallet, json_output=json_output
    )
    params["initiator"] = initiator_for(False)
    result = _bridge_call(
        "trading.bridge.quote", params, wait=False, wait_seconds=0, json_output=json_output
    )
    if json_output:
        print_json(result)
        return
    _print_bridge_quote(_dict(result.get("quote")))


@bridge_app.command("create")
def trade_bridge_create(
    chain: str = typer.Option(..., "--chain", help="Origin chain: base or robinhood"),
    to_chain: str = typer.Option(..., "--to-chain", help="Destination chain: base or robinhood"),
    token: str = typer.Option(..., "--token", help="ETH, USDC, USDG, or an address on the origin"),
    amount: str | None = typer.Option(None, "--amount", help="Amount to bridge, human units"),
    usd: float | None = typer.Option(None, "--usd", help="US dollars' worth of --token"),
    pct: float | None = typer.Option(None, "--pct", help="Share of the balance, 0 < pct <= 100"),
    wallet: str | None = typer.Option(None, "--wallet", help="Wallet address (default primary)"),
    note: str | None = typer.Option(None, "--note", help="Why (kept in history)"),
    client_id: str | None = typer.Option(
        None,
        "--client-id",
        help=(
            "Idempotency key; re-running with the same id returns the same order "
            "instead of bridging twice"
        ),
    ),
    wait: bool = typer.Option(
        False, "--wait", help="Block until the funds have landed on the destination chain"
    ),
    wait_seconds: int = typer.Option(
        300, "--wait-seconds", help="How long --wait blocks", min=1, max=900
    ),
    as_agent: bool = typer.Option(
        False, "--as-agent", help="Apply the agent rules (every bridge waits for approval)"
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Bridge a token to the other chain. From an agent the order always waits for approval."""

    params = _bridge_params(
        chain, to_chain, token, amount, usd, pct, wallet, json_output=json_output
    )
    params["initiator"] = initiator_for(as_agent)
    session_key = os.environ.get("AGENTOS_SESSION_KEY", "").strip()
    if session_key:
        params["sessionKey"] = session_key
    if note:
        params["note"] = note
    if client_id:
        params["clientOrderId"] = client_id.strip()
    result = _bridge_call(
        "trading.bridge.create",
        params,
        wait=wait,
        wait_seconds=wait_seconds,
        json_output=json_output,
    )
    if json_output:
        print_json(result)
        return
    order = _dict(result.get("order"))
    _print_order(order)
    if order.get("status") == "awaiting_approval":
        console.print(
            f"Waiting for approval in the app (or: agentos trade approve {order.get('orderId')})."
        )
    elif order.get("status") == "submitted":
        console.print(
            "The deposit is on chain; the funds land on the destination once Relay fills it "
            f"(agentos trade bridge status {order.get('orderId')})."
        )


@bridge_app.command("status")
def trade_bridge_status(
    reference: str = typer.Argument(..., help="Order id, or Relay request id (0x…)"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Where a bridge stands: the deposit, the fill on the other chain, the final status."""

    key = "requestId" if reference.strip().lower().startswith("0x") else "orderId"

    async def _run(client):
        from agentos.cli.gateway_client import GatewayRPCError

        try:
            return await client.call("trading.bridge.status", {key: reference.strip()})
        except GatewayRPCError as exc:
            if exc.code not in (*_BRIDGE_USAGE_CODES, "trading.bridge.not_found"):
                raise
            emit_error(exc.message, json_output=json_output, code=exc.code, details=exc.data)
            raise typer.Exit(2) from exc

    result = _dict(run_gateway_sync(_run, json_output=json_output))
    if json_output:
        print_json(result)
        return
    _print_order(_dict(result.get("order")))


def _lp_chain(chains: list[str] | None, command: str, *, json_output: bool) -> int | None:
    """The one ``--chain`` a single-card command takes; a repeat is refused, not truncated.

    Click keeps only the last of a repeated option, so ``--chain base --chain
    robinhood`` silently read Robinhood alone; the option is a list so the
    repeat can be seen and refused.
    """
    values = [c for c in chains or [] if c.strip()]
    if len(values) > 1:
        _bad_argument(
            f"`trade lp {command}` takes one --chain (got {', '.join(values)}); "
            "omit it to try base, then robinhood",
            json_output=json_output,
        )
    return chain_id_from_arg(values[0]) if values else None


def _lp_chains(chains: list[str] | None) -> list[int]:
    """Every ``--chain`` given, validated and de-duplicated, in order."""
    return list(dict.fromkeys(chain_id_from_arg(c) for c in chains or [] if c.strip()))


def _lp_call(method: str, params: dict[str, Any], *, json_output: bool) -> Any:
    """Call a ``trading.lp.*`` method; an input the engine rejects exits 2, not 1."""

    async def _run(client):
        from agentos.cli.gateway_client import GatewayRPCError

        try:
            return await client.call(method, params)
        except GatewayRPCError as exc:
            if exc.code not in _LP_USAGE_CODES:
                raise
            emit_error(exc.message, json_output=json_output, code=exc.code, details=exc.data)
            raise typer.Exit(2) from exc

    return run_gateway_sync(_run, json_output=json_output)


def _usd(value: Any) -> str:
    """Dollars, keeping significant digits for sub-cent prices; "—" when unknown."""
    if value is None or value == "":
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if 0 < abs(number) < 0.01:
        return f"${number:.4g}"
    return money(number)


def _lp_card_name(result: dict[str, Any]) -> str:
    kind = str(result.get("kind") or "lp")
    if kind == "positions":
        slug = "wallets"
    elif kind == "position":
        position = _dict(result.get("position"))
        chain = _dict(position.get("chain")).get("key") or ""
        slug = f"{position.get('tokenId') or ''}-{chain}"
    else:
        token = _dict(result.get("token"))
        slug = f"{token.get('symbol') or 'token'}-{_dict(result.get('chain')).get('key') or ''}"
    slug = _LP_SLUG.sub("", slug).strip("-") or kind
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"{LP_CARD_DIR}/{kind}-{slug}-{stamp}.json"


def _write_lp_card(result: dict[str, Any]) -> None:
    """Write the card payload and announce it; the marker is the last line on stdout.

    Never fatal: the reading has already been printed, and a card that could not
    be written must not turn it into a failure.
    """
    name = _lp_card_name(result)
    try:
        path = Path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        typer.echo(f"[card not written: {exc}]", err=True)
        return
    _prune_lp_cards(path.parent)
    print_text(f"publish_artifact path={name} mime={LP_MIME}")


def _prune_lp_cards(directory: Path, keep: int = LP_CARDS_KEPT) -> None:
    """Keep the ``keep`` newest card files in ``directory``; never touch anything else."""
    _prune_cards(directory, _LP_CARD_FILE, keep)


def _prune_cards(directory: Path, pattern: re.Pattern[str], keep: int) -> None:
    """Keep the ``keep`` newest files matching ``pattern``; never touch anything else."""
    try:
        cards = [p for p in directory.iterdir() if p.is_file() and pattern.match(p.name)]
        cards.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
        for old in cards[keep:]:
            old.unlink(missing_ok=True)
    except OSError:
        return


def _lp_emit(
    result: Any, *, json_output: bool, no_card: bool, render: Callable[[dict[str, Any]], None]
) -> None:
    """``--json``: the payload, then the card and its marker. Otherwise the table only."""
    payload = _dict(result)
    if not json_output:
        render(payload)
        return
    print_json(payload)
    if not no_card and payload.get("kind"):
        _write_lp_card(payload)


def _lp_footer(result: dict[str, Any]) -> None:
    if result.get("partialScan"):
        console.print("[yellow]partial scan[/]: some liquidity or positions were not read")
    for warning in result.get("warnings") or []:
        console.print(f"• {markup_escape(str(warning))}")
    console.print(f"as of block {result.get('asOfBlock')} · {result.get('fetchedAt')}")


def _range_text(item: dict[str, Any]) -> str:
    low, high = item.get("mcapLower"), item.get("mcapUpper")
    if low is not None and high is not None:
        return f"{_usd(low)} → {_usd(high)} mcap"
    return f"{item.get('priceLower')} → {item.get('priceUpper')}"


def _render_lp_pool(result: dict[str, Any]) -> None:
    token, quote = _dict(result.get("token")), _dict(result.get("quote"))
    pool, reserves = _dict(result.get("pool")), _dict(result.get("reserves"))
    guard = _dict(result.get("safety"))
    launcher = _dict(guard.get("launcher"))
    locked = guard.get("locked")
    table = Table(
        title=f"{token.get('symbol')}/{quote.get('symbol')} · "
        f"{_dict(result.get('chain')).get('name')} · {pool.get('feePct')}",
        show_header=False,
    )
    table.add_column("Field", style=ACCENT)
    table.add_column("Value")
    for field, value in (
        ("pool", pool.get("poolId")),
        ("hook", pool.get("hook") or "none"),
        ("price", _usd(pool.get("priceUsd"))),
        ("market cap", _usd(pool.get("mcapUsd"))),
        ("TVL", _usd(pool.get("tvlUsd"))),
        (
            f"reserve {token.get('symbol')}",
            f"{_dict(reserves.get('base')).get('human')} "
            f"({_usd(_dict(reserves.get('base')).get('usd'))})",
        ),
        (
            f"reserve {quote.get('symbol')}",
            f"{_dict(reserves.get('quote')).get('human')} "
            f"({_usd(_dict(reserves.get('quote')).get('usd'))})",
        ),
        ("launcher", launcher.get("name") or "—"),
        ("LP locked", "unknown" if locked is None else ("yes" if locked else "no")),
        ("note", guard.get("note")),
    ):
        if value in (None, ""):
            continue
        table.add_row(field, markup_escape(str(value)))
    console.print(table)
    top = [r for r in result.get("topRanges") or [] if isinstance(r, dict)]
    if top:
        ranges = Table(title="Largest ranges", show_header=True, header_style=ACCENT_HEADER)
        ranges.add_column("Range")
        ranges.add_column("Share", justify="right")
        ranges.add_column("Owner")
        for r in top:
            share = r.get("share")
            ranges.add_row(
                markup_escape(_range_text(r)),
                "—" if share is None else f"{float(share) * 100:.1f}%",
                short_address(r.get("owner")) or "—",
            )
        console.print(ranges)
    _lp_footer(result)


def _render_lp_ranges(result: dict[str, Any]) -> None:
    token, quote = _dict(result.get("token")), _dict(result.get("quote"))
    current = _dict(result.get("current"))
    table = Table(
        title=f"{token.get('symbol')}/{quote.get('symbol')} liquidity · "
        f"now {_usd(current.get('priceUsd'))} ({_usd(current.get('mcapUsd'))} mcap)",
        show_header=True,
        header_style=ACCENT_HEADER,
    )
    table.add_column("Range")
    table.add_column("Share", justify="right")
    table.add_column(str(token.get("symbol") or "base"), justify="right")
    table.add_column(str(quote.get("symbol") or "quote"), justify="right")
    table.add_column("")
    for seg in result.get("segments") or []:
        if not isinstance(seg, dict):
            continue
        share = seg.get("share")
        table.add_row(
            markup_escape(_range_text(seg)),
            "—" if share is None else f"{float(share) * 100:.1f}%",
            str(_dict(seg.get("base")).get("human")),
            str(_dict(seg.get("quote")).get("human")),
            "◀ now" if seg.get("active") else "",
        )
    console.print(table)
    _lp_footer(result)


def _position_row(position: dict[str, Any]) -> list[str]:
    token, quote = _dict(position.get("token")), _dict(position.get("quote"))
    fees = _dict(position.get("fees"))
    owner = _dict(position.get("owner"))
    return [
        f"#{position.get('tokenId')}",
        chain_label(_dict(position.get("chain")).get("id")),
        f"{token.get('symbol')}/{quote.get('symbol')}",
        str(position.get("status") or ""),
        str(position.get("band") or "—"),
        _usd(position.get("valueUsd")),
        _usd(fees.get("usd")) if fees else "—",
        str(owner.get("label") or short_address(owner.get("address"))),
    ]


def _positions_table(positions: list[dict[str, Any]], title: str) -> Table:
    table = Table(title=title, show_header=True, header_style=ACCENT_HEADER)
    for column in ("Position", "Chain", "Pair", "Status", "Band"):
        table.add_column(column)
    table.add_column("Value", justify="right")
    table.add_column("Fees", justify="right")
    table.add_column("Owner")
    for position in positions:
        table.add_row(*(markup_escape(cell) for cell in _position_row(position)))
    return table


def _render_lp_position(result: dict[str, Any]) -> None:
    position = _dict(result.get("position"))
    console.print(_positions_table([position], "Position"))
    principal = _dict(position.get("principal"))
    token, quote = _dict(position.get("token")), _dict(position.get("quote"))
    console.print(
        f"principal {_dict(principal.get('base')).get('human')} {token.get('symbol')} + "
        f"{_dict(principal.get('quote')).get('human')} {quote.get('symbol')}"
    )
    _lp_footer(result)


def _render_lp_positions(result: dict[str, Any]) -> None:
    positions = [p for p in result.get("positions") or [] if isinstance(p, dict)]
    totals = _dict(result.get("totals"))
    if not positions:
        wallets = ", ".join(
            str(w.get("label") or short_address(w.get("address")))
            for w in result.get("wallets") or []
            if isinstance(w, dict)
        )
        chains = ", ".join(
            str(c.get("name")) for c in result.get("chains") or [] if isinstance(c, dict)
        )
        console.print(f"No Uniswap V4 positions in {wallets or 'no wallets'} on {chains}.")
    else:
        console.print(
            _positions_table(
                positions,
                f"V4 positions · {totals.get('count')} · {_usd(totals.get('valueUsd'))} "
                f"· {totals.get('outOfRange')} out of range",
            )
        )
    _lp_footer(result)


_PAIR_TARGET_HELP = "Token symbol or address, a TOKEN/QUOTE pair (ETH/USDC), or a V4 poolId"
_QUOTE_HELP = "Only pools paired with this token (same as writing TOKEN/QUOTE)"
_FEE_HELP = (
    "Only pools on this fee tier: a percent (0.05, 0.3%, 1) or V4 units (500, 3000), "
    "or 'dynamic'; the deepest wins if several share it"
)


def _lp_pool_params(params: dict[str, Any], quote: str | None, fee: str | None) -> dict[str, Any]:
    if quote:
        params["quote"] = quote
    if fee is not None and fee.strip():
        params["feePct"] = fee.strip()
    return params


@lp_app.command("pool")
def lp_pool(
    target: str = typer.Argument(..., help=_PAIR_TARGET_HELP),
    chain: list[str] | None = typer.Option(
        None, "--chain", help="base or robinhood (default: try both)"
    ),
    quote: str | None = typer.Option(None, "--quote", help=_QUOTE_HELP),
    fee: str | None = typer.Option(None, "--fee", help=_FEE_HELP),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    no_card: bool = typer.Option(
        False, "--no-card", help="With --json: do not write the chat card"
    ),
) -> None:
    """A token's deepest V4 pool: reserves, TVL, launcher, LP lock, biggest ranges."""

    params: dict[str, Any] = {"target": target}
    chain_id = _lp_chain(chain, "pool", json_output=json_output)
    if chain_id is not None:
        params["chainId"] = chain_id
    _lp_pool_params(params, quote, fee)

    result = _lp_call("trading.lp.pool", params, json_output=json_output)
    _lp_emit(result, json_output=json_output, no_card=no_card, render=_render_lp_pool)


@lp_app.command("ranges")
def lp_ranges(
    target: str = typer.Argument(..., help=_PAIR_TARGET_HELP),
    chain: list[str] | None = typer.Option(
        None, "--chain", help="base or robinhood (default: try both)"
    ),
    quote: str | None = typer.Option(None, "--quote", help=_QUOTE_HELP),
    fee: str | None = typer.Option(None, "--fee", help=_FEE_HELP),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    no_card: bool = typer.Option(
        False, "--no-card", help="With --json: do not write the chat card"
    ),
) -> None:
    """How a pool's liquidity is spread across price (market-cap) ranges."""

    params: dict[str, Any] = {"target": target}
    chain_id = _lp_chain(chain, "ranges", json_output=json_output)
    if chain_id is not None:
        params["chainId"] = chain_id
    _lp_pool_params(params, quote, fee)

    result = _lp_call("trading.lp.ranges", params, json_output=json_output)
    _lp_emit(result, json_output=json_output, no_card=no_card, render=_render_lp_ranges)


@lp_app.command("position")
def lp_position(
    token_id: str = typer.Argument(..., help="Position NFT id"),
    chain: list[str] = typer.Option(..., "--chain", help="base or robinhood"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    no_card: bool = typer.Option(
        False, "--no-card", help="With --json: do not write the chat card"
    ),
) -> None:
    """One V4 position: range, in/out of range, principal, uncollected fees."""

    text = token_id.strip().lstrip("#")
    if not text.isdigit() or int(text) <= 0:
        _bad_argument("tokenId must be a positive integer", json_output=json_output)
    params = {"chainId": _lp_chain(chain, "position", json_output=json_output), "tokenId": text}

    result = _lp_call("trading.lp.position", params, json_output=json_output)
    _lp_emit(result, json_output=json_output, no_card=no_card, render=_render_lp_position)


@lp_app.command("positions")
def lp_positions(
    wallet: list[str] | None = typer.Option(
        None, "--wallet", help="Address (or vault label) to read; repeatable. Default: the vault"
    ),
    chain: list[str] | None = typer.Option(
        None, "--chain", help="base or robinhood; repeatable (default: both)"
    ),
    include_closed: bool = typer.Option(False, "--all", help="Include closed (empty) positions"),
    budget_seconds: float | None = typer.Option(
        None,
        "--budget-seconds",
        help="Stop searching after this many seconds and answer with what was found "
        "(default 25; 5-300)",
        min=5,
        max=300,
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    no_card: bool = typer.Option(
        False, "--no-card", help="With --json: do not write the chat card"
    ),
) -> None:
    """Every V4 position of your vault wallets (or --wallet), out-of-range first."""

    params: dict[str, Any] = {}
    chain_ids = _lp_chains(chain)
    if len(chain_ids) == 1:
        params["chainId"] = chain_ids[0]
    elif chain_ids:
        params["chainIds"] = chain_ids
    if wallet:
        params["wallets"] = list(wallet)
    if include_closed:
        params["all"] = True
    if budget_seconds is not None:
        params["budgetSeconds"] = budget_seconds

    result = _lp_call("trading.lp.positions", params, json_output=json_output)
    _lp_emit(result, json_output=json_output, no_card=no_card, render=_render_lp_positions)


# ── trade lp collect|remove|add: LP writes (docs/lp-write.md) ──────────────

#: Engine codes that mean "change the input" for an LP write: exit 2, not 1.
_LP_WRITE_USAGE_CODES = frozenset(
    {
        "trading.invalid",
        "trading.lp.range_invalid",
        "trading.lp.not_owner",
        "trading.lp.position_closed",
        "trading.lp.nothing_to_collect",
        "trading.lp.not_found",
        "trading.lp.pool_key_unknown",
        "trading.slippage_too_high",
        "trading.unknown_token",
    }
)


def _lp_token_id(value: str, name: str, *, json_output: bool) -> str:
    text = value.strip().lstrip("#")
    if not text.isdigit() or int(text) <= 0:
        _bad_argument(f"{name} must be a positive integer", json_output=json_output)
    return text


async def _lp_card_after(client: Any, order: dict[str, Any]) -> dict[str, Any] | None:
    """The refreshed card for a confirmed write: the position, or (after a burn) the wallet's."""
    plan = _dict(order.get("plan"))
    chain_id = order.get("chainId")
    try:
        if plan.get("op") == "remove" and plan.get("burn"):
            payload = await client.call(
                "trading.lp.positions", {"chainId": chain_id, "wallets": [order.get("wallet")]}
            )
        else:
            token_id = order.get("tokenId") or plan.get("tokenId")
            if not token_id:
                return None
            payload = await client.call(
                "trading.lp.position", {"chainId": chain_id, "tokenId": str(token_id)}
            )
    except Exception as exc:  # noqa: BLE001 - the order is confirmed; the card is a bonus
        typer.echo(f"[card not refreshed: {exc}]", err=True)
        return None
    return payload if isinstance(payload, dict) and payload.get("kind") else None


def _lp_write(
    method: str,
    params: dict[str, Any],
    *,
    wait: bool,
    wait_seconds: int,
    json_output: bool,
    no_card: bool,
) -> None:
    """Create an LP write order, optionally wait for it, and print it like ``trade send``.

    With ``--json`` a *confirmed* order is followed by the refreshed position
    card (``lp position``; after a burn, the wallet's ``lp positions``) written
    to ``lp-cards/`` and announced by the marker, the last line on stdout.
    """
    session_key = os.environ.get("AGENTOS_SESSION_KEY", "").strip()
    params = {**params, "initiator": initiator_for(False)}
    if session_key:
        params["sessionKey"] = session_key

    async def _run(client):
        from agentos.cli.gateway_client import GatewayRPCError

        try:
            result = await client.call(method, params)
        except GatewayRPCError as exc:
            if exc.code not in _LP_WRITE_USAGE_CODES:
                raise
            emit_error(exc.message, json_output=json_output, code=exc.code, details=exc.data)
            raise typer.Exit(2) from exc
        order = _dict(result).get("order")
        if wait and isinstance(order, dict) and order.get("status") in _PENDING_STATUSES:
            waited = await client.call(
                "trading.orders.wait",
                {"orderId": order.get("orderId"), "timeoutSeconds": wait_seconds},
            )
            if isinstance(waited, dict) and isinstance(waited.get("order"), dict):
                result, order = waited, waited["order"]
        card = None
        if json_output and not no_card and _dict(order).get("status") == "confirmed":
            card = await _lp_card_after(client, _dict(order))
        return {"result": result, "card": card}

    out = run_gateway_sync(_run, json_output=json_output)
    result = _dict(out).get("result")
    if json_output:
        print_json(result)
        card = _dict(out).get("card")
        if card:
            _write_lp_card(card)
        return
    order = _dict(_dict(result).get("order"))
    _print_order(order)
    if order.get("status") == "awaiting_approval":
        console.print(
            f"Waiting for approval in the app (or: agentos trade approve {order.get('orderId')})."
        )


def _lp_common(params: dict[str, Any], note: str | None, client_id: str | None) -> dict[str, Any]:
    if note:
        params["note"] = note
    if client_id:
        params["clientOrderId"] = client_id.strip()
    return params


_CLIENT_ID_HELP = (
    "Idempotency key; re-running with the same id returns the same order instead of a second one"
)


@lp_app.command("collect")
def lp_collect(
    token_id: str = typer.Argument(..., help="Position NFT id"),
    chain: list[str] = typer.Option(..., "--chain", help="base or robinhood"),
    allow_empty: bool = typer.Option(
        False,
        "--allow-empty",
        help="Collect even when the position has no uncollected fees (refused otherwise)",
    ),
    note: str | None = typer.Option(None, "--note", help="Why (kept in history)"),
    client_id: str | None = typer.Option(None, "--client-id", help=_CLIENT_ID_HELP),
    wait: bool = typer.Option(False, "--wait", help="Block until the order is decided and settles"),
    wait_seconds: int = typer.Option(
        300, "--wait-seconds", help="How long --wait blocks", min=1, max=900
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    no_card: bool = typer.Option(
        False, "--no-card", help="With --json: do not write the refreshed position card"
    ),
) -> None:
    """Collect a position's uncollected fees. Always waits for your approval.

    A position with no uncollected fees is refused (it would only pay gas)
    unless --allow-empty.
    """

    params: dict[str, Any] = {
        "chainId": _lp_chain(chain, "collect", json_output=json_output),
        "tokenId": _lp_token_id(token_id, "tokenId", json_output=json_output),
    }
    if allow_empty:
        params["allowEmpty"] = True
    _lp_write(
        "trading.lp.collect",
        _lp_common(params, note, client_id),
        wait=wait,
        wait_seconds=wait_seconds,
        json_output=json_output,
        no_card=no_card,
    )


@lp_app.command("remove")
def lp_remove(
    token_id: str = typer.Argument(..., help="Position NFT id"),
    chain: list[str] = typer.Option(..., "--chain", help="base or robinhood"),
    pct: float = typer.Option(
        100.0, "--pct", help="Share of the liquidity to take out; 100 (default) burns the NFT"
    ),
    slippage: float | None = typer.Option(
        None, "--slippage", help="Slippage % below today's amounts (default 1)"
    ),
    note: str | None = typer.Option(None, "--note", help="Why (kept in history)"),
    client_id: str | None = typer.Option(None, "--client-id", help=_CLIENT_ID_HELP),
    wait: bool = typer.Option(False, "--wait", help="Block until the order is decided and settles"),
    wait_seconds: int = typer.Option(
        300, "--wait-seconds", help="How long --wait blocks", min=1, max=900
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    no_card: bool = typer.Option(
        False, "--no-card", help="With --json: do not write the refreshed position card"
    ),
) -> None:
    """Remove liquidity (fees included) from a position. Always waits for your approval."""

    if not 0 < pct <= 100:
        _bad_argument("--pct must be above 0 and at most 100", json_output=json_output)
    params: dict[str, Any] = {
        "chainId": _lp_chain(chain, "remove", json_output=json_output),
        "tokenId": _lp_token_id(token_id, "tokenId", json_output=json_output),
        "pct": pct,
    }
    if slippage is not None:
        params["slippagePct"] = slippage
    _lp_write(
        "trading.lp.remove",
        _lp_common(params, note, client_id),
        wait=wait,
        wait_seconds=wait_seconds,
        json_output=json_output,
        no_card=no_card,
    )


@lp_app.command("add")
def lp_add(
    target: str | None = typer.Argument(
        None,
        help=(
            "Token symbol or address, a TOKEN/QUOTE pair (ETH/USDC), or a V4 poolId "
            "(optional with --to-position)"
        ),
    ),
    chain: list[str] = typer.Option(..., "--chain", help="base or robinhood"),
    quote: str | None = typer.Option(None, "--quote", help=_QUOTE_HELP),
    fee: str | None = typer.Option(None, "--fee", help=_FEE_HELP),
    usd: float | None = typer.Option(
        None, "--usd", help="Deposit this many US dollars, split as the range needs"
    ),
    amount_base: str | None = typer.Option(
        None, "--amount-base", help="Deposit this much of the token (human units)"
    ),
    amount_quote: str | None = typer.Option(
        None, "--amount-quote", help="Deposit this much of the quote token (human units)"
    ),
    range_spec: str | None = typer.Option(
        None,
        "--range",
        help=(
            "mcap:2M-10M | pct:20 | above[:20] | below[:20] | full | ticks:LO:HI "
            "(default pct:20 around the price; above = all token, from just above the "
            "price up N %; below = all quote, from just below it down N %; N defaults to 20)"
        ),
    ),
    to_position: str | None = typer.Option(
        None, "--to-position", help="Add to this position (its range) instead of minting"
    ),
    wallet: str | None = typer.Option(
        None, "--wallet", help="Vault wallet address or label (default primary)"
    ),
    slippage: float | None = typer.Option(
        None, "--slippage", help="Slippage % above today's amounts (default 1)"
    ),
    note: str | None = typer.Option(None, "--note", help="Why (kept in history)"),
    client_id: str | None = typer.Option(None, "--client-id", help=_CLIENT_ID_HELP),
    wait: bool = typer.Option(False, "--wait", help="Block until the order is decided and settles"),
    wait_seconds: int = typer.Option(
        300, "--wait-seconds", help="How long --wait blocks", min=1, max=900
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    no_card: bool = typer.Option(
        False, "--no-card", help="With --json: do not write the refreshed position card"
    ),
) -> None:
    """Add liquidity: mint a position, or top one up. Always waits for your approval.

    No swap is made for you: a wallet short of a side the range needs is refused.
    A range entirely above or below the price needs only one token.
    """

    if usd is not None and (amount_base is not None or amount_quote is not None):
        _bad_argument(
            "Use --usd or --amount-base/--amount-quote, not both", json_output=json_output
        )
    if usd is None and amount_base is None and amount_quote is None:
        _bad_argument(
            "Size the deposit with --usd or --amount-base (and/or --amount-quote)",
            json_output=json_output,
        )
    if usd is not None and usd <= 0:
        _bad_argument("--usd must be above 0", json_output=json_output)
    if not target and to_position is None:
        _bad_argument("Name a token or poolId, or pass --to-position", json_output=json_output)
    if to_position is not None and range_spec:
        _bad_argument("--range cannot change an existing position's range", json_output=json_output)
    params: dict[str, Any] = {"chainId": _lp_chain(chain, "add", json_output=json_output)}
    if target:
        params["token"] = target
    _lp_pool_params(params, quote, fee)
    if usd is not None:
        params["usd"] = usd
    if amount_base is not None:
        params["amountBase"] = amount_base
    if amount_quote is not None:
        params["amountQuote"] = amount_quote
    if range_spec:
        params["range"] = range_spec
    if to_position is not None:
        params["toPosition"] = _lp_token_id(to_position, "--to-position", json_output=json_output)
    if wallet:
        params["wallet"] = wallet
    if slippage is not None:
        params["slippagePct"] = slippage
    _lp_write(
        "trading.lp.add",
        _lp_common(params, note, client_id),
        wait=wait,
        wait_seconds=wait_seconds,
        json_output=json_output,
        no_card=no_card,
    )


# ── trade markets: every pool a token trades in (docs/markets.md) ──────────

MARKETS_MIME = "application/vnd.agentos.markets+json"
#: Where ``trade markets --json`` writes its card payloads, relative to the
#: working directory (same reasoning and pruning as ``LP_CARD_DIR``).
MARKETS_CARD_DIR = "markets-cards"
MARKETS_CARDS_KEPT = 20
_MARKETS_CARD_FILE = re.compile(r"^markets-[A-Za-z0-9._-]*\.json$")
_MARKETS_SIDES = ("all", "quote", "base")


def _markets_card_name(result: dict[str, Any]) -> str:
    token = _dict(result.get("token"))
    slug = _LP_SLUG.sub("", str(token.get("symbol") or "token")).strip("-") or "token"
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"{MARKETS_CARD_DIR}/markets-{slug}-{stamp}.json"


def _write_markets_card(result: dict[str, Any]) -> None:
    """Write the card payload and announce it; the marker is the last line on stdout."""
    name = _markets_card_name(result)
    try:
        path = Path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        typer.echo(f"[card not written: {exc}]", err=True)
        return
    _prune_cards(path.parent, _MARKETS_CARD_FILE, MARKETS_CARDS_KEPT)
    print_text(f"publish_artifact path={name} mime={MARKETS_MIME}")


def _compact_usd(value: Any) -> str:
    """``$4.7M``, ``$61.2k``, ``$10k``, ``$950``; "—" when unknown."""
    if value is None or value == "":
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    size = abs(number)
    for unit, scale in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if size >= scale:
            short = f"{size / scale:.1f}".removesuffix(".0")
            return f"{'-' if number < 0 else ''}${short}{unit}"
    return _usd(number)


def _price_digits(value: Any) -> str:
    """A price with at most 6 significant digits (3 below 1), never in
    scientific notation: ``0.000471788`` -> ``0.000472``, ``240.47`` stays,
    ``1.415e-05`` -> ``0.0000142``; "—" when unknown."""
    if value is None or value == "":
        return "—"
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    if not number.is_finite():
        return str(value)
    if number.is_zero():
        return "0"
    digits = 6 if abs(number) >= 1 else 3
    places = max(0, digits - 1 - number.adjusted())
    text = f"{number.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP):f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _price_usd(value: Any) -> str:
    """:func:`_price_digits` with a dollar sign; "—" when unknown."""
    text = _price_digits(value)
    return text if text == "—" else f"${text}"


def _age(created_at: Any, now: float | None = None) -> str:
    """``2026-07-25T00:52:36Z`` -> ``73d``; "—" when unknown."""
    if not created_at:
        return "—"
    try:
        stamp = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except ValueError:
        return "—"
    seconds = max(0.0, (now if now is not None else time.time()) - stamp.timestamp())
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86_400:
        return f"{int(seconds // 3600)}h"
    days = seconds / 86_400
    if days < 60:
        return f"{int(days)}d"
    if days < 730:
        return f"{int(days // 30)}mo"
    return f"{int(days // 365)}y"


#: Characters a price keeps before it folds in a table wider than the terminal.
_MARKETS_PRICE_WIDTH = 10


def _markets_flags(row: dict[str, Any]) -> str:
    counterparty = _dict(row.get("counterparty"))
    flags = []
    if row.get("viaUniswap"):
        flags.append("uni")
    if counterparty.get("stockToken"):
        flags.append("stock")
    if counterparty.get("lookalike"):
        flags.append("lookalike")
    return " ".join(flags)


def _markets_table(title: str, rows: list[dict[str, Any]], price_in: str) -> Table:
    """One section. ``price_in`` heads the ratio column: ``IN NVDA`` on the quote
    section (the counterparty priced in the token), ``IN QUOTE`` on the base
    section (the token priced in each row's counterparty)."""
    cells: list[tuple[str, ...]] = []
    for row in rows:
        dex = _dict(row.get("dex"))
        venue = " ".join(
            str(x) for x in (dex.get("label") or dex.get("id"), dex.get("version")) if x
        )
        fee = row.get("feePct")
        if fee is not None:
            venue = f"{venue} {fee:g}%"
        cells.append(
            (
                markup_escape(str(row.get("pair") or "—")),
                markup_escape(venue or "—"),
                _compact_usd(row.get("tvlUsd")),
                _compact_usd(row.get("volume24hUsd")),
                _price_usd(row.get("priceUsd")),
                _price_digits(row.get("priceInToken")),
                _age(row.get("createdAt")),
                _markets_flags(row),
            )
        )
    headers = ("PAIR", "DEX", "TVL", "VOL 24H", "PRICE", price_in, "AGE", "FLAGS")
    natural = [max([len(h), *(len(c[i]) for c in cells)]) for i, h in enumerate(headers)]
    # Fold, never truncate: a cell that does not fit breaks onto the next line
    # instead of ending in "…". TVL, volume and age never wrap. When the table
    # is wider than the terminal (80 columns, a sub-cent price written out in
    # full), the two prices keep up to ten characters and the pair, venue and
    # flags fold first (the outer border goes, to give them two more
    # characters); only a longer price folds.
    narrow = sum(natural) + 3 * len(headers) + 1 > console.width
    table = Table(title=title, show_header=True, header_style=ACCENT_HEADER, show_edge=not narrow)
    table.add_column("PAIR", overflow="fold")
    table.add_column("DEX", overflow="fold")
    table.add_column("TVL", justify="right", no_wrap=True)
    table.add_column("VOL 24H", justify="right", no_wrap=True)
    for index in (4, 5):
        width = min(natural[index], _MARKETS_PRICE_WIDTH) if narrow else None
        table.add_column(headers[index], justify="right", overflow="fold", width=width)
    table.add_column("AGE", justify="right", no_wrap=True)
    table.add_column("FLAGS", overflow="fold")
    for cell in cells:
        table.add_row(*cell)
    return table


def _render_markets(result: dict[str, Any]) -> None:
    token = _dict(result.get("token"))
    chain = _dict(result.get("chain"))
    symbol = str(token.get("symbol") or "token")
    parts = [f"[bold]{markup_escape(symbol)}[/]"]
    if token.get("name"):
        parts.append(markup_escape(str(token["name"])))
    parts.append(markup_escape(str(chain.get("name") or "")))
    parts.append(_usd(token.get("priceUsd")))
    oracle = _dict(token.get("oracle"))
    if oracle.get("usd") is not None:
        badge = " paused" if oracle.get("paused") else (" stale" if oracle.get("stale") else "")
        parts.append(f"oracle {_usd(oracle.get('usd'))}{badge}")
        price = token.get("priceUsd")
        if isinstance(price, int | float) and float(oracle["usd"]) > 0:
            parts.append(f"premium {percent((float(price) / float(oracle['usd']) - 1) * 100)}")
    console.print(" · ".join(p for p in parts if p))
    sections = _dict(result.get("sections"))
    params = _dict(_dict(result.get("request")).get("params"))
    side = str(params.get("side") or "all")
    min_tvl = params.get("minTvlUsd")
    floor = (
        f" above {_compact_usd(min_tvl)}" if isinstance(min_tvl, int | float) and min_tvl else ""
    )
    for key, title in (("quote", f"Priced in {symbol}"), ("base", f"{symbol} priced in")):
        if side not in ("all", key):
            continue
        rows = [r for r in sections.get(key) or [] if isinstance(r, dict)]
        if rows:
            price_in = f"IN {symbol}" if key == "quote" else "IN QUOTE"
            console.print(_markets_table(f"{title} · {len(rows)}", rows, price_in))
        else:
            console.print(f"{title}: no pools against {markup_escape(symbol)}{floor}")
    counts = _dict(result.get("counts"))
    line = [f"{counts.get('shown', 0)} of {counts.get('scanned', 0)} pools shown"]
    if counts.get("belowMinTvl"):
        line.append(f"{counts['belowMinTvl']} under {_compact_usd(min_tvl)}")
    if counts.get("hiddenLookalikes"):
        line.append(f"{counts['hiddenLookalikes']} lookalikes hidden")
    if counts.get("limited"):
        line.append(f"{counts['limited']} more over the limit")
    if result.get("partial"):
        line.append("partial")
    console.print(" · ".join(line))
    for warning in result.get("warnings") or []:
        console.print(f"• {markup_escape(str(warning))}")


@app.command("markets")
def trade_markets(
    target: str = typer.Argument(..., help="Token symbol, address or ETH (NVDA, 0x…)"),
    chain: str = typer.Option(
        "robinhood",
        "--chain",
        help="base or robinhood (default robinhood: the one trade command that defaults to it)",
    ),
    side: str = typer.Option(
        "all",
        "--side",
        help="all, quote (tokens priced in this one) or base (this token priced in others)",
    ),
    min_tvl: float = typer.Option(10_000.0, "--min-tvl", help="Hide pools under this TVL (USD)"),
    limit: int = typer.Option(50, "--limit", help="Most pools to show (1-200)", min=1, max=200),
    lookalikes: bool = typer.Option(
        False, "--lookalikes", help="Show counterparties that borrow a Stock Token's name"
    ),
    deep: bool = typer.Option(False, "--deep", help="Read up to 200 pools instead of 100"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    no_card: bool = typer.Option(
        False, "--no-card", help="With --json: do not write the chat card"
    ),
) -> None:
    """Every pool a token trades in, on every DEX: tokens priced in it, and it priced in others.

    Defaults to Robinhood Chain (the only `trade` command that does), where Stock
    Tokens such as NVDA are the quote asset of many launchpad tokens.
    """

    choice = side.strip().lower()
    if choice not in _MARKETS_SIDES:
        _bad_argument(f"--side {side!r} must be all, quote or base", json_output=json_output)
    if min_tvl < 0 or min_tvl != min_tvl:
        _bad_argument("--min-tvl must be 0 or more", json_output=json_output)
    params: dict[str, Any] = {
        "target": target,
        "chainId": chain_id_from_arg(chain),
        "side": choice,
        "minTvlUsd": min_tvl,
        "limit": limit,
        "lookalikes": lookalikes,
        "deep": deep,
    }

    async def _run(client):
        from agentos.cli.gateway_client import GatewayRPCError

        try:
            return await client.call("trading.markets", params)
        except GatewayRPCError as exc:
            candidates = _dict(exc.data).get("candidates")
            if json_output or not isinstance(candidates, list) or not candidates:
                raise
            # An ambiguous symbol: the JSON envelope carries the candidates in
            # ``details``; a person gets them under the message.
            emit_error(exc.message, code=exc.code)
            for item in candidates:
                c = _dict(item)
                line = "  ".join(
                    str(x) for x in (c.get("symbol"), c.get("name"), c.get("address")) if x
                )
                typer.echo(f"  {line}", err=True)
            raise typer.Exit(rpc_error_exit_code(exc.code)) from exc

    result = _dict(run_gateway_sync(_run, json_output=json_output))
    if not json_output:
        _render_markets(result)
        return
    print_json(result)
    if not no_card and result.get("kind"):
        _write_markets_card(result)


# ── trade dca: DCA mandates (docs/dca.md) ──────────────────────────────────

DCA_MIME = "application/vnd.agentos.dca+json"
#: Where ``trade dca --json`` writes its card payloads, relative to the working
#: directory (same reasoning and pruning as ``LP_CARD_DIR``).
DCA_CARD_DIR = "dca-cards"
DCA_CARDS_KEPT = 20
DCA_MIN_EVERY_SECONDS = 60
_DCA_CARD_FILE = re.compile(r"^(mandate|mandates)-[A-Za-z0-9._-]*\.json$")
#: Gateway error codes that mean "change the input" (or the mandate's state):
#: exit 2, not 1.
_DCA_USAGE_CODES = frozenset(
    {
        "trading.dca.invalid",
        "trading.dca.bad_state",
        "trading.dca.not_found",
        "trading.invalid",
        "trading.token_not_found",
    }
)
_DCA_EVERY = re.compile(r"^(\d+(?:\.\d+)?)\s*([smhdw]?)$")
_DCA_EVERY_UNITS = {"": 1, "s": 1, "m": 60, "h": 3_600, "d": 86_400, "w": 604_800}
#: Run statuses whose order may still move after ``trading.dca.run`` returns.
_DCA_OPEN_RUNS = frozenset({"pending", "parked"})
_DCA_BAR_WIDTH = 24


class _DcaGroup(_LpGroup):
    """``trade dca``: a usage error under ``--json`` is a JSON error on stderr, exit 2."""


dca_app = typer.Typer(
    cls=_DcaGroup,
    help=(
        "DCA mandates: recurring buys the trading engine runs itself under a hard cap. "
        "From an agent, create only proposes; you approve, pause, resume, stop, buy now "
        "and edit."
    ),
)
app.add_typer(dca_app, name="dca")


def parse_every(value: str) -> int:
    """``30m``, ``2h``, ``1d``, ``1w`` or a plain number of seconds → seconds (≥ 60).

    Raises ``ValueError`` with a message fit for the user.
    """
    text = str(value or "").strip().lower()
    match = _DCA_EVERY.match(text)
    if not match:
        raise ValueError(
            f"--every {value!r} is not an interval; use 30m, 2h, 1d, 1w or a number of seconds"
        )
    seconds = float(match.group(1)) * _DCA_EVERY_UNITS[match.group(2)]
    if seconds != int(seconds):
        raise ValueError(f"--every {value!r} is not a whole number of seconds")
    if seconds < DCA_MIN_EVERY_SECONDS:
        raise ValueError(
            f"--every must be at least {DCA_MIN_EVERY_SECONDS} seconds (got {value!r})"
        )
    return int(seconds)


def _dca_every(value: str, *, json_output: bool) -> int:
    try:
        return parse_every(value)
    except ValueError as exc:
        _bad_argument(str(exc), json_output=json_output)
        raise  # unreachable: _bad_argument exits


def _dca_id(value: str, *, json_output: bool) -> str:
    text = value.strip()
    if not text:
        _bad_argument("a mandate id is required (dca_…)", json_output=json_output)
    return text


def _dca_positive(value: float | None, flag: str, *, json_output: bool, zero: bool = False) -> None:
    """``flag`` must be above 0 (or at least 0 when ``zero`` means "remove the limit")."""
    if value is None:
        return
    if value < 0 or (value == 0 and not zero) or value != value:
        need = "0 or more" if zero else "above 0"
        _bad_argument(f"{flag} must be {need}", json_output=json_output)


def _dca_card_name(result: dict[str, Any]) -> str:
    kind = str(result.get("kind") or "mandate")
    if kind == "mandates":
        params = _dict(_dict(result.get("request")).get("params"))
        slug = "all" if params.get("all") else "live"
    else:
        slug = str(_dict(result.get("mandate")).get("id") or "")
    slug = _LP_SLUG.sub("", slug).strip("-") or kind
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"{DCA_CARD_DIR}/{kind}-{slug}-{stamp}.json"


def _write_dca_card(result: dict[str, Any]) -> None:
    """Write the card payload and announce it; the marker is the last line on stdout."""
    name = _dca_card_name(result)
    try:
        path = Path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        typer.echo(f"[card not written: {exc}]", err=True)
        return
    _prune_cards(path.parent, _DCA_CARD_FILE, DCA_CARDS_KEPT)
    print_text(f"publish_artifact path={name} mime={DCA_MIME}")


def _dca_call(method: str, params: dict[str, Any], *, json_output: bool) -> Any:
    """Call one ``trading.dca.*`` method; an input or state the engine refuses exits 2."""

    async def _run(client):
        return await _dca_rpc(client, method, params, json_output=json_output)

    return run_gateway_sync(_run, json_output=json_output)


async def _dca_rpc(client: Any, method: str, params: dict[str, Any], *, json_output: bool) -> Any:
    from agentos.cli.gateway_client import GatewayRPCError

    try:
        return await client.call(method, params)
    except GatewayRPCError as exc:
        if exc.code not in _DCA_USAGE_CODES:
            raise
        emit_error(exc.message, json_output=json_output, code=exc.code, details=exc.data)
        raise typer.Exit(2) from exc


def _dca_emit(result: Any, *, json_output: bool, no_card: bool) -> None:
    """``--json``: the payload, then the card and its marker. Otherwise a panel or a table."""
    payload = _dict(result)
    if json_output:
        print_json(payload)
        if not no_card and payload.get("kind") in ("mandate", "mandates"):
            _write_dca_card(payload)
        return
    if payload.get("kind") == "mandates":
        _render_dca_list(payload)
    else:
        _render_dca_mandate(payload)


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _span(seconds: float) -> str:
    """``3 h 12 m``, ``45 m``, ``2 d 4 h``, ``30 s``."""
    seconds = int(max(0, seconds))
    days, rest = divmod(seconds, 86_400)
    hours, rest = divmod(rest, 3_600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days} d {hours} h" if hours else f"{days} d"
    if hours:
        return f"{hours} h {minutes} m" if minutes else f"{hours} h"
    if minutes:
        return f"{minutes} m"
    return f"{secs} s"


def _dca_next(mandate: dict[str, Any], now: datetime | None = None) -> str:
    """The next-buy line: ``in 3 h 12 m``, ``due now``, ``on approval``, ``paused``, ``—``."""
    status = str(mandate.get("status") or "")
    if status == "awaiting_approval":
        return "on approval" if _dict(mandate.get("schedule")).get("startNow") else "after approval"
    if status == "paused":
        return "paused"
    if status != "active":
        return "—"
    at = _parse_iso(_dict(mandate.get("schedule")).get("nextRunAt"))
    if at is None:
        return "—"
    delta = (at - (now or datetime.now(UTC))).total_seconds()
    return "due now" if delta <= 0 else f"in {_span(delta)}"


def _dca_bar(progress: Any, width: int = _DCA_BAR_WIDTH) -> str:
    try:
        share = min(1.0, max(0.0, float(progress)))
    except (TypeError, ValueError):
        share = 0.0
    filled = round(share * width)
    return "█" * filled + "░" * (width - filled)


def _dca_amount(amount: Any, symbol: str) -> str:
    """``0.0000953672 ETH``: six significant digits, never scientific notation."""
    human = _dict(amount).get("human")
    if human is None or human == "":
        return "—"
    try:
        value = Decimal(f"{Decimal(str(human)):.6g}")
    except (InvalidOperation, ValueError):
        return f"{human} {symbol}"
    if not value.is_finite():
        return f"{human} {symbol}"
    return f"{value:f} {symbol}"


def _dca_pair(mandate: dict[str, Any]) -> str:
    return f"{token_symbol(mandate.get('token'))} ← {token_symbol(mandate.get('quote'))}"


def _dca_buys(runs: dict[str, Any]) -> str:
    done, most = runs.get("done") or 0, runs.get("max")
    return f"{done} of {most} buys" if most else f"{done} buys"


def _signed_usd(value: Any) -> str:
    """``$5.45`` / ``-$1.20``; a value that rounds to zero is ``$0.00``, never ``-$0.00``."""
    if value is None or value == "":
        return "—"
    try:
        number = round(float(value), 2)
    except (TypeError, ValueError):
        return str(value)
    return money(number if number != 0 else 0.0)


# The zone run times are shown in. ``None`` is the system's local zone; tests
# pin it (``time.tzset`` does not exist on Windows, so TZ alone is not enough).
_DCA_LOCAL_TZ: tzinfo | None = None


def _dca_when(at: datetime | None, now: datetime | None = None) -> str:
    """Local ``HH:MM`` for a run today, ``Mon DD HH:MM`` otherwise, ``—`` when unknown."""
    if at is None:
        return "—"
    local = at.astimezone(_DCA_LOCAL_TZ)
    today = (now or datetime.now(UTC)).astimezone(_DCA_LOCAL_TZ).date()
    return local.strftime("%H:%M" if local.date() == today else "%b %d %H:%M")


def _dca_run_line(run: dict[str, Any], symbol: str, now: datetime | None = None) -> str:
    """``#12 · 09:01 · $10.00 → 0.0035 WETH @ $2,860.00 · tx 0x1234…abcd``.

    A skipped or failed run carries its reason; a pending or parked one says it is waiting.
    """
    status = str(run.get("status") or "")
    parts = [f"#{run.get('n')}", _dca_when(_parse_iso(run.get("at")), now)]
    if run.get("manual"):
        parts.append("buy now")
    if status == "filled":
        parts.append(
            f"{money(run.get('usd'))} → {_dca_amount(run.get('amount'), symbol)} "
            f"@ {_usd(run.get('priceUsd'))}"
        )
        if run.get("txHash"):
            parts.append(f"tx {short_address(run.get('txHash'))}")
    elif status in ("parked", "pending"):
        what = "awaiting approval" if status == "parked" else "pending, waiting to fill"
        parts.append(f"{money(run.get('usd'))} {what}")
        if run.get("orderId"):
            parts.append(str(run.get("orderId")))
    else:
        parts.append(status or "unknown")
        if run.get("reason"):
            parts.append(str(run.get("reason")))
    return " · ".join(parts)


def _render_dca_mandate(result: dict[str, Any]) -> None:
    mandate = _dict(result.get("mandate"))
    if not mandate:
        console.print("No mandate in the response.")
        return
    schedule, budget = _dict(mandate.get("schedule")), _dict(mandate.get("budget"))
    runs, acquired = _dict(mandate.get("runs")), _dict(mandate.get("acquired"))
    guards, wallet = _dict(mandate.get("guards")), _dict(mandate.get("wallet"))
    chain = _dict(mandate.get("chain"))
    symbol = token_symbol(mandate.get("token"))
    status = str(mandate.get("status") or "")
    reason = mandate.get("statusReason")
    where = (
        f"{chain.get('name') or ''} · {wallet.get('label') or short_address(wallet.get('address'))}"
    )
    lines = [
        f"[{ACCENT}]{markup_escape(_dca_pair(mandate))}[/] · {markup_escape(where)}",
        f"[bold]{money(budget.get('usdPerRun'))} {markup_escape(str(schedule.get('label') or ''))}"
        f"[/] · next buy {_dca_next(mandate)}",
        f"{_dca_bar(budget.get('progress'))} {money(budget.get('spentUsd'))} of "
        f"{money(budget.get('capUsd'))} · {float(budget.get('progress') or 0) * 100:.0f} % · "
        f"{_dca_buys(runs)}",
    ]
    if budget.get("reservedUsd"):
        lines.append(f"reserved {money(budget.get('reservedUsd'))} (open buys)")
    lines.append(
        f"acquired {_dca_amount(acquired.get('amount'), symbol)} · "
        f"avg {_usd(acquired.get('avgPriceUsd'))} vs now {_usd(acquired.get('currentPriceUsd'))} "
        f"({percent(acquired.get('vsAvgPct'))})"
    )
    lines.append(
        f"unrealised {_signed_usd(acquired.get('unrealizedUsd'))} · "
        f"gas {_usd(acquired.get('gasUsd'))}"
    )
    guard_bits = []
    if guards.get("maxPriceUsd"):
        guard_bits.append(f"only under {_usd(guards.get('maxPriceUsd'))}")
    if guards.get("buysNeedApproval"):
        guard_bits.append("each buy waits for your approval")
    if runs.get("skipped") or runs.get("failed"):
        guard_bits.append(f"{runs.get('skipped') or 0} skipped, {runs.get('failed') or 0} failed")
    if guard_bits:
        lines.append(" · ".join(guard_bits))
    history = [r for r in mandate.get("history") or [] if isinstance(r, dict)]
    if history:
        lines.append("")
        lines.append("[bold]recent runs[/]")
        lines.extend(markup_escape(_dca_run_line(r, symbol)) for r in history[:5])
    for warning in result.get("warnings") or []:
        lines.append(f"[yellow]•[/] {markup_escape(str(warning))}")
    title = f"{markup_escape(str(mandate.get('name') or 'DCA'))} · {status}"
    if reason:
        title += f" ({markup_escape(str(reason))})"
    subtitle = f"{mandate.get('id')} · as of {result.get('fetchedAt')}"
    console.print(Panel("\n".join(lines), title=title, subtitle=subtitle, expand=False))
    run = _dict(result.get("run"))
    if run:
        console.print(f"Run: {markup_escape(_dca_run_line(run, symbol))}")
    if status == "awaiting_approval":
        console.print(
            "Waiting for your approval in the app "
            f"(or: agentos trade dca approve {mandate.get('id')})."
        )


def _render_dca_list(result: dict[str, Any]) -> None:
    mandates = [m for m in result.get("mandates") or [] if isinstance(m, dict)]
    if not mandates:
        console.print("No DCA mandates yet.")
        return
    table = Table(
        title=f"DCA · {len(mandates)} mandate{'s' if len(mandates) != 1 else ''}",
        header_style=ACCENT_HEADER,
    )
    for column in ("Name", "Pair", "Cadence", "Status", "Progress", "Buys", "Next buy"):
        # Fold, never ellipsize: a clipped id or cap is worse than a taller row.
        table.add_column(
            column,
            justify="right" if column in ("Progress", "Buys") else "left",
            overflow="fold",
        )
    for mandate in mandates:
        budget, schedule = _dict(mandate.get("budget")), _dict(mandate.get("schedule"))
        runs = _dict(mandate.get("runs"))
        done, most = runs.get("done") or 0, runs.get("max")
        # The id rides under the name: every other dca command needs it.
        name = markup_escape(str(mandate.get("name") or "DCA"))
        table.add_row(
            f"{name}\n[dim]{markup_escape(str(mandate.get('id') or ''))}[/]",
            markup_escape(_dca_pair(mandate)),
            markup_escape(f"{money(budget.get('usdPerRun'))} {schedule.get('label') or ''}"),
            str(mandate.get("status") or ""),
            f"{money(budget.get('spentUsd'))} / {money(budget.get('capUsd'))}",
            f"{done}/{most}" if most else str(done),
            _dca_next(mandate),
        )
    console.print(table)
    totals = _dict(result.get("totals"))
    console.print(
        f"{money(totals.get('spentUsd'))} of {money(totals.get('capUsd'))} · "
        f"{money(totals.get('acquiredUsd'))} acquired · as of {result.get('fetchedAt')}"
    )


_JSON_HELP = "Emit machine-readable JSON (and write the card)"
_NO_CARD_HELP = "With --json: do not write the card file"


@dca_app.command("create")
def dca_create(
    token: str = typer.Argument(..., help="Token to buy: a ticker (ETH, WETH) or an address"),
    usd: float = typer.Option(..., "--usd", help="US dollars spent per buy"),
    every: str = typer.Option(
        ..., "--every", help="Interval: 30m, 2h, 1d, 1w or seconds (minimum 60)"
    ),
    cap: float | None = typer.Option(
        None, "--cap", help="Stop after spending this many US dollars in total"
    ),
    runs: int | None = typer.Option(None, "--runs", help="Stop after this many buys"),
    max_price: float | None = typer.Option(
        None, "--max-price", help="Skip a buy while the token's price is above this (USD)"
    ),
    quote: str | None = typer.Option(
        None, "--quote", help="Token spent (default the chain's USDC; required on robinhood)"
    ),
    chain: str = typer.Option("base", "--chain", help="base or robinhood"),
    wallet: str | None = typer.Option(
        None, "--wallet", help="Vault wallet address or label (default primary)"
    ),
    slippage: float | None = typer.Option(None, "--slippage", help="Slippage % per buy"),
    name: str | None = typer.Option(None, "--name", help='Mandate name (default "DCA <token>")'),
    start: str = typer.Option(
        "now", "--start", help="now: first buy at activation; next: one interval later"
    ),
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
    no_card: bool = typer.Option(False, "--no-card", help=_NO_CARD_HELP),
) -> None:
    """Create a DCA mandate. From an agent it waits for your approval; yours starts at once."""

    every_seconds = _dca_every(every, json_output=json_output)
    if cap is None and runs is None:
        _bad_argument("Pass --cap, --runs or both: a DCA needs a limit", json_output=json_output)
    _dca_positive(usd, "--usd", json_output=json_output)
    _dca_positive(cap, "--cap", json_output=json_output)
    _dca_positive(max_price, "--max-price", json_output=json_output)
    _dca_positive(slippage, "--slippage", json_output=json_output)
    if runs is not None and runs < 1:
        _bad_argument("--runs must be at least 1", json_output=json_output)
    start_key = start.strip().lower()
    if start_key not in ("now", "next"):
        _bad_argument(f"--start must be now or next (got {start!r})", json_output=json_output)
    params: dict[str, Any] = {
        "chainId": chain_id_from_arg(chain),
        "token": token,
        "usdPerRun": usd,
        "everySeconds": every_seconds,
        "startNow": start_key == "now",
        "initiator": initiator_for(False),
    }
    for key, value in (
        ("capUsd", cap),
        ("runsMax", runs),
        ("maxPriceUsd", max_price),
        ("quote", quote),
        ("wallet", wallet),
        ("slippagePct", slippage),
        ("name", name),
    ):
        if value is not None:
            params[key] = value
    session_key = os.environ.get("AGENTOS_SESSION_KEY", "").strip()
    if session_key:
        params["sessionKey"] = session_key
    result = _dca_call("trading.dca.create", params, json_output=json_output)
    _dca_emit(result, json_output=json_output, no_card=no_card)


@dca_app.command("list")
def dca_list(
    all_: bool = typer.Option(False, "--all", help="Include completed, stopped and rejected"),
    wallet: str | None = typer.Option(None, "--wallet", help="Only this wallet's mandates"),
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
    no_card: bool = typer.Option(False, "--no-card", help=_NO_CARD_HELP),
) -> None:
    """Live DCA mandates (awaiting approval, active, paused), or every one with --all."""

    params: dict[str, Any] = {}
    if all_:
        params["all"] = True
    if wallet:
        params["wallet"] = wallet
    result = _dca_call("trading.dca.list", params, json_output=json_output)
    _dca_emit(result, json_output=json_output, no_card=no_card)


def _dca_simple(
    method: str,
    mandate_id: str,
    *,
    json_output: bool,
    no_card: bool,
    reason: str | None = None,
) -> None:
    params: dict[str, Any] = {"mandateId": _dca_id(mandate_id, json_output=json_output)}
    if reason:
        params["reason"] = reason
    result = _dca_call(method, params, json_output=json_output)
    _dca_emit(result, json_output=json_output, no_card=no_card)


_ID_HELP = "Mandate id (dca_…)"


@dca_app.command("show")
def dca_show(
    mandate_id: str = typer.Argument(..., help=_ID_HELP),
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
    no_card: bool = typer.Option(False, "--no-card", help=_NO_CARD_HELP),
) -> None:
    """One mandate: schedule, progress, average buy price and recent runs."""
    _dca_simple("trading.dca.get", mandate_id, json_output=json_output, no_card=no_card)


@dca_app.command("approve")
def dca_approve(
    mandate_id: str = typer.Argument(..., help=_ID_HELP),
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
    no_card: bool = typer.Option(False, "--no-card", help=_NO_CARD_HELP),
) -> None:
    """Approve a proposed mandate and start it (yours only; an agent cannot)."""
    _dca_simple("trading.dca.approve", mandate_id, json_output=json_output, no_card=no_card)


@dca_app.command("reject")
def dca_reject(
    mandate_id: str = typer.Argument(..., help=_ID_HELP),
    reason: str | None = typer.Option(None, "--reason", help="Why (kept on the mandate)"),
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
    no_card: bool = typer.Option(False, "--no-card", help=_NO_CARD_HELP),
) -> None:
    """Reject a proposed mandate."""
    _dca_simple(
        "trading.dca.reject", mandate_id, json_output=json_output, no_card=no_card, reason=reason
    )


@dca_app.command("pause")
def dca_pause(
    mandate_id: str = typer.Argument(..., help=_ID_HELP),
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
    no_card: bool = typer.Option(False, "--no-card", help=_NO_CARD_HELP),
) -> None:
    """Pause an active mandate; no buy fires until you resume it."""
    _dca_simple("trading.dca.pause", mandate_id, json_output=json_output, no_card=no_card)


@dca_app.command("resume")
def dca_resume(
    mandate_id: str = typer.Argument(..., help=_ID_HELP),
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
    no_card: bool = typer.Option(False, "--no-card", help=_NO_CARD_HELP),
) -> None:
    """Resume a paused mandate; missed buys are not made up, the next one fires when due."""
    _dca_simple("trading.dca.resume", mandate_id, json_output=json_output, no_card=no_card)


@dca_app.command("stop")
def dca_stop(
    mandate_id: str = typer.Argument(..., help=_ID_HELP),
    reason: str | None = typer.Option(None, "--reason", help="Why (kept on the mandate)"),
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
    no_card: bool = typer.Option(False, "--no-card", help=_NO_CARD_HELP),
) -> None:
    """Stop a mandate for good; its buys still waiting for approval are rejected."""
    _dca_simple(
        "trading.dca.stop", mandate_id, json_output=json_output, no_card=no_card, reason=reason
    )


@dca_app.command("run")
def dca_run(
    mandate_id: str = typer.Argument(..., help=_ID_HELP),
    wait: bool = typer.Option(False, "--wait", help="Block until the buy is decided and settles"),
    wait_seconds: int = typer.Option(
        300, "--wait-seconds", help="How long --wait blocks", min=1, max=900
    ),
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
    no_card: bool = typer.Option(False, "--no-card", help=_NO_CARD_HELP),
) -> None:
    """Buy now: one buy on an active or paused mandate. The next scheduled buy does not move."""

    params = {"mandateId": _dca_id(mandate_id, json_output=json_output)}

    async def _run(client):
        result = _dict(await _dca_rpc(client, "trading.dca.run", params, json_output=json_output))
        run = _dict(result.get("run"))
        order_id = run.get("orderId")
        if not (wait and order_id and run.get("status") in _DCA_OPEN_RUNS):
            return result
        await client.call(
            "trading.orders.wait", {"orderId": order_id, "timeoutSeconds": wait_seconds}
        )
        fresh = _dict(await _dca_rpc(client, "trading.dca.get", params, json_output=json_output))
        if not fresh:
            return result
        history = _dict(fresh.get("mandate")).get("history") or []
        settled = next(
            (r for r in history if isinstance(r, dict) and r.get("n") == run.get("n")), run
        )
        return {**fresh, "run": settled}

    result = run_gateway_sync(_run, json_output=json_output)
    _dca_emit(result, json_output=json_output, no_card=no_card)


@dca_app.command("update")
def dca_update(
    mandate_id: str = typer.Argument(..., help=_ID_HELP),
    usd: float | None = typer.Option(None, "--usd", help="US dollars per buy"),
    cap: float | None = typer.Option(None, "--cap", help="Total US dollars to spend"),
    runs: int | None = typer.Option(None, "--runs", help="Number of buys (0 removes the limit)"),
    every: str | None = typer.Option(
        None, "--every", help="Interval: 30m, 2h, 1d, 1w or seconds; re-anchors the schedule"
    ),
    max_price: float | None = typer.Option(
        None, "--max-price", help="Skip buys above this price (USD); 0 removes the guard"
    ),
    name: str | None = typer.Option(None, "--name", help="New name"),
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
    no_card: bool = typer.Option(False, "--no-card", help=_NO_CARD_HELP),
) -> None:
    """Change a mandate's terms (yours only). Lowering the cap below what is spent completes it."""

    params: dict[str, Any] = {"mandateId": _dca_id(mandate_id, json_output=json_output)}
    _dca_positive(usd, "--usd", json_output=json_output)
    _dca_positive(cap, "--cap", json_output=json_output)
    _dca_positive(max_price, "--max-price", json_output=json_output, zero=True)
    if runs is not None and runs < 0:
        _bad_argument("--runs must be 0 or more", json_output=json_output)
    if name is not None and not name.strip():
        _bad_argument("--name must not be empty", json_output=json_output)
    for key, value in (
        ("usdPerRun", usd),
        ("capUsd", cap),
        ("runsMax", runs),
        ("everySeconds", _dca_every(every, json_output=json_output) if every else None),
        ("maxPriceUsd", max_price),
        ("name", name),
    ):
        if value is not None:
            params[key] = value
    if len(params) == 1:
        _bad_argument(
            "Nothing to update: pass --usd, --cap, --runs, --every, --max-price or --name",
            json_output=json_output,
        )
    result = _dca_call("trading.dca.update", params, json_output=json_output)
    _dca_emit(result, json_output=json_output, no_card=no_card)


# ── trade trigger: price triggers (docs/triggers.md) ───────────────────────

TRIGGER_MIME = "application/vnd.agentos.trigger+json"
#: Where ``trade trigger --json`` writes its card payloads, relative to the
#: working directory (same reasoning and pruning as ``DCA_CARD_DIR``).
TRIGGER_CARD_DIR = "trigger-cards"
TRIGGER_CARDS_KEPT = 20
#: Brackets (docs/brackets.md) share the folder, the mime and the pruning.
_TRIGGER_CARD_FILE = re.compile(r"^(trigger|triggers|bracket|brackets)-[A-Za-z0-9._-]*\.json$")
_TRIGGER_CARD_KINDS = frozenset({"trigger", "triggers", "bracket", "brackets"})
#: Gateway error codes that mean "change the input" (or the trigger's state):
#: exit 2, not 1.
_TRIGGER_USAGE_CODES = frozenset(
    {
        "trading.trigger.invalid",
        "trading.trigger.bad_state",
        "trading.trigger.not_found",
        "trading.invalid",
        "trading.token_not_found",
    }
)
#: ``3800``, ``$3,800``, ``-10%``, ``10%``, ``+15%``.
_TRIGGER_PRICE = re.compile(r"^([+-]?)(\d+(?:\.\d+)?|\.\d+)(%?)$")
_TRIGGER_AMOUNT = re.compile(r"^(\d+(?:\.\d+)?|\.\d+)$")
#: Fire statuses whose order may still move after ``trading.trigger.fire`` returns.
_TRIGGER_OPEN_FIRES = frozenset({"pending", "parked"})
_TRIGGER_GLYPHS = {"sell": "▼", "buy": "▲", "alert": "◆"}
_TRIGGER_FIRES_SHOWN = 5


class _TriggerGroup(_LpGroup):
    """``trade trigger``: a usage error under ``--json`` is a JSON error on stderr, exit 2."""


trigger_app = typer.Typer(
    cls=_TriggerGroup,
    help=(
        "Price triggers: stop-loss, take-profit, trailing stop, buy-the-dip and price "
        "alerts the trading engine watches and fires itself. From an agent, create only "
        "proposes; you approve, pause, resume, stop and fire now."
    ),
)
app.add_typer(trigger_app, name="trigger")


def parse_trigger_price(value: str, flag: str) -> str:
    """``--below``/``--above`` → the ``price`` param: an absolute USD price or a percent.

    ``3800`` (``$3,800`` too) is a price; ``-10%``/``10%`` on ``--below`` is 10 %
    under the price now and ``+15%``/``15%`` on ``--above`` 15 % over it. The
    engine resolves a percent at creation. Raises ``ValueError`` for the user.
    """
    text = str(value or "").strip().replace(",", "").replace(" ", "")
    if text.startswith("$"):
        text = text[1:]
    match = _TRIGGER_PRICE.match(text)
    if not match:
        raise ValueError(
            f"{flag} {value!r} is not a price; use a USD price (3800) or a percent of the "
            "price now (-10%, +15%)"
        )
    sign, number, pct = match.groups()
    amount = float(number)
    if not pct:
        if sign:
            raise ValueError(f"{flag} {value!r}: a USD price takes no sign")
        if amount <= 0:
            raise ValueError(f"{flag} must be above 0")
        return number
    if amount <= 0:
        raise ValueError(f"{flag} {value!r}: the percent must be above 0")
    if flag == "--below":
        if sign == "+":
            raise ValueError(f"--below {value!r} is over the price now; use --above +{number}%")
        if amount >= 100:
            raise ValueError(f"--below {value!r}: a price cannot fall 100 % or more")
        return f"-{number}%"
    if sign == "-":
        raise ValueError(f"--above {value!r} is under the price now; use --below -{number}%")
    return f"+{number}%"


def _trigger_valid_for(value: str, *, json_output: bool) -> int:
    try:
        return parse_every(value)
    except ValueError as exc:
        _bad_argument(str(exc).replace("--every", "--for"), json_output=json_output)
        raise  # unreachable: _bad_argument exits


def _trigger_id(value: str, *, json_output: bool) -> str:
    text = value.strip()
    if not text:
        _bad_argument("a trigger id is required (trg_…)", json_output=json_output)
    return text


def _trigger_flags(given: dict[str, bool], what: str, *, json_output: bool) -> str:
    """The one flag of ``given`` that is set; none or several is a usage error."""
    chosen = [flag for flag, on in given.items() if on]
    if len(chosen) != 1:
        flags = ", ".join(given)
        got = f" (got {', '.join(chosen)})" if chosen else ""
        _bad_argument(f"Pass exactly one {what}: {flags}{got}", json_output=json_output)
    return chosen[0]


def _trigger_card_name(result: dict[str, Any]) -> str:
    kind = str(result.get("kind") or "trigger")
    if kind in ("triggers", "brackets"):
        params = _dict(_dict(result.get("request")).get("params"))
        slug = "all" if params.get("all") else "live"
    elif kind == "bracket":
        slug = str(_dict(result.get("bracket")).get("id") or "")
    else:
        slug = str(_dict(result.get("trigger")).get("id") or "")
    slug = _LP_SLUG.sub("", slug).strip("-") or kind
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"{TRIGGER_CARD_DIR}/{kind}-{slug}-{stamp}.json"


def _write_trigger_card(result: dict[str, Any]) -> None:
    """Write the card payload and announce it; the marker is the last line on stdout."""
    name = _trigger_card_name(result)
    try:
        path = Path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        typer.echo(f"[card not written: {exc}]", err=True)
        return
    _prune_cards(path.parent, _TRIGGER_CARD_FILE, TRIGGER_CARDS_KEPT)
    print_text(f"publish_artifact path={name} mime={TRIGGER_MIME}")


def _trigger_call(method: str, params: dict[str, Any], *, json_output: bool) -> Any:
    """Call one ``trading.trigger.*`` method; an input or state the engine refuses exits 2."""

    async def _run(client):
        return await _trigger_rpc(client, method, params, json_output=json_output)

    return run_gateway_sync(_run, json_output=json_output)


async def _trigger_rpc(
    client: Any,
    method: str,
    params: dict[str, Any],
    *,
    json_output: bool,
    usage_codes: frozenset[str] = _TRIGGER_USAGE_CODES,
) -> Any:
    from agentos.cli.gateway_client import GatewayRPCError

    try:
        return await client.call(method, params)
    except GatewayRPCError as exc:
        if exc.code not in usage_codes:
            raise
        emit_error(exc.message, json_output=json_output, code=exc.code, details=exc.data)
        raise typer.Exit(2) from exc


def _trigger_emit(result: Any, *, json_output: bool, no_card: bool) -> None:
    """``--json``: the payload, then the card and its marker. Otherwise a panel or a table."""
    payload = _dict(result)
    kind = payload.get("kind")
    if json_output:
        print_json(payload)
        if not no_card and kind in _TRIGGER_CARD_KINDS:
            _write_trigger_card(payload)
        return
    if kind == "triggers":
        _render_trigger_list(payload)
    elif kind == "bracket":
        _render_bracket(payload)
    elif kind == "brackets":
        _render_bracket_list(payload)
    else:
        _render_trigger(payload)


def _trigger_distance(market: dict[str, Any]) -> str:
    """``−2.10 % to fall``, ``+4.00 % to rise``, ``met``, ``—``."""
    value = market.get("distancePct")
    if value is None or value == "":
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number == 0:
        return "met"
    return f"{percent(number)} to {'fall' if number < 0 else 'rise'}"


def _trigger_hits(condition: dict[str, Any]) -> str:
    return f"{condition.get('hits') or 0}/{condition.get('confirmTicks') or 2}"


def _trigger_size(action: dict[str, Any], symbol: str) -> str:
    """``50 % · ≈ $189.40``, ``0.05 ETH · ≈ $189.40``, ``$50.00``, ``—`` (alert)."""
    estimated = action.get("estimatedUsd")
    approx = f" · ≈ {_usd(estimated)}" if estimated is not None else ""
    if action.get("amountPct") is not None:
        return f"{float(action['amountPct']):g} %{approx}"
    amount = _dict(action.get("amount"))
    if amount.get("human") not in (None, ""):
        return f"{amount['human']} {symbol}{approx}"
    if action.get("amountUsd") is not None:
        return money(action.get("amountUsd"))
    return "—"


def _trigger_fire_line(fire: dict[str, Any], now: datetime | None = None) -> str:
    """``#2 · 09:01 · fire now · filled @ $3,788.00 · tx 0x1234…abcd``."""
    status = str(fire.get("status") or "")
    parts = [f"#{fire.get('n')}", _dca_when(_parse_iso(fire.get("at")), now)]
    if fire.get("manual"):
        parts.append("fire now")
    price = fire.get("priceUsd")
    if status in ("filled", "alerted", "pending") and price is not None:
        parts.append(f"{status} @ {_usd(price)}")
    elif status == "parked":
        parts.append("parked, awaiting approval")
    else:
        parts.append(status or "unknown")
    if status in ("skipped", "failed", "expired", "rejected") and fire.get("reason"):
        parts.append(str(fire.get("reason")))
    if fire.get("txHash"):
        parts.append(f"tx {short_address(fire.get('txHash'))}")
    elif status in _TRIGGER_OPEN_FIRES and fire.get("orderId"):
        parts.append(str(fire.get("orderId")))
    return " · ".join(parts)


def _trigger_result_line(trigger: dict[str, Any]) -> str | None:
    result = _dict(trigger.get("result"))
    if not result:
        return None
    token, quote = token_symbol(trigger.get("token")), token_symbol(trigger.get("quote"))
    spent, got = (quote, token) if trigger.get("kind") == "buy" else (token, quote)
    parts = [
        f"{_dca_amount(result.get('amountIn'), spent)} → "
        f"{_dca_amount(result.get('amountOut'), got)} @ {_usd(result.get('priceUsd'))}"
    ]
    if result.get("gasUsd") is not None:
        parts.append(f"gas {_usd(result.get('gasUsd'))}")
    if result.get("txHash"):
        parts.append(f"tx {short_address(result.get('txHash'))}")
    elif result.get("orderId"):
        parts.append(str(result.get("orderId")))
    return " · ".join(parts)


def _render_trigger(result: dict[str, Any]) -> None:
    trigger = _dict(result.get("trigger"))
    if not trigger:
        console.print("No trigger in the response.")
        return
    condition, action = _dict(trigger.get("condition")), _dict(trigger.get("action"))
    market, wallet = _dict(trigger.get("market")), _dict(trigger.get("wallet"))
    chain = _dict(trigger.get("chain"))
    kind = str(trigger.get("kind") or "")
    symbol = token_symbol(trigger.get("token"))
    status = str(trigger.get("status") or "")
    where = (
        f"{chain.get('name') or ''} · {wallet.get('label') or short_address(wallet.get('address'))}"
    )
    lines = [
        f"[bold]{markup_escape(str(action.get('label') or kind))} when "
        f"{markup_escape(str(condition.get('label') or ''))}[/]",
        f"[{ACCENT}]{markup_escape(symbol)}[/] {_usd(market.get('priceUsd'))} now · "
        f"{_trigger_distance(market)} · checks {_trigger_hits(condition)} · "
        f"{markup_escape(where)}",
    ]
    if condition.get("direction") == "trail":
        lines.append(
            f"peak {_usd(condition.get('peakPriceUsd'))} · "
            f"stop {_usd(condition.get('stopPriceUsd'))}"
        )
    if kind != "alert":
        approval = (
            f"waits for you · over {money(action.get('approvalThresholdUsd'))}"
            if action.get("needsApproval")
            else "automatic"
        )
        balance = _dict(market.get("balance"))
        held = token_symbol(trigger.get("quote") if kind == "buy" else trigger.get("token"))
        lines.append(
            f"size {markup_escape(_trigger_size(action, symbol))} · "
            f"balance {_dca_amount(balance, held)} · approval {approval}"
        )
    valid_until = _parse_iso(trigger.get("validUntil"))
    lines.append(
        "valid until " + (valid_until.strftime("%Y-%m-%d %H:%M UTC") if valid_until else "GTC")
    )
    outcome = _trigger_result_line(trigger)
    if outcome:
        lines.append(f"result {markup_escape(outcome)}")
    fires = [f for f in trigger.get("fires") or [] if isinstance(f, dict)]
    if fires:
        lines.append("")
        lines.append("[bold]recent fires[/]")
        lines.extend(markup_escape(_trigger_fire_line(f)) for f in fires[:_TRIGGER_FIRES_SHOWN])
    for warning in result.get("warnings") or []:
        lines.append(f"[yellow]•[/] {markup_escape(str(warning))}")
    glyph = _TRIGGER_GLYPHS.get(kind, "•")
    title = f"{glyph} {markup_escape(str(trigger.get('name') or 'Trigger'))} · {status}"
    reason = trigger.get("statusReason")
    if reason:
        title += f" ({markup_escape(str(reason))})"
    subtitle = f"{trigger.get('id')} · as of {result.get('fetchedAt')}"
    console.print(Panel("\n".join(lines), title=title, subtitle=subtitle, expand=False))
    fire = _dict(result.get("fire"))
    if fire:
        console.print(f"Fire: {markup_escape(_trigger_fire_line(fire))}")
    if status == "awaiting_approval":
        console.print(
            "Waiting for your approval in the app "
            f"(or: agentos trade trigger approve {trigger.get('id')})."
        )


def _render_trigger_list(result: dict[str, Any]) -> None:
    triggers = [t for t in result.get("triggers") or [] if isinstance(t, dict)]
    if not triggers:
        console.print("No triggers yet.")
        return
    table = Table(
        title=f"Triggers · {len(triggers)}",
        header_style=ACCENT_HEADER,
    )
    for column in ("Name", "Kind", "Status", "Condition", "Price now", "Distance", "Wallet"):
        table.add_column(
            column,
            justify="right" if column in ("Price now", "Distance") else "left",
            overflow="fold",
        )
    for trigger in triggers:
        condition, market = _dict(trigger.get("condition")), _dict(trigger.get("market"))
        wallet = _dict(trigger.get("wallet"))
        kind = str(trigger.get("kind") or "")
        # The id rides under the name: every other trigger command needs it.
        name = markup_escape(str(trigger.get("name") or "Trigger"))
        table.add_row(
            f"{name}\n[dim]{markup_escape(str(trigger.get('id') or ''))}[/]",
            f"{_TRIGGER_GLYPHS.get(kind, '•')} {kind}",
            str(trigger.get("status") or ""),
            markup_escape(
                f"{token_symbol(trigger.get('token'))} {condition.get('label') or ''}".strip()
            ),
            _usd(market.get("priceUsd")),
            _trigger_distance(market),
            markup_escape(str(wallet.get("label") or short_address(wallet.get("address")))),
        )
    console.print(table)
    totals = _dict(result.get("totals"))
    console.print(
        f"{totals.get('armed') or 0} armed · {totals.get('awaiting') or 0} awaiting · "
        f"{totals.get('triggered') or 0} triggered · as of {result.get('fetchedAt')}"
    )


_TRIGGER_ID_HELP = "Trigger id (trg_…)"


@trigger_app.command("create")
def trigger_create(
    token: str = typer.Argument(
        ..., help="Token watched and traded: a ticker (ETH, WETH) or an address"
    ),
    below: str | None = typer.Option(
        None,
        "--below",
        help="Fire at or under this USD price (3800) or this far under the price now (-10%)",
    ),
    above: str | None = typer.Option(
        None,
        "--above",
        help="Fire at or over this USD price (5000) or this far over the price now (+15%)",
    ),
    trail: float | None = typer.Option(
        None, "--trail", help="Fire when the price falls this % from its peak since arming"
    ),
    sell: bool = typer.Option(False, "--sell", help="Sell the token for the quote when it fires"),
    buy: bool = typer.Option(False, "--buy", help="Buy the token with the quote when it fires"),
    alert: bool = typer.Option(False, "--alert", help="Only notify you when it fires"),
    pct: float | None = typer.Option(
        None, "--pct", help="--sell: percent of the wallet's balance at fire time"
    ),
    amount: str | None = typer.Option(None, "--amount", help="--sell: a fixed token amount (0.05)"),
    usd: float | None = typer.Option(
        None, "--usd", help="--buy: US dollars to spend; --sell: US dollars' worth to sell"
    ),
    quote: str | None = typer.Option(
        None,
        "--quote",
        help=(
            "Counter token (default the chain's USDC, or the native coin when the token "
            "is USDC; required on robinhood for --sell/--buy)"
        ),
    ),
    chain: str = typer.Option("base", "--chain", help="base or robinhood"),
    wallet: str | None = typer.Option(
        None, "--wallet", help="Vault wallet address or label (default primary)"
    ),
    slippage: float | None = typer.Option(None, "--slippage", help="Slippage % for the order"),
    name: str | None = typer.Option(
        None, "--name", help='Trigger name (default e.g. "Stop-loss ETH")'
    ),
    valid_for: str | None = typer.Option(
        None, "--for", help="Expire if not reached within 30m, 2h, 1d, 1w or seconds (default GTC)"
    ),
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
    no_card: bool = typer.Option(False, "--no-card", help=_NO_CARD_HELP),
) -> None:
    """Create a price trigger. From an agent it waits for your approval; yours arms at once.

    Exactly one condition (--below, --above or --trail) and one action (--sell
    with one of --pct/--amount/--usd, --buy with --usd, or --alert).
    """

    condition = _trigger_flags(
        {"--below": below is not None, "--above": above is not None, "--trail": trail is not None},
        "condition",
        json_output=json_output,
    )
    action = _trigger_flags(
        {"--sell": sell, "--buy": buy, "--alert": alert}, "action", json_output=json_output
    )
    sizes = [
        flag
        for flag, value in (("--pct", pct), ("--amount", amount), ("--usd", usd))
        if value is not None
    ]
    if action == "--sell" and len(sizes) != 1:
        got = f" (got {', '.join(sizes)})" if sizes else ""
        _bad_argument(
            f"--sell takes exactly one size: --pct, --amount or --usd{got}",
            json_output=json_output,
        )
    if action == "--buy" and sizes != ["--usd"]:
        _bad_argument(
            "--buy takes --usd (the US dollars to spend) and no other size", json_output=json_output
        )
    if action == "--alert" and sizes:
        _bad_argument(
            f"--alert takes no size (it sends no order; got {', '.join(sizes)})",
            json_output=json_output,
        )
    if action == "--buy" and condition == "--trail":
        _bad_argument("--trail works with --sell or --alert, not --buy", json_output=json_output)
    params: dict[str, Any] = {
        "chainId": chain_id_from_arg(chain),
        "kind": action.lstrip("-"),
        "token": token,
        "direction": condition.lstrip("-"),
        "initiator": initiator_for(False),
    }
    try:
        if below is not None:
            params["price"] = parse_trigger_price(below, "--below")
        elif above is not None:
            params["price"] = parse_trigger_price(above, "--above")
    except ValueError as exc:
        _bad_argument(str(exc), json_output=json_output)
    if trail is not None:
        if not 0 < trail < 100:
            _bad_argument(
                "--trail must be above 0 and under 100 (a percent)", json_output=json_output
            )
        params["trailPct"] = trail
    if pct is not None:
        if not 0 < pct <= 100:
            _bad_argument("--pct must be above 0 and at most 100", json_output=json_output)
        params["amountPct"] = pct
    if amount is not None:
        text = amount.strip()
        if not _TRIGGER_AMOUNT.match(text) or float(text) <= 0:
            _bad_argument(
                f"--amount {amount!r} must be a token amount above 0 (0.05)",
                json_output=json_output,
            )
        params["amount"] = text
    _dca_positive(usd, "--usd", json_output=json_output)
    _dca_positive(slippage, "--slippage", json_output=json_output)
    if usd is not None:
        params["amountUsd"] = usd
    if valid_for is not None:
        params["validForSeconds"] = _trigger_valid_for(valid_for, json_output=json_output)
    for key, value in (
        ("quote", quote),
        ("wallet", wallet),
        ("slippagePct", slippage),
        ("name", name),
    ):
        if value is not None:
            params[key] = value
    session_key = os.environ.get("AGENTOS_SESSION_KEY", "").strip()
    if session_key:
        params["sessionKey"] = session_key
    result = _trigger_call("trading.trigger.create", params, json_output=json_output)
    _trigger_emit(result, json_output=json_output, no_card=no_card)


@trigger_app.command("list")
def trigger_list(
    all_: bool = typer.Option(False, "--all", help="Include done, stopped, rejected and expired"),
    wallet: str | None = typer.Option(None, "--wallet", help="Only this wallet's triggers"),
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
    no_card: bool = typer.Option(False, "--no-card", help=_NO_CARD_HELP),
) -> None:
    """Live triggers (awaiting approval, armed, triggered, paused), or every one with --all."""

    params: dict[str, Any] = {}
    if all_:
        params["all"] = True
    if wallet:
        params["wallet"] = wallet
    result = _trigger_call("trading.trigger.list", params, json_output=json_output)
    _trigger_emit(result, json_output=json_output, no_card=no_card)


def _trigger_simple(
    method: str,
    trigger_id: str,
    *,
    json_output: bool,
    no_card: bool,
    reason: str | None = None,
) -> None:
    params: dict[str, Any] = {"triggerId": _trigger_id(trigger_id, json_output=json_output)}
    if reason:
        params["reason"] = reason
    result = _trigger_call(method, params, json_output=json_output)
    _trigger_emit(result, json_output=json_output, no_card=no_card)


@trigger_app.command("show")
def trigger_show(
    trigger_id: str = typer.Argument(..., help=_TRIGGER_ID_HELP),
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
    no_card: bool = typer.Option(False, "--no-card", help=_NO_CARD_HELP),
) -> None:
    """One trigger: condition, price now and distance, size, checks and recent fires."""
    _trigger_simple("trading.trigger.get", trigger_id, json_output=json_output, no_card=no_card)


@trigger_app.command("approve")
def trigger_approve(
    trigger_id: str = typer.Argument(..., help=_TRIGGER_ID_HELP),
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
    no_card: bool = typer.Option(False, "--no-card", help=_NO_CARD_HELP),
) -> None:
    """Approve a proposed trigger and arm it (yours only; an agent cannot)."""
    _trigger_simple("trading.trigger.approve", trigger_id, json_output=json_output, no_card=no_card)


@trigger_app.command("reject")
def trigger_reject(
    trigger_id: str = typer.Argument(..., help=_TRIGGER_ID_HELP),
    reason: str | None = typer.Option(None, "--reason", help="Why (kept on the trigger)"),
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
    no_card: bool = typer.Option(False, "--no-card", help=_NO_CARD_HELP),
) -> None:
    """Reject a proposed trigger."""
    _trigger_simple(
        "trading.trigger.reject",
        trigger_id,
        json_output=json_output,
        no_card=no_card,
        reason=reason,
    )


@trigger_app.command("pause")
def trigger_pause(
    trigger_id: str = typer.Argument(..., help=_TRIGGER_ID_HELP),
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
    no_card: bool = typer.Option(False, "--no-card", help=_NO_CARD_HELP),
) -> None:
    """Pause an armed trigger; it is not checked until you resume it."""
    _trigger_simple("trading.trigger.pause", trigger_id, json_output=json_output, no_card=no_card)


@trigger_app.command("resume")
def trigger_resume(
    trigger_id: str = typer.Argument(..., help=_TRIGGER_ID_HELP),
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
    no_card: bool = typer.Option(False, "--no-card", help=_NO_CARD_HELP),
) -> None:
    """Re-arm a paused trigger; checks start over (a trailing stop's peak is the price now)."""
    _trigger_simple("trading.trigger.resume", trigger_id, json_output=json_output, no_card=no_card)


@trigger_app.command("stop")
def trigger_stop(
    trigger_id: str = typer.Argument(..., help=_TRIGGER_ID_HELP),
    reason: str | None = typer.Option(None, "--reason", help="Why (kept on the trigger)"),
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
    no_card: bool = typer.Option(False, "--no-card", help=_NO_CARD_HELP),
) -> None:
    """Stop a trigger for good; its order still waiting for approval is rejected."""
    _trigger_simple(
        "trading.trigger.stop",
        trigger_id,
        json_output=json_output,
        no_card=no_card,
        reason=reason,
    )


@trigger_app.command("fire")
def trigger_fire(
    trigger_id: str = typer.Argument(..., help=_TRIGGER_ID_HELP),
    wait: bool = typer.Option(False, "--wait", help="Block until the order is decided and settles"),
    wait_seconds: int = typer.Option(
        300, "--wait-seconds", help="How long --wait blocks", min=1, max=900
    ),
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
    no_card: bool = typer.Option(False, "--no-card", help=_NO_CARD_HELP),
) -> None:
    """Fire now: act at once on an armed or paused trigger, whatever the price."""

    params = {"triggerId": _trigger_id(trigger_id, json_output=json_output)}

    async def _run(client):
        result = _dict(
            await _trigger_rpc(client, "trading.trigger.fire", params, json_output=json_output)
        )
        fire = _dict(result.get("fire"))
        order_id = fire.get("orderId")
        if not (wait and order_id and fire.get("status") in _TRIGGER_OPEN_FIRES):
            return result
        await client.call(
            "trading.orders.wait", {"orderId": order_id, "timeoutSeconds": wait_seconds}
        )
        fresh = _dict(
            await _trigger_rpc(client, "trading.trigger.get", params, json_output=json_output)
        )
        if not fresh:
            return result
        fires = _dict(fresh.get("trigger")).get("fires") or []
        settled = next(
            (f for f in fires if isinstance(f, dict) and f.get("n") == fire.get("n")), fire
        )
        return {**fresh, "fire": settled}

    result = run_gateway_sync(_run, json_output=json_output)
    _trigger_emit(result, json_output=json_output, no_card=no_card)


# ── trade protect / trade bracket: take-profit + stop-loss (docs/brackets.md) ─

#: Gateway error codes that mean "change the input" (or the bracket's state): exit 2.
_BRACKET_USAGE_CODES = frozenset(
    {
        "trading.bracket.invalid",
        "trading.bracket.bad_state",
        "trading.bracket.not_found",
        "trading.invalid",
        "trading.token_not_found",
    }
)
_BRACKET_GLYPHS = {"sell": "▼▲", "alert": "◆"}
_BRACKET_LEG_WORDS = {"tp": "take-profit", "sl": "stop-loss"}
_BRACKET_ID_HELP = "Bracket id (brk_…)"

bracket_app = typer.Typer(
    cls=_TriggerGroup,
    help=(
        "Brackets: a take-profit and a stop-loss on one position, one cancelling the "
        "other. Create one with `agentos trade protect`. From an agent, create only "
        "proposes; you approve, pause, resume, stop and fire now."
    ),
)
app.add_typer(bracket_app, name="bracket")


def _bracket_line_arg(value: str, flag: str, *, json_output: bool) -> str:
    """``--tp`` reads like ``--above``, ``--sl`` like ``--below`` (``parse_trigger_price``)."""
    rule = "--above" if flag == "--tp" else "--below"
    try:
        return parse_trigger_price(value, rule)
    except ValueError as exc:
        message = str(exc).replace("--below", "--sl").replace("--above", "--tp")
        _bad_argument(message, json_output=json_output)
        raise  # unreachable: _bad_argument exits


def _bracket_id(value: str, *, json_output: bool) -> str:
    text = value.strip()
    if not text:
        _bad_argument("a bracket id is required (brk_…)", json_output=json_output)
    return text


def _bracket_call(method: str, params: dict[str, Any], *, json_output: bool) -> Any:
    """Call one ``trading.bracket.*`` method; an input or state the engine refuses exits 2."""

    async def _run(client):
        return await _trigger_rpc(
            client, method, params, json_output=json_output, usage_codes=_BRACKET_USAGE_CODES
        )

    return run_gateway_sync(_run, json_output=json_output)


def _bracket_leg_word(leg: str, trigger: dict[str, Any]) -> str:
    if leg == "sl" and _dict(trigger.get("condition")).get("direction") == "trail":
        return "trailing stop"
    return _BRACKET_LEG_WORDS.get(leg, leg)


def _bracket_size(action: dict[str, Any], symbol: str) -> str:
    """``100 % · ≈ $189.40``; ``50 % at take-profit, 100 % at stop · ≈ $189.40``."""
    tp_pct = action.get("tpPct")
    if tp_pct is not None and action.get("amountPct") is not None:
        estimated = action.get("estimatedUsd")
        approx = f" · ≈ {_usd(estimated)}" if estimated is not None else ""
        return (
            f"{float(tp_pct):g} % at take-profit, {float(action['amountPct']):g} % at stop{approx}"
        )
    return _trigger_size(action, symbol)


def _bracket_up_down(market: dict[str, Any]) -> str:
    """``+20.30% / -9.80%``: the move to the take-profit and to the stop."""
    return f"{percent(market.get('upsidePct'))} / {percent(market.get('downsidePct'))}"


def _bracket_reward_risk(market: dict[str, Any]) -> str:
    value = market.get("rewardRisk")
    if value is None or value == "":
        return "—"
    try:
        return f"{float(value):g} : 1"
    except (TypeError, ValueError):
        return str(value)


def _bracket_fires(bracket: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Both legs' fires, newest first, each with its leg word."""
    fires: list[tuple[str, dict[str, Any]]] = []
    for leg, key in (("tp", "takeProfit"), ("sl", "stopLoss")):
        trigger = _dict(bracket.get(key))
        word = _bracket_leg_word(leg, trigger)
        fires.extend((word, f) for f in trigger.get("fires") or [] if isinstance(f, dict))
    fires.sort(key=lambda item: str(item[1].get("at") or ""), reverse=True)
    return fires


def _render_bracket(result: dict[str, Any]) -> None:
    bracket = _dict(result.get("bracket"))
    if not bracket:
        console.print("No bracket in the response.")
        return
    lines_, action = _dict(bracket.get("lines")), _dict(bracket.get("action"))
    market, wallet = _dict(bracket.get("market")), _dict(bracket.get("wallet"))
    chain = _dict(bracket.get("chain"))
    kind = str(bracket.get("kind") or "")
    symbol = token_symbol(bracket.get("token"))
    status = str(bracket.get("status") or "")
    tp_label = str(lines_.get("takeProfitLabel") or "")
    sl_label = str(lines_.get("stopLossLabel") or "")
    if kind == "alert":
        headline = f"notify when {symbol} is {tp_label} or {sl_label}"
    else:
        headline = f"{action.get('label') or 'sell'} · take-profit {tp_label} · stop {sl_label}"
    where = (
        f"{chain.get('name') or ''} · {wallet.get('label') or short_address(wallet.get('address'))}"
    )
    stop_text = f"stop {_usd(lines_.get('stopLossUsd'))}"
    if lines_.get("trailPct") is not None:
        stop_text += f" ({sl_label})"
    lines = [
        f"[bold]{markup_escape(headline)}[/]",
        f"{markup_escape(stop_text)}  ◂  [{ACCENT}]{markup_escape(symbol)}[/] "
        f"{_usd(market.get('priceUsd'))} now  ▸  take-profit "
        f"{_usd(lines_.get('takeProfitUsd'))}",
        f"upside {percent(market.get('upsidePct'))} · downside "
        f"{percent(market.get('downsidePct'))} · reward:risk {_bracket_reward_risk(market)} · "
        f"{markup_escape(where)}",
    ]
    if kind != "alert":
        approval = (
            f"waits for you · over {money(action.get('approvalThresholdUsd'))}"
            if action.get("needsApproval")
            else "automatic"
        )
        balance = _dict(market.get("balance"))
        lines.append(
            f"size {markup_escape(_bracket_size(action, symbol))} · "
            f"balance {_dca_amount(balance, symbol)} · approval {approval}"
        )
    valid_until = _parse_iso(bracket.get("validUntil"))
    lines.append(
        "valid until " + (valid_until.strftime("%Y-%m-%d %H:%M UTC") if valid_until else "GTC")
    )
    lines.append("")
    for leg, key in (("tp", "takeProfit"), ("sl", "stopLoss")):
        trigger = _dict(bracket.get(key))
        condition = _dict(trigger.get("condition"))
        state = str(trigger.get("status") or "")
        if trigger.get("statusReason"):
            state += f" · {trigger.get('statusReason')}"
        lines.append(
            f"{markup_escape(_bracket_leg_word(leg, trigger)):<13} "
            f"{markup_escape(str(condition.get('label') or ''))} · {markup_escape(state)} · "
            f"checks {_trigger_hits(condition)}"
        )
    outcome = _trigger_result_line({**bracket, "kind": "sell"})
    if outcome:
        fired = _BRACKET_LEG_WORDS.get(str(bracket.get("fired") or ""), "")
        lines.append(f"result{f' ({fired})' if fired else ''} {markup_escape(outcome)}")
    fires = _bracket_fires(bracket)
    if fires:
        lines.append("")
        lines.append("[bold]recent fires[/]")
        lines.extend(
            markup_escape(f"{word} {_trigger_fire_line(fire)}")
            for word, fire in fires[:_TRIGGER_FIRES_SHOWN]
        )
    for warning in result.get("warnings") or []:
        lines.append(f"[yellow]•[/] {markup_escape(str(warning))}")
    glyph = _BRACKET_GLYPHS.get(kind, "•")
    title = f"{glyph} {markup_escape(str(bracket.get('name') or 'Bracket'))} · {status}"
    reason = bracket.get("statusReason")
    if reason:
        title += f" ({markup_escape(str(reason))})"
    subtitle = f"{bracket.get('id')} · as of {result.get('fetchedAt')}"
    console.print(Panel("\n".join(lines), title=title, subtitle=subtitle, expand=False))
    fire = _dict(result.get("fire"))
    if fire:
        console.print(f"Fire: {markup_escape(_trigger_fire_line(fire))}")
    if status == "awaiting_approval":
        console.print(
            "Waiting for your approval in the app; one approval arms both legs "
            f"(or: agentos trade bracket approve {bracket.get('id')})."
        )


def _render_bracket_list(result: dict[str, Any]) -> None:
    brackets = [b for b in result.get("brackets") or [] if isinstance(b, dict)]
    if not brackets:
        console.print("No brackets yet.")
        return
    table = Table(title=f"Brackets · {len(brackets)}", header_style=ACCENT_HEADER)
    columns = ("Name", "Status", "Token", "Range", "Price now", "Up / down", "Size")
    for column in columns:
        table.add_column(
            column,
            justify="right" if column in ("Price now", "Up / down") else "left",
            overflow="fold",
        )
    for bracket in brackets:
        lines_, market = _dict(bracket.get("lines")), _dict(bracket.get("market"))
        action = _dict(bracket.get("action"))
        kind = str(bracket.get("kind") or "")
        symbol = token_symbol(bracket.get("token"))
        # The id rides under the name: every other bracket command needs it.
        name = markup_escape(str(bracket.get("name") or "Bracket"))
        size = "notify" if kind == "alert" else _bracket_size(action, symbol)
        table.add_row(
            f"{name}\n[dim]{markup_escape(str(bracket.get('id') or ''))}[/]",
            f"{_BRACKET_GLYPHS.get(kind, '•')} {bracket.get('status') or ''}",
            markup_escape(symbol),
            f"{_usd(lines_.get('stopLossUsd'))} – {_usd(lines_.get('takeProfitUsd'))}",
            _usd(market.get("priceUsd")),
            _bracket_up_down(market),
            markup_escape(size),
        )
    console.print(table)
    totals = _dict(result.get("totals"))
    console.print(
        f"{totals.get('armed') or 0} armed · {totals.get('awaiting') or 0} awaiting · "
        f"{totals.get('triggered') or 0} triggered · as of {result.get('fetchedAt')}"
    )


@app.command("protect")
def trade_protect(
    token: str = typer.Argument(
        ..., help="Token held and protected: a ticker (ETH, WETH) or an address"
    ),
    tp: str | None = typer.Option(
        None,
        "--tp",
        help="Take-profit: at or over this USD price (4560) or this far over the price now (+20%)",
    ),
    sl: str | None = typer.Option(
        None,
        "--sl",
        help="Stop-loss: at or under this USD price (3420) or this far under the price now (-10%)",
    ),
    trail: float | None = typer.Option(
        None,
        "--trail",
        help="Trailing stop instead of --sl: sell when the price falls this % from its peak",
    ),
    pct: float | None = typer.Option(
        None, "--pct", help="Percent of the wallet's balance both legs sell (default 100)"
    ),
    amount: str | None = typer.Option(None, "--amount", help="A fixed token amount (0.05)"),
    usd: float | None = typer.Option(None, "--usd", help="US dollars' worth to sell"),
    tp_pct: float | None = typer.Option(
        None,
        "--tp-pct",
        help="Partial take-profit: that leg sells only this % (at most --pct); the stop sells all",
    ),
    alert: bool = typer.Option(
        False, "--alert", help="A range alert: only notify you when the price leaves the range"
    ),
    quote: str | None = typer.Option(
        None,
        "--quote",
        help=(
            "Counter token (default the chain's USDC, or the native coin when the token "
            "is USDC; required on robinhood unless --alert)"
        ),
    ),
    chain: str = typer.Option("base", "--chain", help="base or robinhood"),
    wallet: str | None = typer.Option(
        None, "--wallet", help="Vault wallet address or label (default primary)"
    ),
    slippage: float | None = typer.Option(None, "--slippage", help="Slippage % for the order"),
    name: str | None = typer.Option(
        None, "--name", help='Bracket name (default e.g. "Protect ETH")'
    ),
    valid_for: str | None = typer.Option(
        None, "--for", help="Expire if not fired within 30m, 2h, 1d, 1w or seconds (default GTC)"
    ),
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
    no_card: bool = typer.Option(False, "--no-card", help=_NO_CARD_HELP),
) -> None:
    """Protect a position: a take-profit and a stop-loss, whichever comes first cancels the other.

    --tp and exactly one of --sl / --trail; at most one of --pct / --amount /
    --usd (default --pct 100). From an agent it waits for your approval (one
    approval arms both legs); yours arms at once.
    """

    if tp is None:
        _bad_argument(
            "--tp is required: the take-profit line (4560, or +20% over the price now)",
            json_output=json_output,
        )
    stop = _trigger_flags(
        {"--sl": sl is not None, "--trail": trail is not None}, "stop line", json_output=json_output
    )
    sizes = [
        flag
        for flag, value in (("--pct", pct), ("--amount", amount), ("--usd", usd))
        if value is not None
    ]
    if alert and (sizes or tp_pct is not None):
        given = [*sizes, *(["--tp-pct"] if tp_pct is not None else [])]
        _bad_argument(
            f"--alert takes no size (it sends no order; got {', '.join(given)})",
            json_output=json_output,
        )
    if len(sizes) > 1:
        _bad_argument(
            f"Pass at most one size: --pct, --amount or --usd (got {', '.join(sizes)})",
            json_output=json_output,
        )
    if tp_pct is not None and sizes and sizes[0] != "--pct":
        _bad_argument(
            f"--tp-pct works with --pct (or the default 100 %), not {sizes[0]}",
            json_output=json_output,
        )
    assert tp is not None
    params: dict[str, Any] = {
        "chainId": chain_id_from_arg(chain),
        "kind": "alert" if alert else "sell",
        "token": token,
        "takeProfit": _bracket_line_arg(tp, "--tp", json_output=json_output),
        "initiator": initiator_for(False),
    }
    if stop == "--sl":
        assert sl is not None
        params["stopLoss"] = _bracket_line_arg(sl, "--sl", json_output=json_output)
        take, floor = params["takeProfit"], params["stopLoss"]
        if "%" not in take and "%" not in floor and float(take) <= float(floor):
            _bad_argument(
                f"--tp {tp} must be above --sl {sl}: the take-profit is the upper line",
                json_output=json_output,
            )
    else:
        assert trail is not None
        if not 0 < trail < 100:
            _bad_argument(
                "--trail must be above 0 and under 100 (a percent)", json_output=json_output
            )
        params["trailPct"] = trail
    if pct is not None and not 0 < pct <= 100:
        _bad_argument("--pct must be above 0 and at most 100", json_output=json_output)
    if not alert and not sizes:
        pct = 100.0
    if pct is not None:
        params["amountPct"] = pct
    if tp_pct is not None:
        assert pct is not None
        if not 0 < tp_pct <= pct:
            _bad_argument(
                f"--tp-pct must be above 0 and at most --pct ({pct:g})", json_output=json_output
            )
        params["tpPct"] = tp_pct
    if amount is not None:
        text = amount.strip()
        if not _TRIGGER_AMOUNT.match(text) or float(text) <= 0:
            _bad_argument(
                f"--amount {amount!r} must be a token amount above 0 (0.05)",
                json_output=json_output,
            )
        params["amount"] = text
    _dca_positive(usd, "--usd", json_output=json_output)
    _dca_positive(slippage, "--slippage", json_output=json_output)
    if usd is not None:
        params["amountUsd"] = usd
    if valid_for is not None:
        params["validForSeconds"] = _trigger_valid_for(valid_for, json_output=json_output)
    for key, value in (
        ("quote", quote),
        ("wallet", wallet),
        ("slippagePct", slippage),
        ("name", name),
    ):
        if value is not None:
            params[key] = value
    session_key = os.environ.get("AGENTOS_SESSION_KEY", "").strip()
    if session_key:
        params["sessionKey"] = session_key
    result = _bracket_call("trading.bracket.create", params, json_output=json_output)
    _trigger_emit(result, json_output=json_output, no_card=no_card)


@bracket_app.command("list")
def bracket_list(
    all_: bool = typer.Option(False, "--all", help="Include done, stopped, rejected and expired"),
    wallet: str | None = typer.Option(None, "--wallet", help="Only this wallet's brackets"),
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
    no_card: bool = typer.Option(False, "--no-card", help=_NO_CARD_HELP),
) -> None:
    """Live brackets (awaiting approval, armed, triggered, paused), or every one with --all."""

    params: dict[str, Any] = {}
    if all_:
        params["all"] = True
    if wallet:
        params["wallet"] = wallet
    result = _bracket_call("trading.bracket.list", params, json_output=json_output)
    _trigger_emit(result, json_output=json_output, no_card=no_card)


def _bracket_simple(
    method: str,
    bracket_id: str,
    *,
    json_output: bool,
    no_card: bool,
    reason: str | None = None,
) -> None:
    params: dict[str, Any] = {"bracketId": _bracket_id(bracket_id, json_output=json_output)}
    if reason:
        params["reason"] = reason
    result = _bracket_call(method, params, json_output=json_output)
    _trigger_emit(result, json_output=json_output, no_card=no_card)


@bracket_app.command("show")
def bracket_show(
    bracket_id: str = typer.Argument(..., help=_BRACKET_ID_HELP),
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
    no_card: bool = typer.Option(False, "--no-card", help=_NO_CARD_HELP),
) -> None:
    """One bracket: both lines with the price between them, size, each leg's checks, fires."""
    _bracket_simple("trading.bracket.get", bracket_id, json_output=json_output, no_card=no_card)


@bracket_app.command("approve")
def bracket_approve(
    bracket_id: str = typer.Argument(..., help=_BRACKET_ID_HELP),
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
    no_card: bool = typer.Option(False, "--no-card", help=_NO_CARD_HELP),
) -> None:
    """Approve a proposed bracket and arm both legs (yours only; an agent cannot)."""
    _bracket_simple("trading.bracket.approve", bracket_id, json_output=json_output, no_card=no_card)


@bracket_app.command("reject")
def bracket_reject(
    bracket_id: str = typer.Argument(..., help=_BRACKET_ID_HELP),
    reason: str | None = typer.Option(None, "--reason", help="Why (kept on both legs)"),
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
    no_card: bool = typer.Option(False, "--no-card", help=_NO_CARD_HELP),
) -> None:
    """Reject a proposed bracket (both legs)."""
    _bracket_simple(
        "trading.bracket.reject",
        bracket_id,
        json_output=json_output,
        no_card=no_card,
        reason=reason,
    )


@bracket_app.command("pause")
def bracket_pause(
    bracket_id: str = typer.Argument(..., help=_BRACKET_ID_HELP),
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
    no_card: bool = typer.Option(False, "--no-card", help=_NO_CARD_HELP),
) -> None:
    """Pause both legs; neither is checked until you resume the bracket."""
    _bracket_simple("trading.bracket.pause", bracket_id, json_output=json_output, no_card=no_card)


@bracket_app.command("resume")
def bracket_resume(
    bracket_id: str = typer.Argument(..., help=_BRACKET_ID_HELP),
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
    no_card: bool = typer.Option(False, "--no-card", help=_NO_CARD_HELP),
) -> None:
    """Re-arm both legs; checks start over (a trailing stop's peak is the price now)."""
    _bracket_simple("trading.bracket.resume", bracket_id, json_output=json_output, no_card=no_card)


@bracket_app.command("stop")
def bracket_stop(
    bracket_id: str = typer.Argument(..., help=_BRACKET_ID_HELP),
    reason: str | None = typer.Option(None, "--reason", help="Why (kept on both legs)"),
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
    no_card: bool = typer.Option(False, "--no-card", help=_NO_CARD_HELP),
) -> None:
    """Stop both legs for good; an order of either still waiting for approval is rejected."""
    _bracket_simple(
        "trading.bracket.stop",
        bracket_id,
        json_output=json_output,
        no_card=no_card,
        reason=reason,
    )


@bracket_app.command("fire")
def bracket_fire(
    bracket_id: str = typer.Argument(..., help=_BRACKET_ID_HELP),
    leg: str | None = typer.Option(
        None, "--leg", help="tp or sl (default the leg nearer to firing)"
    ),
    wait: bool = typer.Option(False, "--wait", help="Block until the order is decided and settles"),
    wait_seconds: int = typer.Option(
        300, "--wait-seconds", help="How long --wait blocks", min=1, max=900
    ),
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
    no_card: bool = typer.Option(False, "--no-card", help=_NO_CARD_HELP),
) -> None:
    """Fire one leg now, whatever the price (sell now / notify now); the other goes on hold."""

    params: dict[str, Any] = {"bracketId": _bracket_id(bracket_id, json_output=json_output)}
    if leg is not None:
        choice = leg.strip().lower()
        if choice not in _BRACKET_LEG_WORDS:
            _bad_argument(f"--leg {leg!r} must be tp or sl", json_output=json_output)
        params["leg"] = choice

    async def _run(client):
        result = _dict(
            await _trigger_rpc(
                client,
                "trading.bracket.fire",
                params,
                json_output=json_output,
                usage_codes=_BRACKET_USAGE_CODES,
            )
        )
        fire = _dict(result.get("fire"))
        order_id = fire.get("orderId")
        if not (wait and order_id and fire.get("status") in _TRIGGER_OPEN_FIRES):
            return result
        await client.call(
            "trading.orders.wait", {"orderId": order_id, "timeoutSeconds": wait_seconds}
        )
        fresh = _dict(
            await _trigger_rpc(
                client,
                "trading.bracket.get",
                {"bracketId": params["bracketId"]},
                json_output=json_output,
                usage_codes=_BRACKET_USAGE_CODES,
            )
        )
        if not fresh:
            return result
        # The fire is the leg's; its order id says which leg it was.
        settled = next(
            (
                f
                for _, f in _bracket_fires(_dict(fresh.get("bracket")))
                if f.get("orderId") == order_id
            ),
            fire,
        )
        return {**fresh, "fire": settled}

    result = run_gateway_sync(_run, json_output=json_output)
    _trigger_emit(result, json_output=json_output, no_card=no_card)
