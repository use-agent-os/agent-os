"""Tool visibility derived from configured policy and runtime capability."""

from __future__ import annotations

import os
from collections.abc import Iterable
from enum import StrEnum

import structlog

from agentos.provider.types import ToolDefinition
from agentos.tools.policy_runtime import ToolSurfaceCapabilities, resolve_runtime_tool_surface
from agentos.tools.types import (
    CRON_AGENT_ALLOW,
    CRON_AGENT_DENY,
    SUBAGENT_TOOL_DENY,
    CallerKind,
    InteractionMode,
    RegisteredTool,
    ToolContext,
)

log = structlog.get_logger(__name__)


class ToolProfile(StrEnum):
    """Single role-free profile; fine-grained access lives in agent policy."""

    CONFIGURED = "configured"


def filter_by_profile(
    tools: list[ToolDefinition],
    profile: ToolProfile | str,
    ctx: ToolContext | None = None,
) -> list[ToolDefinition]:
    """Compatibility pass-through for tool definitions under a profile.

    Fine-grained tool visibility and filtering are centrally enforced by
    :func:`is_tool_visible` and :mod:`agentos.tools.policy_config`.
    """
    del ctx
    ToolProfile(profile)
    return list(tools)


def profile_allows_tool(
    tool_name: str,
    profile: ToolProfile | str,
    *,
    explicitly_allowed: set[str] | frozenset[str] | None = None,
) -> bool:
    """Compatibility check for tool allowance under a profile.

    Fine-grained tool policy evaluation is centrally handled in
    :mod:`agentos.tools.policy_config` and :mod:`agentos.tools.policy.checks`.
    """
    del tool_name, explicitly_allowed
    ToolProfile(profile)
    return True


def resolve_profile(ctx: ToolContext | None) -> ToolProfile:
    del ctx
    override = os.environ.get("AGENTOS_TOOL_PROFILE", "").strip()
    if override:
        try:
            return ToolProfile(override)
        except ValueError:
            log.warning("tool_profile.invalid_env_override", value=override)
    return ToolProfile.CONFIGURED


def default_tool_context() -> ToolContext:
    return ToolContext(caller_kind=CallerKind.AGENT)


def tool_context_for_profile(profile: str | None) -> ToolContext:
    if profile == "subagent":
        return ToolContext(
            caller_kind=CallerKind.SUBAGENT,
            interaction_mode=InteractionMode.UNATTENDED,
            denied_tools=set(SUBAGENT_TOOL_DENY),
        )
    if profile == "cron":
        return ToolContext(
            caller_kind=CallerKind.CRON,
            interaction_mode=InteractionMode.UNATTENDED,
            allowed_tools=set(CRON_AGENT_ALLOW),
            denied_tools=set(CRON_AGENT_DENY),
        )
    return default_tool_context()


def parse_interaction_mode(value: InteractionMode | str | None) -> InteractionMode | None:
    if value is None:
        return None
    try:
        return value if isinstance(value, InteractionMode) else InteractionMode(str(value))
    except ValueError:
        return None


def effective_tool_context(
    *,
    session_key: str | None = None,
    agent_id: str | None = None,
    caller_kind: CallerKind | str | None = None,
    interaction_mode: InteractionMode | str | None = None,
    tool_surface_capabilities: ToolSurfaceCapabilities | None = None,
) -> ToolContext:
    try:
        explicit_kind = CallerKind(caller_kind) if caller_kind else None
    except ValueError:
        explicit_kind = None
    mode = parse_interaction_mode(interaction_mode)

    if explicit_kind is CallerKind.SUBAGENT or (
        session_key and session_key.startswith("subagent:")
    ):
        ctx = ToolContext(
            caller_kind=CallerKind.SUBAGENT,
            interaction_mode=mode or InteractionMode.UNATTENDED,
            agent_id=agent_id or "main",
            denied_tools=set(SUBAGENT_TOOL_DENY),
        )
    elif explicit_kind is CallerKind.CRON or (session_key and session_key.startswith("cron:")):
        ctx = ToolContext(
            caller_kind=CallerKind.CRON,
            interaction_mode=mode or InteractionMode.UNATTENDED,
            agent_id=agent_id or "main",
            allowed_tools=set(CRON_AGENT_ALLOW),
            denied_tools=set(CRON_AGENT_DENY),
        )
    else:
        ctx = ToolContext(
            caller_kind=explicit_kind or CallerKind.AGENT,
            interaction_mode=mode or InteractionMode.INTERACTIVE,
            agent_id=agent_id or "main",
        )
    return resolve_runtime_tool_surface(ctx, capabilities=tool_surface_capabilities)


def is_tool_visible(rt: RegisteredTool, ctx: ToolContext | None = None) -> bool:
    explicitly_allowed = (
        ctx is not None and ctx.allowed_tools is not None and rt.spec.name in ctx.allowed_tools
    )
    surfaced = (
        ctx is not None
        and ctx.surfaced_tools is not None
        and rt.spec.name in ctx.surfaced_tools
    )
    if not rt.spec.exposed_by_default and not explicitly_allowed and not surfaced:
        return False
    if ctx is not None:
        if ctx.allowed_tools is not None and rt.spec.name not in ctx.allowed_tools:
            log.debug("tool_filtered", tool=rt.spec.name, reason="not_allowed")
            return False
        if rt.spec.name in ctx.denied_tools:
            log.debug("tool_filtered", tool=rt.spec.name, reason="denied")
            return False
    return True


def visible_registered_tools(
    tools: Iterable[RegisteredTool],
    ctx: ToolContext | None = None,
    *,
    sort: bool = False,
) -> list[RegisteredTool]:
    visible = [rt for rt in tools if is_tool_visible(rt, ctx)]
    if not sort:
        return visible
    return sorted(visible, key=lambda tool: tool.spec.name)
