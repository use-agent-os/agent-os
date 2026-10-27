"""Restart the gateway in place: from chat, the web UI, the agent's tool or the CLI.

Why in place
------------
"Exit cleanly and let the supervisor bring me back" only works under launchd.
The desktop app never respawns the child it spawned, a systemd unit with
``Restart=on-failure`` ignores exit 0 and SIGTERM, and ``agentos gateway start``
or a terminal have no supervisor at all. Re-executing the same PID keeps every
launcher's bookkeeping valid: the desktop keeps its child handle and log
pipes, launchd and systemd see no exit, the CLI pidfile still names a live
process. Upgraded code on disk is picked up because the new image imports it
fresh. Windows cannot replace a process image, so the restart is refused
there with a pointer to ``agentos gateway restart``.

The sequence
------------
``request()`` returns at once so the caller can acknowledge (the channel ack
is sent by the dispatch loop *after* the RPC returns; Hermes learned the hard
way that restarting inside the handler kills the process before the ack
leaves). A background task then:

1. waits a short grace so the ack and the WebSocket response frame leave;
2. persists the request to ``state/gateway_restart.json`` so the next boot
   can confirm back to the chat that asked;
3. stops admitting new turns (``/ready`` reports 503 from here on);
4. runs :meth:`GatewayServer.close` — drain or cancel the turns in flight,
   stop channels, close WebSockets, stop uvicorn, close services;
5. records the exec plan: ``python -m agentos.cli.main <same args>`` with a
   copy of the environment plus a re-materialized desktop operator secret.

The exec itself happens in ``run_gateway`` *after* ``asyncio.run`` returns:
``close()`` ends the uvicorn task the main coroutine is waiting on, and once
that coroutine returns the loop tears down every other task, so an exec from
inside the loop would race its own teardown. ``run_gateway`` awaits
:attr:`GatewayRestarter.task` first, then calls :meth:`exec_if_pending`.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

import structlog

from agentos import __version__
from agentos.asyncio_utils import create_background_task
from agentos.gateway.agent_surface import OPERATOR_SECRET_FILE_ENV, get_agent_surface
from agentos.paths import default_agentos_home

log = structlog.get_logger(__name__)

RestartMode = Literal["safe", "force"]
RESTART_MODES: tuple[str, ...] = ("safe", "force")

#: Safe mode: let the turns in flight finish, as ``close()`` always did.
SAFE_DRAIN_TIMEOUT_S = 30.0
#: Force mode: cancel them and give the cancellation this long to unwind.
FORCE_DRAIN_TIMEOUT_S = 2.0
#: Let the slash-command ack and the RPC response frame leave before the
#: channel adapters and WebSockets are torn down under them.
ACK_GRACE_S = 1.5
#: ``close()`` has no deadline of its own; a stuck adapter or MCP client must
#: not hold the restart forever.
CLOSE_SLACK_S = 60.0
#: A request file older than this at boot is dropped, not announced.
STALE_REQUEST_S = 600.0
REQUEST_FILE = "gateway_restart.json"
REQUEST_VERSION = 1
_ANNOUNCE_ATTEMPTS = 3
_ANNOUNCE_BACKOFF_S = 2.0
_ANNOUNCE_SEND_TIMEOUT_S = 30.0


class RestartPendingError(RuntimeError):
    """A restart is already in progress."""


class RestartUnsupportedError(RuntimeError):
    """In-place restart is not possible on this platform."""


@dataclass(frozen=True)
class RestartOrigin:
    """Where the restart was asked from, so the new image can confirm there."""

    kind: str = "none"  # "channel" | "web" | "none"
    session_key: str | None = None
    channel_name: str | None = None
    channel_type: str | None = None
    channel_id: str | None = None
    thread_id: str | None = None
    account_id: str | None = None
    sender_id: str | None = None
    conn_id: str | None = None

    @classmethod
    def from_envelope(cls, envelope: Any) -> RestartOrigin:
        """The channel chat a ``RouteEnvelope`` came from."""
        return cls(
            kind="channel",
            session_key=getattr(envelope, "session_key", None),
            channel_name=getattr(envelope, "channel_name", None),
            channel_type=getattr(envelope, "channel_type", None),
            channel_id=getattr(envelope, "channel_id", None),
            thread_id=getattr(envelope, "thread_id", None),
            account_id=getattr(envelope, "account_id", None),
            sender_id=getattr(envelope, "sender_id", None),
        )

    @classmethod
    def from_tool_context(cls, ctx: Any) -> RestartOrigin:
        """Best effort from a ``ToolContext``: the turn's chat, no thread."""
        if ctx is None:
            return cls()
        channel_kind = getattr(ctx, "channel_kind", None)
        channel_id = getattr(ctx, "channel_id", None)
        session_key = getattr(ctx, "session_key", None)
        source_kind = str(getattr(ctx, "source_kind", "") or "")
        if channel_kind == "webchat" or source_kind in {"webui", "web"}:
            return cls.web(session_key, None)
        if channel_kind and channel_id and source_kind == "channel":
            return cls(
                kind="channel",
                session_key=session_key,
                channel_name=channel_kind,
                channel_id=channel_id,
                sender_id=getattr(ctx, "sender_id", None),
            )
        if session_key:
            return cls.web(session_key, None)
        return cls()

    @classmethod
    def web(cls, session_key: str | None, conn_id: str | None) -> RestartOrigin:
        if not session_key:
            return cls(kind="none", conn_id=conn_id)
        return cls(kind="web", session_key=session_key, conn_id=conn_id)

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "sessionKey": self.session_key,
            "channelName": self.channel_name,
            "channelType": self.channel_type,
            "channelId": self.channel_id,
            "threadId": self.thread_id,
            "accountId": self.account_id,
            "senderId": self.sender_id,
            "connId": self.conn_id,
        }

    @classmethod
    def from_json(cls, data: Any) -> RestartOrigin:
        if not isinstance(data, dict):
            return cls()

        def _s(key: str) -> str | None:
            value = data.get(key)
            return str(value) if isinstance(value, str) and value else None

        kind = _s("kind") or "none"
        if kind not in {"channel", "web", "none"}:
            kind = "none"
        return cls(
            kind=kind,
            session_key=_s("sessionKey"),
            channel_name=_s("channelName"),
            channel_type=_s("channelType"),
            channel_id=_s("channelId"),
            thread_id=_s("threadId"),
            account_id=_s("accountId"),
            sender_id=_s("senderId"),
            conn_id=_s("connId"),
        )


