"""Issue #1973: ``SessionManager`` normalizes ``display_name`` on every write.

The RPC handlers already run user-typed names through
:func:`normalize_session_name`, but writers that reach the manager directly
(the cron ``agent_run`` handler builds ``display_name`` from a job's
``--name``) used to store the raw value. The manager is the choke point the
``naming`` module promises, so it normalizes on ``create()`` and ``update()``.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from agentos.session.manager import SessionManager
from agentos.session.naming import MAX_SESSION_NAME_LENGTH
from agentos.session.storage import SessionStorage


@pytest_asyncio.fixture
async def manager():
    storage = SessionStorage(":memory:")
    await storage.connect()
    mgr = SessionManager(storage, inject_time_prefix=False)
    yield mgr
    await storage.close()


@pytest.mark.asyncio
async def test_create_normalizes_display_name(manager: SessionManager) -> None:
    # Exactly what scheduler/handlers.py builds from a cron job's --name.
    job_name = "deploy\nwatcher\x07"
    node, created = await manager.get_or_create(
        session_key="agent:main:cron:j1",
        agent_id="main",
        display_name=f"Cron: {job_name[:50]}",
    )
    assert created
    assert node.display_name == "Cron: deploy watcher"

    stored = await manager.get_session("agent:main:cron:j1")
    assert stored is not None
    assert stored.display_name == "Cron: deploy watcher"


@pytest.mark.asyncio
async def test_create_caps_display_name_length(manager: SessionManager) -> None:
    node = await manager.create("agent:main:long", display_name="x" * 400)
    assert node.display_name is not None
    assert len(node.display_name) == MAX_SESSION_NAME_LENGTH


@pytest.mark.asyncio
async def test_create_blank_display_name_stores_none(manager: SessionManager) -> None:
    node = await manager.create("agent:main:blank", display_name="  \n\t ")
    assert node.display_name is None


@pytest.mark.asyncio
async def test_update_normalizes_display_name(manager: SessionManager) -> None:
    await manager.create("agent:main:upd")

    node = await manager.update("agent:main:upd", display_name="  bug\n46\x00 triage  ")
    assert node.display_name == "bug 46 triage"

    stored = await manager.get_session("agent:main:upd")
    assert stored is not None
    assert stored.display_name == "bug 46 triage"


@pytest.mark.asyncio
async def test_update_blank_display_name_clears_it(manager: SessionManager) -> None:
    await manager.create("agent:main:clear", display_name="keep me")

    node = await manager.update("agent:main:clear", display_name="   ")
    assert node.display_name is None


@pytest.mark.asyncio
async def test_update_without_display_name_leaves_it_alone(manager: SessionManager) -> None:
    await manager.create("agent:main:other", display_name="keep me")

    node = await manager.update("agent:main:other", label="something")
    assert node.display_name == "keep me"
    assert node.label == "something"


@pytest.mark.asyncio
async def test_non_string_display_name_is_rejected(manager: SessionManager) -> None:
    with pytest.raises(ValueError):
        await manager.create("agent:main:bad", display_name=42)
