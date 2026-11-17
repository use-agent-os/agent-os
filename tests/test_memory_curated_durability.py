"""CuratedMemoryStore durability guards — hermes parity for the write path.

Each test here pins a failure mode that silently destroyed or froze curated
memory before: an unreadable file being treated as an empty one, a drift check
that re-read the file and raced with it, a rename that ate symlinks, a failure
counter that never expired, and a success payload that invited the model to
redo the write it had just made.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from agentos.memory.curated import (
    _CONSOLIDATION_FAILURE_WINDOW_S,
    _MAX_CONSOLIDATION_FAILURES_PER_TURN,
    ENTRY_DELIMITER,
    CuratedMemoryStore,
)


@pytest.fixture()
def store(tmp_path: Path) -> CuratedMemoryStore:
    s = CuratedMemoryStore(memory_dir=tmp_path, memory_char_limit=200, user_char_limit=100)
    s.load_from_disk()
    return s


# -- unreadable != empty ---------------------------------------------------


@pytest.mark.parametrize("action", ["add", "replace", "remove", "batch"])
def test_write_refuses_when_existing_file_is_unreadable(
    store: CuratedMemoryStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
):
    """An unreadable MEMORY.md must abort the write, never overwrite it.

    Treating a failed read as "no entries" and then flushing is what turned a
    transient lock/permission blip into total memory loss.
    """
    store.add("memory", "fact A")
    store.add("memory", "fact B")
    original = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")

    def boom(*_args, **_kwargs):
        raise OSError(errno.EACCES, "permission denied")

    monkeypatch.setattr(Path, "read_text", boom)

    if action == "add":
        result = store.add("memory", "fact C")
    elif action == "replace":
        result = store.replace("memory", "fact A", "fact A2")
    elif action == "remove":
        result = store.remove("memory", "fact A")
    else:
        result = store.apply_batch("memory", [{"action": "add", "content": "fact C"}])

    assert result["success"] is False
    assert "could not be read" in result["error"]
    monkeypatch.undo()
    # The decisive assertion: disk still holds both original entries.
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == original


def test_write_refuses_on_corrupt_utf8(store: CuratedMemoryStore, tmp_path: Path):
    """Invalid UTF-8 is a failed read, not an empty store."""
    store.add("memory", "fact A")
    (tmp_path / "MEMORY.md").write_bytes(b"\xff\xfe invalid utf-8 \xff")
    raw_before = (tmp_path / "MEMORY.md").read_bytes()

    result = store.add("memory", "fact B")

    assert result["success"] is False
    assert "could not be read" in result["error"]
    assert (tmp_path / "MEMORY.md").read_bytes() == raw_before


def test_read_failure_is_terminal_so_the_model_stops_retrying(
    store: CuratedMemoryStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store.add("memory", "fact A")

    def boom(*_args, **_kwargs):
        raise OSError(errno.EIO, "io error")

    monkeypatch.setattr(Path, "read_text", boom)
    result = store.add("memory", "fact B")

    assert result["done"] is True


def test_missing_file_is_still_a_normal_write(tmp_path: Path):
    """A genuinely absent file is a known state — writes must proceed."""
    s = CuratedMemoryStore(memory_dir=tmp_path, memory_char_limit=200, user_char_limit=100)
    s.load_from_disk()
    (tmp_path / "MEMORY.md").unlink(missing_ok=True)

    assert s.add("memory", "first fact")["success"] is True
    assert "first fact" in (tmp_path / "MEMORY.md").read_text(encoding="utf-8")


# -- drift detection reads the file exactly once ---------------------------


def test_drift_check_and_parse_see_the_same_bytes(
    store: CuratedMemoryStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The drift check must not re-read the file behind the parse's back.

    Two reads leave a window where an external writer lands between them, so
    the store would validate one version of the file and then write over a
    different one.
    """
    store.add("memory", "fact A")
    path = tmp_path / "MEMORY.md"

    reads: list[str] = []
    real_read_text = Path.read_text

    def counting_read(self, *args, **kwargs):
        data = real_read_text(self, *args, **kwargs)
        if os.path.samefile(self, path) if self.exists() else False:
            reads.append(data)
        return data

    monkeypatch.setattr(Path, "read_text", counting_read)
    store.replace("memory", "fact A", "fact A revised")

    assert len(reads) == 1, f"MEMORY.md was read {len(reads)}x during one write"


def test_drift_still_detected_and_backed_up(store: CuratedMemoryStore, tmp_path: Path):
    """Passing raw through must not weaken the actual drift detection."""
    store.add("memory", "fact A")
    path = tmp_path / "MEMORY.md"
    path.write_text("fact A" + ENTRY_DELIMITER + "z" * 500, encoding="utf-8")

    result = store.replace("memory", "fact A", "fact A revised")

    assert result["success"] is False
    assert "drift_backup" in result
    assert Path(result["drift_backup"]).exists()


