#!/usr/bin/env python3
"""Look up Robinhood tokenized-stock (RWA) contract addresses by name or ticker.

Reads the public CoinGecko Robinhood token list and resolves a free-text query
(a company name like "Apple", or a ticker like "AAPL") to the matching on-chain
token(s): symbol, contract address, chain id, and decimals.

Emits a compact JSON object on stdout so a meta-skill can run it as a bounded
tool without spawning an LLM sub-agent.
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
# The Robinhood-token names are suffixed with this marker in the list.
_RH_SUFFIX_RE = re.compile(r"\s*[•·|-]?\s*robinhood token\s*$", re.IGNORECASE)


def _fetch_tokens(timeout: float) -> list[dict[str, Any]]:
    req = urllib.request.Request(  # noqa: S310 - fixed trusted CoinGecko endpoint
        TOKEN_LIST_URL,
        headers={"User-Agent": "AgentOS-robinhood-rwa-skill/0.1"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    tokens = data.get("tokens")
    return tokens if isinstance(tokens, list) else []


def _clean_name(name: str) -> str:
    """Strip the '• Robinhood Token' suffix so 'Apple' matches cleanly."""
    return _RH_SUFFIX_RE.sub("", name or "").strip()


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _score(query: str, token: dict[str, Any]) -> int:
    """Rank a token against the query. Higher is better; 0 = no match.

    The query may be a bare name/ticker ("Apple", "AAPL") or a full sentence —
    the skill entrypoint defaults ``--query`` to the raw user message (e.g.
    "what is Apple's ticker?", "mã cổ phiếu Apple là gì") — so matching must
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
        "logoURI": token.get("logoURI", ""),
    }


def lookup(query: str, tokens: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    scored = [(s, t) for t in tokens if (s := _score(query, t)) > 0]
    scored.sort(key=lambda pair: (-pair[0], _norm(pair[1].get("symbol", ""))))
    return [_shape(t) for _s, t in scored[:limit]]


def main() -> int:
    parser = argparse.ArgumentParser(description="Robinhood RWA contract-address lookup")
    parser.add_argument("--query", required=True, help="Company name or ticker (e.g. Apple, AAPL)")
    parser.add_argument("--limit", type=int, default=5, help="Max matches to return")
    parser.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout seconds")
    args = parser.parse_args()

    try:
        tokens = _fetch_tokens(args.timeout)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        print(
            json.dumps(
                {"query": args.query, "matches": [], "error": f"fetch failed: {exc}"}
            )
        )
        return 0

    matches = lookup(args.query, tokens, max(1, args.limit))
    result = {
        "query": args.query,
        "source": TOKEN_LIST_URL,
        "total_tokens": len(tokens),
        "matches": matches,
    }
    if not matches:
        result["error"] = "no Robinhood token matched the query"
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
