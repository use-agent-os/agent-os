"""Adapter around the ``agent-browser`` CLI engine.

``agent-browser`` (Vercel Labs, Apache-2.0) is a Rust client–daemon that speaks
CDP directly and returns accessibility-tree snapshots with deterministic element
refs (``@e1``…). This module is the thin async layer between AgentOS and that
CLI: it resolves the binary, spawns each command with a **scrubbed** environment
(the gateway token and provider keys must be unreachable from the browser
process — the Hermes lesson, GHSA-m4m8-xjp4-5rmm), manages one engine session per
AgentOS session, and reaps idle sessions.

Two modes:

* **managed** (default): the engine launches its own headless Chromium. Its CDP
  endpoint is read back via ``agent-browser get cdp-url`` (a loopback
  ``ws://127.0.0.1:…`` URL) so :mod:`agentos.tools.browser_supervisor` can attach
  for dialog interception. A non-loopback URL is refused; if none is available
  the session still works and the supervisor stays detached.
* **attach**: when ``browser.cdp_port`` is set the engine connects to the user's
  own Chrome via ``--cdp <port>``. The port is an **int, localhost only** — there
  is no code path that accepts a CDP URL string, which structurally rules out
  cloud CDP endpoints.

The tool layer talks to this adapter's typed surface, so a future engine can be
swapped in without touching the tool.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import glob
import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from typing import Any

import structlog

from agentos.redact import redact_cdp_url
from agentos.tools.env_passthrough import build_subprocess_env

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Defaults / runtime configuration (mirrors x_search's configure/reset pattern)
# ---------------------------------------------------------------------------

DEFAULT_COMMAND_TIMEOUT = 30.0
DEFAULT_NAVIGATE_TIMEOUT = 60.0
#: First navigate of a session pays cold-daemon + Chromium spawn (Hermes needed
#: the same larger floor for the first open).
FIRST_OPEN_TIMEOUT = 120.0
DEFAULT_SESSION_TTL_MINUTES = 15
DEFAULT_MAX_SESSIONS = 3

_active_enabled: bool = True
_active_headless: bool = True
_active_binary_path: str = ""
_active_cdp_port: int = 0
_active_persist_profile: bool = False
_active_session_ttl_minutes: int = DEFAULT_SESSION_TTL_MINUTES
_active_max_sessions: int = DEFAULT_MAX_SESSIONS
_active_allowed_domains: tuple[str, ...] = ()

#: Cached binary resolution.
_resolved_binary: str | None = None
_binary_resolved: bool = False


def configure_browser(config: Any | None = None) -> None:
    """Apply a ``BrowserConfig``-shaped object to process-wide runtime state."""
    global _active_enabled, _active_headless, _active_binary_path, _active_cdp_port
    global _active_persist_profile, _active_session_ttl_minutes, _active_max_sessions
    global _active_allowed_domains, _resolved_binary, _binary_resolved

    def _get(name: str, default: Any) -> Any:
        if config is None:
            return default
        value = getattr(config, name, None)
        return default if value is None else value

    previous_enabled = _active_enabled
    previous_cdp_port = _active_cdp_port
    previous_max_sessions = _active_max_sessions

    _active_enabled = bool(_get("enabled", True))
    _active_headless = bool(_get("headless", True))
    _active_binary_path = str(_get("binary_path", "")).strip()
    _active_cdp_port = int(_get("cdp_port", 0) or 0)
    _active_persist_profile = bool(_get("persist_profile", False))
    _active_session_ttl_minutes = max(
        1, int(_get("session_ttl_minutes", DEFAULT_SESSION_TTL_MINUTES))
    )
    _active_max_sessions = max(1, int(_get("max_sessions", DEFAULT_MAX_SESSIONS)))
    domains = _get("allowed_domains", ()) or ()
    _active_allowed_domains = tuple(str(d).strip().lower() for d in domains if str(d).strip())

    # Binary path may have changed; drop the resolution cache.
    _resolved_binary = None
    _binary_resolved = False

    # A live session captured its mode and port when it was created, so a config
    # change that moves either one has to end those sessions rather than let
    # them keep driving the old browser under the new policy. Turning the tool
    # off matters just as much: nothing would call in again, so the TTL sweep
    # would never run and a managed Chromium would survive until shutdown.
    if _sessions and (previous_cdp_port != _active_cdp_port or previous_enabled != _active_enabled):
        log.info(
            "browser.sessions_dropped_on_reconfigure",
            enabled=_active_enabled,
            cdp_port=_active_cdp_port,
        )
        close_all_sessions()
    elif _sessions and _active_max_sessions < previous_max_sessions:
        # Lowering the cap has to act on the sessions already running. Nothing
        # else will: `_evict_if_over_cap` is only reached when a *new* session
        # is created, and reusing an existing one refreshes `last_used_at`, so
        # the idle reaper never takes it either. An operator who lowers this is
        # almost always relieving memory pressure -- each managed session is a
        # Chromium process -- and would otherwise see the number change in
        # config and nothing change on the box.
        _trim_to_cap()


def _trim_to_cap() -> None:
    """Evict oldest-idle until the live set fits ``max_sessions``.

    Separate from :func:`_evict_if_over_cap`, which compares with ``>=``
    because it is making room for one more session. Enforcing the cap after a
    config change wants ``>``, or it would evict one session too many.
    """
    while len(_sessions) > _active_max_sessions:
        oldest_key = min(_sessions, key=lambda k: _sessions[k].last_used_at)
        log.info(
            "browser.session_evicted_on_reconfigure",
            session_key=oldest_key,
            max_sessions=_active_max_sessions,
        )
        _drop_session(oldest_key)


def reset_browser_runtime() -> None:
    """Restore boot defaults. Used by tests and by a config reload to bare state."""
    configure_browser(None)


# ---------------------------------------------------------------------------
# Binary resolution + availability
# ---------------------------------------------------------------------------


def _version_key(path: str) -> tuple[int, ...]:
    """Sort key for a node-version directory, so newest sorts first.

    The version is whichever component of *path* parses as one, searched from
    the right. A fixed depth does not work for both globs below: nvm ends
    ``.../v24.16.0/bin`` but fnm ends ``.../v24.16.0/installation/bin``, so
    reading the parent of the leaf found ``installation``, keyed every fnm
    entry to ``()`` and left them in ``glob`` order -- which is not newest
    first, and silently picked an older Node's binary (#3606).
    """
    for piece in reversed(path.replace(chr(92), "/").split("/")):
        parts: list[int] = []
        for chunk in piece.lstrip("v").split("."):
            if not chunk.isdigit():
                break
            parts.append(int(chunk))
        if parts:
            return tuple(parts)
    return ()


def _newest_first(pattern: str) -> list[str]:
    return sorted(glob.glob(pattern), key=_version_key, reverse=True)


def _fallback_bin_dirs() -> list[str]:
    """Where npm and the Node version managers put global binaries.

    A gateway started by the desktop app, launchd or systemd inherits a bare
    PATH (``/usr/bin:/bin:/usr/sbin:/sbin`` on macOS), not the login shell's.
    ``npm install -g agent-browser`` under nvm, fnm, Volta or Homebrew then
    lands in a directory :func:`shutil.which` never looks at, and the tool
    stays unavailable on a machine where the operator just installed it.
    """
    home = os.path.expanduser("~")
    env = {
        name: os.environ.get(name, "").strip()
        for name in ("NVM_BIN", "NVM_DIR", "PNPM_HOME", "APPDATA")
    }
    dirs = [env["NVM_BIN"], env["PNPM_HOME"]]
    nvm_dir = env["NVM_DIR"] or os.path.join(home, ".nvm")
    dirs += _newest_first(os.path.join(nvm_dir, "versions", "node", "*", "bin"))
    for fnm_root in (
        os.path.join(home, ".local", "share", "fnm"),
        os.path.join(home, "Library", "Application Support", "fnm"),
    ):
        dirs += _newest_first(os.path.join(fnm_root, "node-versions", "*", "installation", "bin"))
    dirs += [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        os.path.join(home, ".npm-global", "bin"),
        os.path.join(home, ".volta", "bin"),
        os.path.join(home, ".bun", "bin"),
        os.path.join(home, "Library", "pnpm"),
        os.path.join(home, ".local", "share", "pnpm"),
        os.path.join(home, ".local", "bin"),
    ]
    if env["APPDATA"]:
        dirs.append(os.path.join(env["APPDATA"], "npm"))
    return list(dict.fromkeys(d for d in dirs if d))


def resolve_binary() -> str | None:
    """Return the ``agent-browser`` path: the config override, then PATH, then
    the usual npm global install directories (:func:`_fallback_bin_dirs`).

    Only a *successful* resolution is cached. Caching the miss too would make
    the install flow the doctor prints a dead end: after
    ``npm install -g agent-browser`` the tool would stay hidden until the
    gateway restarted, because nothing re-probes.
    """
    global _resolved_binary, _binary_resolved
    if _binary_resolved and _resolved_binary is not None:
        return _resolved_binary
    candidate: str | None = None
    if _active_binary_path:
        expanded = os.path.expanduser(_active_binary_path)
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            candidate = expanded
    if candidate is None:
        candidate = shutil.which("agent-browser")
    if candidate is None:
        candidate = shutil.which("agent-browser", path=os.pathsep.join(_fallback_bin_dirs()))
        if candidate is not None:
            log.info("browser.binary_found_outside_path", path=candidate)
    _resolved_binary = candidate
    _binary_resolved = candidate is not None
    return candidate


def browser_available() -> bool:
    """Whether the browser tool has everything it needs to run.

    Called every time the tool surface is rebuilt (see
    :mod:`agentos.tools.policy_runtime`), so it stays local: enabled + a
    resolvable binary. A broken binary surfaces as a call error, better than a
    tool that vanishes mid-session.
    """
    if not _active_enabled:
        return False
    try:
        return resolve_binary() is not None
    except Exception:  # noqa: BLE001 - capability checks must never raise
        return False


def is_attach_mode() -> bool:
    """True when the adapter would drive the user's own Chrome via ``--cdp``."""
    return _active_cdp_port > 0


