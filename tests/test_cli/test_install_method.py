"""Install-method detection + PATH hardening (Hermes lesson)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from agentos.cli import install_method as im
from agentos.cli.install_method import InstallMethod


@pytest.mark.parametrize(
    ("exe", "pkg", "expected"),
    [
        # uv tool install
        (
            "/home/u/.local/share/uv/tools/use-agent-os/bin/python",
            "/home/u/.local/share/uv/tools/use-agent-os/lib/python3.12/site-packages/agentos",
            InstallMethod.UV_TOOL,
        ),
        # pipx venv
        (
            "/home/u/.local/share/pipx/venvs/use-agent-os/bin/python",
            "/home/u/.local/share/pipx/venvs/use-agent-os/lib/python3.12/site-packages/agentos",
            InstallMethod.PIPX,
        ),
        # plain pip into a virtualenv site-packages
        (
            "/home/u/venv/bin/python",
            "/home/u/venv/lib/python3.12/site-packages/agentos",
            InstallMethod.PIP,
        ),
        # system dist-packages
        (
            "/usr/bin/python3",
            "/usr/lib/python3/dist-packages/agentos",
            InstallMethod.PIP,
        ),
    ],
)
def test_detect_install_method(exe: str, pkg: str, expected: InstallMethod) -> None:
    # Empty env so a real UV_TOOL_DIR in the test host cannot skew classification.
    assert im.detect_install_method(executable=exe, package_dir=Path(pkg), env={}) == expected


def test_uv_tool_dir_override_classified_as_uv_tool(tmp_path: Path) -> None:
    # A custom UV_TOOL_DIR relocates the whole tools tree away from the default
    # ~/.local/share/uv/tools heuristic.
    tool_dir = tmp_path / "custom-uv-tools"
    exe = tool_dir / "use-agent-os" / "bin" / "python"
    pkg = tool_dir / "use-agent-os" / "lib" / "python3.12" / "site-packages" / "agentos"
    pkg.mkdir(parents=True)
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    assert (
        im.detect_install_method(
            executable=str(exe),
            package_dir=pkg,
            env={"UV_TOOL_DIR": str(tool_dir)},
        )
        == InstallMethod.UV_TOOL
    )


def test_uv_tool_dir_symlinked_bin_classified_as_uv_tool(tmp_path: Path) -> None:
    # The executable is a symlink from a bin dir OUTSIDE the tools tree into a
    # real python UNDER UV_TOOL_DIR — resolving the symlink must still classify.
    tool_dir = tmp_path / "uv-tools"
    real_exe = tool_dir / "use-agent-os" / "bin" / "python"
    real_exe.parent.mkdir(parents=True)
    real_exe.write_text("")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    link = bin_dir / "agentos"
    try:
        link.symlink_to(real_exe)
    except OSError as e:
        pytest.skip(f"Symlinks are not allowed or supported: {e}")
    # package_dir is elsewhere (a shim location) so only the symlink target ties
    # the install to the tools tree.
    pkg = tmp_path / "shim" / "agentos"
    pkg.mkdir(parents=True)
    assert (
        im.detect_install_method(
            executable=str(link),
            package_dir=pkg,
            env={"UV_TOOL_DIR": str(tool_dir)},
        )
        == InstallMethod.UV_TOOL
    )


def test_uv_tool_dir_unset_leaves_default_behavior(tmp_path: Path) -> None:
    # Without UV_TOOL_DIR, a plain site-packages install stays PIP.
    exe = "/home/u/venv/bin/python"
    pkg = tmp_path / "venv" / "lib" / "python3.12" / "site-packages" / "agentos"
    pkg.mkdir(parents=True)
    assert im.detect_install_method(executable=exe, package_dir=pkg, env={}) == InstallMethod.PIP
    # And the default ~/.local/share/uv/tools path still matches on the heuristic.
    assert (
        im.detect_install_method(
            executable="/home/u/.local/share/uv/tools/use-agent-os/bin/python",
            package_dir=Path(
                "/home/u/.local/share/uv/tools/use-agent-os/lib/python3.12/site-packages/agentos"
            ),
            env={},
        )
        == InstallMethod.UV_TOOL
    )


def test_editable_checkout_detected(tmp_path: Path) -> None:
    # Mimic a src/agentos editable layout with a sibling pyproject.toml.
    src = tmp_path / "src"
    pkg = src / "agentos"
    pkg.mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='use-agent-os'\n")
    assert (
        im.detect_install_method(executable="/usr/bin/python3", package_dir=pkg)
        == InstallMethod.EDITABLE
    )


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="hardened login-dir PATH semantics are POSIX-specific; "
    "Windows resolution uses standard which/PATHEXT",
)
def test_hardened_path_appends_login_dirs() -> None:
    env = {"PATH": "/custom/bin", "HOME": "/home/u"}
    out = im.hardened_path_env(env)
    parts = out["PATH"].split(os.pathsep)
    assert parts[0] == "/custom/bin"  # operator ordering preserved
    assert "/opt/homebrew/bin" in parts
    assert "/usr/local/bin" in parts
    assert "/home/u/.local/bin" in parts


def test_hardened_path_no_duplicates() -> None:
    env = {"PATH": "/opt/homebrew/bin:/x", "HOME": "/home/u"}
    parts = im.hardened_path_env(env)["PATH"].split(os.pathsep)
    assert parts.count("/opt/homebrew/bin") == 1


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="hardened login-dir PATH semantics are POSIX-specific; "
    "Windows resolution uses standard which/PATHEXT",
)
def test_resolve_tool_uses_hardened_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # uv lives in a login dir NOT on the base PATH.
    brew = tmp_path / "opt" / "homebrew" / "bin"
    brew.mkdir(parents=True)
    uv_bin = brew / "uv"
    uv_bin.write_text("#!/bin/sh\n")
    uv_bin.chmod(0o755)

    import agentos.cli.install_method as mod

    monkeypatch.setattr(mod, "_LOGIN_PATH_DIRS", (str(brew),))
    resolved = im.resolve_tool("uv", {"PATH": "/nowhere", "HOME": str(tmp_path)})
    assert resolved == str(uv_bin.resolve())


def test_resolve_tool_missing_returns_none() -> None:
    assert im.resolve_tool("definitely-not-a-real-tool-xyz", {"PATH": "/nonexistent"}) is None


def test_resolve_tool_falls_back_to_which(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cross-platform: resolve_tool defers to shutil.which and returns its hit.

    Runs on both POSIX and Windows — shutil.which is mocked so no real binary
    (or PATHEXT / executable-bit) semantics are involved, only that resolve_tool
    hardens the PATH, delegates to which, and returns the absolute path without
    crashing.
    """

    seen: dict[str, object] = {}
    resolved_path = str(Path("some") / "abs" / "uv")

    def fake_which(tool: str, path: str | None = None) -> str:
        seen["tool"] = tool
        seen["path"] = path
        return resolved_path

    monkeypatch.setattr(im.shutil, "which", fake_which)
    result = im.resolve_tool("uv", {"PATH": "/base", "HOME": "/home/u"})

    assert result == str(Path(resolved_path).resolve())
    assert seen["tool"] == "uv"
    # resolve_tool hardens the PATH before delegating, so which sees the
    # augmented PATH, not the bare base.
    assert isinstance(seen["path"], str)
    assert "/base" in seen["path"] or "\\base" in seen["path"]


