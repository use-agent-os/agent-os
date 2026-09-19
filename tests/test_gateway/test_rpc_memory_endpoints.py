from __future__ import annotations

from pathlib import Path

import pytest

from agentos.gateway.rpc import RpcContext, get_dispatcher
from agentos.memory.manager import MemoryManager
from agentos.memory.retrieval import MemoryRetriever
from agentos.memory.store import LongTermMemoryStore
from agentos.memory.sync_manager import MemorySyncManager
from agentos.memory.turn_capture import TurnCaptureService


@pytest.mark.asyncio
async def test_rpc_memory_curated_and_knowledge_base(tmp_path: Path):
    dispatcher = get_dispatcher()

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    memory_dir = workspace / "memory"
    memory_dir.mkdir()
    db_path = tmp_path / "memory.db"

    # Setup initial curated files
    (workspace / "MEMORY.md").write_text(
        "First memory entry\n§\nSecond memory entry\n", encoding="utf-8"
    )
    (workspace / "USER.md").write_text("Prefers concise answers\n", encoding="utf-8")

    store = LongTermMemoryStore(db_path)
    await store.initialize()

    sync_manager = MemorySyncManager(store=store, workspace_dir=workspace, memory_dir=memory_dir)
    retriever = MemoryRetriever(store)
    turn_capture = TurnCaptureService(workspace_dir=workspace, turns_dir=tmp_path / "turns")

    manager = MemoryManager(
        agent_id="main",
        db_path=db_path,
        store=store,
        sync_manager=sync_manager,
        retriever=retriever,
        turn_capture=turn_capture,
        workspace_dir=workspace,
        memory_dir=memory_dir,
    )

    ctx = RpcContext(conn_id="test")
    ctx.memory_managers = {"main": manager}

    try:
        # 1. memory.curated.get
        res = await dispatcher.dispatch(
            "r1", "memory.curated.get", {"agentId": "main", "target": "memory"}, ctx
        )
        assert res.ok
        assert res.payload["target"] == "memory"
        assert len(res.payload["entries"]) == 2
        assert "First memory entry" in res.payload["entries"]

        # 2. memory.curated.add
        add_res = await dispatcher.dispatch(
            "r2",
            "memory.curated.add",
            {"agentId": "main", "target": "memory", "content": "Third memory entry"},
            ctx,
        )
        assert add_res.ok
        assert len(add_res.payload["entries"]) == 3
        assert "Third memory entry" in add_res.payload["entries"]

        # 3. memory.curated.replace
        replace_res = await dispatcher.dispatch(
            "r3",
            "memory.curated.replace",
            {
                "agentId": "main",
                "target": "memory",
                "oldText": "Second memory entry",
                "newContent": "Updated second entry",
            },
            ctx,
        )
        assert replace_res.ok
        assert "Updated second entry" in replace_res.payload["entries"]
        assert "Second memory entry" not in replace_res.payload["entries"]

        # 4. memory.curated.remove
        remove_res = await dispatcher.dispatch(
            "r4",
            "memory.curated.remove",
            {"agentId": "main", "target": "memory", "oldText": "Updated second entry"},
            ctx,
        )
        assert remove_res.ok
        assert "Updated second entry" not in remove_res.payload["entries"]

        # 5. memory.curated.batch
        batch_res = await dispatcher.dispatch(
            "r5",
            "memory.curated.batch",
            {
                "agentId": "main",
                "target": "user",
                "operations": [
                    {"action": "add", "content": "Uses metric system"},
                    {"action": "add", "content": "Dark mode preferred"},
                ],
            },
            ctx,
        )
        assert batch_res.ok
        assert len(batch_res.payload["entries"]) == 3
        assert "Uses metric system" in batch_res.payload["entries"]

        # 6. memory.knowledge_base.ingest (direct content)
        ingest_res = await dispatcher.dispatch(
            "r6",
            "memory.knowledge_base.ingest",
            {
                "agentId": "main",
                "content": "Kubernetes cluster configuration handbook.",
                "filename": "k8s.txt",
            },
            ctx,
        )
        assert ingest_res.ok
        assert len(ingest_res.payload["results"]) == 1
        assert ingest_res.payload["results"][0]["status"] == "indexed"
        assert ingest_res.payload["results"][0]["path"] == "knowledge_base/k8s.txt"

        # 7. memory.knowledge_base.list
        kb_list = await dispatcher.dispatch(
            "r7", "memory.knowledge_base.list", {"agentId": "main"}, ctx
        )
        assert kb_list.ok
        assert kb_list.payload["count"] == 1
        assert kb_list.payload["documents"][0]["path"] == "knowledge_base/k8s.txt"

        # 8. memory.list with source filter
        mem_list_kb = await dispatcher.dispatch(
            "r8", "memory.list", {"agentId": "main", "source": "knowledge_base"}, ctx
        )
        assert mem_list_kb.ok
        assert mem_list_kb.payload["count"] == 1
        assert mem_list_kb.payload["files"][0]["source"] == "knowledge_base"

        # 9. memory.show on knowledge base file
        show_res = await dispatcher.dispatch(
            "r9", "memory.show", {"agentId": "main", "path": "knowledge_base/k8s.txt"}, ctx
        )
        assert show_res.ok
        assert "Kubernetes" in show_res.payload["content"]

        # 10. memory.knowledge_base.remove
        rm_kb = await dispatcher.dispatch(
            "r10",
            "memory.knowledge_base.remove",
            {"agentId": "main", "path": "knowledge_base/k8s.txt"},
            ctx,
        )
        assert rm_kb.ok
        assert rm_kb.payload["removed"] is True

        kb_list_after = await dispatcher.dispatch(
            "r11", "memory.knowledge_base.list", {"agentId": "main"}, ctx
        )
        assert kb_list_after.ok
        assert kb_list_after.payload["count"] == 0

        # 11. Security boundaries: path traversal / outside ingestion rejected
        outside_file = tmp_path / "outside.txt"
        outside_file.write_text("Secret outside content", encoding="utf-8")
        ingest_outside_abs = await dispatcher.dispatch(
            "r12",
            "memory.knowledge_base.ingest",
            {"agentId": "main", "path": str(outside_file)},
            ctx,
        )
        assert not ingest_outside_abs.ok
        assert "traversal" in str(ingest_outside_abs.error).lower()

        ingest_outside_rel = await dispatcher.dispatch(
            "r13",
            "memory.knowledge_base.ingest",
            {"agentId": "main", "path": "../outside.txt"},
            ctx,
        )
        assert not ingest_outside_rel.ok
        assert "traversal" in str(ingest_outside_rel.error).lower()

        # 12. Security boundaries: cannot remove files outside knowledge_base/**
        assert (workspace / "MEMORY.md").is_file()
        rm_memory_md = await dispatcher.dispatch(
            "r14",
            "memory.knowledge_base.remove",
            {"agentId": "main", "path": "MEMORY.md"},
            ctx,
        )
        assert not rm_memory_md.ok
        assert (workspace / "MEMORY.md").is_file()

        rm_traversal = await dispatcher.dispatch(
            "r15",
            "memory.knowledge_base.remove",
            {"agentId": "main", "path": "knowledge_base/../MEMORY.md"},
            ctx,
        )
        assert not rm_traversal.ok
        assert (workspace / "MEMORY.md").is_file()

        # 13. Public char_limit and char_count accessors on CuratedMemoryStore
        curated_store = manager.curated_store()
        assert curated_store.char_limit("memory") > 0
        assert curated_store.char_count("memory") > 0

    finally:
        await store.close()


