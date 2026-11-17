"""Harness subprocess launch, env scrubbing, process leases and the reaper.

Every ACP harness runs in its own process group (``start_new_session``) so a
cancel or a gateway shutdown can take down the adapter *and* whatever it
spawned (``npx`` -> node -> the real CLI). Before spawn a lease file is
written under ``<state>/acp/leases/<lease_id>.json``; it is removed on clean
exit. At gateway boot :func:`reap_stale_leases` kills whatever an earlier
gateway left behind, but only after re-checking that the live pid still runs
the command the lease recorded (pids get reused).

Design follows OpenClaw's acpx ``process-lease.ts`` / ``process-reaper.ts`` /
``codex-trust-config.ts`` (MIT, see ``THIRD_PARTY_NOTICES.md``), rewritten in
Python. Third-party adapters cannot take an extra argv marker, so the lease id
travels in the env (``AGENTOS_ACP_LEASE``) and the reaper matches on lease
pid + command + process start time instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import tomllib
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
import tomli_w

from agentos.acp.registry import ResolvedLaunch
from agentos.acp.types import AcpSessionInitError
from agentos.env_policy import SKILL_SIGNING_KEY_ENV

_log = structlog.get_logger("agentos.acp.process")

LEASE_ENV = "AGENTOS_ACP_LEASE"
STDERR_RING_BYTES = 256 * 1024
STDIO_LIMIT_BYTES = 50 * 1024 * 1024  # matches acp.core.DEFAULT_STDIO_BUFFER_LIMIT_BYTES
_START_TIME_SLACK_S = 10.0
_IS_POSIX = sys.platform != "win32"

# --------------------------------------------------------------------------
# Env scrubbing
# --------------------------------------------------------------------------

_ALWAYS_KEEP = frozenset({"PATH", "HOME", "LANG", "TERM", "USER", "LOGNAME", "SHELL", "TMPDIR"})
# Harness credentials pass through even though they look like secrets: the
# harness needs them to talk to its own provider.
_HARNESS_CREDENTIALS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "CODEX_HOME",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "CURSOR_API_KEY",
    }
)
_DROP_PATTERN = re.compile(
    r"^(AGENTOS_.*|GATEWAY_.*|.*_TOKEN|.*_SECRET|.*_PASSWORD|.*_BOT_TOKEN|.*_SIGNING_SECRET)$",
    re.IGNORECASE,
)


def scrub_env(
    base: Mapping[str, str] | None = None,
    *,
    keep: frozenset[str] | set[str] = frozenset(),
    drop: frozenset[str] | set[str] = frozenset(),
) -> dict[str, str]:
    """Return a copy of ``base`` (default ``os.environ``) safe for a harness.

    Drops ``AGENTOS_*``, ``GATEWAY_*`` and anything ending in ``_TOKEN`` /
    ``_SECRET`` / ``_PASSWORD`` (gateway tokens, channel bot tokens, vault
    passwords). Keeps the basics (``PATH``/``HOME``/``LANG``/``TERM``) and
    the harnesses' own credentials (``ANTHROPIC_API_KEY``,
    ``OPENAI_API_KEY``, ``CLAUDE_CODE_OAUTH_TOKEN``, ...). ``keep`` /
    ``drop`` adjust the result per agent (``drop`` wins).
    """
    src = dict(os.environ if base is None else base)
    out: dict[str, str] = {}
    for key, value in src.items():
        upper = key.upper()
        if key in drop or upper in SKILL_SIGNING_KEY_ENV:
            # The skills' hot-wallet keys reach only their gated entry point
            # (tools/skill_signing.py); ``keep`` cannot bring one back either.
            continue
        if upper in _ALWAYS_KEEP or upper in _HARNESS_CREDENTIALS or key in keep:
            out[key] = value
            continue
        if _DROP_PATTERN.match(key):
            continue
        out[key] = value
    return out


def harness_env(
    *,
    lease_id: str,
    base: Mapping[str, str] | None = None,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Scrubbed env plus the lease marker and a bounded tokio pool.

    ``extra`` (per-agent override env, then per-spawn env) is merged last and
    is NOT scrubbed: it is operator config, applied deliberately.
    """
    env = scrub_env(base)
    env[LEASE_ENV] = lease_id
    env.setdefault("TOKIO_WORKER_THREADS", str(min(os.cpu_count() or 1, 8)))
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    env[LEASE_ENV] = lease_id
    return env


