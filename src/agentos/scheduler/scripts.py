"""Script execution for cron jobs.

Two cron modes run a file from here:

* a ``script`` job — the one cron mode that never reaches a model. The
  scheduler runs the file on schedule and delivers its stdout. No prompt, no
  turn runner, no tokens. It covers the watchdog shape: poll something, print
  a line when it matters, stay quiet otherwise.
* an ``agent_turn`` job with a pre-run script — the file runs first and its
  stdout becomes context for the turn, and it can call the turn off entirely
  when there is nothing to look at. Same watchdog shape, but a model reads the
  finding instead of the user.

Because nothing reviews the command before it runs, the trust boundary is
the *directory*: scripts must live under ``<agentos home>/scripts``.
Relative paths resolve there and absolute paths are accepted only when they
land inside it, so neither ``../`` nor a symlink can point the scheduler at
an arbitrary file. Callers that accept a path from a model (the ``cron``
tool, the RPC surface) reject absolute paths outright via
:func:`validate_script_path` — the containment check here is the backstop,
not the only gate.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import structlog

from agentos.paths import default_agentos_home
from agentos.redact import redact_sensitive_text

log = structlog.get_logger(__name__)

#: Delivered output is capped so a runaway script cannot push a multi-megabyte
#: message into a channel. The tail is dropped: watchdogs print their verdict
#: first.
MAX_SCRIPT_OUTPUT_CHARS = 16_000

_SHELL_SUFFIXES = frozenset({".sh", ".bash"})

#: Stands in for the owning job's id inside a script path. A job that keeps its
#: files in a directory named after itself cannot write that name at creation
#: time, because the id is minted by the create. Without the placeholder the
#: only way there is to stage the script somewhere else, create the job against
#: the staging path, move the file, and repoint the job — four steps during
#: which a live job points at a path it will not keep, and any abandoned run
#: leaves files behind that nothing can attribute.
JOB_ID_PLACEHOLDER = "{job_id}"

#: What the placeholder resolves to while a path is being validated, before any
#: job exists. Shaped like a real id so containment is checked against a path
#: the same length and depth as the one that will actually run.
_PLACEHOLDER_PROBE = "00000000-0000-0000-0000-000000000000"

#: A job id becomes a path segment, so it may only hold characters that cannot
#: reshape the path. Real ids are uuid4; this is the backstop for the one place
#: a value is spliced into a path rather than resolved as one.
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class ScriptPathError(ValueError):
    """Raised when a job's script path is missing or escapes the scripts dir."""


def scripts_dir() -> Path:
    """Return the directory cron job scripts must live in."""
    return default_agentos_home() / "scripts"


def normalize_script_value(script: str | None) -> str:
    """Trim a script path as written and drop one layer of matching quotes.

    A model asked for a script job routinely passes ``"watch.sh"`` with the
    quotes still attached. A path whose first and last characters are the same
    quote is never a real file name, so accepting it verbatim only buys a job
    that stores cleanly and then fails at fire time with a confusing path.
    """
    raw = (script or "").strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        raw = raw[1:-1].strip()
    return raw


def substitute_job_id(script: str, job_id: str) -> str:
    """Return *script* with every :data:`JOB_ID_PLACEHOLDER` replaced by *job_id*.

    A path with no placeholder is returned as written, so this is safe to run
    over every script path rather than only the ones that opted in.

    Raises:
        ScriptPathError: *job_id* is not id-shaped, and splicing it into a path
            would change that path's shape rather than name a directory in it.
    """
    if JOB_ID_PLACEHOLDER not in (script or ""):
        return script
    if not _JOB_ID_RE.match(job_id or ""):
        raise ScriptPathError(f"Blocked: {job_id!r} cannot name a directory in a script path")
    return script.replace(JOB_ID_PLACEHOLDER, job_id)


def resolve_script_path(script: str) -> Path:
    """Resolve *script* to an absolute path inside :func:`scripts_dir`.

    Relative paths resolve under the scripts directory. Absolute paths are
    resolved as given and then checked for containment, so an operator-written
    absolute path to a real script still works while traversal and symlink
    escapes do not.

    Raises:
        ScriptPathError: the path is empty, still holds an unresolved
            :data:`JOB_ID_PLACEHOLDER`, or resolves outside the scripts dir.
    """
    raw = normalize_script_value(script)
    if not raw:
        raise ScriptPathError("script path is required")
    if JOB_ID_PLACEHOLDER in raw:
        # Resolving it literally would make a directory actually called
        # "{job_id}" and then report the file missing, which reads as a typo
        # rather than as a job that was persisted before substitution ran.
        raise ScriptPathError(
            f"Blocked: script path still holds {JOB_ID_PLACEHOLDER}; it is replaced "
            f"with the job's id when the job is created: {raw!r}"
        )

    base = scripts_dir()
    base.mkdir(parents=True, exist_ok=True)
    base_resolved = base.resolve()

    candidate = Path(raw).expanduser()
    resolved = (
        candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()
    )
    try:
        resolved.relative_to(base_resolved)
    except ValueError:
        raise ScriptPathError(
            f"Blocked: script path resolves outside {base_resolved}: {raw!r}"
        ) from None
    return resolved


