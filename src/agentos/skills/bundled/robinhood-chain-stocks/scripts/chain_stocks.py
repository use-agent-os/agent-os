#!/usr/bin/env python3
"""Read Robinhood Chain tokenized-stock state directly from the chain.

Resolves a company name or ticker to its Stock Token on Robinhood Chain
(chainId 4663), then reads live on-chain state over plain JSON-RPC: the
Chainlink USD price, the ERC-8056 ``uiMultiplier()`` corporate-action ratio,
whether the price oracle is paused, total supply, and optionally a holder
balance and its USD value.

Read-only by construction: it issues only ``eth_call`` / ``eth_chainId`` and
never signs, sends, or holds a key.

Emits a compact JSON object on stdout so a meta-skill can run it as a bounded
tool without spawning an LLM sub-agent. Network and contract failures are
reported in the payload rather than raised, so the caller always gets JSON.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any

CHAIN_ID = 4663
DEFAULT_RPC_URL = "https://rpc.mainnet.chain.robinhood.com"
EXPLORER_URL = "https://robinhoodchain.blockscout.com"
TOKEN_LIST_URL = "https://tokens.coingecko.com/robinhood/all.json"
FEEDS_URL = "https://reference-data-directory.vercel.app/feeds-robinhood-mainnet.json"

# Only Stock Tokens carry this suffix in the CoinGecko list. Community tokens on
# the same chain do not -- several of them reuse a real company's name, so the
# suffix is the offline signal that separates the two. `uiMultiplier()` is the
# on-chain confirmation.
_RH_SUFFIX_RE = re.compile(r"\s*[•·|-]?\s*robinhood token\s*$", re.IGNORECASE)
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Function selectors (first 4 bytes of keccak256 of the signature).
SEL_SYMBOL = "0x95d89b41"  # symbol()
SEL_DECIMALS = "0x313ce567"  # decimals()
SEL_TOTAL_SUPPLY = "0x18160ddd"  # totalSupply()
SEL_BALANCE_OF = "0x70a08231"  # balanceOf(address)
SEL_UI_MULTIPLIER = "0xa60bf13d"  # uiMultiplier()      -- ERC-8056
SEL_ORACLE_PAUSED = "0x7706ba52"  # oraclePaused()
SEL_LATEST_ROUND_DATA = "0xfeaf968c"  # latestRoundData() -- AggregatorV3Interface

WAD = 10**18


class RpcError(RuntimeError):
    """A JSON-RPC call returned an error or an unusable result."""


def _http_json(url: str, timeout: float, payload: dict[str, Any] | None = None) -> Any:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"User-Agent": "AgentOS-robinhood-chain-stocks/0.1"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)  # noqa: S310 - fixed endpoints
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _eth_call(rpc_url: str, to: str, data: str, timeout: float) -> str:
    """Return the raw hex result of an ``eth_call``, or raise ``RpcError``."""
    body = _http_json(
        rpc_url,
        timeout,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [{"to": to, "data": data}, "latest"],
        },
    )
    if isinstance(body, dict) and "error" in body:
        message = str(body["error"].get("message", body["error"]))
        raise RpcError(message)
    result = body.get("result") if isinstance(body, dict) else None
    if not isinstance(result, str) or not result.startswith("0x"):
        raise RpcError("malformed RPC result")
    return result


def _word(raw: str, index: int) -> int:
    """Decode the ``index``-th 32-byte word of an ABI return blob as uint256."""
    body = raw[2:]
    start = index * 64
    chunk = body[start : start + 64]
    if len(chunk) < 64:
        raise RpcError("result too short")
    return int(chunk, 16)


def _word_signed(raw: str, index: int) -> int:
    """Same as ``_word`` but interpreted as int256 (Chainlink answers are signed)."""
    value = _word(raw, index)
    return value - (1 << 256) if value >= (1 << 255) else value


def _decode_string(raw: str) -> str:
    """Decode an ABI-encoded dynamic ``string`` return value."""
    offset = _word(raw, 0)
    body = raw[2:]
    head = offset * 2
    length = int(body[head : head + 64], 16)
    payload = body[head + 64 : head + 64 + length * 2]
    return bytes.fromhex(payload).decode("utf-8", errors="replace")


def _encode_address_arg(selector: str, address: str) -> str:
    return selector + address.lower().removeprefix("0x").rjust(64, "0")


def _clean_name(name: str) -> str:
    """Strip the '- Robinhood Token' suffix so 'Apple' matches cleanly."""
    return _RH_SUFFIX_RE.sub("", name or "").strip()


def is_stock_token(token: dict[str, Any]) -> bool:
    """True when the list entry is a Robinhood Stock Token, not a community token."""
    return bool(_RH_SUFFIX_RE.search(token.get("name", "") or ""))


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _score(query: str, token: dict[str, Any]) -> int:
    """Rank a token against a free-text query. Higher is better; 0 = no match."""
    q = _norm(query)
    if not q:
        return 0
    symbol = _norm(token.get("symbol", ""))
    name = _norm(_clean_name(token.get("name", "")))

    if q == symbol:
        return 100
    if q == name:
        return 90
    if name and re.search(rf"\b{re.escape(name)}\b", q):
        return 80
    if len(symbol) >= 2 and re.search(rf"\b{re.escape(symbol)}\b", q):
        return 75
    if re.search(rf"\b{re.escape(q)}\b", name):
        return 70
    if q in name:
        return 50
    if q in symbol:
        return 40
    return 0


def resolve_token(query: str, tokens: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Resolve a name/ticker query to the best-matching Stock Token.

    Community tokens are excluded outright: several impersonate a listed
    company's name and symbol, and handing back an impersonator's address in
    answer to "what is GameStop's contract" is the failure this guards.
    """
    stocks = [t for t in tokens if is_stock_token(t)]
    scored = [(s, t) for t in stocks if (s := _score(query, t)) > 0]
    if not scored:
        return None
    scored.sort(key=lambda pair: (-pair[0], _norm(pair[1].get("symbol", ""))))
    return scored[0][1]


