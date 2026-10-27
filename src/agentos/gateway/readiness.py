"""The gateway's side of the supervisor contract: one readiness state machine.

A host that supervises the gateway (the desktop app, launchd, systemd, an
operator's script) reads four things from it, and all four are derived here
so they can never disagree:

* ``GET /ready`` -- ``{ready, state, failing, ...}``; ``state`` is one of
  ``starting``, ``ready``, ``draining``, ``safe_mode``. ``503`` while
  starting or draining, ``200`` otherwise (safe mode included: the control
  plane is up so the operator can repair what tripped the breaker).
* ``GET /startupz`` -- ``200`` once every boot sidecar settled (channels
  started, router preloaded), ``503`` before.
* the WebSocket handshake -- refused while ``starting`` with a retryable
  ``UNAVAILABLE`` (``details.code = STARTUP_SIDECARS``) and close ``1013``.
* the ``shutdown`` event -- ``{reason, restart_expected_ms}``;
  :meth:`GatewayReadiness.restart_expected_ms` is the estimate an in-place
  restart sends, ``None`` marks a terminal stop.

``EX_CONFIG`` is the exit code ``agentos gateway run`` uses for a startup
failure only a config change can fix; a supervisor repairs, then retries.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Literal

from agentos.gateway.protocol import ErrorDetailCode

#: ``sysexits.h`` EX_CONFIG: a configuration error. A service manager treats
#: it as "do not restart blindly" (systemd ``RestartPreventExitStatus=78``).
EX_CONFIG = 78
#: ``ErrorShape.details.code`` of a handshake refused because boot is not done.
STARTUP_SIDECARS = ErrorDetailCode.STARTUP_SIDECARS.value
#: RFC 6455 "Try Again Later": the WebSocket close code that goes with it.
WS_CLOSE_TRY_AGAIN_LATER = 1013
WS_CLOSE_REASON_STARTING = "gateway starting"
#: How long a refused client should wait before its next handshake.
STARTUP_RETRY_AFTER_MS = 1000
#: An in-place restart never comes back faster than this.
MIN_RESTART_EXPECTED_MS = 3000
#: Teardown after the ``shutdown`` frame: close sockets, stop uvicorn, exec.
RESTART_TEARDOWN_MS = 2000

GatewayState = Literal["starting", "ready", "draining", "safe_mode"]
_Phase = Literal["starting", "serving", "draining"]

#: Sidecars the boot sequence registers. Their names surface in ``failing``
#: (``/ready`` while starting) and ``pending`` (``/startupz``).
SIDECAR_CHANNELS = "channels"
SIDECAR_ROUTER = "router"


def drain_budget_ms() -> int:
    """Longest a stop may take: the safe drain plus the forced unwind.

    The same number sizes launchd's ``ExitTimeOut`` and systemd's
    ``TimeoutStopSec`` (plus a margin) in ``agentos gateway install``.
    """
    from agentos.gateway.restart import FORCE_DRAIN_TIMEOUT_S, SAFE_DRAIN_TIMEOUT_S

    return int((SAFE_DRAIN_TIMEOUT_S + FORCE_DRAIN_TIMEOUT_S) * 1000)


def stop_budget_ms() -> int:
    """Worst case from SIGTERM to exit: the drain plus the bounded teardown.

    :func:`drain_budget_ms`, then the WebSocket closing handshakes
    (``WS_CLOSE_TIMEOUT_S``) and uvicorn's own stop (``UVICORN_STOP_TIMEOUT_S``),
    each bounded in :meth:`GatewayServer.close`. A service manager that kills
    sooner cuts a legitimate shutdown short; ``agentos gateway install`` sizes
    launchd's ``ExitTimeOut`` and systemd's ``TimeoutStopSec`` from this.
    """
    from agentos.gateway.boot import UVICORN_STOP_TIMEOUT_S, WS_CLOSE_TIMEOUT_S

    return drain_budget_ms() + int((WS_CLOSE_TIMEOUT_S + UVICORN_STOP_TIMEOUT_S) * 1000)


class GatewayReadiness:
    """Readiness of one gateway process, from boot to the last frame.

    Lives on ``app.state.readiness`` (the WebSocket handshake and the HTTP
    probes read it there) and on :class:`~agentos.gateway.boot.GatewayServer`.
    ``app.state.gateway_ready`` keeps its meaning for the in-place restart,
    which flips it to ``False`` when it stops admitting turns; :meth:`state`
    reads that flag as "draining".
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._created_at = clock()
        self._phase: _Phase = "starting"
        self._pending: dict[str, float] = {}
        self._failed: dict[str, str] = {}
        self._served_at: float | None = None
        self._settled_at: float | None = None
        self._safe_mode: Callable[[], dict[str, Any]] | None = None
        self._active_turns: Callable[[], int] | None = None
        self._failing_channels: Callable[[], list[str]] | None = None
        self._turn_recovery: Callable[[], dict[str, Any]] | None = None

    # ── wiring ──────────────────────────────────────────────────────

    def attach_safe_mode(self, provider: Callable[[], dict[str, Any]]) -> None:
        """``provider()`` returns the breaker snapshot (``{"active": bool, ...}``)."""
        self._safe_mode = provider

    def attach_active_turns(self, provider: Callable[[], int]) -> None:
        self._active_turns = provider

    def attach_failing_channels(self, provider: Callable[[], list[str]]) -> None:
        """``provider()`` names channels that are down (failed start, terminal)."""
        self._failing_channels = provider

    def attach_turn_recovery(self, provider: Callable[[], dict[str, Any]]) -> None:
        """``provider()`` counts the turns restart recovery found, by state."""
        self._turn_recovery = provider

    def turn_recovery(self) -> dict[str, Any] | None:
        if self._turn_recovery is None:
            return None
        try:
            snapshot = self._turn_recovery()
        except Exception:  # noqa: BLE001 - a probe must never fail on recovery
            return None
        return snapshot if isinstance(snapshot, dict) else None

    def failing_channels(self) -> list[str]:
        if self._failing_channels is None:
            return []
        try:
            return sorted(str(name) for name in self._failing_channels())
        except Exception:  # noqa: BLE001 - a probe must never fail on a channel
            return []

    # ── boot sidecars ───────────────────────────────────────────────

    def begin_sidecar(self, name: str) -> None:
        self._pending[name] = self._clock()

    def settle_sidecar(self, name: str, *, error: str | None = None) -> None:
        """Mark a sidecar done; ``error`` records that it settled by failing."""
        self._pending.pop(name, None)
        if error:
            self._failed[name] = error
        if self._phase != "starting" and not self._pending and self._settled_at is None:
            self._settled_at = self._clock()

    # ── phases ──────────────────────────────────────────────────────

    def mark_serving(self) -> None:
        """Boot is done: admit WebSocket handshakes, ``/ready`` may answer 200."""
        if self._phase == "starting":
            self._phase = "serving"
            self._served_at = self._clock()
            if not self._pending and self._settled_at is None:
                self._settled_at = self._served_at

    def begin_drain(self) -> None:
        self._phase = "draining"

    def end_drain(self) -> None:
        """An aborted restart keeps serving."""
        if self._phase == "draining":
            self._phase = "serving"

    # ── reads ───────────────────────────────────────────────────────

    @property
    def phase(self) -> str:
        return self._phase

    def accepting_connections(self) -> bool:
        """``False`` only while booting: the handshake answers STARTUP_SIDECARS."""
        return self._phase != "starting"

    def startup_complete(self) -> bool:
        return self._phase != "starting" and not self._pending

    def pending_sidecars(self) -> list[str]:
        return sorted(self._pending)

    def startup_duration_ms(self) -> int | None:
        if self._served_at is None:
            return None
        return int((self._served_at - self._created_at) * 1000)

    def safe_mode(self) -> dict[str, Any]:
        if self._safe_mode is None:
            return {"active": False}
        try:
            snapshot = self._safe_mode()
        except Exception:  # noqa: BLE001 - a probe must never fail on the breaker
            return {"active": False}
        return snapshot if isinstance(snapshot, dict) else {"active": False}

    def active_turns(self) -> int | None:
        if self._active_turns is None:
            return None
        try:
            return int(self._active_turns())
        except Exception:  # noqa: BLE001
            return None

    def state(self, *, gateway_ready: bool | None = None) -> GatewayState:
        if self._phase == "starting":
            return "starting"
        if self._phase == "draining" or gateway_ready is False:
            return "draining"
        if self.safe_mode().get("active"):
            return "safe_mode"
        return "ready"

    def retry_after_ms(self) -> int:
        return STARTUP_RETRY_AFTER_MS

    def restart_expected_ms(self) -> int:
        """How long until an in-place restart serves again, from this boot's own cost."""
        startup = self.startup_duration_ms() or 0
        return max(MIN_RESTART_EXPECTED_MS, startup + RESTART_TEARDOWN_MS)

    def failing(self, state: GatewayState) -> list[str]:
        """What is wrong right now. Down channels are listed as ``channel:<name>``.

        They do not make the gateway unready: the control plane and the other
        channels serve, and the health sweep keeps restarting them.
        """
        if state == "starting":
            return self.pending_sidecars() or ["startup"]
        if state == "draining":
            return ["draining"]
        channels = [f"channel:{name}" for name in self.failing_channels()]
        if state == "safe_mode":
            return ["safe_mode", *channels]
        return channels

    def snapshot(self, *, gateway_ready: bool | None = None) -> dict[str, Any]:
        """The ``/ready`` payload body (the HTTP layer adds ``uptime_ms``)."""
        state = self.state(gateway_ready=gateway_ready)
        payload: dict[str, Any] = {
            "ready": state in ("ready", "safe_mode"),
            "state": state,
            "failing": self.failing(state),
            "startup": {
                "complete": self.startup_complete(),
                "pending": self.pending_sidecars(),
                "failed": dict(self._failed),
                "duration_ms": self.startup_duration_ms(),
            },
            "safe_mode": self.safe_mode(),
            "drain_budget_ms": drain_budget_ms(),
        }
        recovery = self.turn_recovery()
        if recovery is not None:
            # Informational: interrupted turns never make the gateway unready.
            payload["turn_recovery"] = recovery
        return payload