# -- atomic replace preserves symlinks -------------------------------------


def test_write_through_symlink_keeps_the_symlink(tmp_path: Path):
    """Deployments symlink MEMORY.md into dotfiles; a write must not detach it."""
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    real_file = real_dir / "MEMORY-real.md"
    real_file.write_text("", encoding="utf-8")

    workspace = tmp_path / "ws"
    workspace.mkdir()
    link = workspace / "MEMORY.md"
    link.symlink_to(real_file)

    s = CuratedMemoryStore(memory_dir=workspace, memory_char_limit=200, user_char_limit=100)
    s.load_from_disk()
    assert s.add("memory", "persisted fact")["success"] is True

    assert link.is_symlink(), "symlink was replaced by a regular file"
    assert "persisted fact" in real_file.read_text(encoding="utf-8")


# -- lock follows symlinks to the real file --------------------------------


def _two_workspaces_sharing(tmp_path: Path) -> tuple[Path, Path, Path]:
    real_dir = tmp_path / "shared"
    real_dir.mkdir()
    real_file = real_dir / "MEMORY.md"
    real_file.write_text("", encoding="utf-8")
    workspaces = []
    for name in ("ws-a", "ws-b"):
        ws = tmp_path / name
        ws.mkdir()
        (ws / "MEMORY.md").symlink_to(real_file)
        workspaces.append(ws)
    return workspaces[0], workspaces[1], real_file


@pytest.mark.skipif(os.name != "posix", reason="flock + unprivileged symlinks are POSIX-only")
def test_symlinked_stores_contend_for_one_lock(tmp_path: Path):
    """Two workspaces whose MEMORY.md points at one file must share its lock.

    Locking beside the symlink gave each workspace its own lock file, so their
    read-modify-writes of the same real file could interleave and lose one.
    """
    import fcntl

    ws_a, ws_b, real_file = _two_workspaces_sharing(tmp_path)

    with CuratedMemoryStore._file_lock(ws_a / "MEMORY.md"):
        assert (real_file.parent / "MEMORY.md.lock").exists()
        assert not (ws_a / "MEMORY.md.lock").exists()
        other = CuratedMemoryStore._open_lock_file(ws_b / "MEMORY.md")
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            other.close()


@pytest.mark.skipif(os.name != "posix", reason="unprivileged symlinks are POSIX-only")
def test_symlinked_stores_both_writes_land(tmp_path: Path):
    ws_a, ws_b, real_file = _two_workspaces_sharing(tmp_path)
    a = CuratedMemoryStore(memory_dir=ws_a, memory_char_limit=200, user_char_limit=100)
    b = CuratedMemoryStore(memory_dir=ws_b, memory_char_limit=200, user_char_limit=100)
    a.load_from_disk()
    b.load_from_disk()

    assert a.add("memory", "from a")["success"] is True
    assert b.add("memory", "from b")["success"] is True

    text = real_file.read_text(encoding="utf-8")
    assert "from a" in text and "from b" in text


@pytest.mark.skipif(os.name != "posix", reason="unprivileged symlinks are POSIX-only")
def test_lock_falls_back_beside_the_link_when_real_dir_is_unusable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    ws_a, _ws_b, real_file = _two_workspaces_sharing(tmp_path)
    real_lock = real_file.with_suffix(".md.lock")
    builtin_open = open

    def refuse_real_lock(file, *args, **kwargs):
        if Path(file) == real_lock:
            raise PermissionError(errno.EACCES, "read-only")
        return builtin_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", refuse_real_lock)
    with CuratedMemoryStore._file_lock(ws_a / "MEMORY.md"):
        assert (ws_a / "MEMORY.md.lock").exists()