@pytest.mark.asyncio
async def test_rpc_knowledge_base_path_ingest_persists(tmp_path: Path):
    """Path ingest must copy into knowledge_base/ so list/show survive force sync."""
    dispatcher = get_dispatcher()

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    memory_dir = workspace / "memory"
    memory_dir.mkdir()
    db_path = tmp_path / "memory.db"

    store = LongTermMemoryStore(db_path)
    await store.initialize()

    sync_manager = MemorySyncManager(store=store, workspace_dir=workspace, memory_dir=memory_dir)
    retriever = MemoryRetriever(store)
    turn_capture = TurnCaptureService(workspace_dir=workspace, turns_dir=tmp_path / "turns")

    manager = MemoryManager(
        agent_id="main",
        db_path=db_path,
        store=store,
        sync_manager=sync_manager,
        retriever=retriever,
        turn_capture=turn_capture,
        workspace_dir=workspace,
        memory_dir=memory_dir,
    )

    ctx = RpcContext(conn_id="test")
    ctx.memory_managers = {"main": manager}

    marker = "MARKER-TOKEN-KB-INGEST path ingest check"
    note = workspace / "note.txt"
    note.write_text(marker, encoding="utf-8")

    try:
        # Path ingest from outside knowledge_base/ must persist a durable copy.
        ingest_res = await dispatcher.dispatch(
            "p1",
            "memory.knowledge_base.ingest",
            {"agentId": "main", "path": "note.txt"},
            ctx,
        )
        assert ingest_res.ok
        assert ingest_res.payload["results"][0]["status"] == "indexed"
        assert ingest_res.payload["results"][0]["path"] == "knowledge_base/note.txt"

        durable = workspace / "knowledge_base" / "note.txt"
        assert durable.is_file()
        assert durable.read_text(encoding="utf-8") == marker

        kb_list = await dispatcher.dispatch(
            "p2", "memory.knowledge_base.list", {"agentId": "main"}, ctx
        )
        assert kb_list.ok
        assert kb_list.payload["count"] >= 1
        assert any(d["path"] == "knowledge_base/note.txt" for d in kb_list.payload["documents"])

        show_res = await dispatcher.dispatch(
            "p3",
            "memory.show",
            {"agentId": "main", "path": "knowledge_base/note.txt"},
            ctx,
        )
        assert show_res.ok
        assert marker in show_res.payload["content"]

        await sync_manager.sync("test-force", force=True)

        show_after = await dispatcher.dispatch(
            "p4",
            "memory.show",
            {"agentId": "main", "path": "knowledge_base/note.txt"},
            ctx,
        )
        assert show_after.ok
        assert marker in show_after.payload["content"]

        # File already under knowledge_base/ still works (no-op copy).
        already = workspace / "knowledge_base" / "already.txt"
        already.write_text("Already under KB content", encoding="utf-8")
        ingest_kb = await dispatcher.dispatch(
            "p5",
            "memory.knowledge_base.ingest",
            {"agentId": "main", "path": "knowledge_base/already.txt"},
            ctx,
        )
        assert ingest_kb.ok
        assert ingest_kb.payload["results"][0]["status"] == "indexed"
        assert ingest_kb.payload["results"][0]["path"] == "knowledge_base/already.txt"
        assert already.is_file()

        show_already = await dispatcher.dispatch(
            "p6",
            "memory.show",
            {"agentId": "main", "path": "knowledge_base/already.txt"},
            ctx,
        )
        assert show_already.ok
        assert "Already under KB content" in show_already.payload["content"]

        kb_list2 = await dispatcher.dispatch(
            "p7", "memory.knowledge_base.list", {"agentId": "main"}, ctx
        )
        assert kb_list2.ok
        assert kb_list2.payload["count"] >= 2
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_rpc_knowledge_base_root_ingest_rejected(tmp_path: Path):
    """Workspace-root ingest must not copytree into knowledge_base/ (nesting)."""
    dispatcher = get_dispatcher()

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    memory_dir = workspace / "memory"
    memory_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text("curated root", encoding="utf-8")
    db_path = tmp_path / "memory.db"

    store = LongTermMemoryStore(db_path)
    await store.initialize()

    sync_manager = MemorySyncManager(store=store, workspace_dir=workspace, memory_dir=memory_dir)
    retriever = MemoryRetriever(store)
    turn_capture = TurnCaptureService(workspace_dir=workspace, turns_dir=tmp_path / "turns")

    manager = MemoryManager(
        agent_id="main",
        db_path=db_path,
        store=store,
        sync_manager=sync_manager,
        retriever=retriever,
        turn_capture=turn_capture,
        workspace_dir=workspace,
        memory_dir=memory_dir,
    )

    ctx = RpcContext(conn_id="test")
    ctx.memory_managers = {"main": manager}

    try:
        # CLI reaches this via `agentos memory ingest .` (workspace root).
        root_ingest = await dispatcher.dispatch(
            "r1",
            "memory.knowledge_base.ingest",
            {"agentId": "main", "path": "."},
            ctx,
        )
        assert not root_ingest.ok
        assert "knowledge_base" in str(root_ingest.error).lower()

        # Must not leave nested copy junk under knowledge_base/.
        kb = workspace / "knowledge_base"
        if kb.exists():
            nested = list(kb.rglob("MEMORY.md"))
            assert nested == [], nested
            # Also no knowledge_base/workspace/... nesting
            assert not (kb / "workspace").exists()
    finally:
        await store.close()


def test_rpc_memory_bool_param_coercion() -> None:
    from agentos.gateway.rpc_memory import _bool_param

    # Python bools
    assert _bool_param({"flag": True}, "flag") is True
    assert _bool_param({"flag": False}, "flag") is False

    # Integers 0 and 1
    assert _bool_param({"flag": 1}, "flag") is True
    assert _bool_param({"flag": 0}, "flag") is False

    # Truthy strings
    for truthy in ("true", "True", "TRUE", "1", "yes", "YES", "on", "ON"):
        assert _bool_param({"flag": truthy}, "flag") is True

    # Falsy strings
    for falsy in ("false", "False", "FALSE", "0", "no", "NO", "off", "OFF"):
        assert _bool_param({"flag": falsy}, "flag") is False

    # Missing or None uses default
    assert _bool_param({}, "flag", default=True) is True
    assert _bool_param({}, "flag", default=False) is False
    assert _bool_param({"flag": None}, "flag", default=True) is True
    assert _bool_param({"flag": None}, "flag", default=False) is False

    # Invalid values raise ValueError
    for invalid in (2, -1, "invalid", "maybe", [], {}):
        with pytest.raises(ValueError, match=r"params\.flag must be a boolean"):
            _bool_param({"flag": invalid}, "flag")

