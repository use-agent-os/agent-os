"""`agentos upgrade` command — delegate, check, dry-run, restart+verify."""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from agentos.cli import upgrade_cmd
from agentos.cli.install_method import InstallMethod, UpgradePlan

runner = CliRunner()

# A fake checkout root. Assert against SOURCE_DIR_TEXT, never the literal, so the
# expectation matches what the command actually prints: Path renders this as
# "\w\agent-os" on Windows and "/w/agent-os" elsewhere.
SOURCE_DIR = Path("/w/agent-os")
SOURCE_DIR_TEXT = str(SOURCE_DIR)


@pytest.fixture(autouse=True)
def _no_source_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """Quarantine the PEP 610 probe from the developer's own machine.

    A maintainer's checkout really is installed from a directory, so without
    this the source-install notice would fire in every test and its text would
    embed a machine-dependent path. Tests that want the notice opt in.
    """

    monkeypatch.setattr(upgrade_cmd, "installed_from_directory", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _offline_release_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may reach PyPI or GitHub: both lookups answer "unknown".

    Tests that model a specific answer override the PyPI (or GitHub) stub.
    """

    monkeypatch.setattr("agentos.compat.pypi_client.latest_version", lambda **k: None)
    monkeypatch.setattr("agentos.compat.github_releases.latest_release_version", lambda **k: None)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pre-upgrade snapshot must never touch the developer's ~/.agentos."""

    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path / "home"))


def _app() -> typer.Typer:
    app = typer.Typer()
    app.command("upgrade")(upgrade_cmd.upgrade_command)

    @app.command("noop")
    def _noop() -> None:  # keeps Typer in multi-command mode
        return None

    return app


def _delegated_plan(**_: Any) -> UpgradePlan:
    return UpgradePlan(
        method=InstallMethod.UV_TOOL,
        delegated=True,
        tool="/abs/uv",
        command=[
            "/abs/uv",
            "tool",
            "install",
            "--force",
            "--python",
            "3.12",
            "use-agent-os[recommended]",
        ],
        manual_hint='uv tool install --force --python 3.12 "use-agent-os[recommended]"',
    )


def _pip_plan(**_: Any) -> UpgradePlan:
    return UpgradePlan(
        method=InstallMethod.PIP,
        delegated=False,
        tool=None,
        command=["python", "-m", "pip", "install", "--upgrade", "use-agent-os"],
        manual_hint="python -m pip install --upgrade use-agent-os",
    )


def _ok_run(*_: Any, **__: Any) -> upgrade_cmd.UpgradeRunResult:
    return upgrade_cmd.UpgradeRunResult(
        ok=True, timed_out=False, returncode=0, stdout="upgraded", stderr=""
    )


def _json_payload(stdout: str) -> dict[str, Any]:
    """The `--json` object, which progress prose may precede on stdout."""

    start = stdout.index("{")
    payload = json.loads(stdout[start:])
    assert isinstance(payload, dict)
    return payload


# --- --check ---------------------------------------------------------------


def test_check_reports_newer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upgrade_cmd, "build_upgrade_plan", _delegated_plan)
    monkeypatch.setattr(
        "agentos.compat.pypi_client.latest_version", lambda timeout=5.0: "99999.1.1"
    )
    result = runner.invoke(_app(), ["upgrade", "--check"])
    assert result.exit_code == 0
    assert "newer version is available" in result.stdout


def test_check_offline_exit_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upgrade_cmd, "build_upgrade_plan", _delegated_plan)
    monkeypatch.setattr("agentos.compat.pypi_client.latest_version", lambda timeout=5.0: None)
    result = runner.invoke(_app(), ["upgrade", "--check"])
    assert result.exit_code == 0
    assert "could not check (offline)" in result.stdout


def test_check_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"run": False}
    monkeypatch.setattr(upgrade_cmd, "build_upgrade_plan", _delegated_plan)
    monkeypatch.setattr(
        "agentos.compat.pypi_client.latest_version", lambda timeout=5.0: "99999.1.1"
    )
    monkeypatch.setattr(
        upgrade_cmd,
        "_run_upgrade_subprocess",
        lambda *a, **k: called.__setitem__("run", True),
    )
    runner.invoke(_app(), ["upgrade", "--check"])
    assert called["run"] is False


