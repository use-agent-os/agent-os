"""Let a tool give back its batch slot while it waits for an operator.

``ToolBatchExecutor`` (``agentos.engine.tool_batch``) runs each tool call
under an admission: an ``exclusive`` gate slot, or a key lock plus a
concurrency permit. A tool that waits inline for an approval would hold that
admission for as long as the operator takes, so the next call of the batch
could not even ask for its own approval. :func:`wait_outside_tool_slot` frees
the admission for the duration of such a wait and queues for it again before
returning.

Giving the slot back must not change what runs first. An ``exclusive`` call
therefore runs its body only once every earlier ``exclusive`` call of the
batch has finished, approved or not. A tool declared with
``ToolSpec.early_approval`` may be started ahead of that turn so that its
approval prompt is raised at once; such a tool calls
:func:`wait_for_batch_turn` before it acts on anything, and that wait (like
the re-admission after :func:`wait_outside_tool_slot`) only returns once the
earlier calls have finished.

This module has no engine imports so tools can use it without a cycle; the
executor installs the current call's admission in :data:`current_admission`.
"""

from __future__ import annotations

from collections.abc import Awaitable
from contextvars import ContextVar
from typing import Protocol


class BatchAdmission(Protocol):
    """What the executor exposes to a running call."""

    @property
    def yielded(self) -> bool: ...

    async def wait_outside[T](self, awaitable: Awaitable[T]) -> T: ...

    async def wait_turn(self) -> None: ...


class SkippedForSteering(Exception):  # noqa: N818 - a signal, not an error
    """Raised by :func:`wait_for_batch_turn` when the call must not run.

    A message from the user arrived while the call waited for its turn (see
    ``agentos.engine.steering``). The tool stops where it is; the executor
    replaces whatever result the tool then produces with a "skipped" one.
    """


current_admission: ContextVar[BatchAdmission | None] = ContextVar(
    "tool_batch_admission", default=None
)


async def wait_outside_tool_slot[T](awaitable: Awaitable[T]) -> T:
    """Await *awaitable* without holding this tool call's batch admission.

    Meant for a wait on an operator approval: while the operator decides, the
    call's ``exclusive`` slot (or its key lock and permit) is free for the rest
    of the batch, so the other calls can request their approvals at once. The
    call is admitted again under its declared concurrency before this returns,
    so whatever the tool runs next still respects it. Outside a batch (direct
    tool calls, tests) this is a plain ``await``.

    Do not hold any other lock across this call that a later call of the same
    batch could need.
    """

    admission = current_admission.get()
    if admission is None or admission.yielded:
        return await awaitable
    return await admission.wait_outside(awaitable)


async def wait_for_batch_turn() -> None:
    """Wait until every earlier ``exclusive`` call of this batch has finished.

    For a tool declared with ``ToolSpec.early_approval``: the executor may
    start such a call while an earlier call still waits for its approval, so
    the tool must call this after its approval check and before it does
    anything the earlier calls could depend on (run a command, write a file).
    The call holds no batch admission while it waits and is admitted again
    under its declared concurrency before this returns. Returns at once when
    it is already the call's turn, for non-exclusive calls, and outside a
    batch. Raises :class:`SkippedForSteering` when the user sent a message
    meanwhile and the call is not to run; let it propagate.
    """

    admission = current_admission.get()
    if admission is None or admission.yielded:
        return
    await admission.wait_turn()
