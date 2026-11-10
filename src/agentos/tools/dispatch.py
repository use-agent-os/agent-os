"""Tool dispatch orchestrator.

This module exposes :func:`build_tool_handler`, the single entry point used
by every caller (gateway, CLI, cron, channel adapters). The pipeline is:

1. Ingress injection guard — before registry lookup.
2. Registry lookup — before any policy check. A near-miss name resolves to
   the one tool it unambiguously means
   (:mod:`agentos.tools.tool_name_resolution`).
2b. Argument admission — schema validation with lossless coercions
   (:mod:`agentos.tools.argument_admission`); a mismatch is rejected here.
3. Optional ``ToolHook.before_tool`` fan-out.
4. Policy chain (:func:`agentos.tools.policy.run_chain_with_emit`) —
   first denial wins; chain log emission flows through one site.
5. Handler dispatch inside ``current_tool_context.set(effective_ctx)``.
6. Single finalisation point (:func:`agentos.tools.policy.finalize.finalize`).
7. Optional ``ToolHook.after_tool`` fan-out with the settled ``ToolResult``.
   Every hook whose ``before_tool`` ran gets ``after_tool`` exactly once,
   including when the call was blocked, refused or denied before it ran.
8. ``current_tool_context.reset(token)`` in ``finally``.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import time
import weakref
from collections.abc import Sequence
from typing import Any

import structlog

from agentos.engine.hooks import (
    Block,
    ReplaceResult,
    Rewrite,
    ToolHook,
    ToolHookCall,
    ToolHookResult,
)
from agentos.engine.transcript import (
    HISTORICAL_TOOL_ARGUMENT_PROJECTION_PREFIX,
    INVALID_PROVIDER_CONTEXT_PROJECTION_PREFIX,
    TOOL_ARGUMENT_PROJECTION_PREFIX,
    has_compacted_argument_marker,
)
from agentos.execution_status import (
    derive_is_error,
    mark_execution_status_truncated,
    normalize_execution_status,
    runtime_execution_status,
)
from agentos.result_budget import (
    DEFAULT_TOOL_RESULT_BUDGET_POLICY,
    DEFAULT_TOOL_RUN_BUDGET_POLICY,
    ToolResultBudgetClass,
    ToolResultBudgetPolicy,
    ToolResultBudgetTracker,
    ToolRunBudgetExceededError,
    ToolRunBudgetPolicy,
    ToolRunBudgetReservation,
    ToolRunBudgetTracker,
    clamp_tool_arguments,
    resolve_budget_class,
)
from agentos.safety.injection_guard import (
    REFUSAL_REASON_TOOL_CALL_IN_UNTRUSTED,
    extract_tool_call_refusal_reason,
)
from agentos.tool_boundary import AgentToolHandler, ToolCall, ToolResult
from agentos.tools.argument_admission import (
    REJECTED_ERROR_CLASS,
    REJECTED_REASON,
    ToolArgumentsRejectedError,
    admit_tool_arguments,
)
from agentos.tools.envelope import build_tool_failure_envelope
from agentos.tools.policy import DispatchInput, finalize, run_chain_with_emit
from agentos.tools.registry import ToolRegistry
from agentos.tools.tool_name_resolution import resolve_tool_name
from agentos.tools.types import (
    CallerKind,
    InvalidToolArgumentsError,
    ProjectedToolArgumentsError,
    RegisteredTool,
    SafeToolError,
    ToolContext,
    current_tool_context,
)

log = structlog.get_logger("agentos.tools.dispatch")

__all__ = ["build_tool_handler", "preflight_tool_call"]

_PROJECTION_PREFIXES = (
    TOOL_ARGUMENT_PROJECTION_PREFIX,
    HISTORICAL_TOOL_ARGUMENT_PROJECTION_PREFIX,
    INVALID_PROVIDER_CONTEXT_PROJECTION_PREFIX,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_budget_policy(ctx: ToolContext | None) -> ToolResultBudgetPolicy:
    policy = getattr(ctx, "tool_result_budget_policy", None) if ctx is not None else None
    if isinstance(policy, ToolResultBudgetPolicy):
        return policy
    return DEFAULT_TOOL_RESULT_BUDGET_POLICY


def _build_budget_tracker(ctx: ToolContext | None) -> ToolResultBudgetTracker:
    factory = getattr(ctx, "tool_result_budget_tracker_factory", None) if ctx else None
    if callable(factory):
        tracker = factory()
        if isinstance(tracker, ToolResultBudgetTracker):
            return tracker
    return ToolResultBudgetTracker(_resolve_budget_policy(ctx))


def _resolve_run_budget_policy(ctx: ToolContext | None) -> ToolRunBudgetPolicy:
    policy = getattr(ctx, "tool_run_budget_policy", None) if ctx is not None else None
    if isinstance(policy, ToolRunBudgetPolicy):
        return policy
    return DEFAULT_TOOL_RUN_BUDGET_POLICY


def _build_run_budget_tracker(ctx: ToolContext | None) -> ToolRunBudgetTracker:
    factory = getattr(ctx, "tool_run_budget_tracker_factory", None) if ctx else None
    if callable(factory):
        tracker = factory()
        if isinstance(tracker, ToolRunBudgetTracker):
            return tracker
    return ToolRunBudgetTracker(_resolve_run_budget_policy(ctx))


def _build_envelope_result(
    tool_call: ToolCall,
    *,
    exc: Exception,
    policy_denial: bool = False,
    error_class_override: str | None = None,
    user_message_override: str | None = None,
    reason_override: str | None = None,
) -> ToolResult:
    status = {
        "version": 1,
        "status": "error",
        "exit_code": None,
        "timed_out": False,
        "truncated": False,
        "reason": reason_override or ("denied" if policy_denial else "runtime_error"),
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
                policy_denial=policy_denial,
                error_class_override=error_class_override,
                user_message_override=user_message_override,
            )
        ),
        is_error=True,
        execution_status=normalize_execution_status(status),
    )


async def _emit_webresearch_tool_run_diagnostics(
    *,
    tool_call: ToolCall,
    effective_ctx: ToolContext | None,
    reservation: ToolRunBudgetReservation,
    run_budget_tracker: ToolRunBudgetTracker,
    started_at: float,
    raw_result: Any,
    exception: BaseException | None,
) -> None:
    if not reservation.counted_as_external_text:
        return
    snapshot = await run_budget_tracker.snapshot()
    if exception is None:
        status = "ok"
    elif isinstance(exception, ToolRunBudgetExceededError):
        status = "budget_exhausted"
    else:
        status = "error"
    result_chars = 0
    if raw_result is not None:
        result_chars = len(raw_result if isinstance(raw_result, str) else str(raw_result))
    log.debug(
        "dispatch.webresearch_tool_run_diagnostics",
        tool=tool_call.tool_name,
        tool_use_id=tool_call.tool_use_id,
        agent_id=effective_ctx.agent_id if effective_ctx else None,
        session_key=effective_ctx.session_key if effective_ctx else None,
        status=status,
        tool_wall_time_ms=round((time.monotonic() - started_at) * 1000, 3),
        result_chars=result_chars,
        reserved_external_text_chars=reservation.reserved_external_text_chars,
        counted_as_search=reservation.counted_as_search,
        counted_as_fetch=reservation.counted_as_fetch,
        **snapshot,
    )


def _build_run_budget_control_result(
    tool_call: ToolCall,
    exc: ToolRunBudgetExceededError,
) -> ToolResult:
    payload = {
        "status": "control",
        "tool": tool_call.tool_name,
        "reason": "tool_run_budget_exhausted",
        "user_message": (
            "The tool was skipped by a runtime resource guard. Continue with "
            "available evidence or choose a smaller request."
        ),
        "retry_allowed": False,
    }
    status = {
        "version": 1,
        "status": "unknown",
        "exit_code": None,
        "timed_out": False,
        "truncated": False,
        "reason": "tool_run_budget_exhausted",
        "source": "tool_runtime",
        "preservation_class": "ephemeral",
    }
    log.info(
        "dispatch.tool_run_budget_exhausted",
        tool=tool_call.tool_name,
        tool_use_id=tool_call.tool_use_id,
        message=str(exc),
    )
    return ToolResult(
        tool_use_id=tool_call.tool_use_id,
        tool_name=tool_call.tool_name,
        content=json.dumps(payload),
        is_error=False,
        execution_status=normalize_execution_status(status),
    )


def _check_injection_guard(
    tool_call: ToolCall, effective_ctx: ToolContext | None
) -> ToolResult | None:
    origin = tool_call.origin_trace
    if not origin:
        return None
    reason = extract_tool_call_refusal_reason(origin)
    if reason != REFUSAL_REASON_TOOL_CALL_IN_UNTRUSTED:
        return None
    log.warning(
        "dispatch.injection_refused",
        tool=tool_call.tool_name,
        reason=reason,
        tool_use_id=tool_call.tool_use_id,
        agent_id=effective_ctx.agent_id if effective_ctx else None,
        session_key=effective_ctx.session_key if effective_ctx else None,
    )
    return _build_envelope_result(
        tool_call,
        exc=ValueError("dispatch injection refused"),
        policy_denial=True,
        error_class_override="InjectionRefused",
        user_message_override=str(reason),
    )


def _check_non_executable_arguments(
    tool_call: ToolCall,
    effective_ctx: ToolContext | None,
) -> ToolResult | None:
    arguments = tool_call.arguments
    if set(arguments) == {"_raw"} and isinstance(arguments.get("_raw"), str):
        log.warning(
            "dispatch.invalid_tool_arguments",
            tool=tool_call.tool_name,
            tool_use_id=tool_call.tool_use_id,
            agent_id=effective_ctx.agent_id if effective_ctx else None,
            session_key=effective_ctx.session_key if effective_ctx else None,
            reason="unparsed_raw_arguments",
        )
        return _build_envelope_result(
            tool_call,
            exc=InvalidToolArgumentsError(),
        )

    if has_compacted_argument_marker(arguments):
        log.warning(
            "dispatch.projected_tool_arguments_refused",
            tool=tool_call.tool_name,
            tool_use_id=tool_call.tool_use_id,
            agent_id=effective_ctx.agent_id if effective_ctx else None,
            session_key=effective_ctx.session_key if effective_ctx else None,
            reason="compacted_argument_marker",
        )
        return _build_envelope_result(
            tool_call,
            exc=ProjectedToolArgumentsError(),
            reason_override="provider_context_projection_reused",
        )

    for argument_name, value in arguments.items():
        if isinstance(value, str) and value.startswith(_PROJECTION_PREFIXES):
            log.warning(
                "dispatch.projected_tool_arguments_refused",
                tool=tool_call.tool_name,
                tool_use_id=tool_call.tool_use_id,
                agent_id=effective_ctx.agent_id if effective_ctx else None,
                session_key=effective_ctx.session_key if effective_ctx else None,
                reason="projection_string",
                field=argument_name,
            )
            return _build_envelope_result(
                tool_call,
                exc=ProjectedToolArgumentsError(),
                reason_override="provider_context_projection_reused",
            )

    return None


def _lookup_registered(
    registry: ToolRegistry,
    tool_call: ToolCall,
    ctx: ToolContext | None,
) -> tuple[ToolCall, RegisteredTool | None]:
    """Find the tool a call names, resolving a near-miss name on an exact miss.

    A resolved call is returned under the registered name, so hooks, policy,
    approvals and the result all see the tool that actually runs.
    """
    registered = registry.get(tool_call.tool_name)
    if registered is not None:
        return tool_call, registered
    resolution = resolve_tool_name(tool_call.tool_name, registry, ctx)
    if resolution is None:
        return tool_call, None
    registered = registry.get(resolution.resolved)
    if registered is None:
        return tool_call, None
    log.info(
        "dispatch.tool_name_resolved",
        requested=resolution.requested,
        tool=resolution.resolved,
        rules=list(resolution.rules),
        tool_use_id=tool_call.tool_use_id,
        agent_id=ctx.agent_id if ctx else None,
        session_key=ctx.session_key if ctx else None,
    )
    return dataclasses.replace(tool_call, tool_name=resolution.resolved), registered


def _hook_name(hook: ToolHook) -> str:
    return str(getattr(hook, "name", type(hook).__name__))


def _hook_view(hook_call: ToolHookCall) -> ToolHookCall:
    """A per-hook copy of the call: editing its arguments in place changes nothing.

    A hook that wants other arguments returns :class:`Rewrite`, which is
    checked and admitted again; an in-place edit would skip both.
    """
    tool_call = hook_call.tool_call
    return ToolHookCall(
        tool_call=dataclasses.replace(tool_call, arguments=copy.deepcopy(tool_call.arguments)),
        ctx=hook_call.ctx,
    )


def _run_before_tool_hooks(
    hooks: Sequence[ToolHook], hook_call: ToolHookCall
) -> tuple[Block | Rewrite | None, ToolHookCall, int]:
    """Fan ``before_tool`` out in order; the first block wins.

    A rewrite is shown to the hooks after it. A hook that raises is logged and
    skipped, unless it is ``fail_closed``: then its failure blocks the call.
    Also returns how many hooks were called, so ``after_tool`` goes to exactly
    those.
    """

    rewrite: Rewrite | None = None
    for index, hook in enumerate(hooks):
        name = _hook_name(hook)
        try:
            decision = hook.before_tool(_hook_view(hook_call))
        except Exception as exc:  # noqa: BLE001 - hooks must not break dispatch
            log.warning(
                "dispatch.tool_hook_failed",
                hook=name,
                phase="before_tool",
                error=str(exc),
                fail_closed=bool(getattr(hook, "fail_closed", False)),
            )
            if getattr(hook, "fail_closed", False):
                return (
                    Block(
                        reason=(
                            f"The tool guard {name!r} failed ({type(exc).__name__}), "
                            "so this call was not run."
                        )
                    ),
                    hook_call,
                    index + 1,
                )
            continue
        if isinstance(decision, Block):
            log.info(
                "dispatch.tool_hook_blocked",
                hook=name,
                tool=hook_call.tool_call.tool_name,
                tool_use_id=hook_call.tool_call.tool_use_id,
            )
            return decision, hook_call, index + 1
        if isinstance(decision, Rewrite):
            rewrite = decision
            hook_call = ToolHookCall(
                tool_call=dataclasses.replace(
                    hook_call.tool_call,
                    arguments=_rewritten_arguments(hook_call.tool_call.arguments, decision),
                ),
                ctx=hook_call.ctx,
            )
    return rewrite, hook_call, len(hooks)


def _hook_blocked_result(tool_call: ToolCall, reason: str) -> ToolResult:
    return _build_envelope_result(
        tool_call,
        exc=SafeToolError(reason),
        policy_denial=True,
        error_class_override="ToolHookBlocked",
        user_message_override=reason,
        reason_override="hook_blocked",
    )


@dataclasses.dataclass(frozen=True)
class _BeforeToolOutcome:
    """Where the ``before_tool`` fan-out left a call."""

    tool_call: ToolCall
    hook_call: ToolHookCall
    # The hooks whose before_tool ran; each gets after_tool exactly once.
    called_hooks: tuple[ToolHook, ...]
    # Set when the call stops here: blocked, or a rewrite was refused.
    stopped: ToolResult | None = None
    rewritten: bool = False


def _apply_before_tool_hooks(
    hooks: tuple[ToolHook, ...],
    tool_call: ToolCall,
    registered: RegisteredTool,
    ctx: ToolContext | None,
) -> _BeforeToolOutcome:
    """Run ``before_tool`` and check a rewrite like the model's own arguments.

    A rewrite goes through every check the model's arguments did -- unparsed
    ``_raw`` and copied projection placeholders are refused, then schema
    admission -- and the caller runs the policy chain on the result: hooks
    cannot bypass any of them.
    """
    hook_decision, hook_call, called = _run_before_tool_hooks(
        hooks, ToolHookCall(tool_call=tool_call, ctx=ctx)
    )
    called_hooks = hooks[:called]
    if isinstance(hook_decision, Block):
        return _BeforeToolOutcome(
            tool_call,
            hook_call,
            called_hooks,
            stopped=_hook_blocked_result(tool_call, hook_decision.reason),
        )
    if not isinstance(hook_decision, Rewrite):
        return _BeforeToolOutcome(tool_call, hook_call, called_hooks)
    log.info(
        "dispatch.tool_hook_rewrite",
        tool=tool_call.tool_name,
        tool_use_id=tool_call.tool_use_id,
        argument_keys=sorted(hook_call.tool_call.arguments),
    )
    candidate = dataclasses.replace(
        tool_call, arguments=copy.deepcopy(hook_call.tool_call.arguments)
    )
    refused = _check_non_executable_arguments(candidate, ctx)
    if refused is not None:
        return _BeforeToolOutcome(tool_call, hook_call, called_hooks, stopped=refused)
    readmitted = _admit_arguments(candidate, registered, ctx)
    if isinstance(readmitted, ToolResult):
        return _BeforeToolOutcome(tool_call, hook_call, called_hooks, stopped=readmitted)
    return _BeforeToolOutcome(
        readmitted,
        ToolHookCall(tool_call=readmitted, ctx=ctx),
        called_hooks,
        rewritten=True,
    )


def _hook_withheld_result(result: ToolResult, hook: str, exc: Exception) -> ToolResult:
    """A fail-closed ``after_tool`` crashed: the result it guards is withheld."""
    message = (
        f"The result guard {hook!r} failed ({type(exc).__name__}), so the result "
        "of this call was withheld. The call itself may already have run."
    )
    withheld = _build_envelope_result(
        ToolCall(tool_use_id=result.tool_use_id, tool_name=result.tool_name, arguments={}),
        exc=SafeToolError(message),
        error_class_override="ToolHookFailed",
        user_message_override=message,
        reason_override="hook_failed",
    )
    # Nothing of the guarded result goes through, its artifact references
    # included.
    return dataclasses.replace(withheld, effective_arguments=result.effective_arguments)


def _status_after_replacement(result: ToolResult, is_error: bool) -> Any:
    """Keep the execution status in step with a replacement's error flag.

    The status the tool produced still describes the call when the flag does
    not change (a redaction). When a hook turns a success into an error, or
    the other way round, the old status would contradict it.
    """
    status = result.execution_status
    if is_error == (derive_is_error(status) if status is not None else result.is_error):
        return status
    return runtime_execution_status("error" if is_error else "success", reason="hook_replaced")


async def _apply_replacement(
    result: ToolResult,
    replacement: ReplaceResult,
    *,
    ctx: ToolContext | None,
    registered: RegisteredTool,
) -> ToolResult:
    """Apply a hook's :class:`ReplaceResult` under the per-result size cap."""
    is_error = result.is_error if replacement.is_error is None else bool(replacement.is_error)
    status = _status_after_replacement(result, is_error)
    # The replacement is held to the same single-result cap as a tool's own
    # output. Only the single-result limit applies: the call's original result
    # was already counted against the turn's total.
    policy = dataclasses.replace(
        _resolve_budget_policy(ctx),
        max_tool_result_chars_per_turn=None,
        max_external_tool_result_chars_per_turn=None,
    )
    budget_class = resolve_budget_class(result.tool_name, registered.spec.result_budget_class)
    if budget_class is ToolResultBudgetClass.ARTIFACT:
        budget_class = ToolResultBudgetClass.UNKNOWN
    budgeted = await ToolResultBudgetTracker(policy).normalize(
        tool_name=result.tool_name,
        content=str(replacement.content),
        budget_class=budget_class,
        is_error=is_error,
    )
    if budgeted.changed and status is not None:
        status = mark_execution_status_truncated(status)
    log.info(
        "dispatch.tool_hook_replaced_result",
        tool=result.tool_name,
        tool_use_id=result.tool_use_id,
        truncated=budgeted.changed,
    )
    return dataclasses.replace(
        result,
        content=budgeted.content,
        is_error=is_error,
        execution_status=status,
    )