def test_build_plan_uv_tool_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(im, "resolve_tool", lambda tool, env=None: "/abs/uv")
    plan = im.build_upgrade_plan(method=InstallMethod.UV_TOOL)
    assert plan.delegated is True
    assert plan.tool == "/abs/uv"
    assert plan.command == ["/abs/uv", "tool", "upgrade", "use-agent-os", "--reinstall"]


def test_build_plan_uv_tool_missing_uv_not_delegated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(im, "resolve_tool", lambda tool, env=None: None)
    plan = im.build_upgrade_plan(method=InstallMethod.UV_TOOL)
    assert plan.delegated is False
    assert plan.tool is None
    assert "uv tool upgrade" in plan.manual_hint


def test_build_plan_pipx_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(im, "resolve_tool", lambda tool, env=None: "/abs/pipx")
    plan = im.build_upgrade_plan(method=InstallMethod.PIPX)
    assert plan.delegated is True
    assert plan.command == ["/abs/pipx", "upgrade", "use-agent-os", "--force"]


def test_build_plan_pip_never_delegates() -> None:
    plan = im.build_upgrade_plan(method=InstallMethod.PIP)
    assert plan.delegated is False
    assert "pip install --upgrade use-agent-os" in plan.manual_hint


def test_build_plan_editable_never_delegates() -> None:
    plan = im.build_upgrade_plan(method=InstallMethod.EDITABLE)
    assert plan.delegated is False
    assert "git pull" in plan.manual_hint


def test_build_plan_unknown_lists_all_installers() -> None:
    # Unclassifiable install: never blindly recommend pip (a uv/pipx venv has
    # no pip); list all three installers instead.
    plan = im.build_upgrade_plan(method=InstallMethod.UNKNOWN)
    assert plan.delegated is False
    assert "uv tool install use-agent-os" in plan.manual_hint
    assert "pipx install use-agent-os" in plan.manual_hint
    assert "pip install --upgrade use-agent-os" in plan.manual_hint
