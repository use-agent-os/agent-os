"""``subagent_wakes`` storage: lifetime of the parent-wake outbox rows."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from agentos.session.models import SessionNode
from agentos.session.storage import SessionStorage

# Fixed epoch ms so the tests never read the system clock.
_T0 = 1_700_000_000_000
PARENT = "agent:main:webchat:direct:wake-parent"
OTHER = "agent:main:webchat:direct:someone-else"


@pytest.fixture
async def storage() -> AsyncIterator[SessionStorage]:
    store = SessionStorage(":memory:")
    await store.connect()
    yield store
    await store.close()


async def _wake(
    storage: SessionStorage, key: str, parent: str, *, state: str = "pending", now: int = _T0
) -> None:
    await storage.record_subagent_wake(
        group_key=key,
        parent_session_key=parent,
        parent_task_id=key,
        message="wake up",
        provenance={"kind": "internal_system"},
        payloads=[{"result": "r"}],
        now_ms=now,
        state=state,
    )


async def test_delete_session_removes_its_pending_wakes(storage: SessionStorage) -> None:
    await storage.upsert_session(
        SessionNode(session_key=PARENT, session_id="s-1", created_at=_T0, updated_at=_T0)
    )
    await _wake(storage, "g-parent", PARENT)
    await _wake(storage, "g-other", OTHER)

    await storage.delete_session(PARENT)

    assert await storage.get_subagent_wake("g-parent") is None
    assert await storage.list_due_subagent_wakes(_T0 + 1) == [
        await storage.get_subagent_wake("g-other")
    ]


async def test_delete_session_without_a_row_still_removes_its_wakes(
    storage: SessionStorage,
) -> None:
    await _wake(storage, "g-parent", PARENT)

    await storage.delete_session(PARENT)

    assert await storage.get_subagent_wake("g-parent") is None


async def test_prune_stale_sessions_removes_their_wakes(storage: SessionStorage) -> None:
    await storage.upsert_session(
        SessionNode(session_key=PARENT, session_id="s-1", created_at=_T0, updated_at=_T0)
    )
    await _wake(storage, "g-parent", PARENT, state="delivered")

    assert await storage.prune_stale_sessions(_T0 + 1) == 1
    assert await storage.get_subagent_wake("g-parent") is None


async def test_an_awaiting_row_is_replaced_by_the_wake_once(storage: SessionStorage) -> None:
    await _wake(storage, "g", PARENT, state="awaiting")
    assert (await storage.get_subagent_wake("g"))["message"] == "wake up"  # type: ignore[index]

    created, row = await storage.record_subagent_wake(
        group_key="g",
        parent_session_key=PARENT,
        parent_task_id="g",
        message="all children done",
        provenance={},
        payloads=[],
        now_ms=_T0 + 5,
        replace_state="awaiting",
    )
    assert created is True and row["state"] == "pending"
    assert row["message"] == "all children done" and row["updated_at"] == _T0 + 5

    again, row = await storage.record_subagent_wake(
        group_key="g",
        parent_session_key=PARENT,
        parent_task_id="g",
        message="second",
        provenance={},
        payloads=[],
        now_ms=_T0 + 6,
        replace_state="awaiting",
    )
    assert again is False and row["message"] == "all children done"


async def test_list_by_state_and_prune(storage: SessionStorage) -> None:
    await _wake(storage, "a1", PARENT, state="awaiting")
    await _wake(storage, "d1", PARENT, state="delivered")
    await _wake(storage, "x1", PARENT, state="dropped")
    await _wake(storage, "p1", PARENT)
    await _wake(storage, "d2", PARENT, state="delivered", now=_T0 + 100)

    assert [row["group_key"] for row in await storage.list_subagent_wakes("awaiting")] == ["a1"]
    assert await storage.prune_subagent_wakes(_T0 + 50) == 3
    left = {key for key in ("a1", "d1", "x1", "p1", "d2") if await storage.get_subagent_wake(key)}
    assert left == {"p1", "d2"}