# --------------------------------------------------------------------------
# Atomic JSON helpers
# --------------------------------------------------------------------------


def atomic_write_text(path: Path, text: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            with contextlib.suppress(OSError):
                os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True, default=str) + "\n")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# --------------------------------------------------------------------------
# Leases
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Lease:
    lease_id: str
    argv: list[str]
    cwd: str
    session_key: str
    gateway_instance_id: str
    pid: int | None = None
    pgid: int | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    record_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "session_key": self.session_key,
            "gateway_instance_id": self.gateway_instance_id,
            "pid": self.pid,
            "pgid": self.pgid,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "record_id": self.record_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Lease | None:
        try:
            pid = data.get("pid")
            pgid = data.get("pgid")
            started = data.get("started_at")
            return cls(
                lease_id=str(data["lease_id"]),
                argv=[str(a) for a in data.get("argv") or []],
                cwd=str(data.get("cwd") or ""),
                session_key=str(data.get("session_key") or ""),
                gateway_instance_id=str(data.get("gateway_instance_id") or ""),
                pid=int(pid) if pid is not None else None,
                pgid=int(pgid) if pgid is not None else None,
                created_at=float(data.get("created_at") or 0.0),
                started_at=float(started) if started is not None else None,
                record_id=(str(data["record_id"]) if data.get("record_id") else None),
            )
        except (KeyError, TypeError, ValueError):
            return None


def leases_dir(state_dir: Path) -> Path:
    return Path(state_dir) / "leases"


def lease_path(state_dir: Path, lease_id: str) -> Path:
    return leases_dir(state_dir) / f"{_safe_name(lease_id)}.json"


def write_lease(state_dir: Path, lease: Lease) -> Path:
    path = lease_path(state_dir, lease.lease_id)
    atomic_write_json(path, lease.to_dict())
    return path


def read_lease(state_dir: Path, lease_id: str) -> Lease | None:
    data = _read_json(lease_path(state_dir, lease_id))
    return Lease.from_dict(data) if data else None


