"""ChannelManager — lifecycle management for ManagedChannel adapters."""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any

import structlog
from starlette.routing import Route

from agentos.channels.registry import build_managed_channel
from agentos.channels.types import ChannelHealth, DeliveryTargetResolution, ManagedChannel
from agentos.gateway._debounce import _DefaultDebounceCoordinator
from agentos.gateway.channel_dispatch import run_channel_dispatch
from agentos.session.keys import DmScope, build_direct_key, build_group_key, build_thread_key

log = structlog.get_logger(__name__)

#: Start/dispatch failures that no restart can fix: a revoked or wrong token,
#: an account the platform disabled. The health sweep never auto-restarts a
#: channel that hit one; an operator restart (after fixing the credentials)
#: clears it.
_TERMINAL_ERROR_RE = re.compile(
    r"(?<!\d)(401|403|4004)(?!\d)|unauthori[sz]ed|authentication failed|invalid[_ ]?auth"
    r"|invalid[_ ]?token|not_authed|token_revoked|account_inactive|invalid_api_key"
    r"|forbidden|login ?failure|improper token",
    re.IGNORECASE,
)


def terminal_reason_for(exc: BaseException) -> str | None:
    """``"auth: <message>"`` for a credential-class failure, else ``None``."""
    text = f"{type(exc).__name__}: {exc}"
    if _TERMINAL_ERROR_RE.search(text):
        return f"auth: {text[:200]}"
    return None


#: Runtime states :meth:`ChannelManager.runtime_status` reports. ``stale-socket``
#: is added on top by the gateway health sweep, which owns the clock.
CHANNEL_STATES = (
    "connected",
    "disconnected",
    "stuck",
    "retrying",
    "start_failed",
    "stopped",
    "terminal",
    "blocked",
)

#: :meth:`ChannelManager.restart_if_idle` outcomes. Only ``restarted`` means
#: the channel runs again; ``failed`` means the start raised and a bounded
#: retry loop took over (the channel reads ``retrying``, then ``stuck`` when
#: the loop gives up, which the health sweep picks up again).
RESTART_OUTCOMES = ("restarted", "failed", "busy", "terminal", "stopped", "unknown")


#: Channel types whose send path takes a thread id as the address it delivers
#: to: Slack's ``thread_ts``, Telegram's forum ``message_thread_id``, Discord's
#: thread channel id. Email threads exist too, but there the thread key *is*
#: the ``channel_id``, so a second thread id beside it is still a mistake and
#: keeps being refused.
_THREAD_ADDRESSABLE = frozenset({"slack", "telegram", "discord"})