async def _run_after_tool_hooks(
    hooks: Sequence[ToolHook],
    hook_call: ToolHookCall,
    result: ToolResult,
    *,
    ctx: ToolContext | None,
    registered: RegisteredTool,
    raw_result: Any = None,
    exception: BaseException | None = None,
) -> ToolResult:
    """Fan ``after_tool`` out over the settled result; return what the model sees.

    Each hook sees the result as the hooks before it left it. A
    :class:`ReplaceResult` is applied at once. A hook that raises is logged and
    skipped, unless it is ``fail_closed``: then the result is withheld and the
    model gets an error in its place.
    """
    for hook in hooks:
        name = _hook_name(hook)
        outcome = ToolHookResult(result=result, exception=exception, raw_result=raw_result)
        try:
            after = hook.after_tool(_hook_view(hook_call), outcome)
        except Exception as exc:  # noqa: BLE001 - hooks must not break dispatch
            fail_closed = bool(getattr(hook, "fail_closed", False))
            log.warning(
                "dispatch.tool_hook_failed",
                hook=name,
                phase="after_tool",
                error=str(exc),
                fail_closed=fail_closed,
            )
            if fail_closed:
                result = _hook_withheld_result(result, name, exc)
            continue
        if isinstance(after, ReplaceResult):
            result = await _apply_replacement(result, after, ctx=ctx, registered=registered)
    return result