def remove_lease(state_dir: Path, lease_id: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        lease_path(state_dir, lease_id).unlink()


def list_leases(state_dir: Path) -> list[Lease]:
    d = leases_dir(state_dir)
    if not d.is_dir():
        return []
    out: list[Lease] = []
    for p in sorted(d.glob("*.json")):
        data = _read_json(p)
        lease = Lease.from_dict(data) if data else None
        if lease is not None:
            out.append(lease)
    return out


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", value)
    return cleaned.strip(".") or "_"


# --------------------------------------------------------------------------
# Stderr ring
# --------------------------------------------------------------------------


class StderrRing:
    """Redacted, size-bounded stderr capture for one harness process.

    Lines are redacted (``agentos.redact``, forced) before they touch disk.
    When the file passes ``max_bytes`` the oldest half is dropped. The last
    few lines are also kept in memory for error messages.
    """

    def __init__(self, path: Path, *, max_bytes: int = STDERR_RING_BYTES, tail_lines: int = 40):
        self.path = path
        self.max_bytes = max_bytes
        self._tail: deque[str] = deque(maxlen=tail_lines)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._size = path.stat().st_size if path.exists() else 0

    def tail(self, n: int = 20) -> str:
        return "\n".join(list(self._tail)[-n:])

    def write_line(self, raw: bytes) -> None:
        from agentos.redact import redact_sensitive_text

        text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        text = redact_sensitive_text(text, force=True) or ""
        self._tail.append(text)
        data = (text + "\n").encode("utf-8")
        try:
            with open(self.path, "ab") as fh:
                fh.write(data)
            self._size += len(data)
            if self._size > self.max_bytes:
                self._trim()
        except OSError:
            pass

    def _trim(self) -> None:
        try:
            blob = self.path.read_bytes()
        except OSError:
            return
        keep = blob[-(self.max_bytes // 2) :]
        nl = keep.find(b"\n")
        if 0 <= nl < len(keep) - 1:
            keep = keep[nl + 1 :]
        atomic_write_text(self.path, keep.decode("utf-8", errors="replace"), mode=0o600)
        self._size = len(keep)

    async def pump(self, reader: asyncio.StreamReader) -> None:
        while True:
            try:
                line = await reader.readline()
            except (ValueError, asyncio.LimitOverrunError):
                # Over-long line: drop what is buffered and keep going.
                with contextlib.suppress(Exception):
                    await reader.read(64 * 1024)
                continue
            except Exception:
                return
            if not line:
                return
            self.write_line(line)


# --------------------------------------------------------------------------
# Launch / terminate
# --------------------------------------------------------------------------


@dataclass(slots=True)
class HarnessProcess:
    proc: asyncio.subprocess.Process
    argv: list[str]
    cwd: str
    lease_id: str
    lease: Lease
    state_dir: Path
    stderr: StderrRing
    started_at: float
    stderr_task: asyncio.Task[None] | None = None

    @property
    def pid(self) -> int:
        return int(self.proc.pid)

    @property
    def returncode(self) -> int | None:
        return self.proc.returncode

    @property
    def alive(self) -> bool:
        return self.proc.returncode is None

    @property
    def stdin(self) -> asyncio.StreamWriter:
        assert self.proc.stdin is not None
        return self.proc.stdin

    @property
    def stdout(self) -> asyncio.StreamReader:
        assert self.proc.stdout is not None
        return self.proc.stdout

    def stderr_tail(self, n: int = 20) -> str:
        return self.stderr.tail(n)

    async def wait(self, timeout: float | None = None) -> int | None:
        try:
            return await asyncio.wait_for(self.proc.wait(), timeout)
        except TimeoutError:
            return None

    def release_lease(self) -> None:
        remove_lease(self.state_dir, self.lease_id)


async def launch_harness(
    resolved: ResolvedLaunch,
    *,
    cwd: str,
    env: Mapping[str, str],
    state_dir: Path,
    lease_id: str,
    session_key: str = "",
    gateway_instance_id: str = "",
    record_id: str | None = None,
) -> HarnessProcess:
    """Spawn ``resolved.argv`` in its own process group with piped stdio.

    The lease file is written *before* the spawn (so a crash between spawn
    and bookkeeping still leaves a trace for the reaper) and updated with
    the pid right after. ``env`` should come from :func:`harness_env`.
    """
    if not resolved.argv:
        raise AcpSessionInitError(
            f"no launch command for ACP agent {resolved.agent_id!r}", code="ACP_HARNESS_MISSING"
        )
    state_dir = Path(state_dir)
    lease = Lease(
        lease_id=lease_id,
        argv=list(resolved.argv),
        cwd=cwd,
        session_key=session_key,
        gateway_instance_id=gateway_instance_id,
        record_id=record_id,
    )
    write_lease(state_dir, lease)
    log_name = _safe_name(record_id or lease_id)
    ring = StderrRing(state_dir / "logs" / f"{log_name}.stderr.log")
    child_env = dict(env)
    child_env[LEASE_ENV] = lease_id
    kwargs: dict[str, Any] = {}
    if _IS_POSIX:
        kwargs["start_new_session"] = True
    try:
        proc = await asyncio.create_subprocess_exec(
            *resolved.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=child_env,
            limit=STDIO_LIMIT_BYTES,
            **kwargs,
        )
    except (OSError, ValueError) as exc:
        remove_lease(state_dir, lease_id)
        raise AcpSessionInitError(
            f"could not start ACP agent {resolved.agent_id!r} ({resolved.argv[0]}): {exc}",
            code="ACP_SPAWN_FAILED",
            argv=list(resolved.argv),
        ) from exc
    started = time.time()
    lease.pid = proc.pid
    lease.pgid = proc.pid if _IS_POSIX else None
    lease.started_at = started
    write_lease(state_dir, lease)
    hp = HarnessProcess(
        proc=proc,
        argv=list(resolved.argv),
        cwd=cwd,
        lease_id=lease_id,
        lease=lease,
        state_dir=state_dir,
        stderr=ring,
        started_at=started,
    )
    if proc.stderr is not None:
        hp.stderr_task = asyncio.create_task(ring.pump(proc.stderr), name=f"acp-stderr-{lease_id}")
    _log.info(
        "acp.process.spawned",
        agent_id=resolved.agent_id,
        pid=proc.pid,
        lease_id=lease_id,
        source=resolved.source,
        argv0=resolved.argv[0],
    )
    return hp


def _signal_group(pid: int, sig: int) -> bool:
    """Signal the process group led by ``pid`` (falls back to the pid)."""
    if _IS_POSIX:
        try:
            os.killpg(pid, sig)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            pass
    try:
        os.kill(pid, sig)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


async def terminate(proc: HarnessProcess, grace_s: float = 2.0) -> int | None:
    """SIGTERM the harness process group, wait ``grace_s``, then SIGKILL.

    Always finishes with a group SIGKILL so grandchildren that ignored the
    TERM (or outlived their parent) do not linger. Releases the lease.
    """
    pid = proc.pid
    if proc.alive:
        _signal_group(pid, signal.SIGTERM)
        rc = await proc.wait(grace_s)
        if rc is None:
            _log.warning("acp.process.kill", pid=pid, lease_id=proc.lease_id)
            _signal_group(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            await proc.wait(2.0)
    # Sweep stragglers left in the group.
    if _IS_POSIX:
        _signal_group(pid, signal.SIGKILL)
    await _finish_stderr(proc)
    proc.release_lease()
    return proc.returncode


async def close_gracefully(
    proc: HarnessProcess, *, wait_s: float = 1.0, grace_s: float = 0.75
) -> int | None:
    """Close stdin, give the harness ``wait_s`` to exit, then :func:`terminate`.

    Exit code 0 after a stdin close is a clean exit (Codex logs an
    ``AppServerUnavailableError`` on the way out; that is expected).
    """
    if proc.alive:
        with contextlib.suppress(Exception):
            proc.stdin.close()
        rc = await proc.wait(wait_s)
        if rc is not None:
            if _IS_POSIX:
                _signal_group(proc.pid, signal.SIGKILL)
            await _finish_stderr(proc)
            proc.release_lease()
            return rc
    return await terminate(proc, grace_s=grace_s)


async def _finish_stderr(proc: HarnessProcess) -> None:
    task = proc.stderr_task
    if task is None or task.done():
        return
    try:
        await asyncio.wait_for(asyncio.shield(task), 0.5)
    except (TimeoutError, asyncio.CancelledError, Exception):
        task.cancel()


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
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


# --------------------------------------------------------------------------
# Reaper
# --------------------------------------------------------------------------


@dataclass(slots=True)
class ProcInfo:
    pid: int
    ppid: int
    command: str


def _ps_env() -> dict[str, str]:
    """Environment for ``ps``: no ``COLUMNS``, so output width never follows the caller's tty.

    ``ps`` truncates the command column to ``COLUMNS``; a cut-off command line
    would make :func:`command_matches_lease` mistake a stale harness for a
    reused PID and leave it running.
    """
    env = dict(os.environ)
    env.pop("COLUMNS", None)
    return env


def list_processes() -> list[ProcInfo] | None:
    """``ps -axwwo pid=,ppid=,command=`` parsed; ``None`` when unavailable.

    ``ww`` (unlimited width, on both procps and BSD ``ps``) keeps the full
    command line, which the reaper matches against lease argv.
    """
    if not _IS_POSIX:
        return None
    try:
        out = subprocess.run(
            ["ps", "-axwwo", "pid=,ppid=,command="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=_ps_env(),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    rows: list[ProcInfo] = []
    for line in out.splitlines():
        m = re.match(r"^\s*(\d+)\s+(\d+)\s+(.+?)\s*$", line)
        if m:
            rows.append(ProcInfo(int(m.group(1)), int(m.group(2)), m.group(3)))
    return rows


def _process_elapsed_s(pid: int) -> float | None:
    try:
        out = subprocess.run(
            ["ps", "-o", "etime=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=_ps_env(),
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_etime(out)


def parse_etime(value: str) -> float | None:
    """Parse ``ps`` ``etime`` (``[[dd-]hh:]mm:ss``) into seconds."""
    value = value.strip()
    if not value:
        return None
    days = 0
    if "-" in value:
        d, value = value.split("-", 1)
        try:
            days = int(d)
        except ValueError:
            return None
    parts = value.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    seconds = 0
    for n in nums:
        seconds = seconds * 60 + n
    return float(days * 86400 + seconds)


def _process_env_has_marker(pid: int, lease_id: str) -> bool | None:
    """True/False when the env can be read, ``None`` when it cannot."""
    marker = f"{LEASE_ENV}={lease_id}"
    proc_env = Path(f"/proc/{pid}/environ")
    if proc_env.exists():
        try:
            return marker.encode() in proc_env.read_bytes().split(b"\0")
        except OSError:
            return None
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["ps", "-E", "-ww", "-o", "command=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                env=_ps_env(),
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        if not out.strip():
            return None
        if marker in out.split():
            return True
        # ps only shows env for processes we own; absence is not proof.
        return False if f"{LEASE_ENV}=" in out else None
    return None


def command_matches_lease(command: str, argv: list[str]) -> bool:
    """Does a live ``ps`` command line plausibly belong to ``argv``?

    Launchers rewrite their display (``npx`` shows as ``npm exec <pkg>``),
    so any distinctive argv token (the binary's basename or a non-flag
    argument of 4+ chars) appearing in the command counts.
    """
    if not command or not argv:
        return False
    tokens: list[str] = []
    first = os.path.basename(argv[0])
    if first:
        tokens.append(first)
    for arg in argv[1:]:
        if arg.startswith("-") or len(arg) < 4:
            continue
        tokens.append(arg)
        tokens.append(os.path.basename(arg))
    return any(t and t in command for t in tokens)


def _descendants(root: int, procs: list[ProcInfo]) -> list[int]:
    """Descendants of ``root``, deepest first (leaves before parents)."""
    children: dict[int, list[int]] = {}
    for p in procs:
        children.setdefault(p.ppid, []).append(p.pid)
    order: list[tuple[int, int]] = []
    stack = [(root, 0)]
    seen = {root}
    while stack:
        pid, depth = stack.pop()
        for c in children.get(pid, []):
            if c not in seen:
                seen.add(c)
                order.append((c, depth + 1))
                stack.append((c, depth + 1))
    order.sort(key=lambda item: -item[1])
    return [pid for pid, _ in order]


async def reap_stale_leases(
    state_dir: Path,
    *,
    current_instance_id: str | None = None,
    grace_s: float = 0.75,
) -> dict[str, Any]:
    """Kill harness trees an earlier gateway left behind; clear their leases.

    For each lease not owned by ``current_instance_id``: if its pid is gone
    or now runs something else (command mismatch, start time far from the
    lease, or a readable env without our marker) the lease file is dropped
    and nothing is killed. Otherwise the tree is SIGTERMed leaves-first and
    whatever survives ``grace_s`` gets SIGKILL.
    """
    result: dict[str, Any] = {"inspected": [], "terminated": [], "removed": [], "skipped": []}
    leases = [
        lease
        for lease in list_leases(state_dir)
        if not (current_instance_id and lease.gateway_instance_id == current_instance_id)
    ]
    if not leases:
        return result
    procs = await asyncio.to_thread(list_processes)
    if procs is None:
        result["skipped"].append("process-list-unavailable")
        return result
    by_pid = {p.pid: p for p in procs}
    to_kill: list[int] = []
    for lease in leases:
        result["inspected"].append(lease.lease_id)
        pid = lease.pid
        info = by_pid.get(pid) if pid else None
        if not pid or info is None:
            remove_lease(state_dir, lease.lease_id)
            result["removed"].append(lease.lease_id)
            continue
        if not command_matches_lease(info.command, lease.argv):
            _log.info("acp.reaper.pid_reused", lease_id=lease.lease_id, pid=pid)
            remove_lease(state_dir, lease.lease_id)
            result["removed"].append(lease.lease_id)
            continue
        if lease.started_at:
            elapsed = await asyncio.to_thread(_process_elapsed_s, pid)
            if elapsed is not None:
                proc_start = time.time() - elapsed
                if abs(proc_start - lease.started_at) > _START_TIME_SLACK_S:
                    _log.info("acp.reaper.start_mismatch", lease_id=lease.lease_id, pid=pid)
                    remove_lease(state_dir, lease.lease_id)
                    result["removed"].append(lease.lease_id)
                    continue
        marker = await asyncio.to_thread(_process_env_has_marker, pid, lease.lease_id)
        if marker is False:
            _log.info("acp.reaper.marker_mismatch", lease_id=lease.lease_id, pid=pid)
            remove_lease(state_dir, lease.lease_id)
            result["removed"].append(lease.lease_id)
            continue
        tree = [*_descendants(pid, procs), pid]
        for victim in tree:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.kill(victim, signal.SIGTERM)
        to_kill.extend(tree)
        result["terminated"].extend(tree)
        remove_lease(state_dir, lease.lease_id)
        result["removed"].append(lease.lease_id)
        _log.info("acp.reaper.terminated", lease_id=lease.lease_id, pid=pid, tree=len(tree))
    if to_kill:
        deadline = time.monotonic() + grace_s
        while time.monotonic() < deadline and any(pid_alive(p) for p in to_kill):
            await asyncio.sleep(0.05)
        for victim in to_kill:
            if pid_alive(victim):
                with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                    os.kill(victim, getattr(signal, "SIGKILL", signal.SIGTERM))
    return result


# --------------------------------------------------------------------------
# Codex home isolation (OpenClaw codex-trust-config.ts)
# --------------------------------------------------------------------------

_CODEX_TOP_LEVEL_KEYS = ("model", "model_provider", "model_reasoning_effort", "sandbox_mode")
_CODEX_PROVIDER_KEYS = frozenset(
    {
        "name",
        "base_url",
        "wire_api",
        "env_key",
        "env_key_instructions",
        "requires_openai_auth",
        "request_max_retries",
        "stream_max_retries",
        "stream_idle_timeout_ms",
    }
)


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise AcpSessionInitError(
            f"Codex config.toml at {path} must be valid TOML before starting an ACP session",
            code="ACP_CODEX_CONFIG_INVALID",
        ) from exc


def _trusted_projects(cfg: Mapping[str, Any]) -> list[str]:
    projects = cfg.get("projects")
    if not isinstance(projects, dict):
        return []
    return [
        str(p)
        for p, v in projects.items()
        if isinstance(v, dict) and v.get("trust_level") == "trusted"
    ]


def render_isolated_codex_config(source: Mapping[str, Any], project_paths: list[str]) -> str:
    """Inherit only model/provider/sandbox keys; mark ``project_paths`` trusted."""
    inherited: dict[str, Any] = {k: source[k] for k in _CODEX_TOP_LEVEL_KEYS if k in source}
    providers = source.get("model_providers")
    if isinstance(providers, dict):
        kept: dict[str, Any] = {}
        for name, prov in providers.items():
            if not isinstance(prov, dict):
                continue
            cfg = {k: v for k, v in prov.items() if k in _CODEX_PROVIDER_KEYS}
            if cfg:
                kept[str(name)] = cfg
        if kept:
            inherited["model_providers"] = kept
    paths = sorted(
        {
            os.path.abspath(os.path.expanduser(p.strip()))
            for p in [*project_paths, *_trusted_projects(source)]
            if p and p.strip()
        }
    )
    if paths:
        inherited["projects"] = {p: {"trust_level": "trusted"} for p in paths}
    body = tomli_w.dumps(inherited)
    return "# Generated by AgentOS for Codex ACP sessions.\n" + body


def prepare_codex_home(
    state_dir: Path,
    cwd: str,
    *,
    user_codex_home: Path | None = None,
) -> Path:
    """Build ``<state>/codex-home`` for an isolated Codex harness.

    Copies ``auth.json`` from the user's Codex home (``$CODEX_HOME`` or
    ``~/.codex``) when present, and writes a ``config.toml`` that inherits
    only ``model``, ``model_provider``, ``model_reasoning_effort``,
    ``sandbox_mode`` and ``[model_providers.*]`` and marks ``cwd`` (plus any
    project already trusted there or by an earlier session) as trusted.
    """
    home = Path(state_dir) / "codex-home"
    home.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(home, 0o700)
    if user_codex_home is None:
        env_home = os.environ.get("CODEX_HOME", "").strip()
        user_codex_home = Path(env_home).expanduser() if env_home else Path.home() / ".codex"
    user_codex_home = Path(user_codex_home)
    if user_codex_home.resolve() == home.resolve():
        raise AcpSessionInitError(
            "CODEX_HOME already points at the isolated AgentOS codex home",
            code="ACP_CODEX_CONFIG_INVALID",
        )
    auth = user_codex_home / "auth.json"
    if auth.is_file():
        dest = home / "auth.json"
        tmp = dest.with_name(f".auth.json.{os.getpid()}.tmp")
        shutil.copyfile(auth, tmp)
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
    source = _load_toml(user_codex_home / "config.toml")
    existing: dict[str, Any] = {}
    with contextlib.suppress(AcpSessionInitError):
        existing = _load_toml(home / "config.toml")
    projects = [cwd, *_trusted_projects(existing)]
    atomic_write_text(home / "config.toml", render_isolated_codex_config(source, projects))
    return home
