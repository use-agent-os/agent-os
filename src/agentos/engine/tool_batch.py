"""Run one model iteration's tool calls and stream each result as it finishes.

``ToolBatchExecutor`` takes the tool_use blocks of a single assistant message
and runs them under the concurrency each tool declares on its ``ToolSpec``:

* ``parallel`` calls run together, at most ``max_concurrency`` at a time;
* ``keyed`` calls run like ``parallel`` ones, except that calls sharing a
  concurrency key run one after another;
* ``exclusive`` calls (the default for a tool that declares nothing) run
  alone: every earlier call finishes first and every later call waits.

Admission is strictly in the order the model emitted the calls, so an
``exclusive`` call still acts as a barrier exactly like the old mutex tools.

Each result is delivered (artifact events, ``ToolResultEvent``, router-control
replay) as soon as its call finishes rather than after the whole batch. A call
that comes back asking for operator approval is delivered at once too, and the
wait for that approval runs in the background alongside every other call;
when the approval is resolved the call is retried with its ``approval_id``.
Approvals are therefore awaited concurrently instead of one by one, while what
gets approved is still decided by the tool itself.

A tool that waits for its approval inline instead (``exec`` on the Web UI and
desktop, which poll the approval queue themselves) wraps that wait in
``agentos.tools.batch_slot.wait_outside_tool_slot``: the call gives back its
admission (gate slot, key lock, concurrency permit) for the duration of the
wait and queues again for it before running. A pending approval therefore
never holds an ``exclusive`` slot, so every approval of a batch is requested
at once instead of each one after the previous answer.

Stepping out does not reorder the batch: an ``exclusive`` call runs its body
only once every earlier ``exclusive`` call has finished. A later call may get
ahead only as far as its own approval prompt, and only when its tool declares
``ToolSpec.early_approval`` (it then calls
``agentos.tools.batch_slot.wait_for_batch_turn`` before acting); any other
``exclusive`` call is not admitted before its turn. An ``exclusive`` call
queueing again after its approval goes back to its place in emission order,
not to the back of the queue. ``keyed`` and ``parallel`` calls are not held
to that turn and re-queue at the back, as before. A call is not timed while
it queues to be admitted again, and one admitted again after the batch
deadline times out without running.

A message the user sends while the batch runs (``steering_pending``) cuts the
ordered part of the batch short: an ``exclusive`` call, or a shared call that
would open a new group behind one, that has not started once another call has
gets a synthetic "skipped" result instead of running
(``agentos.engine.steering``). A call let in early for its approval passes
that checkpoint in ``wait_for_batch_turn``. Shared calls already running
together finish.

The pairing guarantee holds whatever order calls finish in, and on timeout,
cancellation or exception: ``outcomes`` has exactly one final result per
tool_use, in the original call order, ready to be appended to the transcript.
"""

from __future__ import annotations

import asyncio
import bisect
import functools
import itertools
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

import structlog

from agentos.engine.steering import BatchSteeringCheckpoint, skipped_for_steering
from agentos.engine.types import (
    AgentEvent,
    ArtifactEvent,
    RunHeartbeatEvent,
    ToolCall,
    ToolResult,
    ToolResultEvent,
    WarningEvent,
)
from agentos.execution_status import ExecutionStatusValue, runtime_execution_status
from agentos.provider import ContentBlockToolResult
from agentos.router_control import router_control_replay_event_from_payload
from agentos.tools.batch_slot import SkippedForSteering, current_admission
from agentos.tools.types import ToolConcurrency, ToolSpec

_log = structlog.get_logger(__name__)

DEFAULT_MAX_TOOL_CONCURRENCY = 8


# ---------------------------------------------------------------------------
# Concurrency policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolConcurrencyPolicy:
    """How one concrete call may overlap with the other calls of its batch."""

    mode: ToolConcurrency
    key: Hashable | None = None


EXCLUSIVE_POLICY = ToolConcurrencyPolicy(mode="exclusive")
PARALLEL_POLICY = ToolConcurrencyPolicy(mode="parallel")