def feed_ticker(feed: dict[str, Any]) -> str:
    """Extract the equity ticker a Robinhood Chainlink feed prices.

    Feed names are not uniform -- 'Robinhood AAPL / USD', 'Robinhood DELL-USD',
    'Robinhood SGOV-USD' all appear -- so prefer the structured ``docs.baseAsset``
    and fall back to parsing the display name.
    """
    docs = feed.get("docs")
    if isinstance(docs, dict):
        base = docs.get("baseAsset")
        if isinstance(base, str) and base.strip():
            return base.strip().upper()
    name = str(feed.get("name", ""))
    if not name.lower().startswith("robinhood"):
        return ""
    rest = name[len("robinhood") :].strip()
    return re.split(r"\s*[/-]\s*", rest, maxsplit=1)[0].strip().upper()


def find_feed(symbol: str, feeds: list[dict[str, Any]]) -> dict[str, Any] | None:
    target = symbol.strip().upper()
    for feed in feeds:
        if feed_ticker(feed) == target and feed.get("proxyAddress"):
            return feed
    return None


def _read_price(rpc_url: str, proxy: str, timeout: float, now: float) -> dict[str, Any]:
    """Read one Chainlink feed, reporting age alongside the answer.

    Age is computed here rather than left to the caller: stock feeds update 24/5
    and carry a 24h heartbeat, so a bare ``updatedAt`` invites reading a
    market-closed quote as the current price.
    """
    raw = _eth_call(rpc_url, proxy, SEL_LATEST_ROUND_DATA, timeout)
    answer = _word_signed(raw, 1)
    updated_at = _word(raw, 3)
    decimals = _word(_eth_call(rpc_url, proxy, SEL_DECIMALS, timeout), 0)

    out: dict[str, Any] = {
        "feedAddress": proxy,
        "answer": answer,
        "decimals": decimals,
        "updatedAt": updated_at,
    }
    # A non-positive answer is not a price; AggregatorV3 uses it to signal an
    # unusable round. Emitting it as "$0.00" would read as a real quote.
    if answer <= 0:
        out["usd"] = None
        out["unusableAnswer"] = True
    else:
        out["usd"] = answer / (10**decimals) if decimals <= 36 else None
    if updated_at > 0:
        out["ageSeconds"] = max(0, int(now - updated_at))
    return out


