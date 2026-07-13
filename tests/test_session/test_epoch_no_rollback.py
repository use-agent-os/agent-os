"""AC-R3-EPOCH: upsert/update paths must never roll back an incremented epoch.

AC-R3-EPOCH-1: All sessions table UPSERT/UPDATE paths do not revert epoch.
AC-R3-EPOCH-2: get_or_create after reset preserves epoch.
AC-R3-EPOCH-3: update (field patch) after reset preserves epoch.
AC-R3-EPOCH-4: concurrent reset + update leaves epoch >= reset value.
AC-R3-EPOCH-HARD-1: upsert_session uses MAX clause — stale epoch=0 cannot lower DB epoch=5.
AC-R3-EPOCH-HARD-2: DB trigger blocks direct UPDATE that would decrease epoch.
AC-R3-EPOCH-HARD-3: high-concurrency mix of increment + upsert leaves epoch >= increment count.
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from agentos.session.manager import SessionManager
from agentos.session.models import SessionNode
from agentos.session.storage import SessionStorage


@pytest_asyncio.fixture
async def storage():
    s = SessionStorage(":memory:")
    await s.connect()
    yield s
    await s.close()


@pytest_asyncio.fixture
async def mgr(storage):
    return SessionManager(storage)


# ── AC-R3-EPOCH-2 ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_or_create_after_reset_preserves_epoch(storage, mgr):
    """get_or_create on an existing key must not overwrite the post-reset epoch.

    Steps:
    1. Create session.
    2. increment_epoch (epoch -> 1).
    3. Call get_or_create with same key (simulates a stale racing caller).
    4. Assert epoch is still 1.
    """
    key = "agent:main:epoch-goc"
    await mgr.create(key)
    assert await storage.get_epoch(key) == 0

    await storage.increment_epoch(key)
    assert await storage.get_epoch(key) == 1

    # get_or_create should return the existing session without touching epoch.
    node, created = await mgr.get_or_create(key)
    assert not created, "session already exists — created must be False"
    assert await storage.get_epoch(key) == 1, (
        f"epoch rolled back by get_or_create; expected 1, got {await storage.get_epoch(key)}"
    )


# ── AC-R3-EPOCH-3 ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_session_after_reset_preserves_epoch(storage, mgr):
    """Patching a field via manager.update() must not revert epoch.

    Steps:
    1. Create session.
    2. increment_epoch (epoch -> 1).
    3. Call manager.update(last_channel=...).
    4. Assert epoch is still 1.
    """
    key = "agent:main:epoch-update"
    await mgr.create(key)

    await storage.increment_epoch(key)
    assert await storage.get_epoch(key) == 1

    # update reads the current node (epoch=1 in DB) then upserts it back.
    await mgr.update(key, last_channel="tg")

    assert await storage.get_epoch(key) == 1, (
        "epoch rolled back by manager.update(); expected 1"
    )


# ── AC-R3-EPOCH-3 (direct upsert variant) ────────────────────────────────────


@pytest.mark.asyncio
async def test_direct_upsert_with_stale_epoch_preserves_db_epoch(storage):
    """Direct storage.upsert_session with a stale node must not lower the DB epoch.

    This covers callers that hold a SessionNode snapshot from before a reset
    and then call upsert_session with epoch=0 in the node.
    """
    key = "agent:main:direct-upsert"
    node = SessionNode(session_key=key, session_id="sid-du")
    await storage.upsert_session(node)

    await storage.increment_epoch(key)
    assert await storage.get_epoch(key) == 1

    # Simulate a stale caller that still holds node with epoch=0.
    node.last_channel = "slack"
    # node.epoch is still 0 — the stale value.
    await storage.upsert_session(node)

    assert await storage.get_epoch(key) == 1, (
        "direct upsert with stale node (epoch=0) rolled back the DB epoch to 0"
    )


# ── AC-R3-EPOCH-4 ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_reset_and_update_session_no_rollback(storage, mgr):
    """Concurrent reset and update_session: epoch must not go below reset value.

    Both orderings are tested deterministically:
    - reset first, then update
    - update first, then reset
    In both cases the final epoch must be >= 1.
    """
    # Ordering A: reset before update
    key_a = "agent:main:concurrent-a"
    await mgr.create(key_a)
    await storage.increment_epoch(key_a)  # epoch -> 1
    # Now update (reads epoch=1 from DB, upserts back — must not write epoch=0)
    await mgr.update(key_a, last_channel="discord")
    assert await storage.get_epoch(key_a) >= 1, (
        "Ordering A: update after reset rolled epoch back"
    )

    # Ordering B: update before reset
    key_b = "agent:main:concurrent-b"
    await mgr.create(key_b)
    # Update first (epoch still 0)
    await mgr.update(key_b, last_channel="telegram")
    # Then reset
    await storage.increment_epoch(key_b)  # epoch -> 1
    assert await storage.get_epoch(key_b) >= 1, (
        "Ordering B: epoch should be 1 after reset, got something else"
    )

    # Ordering C: truly concurrent via asyncio.gather
    key_c = "agent:main:concurrent-c"
    await mgr.create(key_c)

    async def _reset():
        await storage.increment_epoch(key_c)

    async def _update():
        await mgr.update(key_c, last_channel="web")

    await asyncio.gather(_reset(), _update())
    final_epoch = await storage.get_epoch(key_c)
    assert final_epoch >= 1, (
        f"Concurrent reset+update left epoch={final_epoch}, expected >= 1"
    )


# ── AC-R3-EPOCH-HARD-1 ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_with_lower_epoch_no_rollback(storage):
    """upsert_session with a stale epoch=0 node must not lower a DB epoch=5.

    Verifies the MAX(sessions.epoch, excluded.epoch) clause in the UPDATE path.
    """
    key = "agent:main:hard-max-epoch"
    node = SessionNode(session_key=key, session_id="sid-hard1")
    await storage.upsert_session(node)

    # Advance to epoch 5 via five increments.
    for _ in range(5):
        await storage.increment_epoch(key)
    assert await storage.get_epoch(key) == 5

    # Caller still holds the original node with epoch=0.
    node.last_channel = "web"
    # node.epoch is still 0 at this point.
    await storage.upsert_session(node)

    assert await storage.get_epoch(key) == 5, (
        "upsert with stale epoch=0 rolled back DB epoch=5"
    )


# ── AC-R3-EPOCH-HARD-2 ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_db_trigger_blocks_epoch_decrease(storage):
    """Direct SQL UPDATE that would decrease epoch must be rejected by the trigger."""

    key = "agent:main:trigger-block"
    node = SessionNode(session_key=key, session_id="sid-hard2")
    await storage.upsert_session(node)
    await storage.increment_epoch(key)
    assert await storage.get_epoch(key) == 1

    # Attempt to directly lower epoch to 0 — the trigger must ABORT this.
    with pytest.raises(Exception):
        await storage.conn.execute(
            "UPDATE sessions SET epoch = 0 WHERE session_key = ?", (key,)
        )
        await storage.conn.commit()

    # DB epoch must still be 1.
    assert await storage.get_epoch(key) == 1, (
        "trigger did not block the epoch decrease — DB epoch was lowered"
    )


# ── AC-R3-EPOCH-HARD-3 ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_increment_and_upsert_high_concurrency(storage):
    """50 increments concurrent with 50 stale upserts: final epoch must be >= 50."""
    key = "agent:main:high-concurrency"
    node = SessionNode(session_key=key, session_id="sid-hard3")
    await storage.upsert_session(node)

    async def _increment():
        await storage.increment_epoch(key)

    async def _stale_upsert():
        # node.epoch is 0 — always stale relative to the increments.
        await storage.upsert_session(node)

    tasks = [_increment() for _ in range(50)] + [_stale_upsert() for _ in range(50)]
    await asyncio.gather(*tasks)

    final_epoch = await storage.get_epoch(key)
    assert final_epoch >= 50, (
        f"high-concurrency mix left epoch={final_epoch}, expected >= 50"
    )
