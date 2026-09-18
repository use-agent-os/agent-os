"""Tool registry type definitions: ToolSpec, ToolContext, registered ToolHandler."""

from __future__ import annotations

import contextvars
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from agentos.channel_pairing import ChannelAdmission


class CallerKind(StrEnum):
    """Entry-point caller type — used in ToolContext for filtering decisions."""

    AGENT = "agent"
    SUBAGENT = "subagent"
    CRON = "cron"
    CHANNEL = "channel"
    CLI = "cli"
    WEB = "web"


class InteractionMode(StrEnum):
    """Whether the entry point has a live operator available for tool approvals."""

    INTERACTIVE = "interactive"
    UNATTENDED = "unattended"


@dataclass
class ToolContext:
    """Constructed at the entry point, flows through to tool list building.

    Every entry point (gateway, CLI, cron, channel) must explicitly construct
    a ToolContext. There is no default — omitting it is a TypeError.
    """

    caller_kind: CallerKind = CallerKind.AGENT
    interaction_mode: InteractionMode = InteractionMode.INTERACTIVE
    subagent_depth: int = 0
    agent_id: str = "main"
    workspace_dir: str | None = None
    memory_source_dir: str | None = None
    workspace_strict: bool = False
    scratch_dir: str | None = None
    workspace_lockdown: bool = False
    workspace_write_deny_globs: list[str] = field(default_factory=list)
    session_key: str | None = None
    channel_kind: str | None = None
    channel_id: str | None = None
    sender_id: str | None = None
    source_kind: str | None = None
    source_name: str | None = None
    channel_admission: ChannelAdmission | None = None
    channel_admission_validator: Callable[[ChannelAdmission], bool] | None = None
    task_id: str | None = None
    artifact_media_root: str | None = None
    artifact_session_id: str | None = None
    artifact_max_bytes: int | None = None
    artifact_disk_budget_bytes: int | None = None
    published_artifacts: list[dict[str, Any]] = field(default_factory=list)
    workspace_file_writes: list[dict[str, Any]] = field(default_factory=list)
    allowed_tools: set[str] | None = None
    denied_tools: set[str] = field(default_factory=set)
    on_memory_source_write: Callable[[str, str], None] | None = None
    on_bootstrap_source_write: Callable[[str, str], None] | None = None
    # Elevated mode: None/"off" = sandboxed, "on" = host exec with approval,
    # "bypass" = skip approval prompts but keep sensitive-path checks,
    # "full" = bypass approval and sensitive-path checks (trusted operators only).
    elevated: str | None = None
    # Additive per-call tool surface overrides (surfaced tools are made visible even
    # when exposed_by_default=False). Does NOT relax allowed_tools strict denylist.
    surfaced_tools: set[str] | None = None
    tool_policy: dict[str, Any] | None = None
    tool_result_budget_policy: Any | None = None
    tool_result_budget_tracker_factory: Callable[[], Any] | None = None
    tool_run_budget_policy: Any | None = None
    tool_run_budget_tracker_factory: Callable[[], Any] | None = None
    tool_run_budget_key: str | None = None
    router_control_config: Any | None = None
    router_control_hold_store: Any | None = None
    router_control_replay_depth: int = 0
    router_control_turn_hold_applied: bool = False


# Request-scoped context — set by build_tool_handler before each dispatch.
current_tool_context: contextvars.ContextVar[ToolContext | None] = contextvars.ContextVar(
    "current_tool_context", default=None
)


# Tool deny-list constants — exact registered tool names

SUBAGENT_TOOL_DENY: frozenset[str] = frozenset(
    {
        "ask_user",
        "cron",
        "gateway",
        "agents_list",
        "subagents",
        "memory_get",
        "memory_search",
        "session_search",
        "message",
        "publish_artifact",
        "projects_create",
        "projects_list",
        "projects_update",
        "projects_move_session",
    }
)

CRON_AGENT_ALLOW: frozenset[str] = frozenset(
    {
        "git_diff",
        "git_log",
        "git_status",
        "glob_search",
        "grep_search",
        "list_dir",
        "pdf",
        "read_file",
        "session_status",
        "sessions_history",
        "sessions_list",
        # A cron turn gets the same skills block as any other, telling it to call
        # skill_view. Leaving both tools off this allowlist made that block an
        # instruction the agent could not carry out: it could see the names and
        # never open one. Both are read-only.
        "skill_list",
        "skill_view",
        "web_fetch",
        "web_search",
        # Read-only external discovery, same shape as the two above. A cron job
        # watching X is the obvious use for it, and it can only read.
        "x_search",
    }
)

