"""Origins recorded at the write: the ``memory`` tool, ``memory_save`` and operator RPC.

Every test drives the real tool handlers (or RPC dispatcher) over a real
curated store, a real index and a real evidence store in a temp directory.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.gateway.rpc import RpcContext, get_dispatcher
from agentos.memory.curated import ENTRY_DELIMITER
from agentos.memory.evidence import MemoryEvidenceStore, text_hash
from agentos.memory.lineage import find_note_block, remove_spans
from agentos.memory.manager import MemoryManager
from agentos.memory.retrieval import MemoryRetriever
from agentos.memory.store import LongTermMemoryStore
from agentos.memory.sync_manager import MemorySyncManager
from agentos.memory.turn_capture import TurnCaptureService
from agentos.tools.builtin.memory_tools import create_memory_tools
from agentos.tools.registry import ToolRegistry
from agentos.tools.types import ToolContext, current_tool_context

S1 = ("agent:main:webchat:direct:s1", "id-s1")
S2 = ("agent:main:webchat:direct:s2", "id-s2")
S3 = ("agent:main:webchat:direct:s3", "id-s3")


class _Env:
    def __init__(self, root: Path, store: LongTermMemoryStore, evidence: Any) -> None:
        self.root = root
        self.store = store
        self.evidence = evidence
        registry = ToolRegistry()
        create_memory_tools(
            stores=store,
            retrievers=MemoryRetriever(store),
            memory_dir=str(root),
            registry=registry,
            evidence_stores={"main": evidence} if evidence is not None else None,
        )
        self.handlers = {name: registry.get(name).handler for name in registry.list_names()}

    async def call(
        self, tool: str, session: tuple[str, str] = S1, source_kind: str | None = None, **kwargs
    ) -> str:
        token = current_tool_context.set(
            ToolContext(
                agent_id="main",
                session_key=session[0],
                artifact_session_id=session[1],
                source_kind=source_kind,
            )
        )
        try:
            return await self.handlers[tool](**kwargs)
        finally:
            current_tool_context.reset(token)

    async def origins(self, entry: str, target: str = "memory") -> list[tuple[str, str, str]]:
        rows = await self.evidence.origins_for_item("curated", target, text_hash(entry))
        return [(o.session_key, o.session_id, o.writer) for o in rows]


@pytest.fixture()
async def env(tmp_path: Path) -> Iterator[_Env]:
    store = LongTermMemoryStore(tmp_path / "memory.db")
    await store.initialize()
    evidence = MemoryEvidenceStore(tmp_path / "memory.db")
    yield _Env(tmp_path, store, evidence)
    await evidence.close()
    await store.close()


def _ok(result: str) -> dict[str, Any]:
    payload = json.loads(result)
    assert payload["success"] is True, payload
    return payload


# -- memory tool -------------------------------------------------------------


async def test_add_in_a_session_records_one_origin(env: _Env) -> None:
    _ok(await env.call("memory", action="add", content="Deploys run on Fridays"))

    assert await env.origins("Deploys run on Fridays") == [(*S1, "tool")]


async def test_replace_keeps_history_and_clears_the_old_entry(env: _Env) -> None:
    _ok(await env.call("memory", action="add", content="Staging is on port 8080"))
    _ok(
        await env.call(
            "memory",
            session=S2,
            action="replace",
            old_text="port 8080",
            content="Staging is on port 9090",
        )
    )

    assert await env.origins("Staging is on port 9090") == [(*S1, "tool"), (*S2, "tool")]
    assert await env.origins("Staging is on port 8080") == []


async def test_review_writes_are_recorded_as_review(env: _Env) -> None:
    _ok(
        await env.call(
            "memory", session=S3, source_kind="memory_nudge", action="add", content="Uses uv"
        )
    )

    assert await env.origins("Uses uv") == [(*S3, "review")]


async def test_batch_remove_and_add(env: _Env) -> None:
    _ok(await env.call("memory", action="add", content="Old fact"))
    _ok(
        await env.call(
            "memory",
            session=S2,
            operations=[
                {"action": "remove", "old_text": "Old fact"},
                {"action": "add", "content": "New fact"},
            ],
        )
    )

    assert await env.origins("Old fact") == []
    assert await env.origins("New fact") == [(*S2, "tool")]


async def test_remove_deletes_the_entry_origins(env: _Env) -> None:
    _ok(await env.call("memory", action="add", content="Temporary fact"))
    _ok(await env.call("memory", session=S2, action="remove", old_text="Temporary"))

    assert await env.origins("Temporary fact") == []


async def test_duplicate_add_adds_the_session_to_the_existing_entry(env: _Env) -> None:
    _ok(await env.call("memory", action="add", content="Prefers tabs"))
    single = _ok(await env.call("memory", session=S2, action="add", content="Prefers tabs"))
    _ok(
        await env.call(
            "memory",
            session=S3,
            operations=[
                {"action": "add", "content": "Prefers tabs"},
                {"action": "add", "content": "Another fact"},
            ],
        )
    )

    # The store's own behaviour is unchanged: no duplicate entry was written.
    assert "already exists" in single["message"]
    assert (env.root / "MEMORY.md").read_text(encoding="utf-8").count("Prefers tabs") == 1
    assert await env.origins("Prefers tabs") == [(*S1, "tool"), (*S2, "tool"), (*S3, "tool")]
    assert await env.origins("Another fact") == [(*S3, "tool")]


async def test_user_target_is_tracked_separately(env: _Env) -> None:
    _ok(await env.call("memory", target="user", action="add", content="Name is Ada"))

    assert await env.origins("Name is Ada", target="user") == [(*S1, "tool")]
    assert await env.origins("Name is Ada", target="memory") == []


async def test_a_refused_write_records_nothing(env: _Env) -> None:
    result = json.loads(
        await env.call("memory", action="replace", old_text="missing", content="new")
    )

    assert result["success"] is False
    assert (await env.evidence.table_counts())["memory_origins"] == 0


async def test_a_failing_evidence_store_does_not_change_the_tool_result(tmp_path: Path) -> None:
    class _Broken:
        async def add_origin(self, **_kwargs: Any) -> bool:
            raise RuntimeError("disk full")

        async def copy_origins(self, **_kwargs: Any) -> int:
            raise RuntimeError("disk full")

        async def delete_origins(self, *_args: Any) -> int:
            raise RuntimeError("disk full")

    store = LongTermMemoryStore(tmp_path / "memory.db")
    await store.initialize()
    try:
        broken = _Env(tmp_path, store, _Broken())
        result = _ok(await broken.call("memory", action="add", content="Still saved"))
        assert result["message"] == "Entry added."
        saved = await broken.call("memory_save", content="a note", path="memory/n.md")
        assert saved.startswith("Saved to memory/n.md")
    finally:
        await store.close()
    assert "Still saved" in (tmp_path / "MEMORY.md").read_text(encoding="utf-8")


async def test_without_evidence_stores_the_tools_still_work(tmp_path: Path) -> None:
    store = LongTermMemoryStore(tmp_path / "memory.db")
    await store.initialize()
    try:
        plain = _Env(tmp_path, store, None)
        _ok(await plain.call("memory", action="add", content="No lineage here"))
        assert (await plain.call("memory_save", content="x", path="memory/n.md")).startswith(
            "Saved"
        )
    finally:
        await store.close()


# -- memory_save -------------------------------------------------------------


async def _note_origins(env: _Env, path: str) -> list[Any]:
    return await env.evidence.origins_for_note_path(path)


async def test_append_records_the_block_as_written_and_round_trips(env: _Env) -> None:
    path = "memory/2026-11-18.md"
    note = env.root / path
    first = "# 2026-11-18\n- Shipped the billing migration"
    second = "- Rollback plan: keep the old table for a week\n"

    await env.call("memory_save", content=first, path=path)
    before_second = note.read_text(encoding="utf-8")
    await env.call("memory_save", session=S2, content=second, path=path)
    on_disk = note.read_text(encoding="utf-8")

    origins = await _note_origins(env, path)
    assert [(o.session_id, o.writer) for o in origins] == [(S1[1], "tool"), (S2[1], "tool")]
    for origin, block in zip(origins, (first, second), strict=True):
        assert origin.item_hash == text_hash(block)
        assert origin.item_len == len(block)
        # The block is found in the real file by its hash and length alone.
        assert find_note_block(on_disk, origin.item_hash, origin.item_len) == [on_disk.index(block)]

    # Removing the second block restores the file as it was before the append.
    second_origin = origins[1]
    starts = find_note_block(on_disk, second_origin.item_hash, second_origin.item_len)
    restored = remove_spans(on_disk, [(s, s + second_origin.item_len) for s in starts])
    assert restored == before_second


async def test_daily_note_default_path_records_an_origin(env: _Env) -> None:
    result = await env.call("memory_save", content="Met with the infra team")
    path = result.split("Saved to ", 1)[1].split(" ", 1)[0]

    origins = await _note_origins(env, path)
    assert [(o.session_key, o.session_id) for o in origins] == [S1]
    assert origins[0].item_hash == text_hash("Met with the infra team")


async def test_replace_mode_block_is_the_whole_file(env: _Env) -> None:
    path = "memory/projects.md"
    await env.call("memory_save", content="draft", path=path)
    await env.call(
        "memory_save", session=S2, content="# Projects\n- Atlas", path=path, mode="replace"
    )

    on_disk = (env.root / path).read_text(encoding="utf-8")
    replace_origin = (await _note_origins(env, path))[-1]
    assert replace_origin.session_id == S2[1]
    assert replace_origin.item_hash == text_hash(on_disk)
    assert find_note_block(on_disk, replace_origin.item_hash, replace_origin.item_len) == [0]


async def test_the_block_is_hashed_after_redaction(env: _Env) -> None:
    path = "memory/creds.md"
    await env.call("memory_save", content="api_key: sk-live-1234567890abcdef", path=path)

    on_disk = (env.root / path).read_text(encoding="utf-8")
    assert "sk-live-1234567890abcdef" not in on_disk
    origin = (await _note_origins(env, path))[0]
    assert origin.item_hash == text_hash(on_disk)


async def test_review_note_writes_are_recorded_as_review(env: _Env) -> None:
    await env.call(
        "memory_save", session=S3, source_kind="memory_nudge", content="x", path="memory/r.md"
    )

    origin = (await _note_origins(env, "memory/r.md"))[0]
    assert (origin.session_id, origin.writer) == (S3[1], "review")


# -- operator RPC ------------------------------------------------------------


@pytest.fixture()
async def rpc(tmp_path: Path):
    workspace = tmp_path / "workspace"
    (workspace / "memory").mkdir(parents=True)
    (workspace / "MEMORY.md").write_text("Legacy entry", encoding="utf-8")
    db = tmp_path / "memory.db"
    store = LongTermMemoryStore(db)
    await store.initialize()
    evidence = MemoryEvidenceStore(db)
    manager = MemoryManager(
        agent_id="main",
        db_path=db,
        store=store,
        sync_manager=MemorySyncManager(
            store=store, workspace_dir=workspace, memory_dir=workspace / "memory"
        ),
        retriever=MemoryRetriever(store),
        turn_capture=TurnCaptureService(workspace_dir=workspace, turns_dir=tmp_path / "turns"),
        workspace_dir=workspace,
        memory_dir=workspace / "memory",
        evidence=evidence,
    )
    ctx = RpcContext(conn_id="test")
    ctx.memory_managers = {"main": manager}

    async def call(method: str, **params: Any) -> dict[str, Any]:
        res = await get_dispatcher().dispatch("r", method, {"agentId": "main", **params}, ctx)
        assert res.ok, res.error
        return res.payload

    yield SimpleNamespace(call=call, evidence=evidence, manager=manager)
    await evidence.close()
    await store.close()


async def _operator_origins(evidence: MemoryEvidenceStore, entry: str) -> list[tuple]:
    rows = await evidence.origins_for_item("curated", "memory", text_hash(entry))
    return [(o.session_key, o.session_id, o.writer) for o in rows]


async def test_operator_rpc_edits_record_operator_origins(rpc) -> None:
    await rpc.call("memory.curated.add", target="memory", content="Operator fact")
    assert await _operator_origins(rpc.evidence, "Operator fact") == [("", "", "operator")]

    await rpc.call(
        "memory.curated.replace",
        target="memory",
        oldText="Operator fact",
        newContent="Operator fact v2",
    )
    assert await _operator_origins(rpc.evidence, "Operator fact") == []
    assert await _operator_origins(rpc.evidence, "Operator fact v2") == [("", "", "operator")]

    await rpc.call(
        "memory.curated.batch",
        target="memory",
        operations=[
            {"action": "add", "content": "Batch fact"},
            {"action": "add", "content": "Legacy entry"},
        ],
    )
    assert await _operator_origins(rpc.evidence, "Batch fact") == [("", "", "operator")]
    # A duplicate add tracks the existing (legacy) entry as operator-written.
    assert await _operator_origins(rpc.evidence, "Legacy entry") == [("", "", "operator")]

    await rpc.call("memory.curated.remove", target="memory", oldText="Batch fact")
    assert await _operator_origins(rpc.evidence, "Batch fact") == []


async def test_status_counts_untracked_legacy_entries(rpc) -> None:
    await rpc.call("memory.curated.add", target="memory", content="Tracked fact")

    status = await rpc.manager.status()

    assert status["curated"]["memory"]["entries"] == 2
    assert status["curated"]["memory"]["tracked"] == 1
    assert status["curated"]["memory"]["untracked"] == 1
    assert status["curated"]["user"]["untracked"] == 0
    assert (rpc.manager.workspace_dir / "MEMORY.md").read_text(encoding="utf-8").split(
        ENTRY_DELIMITER
    )[0] == "Legacy entry"
