#!/usr/bin/env python3
"""Report new objects from a JSON endpoint, and nothing when there are none.

Built for an AgentOS cron script job:

    agentos cron add --every 5m --name api-events \\
      --script watch_http_json.py \\
      --script-arg --name --script-arg api-events \\
      --script-arg --url --script-arg https://api.example.com/events \\
      --script-arg --id-field --script-arg event_id

The response may be a top-level list, or an object holding one at a dotted
``--items-path`` (e.g. ``data.events``). Each item is deduped by ``--id-field``.
Prints one line per new item and nothing when there is nothing new. A run
where NO fetched item carries ``--id-field`` is a configuration error, not a
quiet feed: it exits non-zero and names the field, leaving the watermark
untouched, instead of looking empty forever.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from _url import require_http_url  # noqa: E402
from _watermark import positive_int, select_new  # noqa: E402

USER_AGENT = "AgentOS-cron-watcher/1.0"


def _dig(payload: Any, dotted: str) -> Any:
    node = payload
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _summarize(item: dict[str, Any], fields: list[str]) -> str:
    if fields:
        picked = [f"{key}={item[key]!r}" for key in fields if key in item]
        if picked:
            return " ".join(picked)
    for key in ("title", "name", "summary", "message", "description"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return json.dumps(item, ensure_ascii=False)[:200]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Endpoint returning JSON")
    parser.add_argument("--name", required=True, help="Watermark name, unique per endpoint")
    parser.add_argument("--id-field", default="id", help="Field that identifies an item")
    parser.add_argument("--items-path", default="", help="Dotted path to the list, if nested")
    parser.add_argument(
        "--field",
        action="append",
        default=[],
        help="Field to show in the report line. Repeatable.",
    )
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        help="Extra request header as 'Key: value'. Repeatable.",
    )
    parser.add_argument(
        "--limit", type=positive_int, default=10, help="Max items to report per run"
    )
    parser.add_argument(
        "--first-run-reports",
        action="store_true",
        help="Report everything on the very first run instead of staying silent.",
    )
    args = parser.parse_args()

    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    for raw in args.header:
        key, _, value = raw.partition(":")
        if key.strip() and value.strip():
            headers[key.strip()] = value.strip()

    try:
        target = require_http_url(args.url, "--url")
    except ValueError as exc:
        print(f"Refusing to fetch: {exc}", file=sys.stderr)
        return 1

    request = urllib.request.Request(target, headers=headers)  # noqa: S310 - http(s) only
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"Request failed for {args.url}: {exc}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"Response was not JSON: {exc}", file=sys.stderr)
        return 1

    items = _dig(payload, args.items_path) if args.items_path else payload
    if not isinstance(items, list):
        where = args.items_path or "the response body"
        print(f"Expected a list at {where}, got {type(items).__name__}", file=sys.stderr)
        return 1

    by_id: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        identifier = item.get(args.id_field)
        if identifier is None:
            continue
        by_id[str(identifier)] = item

    if items and not by_id:
        # Not a quiet feed — a misconfiguration. Silent-zero here is the bug:
        # the operator would keep trusting an empty report forever.
        sample = next((item for item in items if isinstance(item, dict)), None)
        hint = f"; first item has fields {sorted(sample)}" if sample else ""
        print(
            f"Error: --id-field {args.id_field!r} matched none of the "
            f"{len(items)} item(s) fetched from {args.url}; nothing was "
            f"reported and the watermark was not updated{hint}.",
            file=sys.stderr,
        )
        return 1

    fresh = select_new(
        args.name, list(by_id), first_run_reports=args.first_run_reports, limit=args.limit
    )
    if not fresh:
        return 0

    for identifier in fresh:
        print(f"- {_summarize(by_id[identifier], args.field)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