@dataclass
class RestartRequest:
    restart_id: str
    reason: str
    mode: str
    origin: RestartOrigin = field(default_factory=RestartOrigin)
    requested_at: float = field(default_factory=time.time)
    pid: int = field(default_factory=os.getpid)
    from_version: str = __version__
    exec_at: float | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "version": REQUEST_VERSION,
            "restartId": self.restart_id,
            "reason": self.reason,
            "mode": self.mode,
            "requestedAt": self.requested_at,
            "execAt": self.exec_at,
            "pid": self.pid,
            "fromVersion": self.from_version,
            "origin": self.origin.to_json(),
        }

    @classmethod
    def from_json(cls, data: Any) -> RestartRequest | None:
        if not isinstance(data, dict) or not isinstance(data.get("restartId"), str):
            return None
        try:
            requested_at = float(data.get("requestedAt") or 0.0)
            exec_at_raw = data.get("execAt")
            exec_at = float(exec_at_raw) if exec_at_raw is not None else None
            pid = int(data.get("pid") or 0)
        except (TypeError, ValueError):
            return None
        return cls(
            restart_id=data["restartId"],
            reason=str(data.get("reason") or ""),
            mode=str(data.get("mode") or "safe"),
            origin=RestartOrigin.from_json(data.get("origin")),
            requested_at=requested_at,
            pid=pid,
            from_version=str(data.get("fromVersion") or ""),
            exec_at=exec_at,
        )


