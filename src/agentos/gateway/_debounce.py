from __future__ import annotations

# fmt: off
# ruff: noqa: E501
import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any, Protocol

import structlog

from agentos.channels.types import IncomingMessage

log = structlog.get_logger(__name__)

# Maximum messages buffered per session_key before the debounce batch is
# delivered early. Without a cap, a sender who posts faster than the window
# expires grows state.buffer unboundedly — O(messages) memory and attachment
# references until the timer fires (#796). A 50-message cap bounds the batch
# regardless of post rate; the excess flushes immediately instead of being
# dropped.
MAX_COALESCED_MESSAGES = 50


class DebounceCoordinator(Protocol):
    async def schedule(self, session_key: str, message: IncomingMessage, *, window_s: float, on_fire: Any) -> None: ...


class _DefaultDebounceCoordinator:
    def __init__(self) -> None:
        self._pending: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        # Strong references to in-flight cap-triggered deliveries. The event
        # loop holds only weak references to tasks, so a fire-and-forget
        # create_task can be GC'd mid-execution and the batch is silently
        # lost; retaining the task until completion prevents that and lets
        # cancel_all() drain in-flight flushes at shutdown.
        self._deliveries: set[asyncio.Task[None]] = set()

    async def schedule(self, session_key: str, message: IncomingMessage, *, window_s: float, on_fire: Any) -> None:
        async with self._lock:
            if state := self._pending.get(session_key):
                state.buffer.append(message)
                if len(state.buffer) >= MAX_COALESCED_MESSAGES:
                    # Cap hit — deliver the whole batch immediately so the buffer
                    # stays bounded. Pop first, then cancel the sleeping timer:
                    # cancel keeps the orphan from waking later and stealing the
                    # *next* window's state (#796).
                    self._pending.pop(session_key, None)
                    state.task.cancel()
                    log.warning(
                        "channel.debounce_buffer_cap_reached",
                        session_key=session_key,
                        coalesced_count=len(state.buffer),
                    )
                    # Retain the delivery task: the loop only holds weak
                    # references, so an unreferenced task can be GC'd before
                    # it runs and the whole batch is silently lost.
                    delivery = asyncio.create_task(self._deliver(session_key, state.buffer, state.on_fire))
                    self._deliveries.add(delivery)
                    delivery.add_done_callback(self._deliveries.discard)
                return
            task = asyncio.create_task(self._fire(session_key, window_s), name=f"channel-debounce:{session_key}")
            self._pending[session_key] = SimpleNamespace(buffer=[message], on_fire=on_fire, task=task)

    async def cancel(self, session_key: str) -> None:
        async with self._lock:
            state = self._pending.pop(session_key, None)
        if state is None or state.task.done():
            return
        log.info("channel.debounce_cancelled", session_key=session_key)
        state.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await state.task

    async def cancel_all(self) -> None:
        await asyncio.gather(*(self.cancel(k) for k in list(self._pending)), return_exceptions=True)
        # Drain in-flight cap-triggered deliveries so no batch is dropped on
        # gateway shutdown.
        if self._deliveries:
            await asyncio.gather(*self._deliveries, return_exceptions=True)

    async def _fire(self, session_key: str, window_s: float) -> None:
        try:
            await asyncio.sleep(window_s)
            async with self._lock:
                state = self._pending.pop(session_key, None)
            if state is None:
                return
        except asyncio.CancelledError:
            raise
        await self._deliver(session_key, state.buffer, state.on_fire)

    async def _deliver(self, session_key: str, buffer: list[IncomingMessage], on_fire: Any) -> None:
        try:
            first = buffer[0]
            content = "\n".join(m.content for m in buffer)
            attachments = [a for m in buffer for a in (m.attachments or [])]
            msg = IncomingMessage(sender_id=first.sender_id, channel_id=first.channel_id, content=content, attachments=attachments, metadata=dict(first.metadata or {}))
            combined = SimpleNamespace(content=content, attachments=attachments, message=msg, coalesced_count=len(buffer))
            log.info("channel.debounce_coalesced", session_key=session_key, coalesced_count=combined.coalesced_count)
            await on_fire(combined)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("channel_dispatch.debounce_enqueue_failed", session_key=session_key, reason="unexpected", error=str(exc))
