#!/usr/bin/env python3
"""Turn ``rwa_lookup.py`` output into a chat card-grid artifact.

The Web chat renders an artifact whose mime is
``application/vnd.agentos.cards+json`` as an inline grid of cards instead of a
download chip. A contract address is 42 characters, so a markdown table forces a
horizontal scroll; a card carries the address on its own line with a copy
button next to it.

Usage (stdin is ``rwa_lookup.py``'s JSON):

    python3 {baseDir}/scripts/rwa_lookup.py --query "Apple" \
      | python3 {baseDir}/scripts/rwa_cards.py --output apple.cards.json

It then prints the written path, which is what ``publish_artifact`` takes.

The badge tone is derived from the lookup's ``status`` so an undeployed listing
or an impersonator reads as a warning at a glance rather than looking like every
other result.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

CARDS_MIME = "application/vnd.agentos.cards+json"

# status -> badge tone understood by the frontend (cards.ts). Anything missing
# falls back to "neutral" there, so a new status degrades rather than breaks.
_TONES = {
    "verified": "positive",
    "not-deployed": "warning",
    "not-a-stock-token": "danger",
    "unverified": "neutral",
}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def build_cards(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Map lookup matches onto card payloads, best match first."""
    cards: list[dict[str, Any]] = []
    for match in result.get("matches") or []:
        if not isinstance(match, dict):
            continue
        symbol = _text(match.get("symbol"))
        name = _text(match.get("name"))
        title = symbol or name
        if not title:
            continue

        status = _text(match.get("status"))
        fields: list[dict[str, Any]] = []
        address = _text(match.get("address"))
        if address:
            fields.append({"label": "Address", "value": address, "copyable": True})
        chain_id = match.get("chainId")
        if isinstance(chain_id, int):
            fields.append({"label": "Chain", "value": str(chain_id)})

        cards.append(
            {
                "title": title,
                "subtitle": name if symbol else "",
                "logo": _text(match.get("logoURI")),
                "badge": status,
                "badgeTone": _TONES.get(status, "neutral"),
                "fields": fields,
            }
        )
    return cards


def build_payload(result: dict[str, Any]) -> dict[str, Any]:
    query = _text(result.get("query"))
    return {
        "type": "cards",
        "title": f"Robinhood Chain — {query}" if query else "Robinhood Chain",
        # The lookup's caveat rides along as the subtitle so the grid never shows
        # an unverified or undeployed address without its warning.
        "subtitle": _text(result.get("warning")),
        "cards": build_cards(result),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Render rwa_lookup output as chat cards")
    parser.add_argument("--output", required=True, help="Where to write the cards artifact.")
    args = parser.parse_args()

    raw = sys.stdin.read().strip()
    if not raw:
        print("rwa_cards: no input received", file=sys.stderr)
        return 1
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"rwa_cards: input is not valid JSON: {exc}", file=sys.stderr)
        return 1
    if not isinstance(result, dict):
        print("rwa_cards: expected a lookup object", file=sys.stderr)
        return 1

    payload = build_payload(result)
    if not payload["cards"]:
        print("rwa_cards: no matches to render", file=sys.stderr)
        return 1

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"publish_artifact path={output} mime={CARDS_MIME}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