# --- non-delegated (pip/editable) ------------------------------------------


def test_pip_prints_manual_and_exits_3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upgrade_cmd, "build_upgrade_plan", _pip_plan)
    result = runner.invoke(_app(), ["upgrade"])
    assert result.exit_code == 3
    assert "pip install --upgrade use-agent-os" in result.stdout


# --- --dry-run -------------------------------------------------------------


def test_dry_run_touches_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = {"run": False}
    monkeypatch.setattr(upgrade_cmd, "build_upgrade_plan", _delegated_plan)
    monkeypatch.setattr(
        upgrade_cmd, "_run_upgrade_subprocess", lambda *a, **k: ran.__setitem__("run", True)
    )
    result = runner.invoke(_app(), ["upgrade", "--dry-run"])
    assert result.exit_code == 0
    assert (
        "Would run: /abs/uv tool install --force --python 3.12 use-agent-os[recommended]"
        in result.stdout
    )
    assert ran["run"] is False


# --- source-install notice (PEP 610 directory install) ---------------------


def test_source_install_notice_names_the_way_back(monkeypatch: pytest.MonkeyPatch) -> None:
    # A checkout-backed install is exactly the case this command replaces, so
    # it must say so and name install_source.sh — but never block.
    monkeypatch.setattr(upgrade_cmd, "build_upgrade_plan", _delegated_plan)
    monkeypatch.setattr(upgrade_cmd, "installed_from_directory", lambda *a, **k: SOURCE_DIR)
    monkeypatch.setattr(upgrade_cmd, "_run_upgrade_subprocess", _ok_run)
    monkeypatch.setattr(upgrade_cmd, "_installed_version_via", lambda *a, **k: "9.9.9")
    monkeypatch.setattr(upgrade_cmd, "_restart_and_verify", lambda **k: True)

    result = runner.invoke(_app(), ["upgrade"])
    assert result.exit_code == 0
    assert SOURCE_DIR_TEXT in result.stdout
    assert "scripts/install_source.sh" in result.stdout
    assert "Upgraded" in result.stdout


def test_no_source_install_notice_for_a_release_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(upgrade_cmd, "build_upgrade_plan", _delegated_plan)
    monkeypatch.setattr(upgrade_cmd, "_run_upgrade_subprocess", _ok_run)
    monkeypatch.setattr(upgrade_cmd, "_installed_version_via", lambda *a, **k: "9.9.9")
    monkeypatch.setattr(upgrade_cmd, "_restart_and_verify", lambda **k: True)

    result = runner.invoke(_app(), ["upgrade"])
    assert result.exit_code == 0
    assert "install_source.sh" not in result.stdout


def test_dry_run_reports_the_source_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = {"run": False}
    monkeypatch.setattr(upgrade_cmd, "build_upgrade_plan", _delegated_plan)
    monkeypatch.setattr(upgrade_cmd, "installed_from_directory", lambda *a, **k: SOURCE_DIR)
    monkeypatch.setattr(
        upgrade_cmd, "_run_upgrade_subprocess", lambda *a, **k: ran.__setitem__("run", True)
    )

    result = runner.invoke(_app(), ["upgrade", "--dry-run", "--json"])
    assert result.exit_code == 0
    assert _json_payload(result.stdout)["sourceDirectory"] == SOURCE_DIR_TEXT
    assert ran["run"] is False


def test_success_json_reports_the_source_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upgrade_cmd, "build_upgrade_plan", _delegated_plan)
    monkeypatch.setattr(upgrade_cmd, "installed_from_directory", lambda *a, **k: SOURCE_DIR)
    monkeypatch.setattr(upgrade_cmd, "_run_upgrade_subprocess", _ok_run)
    monkeypatch.setattr(upgrade_cmd, "_installed_version_via", lambda *a, **k: "9.9.9")
    monkeypatch.setattr(upgrade_cmd, "_restart_and_verify", lambda **k: True)

    result = runner.invoke(_app(), ["upgrade", "--json"])
    assert result.exit_code == 0
    payload = _json_payload(result.stdout)
    assert payload["sourceDirectory"] == SOURCE_DIR_TEXT
    assert payload["new"] == "9.9.9"


