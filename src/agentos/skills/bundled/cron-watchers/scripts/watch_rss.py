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
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urljoin

sys.path.insert(0, str(Path(__file__).parent))

from _url import require_http_url  # noqa: E402
from _watermark import positive_int, select_new  # noqa: E402

USER_AGENT = "AgentOS-cron-watcher/1.0"


def _text(node: ET.Element | None) -> str:
    return (node.text or "").strip() if node is not None else ""


_XML_BASE = "{http://www.w3.org/XML/1998/namespace}base"

#: Where an Atom ``<link>`` points, ranked. ``alternate`` is the entry's own
#: page and is what a reader wants; RFC 4287 defaults a missing ``rel`` to it,
#: and allows several alternates that differ by ``type``, so an HTML one
#: outranks a PDF one. ``self`` is the feed's own address -- it can never be
#: the destination for an entry -- and ranks last, behind ``enclosure``,
#: ``related`` and the rest.
_HTML_TYPES = {"", "text/html", "application/xhtml+xml"}


def _link_rank(rel: str, mime: str) -> int:
    if rel == "alternate":
        return 0 if mime in _HTML_TYPES else 1
    if rel == "self":
        return 3
    return 2


def _link_rel(link_el: ET.Element) -> str:
    """The relation name, whether written bare or as its IANA IRI."""
    rel = (link_el.get("rel") or "alternate").strip().lower().rstrip("/")
    return rel.rsplit("/", 1)[-1]


def _base_url(node: ET.Element, parents: dict[ET.Element, ET.Element], feed_url: str) -> str:
    """The URL a relative ``href`` under *node* resolves against.

    ``xml:base`` may sit on any ancestor (RFC 4287 §2), each one relative to
    the next one out, and the outermost is relative to the feed's own URL.
    """
    chain: list[str] = []
    current: ET.Element | None = node
    while current is not None:
        base = (current.get(_XML_BASE) or "").strip()
        if base:
            chain.append(base)
        current = parents.get(current)
    resolved = feed_url
    for base in reversed(chain):
        resolved = urljoin(resolved, base)
    return resolved


def _entry_link(item: ET.Element, parents: dict[ET.Element, ET.Element], feed_url: str) -> str:
    """The URL an entry points at, or ``""``.

    RSS writes the URL as ``<link>`` text. Atom writes ``<link href=…>``
    elements, usually several, and document order says nothing about which
    is the article: platforms commonly put ``rel="self"`` first, so taking
    the first ``<link>`` handed back the feed's own URL for every entry (#2468).
    An RSS item with no ``<link>`` at all may still carry its URL as a
    permalink ``<guid>``, which the RSS spec makes the default meaning.
    """
    text_link = _text(item.find("link")) or _text(item.find("{*}link"))
    if text_link:
        return urljoin(_base_url(item, parents, feed_url), text_link)

    best: tuple[int, str] | None = None
    for link_el in item.findall("{*}link"):
        href = (link_el.get("href") or "").strip()
        if not href:
            continue
        rank = _link_rank(_link_rel(link_el), (link_el.get("type") or "").strip().lower())
        if best is None or rank < best[0]:
            best = (rank, urljoin(_base_url(link_el, parents, feed_url), href))
    if best is not None:
        return best[1]

    guid_el = item.find("guid")
    if guid_el is not None and (guid_el.get("isPermaLink") or "true").strip().lower() != "false":
        guid = _text(guid_el)
        if guid.startswith(("http://", "https://")):
            return guid
    return ""


def _entries(root: ET.Element, feed_url: str = "") -> list[tuple[str, str, str]]:
    """Return ``(id, title, link)`` for each item, RSS or Atom."""
    found: list[tuple[str, str, str]] = []
    parents = {child: parent for parent in root.iter() for child in parent}

    for item in root.iter():
        tag = item.tag.rsplit("}", 1)[-1]
        if tag not in {"item", "entry"}:
            continue
        title = _text(item.find("title")) or _text(item.find("{*}title"))
        link = _entry_link(item, parents, feed_url)
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
    parser.add_argument(
        "--limit", type=positive_int, default=10, help="Max entries to report per run"
    )
    parser.add_argument(
        "--first-run-reports",
        action="store_true",
        help="Report everything on the very first run instead of staying silent.",
    )
    args = parser.parse_args()

    try:
        target = require_http_url(args.url, "--url")
    except ValueError as exc:
        print(f"Refusing to fetch: {exc}", file=sys.stderr)
        return 1

    request = urllib.request.Request(  # noqa: S310 - http(s) only
        target, headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            body = response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"Feed fetch failed for {args.url}: {exc}", file=sys.stderr)
        return 1

    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        print(f"Feed is not valid XML: {exc}", file=sys.stderr)
        return 1

    entries = _entries(root, feed_url=target)
    by_id = {entry[0]: entry for entry in entries}
    fresh = select_new(
        args.name, list(by_id), first_run_reports=args.first_run_reports, limit=args.limit
    )
    if not fresh:
        return 0

    for guid in fresh:
        _, title, link = by_id[guid]
        print(f"- {title or guid}" + (f"\n  {link}" if link else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
