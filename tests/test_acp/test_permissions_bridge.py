"""ApprovalQueuePermissionBridge: every mode, timeout, no-surface, remember."""

from __future__ import annotations

import asyncio

import pytest
from acp_perm_helpers import make_queue, next_pending, perm_request

from agentos.acp.permissions import (
    ACP_APPROVAL_NAMESPACE,
    ApprovalQueuePermissionBridge,
    acp_approval_title,
    redact_raw_input,
)
from agentos.acp.types import PermissionOptionInfo, PermissionPromptUnavailableError
from agentos.application import approval_queue as approval_queue_mod
from agentos.gateway.config import AcpConfig
from agentos.tools.types import InteractionMode, ToolContext


def _bridge(tmp_path, **cfg):
    queue = make_queue(tmp_path)
    config = AcpConfig(**cfg)
    return ApprovalQueuePermissionBridge(config, queue=queue), queue


# -- static modes -----------------------------------------------------------


async def test_approve_all_allows_once_and_never_widens(tmp_path) -> None:
    bridge, queue = _bridge(tmp_path, permission_mode="approve-all")
    assert await bridge.decide(perm_request()) == "allow_once"
    # allow_always only when the harness offers no allow_once
    only_always = [
        PermissionOptionInfo("a", "Always", "allow_always"),
        PermissionOptionInfo("r", "Reject", "reject_once"),
    ]
    assert await bridge.decide(perm_request(options=only_always)) == "allow_always"
    assert queue.list_pending() == []
    queue.close()


async def test_deny_all_rejects_without_queueing(tmp_path) -> None:
    bridge, queue = _bridge(tmp_path, permission_mode="deny-all")
    assert await bridge.decide(perm_request(tool_kind="read")) == "reject_once"
    only_reject_always = [PermissionOptionInfo("r", "Never", "reject_always")]
    assert await bridge.decide(perm_request(options=only_reject_always)) == "reject_always"
    assert await bridge.decide(perm_request(options=[PermissionOptionInfo("x", "?", "x")])) == (
        "cancel"
    )
    assert queue.list_pending() == []
    queue.close()


@pytest.mark.parametrize("kind", ["read", "search", "fetch", "think"])
async def test_approve_reads_allows_read_kinds(tmp_path, kind) -> None:
    bridge, queue = _bridge(tmp_path, permission_mode="approve-reads")
    assert await bridge.decide(perm_request(tool_kind=kind)) == "allow_once"
    assert queue.list_pending() == []
    queue.close()


async def test_approve_reads_asks_for_edits(tmp_path) -> None:
    bridge, queue = _bridge(tmp_path, permission_mode="approve-reads")
    task = asyncio.create_task(bridge.decide(perm_request(tool_kind="edit", title="Edit a.py")))
    row = await next_pending(queue, ACP_APPROVAL_NAMESPACE)
    assert row["params"]["tool_kind"] == "edit"
    queue.resolve(row["id"], True)
    assert await task == "allow_once"
    queue.close()


async def test_allow_never_falls_back_to_always_outside_approve_all(tmp_path) -> None:
    bridge, queue = _bridge(tmp_path, permission_mode="approve-reads")
    only_always = [
        PermissionOptionInfo("a", "Always", "allow_always"),
        PermissionOptionInfo("r", "Reject", "reject_once"),
    ]
    assert await bridge.decide(perm_request(tool_kind="read", options=only_always)) == (
        "reject_once"
    )
    queue.close()


# -- ask --------------------------------------------------------------------


async def test_ask_queues_contract_params_and_approves(tmp_path) -> None:
    bridge, queue = _bridge(tmp_path)
    secret_input = {
        "command": "curl -H 'Authorization: Bearer sk-live-abcdefghijklmnopqrstuvwxyz0123' x",
        "api_key": "hunter2",
    }
    task = asyncio.create_task(
        bridge.decide(perm_request(title="curl example.com", raw_input=secret_input))
    )
    row = await next_pending(queue, ACP_APPROVAL_NAMESPACE)
    params = row["params"]
    for key in (
        "session_key",
        "agent_id",
        "acp_session_id",
        "tool_call_id",
        "title",
        "tool_kind",
        "raw_input",
        "cwd",
        "options",
    ):
        assert key in params, key
    assert params["agent_id"] == "codex"
    assert params["displayTitle"] == "Codex wants to run: curl example.com"
    assert params["toolName"] == params["displayTitle"]
    assert params["sessionKey"] == "agent:main:acp:0123abcd"
    assert params["raw_input"]["api_key"] == "«redacted»"
    assert "sk-live-abcdefghijklmnopqrstuvwxyz0123" not in str(params)
    assert [o["kind"] for o in params["options"]] == ["allow_once", "allow_always", "reject_once"]
    assert row["expires_at"] is not None
    queue.resolve(row["id"], True)
    assert await task == "allow_once"
    queue.close()