def drain_timeout_for(mode: str) -> float:
    return FORCE_DRAIN_TIMEOUT_S if mode == "force" else SAFE_DRAIN_TIMEOUT_S


def normalize_mode(value: Any) -> str:
    """``"safe"`` (default) or ``"force"``; anything else is a ``ValueError``."""
    if value is None or value == "":
        return "safe"
    mode = str(value).strip().lower()
    if mode not in RESTART_MODES:
        raise ValueError(f"mode must be one of {', '.join(RESTART_MODES)}")
    return mode


def default_reexec_argv() -> list[str]:
    """The same gateway, the same flags, through the interpreter that runs now.

    Matches the shape ``gateway_lifecycle`` spawns, so an operator's
    ``--listen``/``--port``/``--config`` survive the restart verbatim.
    """
    return [sys.executable, "-m", "agentos.cli.main", *sys.argv[1:]]


def restart_request_path(config: Any) -> Path:
    """``<state dir>/gateway_restart.json`` — the same root as the pid lock."""
    state_root = Path(getattr(config, "state_dir", None) or default_agentos_home() / "state")
    return state_root / REQUEST_FILE


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)


def in_place_restart_supported() -> bool:
    return os.name != "nt"


class GatewayRestarter:
    """One per gateway process; see the module docstring for the sequence."""

    def __init__(
        self,
        server: Any,
        config: Any,
        *,
        pid_lock: Any = None,
        exec_fn: Callable[[str, list[str], dict[str, str]], Any] | None = None,
        argv_factory: Callable[[], list[str]] = default_reexec_argv,
        task_factory: Callable[[Any], Any] = create_background_task,
        agent_surface: Any = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
        supported: bool | None = None,
    ) -> None:
        self._server = server
        self._config = config
        self._pid_lock = pid_lock if pid_lock is not None else getattr(server, "_pid_lock", None)
        self._exec_fn = exec_fn or os.execve
        self._argv_factory = argv_factory
        self._task_factory = task_factory
        self._agent_surface = agent_surface
        self._sleep = sleep
        self._supported = in_place_restart_supported() if supported is None else supported
        self._request: RestartRequest | None = None
        self._task: Any = None
        self._exec_plan: tuple[list[str], dict[str, str]] | None = None
        self._fatal = False
        # Set when the process was told to stop (SIGTERM / SIGINT) while a
        # restart was pending: finish closing, but do not exec a new image.
        self._exec_cancelled: str | None = None

    # ── introspection ────────────────────────────────────────────────

    @property
    def supported(self) -> bool:
        return self._supported

    @property
    def pending(self) -> RestartRequest | None:
        return self._request

    @property
    def task(self) -> Any:
        """The background restart task, awaited by ``run_gateway`` before exec."""
        return self._task

    @property
    def exec_plan(self) -> tuple[list[str], dict[str, str]] | None:
        return self._exec_plan

    def request_path(self) -> Path:
        return restart_request_path(self._config)

    def cancel_exec(self, reason: str) -> None:
        """Stop a pending restart from exec'ing: the process is shutting down.

        A supervisor that sends SIGTERM during a restart's drain wants the
        gateway gone; exec'ing a fresh image would leave an orphan it no
        longer tracks. No-op when no restart is pending.
        """
        if self._request is None or self._exec_cancelled is not None:
            return
        self._exec_cancelled = reason
        self._exec_plan = None
        log.warning("gateway.restart_cancelled", restart_id=self._request.restart_id, reason=reason)

    # ── entry point ──────────────────────────────────────────────────

    def request(
        self,
        *,
        reason: str,
        mode: str = "safe",
        origin: RestartOrigin | None = None,
    ) -> RestartRequest:
        """Schedule the restart and return at once.

        Raises :class:`RestartUnsupportedError` on Windows and
        :class:`RestartPendingError` when one is already underway; the
        single instance on the loop thread is the double-restart guard.
        """
        if not self._supported:
            raise RestartUnsupportedError(
                "In-place restart is not supported on Windows; run 'agentos gateway restart'."
            )
        if self._request is not None:
            raise RestartPendingError(f"restart {self._request.restart_id} is already in progress")
        req = RestartRequest(
            restart_id=uuid.uuid4().hex,
            reason=reason,
            mode=normalize_mode(mode),
            origin=origin or RestartOrigin(),
        )
        self._request = req
        log.info(
            "gateway.restart_requested",
            restart_id=req.restart_id,
            mode=req.mode,
            reason=req.reason,
            origin=req.origin.kind,
        )
        self._task = self._task_factory(self._run())
        return req

    # ── the background sequence ──────────────────────────────────────

    def _task_runtime(self) -> Any:
        services = getattr(self._server, "_services", None)
        return getattr(services, "task_runtime", None)

    def restart_expected_ms(self) -> int:
        """When clients should expect this gateway back, for the ``shutdown`` event.

        Derived from how long this image took to boot (``GatewayReadiness``);
        a server without one gets the contract's floor.
        """
        readiness = getattr(self._server, "readiness", None)
        estimate = getattr(readiness, "restart_expected_ms", None)
        if callable(estimate):
            try:
                return int(estimate())
            except Exception:  # noqa: BLE001
                pass
        from agentos.gateway.readiness import MIN_RESTART_EXPECTED_MS

        return MIN_RESTART_EXPECTED_MS

    def _set_ready(self, ready: bool) -> None:
        app = getattr(self._server, "app", None)
        state = getattr(app, "state", None)
        if state is not None:
            try:
                state.gateway_ready = ready
            except Exception:
                pass

    def _write_request(self, req: RestartRequest) -> None:
        _write_atomic(self.request_path(), req.to_json())

    def _remove_request_file(self) -> None:
        try:
            self.request_path().unlink(missing_ok=True)
        except OSError:
            pass

    def _abort_before_close(self, req: RestartRequest) -> None:
        """Undo the preparation: nothing has been closed yet, keep serving."""
        self._remove_request_file()
        runtime = self._task_runtime()
        resume = getattr(runtime, "resume_admission", None)
        if callable(resume):
            resume()
        self._set_ready(True)
        self._request = None
        self._task = None
        log.warning("gateway.restart_aborted", restart_id=req.restart_id)

    async def _run(self) -> None:
        req = self._request
        if req is None:
            return
        try:
            await self._sleep(ACK_GRACE_S)
            self._write_request(req)
            self._set_ready(False)
            runtime = self._task_runtime()
            stop = getattr(runtime, "stop_admission", None)
            if callable(stop):
                stop()
        except Exception:
            log.exception("gateway.restart_prepare_failed", restart_id=req.restart_id)
            self._abort_before_close(req)
            return

        timeout = drain_timeout_for(req.mode)
        log.info(
            "gateway.restart_draining",
            restart_id=req.restart_id,
            mode=req.mode,
            drain_timeout_s=timeout,
        )
        try:
            await asyncio.wait_for(
                self._server.close(
                    reason=f"restart:{req.mode}",
                    drain_timeout=timeout,
                    cancel_in_flight=req.mode == "force",
                    restart_expected_ms=self.restart_expected_ms(),
                ),
                timeout=timeout + CLOSE_SLACK_S,
            )
        except TimeoutError:
            log.warning("gateway.restart_close_timeout", restart_id=req.restart_id)
        except Exception:
            log.exception("gateway.restart_close_failed", restart_id=req.restart_id)

        if self._exec_cancelled is not None:
            # Told to stop while draining: the services are closed, exit
            # normally instead of coming back.
            self._remove_request_file()
            return

        # From here there is no way back: services are closed. A failure now
        # ends the process with exit 1 so an on-failure supervisor respawns it.
        try:
            argv = list(self._argv_factory())
            env = dict(os.environ)
            secret_path = self._export_operator_secret()
            if secret_path is not None:
                env[OPERATOR_SECRET_FILE_ENV] = str(secret_path)
            self._exec_plan = (argv, env)
            req.exec_at = time.time()
            self._write_request(req)
        except Exception:
            log.exception("gateway.restart_plan_failed", restart_id=req.restart_id)
            self._remove_request_file()
            self._fatal = True

    def _export_operator_secret(self) -> Path | None:
        surface = self._agent_surface if self._agent_surface is not None else get_agent_surface()
        export = getattr(surface, "export_operator_secret_file", None)
        if not callable(export):
            return None
        return cast(Path | None, export())

    # ── after the loop ───────────────────────────────────────────────

    def exec_if_pending(self) -> None:
        """Replace this process with the planned image, if a restart got that far.

        Called by ``run_gateway`` after ``asyncio.run`` returns. Releases the
        pid lock first (``atexit`` does not run across ``execve``; the fd is
        non-inheritable so the lock would drop anyway, but the new image
        would log a stale-pidfile warning), flushes stdio so the log pipes
        the desktop holds see the boundary, then execs. On ``OSError`` the
        process exits 1: an on-failure supervisor respawns it and the desktop
        shows the stderr tail.
        """
        if self._fatal:
            raise SystemExit(1)
        plan = self._exec_plan
        if plan is None or self._exec_cancelled is not None:
            return
        argv, env = plan
        req = self._request
        if self._pid_lock is not None:
            try:
                self._pid_lock.release()
            except Exception:
                log.warning("gateway.restart_pidlock_release_failed", exc_info=True)
        log.info(
            "gateway.restart_exec",
            restart_id=req.restart_id if req else None,
            argv=argv,
        )
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:
                pass
        try:
            self._exec_fn(argv[0], argv, env)
        except OSError as exc:
            print(f"ERROR: gateway restart could not exec {argv[0]}: {exc}", file=sys.stderr)
            self._remove_request_file()
            raise SystemExit(1) from exc


