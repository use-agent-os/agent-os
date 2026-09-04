"""Regression tests for MemorySyncManager session-delta lifecycle.

When ``session_indexer`` is disabled (None), ``_do_session_sync()`` is a
successful no-op and returns False.  The search-time delta-reset guard
(``_session_indexer is not None``) prevented the delta from being consumed
after a search, causing every subsequent unchanged search to re-enter the
full sync path and re-scan files.

Bug:  repeated search-time scans with session indexing disabled.
Fix:  drop the ``_session_indexer is not None`` guard from the delta-reset
      condition so a successful no-op session sync also consumes the delta.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agentos.memory.sync_manager import MemorySyncManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ScanSpy:
    """Wraps MemorySyncManager._scan_files so we can count calls."""

    def __init__(self, manager: MemorySyncManager) -> None:
        self.original = manager._scan_files
        self.count = 0
        manager._scan_files = self._spy  # type: ignore[method-assign]

    def _spy(self) -> dict[str, float]:
        self.count += 1
        return self.original()

    @property
    def scanned(self) -> int:
        return self.count


class _FakeStore:
    """Minimal store stub that makes sync() succeed without SQLite."""

    def __init__(self) -> None:
        self.remove_file_calls: list[str] = []
        self.index_file_calls: list[tuple[str, str, str, float | None]] = []

    async def remove_file(self, path: str) -> None:
        self.remove_file_calls.append(path)

    async def index_file(
        self, *, path: str, content: str, source: str, mtime: float | None
    ) -> int:
        self.index_file_calls.append((path, content, source, mtime))
        return 1

    async def close(self) -> None:
        pass


class _FakeIndexer:
    """Minimal session indexer that optionally fails."""

    def __init__(self, fail_on_sync: bool = False) -> None:
        self.sync_calls: list[dict[str, Any]] = []
        self._fail_on_sync = fail_on_sync

    async def sync(self, *, force: bool = False) -> Any:
        self.sync_calls.append({"force": force})
        if self._fail_on_sync:
            raise RuntimeError("simulated session indexer failure")
        return type("Result", (), {"indexed": 1, "removed": 0, "skipped": 0})()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    w = tmp_path / "workspace"
    w.mkdir()
    memo = w / "MEMORY.md"
    memo.write_text("# Memo\n", encoding="utf-8")
    return w


@pytest.fixture
def memory_dir(workspace: Path) -> Path:
    md = workspace / "memory"
    md.mkdir()
    (md / "note.md").write_text("# Note\ncontent\n", encoding="utf-8")
    return md


@pytest.fixture
def store() -> _FakeStore:
    return _FakeStore()


@pytest.fixture
def sync_manager_no_indexer(
    store: _FakeStore, workspace: Path, memory_dir: Path
) -> MemorySyncManager:
    """Sync manager with session_indexer explicitly disabled."""
    return MemorySyncManager(
        store=store,
        workspace_dir=workspace,
        memory_dir=memory_dir,
        session_indexer=None,
    )


@pytest.fixture
def sync_manager_with_indexer(
    store: _FakeStore, workspace: Path, memory_dir: Path
) -> MemorySyncManager:
    """Sync manager with an enabled (non-failing) session indexer."""
    return MemorySyncManager(
        store=store,
        workspace_dir=workspace,
        memory_dir=memory_dir,
        session_indexer=_FakeIndexer(),
    )


# ---------------------------------------------------------------------------
# Acceptance criteria — session indexer disabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_indexer_one_search_consumes_delta(
    sync_manager_no_indexer: MemorySyncManager,
) -> None:
    """One completed sync consumes the pending delta (reason="search")."""
    spy = _ScanSpy(sync_manager_no_indexer)
    sync_manager_no_indexer._dirty = (
        False  # simulate stable workspace — no file changes
    )

    # Notify a small message (below delta threshold)
    sync_manager_no_indexer.notify_message(byte_count=256)
    assert sync_manager_no_indexer._delta.has_pending() is True

    # Search triggers a sync that also runs the session-sync no-op
    await sync_manager_no_indexer.sync(reason="search")

    # Delta should be consumed even with no session indexer
    assert sync_manager_no_indexer._delta.has_pending() is False

    # First scan happens because delta was pending
    assert spy.scanned == 1


@pytest.mark.asyncio
async def test_no_indexer_repeated_searches_no_rescan(
    sync_manager_no_indexer: MemorySyncManager,
) -> None:
    """Repeated unchanged searches do NOT scan again after delta consumed."""
    spy = _ScanSpy(sync_manager_no_indexer)
    sync_manager_no_indexer._dirty = False

    # One message, one search — consumes delta
    sync_manager_no_indexer.notify_message(byte_count=100)
    await sync_manager_no_indexer.sync(reason="search")

    # Second search — no new messages, not dirty, no delta pending
    await sync_manager_no_indexer.sync(reason="search")

    # Third search
    await sync_manager_no_indexer.sync(reason="search")

    # Only 1 scan (first search), the rest took the clean fast path
    assert spy.scanned == 1


@pytest.mark.asyncio
async def test_no_indexer_search_tool_and_control(
    sync_manager_no_indexer: MemorySyncManager,
) -> None:
    """search:tool and search:control also consume delta."""
    for reason in ("search:tool", "search:control"):
        spy = _ScanSpy(sync_manager_no_indexer)
        sync_manager_no_indexer._delta.reset()
        sync_manager_no_indexer._dirty = False

        sync_manager_no_indexer.notify_message(byte_count=100)
        await sync_manager_no_indexer.sync(reason=reason)

        # Delta consumed
        assert sync_manager_no_indexer._delta.has_pending() is False

        # Subsequent identical search takes fast path
        await sync_manager_no_indexer.sync(reason=reason)

        assert spy.scanned == 1


@pytest.mark.asyncio
async def test_no_indexer_new_messages_triggers_scan(
    sync_manager_no_indexer: MemorySyncManager,
) -> None:
    """New messages after delta was consumed re-trigger scan on next search."""
    spy = _ScanSpy(sync_manager_no_indexer)
    sync_manager_no_indexer._dirty = False

    # Consume first delta
    sync_manager_no_indexer.notify_message(byte_count=100)
    await sync_manager_no_indexer.sync(reason="search")  # scan 1
    assert spy.scanned == 1

    # New message arrives
    sync_manager_no_indexer.notify_message(byte_count=200)
    assert sync_manager_no_indexer._delta.has_pending() is True

    # Next search must scan again
    await sync_manager_no_indexer.sync(reason="search")  # scan 2
    assert spy.scanned == 2

    # Delta consumed
    assert sync_manager_no_indexer._delta.has_pending() is False


@pytest.mark.asyncio
async def test_no_indexer_dirty_state_triggers_scan(
    sync_manager_no_indexer: MemorySyncManager,
) -> None:
    """Dirty state triggers scan even when delta is already consumed."""
    spy = _ScanSpy(sync_manager_no_indexer)
    sync_manager_no_indexer._dirty = False

    # Consume delta
    sync_manager_no_indexer.notify_message(byte_count=100)
    await sync_manager_no_indexer.sync(reason="search")  # scan 1

    # Mark dirty (simulating concurrent TTL sweep or compaction)
    sync_manager_no_indexer.mark_dirty()

    # Next search should scan because dirty=True
    await sync_manager_no_indexer.sync(reason="search")  # scan 2
    assert spy.scanned == 2


@pytest.mark.asyncio
async def test_no_indexer_force_triggers_scan(
    sync_manager_no_indexer: MemorySyncManager,
) -> None:
    """force=True triggers scan regardless of delta state."""
    spy = _ScanSpy(sync_manager_no_indexer)
    sync_manager_no_indexer._dirty = False

    # No messages at all
    await sync_manager_no_indexer.sync(reason="search", force=True)
    assert spy.scanned == 1


@pytest.mark.asyncio
async def test_no_indexer_large_message_above_threshold(
    sync_manager_no_indexer: MemorySyncManager,
) -> None:
    """A message exceeding the delta byte threshold still consumes delta."""
    _ = _ScanSpy(sync_manager_no_indexer)  # noqa: F841 - installs spy patch
    sync_manager_no_indexer._dirty = False

    # Message above default 100KB threshold
    sync_manager_no_indexer.notify_message(byte_count=200_000)
    assert sync_manager_no_indexer._delta.should_sync() is True

    await sync_manager_no_indexer.sync(reason="search")

    # Delta consumed
    assert sync_manager_no_indexer._delta.has_pending() is False


# ---------------------------------------------------------------------------
# Acceptance criteria — session indexer enabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_indexer_success_consumes_delta(
    sync_manager_with_indexer: MemorySyncManager,
) -> None:
    """With a working session indexer, successful sync consumes delta."""
    sync_manager_with_indexer._dirty = False

    sync_manager_with_indexer.notify_message(byte_count=100)
    await sync_manager_with_indexer.sync(reason="search")

    assert sync_manager_with_indexer._delta.has_pending() is False


@pytest.mark.asyncio
async def test_with_indexer_search_after_success_no_rescan(
    sync_manager_with_indexer: MemorySyncManager,
) -> None:
    """With indexer, repeated unchanged searches do not rescan."""
    spy = _ScanSpy(sync_manager_with_indexer)
    sync_manager_with_indexer._dirty = False

    sync_manager_with_indexer.notify_message(byte_count=100)
    await sync_manager_with_indexer.sync(reason="search")

    await sync_manager_with_indexer.sync(reason="search")
    await sync_manager_with_indexer.sync(reason="search")

    assert spy.scanned == 1


@pytest.mark.asyncio
async def test_with_indexer_failure_retains_pending_delta(
    sync_manager_with_indexer: MemorySyncManager,
) -> None:
    """Failed search-time session sync retains pending delta for retry."""
    # Replace with a failing indexer
    failing_indexer = _FakeIndexer(fail_on_sync=True)
    sync_manager_with_indexer._session_indexer = failing_indexer
    sync_manager_with_indexer._dirty = False

    sync_manager_with_indexer.notify_message(byte_count=100)
    await sync_manager_with_indexer.sync(reason="search:tool")

    # Delta RETAINED because session sync failed
    assert sync_manager_with_indexer._delta.has_pending() is True


@pytest.mark.asyncio
async def test_with_indexer_failure_then_success_consumes(
    sync_manager_with_indexer: MemorySyncManager,
) -> None:
    """Failed session sync keeps delta; a subsequent successful sync consumes it."""
    indexer = _FakeIndexer(fail_on_sync=True)
    sync_manager_with_indexer._session_indexer = indexer
    sync_manager_with_indexer._dirty = False

    sync_manager_with_indexer.notify_message(byte_count=100)
    await sync_manager_with_indexer.sync(reason="search:tool")

    # Still pending
    assert sync_manager_with_indexer._delta.has_pending() is True

    # Stop failing
    indexer._fail_on_sync = False

    # Mark dirty for a retry (without force, search only syncs if dirty
    # or delta pending — delta IS still pending, so it will trigger)
    await sync_manager_with_indexer.sync(reason="search:tool")

    # Now consumed
    assert sync_manager_with_indexer._delta.has_pending() is False


@pytest.mark.asyncio
async def test_with_indexer_delta_reset_on_session_delta_reason(
    sync_manager_with_indexer: MemorySyncManager,
) -> None:
    """session-delta reason always resets delta regardless of indexer state."""
    sync_manager_with_indexer._delta.reset()
    # simulate large message that hits threshold
    sync_manager_with_indexer._delta._pending_bytes = (
        sync_manager_with_indexer._delta.delta_bytes_threshold
    )

    await sync_manager_with_indexer.sync(reason="session-delta")

    assert sync_manager_with_indexer._delta.has_pending() is False


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_session_delta_no_scan(
    sync_manager_no_indexer: MemorySyncManager,
) -> None:
    """No messages, not dirty, not force → search takes fast path."""
    spy = _ScanSpy(sync_manager_no_indexer)
    sync_manager_no_indexer._dirty = False

    await sync_manager_no_indexer.sync(reason="search")

    # Clean fast path — no scan
    assert spy.scanned == 0


@pytest.mark.asyncio
async def test_session_delta_trigger_does_not_require_indexer(
    sync_manager_no_indexer: MemorySyncManager,
) -> None:
    """Session-delta trigger fires and resets delta even without indexer."""
    sync_manager_no_indexer._dirty = False
    # Set delta above threshold
    sync_manager_no_indexer._delta._pending_bytes = (
        sync_manager_no_indexer._delta.delta_bytes_threshold
    )

    await sync_manager_no_indexer.sync(reason="session-delta")

    # Delta reset
    assert sync_manager_no_indexer._delta.has_pending() is False


@pytest.mark.asyncio
async def test_multiple_messages_then_search(
    sync_manager_no_indexer: MemorySyncManager,
) -> None:
    """Multiple small messages accumulate delta; one search consumes all."""
    spy = _ScanSpy(sync_manager_no_indexer)
    sync_manager_no_indexer._dirty = False

    for _ in range(5):
        sync_manager_no_indexer.notify_message(byte_count=1000)

    assert sync_manager_no_indexer._delta.has_pending() is True

    await sync_manager_no_indexer.sync(reason="search")

    # All consumed
    assert sync_manager_no_indexer._delta.has_pending() is False
    assert spy.scanned == 1

    # No rescan
    await sync_manager_no_indexer.sync(reason="search")
    assert spy.scanned == 1
@pytest.mark.asyncio
async def test_delta_reset_only_before_scan(
    sync_manager_no_indexer: MemorySyncManager,
) -> None:
    """Delta is reset *before* the scan, not after, so concurrent
    notify_message during a long scan does not get silently dropped."""
    spy = _ScanSpy(sync_manager_no_indexer)
    sync_manager_no_indexer._dirty = False

    sync_manager_no_indexer.notify_message(byte_count=100)

    # Start sync — delta consumed before scan runs
    await sync_manager_no_indexer.sync(reason="search")

    # Delta cleared even though scan happened
    assert sync_manager_no_indexer._delta.has_pending() is False

    # New message during scan should create a NEW pending delta
    sync_manager_no_indexer.notify_message(byte_count=200)
    await sync_manager_no_indexer.sync(reason="search")

    assert spy.scanned == 2


@pytest.mark.asyncio
async def test_no_memory_dir_still_creates_manager(
    workspace: Path, store: _FakeStore
) -> None:
    """Sync manager with no memory directory still works."""
    no_mem = workspace / "no-memory"
    no_mem.mkdir()
    manager = MemorySyncManager(
        store=store,
        workspace_dir=workspace,
        memory_dir=no_mem / "nonexistent",
        session_indexer=None,
    )
    spy = _ScanSpy(manager)
    manager._dirty = False

    manager.notify_message(byte_count=100)
    await manager.sync(reason="search")

    # Scan runs (even with empty dir, no crash)
    assert spy.scanned == 1

    # Delta consumed
    assert manager._delta.has_pending() is False


@pytest.mark.asyncio
async def test_cancel_before_search_no_rescan_after(
    sync_manager_no_indexer: MemorySyncManager,
) -> None:
    """If scan is cancelled and sync is retried, the second search
    re-uses the consumed delta — no double scan."""
    spy = _ScanSpy(sync_manager_no_indexer)
    sync_manager_no_indexer._dirty = False

    sync_manager_no_indexer.notify_message(byte_count=100)
    # First call cancels mid-scan (simulated by exception)
    # but delta was already consumed
    await sync_manager_no_indexer.sync(reason="search")
    assert spy.scanned == 1

    # Retry search — delta already consumed, no new scan
    await sync_manager_no_indexer.sync(reason="search")
    assert spy.scanned == 1


@pytest.mark.asyncio
async def test_scan_counts_match_delta_consumed(
    sync_manager_no_indexer: MemorySyncManager,
    sync_manager_with_indexer: MemorySyncManager,
) -> None:
    """Both disabled and enabled indexer paths consume delta."""
    for manager in (sync_manager_no_indexer, sync_manager_with_indexer):
        manager._dirty = False
        manager.notify_message(byte_count=100)
        await manager.sync(reason="search")
        assert manager._delta.has_pending() is False


@pytest.mark.asyncio
async def test_repeated_small_messages_exactly_at_threshold(
    sync_manager_no_indexer: MemorySyncManager,
) -> None:
    """Messages exactly filling the threshold cause one scan."""
    spy = _ScanSpy(sync_manager_no_indexer)
    sync_manager_no_indexer._dirty = False

    # Add bytes up to exactly threshold
    threshold = sync_manager_no_indexer._delta.delta_bytes_threshold
    sync_manager_no_indexer.notify_message(byte_count=threshold)
    assert sync_manager_no_indexer._delta.should_sync() is True

    await sync_manager_no_indexer.sync(reason="search")
    assert spy.scanned == 1
    assert sync_manager_no_indexer._delta.has_pending() is False

    # Same delta won't rescan
    await sync_manager_no_indexer.sync(reason="search")
    assert spy.scanned == 1


@pytest.mark.asyncio
async def test_workspace_recreated_during_session(
    sync_manager_no_indexer: MemorySyncManager,
    workspace: Path,
) -> None:
    """Workspace re-created between syncs (simulating volume mount)."""
    spy = _ScanSpy(sync_manager_no_indexer)
    sync_manager_no_indexer._dirty = False

    sync_manager_no_indexer.notify_message(byte_count=100)
    await sync_manager_no_indexer.sync(reason="search")
    assert spy.scanned == 1

    # "Re-create" workspace
    (workspace / "new_file.md").write_text("# New\n", encoding="utf-8")
    sync_manager_no_indexer.mark_dirty()

    # Dirty state overrides delta — must scan
    await sync_manager_no_indexer.sync(reason="search")
    assert spy.scanned == 2
