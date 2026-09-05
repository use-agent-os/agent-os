"""Persistent CDP supervisor for the browser tool.

Ported from NousResearch/hermes-agent ``tools/browser_supervisor.py`` (MIT,
Copyright (c) 2025 Nous Research) — see ``THIRD_PARTY_NOTICES.md``.

One supervisor per ``(session_key, cdp_url)`` holds a live CDP connection so the
tool can do three things the one-shot CLI can't observe cheaply:

* **dialog interception** — capture native ``alert``/``confirm``/``prompt``/
  ``beforeunload`` dialogs, surface them as ``pending_dialogs`` in a snapshot,
  and respond via :meth:`CDPSupervisor.respond_to_dialog`. A configurable policy
  (``must_respond`` / ``auto_dismiss`` / ``auto_accept``) with a watchdog decides
  what happens to an unanswered dialog.
* **console capture** — recent ``console.*`` messages folded into the snapshot.
* **evaluate_runtime** — ``Runtime.evaluate`` over the already-open connection.
  The ``eval`` *action* deliberately does not use it: a browser exposes several
  CDP targets and the supervisor cannot reliably tell which one the engine
  considers active, so eval goes through ``agent-browser eval``, which owns that
  choice. Kept for callers that hold a specific page session.

The CDP **transport** (a background thread running an asyncio WebSocket loop) is
separated from the supervisor's **decision logic** behind the :class:`Transport`
protocol, so the state machine here is exercised by real tests against a fake
transport — no Chromium required. The live transport is
:class:`_WebSocketTransport`.
"""

from __future__ import annotations

import contextlib
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog

from agentos.redact import redact_cdp_url

log = structlog.get_logger(__name__)

DIALOG_POLICY_MUST_RESPOND = "must_respond"
DIALOG_POLICY_AUTO_DISMISS = "auto_dismiss"
DIALOG_POLICY_AUTO_ACCEPT = "auto_accept"
VALID_DIALOG_POLICIES = frozenset(
    {DIALOG_POLICY_MUST_RESPOND, DIALOG_POLICY_AUTO_DISMISS, DIALOG_POLICY_AUTO_ACCEPT}
)
DEFAULT_DIALOG_POLICY = DIALOG_POLICY_MUST_RESPOND
DEFAULT_DIALOG_TIMEOUT_S = 300.0

#: How long an attach handshake may take before the transport gives up. Shared
#: with the tool layer so its harness timeout can budget for this leg.
SUPERVISOR_START_TIMEOUT = 15.0
#: How long to wait for a page target once connected. A browser target alone is
#: enough to talk CDP, but dialogs are only delivered from a page session.
PAGE_ATTACH_TIMEOUT = 8.0

CONSOLE_HISTORY_MAX = 50
RECENT_DIALOGS_MAX = 20


def parse_dialog_policy(policy: Any, timeout: Any) -> tuple[str, float]:
    """Normalize a configured ``(policy, timeout_s)`` pair, applying defaults."""
    normalized = str(policy or DEFAULT_DIALOG_POLICY)
    if normalized not in VALID_DIALOG_POLICIES:
        normalized = DEFAULT_DIALOG_POLICY
    try:
        timeout_s = float(timeout) if timeout is not None else DEFAULT_DIALOG_TIMEOUT_S
        if timeout_s <= 0:
            timeout_s = DEFAULT_DIALOG_TIMEOUT_S
    except (TypeError, ValueError):
        timeout_s = DEFAULT_DIALOG_TIMEOUT_S
    return normalized, timeout_s


# ---------------------------------------------------------------------------
# State records
# ---------------------------------------------------------------------------


@dataclass
class PendingDialog:
    dialog_id: str
    dialog_type: str  # alert | confirm | prompt | beforeunload
    message: str
    default_prompt: str = ""
    session_id: str | None = None  # CDP page session the dialog opened on
    opened_at: float = field(default_factory=time.time)


@dataclass
class DialogRecord:
    dialog_id: str
    dialog_type: str
    message: str
    action: str  # accept | dismiss | auto_dismiss | auto_accept
    prompt_text: str = ""
    resolved_at: float = field(default_factory=time.time)


