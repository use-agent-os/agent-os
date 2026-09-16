"""Tool visibility derived from configured policy and runtime capability."""

from __future__ import annotations

import os
from collections.abc import Iterable
from enum import StrEnum

import structlog

from agentos.session.keys import is_cron_key, is_subagent_key, parse_agent_id
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

    # The key is the only thing that knows which agent this turn belongs to
    # when the caller did not say. Hardcoding "main" here sent an ``ops``
    # session to main's memory and evaluated it against main's tool policy,
    # since ``ctx.agent_id`` is what ``memory_tools`` and
    # ``agent_policy_from_config`` read. ``parse_agent_id`` already falls back
    # to "main" for a key it cannot parse, so this only ever narrows a wrong
    # answer into the right one.
    resolved_agent_id = agent_id or (parse_agent_id(session_key) if session_key else "main")

    # ``is_subagent_key`` is the shared definition, and it accepts both the
    # canonical ``agent:<id>:subagent:<run>`` form that
    # ``build_subagent_session_key`` produces and the legacy ``subagent:``
    # prefix. The local ``startswith("subagent:")`` recognised only the legacy
    # one, so every subagent turn started by ``sessions.py`` -- which builds
    # canonical keys -- was classified AGENT/INTERACTIVE and kept the tools
    # SUBAGENT_TOOL_DENY exists to take away. Two definitions of the same
    # question had drifted apart; this leaves one.
    if explicit_kind is CallerKind.SUBAGENT or (session_key and is_subagent_key(session_key)):
        ctx = ToolContext(
            caller_kind=CallerKind.SUBAGENT,
            interaction_mode=mode or InteractionMode.UNATTENDED,
            agent_id=resolved_agent_id,
            denied_tools=set(SUBAGENT_TOOL_DENY),
        )
    elif explicit_kind is CallerKind.CRON or (session_key and is_cron_key(session_key)):
        ctx = ToolContext(
            caller_kind=CallerKind.CRON,
            interaction_mode=mode or InteractionMode.UNATTENDED,
            agent_id=resolved_agent_id,
            allowed_tools=set(CRON_AGENT_ALLOW),
            denied_tools=set(CRON_AGENT_DENY),
        )
    else:
        ctx = ToolContext(
            caller_kind=explicit_kind or CallerKind.AGENT,
            interaction_mode=mode or InteractionMode.INTERACTIVE,
            agent_id=resolved_agent_id,
        )
    return resolve_runtime_tool_surface(ctx, capabilities=tool_surface_capabilities)


def is_tool_visible(rt: RegisteredTool, ctx: ToolContext | None = None) -> bool:
    explicitly_allowed = (
        ctx is not None and ctx.allowed_tools is not None and rt.spec.name in ctx.allowed_tools
    )
    surfaced = (
        ctx is not None and ctx.surfaced_tools is not None and rt.spec.name in ctx.surfaced_tools
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
