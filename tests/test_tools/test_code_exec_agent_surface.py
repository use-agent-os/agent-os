"""``execute_code`` runs the agent's code inside an exec window, like a shell does.

Before, it ran a host interpreter with no window and no token: a connection
the code opened to the gateway was ``unbound`` (approvals accepted it) and the
operator file, if the code got hold of it, was honoured.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.gateway.agent_surface import AgentBinding, get_agent_surface
from agentos.tools.builtin import code_exec
from agentos.tools.types import CallerKind, ToolContext, current_tool_context


@pytest.mark.asyncio
async def test_execute_code_holds_an_exec_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    surface = get_agent_surface()
    surface.reset()
    file_secret = surface.ensure_operator_file(tmp_path / "home").read_text().strip()
    host_runtime = SimpleNamespace(
        effective=SimpleNamespace(sandbox_enabled=False), workspace=tmp_path
    )
    monkeypatch.setattr(code_exec, "get_runtime", lambda: host_runtime)
    seen: list[Any] = []

    class _Proc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"ok\n", b""

    async def fake_exec(*_args: Any, **_kwargs: Any) -> _Proc:
        # What a connection the code opens right now would be bound as.
        seen.append(surface.bind({}))
        seen.append(surface.bind({"operatorSecret": file_secret}))
        return _Proc()

    monkeypatch.setattr(code_exec.asyncio, "create_subprocess_exec", fake_exec)
    token = current_tool_context.set(
        ToolContext(
            caller_kind=CallerKind.CLI,
            session_key="agent:main:code",
            workspace_dir=str(tmp_path / "ws"),
            elevated="bypass",
        )
    )
    try:
        out = await code_exec.execute_code("print('ok')")
    finally:
        current_tool_context.reset(token)
        assert surface.active_windows() == 0
        surface.reset()
    assert '"exit_code": 0' in out
    window = AgentBinding(via="window", session_key="agent:main:code")
    assert seen == [window, window]
