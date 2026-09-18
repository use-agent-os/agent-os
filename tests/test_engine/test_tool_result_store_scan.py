"""``ToolResultStore.write`` must not walk the whole store on every call (#2126).

Expiry and budget accounting each used to run their own ``rglob`` plus a
``json.loads`` per record, on the turn loop, for every tool result. A write
now scans at most once; the expiry sweep runs on a timer, the budget check
consults a cached usage bound, and both share the one scan when it happens.
The engine builds a fresh store object per write, so that state is keyed by
root rather than held on the instance.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agentos.engine import tool_result_store as trs
from agentos.engine.tool_result_store import (
    TOOL_RESULT_META_NAME,
    ToolResultStore,
    ToolResultStoreBudgetError,
)


@pytest.fixture(autouse=True)
def _fresh_scan_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trs, "_scan_state_by_root", {})


@pytest.fixture
def scans(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count full-store walks; the list holds one entry per ``_iter_records`` call."""
    calls: list[int] = []
    original = ToolResultStore._iter_records

    def counting(self: ToolResultStore) -> list[trs._StoredMeta]:
        calls.append(1)
        return original(self)

    monkeypatch.setattr(ToolResultStore, "_iter_records", counting)
    return calls


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    clock = _Clock()
    monkeypatch.setattr(trs.time, "monotonic", clock)
    return clock


def _write(
    store: ToolResultStore,
    content: str,
    *,
    budget: int | None = 10_000,
    retention: int | None = 3600,
) -> trs.ToolResultRecord:
    return store.write(
        content,
        tool_use_id="tu",
        tool_name="x",
        session_id="s1",
        session_key="k",
        agent_id="a",
        disk_budget_bytes=budget,
        retention_seconds=retention,
    )


def _handles(root: Path) -> set[str]:
    return {p.parent.name for p in root.rglob(TOOL_RESULT_META_NAME)}


def _age_record(record_dir: Path, *, seconds: int) -> None:
    meta_path = record_dir / TOOL_RESULT_META_NAME
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    stamp = datetime.now(UTC) - timedelta(seconds=seconds)
    meta["created_at"] = stamp.isoformat().replace("+00:00", "Z")
    meta_path.write_text(json.dumps(meta), encoding="utf-8")


def test_first_write_scans_exactly_once_for_both_passes(tmp_path: Path, scans: list[int]) -> None:
    store = ToolResultStore(tmp_path)

    _write(store, "hello", budget=10_000, retention=3600)

    assert len(scans) == 1


def test_steady_state_writes_do_not_scan(tmp_path: Path, scans: list[int], clock: _Clock) -> None:
    store = ToolResultStore(tmp_path)
    _write(store, "first")
    scans.clear()

    for _ in range(25):
        _write(store, "x" * 100)

    assert scans == []


def test_expiry_sweep_runs_once_per_interval(
    tmp_path: Path, scans: list[int], clock: _Clock
) -> None:
    store = ToolResultStore(tmp_path, expiry_sweep_interval_seconds=60)
    old = _write(store, "old", retention=3600)
    _age_record(store._record_dir(old.handle, session_id="s1"), seconds=7200)
    scans.clear()

    clock.now += 59
    _write(store, "not yet")
    assert scans == []
    assert old.handle in _handles(tmp_path)

    clock.now += 1
    _write(store, "sweep now")
    assert len(scans) == 1
    assert old.handle not in _handles(tmp_path)


def test_expiry_sweep_state_survives_a_fresh_store_object(
    tmp_path: Path, scans: list[int], clock: _Clock
) -> None:
    """agent.py constructs ``ToolResultStore(...)`` anew on every write."""
    _write(ToolResultStore(tmp_path), "first")
    scans.clear()

    _write(ToolResultStore(tmp_path), "second")
    _write(ToolResultStore(tmp_path / "."), "third")

    assert scans == []


def test_budget_check_uses_the_cached_bound_until_it_would_overflow(
    tmp_path: Path, scans: list[int], clock: _Clock
) -> None:
    store = ToolResultStore(tmp_path)
    first = _write(store, "A" * 100, budget=250, retention=None)
    scans.clear()

    second = _write(store, "B" * 100, budget=250, retention=None)
    assert scans == [], "100 + 100 fits: no walk needed"

    third = _write(store, "C" * 100, budget=250, retention=None)
    assert len(scans) == 1, "the bound said 300 > 250, so one walk and a prune"

    handles = _handles(tmp_path)
    assert third.handle in handles
    assert second.handle in handles
    assert first.handle not in handles


def test_one_scan_serves_both_expiry_and_prune(
    tmp_path: Path, scans: list[int], clock: _Clock
) -> None:
    store = ToolResultStore(tmp_path, expiry_sweep_interval_seconds=0)
    stale = _write(store, "S" * 100, budget=250, retention=3600)
    _age_record(store._record_dir(stale.handle, session_id="s1"), seconds=7200)
    kept = _write(store, "K" * 100, budget=250, retention=3600)
    scans.clear()

    new = _write(store, "N" * 100, budget=250, retention=3600)

    assert len(scans) == 1
    handles = _handles(tmp_path)
    assert stale.handle not in handles, "expired on the shared scan"
    assert kept.handle in handles, "expiry freed enough room; nothing pruned"
    assert new.handle in handles


def test_external_deletion_only_costs_one_extra_scan(
    tmp_path: Path, scans: list[int], clock: _Clock
) -> None:
    """The cached bound may over-estimate, never under-estimate."""
    store = ToolResultStore(tmp_path)
    first = _write(store, "A" * 100, budget=250, retention=None)
    _write(store, "B" * 100, budget=250, retention=None)
    trs._remove_record_dir(store._record_dir(first.handle, session_id="s1"))
    scans.clear()

    third = _write(store, "C" * 100, budget=250, retention=None)
    assert len(scans) == 1, "bound said 300: walk, learn it is 100, no prune"
    assert _handles(tmp_path) >= {third.handle}
    assert len(_handles(tmp_path)) == 2

    _write(store, "D" * 40, budget=250, retention=None)
    assert len(scans) == 1, "bound is now exact (200 + 40 fits)"


def test_prune_leaves_headroom_so_the_next_writes_do_not_rescan(
    tmp_path: Path, scans: list[int], clock: _Clock
) -> None:
    store = ToolResultStore(tmp_path)
    budget = 16_000  # headroom = budget // 16 = 1000
    for _ in range(16):
        _write(store, "x" * 1000, budget=budget, retention=None)
    scans.clear()

    _write(store, "y" * 1000, budget=budget, retention=None)
    assert len(scans) == 1
    assert len(_handles(tmp_path)) == 15, "two evicted: one for the write, one for headroom"

    _write(store, "z" * 1000, budget=budget, retention=None)
    assert len(scans) == 1, "the headroom absorbed the next write without a walk"


def test_oversized_write_is_rejected_before_any_scan(tmp_path: Path, scans: list[int]) -> None:
    store = ToolResultStore(tmp_path)
    with pytest.raises(ToolResultStoreBudgetError, match="exceeds disk budget"):
        _write(store, "X" * 2000, budget=500, retention=None)
    assert scans == []


def test_no_budget_and_no_retention_never_scans(tmp_path: Path, scans: list[int]) -> None:
    store = ToolResultStore(tmp_path)
    for _ in range(3):
        _write(store, "x", budget=None, retention=None)
    assert scans == []
