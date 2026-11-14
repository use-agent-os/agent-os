"""AOS-16: an exec approval authorizes exactly what the operator was shown.

The retry path (``approval_id`` supplied) compared only the tool name and the
command text, so an approval for ``rm -rf build`` in workdir A from session A
could be consumed for the same text in another workdir or another session,
and env overrides were never part of the approval at all. The intent cache
had the same blind spot: a relative target resolved against the gateway's
process directory rather than the command's workdir.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentos.gateway.approval_queue import get_approval_queue, reset_approval_queue
from agentos.sandbox.integration import reset_runtime
from agentos.sandbox.intent_cache import reset_intent_cache
from agentos.tools.builtin import shell
from agentos.tools.types import CallerKind, ToolContext, ToolError, current_tool_context

_OPERATOR = "agent:main:operator"
_CMD = "rm -rf build"


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    reset_approval_queue()
    reset_intent_cache()
    reset_runtime()
    get_approval_queue().set_settings("prompt")
    ws_a = tmp_path / "wsA"
    other = tmp_path / "home" / "other"
    ws_a.mkdir()
    other.mkdir(parents=True)
    elevate_token = shell._elevate_current_call.set(False)
    token = current_tool_context.set(
        ToolContext(caller_kind=CallerKind.CLI, session_key=_OPERATOR, workspace_dir=str(ws_a))
    )
    yield ws_a, other
    current_tool_context.reset(token)
    shell._elevate_current_call.reset(elevate_token)
    reset_approval_queue()
    reset_intent_cache()


async def _approved_id(workdir: Path, env: dict[str, str] | None = None) -> str:
    pending = await shell._check_exec_approval(
        "exec_command", _CMD, str(workdir), "warn", None, False, env=env
    )
    assert pending is not None and pending["status"] == "approval_required"
    approval_id = str(pending["approval_id"])
    get_approval_queue().resolve(approval_id, True)
    return approval_id


@pytest.mark.asyncio
async def test_retry_in_a_different_workdir_is_refused(dirs) -> None:
    ws_a, other = dirs
    approval_id = await _approved_id(ws_a)

    with pytest.raises(ToolError, match="workdir"):
        await shell._check_exec_approval(
            "exec_command", _CMD, str(other), "warn", approval_id, False
        )

    assert shell._elevate_current_call.get() is False
    assert get_approval_queue().get(approval_id).consumed is False


@pytest.mark.asyncio
async def test_retry_from_a_different_session_is_refused(dirs) -> None:
    ws_a, _other = dirs
    approval_id = await _approved_id(ws_a)

    token = current_tool_context.set(
        ToolContext(
            caller_kind=CallerKind.CLI,
            session_key="agent:main:telegram:dm:999",
            workspace_dir=str(ws_a),
        )
    )
    try:
        with pytest.raises(ToolError, match="different session"):
            await shell._check_exec_approval(
                "exec_command", _CMD, str(ws_a), "warn", approval_id, False
            )
    finally:
        current_tool_context.reset(token)

    assert get_approval_queue().get(approval_id).consumed is False


@pytest.mark.asyncio
async def test_retry_with_different_env_is_refused(dirs) -> None:
    ws_a, _other = dirs
    approval_id = await _approved_id(ws_a)

    with pytest.raises(ToolError, match="env"):
        await shell._check_exec_approval(
            "exec_command",
            _CMD,
            str(ws_a),
            "warn",
            approval_id,
            False,
            env={"PATH": "/tmp/evil"},
        )

    assert get_approval_queue().get(approval_id).consumed is False


@pytest.mark.asyncio
async def test_exact_retry_is_granted(dirs) -> None:
    ws_a, _other = dirs
    approval_id = await _approved_id(ws_a, env={"FOO": "1"})

    result = await shell._check_exec_approval(
        "exec_command", _CMD, str(ws_a), "warn", approval_id, False, env={"FOO": "1"}
    )

    assert result is None
    assert shell._elevate_current_call.get() is True
    assert get_approval_queue().get(approval_id).consumed is True


@pytest.mark.asyncio
async def test_exec_command_env_is_on_the_approval_card(dirs) -> None:
    ws_a, _other = dirs

    pending = json.loads(
        await shell.exec_command(_CMD, workdir=str(ws_a), env={"LD_PRELOAD": "/tmp/x.so"})
    )

    assert pending["status"] == "approval_required"
    entry = get_approval_queue().get(pending["approval_id"])
    assert entry.params["args"]["env"] == {"LD_PRELOAD": "/tmp/x.so"}
    assert entry.params["args"]["workdir"] == str(ws_a.resolve())

    # Retrying without the env the operator saw does not consume the approval.
    get_approval_queue().resolve(pending["approval_id"], True)
    with pytest.raises(ToolError):
        await shell.exec_command(_CMD, workdir=str(ws_a), approval_id=pending["approval_id"])


@pytest.mark.asyncio
async def test_remembered_intent_is_bound_to_the_workdir(
    dirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_a, other = dirs
    # The intent short-circuit only runs when the sandbox is on.
    monkeypatch.setattr(shell, "_sandbox_effectively_off", lambda: False)
    pending = await shell._check_exec_approval("exec_command", _CMD, str(ws_a), "warn", None, False)
    assert pending is not None
    get_approval_queue().resolve(str(pending["approval_id"]), True, remember_intent=True)

    same_dir = await shell._check_exec_approval(
        "exec_command", _CMD, str(ws_a), "warn", None, False
    )
    assert same_dir is None

    shell._elevate_current_call.set(False)
    elsewhere = await shell._check_exec_approval(
        "exec_command", _CMD, str(other), "warn", None, False
    )
    assert elsewhere is not None and elsewhere["status"] == "approval_required"
    assert shell._elevate_current_call.get() is False