def readiness_payload(app_state: Any) -> tuple[dict[str, Any], int]:
    """``/ready`` body + status code for an app, with or without a readiness object.

    Apps built without the boot sequence (tests, embedded use) only carry the
    ``gateway_ready`` flag, or nothing at all, and keep the old meaning.
    """
    raw_flag = getattr(app_state, "gateway_ready", None)
    flag = None if raw_flag is None else bool(raw_flag)
    readiness = getattr(app_state, "readiness", None)
    if isinstance(readiness, GatewayReadiness):
        payload = readiness.snapshot(gateway_ready=flag)
    else:
        is_ready = True if flag is None else flag
        state: GatewayState = "ready" if is_ready else "starting"
        payload = {
            "ready": is_ready,
            "state": state,
            "failing": [] if is_ready else ["startup"],
        }
    # ``status`` predates ``state``; existing probes read it.
    payload["status"] = payload["state"]
    return payload, 200 if payload["ready"] else 503


def startup_payload(app_state: Any) -> tuple[dict[str, Any], int]:
    """``/startupz`` body + status code."""
    readiness = getattr(app_state, "readiness", None)
    if isinstance(readiness, GatewayReadiness):
        complete = readiness.startup_complete()
        payload = {
            "started": complete,
            "state": readiness.state(gateway_ready=getattr(app_state, "gateway_ready", None)),
            "pending": readiness.pending_sidecars(),
            "duration_ms": readiness.startup_duration_ms(),
        }
    else:
        flag = getattr(app_state, "gateway_ready", None)
        complete = True if flag is None else bool(flag)
        payload = {"started": complete, "pending": [] if complete else ["startup"]}
    return payload, 200 if complete else 503


