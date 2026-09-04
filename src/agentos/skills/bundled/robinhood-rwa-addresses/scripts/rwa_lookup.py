#!/usr/bin/env python3
"""Look up Robinhood tokenized-stock (RWA) contract addresses by name or ticker.

Resolves a free-text query (a company name like "Apple", or a ticker like
"AAPL") to the matching on-chain token: symbol, contract address, chain id, and
decimals.

Two stages, deliberately separated:

1. **Discovery** -- rank candidates against the public CoinGecko Robinhood token
   list. Cheap, offline-friendly, but the list is a third-party index: it names
   assets Robinhood has announced but not yet deployed, and it truncates long
   names (so the "Robinhood Token" suffix that used to be the only stock filter
   goes missing on entries like IBM and XLK).
2. **Verification** -- confirm each candidate against Robinhood Chain itself.
   Every genuine Stock Token is a beacon proxy pointing at Robinhood's shared
   EIP-1967 beacon; an impersonator cannot forge that, and an undeployed listing
   has no contract at all. This is the authoritative signal.

Emits a compact JSON object on stdout so a meta-skill can run it as a bounded
tool without spawning an LLM sub-agent. Network failures degrade to
``status: "unverified"`` rather than raising -- an unreachable node must never
be reported as evidence that a token is fake.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from typing import Any

TOKEN_LIST_URL = "https://tokens.coingecko.com/robinhood/all.json"
DEFAULT_RPC_URL = "https://rpc.mainnet.chain.robinhood.com"

# EIP-1967 beacon slot: bytes32(uint256(keccak256("eip1967.proxy.beacon")) - 1).
BEACON_SLOT = "0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50"
# Robinhood's shared implementation beacon. Every Stock Token on chain 4663 is a
# proxy pointing here; nothing else on the permissionless chain can point at it.
ROBINHOOD_BEACON = "0xe10b6f6b275de231345c20d14ab812db62151b00"

# Robinhood-token names carry this marker in the CoinGecko list. It is a *hint*
# only: CoinGecko caps `name` at 60 characters, so long listings arrive with the
# suffix chopped ("... - Robinhood Toke"). Used for display cleanup and as the
# offline fallback filter, never as proof on its own.
_RH_SUFFIX_RE = re.compile(r"\s*[•·|-]?\s*robinhood token\s*$", re.IGNORECASE)
# The truncation-tolerant form: a bullet followed by any prefix of the marker.
_RH_SUFFIX_LOOSE_RE = re.compile(
    r"\s*[•·|-]\s*r(?:o(?:b(?:i(?:n(?:h(?:o(?:o(?:d)?)?)?)?)?)?)?)?"
    r"(?:\s+t(?:o(?:k(?:e(?:n)?)?)?)?)?\s*$",
    re.IGNORECASE,
)
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Verification outcomes, most trustworthy first.
STATUS_VERIFIED = "verified"  # beacon matches: a genuine, deployed Stock Token
STATUS_NOT_DEPLOYED = "not-deployed"  # listed by the index, no contract on chain
STATUS_NOT_STOCK = "not-a-stock-token"  # a contract exists, but not Robinhood's
STATUS_UNVERIFIED = "unverified"  # the chain could not be reached

_STATUS_RANK = {
    STATUS_VERIFIED: 0,
    STATUS_UNVERIFIED: 1,
    STATUS_NOT_DEPLOYED: 2,
    STATUS_NOT_STOCK: 3,
}


class RpcError(RuntimeError):
    """A JSON-RPC call returned an error or an unusable result."""


def _fetch_tokens(timeout: float) -> list[dict[str, Any]]:
    req = urllib.request.Request(  # noqa: S310 - fixed trusted CoinGecko endpoint
        TOKEN_LIST_URL,
        headers={"User-Agent": "AgentOS-robinhood-rwa-skill/0.2"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    tokens = data.get("tokens")
    return tokens if isinstance(tokens, list) else []


def _clean_name(name: str) -> str:
    """Strip the '• Robinhood Token' suffix so 'Apple' matches cleanly.

    Handles the truncated tail too: CoinGecko cuts `name` at 60 characters, so
    'International Business Machines Corporation • Robinhood Toke' must still
    display as the company name.
    """
    stripped = _RH_SUFFIX_RE.sub("", name or "").strip()
    if stripped == (name or "").strip():
        stripped = _RH_SUFFIX_LOOSE_RE.sub("", stripped).strip()
    return stripped


def is_stock_token(token: dict[str, Any]) -> bool:
    """True when the list entry *looks* like a Robinhood Stock Token.

    Name-based and therefore only a hint -- ``verify_tokens`` is what actually
    decides. Kept as the offline fallback for when the chain is unreachable.
    """
    name = token.get("name", "") or ""
    return bool(_RH_SUFFIX_RE.search(name) or _RH_SUFFIX_LOOSE_RE.search(name))


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _score(query: str, token: dict[str, Any]) -> int:
    """Rank a token against the query. Higher is better; 0 = no match.

    The query may be a bare name/ticker ("Apple", "AAPL") or a full sentence --
    the skill entrypoint defaults ``--query`` to the raw user message (e.g.
    "what is Apple's ticker?", "mã cổ phiếu Apple là gì") -- so matching must
    also find the company name or ticker *inside* the query.
    """
    q = _norm(query)
    if not q:
        return 0
    symbol = _norm(token.get("symbol", ""))
    name = _norm(_clean_name(token.get("name", "")))

    if q == symbol:
        return 100
    if q == name:
        return 90
    # Company name appears as a whole phrase inside a longer query
    # ("apple" in "what is apple s ticker").
    if name and re.search(rf"\b{re.escape(name)}\b", q):
        return 80
    # Ticker appears as a standalone word inside the query ("aapl" in
    # "gia aapl bao nhieu"). Require len >= 2 to avoid single-letter noise.
    if len(symbol) >= 2 and re.search(rf"\b{re.escape(symbol)}\b", q):
        return 75
    # Query is a word inside the company name ("beauty" → "e l f beauty").
    if re.search(rf"\b{re.escape(q)}\b", name):
        return 70
    if q in name:
        return 50
    if q in symbol:
        return 40
    return 0


def _shape(token: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": _clean_name(token.get("name", "")),
        "symbol": token.get("symbol", ""),
        "address": token.get("address", ""),
        "chainId": token.get("chainId"),
        "decimals": token.get("decimals"),
        "isStockToken": is_stock_token(token),
        "logoURI": token.get("logoURI", ""),
    }


def lookup(
    query: str,
    tokens: list[dict[str, Any]],
    limit: int,
    *,
    include_community: bool = False,
) -> list[dict[str, Any]]:
    """Rank tokens against the query, likely Stock Tokens first.

    Discovery only -- no network. Community tokens are dropped unless explicitly
    requested: several of them impersonate a listed company, so answering
    "GameStop's address" with one of those would hand the user a token that is
    not the equity they asked for.
    """
    pool = tokens if include_community else [t for t in tokens if is_stock_token(t)]
    scored = [(s, t) for t in pool if (s := _score(query, t)) > 0]
    # Stock Tokens outrank community tokens at equal relevance, so an impersonator
    # can never displace the real listing even when both are requested.
    scored.sort(
        key=lambda pair: (-pair[0], not is_stock_token(pair[1]), _norm(pair[1].get("symbol", "")))
    )
    return [_shape(t) for _s, t in scored[:limit]]


def _rpc_batch(rpc_url: str, calls: list[dict[str, Any]], timeout: float) -> dict[str, str]:
    """Send one batched JSON-RPC request; return ``{id: result}`` for successes.

    Batching keeps verification to a single HTTP round-trip no matter how many
    candidates are being checked.
    """
    payload = json.dumps(calls).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310 - operator-supplied RPC endpoint
        rpc_url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "AgentOS-robinhood-rwa-skill/0.2",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        body = json.loads(resp.read().decode("utf-8", errors="replace"))
    if isinstance(body, dict) and "error" in body:
        error_val = body["error"]
        if isinstance(error_val, dict):
            message = str(error_val.get("message", error_val))
        else:
            message = str(error_val)
        raise RpcError(message)
    if not isinstance(body, list):
        raise RpcError("expected a batched JSON-RPC response")
    out: dict[str, str] = {}
    for item in body:
        if isinstance(item, dict) and isinstance(item.get("result"), str):
            out[str(item.get("id"))] = item["result"]
    return out


def classify(beacon_word: str | None, code: str | None) -> str:
    """Turn a raw beacon-slot word and ``eth_getCode`` result into a status."""
    if beacon_word is None or code is None:
        return STATUS_UNVERIFIED
    if len(code) <= 2:  # "0x" -- nothing deployed at this address
        return STATUS_NOT_DEPLOYED
    if beacon_word[-40:].lower() == ROBINHOOD_BEACON.removeprefix("0x").lower():
        return STATUS_VERIFIED
    return STATUS_NOT_STOCK


def verify_tokens(
    matches: list[dict[str, Any]],
    rpc_url: str,
    timeout: float,
) -> list[dict[str, Any]]:
    """Annotate each match with its on-chain ``status`` (and beacon when genuine).

    On any RPC failure every match is marked ``unverified`` -- an unreachable
    node means the check did not run, never that the token is fake.
    """
    checkable = [m for m in matches if _ADDRESS_RE.match(str(m.get("address", "")))]
    if not checkable:
        for match in matches:
            match["status"] = STATUS_UNVERIFIED
            match["beacon"] = None
        return matches

    calls: list[dict[str, Any]] = []
    for index, match in enumerate(checkable):
        address = match["address"]
        calls.append(
            {
                "jsonrpc": "2.0",
                "id": f"s{index}",
                "method": "eth_getStorageAt",
                "params": [address, BEACON_SLOT, "latest"],
            }
        )
        calls.append(
            {
                "jsonrpc": "2.0",
                "id": f"c{index}",
                "method": "eth_getCode",
                "params": [address, "latest"],
            }
        )

    try:
        results = _rpc_batch(rpc_url, calls, timeout)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError, RpcError):
        results = {}

    for match in matches:
        match["status"] = STATUS_UNVERIFIED
        match["beacon"] = None
    for index, match in enumerate(checkable):
        status = classify(results.get(f"s{index}"), results.get(f"c{index}"))
        match["status"] = status
        if status == STATUS_VERIFIED:
            match["beacon"] = ROBINHOOD_BEACON
    return matches


def sort_by_status(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stable re-sort so verified tokens outrank unverified/undeployed ones."""
    return sorted(matches, key=lambda m: _STATUS_RANK.get(str(m.get("status")), 9))