def _notify_after_tool_hooks_without_result(
    hooks: Sequence[ToolHook], hook_call: ToolHookCall, exception: BaseException | None
) -> None:
    """Close the ``after_tool`` pairing for a call that has no result.

    The turn was cancelled while the tool ran (``exception`` is the
    ``CancelledError``), or a dry run found the call would run. Hooks hear
    about it, but there is nothing for a decision to apply to.
    """
    outcome = ToolHookResult(result=None, exception=exception)
    for hook in hooks:
        try:
            hook.after_tool(_hook_view(hook_call), outcome)
        except Exception as exc:  # noqa: BLE001 - hooks must not break dispatch
            log.warning(
                "dispatch.tool_hook_failed",
                hook=_hook_name(hook),
                phase="after_tool",
                error=str(exc),
            )


def _rewritten_arguments(original: dict[str, Any], rewrite: Rewrite) -> dict[str, Any]:
    """The arguments a rewrite asks for, keeping a granted ``approval_id``.

    An approval retry re-dispatches the model's call with the ``approval_id``
    added, and the hook runs again. A hook that builds fresh arguments would
    drop the id and park the call for approval a second time. The approval is
    still checked by the tool against what actually runs.
    """
    arguments = copy.deepcopy(dict(rewrite.arguments))
    if "approval_id" in original and "approval_id" not in arguments:
        arguments["approval_id"] = original["approval_id"]
    return arguments


