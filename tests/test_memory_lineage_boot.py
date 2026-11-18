"""Boot wiring of the evidence store: opened per agent, shared with the session
indexer, closed with the manager, and never fatal to boot."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentos.gateway.config import GatewayConfig
from agentos.memory.evidence import EVIDENCE_TABLES, MemoryEvidenceStore
from agentos.memory.session_source import SessionSourceIndexer


class _FakeStore:
    def __init__(self, db_path, embedding_provider=None, query_embedding_cache_mode="on"):
        self.db_path = db_path

    async def initialize(self) -> None:
        return None

    async def close(self) -> None:
        return None


class _FakeSyncManager:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


def _patch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("agentos.memory.embedding_resolver.local_bge_available", lambda *_: False)
    monkeypatch.setattr("agentos.memory.store.LongTermMemoryStore", _FakeStore)
    monkeypatch.setattr("agentos.memory.sync_manager.MemorySyncManager", _FakeSyncManager)
    monkeypatch.setattr("agentos.agents.scope.maybe_migrate_legacy_memory", lambda *_: None)
    monkeypatch.setattr(
        "agentos.agents.scope.resolve_agent_memory_db",
        lambda agent_id, state_dir: tmp_path / "state" / agent_id / "memory.db",
    )
    monkeypatch.setattr(
        "agentos.agents.scope.resolve_agent_data_dir",
        lambda agent_id, *_: tmp_path / "data" / agent_id,
    )
    monkeypatch.setattr(
        "agentos.agents.scope.resolve_agent_memory_dir",
        lambda agent_id: tmp_path / "memory" / agent_id,
    )


async def test_each_agent_gets_an_open_evidence_store_shared_with_the_indexer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agentos.memory.manager import build_memory_managers

    _patch(monkeypatch, tmp_path)
    managers = await build_memory_managers(
        GatewayConfig(memory={"session_source_enabled": True}),
        ["main", "ops"],
        session_storage=object(),
    )
    try:
        for agent_id, manager in managers.items():
            evidence = manager.evidence
            assert isinstance(evidence, MemoryEvidenceStore)
            assert evidence.is_open
            assert evidence.db_path == str(tmp_path / "state" / agent_id / "memory.db")
            indexer = manager.sync_manager.kwargs["session_indexer"]
            assert indexer._forgotten == evidence.forgotten_sessions
        conn = sqlite3.connect(tmp_path / "state" / "main" / "memory.db")
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        conn.close()
        assert set(EVIDENCE_TABLES) <= names
    finally:
        for manager in managers.values():
            await manager.close()
    assert not any(m.evidence.is_open for m in managers.values())  # type: ignore[union-attr]


async def test_an_evidence_failure_degrades_lineage_not_boot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agentos.memory.manager import build_memory_managers

    _patch(monkeypatch, tmp_path)

    async def _fail(self: MemoryEvidenceStore) -> None:
        raise OSError("read-only filesystem")

    monkeypatch.setattr(MemoryEvidenceStore, "initialize", _fail)
    managers = await build_memory_managers(
        GatewayConfig(memory={"session_source_enabled": True}),
        ["main"],
        session_storage=object(),
    )
    try:
        assert managers["main"].evidence is None
        assert managers["main"].sync_manager.kwargs["session_indexer"]._forgotten is None
    finally:
        for manager in managers.values():
            await manager.close()


async def test_indexer_fails_closed_when_forgotten_sessions_cannot_be_read() -> None:
    indexed: list[str] = []

    class _Storage:
        async def list_sessions(self, **_kwargs):
            return [SimpleNamespace(session_id="s1", session_key="k1", agent_id="main")]

    class _Store:
        async def index_file(self, **kwargs):
            indexed.append(kwargs["path"])
            return 1

    async def _broken():
        raise RuntimeError("database is locked")

    indexer = SessionSourceIndexer(storage=_Storage(), store=_Store(), forgotten=_broken)

    with pytest.raises(RuntimeError):
        await indexer.sync(force=True)
    assert indexed == []
