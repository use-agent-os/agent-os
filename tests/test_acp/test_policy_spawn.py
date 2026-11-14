"""ACP spawn policy: allow-list, sandboxed/unattended requesters, modes, admission."""

from __future__ import annotations

import pytest

from agentos.acp.policy import (
    AcpAdmission,
    AcpConcurrencyLimitError,
    AcpPermissionModeNotAllowedError,
    agent_allowed,
    check_spawn_allowed,
    effective_permission_mode,
)
from agentos.acp.types import AcpAgentNotAllowedError
from agentos.gateway.config import AcpAgentOverride, AcpConfig
from agentos.tools.types import InteractionMode, ToolContext


def test_default_config_allows_builtin_and_normalizes_aliases() -> None:
    config = AcpConfig()
    assert check_spawn_allowed(config, "claude") == "claude"
    assert check_spawn_allowed(config, " Codex ") == "codex"


def test_disabled_refuses() -> None:
    with pytest.raises(AcpAgentNotAllowedError) as exc:
        check_spawn_allowed(AcpConfig(enabled=False), "claude")
    assert exc.value.details["reason"] == "disabled"
    assert exc.value.code == "ACP_AGENT_NOT_ALLOWED"


def test_allowed_agents_list() -> None:
    config = AcpConfig(allowed_agents=["codex"])
    assert agent_allowed(config, "codex")
    assert not agent_allowed(config, "claude")
    with pytest.raises(AcpAgentNotAllowedError) as exc:
        check_spawn_allowed(config, "claude")
    assert exc.value.details["reason"] == "not_allowed"


def test_unknown_agent_needs_an_override_entry() -> None:
    with pytest.raises(AcpAgentNotAllowedError) as exc:
        check_spawn_allowed(AcpConfig(), "no-such-harness")
    assert exc.value.details["reason"] == "unknown"
    config = AcpConfig(agents={"mine": AcpAgentOverride(command="my-acp")})
    assert check_spawn_allowed(config, "mine") == "mine"


def test_empty_agent_id_refused() -> None:
    with pytest.raises(AcpAgentNotAllowedError):
        check_spawn_allowed(AcpConfig(), "  ")


def test_workspace_lockdown_requester_refused() -> None:
    ctx = ToolContext(workspace_lockdown=True)
    with pytest.raises(AcpAgentNotAllowedError) as exc:
        check_spawn_allowed(AcpConfig(), "claude", requester_ctx=ctx)
    assert exc.value.details["reason"] == "sandboxed_requester"
    # dict-shaped contexts work too
    with pytest.raises(AcpAgentNotAllowedError):
        check_spawn_allowed(AcpConfig(), "claude", requester_ctx={"workspace_lockdown": True})


def test_requester_without_shell_or_writes_cannot_start_a_harness() -> None:
    """A harness has its own shell and file tools: no escape via delegation."""
    for ctx in (
        ToolContext(denied_tools={"exec_command"}),
        ToolContext(allowed_tools={"read_file", "sessions_spawn"}),
        {"denied_tools": ["write_file"]},
    ):
        with pytest.raises(AcpAgentNotAllowedError) as exc:
            check_spawn_allowed(AcpConfig(), "claude", requester_ctx=ctx)
        assert exc.value.details["reason"] == "restricted_requester"
    # The operator's unrestricted session still starts one.
    assert check_spawn_allowed(AcpConfig(), "claude", requester_ctx=ToolContext()) == "claude"


def test_unattended_requester_only_refused_when_every_prompt_would_fail() -> None:
    ctx = ToolContext(interaction_mode=InteractionMode.UNATTENDED)
    with pytest.raises(AcpAgentNotAllowedError) as exc:
        check_spawn_allowed(AcpConfig(), "claude", requester_ctx=ctx)
    assert exc.value.details["reason"] == "unattended_requester"
    # deny fallback, or a non-ask mode, is fine unattended
    check_spawn_allowed(AcpConfig(non_interactive_permissions="deny"), "claude", requester_ctx=ctx)
    check_spawn_allowed(AcpConfig(permission_mode="approve-reads"), "claude", requester_ctx=ctx)
    check_spawn_allowed(AcpConfig(), "claude", requester_ctx=ctx, permission_mode="deny-all")
    # interactive requester always passes
    check_spawn_allowed(AcpConfig(), "claude", requester_ctx=ToolContext())


def test_effective_permission_mode_only_tightens() -> None:
    config = AcpConfig(permission_mode="approve-reads")
    assert effective_permission_mode(config) == "approve-reads"
    assert effective_permission_mode(config, "") == "approve-reads"
    assert effective_permission_mode(config, "ask") == "ask"
    assert effective_permission_mode(config, "DENY-ALL") == "deny-all"
    with pytest.raises(AcpPermissionModeNotAllowedError) as exc:
        effective_permission_mode(config, "approve-all")
    assert isinstance(exc.value, AcpAgentNotAllowedError)
    assert effective_permission_mode(config, "approve-all", allow_loosen=True) == "approve-all"
    with pytest.raises(ValueError):
        effective_permission_mode(config, "yolo")


def test_admission_counts_against_max_concurrent() -> None:
    admission = AcpAdmission(AcpConfig(max_concurrent=2))
    admission.acquire("a")
    admission.acquire("a")  # idempotent
    admission.acquire("b")
    assert admission.active() == 2
    assert not admission.has_capacity()
    with pytest.raises(AcpConcurrencyLimitError) as exc:
        admission.acquire("c")
    assert exc.value.code == "ACP_MAX_CONCURRENT"
    admission.release("a")
    admission.release("a")  # idempotent
    admission.acquire("c")
    assert admission.active() == 2


def test_admission_reads_limit_live_from_config() -> None:
    config = AcpConfig(max_concurrent=1)
    admission = AcpAdmission(config)
    admission.acquire("a")
    config.max_concurrent = 2
    admission.acquire("b")
    assert AcpAdmission(limit=3).limit == 3
