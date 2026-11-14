"""Gateway lifecycle hooks: ``gateway:startup``, ``gateway:shutdown``, ``gateway:pre-restart``.

Skills and plugins running in the gateway process register async (or plain)
callables with :func:`register_lifecycle_hook` to flush state before the
in-place restart, release a lock at shutdown, or warm a cache at startup.
Operators can add shell commands in the config file:

.. code-block:: toml

    [hooks.lifecycle]
    shutdown = ["/usr/local/bin/backup-agentos"]

Each event has one total time budget shared by everything registered for it
-- ``gateway:shutdown`` 5 s, ``gateway:pre-restart`` 10 s, ``gateway:startup``
10 s -- and runs its hooks concurrently. A hook still running at the deadline
is cancelled (a command is killed); a hook that raises is logged. Neither
ever holds up or fails the gateway transition itself.

Commands run with ``/bin/sh -c`` under the gateway's own environment minus
its credentials (gateway token, operator secret, agent tokens), plus
``AGENTOS_LIFECYCLE_EVENT`` and ``AGENTOS_LIFECYCLE_REASON``. They are
file-only: config RPC writes cannot add one. They are POSIX-only: on Windows
each configured command is reported as failed without being started, while
in-process hooks still run.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

from agentos.env_policy import GATEWAY_CREDENTIAL_ENV

log = structlog.get_logger(__name__)

STARTUP = "gateway:startup"
SHUTDOWN = "gateway:shutdown"
PRE_RESTART = "gateway:pre-restart"

EVENTS: tuple[str, ...] = (STARTUP, SHUTDOWN, PRE_RESTART)

#: Total seconds an event may spend in its hooks.
BUDGETS: dict[str, float] = {STARTUP: 10.0, SHUTDOWN: 5.0, PRE_RESTART: 10.0}

#: ``[hooks.lifecycle]`` key for each event.
CONFIG_KEYS: dict[str, str] = {
    STARTUP: "startup",
    SHUTDOWN: "shutdown",
    PRE_RESTART: "pre_restart",
}

#: Environment a lifecycle command never inherits: the shared gateway
#: credential list (``env_policy.GATEWAY_CREDENTIAL_ENV``) plus an agent token.
_CREDENTIAL_ENV = GATEWAY_CREDENTIAL_ENV | frozenset({"AGENTOS_AGENT_TOKEN"})


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    name: str
    reason: str | None = None


LifecycleHook = Callable[[LifecycleEvent], Awaitable[None] | None]


@dataclass
class LifecycleReport:
    event: str
    ran: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    timed_out: list[str] = field(default_factory=list)
    duration_s: float = 0.0


_hooks: dict[str, list[tuple[str, LifecycleHook]]] = {event: [] for event in EVENTS}
_lock = threading.Lock()


def register_lifecycle_hook(
    event: str, hook: LifecycleHook, *, name: str | None = None
) -> Callable[[], None]:
    """Register ``hook`` for ``event``; returns a function that unregisters it."""
    if event not in EVENTS:
        raise ValueError(f"unknown lifecycle event {event!r}; expected one of {', '.join(EVENTS)}")
    label = name or getattr(hook, "__qualname__", None) or repr(hook)
    entry = (label, hook)
    with _lock:
        _hooks[event].append(entry)

    def unregister() -> None:
        with _lock:
            if entry in _hooks[event]:
                _hooks[event].remove(entry)

    return unregister


_shutdown_started = False


def clear_lifecycle_hooks() -> None:
    global _shutdown_started
    with _lock:
        for entries in _hooks.values():
            entries.clear()
        _shutdown_started = False


async def run_shutdown_hooks_once(config: Any = None, reason: str | None = None) -> None:
    """Run ``gateway:shutdown`` at most once per process.

    Two paths end a gateway: ``GatewayServer.close()`` (Ctrl+C, an in-place
    restart) and the ASGI lifespan shutdown uvicorn runs on SIGTERM
    (``agentos gateway stop``), which does not go through ``close()``. Both
    call this; whichever comes first runs the hooks.
    """
    global _shutdown_started
    with _lock:
        if _shutdown_started:
            return
        _shutdown_started = True
    await run_lifecycle_hooks(SHUTDOWN, config=config, reason=reason)


def _commands(config: Any, event: str) -> list[str]:
    hooks = getattr(config, "hooks", None)
    lifecycle = getattr(hooks, "lifecycle", None)
    values = getattr(lifecycle, CONFIG_KEYS[event], None) or []
    return [str(cmd) for cmd in values if str(cmd).strip()]


def _command_env(event: LifecycleEvent) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in _CREDENTIAL_ENV}
    env["AGENTOS_LIFECYCLE_EVENT"] = event.name
    env["AGENTOS_LIFECYCLE_REASON"] = event.reason or ""
    return env


async def _run_callable(hook: LifecycleHook, event: LifecycleEvent) -> None:
    result = hook(event)
    if inspect.isawaitable(result):
        await result


#: Of each event's budget, kept back to kill and reap what is still running,
#: so the event as a whole ends inside its budget.
_KILL_GRACE_SECONDS = 0.2
_TEARDOWN_RESERVE_SECONDS = 0.3


#: Why a configured command is not started where there is no ``/bin/sh``.
_NO_POSIX_SHELL = "lifecycle hook commands need /bin/sh and are not run on Windows"


def _shell_commands_supported() -> bool:
    return os.name != "nt"


def _kill_group(process: asyncio.subprocess.Process) -> None:
    """Kill the command's whole process group, or the process where there are none."""
    import signal

    killpg = getattr(os, "killpg", None)
    sigkill = getattr(signal, "SIGKILL", None)
    if killpg is not None and sigkill is not None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            killpg(process.pid, sigkill)
        return
    with contextlib.suppress(ProcessLookupError):
        process.kill()


