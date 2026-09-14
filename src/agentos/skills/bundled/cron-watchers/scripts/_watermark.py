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


def _read_seen(name: str) -> list[str] | None:
    """Return the recorded ids, or ``None`` when this watcher has no state yet.

    The distinction is what separates "never ran" from "ran, and the feed was
    empty" -- an ordinary state for a newly created repo or a drained queue,
    and one an empty ``seen`` list cannot express on its own. A file that is
    missing, unreadable, or not the shape we write counts as never ran: with no
    usable record of what was already reported, adopting the feed quietly beats
    dumping all of it into a chat.
    """
    try:
        raw = json.loads(watermark_path(name).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    seen = raw.get("seen") if isinstance(raw, dict) else None
    if not isinstance(seen, list):
        return None
    return [str(item) for item in seen]


def load_seen(name: str) -> list[str]:
    """Return the ids this watcher has already reported, oldest first."""
    return _read_seen(name) or []


def save_seen(name: str, ids: list[str]) -> None:
    """Persist the most recent ids, trimmed so the file cannot grow forever.

    Duplicates are collapsed on the way in, keeping the first occurrence. The
    trim keeps the newest ``MAX_REMEMBERED_IDS``, so a repeated id that was
    allowed to occupy several slots would shorten the watcher's real memory and
    let an older id fall off the end sooner than it should.
    """
    path = watermark_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    unique: dict[str, None] = {}
    for item in ids:
        unique.setdefault(str(item), None)
    trimmed = list(unique)[-MAX_REMEMBERED_IDS:]
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
    the whole feed, so no backlog is left behind. Whether a run is the first is
    decided by whether the watermark file exists, not by whether it lists any
    ids: a watcher pointed at a feed that happens to be empty records an empty
    file, and reading that back as "never ran" swallowed the first real item to
    arrive -- permanently, since the next run does see state (Issue #1946).

    Ids repeated within one poll are reported once and recorded once, so a feed
    that lists the same entry twice neither doubles up in the chat nor spends
    two slots of the remembered-ids budget.
    """
    recorded = _read_seen(name)
    is_first_run = recorded is None
    seen = recorded or []
    known = set(seen)
    fresh: list[str] = []
    for item in ids:
        if item in known:
            continue
        known.add(item)
        fresh.append(item)
    if is_first_run and not first_run_reports:
        save_seen(name, [*seen, *fresh])
        return []
    reported = fresh if limit is None else fresh[-limit:]
    save_seen(name, [*seen, *reported])
    return reported