def test_release_install_json_reports_null_source_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(upgrade_cmd, "build_upgrade_plan", _delegated_plan)
    monkeypatch.setattr(upgrade_cmd, "_run_upgrade_subprocess", _ok_run)
    monkeypatch.setattr(upgrade_cmd, "_installed_version_via", lambda *a, **k: "9.9.9")
    monkeypatch.setattr(upgrade_cmd, "_restart_and_verify", lambda **k: True)

    result = runner.invoke(_app(), ["upgrade", "--json"])
    assert result.exit_code == 0
    assert _json_payload(result.stdout)["sourceDirectory"] is None


# --- successful delegate + restart+verify ----------------------------------


def test_upgrade_success_restarts_and_verifies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upgrade_cmd, "build_upgrade_plan", _delegated_plan)
    monkeypatch.setattr(upgrade_cmd, "_run_upgrade_subprocess", _ok_run)
    monkeypatch.setattr(upgrade_cmd, "_installed_version_via", lambda *a, **k: "99999.2.0")
    seen: dict[str, Any] = {}

    def fake_restart(**kwargs: Any) -> bool:
        seen.update(kwargs)
        return True

    monkeypatch.setattr(upgrade_cmd, "_restart_and_verify", fake_restart)
    result = runner.invoke(_app(), ["upgrade"])
    assert result.exit_code == 0
    assert "Upgraded:" in result.stdout
    assert "→ 99999.2.0" in result.stdout
    assert seen["expected_version"] == "99999.2.0"


def test_upgrade_verify_failure_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upgrade_cmd, "build_upgrade_plan", _delegated_plan)
    monkeypatch.setattr(upgrade_cmd, "_run_upgrade_subprocess", _ok_run)
    monkeypatch.setattr(upgrade_cmd, "_installed_version_via", lambda *a, **k: "99999.2.0")
    monkeypatch.setattr(upgrade_cmd, "_restart_and_verify", lambda **k: False)
    result = runner.invoke(_app(), ["upgrade"])
    assert result.exit_code == 1


# --- --no-restart ----------------------------------------------------------


def test_no_restart_loud_warning_exit_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upgrade_cmd, "build_upgrade_plan", _delegated_plan)
    monkeypatch.setattr(upgrade_cmd, "_run_upgrade_subprocess", _ok_run)
    monkeypatch.setattr(upgrade_cmd, "_installed_version_via", lambda *a, **k: "99999.2.0")
    restarted = {"called": False}
    monkeypatch.setattr(
        upgrade_cmd,
        "_restart_and_verify",
        lambda **k: restarted.__setitem__("called", True),
    )
    result = runner.invoke(_app(), ["upgrade", "--no-restart"])
    assert result.exit_code == 0
    assert restarted["called"] is False
    # Loud warning, prefixed ⚠ (emitted to stderr; CliRunner merges streams).
    assert "⚠" in result.output
    assert "OLD version" in result.output


# --- timeout ---------------------------------------------------------------


def test_upgrade_timeout_exits_one_with_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upgrade_cmd, "build_upgrade_plan", _delegated_plan)
    monkeypatch.setattr(
        upgrade_cmd,
        "_run_upgrade_subprocess",
        lambda *a, **k: upgrade_cmd.UpgradeRunResult(
            ok=False, timed_out=True, returncode=None, stdout="", stderr=""
        ),
    )
    result = runner.invoke(_app(), ["upgrade"])
    assert result.exit_code == 1
    assert "timed out" in result.stdout
    assert "process group" in result.stdout


def test_upgrade_failure_exits_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upgrade_cmd, "build_upgrade_plan", _delegated_plan)
    monkeypatch.setattr(
        upgrade_cmd,
        "_run_upgrade_subprocess",
        lambda *a, **k: upgrade_cmd.UpgradeRunResult(
            ok=False, timed_out=False, returncode=2, stdout="", stderr="boom"
        ),
    )
    result = runner.invoke(_app(), ["upgrade"])
    assert result.exit_code == 1
    assert "Upgrade failed" in result.stdout


# --- _kill_process_group ---------------------------------------------------


class _FakeProc:
    """Minimal subprocess.Popen stand-in: records kills, never exits."""

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.killed = False

    def kill(self) -> None:
        self.killed = True

    def poll(self) -> None:
        return None


