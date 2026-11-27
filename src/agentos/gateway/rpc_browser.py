"""RPC handlers for the operator's live view of a session's browser.

The ``browser`` tool drives one browser per session key
(:mod:`agentos.tools.agent_browser`). The web console's Browser panel shows
that same browser beside the conversation and offers the few controls a
person needs to follow along:

* ``browser.status`` -- whether the tool is enabled and installed, and whether
  this session has a browser running. It never calls the engine: any engine
  command against an unknown session launches a browser.
* ``browser.snapshot`` -- the running browser's tabs and a JPEG of the active
  tab. It neither starts a browser nor extends an idle one's life.
* ``browser.start`` -- start the session's browser on ``about:blank`` (a new
  tab in attach mode, never the tab the user is on).
* ``browser.navigate`` -- open a URL in the active tab.
* ``browser.history`` -- back, forward or reload.
* ``browser.tabs.select`` -- bring a listed tab to the front.

The policy is the tool's own, not a second copy of it: navigation goes
through the tool's allowlist and SSRF check, a page that ends on a private or
internal address is left for ``about:blank``, and a page sitting on one is not
captured (all relaxed in attach mode, as the tool relaxes them). No method
takes a script, a selector or a raw engine command; the arguments are a URL, a
fixed action name or a tab id the engine listed.

Every method is a Control-surface method and is refused to a connection the
gateway recognizes as an agent's: an agent drives the browser through its
tool, under that tool's policy, not through the operator's view.
"""

from __future__ import annotations

import atexit
import base64
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

import anyio

from agentos.gateway.access import CONTROL_ONLY
from agentos.gateway.protocol import ErrorCode
from agentos.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher
from agentos.tools import agent_browser
from agentos.tools.builtin import browser as browser_tool
from agentos.tools.types import ToolError

_d = get_dispatcher()

MAX_SESSION_KEY_LENGTH = 512
MAX_URL_LENGTH = 2048
#: Tabs returned; a browser with more says so with ``truncated``.
MAX_TABS = 50
MAX_TITLE_LENGTH = 300
#: A captured frame larger than this is refused rather than sent.
MAX_IMAGE_BYTES = 8 * 1024 * 1024
JPEG_QUALITY = 70
#: Engine calls the view makes are quick reads; never the navigate budget.
_VIEW_TIMEOUT_S = 15.0
_NAVIGATE_TIMEOUT_S = 45.0

HISTORY_ACTIONS = frozenset({"back", "forward", "reload"})
#: The engine's stable tab ids: ``t1``, ``t2``, ... never reused in a session.
_TAB_ID = re.compile(r"^t[1-9][0-9]{0,8}$")


def _refuse_agent_caller(ctx: RpcContext) -> None:
    from agentos.gateway.agent_surface import recognized_agent

    binding = recognized_agent(ctx)
    if binding is not None:
        raise RpcHandlerError(
            "agent.operator_required",
            "The browser view is the user's; an agent drives the browser with its browser tool.",
            details=binding.to_dict(),
        )


def _params(params: Any) -> dict[str, Any]:
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    return params


def _session_key(body: dict[str, Any]) -> str:
    raw = body.get("sessionKey", body.get("session_key"))
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("params.sessionKey is required")
    key = raw.strip()
    if len(key) > MAX_SESSION_KEY_LENGTH:
        raise ValueError("params.sessionKey is too long")
    if any(ord(ch) < 33 or ord(ch) == 127 for ch in key):
        raise ValueError("params.sessionKey must not contain spaces or control characters")
    return key


def _error(code: str, message: str, *, top: ErrorCode = ErrorCode.UNAVAILABLE) -> RpcHandlerError:
    """One error shape for every refusal: ``details.code`` is what the UI reads."""
    return RpcHandlerError(top, message, details={"code": f"browser.{code}"})


def _mode() -> str:
    return "attach" if agent_browser.is_attach_mode() else "managed"