@dataclass
class ChannelManager:
    """Manages lifecycle of ManagedChannel instances.

    Responsibilities:
    - Build adapters from gateway config entries (from_config)
    - Collect webhook routes for Starlette registration
    - Start/stop/restart individual channels or all at once
    - Run dispatch loops with exponential-backoff retry
    - Build proper session keys via session/keys.py
    """

    _channels: dict[str, ManagedChannel]
    _turn_runner: Any  # TurnRunner (avoid circular import at module level)
    _session_manager: Any  # SessionManager
    _event_bridge: Any = None  # EventBridge | None (injected from gateway boot)
    _config: Any = None
    _task_runtime: Any = None
    _rpc_dispatcher: Any = None
    _channel_rpc_context_factory: Callable[[Any], Any] | None = None
    _debounce_coordinator: Any = field(default_factory=_DefaultDebounceCoordinator)
    _agent_ids: dict[str, str] = field(default_factory=dict)
    _channel_types: dict[str, str] = field(default_factory=dict)
    _tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    # Per-channel in-flight reply task sets.
    # Keyed by channel name; populated in _safe_start and consumed in stop_channel.
    _in_flight_sets: dict[str, Any] = field(default_factory=dict)
    # Per-channel "current session" pointer maps for channel /new (fresh-key)
    # semantics. Keyed by channel name; populated in _safe_start.
    _session_pointers: dict[str, Any] = field(default_factory=dict)
    # Dispatch state machine — see _dispatch_with_retry for the lifecycle.
    # Values: "running" | "exhausted" | "restarting" | "dead". Unset entries
    # are treated as "unknown" by health() so a channel that never started
    # does not look healthy.
    _dispatch_states: dict[str, str] = field(default_factory=dict)
    _restart_counts: dict[str, int] = field(default_factory=dict)
    _start_errors: dict[str, dict[str, str]] = field(default_factory=dict)
    # Runtime status for the gateway health sweep (epoch seconds): when each
    # channel last (re)started and last handed the dispatch loop an inbound
    # event, and why a channel is terminal (never auto-restarted).
    _started_at: dict[str, float] = field(default_factory=dict)
    _last_event_at: dict[str, float] = field(default_factory=dict)
    _terminal_reasons: dict[str, str] = field(default_factory=dict)
    # Channels whose last start raised a non-terminal error (network down,
    # adapter not reachable yet). They read ``start_failed`` and stay eligible
    # for an automatic restart; an operator stop clears the mark.
    _start_failed: dict[str, str] = field(default_factory=dict)
    _clock: Callable[[], float] = field(default=time.time, repr=False)
    # Bounded retry after a failed automatic restart (overridable for tests):
    # the dispatch loop used to own retries; a restart that fails before a
    # loop exists hands them to this instead.
    _start_retry_attempts: int = 6
    _sleep: Callable[[float], Any] = field(default=asyncio.sleep, repr=False)
    # Inner-loop retry policy (overridable for tests).
    _max_retries: int = 5
    _retry_backoff_initial: float = 1.0
    _retry_backoff_max: float = 60.0
    # Outer-loop restart policy. ``dead`` is operator-recoverable via the
    # ``channels.restart`` Control RPC; the cap only bounds *automatic*
    # restart attempts.
    _restart_delay_s: float = 30.0
    _max_restart_cycles: int = 3

    # ── Factory ──────────────────────────────────────────────

    @classmethod
    def from_config(
        cls,
        entries: list,
        *,
        turn_runner: Any,
        session_manager: Any,
        event_bridge: Any = None,
        config: Any = None,
        task_runtime: Any = None,
        rpc_dispatcher: Any = None,
        channel_rpc_context_factory: Callable[[Any], Any] | None = None,
    ) -> ChannelManager:
        """Build adapter instances from gateway config entries.

        Each entry's ``type`` field selects the adapter class.
        Disabled entries are skipped.
        """
        channels: dict[str, ManagedChannel] = {}
        agent_ids: dict[str, str] = {}
        channel_types: dict[str, str] = {}
        slack_webhook_count = sum(
            entry.enabled
            and entry.type == "slack"
            and getattr(entry, "connection_mode", "webhook") == "webhook"
            for entry in entries
        )
        # The first Slack webhook entry encountered keeps whatever path it
        # already resolves to (explicit, or the bare "/slack/events" default
        # when unset) instead of being auto-derived. Without this, adding a
        # second account silently re-paths an already-configured account
        # (webhook_path unset -> "/slack/events/<name>"), 404ing a Slack app
        # that is already pointed at the old default with no error at
        # startup. Only accounts added *after* the first are named.
        seen_first_slack_webhook = False
        for entry in entries:
            if not entry.enabled:
                log.info("channel.skipped_disabled", name=entry.name)
                continue

            is_slack_webhook = (
                entry.type == "slack" and getattr(entry, "connection_mode", "webhook") == "webhook"
            )
            if is_slack_webhook and not seen_first_slack_webhook:
                seen_first_slack_webhook = True
            elif (
                slack_webhook_count > 1
                and is_slack_webhook
                and not getattr(entry, "webhook_path", "")
            ):
                if entry.name in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9._~-]+", entry.name):
                    raise ValueError(
                        f"Slack channel {entry.name!r} needs an explicit webhook_path: "
                        "its name is not a safe URL path segment"
                    )
                # Copy rather than persisting a derived default into the user's config.
                entry = entry.model_copy(update={"webhook_path": f"/slack/events/{entry.name}"})

            adapter = build_managed_channel(entry)
            if adapter is None:
                log.warning("channel.unknown_type", type=entry.type, name=entry.name)
                continue

            channels[entry.name] = adapter
            cls._register_tool_channel(entry.name, adapter)
            agent_ids[entry.name] = getattr(entry, "agent_id", "main")
            channel_types[entry.name] = entry.type
            setattr(adapter, "debounce_window_s", getattr(entry, "debounce_window_s", 0.0))
            log.info("channel.adapter_created", name=entry.name, type=entry.type)

        return cls(
            _channels=channels,
            _turn_runner=turn_runner,
            _session_manager=session_manager,
            _event_bridge=event_bridge,
            _config=config,
            _task_runtime=task_runtime,
            _rpc_dispatcher=rpc_dispatcher,
            _channel_rpc_context_factory=channel_rpc_context_factory,
            _agent_ids=agent_ids,
            _channel_types=channel_types,
        )

    @staticmethod
    def _register_tool_channel(name: str, adapter: ManagedChannel) -> None:
        try:
            from agentos.tools.builtin.messaging import register_channel

            register_channel(name, adapter)
        except Exception as exc:
            log.debug("channel.tool_register_failed", name=name, tool="message", error=str(exc))

    @staticmethod
    def _unregister_tool_channel(name: str, adapter: ManagedChannel | None) -> None:
        try:
            from agentos.tools.builtin.messaging import unregister_channel

            unregister_channel(name)
        except Exception as exc:
            log.debug("channel.tool_unregister_failed", name=name, tool="message", error=str(exc))

    # ── Webhook routes ───────────────────────────────────────

    def collect_webhook_routes(self) -> list[Route]:
        """Extract Starlette Routes from adapters that support webhooks.

        Slack adapters expose ``create_webhook_route()``;
        Discord uses a persistent WebSocket and has no webhook.
        Conflicting paths/methods are rejected instead of shadowing an account.
        """
        routes: list[Route] = []
        owners: dict[str, list[tuple[str, Route]]] = {}
        for name, adapter in self._channels.items():
            if getattr(adapter, "transport_name", "webhook") != "webhook":
                continue
            if hasattr(adapter, "create_webhook_route"):
                route = adapter.create_webhook_route()
                for other_name, other_route in owners.get(route.path, []):
                    if (
                        not route.methods
                        or not other_route.methods
                        or route.methods & other_route.methods
                    ):
                        raise ValueError(
                            f"Webhook route {route.path!r} conflicts between channel entries "
                            f"{other_name!r} and {name!r}; configure distinct webhook_path values"
                        )
                owners.setdefault(route.path, []).append((name, route))
                routes.append(route)
                log.info("channel.webhook_route_collected", channel=name, path=route.path)
        return routes

    # ── Lifecycle ────────────────────────────────────────────

    async def start_all(self) -> dict[str, bool]:
        """Start all channels concurrently.

        Returns ``{name: success}`` map.  Partial failures do NOT
        prevent other channels from starting.
        """
        results = await asyncio.gather(
            *[self._safe_start(name) for name in self._channels],
            return_exceptions=True,
        )
        statuses: dict[str, bool] = {}
        for name, result in zip(self._channels, results):
            if isinstance(result, BaseException):
                self._note_start_failure(name, result)
                statuses[name] = False
            else:
                self._note_start_success(name)
                statuses[name] = True
        return statuses

    def _note_start_failure(self, name: str, exc: BaseException) -> None:
        self._start_errors[name] = {
            "error_type": type(exc).__name__,
            "error": str(exc),
            "exception": repr(exc),
        }
        reason = terminal_reason_for(exc)
        if reason:
            self._terminal_reasons[name] = reason
            self._start_failed.pop(name, None)
        else:
            self._start_failed[name] = f"{type(exc).__name__}: {exc}"[:200]

    def _note_start_success(self, name: str) -> None:
        self._start_errors.pop(name, None)
        self._start_failed.pop(name, None)

    def failed_channels(self) -> dict[str, str]:
        """Channels that are down for a reason: terminal, or their last start failed."""
        failed = {name: reason for name, reason in self._start_failed.items()}
        failed.update(self._terminal_reasons)
        return {name: failed[name] for name in sorted(failed)}

    async def start_stopped(self) -> dict[str, bool]:
        """Start only the channels with no dispatch loop; running ones are left alone.

        :meth:`start_all` assumes nothing runs yet (boot). This is the safe
        form for later: leaving the gateway's safe mode, or ``channels.start``
        while some channels already run.
        """
        statuses: dict[str, bool] = {}
        for name in self._channels:
            if name in self._tasks:
                continue
            try:
                await self._safe_start(name)
            except Exception as exc:  # noqa: BLE001 - reported per channel
                self._note_start_failure(name, exc)
                statuses[name] = False
            else:
                self._note_start_success(name)
                statuses[name] = True
        return statuses

    def start_errors(self) -> dict[str, dict[str, str]]:
        """Return sanitized per-channel startup errors for operator diagnostics."""
        return {name: dict(details) for name, details in self._start_errors.items()}

    async def _safe_start(self, name: str) -> None:
        """Start a single channel with 30 s timeout, then launch dispatch loop."""
        from agentos.gateway.channel_dispatch import (
            ChannelSessionPointers,
            _ChannelInFlightSet,
            _compute_channel_cap,
        )

        adapter = self._channels[name]
        startup_timeout = float(getattr(adapter, "startup_timeout_s", 30.0))
        try:
            self._unregister_tool_channel(name, adapter)
            await asyncio.wait_for(adapter.start(), timeout=startup_timeout)
            self._register_tool_channel(name, adapter)
        except Exception:
            stop = getattr(adapter, "stop", None)
            if callable(stop):
                with contextlib.suppress(Exception):
                    await stop()
            self._unregister_tool_channel(name, adapter)
            raise
        entry_agent_id = self._agent_ids.get(name, "main")
        build_key = partial(self._build_session_key, name, agent_id=entry_agent_id)

        def key_builder(msg: Any) -> str:
            # Called once per inbound event the dispatch loop receives: the
            # cheapest place to know a socket still delivers.
            self._last_event_at[name] = self._clock()
            return build_key(msg)

        self._started_at[name] = self._clock()
        cap = _compute_channel_cap(self._config)
        in_flight = _ChannelInFlightSet(cap)
        self._in_flight_sets[name] = in_flight
        # One pointer map per channel instance: backs channel /new "fresh session"
        # semantics so a /new points the chat at a brand-new key (see
        # ChannelSessionPointers). Recreated on (re)start; cleared on restart.
        self._session_pointers[name] = ChannelSessionPointers()
        self._tasks[name] = asyncio.create_task(
            self._dispatch_with_retry(name, key_builder, in_flight=in_flight),
            name=f"channel:{name}",
        )

    async def _run_one_dispatch_cycle(
        self,
        name: str,
        key_builder: Callable[[Any], str],
        in_flight: Any = None,
    ) -> None:
        """Inner retry loop. Returns once retries are exhausted.

        CRITICAL: ``CancelledError`` always propagates — it signals
        intentional shutdown via ``stop_channel``.  Only ``Exception``
        subclasses trigger retry.
        """
        backoff = self._retry_backoff_initial
        max_backoff = self._retry_backoff_max

        for attempt in range(self._max_retries + 1):
            try:
                await run_channel_dispatch(
                    channel=self._channels[name],
                    turn_runner=self._turn_runner,
                    session_manager=self._session_manager,
                    session_key_builder=key_builder,
                    session_prefix=name,
                    event_bridge=self._event_bridge,
                    config=self._config,
                    task_runtime=self._task_runtime,
                    rpc_dispatcher=self._rpc_dispatcher,
                    channel_rpc_context_factory=self._channel_rpc_context_factory,
                    debounce_coordinator=self._debounce_coordinator,
                    debounce_window_s=getattr(self._channels[name], "debounce_window_s", 0.0),
                    _in_flight=in_flight,
                    session_pointers=self._session_pointers.get(name),
                )
            except asyncio.CancelledError:
                raise  # intentional shutdown — never retry
            except Exception as exc:
                log.error(
                    "channel.dispatch_error",
                    channel=name,
                    attempt=attempt,
                    max_retries=self._max_retries,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                reason = terminal_reason_for(exc)
                if reason:
                    self._terminal_reasons[name] = reason
                if attempt < self._max_retries:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, max_backoff)
                else:
                    log.error("channel.dispatch_exhausted", channel=name)

    async def _dispatch_with_retry(
        self,
        name: str,
        key_builder: Callable[[Any], str],
        in_flight: Any = None,
    ) -> None:
        """Outer cycle loop wrapping the inner retry budget.

        Each iteration runs one dispatch cycle. After the inner retry budget
        is exhausted the channel transitions through
        ``running → exhausted → restarting → running`` until the configured
        restart cap is hit, at which point it transitions to ``dead`` and
        the loop exits. ``dead`` is operator-recoverable through
        ``restart_channel``.
        """
        self._dispatch_states[name] = "running"
        self._restart_counts.setdefault(name, 0)
        while True:
            await self._run_one_dispatch_cycle(name, key_builder, in_flight=in_flight)

            self._dispatch_states[name] = "exhausted"
            log.warning(
                "dispatch.running_to_exhausted",
                channel=name,
                restart_count=self._restart_counts[name],
            )

            if self._restart_counts[name] >= self._max_restart_cycles:
                self._dispatch_states[name] = "dead"
                log.error(
                    "dispatch.restarting_to_dead",
                    channel=name,
                    restart_count=self._restart_counts[name],
                )
                return

            self._restart_counts[name] += 1
            self._dispatch_states[name] = "restarting"
            log.warning(
                "dispatch.exhausted_to_restarting",
                channel=name,
                restart_count=self._restart_counts[name],
                max_cycles=self._max_restart_cycles,
            )
            await asyncio.sleep(self._restart_delay_s)
            self._dispatch_states[name] = "running"

    async def stop_all(self) -> None:
        """Stop every managed channel (dispatch task + adapter)."""
        for name in list(self._channels):
            await self.stop_channel(name)
        await self._debounce_coordinator.cancel_all()

    async def stop_channel(self, name: str) -> None:
        """Cancel dispatch task, cancel all in-flight reply tasks, then stop adapter.

        MUST use this instead of calling ``adapter.stop()`` directly,
        otherwise the dispatch task becomes orphaned.

        In-flight reply tasks are cancelled and awaited
        before the adapter is stopped so no dangling coroutines remain after
        shutdown.
        """
        task = self._tasks.pop(name, None)
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        # A stopped channel is not "dead" or "retrying" any more: without
        # this a logged-out channel would read ``stuck`` and be revived by
        # the health sweep. ``restart_channel`` sets the state again.
        if name in self._dispatch_states:
            self._dispatch_states[name] = "stopped"
        self._start_failed.pop(name, None)
        # Cancel and await all in-flight reply tasks for this channel.
        in_flight = self._in_flight_sets.pop(name, None)
        if in_flight is not None:
            await in_flight.cancel_all()
        adapter = self._channels.get(name)
        if adapter:
            await adapter.stop()
        self._unregister_tool_channel(name, adapter)

    async def restart_channel(self, name: str) -> None:
        """Stop then re-start a single channel.

        On a ``dead`` channel this is the operator-recoverable path: the
        restart counter is cleared and a single ``dispatch.dead_to_running``
        decision-log entry is emitted before the new dispatch loop spins up.
        """
        prev_state = self._dispatch_states.get(name)
        await self.stop_channel(name)
        self._restart_counts[name] = 0
        # An operator restart is the way out of a terminal state: the
        # credentials were presumably fixed. A failure sets it again.
        self._terminal_reasons.pop(name, None)
        if prev_state == "dead":
            log.info("dispatch.dead_to_running", channel=name)
        try:
            await self._safe_start(name)
        except Exception as exc:
            self._note_start_failure(name, exc)
            raise
        self._note_start_success(name)

    async def start_channel(self, name: str) -> None:
        """Start one channel: a restart when it runs, a first start when it does not."""
        if name not in self._channels:
            raise KeyError(f"Channel not found: {name}")
        await self.restart_channel(name)

    def in_flight_count(self, name: str) -> int:
        in_flight = self._in_flight_sets.get(name)
        count = getattr(in_flight, "active_count", None)
        return int(count()) if callable(count) else 0

    async def restart_if_idle(self, name: str) -> str:
        """Restart ``name`` unless it is busy, terminal or stopped by the operator.

        Used by the gateway's thaw handler and health sweep, never by an
        operator. Returns one of :data:`RESTART_OUTCOMES`. A restart whose
        start raises a non-terminal error (the network is not back yet after
        a wake) is not left stopped: a bounded retry loop takes over and the
        channel reads ``retrying``; when that loop gives up the channel reads
        ``stuck``, which the next eligible sweep restarts again. Only a
        credential-class failure ends automatic restarts (``terminal``).
        """
        if name not in self._channels:
            return "unknown"
        if self._terminal_reasons.get(name):
            return "terminal"
        if (
            name not in self._tasks
            and name not in self._start_failed
            and self._dispatch_states.get(name) != "dead"
        ):
            return "stopped"
        if self.in_flight_count(name) > 0:
            log.info("channel.restart_deferred_busy", channel=name)
            return "busy"
        try:
            await self.restart_channel(name)
        except Exception as exc:  # noqa: BLE001 - the sweep logs and moves on
            log.warning(
                "channel.auto_restart_failed",
                channel=name,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            if self._terminal_reasons.get(name):
                return "terminal"
            self._begin_start_retry(name)
            return "failed"
        return "restarted"

    def _begin_start_retry(self, name: str) -> None:
        self._dispatch_states[name] = "retrying"
        self._tasks[name] = asyncio.create_task(
            self._retry_start(name), name=f"channel-start-retry:{name}"
        )

    async def _retry_start(self, name: str) -> None:
        """Retry a failed start with backoff; ``dead`` (read as ``stuck``) when out of tries."""
        delay = self._retry_backoff_initial
        for attempt in range(1, self._start_retry_attempts + 1):
            await self._sleep(delay)
            try:
                await self._safe_start(name)
            except Exception as exc:  # noqa: BLE001 - every outcome is recorded
                self._note_start_failure(name, exc)
                if self._terminal_reasons.get(name):
                    log.error("channel.start_retry_terminal", channel=name, error=str(exc))
                    self._dispatch_states[name] = "stopped"
                    return
                log.warning(
                    "channel.start_retry_failed",
                    channel=name,
                    attempt=attempt,
                    max_attempts=self._start_retry_attempts,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                delay = min(delay * 2, self._retry_backoff_max)
                continue
            # _safe_start replaced this task with the new dispatch loop.
            self._note_start_success(name)
            log.info("channel.start_recovered", channel=name, attempt=attempt)
            return
        self._dispatch_states[name] = "dead"
        log.error("channel.start_retry_exhausted", channel=name)

    async def runtime_status(self) -> dict[str, dict[str, Any]]:
        """Per-channel runtime facts for the health sweep and ``channels.status``.

        ``state`` is one of :data:`CHANNEL_STATES`. Adapters may report a
        terminal condition themselves through ``health_check().extra``
        (``terminal_reason``, or ``blocked``); otherwise it is inferred from
        start/dispatch errors that look like a credential failure.
        """
        out: dict[str, dict[str, Any]] = {}
        for name, adapter in self._channels.items():
            connected = False
            extra: dict[str, Any] = {}
            try:
                health = await adapter.health_check()
                connected = bool(getattr(health, "connected", False))
                extra = dict(getattr(health, "extra", None) or {})
            except Exception:  # noqa: BLE001 - a broken probe reads as disconnected
                connected = False
            terminal = self._terminal_reasons.get(name) or extra.get("terminal_reason")
            dispatch_state = self._dispatch_states.get(name, "unknown")
            task = self._tasks.get(name)
            if extra.get("blocked"):
                state = "blocked"
                terminal = terminal or f"blocked: {extra.get('blocked')}"
            elif terminal:
                state = "terminal"
            elif dispatch_state == "retrying" and task is not None and not task.done():
                state = "retrying"
            elif dispatch_state == "dead" or (
                task is not None and task.done() and dispatch_state in {"running", "retrying"}
            ):
                state = "stuck"
            elif task is None:
                state = "start_failed" if name in self._start_failed else "stopped"
            elif not connected:
                state = "disconnected"
            else:
                state = "connected"
            out[name] = {
                "state": state,
                "connected": connected,
                "dispatch_state": dispatch_state,
                "transport": getattr(adapter, "transport_name", None) or extra.get("transport"),
                "started_at": self._started_at.get(name),
                "last_event_at": self._last_event_at.get(name),
                "terminal_reason": str(terminal) if terminal else None,
                "start_error": self._start_failed.get(name),
                "in_flight": self.in_flight_count(name),
            }
        return out

    # ── Health ───────────────────────────────────────────────

    async def health(self) -> dict[str, ChannelHealth]:
        """Return health status for every managed channel.

        Adapter-reported ``ChannelHealth.extra`` is augmented with the
        dispatch-loop state so operators can distinguish "channel dropped a
        message" from "channel is permanently dead pending a Control restart".
        """
        out: dict[str, ChannelHealth] = {}
        for name, a in self._channels.items():
            health = await a.health_check()
            health.extra["dispatch_state"] = self._dispatch_states.get(name, "unknown")
            out[name] = health
        return out

    # ── Accessors ────────────────────────────────────────────

    def items(self):  # noqa: ANN201
        """Iterate ``(name, adapter)`` pairs."""
        return self._channels.items()

    def get(self, name: str) -> ManagedChannel | None:
        """Look up an adapter by name."""
        return self._channels.get(name)

    def resolve_delivery_target(
        self,
        *,
        target: str,
        to: str = "",
        account_id: str = "",
        thread_id: str = "",
    ) -> DeliveryTargetResolution:
        """Resolve delivery fields to a concrete adapter.

        ``target`` may be an AgentOS adapter entry name, or a
        channel type such as ``slack`` when the type maps to one adapter.
        ``account_id`` currently selects a concrete entry until agentos grows a
        first-class multi-account channel config.

        An exact entry name wins, then a channel type, then an entry name that
        differs only by case. That last step is for jobs already stored with a
        folded name (older clients lowercased what they saved), so ``mytele``
        still reaches the entry named ``MyTele``.
        """

        target_name = target.strip()
        target_type = target_name.lower()
        account = account_id.strip()
        to = to.strip()
        thread = thread_id.strip()

        if not target_name:
            return DeliveryTargetResolution(ok=False, reason="unsupported_target")

        candidates = [
            name
            for name, channel_type in self._channel_types.items()
            if channel_type.lower() == target_type
        ]
        if account:
            if account not in candidates:
                return DeliveryTargetResolution(ok=False, reason="unsupported_account")
            return self._build_delivery_resolution(
                adapter_name=account,
                channel_type=target_type,
                to=to,
                account_id=account,
                thread_id=thread,
            )

        if target_name in self._channels:
            adapter_name = target_name
            channel_type = self._channel_types.get(adapter_name, adapter_name).lower()
            return self._build_delivery_resolution(
                adapter_name=adapter_name,
                channel_type=channel_type,
                to=to,
                account_id=account,
                thread_id=thread,
            )

        channel_type = target_type
        if not candidates:
            candidates = [name for name in self._channels if name.lower() == target_type]
            if not candidates:
                return DeliveryTargetResolution(ok=False, reason="unsupported_target")
            channel_type = self._channel_types.get(candidates[0], candidates[0]).lower()
        if len(candidates) > 1:
            return DeliveryTargetResolution(ok=False, reason="ambiguous_account")

        return self._build_delivery_resolution(
            adapter_name=candidates[0],
            channel_type=channel_type,
            to=to,
            account_id=account,
            thread_id=thread,
        )

    def _build_delivery_resolution(
        self,
        *,
        adapter_name: str,
        channel_type: str,
        to: str,
        account_id: str,
        thread_id: str,
    ) -> DeliveryTargetResolution:
        if thread_id and channel_type not in _THREAD_ADDRESSABLE:
            return DeliveryTargetResolution(ok=False, reason="unsupported_thread")
        return DeliveryTargetResolution(
            ok=True,
            adapter=self._channels.get(adapter_name),
            adapter_name=adapter_name,
            channel_type=channel_type,
            to=to,
            account_id=account_id,
            thread_id=thread_id,
        )

    # ── Session key builder ──────────────────────────────────

    @staticmethod
    def _build_session_key(channel_name: str, msg: Any, agent_id: str = "main") -> str:
        """Build a proper session key using ``session/keys.py`` builders.

        Detects group vs DM from message metadata:
        - Discord: ``metadata.guild_id is not None``
        - Slack: ``metadata.channel_type in ("channel", "group")``

        Group keys use ``msg.channel_id`` as peer_id (per-room session).
        DM keys use ``msg.sender_id`` as peer_id (per-user session), plus a
        ``:thread:`` suffix when the adapter opts in with
        ``metadata['dm_thread_scoped']``.
        """
        meta = getattr(msg, "metadata", {}) or {}
        flag = meta.get("is_group")
        if flag is not None:
            is_group = bool(flag)
        else:
            # Adapter metadata fallback for events that do not yet carry the
            # ``metadata['is_group']`` contract documented in ``channels/types.py``.
            is_group = (
                meta.get("guild_id") is not None  # Discord
                or meta.get("channel_type") in ("channel", "group")  # Slack
            )

        if is_group:
            base_key = build_group_key(
                agent_id=agent_id,
                channel=channel_name,
                peer_id=msg.channel_id,  # group/room ID, NOT sender
            )
            thread_id = (
                meta.get("native_thread_id") or meta.get("thread_ts") or meta.get("thread_id")
            )
            if isinstance(thread_id, str) and thread_id:
                return build_thread_key(base_key, thread_id, channel_hint=channel_name)
            return base_key

        direct_key = build_direct_key(
            agent_id=agent_id,
            channel=channel_name,
            peer_id=msg.sender_id,
            dm_scope=DmScope.PER_CHANNEL_PEER,
        )
        # A DM surface can still be threaded — one email peer holds many
        # independent threads. Opt-in only: an adapter asks for per-thread DM
        # sessions with ``metadata['dm_thread_scoped']``, so chat adapters that
        # happen to carry a thread id on a DM keep their per-peer session.
        dm_thread_id = meta.get("native_thread_id")
        if meta.get("dm_thread_scoped") and isinstance(dm_thread_id, str) and dm_thread_id:
            return build_thread_key(direct_key, dm_thread_id, channel_hint=channel_name)
        return direct_key