def configured_cdp_port() -> int:
    return _active_cdp_port


def persist_profile_enabled() -> bool:
    return _active_persist_profile


def browser_doctor_status() -> dict[str, Any]:
    """Snapshot for ``agentos doctor``: enabled + binary presence + mode."""
    binary = None
    try:
        binary = resolve_binary()
    except Exception:  # noqa: BLE001 - status probe must not raise
        binary = None
    return {
        "enabled": _active_enabled,
        "binaryPresent": binary is not None,
        "binaryPath": binary or "",
        "attachMode": is_attach_mode(),
        "cdpPort": _active_cdp_port,
        "installHint": _install_hint(),
    }


# ---------------------------------------------------------------------------
# Environment scrubbing (Hermes lesson: never hand the engine os.environ)
# ---------------------------------------------------------------------------

#: The only names a headless-browser subprocess legitimately needs. We start
#: from this minimal set — never ``os.environ`` — so provider keys and the
#: gateway token cannot be read out of the engine's ``process.env``.
_ENV_PASSTHROUGH_NAMES: tuple[str, ...] = (
    "PATH",
    "HOME",
    "TMPDIR",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "DISPLAY",
    "XAUTHORITY",
    "USER",
    "LOGNAME",
    # Windows needs its own floor: a process started without SystemRoot/COMSPEC
    # frequently fails to initialise at all (socket and crypto startup read
    # them), and PATHEXT is how the loader resolves an extensionless command.
    # Leaving these out made the engine unspawnable on Windows.
    "SYSTEMROOT",
    "SystemRoot",
    "COMSPEC",
    "ComSpec",
    "PATHEXT",
    "WINDIR",
    "SYSTEMDRIVE",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMFILES",
    "PROGRAMDATA",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    # Chromium/Playwright locations, when the operator set them.
    "PLAYWRIGHT_BROWSERS_PATH",
    "AGENT_BROWSER_HOME",
)