def test_kill_process_group_windows_kills_whole_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows the timeout kill must terminate the ENTIRE process tree.

    Regression for #536: ``proc.kill()`` (``TerminateProcess``) only kills the
    direct child, orphaning grandchildren (compilers, downloads) that hold
    file locks on the virtualenv. ``taskkill /T /F`` terminates the tree.
    """

    monkeypatch.setattr(upgrade_cmd.os, "name", "nt")
    calls: list[tuple[Any, Any]] = []

    def fake_run(*args: Any, **kwargs: Any) -> types.SimpleNamespace:
        calls.append((args, kwargs))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(upgrade_cmd.subprocess, "run", fake_run)
    proc = _FakeProc(4242)
    upgrade_cmd._kill_process_group(proc)

    assert calls, "taskkill must be invoked on Windows"
    argv = calls[0][0][0]
    assert argv[:2] == ["taskkill", "/T"], "must kill the whole tree with /T"
    assert "/F" in argv
    assert argv[argv.index("/PID") + 1] == "4242"
    assert calls[0][1]["timeout"] == 5
    assert proc.killed is False, "fallback kill() must not fire when taskkill succeeds"


def test_kill_process_group_windows_falls_back_when_taskkill_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If taskkill itself errors (timeout/OSError), degrade to proc.kill()."""

    monkeypatch.setattr(upgrade_cmd.os, "name", "nt")

    def fake_run(*args: Any, **kwargs: Any) -> types.SimpleNamespace:
        raise subprocess.TimeoutExpired(cmd="taskkill", timeout=5)

    monkeypatch.setattr(upgrade_cmd.subprocess, "run", fake_run)
    proc = _FakeProc(4242)
    upgrade_cmd._kill_process_group(proc)
    assert proc.killed is True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only code path (os.getpgid/killpg)")
def test_kill_process_group_unix_still_uses_killpg(monkeypatch: pytest.MonkeyPatch) -> None:
    """POSIX behavior is unchanged: SIGTERM then SIGKILL to the process group."""

    monkeypatch.setattr(upgrade_cmd.os, "name", "posix")
    monkeypatch.setattr(upgrade_cmd.os, "getpgid", lambda pid: 9999)
    sent: list[tuple[int, Any]] = []
    monkeypatch.setattr(upgrade_cmd.os, "killpg", lambda pgid, sig: sent.append((pgid, sig)))
    proc = _FakeProc(4242)
    # First SIGTERM lands; fake proc dies instantly so the loop exits before SIGKILL.
    proc.poll = lambda: 0
    upgrade_cmd._kill_process_group(proc)
    assert sent == [(9999, signal.SIGTERM)]


# --- _run_upgrade_subprocess timeout path ----------------------------------


class _StuckPopen:
    """Popen stand-in whose communicate() always times out.

    Simulates the #536 regression: after the tree kill, orphaned grandchildren
    keep the inherited stdout/stderr pipe handles open, so communicate() never
    returns. The CLI must still give up instead of hanging.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.pid = 4242
        self.returncode: int | None = None

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        raise subprocess.TimeoutExpired(cmd="uv", timeout=timeout or 0)


def test_upgrade_subprocess_windows_guards_stuck_communicate_after_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stuck pipe handle after the kill must not hang the CLI (Windows path)."""

    monkeypatch.setattr(upgrade_cmd.os, "name", "nt")
    monkeypatch.setattr(upgrade_cmd.subprocess, "Popen", _StuckPopen)
    killed: list[Any] = []
    monkeypatch.setattr(upgrade_cmd, "_kill_process_group", lambda p: killed.append(p))

    result = upgrade_cmd._run_upgrade_subprocess(
        ["uv", "tool", "install", "use-agent-os"], env={}, timeout=1.0
    )

    assert killed, "_kill_process_group must run on timeout"
    assert result.timed_out is True
    assert result.ok is False
    assert result.stdout == ""
    assert result.stderr == ""


# --- release source: PyPI first, GitHub when it is ahead --------------------


def _gh(monkeypatch: pytest.MonkeyPatch, version: str | None) -> None:
    monkeypatch.setattr(
        "agentos.compat.github_releases.latest_release_version", lambda **k: version
    )


def _pypi(monkeypatch: pytest.MonkeyPatch, version: str | None) -> None:
    monkeypatch.setattr("agentos.compat.pypi_client.latest_version", lambda **k: version)


