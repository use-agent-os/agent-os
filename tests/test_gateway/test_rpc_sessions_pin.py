"""``sessions.pin`` stores a pin every client shares.

These run against a real ``SessionManager``/``SessionStorage`` rather than a
fake, so a handler that "succeeds" without writing the column would fail here
the way it fails in the gateway.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from agentos.gateway.config import GatewayConfig
from agentos.gateway.rpc import RpcContext, get_dispatcher
from agentos.session.manager import SessionManager
from agentos.session.storage import SessionStorage

KEY = "agent:main:main"
OTHER = "agent:main:other"


@pytest_asyncio.fixture
async def manager():
    storage = SessionStorage(":memory:")
    await storage.connect()
    mgr = SessionManager(storage, inject_time_prefix=False)
    yield mgr
    await storage.close()


@pytest.fixture
def dispatcher():
    return get_dispatcher()


def make_ctx(session_manager) -> RpcContext:
    ctx = RpcContext(conn_id="test-conn", config=GatewayConfig())
    ctx.session_manager = session_manager
    return ctx


async def _pin(dispatcher, ctx, key: str, pinned: bool | None = None):
    params: dict = {"key": key}
    if pinned is not None:
        params["pinned"] = pinned
    res = await dispatcher.dispatch("r1", "sessions.pin", params, ctx)
    assert res.ok is True, res.error
    return res.payload


async def _rows(dispatcher, ctx, **params):
    res = await dispatcher.dispatch("r1", "sessions.list", params, ctx)
    assert res.ok is True, res.error
    return res.payload["sessions"]


@pytest.mark.asyncio
async def test_pin_defaults_to_pinning_and_persists(dispatcher, manager):
    await manager.create(KEY)
    ctx = make_ctx(manager)

    result = await _pin(dispatcher, ctx, KEY)

    assert result["key"] == KEY
    assert result["pinned"] is True
    assert result["changed"] is True
    stored = await manager.get_session(KEY)
    assert stored is not None
    assert stored.pinned_at == result["pinnedAt"]
    assert isinstance(stored.pinned_at, int)


@pytest.mark.asyncio
async def test_unpin_clears_the_pin(dispatcher, manager):
    await manager.create(KEY)
    ctx = make_ctx(manager)
    await _pin(dispatcher, ctx, KEY)

    result = await _pin(dispatcher, ctx, KEY, False)

    assert result["pinned"] is False
    assert result["pinnedAt"] is None
    stored = await manager.get_session(KEY)
    assert stored is not None
    assert stored.pinned_at is None


@pytest.mark.asyncio
async def test_pinning_does_not_touch_updated_at(dispatcher, manager):
    """A pin arranges the list; it is not activity, so the row must not jump to "now"."""
    node = await manager.create(KEY)
    await manager._storage.conn.execute(
        "UPDATE sessions SET updated_at = 1000 WHERE session_key = ?", (node.session_key,)
    )
    await manager._storage.conn.commit()
    ctx = make_ctx(manager)

    await _pin(dispatcher, ctx, KEY)

    stored = await manager.get_session(KEY)
    assert stored is not None
    assert stored.updated_at == 1000


@pytest.mark.asyncio
async def test_repinning_keeps_the_original_pin_time(dispatcher, manager):
    await manager.create(KEY)
    ctx = make_ctx(manager)
    first = await _pin(dispatcher, ctx, KEY)

    again = await _pin(dispatcher, ctx, KEY)

    assert again["changed"] is False
    assert again["pinnedAt"] == first["pinnedAt"]


@pytest.mark.asyncio
async def test_a_stale_upsert_does_not_undo_a_pin(dispatcher, manager):
    """A turn that read the row before the pin writes it back afterwards."""
    await manager.create(KEY)
    stale = await manager.get_session(KEY)
    assert stale is not None
    ctx = make_ctx(manager)
    await _pin(dispatcher, ctx, KEY)

    stale.display_name = "written by the turn"
    await manager._storage.upsert_session(stale)

    stored = await manager.get_session(KEY)
    assert stored is not None
    assert stored.display_name == "written by the turn"
    assert stored.pinned_at is not None

    # ...and the same stale snapshot must not resurrect a pin removed since.
    pinned_snapshot = await manager.get_session(KEY)
    assert pinned_snapshot is not None
    await _pin(dispatcher, ctx, KEY, False)
    await manager._storage.upsert_session(pinned_snapshot)
    stored = await manager.get_session(KEY)
    assert stored is not None
    assert stored.pinned_at is None


@pytest.mark.asyncio
async def test_pin_survives_a_generic_update(dispatcher, manager):
    await manager.create(KEY)
    ctx = make_ctx(manager)
    await _pin(dispatcher, ctx, KEY)

    await manager.update(KEY, display_name="renamed")

    stored = await manager.get_session(KEY)
    assert stored is not None
    assert stored.pinned_at is not None


@pytest.mark.asyncio
async def test_pin_resolves_a_session_by_its_name(dispatcher, manager):
    await manager.create(KEY)
    await manager.update(KEY, display_name="quote-post")
    ctx = make_ctx(manager)

    result = await _pin(dispatcher, ctx, "quote-post")

    assert result["key"] == KEY


@pytest.mark.asyncio
async def test_pin_rejects_an_unknown_session_and_a_non_boolean(dispatcher, manager):
    await manager.create(KEY)
    ctx = make_ctx(manager)

    missing = await dispatcher.dispatch("r1", "sessions.pin", {"key": "agent:main:nope"}, ctx)
    assert missing.ok is False

    bad = await dispatcher.dispatch("r1", "sessions.pin", {"key": KEY, "pinned": "yes"}, ctx)
    assert bad.ok is False
    stored = await manager.get_session(KEY)
    assert stored is not None
    assert stored.pinned_at is None


@pytest.mark.asyncio
async def test_list_reports_the_pin(dispatcher, manager):
    await manager.create(KEY)
    await manager.create(OTHER)
    ctx = make_ctx(manager)
    result = await _pin(dispatcher, ctx, KEY)

    rows = {r["key"]: r for r in await _rows(dispatcher, ctx, limit=10)}

    assert rows[KEY]["pinned"] is True
    assert rows[KEY]["pinnedAt"] == rows[KEY]["pinned_at"] == result["pinnedAt"]
    assert rows[OTHER]["pinned"] is False
    assert rows[OTHER]["pinnedAt"] is None


@pytest.mark.asyncio
async def test_list_keeps_a_pinned_session_that_fell_off_the_page(dispatcher, manager):
    """The page is the newest ``limit`` rows; a quiet pinned session is older than that."""
    await manager.create(KEY)
    ctx = make_ctx(manager)
    await _pin(dispatcher, ctx, KEY)
    await manager._storage.conn.execute(
        "UPDATE sessions SET updated_at = 1 WHERE session_key = ?", (KEY,)
    )
    await manager._storage.conn.commit()
    for i in range(3):
        await manager.create(f"agent:main:new{i}")

    rows = await _rows(dispatcher, ctx, limit=2, includePinned=True)

    keys = [r["key"] for r in rows]
    assert KEY in keys
    assert len(keys) == len(set(keys)) == 3

    # Listed once when it is on the page anyway.
    rows = await _rows(dispatcher, ctx, limit=50, includePinned=True)
    assert [r["key"] for r in rows].count(KEY) == 1

    # Opt-in: without it ``limit`` is an exact ceiling, as callers that page
    # on it (the MCP bridge, the CLI) expect.
    rows = await _rows(dispatcher, ctx, limit=2)
    assert len(rows) == 2
    assert KEY not in [r["key"] for r in rows]


@pytest.mark.asyncio
async def test_resolve_reports_the_pin(dispatcher, manager):
    await manager.create(KEY)
    ctx = make_ctx(manager)
    await _pin(dispatcher, ctx, KEY)

    res = await dispatcher.dispatch("r1", "sessions.resolve", {"key": KEY}, ctx)

    assert res.ok is True, res.error
    assert res.payload["pinned"] is True


@pytest.mark.asyncio
async def test_storage_adds_the_column_to_an_older_database(tmp_path):
    import aiosqlite

    path = tmp_path / "sessions.sqlite"
    storage = SessionStorage(str(path))
    await storage.connect()
    await SessionManager(storage, inject_time_prefix=False).create(KEY)
    await storage.close()
    async with aiosqlite.connect(path) as conn:
        await conn.execute("ALTER TABLE sessions DROP COLUMN pinned_at")
        await conn.commit()

    storage = SessionStorage(str(path))
    await storage.connect()
    try:
        mgr = SessionManager(storage, inject_time_prefix=False)
        node = await mgr.set_session_pinned(KEY, True)
        assert node.pinned_at is not None
        assert [s.session_key for s in await storage.list_pinned_sessions()] == [KEY]
    finally:
        await storage.close()
