"""How the agent waits for a tool call's pending approval (#3819).

A tool that needs a human decision returns a ``pending`` result carrying an
``approval_id``; the tool batch then waits for that approval to resolve.
``Agent`` takes the waiting as an :class:`ApprovalWaiter` through its
constructor instead of reaching for the approval-queue singleton itself.
``TurnRunner`` passes the process's queue; an agent built without one uses
:func:`default_approval_waiter`, which resolves the same queue lazily.
"""

from __future__ import annotations

from typing import Any, Protocol


class ApprovalWaiter(Protocol):
    """Wait until an approval is resolved, or the timeout passes.

    Raises ``KeyError`` for an approval the waiter does not know.
    """

    async def wait(self, approval_id: str, timeout: float | None = None) -> bool: ...


class _QueueApprovalWaiter:
    """The process's approval queue, looked up when a wait starts.

    Looked up per wait (not at construction) so a queue reset between turns
    or in tests is honoured.
    """

    async def wait(self, approval_id: str, timeout: float | None = None) -> bool:
        # The engine already depends on the gateway package; reaching the
        # queue through it adds no new package edge.
        from agentos.gateway.approval_queue import get_approval_queue

        return await get_approval_queue().wait(approval_id, timeout=timeout)


_DEFAULT_WAITER = _QueueApprovalWaiter()


def default_approval_waiter() -> ApprovalWaiter:
    """The waiter an agent uses when none is injected."""
    return _DEFAULT_WAITER


async def wait_for_pending_approval(
    waiter: ApprovalWaiter,
    payload: dict[str, Any],
    *,
    timeout: float,
) -> None:
    """Wait for the approval a pending tool result names; unknown ones are skipped."""
    approval_id = payload.get("approval_id")
    if not isinstance(approval_id, str) or not approval_id:
        return
    try:
        await waiter.wait(approval_id, timeout=timeout)
    except KeyError:
        return


__all__ = ["ApprovalWaiter", "default_approval_waiter", "wait_for_pending_approval"]