def _admit_arguments(
    tool_call: ToolCall,
    registered: RegisteredTool,
    ctx: ToolContext | None,
) -> ToolCall | ToolResult:
    """Validate a call's arguments against its schema; coerce or reject.

    A rejection is an ordinary failed call (``is_error``) with its own error
    class and status reason, so failure-streak and progress tracking count it
    like any other failure of the same call.
    """
    try:
        admission = admit_tool_arguments(registered.spec, tool_call.arguments)
    except Exception as exc:  # noqa: BLE001 - a validator bug must not block the tool
        log.warning(
            "dispatch.argument_admission_failed",
            tool=tool_call.tool_name,
            tool_use_id=tool_call.tool_use_id,
            error=str(exc),
        )
        return tool_call
    if not admission.ok:
        log.warning(
            "dispatch.tool_arguments_rejected",
            tool=tool_call.tool_name,
            tool_use_id=tool_call.tool_use_id,
            agent_id=ctx.agent_id if ctx else None,
            session_key=ctx.session_key if ctx else None,
            paths=[issue.path for issue in admission.issues],
        )
        return _build_envelope_result(
            tool_call,
            exc=ToolArgumentsRejectedError(tool_call.tool_name, admission.issues),
            error_class_override=REJECTED_ERROR_CLASS,
            reason_override=REJECTED_REASON,
        )
    if not admission.coerced:
        return tool_call
    log.info(
        "dispatch.tool_arguments_coerced",
        tool=tool_call.tool_name,
        tool_use_id=tool_call.tool_use_id,
        paths=list(admission.coerced),
    )
    return dataclasses.replace(tool_call, arguments=admission.arguments)


