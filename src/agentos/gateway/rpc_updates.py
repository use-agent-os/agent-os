"""Update RPC handlers: check, apply, watch, and verify the data afterwards.

``updates.apply`` runs ``agentos upgrade`` for the operator instead of telling
them to open a terminal. The upgrade restarts this very gateway, so the work
cannot live in this process: it is spawned as a detached job whose progress and
outcome are written to files under the state directory, and ``updates.status``
reads those files — before, during and after the restart, from whichever
gateway process happens to answer.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from agentos import __version__
from agentos.compat import pypi_client, version_utils
from agentos.gateway.access import CONTROL_ONLY
from agentos.gateway.rpc import RpcContext, get_dispatcher
from agentos.paths import state_dir

_d = get_dispatcher()

_JOB_FILE = "upgrade_job.json"
_LOG_FILE = "upgrade_job.log"
_LOG_TAIL_LINES = 40
_SOURCES = ("auto", "pypi", "github")

# Runs detached from the gateway, survives its restart, and records the exit
# code the gateway can no longer wait for. Reads its argv as JSON so no quoting
# of paths ever happens in shell.
_RUNNER = """
import json, subprocess, sys, time
job = json.loads(sys.argv[1])
with open(job["log"], "ab") as log:
    proc = subprocess.run(job["command"], stdout=log, stderr=subprocess.STDOUT, env=job["env"])
state = json.load(open(job["job"], encoding="utf-8"))
state["status"] = "done" if proc.returncode == 0 else "failed"
state["exitCode"] = proc.returncode
state["finishedAt"] = time.time()
tmp = job["job"] + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(state, fh)
import os
os.replace(tmp, job["job"])
"""


def job_path() -> Path:
    return state_dir(_JOB_FILE)


def log_path() -> Path:
    return state_dir(_LOG_FILE)


def _read_job() -> dict[str, Any]:
    try:
        data = json.loads(job_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_job(state: dict[str, Any]) -> None:
    path = job_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, path)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _log_tail() -> list[str]:
    try:
        text = log_path().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = [line.rstrip("\r") for line in text.splitlines() if line.strip()]
    return lines[-_LOG_TAIL_LINES:]


def _last_json_line(lines: list[str]) -> dict[str, Any] | None:
    for line in reversed(lines):
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def agentos_command() -> list[str]:
    """argv that runs *this* install's ``agentos``.

    The console script next to the running interpreter is the same install the
    gateway came from, which is what an upgrade must target; a bare ``agentos``
    on PATH could be a different one. ``python -m`` is the last resort.
    """

    sibling = Path(sys.executable).with_name("agentos.exe" if os.name == "nt" else "agentos")
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return [str(sibling)]
    found = shutil.which("agentos")
    if found:
        return [found]
    return [sys.executable, "-m", "agentos.cli.main"]


def current_status() -> dict[str, Any]:
    """The job as the files describe it, with a liveness check on a running one."""

    job = _read_job()
    status = job.get("status")
    if status not in ("running", "done", "failed"):
        return {"status": "idle"}

    pid = job.get("pid")
    if status == "running" and isinstance(pid, int) and not _pid_alive(pid):
        # The runner died before it could record an exit code (killed, or the
        # machine went down mid-upgrade). Report it rather than spinning.
        job = {**job, "status": "failed", "exitCode": None, "finishedAt": time.time()}
        _write_job(job)

    tail = _log_tail()
    payload: dict[str, Any] = {
        "status": job["status"],
        "pid": pid,
        "source": job.get("source"),
        "startedAt": job.get("startedAt"),
        "finishedAt": job.get("finishedAt"),
        "exitCode": job.get("exitCode"),
        "logTail": tail,
        "result": _last_json_line(tail) if job["status"] != "running" else None,
    }
    return payload


def start_upgrade(*, source: str) -> dict[str, Any]:
    """Spawn the detached upgrade runner; returns the new job status."""

    from agentos.cli.install_method import hardened_path_env

    log = log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        log.unlink()
    except OSError:
        pass

    env = hardened_path_env()
    # The upgrade's own once-a-day notice would land in the log as noise.
    env["AGENTOS_NO_UPDATE_NOTICE"] = "1"
    command = [*agentos_command(), "upgrade", "--json", "--source", source]
    state: dict[str, Any] = {
        "status": "running",
        "pid": None,
        "source": source,
        "startedAt": time.time(),
        "command": command,
    }
    _write_job(state)

    runner_args = json.dumps(
        {"command": command, "env": env, "log": str(log), "job": str(job_path())}
    )
    proc = subprocess.Popen(  # noqa: S603 - argv built internally
        [sys.executable, "-c", _RUNNER, runner_args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=os.name != "nt",
        close_fds=True,
    )
    state["pid"] = proc.pid
    _write_job(state)
    return current_status()


@_d.method("updates.check", audiences=CONTROL_ONLY)
async def _handle_updates_check(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Check for new release availability, returning version info and status.

    Params:
        force: When true, skip the 24h throttle and ask PyPI right now. Meant
          for an explicit "Check for updates" click; the passive banner check
          keeps using the cache.

    Returns:
        A dict containing:
          - current: The currently running version of agent-os
          - latest: The latest version available on PyPI, or None if check is suppressed/failed
          - status: "up-to-date" | "outdated" | "offline"
    """
    config = getattr(ctx, "config", None)
    force = bool((params or {}).get("force", False))

    # 1. Respect preferences: AGENTOS_NO_UPDATE_NOTICE or updates.notify == False
    if os.environ.get(
        "AGENTOS_NO_UPDATE_NOTICE", ""
    ).strip() == "1" or not pypi_client.config_notify_enabled(config):
        return {
            "current": __version__,
            "latest": None,
            "status": "offline",
        }

    now = time.time()
    path = pypi_client.notice_state_path()

    # 2. Check if we need to contact PyPI or use cached state
    latest: str | None = None
    due = force or await asyncio.to_thread(pypi_client.due_for_check, path, now, "webui")
    if due:
        latest = await asyncio.to_thread(pypi_client.latest_version, timeout=2.0)
        # Record/cache the result
        await asyncio.to_thread(pypi_client.write_state, path, now, latest, "webui")
    else:
        state = await asyncio.to_thread(pypi_client.read_state, path)
        latest_val = state.get("latest")
        if isinstance(latest_val, str):
            latest = latest_val

    # 3. Determine status
    if latest is None:
        status = "offline"
    elif version_utils.is_newer(latest, __version__):
        status = "outdated"
    else:
        status = "up-to-date"

    return {
        "current": __version__,
        "latest": latest,
        "status": status,
    }


