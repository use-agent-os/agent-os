"""Durable delivery of parent wakes for subagent spawn groups.

When every child of a spawn group has finished, ``subagent_announce`` wakes
the parent session with one message. That wake used to be best-effort: the
"already woken" marker lived in memory, so a restart could repeat or lose it,
and a failed ``TaskRuntime.send`` (for example while the runtime drains)
dropped it.

The outbox makes it durable:

* a parent that closes its spawn group while children still run records it
  (``awaiting``), so a crash before the last child's announce cannot lose the
  wake: the next boot finds the group complete and wakes the parent
  (``subagent_announce.reconcile_awaiting_wakes``);
* every wake is written to ``subagent_wakes`` (``pending``) *before* it is
  sent and before the announce's other side effects, keyed by the spawn group;
* a group whose row is ``delivered`` or ``dropped`` is never woken again, across
  restarts too, which makes delivery idempotent;
* the row turns ``delivered`` only once the task runtime accepted the wake; a
  failure keeps it ``pending`` with a backoff (1 s, 5 s, 30 s, then every 60 s)
  and the :class:`SubagentWakeWorker` retries it, giving up (``dropped``) after
  :data:`MAX_PENDING_AGE_S` (checked on every pass, not only after a failure);
* the wake is enqueued with a key derived from the group
  (:func:`wake_idempotency_key`), so a row whose ``delivered`` write was lost
  does not wake the parent a second time;
* at startup the worker drains whatever a previous process left pending, and
  it prunes finished rows older than :data:`RETENTION_S`.

Sending never waits for the outcome: with the completion manager a wake is
enqueued only once the parent's own task has released, and the sender can be
that very task (a parent closing its spawn group from ``sessions_yield``) or a
child still holding its session's execution lock. The outcome is recorded
whenever it arrives, through the once-only callback from
:meth:`SubagentWakeOutbox.reporter`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

log = structlog.get_logger(__name__)

#: Records one delivery's outcome: ``None`` once the task runtime accepted the
#: wake, the error otherwise.
Report = Callable[[BaseException | None], Awaitable[None]]

#: Seconds between attempts: 1st retry, 2nd, 3rd, then every last value.
BACKOFF_S: tuple[float, ...] = (1.0, 5.0, 30.0, 60.0)
#: A wake still not delivered this long after it was first written is dropped.
MAX_PENDING_AGE_S = 30 * 60
#: How long a freshly claimed wake is left to its first sender before the
#: worker may take it (only matters when the sending process died meanwhile:
#: in-process the in-flight set already keeps the worker off it).
CLAIM_LEASE_S = 5.0
#: A hand-off still without an outcome after this long is taken over by the
#: worker (the sender waits on the parent's task, which may never end).
IN_FLIGHT_STALE_S = 10 * 60
#: Delivered, dropped and awaiting rows older than this are deleted.
RETENTION_S = 7 * 24 * 60 * 60
#: How often the worker prunes.
PRUNE_INTERVAL_S = 60 * 60

AWAITING = "awaiting"
PENDING = "pending"
DELIVERED = "delivered"
DROPPED = "dropped"

#: Told when a pending wake is given up on: ``(parent_session_key,
#: parent_task_id, reason)``.
DropListener = Callable[[str, str | None, str], Awaitable[None]]


def group_key(parent_session_key: str, parent_task_id: str | None) -> str:
    return f"{parent_session_key}::{parent_task_id or ''}"


def wake_idempotency_key(parent_session_key: str, parent_task_id: str | None) -> str:
    """The task-ledger idempotency key of a group's wake turn."""

    return f"subagent_wake:{group_key(parent_session_key, parent_task_id)}"


def _now_ms() -> int:
    return int(time.time() * 1000)


