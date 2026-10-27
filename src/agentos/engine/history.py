"""History turn limiting, orphan tool-pairing repair, and transcript reload."""

from __future__ import annotations

import json
from typing import Any, TypeGuard

from agentos.execution_status import (
    normalize_execution_status,
    normalize_legacy_execution_status,
)
from agentos.provider import (
    ContentBlockText,
    ContentBlockToolResult,
    ContentBlockToolUse,
    Message,
)

_SYNTHETIC_USER_PREFIXES = (
    "[Available skills for this turn]",
    "[Context summary]",
    "[Request context for this turn]",
    "[Runtime context for this turn]",
)


def _is_real_user_turn(message: Message) -> bool:
    if message.role != "user":
        return False
    content = message.content
    if isinstance(content, str):
        return not content.startswith(_SYNTHETIC_USER_PREFIXES)
    if isinstance(content, list):
        return not all(isinstance(block, ContentBlockToolResult) for block in content)
    return True


def limit_turns(messages: list[Message], max_turns: int) -> list[Message]:
    """Keep the most recent max_turns user/assistant turn pairs.

    A 'turn' is counted by user messages. Returns the original list
    reference if no truncation needed (caller can use identity check).
    """
    if max_turns <= 0 or not messages:
        return messages

    # Count real user messages from the end; synthetic context messages should
    # not evict conversation turns from the provider prefix.
    user_count = 0
    cut_index = 0
    for i in range(len(messages) - 1, -1, -1):
        if _is_real_user_turn(messages[i]):
            user_count += 1
            if user_count > max_turns:
                # i is the user msg we want to exclude; next msg after i is the cut point
                # but we want to start at the *next* user msg (i+2 skips the assistant at i+1)
                # Actually: cut at the user msg itself, i.e. cut_index = i + 1 would include
                # the assistant reply to this user msg. We want to exclude msg[i] and prior,
                # so cut at the first non-excluded index, which is i+1 only if i+1 is a user msg.
                # Simpler: scan forward from i+1 to find the next user message.
                cut_index = i + 1
                while cut_index < len(messages) and not _is_real_user_turn(messages[cut_index]):
                    cut_index += 1
                break

    if cut_index == 0:
        return messages  # within budget

    return messages[cut_index:]


# What the model reads for a tool call whose result never reached history
# (the process stopped while the call ran, or the turn was cut short). It must
# neither claim the call failed nor that it succeeded: it may have run.
UNKNOWN_TOOL_OUTCOME_TEXT = (
    "No result was recorded for this tool call: the turn stopped before the "
    "result came back (for example, the process was restarted). The call may "
    "or may not have run, and its effects are unknown. Check the current state "
    "before repeating it."
)
UNKNOWN_TOOL_OUTCOME_REASON = "missing_tool_result"


def unknown_tool_outcome_result(tool_use_id: str) -> ContentBlockToolResult:
    """The stand-in result for a call that has none in history."""
    return ContentBlockToolResult(
        tool_use_id=tool_use_id,
        content=UNKNOWN_TOOL_OUTCOME_TEXT,
        is_error=True,
        execution_status={
            "version": 1,
            "status": "error",
            "exit_code": None,
            "timed_out": False,
            "truncated": False,
            "reason": UNKNOWN_TOOL_OUTCOME_REASON,
            "source": "replay",
            "preservation_class": "diagnostic",
        },
    )


def is_unknown_tool_outcome_result(block: Any) -> bool:
    status = getattr(block, "execution_status", None)
    return (
        isinstance(block, ContentBlockToolResult)
        and isinstance(status, dict)
        and status.get("reason") == UNKNOWN_TOOL_OUTCOME_REASON
    )


def _is_tool_use_block(block: Any) -> TypeGuard[ContentBlockToolUse]:
    return isinstance(block, ContentBlockToolUse)


def _is_tool_result_block(block: Any) -> TypeGuard[ContentBlockToolResult]:
    return isinstance(block, ContentBlockToolResult)


def _extract_tool_use_ids(content: Any) -> set[str]:
    """Extract tool_use IDs from message content."""
    ids: set[str] = set()
    if isinstance(content, list):
        for block in content:
            # ContentBlockToolUse has 'id' field
            if _is_tool_use_block(block):
                ids.add(block.id)
    return ids


def _extract_tool_result_ids(content: Any) -> set[str]:
    """Extract tool_use_ids from tool result blocks."""
    ids: set[str] = set()
    if isinstance(content, list):
        for block in content:
            if _is_tool_result_block(block):
                ids.add(block.tool_use_id)
    return ids


def _ordered_call_ids(message: Message) -> list[str]:
    if message.role != "assistant" or not isinstance(message.content, list):
        return []
    return list(dict.fromkeys(block.id for block in message.content if _is_tool_use_block(block)))


