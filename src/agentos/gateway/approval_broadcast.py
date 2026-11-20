"""Push approval queue changes to Control connections over the WebSocket.

The approval queue is synchronous and may be written from a worker thread, so
its listener only hands each change to the event loop. The loop side coalesces:
a row resolved before its ``approval.requested`` went out (the auto-approve /
auto-deny policies resolve in the same call that queued it) is never announced
at all, so a policy decision does not flash a prompt in every open console.

It also sweeps the queue on a timer, so a prompt whose lifespan ended is
announced as ``expired`` even when nothing waits on it any more.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from agentos.application.approval_events import (
    APPROVAL_REQUESTED,
    APPROVAL_RESOLVED,
    add_approval_listener,
)

log = structlog.get_logger(__name__)

Broadcast = Callable[[str, dict[str, Any]], Awaitable[None]]

#: How often the queue is swept for prompts whose lifespan ended.
SWEEP_INTERVAL_S = 30.0


class ApprovalBroadcaster:
    """Queue listener that forwards changes to ``broadcast`` on ``loop``."""

    def __init__(self, loop: asyncio.AbstractEventLoop, broadcast: Broadcast) -> None:
        self._loop = loop
        self._broadcast = broadcast
        self._unannounced: dict[str, dict[str, Any]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._remove: Callable[[], None] | None = None
        self._sweeper: asyncio.Task[None] | None = None

    def install(self) -> ApprovalBroadcaster:
        self._remove = add_approval_listener(self)
        return self

    def start_sweeper(
        self,
        sweep: Callable[[], None],
        *,
        interval_s: float = SWEEP_INTERVAL_S,
    ) -> ApprovalBroadcaster:
        """Call ``sweep`` every ``interval_s`` on the loop until uninstalled."""

        async def _run() -> None:
            while True:
                await asyncio.sleep(interval_s)
                try:
                    sweep()
                except Exception:
                    log.warning("approval_broadcast.sweep_failed", exc_info=True)

        self._sweeper = self._loop.create_task(_run(), name="agentos-approval-sweeper")
        return self

    def uninstall(self) -> None:
        if self._remove is not None:
            self._remove()
            self._remove = None
        if self._sweeper is not None:
            self._sweeper.cancel()
            self._sweeper = None

    def __call__(self, event_name: str, payload: dict[str, Any]) -> None:
        if self._loop.is_closed():
            return
        try:
            self._loop.call_soon_threadsafe(self._on_loop, event_name, payload)
        except RuntimeError:
            # The loop is shutting down; a console reconnecting later backfills.
            return

    def _on_loop(self, event_name: str, payload: dict[str, Any]) -> None:
        approval_id = str(payload.get("id") or "")
        if not approval_id:
            return
        if event_name == APPROVAL_REQUESTED:
            self._unannounced[approval_id] = payload
            # One loop turn later: anything resolved in the same call stack as
            # the request has queued its own change by then.
            self._loop.call_soon(self._announce, approval_id)
            return
        if event_name == APPROVAL_RESOLVED:
            if self._unannounced.pop(approval_id, None) is not None:
                return
            self._send(event_name, payload)

    def _announce(self, approval_id: str) -> None:
        payload = self._unannounced.pop(approval_id, None)
        if payload is not None:
            self._send(APPROVAL_REQUESTED, payload)

    def _send(self, event_name: str, payload: dict[str, Any]) -> None:
        task = self._loop.create_task(self._deliver(event_name, payload))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _deliver(self, event_name: str, payload: dict[str, Any]) -> None:
        try:
            await self._broadcast(event_name, payload)
        except Exception:
            log.debug("approval_broadcast.send_failed", event_name=event_name, exc_info=True)

    async def drain(self) -> None:
        """Wait for the frames already handed to the loop (tests, shutdown)."""
        for _ in range(3):
            await asyncio.sleep(0)
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


def install_approval_broadcaster(
    loop: asyncio.AbstractEventLoop | None = None,
) -> ApprovalBroadcaster:
    """Forward approval queue changes to every Control connection; sweep on a timer."""
    from agentos.application.approval_queue import get_approval_queue
    from agentos.gateway.websocket import get_registry

    async def _broadcast(event_name: str, payload: dict[str, Any]) -> None:
        await get_registry().broadcast_control(event_name, payload)

    broadcaster = ApprovalBroadcaster(loop or asyncio.get_running_loop(), _broadcast).install()
    return broadcaster.start_sweeper(lambda: get_approval_queue().sweep())
