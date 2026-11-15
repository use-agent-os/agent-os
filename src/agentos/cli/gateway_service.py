"""``agentos gateway install | uninstall``: run the gateway as a user service.

macOS gets a LaunchAgent (``~/Library/LaunchAgents/dev.useagentos.gateway.plist``),
Linux a systemd user unit (``~/.config/systemd/user/agentos-gateway.service``).
Windows is not supported. Both run the absolute ``agentos`` executable with
``gateway run --port N`` and restart it when it dies:

* launchd: ``KeepAlive`` + ``ThrottleInterval 10`` (at most one respawn per
  10 s), ``ExitTimeOut`` = the worst-case stop + a margin, so a stop can
  drain turns and tear down before it is killed;
* systemd: ``Restart=always``, ``RestartSec=5``, ``RestartPreventExitStatus=78``
  (EX_CONFIG: a broken config is not restarted into a loop), ``KillMode=mixed``
  and ``TimeoutStopSec`` = the same timeout.

The gateway's crash-loop breaker (safe mode) is what keeps keep-alive from
becoming a restart storm when a channel crashes it at boot.

Every function takes the home directory, the platform and the subprocess
runner as arguments, so tests (and an operator's dry run with ``--no-start``)
never touch the real service manager.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

LAUNCHD_LABEL = "dev.useagentos.gateway"
SYSTEMD_UNIT = "agentos-gateway.service"
#: Seconds on top of the worst-case stop before the service manager kills.
STOP_MARGIN_S = 10
#: Seconds between two respawns (launchd ``ThrottleInterval``).
LAUNCHD_THROTTLE_S = 10
#: Seconds systemd waits before a restart (``RestartSec``).
SYSTEMD_RESTART_S = 5
#: Variables carried into the unit when the installing shell sets them, so
#: the service runs against the same state and config as the operator's CLI.
CARRIED_ENV = ("AGENTOS_STATE_DIR", "AGENTOS_GATEWAY_CONFIG_PATH")
#: Set in every generated unit so the gateway knows a service manager runs
#: it (``launchd`` or ``systemd``): see :func:`config_failure_backoff`.
SERVICE_ENV = "AGENTOS_SERVICE"
#: Exit-78 backoff under launchd: 5 s, doubling per recent failure, capped.
CONFIG_BACKOFF_BASE_S = 5.0
CONFIG_BACKOFF_MAX_S = 300.0
#: A failure streak older than this starts over.
CONFIG_BACKOFF_RESET_S = 900.0

ServicePlatform = Literal["launchd", "systemd"]
Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]
UNSUPPORTED_MESSAGE = (
    "agentos gateway install is not supported on Windows; it supports macOS (launchd) "
    "and Linux (systemd user units)."
)


def detect_platform(sys_platform: str | None = None) -> ServicePlatform | None:
    value = sys.platform if sys_platform is None else sys_platform
    if value == "darwin":
        return "launchd"
    if value.startswith("linux"):
        return "systemd"
    return None


def default_runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: S603 - argv is built here, never a shell string
            list(argv),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(list(argv), 127, "", str(exc))


def resolve_agentos_program(
    *,
    argv0: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    executable: str | None = None,
) -> list[str]:
    """The absolute command that starts this CLI: what the unit executes.

    ``sys.argv[0]`` when it is the ``agentos`` script (made absolute but not
    resolved, so a ``~/.local/bin/agentos`` symlink keeps following
    ``uv tool upgrade``), else ``agentos`` on ``PATH``, else this interpreter
    with ``-P -m agentos.cli.main``.
    """
    raw = sys.argv[0] if argv0 is None else argv0
    if raw:
        candidate = Path(raw).expanduser()
        if candidate.name in {"agentos", "agentos.exe"} and candidate.exists():
            return [str(candidate.absolute())]
    found = which("agentos")
    if found:
        return [str(Path(found).absolute())]
    # ``-P``: the gateway must not import from its working directory.
    return [executable or sys.executable, "-P", "-m", "agentos.cli.main"]


def service_path_env(home: Path, inherited: str | None = None) -> str:
    """``PATH`` for the unit: the user's tool dirs first, then the system ones.

    A service manager starts the gateway with a bare ``PATH``; without these
    the harness checks miss Homebrew/npm/uv-installed CLIs.
    """
    entries = [
        str(home / ".local" / "bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    for extra in (inherited or "").split(os.pathsep):
        if extra and extra not in entries:
            entries.append(extra)
    return os.pathsep.join(entries)


def stop_timeout_s() -> int:
    """launchd ``ExitTimeOut`` / systemd ``TimeoutStopSec``, in whole seconds.

    The gateway's own worst-case stop (drain 30 s + forced unwind 2 s +
    socket close 2 s + uvicorn stop 10 s, from the constants that bound
    them) plus :data:`STOP_MARGIN_S`: 54 s today.
    """
    import math

    from agentos.gateway.readiness import stop_budget_ms

    return math.ceil(stop_budget_ms() / 1000) + STOP_MARGIN_S


@dataclass(frozen=True)
class ServiceSpec:
    platform: ServicePlatform
    home: Path
    program: list[str]
    port: int
    config_path: str | None = None
    env: dict[str, str] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return LAUNCHD_LABEL if self.platform == "launchd" else SYSTEMD_UNIT

    @property
    def unit_path(self) -> Path:
        return unit_path_for(self.platform, self.home)

    @property
    def log_dir(self) -> Path:
        return self.home / "Library" / "Logs" / "AgentOS"

    @property
    def argv(self) -> list[str]:
        argv = [*self.program, "gateway", "run", "--port", str(self.port)]
        if self.config_path:
            argv.extend(["--config", self.config_path])
        return argv


def unit_path_for(platform: ServicePlatform, home: Path) -> Path:
    if platform == "launchd":
        return home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    return home / ".config" / "systemd" / "user" / SYSTEMD_UNIT


def build_spec(
    platform: ServicePlatform,
    *,
    home: Path,
    port: int,
    config_path: str | None = None,
    program: list[str] | None = None,
    environ: dict[str, str] | None = None,
) -> ServiceSpec:
    source = os.environ if environ is None else environ
    # The installing shell's PATH rides along after the standard dirs, so
    # CLIs installed with nvm, volta, bun or pyenv are found under the service.
    env = {
        "PATH": service_path_env(home, inherited=source.get("PATH")),
        SERVICE_ENV: platform,
    }
    for key in CARRIED_ENV:
        value = str(source.get(key, "") or "").strip()
        if value:
            env[key] = value
    resolved_config = str(Path(config_path).expanduser().absolute()) if config_path else None
    return ServiceSpec(
        platform=platform,
        home=home,
        program=program or resolve_agentos_program(),
        port=port,
        config_path=resolved_config,
        env=env,
    )


def render_launchd_plist(spec: ServiceSpec) -> str:
    payload: dict[str, Any] = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": spec.argv,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": LAUNCHD_THROTTLE_S,
        "ExitTimeOut": stop_timeout_s(),
        "WorkingDirectory": str(spec.home),
        "StandardOutPath": str(spec.log_dir / "gateway-service.log"),
        "StandardErrorPath": str(spec.log_dir / "gateway-service.err.log"),
        "EnvironmentVariables": dict(spec.env),
    }
    return plistlib.dumps(payload, sort_keys=True).decode("utf-8")


def _systemd_quote(value: str) -> str:
    """One ``ExecStart``/``Environment`` word: ``%`` doubled, quoted when needed."""
    escaped = value.replace("%", "%%")
    if escaped and not any(ch in escaped for ch in " \t\"\\'$;"):
        return escaped
    return '"' + escaped.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_systemd_unit(spec: ServiceSpec) -> str:
    exec_start = " ".join(_systemd_quote(part) for part in spec.argv)
    env_lines = "".join(
        f"Environment={_systemd_quote(f'{key}={value}')}\n" for key, value in spec.env.items()
    )
    return (
        "[Unit]\n"
        "Description=AgentOS gateway\n"
        "Documentation=https://github.com/use-agent-os/agent-os/blob/main/docs/gateway.md\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_start}\n"
        f"WorkingDirectory={_systemd_quote(str(spec.home))}\n"
        f"{env_lines}"
        "Restart=always\n"
        f"RestartSec={SYSTEMD_RESTART_S}\n"
        "RestartPreventExitStatus=78\n"
        "KillMode=mixed\n"
        "KillSignal=SIGTERM\n"
        f"TimeoutStopSec={stop_timeout_s()}\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def unit_identity(platform: ServicePlatform, text: str) -> str:
    """A unit with its ``PATH`` removed: what decides "the same service"."""
    if platform == "launchd":
        try:
            payload = plistlib.loads(text.encode("utf-8"))
        except Exception:  # noqa: BLE001 - an unparsable plist is its own identity
            return text
        if isinstance(payload, dict):
            env = payload.get("EnvironmentVariables")
            if isinstance(env, dict):
                env.pop("PATH", None)
            return plistlib.dumps(payload, sort_keys=True).decode("utf-8")
        return text
    return "".join(
        line
        for line in text.splitlines(keepends=True)
        if not line.startswith(("Environment=PATH=", 'Environment="PATH='))
    )


def unit_port(platform: ServicePlatform, text: str) -> int | None:
    """The ``--port`` an installed unit runs the gateway with, if readable."""
    argv: list[str] = []
    if platform == "launchd":
        try:
            payload = plistlib.loads(text.encode("utf-8"))
        except Exception:  # noqa: BLE001
            return None
        raw = payload.get("ProgramArguments") if isinstance(payload, dict) else None
        argv = [str(part) for part in raw] if isinstance(raw, list) else []
    else:
        import shlex

        for line in text.splitlines():
            if line.startswith("ExecStart="):
                try:
                    argv = shlex.split(line[len("ExecStart=") :])
                except ValueError:
                    return None
    for flag, value in zip(argv, argv[1:]):
        if flag in {"--port", "-p"}:
            try:
                return int(value)
            except ValueError:
                return None
    return None


def render_unit(spec: ServiceSpec) -> str:
    return render_launchd_plist(spec) if spec.platform == "launchd" else render_systemd_unit(spec)


@dataclass
class ServiceResult:
    action: str
    state: str
    ok: bool = True
    platform: str | None = None
    label: str | None = None
    path: str | None = None
    message: str = ""
    commands: list[dict[str, Any]] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "action": self.action,
            "state": self.state,
            "platform": self.platform,
            "label": self.label,
            "path": self.path,
        }
        if self.message:
            payload["message"] = self.message
        if self.commands:
            payload["commands"] = self.commands
        if self.details:
            payload["details"] = self.details
        return payload


def _run(runner: Runner, argv: list[str], log: list[dict[str, Any]]) -> int:
    proc = runner(argv)
    entry: dict[str, Any] = {"argv": argv, "returncode": proc.returncode}
    stderr = (proc.stderr or "").strip()
    if stderr:
        entry["stderr"] = stderr[-500:]
    log.append(entry)
    return int(proc.returncode)


def _launchd_domain(uid: int) -> str:
    return f"gui/{uid}"


def _launchd_loaded(runner: Runner, uid: int, log: list[dict[str, Any]]) -> bool:
    return _run(runner, ["launchctl", "print", f"{_launchd_domain(uid)}/{LAUNCHD_LABEL}"], log) == 0


def _systemd_active(runner: Runner, log: list[dict[str, Any]]) -> bool:
    return _run(runner, ["systemctl", "--user", "is-active", "--quiet", SYSTEMD_UNIT], log) == 0


def _current_uid() -> int:
    getuid = getattr(os, "getuid", None)
    return int(getuid()) if callable(getuid) else 0


UNINSTALL_HINT = "agentos gateway uninstall"


def install_service(
    spec: ServiceSpec,
    *,
    runner: Runner = default_runner,
    uid: int | None = None,
    start: bool = True,
) -> ServiceResult:
    """Write the unit and load it. Idempotent for an identical unit; refuses a different one."""
    uid = _current_uid() if uid is None else uid
    path = spec.unit_path
    rendered = render_unit(spec)
    commands: list[dict[str, Any]] = []
    result = ServiceResult(
        action="install",
        state="installed",
        platform=spec.platform,
        label=spec.label,
        path=str(path),
        commands=commands,
        details={
            "argv": spec.argv,
            "env": dict(spec.env),
            "stopTimeoutS": stop_timeout_s(),
            "unit": rendered,
        },
    )

    existing: str | None = None
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError as exc:
            existing = f"<unreadable: {exc}>"
    if existing is not None and existing != rendered:
        if unit_identity(spec.platform, existing) == unit_identity(spec.platform, rendered):
            # Same gateway, installed from a shell with another PATH: keep it.
            result.state = "already_installed"
            result.details["pathDiffers"] = True
            result.message = (
                f"{spec.label} is already installed ({path}); only its PATH differs from "
                f"this shell's. To refresh it: {UNINSTALL_HINT}, then install again."
            )
        else:
            result.ok = False
            result.state = "conflict"
            result.message = (
                f"A service named {spec.label} is already installed at {path} with different "
                f"settings. Remove it first with '{UNINSTALL_HINT}', then install again."
            )
            return result

    if spec.platform == "launchd" and existing is None and _launchd_loaded(runner, uid, commands):
        result.ok = False
        result.state = "conflict"
        result.message = (
            f"A launchd job labelled {LAUNCHD_LABEL} is already loaded from another plist. "
            f"Unload it first: launchctl bootout {_launchd_domain(uid)}/{LAUNCHD_LABEL}"
        )
        return result

    if existing is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if spec.platform == "launchd":
            spec.log_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(rendered, encoding="utf-8")
        os.replace(tmp, path)
    else:
        result.state = "already_installed"

    if not start:
        verb = "Wrote" if result.state == "installed" else "Already installed:"
        result.message = result.message or f"{verb} {path}; not loaded (--no-start)."
        return result

    if spec.platform == "launchd":
        if _launchd_loaded(runner, uid, commands):
            result.message = result.message or f"{LAUNCHD_LABEL} is loaded."
            return result
        _run(runner, ["launchctl", "enable", f"{_launchd_domain(uid)}/{LAUNCHD_LABEL}"], commands)
        rc = _run(runner, ["launchctl", "bootstrap", _launchd_domain(uid), str(path)], commands)
        if rc != 0:
            result.ok = False
            result.state = "load_failed"
            result.message = (
                f"Wrote {path} but 'launchctl bootstrap' failed (exit {rc}); see commands[].stderr."
            )
            return result
        result.message = f"Installed and loaded {LAUNCHD_LABEL} ({path})."
        return result

    if _run(runner, ["systemctl", "--user", "daemon-reload"], commands) != 0:
        result.ok = False
        result.state = "load_failed"
        result.message = f"Wrote {path} but 'systemctl --user daemon-reload' failed."
        return result
    rc = _run(runner, ["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT], commands)
    if rc != 0:
        result.ok = False
        result.state = "load_failed"
        result.message = (
            f"Wrote {path} but 'systemctl --user enable --now {SYSTEMD_UNIT}' failed (exit {rc})."
        )
        return result
    result.message = (
        f"Installed and started {SYSTEMD_UNIT} ({path}). To keep it running after you log "
        "out: loginctl enable-linger $USER"
    )
    return result


def uninstall_service(
    platform: ServicePlatform,
    *,
    home: Path,
    runner: Runner = default_runner,
    uid: int | None = None,
) -> ServiceResult:
    """Stop and remove the unit. Idempotent: nothing installed is success."""
    uid = _current_uid() if uid is None else uid
    path = unit_path_for(platform, home)
    commands: list[dict[str, Any]] = []
    label = LAUNCHD_LABEL if platform == "launchd" else SYSTEMD_UNIT
    result = ServiceResult(
        action="uninstall",
        state="uninstalled",
        platform=platform,
        label=label,
        path=str(path),
        commands=commands,
    )
    stopped = False
    if platform == "launchd":
        if _launchd_loaded(runner, uid, commands):
            rc = _run(runner, ["launchctl", "bootout", f"{_launchd_domain(uid)}/{label}"], commands)
            if rc != 0 and _launchd_loaded(runner, uid, commands):
                result.ok = False
                result.state = "unload_failed"
                result.message = f"'launchctl bootout' failed (exit {rc}); the job is still loaded."
                return result
            stopped = True
    elif path.exists() or _systemd_active(runner, commands):
        _run(runner, ["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT], commands)
        stopped = True
    existed = path.exists()
    if existed:
        path.unlink()
        if platform == "systemd":
            _run(runner, ["systemctl", "--user", "daemon-reload"], commands)
    if not existed and not stopped:
        result.state = "not_installed"
        result.message = "No AgentOS gateway service is installed."
        return result
    result.message = f"{'Stopped and removed' if stopped else 'Removed'} {label} ({path})."
    return result


def service_status(
    platform: ServicePlatform | None,
    *,
    home: Path,
    runner: Runner = default_runner,
    uid: int | None = None,
) -> dict[str, Any]:
    """What ``agentos gateway status --deep`` reports under ``service``."""
    if platform is None:
        return {"supported": False, "installed": False, "message": UNSUPPORTED_MESSAGE}
    uid = _current_uid() if uid is None else uid
    path = unit_path_for(platform, home)
    commands: list[dict[str, Any]] = []
    loaded = (
        _launchd_loaded(runner, uid, commands)
        if platform == "launchd"
        else _systemd_active(runner, commands)
    )
    port: int | None = None
    if path.exists():
        try:
            port = unit_port(platform, path.read_text(encoding="utf-8"))
        except OSError:
            port = None
    return {
        "supported": True,
        "platform": platform,
        "label": LAUNCHD_LABEL if platform == "launchd" else SYSTEMD_UNIT,
        "path": str(path),
        "installed": path.exists(),
        "port": port,
        "loaded" if platform == "launchd" else "active": loaded,
    }


def service_stop_hint(service: dict[str, Any], *, uid: int | None = None) -> str | None:
    """How to stop a gateway the installed service runs, or ``None`` if none does."""
    if not service.get("supported"):
        return None
    loaded = bool(service.get("loaded", service.get("active")))
    if not (service.get("installed") or loaded):
        return None
    if service.get("platform") == "launchd":
        uid = _current_uid() if uid is None else uid
        return (
            f"It is run by the {LAUNCHD_LABEL} LaunchAgent ({service.get('path')}), which "
            "starts it again whenever it exits. Stop it until the next login with "
            f"'launchctl bootout {_launchd_domain(uid)}/{LAUNCHD_LABEL}', or remove the "
            f"service with '{UNINSTALL_HINT}'."
        )
    return (
        f"It is run by the systemd user unit {SYSTEMD_UNIT} ({service.get('path')}), which "
        f"restarts it whenever it exits. Stop it with 'systemctl --user stop {SYSTEMD_UNIT}' "
        f"('disable --now' keeps it off), or remove the service with '{UNINSTALL_HINT}'."
    )


# ── exit 78 under launchd: do not respawn a broken config every 10 s ──


def config_failure_backoff(
    *,
    counter_path: Path,
    environ: dict[str, str] | None = None,
    now: float | None = None,
    sleep: Callable[[float], Any] | None = None,
    emit: Callable[[str], Any] | None = None,
) -> float:
    """Wait before an exit 78 when launchd would restart it at once; returns the wait.

    systemd honours ``RestartPreventExitStatus=78`` and does not restart a
    config error, but launchd's ``KeepAlive`` respawns every exit after
    ``ThrottleInterval`` (10 s), forever. The wait doubles per recent
    config failure (5 s, 10 s, 20 s, ... capped at 300 s; a streak older
    than 15 minutes starts over), so a broken config costs a respawn every
    five minutes instead of every ten seconds. The streak is kept in
    ``counter_path`` and cleared by :func:`clear_config_failures` once a
    boot succeeds.
    """
    import json
    import time as _time

    source = os.environ if environ is None else environ
    if str(source.get(SERVICE_ENV, "")).strip().lower() != "launchd":
        return 0.0
    current = _time.time() if now is None else now
    count = 0
    try:
        data = json.loads(counter_path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and current - float(data.get("last_at", 0)) <= (
            CONFIG_BACKOFF_RESET_S
        ):
            count = max(0, int(data.get("count", 0)))
    except (OSError, ValueError, TypeError):
        count = 0
    delay = min(CONFIG_BACKOFF_BASE_S * (2**count), CONFIG_BACKOFF_MAX_S)
    try:
        counter_path.parent.mkdir(parents=True, exist_ok=True)
        counter_path.write_text(
            json.dumps({"count": count + 1, "last_at": current}), encoding="utf-8"
        )
    except OSError:
        pass
    message = (
        f"Configuration error under launchd (failure {count + 1} in a row): waiting "
        f"{int(delay)}s before exiting so the service does not restart a broken config "
        f"every {LAUNCHD_THROTTLE_S}s. Fix it (agentos doctor) or stop the service "
        f"({UNINSTALL_HINT})."
    )
    if emit is not None:
        emit(message)
    if sleep is None:
        _time.sleep(delay)
    else:
        sleep(delay)
    return float(delay)


def clear_config_failures(counter_path: Path) -> None:
    """A boot got past its config: the next config failure starts the streak over."""
    try:
        counter_path.unlink(missing_ok=True)
    except OSError:
        pass


# ── CLI entry points (``agentos gateway install`` / ``uninstall``) ────


def _emit(result: ServiceResult, *, json_output: bool, show_unit: str | None = None) -> None:
    import json

    import typer

    if json_output:
        typer.echo(json.dumps(result.to_payload(), ensure_ascii=False, default=str))
    else:
        stream_err = not result.ok
        if result.message:
            typer.echo(result.message, err=stream_err)
        if show_unit is not None and result.ok:
            typer.echo(f"\n# {result.path}\n{show_unit}")
    if result.exit_code != 0:
        raise typer.Exit(code=result.exit_code)


def install_gateway(
    *,
    port: int | None,
    config_path: str | None,
    start: bool,
    json_output: bool,
    home: Path | None = None,
    platform: ServicePlatform | None | Literal["auto"] = "auto",
    runner: Runner = default_runner,
    program: list[str] | None = None,
) -> ServiceResult:
    import typer

    resolved_platform = detect_platform() if platform == "auto" else platform
    if resolved_platform is None:
        result = ServiceResult(
            action="install", state="unsupported", ok=False, message=UNSUPPORTED_MESSAGE
        )
        _emit(result, json_output=json_output)
        return result
    resolved_port = port
    if resolved_port is None:
        from agentos.cli.gateway_cmd import _load_config_or_exit

        resolved_port = int(_load_config_or_exit(config_path).port)
    if not 0 < int(resolved_port) < 65536:
        raise typer.BadParameter("--port must be between 1 and 65535")
    spec = build_spec(
        resolved_platform,
        home=home or Path.home(),
        port=int(resolved_port),
        config_path=config_path,
        program=program,
    )
    result = install_service(spec, runner=runner, start=start)
    _emit(
        result,
        json_output=json_output,
        show_unit=render_unit(spec) if result.state == "installed" else None,
    )
    return result


def uninstall_gateway(
    *,
    json_output: bool,
    home: Path | None = None,
    platform: ServicePlatform | None | Literal["auto"] = "auto",
    runner: Runner = default_runner,
) -> ServiceResult:
    resolved_platform = detect_platform() if platform == "auto" else platform
    if resolved_platform is None:
        result = ServiceResult(
            action="uninstall", state="unsupported", ok=False, message=UNSUPPORTED_MESSAGE
        )
    else:
        result = uninstall_service(resolved_platform, home=home or Path.home(), runner=runner)
    _emit(result, json_output=json_output)
    return result


__all__ = [
    "CARRIED_ENV",
    "SERVICE_ENV",
    "clear_config_failures",
    "config_failure_backoff",
    "service_stop_hint",
    "unit_identity",
    "unit_port",
    "LAUNCHD_LABEL",
    "STOP_MARGIN_S",
    "SYSTEMD_UNIT",
    "UNSUPPORTED_MESSAGE",
    "ServiceResult",
    "ServiceSpec",
    "build_spec",
    "default_runner",
    "detect_platform",
    "install_gateway",
    "install_service",
    "render_launchd_plist",
    "render_systemd_unit",
    "resolve_agentos_program",
    "service_path_env",
    "service_status",
    "stop_timeout_s",
    "uninstall_gateway",
    "uninstall_service",
    "unit_path_for",
]
