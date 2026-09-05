#!/usr/bin/env python3
"""Turn ``chain_stocks.py`` output into a chat card artifact.

The Web chat renders an artifact whose mime is
``application/vnd.agentos.cards+json`` as an inline card instead of a download
chip, and ``exec_command`` publishes the marker this script prints without the
model having to ask for it.

Usage (stdin is ``chain_stocks.py``'s JSON):

    python3 {baseDir}/scripts/chain_stocks.py --query Apple \
      | python3 {baseDir}/scripts/chain_cards.py --output aapl.cards.json

One card per reading, laid out so the two things a caller can act on wrongly --
a **stale price** and an **unverified token** -- are the badge, not a footnote.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

CARDS_MIME = "application/vnd.agentos.cards+json"


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def badge(token: dict[str, Any]) -> tuple[str, str]:
    """Return ``(badge, tone)`` for the token's trustworthiness and freshness.

    Ordered by what would mislead a reader most. ``isStockToken`` is three-state
    (see the skill's own contract): ``None`` means the chain could not be
    reached, which is *unverified*, never *fake*.
    """
    is_stock = token.get("isStockToken")
    if is_stock is None:
        return "unverified", "neutral"
    if is_stock is False:
        return "not-a-stock-token", "danger"
    price = token.get("price")
    if isinstance(price, dict) and price.get("stale") is True:
        return "price stale", "warning"
    if token.get("oraclePaused") is True:
        return "oracle paused", "warning"
    return "verified", "positive"


def _price_field(token: dict[str, Any]) -> dict[str, Any] | None:
    price = token.get("price")
    if not isinstance(price, dict):
        return None
    usd = price.get("usd")
    # A non-positive feed answer is not a price; the skill forbids reporting it
    # as "$0", so it is omitted rather than shown.
    if not isinstance(usd, int | float) or usd <= 0:
        return None
    return {"label": "Price", "value": f"${usd:,.4f}"}


def build_cards(result: dict[str, Any]) -> list[dict[str, Any]]:
    token = result.get("token")
    if not isinstance(token, dict):
        return []
    symbol = _text(token.get("symbol")) or _text(token.get("onchainSymbol"))
    name = _text(token.get("name"))
    title = symbol or name
    if not title:
        return []

    label, tone = badge(token)
    fields: list[dict[str, Any]] = []

    price_field = _price_field(token)
    if price_field:
        fields.append(price_field)

    supply = token.get("totalSupplyFormatted")
    if isinstance(supply, int | float):
        fields.append({"label": "Supply", "value": f"{supply:,.4f}"})

    multiplier = token.get("uiMultiplierFormatted")
    if isinstance(multiplier, int | float):
        fields.append({"label": "Multiplier", "value": f"{multiplier:.6f}"})

    address = _text(token.get("address"))
    if address:
        fields.append({"label": "Address", "value": address, "copyable": True})

    chain_id = token.get("chainId") or result.get("chainId")
    if isinstance(chain_id, int):
        fields.append({"label": "Chain", "value": str(chain_id)})

    return [
        {
            "title": title,
            "subtitle": name if symbol else "",
            "logo": _text(token.get("logoURI")),
            "badge": label,
            "badgeTone": tone,
            "fields": fields,
        }
    ]


def build_payload(result: dict[str, Any]) -> dict[str, Any]:
    query = _text(result.get("query"))
    token = result.get("token")
    subtitle = ""
    if isinstance(token, dict):
        price = token.get("price")
        if isinstance(price, dict) and price.get("stale") is True:
            subtitle = "price is stale — do not quote it as current"
        elif token.get("isStockToken") is None:
            subtitle = "chain unreachable — unverified, not disproven"
    return {
        "type": "cards",
        "title": f"Robinhood Chain — {query}" if query else "Robinhood Chain",
        "subtitle": subtitle,
        "cards": build_cards(result),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Render chain_stocks output as a chat card")
    parser.add_argument("--output", required=True, help="Where to write the cards artifact.")
    args = parser.parse_args()

    raw = sys.stdin.read().strip()
    if not raw:
        print("chain_cards: no input received", file=sys.stderr)
        return 1
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"chain_cards: input is not valid JSON: {exc}", file=sys.stderr)
        return 1
    if not isinstance(result, dict):
        print("chain_cards: expected a chain_stocks object", file=sys.stderr)
        return 1

    payload = build_payload(result)
    if not payload["cards"]:
        print("chain_cards: no token to render", file=sys.stderr)
        return 1

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"publish_artifact path={output} mime={CARDS_MIME}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
