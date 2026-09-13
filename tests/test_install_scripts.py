import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RELEASE_PS1 = ROOT / "install.ps1"
RELEASE_SH = ROOT / "install.sh"
SOURCE_PS1 = ROOT / "scripts" / "install_source.ps1"
SOURCE_SH = ROOT / "scripts" / "install_source.sh"
CURRENT_RELEASE_TAG = "v2026.9.11"


def test_source_install_scripts_force_refresh_local_uv_tool_package() -> None:
    ps1 = SOURCE_PS1.read_text(encoding="utf-8")
    sh = SOURCE_SH.read_text(encoding="utf-8")

    assert "'--force', '--reinstall-package', 'use-agent-os'" in ps1
    assert "--force --reinstall-package use-agent-os" in sh


def test_cli_upgrade_targets_the_release_not_the_checkout() -> None:
    """`agentos upgrade` and install_source.sh must stay deliberately different.

    They diverged silently once already: install_source.sh builds the Control UI
    and installs the checkout, while `agentos upgrade` delegated to `uv tool
    upgrade`, which re-resolved uv's DIRECTORY receipt and re-packaged whatever
    stale `static/dist/` was on disk. Upgrade now installs the published wheel
    (whose UI is built in CI); the checkout path stays with the shell script.
    """

    from agentos.cli.install_method import InstallMethod, build_upgrade_plan

    sh = SOURCE_SH.read_text(encoding="utf-8")
    assert 'install_target=".' in sh, "install_source.sh must install the local checkout"

    plan = build_upgrade_plan(
        method=InstallMethod.UV_TOOL,
        env={"PATH": "/usr/bin"},
        python_tag="3.12",
    )
    assert plan.command[-1] == "use-agent-os[recommended]"
    assert "." not in plan.command, "upgrade must never install a local path"
    assert "upgrade" not in plan.command, "`uv tool upgrade` re-resolves the receipt"


def test_source_installers_build_control_ui_before_python_package_install() -> None:
    ps1 = SOURCE_PS1.read_text(encoding="utf-8")
    sh = SOURCE_SH.read_text(encoding="utf-8")

    for script in (ps1, sh):
        assert "scripts/build_control_ui.py" in script
        assert "Node.js 22 or newer" in script
        assert "npm" in script

    assert "node_major < 22" in sh
    assert "command -v npm" in sh
    assert sh.index('"${control_ui_build_args[@]}"') < sh.index('"${install_args[@]}"')

    assert "Get-Command npm" in ps1
    assert "-lt 22" in ps1
    assert ps1.index("install_source.ps1: building the React control UI") < ps1.index(
        "install_source.ps1: installing via"
    )