def _availability() -> tuple[bool, bool]:
    """``(enabled, installed)`` for the browser tool, without raising."""
    enabled = bool(agent_browser.browser_doctor_status().get("enabled"))
    installed = False
    if enabled:
        try:
            installed = agent_browser.resolve_binary() is not None
        except Exception:  # noqa: BLE001 - an availability probe must not raise
            installed = False
    return enabled, installed


def _require_usable() -> None:
    """Refuse when the tool could not run either: disabled, missing or unconsented."""
    enabled, installed = _availability()
    if not enabled:
        raise _error("disabled", "The browser tool is turned off (browser.enabled = false).")
    if not installed:
        raise _error("not_installed", agent_browser.browser_doctor_status()["installHint"])
    refusal = browser_tool.attach_refusal()
    if refusal is not None:
        raise _error("attach_unconfirmed", refusal)


def _engine_error(result: dict[str, Any], fallback: str) -> RpcHandlerError:
    if result.get("code") == "not_running":
        return _error("not_running", "No browser is running for this session.")
    message = str(result.get("error") or fallback)
    return _error("failed", agent_browser.redact_cdp_url(message)[:500])


def _clip(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else ""
    return text[:limit]


def _tabs(data: Any) -> tuple[list[dict[str, Any]], bool]:
    """The engine's tab list as the view shows it: page tabs only, capped."""
    rows = data.get("tabs") if isinstance(data, dict) else None
    tabs: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        tab_id = row.get("tabId")
        if not isinstance(tab_id, str) or not _TAB_ID.match(tab_id):
            continue
        if row.get("type") not in (None, "page"):
            continue
        tabs.append(
            {
                "tabId": tab_id,
                "title": _clip(row.get("title"), MAX_TITLE_LENGTH),
                "url": _clip(row.get("url"), MAX_URL_LENGTH),
                "active": row.get("active") is True,
            }
        )
    truncated = len(tabs) > MAX_TABS
    return tabs[:MAX_TABS], truncated


def status_payload(session_key: str) -> dict[str, Any]:
    """What ``browser.status`` answers; reads only process-local state."""
    enabled, installed = _availability()
    running = agent_browser.get_session(session_key) is not None
    attach_unconfirmed = browser_tool.attach_refusal() is not None
    payload: dict[str, Any] = {
        "sessionKey": session_key,
        "enabled": enabled,
        "installed": installed,
        "mode": _mode(),
        "running": running and enabled and installed,
        "attachConfirmed": not attach_unconfirmed,
        "canStart": enabled and installed and not attach_unconfirmed and not running,
    }
    if enabled and not installed:
        payload["installHint"] = agent_browser.browser_doctor_status()["installHint"]
    return payload


# ---------------------------------------------------------------------------
# Screenshots
# ---------------------------------------------------------------------------

#: Where the capture directory is made; None is the system temp directory.
#: Tests point it at their own tmp path so a run leaves nothing behind.
CAPTURE_PARENT: str | None = None
CAPTURE_PREFIX = "agentos-browser-view-"

_capture_dir: Path | None = None
_exit_cleanup_registered = False


def _capture_path() -> Path:
    """A fresh file name in a private (0700) directory owned by this process.

    The directory outlives one capture (each file is removed once read) and
    goes when the process does: at interpreter exit, and earlier on the
    gateway's shutdown (:func:`release_capture_dir`).
    """
    global _capture_dir, _exit_cleanup_registered
    if _capture_dir is None or not _capture_dir.is_dir():
        _capture_dir = Path(tempfile.mkdtemp(prefix=CAPTURE_PREFIX, dir=CAPTURE_PARENT))
        if not _exit_cleanup_registered:
            atexit.register(release_capture_dir)
            _exit_cleanup_registered = True
    return _capture_dir / f"{uuid.uuid4().hex}.jpg"


def release_capture_dir() -> None:
    """Remove the capture directory, if one was made. Safe to call repeatedly."""
    global _capture_dir
    directory, _capture_dir = _capture_dir, None
    if directory is not None:
        shutil.rmtree(directory, ignore_errors=True)


def _read_capture(path: Path) -> bytes:
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_IMAGE_BYTES + 1)
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    if len(data) > MAX_IMAGE_BYTES:
        raise _error("too_large", "The page image is too large to show.")
    return data


