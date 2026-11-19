"""``memory.forget``: preview and remove what chosen sessions produced.

Driven through the real RPC dispatcher over real stores: curated files written
by the real ``memory`` tool, notes by ``memory_save``, a real FTS index, a real
evidence store, real SQLite session storage, and a real ``TurnRunner`` whose
prompt snapshot must stop showing a forgotten entry.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.engine.runtime import MemorySnapshot, TurnRunner
from agentos.gateway.access import CONTROL_ONLY
from agentos.gateway.config import GatewayConfig, MemoryConfig
from agentos.gateway.http_rpc import HTTP_RPC_METHODS
from agentos.gateway.rpc import RpcContext, get_dispatcher
from agentos.memory.curated import ENTRY_DELIMITER
from agentos.memory.evidence import MemoryEvidenceStore, text_hash
from agentos.memory.lineage import FORGET_NOT_COVERED
from agentos.memory.manager import MemoryManager
from agentos.memory.retrieval import MemoryRetriever
from agentos.memory.session_source import SessionSourceIndexer, session_source_path
from agentos.memory.store import LongTermMemoryStore
from agentos.memory.sync_manager import MemorySyncManager
from agentos.memory.turn_capture import TurnCaptureService
from agentos.memory.types import MemorySource
from agentos.session.models import SessionNode, TranscriptEntry
from agentos.session.storage import SessionStorage
from agentos.tools.builtin.memory_tools import create_memory_tools
from agentos.tools.registry import ToolRegistry
from agentos.tools.types import ToolContext, current_tool_context

NOTE = "memory/2026-11-18.md"


class _Provider:
    """Records what the external-provider mirror was told."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, str, str, dict[str, Any]]] = []

    def notify_memory_write(self, action: str, target: str, content: str, metadata: dict) -> None:
        self.writes.append((action, target, content, metadata))