def resolve_tool_concurrency(
    spec: ToolSpec | None,
    arguments: Mapping[str, Any] | None,
    *,
    session_key: str | None = None,
) -> ToolConcurrencyPolicy:
    """Return the policy for one call of the tool described by *spec*.

    An unknown tool, an unknown mode, or a ``keyed`` tool whose key cannot be
    derived runs ``exclusive``: the safe choice, and what such a call did
    before tools declared their concurrency.
    """

    if spec is None:
        return EXCLUSIVE_POLICY
    mode = getattr(spec, "concurrency", "exclusive")
    if mode == "parallel":
        return PARALLEL_POLICY
    if mode != "keyed":
        return EXCLUSIVE_POLICY
    key_fn = getattr(spec, "concurrency_key", None)
    if key_fn is None:
        return ToolConcurrencyPolicy(mode="keyed", key=("tool", spec.name))
    try:
        key = key_fn(arguments or {}, session_key)
    except Exception:  # noqa: BLE001 - a broken key function must not lose the call
        _log.warning("tool_batch.concurrency_key_failed", tool=spec.name, exc_info=True)
        return EXCLUSIVE_POLICY
    if key is None:
        return EXCLUSIVE_POLICY
    return ToolConcurrencyPolicy(mode="keyed", key=key)


def registry_spec_lookup(registry: Any | None) -> Callable[[str], ToolSpec | None]:
    """Find a tool's spec in *registry*, then in the default registry.

    The fallback matters for agents built without a registry (embedded use and
    tests): builtins register on the default registry, so their declared
    concurrency still applies.
    """

    def _lookup(name: str) -> ToolSpec | None:
        from agentos.tools.registry import get_default_registry  # noqa: PLC0415

        for candidate in (registry, get_default_registry()):
            getter = getattr(candidate, "get", None)
            if getter is None:
                continue
            registered = getter(name)
            spec = getattr(registered, "spec", None)
            if isinstance(spec, ToolSpec):
                return spec
        return None

    return _lookup


# ---------------------------------------------------------------------------
# Ordered admission gate
# ---------------------------------------------------------------------------


class _ExclusiveTurns:
    """Which ``exclusive`` calls of a batch are still unfinished, by call index.

    It is an exclusive call's turn once every exclusive call emitted before it
    has finished; ``keyed`` and ``parallel`` calls are not counted.
    """

    def __init__(self, indices: Sequence[int] = ()) -> None:
        self._unfinished = set(indices)

    def ready(self, index: int) -> bool:
        return all(other >= index for other in self._unfinished)

    def finish(self, index: int) -> None:
        self._unfinished.discard(index)


@dataclass(eq=False)
class _Slot:
    exclusive: bool
    future: asyncio.Future[None]
    released: bool = False
    # Queue position: the call's index, or None to queue at the back.
    order: int | None = None
    # An exclusive slot that may be admitted only in its call's turn.
    needs_turn: bool = False
    rank: tuple[float, int] = (0.0, 0)


class _OrderedGate:
    """Ordered reader/writer gate.

    Shared holders run together; an exclusive holder runs alone. Requests are
    admitted strictly in queue order, so a shared request behind a waiting
    exclusive one does not overtake it. The queue is ordered by the slot's
    ``order`` (the call index; ``None`` sorts last) and then by reservation,
    so a call that queues again can go back to its place. A ``needs_turn``
    exclusive slot at the head waits until :class:`_ExclusiveTurns` says it
    is the call's turn.
    """

    def __init__(self, turns: _ExclusiveTurns | None = None) -> None:
        self._queue: list[_Slot] = []
        self._shared_active = 0
        self._exclusive_active = False
        self._turns = turns
        self._counter = itertools.count()

    def reserve(
        self, *, exclusive: bool, order: int | None = None, needs_turn: bool = False
    ) -> _Slot:
        slot = _Slot(
            exclusive=exclusive,
            future=asyncio.get_running_loop().create_future(),
            order=order,
            needs_turn=needs_turn and exclusive and order is not None,
            rank=(float("inf") if order is None else float(order), next(self._counter)),
        )
        bisect.insort(self._queue, slot, key=_slot_rank)
        self._admit()
        return slot

    def wake(self) -> None:
        """Re-check the head after a call finished (its turn may have come)."""

        self._admit()

    def release(self, slot: _Slot) -> None:
        """Give *slot* back. Idempotent; also abandons a slot never admitted."""

        if slot.released:
            return
        slot.released = True
        future = slot.future
        if future.done() and not future.cancelled():
            if slot.exclusive:
                self._exclusive_active = False
            else:
                self._shared_active -= 1
        elif not future.done():
            future.cancel()
        self._admit()

    def _admit(self) -> None:
        while self._queue:
            slot = self._queue[0]
            if slot.future.done():
                # Abandoned before admission.
                self._queue.pop(0)
                continue
            if self._exclusive_active:
                return
            if slot.exclusive:
                if self._shared_active:
                    return
                if (
                    slot.needs_turn
                    and slot.order is not None
                    and self._turns is not None
                    and not self._turns.ready(slot.order)
                ):
                    return
                self._exclusive_active = True
            else:
                self._shared_active += 1
            self._queue.pop(0)
            slot.future.set_result(None)