CRON_AGENT_DENY: frozenset[str] = frozenset(
    {
        "cron",
        "agents_list",
        "subagents",
        "message",
        "exec_command",
        "background_process",
        "write_file",
        "edit_file",
        "apply_patch",
        "execute_code",
        "git_commit",
    }
)

# What a cron job gains when it opts in to elevation. Almost every skill body is
# a block of shell, so a cron turn that is shown a skill and denied exec_command
# can read the instructions and never carry them out. These three are what the
# script skills need: run the script, and stage its inputs/outputs.
CRON_ELEVATED_EXTRA_ALLOW: frozenset[str] = frozenset(
    {"exec_command", "write_file", "edit_file"}
)

CRON_ELEVATED_ALLOW: frozenset[str] = CRON_AGENT_ALLOW | CRON_ELEVATED_EXTRA_ALLOW

# Read this as a *default surface*, not as containment. Once exec_command is on,
# git_commit / apply_patch / execute_code are all a shell command away, so
# keeping them off the list only decides what the agent is offered — the real
# boundaries are shell_policy.check_safe_bin, the sensitive-path block, and
# workspace lockdown, none of which "bypass" elevation turns off.
#
# The pair stays derived from CRON_AGENT_DENY so the two surfaces cannot drift,
# and CRON_ELEVATED_ALLOW stays an explicit allowlist so that `gateway`,
# `sessions_spawn`, `publish_artifact`, `memory_*` — and any tool added later —
# are never granted to an elevated cron turn by omission.
CRON_ELEVATED_DENY: frozenset[str] = CRON_AGENT_DENY - CRON_ELEVATED_EXTRA_ALLOW


# Internal tool spec
@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema properties dict
    required: list[str] = field(default_factory=list)
    exposed_by_default: bool = True
    execution_timeout_seconds: float | None = None
    execution_timeout_argument: str | None = None
    execution_timeout_padding: float = 0.0
    result_budget_class: str | None = None


# Registered tool implementation: async fn that accepts keyword args and returns str.
# Agent-level tool-call handlers live in agentos.tool_boundary.
ToolHandler = Callable[..., Awaitable[str]]


@dataclass
class RegisteredTool:
    spec: ToolSpec
    handler: ToolHandler


class ToolError(Exception):
    """Raised for invalid tool inputs."""


class SafeToolUserMessage:
    """Marker for exceptions with a sanitized, user-actionable message.

    Subclasses may carry raw details in ``args`` for tests or logs, but only
    ``user_message`` is safe to expose to the model/user.
    """

    user_message = "The tool could not complete this action."


class SafeToolError(SafeToolUserMessage, ToolError):
    """ToolError variant that may expose a sanitized user-actionable message."""

    def __init__(self, user_message: str | None = None, *raw_details: object) -> None:
        super().__init__(*(raw_details or (user_message or self.user_message,)))
        if user_message is not None and user_message.strip():
            self.user_message = user_message


class InvalidToolArgumentsError(SafeToolUserMessage, ValueError):
    """Raised when provider output did not produce executable tool arguments."""

    user_message = (
        "The tool call arguments were not valid JSON. Reissue the tool call with "
        "valid JSON that matches the tool schema."
    )


class ProjectedToolArgumentsError(SafeToolUserMessage, ValueError):
    """Raised when provider-context argument projections reach dispatch."""

    user_message = (
        "The tool call arguments were compacted for model context and cannot be "
        "executed. Reissue the tool call with the real schema fields."
    )


class UnsupportedSurfaceError(SafeToolError):
    """Raised when a tool needs an interactive surface that is unavailable."""

    user_message = (
        "This tool requires a live approval surface, but the current run is unattended."
    )


class EditMatchError(SafeToolError, ValueError):
    """Raised when edit_file cannot locate unique text to replace."""


class UnsupportedURLSchemeError(SafeToolUserMessage, ValueError):
    """Raised when a URL tool receives a URL without an HTTP(S) scheme."""

    user_message = "The URL must include http:// or https:// before the hostname."


class SSRFBlockedError(SafeToolUserMessage, ValueError):
    """Raised when URL safety checks block a private/internal destination."""

    user_message = (
        "The URL was blocked by the network safety policy. Use a public HTTP(S) URL "
        "from trusted search results instead."
    )


class WorkspaceAccessError(SafeToolError):
    """Raised when a filesystem operation escapes the active workspace."""

    user_message = (
        "Filesystem operations must stay inside the active workspace. Use a relative "
        "path within the workspace or choose an approved workspace file."
    )
