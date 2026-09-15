"""``SessionManager`` is the funnel ``agentos.session.naming`` says it is.

That module documents ``normalize_session_name`` as the function every write
path goes through, but only the RPC handlers called it. A writer that reaches
the manager directly stored the name raw — the cron ``agent_run`` handler
builds one out of the job's user-typed ``--name``, so a newline or a control
byte in ``agentos cron add --name`` landed in the session row and then in every
surface that renders it.

Assertions are on the stored row, not on what a caller passed in.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio

from agentos.scheduler.delivery import DeliveryChain
from agentos.scheduler.handlers import make_agent_run_handler
from agentos.scheduler.payloads import make_agent_turn_payload
from agentos.scheduler.types import CronJob, DeliveryConfig, SessionTarget
from agentos.session.manager import SessionManager
from agentos.session.naming import MAX_SESSION_NAME_LENGTH
from agentos.session.storage import SessionStorage

_KEY = "agent:main:cli:aaa"


@pytest_asyncio.fixture
async def storage():
    store = SessionStorage(":memory:")
    await store.connect()
    yield store
    await store.close()


@pytest_asyncio.fixture
async def mgr(storage):
    return SessionManager(storage)


async def _stored_name(mgr: SessionManager, key: str = _KEY) -> str | None:
    node = await mgr.get_session(key)
    assert node is not None
    return node.display_name


@pytest.mark.asyncio
async def test_create_collapses_newlines_and_drops_control_bytes(mgr: SessionManager) -> None:
    await mgr.create(_KEY, display_name="deploy\nwatcher")

    assert await _stored_name(mgr) == "deploy watcher"


@pytest.mark.asyncio
async def test_update_normalizes_the_same_way(mgr: SessionManager) -> None:
    await mgr.create(_KEY, display_name="fine")

    await mgr.update(_KEY, display_name="two\t\tlines\nhere")

    assert await _stored_name(mgr) == "two lines here"


@pytest.mark.asyncio
async def test_create_caps_the_stored_name(mgr: SessionManager) -> None:
    await mgr.create(_KEY, display_name="x" * 400)

    stored = await _stored_name(mgr)
    assert stored is not None
    assert len(stored) == MAX_SESSION_NAME_LENGTH


@pytest.mark.asyncio
async def test_a_whitespace_only_name_clears_rather_than_stores_blank(mgr: SessionManager) -> None:
    await mgr.create(_KEY, display_name="   \n ")

    assert await _stored_name(mgr) is None


@pytest.mark.asyncio
async def test_an_already_normalized_name_is_untouched(mgr: SessionManager) -> None:
    """The normalizer is idempotent, so the RPC paths that already ran it are unaffected."""
    await mgr.create(_KEY, display_name="api refactor")

    assert await _stored_name(mgr) == "api refactor"


@pytest.mark.asyncio
async def test_a_session_without_a_name_is_unchanged(mgr: SessionManager) -> None:
    await mgr.create(_KEY)

    assert await _stored_name(mgr) is None


class _TurnRunner:
    """Minimal stand-in: the handler only needs a turn that yields and persists."""

    def __init__(self, session_manager: SessionManager) -> None:
        self._sm = session_manager

    def run(self, **kwargs):
        async def events():
            await self._sm.append_message(kwargs["session_key"], role="assistant", content="ok")
            yield SimpleNamespace(kind="message", text="ok")
            yield SimpleNamespace(kind="done")

        return events()


@pytest.mark.asyncio
async def test_a_cron_job_name_reaches_the_session_row_normalized(
    mgr: SessionManager, storage: SessionStorage
) -> None:
    """The public path: ``agentos cron add --name`` -> job fires -> session row.

    ``agent_run_handler`` stores ``f"Cron: {job.name[:50]}"``, so the job's
    name is carried into the row verbatim.
    """
    job = CronJob(
        id="triage",
        name="nightly\ndeploywatch",
        handler_key="agent_run",
        payload=make_agent_turn_payload("Report anything odd.", "main"),
        session_target=SessionTarget.ISOLATED,
        timeout_seconds=30.0,
        delivery=DeliveryConfig(best_effort=True),
    )
    handler = make_agent_run_handler(
        DeliveryChain(),
        turn_runner_ref=lambda: _TurnRunner(mgr),
        session_manager_ref=lambda: mgr,
    )

    await handler(job)

    rows = await storage.list_sessions(limit=10)
    names = [row.display_name for row in rows if row.display_name]
    assert names == ["Cron: nightly deploywatch"]