def test_auto_source_prefers_pypi_when_it_is_current(monkeypatch: pytest.MonkeyPatch) -> None:
    _pypi(monkeypatch, "2026.9.11")
    _gh(monkeypatch, "2026.9.11")
    choice = upgrade_cmd._choose_release("auto")
    assert choice.source == "pypi"
    assert choice.spec == "use-agent-os[recommended]"
    assert choice.latest == "2026.9.11"


def test_auto_source_falls_back_to_github_when_it_is_ahead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The "PyPI publish failed for this tag" case: the GitHub release exists,
    # PyPI still serves the previous version.
    monkeypatch.delenv("AGENTOS_REPOSITORY", raising=False)
    _pypi(monkeypatch, "2026.9.9")
    _gh(monkeypatch, "2026.9.11")
    choice = upgrade_cmd._choose_release("auto")
    assert choice.source == "github"
    assert choice.spec == (
        "use-agent-os[recommended] @ https://github.com/use-agent-os/agent-os/releases/"
        "download/v2026.9.11/use_agent_os-2026.9.11-py3-none-any.whl"
    )
    assert choice.latest == "2026.9.11"


def test_auto_source_uses_github_when_pypi_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pypi(monkeypatch, None)
    _gh(monkeypatch, "2026.9.11")
    assert upgrade_cmd._choose_release("auto").source == "github"


def test_explicit_pypi_source_never_asks_github(monkeypatch: pytest.MonkeyPatch) -> None:
    _pypi(monkeypatch, "2026.9.9")
    _gh(monkeypatch, "2026.9.11")
    choice = upgrade_cmd._choose_release("pypi")
    assert choice.source == "pypi"
    assert choice.github is None


def test_check_json_reports_both_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    _pypi(monkeypatch, "2026.9.9")
    _gh(monkeypatch, "99999.1.1")
    result = runner.invoke(_app(), ["upgrade", "--check", "--json"])
    assert result.exit_code == 0
    payload = _json_payload(result.stdout)
    assert payload["status"] == "outdated"
    assert payload["latest"] == "99999.1.1"
    assert payload["pypi"] == "2026.9.9"
    assert payload["github"] == "99999.1.1"
    assert payload["source"] == "github"


def test_invalid_source_is_rejected() -> None:
    result = runner.invoke(_app(), ["upgrade", "--source", "ftp"])
    assert result.exit_code == 2


def test_plan_is_built_from_the_chosen_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    _pypi(monkeypatch, None)
    _gh(monkeypatch, "2026.9.11")
    seen: dict[str, Any] = {}

    def fake_plan(**kwargs: Any) -> UpgradePlan:
        seen.update(kwargs)
        return _delegated_plan()

    monkeypatch.setattr(upgrade_cmd, "build_upgrade_plan", fake_plan)
    result = runner.invoke(_app(), ["upgrade", "--dry-run", "--json"])
    assert result.exit_code == 0
    assert seen["spec"].startswith("use-agent-os[recommended] @ https://github.com/")
    assert _json_payload(result.stdout)["source"] == "github"


# --- snapshot + data verification ------------------------------------------


def test_upgrade_snapshots_before_installing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text("a = 1\n", encoding="utf-8")
    monkeypatch.setattr(upgrade_cmd, "build_upgrade_plan", _delegated_plan)
    order: list[str] = []

    def fake_run(*a: Any, **k: Any) -> upgrade_cmd.UpgradeRunResult:
        order.append("install")
        return _ok_run()

    original = upgrade_cmd.upgrade_snapshot.create_snapshot

    def spy_snapshot(**kwargs: Any) -> Any:
        order.append("snapshot")
        return original(**kwargs)

    monkeypatch.setattr(upgrade_cmd.upgrade_snapshot, "create_snapshot", spy_snapshot)
    monkeypatch.setattr(upgrade_cmd, "_run_upgrade_subprocess", fake_run)
    monkeypatch.setattr(upgrade_cmd, "_installed_version_via", lambda *a, **k: "9.9.9")

    result = runner.invoke(_app(), ["upgrade", "--no-restart", "--json"])
    assert result.exit_code == 0
    assert order == ["snapshot", "install"]
    payload = _json_payload(result.stdout)
    assert payload["snapshot"]["files"] == 1
    assert Path(payload["snapshot"]["path"]).is_dir()


