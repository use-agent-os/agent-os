"""Install-method detection + PATH hardening (Hermes lesson)."""

from __future__ import annotations

import json
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


def test_hardened_path_windows_case_insensitivity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows environment dicts with 'Path' or 'path' must preserve existing entries."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "pathsep", ";")
    env = {"Path": f"C:\\Program Files\\uv\\bin{os.pathsep}C:\\Windows\\System32"}
    out = im.hardened_path_env(env)
    assert "Path" not in out
    assert "PATH" in out
    parts = out["PATH"].split(os.pathsep)
    assert "C:\\Program Files\\uv\\bin" in parts
    assert "C:\\Windows\\System32" in parts


def test_hardened_path_windows_merges_mixed_case_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When both 'Path' and 'PATH' are present, merge and deduplicate into 'PATH'."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "pathsep", ";")
    env = {"Path": "C:\\uv\\bin", "PATH": "C:\\Windows\\System32"}
    out = im.hardened_path_env(env)
    assert "Path" not in out
    assert "PATH" in out
    parts = out["PATH"].split(os.pathsep)
    assert "C:\\uv\\bin" in parts
    assert "C:\\Windows\\System32" in parts


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific Path resolution")
def test_resolve_tool_finds_tool_with_windows_path_casing(tmp_path: Path) -> None:
    """resolve_tool must locate tools even when the input environment uses 'Path'."""
    uv_dir = tmp_path / "custom_tool_dir"
    uv_dir.mkdir()
    tool_file = uv_dir / "fake_uv.exe"
    tool_file.write_text("")
    resolved = im.resolve_tool("fake_uv", {"Path": str(uv_dir)})
    assert resolved == str(tool_file.resolve())


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


def test_build_plan_uv_tool_installs_the_published_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ``install``, not ``upgrade``: ``uv tool upgrade`` takes a bare NAME and
    # re-resolves uv's receipt, so an install laid down from a local checkout
    # would keep rebuilding from the working tree and re-package its stale
    # Control UI bundle instead of fetching the CI-built wheel.
    monkeypatch.setattr(im, "resolve_tool", lambda tool, env=None: "/abs/uv")
    plan = im.build_upgrade_plan(method=InstallMethod.UV_TOOL, python_tag="3.12")
    assert plan.delegated is True
    assert plan.tool == "/abs/uv"
    assert plan.command == [
        "/abs/uv",
        "tool",
        "install",
        "--force",
        "--python",
        "3.12",
        "use-agent-os[recommended]",
    ]


def test_build_plan_pins_the_running_python(monkeypatch: pytest.MonkeyPatch) -> None:
    # A forced reinstall must not silently move the tool venv onto another
    # interpreter, so the pin follows whatever is running the CLI.
    monkeypatch.setattr(im, "resolve_tool", lambda tool, env=None: "/abs/uv")
    pinned = im.build_upgrade_plan(method=InstallMethod.UV_TOOL, python_tag="3.13")
    assert pinned.command[pinned.command.index("--python") + 1] == "3.13"

    default = im.build_upgrade_plan(method=InstallMethod.UV_TOOL)
    assert default.command[default.command.index("--python") + 1] == im.runtime_python_tag()
    assert im.runtime_python_tag() == f"{sys.version_info.major}.{sys.version_info.minor}"


def test_build_plan_uv_tool_missing_uv_not_delegated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(im, "resolve_tool", lambda tool, env=None: None)
    plan = im.build_upgrade_plan(method=InstallMethod.UV_TOOL)
    assert plan.delegated is False
    assert plan.tool is None
    assert "uv tool install --force" in plan.manual_hint
    assert "use-agent-os[recommended]" in plan.manual_hint


def test_build_plan_pipx_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(im, "resolve_tool", lambda tool, env=None: "/abs/pipx")
    plan = im.build_upgrade_plan(method=InstallMethod.PIPX)
    assert plan.delegated is True
    assert plan.command == ["/abs/pipx", "install", "--force", "use-agent-os[recommended]"]


def test_build_plan_pip_never_delegates() -> None:
    plan = im.build_upgrade_plan(method=InstallMethod.PIP)
    assert plan.delegated is False
    assert "pip install --upgrade" in plan.manual_hint
    assert "use-agent-os[recommended]" in plan.manual_hint