def test_source_installer_dry_run_reports_control_ui_build_without_running_it() -> None:
    if sys.platform.startswith("win"):
        return

    env = os.environ.copy()
    env["AGENTOS_INSTALL_DRY_RUN"] = "1"
    env["AGENTOS_INSTALL_PROFILE"] = "core"
    result = subprocess.run(
        ["bash", str(SOURCE_SH)],
        cwd=ROOT.parent,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert (
        "dry-run — would build control UI: python3 scripts/build_control_ui.py build"
    ) in result.stdout


def test_source_installer_rejects_node_older_than_22(tmp_path: Path) -> None:
    if sys.platform.startswith("win"):
        return

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_node = fake_bin / "node"
    fake_node.write_text("#!/bin/sh\nprintf 'v21.9.0\\n'\n", encoding="utf-8")
    fake_node.chmod(0o755)
    fake_npm = fake_bin / "npm"
    fake_npm.write_text("#!/bin/sh\nprintf '10.9.0\\n'\n", encoding="utf-8")
    fake_npm.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["AGENTOS_INSTALL_PROFILE"] = "core"
    result = subprocess.run(
        ["bash", str(SOURCE_SH)],
        cwd=ROOT.parent,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert "Node.js v21.9.0 is too old" in result.stderr
    assert "building the React control UI" not in result.stdout


def test_install_scripts_do_not_run_onboarding_or_gateway() -> None:
    scripts = [
        RELEASE_PS1.read_text(encoding="utf-8"),
        RELEASE_SH.read_text(encoding="utf-8"),
        SOURCE_PS1.read_text(encoding="utf-8"),
        SOURCE_SH.read_text(encoding="utf-8"),
    ]

    for script in scripts:
        assert "onboard --if-needed" not in script
        assert "& agentos onboard" not in script
        assert "& agentos gateway run" not in script
        assert '"agentos onboard"' not in script
        assert '"agentos gateway run"' not in script


def test_release_installers_install_version_pinned_wheel_with_uv() -> None:
    ps1 = RELEASE_PS1.read_text(encoding="utf-8")
    sh = RELEASE_SH.read_text(encoding="utf-8")

    for script in (ps1, sh):
        assert CURRENT_RELEASE_TAG in script
        assert "use_agent_os-$releaseVersion-py3-none-any.whl" in script or (
            "use_agent_os-${release_version}-py3-none-any.whl" in script
        )
        assert "use_agent_os-latest-py3-none-any.whl" not in script
        assert "releases/latest/download" not in script
        assert "--python" in script
        assert "--force" in script
        assert "--reinstall-package" in script
        assert "recommended" in script
        assert "https://astral.sh/uv/install" in script
        assert "Next steps:" in script


def test_release_installer_rejects_non_release_selectors() -> None:
    ps1 = RELEASE_PS1.read_text(encoding="utf-8")

    if not sys.platform.startswith("win"):
        result = subprocess.run(
            ["bash", "install.sh", "--version", "main"],
            capture_output=True,
            check=False,
            text=True,
        )
        assert result.returncode != 0
        assert "only supports latest, stable, or release versions" in result.stderr
        assert "scripts/install_source.sh" in result.stderr
    assert "only supports latest, stable, or release versions" in ps1
    assert "scripts/install_source.ps1" in ps1


def test_windows_installer_stops_when_native_install_command_fails() -> None:
    ps1 = SOURCE_PS1.read_text(encoding="utf-8")

    assert "if ($LASTEXITCODE -ne 0) {" in ps1
    assert "install_source.ps1: install command failed with exit code $LASTEXITCODE." in ps1
    assert (
        "Close any running AgentOS gateway or shell using the existing "
        "tool environment, then retry." in ps1
    )
    assert "exit $LASTEXITCODE" in ps1


def test_install_script_banners_are_ascii_for_windows_terminals() -> None:
    scripts = [
        RELEASE_PS1.read_text(encoding="utf-8"),
        RELEASE_SH.read_text(encoding="utf-8"),
        SOURCE_PS1.read_text(encoding="utf-8"),
        SOURCE_SH.read_text(encoding="utf-8"),
    ]

    for script in scripts:
        assert "AgentOS installed" in script
        assert "----" in script
        assert "→" not in script
        assert "─" not in script
        assert "⚠" not in script


def test_install_scripts_support_optional_extras() -> None:
    scripts = [
        RELEASE_PS1.read_text(encoding="utf-8"),
        RELEASE_SH.read_text(encoding="utf-8"),
        SOURCE_PS1.read_text(encoding="utf-8"),
        SOURCE_SH.read_text(encoding="utf-8"),
    ]

    for script in scripts:
        assert "AGENTOS_INSTALL_EXTRAS" in script
        for retired_extra in ("dingtalk", "matrix", "matrix-e2e", "qq", "wecom"):
            assert retired_extra not in script.lower()
        assert "document-extras" in script
        assert "msteams" not in script


def test_windows_installer_bootstraps_vc_redist_for_onnx_runtime() -> None:
    scripts = [
        RELEASE_PS1.read_text(encoding="utf-8"),
        SOURCE_PS1.read_text(encoding="utf-8"),
    ]

    for ps1 in scripts:
        assert "Install-WindowsVCRedistIfNeeded" in ps1
        assert "AGENTOS_SKIP_VC_REDIST" in ps1
        assert "Microsoft.VCRedist.2015+.x64" in ps1
        assert "https://aka.ms/vs/17/release/vc_redist.x64.exe" in ps1
        assert "safe embedding fallback" in ps1
        assert "If automatic installation fails, install it manually" in ps1
        assert "After installing, reopen PowerShell and restart AgentOS" in ps1


# --- install.sh stage protocol (drives the macOS desktop's first-run install) ---


def _run_install_sh(
    *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    full_env = dict(os.environ)
    full_env["AGENTOS_INSTALL_DRY_RUN"] = "1"
    if env:
        full_env.update(env)
    return subprocess.run(
        ["bash", str(RELEASE_SH), *args],
        capture_output=True,
        check=False,
        text=True,
        env=full_env,
    )


def _last_json_line(stdout: str) -> dict[str, object]:
    lines = [line for line in stdout.splitlines() if line.startswith("{")]
    assert lines, f"no JSON frame on stdout:\n{stdout}"
    payload = json.loads(lines[-1])
    assert isinstance(payload, dict)
    return payload


@pytest.mark.skipif(sys.platform.startswith("win"), reason="bash installer")
def test_install_sh_manifest_lists_stages_in_install_order() -> None:
    result = _run_install_sh("--manifest")
    assert result.returncode == 0, result.stderr
    manifest = json.loads(result.stdout.strip())
    assert manifest["protocol_version"] == 1
    names = [stage["name"] for stage in manifest["stages"]]
    assert names == ["prerequisites", "uv", "python", "package", "path", "complete"]
    for stage in manifest["stages"]:
        assert set(stage) == {"name", "title", "category", "needs_user_input"}
        assert stage["needs_user_input"] is False


@pytest.mark.skipif(sys.platform.startswith("win"), reason="bash installer")
def test_install_sh_stage_emits_a_result_frame_last_on_stdout() -> None:
    result = _run_install_sh("--stage", "package", "--json", "--non-interactive")
    assert result.returncode == 0, result.stderr
    # Progress prose precedes the frame; the frame is the last line.
    assert result.stdout.strip().splitlines()[-1].startswith("{")
    assert _last_json_line(result.stdout) == {"ok": True, "stage": "package", "skipped": False}
    assert "use_agent_os-" in result.stdout


@pytest.mark.skipif(sys.platform.startswith("win"), reason="bash installer")
def test_install_sh_unknown_stage_is_a_frame_not_a_crash() -> None:
    result = _run_install_sh("--stage", "bogus", "--json")
    assert result.returncode == 2
    assert _last_json_line(result.stdout) == {
        "ok": False,
        "stage": "bogus",
        "skipped": False,
        "reason": "unknown stage",
    }


@pytest.mark.skipif(sys.platform.startswith("win"), reason="bash installer")
def test_install_sh_failed_stage_still_emits_a_frame(tmp_path: Path) -> None:
    # The stage body calls `exit 1` inside its subshell; the caller must still
    # get {"ok": false} rather than a process that vanished mid-run. A HOME
    # with no uv and a PATH without it makes `package` fail at require_uv.
    result = _run_install_sh(
        "--stage",
        "package",
        "--json",
        env={"AGENTOS_INSTALL_DRY_RUN": "0", "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    assert result.returncode != 0
    frame = _last_json_line(result.stdout)
    assert frame["ok"] is False
    assert frame["stage"] == "package"
    assert "failed" in str(frame["reason"])
    assert "uv is not installed" in result.stderr


@pytest.mark.skipif(sys.platform.startswith("win"), reason="bash installer")
def test_install_sh_plain_run_still_walks_every_stage() -> None:
    result = _run_install_sh()
    assert result.returncode == 0, result.stderr
    for text in ("would install uv", "would ensure Python", "would install AgentOS", "PATH"):
        assert text in result.stdout + result.stderr
    assert "{" not in result.stdout, "no JSON frames outside --json mode"
