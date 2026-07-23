import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE_PS1 = ROOT / "install.ps1"
RELEASE_SH = ROOT / "install.sh"
SOURCE_PS1 = ROOT / "scripts" / "install_source.ps1"
SOURCE_SH = ROOT / "scripts" / "install_source.sh"
CURRENT_RELEASE_TAG = "v2026.7.23"


def test_source_install_scripts_force_refresh_local_uv_tool_package() -> None:
    ps1 = SOURCE_PS1.read_text(encoding="utf-8")
    sh = SOURCE_SH.read_text(encoding="utf-8")

    assert "'--force', '--reinstall-package', 'use-agent-os'" in ps1
    assert "--force --reinstall-package use-agent-os" in sh


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

    assert 'if ($LASTEXITCODE -ne 0) {' in ps1
    assert "install_source.ps1: install command failed with exit code $LASTEXITCODE." in ps1
    assert (
        "Close any running AgentOS gateway or shell using the existing "
        "tool environment, then retry."
        in ps1
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
