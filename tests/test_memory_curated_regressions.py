"""Regressions for curated memory: lowered limits, batch op types, live limits."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agentos.gateway.rpc import RpcContext, get_dispatcher
from agentos.memory.curated import ENTRY_DELIMITER, CuratedMemoryStore
from agentos.memory.manager import MemoryManager

# -- A: lowering a curated char limit below an existing entry -----------------


def _store(tmp_path: Path, memory_limit: int) -> CuratedMemoryStore:
    store = CuratedMemoryStore(
        memory_dir=tmp_path, memory_char_limit=memory_limit, user_char_limit=2000
    )
    store.load_from_disk()
    return store


def test_lowered_limit_does_not_turn_a_clean_file_into_drift(tmp_path: Path) -> None:
    big = "b" * 1500
    assert _store(tmp_path, 4000).add("memory", big)["success"] is True

    lowered = _store(tmp_path, 1000)
    result = lowered.remove("memory", "bbbb")

    assert result["success"] is True, result
    assert lowered.entries_for("memory") == []
    assert not list(tmp_path.glob("MEMORY.md.bak.*")), "a clean file must not be snapshotted"


def test_lowered_limit_oversized_entry_is_replaceable(tmp_path: Path) -> None:
    _store(tmp_path, 4000).add("memory", "keep me")
    _store(tmp_path, 4000).add("memory", "b" * 1500)

    lowered = _store(tmp_path, 1000)
    result = lowered.replace("memory", "bbbb", "short summary")

    assert result["success"] is True, result
    assert lowered.entries_for("memory") == ["keep me", "short summary"]
    assert not list(tmp_path.glob("MEMORY.md.bak.*"))


def test_lowered_limit_still_budgets_adds_and_final_state(tmp_path: Path) -> None:
    _store(tmp_path, 4000).add("memory", "b" * 1500)
    lowered = _store(tmp_path, 1000)

    added = lowered.add("memory", "new fact")
    assert added["success"] is False
    assert "drift_backup" not in added
    assert "exceed the limit" in added["error"]

    # A replacement that still leaves the file over the new limit is refused.
    replaced = lowered.replace("memory", "bbbb", "c" * 1200)
    assert replaced["success"] is False
    assert "drift_backup" not in replaced

    # A batch that frees the space and adds in one call is allowed.
    batch = lowered.apply_batch(
        "memory",
        [{"action": "remove", "old_text": "bbbb"}, {"action": "add", "content": "new fact"}],
    )
    assert batch["success"] is True, batch
    assert lowered.entries_for("memory") == ["new fact"]


def test_external_growth_of_an_entry_is_still_drift(tmp_path: Path) -> None:
    store = _store(tmp_path, 200)
    store.add("memory", "tool-written entry")
    mem = tmp_path / "MEMORY.md"
    mem.write_text(mem.read_text(encoding="utf-8") + " appended" * 40, encoding="utf-8")

    result = store.remove("memory", "tool-written")

    assert result["success"] is False
    assert "drift_backup" in result


# -- B: non-string batch fields -----------------------------------------------


@pytest.mark.parametrize(
    "op",
    [
        {"action": "add", "content": 123},
        {"action": "replace", "old_text": ["x"], "content": "y"},
        {"action": "remove", "old_text": {"x": 1}},
        {"action": 7, "content": "y"},
    ],
)
def test_apply_batch_rejects_non_string_fields(tmp_path: Path, op: dict) -> None:
    store = _store(tmp_path, 4000)
    store.add("memory", "keep me")

    result = store.apply_batch("memory", [op])

    assert result["success"] is False
    assert "must be a string" in result["error"]
    assert store.entries_for("memory") == ["keep me"]


def test_apply_batch_rejects_non_object_operation(tmp_path: Path) -> None:
    store = _store(tmp_path, 4000)
    result = store.apply_batch("memory", ["add something"])  # type: ignore[list-item]
    assert result["success"] is False
    assert "must be an object" in result["error"]


def _manager(workspace: Path, **kwargs) -> MemoryManager:
    return MemoryManager(
        agent_id="main",
        db_path=workspace / "memory.db",
        store=SimpleNamespace(),  # type: ignore[arg-type]
        sync_manager=SimpleNamespace(),  # type: ignore[arg-type]
        retriever=SimpleNamespace(),  # type: ignore[arg-type]
        turn_capture=SimpleNamespace(),  # type: ignore[arg-type]
        workspace_dir=workspace,
        memory_dir=workspace / "memory",
        **kwargs,
    )


@pytest.mark.asyncio
async def test_rpc_batch_with_non_string_content_is_a_clean_error(tmp_path: Path) -> None:
    ctx = RpcContext(conn_id="test")
    ctx.memory_managers = {"main": _manager(tmp_path)}

    res = await get_dispatcher().dispatch(
        "r1",
        "memory.curated.batch",
        {"agentId": "main", "target": "memory", "operations": [{"action": "add", "content": 123}]},
        ctx,
    )

    assert not res.ok
    assert "must be a string" in str(res.error)


# -- C: manager reads curated limits from the live root config ----------------


def test_manager_curated_store_follows_config_patch(tmp_path: Path) -> None:
    root = SimpleNamespace(
        memory=SimpleNamespace(curated_memory_char_limit=4000, curated_user_char_limit=2000)
    )
    manager = _manager(tmp_path, memory_config=root.memory, config_root=root)
    assert manager.curated_store().char_limit("memory") == 4000

    # config.patch swaps the whole [memory] object on the root.
    root.memory = SimpleNamespace(curated_memory_char_limit=900, curated_user_char_limit=300)

    store = manager.curated_store()
    assert store.char_limit("memory") == 900
    assert store.char_limit("user") == 300


@pytest.mark.asyncio
async def test_manager_status_reports_live_limits(tmp_path: Path) -> None:
    (tmp_path / "MEMORY.md").write_text(f"a{ENTRY_DELIMITER}b", encoding="utf-8")
    root = SimpleNamespace(memory=SimpleNamespace(curated_memory_char_limit=4000))
    manager = _manager(tmp_path, memory_config=root.memory, config_root=lambda: root)

    root.memory = SimpleNamespace(curated_memory_char_limit=1234, curated_user_char_limit=500)

    status = await manager._curated_status()
    assert status is not None
    assert status["memory"]["usage"].endswith("/1,234")


def test_manager_without_root_falls_back_to_boot_config(tmp_path: Path) -> None:
    manager = _manager(
        tmp_path,
        memory_config=SimpleNamespace(curated_memory_char_limit=777, curated_user_char_limit=99),
    )
    assert manager.curated_store().char_limit("memory") == 777
