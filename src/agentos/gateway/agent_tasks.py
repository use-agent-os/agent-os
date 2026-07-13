"""Registry for tracking running agent tasks per session."""

from __future__ import annotations

import asyncio

import structlog

log = structlog.get_logger(__name__)


class AgentTaskRegistry:
    """Track running agent tasks per session for abort/status queries."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}

    def register(
        self,
        session_key: str,
        task: asyncio.Task,
        *,
        cancel_existing: bool = True,
    ) -> None:
        """Register a running agent task for a session.

        When ``cancel_existing`` is ``True`` (the default — ``steer`` queue mode),
        any in-flight task is cancelled before the new one is stored. Callers
        using ``queue``/``followup`` modes must pass ``cancel_existing=False``
        AND must only register when no task is currently running — otherwise
        the in-flight task is silently orphaned. Automatically removes the task
        when it completes.
        """
        existing = self._tasks.get(session_key)
        if existing is not None and not existing.done():
            if cancel_existing:
                existing.cancel()
                log.warning("agent_task.replaced", session_key=session_key)
            else:
                # Caller violated the contract. Refuse to orphan the live task.
                raise RuntimeError(
                    f"agent_task.register(cancel_existing=False) called while a "
                    f"task is still running for session={session_key!r}. "
                    f"Queue mode must wait for the current task's completion."
                )
        self._tasks[session_key] = task

        def _on_done(t: asyncio.Task) -> None:
            self._tasks.pop(session_key, None)
            try:
                if t.cancelled():
                    log.info("agent_task.cancelled", session_key=session_key)
                elif t.exception():
                    log.error(
                        "agent_task.failed",
                        session_key=session_key,
                        error=str(t.exception()),
                    )
                else:
                    log.info("agent_task.completed", session_key=session_key)
            except BrokenPipeError:
                pass

        task.add_done_callback(_on_done)

    def cancel(self, session_key: str) -> bool:
        """Cancel the running agent task for a session.

        Returns True if a task was cancelled, False if no task was running.
        """
        task = self._tasks.get(session_key)
        if task is None or task.done():
            return False

        task.cancel()
        log.info("agent_task.cancel_requested", session_key=session_key)
        return True

    def get(self, session_key: str) -> asyncio.Task | None:
        """Return the tracked task for a session, if any."""
        return self._tasks.get(session_key)

    def is_running(self, session_key: str) -> bool:
        """Check if an agent task is currently running for a session."""
        task = self._tasks.get(session_key)
        return task is not None and not task.done()

    def get_all(self) -> dict[str, asyncio.Task]:
        """Get all currently running agent tasks."""
        return dict(self._tasks)


# Global singleton registry
_registry: AgentTaskRegistry | None = None


def get_agent_task_registry() -> AgentTaskRegistry:
    """Get or create the global agent task registry."""
    global _registry
    if _registry is None:
        _registry = AgentTaskRegistry()
    return _registry
