"""Bug C2 invariant test — AC-C2-2.

Verifies that two concurrent asyncio Tasks calling TurnRunner.run() for the
same session are properly serialized (max in-flight == 1) rather than running
concurrently due to the broken lock.locked() owner-blind check.

The fix replaces lock.locked() with a ContextVar-based owner check so Task B
cannot skip lock acquisition just because Task A holds it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agentos.engine.runtime import TurnRunner
from agentos.engine.types import DoneEvent


def _make_runner_with_lock(
    shared_lock: asyncio.Lock,
) -> TurnRunner:
    """Build a minimal TurnRunner whose session lock is the provided shared_lock."""
    provider = MagicMock()
    provider.provider_name = "stub"

    async def _chat(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        # Yield a small delay so concurrent tasks have time to overlap if
        # serialization is broken.
        await asyncio.sleep(0)
        yield DoneEvent(stop_reason="end_turn", usage={})

    provider.chat = _chat

    selector = MagicMock()
    selector.resolve.return_value = provider
    selector.clone.return_value = selector
    selector.current_config = MagicMock(model="stub-model")

    session_manager = MagicMock()
    session_manager.get = AsyncMock(return_value=None)
    session_manager.append_message = AsyncMock(return_value=None)
    session_manager.update = AsyncMock(return_value=None)
    session_manager.get_compaction_summary = AsyncMock(return_value=None)

    return TurnRunner(
        provider_selector=selector,
        session_manager=session_manager,
        session_lock_provider=lambda key: shared_lock,
    )


@pytest.mark.asyncio
async def test_two_concurrent_runs_serialize() -> None:
    """Two concurrent TurnRunner.run() calls for the same session must serialize.

    AC-C2-2: max tasks in-flight simultaneously must be 1, not 2.

    If the bug is present (lock.locked() used instead of ContextVar owner check),
    Task B sees locked()=True while Task A holds the lock and skips acquire,
    causing both to run concurrently (max_in_flight == 2).

    With the fix, Task B correctly waits for Task A to release the lock.
    """
    session_key = "agent:main:concurrent-run-test"
    shared_lock = asyncio.Lock()
    runner = _make_runner_with_lock(shared_lock)

    from agentos.tools.types import ToolContext

    tool_ctx = ToolContext(session_key=session_key)

    in_flight = 0
    max_in_flight = 0

    # Patch _run_turn to track concurrency
    original_run_turn = runner._run_turn

    async def _instrumented_run_turn(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        if in_flight > max_in_flight:
            max_in_flight = in_flight
        try:
            async for event in original_run_turn(*args, **kwargs):
                yield event
        finally:
            in_flight -= 1

    runner._run_turn = _instrumented_run_turn  # type: ignore[method-assign]

    async def _run_one() -> None:
        async for _ in runner.run(
            message="hello",
            session_key=session_key,
            tool_context=tool_ctx,
        ):
            pass

    # Launch two concurrent tasks for the same session
    await asyncio.gather(_run_one(), _run_one())

    assert max_in_flight == 1, (
        f"Expected max 1 concurrent _run_turn execution per session, "
        f"got {max_in_flight}. "
        "Two concurrent TurnRunner.run() calls are not serialized — "
        "the lock.locked() owner-blind check is still present."
    )
