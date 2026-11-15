"""``agentos gateway install | uninstall | status --deep`` (#3690).

Everything runs against a temp home and a scripted subprocess runner: no test
touches the real LaunchAgents directory, launchctl or systemctl.
"""

from __future__ import annotations

import json
import plistlib
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentos.cli import gateway_service as gs
from agentos.cli.main import app

runner = CliRunner()
PROGRAM = ["/opt/agentos/bin/agentos"]
#: launchd plists and systemd units are POSIX artifacts (``:``-separated PATH,
#: ``$HOME``-relative unit paths); on Windows ``gateway install`` is refused
#: instead -- see ``test_cli_install_on_windows_is_refused``, which runs everywhere.
posix_service = pytest.mark.skipif(
    sys.platform == "win32", reason="service units are POSIX-only; Windows refuses install"
)


class ScriptedRunner:
    """Records argv; answers ``launchctl print`` / ``is-active`` from ``loaded``."""

    def __init__(self, *, loaded: bool = False, fail: Sequence[str] = ()) -> None:
        self.loaded = loaded
        self.fail = set(fail)
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        self.calls.append(argv)
        verb = argv[1] if argv[0] == "launchctl" else argv[2]
        if verb in self.fail:
            return subprocess.CompletedProcess(argv, 5, "", f"{verb} failed")
        if verb in {"print", "is-active"}:
            return subprocess.CompletedProcess(argv, 0 if self.loaded else 113, "", "")
        if verb in {"bootstrap"} or argv[2:4] == ["enable", "--now"]:
            self.loaded = True
        if verb == "bootout" or argv[2:4] == ["disable", "--now"]:
            self.loaded = False
        return subprocess.CompletedProcess(argv, 0, "", "")

    def verbs(self) -> list[str]:
        return [" ".join(call[:3]) for call in self.calls]


def _spec(tmp_path: Path, platform: gs.ServicePlatform = "launchd", **kw) -> gs.ServiceSpec:
    return gs.build_spec(
        platform,
        home=tmp_path,
        port=kw.pop("port", 18803),
        program=PROGRAM,
        environ=kw.pop("environ", {}),
        **kw,
    )


@posix_service
def test_launchd_plist_carries_the_keep_alive_contract(tmp_path: Path) -> None:
    spec = _spec(tmp_path, config_path=str(tmp_path / "cfg.toml"))
    plist = plistlib.loads(gs.render_launchd_plist(spec).encode())
    assert plist["Label"] == "dev.useagentos.gateway"
    assert plist["ProgramArguments"] == [
        *PROGRAM,
        "gateway",
        "run",
        "--port",
        "18803",
        "--config",
        str(tmp_path / "cfg.toml"),
    ]
    assert plist["KeepAlive"] is True
    assert plist["RunAtLoad"] is True
    assert plist["ThrottleInterval"] == 10
    # drain (30 s safe + 2 s force) + socket close 2 s + uvicorn stop 10 s + 10 s margin
    assert plist["ExitTimeOut"] == 54 == gs.stop_timeout_s()
    logs = tmp_path / "Library" / "Logs" / "AgentOS"
    assert plist["StandardOutPath"] == str(logs / "gateway-service.log")
    assert plist["StandardErrorPath"] == str(logs / "gateway-service.err.log")
    path = plist["EnvironmentVariables"]["PATH"].split(":")
    assert path[:3] == [str(tmp_path / ".local" / "bin"), "/opt/homebrew/bin", "/usr/local/bin"]


def test_state_dir_and_config_env_are_carried_into_the_unit(tmp_path: Path) -> None:
    spec = _spec(tmp_path, environ={"AGENTOS_STATE_DIR": "/tmp/agentos-x", "OTHER": "no"})
    assert spec.env["AGENTOS_STATE_DIR"] == "/tmp/agentos-x"
    assert "OTHER" not in spec.env


def test_systemd_unit_carries_the_restart_contract(tmp_path: Path) -> None:
    spec = gs.build_spec(
        "systemd",
        home=tmp_path,
        port=18803,
        program=["/opt/My Tools/agentos"],
        environ={"AGENTOS_STATE_DIR": "/srv/agent os"},
    )
    unit = gs.render_systemd_unit(spec)
    assert 'ExecStart="/opt/My Tools/agentos" gateway run --port 18803\n' in unit
    for line in (
        "Restart=always",
        "RestartSec=5",
        "RestartPreventExitStatus=78",
        "KillMode=mixed",
        "TimeoutStopSec=54",
        "WantedBy=default.target",
    ):
        assert f"\n{line}\n" in unit
    assert 'Environment="AGENTOS_STATE_DIR=/srv/agent os"\n' in unit
    assert gs.unit_path_for("systemd", tmp_path) == (
        tmp_path / ".config" / "systemd" / "user" / "agentos-gateway.service"
    )