@_d.method("updates.apply", audiences=CONTROL_ONLY)
async def _handle_updates_apply(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Run ``agentos upgrade`` as a detached job and return its status.

    Params:
        source: "auto" (default), "pypi" or "github" — passed to ``--source``.

    A job already running is returned as-is with ``started: false`` so two
    clicks never race two installers. The job restarts this gateway itself;
    clients should expect a reconnect and keep polling ``updates.status``.
    """

    source = str((params or {}).get("source") or "auto")
    if source not in _SOURCES:
        raise ValueError(f"source must be one of {', '.join(_SOURCES)}")
    current = await asyncio.to_thread(current_status)
    if current.get("status") == "running":
        return {"started": False, **current}
    started = await asyncio.to_thread(start_upgrade, source=source)
    return {"started": True, **started}


@_d.method("updates.status", audiences=CONTROL_ONLY)
async def _handle_updates_status(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """The state of the last ``updates.apply`` job: idle, running, done or failed.

    ``result`` carries the upgrade's final JSON line once it has finished and
    ``logTail`` the last lines of its output either way.
    """

    return await asyncio.to_thread(current_status)


@_d.method("updates.verifyData", audiences=CONTROL_ONLY)
async def _handle_updates_verify_data(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """``PRAGMA quick_check`` every state database this gateway has migrated.

    Returns ``ok``, the paths checked, the problems found, and the newest
    pre-upgrade snapshot (if any) a client can offer to restore.
    """

    from agentos.cli import upgrade_snapshot

    check = await asyncio.to_thread(upgrade_snapshot.verify_state)
    latest = await asyncio.to_thread(upgrade_snapshot.latest_snapshot)
    return {**check.to_payload(), "snapshot": str(latest) if latest else None}