def test_no_snapshot_flag_skips_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upgrade_cmd, "build_upgrade_plan", _delegated_plan)
    monkeypatch.setattr(upgrade_cmd, "_run_upgrade_subprocess", _ok_run)
    monkeypatch.setattr(upgrade_cmd, "_installed_version_via", lambda *a, **k: "9.9.9")
    monkeypatch.setattr(
        upgrade_cmd.upgrade_snapshot,
        "create_snapshot",
        lambda **k: pytest.fail("snapshot must not run with --no-snapshot"),
    )
    result = runner.invoke(_app(), ["upgrade", "--no-restart", "--no-snapshot", "--json"])
    assert result.exit_code == 0
    assert _json_payload(result.stdout)["snapshot"] is None


def test_data_check_runs_only_after_a_verified_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(upgrade_cmd, "build_upgrade_plan", _delegated_plan)
    monkeypatch.setattr(upgrade_cmd, "_run_upgrade_subprocess", _ok_run)
    monkeypatch.setattr(upgrade_cmd, "_installed_version_via", lambda *a, **k: "9.9.9")
    monkeypatch.setattr(upgrade_cmd, "_restart_and_verify", lambda **k: True)
    checked = {"n": 0}

    def fake_verify(**kwargs: Any) -> dict[str, Any]:
        checked["n"] += 1
        return {"data": {"ok": True, "checked": [], "problems": []}, "restored": False}

    monkeypatch.setattr(upgrade_cmd, "_verify_data_after_restart", fake_verify)
    result = runner.invoke(_app(), ["upgrade", "--json"])
    assert result.exit_code == 0
    assert checked["n"] == 1
    assert _json_payload(result.stdout)["data"]["ok"] is True

    monkeypatch.setattr(upgrade_cmd, "_restart_and_verify", lambda **k: False)
    result = runner.invoke(_app(), ["upgrade", "--json"])
    assert result.exit_code == 1
    assert checked["n"] == 1, "an unverified gateway has not migrated anything to check"


def test_corrupt_data_after_upgrade_exits_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upgrade_cmd, "build_upgrade_plan", _delegated_plan)
    monkeypatch.setattr(upgrade_cmd, "_run_upgrade_subprocess", _ok_run)
    monkeypatch.setattr(upgrade_cmd, "_installed_version_via", lambda *a, **k: "9.9.9")
    monkeypatch.setattr(upgrade_cmd, "_restart_and_verify", lambda **k: True)
    monkeypatch.setattr(
        upgrade_cmd,
        "_verify_data_after_restart",
        lambda **k: {
            "data": {
                "ok": False,
                "checked": ["x.db"],
                "problems": [{"path": "x.db", "result": "bad"}],
            },
            "restored": False,
        },
    )
    result = runner.invoke(_app(), ["upgrade", "--json"])
    assert result.exit_code == 1


def test_verify_data_flag_reports_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = tmp_path / "home" / "state"
    state.mkdir(parents=True)
    (state / "broken.db").write_bytes(b"not sqlite" * 50)
    monkeypatch.setattr(
        upgrade_cmd,
        "_run_upgrade_subprocess",
        lambda *a, **k: pytest.fail("--verify-data must not install"),
    )
    result = runner.invoke(_app(), ["upgrade", "--verify-data", "--json"])
    assert result.exit_code == 1
    payload = _json_payload(result.stdout)
    assert payload["ok"] is False
    assert payload["problems"][0]["path"] == str(state / "broken.db")


def test_restore_snapshot_refuses_while_gateway_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text("a = 1\n", encoding="utf-8")
    snap = upgrade_cmd.upgrade_snapshot.create_snapshot(version="1")
    monkeypatch.setattr(upgrade_cmd, "_gateway_answering", lambda config_path: True)
    result = runner.invoke(_app(), ["upgrade", "--restore-snapshot", str(snap.path)])
    assert result.exit_code == 1
    assert "gateway is running" in result.stdout