# ── module holder, like tools.builtin.control ────────────────────────

_restarter: GatewayRestarter | None = None


def set_restarter(restarter: GatewayRestarter | None) -> None:
    """Install the process-wide restarter (gateway boot) or clear it (tests)."""
    global _restarter
    _restarter = restarter


def get_restarter() -> GatewayRestarter | None:
    return _restarter


# ── after boot: confirm back to the chat that asked ─────────────────


def peek_request(config: Any) -> RestartRequest | None:
    """The request the previous image left behind, left in place (or ``None``).

    For boot code that must know who asked for the restart before
    :func:`read_and_clear_request` consumes it (restart recovery does not
    resume the turn that asked). Never raises.
    """
    try:
        raw = restart_request_path(config).read_text(encoding="utf-8")
        return RestartRequest.from_json(json.loads(raw))
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def read_and_clear_request(config: Any, *, now: float | None = None) -> RestartRequest | None:
    """The request the previous image left behind, or ``None``.

    The file is removed before anything else happens so a crash loop never
    announces twice. Malformed or stale (older than :data:`STALE_REQUEST_S`)
    requests are dropped with a log line.
    """
    path = restart_request_path(config)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning("gateway.restart_request_unreadable", path=str(path), error=str(exc))
        return None
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("gateway.restart_request_malformed", path=str(path))
        return None
    req = RestartRequest.from_json(data)
    if req is None:
        log.warning("gateway.restart_request_malformed", path=str(path))
        return None
    current = time.time() if now is None else now
    stamp = req.exec_at if req.exec_at is not None else req.requested_at
    if current - stamp > STALE_REQUEST_S:
        log.warning(
            "gateway.restart_announce_stale",
            restart_id=req.restart_id,
            age_s=round(current - stamp),
        )
        return None
    return req