def _try(fn: Any, errors: dict[str, str], key: str) -> Any:
    """Run a read, recording the failure instead of aborting the whole payload."""
    try:
        return fn()
    except (RpcError, urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        errors[key] = str(exc)
        return None


def _try_call(fn: Any, errors: dict[str, str], key: str) -> tuple[Any, bool]:
    """Like ``_try`` but reports whether the contract answered at all.

    The second element is True only when the node was reached and the contract
    rejected the call. An unreachable node is not evidence about the contract,
    and conflating the two would let a network fault masquerade as proof that a
    genuine Stock Token is an impersonator.
    """
    try:
        return fn(), True
    except RpcError as exc:
        errors[key] = str(exc)
        return None, True
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        errors[key] = str(exc)
        return None, False


def inspect_token(
    rpc_url: str,
    address: str,
    timeout: float,
    holder: str | None = None,
    feed: dict[str, Any] | None = None,
    feeds: list[dict[str, Any]] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Read live on-chain state for one token address.

    ``feeds`` lets the price feed be resolved from the ticker the contract
    reports itself, which is what makes ``--address`` (no name resolution)
    still find a price. Without it, "we never looked up a feed" would surface
    as "this token has no feed" -- a claim the run never actually tested.
    """
    errors: dict[str, str] = {}
    out: dict[str, Any] = {"address": address, "chainId": CHAIN_ID}
    now = time.time() if now is None else now

    out["onchainSymbol"] = _try(
        lambda: _decode_string(_eth_call(rpc_url, address, SEL_SYMBOL, timeout)), errors, "symbol"
    )
    out["decimals"] = _try(
        lambda: _word(_eth_call(rpc_url, address, SEL_DECIMALS, timeout), 0), errors, "decimals"
    )
    total_supply = _try(
        lambda: _word(_eth_call(rpc_url, address, SEL_TOTAL_SUPPLY, timeout), 0),
        errors,
        "totalSupply",
    )
    if total_supply is not None:
        out["totalSupply"] = str(total_supply)
        out["totalSupplyFormatted"] = total_supply / WAD

    # `uiMultiplier()` is the ERC-8056 marker. A genuine Stock Token answers it;
    # a community token reverts, which is what makes this the authoritative check.
    # A node we could not reach proves neither, so that case stays `None` --
    # callers must not present "unverified" as "fake".
    multiplier, answered = _try_call(
        lambda: _word(_eth_call(rpc_url, address, SEL_UI_MULTIPLIER, timeout), 0),
        errors,
        "uiMultiplier",
    )
    out["isStockToken"] = True if multiplier is not None else (False if answered else None)
    if multiplier is not None:
        out["uiMultiplier"] = str(multiplier)
        out["uiMultiplierFormatted"] = multiplier / WAD

    paused = _try(
        lambda: _word(_eth_call(rpc_url, address, SEL_ORACLE_PAUSED, timeout), 0),
        errors,
        "oraclePaused",
    )
    if paused is not None:
        out["oraclePaused"] = bool(paused)

    # Prefer the ticker the contract reports over any caller-supplied hint, so
    # an address-only run resolves its own feed.
    if feed is None and feeds:
        onchain_symbol = out.get("onchainSymbol")
        if isinstance(onchain_symbol, str) and onchain_symbol:
            feed = find_feed(onchain_symbol, feeds)

        # Prefer the ticker the contract reports over any caller-supplied hint, so
    # an address-only run resolves its own feed.
    if feed is None and feeds:
        onchain_symbol = out.get("onchainSymbol")
        if isinstance(onchain_symbol, str) and onchain_symbol:
            feed = find_feed(onchain_symbol, feeds)

    # `isStockToken: False` is documented (and tested) as "the caller should
    # refuse this address" -- but attaching a real company's live Chainlink
    # price to a contract already proven fake does the opposite: an
    # impersonator can self-report a real ticker via symbol() (this lookup
    # trusts that string, not the contract identity) and walk away wearing a
    # genuine, accurate quote. Unverified (None) still gets a price -- only a
    # *confirmed* impersonator is refused, per the same "unverified is not
    # disproven" rule already applied to isStockToken itself above.
    is_confirmed_fake = out["isStockToken"] is False

    if feed is not None and not is_confirmed_fake:
        price = _try(
            lambda: _read_price(rpc_url, str(feed["proxyAddress"]), timeout, now), errors, "price"
        )
        if price is not None:
            heartbeat = feed.get("heartbeat")
            price["heartbeatSeconds"] = heartbeat
            price["deviationThresholdPercent"] = feed.get("threshold")
            # Mark staleness in the payload instead of leaving the reader to
            # compare a unix timestamp against the heartbeat by eye.
            age = price.get("ageSeconds")
            beyond = isinstance(age, int) and isinstance(heartbeat, int) and age > heartbeat
            price["stale"] = bool(beyond or out.get("oraclePaused"))
            out["price"] = price
    elif feed is not None and is_confirmed_fake:
        errors["price"] = "withheld: uiMultiplier() proved this is not a Stock Token"

    if holder and not is_confirmed_fake:
        balance = _try(
            lambda: _word(
                _eth_call(rpc_url, address, _encode_address_arg(SEL_BALANCE_OF, holder), timeout), 0
            ),
            errors,
            "balanceOf",
        )
        if balance is not None:
            tokens_held = balance / WAD
            holding: dict[str, Any] = {
                "holder": holder,
                "balance": str(balance),
                "balanceFormatted": tokens_held,
            }
            usd = (out.get("price") or {}).get("usd")
            if usd is not None:
                holding["valueUsd"] = tokens_held * usd
            out["holding"] = holding
    if errors:
        out["readErrors"] = errors
    return out


def _resolve_target(
    args: argparse.Namespace, timeout: float
) -> tuple[str, dict[str, Any] | None, list[dict[str, Any]]]:
    """Return (address, list-entry-or-None, feeds) for the requested target."""
    feeds: list[dict[str, Any]] = []
    if not args.no_price:
        fetched = _http_json(FEEDS_URL, timeout)
        if isinstance(fetched, list):
            feeds = fetched

    if args.address:
        if not _ADDRESS_RE.match(args.address):
            raise ValueError(f"not a valid 0x address: {args.address}")
        return args.address, None, feeds

    listed = _http_json(TOKEN_LIST_URL, timeout)
    tokens = listed.get("tokens") if isinstance(listed, dict) else None
    if not isinstance(tokens, list):
        raise ValueError("token list unavailable")
    match = resolve_token(args.query or "", tokens)
    if match is None:
        raise ValueError(f"no Robinhood Stock Token matched {args.query!r}")
    return str(match.get("address", "")), match, feeds


def main() -> int:
    parser = argparse.ArgumentParser(description="Robinhood Chain on-chain stock reader")
    parser.add_argument("--query", help="Company name or ticker (e.g. Apple, AAPL)")
    parser.add_argument("--address", help="Token contract address; skips name resolution")
    parser.add_argument("--holder", help="Wallet address to read a balance for")
    parser.add_argument("--rpc-url", default=DEFAULT_RPC_URL, help="Robinhood Chain JSON-RPC URL")
    parser.add_argument("--no-price", action="store_true", help="Skip the Chainlink price read")
    parser.add_argument("--timeout", type=float, default=15.0, help="HTTP timeout seconds")
    args = parser.parse_args()

    if not args.query and not args.address:
        print(json.dumps({"error": "provide --query or --address"}))
        return 0
    if args.holder and not _ADDRESS_RE.match(args.holder):
        print(json.dumps({"error": f"not a valid holder address: {args.holder}"}))
        return 0

    try:
        address, listed, feeds = _resolve_target(args, args.timeout)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        print(json.dumps({"query": args.query, "error": str(exc)}, ensure_ascii=False))
        return 0

    symbol = str((listed or {}).get("symbol", "")) or ""
    feed = find_feed(symbol, feeds) if symbol and feeds else None

    state = inspect_token(
        args.rpc_url, address, args.timeout, holder=args.holder, feed=feed, feeds=feeds
    )
    if not args.no_price and "price" not in state:
        # Say which of the two happened. "We could not fetch the feed list" is
        # not the same claim as "this token has no feed", and reporting the
        # first as the second asserts something the run never checked.
        state.setdefault("notes", []).append(
            "could not fetch the Chainlink feed directory; price unavailable, not disproven"
            if not feeds
            else "no Chainlink feed published for this token; price omitted"
        )
    if listed is not None:
        state["name"] = _clean_name(str(listed.get("name", "")))
        state["symbol"] = symbol
    if state.get("isStockToken") is False:
        state.setdefault("notes", []).append(
            "uiMultiplier() reverted: this address is not a Robinhood Stock Token"
        )
    elif state.get("isStockToken") is None:
        state.setdefault("notes", []).append(
            "could not reach the RPC node: Stock Token status unverified, not disproven"
        )

    result = {
        "query": args.query,
        "chainId": CHAIN_ID,
        "rpcUrl": args.rpc_url,
        "explorer": f"{EXPLORER_URL}/token/{address}",
        "sources": {"tokenList": TOKEN_LIST_URL, "priceFeeds": FEEDS_URL},
        "token": state,
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
