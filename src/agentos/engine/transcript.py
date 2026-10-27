"""Append-only structured transcript: storage codec, history load, append rules.

The ``Agent``'s ``list[Message]`` is the single source of truth for what a
provider sees. This module owns the three rules that keep it append-only:

1. **Persist as-is.** Each assistant transcript row carries a
   ``provider_messages`` record (:data:`PROVIDER_MESSAGES_KIND`) holding the
   exact provider-facing messages the turn appended -- the turn's input user
   message (with its trailing volatile-context text), every assistant message
   with its content blocks (text, ``tool_use``, per-block ``thinking`` /
   ``redacted_thinking`` with ``signature`` / ``data``) and every
   ``tool_result`` message. Loading replays that record verbatim, so the next
   turn's request starts with a byte-identical prefix.

2. **Read both formats.** Rows without a record (sessions written before the
   structured record existed, partial or cancelled turns) are rebuilt by the
   legacy heuristic reconstruction in :mod:`agentos.engine.history` and then
   migrated ONCE, deterministically (historical tool payload projection,
   projected-argument scrub, reasoning cleanup). Because the migration is a
   pure per-message function, a legacy prefix is also stable across turns.

3. **Shrink before first send.** New messages are "sealed" before a provider
   request first carries them (see :func:`seal_window`): an oversized tool
   result is projected to its per-class budget, copied-marker tool calls are
   stripped, and the new window's tool pairing is checked. A sealed message is
   never rewritten afterwards; compaction is the only history edit and it
   replaces the whole prefix (a ``reset`` record).

Record shape (``transcript_entries.provider_messages`` JSON)::

    {
      "kind": "agentos.provider_messages",
      "v": 1,
      "mode": "append" | "reset",
      "input_entry_id": "<message_id of the persisted user row it replays>" | null,
      "compaction_id": "<id>" | null,
      "messages": [
        {"role": "user", "content": "text ..."},
        {"role": "assistant",
         "content": [
           {"type": "thinking", "thinking": "...", "signature": "..."},
           {"type": "redacted_thinking", "data": "..."},
           {"type": "text", "text": "..."},
           {"type": "tool_use", "id": "...", "name": "...", "input": {...}}
         ],
         "reasoning_content": "...",
         "reasoning_details": [{...}],
         "provenance": {"provider": "...", "model": "...", "api": "..."}},
        {"role": "user",
         "content": [{"type": "tool_result", "tool_use_id": "...", "content": "..."}]}
      ]
    }

``append`` records extend the history; a ``reset`` record (written by a turn
that compacted) replaces everything before it. Assistant messages also keep
``reasoning_content`` and OpenRouter ``reasoning_details`` verbatim, plus the
``provenance`` (provider, model, API family) of the response that produced
them; rows written before provenance existed read as unknown. Whether a
later request replays reasoning is the provider's decision (and, for models
that take none, an opt-in drop on load) -- never a storage one. A request to a
different model drops the foreign signed reasoning in the request view only
(:mod:`agentos.provider.replay`).
"""

from __future__ import annotations

import hashlib
import json
import re
import weakref
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import structlog
from pydantic import BaseModel

from agentos.engine.history import reconstruct_messages_from_entry, repair_tool_pairing
from agentos.provider import types as provider_types
from agentos.provider.types import (
    ContentBlockCompaction,
    ContentBlockDocument,
    ContentBlockImage,
    ContentBlockRedactedThinking,
    ContentBlockText,
    ContentBlockThinking,
    ContentBlockToolResult,
    ContentBlockToolUse,
    Message,
    MessageProvenance,
)

log = structlog.get_logger(__name__)

PROVIDER_MESSAGES_KIND = "agentos.provider_messages"
PROVIDER_MESSAGES_VERSION = 1

RecordMode = Literal["append", "reset"]

# Markers the engine writes into tool-call arguments when it projects them.
# A model that copies one back must never have it dispatched or replayed.
TOOL_ARGUMENT_PROJECTION_PREFIX = "[tool_use_argument_projection]\n"
HISTORICAL_TOOL_ARGUMENT_PROJECTION_PREFIX = "[historical_tool_argument_omitted]\n"
INVALID_PROVIDER_CONTEXT_PROJECTION_PREFIX = "[invalid_provider_context_projection:"
INVALID_PROVIDER_CONTEXT_ARGUMENTS_KEY = "_invalid_provider_context_arguments"
COMPACTED_TOOL_ARGUMENT_MARKERS = frozenset(
    {
        "_agentos_compacted_tool_arguments",
        "_agentos_compacted_tool_input",
    }
)
_PROJECTION_PREFIXES = (
    TOOL_ARGUMENT_PROJECTION_PREFIX,
    HISTORICAL_TOOL_ARGUMENT_PROJECTION_PREFIX,
    INVALID_PROVIDER_CONTEXT_PROJECTION_PREFIX,
)

