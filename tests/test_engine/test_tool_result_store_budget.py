"""Regression tests for tool_result_store budget enforcement."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from agentos.engine.tool_result_store import (
    ToolResultStore,
    ToolResultStoreBudgetError,
)


class TestPruneToFitDataLoss:
    """Ensure oversized writes do not destroy existing records.

    Regression for: an incoming payload larger than the total budget would
    cause _prune_to_fit to delete ALL existing records before raising the
    budget-exceeded error. The caller only logged a "skipped" metric,
    unaware that unrelated prior results had already been destroyed.
    """

    def test_oversized_payload_does_not_destroy_existing_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ToolResultStore(tmpdir)
            # Write three small records within budget.
            for i in range(3):
                store.write(
                    f"record-{i}",
                    tool_use_id=f"tu-{i}",
                    tool_name="x",
                    session_id="s1",
                    session_key="k",
                    agent_id="a",
                    disk_budget_bytes=500,
                    retention_seconds=None,
                )
            existing = list(Path(tmpdir).rglob("content.txt"))

            # Attempt an oversized write (2 KB against a 500-byte budget).
            # Must raise WITHOUT deleting the three prior records.
            with pytest.raises(ToolResultStoreBudgetError):
                store.write(
                    "X" * 2000,
                    tool_use_id="tu-big",
                    tool_name="x",
                    session_id="s1",
                    session_key="k",
                    agent_id="a",
                    disk_budget_bytes=500,
                    retention_seconds=None,
                )

            remaining = list(Path(tmpdir).rglob("content.txt"))
            assert len(remaining) == len(existing), (
                f"expected {len(existing)} records preserved, got {len(remaining)}"
            )

    def test_normal_payload_within_budget_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ToolResultStore(tmpdir)
            store.write(
                "hello",
                tool_use_id="tu-1",
                tool_name="x",
                session_id="s1",
                session_key="k",
                agent_id="a",
                disk_budget_bytes=500,
                retention_seconds=None,
            )
            assert len(list(Path(tmpdir).rglob("content.txt"))) == 1