class SubagentWakeOutbox:
    """State transitions of the ``subagent_wakes`` rows."""

    def __init__(
        self,
        storage: Any,
        *,
        clock: Callable[[], int] = _now_ms,
        on_dropped: DropListener | None = None,
    ) -> None:
        self._storage = storage
        self._clock = clock
        self._on_dropped = on_dropped
        #: Groups a sender in this process is delivering right now: the
        #: attempt's token and when it began (ms).
        self._in_flight: dict[str, tuple[int, int]] = {}
        self._tokens = 0

    def set_drop_listener(self, listener: DropListener | None) -> None:
        self._on_dropped = listener

    def _enter(self, key: str) -> None:
        self._tokens += 1
        self._in_flight[key] = (self._tokens, self._clock())

    def _token(self, key: str) -> int | None:
        entry = self._in_flight.get(key)
        return entry[0] if entry is not None else None

    def _leave(self, key: str, token: int | None) -> None:
        """End an attempt; a stale one taken over meanwhile leaves the new one be."""

        entry = self._in_flight.get(key)
        if entry is not None and (token is None or entry[0] == token):
            del self._in_flight[key]

    async def await_group(self, *, parent_session_key: str, parent_task_id: str) -> None:
        """Record that the parent closed this group and waits for its wake.

        A no-op when the group already has a row.
        """

        await self._storage.record_subagent_wake(
            group_key=group_key(parent_session_key, parent_task_id),
            parent_session_key=parent_session_key,
            parent_task_id=parent_task_id,
            message="",
            provenance={},
            payloads=[],
            now_ms=self._clock(),
            state=AWAITING,
        )

    async def awaiting(self, limit: int = 200) -> list[dict[str, Any]]:
        """Groups closed by their parent whose wake has not been written yet."""

        return list(await self._storage.list_subagent_wakes(AWAITING, limit=limit))

    async def prune(self, *, retention_s: float = RETENTION_S) -> int:
        """Delete finished rows untouched for *retention_s*; return how many."""

        prune = getattr(self._storage, "prune_subagent_wakes", None)
        if not callable(prune):
            return 0
        removed = int(await prune(self._clock() - int(retention_s * 1000)))
        if removed:
            log.info("subagent_wakes.pruned", count=removed)
        return removed

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
            replace_state=AWAITING,
        )
        if created:
            self._enter(key)
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
        if not created and row.get("state") in (PENDING, AWAITING):
            await self._storage.update_subagent_wake(
                key,
                expected_state=row.get("state"),
                state=DROPPED,
                reason=reason,
                updated_at=self._clock(),
            )

    def begin(self, parent_session_key: str, parent_task_id: str | None) -> None:
        """Mark a group as being delivered by this process (the worker skips it)."""

        self._enter(group_key(parent_session_key, parent_task_id))

    def release(self, parent_session_key: str, parent_task_id: str | None) -> None:
        """The sender gave up without an outcome (it was cancelled).

        The row stays ``pending``; the worker takes it once its lease ran out.
        """

        self._in_flight.pop(group_key(parent_session_key, parent_task_id), None)

    def reporter(self, parent_session_key: str, parent_task_id: str | None) -> Report:
        """A once-only callback that records a delivery's outcome.

        ``None`` marks the wake delivered, an error schedules a retry. It may
        be called long after the sender returned (see the module docstring).
        Create it right after :meth:`claim` / :meth:`begin`: it belongs to
        that attempt.
        """

        reported = False
        token = self._token(group_key(parent_session_key, parent_task_id))

        async def report(error: BaseException | None) -> None:
            nonlocal reported
            if reported:
                return
            reported = True
            if error is None:
                await self.delivered(parent_session_key, parent_task_id, token=token)
            else:
                await self.failed(
                    parent_session_key,
                    parent_task_id,
                    f"{type(error).__name__}: {error}",
                    token=token,
                )

        return report

    async def still_pending(self, row: dict[str, Any]) -> bool:
        """Whether *row* (from :meth:`due`) is still waiting to be delivered."""

        current = await self._storage.get_subagent_wake(row["group_key"])
        return current is not None and current.get("state") == PENDING

    async def delivered(
        self, parent_session_key: str, parent_task_id: str | None, *, token: int | None = None
    ) -> None:
        key = group_key(parent_session_key, parent_task_id)
        try:
            # A row dropped meanwhile (its parent was aborted) stays dropped.
            await self._storage.update_subagent_wake(
                key,
                expected_state=PENDING,
                state=DELIVERED,
                updated_at=self._clock(),
            )
        finally:
            # Only once the row is written: until then the worker must not
            # take it for a retry. Should the write fail, the retry finds the
            # wake turn by its idempotency key and does not enqueue another.
            self._leave(key, token)

    async def failed(
        self,
        parent_session_key: str,
        parent_task_id: str | None,
        error: str,
        *,
        token: int | None = None,
    ) -> None:
        """Schedule a retry with backoff, or drop the wake once it is too old."""

        key = group_key(parent_session_key, parent_task_id)
        try:
            await self._record_failure(key, parent_session_key, parent_task_id, error)
        finally:
            self._leave(key, token)

    async def _record_failure(
        self, key: str, parent_session_key: str, parent_task_id: str | None, error: str
    ) -> None:
        row = await self._storage.get_subagent_wake(key)
        if row is None or row.get("state") != PENDING:
            return
        now = self._clock()
        attempts = int(row.get("attempts") or 0) + 1
        if self._too_old(row, now):
            await self._give_up(row, attempts=attempts, error=error)
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
            expected_state=PENDING,
            attempts=attempts,
            next_attempt_at=now + int(delay_s * 1000),
            last_error=error[:500],
            updated_at=now,
        )

    def _too_old(self, row: dict[str, Any], now: int) -> bool:
        return now - int(row.get("created_at") or now) >= MAX_PENDING_AGE_S * 1000

    async def _give_up(self, row: dict[str, Any], *, attempts: int, error: str | None) -> None:
        parent_session_key = row["parent_session_key"]
        parent_task_id = row.get("parent_task_id") or None
        log.warning(
            "subagent_wakes.dropped_after_retries",
            parent_session_key=parent_session_key,
            parent_task_id=parent_task_id,
            attempts=attempts,
            error=error,
        )
        fields: dict[str, Any] = {
            "state": DROPPED,
            "attempts": attempts,
            "reason": "delivery_timeout",
            "updated_at": self._clock(),
        }
        if error:
            fields["last_error"] = error[:500]
        await self._storage.update_subagent_wake(row["group_key"], expected_state=PENDING, **fields)
        if self._on_dropped is not None:
            try:
                await self._on_dropped(parent_session_key, parent_task_id, "delivery_timeout")
            except Exception:  # noqa: BLE001 - a listener never blocks the outbox
                log.warning("subagent_wakes.drop_listener_failed", exc_info=True)

    async def expire(self, row: dict[str, Any]) -> bool:
        """Drop *row* when it is past :data:`MAX_PENDING_AGE_S`; ``True`` if it was."""

        if not self._too_old(row, self._clock()):
            return False
        self._in_flight.pop(row["group_key"], None)
        await self._give_up(
            row,
            attempts=int(row.get("attempts") or 0),
            error=row.get("last_error") or "not delivered in time",
        )
        return True

    async def due(self, limit: int = 50) -> list[dict[str, Any]]:
        """Pending rows to hand off now: due, and not being delivered right now.

        A hand-off in flight for longer than :data:`IN_FLIGHT_STALE_S` no
        longer keeps the worker off its row.
        """

        now = self._clock()
        stale_ms = int(IN_FLIGHT_STALE_S * 1000)
        rows = await self._storage.list_due_subagent_wakes(now, limit=limit)
        due: list[dict[str, Any]] = []
        for row in rows:
            entry = self._in_flight.get(row["group_key"])
            if entry is not None and now - entry[1] < stale_ms:
                continue
            if entry is not None:
                log.warning(
                    "subagent_wakes.in_flight_stale",
                    parent_session_key=row["parent_session_key"],
                    parent_task_id=row.get("parent_task_id"),
                )
            due.append(row)
        return due