def test_restore_snapshot_latest_puts_files_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    config = home / "config.toml"
    config.write_text("good = 1\n", encoding="utf-8")
    upgrade_cmd.upgrade_snapshot.create_snapshot(version="1")
    config.write_text("bad = 1\n", encoding="utf-8")
    monkeypatch.setattr(upgrade_cmd, "_gateway_answering", lambda config_path: False)
    result = runner.invoke(_app(), ["upgrade", "--restore-snapshot", "latest", "--json"])
    assert result.exit_code == 0
    assert config.read_text(encoding="utf-8") == "good = 1\n"
    assert _json_payload(result.stdout)["restoredFiles"] == [str(config)]


def test_restore_snapshot_rejects_a_non_snapshot_directory(tmp_path: Path) -> None:
    result = runner.invoke(_app(), ["upgrade", "--restore-snapshot", str(tmp_path)])
    assert result.exit_code == 1
    assert "Not a snapshot" in result.stdout


# --- Windows: stop the gateway before its files are replaced (issue #1365) ---


class _FakeLifecycleManager:
    """Records the lifecycle calls `agentos upgrade` makes, in order."""

    def __init__(self, calls: list[str], *, state: str = "running", managed: bool = True) -> None:
        self._calls = calls
        self._state = state
        self._managed = managed
        self.stop_exit_code = 0
        self.start_exit_code = 0

    def status(self) -> Any:
        self._calls.append("status")
        return types.SimpleNamespace(state=self._state, managed=self._managed)

    def stop(self) -> Any:
        self._calls.append("stop")
        return types.SimpleNamespace(
            exit_code=self.stop_exit_code, message="", code="", state="stopped"
        )

    def start(self) -> Any:
        self._calls.append("start")
        return types.SimpleNamespace(
            exit_code=self.start_exit_code, message="", code="", state="running"
        )

    def restart(self) -> Any:
        self._calls.append("restart")
        return types.SimpleNamespace(exit_code=0, message="", code="", state="running")


def _install_fake_lifecycle(
    monkeypatch: pytest.MonkeyPatch, manager: _FakeLifecycleManager
) -> None:
    from agentos.cli import gateway_cmd

    monkeypatch.setattr(gateway_cmd, "_lifecycle_manager", lambda **_: manager)


def _lock_failure(*_: Any, **__: Any) -> upgrade_cmd.UpgradeRunResult:
    return upgrade_cmd.UpgradeRunResult(
        ok=False,
        timed_out=False,
        returncode=2,
        stdout="",
        stderr=(
            r"error: failed to remove directory "
            r"`...\uv\tools\use-agent-os\Scripts`: "
            "Access is denied. (os error 5)"
        ),
    )


def _upgrade_with_recorded_restart(
    monkeypatch: pytest.MonkeyPatch,
    *,
    on_windows: bool,
    run: Any = _ok_run,
    args: list[str] | None = None,
) -> tuple[Any, list[dict[str, Any]]]:
    restart_kwargs: list[dict[str, Any]] = []

    def fake_restart(**kwargs: Any) -> bool:
        restart_kwargs.append(kwargs)
        return True

    monkeypatch.setattr(upgrade_cmd, "_ON_WINDOWS", on_windows)
    monkeypatch.setattr(upgrade_cmd, "build_upgrade_plan", _delegated_plan)
    monkeypatch.setattr(upgrade_cmd, "_run_upgrade_subprocess", run)
    monkeypatch.setattr(upgrade_cmd, "_installed_version_via", lambda *a, **k: "99999.2.0")
    monkeypatch.setattr(upgrade_cmd, "_restart_and_verify", fake_restart)

    result = runner.invoke(_app(), ["upgrade", *(args or [])])
    return result, restart_kwargs


def test_windows_stops_the_managed_gateway_before_replacing_its_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    _install_fake_lifecycle(monkeypatch, _FakeLifecycleManager(calls))

    result, restart_kwargs = _upgrade_with_recorded_restart(monkeypatch, on_windows=True)

    assert result.exit_code == 0
    assert calls == ["status", "stop"]
    # Already stopped, so the gateway has to be started, not restarted.
    assert restart_kwargs[0]["start_only"] is True


