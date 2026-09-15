"""Shared watermark store for cron watcher scripts.

A watcher runs every few minutes and must report only what is new since the
last run, so each one keeps a small JSON file of the ids it has already seen.
State lives under the AgentOS state root (``AGENTOS_STATE_DIR`` or
``~/.agentos``) in ``state/cron-watchers/<name>.json`` — never next to the
script, so the scripts directory stays read-only in practice.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

MAX_REMEMBERED_IDS = 500


def positive_int(raw: str) -> int:
    """argparse type for ``--limit``: a cap of 0 or less would stall the watermark."""
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected an integer, got {raw!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {value}")
    return value


def _state_root() -> Path:
    override = os.environ.get("AGENTOS_STATE_DIR", "").strip()
    if override:
        base = Path(override).expanduser()
    else:
        home = os.environ.get("HOME", "").strip()
        base = (Path(home) if home else Path.home()) / ".agentos"
    return base / "state" / "cron-watchers"


def watermark_path(name: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name) or "watcher"
    return _state_root() / f"{safe}.json"


def load_seen(name: str) -> list[str]:
    """Return the ids this watcher has already reported, oldest first."""
    try:
        raw = json.loads(watermark_path(name).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    seen = raw.get("seen") if isinstance(raw, dict) else None
    if not isinstance(seen, list):
        return []
    return [str(item) for item in seen]


def save_seen(name: str, ids: list[str]) -> None:
    """Persist the most recent ids, trimmed so the file cannot grow forever."""
    path = watermark_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    seen_ids: set[str] = set()
    deduped: list[str] = []
    for item in ids:
        val = str(item)
        if val not in seen_ids:
            seen_ids.add(val)
            deduped.append(val)
    trimmed = deduped[-MAX_REMEMBERED_IDS:]
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"seen": trimmed}), encoding="utf-8")
    tmp.replace(path)


def select_new(
    name: str,
    ids: list[str],
    *,
    first_run_reports: bool = False,
    limit: int | None = None,
) -> list[str]:
    """Return the ids to report this run and record only those as seen.

    ``limit`` caps how much one run reports, not how much the feed may
    publish: the surplus is left out of the watermark so the next run picks it
    up. Committing every fresh id while printing only the first ``limit`` lost
    the rest for good, silently, on exactly the busy feeds a watcher is for.
    Feeds list newest first, so the capped run takes the *tail* of the fresh
    ids: the backlog drains oldest first, in feed order, and what is deferred
    is the newest, which stays on the page longest. An id that leaves the page
    before its turn comes is still never reported -- the page is the only
    memory the watcher has of it.

    The first run reports nothing by default: a watcher that has never run has
    no idea which of the 50 items on the page are actually new, and dumping all
    of them into a chat is the wrong first impression. That silent run adopts
    the whole feed, so no backlog is left behind.
    """
    seen = load_seen(name)
    known = set(seen)
    is_first_run = not watermark_path(name).is_file()
    fresh_seen: set[str] = set()
    fresh: list[str] = []
    for item in ids:
        val = str(item)
        if val not in known and val not in fresh_seen:
            fresh_seen.add(val)
            fresh.append(val)
    if is_first_run and not first_run_reports:
        save_seen(name, [*seen, *fresh])
        return []
    reported = fresh if limit is None else fresh[-limit:]
    save_seen(name, [*seen, *reported])
    return reported
