#!/usr/bin/env python3
"""Report new entries in an RSS or Atom feed, and nothing when there are none.

Built for an AgentOS cron script job:

    agentos cron add --every 15m --name hn-watch \\
      --script watch_rss.py --script-arg --name --script-arg hn \\
      --script-arg --url --script-arg https://news.ycombinator.com/rss

Prints one line per new entry and exits 0. Prints nothing when the feed has
nothing new, which the scheduler treats as a silent run.
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _watermark import select_new  # noqa: E402

USER_AGENT = "AgentOS-cron-watcher/1.0"


def validate_http_url(url: str) -> str:
    """Validate and return a URL that must be http:// or https://.

    Rejects ``file://``, ``ftp://``, and any custom scheme that could leak
    local data or be abused as an SSRF oracle. Uses ``urlsplit`` for robust
    scheme detection (not a fragile ``startswith`` prefix check).
    """
    cleaned = (url or "").strip()
    if not cleaned:
        raise ValueError(f"empty URL: {url!r}")
    try:
        parsed = urllib.parse.urlsplit(cleaned)
    except ValueError as exc:
        raise ValueError(f"invalid URL {url!r}: {exc}") from exc
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(
            f"invalid URL scheme {parsed.scheme!r} in {url!r}: must be http:// or https://"
        )
    if not parsed.netloc:
        raise ValueError(f"URL missing host {url!r}: must be http:// or https://")
    return cleaned


def _text(node: ET.Element | None) -> str:
    return (node.text or "").strip() if node is not None else ""


def _entries(root: ET.Element) -> list[tuple[str, str, str]]:
    """Return ``(id, title, link)`` for each item, RSS or Atom."""
    found: list[tuple[str, str, str]] = []

    for item in root.iter():
        tag = item.tag.rsplit("}", 1)[-1]
        if tag not in {"item", "entry"}:
            continue
        title = _text(item.find("title")) or _text(item.find("{*}title"))
        link = _text(item.find("link")) or _text(item.find("{*}link"))
        if not link:
            link_el = item.find("{*}link")
            if link_el is not None:
                link = (link_el.get("href") or "").strip()
        guid = (
            _text(item.find("guid"))
            or _text(item.find("id"))
            or _text(item.find("{*}id"))
            or link
            or title
        )
        if guid:
            found.append((guid, title, link))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Feed URL")
    parser.add_argument("--name", required=True, help="Watermark name, unique per feed")
    parser.add_argument("--limit", type=int, default=10, help="Max entries to report")
    parser.add_argument(
        "--first-run-reports",
        action="store_true",
        help="Report everything on the very first run instead of staying silent.",
    )
    args = parser.parse_args()

    try:
        validated_url = validate_http_url(args.url)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    request = urllib.request.Request(validated_url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"Feed fetch failed for {args.url}: {exc}", file=sys.stderr)
        return 1

    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        print(f"Feed is not valid XML: {exc}", file=sys.stderr)
        return 1

    entries = _entries(root)
    by_id = {entry[0]: entry for entry in entries}
    fresh = select_new(args.name, list(by_id), first_run_reports=args.first_run_reports)
    if not fresh:
        return 0

    for guid in fresh[: args.limit]:
        _, title, link = by_id[guid]
        print(f"- {title or guid}" + (f"\n  {link}" if link else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