def validate_script_path(script: str | None) -> str | None:
    """Validate a script path at an API boundary.

    Returns an error string when the path is unusable, else ``None``. Absolute
    and ``~``-prefixed paths are refused here even though
    :func:`resolve_script_path` would accept a contained one: a path arriving
    from a model or an HTTP client should name a file, not a location.

    Existence is deliberately not checked — a job may be scheduled before its
    script is written, and a missing file surfaces as a delivered run error.

    A :data:`JOB_ID_PLACEHOLDER` is allowed and stands in for one path segment
    while the containment check runs: the path has to pass this gate before any
    job exists to substitute into it.
    """
    raw = normalize_script_value(script)
    if not raw:
        return None  # empty means "no script" / "clear the field"

    if raw.startswith(("/", "~", "\\")) or (len(raw) >= 2 and raw[1] == ":"):
        return (
            f"Script path must be relative to {scripts_dir()}. Got {raw!r} — "
            "place the script in that directory and pass just the file name."
        )
    try:
        resolve_script_path(raw.replace(JOB_ID_PLACEHOLDER, _PLACEHOLDER_PROBE))
    except ScriptPathError as exc:
        return str(exc)
    return None


def _resolve_workdir(workdir: str, fallback: Path) -> str:
    """Return the directory a job's script runs in.

    *fallback* is the script's own directory — the documented default. A
    relative *workdir* is joined onto it, the same rule
    :func:`resolve_script_path` applies to the script itself; tested as
    written it would name a path under the gateway process's CWD, which is
    wherever the operator happened to start the service. Absolute paths are
    used as given, and a workdir that does not exist falls back with a warning
    that names where it looked.
    """
    candidate = (workdir or "").strip()
    if not candidate:
        return str(fallback)
    expanded = Path(candidate).expanduser()
    if not expanded.is_absolute():
        expanded = fallback / expanded
    if not expanded.is_dir():
        log.warning("cron.script.workdir_missing", workdir=candidate, resolved=str(expanded))
        return str(fallback)
    return str(expanded)


def _read_pyvenv_cfg(venv_dir: Path) -> dict[str, str]:
    try:
        lines = (venv_dir / "pyvenv.cfg").read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    parsed: dict[str, str] = {}
    for raw in lines:
        if "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        parsed[key.strip().lower()] = value.strip()
    return parsed


def _python_invocation(python_exe: str) -> tuple[str, dict[str, str]]:
    """Return the Python to run scripts with, plus any env it needs.

    On Windows a uv-created venv ``python.exe`` is a launcher that re-execs the
    base interpreter, which flashes a console window even with
    ``CREATE_NO_WINDOW``. Run the base interpreter directly instead and overlay
    the venv paths so the script still imports what the gateway can. Everywhere
    else the running interpreter is already the right one.
    """
    if sys.platform != "win32":
        return python_exe, {}

    interpreter = Path(python_exe)
    if interpreter.name.lower() == "pythonw.exe":
        # pythonw has no stdout to capture.
        sibling = interpreter.with_name("python.exe")
        if sibling.exists():
            interpreter = sibling

    venv_dir = interpreter.parent.parent
    cfg = _read_pyvenv_cfg(venv_dir)
    home = cfg.get("home", "")
    site_packages = venv_dir / "Lib" / "site-packages"
    if "uv" not in cfg or not home:
        return str(interpreter), {}

    base_python = Path(home) / "python.exe"
    if not (base_python.exists() and site_packages.exists()):
        return str(interpreter), {}

    env_overlay = {"VIRTUAL_ENV": str(venv_dir)}
    pythonpath = [str(site_packages)]
    existing = os.environ.get("PYTHONPATH", "")
    if existing:
        pythonpath.append(existing)
    env_overlay["PYTHONPATH"] = os.pathsep.join(pythonpath)
    return str(base_python), env_overlay


def _platform_popen_kwargs() -> dict[str, Any]:
    """Hide the child's console window on Windows; nothing to add elsewhere."""
    if sys.platform != "win32":
        return {}
    return {"creationflags": int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))}