def _without_tool_results(message: Message) -> Message | None:
    """*message* minus its tool_result blocks (``None`` when nothing is left)."""
    if not isinstance(message.content, list):
        return message
    kept = [block for block in message.content if not _is_tool_result_block(block)]
    if len(kept) == len(message.content):
        return message
    if not kept:
        return None
    return Message(
        role=message.role,
        content=kept,
        reasoning_content=message.reasoning_content,
        reasoning_details=message.reasoning_details,
    )


def _frame_is_paired(call_ids: list[str], span: list[Message]) -> bool:
    """True when *span* opens with results for exactly *call_ids*, once each."""
    wanted = set(call_ids)
    seen: list[str] = []
    index = 0
    while index < len(span):
        message = span[index]
        if message.role != "user" or not _extract_tool_result_ids(message.content):
            break
        for block in message.content:
            if _is_tool_result_block(block):
                seen.append(block.tool_use_id)
        index += 1
    if len(seen) != len(set(seen)) or set(seen) != wanted:
        return False
    # No stray results later in the frame.
    return not any(_extract_tool_result_ids(message.content) for message in span[index:])


def _repair_frame(call_ids: list[str], span: list[Message]) -> list[Message]:
    """Results for *call_ids* right after the call, in call order.

    A result found anywhere in the frame is moved up; a call without one gets
    :func:`unknown_tool_outcome_result`. Results that answer no call of this
    frame are dropped; the rest of every message is kept where it was.
    """
    wanted = set(call_ids)
    found: dict[str, Any] = {}
    for message in span:
        if not isinstance(message.content, list):
            continue
        for block in message.content:
            if (
                _is_tool_result_block(block)
                and block.tool_use_id in wanted
                and block.tool_use_id not in found
            ):
                found[block.tool_use_id] = block
    results = [found.get(call_id) or unknown_tool_outcome_result(call_id) for call_id in call_ids]
    repaired: list[Message] = [Message(role="user", content=results)]
    for message in span:
        rest = _without_tool_results(message)
        if rest is not None:
            repaired.append(rest)
    return repaired


def repair_tool_pairing(messages: list[Message]) -> list[Message]:
    """Make every tool call answered, and answered right after it.

    OpenAI-compatible providers require an assistant message with tool calls
    to be followed immediately by tool result messages for every requested
    ``tool_call_id``; Anthropic requires the same of the next user message. A
    matching ID elsewhere in the transcript is not enough.

    The assistant message is always kept, text included: a call that is in
    history may have happened, and forgetting it invites the model to repeat
    a side effect. Per assistant message with tool calls (its *frame* runs to
    the next assistant message):

    * results already in place are left untouched;
    * a result that sits later in the frame is moved up next to its call;
    * a call with no result gets an unknown-outcome error result
      (:data:`UNKNOWN_TOOL_OUTCOME_TEXT`) telling the model to check the
      current state before repeating it;
    * results that answer no call of their frame are dropped.

    Deterministic, so a repaired prefix is byte-identical on every load.
    Returns the original list reference if no repairs are needed.
    """
    if not messages:
        return messages

    repaired: list[Message] = []
    changed = False
    index = 0
    total = len(messages)
    while index < total:
        message = messages[index]
        call_ids = _ordered_call_ids(message)
        if message.role == "assistant":
            repaired.append(message)
            start = index + 1
        else:
            # Messages before the first assistant message pair with nothing.
            start = index
        end = index + 1
        while end < total and messages[end].role != "assistant":
            end += 1
        span = messages[start:end]
        if not call_ids:
            # Nothing to answer here: any tool result is an orphan.
            for item in span:
                rest = _without_tool_results(item)
                changed = changed or rest is not item
                if rest is not None:
                    repaired.append(rest)
        elif _frame_is_paired(call_ids, span):
            repaired.extend(span)
        else:
            repaired.extend(_repair_frame(call_ids, span))
            changed = True
        index = end

    return repaired if changed else messages


