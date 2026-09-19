#!/usr/bin/env python3
"""Report new GitHub issues, pull requests, or releases for a repository.

Built for an AgentOS cron script job:

    agentos cron add --every 10m --name repo-issues \\
      --script watch_github.py \\
      --script-arg --repo --script-arg owner/name \\
      --script-arg --scope --script-arg issues

Reads ``GITHUB_TOKEN`` from the environment when present, which raises the
rate limit and reaches private repositories. Prints one line per new item and
nothing when there is nothing new.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# Bundled scripts run under AgentOS's own interpreter; the path insert only
# matters in a source checkout where the package is not installed (#2804).
_SRC_ROOT = str(Path(__file__).resolve().parents[5])
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)
from agentos.skills.stdio import configure_utf8_stdio  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))

from _watermark import positive_int, select_new  # noqa: E402

API_ROOT = "https://api.github.com"
USER_AGENT = "AgentOS-cron-watcher/1.0"
SCOPES = ("issues", "pulls", "releases")


def _fetch(url: str) -> Any:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def _describe(scope: str, item: dict[str, Any]) -> tuple[str, str]:
    """Return ``(id, line)`` for one API item."""
    if scope == "releases":
        identifier = str(item.get("id") or item.get("tag_name") or "")
        name = item.get("name") or item.get("tag_name") or identifier
        return identifier, f"- release {name}\n  {item.get('html_url', '')}"
    number = item.get("number")
    identifier = str(number if number is not None else item.get("id") or "")
    label = "PR" if scope == "pulls" else "issue"
    title = item.get("title") or identifier
    author = (item.get("user") or {}).get("login", "?")
    return identifier, f"- {label} #{number} {title} (by {author})\n  {item.get('html_url', '')}"


def main() -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--scope", default="issues", choices=SCOPES, help="What to watch")
    parser.add_argument("--name", default="", help="Watermark name (default: repo+scope)")
    parser.add_argument(
        "--limit", type=positive_int, default=10, help="Max items to report per run"
    )
    parser.add_argument(
        "--first-run-reports",
        action="store_true",
        help="Report everything on the very first run instead of staying silent.",
    )
    args = parser.parse_args()

    if "/" not in args.repo:
        print("--repo must look like owner/name", file=sys.stderr)
        return 1

    watermark = args.name or f"github-{args.repo.replace('/', '-')}-{args.scope}"
    url = f"{API_ROOT}/repos/{args.repo}/{args.scope}?per_page=30"
    if args.scope == "issues":
        url += "&state=open&sort=created&direction=desc"

    try:
        payload = _fetch(url)
    except urllib.error.HTTPError as exc:
        print(f"GitHub returned {exc.code} for {args.repo}: {exc.reason}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"GitHub request failed: {exc}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"GitHub response was not JSON: {exc}", file=sys.stderr)
        return 1

    if not isinstance(payload, list):
        print("Unexpected GitHub response shape", file=sys.stderr)
        return 1

    lines: dict[str, str] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        # The issues endpoint also returns PRs; keep the two watchers distinct.
        if args.scope == "issues" and item.get("pull_request"):
            continue
        identifier, line = _describe(args.scope, item)
        if identifier:
            lines[identifier] = line

    fresh = select_new(
        watermark, list(lines), first_run_reports=args.first_run_reports, limit=args.limit
    )
    if not fresh:
        return 0

    for identifier in fresh:
        print(lines[identifier])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