def _slot_rank(slot: _Slot) -> tuple[float, int]:
    return slot.rank


@dataclass(eq=False)
class _Admission:
    """Everything one running call holds: its gate slot and, for a
    non-exclusive call, its key lock and concurrency permit.

    Acquired in the fixed order gate -> key lock -> permit, released in
    reverse; tracking what is held makes release idempotent and safe after a
    cancellation that landed half-way through acquiring.
    """

    gate: _OrderedGate
    slot: _Slot
    exclusive: bool
    key_lock: asyncio.Lock | None = None
    semaphore: asyncio.Semaphore | None = None
    holds_key: bool = False
    holds_permit: bool = False
    yielded: bool = False
    # The call's index for an exclusive first run (its place in the turn
    # order); None for a call that is not held to a turn.
    order: int | None = None
    turns: _ExclusiveTurns | None = None
    # The running call's own timeout. It is paused while the call queues to
    # be admitted again (a queued call is not timed, as before its first
    # admission); the time spent waiting for the approval itself does count.
    timeout_scope: asyncio.Timeout | None = None
    # Seconds left in the batch; a call admitted again with none left times
    # out without running, like a call still queued at the deadline.
    batch_remaining: Callable[[], float] | None = None
    deadline_passed: bool = False
    # Wraps the awaitable a call waits on outside its slot (an operator
    # approval), so the turn's timing can count that wait.
    outside_wait: Callable[[Awaitable[Any]], Awaitable[Any]] | None = None
    # The steering checkpoint of a call started ahead of its turn
    # (``early_approval``), passed once its turn comes (``wait_turn``); False
    # means a user message arrived and the call must not run.
    turn_checkpoint: Callable[[], bool] | None = None
    skipped_for_steering: bool = False

    async def acquire(self) -> None:
        await self.slot.future
        if self.key_lock is not None:
            await self.key_lock.acquire()
            self.holds_key = True
        if self.semaphore is not None:
            await self.semaphore.acquire()
            self.holds_permit = True

    def release(self) -> None:
        if self.holds_permit and self.semaphore is not None:
            self.holds_permit = False
            self.semaphore.release()
        if self.holds_key and self.key_lock is not None:
            self.holds_key = False
            self.key_lock.release()
        self.gate.release(self.slot)

    def finish(self) -> None:
        """The call is done for good: release it and let later calls take their turn."""

        if self.turns is not None and self.order is not None:
            self.turns.finish(self.order)
        self.release()
        self.gate.wake()

    def in_turn(self) -> bool:
        return (
            not self.exclusive
            or self.order is None
            or self.turns is None
            or self.turns.ready(self.order)
        )

    async def wait_turn(self) -> None:
        if self.yielded:
            return
        if not self.in_turn():
            self.yielded = True
            self.release()
            await self._readmit()
        self._pass_turn_checkpoint()

    def _pass_turn_checkpoint(self) -> None:
        """Run the deferred steering checkpoint once the call is in its turn."""
        if self.turn_checkpoint is None or not self.in_turn():
            return
        checkpoint, self.turn_checkpoint = self.turn_checkpoint, None
        if not checkpoint():
            self.skipped_for_steering = True
            raise SkippedForSteering

    async def wait_outside[T](self, awaitable: Awaitable[T]) -> T:
        self.yielded = True
        self.release()
        try:
            if self.outside_wait is not None:
                result = cast(T, await self.outside_wait(awaitable))
            else:
                result = await awaitable
        except asyncio.CancelledError:
            # Nothing is held; the call's own cleanup releases nothing more.
            raise
        except BaseException:
            await self._readmit()
            raise
        await self._readmit()
        self._pass_turn_checkpoint()
        return result

    async def _readmit(self) -> None:
        # Queue again; the call runs its tool only once it is admitted under
        # its own policy again. An exclusive first run goes back to its place
        # in emission order and is admitted only in its turn; anything else
        # queues at the back. The call's timeout is paused meanwhile and
        # resumes with the time it had left, capped by the batch.
        loop = asyncio.get_running_loop()
        scope = self.timeout_scope
        when = scope.when() if scope is not None else None
        left = None if when is None else max(0.0, when - loop.time())
        if scope is not None and when is not None:
            scope.reschedule(None)
        self.slot = self.gate.reserve(
            exclusive=self.exclusive,
            order=self.order if self.exclusive else None,
            needs_turn=self.exclusive,
        )
        await self.acquire()
        self.yielded = False
        batch_left = self.batch_remaining() if self.batch_remaining is not None else None
        if batch_left is not None and batch_left <= 0:
            # Admitted again only after the batch gave up: time out unstarted.
            self.deadline_passed = True
            if scope is None:
                raise TimeoutError
            scope.reschedule(loop.time())
            await loop.create_future()  # the scope expires and cancels this
        if scope is not None and left is not None:
            if batch_left is not None:
                left = min(left, batch_left)
            scope.reschedule(loop.time() + left)


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolBatchOutcome:
    """Final result of one tool_use: the call that produced it and its projection."""

    call: ToolCall
    result: ToolResult
    projected: ToolResult