def _coerce_tool_input(raw: Any) -> dict[str, Any]:
    """Coerce a persisted tool_use.input back into a dict.

    Persistence may store input as dict, JSON string, or empty string (mid-stream
    partial). Anthropic's tool_use.input must conform to the tool's input_schema;
    fabricating a fallback key like ``{"_raw": ...}`` produces a shape no real
    schema declares, so on any non-dict payload we emit ``{}`` — a faithful
    "input missing" marker. Matching tool_result blocks still pair via
    tool_use_id, and ``repair_tool_pairing`` answers a call left without one.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def reconstruct_messages_from_entry(
    role: str,
    content: Any,
    tool_calls: list[dict[str, Any]] | None,
    reasoning_content: str | None = None,
) -> list[Message]:
    """Rebuild provider Messages from one persisted transcript entry.

    An assistant turn is persisted as a single row whose ``tool_calls`` JSON
    column flattens every iteration's segments (text / tool_use / tool_result)
    into one ordered list. The in-memory agent loop instead appends a separate
    Message per iteration (see ``agent.run_turn``): each iteration produces an
    assistant message (text + tool_use blocks) followed, once the tools run,
    by a user message carrying tool_result blocks. A multi-iteration turn
    reloaded from disk must restore that per-iteration shape.

    Segmentation rule: a ``tool_result`` segment closes the current iteration.
    Whatever arrives after (text or tool_use) starts the next iteration — so we
    flush the accumulated assistant + user(tool_result) pair first.

    Returns ``[]`` for entries that contribute nothing. Tool_use blocks
    without a matching tool_result are preserved here; ``repair_tool_pairing``
    gives them an unknown-outcome result later.
    """
    if role not in ("user", "assistant"):
        return []

    if role == "user":
        if content:
            return [Message(role="user", content=content)]
        return []

    if not tool_calls:
        if content:
            return [
                Message(
                    role="assistant",
                    content=content,
                    reasoning_content=reasoning_content,
                )
            ]
        return []

    messages: list[Message] = []
    pending_assistant: list[Any] = []
    pending_results: list[Any] = []

    def _flush() -> None:
        if pending_assistant:
            messages.append(Message(role="assistant", content=list(pending_assistant)))
            pending_assistant.clear()
        if pending_results:
            messages.append(Message(role="user", content=list(pending_results)))
            pending_results.clear()

    for seg in tool_calls:
        if not isinstance(seg, dict):
            continue
        seg_type = seg.get("type")
        if seg_type == "text":
            text = seg.get("text") or ""
            if not text:
                continue
            # text after a tool_result begins the next iteration → flush prior pair
            if pending_results:
                _flush()
            pending_assistant.append(ContentBlockText(text=text))
        elif seg_type == "tool_use":
            tool_use_id = seg.get("tool_use_id") or seg.get("id")
            name = seg.get("name") or ""
            if not tool_use_id or not name:
                continue
            if pending_results:
                _flush()
            pending_assistant.append(
                ContentBlockToolUse(
                    id=tool_use_id,
                    name=name,
                    input=_coerce_tool_input(seg.get("input")),
                    thought_signature=seg.get("thought_signature") or seg.get("thoughtSignature"),
                )
            )
        elif seg_type == "tool_result":
            tool_use_id = seg.get("tool_use_id")
            if not tool_use_id:
                continue
            raw_result = seg.get("result", "")
            if isinstance(raw_result, (str, list)):
                result_content: str | list[Any] = raw_result
            else:
                result_content = str(raw_result)
            pending_results.append(
                ContentBlockToolResult(
                    tool_use_id=tool_use_id,
                    content=result_content,
                    is_error=bool(seg.get("is_error")),
                    execution_status=(
                        normalize_execution_status(seg.get("execution_status"))
                        if "execution_status" in seg
                        else normalize_legacy_execution_status(is_error=bool(seg.get("is_error")))
                    ),
                )
            )

    _flush()

    # If the segment list carried no text at all but the entry.content still
    # holds the concatenated turn text, prepend it to the first assistant
    # message as a best-effort preserve. (The per-iteration assignment is
    # ambiguous in this degenerate case, but never happens in practice — the
    # runtime flushes current_text_parts into a "text" segment before any
    # tool_use or at end of stream.)
    if (
        isinstance(content, str)
        and content.strip()
        and not any(
            isinstance(m.content, list) and any(isinstance(b, ContentBlockText) for b in m.content)
            for m in messages
            if m.role == "assistant"
        )
    ):
        first_assistant = next((m for m in messages if m.role == "assistant"), None)
        if first_assistant is not None and isinstance(first_assistant.content, list):
            first_assistant.content.insert(0, ContentBlockText(text=content))
        elif not messages:
            messages.append(Message(role="assistant", content=content))

    if isinstance(content, str) and "[generated artifact omitted:" in content:
        markers = [
            line.strip()
            for line in content.splitlines()
            if line.strip().startswith("[generated artifact omitted:")
        ]
        if markers:
            marker_text = "\n".join(markers)
            assistant = next(
                (
                    m
                    for m in reversed(messages)
                    if m.role == "assistant" and isinstance(m.content, list)
                ),
                None,
            )
            if assistant is not None:
                content_blocks = assistant.content
                if not isinstance(content_blocks, list):
                    return messages
                existing = "\n".join(
                    block.text for block in content_blocks if isinstance(block, ContentBlockText)
                )
                if marker_text not in existing:
                    content_blocks.append(ContentBlockText(text=marker_text))
            elif not messages:
                messages.append(Message(role="assistant", content=marker_text))

    if reasoning_content:
        first_assistant = next((m for m in messages if m.role == "assistant"), None)
        if first_assistant is not None:
            first_assistant.reasoning_content = reasoning_content

    return messages
