"""Pre-upgrade snapshot of the small, critical AgentOS state; post-upgrade check.

Borrowed from the Hermes updater's two safety nets, adapted to the wheel-based
install here: an upgrade never rewrites ``~/.agentos`` itself, but the *new*
gateway does — config migration on load, schema migrations on the session,
scheduler and approval databases. A backup nobody verifies is not a backup, so
this module pairs the snapshot with a targeted post-restart verification
(``PRAGMA quick_check`` on every SQLite file) and a restore that puts the
snapshot back file for file.

What is captured (only when present, and only files under 1 GiB so a fat
session database can never stall an upgrade):

* ``config.toml``, ``auth.json``, ``skills-lock.json`` at the AgentOS home;
* every ``*.db`` / ``*.sqlite`` under the state directory, copied through the
  SQLite online-backup API so a WAL-mode database in use by the gateway still
  yields a consistent copy.

``.env`` is deliberately left out: it holds provider secrets, and a copy that
outlives the original is a liability, not a safety net.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from agentos.paths import default_agentos_home, state_dir

SNAPSHOT_PREFIX = "pre-upgrade-"
MANIFEST_NAME = "manifest.json"
DEFAULT_KEEP = 3
MAX_FILE_BYTES = 1 << 30  # 1 GiB
_HOME_FILES = ("config.toml", "auth.json", "skills-lock.json")
_DB_SUFFIXES = (".db", ".sqlite")


@dataclass(frozen=True)
class SnapshotEntry:
    kind: str  # "file" | "sqlite"
    source: str
    relative: str
    size: int


@dataclass
class SnapshotResult:
    path: Path
    entries: list[SnapshotEntry] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def to_payload(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "files": len(self.entries),
            "skipped": list(self.skipped),
        }


@dataclass
class DataCheck:
    ok: bool
    checked: list[str] = field(default_factory=list)
    problems: list[dict[str, str]] = field(default_factory=list)

    def to_payload(self) -> dict[str, object]:
        return {"ok": self.ok, "checked": list(self.checked), "problems": list(self.problems)}


def snapshots_root() -> Path:
    return state_dir("snapshots")


def _is_db(path: Path) -> bool:
    return path.suffix.lower() in _DB_SUFFIXES


def state_databases() -> list[Path]:
    """Every SQLite file under the state directory, snapshots excluded."""

    root = state_dir()
    if not root.is_dir():
        return []
    snapshots = snapshots_root()
    found: list[Path] = []
    for candidate in sorted(root.rglob("*")):
        if not candidate.is_file() or not _is_db(candidate):
            continue
        try:
            candidate.relative_to(snapshots)
            continue  # a copy inside an earlier snapshot
        except ValueError:
            pass
        found.append(candidate)
    return found


def _backup_sqlite(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def create_snapshot(*, version: str, keep: int = DEFAULT_KEEP) -> SnapshotResult:
    """Copy the critical state into a fresh ``snapshots/pre-upgrade-<utc>/``.

    Returns the snapshot even when nothing qualified (a brand-new install has
    no databases yet); callers report ``files == 0`` rather than failing.
    """

    home = default_agentos_home()
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    root = snapshots_root() / f"{SNAPSHOT_PREFIX}{stamp}"
    suffix = 1
    while root.exists():  # two upgrades in the same second
        root = snapshots_root() / f"{SNAPSHOT_PREFIX}{stamp}-{suffix}"
        suffix += 1
    root.mkdir(parents=True, exist_ok=False)
    result = SnapshotResult(path=root)

    candidates: list[tuple[str, Path, Path]] = []
    for name in _HOME_FILES:
        source = home / name
        if source.is_file():
            candidates.append(("file", source, Path("home") / name))
    state_root = state_dir()
    for db in state_databases():
        candidates.append(("sqlite", db, Path("state") / db.relative_to(state_root)))

    for kind, source, relative in candidates:
        try:
            size = source.stat().st_size
        except OSError:
            result.skipped.append(str(source))
            continue
        if size > MAX_FILE_BYTES:
            result.skipped.append(str(source))
            continue
        target = root / relative
        try:
            if kind == "sqlite":
                _backup_sqlite(source, target)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        except (OSError, sqlite3.Error):
            result.skipped.append(str(source))
            continue
        result.entries.append(
            SnapshotEntry(kind=kind, source=str(source), relative=str(relative), size=size)
        )

    manifest = {
        "created_at": time.time(),
        "version_before": version,
        "entries": [entry.__dict__ for entry in result.entries],
        "skipped": list(result.skipped),
    }
    (root / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    prune_snapshots(keep=keep)
    return result


def list_snapshots() -> list[Path]:
    """Snapshot directories, oldest first."""

    root = snapshots_root()
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and p.name.startswith(SNAPSHOT_PREFIX))


def prune_snapshots(*, keep: int = DEFAULT_KEEP) -> list[Path]:
    """Delete all but the newest ``keep`` snapshots; returns what was removed."""

    removed: list[Path] = []
    snapshots = list_snapshots()
    excess = max(0, len(snapshots) - max(keep, 0))
    for old in snapshots[:excess]:
        shutil.rmtree(old, ignore_errors=True)
        removed.append(old)
    return removed


def latest_snapshot() -> Path | None:
    snapshots = list_snapshots()
    return snapshots[-1] if snapshots else None


def verify_state() -> DataCheck:
    """``PRAGMA quick_check`` every state database, read-only.

    Safe to run while the gateway is up: a WAL-mode database admits readers
    alongside its writer. A database that cannot be opened at all counts as a
    problem, since that is exactly the corruption a bad migration leaves.
    """

    check = DataCheck(ok=True)
    for db in state_databases():
        check.checked.append(str(db))
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                rows = conn.execute("PRAGMA quick_check").fetchall()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            check.ok = False
            check.problems.append({"path": str(db), "result": str(exc)})
            continue
        verdict = ";".join(str(r[0]) for r in rows) if rows else "no result"
        if verdict != "ok":
            check.ok = False
            check.problems.append({"path": str(db), "result": verdict})
    return check


def read_manifest(snapshot: Path) -> dict[str, object]:
    path = snapshot / MANIFEST_NAME
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: manifest is not an object")
    return data


def restore_snapshot(snapshot: Path) -> list[Path]:
    """Put every file in ``snapshot`` back where it came from.

    The gateway must be stopped: a live WAL-mode database would silently merge
    the restored file with its pending journal. The caller checks that; this
    function only moves files. Returns the paths written.
    """

    manifest = read_manifest(snapshot)
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise ValueError(f"{snapshot / MANIFEST_NAME}: no entries")
    restored: list[Path] = []
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        relative = raw.get("relative")
        source = raw.get("source")
        if not isinstance(relative, str) or not isinstance(source, str):
            continue
        copy = snapshot / relative
        if not copy.is_file():
            raise FileNotFoundError(f"snapshot is missing {copy}")
        target = Path(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        if raw.get("kind") == "sqlite":
            # A stale journal from the corrupted database would be replayed
            # over the restored copy on first open; drop both sidecars first.
            for sidecar in (f"{target}-wal", f"{target}-shm"):
                Path(sidecar).unlink(missing_ok=True)
        shutil.copy2(copy, target)
        restored.append(target)
    return restored