#: Hands one pending row off for delivery and reports the outcome through the
#: callback (maybe after it returned); raises when the hand-off itself failed.
Deliver = Callable[[dict[str, Any], Report], Awaitable[None]]


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
        """Hand every due row off once; return how many were handed off."""

        handed_off = 0
        for row in await self._outbox.due():
            if self._stopping.is_set():
                break
            if not await self._outbox.still_pending(row):
                continue  # dropped or delivered since the pass began
            if await self._outbox.expire(row):
                continue
            parent = row["parent_session_key"]
            parent_task_id = row.get("parent_task_id")
            self._outbox.begin(parent, parent_task_id)
            report = self._outbox.reporter(parent, parent_task_id)
            try:
                await self._deliver(row, report)
            except Exception as exc:  # noqa: BLE001 - recorded, retried with backoff
                await report(exc)
                continue
            except BaseException:
                self._outbox.release(parent, parent_task_id)
                raise
            handed_off += 1
        return handed_off

    async def _prune(self) -> None:
        try:
            await self._outbox.prune()
        except Exception:  # noqa: BLE001 - pruning is housekeeping
            log.warning("subagent_wakes.prune_failed", exc_info=True)

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        next_prune = loop.time()
        while not self._stopping.is_set():
            if loop.time() >= next_prune:
                next_prune = loop.time() + PRUNE_INTERVAL_S
                await self._prune()
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
