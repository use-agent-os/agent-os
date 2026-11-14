"""The shell tool marks its children as an agent's, and the gateway can tell."""

from __future__ import annotations

import asyncio
import os

import pytest

from agentos.gateway.agent_surface import AGENT_TOKEN_ENV, get_agent_surface
from agentos.tools.builtin import shell
from agentos.tools.types import CallerKind, ToolContext, current_tool_context


@pytest.fixture(autouse=True)
def _agent_turn(monkeypatch: pytest.MonkeyPatch):
    get_agent_surface().reset()
    monkeypatch.setattr(shell, "_sandbox_effectively_off", lambda: True)
    token = current_tool_context.set(
        ToolContext(caller_kind=CallerKind.CLI, session_key="agent:trading:webchat:t")
    )
    yield
    current_tool_context.reset(token)
    get_agent_surface().reset()


def test_session_env_carries_a_gateway_minted_token() -> None:
    env = {"AGENTOS_SESSION_KEY": "", "PATH": "/usr/bin"}
    shell._add_session_env(env)
    assert env["AGENTOS_SESSION_KEY"] == "agent:trading:webchat:t"
    binding = get_agent_surface().resolve_token(env[AGENT_TOKEN_ENV])
    assert binding is not None and binding.session_key == "agent:trading:webchat:t"


def test_a_caller_cannot_plant_its_own_token() -> None:
    env = {AGENT_TOKEN_ENV: "forged"}
    shell._add_session_env(env)
    assert env[AGENT_TOKEN_ENV] != "forged"
    assert get_agent_surface().resolve_token("forged") is None


def test_no_tool_context_means_no_token() -> None:
    reset = current_tool_context.set(None)
    try:
        env: dict[str, str] = {}
        shell._add_session_env(env)
        assert AGENT_TOKEN_ENV not in env
    finally:
        current_tool_context.reset(reset)


@pytest.mark.asyncio
async def test_exec_command_holds_an_exec_window_while_the_child_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    surface = get_agent_surface()
    seen: list[int] = []

    async def fake_run(command, cwd, env, timeout):
        seen.append(surface.active_windows())
        # A connection opened now, presenting nothing, is the agent's.
        assert surface.bind({}) is not None
        assert surface.bind({}).session_key == "agent:trading:webchat:t"
        return "exit_code=0\n"

    monkeypatch.setattr(shell, "_run_exec_subprocess", fake_run)
    assert surface.active_windows() == 0
    out = await shell.exec_command("echo hi")
    assert out.startswith("exit_code=0")
    assert seen == [1]
    assert surface.active_windows() == 0
    assert surface.bind({}).via == "unbound"


@pytest.mark.asyncio
async def test_background_process_keeps_the_window_open_until_it_ends() -> None:
    surface = get_agent_surface()
    proc = await asyncio.create_subprocess_shell("sleep 0.2", stdout=asyncio.subprocess.DEVNULL)
    session = shell._BgSession(
        session_id="bg1", command="sleep 0.2", process=proc, session_key="agent:trading:webchat:t"
    )
    shell._open_bg_window(session)
    assert surface.active_windows() == 1
    await proc.wait()
    shell._finalize_bg_session(session)
    assert surface.active_windows() == 0


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.mark.skipif(os.name != "posix", reason="process groups are a POSIX thing")
@pytest.mark.asyncio
async def test_a_detached_sleeper_does_not_outlive_the_exec_window(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process the command left behind is killed when the command ends.

    Otherwise it could connect to the gateway after the window closed. The
    sleeper is a ``nohup … &`` job from a nested subshell (the double-fork
    shape), writes its pid and ignores SIGTERM, so only the SIGKILL that
    follows can end it. (A true ``setsid`` escapes the process group; that
    needs a sandbox, not a signal, and is out of scope here.)
    """
    pid_file = tmp_path / "sleeper.pid"
    sleeper = f'trap "" TERM; echo $$ > {pid_file}; sleep 30'
    command = f"(nohup sh -c '{sleeper}' >/dev/null 2>&1 &) ; sleep 0.3; echo done"

    # The host path: what the sandbox backend does with a process group is
    # its own business (and the sandbox-backend fallback does the same).
    monkeypatch.setattr(shell, "get_runtime", lambda: None)
    out = await shell._run_exec_subprocess(command, str(tmp_path), None, 10)

    assert out.startswith("exit_code=0"), out
    pid = int(pid_file.read_text().strip())
    # SIGTERM was ignored, SIGKILL was not; give the kernel a moment to reap.
    for _ in range(50):
        if not _pid_alive(pid):
            break
        await asyncio.sleep(0.05)
    assert not _pid_alive(pid), f"detached sleeper {pid} survived the exec window"


@pytest.mark.skipif(os.name != "posix", reason="POSIX env(1) and process ancestry")
@pytest.mark.asyncio
async def test_cli_in_an_agent_shell_with_a_wiped_env_never_reads_the_operator_file(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``env -i … agentos …`` from the agent's shell: no variable left, same parent.

    The test process stands in for the gateway: its pid is in the state dir's
    pid file, exactly as a running gateway's is, and the shell it spawns is
    its child. The operator file is readable on purpose (a sandbox-off host).
    """
    import json
    import sys

    from agentos.gateway.agent_surface import ensure_operator_file

    state = tmp_path / "home"
    (state / "state").mkdir(parents=True)
    (state / "state" / "gateway.pid").write_text(json.dumps({"pid": os.getpid()}))
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(state))
    ensure_operator_file(state)
    monkeypatch.setattr(shell, "get_runtime", lambda: None)
    probe = (
        "from agentos.cli.gateway_client import agent_surface_auth as a;"
        "print(sorted(a('ws://127.0.0.1:18791')))"
    )
    command = (
        f'env -i PATH="$PATH" HOME="$HOME" AGENTOS_STATE_DIR="$AGENTOS_STATE_DIR" '
        f'{sys.executable} -c "{probe}"'
    )
    out = await shell.exec_command(command, workdir=str(tmp_path))
    assert out.startswith("exit_code=0"), out
    assert out.strip().splitlines()[-1] == "[]", out
