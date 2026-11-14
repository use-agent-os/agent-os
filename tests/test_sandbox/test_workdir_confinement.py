"""AOS-05: a model-chosen ``workdir`` must not become the sandbox mount.

With the OS sandbox on, ``exec_command`` used to hand its resolved ``workdir``
to ``gate_action`` as the *workspace*, so ``workdir="/"`` or ``$HOME`` turned
into ``--bind / /workspace`` (bubblewrap) and ``(allow file-write* (subpath
"/"))`` (seatbelt). The configured workspace is now always the mount; the
workdir is only a chdir inside it, and one outside every mount is refused.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agentos.sandbox.backend import bubblewrap, seatbelt
from agentos.sandbox.config import SandboxSettings
from agentos.sandbox.integration import (
    configure_runtime,
    gate_action,
    reset_runtime,
)
from agentos.sandbox.policy import build_policy
from agentos.sandbox.types import (
    DenialResult,
    SandboxBackendError,
    SandboxRequest,
    SandboxResult,
    SecurityLevel,
)
from agentos.tools.builtin import git, shell
from agentos.tools.types import CallerKind, ToolContext, current_tool_context

#: bubblewrap (Linux) and seatbelt (macOS) model in-sandbox paths as POSIX;
#: on Windows ``Path("/workspace")`` is not absolute and neither backend runs.
POSIX_BACKENDS = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="bubblewrap/seatbelt backends model POSIX sandbox paths",
)


@pytest.fixture
def sandboxed_ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    fakehome = tmp_path / "fakehome"
    fakehome.mkdir()
    reset_runtime()
    configure_runtime(
        SandboxSettings(sandbox=True, security_grading=True, backend="noop"),
        workspace=ws,
    )
    token = current_tool_context.set(
        ToolContext(
            caller_kind=CallerKind.CLI,
            session_key="agent:main:test",
            workspace_dir=str(ws),
            elevated="off",
        )
    )
    captured: list[SandboxRequest] = []

    async def record(request: SandboxRequest, **_kwargs: object) -> SandboxResult:
        captured.append(request)
        return SandboxResult(
            returncode=0, stdout="", stderr="", wall_time_s=0.0, backend_used="noop"
        )

    monkeypatch.setattr(shell, "run_under_backend", record)
    monkeypatch.setattr(git, "run_under_backend", record)
    yield ws, fakehome, captured
    current_tool_context.reset(token)
    reset_runtime()


def _write_rules(profile: str) -> list[str]:
    return [line for line in profile.splitlines() if "file-write*" in line]


@pytest.mark.asyncio
@pytest.mark.parametrize("outside", ["/", "fakehome"])
async def test_exec_workdir_outside_workspace_is_refused(sandboxed_ws, outside: str) -> None:
    ws, fakehome, captured = sandboxed_ws
    workdir = "/" if outside == "/" else str(fakehome)

    result = json.loads(await shell.exec_command("echo pwned >> .bashrc", workdir=workdir))

    assert result["status"] == "denied"
    assert result["reason"] == "policy_denied"
    assert "outside the sandbox workspace" in result["message"]
    assert captured == []


@pytest.mark.asyncio
async def test_exec_relative_escape_is_refused(sandboxed_ws) -> None:
    _ws, _fakehome, captured = sandboxed_ws

    result = json.loads(await shell.exec_command("true", workdir="../fakehome"))

    assert result["status"] == "denied"
    assert captured == []


@pytest.mark.asyncio
async def test_background_workdir_outside_workspace_is_refused(
    sandboxed_ws, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ws, fakehome, _captured = sandboxed_ws

    async def must_not_spawn(**_kwargs: object) -> object:
        raise AssertionError("background process must not spawn outside the workspace")

    monkeypatch.setattr(shell, "_spawn_sandboxed_background_process", must_not_spawn)

    result = json.loads(await shell.background_process("sleep 1", workdir=str(fakehome)))

    assert result["status"] == "denied"
    assert result["reason"] == "policy_denied"


@pytest.mark.asyncio
async def test_exec_workdir_inside_workspace_mounts_root_and_chdirs(sandboxed_ws) -> None:
    ws, _fakehome, captured = sandboxed_ws

    await shell.exec_command("true", workdir="sub")

    assert len(captured) == 1
    request = captured[0]
    mounts = [(m.host_path, m.sandbox_path, m.mode) for m in request.policy.mounts]
    assert mounts == [(ws, Path("/workspace"), "rw")]
    assert request.cwd == ws / "sub"


@POSIX_BACKENDS
@pytest.mark.asyncio
async def test_backends_chdir_inside_the_workspace_mount(sandboxed_ws) -> None:
    ws, _fakehome, captured = sandboxed_ws

    await shell.exec_command("true", workdir="sub")
    request = captured[0]

    argv = bubblewrap.build_bwrap_argv(request)
    bind = argv.index("--bind")
    assert argv[bind : bind + 3] == ["--bind", str(ws), "/workspace"]
    chdir = argv.index("--chdir")
    assert argv[chdir + 1] == "/workspace/sub"
    assert "/" not in [argv[i + 1] for i, a in enumerate(argv) if a == "--bind"]

    profile = seatbelt.render_seatbelt_profile(request)
    assert _write_rules(profile) == [f'(allow file-write* (subpath "{ws}"))']


@pytest.mark.asyncio
async def test_absolute_workdir_inside_workspace_still_runs(sandboxed_ws) -> None:
    ws, _fakehome, captured = sandboxed_ws

    await shell.exec_command("true", workdir=str(ws / "sub"))

    assert [r.cwd for r in captured] == [ws / "sub"]
    assert captured[0].policy.mounts[0].host_path == ws


@pytest.mark.asyncio
async def test_gate_action_never_mounts_cwd(sandboxed_ws) -> None:
    ws, fakehome, _captured = sandboxed_ws

    decision, policy, request = await gate_action(
        action_kind="shell.exec", argv=("exec_command", "true"), cwd=fakehome
    )

    assert isinstance(decision, DenialResult)
    assert [m.host_path for m in policy.mounts] == [ws]
    assert request.cwd == fakehome


@pytest.mark.asyncio
async def test_git_sandbox_mount_is_workspace_not_workdir(sandboxed_ws) -> None:
    ws, fakehome, captured = sandboxed_ws

    await git._run_git("status", cwd=str(ws / "sub"))
    assert captured[0].policy.mounts[0].host_path == ws
    assert captured[0].cwd == ws / "sub"

    with pytest.raises(RuntimeError, match="outside the sandbox workspace"):
        await git._run_git("commit", "-m", "x", cwd=str(fakehome))
    assert len(captured) == 1


@POSIX_BACKENDS
def test_bwrap_refuses_cwd_outside_every_mount(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    policy = build_policy(SecurityLevel.STANDARD, "shell.exec", ws, SandboxSettings(sandbox=True))
    request = SandboxRequest(argv=("true",), cwd=Path("/"), action_kind="shell.exec", policy=policy)

    with pytest.raises(SandboxBackendError, match="outside every sandbox mount"):
        bubblewrap.build_bwrap_argv(request)


@POSIX_BACKENDS
def test_seatbelt_refuses_cwd_outside_every_mount(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    policy = build_policy(SecurityLevel.STANDARD, "shell.exec", ws, SandboxSettings(sandbox=True))
    request = SandboxRequest(argv=("true",), cwd=tmp_path, action_kind="shell.exec", policy=policy)

    with pytest.raises(SandboxBackendError, match="outside every sandbox mount"):
        seatbelt._validate_request(request)
