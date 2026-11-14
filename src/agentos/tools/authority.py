"""Caller authority: derived work never exceeds the context that created it.

A turn can start work that runs somewhere else: ``sessions_spawn`` queues a
child on its own session, ``sessions_send`` queues a turn on another session,
and the ``cron`` tool schedules an unattended job. That work is resolved
later, for its own agent and session, so without a ceiling it would get the
*target's* tool surface and elevation -- a tool-restricted channel turn could
reach a shell by asking another session to run it.

The creator's effective authority is captured as a :class:`ToolAuthority`,
carried in the derived turn's route metadata under
:data:`AUTHORITY_METADATA_KEY`, and re-applied after every policy layer
(``resolve_runtime_tool_surface``), so config policy for the derived turn can
only narrow it further. Elevation is capped the same way, including the
per-session ``/elevated`` mode the shell tool falls back to.

The ceiling is monotone: it can remove tools and lower elevation, never add
either. That makes it safe to read from route metadata even where that
metadata is partly caller-supplied, and a malformed entry fails closed to an
empty surface.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import structlog

from agentos.tools.types import CallerKind, ToolAuthority, ToolContext

log = structlog.get_logger(__name__)

AUTHORITY_METADATA_KEY = "caller_authority"

_ELEVATION_RANK: dict[str, int] = {"on": 1, "bypass": 2, "full": 3}
_OPERATOR_CALLERS: frozenset[CallerKind] = frozenset({CallerKind.CLI, CallerKind.WEB})
_DENY_ALL = ToolAuthority(allowed_tools=frozenset(), restricted=True)


def _rank(mode: str | None) -> int:
    return _ELEVATION_RANK.get(mode or "", 0)


def cap_elevated(mode: str | None, ceiling: ToolAuthority | None) -> str | None:
    """Return *mode* lowered to the ceiling's elevation, if it is above it."""
    if ceiling is None:
        return mode
    if _rank(mode) <= _rank(ceiling.elevated):
        return mode
    return ceiling.elevated if _rank(ceiling.elevated) > 0 else None


def context_elevated(ctx: ToolContext) -> str | None:
    """The elevation exec calls in *ctx* run with, after the ceiling.

    Mirrors the shell tool: ``ctx.elevated`` first, then the session's
    ``/elevated`` mode held by the approval queue.
    """
    mode = ctx.elevated if ctx.elevated in _ELEVATION_RANK else None
    if mode is None and ctx.session_key:
        with contextlib.suppress(Exception):
            from agentos.gateway.approval_queue import get_approval_queue

            candidate = get_approval_queue().get_elevated_mode(ctx.session_key)
            if candidate in _ELEVATION_RANK:
                mode = candidate
    return cap_elevated(mode, ctx.authority_ceiling)


def authority_restricted(ctx: ToolContext | None) -> bool:
    """Whether *ctx*'s tool surface was narrowed below the operator's.

    The operator's own interactive CLI/Web session is never restricted. Any
    other caller is when policy narrowed it (an agent/channel/sender tool
    policy, a cron allowlist, plan mode) or when it inherited a restricted
    ceiling. A restricted caller may not delegate to another agent unless that
    agent's ``subagents.allow_agents`` names it.
    """
    if ctx is None:
        return False
    ceiling = ctx.authority_ceiling
    if ceiling is not None and ceiling.restricted:
        return True
    if ctx.caller_kind in _OPERATOR_CALLERS:
        return False
    return ctx.tool_policy_restricted or ctx.allowed_tools is not None


def narrow_authority(first: ToolAuthority, second: ToolAuthority | None) -> ToolAuthority:
    """The intersection of two ceilings (provenance kept from *first*)."""
    if second is None:
        return first
    if first.allowed_tools is None:
        allowed = second.allowed_tools
    elif second.allowed_tools is None:
        allowed = first.allowed_tools
    else:
        allowed = first.allowed_tools & second.allowed_tools
    return replace(
        first,
        allowed_tools=allowed,
        denied_tools=first.denied_tools | second.denied_tools,
        elevated=cap_elevated(first.elevated, second),
        restricted=first.restricted or second.restricted,
    )