async def _capture(session_key: str) -> dict[str, Any]:
    path = _capture_path()
    try:
        result = await agent_browser.run_command(
            session_key,
            "screenshot",
            ["--screenshot-format", "jpeg", "--screenshot-quality", str(JPEG_QUALITY), str(path)],
            timeout=_VIEW_TIMEOUT_S,
            create=False,
            touch=False,
        )
        if not result.get("success"):
            raise _engine_error(result, "The page could not be captured.")
        try:
            data = await anyio.to_thread.run_sync(_read_capture, path)
        except FileNotFoundError as exc:
            raise _error("failed", "The engine reported a capture but wrote no image.") from exc
    finally:
        if path.exists():
            os.unlink(path)
    if not data:
        raise _error("failed", "The engine wrote an empty image.")
    return {
        "mime": "image/jpeg",
        "data": base64.b64encode(data).decode("ascii"),
        "bytes": len(data),
    }


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


@_d.method("browser.status", CONTROL_ONLY)
async def _handle_browser_status(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Params: ``sessionKey``. Never starts, touches or calls the engine."""
    _refuse_agent_caller(ctx)
    return status_payload(_session_key(_params(params)))


@_d.method("browser.snapshot", CONTROL_ONLY)
async def _handle_browser_snapshot(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Tabs and a JPEG of the active tab. Params: ``sessionKey``, ``image`` (default true).

    Answers ``running: false`` (not an error) when the session has no
    browser, which is also what a reaped one looks like.
    """
    _refuse_agent_caller(ctx)
    body = _params(params)
    key = _session_key(body)
    _require_usable()
    listing = await agent_browser.run_command(
        key, "tab", ["list"], timeout=_VIEW_TIMEOUT_S, create=False, touch=False
    )
    if listing.get("code") == "not_running":
        return {"sessionKey": key, "running": False, "mode": _mode(), "tabs": []}
    if not listing.get("success"):
        raise _engine_error(listing, "The browser's tabs could not be read.")
    tabs, truncated = _tabs(listing.get("data"))
    active = next((tab for tab in tabs if tab["active"]), tabs[0] if tabs else None)
    payload: dict[str, Any] = {
        "sessionKey": key,
        "running": True,
        "mode": _mode(),
        "tabs": tabs,
        "truncated": truncated,
        "activeTabId": active["tabId"] if active else None,
        "url": active["url"] if active else "",
        "title": active["title"] if active else "",
        "image": None,
    }
    if body.get("image") is False or active is None:
        return payload
    url = active["url"]
    if not agent_browser.is_attach_mode() and await anyio.to_thread.run_sync(
        browser_tool.url_is_private, url
    ):
        payload["withheld"] = "private"
        return payload
    payload["image"] = await _capture(key)
    return payload


@_d.method("browser.start", CONTROL_ONLY)
async def _handle_browser_start(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Start the session's browser on ``about:blank``. Params: ``sessionKey``.

    In attach mode the browser is the user's own Chrome: a new blank tab is
    opened rather than sending whatever tab they are on to ``about:blank``.
    """
    _refuse_agent_caller(ctx)
    key = _session_key(_params(params))
    _require_usable()
    if agent_browser.get_session(key) is None:
        command, args = (
            ("tab", ["new"]) if agent_browser.is_attach_mode() else ("open", ["about:blank"])
        )
        result = await agent_browser.run_command(key, command, args)
        if not result.get("success"):
            # A browser that failed to come up must not read as running.
            await anyio.to_thread.run_sync(agent_browser.close_session, key)
            raise _engine_error(result, "The browser could not be started.")
        await browser_tool.ensure_supervisor(key)
    return status_payload(key)


def _navigation_url(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("params.url is required")
    url = raw.strip()
    if len(url) > MAX_URL_LENGTH:
        raise ValueError("params.url is too long")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise ValueError("params.url contains control characters")
    lowered = url.lower()
    if lowered != "about:blank" and not lowered.startswith(("http://", "https://")):
        raise _error(
            "blocked",
            "Only http(s) addresses (and about:blank) can be opened.",
            top=ErrorCode.FORBIDDEN,
        )
    return url


@_d.method("browser.navigate", CONTROL_ONLY)
async def _handle_browser_navigate(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Open ``url`` in the active tab. Params: ``sessionKey``, ``url``.

    The tool's allowlist and SSRF policy decide; a page that lands on a
    private address after redirects is replaced with ``about:blank``.
    """
    _refuse_agent_caller(ctx)
    body = _params(params)
    key = _session_key(body)
    url = _navigation_url(body.get("url"))
    _require_usable()
    if agent_browser.get_session(key) is None:
        raise _error("not_running", "No browser is running for this session.")
    try:
        await anyio.to_thread.run_sync(browser_tool.check_navigable, url)
    except ToolError as exc:
        raise _error("blocked", str(exc), top=ErrorCode.FORBIDDEN) from exc
    result = await agent_browser.run_command(
        key, "open", [url], timeout=_NAVIGATE_TIMEOUT_S, create=False
    )
    if not result.get("success"):
        raise _engine_error(result, "The page could not be opened.")
    raw_data = result.get("data")
    data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
    final_url = _clip(data.get("url") or url, MAX_URL_LENGTH)
    if not agent_browser.is_attach_mode() and await anyio.to_thread.run_sync(
        browser_tool.url_is_private, final_url
    ):
        await agent_browser.run_command(
            key, "open", ["about:blank"], timeout=_VIEW_TIMEOUT_S, create=False
        )
        raise _error(
            "blocked",
            "The page ended on a private or internal address and was closed.",
            top=ErrorCode.FORBIDDEN,
        )
    await browser_tool.ensure_supervisor(key)
    return {
        "sessionKey": key,
        "url": final_url,
        "title": _clip(data.get("title"), MAX_TITLE_LENGTH),
    }


@_d.method("browser.history", CONTROL_ONLY)
async def _handle_browser_history(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Back, forward or reload the active tab. Params: ``sessionKey``, ``action``."""
    _refuse_agent_caller(ctx)
    body = _params(params)
    key = _session_key(body)
    action = body.get("action")
    if action not in HISTORY_ACTIONS:
        raise ValueError("params.action must be one of: back, forward, reload")
    _require_usable()
    result = await agent_browser.run_command(
        key, str(action), [], timeout=_NAVIGATE_TIMEOUT_S, create=False
    )
    if not result.get("success"):
        raise _engine_error(result, f"The browser could not {action}.")
    return {"sessionKey": key, "action": action}


@_d.method("browser.tabs.select", CONTROL_ONLY)
async def _handle_browser_tabs_select(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Bring a listed tab to the front. Params: ``sessionKey``, ``tabId`` (``t<N>``)."""
    _refuse_agent_caller(ctx)
    body = _params(params)
    key = _session_key(body)
    tab_id = body.get("tabId")
    if not isinstance(tab_id, str) or not _TAB_ID.match(tab_id):
        raise ValueError("params.tabId must be a tab id from browser.snapshot (t1, t2, ...)")
    _require_usable()
    result = await agent_browser.run_command(
        key, "tab", [tab_id], timeout=_VIEW_TIMEOUT_S, create=False
    )
    if not result.get("success"):
        raise _engine_error(result, "The tab could not be selected.")
    return {"sessionKey": key, "tabId": tab_id}


__all__ = [
    "HISTORY_ACTIONS",
    "MAX_IMAGE_BYTES",
    "MAX_TABS",
    "release_capture_dir",
    "status_payload",
]