async def test_ask_denied_rejects(tmp_path) -> None:
    bridge, queue = _bridge(tmp_path)
    task = asyncio.create_task(bridge.decide(perm_request()))
    row = await next_pending(queue, ACP_APPROVAL_NAMESPACE)
    queue.resolve(row["id"], False)
    assert await task == "reject_once"
    queue.close()


async def test_ask_timeout_rejects_and_clears_row(tmp_path) -> None:
    queue = make_queue(tmp_path)
    config = AcpConfig(approval_timeout_s=1)
    bridge = ApprovalQueuePermissionBridge(config, queue=queue)
    # Shrink the wait without touching the config's ge=1 bound.
    original_wait = queue.wait

    async def short_wait(approval_id, timeout=None):
        return await original_wait(approval_id, timeout=0.03)

    queue.wait = short_wait  # type: ignore[method-assign]
    assert await bridge.decide(perm_request()) == "reject_once"
    assert queue.list_pending(ACP_APPROVAL_NAMESPACE) == []
    queue.close()


async def test_ask_cancelled_turn_denies_pending_row(tmp_path) -> None:
    bridge, queue = _bridge(tmp_path)
    task = asyncio.create_task(bridge.decide(perm_request()))
    row = await next_pending(queue, ACP_APPROVAL_NAMESPACE)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    entry = queue.get(row["id"])
    assert entry.resolved and not entry.approved
    queue.close()


# -- no surface -------------------------------------------------------------


async def test_no_surface_fail_raises(tmp_path) -> None:
    bridge, queue = _bridge(tmp_path, non_interactive_permissions="fail")
    bridge.bind_session("agent:main:acp:0123abcd", interactive=False)
    with pytest.raises(PermissionPromptUnavailableError) as exc:
        await bridge.decide(perm_request())
    assert exc.value.code == "ACP_PERMISSION_PROMPT_UNAVAILABLE"
    assert queue.list_pending() == []
    queue.close()


async def test_no_surface_deny_rejects(tmp_path) -> None:
    bridge, queue = _bridge(tmp_path, non_interactive_permissions="deny")
    bridge.bind_session("agent:main:acp:0123abcd", interactive=False)
    assert await bridge.decide(perm_request()) == "reject_once"
    assert queue.list_pending() == []
    queue.close()


async def test_unattended_requester_ctx_is_detected_like_shell(tmp_path) -> None:
    bridge, queue = _bridge(tmp_path)
    bridge.bind_session(
        "agent:main:acp:0123abcd",
        requester_ctx=ToolContext(interaction_mode=InteractionMode.UNATTENDED),
    )
    assert bridge.is_unattended("agent:main:acp:0123abcd")
    with pytest.raises(PermissionPromptUnavailableError):
        await bridge.decide(perm_request())
    bridge.bind_session("agent:main:acp:other", requester_ctx=ToolContext())
    assert not bridge.is_unattended("agent:main:acp:other")
    queue.close()


async def test_injected_surface_resolver(tmp_path) -> None:
    queue = make_queue(tmp_path)
    bridge = ApprovalQueuePermissionBridge(
        AcpConfig(non_interactive_permissions="deny"),
        queue=queue,
        surface=lambda key: "unattended" if key.endswith("cron") else None,
    )
    assert await bridge.decide(perm_request(session_key="agent:main:acp:cron")) == "reject_once"
    assert bridge.is_unattended("agent:main:acp:other") is False
    queue.close()


async def test_queue_owned_by_foreign_gateway_counts_as_no_surface(tmp_path, monkeypatch) -> None:
    bridge, queue = _bridge(tmp_path, non_interactive_permissions="deny")
    monkeypatch.setattr(queue, "_foreign_live_gateway", lambda: 4242)
    assert await bridge.decide(perm_request()) == "reject_once"
    bridge2 = ApprovalQueuePermissionBridge(AcpConfig(), queue=queue)
    with pytest.raises(PermissionPromptUnavailableError):
        await bridge2.decide(perm_request())
    queue.close()