def authority_from_context(ctx: ToolContext | None) -> ToolAuthority | None:
    """Capture *ctx*'s effective authority as a ceiling for work it starts."""
    if ctx is None:
        return None
    captured = ToolAuthority(
        allowed_tools=frozenset(ctx.allowed_tools) if ctx.allowed_tools is not None else None,
        denied_tools=frozenset(ctx.denied_tools),
        elevated=context_elevated(ctx),
        restricted=authority_restricted(ctx),
        caller_kind=str(ctx.caller_kind.value),
        agent_id=ctx.agent_id,
        session_key=ctx.session_key,
    )
    # A context that skipped surface resolution has not applied its own
    # inherited ceiling yet; carry it forward regardless.
    return narrow_authority(captured, ctx.authority_ceiling)


def authority_to_metadata(authority: ToolAuthority) -> dict[str, Any]:
    """JSON form stored in route metadata (survives the task ledger)."""
    return {
        "allowed_tools": (
            sorted(authority.allowed_tools) if authority.allowed_tools is not None else None
        ),
        "denied_tools": sorted(authority.denied_tools),
        "elevated": authority.elevated,
        "restricted": authority.restricted,
        "caller_kind": authority.caller_kind,
        "agent_id": authority.agent_id,
        "session_key": authority.session_key,
    }


def _name_set(value: object) -> frozenset[str] | None:
    if isinstance(value, list | tuple | set | frozenset) and all(
        isinstance(item, str) for item in value
    ):
        return frozenset(value)
    return None


def authority_from_metadata(metadata: object) -> ToolAuthority | None:
    """Read the ceiling out of route metadata; malformed entries deny all."""
    if not isinstance(metadata, Mapping) or AUTHORITY_METADATA_KEY not in metadata:
        return None
    raw = metadata[AUTHORITY_METADATA_KEY]
    if not isinstance(raw, Mapping):
        log.warning("caller_authority.malformed", reason="not_a_mapping")
        return _DENY_ALL
    allowed_raw = raw.get("allowed_tools")
    allowed = None if allowed_raw is None else _name_set(allowed_raw)
    denied = _name_set(raw.get("denied_tools") or [])
    if (allowed_raw is not None and allowed is None) or denied is None:
        log.warning("caller_authority.malformed", reason="tool_sets")
        return _DENY_ALL
    elevated = raw.get("elevated")
    restricted = raw.get("restricted", True)

    def _opt_str(name: str) -> str | None:
        value = raw.get(name)
        return value if isinstance(value, str) and value else None

    return ToolAuthority(
        allowed_tools=allowed,
        denied_tools=denied,
        elevated=elevated if isinstance(elevated, str) and elevated in _ELEVATION_RANK else None,
        restricted=restricted if isinstance(restricted, bool) else True,
        caller_kind=_opt_str("caller_kind"),
        agent_id=_opt_str("agent_id"),
        session_key=_opt_str("session_key"),
    )


def apply_authority_ceiling(ctx: ToolContext) -> ToolContext:
    """Clip *ctx*'s tool surface and elevation to its inherited ceiling."""
    ceiling = ctx.authority_ceiling
    if ceiling is None:
        return ctx
    denied = set(ctx.denied_tools) | ceiling.denied_tools
    allowed = set(ctx.allowed_tools) if ctx.allowed_tools is not None else None
    if ceiling.allowed_tools is not None:
        allowed = set(ceiling.allowed_tools) if allowed is None else allowed & ceiling.allowed_tools
    if allowed is not None:
        allowed -= denied
    return replace(
        ctx,
        allowed_tools=allowed,
        denied_tools=denied,
        elevated=cap_elevated(ctx.elevated, ceiling),
    )


__all__ = [
    "AUTHORITY_METADATA_KEY",
    "apply_authority_ceiling",
    "authority_from_context",
    "authority_from_metadata",
    "authority_restricted",
    "authority_to_metadata",
    "cap_elevated",
    "context_elevated",
    "narrow_authority",
]
