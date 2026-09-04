from __future__ import annotations

from pathlib import Path

import pytest

from agentos.engine.tool_result_store import ToolResultStore, ToolResultStoreBudgetError


def test_prune_to_fit_preserves_existing_on_oversized_write(tmp_path: Path) -> None:
    """An oversized write must not delete existing records."""
    store = ToolResultStore(str(tmp_path))
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

    before = len(list(tmp_path.rglob("*meta.json")))
    assert before == 3, f"expected 3 records before oversized write, got {before}"

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

    after = len(list(tmp_path.rglob("*meta.json")))
    assert after == 3, f"expected 3 records preserved after oversized write, got {after}"


def test_prune_to_fit_frees_space_for_borderline_write(tmp_path: Path) -> None:
    """A write that fits after pruning one record should succeed."""
    store = ToolResultStore(str(tmp_path))
    for i in range(3):
        store.write(
            f"record-{i}",
            tool_use_id=f"tu-{i}",
            tool_name="x",
            session_id="s1",
            session_key="k",
            agent_id="a",
            disk_budget_bytes=200,
            retention_seconds=None,
        )

    before = len(list(tmp_path.rglob("*meta.json")))
    assert before == 3

    # Write a ~100-byte record; budget=200, current=3 small records may
    # exceed budget, so pruning should drop the oldest and succeed.
    result = store.write(
        "medium payload that is about one hundred bytes long",
        tool_use_id="tu-new",
        tool_name="x",
        session_id="s1",
        session_key="k",
        agent_id="a",
        disk_budget_bytes=200,
        retention_seconds=None,
    )
    assert result is not None
    assert result.tool_use_id == "tu-new"


def test_small_write_within_budget_no_pruning(tmp_path: Path) -> None:
    """A write well within budget should not prune anything."""
    store = ToolResultStore(str(tmp_path))
    for i in range(3):
        store.write(
            f"tiny-{i}",
            tool_use_id=f"tu-{i}",
            tool_name="x",
            session_id="s1",
            session_key="k",
            agent_id="a",
            disk_budget_bytes=10_000,
            retention_seconds=None,
        )

    before = len(list(tmp_path.rglob("*meta.json")))
    store.write(
        "another tiny record",
        tool_use_id="tu-extra",
        tool_name="x",
        session_id="s1",
        session_key="k",
        agent_id="a",
        disk_budget_bytes=10_000,
        retention_seconds=None,
    )
    after = len(list(tmp_path.rglob("*meta.json")))
    assert after == before + 1
