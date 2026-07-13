"""Tests for the session cost source rollup migration."""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
from unittest.mock import patch


def test_v007_adds_cost_source_columns_and_backfills_legacy_estimate() -> None:
    migration_path = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "V007__session_cost_source_rollup.py"
    )
    spec = importlib.util.spec_from_file_location("v007_cost_source", migration_path)
    assert spec is not None
    assert spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    with patch("yoyo.step", lambda apply, rollback: (apply, rollback)):
        spec.loader.exec_module(migration)
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(
            """
            CREATE TABLE sessions (
                session_key TEXT PRIMARY KEY,
                estimated_cost_usd REAL NOT NULL DEFAULT 0.0
            )
            """
        )
        conn.execute(
            "INSERT INTO sessions (session_key, estimated_cost_usd) VALUES (?, ?)",
            ("agent:main:legacy", 0.42),
        )

        migration.apply_step(conn)
        migration.apply_step(conn)

        columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        assert {
            "total_cost_usd",
            "billed_cost_usd",
            "estimated_cost_component_usd",
            "cost_source",
            "missing_cost_entries",
        }.issubset(columns)

        row = conn.execute(
            """
            SELECT
                total_cost_usd,
                billed_cost_usd,
                estimated_cost_component_usd,
                cost_source,
                missing_cost_entries
            FROM sessions
            WHERE session_key = ?
            """,
            ("agent:main:legacy",),
        ).fetchone()
        assert row == (0.42, 0.0, 0.42, "agentos_estimate", 0)
    finally:
        conn.close()