# -- remember "always" ------------------------------------------------------


async def test_always_is_remembered_per_session_kind_and_prefix(tmp_path) -> None:
    bridge, queue = _bridge(tmp_path)
    task = asyncio.create_task(bridge.decide(perm_request(title="git push origin main")))
    row = await next_pending(queue, ACP_APPROVAL_NAMESPACE)
    queue.resolve(row["id"], True, allow_always=True)
    assert await task == "allow_once"

    # Same prefix, same session: no prompt.
    assert await bridge.decide(perm_request(title="git push --force")) == "allow_once"
    assert queue.list_pending(ACP_APPROVAL_NAMESPACE) == []

    # Different prefix prompts again.
    seen = {row["id"]}
    task = asyncio.create_task(bridge.decide(perm_request(title="git reset --hard")))
    row2 = await next_pending(queue, ACP_APPROVAL_NAMESPACE, seen=seen)
    queue.resolve(row2["id"], False)
    assert await task == "reject_once"

    # Other session prompts again.
    task = asyncio.create_task(
        bridge.decide(perm_request(session_key="agent:main:acp:ffff0000", title="git push"))
    )
    row3 = await next_pending(queue, ACP_APPROVAL_NAMESPACE, seen=seen)
    queue.resolve(row3["id"], True)
    assert await task == "allow_once"

    bridge.forget_session("agent:main:acp:0123abcd")
    assert bridge.remembered("agent:main:acp:0123abcd") == set()
    queue.close()


async def test_bypass_in_web_ui_remembers_without_elevating(tmp_path) -> None:
    bridge, queue = _bridge(tmp_path)
    task = asyncio.create_task(bridge.decide(perm_request(tool_kind="edit", title="Edit a.py")))
    row = await next_pending(queue, ACP_APPROVAL_NAMESPACE)
    queue.resolve(row["id"], True, elevated_mode="bypass")
    assert await task == "allow_once"
    assert queue.get_elevated_mode("agent:main:acp:0123abcd") is None
    assert ("edit", "") in bridge.remembered("agent:main:acp:0123abcd")
    assert await bridge.decide(perm_request(tool_kind="edit", title="Edit b.py")) == "allow_once"
    queue.close()


async def test_plain_approve_is_not_remembered(tmp_path) -> None:
    bridge, queue = _bridge(tmp_path)
    task = asyncio.create_task(bridge.decide(perm_request()))
    row = await next_pending(queue, ACP_APPROVAL_NAMESPACE)
    queue.resolve(row["id"], True)
    assert await task == "allow_once"
    assert bridge.remembered("agent:main:acp:0123abcd") == set()
    queue.close()


async def test_bound_session_mode_overrides_config(tmp_path) -> None:
    bridge, queue = _bridge(tmp_path, permission_mode="ask")
    bridge.bind_session("agent:main:acp:0123abcd", permission_mode="deny-all")
    assert await bridge.decide(perm_request()) == "reject_once"
    queue.close()


async def test_default_queue_is_the_process_queue(tmp_path, monkeypatch) -> None:
    queue = make_queue(tmp_path)
    monkeypatch.setattr(approval_queue_mod, "_queue", queue)
    bridge = ApprovalQueuePermissionBridge(AcpConfig())
    task = asyncio.create_task(bridge.decide(perm_request()))
    row = await next_pending(queue, ACP_APPROVAL_NAMESPACE)
    queue.resolve(row["id"], True)
    assert await task == "allow_once"
    queue.close()


# -- helpers ------------------------------------------------------------------


# command_prefix / remember_key: see test_permissions_always_allow.py (AOS-12).


def test_raw_input_is_truncated_to_4k() -> None:
    big = {"content": "x" * 10_000}
    out = redact_raw_input(big)
    assert out["_truncated"] is True
    assert len(out["preview"].encode()) <= 4096
    assert redact_raw_input(None) is None


def test_title_shape() -> None:
    assert acp_approval_title("claude", "edit", "Edit src/a.py") == (
        "Claude Code wants to edit: Edit src/a.py"
    )
    assert acp_approval_title("mystery", None, "") == "mystery wants to use a tool"