@dataclass
class ConsoleEvent:
    level: str
    text: str
    at: float = field(default_factory=time.time)


@dataclass
class SupervisorSnapshot:
    pending_dialogs: list[dict[str, Any]] = field(default_factory=list)
    recent_dialogs: list[dict[str, Any]] = field(default_factory=list)
    console: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.pending_dialogs:
            payload["pending_dialogs"] = self.pending_dialogs
        if self.recent_dialogs:
            payload["recent_dialogs"] = self.recent_dialogs
        if self.console:
            payload["console"] = self.console
        return payload


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


class Transport(Protocol):
    """A live CDP connection. ``call`` issues a method; events go to a handler."""

    def start(self, on_event: Any) -> None:
        """Connect and begin delivering ``(method, params, session_id)`` to *on_event*."""

    def call(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Issue a CDP method (optionally on a page session) and return its result."""

    def stop(self) -> None:
        """Close the connection and release resources."""


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------


class CDPSupervisor:
    """One supervisor per ``(session_key, cdp_url)``.

    The decision logic (dialog capture/response, console history, eval framing)
    lives here and is synchronous + thread-safe; the transport does the I/O.
    """

    def __init__(
        self,
        session_key: str,
        cdp_url: str,
        *,
        dialog_policy: str = DEFAULT_DIALOG_POLICY,
        dialog_timeout_s: float = DEFAULT_DIALOG_TIMEOUT_S,
        transport: Transport | None = None,
    ) -> None:
        if dialog_policy not in VALID_DIALOG_POLICIES:
            raise ValueError(f"Invalid dialog_policy {dialog_policy!r}")
        self.session_key = session_key
        self.cdp_url = cdp_url
        self.dialog_policy = dialog_policy
        self.dialog_timeout_s = float(dialog_timeout_s)

        self._lock = threading.Lock()
        self._pending: dict[str, PendingDialog] = {}
        self._recent: list[DialogRecord] = []
        self._console: list[ConsoleEvent] = []
        #: Per-dialog auto-dismiss timers for the ``must_respond`` policy.
        self._watchdogs: dict[str, threading.Timer] = {}
        self._dialog_seq = 0
        self._active = False
        self._transport: Transport = transport or _WebSocketTransport(cdp_url)

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        # The transport connects, attaches to the page target, and enables the
        # Page/Runtime domains on it before returning — so by the time start()
        # completes, dialog events flow and eval is scoped to the page.
        self._transport.start(self._on_event)
        with self._lock:
            self._active = True

    def stop(self) -> None:
        with self._lock:
            self._active = False
            timers = list(self._watchdogs.values())
            self._watchdogs.clear()
        for timer in timers:
            timer.cancel()
        try:
            self._transport.stop()
        except Exception:  # noqa: BLE001 - teardown is best-effort
            log.debug("browser_supervisor.stop_failed", exc_info=True)

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    # ── event ingestion ──────────────────────────────────────────────────────

    def _on_event(self, method: str, params: dict[str, Any], session_id: str | None = None) -> None:
        if method == "Page.javascriptDialogOpening":
            self._on_dialog_opening(params, session_id)
        elif method == "Runtime.consoleAPICalled":
            self._on_console(params)

    def _on_dialog_opening(self, params: dict[str, Any], session_id: str | None) -> None:
        with self._lock:
            self._dialog_seq += 1
            dialog_id = f"d{self._dialog_seq}"
            dialog = PendingDialog(
                dialog_id=dialog_id,
                dialog_type=str(params.get("type") or "alert"),
                message=str(params.get("message") or ""),
                default_prompt=str(params.get("defaultPrompt") or ""),
                session_id=session_id,
            )
            self._pending[dialog_id] = dialog
            policy = self.dialog_policy

        if policy == DIALOG_POLICY_AUTO_DISMISS:
            self._resolve_dialog(dialog_id, accept=False, prompt_text="", action="auto_dismiss")
        elif policy == DIALOG_POLICY_AUTO_ACCEPT:
            self._resolve_dialog(dialog_id, accept=True, prompt_text="", action="auto_accept")
        else:
            # must_respond: wait for respond_to_dialog, but not forever. A
            # JavaScript dialog blocks the page's whole event loop, so a turn
            # that never answers would wedge the browser for good; the watchdog
            # dismisses it once the timeout passes.
            self._arm_dialog_watchdog(dialog_id)

    def _arm_dialog_watchdog(self, dialog_id: str) -> None:
        timer = threading.Timer(
            self.dialog_timeout_s,
            self._expire_dialog,
            args=(dialog_id,),
        )
        timer.daemon = True
        with self._lock:
            self._watchdogs[dialog_id] = timer
        timer.start()

    def _cancel_dialog_watchdog(self, dialog_id: str) -> None:
        with self._lock:
            timer = self._watchdogs.pop(dialog_id, None)
        if timer is not None:
            timer.cancel()

    def _expire_dialog(self, dialog_id: str) -> None:
        with self._lock:
            still_open = dialog_id in self._pending
        if not still_open:
            return
        log.warning(
            "browser_supervisor.dialog_timed_out",
            session_key=self.session_key,
            dialog_id=dialog_id,
            timeout_s=self.dialog_timeout_s,
        )
        self._resolve_dialog(dialog_id, accept=False, prompt_text="", action="timed_out")

    def _on_console(self, params: dict[str, Any]) -> None:
        text_parts: list[str] = []
        for arg in params.get("args", []) or []:
            value = arg.get("value")
            if value is not None:
                text_parts.append(str(value))
            elif arg.get("description"):
                text_parts.append(str(arg["description"]))
        event = ConsoleEvent(level=str(params.get("type") or "log"), text=" ".join(text_parts))
        with self._lock:
            self._console.append(event)
            if len(self._console) > CONSOLE_HISTORY_MAX:
                self._console = self._console[-CONSOLE_HISTORY_MAX:]

    # ── snapshot ─────────────────────────────────────────────────────────────

    def snapshot(self) -> SupervisorSnapshot:
        with self._lock:
            pending = [
                {
                    "id": d.dialog_id,
                    "type": d.dialog_type,
                    "message": d.message,
                    "default_prompt": d.default_prompt,
                }
                for d in self._pending.values()
            ]
            recent = [
                {
                    "id": r.dialog_id,
                    "type": r.dialog_type,
                    "action": r.action,
                }
                for r in self._recent[-RECENT_DIALOGS_MAX:]
            ]
            console = [{"level": c.level, "text": c.text} for c in self._console]
        return SupervisorSnapshot(pending_dialogs=pending, recent_dialogs=recent, console=console)

    # ── dialog response ──────────────────────────────────────────────────────

    def respond_to_dialog(
        self,
        action: str,
        *,
        prompt_text: str | None = None,
        dialog_id: str | None = None,
    ) -> dict[str, Any]:
        if action not in {"accept", "dismiss"}:
            return {"ok": False, "error": "action must be 'accept' or 'dismiss'"}
        with self._lock:
            if not self._pending:
                return {"ok": False, "error": "no dialog is currently open"}
            if dialog_id is None:
                if len(self._pending) > 1:
                    return {
                        "ok": False,
                        "error": "multiple dialogs open; pass dialog_id from the snapshot",
                    }
                dialog_id = next(iter(self._pending))
            elif dialog_id not in self._pending:
                return {"ok": False, "error": f"no pending dialog with id {dialog_id!r}"}
        return self._resolve_dialog(
            dialog_id,
            accept=action == "accept",
            prompt_text=prompt_text or "",
            action=action,
        )

    def _resolve_dialog(
        self,
        dialog_id: str,
        *,
        accept: bool,
        prompt_text: str,
        action: str,
    ) -> dict[str, Any]:
        with self._lock:
            dialog = self._pending.pop(dialog_id, None)
        if dialog is None:
            return {"ok": False, "error": f"dialog {dialog_id!r} is no longer open"}
        self._cancel_dialog_watchdog(dialog_id)

        cdp_params: dict[str, Any] = {"accept": accept}
        if dialog.dialog_type == "prompt" and accept:
            cdp_params["promptText"] = prompt_text
        try:
            self._transport.call(
                "Page.handleJavaScriptDialog", cdp_params, 10.0, session_id=dialog.session_id
            )
        except Exception as exc:  # noqa: BLE001 - surface the transport error
            with self._lock:
                # Put it back so a later attempt can retry.
                self._pending[dialog_id] = dialog
            # Re-arm the watchdog we cancelled above. Without this, a transient
            # CDP failure while answering leaves the dialog pending forever —
            # and since the engine runs with --no-auto-dialog, nothing else will
            # ever dismiss it, so the page's event loop stays blocked and every
            # later command on the session times out.
            if self.dialog_policy == DIALOG_POLICY_MUST_RESPOND:
                self._arm_dialog_watchdog(dialog_id)
            return {"ok": False, "error": f"failed to resolve dialog: {exc}"}

        record = DialogRecord(
            dialog_id=dialog.dialog_id,
            dialog_type=dialog.dialog_type,
            message=dialog.message,
            action=action,
            prompt_text=prompt_text,
        )
        with self._lock:
            self._recent.append(record)
            if len(self._recent) > RECENT_DIALOGS_MAX:
                self._recent = self._recent[-RECENT_DIALOGS_MAX:]
        return {"ok": True, "dialog": {"id": dialog.dialog_id, "type": dialog.dialog_type}}

    # ── eval fast path ───────────────────────────────────────────────────────

    def evaluate_runtime(self, expression: str, timeout: float = 30.0) -> dict[str, Any]:
        """Run ``Runtime.evaluate`` over the live connection.

        Returns ``{"ok": True, "result": <value>}`` or ``{"ok": False, "error": …}``.
        A JS-side exception is a real failure; a transport-side failure is
        signalled so the caller can fall back to the subprocess path.
        """
        try:
            reply = self._transport.call(
                "Runtime.evaluate",
                {"expression": expression, "returnByValue": True, "awaitPromise": True},
                timeout,
            )
        except Exception as exc:  # noqa: BLE001 - transport-side, caller may fall back
            return {"ok": False, "error": f"supervisor transport unavailable: {exc}"}

        exception_details = reply.get("exceptionDetails")
        if exception_details:
            text = exception_details.get("text") or "JavaScript exception"
            return {"ok": False, "error": str(text)}
        result_obj = reply.get("result", {})
        return {"ok": True, "result": result_obj.get("value")}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class SupervisorRegistry:
    """Process-wide ``session_key`` → supervisor map, idempotent start/stop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_key: dict[str, CDPSupervisor] = {}

    def get(self, session_key: str) -> CDPSupervisor | None:
        with self._lock:
            return self._by_key.get(session_key)

    def get_or_start(
        self,
        session_key: str,
        cdp_url: str,
        *,
        dialog_policy: str = DEFAULT_DIALOG_POLICY,
        dialog_timeout_s: float = DEFAULT_DIALOG_TIMEOUT_S,
        transport: Transport | None = None,
    ) -> CDPSupervisor:
        with self._lock:
            existing = self._by_key.get(session_key)
            if existing is not None and existing.cdp_url == cdp_url and existing.active:
                return existing
            if existing is not None:
                # URL changed or connection dead — tear down and restart.
                try:
                    existing.stop()
                except Exception:  # noqa: BLE001
                    pass
            supervisor = CDPSupervisor(
                session_key,
                cdp_url,
                dialog_policy=dialog_policy,
                dialog_timeout_s=dialog_timeout_s,
                transport=transport,
            )
            self._by_key[session_key] = supervisor
        # start() does I/O; keep it outside the registry lock.
        try:
            supervisor.start()
        except Exception as exc:  # noqa: BLE001 - attach failure must not break the session
            log.debug(
                "browser_supervisor.start_failed",
                session_key=session_key,
                cdp_url=redact_cdp_url(cdp_url),
                error=str(exc),
            )
            # Do not leave a supervisor that never attached in the registry: a
            # later lookup would take the dead object and never retry, so every
            # dialog for the rest of the session goes unanswered.
            with self._lock:
                if self._by_key.get(session_key) is supervisor:
                    del self._by_key[session_key]
            with contextlib.suppress(Exception):
                supervisor.stop()
            raise
        return supervisor

    def stop(self, session_key: str) -> None:
        with self._lock:
            supervisor = self._by_key.pop(session_key, None)
        if supervisor is not None:
            supervisor.stop()

    def stop_all(self) -> None:
        with self._lock:
            supervisors = list(self._by_key.values())
            self._by_key.clear()
        for supervisor in supervisors:
            try:
                supervisor.stop()
            except Exception:  # noqa: BLE001
                pass


SUPERVISOR_REGISTRY = SupervisorRegistry()


# ---------------------------------------------------------------------------
# Live WebSocket transport
# ---------------------------------------------------------------------------


class _WebSocketTransport:
    """Live CDP transport: a background thread running an asyncio WS loop.

    The endpoint is either a ``ws(s)://…`` browser URL (managed mode, from
    agent-browser ``get cdp-url``) used directly, or an ``http://host:port``
    endpoint (attach mode) whose browser WS URL is resolved from
    ``/json/version``. Attaches to page targets and pumps events to the
    supervisor. Kept deliberately small; the supervisor holds the logic.
    """

    def __init__(self, cdp_endpoint: str) -> None:
        self._endpoint = cdp_endpoint.rstrip("/")
        self._is_ws = cdp_endpoint.lower().startswith(("ws://", "wss://"))
        self._http_url = "" if self._is_ws else self._endpoint
        self._loop: Any = None
        self._thread: threading.Thread | None = None
        self._ws: Any = None
        self._on_event: Any = None
        self._ready = threading.Event()
        self._next_id = 1
        self._pending: dict[int, Any] = {}
        self._session_id: str | None = None
        self._page_sessions: set[str] = set()
        self._page_ready: Any = None
        self._stopped = False
        self._start_error: BaseException | None = None

    def start(self, on_event: Any) -> None:
        self._on_event = on_event
        self._thread = threading.Thread(target=self._run, name="cdp-supervisor", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=SUPERVISOR_START_TIMEOUT):
            raise TimeoutError(
                f"CDP supervisor did not attach within {SUPERVISOR_START_TIMEOUT:g}s"
            )
        if self._start_error is not None:
            raise self._start_error

    def call(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        import asyncio

        if self._loop is None:
            raise RuntimeError("transport not started")
        future = asyncio.run_coroutine_threadsafe(
            self._call_async(method, params, session_id=session_id), self._loop
        )
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            # Cancel the coroutine, or it stays parked on `await future` forever
            # and its pending id is never reclaimed — the `finally` inside
            # _call_async only runs if the coroutine actually resumes.
            future.cancel()
            raise

    def stop(self) -> None:
        import asyncio

        self._stopped = True
        # Only schedule the close coroutine when the loop is actually running;
        # if connect failed the loop has already exited and scheduling would
        # leave an un-awaited coroutine.
        if self._loop is not None and self._loop.is_running():
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(self._close_async(), self._loop).result(timeout=5)
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)

    # -- background loop --

    def _run(self) -> None:
        import asyncio

        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._connect_async())
            self._ready.set()
            loop.run_forever()
        except BaseException as exc:  # noqa: BLE001 - report to start()
            self._start_error = exc
            self._ready.set()
        finally:
            with contextlib.suppress(Exception):
                loop.close()

    async def _connect_async(self) -> None:
        import websockets

        if self._is_ws:
            ws_url = self._endpoint
        else:
            import httpx

            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{self._http_url}/json/version", timeout=10.0)
                ws_url = resp.json().get("webSocketDebuggerUrl")
            if not ws_url:
                raise RuntimeError("CDP endpoint did not advertise a webSocketDebuggerUrl")
        self._ws = await websockets.connect(ws_url, max_size=None)
        import asyncio

        self._page_ready = asyncio.Event()
        asyncio.ensure_future(self._reader_loop())
        # Auto-attach to page targets (flattened) so dialog/console events flow
        # with a page session id.
        await self._call_async(
            "Target.setAutoAttach",
            {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
        )
        # Wait for at least one page session so dialog interception is armed by
        # the time start() returns. Domains are enabled per-session as each page
        # attaches (see the reader). If no page attaches, proceed without — the
        # session still works, dialogs just aren't intercepted.
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            await asyncio.wait_for(self._page_ready.wait(), timeout=PAGE_ATTACH_TIMEOUT)

    async def _reader_loop(self) -> None:
        import json

        try:
            async for raw in self._ws:  # type: ignore[union-attr]
                # Each message is handled in its own guard. One malformed frame,
                # or a future already cancelled by a timed-out caller, used to
                # take down the whole pump: the connection then looked alive
                # while delivering nothing, so dialogs stopped being intercepted
                # for the rest of the session and the page wedged.
                try:
                    self._dispatch_message(json.loads(raw))
                except Exception:  # noqa: BLE001 - never kill the pump
                    log.debug("browser_supervisor.message_dispatch_failed", exc_info=True)
        except Exception:  # noqa: BLE001 - loop ends when the socket closes
            return

    def _dispatch_message(self, message: dict[str, Any]) -> None:
        import asyncio

        mid = message.get("id")
        if mid is not None and mid in self._pending:
            future = self._pending.pop(mid)
            if not future.done():
                future.set_result(message)
            return
        method = message.get("method")
        sess = message.get("sessionId")
        if method == "Target.attachedToTarget":
            params = message.get("params", {})
            target_type = params.get("targetInfo", {}).get("type")
            child = params.get("sessionId")
            if target_type == "page" and child:
                self._page_sessions.add(child)
                self._session_id = child
                # Enable the domains we consume ON this page session so dialog
                # events are delivered from it.
                for dom in ("Page.enable", "Runtime.enable"):
                    asyncio.ensure_future(self._enable_on(dom, child))
                if self._page_ready is not None:
                    self._page_ready.set()
            return
        if method and self._on_event is not None:
            with contextlib.suppress(Exception):
                self._on_event(method, message.get("params", {}), sess)

    async def _enable_on(self, method: str, session_id: str) -> None:
        with contextlib.suppress(Exception):
            await self._call_async(method, {}, session_id=session_id)

    async def _call_async(
        self, method: str, params: dict[str, Any], session_id: str | None = None
    ) -> dict[str, Any]:
        import asyncio
        import json

        if self._ws is None:
            raise RuntimeError("websocket not connected")
        call_id = self._next_id
        self._next_id += 1
        future: Any = asyncio.get_running_loop().create_future()
        self._pending[call_id] = future
        payload: dict[str, Any] = {"id": call_id, "method": method, "params": params}
        default_session = self._session_id if method != "Target.setAutoAttach" else None
        target_session = session_id or default_session
        if target_session:
            payload["sessionId"] = target_session
        try:
            await self._ws.send(json.dumps(payload))
            message = await future
        finally:
            # A send that raises, or a caller that times out, would otherwise
            # leave this id pending forever — the reader only removes ids it
            # sees a reply for.
            self._pending.pop(call_id, None)
        if "error" in message:
            raise RuntimeError(str(message["error"]))
        result: dict[str, Any] = message.get("result", {})
        return result

    async def _close_async(self) -> None:
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()


__all__ = [
    "CDPSupervisor",
    "ConsoleEvent",
    "DEFAULT_DIALOG_POLICY",
    "DEFAULT_DIALOG_TIMEOUT_S",
    "DialogRecord",
    "PendingDialog",
    "PAGE_ATTACH_TIMEOUT",
    "SUPERVISOR_REGISTRY",
    "SUPERVISOR_START_TIMEOUT",
    "SupervisorRegistry",
    "SupervisorSnapshot",
    "Transport",
    "VALID_DIALOG_POLICIES",
    "parse_dialog_policy",
]