@dataclass(eq=False)
class _Job:
    index: int
    call: ToolCall
    is_approval_retry: bool = False


@dataclass
class _BatchClock:
    deadline: float
    total_deadline: float | None
    now: Callable[[], float]

    def remaining(self) -> float:
        current = self.now()
        remaining = max(0.0, self.deadline - current)
        if self.total_deadline is not None:
            remaining = min(remaining, max(0.0, self.total_deadline - current))
        return remaining

    def cap(self, timeout: float) -> float:
        return max(0.001, min(timeout, self.remaining()))


def _error_result(
    call: ToolCall, content: str, status: ExecutionStatusValue, reason: str
) -> ToolResult:
    return ToolResult(
        tool_use_id=call.tool_use_id,
        tool_name=call.tool_name,
        content=content,
        is_error=True,
        execution_status=runtime_execution_status(status, reason=reason),
    )


def _timeout_result(call: ToolCall, seconds: float) -> ToolResult:
    return ToolResult(
        tool_use_id=call.tool_use_id,
        tool_name=call.tool_name,
        content=f"Tool '{call.tool_name}' timed out after {seconds}s",
        is_error=True,
        execution_status=runtime_execution_status(
            "timeout",
            reason="runtime_timeout",
            timed_out=True,
        ),
    )


