"""Tests for the V016 pinned-sessions migration."""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
from unittest.mock import patch


def _load_migration():
    path = Path(__file__).resolve().parents[2] / "migrations" / "V016__session_pinned_at.py"
    spec = importlib.util.spec_from_file_location("v016_session_pinned_at", path)
    assert spec is not None
    assert spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    with patch("yoyo.step", lambda apply, rollback: (apply, rollback)):
        spec.loader.exec_module(migration)
    return migration


def test_v016_adds_the_pin_column_idempotently() -> None:
    migration = _load_migration()
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE sessions (session_key TEXT PRIMARY KEY, agent_id TEXT)")
        conn.execute(
            "INSERT INTO sessions (session_key, agent_id) VALUES (?, ?)",
            ("agent:main:legacy", "main"),
        )

        migration.apply_step(conn)
        migration.apply_step(conn)

        columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        assert "pinned_at" in columns
        # Existing rows are not pinned — that IS the data migration.
        row = conn.execute("SELECT pinned_at FROM sessions").fetchone()
        assert row == (None,)
    finally:
        conn.close()


def test_v016_is_a_no_op_without_a_sessions_table() -> None:
    migration = _load_migration()
    conn = sqlite3.connect(":memory:")
    try:
        migration.apply_step(conn)
        migration.rollback_step(conn)
    finally:
        conn.close()