async def _run_command(command: str, event: LifecycleEvent) -> None:
    if not _shell_commands_supported():
        raise RuntimeError(_NO_POSIX_SHELL)
    process = await asyncio.create_subprocess_exec(
        "/bin/sh",
        "-c",
        command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=_command_env(event),
        start_new_session=True,
    )
    try:
        output, _ = await process.communicate()
    except asyncio.CancelledError:
        # The command runs in its own session (start_new_session), so its
        # process group is everything it started: kill all of it, not just
        # the shell, or a ``sleep`` it spawned outlives the gateway.
        _kill_group(process)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.wait(), timeout=_KILL_GRACE_SECONDS)
        raise
    if process.returncode != 0:
        tail = output.decode("utf-8", "replace")[-400:] if output else ""
        raise RuntimeError(f"exit status {process.returncode}: {tail.strip()}")


async def run_lifecycle_hooks(
    event: str,
    *,
    config: Any = None,
    reason: str | None = None,
    budget: float | None = None,
) -> LifecycleReport:
    """Run every hook and command for ``event`` within its budget. Never raises."""
    report = LifecycleReport(event=event)
    if event not in EVENTS:
        return report
    payload = LifecycleEvent(name=event, reason=reason)
    with _lock:
        registered = list(_hooks[event])
    jobs: list[tuple[str, Awaitable[None]]] = [
        (label, _run_callable(hook, payload)) for label, hook in registered
    ]
    jobs.extend(
        (f"command:{command}", _run_command(command, payload))
        for command in _commands(config, event)
    )
    if not jobs:
        return report
    started = time.monotonic()
    tasks = {asyncio.ensure_future(job): label for label, job in jobs}
    limit = budget if budget is not None else BUDGETS[event]
    done, pending = await asyncio.wait(
        tasks, timeout=max(0.0, limit - min(_TEARDOWN_RESERVE_SECONDS, limit / 2))
    )
    for task in pending:
        task.cancel()
        report.timed_out.append(tasks[task])
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    for task in done:
        label = tasks[task]
        error: BaseException | None = (
            asyncio.CancelledError() if task.cancelled() else task.exception()
        )
        if error is None:
            report.ran.append(label)
            continue
        report.failed.append(label)
        log.warning(
            "gateway.lifecycle_hook_failed",
            lifecycle_event=event,
            hook=label,
            error=str(error) or type(error).__name__,
        )
    report.duration_s = round(time.monotonic() - started, 3)
    if report.timed_out:
        log.warning(
            "gateway.lifecycle_hook_timeout",
            lifecycle_event=event,
            hooks=report.timed_out,
            budget_s=limit,
        )
    log.info(
        "gateway.lifecycle_hooks",
        lifecycle_event=event,
        ran=len(report.ran),
        failed=len(report.failed),
        timed_out=len(report.timed_out),
        duration_s=report.duration_s,
    )
    return report


__all__ = [
    "BUDGETS",
    "EVENTS",
    "PRE_RESTART",
    "SHUTDOWN",
    "STARTUP",
    "LifecycleEvent",
    "LifecycleHook",
    "LifecycleReport",
    "clear_lifecycle_hooks",
    "register_lifecycle_hook",
    "run_lifecycle_hooks",
    "run_shutdown_hooks_once",
]
