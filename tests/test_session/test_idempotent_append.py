"""``SessionManager.append_message(message_id=...)`` stores a row at most once.

A turn names its reply row before the first attempt; appending again with
that identity (a retry, or a second path that cannot tell whether the first
attempt committed) must not leave a second row or touch the session twice.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from agentos.session.manager import SessionManager
from agentos.session.storage import SessionStorage, StaleEpochError

KEY = "agent:main:webchat:idempotent-append"


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SessionStorage]:
    store = SessionStorage(":memory:")
    await store.connect()
    yield store
    await store.close()


async def _rows(storage: SessionStorage) -> list[tuple[str, str, str]]:
    async with storage.conn.execute(
        "SELECT message_id, role, content FROM transcript_entries ORDER BY id"
    ) as cursor:
        return [(row[0], row[1], row[2]) for row in await cursor.fetchall()]


@pytest.mark.asyncio
async def test_a_second_append_with_the_same_identity_stores_nothing(
    storage: SessionStorage,
) -> None:
    manager = SessionManager(storage, inject_time_prefix=False)
    await manager.create(KEY)
    await manager.append_message(KEY, "user", "hi")

    first = await manager.append_message(
        KEY, "assistant", "hello", token_count=7, message_id="reply-1"
    )
    after_first = await storage.get_session(KEY)
    assert after_first is not None
    second = await manager.append_message(
        KEY, "assistant", "hello", token_count=7, message_id="reply-1"
    )
    after_second = await storage.get_session(KEY)
    assert after_second is not None

    assert first.message_id == second.message_id == "reply-1"
    assert [(role, content) for _, role, content in await _rows(storage)] == [
        ("user", "hi"),
        ("assistant", "hello"),
    ]
    # The session was touched once: no second token count, no second touch.
    assert after_second.total_tokens == after_first.total_tokens
    assert after_second.updated_at == after_first.updated_at


@pytest.mark.asyncio
async def test_an_identified_append_still_replaces_the_in_progress_row(
    storage: SessionStorage,
) -> None:
    manager = SessionManager(storage, inject_time_prefix=False)
    await manager.create(KEY)
    await manager.write_turn_progress(
        KEY, message_id="progress-1", tool_calls=None, provider_messages={"messages": []}
    )
    for _ in range(2):
        await manager.append_message(
            KEY,
            "assistant",
            "done",
            replaces_message_id="progress-1",
            message_id="reply-1",
        )
    assert [(mid, content) for mid, _, content in await _rows(storage)] == [("reply-1", "done")]


@pytest.mark.asyncio
async def test_an_identified_append_is_refused_on_a_stale_epoch(storage: SessionStorage) -> None:
    manager = SessionManager(storage, inject_time_prefix=False)
    node = await manager.create(KEY)
    entry_epoch = node.epoch or 0
    await storage.increment_epoch(KEY)
    from agentos.session.models import TranscriptEntry

    entry = TranscriptEntry(
        session_id=node.session_id,
        session_key=KEY,
        message_id="reply-1",
        role="assistant",
        content="late",
    )
    with pytest.raises(StaleEpochError):
        await storage.append_transcript_entry(entry, expected_epoch=entry_epoch, if_absent=True)
    assert await _rows(storage) == []
