"""Pre-upgrade snapshot: capture, prune, verify, restore."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agentos.cli import upgrade_snapshot


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "agentos-home"
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(root))
    (root / "state").mkdir(parents=True)
    return root


def _make_db(path: Path, rows: int = 3) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.executemany("INSERT INTO t (v) VALUES (?)", [(f"row{i}",) for i in range(rows)])
        conn.commit()
    finally:
        conn.close()


def _row_count(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute("SELECT count(*) FROM t").fetchone()[0])
    finally:
        conn.close()


def test_snapshot_captures_config_and_databases(home: Path) -> None:
    (home / "config.toml").write_text("[gateway]\nport = 1\n", encoding="utf-8")
    (home / "auth.json").write_text("{}", encoding="utf-8")
    (home / ".env").write_text("SECRET=1\n", encoding="utf-8")
    _make_db(home / "state" / "sessions.db")
    (home / "state" / "agents" / "main").mkdir(parents=True)
    _make_db(home / "state" / "agents" / "main" / "memory.sqlite")

    snap = upgrade_snapshot.create_snapshot(version="1.0.0")

    relatives = sorted(e.relative for e in snap.entries)
    assert relatives == [
        "home/auth.json",
        "home/config.toml",
        "state/agents/main/memory.sqlite",
        "state/sessions.db",
    ]
    assert not (snap.path / "home" / ".env").exists(), ".env holds secrets and is never copied"
    assert _row_count(snap.path / "state" / "sessions.db") == 3
    manifest = json.loads((snap.path / upgrade_snapshot.MANIFEST_NAME).read_text())
    assert manifest["version_before"] == "1.0.0"
    assert len(manifest["entries"]) == 4
    assert snap.to_payload()["files"] == 4


def test_snapshot_skips_oversized_files(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (home / "config.toml").write_text("x" * 64, encoding="utf-8")
    monkeypatch.setattr(upgrade_snapshot, "MAX_FILE_BYTES", 16)
    snap = upgrade_snapshot.create_snapshot(version="1.0.0")
    assert snap.entries == []
    assert snap.skipped == [str(home / "config.toml")]


def test_snapshot_ignores_earlier_snapshots(home: Path) -> None:
    _make_db(home / "state" / "sessions.db")
    first = upgrade_snapshot.create_snapshot(version="1.0.0")
    second = upgrade_snapshot.create_snapshot(version="1.0.0")
    assert first.path != second.path
    # The copy inside the first snapshot must not be snapshotted again.
    assert [e.relative for e in second.entries] == ["state/sessions.db"]


def test_prune_keeps_newest(home: Path) -> None:
    paths = [upgrade_snapshot.create_snapshot(version="1", keep=99).path for _ in range(5)]
    removed = upgrade_snapshot.prune_snapshots(keep=2)
    assert removed == paths[:3]
    assert upgrade_snapshot.list_snapshots() == paths[3:]
    assert upgrade_snapshot.latest_snapshot() == paths[-1]


def test_verify_state_passes_on_healthy_databases(home: Path) -> None:
    _make_db(home / "state" / "sessions.db")
    check = upgrade_snapshot.verify_state()
    assert check.ok is True
    assert check.checked == [str(home / "state" / "sessions.db")]
    assert check.problems == []


def test_verify_state_flags_a_corrupt_database(home: Path) -> None:
    bad = home / "state" / "scheduler.db"
    bad.write_bytes(b"this is not a sqlite file" * 40)
    check = upgrade_snapshot.verify_state()
    assert check.ok is False
    assert check.problems and check.problems[0]["path"] == str(bad)


def test_restore_puts_files_back_and_drops_stale_journals(home: Path) -> None:
    config = home / "config.toml"
    config.write_text("good = 1\n", encoding="utf-8")
    db = home / "state" / "sessions.db"
    _make_db(db, rows=5)
    snap = upgrade_snapshot.create_snapshot(version="1.0.0")

    # A "bad migration": config rewritten, database trashed, journal left behind.
    config.write_text("bad = 1\n", encoding="utf-8")
    db.write_bytes(b"garbage")
    Path(f"{db}-wal").write_bytes(b"stale")
    Path(f"{db}-shm").write_bytes(b"stale")

    restored = upgrade_snapshot.restore_snapshot(snap.path)

    assert sorted(restored) == sorted([config, db])
    assert config.read_text(encoding="utf-8") == "good = 1\n"
    assert not Path(f"{db}-wal").exists()
    assert not Path(f"{db}-shm").exists()
    assert _row_count(db) == 5
    assert upgrade_snapshot.verify_state().ok is True


def test_restore_refuses_a_broken_snapshot(home: Path) -> None:
    (home / "config.toml").write_text("x", encoding="utf-8")
    snap = upgrade_snapshot.create_snapshot(version="1.0.0")
    (snap.path / "home" / "config.toml").unlink()
    with pytest.raises(FileNotFoundError):
        upgrade_snapshot.restore_snapshot(snap.path)