def _warning_for(
    matches: list[dict[str, Any]], *, verification_skipped: bool = False
) -> str | None:
    """A single caller-facing caveat, chosen by the worst status that surfaced.

    ``verification_skipped`` distinguishes "the operator turned the check off"
    from "the node was unreachable" -- both leave matches unverified, but only
    the second one is a fault worth retrying.
    """
    statuses = {str(m.get("status")) for m in matches}
    if verification_skipped and statuses:
        return (
            "on-chain verification was skipped (--no-verify), so these addresses are "
            "UNVERIFIED: the token index alone cannot prove a contract is deployed or genuine."
        )
    if STATUS_VERIFIED in statuses:
        return None
    if STATUS_NOT_DEPLOYED in statuses:
        return (
            "listed by the token index but not deployed on Robinhood Chain: there is no "
            "contract at this address. Do not send funds to it."
        )
    if STATUS_NOT_STOCK in statuses:
        return (
            "a contract exists at this address but it is not a Robinhood Stock Token "
            "(it does not use Robinhood's beacon). Treat it as an impersonator."
        )
    if STATUS_UNVERIFIED in statuses:
        return (
            "Robinhood Chain could not be reached, so these addresses are UNVERIFIED "
            "(not disproven). Re-run before relying on them."
        )
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Robinhood RWA contract-address lookup")
    parser.add_argument("--query", required=True, help="Company name or ticker (e.g. Apple, AAPL)")
    parser.add_argument("--limit", type=int, default=5, help="Max matches to return")
    parser.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout seconds")
    parser.add_argument(
        "--include-community",
        action="store_true",
        help="Also match non-stock community tokens (off by default; some impersonate listings)",
    )
    parser.add_argument("--rpc-url", default=DEFAULT_RPC_URL, help="Robinhood Chain JSON-RPC URL")
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip the on-chain beacon check (offline; every match is reported unverified)",
    )
    parser.add_argument(
        "--cards",
        metavar="FILE",
        help=(
            "Where to write the Web-chat card artifact. Defaults to <query>.cards.json "
            "in the working directory; the publish marker goes to stderr so stdout stays "
            "pure JSON."
        ),
    )
    parser.add_argument(
        "--no-cards",
        action="store_true",
        help="Do not write the card artifact (JSON on stdout only).",
    )
    args = parser.parse_args()

    try:
        tokens = _fetch_tokens(args.timeout)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        print(json.dumps({"query": args.query, "matches": [], "error": f"fetch failed: {exc}"}))
        return 0

    matches = lookup(
        args.query, tokens, max(1, args.limit), include_community=args.include_community
    )
    if args.no_verify:
        for match in matches:
            match["status"] = STATUS_UNVERIFIED
            match["beacon"] = None
    else:
        matches = sort_by_status(verify_tokens(matches, args.rpc_url, args.timeout))

    result: dict[str, Any] = {
        "query": args.query,
        "source": TOKEN_LIST_URL,
        "rpc": None if args.no_verify else args.rpc_url,
        "beacon": ROBINHOOD_BEACON,
        "total_tokens": len(tokens),
        "stock_tokens": sum(1 for t in tokens if is_stock_token(t)),
        "matches": matches,
    }
    if not matches:
        result["error"] = "no Robinhood token matched the query"
    warning = _warning_for(matches, verification_skipped=args.no_verify)
    if warning:
        result["warning"] = warning
    print(json.dumps(result, ensure_ascii=False))
    if not args.no_cards:
        _write_cards(result, args.cards or _default_cards_name(result))
    return 0


