"""Concrete :class:`PolicyCheck` implementations.

Each check encapsulates a single authorization/transform branch of the
dispatch pipeline.

Behaviour notes shared across the checks:

* ``ctx is None`` always allows — every policy guards on
  ``if effective_ctx and ...``.
* Denial envelopes are constructed via :func:`_denial_envelope`, which
  centralises the ``ToolResult`` shape used for ``policy_denial=True``.
* The structured log event returned alongside an envelope is emitted by
  the orchestrator at WARN level — keeping I/O out of the checks
  themselves.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from agentos.execution_status import normalize_execution_status
from agentos.tool_boundary import ToolCall, ToolResult
from agentos.tools.envelope import build_tool_failure_envelope
from agentos.tools.policy.types import DispatchInput, PolicyDecision
from agentos.tools.policy_helpers import private_memory_read_tool_denied
from agentos.tools.types import ToolContext


def _denial_envelope(
    tool_call: ToolCall,
    *,
    exc: Exception,
    error_class_override: str,
    user_message_override: str,
) -> ToolResult:
    """Build the ``ToolResult`` returned for a policy-denial outcome.

    Produces the canonical ``policy_denial=True`` envelope shape.
    """
    status = {
        "version": 1,
        "status": "error",
        "exit_code": None,
        "timed_out": False,
        "truncated": False,
        "reason": "denied",
        "source": "tool_runtime",
        "preservation_class": "diagnostic",
    }
    return ToolResult(
        tool_use_id=tool_call.tool_use_id,
        tool_name=tool_call.tool_name,
        content=json.dumps(
            build_tool_failure_envelope(
                exc,
                tool_call.tool_name,
                policy_denial=True,
                error_class_override=error_class_override,
                user_message_override=user_message_override,
            )
        ),
        is_error=True,
        execution_status=normalize_execution_status(status),
    )

def _block_log_event(
    tool_call: ToolCall,
    ctx: ToolContext | None,
    *,
    event: str,
    reason: str,
) -> dict[str, Any]:
    """Construct the ``dispatch.defense_in_depth_block`` log payload.

    Field order and key names are the canonical structured-log shape
    expected by downstream consumers of ``dispatch.defense_in_depth_block``.
    """
    return {
        "event": event,
        "tool": tool_call.tool_name,
        "reason": reason,
        "tool_use_id": tool_call.tool_use_id,
        "agent_id": ctx.agent_id if ctx else None,
        "session_key": ctx.session_key if ctx else None,
    }

# ---------------------------------------------------------------------------
# Channel admission
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ChannelAdmissionPolicy:
    """Reject tool calls whose channel pairing grant has been revoked."""

    name: str = "channel_admission"

    def evaluate(self, d: DispatchInput) -> PolicyDecision:
        ctx = d.ctx
        if (
            ctx
            and ctx.channel_admission is not None
            and ctx.channel_admission_validator is not None
            and not ctx.channel_admission_validator(ctx.channel_admission)
        ):
            envelope = _denial_envelope(
                d.tool_call,
                exc=PermissionError("channel pairing revoked"),
                error_class_override="PolicyDenied",
                user_message_override="This channel connection is no longer paired.",
            )
            log_event = _block_log_event(
                d.tool_call,
                ctx,
                event="dispatch.defense_in_depth_block",
                reason="channel_pairing_revoked",
            )
            return PolicyDecision(
                allowed=False,
                envelope=envelope,
                log_event=log_event,
            )
        return PolicyDecision(allowed=True)


# ---------------------------------------------------------------------------
# Deny list
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DenyListPolicy:
    """Reject tools that appear in ``ctx.denied_tools``."""

    name: str = "denied"

    def evaluate(self, d: DispatchInput) -> PolicyDecision:
        ctx = d.ctx
        if ctx and d.tool_call.tool_name in ctx.denied_tools:
            envelope = _denial_envelope(
                d.tool_call,
                exc=PermissionError("tool blocked"),
                error_class_override="PolicyDenied",
                user_message_override=(
                    f"Tool '{d.tool_call.tool_name}' not available in this context."
                ),
            )
            log_event = _block_log_event(
                d.tool_call,
                ctx,
                event="dispatch.defense_in_depth_block",
                reason="denied",
            )
            return PolicyDecision(allowed=False, envelope=envelope, log_event=log_event)
        return PolicyDecision(allowed=True)

# ---------------------------------------------------------------------------
# Private memory scope
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PrivateMemoryScopePolicy:
    """Reject private-memory reads from contexts that may not access them."""

    name: str = "private_memory_scope"

    def evaluate(self, d: DispatchInput) -> PolicyDecision:
        ctx = d.ctx
        if private_memory_read_tool_denied(ctx, d.tool_call.tool_name):
            envelope = _denial_envelope(
                d.tool_call,
                exc=PermissionError("private memory blocked"),
                error_class_override="PolicyDenied",
                user_message_override=(
                    f"Tool '{d.tool_call.tool_name}' not available in this context."
                ),
            )
            log_event = _block_log_event(
                d.tool_call,
                ctx,
                event="dispatch.defense_in_depth_block",
                reason="private_memory_scope",
            )
            return PolicyDecision(allowed=False, envelope=envelope, log_event=log_event)
        return PolicyDecision(allowed=True)

# ---------------------------------------------------------------------------
# Allow list
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AllowListPolicy:
    """Reject tools absent from a non-None ``ctx.allowed_tools``."""

    name: str = "allowlist"

    def evaluate(self, d: DispatchInput) -> PolicyDecision:
        ctx = d.ctx
        if (
            ctx
            and ctx.allowed_tools is not None
            and d.tool_call.tool_name not in ctx.allowed_tools
        ):
            envelope = _denial_envelope(
                d.tool_call,
                exc=PermissionError("tool blocked"),
                error_class_override="PolicyDenied",
                user_message_override=(
                    f"Tool '{d.tool_call.tool_name}' not available in this context."
                ),
            )
            log_event = _block_log_event(
                d.tool_call,
                ctx,
                event="dispatch.defense_in_depth_block",
                reason="not_allowed",
            )
            return PolicyDecision(allowed=False, envelope=envelope, log_event=log_event)
        return PolicyDecision(allowed=True)


# ---------------------------------------------------------------------------
# Visibility
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VisibilityPolicy:
    """Reject a hidden (``exposed_by_default=False``) tool the context never offered.

    Visibility used to shape only what the model was *shown*: dispatch looked
    the tool up by name, and with ``allowed_tools`` unset nothing else stood in
    the way, so a model that named ``install_skill_deps`` ran it. Admission now
    matches :func:`agentos.tools.visibility.is_tool_visible`: a hidden tool runs
    when the context allows or surfaces it by name, and ``tool_search`` runs
    while the session has a deferred-tool surface (that is when the turn
    offers it).
    """

    name: str = "visibility"

    def evaluate(self, d: DispatchInput) -> PolicyDecision:
        ctx = d.ctx
        registered = d.registered
        if ctx is None or registered is None or registered.spec.exposed_by_default:
            return PolicyDecision(allowed=True)
        name = d.tool_call.tool_name
        if ctx.allowed_tools is not None and name in ctx.allowed_tools:
            return PolicyDecision(allowed=True)
        if ctx.surfaced_tools is not None and name in ctx.surfaced_tools:
            return PolicyDecision(allowed=True)
        from agentos.tools.deferred import TOOL_SEARCH_NAME, surface_for

        if name == TOOL_SEARCH_NAME and surface_for(ctx.session_key) is not None:
            return PolicyDecision(allowed=True)
        envelope = _denial_envelope(
            d.tool_call,
            exc=PermissionError("tool not exposed"),
            error_class_override="PolicyDenied",
            user_message_override=f"Tool '{name}' not available in this context.",
        )
        log_event = _block_log_event(
            d.tool_call,
            ctx,
            event="dispatch.defense_in_depth_block",
            reason="not_exposed",
        )
        return PolicyDecision(allowed=False, envelope=envelope, log_event=log_event)