def _is_untrusted_caller(ctx: ToolContext | None) -> bool:
    """Return True when the caller cannot be trusted with tool-name disclosure.

    Channel callers and callers with no ``ToolContext`` receive an opaque
    envelope on a registry miss so they cannot enumerate the tool catalogue by
    probing names. Pairing admits a sender to the agent; it does not turn the
    messaging protocol into a Control surface.
    """
    if ctx is None:
        return True
    return ctx.caller_kind is CallerKind.CHANNEL


def _resolve_registry_miss(
    tool_call: ToolCall,
    known_skill_names: frozenset[str],
    ctx: ToolContext | None,
) -> ToolResult:
    untrusted = _is_untrusted_caller(ctx)
    is_skill = tool_call.tool_name in known_skill_names

    # Always record the actual tool name in the structured log so operators
    # retain debug visibility regardless of what the caller is allowed to see.
    log.warning(
        "dispatch.registry_miss",
        tool=tool_call.tool_name,
        tool_use_id=tool_call.tool_use_id,
        is_skill=is_skill,
        untrusted_caller=untrusted,
        agent_id=ctx.agent_id if ctx else None,
        session_key=ctx.session_key if ctx else None,
    )

    if untrusted:
        # Opaque envelope: do NOT echo tool_call.tool_name. A bare CHANNEL
        # caller could otherwise enumerate the registry by probing names and
        # observing which ones come back as ToolNotFound vs. UnsupportedSurface.
        return _build_envelope_result(
            tool_call,
            exc=PermissionError("tool unavailable for this surface"),
            policy_denial=True,
            error_class_override="PolicyDenied",
            user_message_override="Tool unavailable for this surface.",
        )

    if is_skill:
        skill_name = tool_call.tool_name
        user_message = (
            f"{skill_name} is a skill, not a tool. Do not call skill names as tools. "
            f'Use skill_view(name="{skill_name}") to read the skill instructions, '
            "then continue using only tools listed in Available Tools."
        )
        return _build_envelope_result(
            tool_call,
            exc=ValueError("skill call mismatch"),
            policy_denial=True,
            error_class_override="UnsupportedSurface",
            user_message_override=user_message,
        )
    if tool_call.tool_name == "bash":
        user_message = (
            "Tool not found: bash. Use exec_command with a command string instead; "
            "do not retry bash as a tool."
        )
    else:
        user_message = (
            f"Tool not found: {tool_call.tool_name}. Do not retry unavailable tools; "
            "use only tools listed in Available Tools."
        )
    return _build_envelope_result(
        tool_call,
        exc=KeyError(tool_call.tool_name),
        policy_denial=True,
        error_class_override="ToolNotFound",
        user_message_override=user_message,
    )