# Volatile per-turn context headers. They travel as trailing text of the
# CURRENT user turn and are persisted with it; nothing rewrites them later.
SKILLS_CONTEXT_HEADER = "[Available skills for this turn]"
REQUEST_CONTEXT_HEADER = "[Request context for this turn]"
RUNTIME_CONTEXT_HEADER = "[Runtime context for this turn]"
CONTEXT_SUMMARY_HEADER = "[Context summary]"


# ---------------------------------------------------------------------------
# Record codec
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TranscriptRecord:
    """The provider-facing messages one turn appended (or reset to)."""

    messages: list[Message]
    mode: RecordMode = "append"
    input_entry_id: str | None = None
    compaction_id: str | None = None


def _block_classes() -> dict[str, type[BaseModel]]:
    classes: dict[str, type[BaseModel]] = {
        "text": ContentBlockText,
        "tool_use": ContentBlockToolUse,
        "tool_result": ContentBlockToolResult,
        "image": ContentBlockImage,
        "document": ContentBlockDocument,
        "thinking": ContentBlockThinking,
        "redacted_thinking": ContentBlockRedactedThinking,
        "compaction": ContentBlockCompaction,
    }
    # Any block type a newer provider layer adds is picked up by name.
    for name in dir(provider_types):
        candidate = getattr(provider_types, name)
        if (
            name.startswith("ContentBlock")
            and isinstance(candidate, type)
            and issubclass(candidate, BaseModel)
        ):
            block_type = candidate.model_fields.get("type")
            default = getattr(block_type, "default", None) if block_type else None
            if isinstance(default, str) and default not in classes:
                classes[default] = candidate
    return classes


def encode_content_block(block: Any) -> dict[str, Any] | None:
    """Return the JSON form of one content block (``None`` if unusable)."""
    if isinstance(block, BaseModel):
        # Full dump (no ``exclude_none``): nested typed dicts such as a tool
        # result's ``execution_status`` require their ``None`` keys to validate
        # on the way back, and the round trip must be exact.
        payload = block.model_dump(mode="json")
        return payload if isinstance(payload.get("type"), str) else None
    if isinstance(block, dict) and isinstance(block.get("type"), str):
        copied: dict[str, Any] = json.loads(json.dumps(block, ensure_ascii=False, default=str))
        return copied
    return None


def decode_content_block(payload: Any) -> Any | None:
    """Rebuild one content block; unknown or invalid blocks return ``None``."""
    if not isinstance(payload, dict):
        return None
    block_type = payload.get("type")
    model_cls = _block_classes().get(block_type) if isinstance(block_type, str) else None
    if model_cls is None:
        return None
    try:
        return model_cls.model_validate(payload)
    except Exception:  # noqa: BLE001 - a corrupt block must not break history load
        return None


def _with_content(message: Message, content: Any) -> Message:
    """*message* with new content; every other field carried over."""
    return Message(
        role=message.role,
        content=content,
        reasoning_content=message.reasoning_content,
        reasoning_details=message.reasoning_details,
        provenance=message.provenance,
    )


def encode_message(message: Message) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": message.role}
    if isinstance(message.content, str):
        payload["content"] = message.content
    else:
        payload["content"] = [
            encoded
            for encoded in (encode_content_block(block) for block in message.content)
            if encoded is not None
        ]
    if message.reasoning_content is not None:
        payload["reasoning_content"] = message.reasoning_content
    if message.reasoning_details is not None:
        payload["reasoning_details"] = json.loads(
            json.dumps(message.reasoning_details, ensure_ascii=False, default=str)
        )
    if message.provenance is not None:
        payload["provenance"] = message.provenance.model_dump(mode="json", exclude_none=True)
    return payload


def _decode_provenance(payload: Any) -> MessageProvenance | None:
    """Read a stored provenance; anything unusable reads as unknown."""
    if not isinstance(payload, dict):
        return None
    fields = {
        key: value.strip()
        for key in ("provider", "model", "api")
        if isinstance(value := payload.get(key), str) and value.strip()
    }
    return MessageProvenance(**fields) if fields else None