def _scrubbed_env() -> dict[str, str]:
    """Build a minimal environment for the engine subprocess.

    Two layers: (1) copy only the small passthrough allowlist from the current
    environment, then (2) run even that through ``build_subprocess_env`` so
    AgentOS's own control names are stripped regardless. The result contains no
    provider credential and no gateway token.
    """
    minimal = {name: os.environ[name] for name in _ENV_PASSTHROUGH_NAMES if name in os.environ}
    return build_subprocess_env(base=minimal)


# ---------------------------------------------------------------------------
# Session registry
# ---------------------------------------------------------------------------


@dataclass
class BrowserSession:
    session_key: str
    engine_session_name: str
    mode: str  # "managed" | "attach"
    cdp_port: int = 0  # attach: the user's Chrome debug port
    cdp_ws_url: str = ""  # managed: cached CDP ws URL from `get cdp-url`
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    first_open_done: bool = False

    def cdp_endpoint(self) -> str | None:
        """CDP endpoint the supervisor can attach to, if known.

        Attach mode: the operator's Chrome debug port as a loopback http URL.
        Managed mode: the ws URL agent-browser reports via ``get cdp-url`` (also
        loopback), resolved lazily and cached here.
        """
        if self.mode == "attach" and self.cdp_port > 0:
            return f"http://127.0.0.1:{self.cdp_port}"
        return self.cdp_ws_url or None