async def preflight_tool_call(
    *,
    registry: ToolRegistry,
    ctx: ToolContext | None,
    tool_call: ToolCall,
    known_skill_names: set[str] | frozenset[str] | None = None,
    tool_hooks: Sequence[ToolHook] | None = None,
) -> ToolResult | None:
    """Return a denial envelope when a tool call fails dispatch preflight.

    With ``tool_hooks``, their ``before_tool`` runs where it does in a real
    dispatch: a block is a denial, and a rewrite is re-checked and the policy
    chain judges the rewritten call. Those hooks then get ``after_tool`` with
    the denial, or with ``result=None`` when the call would run -- nothing ran.
    Pass hooks only for a check that stands alone (a dry run); a preflight
    followed by a real dispatch would run them twice.
    """
    known = frozenset(known_skill_names or ())
    hooks: tuple[ToolHook, ...] = tuple(tool_hooks or ())

    injection_envelope = _check_injection_guard(tool_call, ctx)
    if injection_envelope is not None:
        return injection_envelope

    tool_call, registered = _lookup_registered(registry, tool_call, ctx)
    if registered is None:
        return _resolve_registry_miss(tool_call, known, ctx)

    non_executable_arguments = _check_non_executable_arguments(tool_call, ctx)
    if non_executable_arguments is not None:
        return non_executable_arguments

    admitted = _admit_arguments(tool_call, registered, ctx)
    if isinstance(admitted, ToolResult):
        return admitted
    tool_call = admitted

    hook_call: ToolHookCall | None = None
    called_hooks: tuple[ToolHook, ...] = ()

    async def _settle(denial: ToolResult | None) -> ToolResult | None:
        if hook_call is None or not called_hooks:
            return denial
        if denial is None:
            _notify_after_tool_hooks_without_result(called_hooks, hook_call, None)
            return None
        return await _run_after_tool_hooks(
            called_hooks, hook_call, denial, ctx=ctx, registered=registered
        )

    if hooks:
        before = _apply_before_tool_hooks(hooks, tool_call, registered, ctx)
        tool_call, hook_call, called_hooks = before.tool_call, before.hook_call, before.called_hooks
        if before.stopped is not None:
            return await _settle(before.stopped)

    dispatch_input = DispatchInput(
        tool_call=tool_call,
        ctx=ctx,
        registered=registered,
        known_skill_names=known,
        registry=registry,
    )

    def _emit_policy_log(log_event: dict) -> None:
        event = log_event.get("event", "dispatch.policy_block")
        fields = {k: v for k, v in log_event.items() if k != "event"}
        log.warning(event, **fields)

    decision = run_chain_with_emit(dispatch_input, emit=_emit_policy_log)
    if not decision.allowed:
        if decision.envelope is None:
            raise RuntimeError("PolicyCheck returned a denial without an envelope")
        return await _settle(decision.envelope)
    return await _settle(None)


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def build_tool_handler(
    registry: ToolRegistry,
    ctx: ToolContext | None = None,
    *,
    known_skill_names: set[str] | None = None,
    tool_hooks: Sequence[ToolHook] | None = None,
) -> AgentToolHandler:
    """Build an async tool handler from a :class:`ToolRegistry`.

    The returned handler:

    1. Injection-guard check before registry lookup.
    2. Registry lookup; returns structured error on miss.
    3. ``ToolHook.before_tool`` fan-out (no-op if ``tool_hooks`` is empty).
    4. Policy chain; first denial returns immediately.
    5. Reserves run budget, including external call counts and text caps.
    6. Dispatches to the registered handler inside the request-scoped contextvar.
    7. Commits or aborts the run-budget reservation.
    8. Finalises the result (execution status, budget, artefacts) via
       :func:`agentos.tools.policy.finalize`.
    9. ``ToolHook.after_tool`` fan-out with the settled result, to every hook
       whose ``before_tool`` ran -- also when the call was blocked, refused,
       denied or skipped by the run budget.
    10. Resets ``current_tool_context`` unconditionally in ``finally``.

    When a hook rewrote the arguments, the result carries the arguments the
    call ran with in ``ToolResult.effective_arguments``.

    ``tool_hooks`` defaults to empty so callers that do not pass hooks are
    bit-for-bit equivalent to the legacy path.
    """
    known = frozenset(known_skill_names or ())
    hooks: tuple[ToolHook, ...] = tuple(tool_hooks or ())
    fallback_budget_tracker = _build_budget_tracker(ctx)
    scoped_budget_trackers: dict[
        int,
        tuple[weakref.ReferenceType[ToolContext], ToolResultBudgetTracker],
    ] = {}
    keyed_run_budget_trackers: dict[str, ToolRunBudgetTracker] = {}

    def _budget_tracker_for(effective_ctx: ToolContext | None) -> ToolResultBudgetTracker:
        if effective_ctx is None or effective_ctx is ctx:
            return fallback_budget_tracker
        key = id(effective_ctx)
        entry = scoped_budget_trackers.get(key)
        if entry is not None:
            context_ref, tracker = entry
            if context_ref() is effective_ctx:
                return tracker
        tracker = _build_budget_tracker(effective_ctx)
        scoped_budget_trackers[key] = (weakref.ref(effective_ctx), tracker)
        return tracker

    def _run_budget_tracker_for(
        effective_ctx: ToolContext | None,
    ) -> ToolRunBudgetTracker:
        run_budget_key = (
            getattr(effective_ctx, "tool_run_budget_key", None)
            if effective_ctx is not None
            else None
        )
        if isinstance(run_budget_key, str) and run_budget_key:
            tracker = keyed_run_budget_trackers.get(run_budget_key)
            if tracker is not None:
                return tracker
            tracker = _build_run_budget_tracker(effective_ctx)
            keyed_run_budget_trackers[run_budget_key] = tracker
            return tracker
        tracker = _build_run_budget_tracker(effective_ctx)
        return tracker

    async def _handler(tool_call: ToolCall) -> ToolResult:
        effective_ctx = current_tool_context.get() or ctx

        # 1. Ingress injection guard.
        injection_envelope = _check_injection_guard(tool_call, effective_ctx)
        if injection_envelope is not None:
            return injection_envelope

        # 2. Registry lookup (a near-miss name resolves to the one tool it means).
        tool_call, registered = _lookup_registered(registry, tool_call, effective_ctx)
        if registered is None:
            return _resolve_registry_miss(tool_call, known, effective_ctx)

        non_executable_arguments = _check_non_executable_arguments(tool_call, effective_ctx)
        if non_executable_arguments is not None:
            return non_executable_arguments

        # 2b. Argument admission: validate against the schema, coerce or reject.
        admitted = _admit_arguments(tool_call, registered, effective_ctx)
        if isinstance(admitted, ToolResult):
            return admitted
        tool_call = admitted

        # 3. ToolHook.before_tool: observe, block, or rewrite the arguments.
        hook_call: ToolHookCall | None = None
        called_hooks: tuple[ToolHook, ...] = ()
        rewritten = False

        async def _settle(result: ToolResult, **outcome: Any) -> ToolResult:
            if rewritten:
                result = dataclasses.replace(
                    result, effective_arguments=copy.deepcopy(tool_call.arguments)
                )
            if hook_call is None or not called_hooks:
                return result
            return await _run_after_tool_hooks(
                called_hooks,
                hook_call,
                result,
                ctx=effective_ctx,
                registered=registered,
                **outcome,
            )

        if hooks:
            before = _apply_before_tool_hooks(hooks, tool_call, registered, effective_ctx)
            hook_call, called_hooks = before.hook_call, before.called_hooks
            if before.stopped is not None:
                return await _settle(before.stopped)
            tool_call, rewritten = before.tool_call, before.rewritten

        # 4. Policy chain — first denial wins. Single emission site via run_chain_with_emit.
        dispatch_input = DispatchInput(
            tool_call=tool_call,
            ctx=effective_ctx,
            registered=registered,
            known_skill_names=known,
            registry=registry,
        )

        def _emit_policy_log(log_event: dict) -> None:
            event = log_event.get("event", "dispatch.policy_block")
            fields = {k: v for k, v in log_event.items() if k != "event"}
            log.warning(event, **fields)

        decision = run_chain_with_emit(dispatch_input, emit=_emit_policy_log)
        if not decision.allowed:
            if decision.envelope is None:
                raise RuntimeError("PolicyCheck returned a denial without an envelope")
            return await _settle(decision.envelope)

        # 5. Handler dispatch inside the request-scoped contextvar.
        run_budget_tracker = _run_budget_tracker_for(effective_ctx)
        try:
            run_budget_policy = _resolve_run_budget_policy(effective_ctx)
            reservation = await run_budget_tracker.reserve_tool_call(
                tool_name=tool_call.tool_name,
                arguments=clamp_tool_arguments(
                    tool_call.tool_name,
                    dict(tool_call.arguments),
                    run_budget_policy,
                ),
            )
        except ToolRunBudgetExceededError as exc:
            return await _settle(_build_run_budget_control_result(tool_call, exc))

        token = current_tool_context.set(effective_ctx)
        from agentos.engine.usage import _current_tool_name

        tool_token = _current_tool_name.set(tool_call.tool_name)
        tool_started_at = time.monotonic()
        raw_result: Any = None
        exception: BaseException | None = None
        artifact_start = len(effective_ctx.published_artifacts) if effective_ctx is not None else 0
        settled_result: ToolResult | None = None
        try:
            raw_result = await registered.handler(**reservation.arguments)
            await run_budget_tracker.commit_tool_result(reservation, raw_result)
        except asyncio.CancelledError as exc:
            exception = exc
            await run_budget_tracker.abort_tool_result(reservation)
            raise
        except ToolRunBudgetExceededError as exc:
            exception = exc
            if raw_result is None:
                await run_budget_tracker.abort_tool_result(reservation)
        except Exception as exc:  # noqa: BLE001
            exception = exc
            await run_budget_tracker.abort_tool_result(reservation)
        finally:
            try:
                if isinstance(exception, asyncio.CancelledError):
                    # The call is going away: hooks hear about it, but there
                    # is no result for a decision to apply to.
                    if hook_call is not None:
                        _notify_after_tool_hooks_without_result(called_hooks, hook_call, exception)
                else:
                    await _emit_webresearch_tool_run_diagnostics(
                        tool_call=tool_call,
                        effective_ctx=effective_ctx,
                        reservation=reservation,
                        run_budget_tracker=run_budget_tracker,
                        started_at=tool_started_at,
                        raw_result=raw_result,
                        exception=exception,
                    )
                    # 6. Single finalisation point, then 7. ToolHook.after_tool
                    # over the settled result. Assigned here (not returned) so
                    # an in-flight CancelledError keeps propagating instead of
                    # being swallowed by the finally.
                    settled_result = await _settle(
                        await finalize(
                            tool_call,
                            effective_ctx,
                            raw_result,
                            exception,
                            artifact_start,
                            _budget_tracker_for(effective_ctx),
                            registered,
                        ),
                        raw_result=raw_result,
                        exception=exception,
                    )
            finally:
                from agentos.engine.usage import _current_tool_name

                _current_tool_name.reset(tool_token)
                current_tool_context.reset(token)
        # Reached only when no exception is propagating (CancelledError
        # re-raises above), so finalize has always run by this point.
        assert settled_result is not None
        return settled_result

    return _handler