def readiness_from_scope(scope: Any) -> GatewayReadiness | None:
    """The readiness object of the app serving an ASGI scope, if it has one."""
    app = scope.get("app") if isinstance(scope, dict) else None
    readiness = getattr(getattr(app, "state", None), "readiness", None)
    return readiness if isinstance(readiness, GatewayReadiness) else None


# ── process holder, for RPC handlers that have no app in reach ───────

_current: GatewayReadiness | None = None


def set_gateway_readiness(readiness: GatewayReadiness | None) -> None:
    global _current
    _current = readiness


def get_gateway_readiness() -> GatewayReadiness | None:
    return _current


__all__ = [
    "EX_CONFIG",
    "MIN_RESTART_EXPECTED_MS",
    "SIDECAR_CHANNELS",
    "SIDECAR_ROUTER",
    "STARTUP_RETRY_AFTER_MS",
    "STARTUP_SIDECARS",
    "WS_CLOSE_REASON_STARTING",
    "WS_CLOSE_TRY_AGAIN_LATER",
    "GatewayReadiness",
    "GatewayState",
    "drain_budget_ms",
    "stop_budget_ms",
    "get_gateway_readiness",
    "readiness_from_scope",
    "readiness_payload",
    "set_gateway_readiness",
    "startup_payload",
]