_sessions: dict[str, BrowserSession] = {}


def is_loopback_cdp_url(url: str) -> bool:
    """True when *url*'s host is loopback — the security floor for attach/managed.

    agent-browser's managed Chromium and a correctly-configured attach target
    both expose CDP on 127.0.0.1; anything else (a routable or 0.0.0.0 host)
    means the debug endpoint is reachable off-box and must be refused.
    """
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").strip().lower()
    return host in {"127.0.0.1", "::1", "localhost"}


def _new_engine_session_name(session_key: str) -> str:
    """Deterministic engine-session name for an AgentOS session key.

    Hashed with SHA-256 rather than :func:`hash`, whose salt is per-process: a
    gateway restart would otherwise mint a new engine session for the same
    AgentOS session and orphan the browser the old name still owns. Namespaced
    so a reaper never touches a non-AgentOS ``agent-browser`` session.
    """
    digest = hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:12]
    return f"agentos-{digest}"


def _evict_if_over_cap() -> None:
    """Drop the oldest-idle managed session when over ``max_sessions``."""
    while len(_sessions) >= _active_max_sessions:
        oldest_key = min(_sessions, key=lambda k: _sessions[k].last_used_at)
        _drop_session(oldest_key)


def get_or_create_session(session_key: str) -> BrowserSession:
    """Return the engine session for *session_key*, creating it if needed."""
    # Opportunistic TTL sweep. Reaping on use rather than from a background
    # timer keeps the adapter free of a task lifecycle to own; a process that
    # stops using the browser entirely is covered by the atexit hook and by the
    # gateway's own teardown instead.
    reap_idle_sessions()

    existing = _sessions.get(session_key)
    if existing is not None:
        existing.last_used_at = time.time()
        return existing

    _evict_if_over_cap()

    if is_attach_mode():
        session = BrowserSession(
            session_key=session_key,
            engine_session_name=_new_engine_session_name(session_key),
            mode="attach",
            cdp_port=_active_cdp_port,
        )
    else:
        session = BrowserSession(
            session_key=session_key,
            engine_session_name=_new_engine_session_name(session_key),
            mode="managed",
        )
    _sessions[session_key] = session
    log.info(
        "browser.session_created",
        session_key=session_key,
        mode=session.mode,
        engine_session=session.engine_session_name,
    )
    return session


def get_session(session_key: str) -> BrowserSession | None:
    return _sessions.get(session_key)


def active_session_count() -> int:
    return len(_sessions)


def reap_idle_sessions(now: float | None = None) -> list[str]:
    """Close sessions idle past the TTL. Returns the closed session keys."""
    now = time.time() if now is None else now
    ttl_seconds = _active_session_ttl_minutes * 60
    stale = [key for key, s in _sessions.items() if now - s.last_used_at > ttl_seconds]
    for key in stale:
        _drop_session(key)
    return stale


def _drop_session(session_key: str) -> None:
    session = _sessions.pop(session_key, None)
    if session is None:
        return
    # The CDP supervisor owns a thread and a live WebSocket; dropping the
    # session without stopping it leaks both. Imported lazily — the supervisor
    # never imports this module, so there is no cycle, and a supervisor-side
    # failure must not prevent the engine teardown below.
    try:
        from agentos.tools.browser_supervisor import SUPERVISOR_REGISTRY

        SUPERVISOR_REGISTRY.stop(session_key)
    except Exception:  # noqa: BLE001 - teardown is best-effort
        log.debug("browser.supervisor_stop_failed", session_key=session_key, exc_info=True)
    # Best-effort engine teardown. In attach mode we only disconnect — never
    # kill the user's own Chrome.
    try:
        if session.mode == "managed":
            _spawn_detached_close(session)
    except Exception:  # noqa: BLE001 - teardown is best-effort
        log.debug("browser.session_close_failed", session_key=session_key, exc_info=True)
    log.info("browser.session_dropped", session_key=session_key, mode=session.mode)