class _Agent:
    async def start(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.workspace = tmp_path / "workspace"
        (self.workspace / "memory").mkdir(parents=True)
        self.db = tmp_path / "memory.db"
        self.store = LongTermMemoryStore(self.db)
        await self.store.initialize()
        self.evidence = MemoryEvidenceStore(self.db)
        self.storage = SessionStorage(str(tmp_path / "sessions.db"))
        await self.storage.connect()
        self.provider = _Provider()
        self.manager = MemoryManager(
            agent_id="main",
            db_path=self.db,
            store=self.store,
            sync_manager=MemorySyncManager(
                store=self.store,
                workspace_dir=self.workspace,
                memory_dir=self.workspace / "memory",
            ),
            retriever=MemoryRetriever(self.store),
            turn_capture=TurnCaptureService(
                workspace_dir=self.workspace, turns_dir=tmp_path / "turns"
            ),
            workspace_dir=self.workspace,
            memory_dir=self.workspace / "memory",
            evidence=self.evidence,
            provider_manager=self.provider,
        )
        registry = ToolRegistry()
        create_memory_tools(
            stores=self.store,
            retrievers=MemoryRetriever(self.store),
            memory_dir=str(self.workspace),
            registry=registry,
            evidence_stores={"main": self.evidence},
        )
        self.tools = {name: registry.get(name).handler for name in registry.list_names()}
        self.config = GatewayConfig(
            workspace_dir=str(self.workspace), memory=MemoryConfig(source="workspace")
        )
        self.runner = TurnRunner(
            provider_selector=None,
            tool_registry=ToolRegistry(),
            session_manager=None,
            config=self.config,
        )
        self.ctx = RpcContext(conn_id="test", config=self.config)
        self.ctx.session_manager = SimpleNamespace(storage=self.storage)
        self.ctx.memory_managers = {"main": self.manager}
        self.ctx.turn_runner = self.runner
        self.indexer = SessionSourceIndexer(
            storage=self.storage,
            store=self.store,
            agent_id="main",
            forgotten=self.evidence.forgotten_sessions,
        )

    async def close(self) -> None:
        await self.evidence.close()
        await self.store.close()
        await self.storage.close()

    async def session(self, name: str, *, agent_id: str = "main", text: str = "") -> SessionNode:
        node = SessionNode(
            session_key=f"agent:{agent_id}:webchat:direct:{name}",
            session_id=f"{name}-0f3a9c2e-uuid",
            agent_id=agent_id,
            updated_at=1_700_000_000_000,
        )
        await self.storage.upsert_session(node)
        if text:
            await self.say(node, text)
        return node

    async def say(self, node: SessionNode, text: str, at: int = 1_700_000_000_000) -> None:
        await self.storage.append_transcript_entry(
            TranscriptEntry(
                session_id=node.session_id,
                session_key=node.session_key,
                role="user",
                content=text,
                created_at=at,
            )
        )

    async def tool(self, node: SessionNode, name: str, **kwargs: Any) -> str:
        token = current_tool_context.set(
            ToolContext(
                agent_id="main",
                session_key=node.session_key,
                artifact_session_id=node.session_id,
            )
        )
        try:
            return await self.tools[name](**kwargs)
        finally:
            current_tool_context.reset(token)

    async def remember(self, node: SessionNode, entry: str, target: str = "memory") -> None:
        result = json.loads(
            await self.tool(node, "memory", action="add", target=target, content=entry)
        )
        assert result["success"] is True, result

    async def forget(self, *selectors: str, dry_run: bool = False, agent: str = "main") -> Any:
        return await get_dispatcher().dispatch(
            "r",
            "memory.forget",
            {"agentId": agent, "sessions": list(selectors), "dryRun": dry_run},
            self.ctx,
        )

    async def forget_ok(self, *selectors: str, dry_run: bool = False) -> dict[str, Any]:
        res = await self.forget(*selectors, dry_run=dry_run)
        assert res.ok, res.error
        return res.payload

    def curated(self, target: str = "memory") -> list[str]:
        path = self.workspace / ("MEMORY.md" if target == "memory" else "USER.md")
        if not path.exists():
            return []
        return [e for e in path.read_text(encoding="utf-8").split(ENTRY_DELIMITER) if e]

    def snapshot_state(self) -> dict[str, Any]:
        """Bytes of every workspace file plus every index and evidence row."""
        files = {
            str(p.relative_to(self.workspace)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(self.workspace.rglob("*"))
            if p.is_file()
        }
        conn = sqlite3.connect(self.db)
        try:
            rows = {
                table: conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
                for table in (
                    "files",
                    "chunks",
                    "memory_origins",
                    "forgotten_sessions",
                )
            }
        finally:
            conn.close()
        return {"files": files, "rows": rows}


@pytest.fixture()
async def agent(tmp_path: Path):
    a = _Agent()
    await a.start(tmp_path)
    yield a
    await a.close()


def _note_bytes(agent: _Agent, path: str = NOTE) -> bytes:
    return (agent.workspace / path).read_bytes()


# -- dry run -----------------------------------------------------------------


async def test_dry_run_reports_and_changes_nothing(agent: _Agent) -> None:
    (agent.workspace / "MEMORY.md").write_text("Legacy entry", encoding="utf-8")
    s1 = await agent.session("s1", text="the deploy window is friday")
    s2 = await agent.session("s2")
    await agent.remember(s1, "Deploy window is Friday")
    await agent.remember(s2, "Prod region is eu-west-1")
    await agent.tool(s1, "memory_save", content="- deploy checklist v2", path=NOTE)
    await agent.indexer.sync(force=True)
    before = agent.snapshot_state()

    report = await agent.forget_ok(s1.session_id, dry_run=True)

    assert agent.snapshot_state() == before
    assert report["dryRun"] is True
    assert [s["matchedBy"] for s in report["sessions"]] == ["session_id"]
    assert report["curated"] == [
        {"target": "memory", "entry": "Deploy window is Friday", "mixed": False}
    ]
    assert [(n["path"], n["occurrences"]) for n in report["notes"]] == [(NOTE, 1)]
    assert report["sessionDocuments"] == [session_source_path(s1)]
    assert report["untracked"] == {"memory": 1, "user": 0}
    assert report["errors"] == []
    assert report["notCovered"] == FORGET_NOT_COVERED


async def test_dry_run_on_a_workspace_with_the_old_template_changes_nothing(
    agent: _Agent,
) -> None:
    # An upgraded workspace still carries the instructions older releases
    # seeded MEMORY.md / USER.md with. A preview must not be what removes
    # them: every byte stays put, and the report never lists or counts them.
    from agentos.memory.curated_migration import _split_freeform
    from agentos.memory.curated_template import SHIPPED_TEMPLATES

    template = _split_freeform(SHIPPED_TEMPLATES["memory"][0])
    user_template = SHIPPED_TEMPLATES["user"][0].strip()
    s1 = await agent.session("s1")
    s2 = await agent.session("s2")
    await agent.remember(s1, "Deploy window is Friday")
    (agent.workspace / "MEMORY.md").write_text(
        ENTRY_DELIMITER.join([*template[:4], "Deploy window is Friday", *template[4:], "Legacy"]),
        encoding="utf-8",
    )
    (agent.workspace / "USER.md").write_text(
        ENTRY_DELIMITER.join([user_template, "Prefers tea"]), encoding="utf-8"
    )
    before = agent.snapshot_state()

    report = await agent.forget_ok(s1.session_id, dry_run=True)

    assert agent.snapshot_state() == before
    assert report["curated"] == [
        {"target": "memory", "entry": "Deploy window is Friday", "mixed": False}
    ]
    assert report["untracked"] == {"memory": 1, "user": 1}
    assert report["errors"] == []
    wire = json.dumps(report)
    for fragment in ("Use this file for", "What to call them", "Examples:"):
        assert fragment not in wire

    # The first real write leaves clean files.
    await agent.remember(s2, "Prod region is eu-west-1")
    await agent.remember(s2, "Name is Ada", target="user")

    assert agent.curated() == ["Deploy window is Friday", "Legacy", "Prod region is eu-west-1"]
    assert agent.curated("user") == ["Prefers tea", "Name is Ada"]
    assert list(agent.workspace.glob("*.bak*")) == []


# -- apply -------------------------------------------------------------------


async def test_curated_entry_is_removed_from_file_snapshot_and_origins(agent: _Agent) -> None:
    (agent.workspace / "MEMORY.md").write_text("Legacy entry", encoding="utf-8")
    s1 = await agent.session("s1")
    s2 = await agent.session("s2")
    await agent.remember(s1, "Deploy window is Friday")
    await agent.remember(s2, "Prod region is eu-west-1")
    await agent.remember(s1, "Prefers short answers", target="user")
    agent.runner._memory_snapshots[("main", s2.session_key)] = MemorySnapshot(
        memory_md=agent.runner._load_memory_md(agent.workspace)
    )
    assert (
        "Deploy window is Friday"
        in agent.runner._memory_snapshots[("main", s2.session_key)].memory_md
    )

    report = await agent.forget_ok(s1.session_key)

    assert report["dryRun"] is False
    assert agent.curated() == ["Legacy entry", "Prod region is eu-west-1"]
    assert agent.curated("user") == []
    snapshot = agent.runner._memory_snapshots[("main", s2.session_key)].memory_md
    assert "Deploy window is Friday" not in snapshot
    assert "Prod region is eu-west-1" in snapshot
    for entry, target in (("Deploy window is Friday", "memory"), ("Prefers short answers", "user")):
        assert await agent.evidence.origins_for_item("curated", target, text_hash(entry)) == []
    assert await agent.evidence.origins_for_item(
        "curated", "memory", text_hash("Prod region is eu-west-1")
    )
    assert {(w[0], w[1], w[3]["old_entry"]) for w in agent.provider.writes} == {
        ("remove", "memory", "Deploy window is Friday"),
        ("remove", "user", "Prefers short answers"),
    }
    assert report["providerMirrored"] is True
    assert report["untracked"] == {"memory": 1, "user": 0}


async def test_mixed_lineage_entry_is_removed_whole_and_reported(agent: _Agent) -> None:
    s1 = await agent.session("s1")
    s2 = await agent.session("s2")
    await agent.remember(s1, "Staging uses port 8080")
    await agent.tool(
        s2, "memory", action="replace", old_text="port 8080", content="Staging uses port 9090"
    )

    report = await agent.forget_ok(s1.session_id)

    assert agent.curated() == []
    assert report["curated"] == [
        {"target": "memory", "entry": "Staging uses port 9090", "mixed": True}
    ]
    assert report["mixed"] == [
        {
            "target": "memory",
            "entry": "Staging uses port 9090",
            "otherOrigins": [
                {"sessionKey": s2.session_key, "sessionId": s2.session_id, "writer": "tool"}
            ],
        }
    ]


async def test_untracked_legacy_entry_is_never_removed(agent: _Agent) -> None:
    (agent.workspace / "MEMORY.md").write_text("Legacy entry", encoding="utf-8")
    s1 = await agent.session("s1")
    await agent.remember(s1, "Session fact")

    report = await agent.forget_ok(s1.session_id)

    assert agent.curated() == ["Legacy entry"]
    assert report["untracked"]["memory"] == 1


async def test_note_block_is_removed_and_the_file_restored(agent: _Agent) -> None:
    note = agent.workspace / NOTE
    note.write_text("# 2026-11-18\n- written by hand", encoding="utf-8")
    s1 = await agent.session("s1")
    s2 = await agent.session("s2")
    await agent.tool(s2, "memory_save", content="- kept: the vendor is Acme", path=NOTE)
    after_s2 = note.read_text(encoding="utf-8")
    await agent.tool(s1, "memory_save", content="- forget me: zebra-password-hint", path=NOTE)

    report = await agent.forget_ok(s1.session_id)

    assert note.read_text(encoding="utf-8") == after_s2
    assert [(n["path"], n["occurrences"]) for n in report["notes"]] == [(NOTE, 1)]
    hits, _mode = await agent.store.search("zebra", min_score=0.0)
    assert hits == []
    kept, _mode = await agent.store.search("Acme", min_score=0.0)
    assert [h.path for h in kept] == [NOTE]
    assert len(await agent.evidence.origins_for_note_path(NOTE)) == 1


async def test_a_note_holding_only_the_forgotten_block_is_deleted(agent: _Agent) -> None:
    s1 = await agent.session("s1")
    await agent.tool(s1, "memory_save", content="only this", path="memory/solo.md")

    await agent.forget_ok(s1.session_id)

    assert not (agent.workspace / "memory/solo.md").exists()
    assert "memory/solo.md" not in await agent.store.list_paths()


async def test_a_hand_edited_note_block_is_unmatched_and_left_alone(agent: _Agent) -> None:
    note = agent.workspace / NOTE
    s1 = await agent.session("s1")
    await agent.tool(s1, "memory_save", content="- the vendor is Acme", path=NOTE)
    note.write_text("- the vendor is Acme Corp (edited)", encoding="utf-8")
    edited = note.read_bytes()

    report = await agent.forget_ok(s1.session_id)

    assert note.read_bytes() == edited
    assert report["notes"] == []
    assert [(u["path"], u["reason"]) for u in report["unmatched"]] == [(NOTE, "changed")]
    assert report["errors"] == []


async def test_a_deleted_note_file_is_reported_not_raised(agent: _Agent) -> None:
    s1 = await agent.session("s1")
    await agent.tool(s1, "memory_save", content="gone soon", path="memory/gone.md")
    (agent.workspace / "memory/gone.md").unlink()

    report = await agent.forget_ok(s1.session_id)

    assert [(u["path"], u["reason"]) for u in report["unmatched"]] == [
        ("memory/gone.md", "file_missing")
    ]
    assert report["errors"] == []


async def test_the_session_document_is_removed(agent: _Agent) -> None:
    s1 = await agent.session("s1", text="the launch codename is heliotrope")
    s2 = await agent.session("s2", text="the other codename is vermilion")
    await agent.indexer.sync(force=True)

    report = await agent.forget_ok(s1.session_key)

    assert report["sessionDocuments"] == [session_source_path(s1)]
    hits, _mode = await agent.store.search("heliotrope", min_score=0.0)
    assert hits == []
    other, _mode = await agent.store.search("vermilion", min_score=0.0)
    assert [h.path for h in other] == [session_source_path(s2)]


async def test_a_forgotten_session_is_not_reindexed(agent: _Agent) -> None:
    s1 = await agent.session("s1", text="first message about cobalt")
    s2 = await agent.session("s2", text="other session about saffron")
    await agent.indexer.sync(force=True)

    await agent.forget_ok(s1.session_id)
    await agent.say(s1, "a new message about cobalt", at=1_700_000_100_000)
    s1.updated_at = 1_700_000_100_000
    await agent.storage.upsert_session(s1)
    result = await agent.indexer.sync(force=True)

    paths = await agent.store.list_paths(source=MemorySource.sessions)
    assert paths == [session_source_path(s2)]
    assert result.skipped >= 1
    assert await agent.evidence.is_session_forgotten(session_id=s1.session_id)
    assert not await agent.evidence.is_session_forgotten(session_id=s2.session_id)


async def test_forgetting_twice_reports_already_forgotten_and_removes_nothing(
    agent: _Agent,
) -> None:
    s1 = await agent.session("s1")
    await agent.remember(s1, "Once only")
    await agent.forget_ok(s1.session_id)

    again = await agent.forget_ok(s1.session_id)

    assert again["sessions"][0]["alreadyForgotten"] is True
    assert again["curated"] == [] and again["notes"] == []
    assert again["errors"] == []


async def test_two_forgets_of_one_note_do_not_undo_each_other(
    agent: _Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rewriting a note reads it, awaits its origins, then writes: a second
    forget of the same note must not run in between, or the first one's
    write puts back the block the second removed."""
    s1 = await agent.session("s1")
    s2 = await agent.session("s2")
    await agent.tool(s1, "memory_save", content="- block from s1", path=NOTE)
    await agent.tool(s2, "memory_save", content="- block from s2", path=NOTE)
    original = agent.evidence.origins_for_note_path
    calls: dict[asyncio.Task[Any] | None, int] = {}
    first: list[asyncio.Task[Any] | None] = []
    paused, release = asyncio.Event(), asyncio.Event()

    async def held(path: str) -> Any:
        task = asyncio.current_task()
        first[:] = first or [task]
        calls[task] = calls.get(task, 0) + 1
        if task is first[0] and calls[task] == 2:  # the first forget's apply, note read
            paused.set()
            await release.wait()
        return await original(path)

    monkeypatch.setattr(agent.evidence, "origins_for_note_path", held)
    one = asyncio.ensure_future(agent.forget(s1.session_id))
    await asyncio.wait_for(paused.wait(), timeout=5)
    two = asyncio.ensure_future(agent.forget(s2.session_id))
    await asyncio.wait({two}, timeout=0.5)
    release.set()
    results = await asyncio.wait_for(asyncio.gather(one, two), timeout=10)

    assert all(r.ok for r in results), [r.error for r in results]
    note = agent.workspace / NOTE
    remaining = note.read_text(encoding="utf-8") if note.exists() else ""
    assert "block from s1" not in remaining
    assert "block from s2" not in remaining


async def test_a_deleted_session_can_still_be_forgotten(agent: _Agent) -> None:
    s1 = await agent.session("s1")
    await agent.remember(s1, "Learned before the chat was deleted")
    await agent.storage.delete_session(s1.session_key)

    report = await agent.forget_ok(s1.session_id)

    assert report["sessions"][0]["sessionKey"] == s1.session_key
    assert agent.curated() == []


async def test_forget_by_key_covers_every_transcript_under_it(agent: _Agent) -> None:
    s1 = await agent.session("s1")
    await agent.remember(s1, "Before the reset")
    rotated = SessionNode(session_key=s1.session_key, session_id="s1-after-reset", agent_id="main")
    await agent.storage.upsert_session(rotated)
    await agent.remember(rotated, "After the reset")

    by_id = await agent.forget_ok(s1.session_id, dry_run=True)
    assert [c["entry"] for c in by_id["curated"]] == ["Before the reset"]

    await agent.forget_ok(s1.session_key)

    assert agent.curated() == []
    assert await agent.evidence.is_session_forgotten(
        session_id="a-future-transcript", session_key=s1.session_key
    )


# -- refusals and degraded states ----------------------------------------------


async def test_a_prefix_selector_resolves_nothing_and_changes_nothing(agent: _Agent) -> None:
    s1 = await agent.session("s1")
    await agent.remember(s1, "Keep me")
    before = agent.snapshot_state()

    res = await agent.forget(s1.session_id[:6], s1.session_id)

    assert not res.ok
    assert res.error.code == "NOT_FOUND"
    assert s1.session_id[:6] in res.error.message
    assert res.error.details["unresolved"] == [
        {"selector": s1.session_id[:6], "reason": "not_found"}
    ]
    assert agent.snapshot_state() == before


async def test_a_session_of_another_agent_does_not_resolve(agent: _Agent) -> None:
    ops = await agent.session("ops1", agent_id="ops")

    res = await agent.forget(ops.session_id)

    assert not res.ok
    assert res.error.details["unresolved"][0]["reason"] == "other_agent"


async def test_curated_drift_is_reported_and_the_rest_still_applies(agent: _Agent) -> None:
    s1 = await agent.session("s1")
    await agent.remember(s1, "Drifted entry")
    await agent.tool(s1, "memory_save", content="note from s1", path="memory/d.md")
    memory_md = agent.workspace / "MEMORY.md"
    memory_md.write_text("Drifted entry\n§\n\nhand-written paragraph", encoding="utf-8")
    drifted = memory_md.read_bytes()

    preview = await agent.forget_ok(s1.session_id, dry_run=True)
    assert [(e["stage"], e["target"]) for e in preview["errors"]] == [("curated", "memory")]
    assert memory_md.read_bytes() == drifted
    assert not list(agent.workspace.glob("MEMORY.md.bak.*"))

    report = await agent.forget_ok(s1.session_id)

    assert [(e["stage"], e["target"]) for e in report["errors"]] == [("curated", "memory")]
    assert "Refusing to write MEMORY.md" in report["errors"][0]["message"]
    assert memory_md.read_bytes() == drifted
    assert report["curated"] == []
    assert not (agent.workspace / "memory/d.md").exists()
    # The entry keeps its origins, so the same forget completes once fixed.
    memory_md.write_text("Drifted entry", encoding="utf-8")
    retry = await agent.forget_ok(s1.session_id)
    assert [c["entry"] for c in retry["curated"]] == ["Drifted entry"]
    assert retry["errors"] == []


async def test_no_memory_manager_or_no_lineage_is_a_clear_error(agent: _Agent) -> None:
    unknown = await agent.forget("anything", agent="nope")
    assert not unknown.ok and unknown.error.code == "NOT_FOUND"

    agent.manager.evidence = None
    unavailable = await agent.forget("anything")
    assert not unavailable.ok
    assert unavailable.error.code == "UNAVAILABLE"

    agent.ctx.memory_managers = {}
    none = await agent.forget("anything")
    assert not none.ok and none.error.code == "UNAVAILABLE"


@pytest.mark.parametrize("sessions", [[], "", [1], None])
async def test_bad_session_params_are_invalid(agent: _Agent, sessions: Any) -> None:
    res = await get_dispatcher().dispatch(
        "r", "memory.forget", {"agentId": "main", "sessions": sessions}, agent.ctx
    )
    assert not res.ok and res.error.code == "INVALID_REQUEST"


def test_memory_forget_is_control_only_and_not_on_the_http_allow_list() -> None:
    entry = get_dispatcher().get_entry("memory.forget")
    assert entry is not None
    assert entry.audiences == CONTROL_ONLY
    assert "memory.forget" not in HTTP_RPC_METHODS


def test_the_report_states_what_forget_does_not_remove() -> None:
    statement = FORGET_NOT_COVERED.lower()
    for item in ("transcripts", "turn-capture", "checkpoints", "untracked"):
        assert item in statement


# -- review fixes: whose text is it (finding 1) ----------------------------------


async def test_a_saved_block_extended_by_hand_is_unmatched_not_cut(agent: _Agent) -> None:
    """The user appended a line to the saved paragraph: the session's text is
    no longer a block of its own, so forget must not cut its first line."""
    note = agent.workspace / NOTE
    note.write_text("# Notes", encoding="utf-8")
    s1 = await agent.session("s1")
    await agent.tool(s1, "memory_save", content="- the vendor is Acme", path=NOTE)
    note.write_text(note.read_text(encoding="utf-8") + "\n- and Beta for EU", encoding="utf-8")
    before = _note_bytes(agent)

    report = await agent.forget_ok(s1.session_id)

    assert _note_bytes(agent) == before
    assert report["notes"] == []
    assert [(u["path"], u["reason"]) for u in report["unmatched"]] == [(NOTE, "changed")]


async def test_an_untracked_identical_copy_makes_the_block_ambiguous(agent: _Agent) -> None:
    note = agent.workspace / NOTE
    s1 = await agent.session("s1")
    await agent.tool(s1, "memory_save", content="- the vendor is Acme", path=NOTE)
    note.write_text(note.read_text(encoding="utf-8") + "\n\n- the vendor is Acme", encoding="utf-8")
    before = _note_bytes(agent)

    report = await agent.forget_ok(s1.session_id)

    assert _note_bytes(agent) == before
    assert report["notes"] == []
    assert [(u["reason"], u.get("detail")) for u in report["unmatched"]] == [
        ("ambiguous", "untracked_copies")
    ]


async def test_a_copy_written_by_another_session_is_never_removed(agent: _Agent) -> None:
    s1 = await agent.session("s1")
    s2 = await agent.session("s2")
    await agent.tool(s1, "memory_save", content="- the vendor is Acme", path=NOTE)
    await agent.tool(s2, "memory_save", content="- the vendor is Acme", path=NOTE)
    before = _note_bytes(agent)

    report = await agent.forget_ok(s1.session_id)

    assert _note_bytes(agent) == before
    assert [(u["reason"], u.get("detail")) for u in report["unmatched"]] == [
        ("ambiguous", "other_sessions")
    ]
    # Forgetting both writers together leaves no other claimant.
    both = await agent.forget_ok(s1.session_id, s2.session_id)
    assert [(n["path"], n["occurrences"]) for n in both["notes"]] == [(NOTE, 2)]
    assert not (agent.workspace / NOTE).exists()


async def test_a_block_saved_twice_by_one_session_is_removed_twice(agent: _Agent) -> None:
    (agent.workspace / NOTE).write_text("# Notes", encoding="utf-8")
    s1 = await agent.session("s1")
    await agent.tool(s1, "memory_save", content="- ping the oncall", path=NOTE)
    await agent.tool(s1, "memory_save", content="- ping the oncall", path=NOTE)

    report = await agent.forget_ok(s1.session_id)

    assert [(n["path"], n["occurrences"]) for n in report["notes"]] == [(NOTE, 2)]
    assert _note_bytes(agent) == b"# Notes"


async def test_a_block_inside_another_sessions_rewrite_is_ambiguous(agent: _Agent) -> None:
    s1 = await agent.session("s1")
    s2 = await agent.session("s2")
    await agent.tool(s1, "memory_save", content="Vendor is Acme", path=NOTE)
    await agent.tool(
        s2,
        "memory_save",
        content="# Notes\n\nVendor is Acme\n\nRenewal in March",
        path=NOTE,
        mode="replace",
    )
    before = _note_bytes(agent)

    report = await agent.forget_ok(s1.session_id)

    assert _note_bytes(agent) == before
    assert [(u["reason"], u.get("detail")) for u in report["unmatched"]] == [
        ("ambiguous", "overlaps_other_block")
    ]


# -- review fixes: newlines (finding 14) ------------------------------------------


async def _note_origin(agent: _Agent, node: SessionNode, block: str, path: str = NOTE) -> None:
    from agentos.memory.evidence import SessionRef

    await agent.evidence.add_origin(
        kind="note",
        target=path,
        item_hash=text_hash(block),
        item_len=len(block),
        writer="tool",
        session=SessionRef(session_key=node.session_key, session_id=node.session_id),
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"# Title\n\nkeep me\n\nforget me\n", b"# Title\n\nkeep me\n"),
        (b"# Title\r\n\r\nkeep me\r\n\r\nforget me\r\n", b"# Title\r\n\r\nkeep me\r\n"),
        # The blank line before the block goes with it (here two CRLFs); the
        # LF endings after it are untouched.
        (b"keep one\r\n\r\nforget me\n\nkeep two\n", b"keep one\n\nkeep two\n"),
        (b"forget me\r\n\r\nkeep\r\n", b"keep\r\n"),
    ],
    ids=["lf", "crlf", "mixed", "first-block"],
)
async def test_note_removal_keeps_every_other_byte(
    agent: _Agent, raw: bytes, expected: bytes
) -> None:
    (agent.workspace / NOTE).write_bytes(raw)
    s1 = await agent.session("s1")
    await _note_origin(agent, s1, "forget me")

    report = await agent.forget_ok(s1.session_id)

    assert [n["occurrences"] for n in report["notes"]] == [1]
    assert _note_bytes(agent) == expected


async def test_a_crlf_curated_entry_is_found_and_forgotten(agent: _Agent) -> None:
    s1 = await agent.session("s1")
    await agent.remember(s1, "Deploy steps:\r\n1. build\r\n2. ship")

    report = await agent.forget_ok(s1.session_id)

    assert [c["entry"] for c in report["curated"]] == ["Deploy steps:\n1. build\n2. ship"]
    assert agent.curated() == []


# -- review fixes: retry and leftovers (finding 5) --------------------------------


@pytest.mark.skipif(
    sys.platform == "win32" or getattr(os, "geteuid", lambda: 1)() == 0,
    reason="POSIX permissions; root reads anything",
)
async def test_an_unreadable_note_keeps_its_origin_so_forget_can_retry(agent: _Agent) -> None:
    note = agent.workspace / "memory/locked.md"
    s1 = await agent.session("s1")
    await agent.tool(s1, "memory_save", content="secret plan", path="memory/locked.md")
    note.chmod(0)
    try:
        first = await agent.forget_ok(s1.session_id)
    finally:
        note.chmod(0o644)
    assert [(u["path"], u["reason"]) for u in first["unmatched"]] == [
        ("memory/locked.md", "unreadable")
    ]

    retry = await agent.forget_ok(s1.session_id)

    assert [n["path"] for n in retry["notes"]] == ["memory/locked.md"]
    assert not note.exists()


async def test_a_missing_note_keeps_its_origin_so_forget_can_retry(agent: _Agent) -> None:
    note = agent.workspace / "memory/moved.md"
    aside = agent.tmp / "moved.md"
    s1 = await agent.session("s1")
    await agent.tool(s1, "memory_save", content="secret plan", path="memory/moved.md")
    note.rename(aside)

    first = await agent.forget_ok(s1.session_id)
    assert [u["reason"] for u in first["unmatched"]] == ["file_missing"]

    aside.rename(note)
    retry = await agent.forget_ok(s1.session_id)
    assert [n["path"] for n in retry["notes"]] == ["memory/moved.md"]


@pytest.mark.skipif(
    sys.platform == "win32" or getattr(os, "geteuid", lambda: 1)() == 0,
    reason="POSIX permissions; root reads anything",
)
async def test_an_unreadable_memory_file_keeps_its_origins(agent: _Agent) -> None:
    s1 = await agent.session("s1")
    await agent.remember(s1, "Kept for a retry")
    memory_md = agent.workspace / "MEMORY.md"
    memory_md.chmod(0)
    try:
        first = await agent.forget_ok(s1.session_id)
    finally:
        memory_md.chmod(0o644)
    assert [(e["stage"], e["target"]) for e in first["errors"]] == [("curated", "memory")]
    assert first["staleOrigins"] == 0

    retry = await agent.forget_ok(s1.session_id)

    assert [c["entry"] for c in retry["curated"]] == ["Kept for a retry"]
    assert agent.curated() == []


async def test_forget_logs_carry_no_removed_text(agent: _Agent) -> None:
    from structlog.testing import capture_logs

    class _Failing(_Provider):
        def notify_memory_write(self, action, target, content, metadata) -> None:
            raise RuntimeError(f"provider refused {metadata.get('old_entry')}")

    agent.manager.provider_manager = _Failing()
    s1 = await agent.session("s1")
    await agent.remember(s1, "zanzibar-secret-entry")
    await agent.tool(s1, "memory_save", content="zanzibar-secret-note", path=NOTE)

    with capture_logs() as logs:
        await agent.forget_ok(s1.session_id)

    assert logs
    assert "zanzibar" not in json.dumps(logs, default=str)


# -- review fixes: forgotten sessions coming back (finding 6) ---------------------


async def test_a_sync_past_its_check_cannot_republish_a_forgotten_session(agent: _Agent) -> None:
    s1 = await agent.session("s1", text="the codename is heliotrope")
    real_get_transcript = agent.storage.get_transcript

    async def get_transcript(session_id: str, *args: Any, **kwargs: Any) -> Any:
        entries = await real_get_transcript(session_id, *args, **kwargs)
        if session_id == s1.session_id:
            # The forget lands while this sync is already reading S1.
            await agent.forget_ok(s1.session_id)
        return entries

    agent.storage.get_transcript = get_transcript  # type: ignore[method-assign]
    await agent.indexer.sync(force=True)

    assert session_source_path(s1) not in await agent.store.list_paths()


async def test_a_sync_past_its_check_cannot_republish_a_deleted_session(agent: _Agent) -> None:
    s1 = await agent.session("s1", text="the codename is heliotrope")
    real_get_transcript = agent.storage.get_transcript
    agent.ctx.session_manager = SimpleNamespace(storage=agent.storage)

    async def get_transcript(session_id: str, *args: Any, **kwargs: Any) -> Any:
        entries = await real_get_transcript(session_id, *args, **kwargs)
        if session_id == s1.session_id:
            res = await get_dispatcher().dispatch(
                "r", "sessions.delete", {"key": s1.session_key}, agent.ctx
            )
            assert res.ok, res.error
        return entries

    agent.storage.get_transcript = get_transcript  # type: ignore[method-assign]
    await agent.indexer.sync(force=True)

    assert session_source_path(s1) not in await agent.store.list_paths()


async def test_forget_removes_a_document_indexed_after_its_plan(agent: _Agent) -> None:
    from agentos.gateway.rpc_memory import _forget_session_lookup
    from agentos.memory.lineage import apply_forget, plan_forget, resolve_sessions

    s1 = await agent.session("s1", text="the codename is heliotrope")
    resolved, unresolved = await resolve_sessions(
        [s1.session_id],
        agent_id="main",
        evidence=agent.evidence,
        lookup=_forget_session_lookup(agent.ctx),
    )
    assert unresolved == [] and resolved
    plan = await plan_forget(agent.manager, resolved)
    assert plan.session_documents == []
    await agent.indexer.sync(force=True)  # published between plan and apply

    report = await apply_forget(agent.manager, plan)

    assert report["sessionDocuments"] == [session_source_path(s1)]
    assert session_source_path(s1) not in await agent.store.list_paths()


async def test_origins_written_after_a_forget_by_key_are_forgotten_next_time(
    agent: _Agent,
) -> None:
    """Origins are recorded for forgotten sessions that keep writing; a forget
    by the same key removes them."""
    s1 = await agent.session("s1")
    await agent.remember(s1, "Before the forget")
    await agent.forget_ok(s1.session_key)

    await agent.remember(s1, "After the forget")
    assert await agent.evidence.origins_for_item("curated", "memory", text_hash("After the forget"))

    again = await agent.forget_ok(s1.session_key)

    assert again["sessions"][0]["alreadyForgotten"] is True
    assert [c["entry"] for c in again["curated"]] == ["After the forget"]
    assert agent.curated() == []


async def test_a_forget_during_publishing_is_caught_after_it(agent: _Agent) -> None:
    """The forget lands after the pre-publish check, while the document is
    being embedded: the check after publishing withdraws it."""
    s1 = await agent.session("s1", text="the codename is heliotrope")
    real_index_file = agent.store.index_file

    async def index_file(*args: Any, **kwargs: Any) -> int:
        if kwargs.get("path") == session_source_path(s1):
            await agent.forget_ok(s1.session_id)
        return await real_index_file(*args, **kwargs)

    agent.store.index_file = index_file  # type: ignore[method-assign]
    result = await agent.indexer.sync(force=True)

    assert session_source_path(s1) not in await agent.store.list_paths()
    assert result.removed == 1
