"""Durable delivery of parent wakes for subagent spawn groups.

When every child of a spawn group has finished, ``subagent_announce`` wakes
the parent session with one message. That wake used to be best-effort: the
"already woken" marker lived in memory, so a restart could repeat or lose it,
and a failed ``TaskRuntime.send`` (for example while the runtime drains)
dropped it.

The outbox makes it durable:

* every wake is written to ``subagent_wakes`` (``pending``) *before* it is
  sent, keyed by the spawn group;
* a group whose row is ``delivered`` or ``dropped`` is never woken again, across
  restarts too, which makes delivery idempotent;
* the row turns ``delivered`` only once the task runtime accepted the wake; a
  failure keeps it ``pending`` with a backoff (1 s, 5 s, 30 s, then every 60 s)
  and the :class:`SubagentWakeWorker` retries it, giving up (``dropped``) after
  :data:`MAX_PENDING_AGE_S`;
* at startup the worker drains whatever a previous process left pending.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

log = structlog.get_logger(__name__)

#: Seconds between attempts: 1st retry, 2nd, 3rd, then every last value.
BACKOFF_S: tuple[float, ...] = (1.0, 5.0, 30.0, 60.0)
#: A wake still not delivered this long after it was first written is dropped.
MAX_PENDING_AGE_S = 30 * 60
#: How long a freshly claimed wake is left to its first sender before the
#: worker may take it (only matters when the sending process died meanwhile:
#: in-process the in-flight set already keeps the worker off it).
CLAIM_LEASE_S = 5.0

PENDING = "pending"
DELIVERED = "delivered"
DROPPED = "dropped"


def group_key(parent_session_key: str, parent_task_id: str | None) -> str:
    return f"{parent_session_key}::{parent_task_id or ''}"


def _now_ms() -> int:
    return int(time.time() * 1000)


class SubagentWakeOutbox:
    """State transitions of the ``subagent_wakes`` rows."""

    def __init__(self, storage: Any, *, clock: Callable[[], int] = _now_ms) -> None:
        self._storage = storage
        self._clock = clock
        #: Groups a sender in this process is delivering right now.
        self._in_flight: set[str] = set()

    async def claim(
        self,
        *,
        parent_session_key: str,
        parent_task_id: str | None,
        message: str,
        provenance: dict[str, Any],
        payloads: list[dict[str, Any]],
    ) -> bool:
        """Record the group's wake; ``True`` when this caller should send it now.

        ``False`` when the group already has a row: delivered or dropped (do not
        wake twice), or pending (the worker owns the retry).
        """

        key = group_key(parent_session_key, parent_task_id)
        now = self._clock()
        created, row = await self._storage.record_subagent_wake(
            group_key=key,
            parent_session_key=parent_session_key,
            parent_task_id=parent_task_id,
            message=message,
            provenance=provenance,
            payloads=payloads,
            now_ms=now,
            next_attempt_at=now + int(CLAIM_LEASE_S * 1000),
        )
        if created:
            self._in_flight.add(key)
        else:
            log.info(
                "subagent_wakes.already_recorded",
                parent_session_key=parent_session_key,
                parent_task_id=parent_task_id,
                state=row.get("state"),
            )
        return bool(created)

    async def drop(
        self, *, parent_session_key: str, parent_task_id: str | None, reason: str
    ) -> None:
        """Record that this group must not wake its parent (e.g. it was aborted)."""

        key = group_key(parent_session_key, parent_task_id)
        created, row = await self._storage.record_subagent_wake(
            group_key=key,
            parent_session_key=parent_session_key,
            parent_task_id=parent_task_id,
            message="",
            provenance={},
            payloads=[],
            now_ms=self._clock(),
            state=DROPPED,
            reason=reason,
        )
        if not created and row.get("state") == PENDING:
            await self._storage.update_subagent_wake(
                key, state=DROPPED, reason=reason, updated_at=self._clock()
            )

    def begin(self, parent_session_key: str, parent_task_id: str | None) -> None:
        """Mark a group as being delivered by this process (the worker skips it)."""

        self._in_flight.add(group_key(parent_session_key, parent_task_id))

    async def delivered(self, parent_session_key: str, parent_task_id: str | None) -> None:
        key = group_key(parent_session_key, parent_task_id)
        self._in_flight.discard(key)
        await self._storage.update_subagent_wake(
            key,
            state=DELIVERED,
            updated_at=self._clock(),
        )

    async def failed(self, parent_session_key: str, parent_task_id: str | None, error: str) -> None:
        """Schedule a retry with backoff, or drop the wake once it is too old."""

        key = group_key(parent_session_key, parent_task_id)
        self._in_flight.discard(key)
        row = await self._storage.get_subagent_wake(key)
        if row is None or row.get("state") != PENDING:
            return
        now = self._clock()
        attempts = int(row.get("attempts") or 0) + 1
        if now - int(row.get("created_at") or now) >= MAX_PENDING_AGE_S * 1000:
            log.warning(
                "subagent_wakes.dropped_after_retries",
                parent_session_key=parent_session_key,
                parent_task_id=parent_task_id,
                attempts=attempts,
                error=error,
            )
            await self._storage.update_subagent_wake(
                key,
                state=DROPPED,
                attempts=attempts,
                reason="delivery_timeout",
                last_error=error[:500],
                updated_at=now,
            )
            return
        delay_s = BACKOFF_S[min(attempts - 1, len(BACKOFF_S) - 1)]
        log.warning(
            "subagent_wakes.delivery_failed",
            parent_session_key=parent_session_key,
            parent_task_id=parent_task_id,
            attempts=attempts,
            retry_in_s=delay_s,
            error=error,
        )
        await self._storage.update_subagent_wake(
            key,
            attempts=attempts,
            next_attempt_at=now + int(delay_s * 1000),
            last_error=error[:500],
            updated_at=now,
        )

    async def due(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self._storage.list_due_subagent_wakes(self._clock(), limit=limit)
        return [row for row in rows if row["group_key"] not in self._in_flight]


#: Delivers one pending row; must raise when the wake was not accepted.
Deliver = Callable[[dict[str, Any]], Awaitable[None]]


class SubagentWakeWorker:
    """Retries pending wakes; drains the leftovers of a previous process first."""

    def __init__(
        self,
        outbox: SubagentWakeOutbox,
        deliver: Deliver,
        *,
        interval_s: float = 1.0,
    ) -> None:
        self._outbox = outbox
        self._deliver = deliver
        self._interval_s = interval_s
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    def start(self) -> asyncio.Task[None]:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._run(), name="subagent-wake-worker")
        return self._task

    async def stop(self) -> None:
        self._stopping.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def run_once(self) -> int:
        """Attempt every due row once; return how many were delivered."""

        delivered = 0
        for row in await self._outbox.due():
            parent = row["parent_session_key"]
            parent_task_id = row.get("parent_task_id")
            self._outbox.begin(parent, parent_task_id)
            try:
                await self._deliver(row)
            except Exception as exc:  # noqa: BLE001 - recorded, retried with backoff
                await self._outbox.failed(parent, parent_task_id, f"{type(exc).__name__}: {exc}")
                continue
            await self._outbox.delivered(parent, parent_task_id)
            delivered += 1
        return delivered

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the worker must keep running
                log.warning("subagent_wakes.worker_pass_failed", exc_info=True)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._interval_s)
            except TimeoutError:
                continue


_outbox: SubagentWakeOutbox | None = None


def set_wake_outbox(outbox: SubagentWakeOutbox | None) -> None:
    """Install (or clear, with ``None``) the gateway's outbox."""

    global _outbox
    _outbox = outbox


def get_wake_outbox() -> SubagentWakeOutbox | None:
    return _outbox