def close_session(session_key: str) -> bool:
    """Public: end one session now. Returns True if it existed."""
    if session_key not in _sessions:
        return False
    _drop_session(session_key)
    return True


def close_all_sessions() -> None:
    """Best-effort teardown of every live session (shutdown / atexit)."""
    for key in list(_sessions):
        _drop_session(key)


# A managed session owns a Chromium that outlives this process unless something
# closes it: without this hook, every run that forgot an explicit `close` left a
# browser behind (observed: 24 orphaned Chromium processes after a demo).
atexit.register(close_all_sessions)


# ---------------------------------------------------------------------------
# Command execution
# ---------------------------------------------------------------------------


def _backend_args(session: BrowserSession) -> list[str]:
    """Engine flags selecting the session's backend, mode, and profile.

    Verified against agent-browser 0.26.0: ``--session`` isolates state,
    ``--cdp <port>`` attaches (int only — never a URL string, which structurally
    rules out remote/cloud CDP), ``--session-name`` persists cookies/storage,
    ``--no-auto-dialog`` hands native dialogs to our supervisor instead of the
    engine's default auto-dismiss, and ``--allowed-domains`` bounds navigation at
    the engine layer (the tool enforces the allowlist too — defense in depth).
    """
    args: list[str] = ["--session", session.engine_session_name, "--json"]
    if session.mode == "attach":
        args += ["--cdp", str(session.cdp_port)]
    else:
        if not _active_headless:
            args.append("--headed")
    # Our supervisor owns dialogs in both modes.
    args.append("--no-auto-dialog")
    if _active_persist_profile:
        args += ["--session-name", session.engine_session_name]
    if _active_allowed_domains:
        args += ["--allowed-domains", ",".join(_active_allowed_domains)]
    return args


def _command_timeout(command: str, session: BrowserSession) -> float:
    if command in {"open", "goto", "navigate"}:
        if not session.first_open_done:
            return FIRST_OPEN_TIMEOUT
        return DEFAULT_NAVIGATE_TIMEOUT
    return DEFAULT_COMMAND_TIMEOUT


async def run_command(
    session_key: str,
    command: str,
    args: list[str] | None = None,
    *,
    timeout: float | None = None,
    max_timeout: float | None = None,
    create: bool = True,
    touch: bool = True,
) -> dict[str, Any]:
    """Run one ``agent-browser`` command and return its parsed JSON result.

    Never raises for an engine-level failure: returns
    ``{"success": False, "error": …}`` so the tool layer maps it to a clean
    ``ToolError`` message. Raises only for programmer errors.

    ``create=False`` runs only against a session that already exists: with
    none, it answers ``{"success": False, "code": "not_running"}`` and spawns
    nothing (any engine command against an unknown session launches a
    browser). ``touch=False`` leaves the session's idle clock alone, so a
    viewer polling the page never keeps a browser alive past its TTL.
    ``max_timeout`` caps whichever timeout applies, so a caller with a deadline
    of its own (a batch) cannot be overrun by one command's default.
    """
    binary = resolve_binary()
    if binary is None:
        return {"success": False, "error": _install_hint()}

    if create:
        session = get_or_create_session(session_key)
    else:
        found = get_session(session_key)
        if found is None:
            return {
                "success": False,
                "code": "not_running",
                "error": "No browser is running for this session.",
            }
        session = found
        if touch:
            session.last_used_at = time.time()
    argv = _command_line(binary, session, command, args or [])
    effective_timeout = timeout if timeout is not None else _command_timeout(command, session)
    if max_timeout is not None:
        effective_timeout = min(effective_timeout, max_timeout)

    try:
        result = await _spawn_and_collect(argv, effective_timeout)
    except TimeoutError:
        return {
            "success": False,
            "error": (
                f"agent-browser '{command}' timed out after {effective_timeout:g}s. "
                "The page may be slow or blocked."
            ),
        }
    except Exception as exc:  # noqa: BLE001 - a tool returns errors, does not raise
        log.warning("browser.command_failed", command=command, error_type=type(exc).__name__)
        return {"success": False, "error": f"agent-browser '{command}' failed: {exc}"}

    if touch:
        session.last_used_at = time.time()
    if command in {"open", "goto", "navigate"}:
        session.first_open_done = True
    return result