def test_build_plan_editable_points_at_the_source_installer() -> None:
    # An editable install serves the Control UI out of the checkout, and only
    # install_source.sh rebuilds that bundle before installing.
    plan = im.build_upgrade_plan(method=InstallMethod.EDITABLE)
    assert plan.delegated is False
    assert "git pull" in plan.manual_hint
    assert "scripts/install_source.sh" in plan.manual_hint


def test_build_plan_unknown_lists_all_installers() -> None:
    # Unclassifiable install: never blindly recommend pip (a uv/pipx venv has
    # no pip); list all three installers instead.
    plan = im.build_upgrade_plan(method=InstallMethod.UNKNOWN)
    assert plan.delegated is False
    assert "uv tool install --force" in plan.manual_hint
    assert "pipx install --force" in plan.manual_hint
    assert "pip install --upgrade" in plan.manual_hint
    assert plan.manual_hint.count("use-agent-os[recommended]") == 3


def test_every_delegated_plan_carries_the_recommended_extras(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Dropping the extras is silent: the ONNX embedding models and the pilot
    # router degrade at runtime, not at install time.
    monkeypatch.setattr(im, "resolve_tool", lambda tool, env=None: f"/abs/{tool}")
    for method in (InstallMethod.UV_TOOL, InstallMethod.PIPX):
        plan = im.build_upgrade_plan(method=method)
        assert plan.delegated is True
        assert plan.command[-1] == "use-agent-os[recommended]"


# --- installed_from_directory (PEP 610) ------------------------------------


def _direct_url(monkeypatch: pytest.MonkeyPatch, payload: str | None) -> None:
    parsed = json.loads(payload) if payload else None
    monkeypatch.setattr(im, "_direct_url_payload", lambda dist: parsed)


def test_installed_from_directory_reads_a_file_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _direct_url(
        monkeypatch,
        '{"url": "file:///w/checkouts/agent-os", "dir_info": {"editable": false}}',
    )
    assert im.installed_from_directory() == Path("/w/checkouts/agent-os")


def test_installed_from_directory_decodes_percent_escapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Slicing the URL instead of url2pathname would leave the %20 in the path.
    _direct_url(monkeypatch, '{"url": "file:///tmp/a%20b/repo", "dir_info": {}}')
    assert im.installed_from_directory() == Path("/tmp/a b/repo")


def test_installed_from_directory_reports_editable_checkouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _direct_url(monkeypatch, '{"url": "file:///w/agent-os", "dir_info": {"editable": true}}')
    assert im.installed_from_directory() == Path("/w/agent-os")


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        ('{"url": "file:///tmp/x.whl", "archive_info": {}}', "a local wheel is not a checkout"),
        (
            '{"url": "git+https://example.com/a.git", "vcs_info": {"vcs": "git"}}',
            "uv's VCS clone is a cache artifact, not an editable tree",
        ),
        ('{"url": "https://pypi.org/x.whl", "dir_info": {}}', "not a file:// URL"),
        ("{}", "no direct-URL kind at all"),
    ],
)
def test_installed_from_directory_rejects_non_checkouts(
    monkeypatch: pytest.MonkeyPatch, payload: str, why: str
) -> None:
    _direct_url(monkeypatch, payload)
    # site-packages layout: the editable fallback must not fire either.
    pkg_dir = Path("/venv/lib/python3.12/site-packages/agentos")
    assert im.installed_from_directory(package_dir=pkg_dir) is None, why


def test_installed_from_directory_falls_back_to_the_src_layout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # An editable install laid down without usable metadata still has the
    # ``src/agentos`` tell that _looks_editable keys on.
    _direct_url(monkeypatch, None)
    assert im.installed_from_directory(package_dir=tmp_path / "src" / "agentos") == tmp_path


def test_installed_from_directory_handles_a_shallow_package_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The src-layout fallback indexes parents[1]; a root-level path must not
    # blow up a command whose real job is upgrading.
    _direct_url(monkeypatch, None)
    assert im.installed_from_directory(package_dir=Path("/agentos")) is None


def test_direct_url_payload_swallows_unreadable_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib.metadata

    def _raise(dist: str) -> None:
        raise importlib.metadata.PackageNotFoundError(dist)

    monkeypatch.setattr(importlib.metadata, "distribution", _raise)
    assert im._direct_url_payload("use-agent-os") is None


def test_direct_url_payload_swallows_malformed_json(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.metadata

    class _Dist:
        def read_text(self, name: str) -> str:
            return "{not json"

    monkeypatch.setattr(importlib.metadata, "distribution", lambda dist: _Dist())
    assert im._direct_url_payload("use-agent-os") is None