def _default_cards_name(result: dict[str, Any]) -> str:
    matches = result.get("matches") or []
    symbol = ""
    if matches and isinstance(matches[0], dict):
        symbol = str(matches[0].get("symbol") or "")
    slug = re.sub(r"[^A-Za-z0-9._-]", "", symbol) or "lookup"
    return f"{slug}.cards.json"


def _write_cards(result: dict[str, Any], output: str) -> None:
    """Write the card artifact and announce it on **stderr**.

    On by default rather than opt-in. Both were tried first and both failed the
    same way: told to run a second piped command, or to pass a flag the docs put
    in every example, the model ran the bare command anyway and answered with a
    hand-written table. The only arrangement that actually renders is the one
    that needs no decision from it at all.

    stdout stays pure JSON so the lookup itself is unchanged; ``exec_command``
    merges stderr into the captured output, so the publish marker still reaches
    the auto-publisher.

    Never fatal -- a failed render must not cost the caller the lookup it
    already paid for.
    """
    try:
        from pathlib import Path  # noqa: PLC0415 - only needed on this path

        import rwa_cards  # noqa: PLC0415 - sibling module, resolved at call time

        payload = rwa_cards.build_payload(result)
        if not payload["cards"]:
            return
        Path(output).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        print(f"publish_artifact path={output} mime={rwa_cards.CARDS_MIME}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - lookup already printed; never fail on the card
        print(f"[card not written: {exc}]", file=sys.stderr)


if __name__ == "__main__":
    import pathlib

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    sys.exit(main())