@dataclass
class ToolBatchExecutor:
    """Execute one iteration's tool calls; see the module docstring.

    The agent supplies the pieces that stay agent-specific (dispatch, result
    projection, approval waiting, logging) so the scheduling, timeout and
    pairing rules live here and can be tested without a provider.
    """

    execute: Callable[[ToolCall], Awaitable[ToolResult]]
    project: Callable[..., Awaitable[ToolResult]]
    timeout_for: Callable[[ToolCall], float]
    spec_for: Callable[[str], ToolSpec | None]
    deadline: float
    iteration_timeout: float
    total_deadline: float | None = None
    session_key: str | None = None
    max_concurrency: int = DEFAULT_MAX_TOOL_CONCURRENCY
    heartbeat_interval: float = 15.0
    approval_wait_timeout: float = 180.0
    pending_approval: Callable[[str], dict[str, Any] | None] = lambda _content: None
    wait_for_approval: Callable[..., Awaitable[Any]] | None = None
    # Wraps an inline approval wait a tool does outside its slot (see
    # ``agentos.tools.batch_slot``); used to time it like ``wait_for_approval``.
    outside_wait: Callable[[Awaitable[Any]], Awaitable[Any]] | None = None
    artifact_event: Callable[[dict[str, Any]], ArtifactEvent] = lambda a: ArtifactEvent(**a)
    pending_warnings: list[WarningEvent] = field(default_factory=list)
    preflight_results: Mapping[str, ToolResult] = field(default_factory=dict)
    call_log: Callable[..., None] = lambda _kind, **_payload: None
    iteration: int = 0
    clock: Callable[[], float] | None = None
    # True while a user message waits to be steered into the turn: calls of
    # an ordered sequence not started yet are then skipped
    # (``agentos.engine.steering``).
    steering_pending: Callable[[], bool] | None = None

    _outcomes: dict[int, ToolBatchOutcome] = field(default_factory=dict, init=False)
    _calls: list[ToolCall] = field(default_factory=list, init=False)

    # -- public results -------------------------------------------------

    @property
    def outcomes(self) -> list[ToolBatchOutcome]:
        """One final outcome per tool_use, in the original call order."""

        return [self._outcomes[index] for index in range(len(self._calls))]

    @property
    def executed_results(self) -> list[ToolResult]:
        return [outcome.result for outcome in self.outcomes]

    def result_blocks(self) -> list[ContentBlockToolResult]:
        return [
            ContentBlockToolResult(
                tool_use_id=outcome.projected.tool_use_id,
                content=outcome.projected.content,
                is_error=outcome.projected.is_error,
                execution_status=outcome.projected.execution_status,
            )
            for outcome in self.outcomes
        ]

    def policy_for(self, call: ToolCall) -> ToolConcurrencyPolicy:
        return resolve_tool_concurrency(
            self.spec_for(call.tool_name),
            call.arguments,
            session_key=self.session_key,
        )

    # -- execution ------------------------------------------------------

    async def run(self, tool_calls: Sequence[ToolCall]) -> AsyncIterator[AgentEvent]:
        """Run every call; yield events as results arrive. Fills ``outcomes``."""

        self._calls = list(tool_calls)
        self._outcomes = {}
        if not self._calls:
            return
        clock = _BatchClock(
            deadline=self.deadline,
            total_deadline=self.total_deadline,
            now=self.clock or asyncio.get_running_loop().time,
        )
        policies = [self.policy_for(call) for call in self._calls]
        turns = _ExclusiveTurns(
            [index for index, policy in enumerate(policies) if policy.mode == "exclusive"]
        )
        gate = _OrderedGate(turns)
        steering = BatchSteeringCheckpoint(
            self.steering_pending,
            [policy.mode == "exclusive" for policy in policies],
        )
        semaphore = asyncio.Semaphore(max(1, int(self.max_concurrency)))
        key_locks: dict[Hashable, asyncio.Lock] = {}
        tasks: dict[asyncio.Task[ToolResult], _Job] = {}

        async def _run_one(
            call: ToolCall, admission: _Admission, index: int | None = None
        ) -> ToolResult:
            if index is not None and call.tool_use_id not in self.preflight_results:
                # First run of a call: the steering checkpoint. A call let in
                # ahead of its turn passes it when its turn comes instead.
                if not admission.in_turn():
                    admission.turn_checkpoint = functools.partial(steering.enter, index)
                elif not steering.enter(index):
                    self.call_log(
                        "tool_skipped_for_steering",
                        iteration=self.iteration,
                        tool_use_id=call.tool_use_id,
                        name=call.tool_name,
                    )
                    return skipped_for_steering(call)
            started = time.monotonic()
            self.call_log(
                "tool_request",
                iteration=self.iteration,
                tool_use_id=call.tool_use_id,
                name=call.tool_name,
                arguments=call.arguments,
            )
            tool_timeout = clock.cap(self.timeout_for(call))
            preflight = self.preflight_results.get(call.tool_use_id)
            if preflight is not None:
                result = preflight
            elif clock.remaining() <= 0:
                # Admitted only because an earlier call's own timeout fired at
                # the batch deadline: starting it now would run a tool the
                # batch has already given up on. It times out unstarted, like
                # every other call still queued at the deadline.
                result = _timeout_result(call, self.iteration_timeout)
            else:
                try:
                    async with asyncio.timeout(tool_timeout) as scope:
                        admission.timeout_scope = scope
                        result = await self.execute(call)
                except TimeoutError:
                    result = _timeout_result(
                        call,
                        self.iteration_timeout if admission.deadline_passed else tool_timeout,
                    )
                except SkippedForSteering:
                    result = skipped_for_steering(call)
                if admission.skipped_for_steering:
                    result = skipped_for_steering(call)
            self.call_log(
                "tool_response",
                iteration=self.iteration,
                tool_use_id=result.tool_use_id,
                name=result.tool_name,
                result=result.content,
                result_chars=len(result.content),
                is_error=result.is_error,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            return result

        def _admission(
            call: ToolCall, policy: ToolConcurrencyPolicy, order: int | None
        ) -> _Admission:
            exclusive = policy.mode == "exclusive"
            # A first run queues at its call index whatever its mode, so a
            # shared call is admitted before a later exclusive one (the gate
            # is ordered by this rank). Only an exclusive call is held to a
            # turn; a shared one re-queues at the back after stepping out.
            queue_order = order
            if not exclusive:
                order = None
            spec = self.spec_for(call.tool_name)
            early = bool(getattr(spec, "early_approval", False))
            return _Admission(
                gate=gate,
                # An exclusive first run is admitted in its turn, unless its
                # tool asks for approval first and waits for the turn itself.
                slot=gate.reserve(
                    exclusive=exclusive, order=queue_order, needs_turn=exclusive and not early
                ),
                exclusive=exclusive,
                order=order,
                turns=turns if order is not None else None,
                batch_remaining=clock.remaining,
                key_lock=(
                    key_locks.setdefault(policy.key, asyncio.Lock())
                    if not exclusive and policy.key is not None
                    else None
                ),
                semaphore=None if exclusive else semaphore,
                outside_wait=self.outside_wait,
            )

        async def _run_admitted(
            call: ToolCall, admission: _Admission, index: int | None = None
        ) -> ToolResult:
            token = current_admission.set(admission)
            try:
                await admission.acquire()
                return await _run_one(call, admission, index)
            finally:
                admission.finish()
                current_admission.reset(token)

        def _schedule(job: _Job) -> None:
            admission = _admission(job.call, policies[job.index], job.index)
            task = asyncio.create_task(_run_admitted(job.call, admission, job.index))
            # A task cancelled before it first runs never reaches its
            # ``finally``; give its slot back so the gate cannot stall.
            task.add_done_callback(functools.partial(_release_admission, admission))
            tasks[task] = job

        async def _approve_then_retry(job: _Job, payload: dict[str, Any]) -> ToolResult:
            if self.wait_for_approval is not None:
                await self.wait_for_approval(
                    payload,
                    timeout=clock.cap(self.approval_wait_timeout),
                )
            # A retry is not held to the turn order and queues at the back:
            # each call runs the moment its own approval is resolved.
            admission = _admission(job.call, self.policy_for(job.call), None)
            return await _run_admitted(job.call, admission)

        def _schedule_retry(job: _Job, payload: dict[str, Any]) -> None:
            retry_arguments = dict(job.call.arguments)
            retry_arguments["approval_id"] = payload["approval_id"]
            retry_call = ToolCall(
                tool_use_id=job.call.tool_use_id,
                tool_name=job.call.tool_name,
                arguments=retry_arguments,
                synthetic_from_text=job.call.synthetic_from_text,
                origin_trace=job.call.origin_trace,
                thought_signature=job.call.thought_signature,
            )
            retry_job = _Job(index=job.index, call=retry_call, is_approval_retry=True)
            tasks[asyncio.create_task(_approve_then_retry(retry_job, payload))] = retry_job

        for index, call in enumerate(self._calls):
            _schedule(_Job(index=index, call=call))

        interval = float(self.heartbeat_interval)
        started = time.monotonic()
        last_event_at = started
        try:
            while tasks:
                remaining = clock.remaining()
                if remaining <= 0:
                    timed_out = sorted(tasks.items(), key=lambda item: item[1].index)
                    tasks.clear()
                    await _cancel_and_wait([task for task, _job in timed_out])
                    for _task, job in timed_out:
                        result = _timeout_result(job.call, self.iteration_timeout)
                        for event in await self._finish(job, result):
                            yield event
                    break
                wait_timeout = remaining if interval <= 0 else min(interval, remaining)
                done, _pending = await asyncio.wait(
                    tasks,
                    timeout=max(0.001, wait_timeout),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    if clock.remaining() <= 0:
                        continue
                    now = time.monotonic()
                    yield RunHeartbeatEvent(
                        phase="tool",
                        elapsed_ms=int((now - started) * 1000),
                        idle_ms=int((now - last_event_at) * 1000),
                        message="Tool still running",
                    )
                    continue

                last_event_at = time.monotonic()
                finished = sorted(((task, tasks.pop(task)) for task in done), key=_job_index)
                for task, job in finished:
                    result = _task_result(task, job.call)
                    payload = (
                        None
                        if job.is_approval_retry or job.call.arguments.get("approval_id")
                        else self.pending_approval(result.content)
                    )
                    if payload is None:
                        for event in await self._finish(job, result):
                            yield event
                        continue
                    events, _projected = await self._render(job.call, result)
                    for event in events:
                        yield event
                    # Start waiting only once the approval prompt has been
                    # delivered; a consumer that answers it inline (CLI) has
                    # resolved it by the time the generator resumes.
                    _schedule_retry(job, payload)
        finally:
            leftover = list(tasks)
            tasks.clear()
            await _cancel_and_wait(leftover)

        for index, call in enumerate(self._calls):
            if index not in self._outcomes:  # pragma: no cover - defensive pairing guard
                result = _error_result(
                    call, f"Tool '{call.tool_name}' was cancelled", "cancelled", "cancelled"
                )
                self._outcomes[index] = ToolBatchOutcome(call=call, result=result, projected=result)

    async def _render(
        self, call: ToolCall, result: ToolResult
    ) -> tuple[list[AgentEvent], ToolResult]:
        """Build the events delivering *result*, in the order the UI expects."""

        events: list[AgentEvent] = [self.artifact_event(a) for a in result.artifacts]
        projected = await self.project(result, tool_call=call)
        events.append(
            ToolResultEvent(
                tool_use_id=projected.tool_use_id,
                tool_name=projected.tool_name,
                result=projected.content,
                is_error=projected.is_error,
                arguments=call.arguments,
                execution_status=projected.execution_status,
                thought_signature=call.thought_signature,
            )
        )
        replay_event = router_control_replay_event_from_payload(result.content)
        if replay_event is not None:
            events.append(replay_event)
        return events, projected

    async def _finish(self, job: _Job, result: ToolResult) -> list[AgentEvent]:
        """Deliver a final result and record it as the call's outcome."""

        events, projected = await self._render(job.call, result)
        self._outcomes[job.index] = ToolBatchOutcome(
            call=job.call,
            result=result,
            projected=projected,
        )
        while self.pending_warnings:
            events.append(self.pending_warnings.pop(0))
        return events


def _release_admission(admission: _Admission, _task: asyncio.Task[ToolResult]) -> None:
    admission.finish()


def _job_index(item: tuple[asyncio.Task[ToolResult], _Job]) -> int:
    return item[1].index


def _task_result(task: asyncio.Task[ToolResult], call: ToolCall) -> ToolResult:
    try:
        return task.result()
    except asyncio.CancelledError:
        return _error_result(
            call, f"Tool '{call.tool_name}' was cancelled", "cancelled", "cancelled"
        )
    except Exception as exc:  # noqa: BLE001
        return _error_result(
            call, f"Tool '{call.tool_name}' raised: {exc}", "error", "runtime_error"
        )


async def _cancel_and_wait(tasks: list[asyncio.Task[ToolResult]]) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.wait(tasks)
