"""Keep configured MCP servers connected: retry failed connects, revive dead stdio servers.

A server that cannot be reached at boot is retried in the background on an
exponential backoff — 30 s, doubling, capped at 10 min — and each failed
attempt logs once. A stdio server whose process exits on its own is marked
unavailable (its tools leave the registry, so the model is not offered tools
that can only fail) and reconnected on the same schedule; its tools are
registered again when it comes back. An operator's ``mcp.disconnect`` or a
gateway shutdown closes the client deliberately and is never "revived".
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from agentos.mcp import discovery
from agentos.mcp.types import MCPServerConfig

if TYPE_CHECKING:
    from agentos.tools.registry import ToolRegistry

log = structlog.get_logger(__name__)

INITIAL_BACKOFF_SECONDS = 30.0
MAX_BACKOFF_SECONDS = 600.0


class Backoff:
    """30 s, 60 s, 120 s, … capped at ``cap``. ``reset()`` starts over."""

    def __init__(
        self, initial: float = INITIAL_BACKOFF_SECONDS, cap: float = MAX_BACKOFF_SECONDS
    ) -> None:
        self.initial = initial
        self.cap = cap
        self.attempt = 0

    def peek(self) -> float:
        return min(self.cap, self.initial * float(2**self.attempt))

    def next_delay(self) -> float:
        delay = self.peek()
        # Once at the cap the exponent stops mattering; stop growing it.
        if delay < self.cap:
            self.attempt += 1
        return delay

    def reset(self) -> None:
        self.attempt = 0


@dataclass
class RetryState:
    """What ``mcp.status`` shows for a server that is waiting to reconnect."""

    reason: str
    attempts: int = 0
    last_error: str | None = None
    next_retry_at: float | None = None  # wall-clock epoch seconds

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "attempts": self.attempts,
            "lastError": self.last_error,
            "nextRetryAt": self.next_retry_at,
        }


Sleep = Callable[[float], Awaitable[None]]


class MCPSupervisor:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        default_connect_timeout: float,
        sleep: Sleep = asyncio.sleep,
        clock: Callable[[], float] = time.time,
        initial_backoff: float = INITIAL_BACKOFF_SECONDS,
        max_backoff: float = MAX_BACKOFF_SECONDS,
    ) -> None:
        self.registry = registry
        self.default_connect_timeout = default_connect_timeout
        self._sleep = sleep
        self._clock = clock
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff
        self._retries: dict[str, asyncio.Task[None]] = {}
        self._watchers: dict[str, asyncio.Task[None]] = {}
        self._state: dict[str, RetryState] = {}
        self._closed = False

    # ── public surface ───────────────────────────────────────────────

    def retry_state(self, name: str) -> RetryState | None:
        return self._state.get(name)

    def schedule_retry(self, config: MCPServerConfig, *, error: str, reason: str) -> None:
        """Start the backoff loop for *config* unless one is already running."""
        if self._closed or not config.enabled:
            return
        existing = self._retries.get(config.name)
        if existing is not None and not existing.done():
            return
        self._state[config.name] = RetryState(reason=reason, last_error=error)
        self._retries[config.name] = asyncio.get_running_loop().create_task(
            self._retry_loop(config), name=f"mcp-retry-{config.name}"
        )

    def watch(self, config: MCPServerConfig) -> None:
        """Revive *config*'s stdio server if its process dies on its own."""
        if self._closed:
            return
        entry = next(
            (e for e in discovery.active_clients_snapshot() if e.server_name == config.name),
            None,
        )
        wait_for_exit = getattr(entry.client, "wait_for_exit", None) if entry else None
        if wait_for_exit is None:
            return
        self._cancel(self._watchers, config.name)
        self._watchers[config.name] = asyncio.get_running_loop().create_task(
            self._watch_exit(config, wait_for_exit), name=f"mcp-watch-{config.name}"
        )

    def forget(self, name: str) -> None:
        """Stop retrying and watching *name* — the operator took it over."""
        self._cancel(self._retries, name)
        self._cancel(self._watchers, name)
        self._state.pop(name, None)

    async def aclose(self) -> None:
        self._closed = True
        tasks = [*self._retries.values(), *self._watchers.values()]
        self._retries.clear()
        self._watchers.clear()
        self._state.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # ── internals ────────────────────────────────────────────────────

    @staticmethod
    def _cancel(tasks: dict[str, asyncio.Task[None]], name: str) -> None:
        task = tasks.pop(name, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _connect(self, config: MCPServerConfig) -> list[str]:
        await discovery.disconnect_and_unregister(config.name, self.registry)
        timeout = config.effective_connect_timeout(self.default_connect_timeout)
        return await asyncio.wait_for(
            discovery.discover_and_register(config, self.registry), timeout=timeout
        )

    async def _retry_loop(self, config: MCPServerConfig) -> None:
        backoff = Backoff(self._initial_backoff, self._max_backoff)
        state = self._state[config.name]
        while True:
            delay = backoff.next_delay()
            state.next_retry_at = self._clock() + delay
            await self._sleep(delay)
            state.attempts += 1
            try:
                names = await self._connect(config)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - every failure is retried.
                error = "timed out" if isinstance(exc, TimeoutError) else str(exc)
                state.last_error = error
                log.warning(
                    "mcp.reconnect_failed",
                    server=config.name,
                    attempt=state.attempts,
                    error=error,
                    next_retry_seconds=backoff.peek(),
                )
                continue
            log.info(
                "mcp.reconnected",
                server=config.name,
                attempts=state.attempts,
                tools=len(names),
            )
            self._state.pop(config.name, None)
            self._retries.pop(config.name, None)
            self.watch(config)
            return

    async def _watch_exit(
        self, config: MCPServerConfig, wait_for_exit: Callable[[], Awaitable[int | None]]
    ) -> None:
        code = await wait_for_exit()
        if code is None or self._closed:
            return
        self._watchers.pop(config.name, None)
        log.warning("mcp.server_exited", server=config.name, exit_code=code)
        # Unavailable until it is back: a tool that can only fail is not offered.
        await discovery.disconnect_and_unregister(config.name, self.registry)
        self.schedule_retry(config, error=f"process exited with code {code}", reason="exited")


# One per gateway process, installed at boot; ``rpc_mcp`` reaches it here.
_supervisor: MCPSupervisor | None = None


def install_supervisor(registry: ToolRegistry, *, default_connect_timeout: float) -> MCPSupervisor:
    global _supervisor
    if _supervisor is None or _supervisor.registry is not registry or _supervisor._closed:
        _supervisor = MCPSupervisor(registry, default_connect_timeout=default_connect_timeout)
    else:
        _supervisor.default_connect_timeout = default_connect_timeout
    return _supervisor


def get_supervisor() -> MCPSupervisor | None:
    return _supervisor


async def shutdown_supervisor() -> None:
    global _supervisor
    supervisor, _supervisor = _supervisor, None
    if supervisor is not None:
        await supervisor.aclose()
