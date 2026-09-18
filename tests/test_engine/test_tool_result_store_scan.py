"""``write`` must scan the store once, not once per maintenance pass.

``_iter_records`` walks the whole store and JSON-parses every record's
metadata. Expiry and budget pruning both need that data, and both used to
fetch it independently — two full-tree scans and two rounds of JSON parsing
per tool result, on the turn loop's hot path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agentos.engine.tool_result_store import ToolResultStore, ToolResultStoreBudgetError


def _write(
    store: ToolResultStore,
    content: str,
    *,
    budget=None,
    retention=None,
    expiry_interval_seconds=None,
):
    kwargs = {}
    if expiry_interval_seconds is not None:
        kwargs["expiry_interval_seconds"] = expiry_interval_seconds
    return store.write(
        content,
        tool_use_id="tu-1",
        tool_name="x",
        session_id="s1",
        session_key="k",
        agent_id="a",
        disk_budget_bytes=budget,
        retention_seconds=retention,
        **kwargs,
    )


def _count_scans(store: ToolResultStore, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls = [0]
    original = store._iter_records

    def counting():  # type: ignore[no-untyped-def]
        calls[0] += 1
        return original()

    monkeypatch.setattr(store, "_iter_records", counting)
    return calls


def test_write_scans_the_store_once_with_both_passes_active(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ToolResultStore(tmp_path / "tr")
    _write(store, "seed", budget=10_000, retention=3600)

    calls = _count_scans(store, monkeypatch)
    _write(store, "payload", budget=10_000, retention=3600)

    assert calls[0] == 1, f"expected a single scan per write, got {calls[0]}"


def test_write_does_not_scan_when_no_maintenance_is_configured(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No retention and no budget means there is nothing to scan for."""
    store = ToolResultStore(tmp_path / "tr")
    _write(store, "seed")

    calls = _count_scans(store, monkeypatch)
    _write(store, "payload")

    assert calls[0] == 0


def test_expiry_runs_on_a_timer_rather_than_every_write(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retention expiry is throttled on a timer, avoiding scans on every write."""
    store = ToolResultStore(tmp_path / "tr")
    # First write triggers initial expiry scan.
    _write(store, "seed", retention=3600)

    calls = _count_scans(store, monkeypatch)
    # Second write immediately after has timer unexpired and no budget: 0 scans.
    _write(store, "payload-1", retention=3600)
    assert calls[0] == 0, "expiry should not re-scan before the timer fires"

    # Forcing expiry (interval=0) triggers the scan.
    _write(store, "payload-2", retention=3600, expiry_interval_seconds=0)
    assert calls[0] == 1, "expiry should scan once timer fires"


def test_expiry_still_removes_records_past_retention(tmp_path) -> None:
    store = ToolResultStore(tmp_path / "tr")
    old = _write(store, "stale", retention=None)

    meta_path = store._record_dir(old.handle, session_id="s1") / "meta.json"
    stale_ts = (datetime.now(UTC) - timedelta(days=30)).isoformat().replace("+00:00", "Z")
    meta_path.write_text(
        meta_path.read_text(encoding="utf-8").replace(old.created_at, stale_ts),
        encoding="utf-8",
    )

    _write(store, "fresh", retention=3600)

    handles = {record.handle for record in store._iter_records()}
    assert old.handle not in handles, "record past retention should be gone"


def test_budget_pruning_still_makes_room(tmp_path) -> None:
    store = ToolResultStore(tmp_path / "tr")
    # Budget 500 with three 200-byte records: the third write needs room,
    # so the oldest is evicted. (At budget 600 all three fit and nothing
    # is pruned -- the margin has to be tight for this to exercise pruning.)
    first = _write(store, "x" * 200, budget=500)
    _write(store, "y" * 200, budget=500)
    _write(store, "z" * 200, budget=500)

    handles = {record.handle for record in store._iter_records()}
    assert first.handle not in handles, "oldest record should be pruned first"


def test_oversized_write_still_raises_before_deleting(tmp_path) -> None:
    """Preserved behaviour: raise before pruning, so nothing is lost silently."""
    store = ToolResultStore(tmp_path / "tr")
    keep = _write(store, "keeper", budget=5_000)

    with pytest.raises(ToolResultStoreBudgetError, match="exceeds disk budget"):
        _write(store, "z" * 900, budget=500)

    handles = {record.handle for record in store._iter_records()}
    assert keep.handle in handles
