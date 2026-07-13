#!/usr/bin/env python3
"""History explorer: aggregate DecisionEntry.skills_invoked.

Produces co-occurrence data and router fixtures; emits JSON to stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Derive the agentos package root from this file's location.
# Path layout from explore.py:
#   .../agentos/skills/bundled/history-explorer/scripts/explore.py
# parents: [0]=scripts  [1]=history-explorer  [2]=bundled
#          [3]=skills    [4]=agentos
# Works for both source-tree checkouts and wheel installs (site-packages).
_AGENTOS_ROOT = Path(__file__).resolve().parents[4]
_BUNDLED = _AGENTOS_ROOT / "skills" / "bundled"

# Ensure agentos package is importable so we can share the aggregation
# logic with in-tree callers (tests, etc).
if str(_AGENTOS_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_AGENTOS_ROOT.parent))

from agentos.observability.decision_log_aggregate import (  # noqa: E402
    aggregate_co_occurrences,
)


def _expand_user_path(raw_path: str) -> Path:
    """Expand home-relative paths using testable env overrides on every OS."""
    if raw_path == "~" or raw_path.startswith(("~/", "~\\")):
        home = os.environ.get("HOME") or os.environ.get("USERPROFILE")
        if home:
            suffix = raw_path[1:].lstrip("/\\")
            return Path(home, suffix) if suffix else Path(home)
    return Path(raw_path).expanduser()


def _resolve_log_dir(cli_arg: str | None) -> Path:
    """Resolve --log-dir to an absolute Path with env-aware fallback.

    Order: CLI arg → AGENTOS_LOG_DIR → AGENTOS_STATE_DIR/logs →
    ~/.agentos/logs. Tilde always expanded; $ENVVAR-style references are
    NOT expanded (use the env-var overrides instead).

    N18 fix: skill_exec invokes create_subprocess_exec directly — no shell
    expansion — so a literal '~' in the SKILL.md entrypoint args stays
    literal. This resolver is called at runtime so the subprocess always
    lands on the real home-relative path regardless of how --log-dir was
    supplied (or omitted entirely).
    """
    if cli_arg:
        return _expand_user_path(cli_arg).resolve()
    env_log = os.environ.get("AGENTOS_LOG_DIR")
    if env_log:
        return _expand_user_path(env_log).resolve()
    env_state = os.environ.get("AGENTOS_STATE_DIR")
    if env_state:
        return (_expand_user_path(env_state) / "logs").resolve()
    return (Path.home() / ".agentos" / "logs").resolve()


def aggregate_router_fixtures(repo_root: Path | None = None) -> list[dict]:
    """Surface the D.2 router-fixture corpus."""
    if repo_root is None:
        # Derive the agentos package root from this file's location.
        # Path layout from explore.py:
        #   .../agentos/skills/bundled/history-explorer/scripts/explore.py
        # parents: [0]=scripts  [1]=history-explorer  [2]=bundled
        #          [3]=skills    [4]=agentos
        # Works for both source-tree checkouts and wheel installs.
        # In a source checkout: agentos_root.parent = src/, repo = src/../
        # In a wheel install: test fixtures are absent; the is_dir() guard
        # below returns [] gracefully.
        _agentos_root = Path(__file__).resolve().parents[4]
        repo_root = _agentos_root.parent.parent
    fixtures_dir = repo_root / "tests" / "test_skills" / "router_fixtures"
    fixtures: list[dict] = []
    if not fixtures_dir.is_dir():
        return fixtures
    for fixture_file in fixtures_dir.glob("*.py"):
        if fixture_file.name.startswith("_"):
            continue
        text = fixture_file.read_text(encoding="utf-8")
        if "expected_choice" not in text:
            continue
        fixtures.append({"fixture_file": fixture_file.name, "note": "see fixture file for details"})
    return fixtures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--log-dir",
        required=False,
        type=str,
        default=None,
        help=(
            "Decision-log directory (defaults to $AGENTOS_LOG_DIR, "
            "$AGENTOS_STATE_DIR/logs, or ~/.agentos/logs in that order). "
            "Tilde is expanded; $ENVVAR-style references are NOT expanded."
        ),
    )
    parser.add_argument("--query", required=True)
    parser.add_argument("--window-days", type=int, default=30)
    parser.add_argument("--include", default="co_occurrences,router_fixtures")
    parser.add_argument("--top-k", type=int, default=10)
    args = parser.parse_args(argv)

    log_dir = _resolve_log_dir(args.log_dir)
    include = set(args.include.split(","))
    result: dict = {"query": args.query}

    if "co_occurrences" in include:
        result["co_occurrences"] = aggregate_co_occurrences(
            log_dir, args.window_days, args.top_k
        )
    if "router_fixtures" in include:
        result["router_fixtures"] = aggregate_router_fixtures()

    if not result.get("co_occurrences"):
        result["placeholder"] = "no history available; downstream should rely on user intent only"

    json.dump(result, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
