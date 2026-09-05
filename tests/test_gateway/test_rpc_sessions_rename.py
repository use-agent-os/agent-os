"""Issue #248: ``sessions.rename`` gives a session a retrievable name.

These run against a real ``SessionManager``/``SessionStorage`` rather than a
fake, so a handler that "succeeds" without writing the column would fail here
the way it fails in the gateway.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from agentos.channels.command_replies import format_channel_success_reply
from agentos.engine.commands import DEFAULT_REGISTRY, Surface
from agentos.gateway.access import CHANNEL_RPC_METHODS, ConnectionSurface
from agentos.gateway.auth import AccessContext
from agentos.gateway.config import GatewayConfig
from agentos.gateway.rpc import RpcContext, get_dispatcher
from agentos.session.manager import SessionManager
from agentos.session.storage import SessionStorage

KEY = "agent:main:main"


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


async def _dispatch(dispatcher, ctx, method: str, params: dict):
    res = await dispatcher.dispatch("r1", method, params, ctx)
    return res


async def _rename(dispatcher, ctx, key: str, name):
    res = await _dispatch(dispatcher, ctx, "sessions.rename", {"key": key, "name": name})
    assert res.ok is True, res.error
    return res.payload


@pytest.mark.asyncio
async def test_rename_persists_the_display_name(dispatcher, manager):
    await manager.create(KEY)
    ctx = make_ctx(manager)

    result = await _rename(dispatcher, ctx, KEY, "api-refactor")

    assert result["key"] == KEY
    assert result["name"] == "api-refactor"
    assert result["previousName"] is None

    stored = await manager.get_session(KEY)
    assert stored is not None
    assert stored.display_name == "api-refactor"
    assert stored.derived_title == "api-refactor"


@pytest.mark.asyncio
async def test_rename_normalizes_the_name(dispatcher, manager):
    await manager.create(KEY)
    ctx = make_ctx(manager)

    result = await _rename(dispatcher, ctx, KEY, "  deep   research\npricing  ")

    assert result["name"] == "deep research pricing"


@pytest.mark.asyncio
async def test_empty_name_clears_the_custom_name(dispatcher, manager):
    node = await manager.create(KEY)
    ctx = make_ctx(manager)
    await _rename(dispatcher, ctx, KEY, "bug-46")

    result = await _rename(dispatcher, ctx, KEY, "   ")

    assert result["name"] is None
    assert result["previousName"] == "bug-46"

    stored = await manager.get_session(KEY)
    assert stored is not None
    assert stored.display_name is None
    # Falls back to the short opaque id, so list rows never go blank.
    assert stored.derived_title == node.session_id[:8]


@pytest.mark.asyncio
async def test_rename_resolves_a_session_by_its_current_name(dispatcher, manager):
    await manager.create(KEY)
    ctx = make_ctx(manager)
    await _rename(dispatcher, ctx, KEY, "bug-46")

    # Same targeting `/resume` and `sessions show` accept — rename by the
    # name you can actually see, not only by the full session key.
    result = await _rename(dispatcher, ctx, "bug-46", "bug-46-fixed")

    assert result["key"] == KEY
    stored = await manager.get_session(KEY)
    assert stored is not None
    assert stored.display_name == "bug-46-fixed"


@pytest.mark.asyncio
async def test_rename_rejects_an_unknown_session(dispatcher, manager):
    ctx = make_ctx(manager)

    res = await _dispatch(
        dispatcher, ctx, "sessions.rename", {"key": "agent:main:nope", "name": "whatever"}
    )

    assert res.ok is False


@pytest.mark.asyncio
async def test_rename_requires_a_name_argument(dispatcher, manager):
    await manager.create(KEY)
    ctx = make_ctx(manager)

    res = await _dispatch(dispatcher, ctx, "sessions.rename", {"key": KEY})

    assert res.ok is False


@pytest.mark.asyncio
async def test_patch_normalizes_the_display_name(dispatcher, manager):
    await manager.create(KEY)
    ctx = make_ctx(manager)

    res = await _dispatch(
        dispatcher, ctx, "sessions.patch", {"key": KEY, "displayName": "  spaced   out  "}
    )
    assert res.ok is True, res.error

    stored = await manager.get_session(KEY)
    assert stored is not None
    assert stored.display_name == "spaced out"


@pytest.mark.asyncio
async def test_list_exposes_the_derived_title(dispatcher, manager):
    await manager.create(KEY)
    ctx = make_ctx(manager)
    await _rename(dispatcher, ctx, KEY, "api-refactor")

    res = await _dispatch(dispatcher, ctx, "sessions.list", {"limit": 10})
    assert res.ok is True, res.error
    row = next(r for r in res.payload["sessions"] if r["key"] == KEY)

    # The Web UI session filter scores derived_title; it has to be shipped.
    assert row["display_name"] == "api-refactor"
    assert row["derived_title"] == "api-refactor"
    assert row["derivedTitle"] == "api-refactor"


# ── Channel surface ──────────────────────────────────────────────────────────


def test_every_channel_command_method_is_allowlisted() -> None:
    """A CHANNEL `CommandDef` whose RPC is not allowlisted is dead on arrival.

    `validate_classification` only compares handler audiences to the
    allowlist; nothing checked the third leg — the command registry — which is
    how `/rename` shipped as a channel command the gateway then refused.
    """
    methods = {
        command.rpc_method
        for command in DEFAULT_REGISTRY.for_surface(Surface.CHANNEL)
        if command.rpc_method
    }

    assert methods <= CHANNEL_RPC_METHODS, sorted(methods - CHANNEL_RPC_METHODS)
    assert "sessions.rename" in methods


@pytest.mark.asyncio
async def test_rename_is_reachable_from_the_channel_surface(dispatcher, manager):
    await manager.create(KEY)
    ctx = RpcContext(
        conn_id="test-conn",
        config=GatewayConfig(),
        access=AccessContext(
            surface=ConnectionSurface.CHANNEL,
            admitted=True,
            credential_verified=True,
        ),
    )
    ctx.session_manager = manager

    res = await dispatcher.dispatch("r1", "sessions.rename", {"key": KEY, "name": "bug-46"}, ctx)

    assert res.ok is True, res.error
    stored = await manager.get_session(KEY)
    assert stored is not None
    assert stored.display_name == "bug-46"


def test_channel_reply_reports_the_stored_name() -> None:
    reply = format_channel_success_reply(
        name="rename", method="sessions.rename", payload={"key": KEY, "name": "bug 46"}
    )
    assert reply == "Session renamed to bug 46."

    cleared = format_channel_success_reply(
        name="rename", method="sessions.rename", payload={"key": KEY, "name": None}
    )
    assert cleared == "Cleared the session name."


# ── Resolution ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_exact_name_wins_over_session_key_prefix_matches(dispatcher, manager):
    """A user is free to name a session "agent", which prefix-matches every key."""
    await manager.create(KEY)
    await manager.create("agent:main:cli:other")
    ctx = make_ctx(manager)
    await _rename(dispatcher, ctx, KEY, "agent")

    result = await _rename(dispatcher, ctx, "agent", "agent-renamed")

    assert result["key"] == KEY
    other = await manager.get_session("agent:main:cli:other")
    assert other is not None
    assert other.display_name is None


@pytest.mark.asyncio
async def test_rename_with_invalid_params(dispatcher, manager):
    ctx = make_ctx(manager)
    res = await dispatcher.dispatch("r1", "sessions.rename", None, ctx)
    assert res.ok is False
    assert res.error.code == "INVALID_REQUEST"