def test_launchd_install_writes_the_plist_and_bootstraps_it(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    fake = ScriptedRunner()
    result = gs.install_service(spec, runner=fake, uid=501)
    assert result.ok and result.state == "installed"
    path = tmp_path / "Library" / "LaunchAgents" / "dev.useagentos.gateway.plist"
    assert path.read_text(encoding="utf-8") == gs.render_launchd_plist(spec)
    assert (tmp_path / "Library" / "Logs" / "AgentOS").is_dir()
    assert ["launchctl", "bootstrap", "gui/501", str(path)] in fake.calls
    assert ["launchctl", "enable", "gui/501/dev.useagentos.gateway"] in fake.calls


def test_install_is_idempotent_for_the_same_unit(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    fake = ScriptedRunner()
    gs.install_service(spec, runner=fake, uid=501)
    fake.calls.clear()
    again = gs.install_service(spec, runner=fake, uid=501)
    assert again.ok and again.state == "already_installed"
    # Already loaded: nothing is bootstrapped twice.
    assert [c[1] for c in fake.calls] == ["print"]


def test_install_refuses_a_different_unit_with_the_same_label(tmp_path: Path) -> None:
    gs.install_service(_spec(tmp_path, port=18791), runner=ScriptedRunner(), uid=501)
    fake = ScriptedRunner()
    result = gs.install_service(_spec(tmp_path, port=18803), runner=fake, uid=501)
    assert not result.ok and result.state == "conflict"
    assert result.exit_code == 1
    assert "agentos gateway uninstall" in result.message
    assert fake.calls == []


def test_install_refuses_a_label_loaded_from_another_plist(tmp_path: Path) -> None:
    result = gs.install_service(_spec(tmp_path), runner=ScriptedRunner(loaded=True), uid=501)
    assert result.state == "conflict"
    assert "launchctl bootout gui/501/dev.useagentos.gateway" in result.message
    assert not gs.unit_path_for("launchd", tmp_path).exists()


def test_install_no_start_writes_the_unit_only(tmp_path: Path) -> None:
    fake = ScriptedRunner()
    result = gs.install_service(_spec(tmp_path), runner=fake, uid=501, start=False)
    assert result.ok and gs.unit_path_for("launchd", tmp_path).exists()
    assert all(call[1] == "print" for call in fake.calls)


def test_install_reports_a_failed_bootstrap(tmp_path: Path) -> None:
    result = gs.install_service(_spec(tmp_path), runner=ScriptedRunner(fail=["bootstrap"]), uid=1)
    assert not result.ok and result.state == "load_failed"


def test_launchd_uninstall_boots_out_and_removes_then_is_a_no_op(tmp_path: Path) -> None:
    fake = ScriptedRunner()
    gs.install_service(_spec(tmp_path), runner=fake, uid=501)
    result = gs.uninstall_service("launchd", home=tmp_path, runner=fake, uid=501)
    assert result.ok and result.state == "uninstalled"
    assert ["launchctl", "bootout", "gui/501/dev.useagentos.gateway"] in fake.calls
    assert not gs.unit_path_for("launchd", tmp_path).exists()
    again = gs.uninstall_service("launchd", home=tmp_path, runner=fake, uid=501)
    assert again.ok and again.state == "not_installed"


def test_systemd_install_and_uninstall(tmp_path: Path) -> None:
    fake = ScriptedRunner()
    result = gs.install_service(_spec(tmp_path, "systemd"), runner=fake)
    assert result.ok and result.state == "installed"
    assert fake.verbs()[-2:] == ["systemctl --user daemon-reload", "systemctl --user enable"]
    assert fake.calls[-1] == ["systemctl", "--user", "enable", "--now", "agentos-gateway.service"]
    assert "loginctl enable-linger" in result.message
    fake.calls.clear()
    removed = gs.uninstall_service("systemd", home=tmp_path, runner=fake)
    assert removed.state == "uninstalled"
    assert ["systemctl", "--user", "disable", "--now", "agentos-gateway.service"] in fake.calls
    assert fake.calls[-1] == ["systemctl", "--user", "daemon-reload"]
    assert not gs.unit_path_for("systemd", tmp_path).exists()


def test_service_status_reports_installed_and_loaded(tmp_path: Path) -> None:
    fake = ScriptedRunner()
    assert gs.service_status("launchd", home=tmp_path, runner=fake, uid=501)["installed"] is False
    gs.install_service(_spec(tmp_path), runner=fake, uid=501)
    status = gs.service_status("launchd", home=tmp_path, runner=fake, uid=501)
    assert status["installed"] is True and status["loaded"] is True
    assert gs.service_status(None, home=tmp_path)["supported"] is False


def test_platform_detection() -> None:
    assert gs.detect_platform("darwin") == "launchd"
    assert gs.detect_platform("linux") == "systemd"
    assert gs.detect_platform("win32") is None


def test_program_resolution_prefers_the_running_script(tmp_path: Path) -> None:
    script = tmp_path / "bin" / "agentos"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\n")
    assert gs.resolve_agentos_program(argv0=str(script), which=lambda _: None) == [str(script)]
    # Made absolute, not resolved: on Windows that adds the current drive.
    assert gs.resolve_agentos_program(argv0="python", which=lambda _: "/opt/x/agentos") == [
        str(Path("/opt/x/agentos").absolute())
    ]
    assert gs.resolve_agentos_program(argv0="", which=lambda _: None, executable="/py") == [
        "/py",
        "-P",
        "-m",
        "agentos.cli.main",
    ]


# ── through the CLI ──────────────────────────────────────────────────


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ScriptedRunner:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(gs, "detect_platform", lambda sys_platform=None: "launchd")
    fake = ScriptedRunner()
    monkeypatch.setattr(gs, "default_runner", fake)
    monkeypatch.setattr(gs, "install_service", _bind_runner(gs.install_service, fake), raising=True)
    monkeypatch.setattr(gs, "uninstall_service", _bind_runner(gs.uninstall_service, fake))
    monkeypatch.setattr(gs, "resolve_agentos_program", lambda **_: PROGRAM)
    return fake


def _bind_runner(fn, fake):  # type: ignore[no-untyped-def]
    def _wrapped(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["runner"] = fake
        kwargs.setdefault("uid", 501)
        return fn(*args, **kwargs)

    return _wrapped


@posix_service
def test_cli_install_status_deep_uninstall(cli_env: ScriptedRunner, tmp_path: Path) -> None:
    result = runner.invoke(app, ["gateway", "install", "--port", "18803", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["state"] == "installed"
    assert payload["label"] == "dev.useagentos.gateway"
    assert payload["details"]["argv"] == [*PROGRAM, "gateway", "run", "--port", "18803"]
    assert "<key>KeepAlive</key>" in payload["details"]["unit"]
    plist = tmp_path / "Library" / "LaunchAgents" / "dev.useagentos.gateway.plist"
    assert plist.exists()

    text = runner.invoke(app, ["gateway", "install", "--port", "18803"])
    assert text.exit_code == 0
    assert "is loaded" in text.output

    conflict = runner.invoke(app, ["gateway", "install", "--port", "18999"])
    assert conflict.exit_code == 1
    assert "agentos gateway uninstall" in conflict.output

    removed = runner.invoke(app, ["gateway", "uninstall", "--json"])
    assert removed.exit_code == 0, removed.output
    assert json.loads(removed.output)["state"] == "uninstalled"
    assert not plist.exists()


def test_cli_install_on_windows_is_refused(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(gs, "detect_platform", lambda sys_platform=None: None)
    result = runner.invoke(app, ["gateway", "install"])
    assert result.exit_code == 1
    assert "not supported on Windows" in result.output
    result = runner.invoke(app, ["gateway", "uninstall"])
    assert result.exit_code == 1


def test_cli_status_deep_reports_the_service(tmp_path: Path, monkeypatch) -> None:
    from agentos.cli import gateway_cmd, gateway_lifecycle

    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path / "home"))
    monkeypatch.setattr(gateway_lifecycle.GatewayLifecycleManager, "_probe_health", lambda s: False)
    monkeypatch.setattr(
        gateway_cmd,
        "_deep_service_status",
        lambda: {"supported": True, "installed": True, "loaded": False, "label": "x", "path": "p"},
    )
    result = runner.invoke(app, ["gateway", "status", "--deep", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["service"]["installed"] is True


# ── follow-ups: PATH carry-over, service env, port, stop hint, exit-78 backoff ──


@posix_service
def test_the_installing_shells_path_is_carried_after_the_standard_dirs(tmp_path: Path) -> None:
    spec = _spec(tmp_path, environ={"PATH": "/opt/nvm/versions/node/v22/bin:/usr/bin"})
    path = spec.env["PATH"].split(":")
    assert path[:3] == [str(tmp_path / ".local" / "bin"), "/opt/homebrew/bin", "/usr/local/bin"]
    assert "/opt/nvm/versions/node/v22/bin" in path
    assert path.count("/usr/bin") == 1
    assert spec.env["AGENTOS_SERVICE"] == "launchd"
    systemd = _spec(tmp_path, "systemd")
    assert "Environment=AGENTOS_SERVICE=systemd\n" in gs.render_systemd_unit(systemd)


@pytest.mark.parametrize("platform", ["launchd", "systemd"])
def test_reinstalling_from_a_shell_with_another_path_is_not_a_conflict(
    tmp_path: Path, platform: gs.ServicePlatform
) -> None:
    first = _spec(tmp_path, platform, environ={"PATH": "/a/bin"})
    gs.install_service(first, runner=ScriptedRunner(), uid=501)
    again = gs.install_service(
        _spec(tmp_path, platform, environ={"PATH": "/b/bin"}), runner=ScriptedRunner(), uid=501
    )
    assert again.ok and again.state == "already_installed"
    assert again.details["pathDiffers"] is True
    # The unit on disk is left as it was.
    assert gs.unit_path_for(platform, tmp_path).read_text(encoding="utf-8") == gs.render_unit(first)
    other_port = gs.install_service(
        _spec(tmp_path, platform, port=18999), runner=ScriptedRunner(), uid=501
    )
    assert other_port.state == "conflict"


@pytest.mark.parametrize("platform", ["launchd", "systemd"])
def test_status_reports_the_units_port(tmp_path: Path, platform: gs.ServicePlatform) -> None:
    gs.install_service(_spec(tmp_path, platform, port=18803), runner=ScriptedRunner(), uid=501)
    status = gs.service_status(platform, home=tmp_path, runner=ScriptedRunner(), uid=501)
    assert status["port"] == 18803


def test_stop_hint_names_the_service_and_the_way_out() -> None:
    launchd = gs.service_stop_hint(
        {"supported": True, "platform": "launchd", "installed": True, "loaded": True, "path": "p"},
        uid=501,
    )
    assert launchd is not None
    assert "launchctl bootout gui/501/dev.useagentos.gateway" in launchd
    assert "agentos gateway uninstall" in launchd
    systemd = gs.service_stop_hint(
        {"supported": True, "platform": "systemd", "installed": True, "active": True, "path": "p"}
    )
    assert systemd is not None and "systemctl --user stop agentos-gateway.service" in systemd
    assert gs.service_stop_hint({"supported": True, "installed": False, "loaded": False}) is None
    assert gs.service_stop_hint({"supported": False}) is None


def test_exit_78_under_launchd_backs_off_and_doubles(tmp_path: Path) -> None:
    counter = tmp_path / "state" / "gateway" / "service-config-failures.json"
    slept: list[float] = []
    messages: list[str] = []
    env = {"AGENTOS_SERVICE": "launchd"}
    now = 1_000.0
    for _ in range(8):
        gs.config_failure_backoff(
            counter_path=counter, environ=env, now=now, sleep=slept.append, emit=messages.append
        )
        now += slept[-1] + 10  # launchd's ThrottleInterval
    assert slept == [5, 10, 20, 40, 80, 160, 300, 300]
    assert "failure 1 in a row" in messages[0]
    assert "agentos gateway uninstall" in messages[0]

    # A streak older than 15 minutes starts over; a good boot clears it.
    slept.clear()
    gs.config_failure_backoff(counter_path=counter, environ=env, now=now + 901, sleep=slept.append)
    assert slept == [5]
    gs.clear_config_failures(counter)
    assert not counter.exists()


@pytest.mark.parametrize("env", [{}, {"AGENTOS_SERVICE": "systemd"}])
def test_no_backoff_outside_launchd(tmp_path: Path, env: dict[str, str]) -> None:
    slept: list[float] = []
    delay = gs.config_failure_backoff(
        counter_path=tmp_path / "c.json", environ=env, now=1.0, sleep=slept.append
    )
    # systemd's RestartPreventExitStatus=78 already stops the loop.
    assert delay == 0.0 and slept == []


def test_stop_timeout_is_derived_from_the_gateways_own_shutdown_bounds() -> None:
    """Not a constant: raising any bound the shutdown waits on raises it too."""
    from agentos.gateway.boot import UVICORN_STOP_TIMEOUT_S, WS_CLOSE_TIMEOUT_S
    from agentos.gateway.readiness import drain_budget_ms, stop_budget_ms
    from agentos.gateway.restart import FORCE_DRAIN_TIMEOUT_S, SAFE_DRAIN_TIMEOUT_S

    assert drain_budget_ms() == (SAFE_DRAIN_TIMEOUT_S + FORCE_DRAIN_TIMEOUT_S) * 1000
    assert (
        stop_budget_ms() == drain_budget_ms() + (WS_CLOSE_TIMEOUT_S + UVICORN_STOP_TIMEOUT_S) * 1000
    )
    assert gs.stop_timeout_s() == stop_budget_ms() / 1000 + gs.STOP_MARGIN_S == 54


def test_stop_timeout_follows_a_raised_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentos.gateway import boot

    monkeypatch.setattr(boot, "UVICORN_STOP_TIMEOUT_S", 20.5)
    assert gs.stop_timeout_s() == 65  # 32 + 2 + 20.5 rounded up, + 10