def test_cross_device_rename_falls_back_to_copy(
    store: CuratedMemoryStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """EXDEV/EBUSY must degrade to copy, not raise and lose the write."""
    calls = {"n": 0}
    real_replace = os.replace

    def flaky_replace(src, dst, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(errno.EXDEV, "cross-device link")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", flaky_replace)
    result = store.add("memory", "durable fact")

    assert result["success"] is True
    assert "durable fact" in (tmp_path / "MEMORY.md").read_text(encoding="utf-8")


def test_unexpected_oserror_still_propagates(
    store: CuratedMemoryStore, monkeypatch: pytest.MonkeyPatch
):
    """Only EXDEV/EBUSY are recoverable — other errors must not be swallowed."""

    def boom(*_args, **_kwargs):
        raise OSError(errno.EACCES, "permission denied")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        store.add("memory", "fact")


# -- consolidation failure streak expires ----------------------------------


def test_failure_streak_expires_so_memory_never_latches_dead(
    store: CuratedMemoryStore, monkeypatch: pytest.MonkeyPatch
):
    """Failures from an old turn must not permanently disable memory writes.

    The store is cached per workspace for the process lifetime, so without an
    expiry the Nth failure across unrelated turns would freeze every later
    write into the terminal error.
    """
    clock = {"t": 1000.0}
    monkeypatch.setattr("agentos.memory.curated.time.monotonic", lambda: clock["t"])

    store.add("memory", "x" * 150)
    for _ in range(_MAX_CONSOLIDATION_FAILURES_PER_TURN + 2):
        store.add("memory", "y" * 150)  # over budget -> consolidation failures

    frozen = store.add("memory", "y" * 150)
    assert frozen["done"] is True and frozen["success"] is False

    # A later turn, past the window: the store must accept work again.
    clock["t"] += _CONSOLIDATION_FAILURE_WINDOW_S + 1
    recovered = store.add("memory", "small")
    assert recovered["success"] is True


def test_failure_streak_still_cuts_off_a_runaway_loop(
    store: CuratedMemoryStore, monkeypatch: pytest.MonkeyPatch
):
    """Expiry must not defeat the anti-loop guard within a single turn."""
    monkeypatch.setattr("agentos.memory.curated.time.monotonic", lambda: 1000.0)

    store.add("memory", "x" * 150)
    results = [store.add("memory", "y" * 150) for _ in range(5)]

    assert results[-1]["done"] is True
    assert "Stop retrying" in results[-1]["error"]


def test_successful_write_clears_the_streak(store: CuratedMemoryStore):
    store.add("memory", "x" * 150)
    store.add("memory", "y" * 150)  # failure
    assert store.add("memory", "ok")["success"] is True
    assert store.consolidation_failure_count() == 0


def test_failure_streaks_are_scoped_per_caller(
    store: CuratedMemoryStore, monkeypatch: pytest.MonkeyPatch
):
    """Two sessions sharing the cached workspace store must not trip each other.

    Alternating failures from two callers add up to more than the per-turn
    cap, but neither caller's own streak reaches it, so neither may be cut off.
    """
    monkeypatch.setattr("agentos.memory.curated.time.monotonic", lambda: 1000.0)
    store.add("memory", "x" * 150)

    results = []
    for _ in range(_MAX_CONSOLIDATION_FAILURES_PER_TURN):
        results.append(store.add("memory", "y" * 150, caller_key="session-a"))
        results.append(store.add("memory", "y" * 150, caller_key="session-b"))

    assert all("done" not in r for r in results)
    assert store.consolidation_failure_count("session-a") == _MAX_CONSOLIDATION_FAILURES_PER_TURN
    assert store.consolidation_failure_count("session-b") == _MAX_CONSOLIDATION_FAILURES_PER_TURN
    # The unkeyed (RPC / CLI) streak is untouched by either session.
    assert store.consolidation_failure_count() == 0


def test_one_caller_still_trips_after_the_cap(
    store: CuratedMemoryStore, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("agentos.memory.curated.time.monotonic", lambda: 1000.0)
    store.add("memory", "x" * 150)

    for _ in range(_MAX_CONSOLIDATION_FAILURES_PER_TURN):
        assert "done" not in store.add("memory", "y" * 150, caller_key="session-a")
    frozen = store.replace("memory", "no-such-entry", "z", caller_key="session-a")
    assert frozen["done"] is True and "Stop retrying" in frozen["error"]

    # Another session in the same workspace is still allowed to consolidate.
    other = store.add("memory", "y" * 150, caller_key="session-b")
    assert "done" not in other and other["success"] is False


def test_success_clears_only_the_callers_streak(store: CuratedMemoryStore):
    store.add("memory", "x" * 150)
    store.add("memory", "y" * 150, caller_key="session-a")
    store.add("memory", "y" * 150, caller_key="session-b")
    assert store.add("memory", "ok", caller_key="session-a")["success"] is True
    assert store.consolidation_failure_count("session-a") == 0
    assert store.consolidation_failure_count("session-b") == 1


def test_failure_streak_map_is_bounded(store: CuratedMemoryStore):
    from agentos.memory.curated import _MAX_FAILURE_STREAKS

    store.add("memory", "x" * 150)
    for i in range(_MAX_FAILURE_STREAKS + 10):
        store.add("memory", "y" * 150, caller_key=f"session-{i}")
    assert len(store._failure_streaks) == _MAX_FAILURE_STREAKS
    assert store.consolidation_failure_count("session-0") == 0


# -- success payload stays terminal ----------------------------------------


def test_success_does_not_echo_entries(store: CuratedMemoryStore):
    """Echoing the store after a write invited redundant repeat calls."""
    store.add("memory", "fact A")
    result = store.add("memory", "fact B")

    assert result["success"] is True
    assert "current_entries" not in result
    assert result["entry_count"] == 2
    assert result["done"] is True


def test_error_paths_still_carry_entries(store: CuratedMemoryStore):
    """The model needs the inventory precisely when it must consolidate."""
    store.add("memory", "x" * 150)
    result = store.add("memory", "y" * 150)

    assert result["success"] is False
    assert result["current_entries"] == ["x" * 150]
