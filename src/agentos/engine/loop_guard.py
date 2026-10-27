"""Turn the progress watchdog's decisions into what the model and user see.

``progress_watchdog`` only decides. This module applies a decision to one
iteration of the turn loop:

* a ``warn`` decision appends its guidance to the tool results it is about,
  so the model reads it next to the output that triggered it;
* a ``block`` decision answers every call of the proposed batch with a
  synthetic result instead of running it, telling the model to stop repeating
  the action and reassess;
* a terminal ``block`` (the second in one turn) also ends the turn with the
  ``loop_detected`` error code and a message for the user.

``[tools] loop_guard = "observe"`` turns all of this off: decisions are only
written to the turn call log, as before.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from agentos.engine.progress_watchdog import (
    ProgressDecision,
    call_key,
    guidance_for,
)
from agentos.engine.types import ErrorEvent, ToolCall, ToolResult
from agentos.execution_status import runtime_execution_status
from agentos.provider import ContentBlockToolResult

LoopGuardMode = Literal["enforce", "observe"]
LOOP_GUARD_MODES: tuple[LoopGuardMode, ...] = ("enforce", "observe")
LOOP_DETECTED_CODE = "loop_detected"
TOOL_LOOP_BLOCKED_REASON = "tool_loop_blocked"


def loop_guard_enforced(mode: Any) -> bool:
    """Anything but an explicit ``"observe"`` acts on decisions."""

    return str(mode or "enforce").strip().lower() != "observe"


def append_guidance(
    blocks: Sequence[ContentBlockToolResult],
    calls: Sequence[ToolCall],
    decision: ProgressDecision,
) -> list[str]:
    """Append the decision's guidance to the tool results it concerns.

    Returns the ``tool_use_id`` of every block that received it. When no block
    matches the decision's targets (a provider-level signal, say), the last
    block carries it so the model still sees it once.
    """

    guidance = guidance_for(decision)
    if not guidance or not blocks:
        return []
    keys_by_id = {call.tool_use_id: call_key(call.tool_name, call.arguments) for call in calls}
    targets = set(decision.targets)
    chosen = [block for block in blocks if keys_by_id.get(block.tool_use_id) in targets]
    if not chosen:
        chosen = [blocks[-1]]
    note = f"[Loop check] {guidance}"
    for block in chosen:
        block.content = _with_note(block.content, note)
    return [block.tool_use_id for block in chosen]


def blocked_batch_results(
    decision: ProgressDecision,
    calls: Sequence[ToolCall],
) -> dict[str, ToolResult]:
    """One synthetic result per call, so the batch is answered without running."""

    targets = set(decision.targets)
    guidance = guidance_for(decision)
    results: dict[str, ToolResult] = {}
    for call in calls:
        is_trigger = call_key(call.tool_name, call.arguments) in targets
        results[call.tool_use_id] = ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content=_blocked_text(guidance, is_trigger=is_trigger, terminal=decision.terminal),
            is_error=True,
            execution_status=runtime_execution_status("error", reason=TOOL_LOOP_BLOCKED_REASON),
        )
    return results


def loop_detected_error(decision: ProgressDecision) -> ErrorEvent:
    """The user-facing end of a turn stopped by the loop guard."""

    tool = decision.details.get("tool") or "a tool"
    if decision.reason == "ping_pong":
        paired = decision.details.get("paired_tool") or tool
        what = f"alternating between {tool} and {paired}"
        if paired == tool:
            what = f"alternating between two {tool} calls"
    else:
        what = f"repeating the same {tool} call"
    return ErrorEvent(
        message=(
            f"Stopped this turn: the agent kept {what} without getting any new"
            " result, even after it was told to change course. Send a message to"
            " continue or point it in a different direction."
        ),
        code=LOOP_DETECTED_CODE,
    )


def loop_guard_log_fields(decision: ProgressDecision) -> Mapping[str, Any]:
    return {
        "action": decision.action,
        "reason": decision.reason,
        "details": decision.details,
        "targets": [list(target) for target in decision.targets],
        "terminal": decision.terminal,
        "guidance": guidance_for(decision),
    }


def _blocked_text(guidance: str, *, is_trigger: bool, terminal: bool) -> str:
    if terminal:
        if is_trigger:
            return (
                f"{guidance}\n\nNot run. The loop check already stopped this action"
                " once in this turn and it was proposed again, so the turn ends here."
            )
        return (
            "Not run: another call in this batch repeated an action the loop check"
            " had already stopped, so the turn ends here."
        )
    if is_trigger:
        return (
            f"{guidance}\n\nNot run: this exact call has stopped producing anything"
            " new. Do not repeat it unchanged. Reassess the task — answer the user"
            " with what you already have, ask a clarifying question, or try a"
            " different tool or different arguments."
        )
    return (
        "Not run: another call in this batch repeated an action that keeps"
        " returning the same result. Reassess the task before choosing the next"
        " step; this call may be proposed again if it is still needed."
    )


def _with_note(content: str | list[Any], note: str) -> str | list[Any]:
    if isinstance(content, str):
        return f"{content}\n\n{note}" if content else note
    return [*content, {"type": "text", "text": note}]