def test_posix_keeps_the_restart_afterwards_path(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    _install_fake_lifecycle(monkeypatch, _FakeLifecycleManager(calls))

    result, restart_kwargs = _upgrade_with_recorded_restart(monkeypatch, on_windows=False)

    assert result.exit_code == 0
    assert calls == []
    assert restart_kwargs[0]["start_only"] is False


def test_windows_no_restart_leaves_the_gateway_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    _install_fake_lifecycle(monkeypatch, _FakeLifecycleManager(calls))
    monkeypatch.setattr(upgrade_cmd, "_ON_WINDOWS", True)
    monkeypatch.setattr(upgrade_cmd, "build_upgrade_plan", _delegated_plan)
    monkeypatch.setattr(upgrade_cmd, "_run_upgrade_subprocess", _ok_run)
    monkeypatch.setattr(upgrade_cmd, "_installed_version_via", lambda *a, **k: "99999.2.0")

    result = runner.invoke(_app(), ["upgrade", "--no-restart"])

    assert result.exit_code == 0
    assert calls == []


def test_windows_unmanaged_gateway_is_not_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    _install_fake_lifecycle(
        monkeypatch, _FakeLifecycleManager(calls, state="unmanaged", managed=False)
    )

    result, restart_kwargs = _upgrade_with_recorded_restart(monkeypatch, on_windows=True)

    assert result.exit_code == 0
    assert calls == ["status"]
    assert restart_kwargs[0]["start_only"] is False


def test_failed_upgrade_starts_the_gateway_it_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    _install_fake_lifecycle(monkeypatch, _FakeLifecycleManager(calls))

    result, _ = _upgrade_with_recorded_restart(monkeypatch, on_windows=True, run=_lock_failure)

    assert result.exit_code == 1
    assert calls == ["status", "stop", "start"]


def test_access_denied_failure_names_the_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    _install_fake_lifecycle(monkeypatch, _FakeLifecycleManager(calls))

    result, _ = _upgrade_with_recorded_restart(monkeypatch, on_windows=True, run=_lock_failure)

    assert result.exit_code == 1
    assert "access denied" in result.stdout
    assert "agentos gateway stop" in result.stdout
    # Rich wraps the panel-free console output, so assert on stable fragments.
    assert "uv tool install --force" in result.stdout
    assert "uv tool update-shell" in result.stdout


def test_ordinary_failure_does_not_print_the_lock_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _plain_failure(*_: Any, **__: Any) -> upgrade_cmd.UpgradeRunResult:
        return upgrade_cmd.UpgradeRunResult(
            ok=False,
            timed_out=False,
            returncode=1,
            stdout="",
            stderr="error: no solution found for use-agent-os",
        )

    calls: list[str] = []
    _install_fake_lifecycle(monkeypatch, _FakeLifecycleManager(calls))

    result, _ = _upgrade_with_recorded_restart(monkeypatch, on_windows=True, run=_plain_failure)

    assert result.exit_code == 1
    assert "uv tool update-shell" not in result.stdout


@pytest.mark.parametrize(
    "stderr",
    [
        "Access is denied. (os error 5)",
        "ACCESS IS DENIED",
        "PermissionError: [WinError 5] Access is denied",
    ],
)
def test_looks_like_file_lock_matches_windows_denials(stderr: str) -> None:
    result = upgrade_cmd.UpgradeRunResult(
        ok=False, timed_out=False, returncode=2, stdout="", stderr=stderr
    )

    assert upgrade_cmd._looks_like_file_lock(result) is True


def test_looks_like_file_lock_ignores_ordinary_failures() -> None:
    result = upgrade_cmd.UpgradeRunResult(
        ok=False, timed_out=False, returncode=1, stdout="", stderr="no solution found"
    )

    assert upgrade_cmd._looks_like_file_lock(result) is False


def test_start_only_starts_instead_of_restarting(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    _install_fake_lifecycle(monkeypatch, _FakeLifecycleManager(calls))
    monkeypatch.setattr(upgrade_cmd, "_query_gateway_version", lambda _: "99999.2.0")

    verified = upgrade_cmd._restart_and_verify(
        config_path=None,
        expected_version="99999.2.0",
        json_output=False,
        start_only=True,
    )

    assert verified is True
    assert calls == ["start"]


def test_stop_failure_falls_back_to_the_restart_path(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    manager = _FakeLifecycleManager(calls)
    manager.stop_exit_code = 1
    _install_fake_lifecycle(monkeypatch, manager)

    result, restart_kwargs = _upgrade_with_recorded_restart(monkeypatch, on_windows=True)

    assert result.exit_code == 0
    assert calls == ["status", "stop"]
    assert restart_kwargs[0]["start_only"] is False