def _command_line(
    binary: str,
    session: BrowserSession,
    command: str,
    args: list[str],
) -> list[str]:
    return [binary, *_backend_args(session), command, *args]


async def resolve_cdp_endpoint(session_key: str) -> str | None:
    """Return the CDP endpoint the supervisor can attach to for this session.

    Attach mode: the loopback http endpoint of the operator's Chrome. Managed
    mode: the ws URL agent-browser reports via ``get cdp-url`` (loopback),
    resolved once and cached. Returns ``None`` when no endpoint is available or
    the reported URL is not loopback (which is refused as a safety floor).
    """
    session = get_session(session_key)
    if session is None:
        return None
    if session.mode == "attach":
        endpoint = session.cdp_endpoint()
        if endpoint and not is_loopback_cdp_url(endpoint):
            return None
        return endpoint
    if session.cdp_ws_url:
        return session.cdp_ws_url
    result = await run_command(session_key, "get", ["cdp-url"], timeout=DEFAULT_COMMAND_TIMEOUT)
    if not result.get("success"):
        return None
    data = result.get("data") or {}
    ws_url = str(data.get("cdpUrl") or data.get("cdp_url") or "").strip()
    if not ws_url or not is_loopback_cdp_url(ws_url):
        if ws_url:
            log.warning("browser.non_loopback_cdp_refused", url=redact_cdp_url(ws_url))
        return None
    session.cdp_ws_url = ws_url
    return ws_url


async def _spawn_and_collect(argv: list[str], timeout: float) -> dict[str, Any]:
    """Spawn the engine, enforce *timeout*, and parse stdout as JSON."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        # No command the tool runs reads stdin; an engine that treats a `-`
        # operand as "read from stdin" must not get the gateway's.
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_scrubbed_env(),
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        with contextlib.suppress(Exception):
            proc.kill()
        await _suppress_wait(proc)
        raise

    stdout = stdout_b.decode("utf-8", "replace").strip()
    stderr = stderr_b.decode("utf-8", "replace").strip()

    if not stdout:
        detail = redact_cdp_url(stderr) if stderr else f"exit code {proc.returncode}"
        return {"success": False, "error": f"agent-browser produced no output ({detail})"}

    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        # The engine may print a non-JSON line before/after JSON; try last line.
        last = stdout.splitlines()[-1]
        try:
            parsed = json.loads(last)
        except json.JSONDecodeError:
            return {
                "success": False,
                "error": "agent-browser returned non-JSON output",
                "raw": redact_cdp_url(stdout[:500]),
            }
    if not isinstance(parsed, dict):
        return {"success": False, "error": "agent-browser returned a non-object JSON result"}
    return parsed


def _spawn_detached_close(session: BrowserSession) -> None:
    """Fire-and-forget ``close`` for a managed session (no await available)."""
    binary = resolve_binary()
    if binary is None:
        return
    import subprocess

    argv = [binary, "--session", session.engine_session_name, "--json", "close"]
    with contextlib.suppress(Exception):
        subprocess.Popen(  # noqa: S603 - argv is fully controlled, no shell
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_scrubbed_env(),
        )


def _install_hint() -> str:
    return (
        "agent-browser is not installed. Install it with: "
        "npm install -g agent-browser && agent-browser install"
    )


async def _suppress_wait(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(Exception):
        await proc.wait()


__all__ = [
    "BrowserSession",
    "browser_available",
    "browser_doctor_status",
    "close_all_sessions",
    "close_session",
    "configure_browser",
    "configured_cdp_port",
    "get_or_create_session",
    "get_session",
    "is_attach_mode",
    "is_loopback_cdp_url",
    "persist_profile_enabled",
    "reap_idle_sessions",
    "reset_browser_runtime",
    "resolve_binary",
    "resolve_cdp_endpoint",
    "run_command",
]