def _interpreter(path: Path) -> tuple[list[str], dict[str, str], str | None]:
    """Return ``(argv, env_overlay, error)`` for running *path*.

    Extension picks the interpreter — the shebang is deliberately ignored so
    the set of things cron will exec stays small and readable.
    """
    if path.suffix.lower() in _SHELL_SUFFIXES:
        bash = shutil.which("bash") or ("/bin/bash" if os.path.isfile("/bin/bash") else None)
        if bash is None:
            return [], {}, (
                f"Cannot run {path.name!r}: bash was not found on PATH. "
                "Rewrite the script in Python (.py) or install bash."
            )
        return [bash, str(path)], {}, None
    python_exe, env_overlay = _python_invocation(sys.executable)
    return [python_exe, str(path)], env_overlay, None


def _clip(text: str) -> str:
    if len(text) <= MAX_SCRIPT_OUTPUT_CHARS:
        return text
    return text[:MAX_SCRIPT_OUTPUT_CHARS] + "\n…[output truncated]"


def _redact(text: str) -> str:
    try:
        return redact_sensitive_text(text) or ""
    except Exception:
        log.warning("cron.script.redaction_failed", exc_info=True)
        return "[REDACTED — redaction failed]"


async def run_job_script(
    script: str,
    *,
    timeout: float,
    workdir: str = "",
    args: Sequence[str] = (),
) -> tuple[bool, str]:
    """Run a cron job's script and capture its output.

    *args* are passed to the script as argv, exec'd directly with no shell, so
    a value containing spaces or shell metacharacters arrives as one argument
    and cannot start a second command.

    Returns ``(success, output)``. On failure *output* is the error text so the
    job can deliver it — a watchdog that breaks silently is worse than one that
    reports the break. stdout and stderr are redacted and clipped before they
    leave this function.
    """
    try:
        path = resolve_script_path(script)
    except ScriptPathError as exc:
        return False, str(exc)

    if not path.exists():
        return False, f"Script not found: {path}"
    if not path.is_file():
        return False, f"Script path is not a file: {path}"

    argv, env_overlay, interpreter_error = _interpreter(path)
    if interpreter_error:
        return False, interpreter_error
    argv = [*argv, *(str(arg) for arg in args)]

    # Imported here, not at module scope: the tool package imports this module
    # to validate script paths, so a top-level import would close a cycle.
    from agentos.tools.env_passthrough import build_subprocess_env

    cwd = _resolve_workdir(workdir, path.parent)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=build_subprocess_env(extra=env_overlay),
            **_platform_popen_kwargs(),
        )
    except Exception as exc:
        return False, f"Script execution failed: {exc}"

    try:
        raw_stdout, raw_stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        # Reap the killed child so the event loop does not warn about a
        # pending transport on the next GC pass.
        try:
            await proc.communicate()
        except Exception:
            pass
        return False, f"Script timed out after {timeout:g}s: {path.name}"
    except Exception as exc:
        return False, f"Script execution failed: {exc}"

    stdout = _redact((raw_stdout or b"").decode("utf-8", errors="replace").strip())
    stderr = _redact((raw_stderr or b"").decode("utf-8", errors="replace").strip())

    if proc.returncode != 0:
        parts = [f"Script exited with code {proc.returncode}"]
        if stderr:
            parts.append(f"stderr:\n{stderr}")
        if stdout:
            parts.append(f"stdout:\n{stdout}")
        return False, _clip("\n".join(parts))

    return True, _clip(stdout)


def has_actionable_output(output: str) -> bool:
    """Return whether a successful script run found something worth acting on.

    A script signals "nothing to report" two ways: print nothing, or end with a
    JSON line ``{"wakeAgent": false}``. The second form exists so watchdog
    scripts written for other runtimes work here unchanged.

    Both callers treat a false here as "do nothing this tick": a ``script`` job
    delivers nothing, and an ``agent_turn`` job with a pre-run script skips the
    turn entirely rather than paying for a model call with no news in it.
    """
    stripped = (output or "").strip()
    if not stripped:
        return False

    last_line = stripped.splitlines()[-1].strip()
    try:
        gate = json.loads(last_line)
    except (json.JSONDecodeError, ValueError):
        return True
    if not isinstance(gate, dict):
        return True
    return gate.get("wakeAgent", True) is not False


__all__ = [
    "JOB_ID_PLACEHOLDER",
    "MAX_SCRIPT_OUTPUT_CHARS",
    "ScriptPathError",
    "has_actionable_output",
    "normalize_script_value",
    "resolve_script_path",
    "run_job_script",
    "scripts_dir",
    "substitute_job_id",
    "validate_script_path",
]