def restart_completion_text(req: RestartRequest, *, now: float | None = None) -> str:
    current = time.time() if now is None else now
    elapsed = max(0.0, current - req.requested_at)
    text = f"Gateway restarted (v{__version__}, {elapsed:.0f}s)."
    if req.from_version and req.from_version != __version__:
        text += f" Version changed from {req.from_version}."
    return text


async def announce_restart_completion(
    config: Any,
    *,
    channel_manager: Any,
    session_manager: Any,
    now: float | None = None,
) -> bool:
    """Post "Gateway restarted" where the restart was asked. Never raises."""
    try:
        req = read_and_clear_request(config, now=now)
    except Exception:
        log.exception("gateway.restart_announce_read_failed")
        return False
    if req is None:
        return False
    text = restart_completion_text(req, now=now)
    log.info(
        "gateway.restart_completed",
        restart_id=req.restart_id,
        mode=req.mode,
        reason=req.reason,
        origin=req.origin.kind,
    )
    try:
        if req.origin.kind == "channel":
            return await _announce_to_channel(channel_manager, req.origin, text)
        if req.origin.kind == "web":
            return await _announce_to_web(session_manager, req.origin, text)
    except Exception:
        log.exception("gateway.restart_announce_failed", restart_id=req.restart_id)
    return False


