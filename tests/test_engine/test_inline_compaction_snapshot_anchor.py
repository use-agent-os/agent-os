"""The turn runner anchors inline compaction persistence on the history it loaded.

``TurnRunner._load_history`` records a ``TranscriptSnapshot`` of the last
persisted row the agent's history covers; the compaction persist adapter
hands it to ``SessionManager.persist_compaction_result`` and re-anchors on
the snapshot that call returns, so a follow-up queued by ``sessions.send``
during a long turn survives one -- or several -- inline compactions.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from agentos.engine import Agent, AgentConfig
from agentos.engine.runtime import TurnRunner
from agentos.engine.turn_runner.harness import (
    CompactionPersistRejectedError,
)
from agentos.session.manager import SessionManager, TranscriptSnapshot
from agentos.session.storage import SessionStorage

KEY = "agent:main:anchor"


class _Provider:
    provider_name = "fake"

    async def chat(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover - never called
        raise AssertionError("provider must not be called")


@pytest.fixture
async def session_manager(tmp_path: Path) -> AsyncIterator[SessionManager]:
    storage = SessionStorage(":memory:")
    await storage.connect()
    # Inline compaction records a durable checkpoint before it rewrites rows;
    # the gateway always configures the checkpoint workspace.
    manager = SessionManager(
        storage,
        inject_time_prefix=False,
        checkpoint_workspace_dir=tmp_path,
    )
    yield manager
    await storage.close()


def _runner(session_manager: Any) -> TurnRunner:
    return TurnRunner(provider_selector=MagicMock(), session_manager=session_manager)


def _agent() -> Agent:
    return Agent(provider=_Provider(), config=AgentConfig(system_prompt="base"), session_key=KEY)


async def _seed(session_manager: SessionManager) -> None:
    await session_manager.create(KEY)
    await session_manager.append_message(KEY, "user", "A", token_count=1)
    await session_manager.append_message(KEY, "assistant", "B", token_count=1)
    await session_manager.append_message(KEY, "user", "C", token_count=1)


@pytest.mark.asyncio
async def test_load_history_records_the_last_loaded_row(session_manager: SessionManager) -> None:
    await _seed(session_manager)
    runner = _runner(session_manager)

    await runner._load_history(_agent(), KEY, trim_last_user=True)

    node = await session_manager.get_session(KEY)
    rows = await session_manager.get_transcript(KEY)
    assert node is not None
    assert runner._compaction_snapshots[KEY] == TranscriptSnapshot(
        session_id=node.session_id,
        through_message_id=rows[-1].message_id,
    )


@pytest.mark.asyncio
async def test_load_history_of_an_empty_transcript_records_an_empty_snapshot(
    session_manager: SessionManager,
) -> None:
    node = await session_manager.create(KEY)
    runner = _runner(session_manager)

    await runner._load_history(_agent(), KEY, trim_last_user=True)

    assert runner._compaction_snapshots[KEY] == TranscriptSnapshot(
        session_id=node.session_id,
        through_message_id=None,
    )


@pytest.mark.asyncio
async def test_queued_follow_up_survives_repeated_inline_compactions(
    session_manager: SessionManager,
) -> None:
    """End to end through the adapter: load history, queue D, compact twice."""
    await _seed(session_manager)
    runner = _runner(session_manager)
    await runner._load_history(_agent(), KEY, trim_last_user=True)
    queued = await session_manager.append_message(KEY, "user", "D", token_count=1)

    await TurnRunner.persist_and_notify(
        runner,
        session_key=KEY,
        summary="summary of A",
        kept_entries=[{"role": "assistant", "content": "B"}, {"role": "user", "content": "C"}],
        compaction_id="cmp_1",
    )
    live = await session_manager.get_transcript(KEY)
    assert [entry.content for entry in live] == ["B", "C", "D"]
    assert live[-1].message_id == queued.message_id
    # Re-anchored on the kept tail the first persist wrote.
    assert runner._compaction_snapshots[KEY].through_message_id == live[1].message_id

    await session_manager.append_message(KEY, "user", "E", token_count=1)
    await TurnRunner.persist_and_notify(
        runner,
        session_key=KEY,
        summary="summary of A and B",
        kept_entries=[{"role": "user", "content": "C"}],
        compaction_id="cmp_2",
    )

    live = await session_manager.get_transcript(KEY)
    assert [entry.content for entry in live] == ["C", "D", "E"]
    assert live[1].message_id == queued.message_id
    canonical = await session_manager.get_canonical_transcript(KEY)
    assert [entry.content for entry in canonical] == ["A", "B", "C", "D", "E"]


@pytest.mark.asyncio
async def test_stale_snapshot_is_reported_as_a_failed_persist(
    session_manager: SessionManager,
) -> None:
    """Nothing was written, so the adapter must not announce a completed persist.

    The stream consumer stage turns the exception into a ``failed`` lifecycle
    notification and skips the post-persist prompt refresh.
    """
    await _seed(session_manager)
    runner = _runner(session_manager)
    await runner._load_history(_agent(), KEY, trim_last_user=True)
    stale = runner._compaction_snapshots[KEY]
    await session_manager.truncate(KEY, max_messages=0)
    await session_manager.append_message(KEY, "user", "fresh", token_count=1)
    before = await session_manager.get_transcript(KEY)

    with pytest.raises(CompactionPersistRejectedError, match="not persisted"):
        await TurnRunner.persist_and_notify(
            runner,
            session_key=KEY,
            summary="summary of A",
            kept_entries=[{"role": "assistant", "content": "B"}, {"role": "user", "content": "C"}],
        )

    assert await session_manager.get_transcript(KEY) == before
    assert await session_manager.get_summaries(KEY) == []
    assert runner._compaction_snapshots[KEY] == stale


@pytest.mark.asyncio
async def test_the_anchor_is_dropped_with_the_rest_of_the_turn_state(
    session_manager: SessionManager,
) -> None:
    """Keyed per session and re-recorded by every _load_history, so it must not
    outlive the turn -- ephemeral subagent sessions would pile up otherwise."""
    await _seed(session_manager)
    runner = _runner(session_manager)
    await runner._load_history(_agent(), KEY, trim_last_user=True)
    assert KEY in runner._compaction_snapshots

    runner.clear_compaction_turn_state(KEY)

    assert KEY not in runner._compaction_snapshots


@pytest.mark.asyncio
async def test_adapter_tolerates_a_persist_method_without_snapshot_support() -> None:
    """Older / fake session managers keep receiving the three positional args."""
    calls: list[tuple[Any, ...]] = []

    class _Legacy:
        async def persist_compaction_result(self, session_key, summary, kept):
            calls.append((session_key, summary, list(kept)))

        async def get_transcript(self, session_key):
            return []

    runner = _runner(_Legacy())
    runner._compaction_snapshots[KEY] = TranscriptSnapshot(session_id="s", through_message_id="m")

    await TurnRunner.persist_and_notify(
        runner,
        session_key=KEY, summary="S", kept_entries=[1, 2]
    )

    assert calls == [(KEY, "S", [1, 2])]