def decode_message(payload: Any) -> Message | None:
    if not isinstance(payload, dict):
        return None
    role = payload.get("role")
    if role not in ("user", "assistant"):
        return None
    raw_content = payload.get("content")
    content: Any
    if isinstance(raw_content, str):
        content = raw_content
    elif isinstance(raw_content, list):
        blocks = [decode_content_block(item) for item in raw_content]
        content = [block for block in blocks if block is not None]
        if raw_content and not content:
            return None
    else:
        return None
    reasoning = payload.get("reasoning_content")
    details = payload.get("reasoning_details")
    return Message(
        role=role,
        content=content,
        reasoning_content=reasoning if isinstance(reasoning, str) else None,
        reasoning_details=(
            [item for item in details if isinstance(item, dict)]
            if isinstance(details, list)
            else None
        ),
        provenance=_decode_provenance(payload.get("provenance")),
    )


def encode_transcript_record(record: TranscriptRecord) -> dict[str, Any]:
    return {
        "kind": PROVIDER_MESSAGES_KIND,
        "v": PROVIDER_MESSAGES_VERSION,
        "mode": record.mode,
        "input_entry_id": record.input_entry_id,
        "compaction_id": record.compaction_id,
        "messages": [encode_message(message) for message in record.messages],
    }


def decode_transcript_record(payload: Any) -> TranscriptRecord | None:
    """Parse a stored ``provider_messages`` value; ``None`` means "use legacy"."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return None
    if not isinstance(payload, dict):
        return None
    if payload.get("kind") != PROVIDER_MESSAGES_KIND:
        return None
    version = payload.get("v")
    if not isinstance(version, int) or version < 1 or version > PROVIDER_MESSAGES_VERSION:
        return None
    raw_messages = payload.get("messages")
    if not isinstance(raw_messages, list):
        return None
    messages: list[Message] = []
    for raw in raw_messages:
        message = decode_message(raw)
        if message is None:
            log.warning("transcript.record_message_undecodable")
            return None
        messages.append(message)
    mode = payload.get("mode")
    input_entry_id = payload.get("input_entry_id")
    compaction_id = payload.get("compaction_id")
    return TranscriptRecord(
        messages=messages,
        mode="reset" if mode == "reset" else "append",
        input_entry_id=input_entry_id if isinstance(input_entry_id, str) else None,
        compaction_id=compaction_id if isinstance(compaction_id, str) else None,
    )


def persistable_messages(messages: Iterable[Message]) -> list[Message]:
    """Return *messages* as they are stored for later turns.

    Image and document bytes are not stored a second time (the user row keeps
    the attachment envelope) and are never replayed on a later turn: a text
    follow-up may be routed to a text-only model. They become the same
    compact markers the legacy reload produced.
    """
    out: list[Message] = []
    for message in messages:
        if not isinstance(message.content, list):
            out.append(message)
            continue
        changed = False
        blocks: list[Any] = []
        for block in message.content:
            if isinstance(block, ContentBlockImage):
                blocks.append(
                    ContentBlockText(
                        text=f"[historical image omitted: {block.media_type or 'image'}]"
                    )
                )
                changed = True
            elif isinstance(block, ContentBlockDocument):
                label = block.title or block.media_type
                blocks.append(ContentBlockText(text=f"[historical document omitted: {label}]"))
                changed = True
            else:
                blocks.append(block)
        out.append(_with_content(message, blocks) if changed else message)
    return out


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


@dataclass
class LoadedTranscript:
    messages: list[Message] = field(default_factory=list)
    summary_markers: list[str] = field(default_factory=list)
    last_entry_was_user: bool = False
    # ``id`` of the row whose ``reset`` record the history starts from; durable
    # summaries covering rows before it are already inside the transcript.
    reset_entry_id: int | None = None
    structured_entries: int = 0
    legacy_entries: int = 0
    # The persisted user row of the turn being loaded (``current_input_id``).
    input_entry: Any = None
    # The row the trailing user message came from (when it is the last one).
    last_user_entry: Any = None


def build_history_from_transcript(
    entries: Sequence[Any],
    *,
    unpack_user: Callable[[str], Any] = lambda content: content,
    unpack_assistant: Callable[[str], Any] = lambda content: content,
    summary_marker: str | None = None,
    strip_summary_marker: Callable[[str], str] = lambda content: content,
    legacy_migrate: Callable[[list[Message]], list[Message]] | None = None,
    current_input_id: str | None = None,
) -> LoadedTranscript:
    """Rebuild provider messages from persisted transcript rows.

    Rows with a structured record are replayed verbatim; rows without one go
    through the legacy reconstruction and *legacy_migrate*. A record that
    names its turn's persisted user row (``input_entry_id``) replaces that row,
    because the record already starts with the turn's input message.

    User rows persisted *between* a record's input row and the record's own
    row are follow-ups queued while that turn ran (``sessions.send`` persists
    them at once). They are replayed after the record, where the model met
    them, not before it.

    *current_input_id* names the persisted user row of the turn being loaded:
    it is left out of the history (the agent appends it itself) and returned
    as :attr:`LoadedTranscript.input_entry`, wherever it sits.
    """
    records: dict[int, TranscriptRecord] = {}
    start = 0
    for index, entry in enumerate(entries):
        if getattr(entry, "role", None) != "assistant":
            continue
        record = decode_transcript_record(getattr(entry, "provider_messages", None))
        if record is None:
            continue
        records[index] = record
        if record.mode == "reset":
            start = index
    covered_input_ids = {
        record.input_entry_id
        for index, record in records.items()
        if index >= start and record.input_entry_id
    }
    position_by_id = {
        getattr(entry, "message_id", None): index for index, entry in enumerate(entries)
    }
    # Follow-ups queued during a turn: user rows between its input row and its
    # record row move to just after the record.
    deferred_after: dict[int, list[int]] = {}
    deferred: set[int] = set()
    for record_index, record in records.items():
        if record_index < start or not record.input_entry_id:
            continue
        input_index = position_by_id.get(record.input_entry_id)
        if input_index is None or input_index >= record_index:
            continue
        for index in range(max(input_index + 1, start), record_index):
            entry = entries[index]
            if (
                index in deferred
                or getattr(entry, "role", None) != "user"
                or getattr(entry, "message_id", None) in covered_input_ids
            ):
                continue
            deferred.add(index)
            deferred_after.setdefault(record_index, []).append(index)

    loaded = LoadedTranscript()

    def _emit(index: int) -> None:
        entry = entries[index]
        role = getattr(entry, "role", None)
        raw_content = getattr(entry, "content", None) or ""
        message_id = getattr(entry, "message_id", None)
        if current_input_id is not None and message_id == current_input_id:
            loaded.input_entry = entry
            return
        if (
            role == "system"
            and summary_marker
            and isinstance(raw_content, str)
            and raw_content.startswith(summary_marker)
        ):
            loaded.summary_markers.append(strip_summary_marker(raw_content))
            return
        if role not in ("user", "assistant"):
            return
        record = records.get(index)
        if record is not None:
            if record.mode == "reset":
                loaded.messages = list(record.messages)
                entry_id = getattr(entry, "id", None)
                loaded.reset_entry_id = entry_id if isinstance(entry_id, int) else None
            else:
                loaded.messages.extend(record.messages)
            loaded.structured_entries += 1
            loaded.last_entry_was_user = False
            for follow_up in deferred_after.get(index, ()):
                _emit_row(follow_up)
            return
        if role == "user" and message_id in covered_input_ids:
            loaded.last_entry_was_user = True
            return
        _emit_row(index)

    def _emit_row(index: int) -> None:
        entry = entries[index]
        role = getattr(entry, "role", None)
        raw_content = getattr(entry, "content", None) or ""
        if current_input_id is not None and getattr(entry, "message_id", None) == current_input_id:
            loaded.input_entry = entry
            return
        if raw_content and role == "user":
            content: Any = unpack_user(raw_content)
        elif raw_content and role == "assistant":
            content = unpack_assistant(raw_content)
        else:
            content = raw_content
        rebuilt = reconstruct_messages_from_entry(
            str(role),
            content,
            getattr(entry, "tool_calls", None),
            getattr(entry, "reasoning_content", None),
        )
        if rebuilt:
            loaded.legacy_entries += 1
            if legacy_migrate is not None:
                rebuilt = legacy_migrate(rebuilt)
            loaded.messages.extend(rebuilt)
        loaded.last_entry_was_user = role == "user"
        loaded.last_user_entry = entry if role == "user" else None

    for index in range(start, len(entries)):
        if index in deferred:
            continue
        _emit(index)
    if current_input_id is not None and loaded.input_entry is None:
        # A current input row that predates a reset is still this turn's input.
        for entry in entries[:start]:
            if getattr(entry, "message_id", None) == current_input_id:
                loaded.input_entry = entry
    return loaded


def legacy_reasoning_flags(config: Any, model_id: str | None) -> tuple[bool, bool]:
    """Return ``(preserve_tool_call_reasoning, preserve_reasoning_content)``.

    Mirrors the reasoning cleanup legacy reloads always had, so migrating an
    old row produces exactly the provider view it produced before.
    """
    try:
        thinking_enabled, _ = config.resolve_thinking(None)
    except Exception:  # noqa: BLE001 - lightweight config doubles
        thinking_enabled = False
    caps = getattr(config, "model_capabilities", None)
    reasoning_format = str(getattr(caps, "reasoning_format", "") or "") if caps else ""
    normalized = (model_id or "").strip().lower()
    direct_v4 = normalized in {"deepseek-v4-flash", "deepseek-v4-pro"}
    deepseek = "deepseek" in normalized
    preserve_reasoning_content = bool(
        direct_v4 or (thinking_enabled and reasoning_format == "deepseek" and deepseek)
    )
    return bool(thinking_enabled), preserve_reasoning_content


def migrate_legacy_messages(
    messages: list[Message],
    *,
    preserve_tool_call_reasoning: bool = False,
    preserve_reasoning_content: bool = False,
) -> list[Message]:
    """One-time, deterministic provider view of legacy-reconstructed rows."""
    from agentos.engine.session_sanitize import (
        project_historical_tool_payloads,
        sanitize_session_messages,
    )
    from agentos.engine.thinking import drop_reasoning

    migrated, _ = sanitize_session_messages(messages)
    migrated, _ = project_historical_tool_payloads(
        migrated,
        preserve_reasoning_content=preserve_reasoning_content,
    )
    migrated, _ = scrub_projected_tool_arguments(migrated)
    return drop_reasoning(
        migrated,
        preserve_tool_call_reasoning=preserve_tool_call_reasoning,
        preserve_reasoning_content=preserve_reasoning_content,
    )


def legacy_migration_for(agent: Any) -> Callable[[list[Message]], list[Message]]:
    config = getattr(agent, "config", None)
    preserve_tool_call_reasoning, preserve_reasoning_content = legacy_reasoning_flags(
        config,
        getattr(config, "model_id", None),
    )

    def _migrate(messages: list[Message]) -> list[Message]:
        return migrate_legacy_messages(
            messages,
            preserve_tool_call_reasoning=preserve_tool_call_reasoning,
            preserve_reasoning_content=preserve_reasoning_content,
        )

    return _migrate


# ---------------------------------------------------------------------------
# Tool-argument projection markers
# ---------------------------------------------------------------------------


def projection_placeholder(tool_name: str, field_name: str) -> str:
    return (
        f"{INVALID_PROVIDER_CONTEXT_PROJECTION_PREFIX}{tool_name}.{field_name}] "
        "provider-only compacted tool argument omitted; regenerate the real "
        "argument instead of copying provider context."
    )


def has_compacted_argument_marker(arguments: dict[str, Any]) -> bool:
    return arguments.get(INVALID_PROVIDER_CONTEXT_ARGUMENTS_KEY) is True or any(
        arguments.get(marker) is True for marker in COMPACTED_TOOL_ARGUMENT_MARKERS
    )


def compacted_arguments_placeholder(tool_name: str) -> dict[str, Any]:
    return {
        INVALID_PROVIDER_CONTEXT_ARGUMENTS_KEY: True,
        "tool": tool_name,
        "reason": "provider_context_omitted",
    }


def has_replay_marker(arguments: dict[str, Any]) -> bool:
    """True when a tool call carries a copied provider-only projection."""
    if has_compacted_argument_marker(arguments):
        return True
    return any(
        isinstance(value, str) and value.startswith(INVALID_PROVIDER_CONTEXT_PROJECTION_PREFIX)
        for value in arguments.values()
    )


def history_argument_projection(
    *,
    tool_name: str,
    tool_use_id: str,
    field_name: str,
    value: str,
    handle: str | None,
    preview_chars: int = 480,
) -> str:
    """Head/tail stand-in for a huge tool-call argument of a finished turn.

    It starts with :data:`HISTORICAL_TOOL_ARGUMENT_PROJECTION_PREFIX`, so a
    model that copies it into a new call is refused (scrubbed to a
    placeholder), never dispatched.
    """
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    head_chars = max(1, preview_chars // 2)
    tail_chars = max(1, preview_chars - head_chars)
    lines = [
        HISTORICAL_TOOL_ARGUMENT_PROJECTION_PREFIX.rstrip("\n"),
        f"tool: {tool_name}",
        f"tool_use_id: {tool_use_id}",
        f"field: {field_name}",
        f"original_chars: {len(value)}",
        f"sha256: {digest}",
    ]
    if handle:
        lines.append(f"tool_result_handle: {handle}")
    lines.extend(
        [
            "reason: argument of an earlier turn, shortened for history.",
            "head:",
            value[:head_chars],
            "...",
            "tail:",
            value[-tail_chars:],
        ]
    )
    return "\n".join(lines)


def scrub_tool_use_arguments(block: ContentBlockToolUse) -> ContentBlockToolUse:
    """Replace projected argument values with non-executable placeholders."""
    if has_compacted_argument_marker(block.input):
        return ContentBlockToolUse(
            id=block.id,
            name=block.name,
            input=compacted_arguments_placeholder(block.name),
            thought_signature=block.thought_signature,
        )
    scrubbed = dict(block.input)
    changed = False
    for key, value in block.input.items():
        if isinstance(value, str) and value.startswith(_PROJECTION_PREFIXES):
            scrubbed[key] = projection_placeholder(block.name, key)
            changed = True
    if not changed:
        return block
    return ContentBlockToolUse(
        id=block.id,
        name=block.name,
        input=scrubbed,
        thought_signature=block.thought_signature,
    )


def scrub_projected_tool_arguments(messages: list[Message]) -> tuple[list[Message], int]:
    """Scrub projected tool arguments out of a message list (legacy rows)."""
    out: list[Message] = []
    scrubbed = 0
    for message in messages:
        if not isinstance(message.content, list):
            out.append(message)
            continue
        blocks: list[Any] = []
        changed = False
        for block in message.content:
            if isinstance(block, ContentBlockToolUse):
                replacement = scrub_tool_use_arguments(block)
                if replacement is not block:
                    scrubbed += 1
                    changed = True
                blocks.append(replacement)
            else:
                blocks.append(block)
        out.append(_with_content(message, blocks) if changed else message)
    return (out if scrubbed else messages), scrubbed


# ---------------------------------------------------------------------------
# Append-time seal
# ---------------------------------------------------------------------------


@dataclass
class SealReport:
    tool_results_shrunk: int = 0
    replay_pairs_stripped: int = 0
    pairing_repairs: int = 0
    stripped_tool_use_ids: list[str] = field(default_factory=list)


def _window_has_tool_blocks(messages: Sequence[Message]) -> bool:
    for message in messages:
        if isinstance(message.content, list) and any(
            isinstance(block, (ContentBlockToolUse, ContentBlockToolResult))
            for block in message.content
        ):
            return True
    return False


def seal_window(
    messages: list[Message],
    start: int,
    *,
    shrink_tool_result: Callable[[ContentBlockToolResult, str], ContentBlockToolResult],
    repair_prompt: str,
) -> SealReport:
    """Normalize ``messages[start:]`` in place, once, before they are first sent.

    * ``tool_result`` blocks go through *shrink_tool_result* (with the tool
      name of their ``tool_use``) so an oversized result is projected before
      any provider sees it;
    * a tool call carrying a copied provider-only projection is removed with
      its result, and a repair prompt keeps the request from ending on an
      assistant message;
    * the new window's ``tool_use``/``tool_result`` pairing is checked (one
      pass over the window only, never the whole history).
    """
    report = SealReport()
    if start >= len(messages):
        return report
    window = messages[start:]

    tool_names: dict[str, str] = {}
    blocked: set[str] = set()
    anchor = messages[start - 1] if start > 0 else None
    for position, message in enumerate([anchor, *window] if anchor is not None else window):
        if not isinstance(message.content, list):
            continue
        for block in message.content:
            if isinstance(block, ContentBlockToolUse):
                tool_names[block.id] = block.name
                # Only calls in the unsent window can be stripped; a sent
                # message is never rewritten.
                in_window = anchor is None or position > 0
                if in_window and has_replay_marker(block.input):
                    blocked.add(block.id)

    sealed: list[Message] = []
    for message in window:
        if not isinstance(message.content, list):
            sealed.append(message)
            continue
        blocks: list[Any] = []
        changed = False
        for block in message.content:
            if isinstance(block, ContentBlockToolUse) and block.id in blocked:
                report.replay_pairs_stripped += 1
                report.stripped_tool_use_ids.append(block.id)
                changed = True
                continue
            if isinstance(block, ContentBlockToolResult):
                if block.tool_use_id in blocked:
                    changed = True
                    continue
                replacement = shrink_tool_result(block, tool_names.get(block.tool_use_id, ""))
                if replacement is not block:
                    report.tool_results_shrunk += 1
                    changed = True
                blocks.append(replacement)
                continue
            blocks.append(block)
        if not changed:
            sealed.append(message)
        elif blocks:
            sealed.append(_with_content(message, blocks))
    if report.replay_pairs_stripped and sealed and sealed[-1].role == "assistant":
        sealed.append(Message(role="user", content=repair_prompt))

    if _window_has_tool_blocks(sealed):
        # Results at the head of the window may answer a call in the last
        # sent message; check them against it, but never rewrite it.
        lead: list[Message] = []
        if (
            anchor is not None
            and anchor.role == "assistant"
            and isinstance(anchor.content, list)
            and any(isinstance(block, ContentBlockToolUse) for block in anchor.content)
        ):
            lead = [anchor]
        candidate = [*lead, *sealed]
        repaired = repair_tool_pairing(candidate)
        if repaired is not candidate:
            if lead and (not repaired or repaired[0] is not lead[0]):
                log.warning("transcript.seal_pairing_unrepairable", start=start)
            else:
                report.pairing_repairs += 1
                sealed = repaired[len(lead) :]

    messages[start:] = sealed
    return report


# ---------------------------------------------------------------------------
# Volatile context
# ---------------------------------------------------------------------------


def message_text(message: Message) -> str:
    if isinstance(message.content, str):
        return message.content
    return "".join(block.text for block in message.content if isinstance(block, ContentBlockText))


def latest_context_block_present(
    history: Sequence[Message],
    *,
    header: str,
    block: str,
) -> bool:
    """True when the most recent *header* block in *history* equals *block*."""
    for message in reversed(history):
        if message.role != "user":
            continue
        text = message_text(message)
        if header not in text:
            continue
        return block in text
    return False


# ``## <title>`` sections the dynamic prompt suffix is built from (extra
# context keys, pipeline blocks). Only these titles split a request-context
# block, so a markdown heading inside recalled text never does.
REQUEST_CONTEXT_SECTION_TITLES: frozenset[str] = frozenset(
    {
        "Memory Context",
        "Internal Event Mode",
        "Subagent Task Protocol",
        "Project Knowledge",
        "Router Control",
        "Channel Rendering",
        "Plan Mode (Active)",
        "Skills",
        "Recent Notes",
        "Workspace Files (injected)",
    }
)
_REQUEST_CONTEXT_INTRO = (
    "This request-scoped context is not a user request and is not transcript history.\n"
    "Use it only when it is relevant to the current user request."
)
_SECTION_GONE = "[No longer applies from this turn on.]"
_SECTION_LINE = re.compile(r"(?m)^## (.+?)[ \t]*$")


def split_context_sections(text: str) -> list[tuple[str, str]]:
    """Split request context into ``(title, section text)`` pairs.

    Text before the first known ``## <title>`` (durable summaries, subagent
    grounding) is one section with title ``""``.
    """
    starts = [
        match
        for match in _SECTION_LINE.finditer(text)
        if match.group(1) in REQUEST_CONTEXT_SECTION_TITLES
        and (match.start() == 0 or text[max(0, match.start() - 2) : match.start()] == "\n\n")
    ]
    sections: list[tuple[str, str]] = []
    lead = text[: starts[0].start()] if starts else text
    if lead.strip():
        sections.append(("", lead.strip()))
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        body = text[match.start() : end].strip()
        if body:
            sections.append((match.group(1), body))
    return sections


def _request_context_sections_in(message: Message) -> list[tuple[str, str]]:
    text = message_text(message)
    start = text.find(REQUEST_CONTEXT_HEADER)
    if start < 0:
        return []
    end = text.find(f"\n\n{RUNTIME_CONTEXT_HEADER}", start)
    block = text[start : end if end >= 0 else len(text)]
    block = block[len(REQUEST_CONTEXT_HEADER) :].lstrip("\n")
    if block.startswith(_REQUEST_CONTEXT_INTRO):
        block = block[len(_REQUEST_CONTEXT_INTRO) :].lstrip("\n")
    return split_context_sections(block)


def request_context_delta(history: Sequence[Message], request_context: str | None) -> str | None:
    """The request-context block this turn must carry, section by section.

    Every turn's block is persisted with that turn (append-only), so only the
    ``## <title>`` sections that changed since their latest copy in *history*
    are sent again; a section present before and gone now gets a one-line
    "no longer applies" marker. ``None`` when nothing changed.
    """
    current = split_context_sections(request_context.strip()) if request_context else []
    latest: dict[str, str] = {}
    for message in reversed(history):
        if message.role != "user":
            continue
        for title, body in _request_context_sections_in(message):
            latest.setdefault(title, body)
    current_titles = {title for title, _ in current}
    changed = [body for title, body in current if latest.get(title) != body]
    for title, body in latest.items():
        if title and title not in current_titles and not body.endswith(_SECTION_GONE):
            changed.append(f"## {title}\n\n{_SECTION_GONE}")
    if not changed:
        return None
    return f"{REQUEST_CONTEXT_HEADER}\n{_REQUEST_CONTEXT_INTRO}\n\n" + "\n\n".join(changed)


def append_context_blocks(message: Message, blocks: Sequence[str]) -> Message:
    """Return *message* with *blocks* appended as trailing text."""
    if not blocks:
        return message
    if isinstance(message.content, str):
        suffix = "\n\n".join(blocks)
        content = f"{message.content}\n\n{suffix}" if message.content else suffix
        return _with_content(message, content)
    return _with_content(
        message,
        [*message.content, *(ContentBlockText(text=f"\n\n{block}") for block in blocks)],
    )


# ---------------------------------------------------------------------------
# Request size estimate (incremental)
# ---------------------------------------------------------------------------


class MessageSizeCache:
    """Memoized JSON size of sealed messages.

    Sealed messages are immutable, so their serialized size is computed once
    per turn instead of once per provider request (the old estimate dumped
    the whole request every call, which made a long tool loop quadratic).
    Holds a weak reference per entry, never the message itself.
    """

    def __init__(self, jsonable: Callable[[Any], Any]) -> None:
        self._jsonable = jsonable
        self._sizes: dict[int, tuple[weakref.ref[Message], int]] = {}

    def size(self, message: Message) -> int:
        cached = self._sizes.get(id(message))
        if cached is not None and cached[0]() is message:
            return cached[1]
        chars = len(
            json.dumps(self._jsonable(message), ensure_ascii=False, sort_keys=True, default=str)
        )
        self._sizes[id(message)] = (weakref.ref(message), chars)
        return chars

    def messages_chars(self, messages: Sequence[Message]) -> int:
        """Length of ``json.dumps(list)`` for *messages* (default separators)."""
        if not messages:
            return 2
        return 2 + sum(self.size(message) for message in messages) + 2 * (len(messages) - 1)

    def retain_only(self, messages: Sequence[Message]) -> None:
        live = {id(message) for message in messages}
        for key in [key for key in self._sizes if key not in live]:
            del self._sizes[key]


def transcript_record_of(
    agent: Any,
    *,
    artifacts: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """The persistable record of *agent*'s last turn, or ``None``.

    *artifacts* published this turn are named on the turn's final assistant
    message (``[generated artifact omitted: name (mime)]``), as the legacy
    reload did, so later turns know which files the user received. That
    message was never sent in a request, so this is still "before first send".

    Tolerates agent doubles: anything but a ``dict`` from the agent is ignored
    and the turn is persisted as flat segments only (legacy reload).
    """
    getter = getattr(agent, "transcript_record", None)
    if not callable(getter):
        return None
    try:
        record = getter()
    except Exception:  # noqa: BLE001 - persistence of the record is best-effort
        log.warning("transcript.record_unavailable", exc_info=True)
        return None
    if not isinstance(record, dict):
        return None
    result: dict[str, Any] = record
    if artifacts:
        result = _with_artifact_markers(result, artifacts)
    return result


def _with_artifact_markers(
    record: dict[str, Any],
    artifacts: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    from agentos.artifacts import artifact_marker

    markers = [artifact_marker(item) for item in artifacts if isinstance(item, dict)]
    messages = record.get("messages")
    if not markers or not isinstance(messages, list):
        return record
    marker_text = "\n".join(markers)
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str):
            if marker_text in content:
                return record
            new_content: Any = f"{content}\n{marker_text}" if content else marker_text
        elif isinstance(content, list):
            existing = "\n".join(
                str(block.get("text") or "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
            if marker_text in existing:
                return record
            new_content = [*content, {"type": "text", "text": marker_text}]
        else:
            return record
        updated = list(messages)
        updated[index] = {**message, "content": new_content}
        return {**record, "messages": updated}
    return record