async def _announce_to_channel(channel_manager: Any, origin: RestartOrigin, text: str) -> bool:
    from agentos.channels.addressing import build_outgoing_for_target

    if channel_manager is None or not origin.channel_name:
        return False
    last_error: str | None = None
    for attempt in range(1, _ANNOUNCE_ATTEMPTS + 1):
        resolved = channel_manager.resolve_delivery_target(
            target=origin.channel_name,
            to=origin.channel_id or "",
            account_id=origin.account_id or "",
            thread_id=origin.thread_id or "",
        )
        adapter = getattr(resolved, "adapter", None) if getattr(resolved, "ok", False) else None
        if adapter is None:
            last_error = getattr(resolved, "reason", None) or "adapter_not_found"
        else:
            message = build_outgoing_for_target(
                channel_type=resolved.channel_type,
                to=resolved.to,
                thread_id=resolved.thread_id,
                text=text,
            )
            try:
                await asyncio.wait_for(adapter.send(message), timeout=_ANNOUNCE_SEND_TIMEOUT_S)
                log.info("gateway.restart_announced", channel=origin.channel_name)
                return True
            except Exception as exc:
                last_error = str(exc) or exc.__class__.__name__
        if attempt < _ANNOUNCE_ATTEMPTS:
            await asyncio.sleep(_ANNOUNCE_BACKOFF_S * attempt)
    log.warning(
        "gateway.restart_announce_channel_failed",
        channel=origin.channel_name,
        error=last_error,
    )
    return False


async def _announce_to_web(session_manager: Any, origin: RestartOrigin, text: str) -> bool:
    if session_manager is None or not origin.session_key:
        return False
    if await session_manager.get_session(origin.session_key) is None:
        log.info("gateway.restart_announce_session_gone", session_key=origin.session_key)
        return False
    await session_manager.append_message(
        origin.session_key,
        role="assistant",
        content=text,
        provenance={"kind": "system", "source": "gateway.restart"},
    )
    log.info("gateway.restart_announced", session_key=origin.session_key)
    return True


__all__ = [
    "ACK_GRACE_S",
    "FORCE_DRAIN_TIMEOUT_S",
    "RESTART_MODES",
    "SAFE_DRAIN_TIMEOUT_S",
    "STALE_REQUEST_S",
    "GatewayRestarter",
    "RestartOrigin",
    "RestartPendingError",
    "RestartRequest",
    "RestartUnsupportedError",
    "announce_restart_completion",
    "default_reexec_argv",
    "drain_timeout_for",
    "get_restarter",
    "in_place_restart_supported",
    "normalize_mode",
    "peek_request",
    "read_and_clear_request",
    "restart_completion_text",
    "restart_request_path",
    "set_restarter",
]
