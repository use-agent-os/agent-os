"""RPC handlers for the chat domain — wired to sessions engine bridge."""

from __future__ import annotations

import json
from typing import Any, cast
from urllib.parse import quote

import structlog

from agentos.chat.conversation import ChatSendRequest, sessions_send_params
from agentos.chat.history import transcript_entries_to_chat_messages
from agentos.chat.source import chat_source_metadata
from agentos.engine.steering import is_steer_segment
from agentos.engine.turn_progress import hide_live_turn_progress
from agentos.gateway.access import CONTROL_AND_CHANNEL, CONTROL_ONLY
from agentos.gateway.config import GatewayConfig
from agentos.gateway.context_overflow import apply_context_overflow_policy
from agentos.gateway.idempotency import get_idempotency_cache, idempotency_key, request_fingerprint
from agentos.gateway.protocol import ErrorCode, ErrorDetailCode
from agentos.gateway.rpc import RpcContext, RpcHandlerError, RpcUnavailableError, get_dispatcher
from agentos.gateway.session_events import build_sessions_changed_payload
from agentos.session.compaction import (
    build_compaction_config_from_provider,
    effective_compaction_model,
    resolve_compaction_provider,
)
from agentos.session.keys import build_webchat_key, canonicalize_session_key, parse_agent_id

_d = get_dispatcher()
log = structlog.get_logger(__name__)

_WEBCHAT_SESSION_KEY = build_webchat_key()
_CHAT_HISTORY_DEFAULT_LIMIT = 50
_CHAT_HISTORY_MAX_LIMIT = 200
#: One ``chat.history`` answer stays under this many bytes of messages, so a
#: single huge tool result cannot blow the WebSocket frame; older rows that do
#: not fit are left for the next ``before`` page.
CHAT_HISTORY_RESPONSE_BUDGET_BYTES = 6 * 1024 * 1024
#: A message larger than this is sent as a placeholder (see ``chat.entry``).
CHAT_HISTORY_MESSAGE_BUDGET_BYTES = 128 * 1024
#: ``delta_cursor`` answers: at most this many rows / bytes after the cursor.
CHAT_HISTORY_DELTA_MAX_ROWS = 200
CHAT_HISTORY_DELTA_MAX_BYTES = 1024 * 1024
#: Leading text a placeholder keeps so the row still reads as something.
_PLACEHOLDER_PREVIEW_CHARS = 2_000
#: ``chat.entry`` refuses a row that would not fit a frame on its own.
_CHAT_ENTRY_MAX_BYTES = 20 * 1024 * 1024
#: Segment keys that identify a tool call; a placeholder keeps only these.
_SEGMENT_IDENTITY_KEYS = ("type", "name", "tool_name", "tool_use_id", "id", "is_error", "status")


def _canonical_webchat_session_key(value: object = None) -> str:
    """Map legacy WebChat defaults onto the canonical WebChat session."""
    raw = str(value or "").strip()
    if not raw or raw in {"default", "webchat:default", "unknown"}:
        return _WEBCHAT_SESSION_KEY
    if raw.startswith("sess-"):
        return f"agent:main:webchat:{raw[len('sess-') :]}"
    return canonicalize_session_key(raw)


def _require_chat_session_manager(ctx: RpcContext):
    if ctx.session_manager is None:
        raise RpcUnavailableError("Chat session manager not available")
    return ctx.session_manager


def _normalize_chat_history_limit(value: object) -> int:
    try:
        if isinstance(value, int):
            limit = value
        elif isinstance(value, str):
            limit = int(value)
        else:
            limit = _CHAT_HISTORY_DEFAULT_LIMIT
    except (TypeError, ValueError):
        limit = _CHAT_HISTORY_DEFAULT_LIMIT
    return max(1, min(limit, _CHAT_HISTORY_MAX_LIMIT))


def _is_webchat_session_key(key: str) -> bool:
    parts = str(key or "").split(":")
    return (
        len(parts) == 4
        and parts[0] == "agent"
        and bool(parts[1])
        and parts[2] == "webchat"
        and bool(parts[3])
    )


def _is_cron_session_key(key: str) -> bool:
    """Cron run keys — ``cron:<job>:run:<hex>`` (isolated) or ``cron:<job>``.

    These routinely name a session that does not exist: ``script_run`` jobs never
    create one, and isolated per-run sessions are reaped after 24h. Opening one
    should read as an empty transcript, not a failed request.
    """
    parts = str(key or "").split(":")
    return len(parts) >= 2 and parts[0] == "cron" and bool(parts[1])


def _empty_chat_history_payload(limit: int) -> dict[str, Any]:
    return {
        "messages": [],
        "has_more": False,
        "oldest_cursor": None,
        "newest_cursor": None,
        "history_scope": "complete",
        "loaded_count": 0,
        "page_size": limit,
        "canonical_available": False,
        "compaction_summaries": [],
    }


def _chat_history_bool(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if not cleaned:
            return default
        return cleaned not in {"0", "false", "no", "off"}
    return bool(value)


def _chat_history_cursor(entry: object | None) -> str | None:
    if entry is None:
        return None
    created_at = getattr(entry, "created_at", "")
    stable_id = getattr(entry, "id", None) or getattr(entry, "message_id", "")
    if created_at in {None, ""} or stable_id in {None, ""}:
        return None
    return f"{created_at}|{stable_id}"


def _chat_history_cursor_index(entries: list[object], cursor: object) -> int | None:
    raw = str(cursor or "").strip()
    if not raw:
        return None
    for idx, entry in enumerate(entries):
        if _chat_history_cursor(entry) == raw:
            return idx
    return None


def _chat_history_page(
    entries: list[object],
    *,
    limit: int,
    before: object = None,
    after: object = None,
) -> tuple[list[object], bool]:
    if not entries:
        return [], False
    before_idx = _chat_history_cursor_index(entries, before)
    if before_idx is not None:
        end = before_idx
        start = max(0, end - limit)
        return entries[start:end], start > 0

    after_idx = _chat_history_cursor_index(entries, after)
    if after_idx is not None:
        start = min(len(entries), after_idx + 1)
        end = min(len(entries), start + limit)
        return entries[start:end], end < len(entries)

    if len(entries) <= limit:
        return entries, False
    return entries[-limit:], True


def _message_size(message: dict[str, Any]) -> int:
    return len(json.dumps(message, ensure_ascii=False, separators=(",", ":"), default=str).encode())


def _placeholder_message(message: dict[str, Any], *, size: int) -> dict[str, Any]:
    """An oversized row: identity, tool identity and size, not the body.

    ``entry_id`` fetches the full row through ``chat.entry``. A short preview
    of the text keeps the row readable in a client that knows nothing of
    placeholders.
    """
    placeholder = {
        key: value
        for key, value in message.items()
        if key not in {"text", "tool_calls", "attachments", "artifacts", "usage"}
    }
    raw_text = message.get("text")
    text = raw_text if isinstance(raw_text, str) else ""
    placeholder["text"] = text[:_PLACEHOLDER_PREVIEW_CHARS]
    placeholder["placeholder"] = True
    placeholder["truncated"] = True
    placeholder["byte_size"] = size
    placeholder["entry_id"] = message.get("transcript_id") or message.get("message_id")
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        placeholder["tool_calls"] = [
            {key: segment[key] for key in _SEGMENT_IDENTITY_KEYS if key in segment}
            for segment in tool_calls
            if isinstance(segment, dict)
        ]
    return placeholder


def _budgeted_messages(
    entries: list[object],
    *,
    response_budget: int,
    message_budget: int,
    newest_first: bool,
    max_rows: int | None = None,
) -> tuple[list[object], list[dict[str, Any]], bool]:
    """Project ``entries`` to chat messages within the byte budgets.

    Walks newest-first for a history page (the rows a reader sees first are
    kept; older ones wait for the next ``before`` page) and oldest-first for a
    delta (rows after the cursor stay contiguous). Returns the entries kept,
    their messages in chronological order, and whether rows were left out.
    """
    order = list(reversed(entries)) if newest_first else list(entries)
    kept: list[tuple[object, dict[str, Any] | None]] = []
    total = 0
    cut = False
    for entry in order:
        if max_rows is not None and len(kept) >= max_rows:
            cut = True
            break
        projected = transcript_entries_to_chat_messages([entry], limit=None)
        message = projected[0] if projected else None
        size = 0
        if message is not None:
            size = _message_size(message)
            if size > message_budget:
                message = _placeholder_message(message, size=size)
                size = _message_size(message)
        if kept and total + size > response_budget:
            cut = True
            break
        total += size
        kept.append((entry, message))
    if newest_first:
        kept.reverse()
    return (
        [entry for entry, _ in kept],
        [message for _, message in kept if message is not None],
        cut,
    )


# Fields of a split assistant message that belong to the turn as a whole:
# only its last part (which keeps the row's own ids) carries them.
_TURN_LEVEL_MESSAGE_KEYS = frozenset(
    {
        "transcript_id",
        "has_thinking",
        "attachments",
        "artifacts",
        "usage",
        "model",
        "input",
        "output",
        "input_tokens",
        "output_tokens",
        "cost_usd",
    }
)


def _steer_marker_ids(message: dict[str, Any]) -> list[str | None] | None:
    """The steer markers of an assistant message, in order; ``None`` if it has none."""
    if message.get("role") != "assistant" or message.get("placeholder"):
        return None
    segments = message.get("tool_calls")
    if not isinstance(segments, list):
        return None
    ids: list[str | None] = []
    for segment in segments:
        if is_steer_segment(segment):
            raw = segment.get("message_id")
            ids.append(raw if isinstance(raw, str) and raw else None)
    return ids or None


def _segments_text(segments: list[Any]) -> str:
    return "".join(
        segment["text"]
        for segment in segments
        if isinstance(segment, dict)
        and segment.get("type") == "text"
        and isinstance(segment.get("text"), str)
    )


def _chunk_has_content(segments: list[Any]) -> bool:
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        if segment.get("type") != "text" or str(segment.get("text") or "").strip():
            return True
    return False


def _split_steered_message(
    message: dict[str, Any], steered: list[dict[str, Any] | None]
) -> list[dict[str, Any]]:
    """*message* cut at its steer markers, with each steered user message between."""
    chunks: list[list[Any]] = [[]]
    for segment in message.get("tool_calls") or []:
        if is_steer_segment(segment):
            chunks.append([])
        else:
            chunks[-1].append(segment)
    # (chunk, user message that follows it)
    layout: list[tuple[list[Any] | None, dict[str, Any] | None]] = []
    for index, segments in enumerate(chunks):
        follower = steered[index] if index < len(steered) else None
        layout.append((segments if _chunk_has_content(segments) else None, follower))
    emitted = [index for index, (kept, _) in enumerate(layout) if kept is not None]
    last_part = emitted[-1] if emitted else len(layout) - 1
    if not emitted:
        layout[last_part] = (chunks[last_part], layout[last_part][1])

    message_id = message.get("message_id") or message.get("id")
    raw_text = message.get("text")
    remainder: str = raw_text if isinstance(raw_text, str) else ""
    out: list[dict[str, Any]] = []
    part_number = 0
    for index, (chunk, follower) in enumerate(layout):
        if chunk is not None:
            part_number += 1
            part_text = _segments_text(chunk).strip()
            if index == last_part:
                part = {**message, "tool_calls": chunk}
                # The row's text may carry more than its segments (a notice
                # appended at the end): keep whatever the earlier parts did
                # not show.
                part["text"] = remainder.strip() if remainder.strip() else part_text
            else:
                part = {
                    key: value
                    for key, value in message.items()
                    if key not in _TURN_LEVEL_MESSAGE_KEYS
                }
                part_id = f"{message_id}#part{part_number}" if message_id else None
                part.update({"id": part_id, "message_id": part_id, "split_from": message_id})
                part["tool_calls"] = chunk
                part["text"] = part_text
                if follower is not None and follower.get("timestamp") is not None:
                    part["timestamp"] = follower.get("timestamp")
                found = remainder.find(part_text) if part_text else -1
                if found >= 0:
                    remainder = remainder[found + len(part_text) :]
            out.append(part)
        if follower is not None:
            out.append(follower)
    return out


def _split_steered_turns(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Show a turn that took steered messages in the order things happened.

    A message steered into a running turn (``agentos.engine.steering``) is
    its own user row, written when the turn took it, so it sorts before the
    turn's single assistant row, which also holds the work done before the
    message. That row's segments mark the point (``steer`` segments): the
    assistant message is cut there and the steered user message (when it is
    on this page) moves between the parts -- the work before it, the
    message, the work after it. The last part keeps the row's ids, usage and
    artifacts; earlier parts get ``<message_id>#partN`` ids and
    ``split_from``. Messages without a marker are returned as they are.
    """
    marked = {
        index: ids
        for index, message in enumerate(messages)
        if (ids := _steer_marker_ids(message)) is not None
    }
    if not marked:
        return messages
    user_index = {
        message.get("message_id"): index
        for index, message in enumerate(messages)
        if message.get("role") == "user" and message.get("message_id")
    }
    moved: set[int] = set()
    replacements: dict[int, list[dict[str, Any]]] = {}
    for index, ids in marked.items():
        steered: list[dict[str, Any] | None] = []
        for steered_id in ids:
            found = user_index.get(steered_id) if steered_id else None
            if found is None or found in moved:
                steered.append(None)
                continue
            moved.add(found)
            steered.append(messages[found])
        replacements[index] = _split_steered_message(messages[index], steered)
    out: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if index in moved:
            continue
        out.extend(replacements.get(index, [message]))
    return out


def _steered_ids_outside(messages: list[dict[str, Any]]) -> set[str]:
    """Steered row ids *messages* name but do not contain."""
    named = {
        steered_id
        for message in messages
        for steered_id in (_steer_marker_ids(message) or [])
        if steered_id
    }
    present = {message.get("message_id") for message in messages}
    return named - present


def _session_summary_to_chat_payload(summary: object) -> dict[str, Any]:
    return {
        "id": getattr(summary, "id", None),
        "compaction_id": getattr(summary, "compaction_id", None),
        "compaction_index": getattr(summary, "compaction_index", None),
        "trigger_reason": getattr(summary, "trigger_reason", None),
        "summary_text": getattr(summary, "summary_text", "") or "",
        "summary_format": getattr(summary, "summary_format", "") or "",
        "coverage_status": getattr(summary, "coverage_status", "") or "",
        "removed_count": getattr(summary, "removed_count", None),
        "kept_count": getattr(summary, "kept_count", None),
        "covered_through_id": getattr(summary, "covered_through_id", None),
        "created_at": getattr(summary, "created_at", None),
    }


def _annotate_transcript_attachment_downloads(
    messages: list[dict[str, Any]],
    *,
    session_key: str,
) -> list[dict[str, Any]]:
    session_qs = quote(session_key, safe="")
    for msg in messages:
        attachments = msg.get("attachments")
        if not isinstance(attachments, list):
            continue
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            sha = attachment.get("sha256_ref")
            if not isinstance(sha, str) or not sha:
                continue
            if attachment.get("download_url"):
                continue
            name = str(attachment.get("name") or "attachment")
            mime = str(attachment.get("mime") or attachment.get("type") or "")
            attachment["download_url"] = (
                f"/api/v1/attachments/{quote(sha, safe='')}?sessionKey={session_qs}"
                f"&name={quote(name, safe='')}&mime={quote(mime, safe='')}"
            )
    return messages


async def _chat_history_transcript(
    mgr: object,
    session_key: str,
    *,
    include_canonical: bool,
) -> tuple[list[object], bool]:
    if include_canonical:
        getter = getattr(mgr, "get_canonical_transcript", None)
        if callable(getter):
            try:
                return list(await getter(session_key)), True
            except Exception:  # noqa: BLE001 - fall back to active transcript
                pass
    transcript_getter = getattr(mgr, "get_transcript", None)
    if not callable(transcript_getter):
        return [], False
    transcript = await transcript_getter(session_key)
    return list(transcript or []), False


async def _chat_history_summaries(
    mgr: object,
    session_key: str,
    *,
    include_summaries: bool,
) -> list[dict[str, Any]]:
    if not include_summaries:
        return []
    getter = getattr(mgr, "get_summaries", None)
    if not callable(getter):
        return []
    try:
        summaries = await getter(session_key)
    except Exception:  # noqa: BLE001 - summaries are optional display metadata
        return []
    return [_session_summary_to_chat_payload(summary) for summary in summaries or []]


async def _build_context_overflow_compaction_config(ctx: RpcContext, session_key: str):
    session = None
    storage = getattr(getattr(ctx, "session_manager", None), "_storage", None)
    if storage is not None:
        try:
            session = await storage.get_session(session_key)
        except Exception:  # noqa: BLE001
            session = None
    model = effective_compaction_model(session)
    return build_compaction_config_from_provider(
        resolve_compaction_provider(getattr(ctx, "provider_selector", None), model),
        model_override=model,
        compaction_config=getattr(getattr(ctx, "config", None), "compaction", None),
    )


async def _enforce_context_overflow(
    ctx: RpcContext,
    session_key: str,
    message: str,
) -> dict | None:
    """Apply the configured context-overflow policy before a turn runs.

    Returns a stable error envelope when the policy is REFUSE and the
    payload exceeds the budget; returns ``None`` for every other path
    (policy consults pass, HARD_TRUNCATE dropped some history in place,
    AUTO_SUMMARIZE kicked off a compaction). The caller short-circuits
    on a non-None return.
    """

    config = ctx.config if isinstance(ctx.config, GatewayConfig) else GatewayConfig()

    transcript: list = []
    if ctx.session_manager is not None:
        try:
            transcript = list(await ctx.session_manager.get_transcript(session_key))
        except Exception:  # noqa: BLE001 — missing transcript just means "no history"
            transcript = []

    # Per-session context-budget overrides are independent from runtime/request
    # timeout resolution, which happens in TurnRunner.
    # A session-scoped context_budget_tokens override is supported via
    # ctx.session_manager.get_config(session_key) if present.
    budget_override = None
    policy_override = None
    if ctx.session_manager is not None and hasattr(ctx.session_manager, "get_session_config"):
        try:
            session_cfg = await ctx.session_manager.get_session_config(session_key)
            if session_cfg is not None:
                budget_override = getattr(session_cfg, "context_budget_tokens", None)
                policy_override = getattr(session_cfg, "context_overflow_policy", None)
        except Exception:  # noqa: BLE001
            pass

    outcome = await apply_context_overflow_policy(
        config=config,
        message=message,
        transcript=transcript,
        session_key=session_key,
        session_manager=ctx.session_manager,
        compaction_config=await _build_context_overflow_compaction_config(ctx, session_key),
        compaction_marker=getattr(ctx, "turn_runner", None),
        policy_override=policy_override,
        budget_override=budget_override,
    )

    if outcome.refusal is not None:
        log.warning(
            "chat_send.context_overflow_refused",
            session_key=session_key,
            estimated_tokens=outcome.estimated_tokens,
            budget_tokens=outcome.budget_tokens,
        )
        return outcome.refusal

    if outcome.compacted_this_turn:
        marker = getattr(ctx, "turn_runner", None)
        mark = getattr(marker, "mark_compacted_this_turn", None)
        if callable(mark):
            mark(session_key)

    return None


def _schedule_session_title(ctx: RpcContext, session_key: str, message: Any, extra: dict) -> None:
    """Name a placeholder-titled session from this message, off the turn path."""
    sessions_cfg = getattr(getattr(ctx, "config", None), "sessions", None)
    if sessions_cfg is not None and not getattr(sessions_cfg, "auto_title", True):
        return
    if not isinstance(message, str):
        return
    from agentos.gateway.rpc_sessions import _emit_to_subscribers
    from agentos.gateway.session_titler import fast_model_hint, titler_for

    async def broadcast(key: str, state: dict) -> None:
        await _emit_to_subscribers(
            ctx, key, "sessions.changed", build_sessions_changed_payload(key, "renamed", **state)
        )

    timeout = float(getattr(sessions_cfg, "auto_title_timeout_seconds", 30.0) or 30.0)
    titler = titler_for(
        ctx.session_manager,
        timeout=timeout,
        hint=fast_model_hint(getattr(ctx, "config", None)),
    )
    run_kind = extra.get("runKind", extra.get("run_kind"))
    titler.maybe_schedule(
        session_key,
        message,
        run_kind=str(run_kind) if run_kind is not None else None,
        broadcast=broadcast,
    )


@_d.method("chat.send")
async def _handle_chat_send(params: dict | None, ctx: RpcContext) -> dict:
    """Send a chat message.

    ``idempotency_key`` (a client-minted id per message) makes a retry safe:
    within the dedupe window, and after a restart through the key recorded on
    the turn's task, the same key returns the first answer and starts nothing.
    """
    if not isinstance(params, dict) or "message" not in params:
        raise ValueError("params.message is required")
    key = idempotency_key(params)
    if key is None or ctx.session_manager is None:
        return await _chat_send(params, ctx, idempotency=None)
    fingerprint = request_fingerprint(params)
    from agentos.gateway.rpc_sessions import _task_still_active

    return await get_idempotency_cache().run(
        "chat.send",
        key,
        fingerprint,
        lambda: _chat_send(params, ctx, idempotency=(key, fingerprint)),
        pinned=_task_still_active(ctx),
    )


async def _chat_send(
    params: dict[str, Any],
    ctx: RpcContext,
    *,
    idempotency: tuple[str, str] | None,
) -> dict:
    message = params["message"]
    session_key = _canonical_webchat_session_key(params.get("sessionKey"))
    agent_id = parse_agent_id(session_key)

    # Fresh-WebUI / smoke path: when no session manager is wired (webui
    # simulator, dispatcher-only boot), instant-accept without kicking off a
    # turn. This matches the roundtrip the WebUI observes on first paint
    # before the sessions engine is attached.
    if ctx.session_manager is None:
        return {"ok": True, "sessionKey": session_key, "instant_accept": True}

    mgr = _require_chat_session_manager(ctx)
    intent = params.get("intent")

    # WebChat must accept the turn even when existing history is oversized.
    # Context shaping happens inside TurnRunner so it can produce a request-scoped
    # sendable view instead of making the RPC layer a terminal overflow gate.

    try:
        if intent != "new_chat":
            # Ensure session exists — auto-create if needed
            try:
                # No placeholder name: the session is titled from this very
                # message a moment later (session_titler), and until then the
                # short id is more honest than a client label — the desktop
                # app and channel bridges send here too, not only WebChat.
                await mgr.get_or_create(
                    session_key=session_key,
                    agent_id=agent_id,
                )
            except Exception as exc:
                raise RpcUnavailableError(f"Failed to initialize chat session: {exc}") from exc

        from agentos.gateway.rpc_sessions import _handle_sessions_send, persisted_send_answer

        if idempotency is not None:
            persisted = await persisted_send_answer(ctx, session_key, *idempotency)
            if persisted is not None:
                return {"ok": True, "sessionKey": session_key, **persisted}

        incoming_source = params.get("_source")
        if not isinstance(incoming_source, dict):
            incoming_source = {}

        elevated_hint = incoming_source.get("elevated")
        attachments = params.get("attachments")
        extra: dict = {}
        for source_key, target_key in (
            ("noMemoryCapture", "noMemoryCapture"),
            ("no_memory_capture", "no_memory_capture"),
            ("inputProvenance", "inputProvenance"),
            ("input_provenance", "input_provenance"),
            ("inputProvenanceKind", "inputProvenanceKind"),
            ("input_provenance_kind", "input_provenance_kind"),
            ("provenance_kind", "provenance_kind"),
            ("runKind", "runKind"),
            ("run_kind", "run_kind"),
            # ``steer`` (join the running turn), ``followup``, ``interrupt``,
            # ``collect``; unset uses the session's queue mode.
            ("queueMode", "queueMode"),
            ("queue_mode", "queue_mode"),
        ):
            if source_key in params:
                extra[target_key] = params[source_key]
        send_params = sessions_send_params(
            ChatSendRequest(
                session_key=session_key,
                message=message,
                attachments=attachments if isinstance(attachments, list) else [],
                display_text=params.get("displayText")
                if attachments and "displayText" in params
                else None,
                intent=cast(str, intent) if intent is not None else None,
                extra=extra,
            ),
            chat_source_metadata(
                caller_kind="web",
                channel_kind="webchat",
                channel_id=f"webchat:{session_key}",
                sender_id=ctx.conn_id or "control",
                source_kind="webui",
                source_name="WebChat",
                elevated=elevated_hint if isinstance(elevated_hint, str) else None,
            ),
        )
        if idempotency is not None:
            # Deduplicated here already; sessions.send only records the key on
            # the turn's task (so a retry after a restart finds the turn).
            send_params["_idempotency"] = {"key": idempotency[0], "fingerprint": idempotency[1]}
        result = await _handle_sessions_send(send_params, ctx)
        _schedule_session_title(ctx, session_key, message, extra)
        return {"ok": True, "sessionKey": session_key, **result}
    except Exception:
        marker = getattr(ctx, "turn_runner", None)
        clear = getattr(marker, "clear_compacted_this_turn", None)
        if callable(clear):
            clear(session_key)
        raise


@_d.method("chat.suggestNext")
async def _handle_chat_suggest_next(params: dict | None, ctx: RpcContext) -> dict:
    """The user's likely next prompt, for the chat input's ghost text.

    Clients call this after a turn ends. ``{"text": null}`` means "show
    nothing": the feature is off, the turn did not finish with a reply, or
    the model had nothing useful to say.
    """
    raw_params = params if isinstance(params, dict) else {}
    sessions_cfg = getattr(getattr(ctx, "config", None), "sessions", None)
    if sessions_cfg is not None and not getattr(sessions_cfg, "suggest_next", True):
        return {"text": None}
    if ctx.session_manager is None:
        return {"text": None}
    from agentos.gateway.session_suggester import TASK, suggester_for
    from agentos.gateway.session_titler import fast_model_hint

    session_key = _canonical_webchat_session_key(raw_params.get("sessionKey"))
    timeout = float(getattr(sessions_cfg, "suggest_next_timeout_seconds", 15.0) or 15.0)
    suggester = suggester_for(
        ctx.session_manager,
        timeout=timeout,
        hint=fast_model_hint(getattr(ctx, "config", None), task=TASK),
    )
    return {"text": await suggester.suggest(session_key)}


@_d.method("chat.abort")
async def _handle_chat_abort(params: dict | None, ctx: RpcContext) -> dict:
    """Stop the session's run; ``taskId`` withdraws one queued message instead.

    ``taskId`` is the id ``chat.send`` answered with (also listed in
    ``chat.history.pending_inputs``). A queued one is withdrawn
    (``withdrawn: true``), a running one is stopped alone, and one a running
    turn already took answers ``delivered: true``.
    """
    raw_params = params or {}
    session_key = _canonical_webchat_session_key(raw_params.get("sessionKey"))
    # Fresh-WebUI / smoke path: abort always returns an ok envelope keyed by
    # sessionKey, regardless of whether a live task exists to cancel.
    if ctx.session_manager is None:
        return {"ok": True, "sessionKey": session_key, "aborted": False}
    _require_chat_session_manager(ctx)
    from agentos.gateway.rpc_sessions import _handle_sessions_abort

    abort_params: dict[str, Any] = {
        "key": session_key,
        "source": raw_params.get("source") or "webui_abort",
    }
    task_id = raw_params.get("taskId", raw_params.get("task_id"))
    if isinstance(task_id, str) and task_id:
        abort_params["taskId"] = task_id
    result = await _handle_sessions_abort(abort_params, ctx)
    return {"sessionKey": session_key, **result}


def _pending_inputs(ctx: RpcContext, session_key: str) -> list[dict[str, Any]]:
    """The session's queued messages (``TaskRuntime.pending_inputs``)."""
    runtime = getattr(ctx, "task_runtime", None)
    probe = getattr(runtime, "pending_inputs", None)
    if not callable(probe):
        return []
    try:
        return list(probe(session_key))
    except Exception:  # noqa: BLE001 - history must render without the queue
        log.warning("chat.history.pending_inputs_failed", session_key=session_key, exc_info=True)
        return []


def _control_ui_show_thinking(ctx: RpcContext) -> bool:
    control_ui_cfg = getattr(getattr(ctx, "config", None), "control_ui", None)
    return bool(getattr(control_ui_cfg, "show_thinking", True))


@_d.method("chat.thinking", CONTROL_ONLY)
async def _handle_chat_thinking(params: dict | None, ctx: RpcContext) -> dict:
    """Serve one message's reasoning on demand (WebUI fetch-on-expand).

    Deliberately CONTROL_ONLY: reasoning never flows to channel surfaces.
    """
    raw_params = params or {}
    session_key = _canonical_webchat_session_key(raw_params.get("sessionKey"))
    message_id = raw_params.get("messageId") or raw_params.get("message_id")
    if not message_id:
        raise ValueError("params.messageId is required")
    payload: dict[str, Any] = {
        "sessionKey": session_key,
        "messageId": message_id,
        "reasoning": None,
    }
    if not _control_ui_show_thinking(ctx) or ctx.session_manager is None:
        return payload
    mgr = _require_chat_session_manager(ctx)
    try:
        transcript, _ = await _chat_history_transcript(
            mgr,
            session_key,
            include_canonical=True,
        )
    except KeyError:
        return payload
    for entry in reversed(transcript):
        if str(getattr(entry, "message_id", "")) == str(message_id):
            reasoning = getattr(entry, "reasoning_content", None)
            payload["reasoning"] = reasoning or None
            break
    return payload


@_d.method("chat.history", CONTROL_AND_CHANNEL)
async def _handle_chat_history(params: dict | None, ctx: RpcContext) -> dict:
    """A page of a session's transcript, display-ready and byte-bounded.

    ``delta_cursor`` (a ``newest_cursor`` from an earlier answer) asks for
    only the rows after it: ``kind: "delta"``, or ``kind: "reset"`` with the
    latest page when the cursor no longer names a row. Messages over
    ``CHAT_HISTORY_MESSAGE_BUDGET_BYTES`` come back as placeholders
    (``placeholder: true``, ``byte_size``, ``entry_id`` for ``chat.entry``).
    Unless paging backwards, ``in_flight_run`` reports the running turn and
    ``pending_inputs`` the messages queued behind it or waiting to be steered
    into it (``TaskRuntime.pending_inputs``), oldest first.
    """
    raw_params = params or {}
    session_key = _canonical_webchat_session_key(raw_params.get("sessionKey"))
    limit = _normalize_chat_history_limit(raw_params.get("limit"))
    before = raw_params.get("before")
    after = raw_params.get("after")
    delta_cursor = raw_params.get("delta_cursor", raw_params.get("deltaCursor"))
    include_canonical = _chat_history_bool(
        raw_params.get("includeCanonical"),
        default=True,
    )
    include_summaries = _chat_history_bool(
        raw_params.get("includeSummaries"),
        default=True,
    )

    mgr = _require_chat_session_manager(ctx)

    try:
        transcript, canonical_available = await _chat_history_transcript(
            mgr,
            session_key,
            include_canonical=include_canonical,
        )
    except KeyError:
        # A whitelist, not a blanket swallow — a typo'd agent key should still
        # surface as an error rather than silently render an empty chat.
        if _is_webchat_session_key(session_key) or _is_cron_session_key(session_key):
            empty = _empty_chat_history_payload(limit)
            if delta_cursor is not None:
                empty["kind"] = "reset"
            empty["in_flight_run"] = None
            empty["pending_inputs"] = _pending_inputs(ctx, session_key)
            return empty
        raise

    in_flight_run: dict[str, Any] | None = None
    pending_inputs: list[dict[str, Any]] = []
    if not before:
        from agentos.gateway.rpc_sessions import session_in_flight_run

        in_flight_run = await session_in_flight_run(ctx, session_key)
        # The running turn is shown from ``in_flight_run``; its in-progress
        # transcript row would list the same tool calls a second time.
        transcript = hide_live_turn_progress(transcript, in_flight_run)
        pending_inputs = _pending_inputs(ctx, session_key)

    kind: str | None = None
    if delta_cursor is not None:
        cursor_index = _chat_history_cursor_index(transcript, delta_cursor)
        if cursor_index is not None:
            delta_entries, delta_messages, delta_more = _budgeted_messages(
                transcript[cursor_index + 1 :],
                response_budget=CHAT_HISTORY_DELTA_MAX_BYTES,
                message_budget=CHAT_HISTORY_MESSAGE_BUDGET_BYTES,
                newest_first=False,
                max_rows=CHAT_HISTORY_DELTA_MAX_ROWS,
            )
            outside = _steered_ids_outside(delta_messages)
            if not outside or not any(
                getattr(entry, "message_id", None) in outside
                for entry in transcript[: cursor_index + 1]
            ):
                delta_messages = _split_steered_turns(delta_messages)
                if not _control_ui_show_thinking(ctx):
                    for message in delta_messages:
                        message.pop("has_thinking", None)
                return {
                    "kind": "delta",
                    "messages": _annotate_transcript_attachment_downloads(
                        delta_messages, session_key=session_key
                    ),
                    # More rows after these: ask again from ``newest_cursor``.
                    "has_more": delta_more,
                    "newest_cursor": (
                        _chat_history_cursor(delta_entries[-1])
                        if delta_entries
                        else str(delta_cursor)
                    ),
                    "loaded_count": len(delta_entries),
                    "canonical_available": canonical_available,
                    "in_flight_run": in_flight_run,
                    "pending_inputs": pending_inputs,
                }
            # A steered turn whose steered message the client already has
            # (at or before the cursor): the message has to move between the
            # turn's parts, which a delta cannot do; send the latest page.
        # The cursor names no row (compaction, reset, another session): the
        # client must replace what it has with the latest page.
        kind = "reset"
        before = after = None

    page_entries, has_more = _chat_history_page(
        transcript,
        limit=limit,
        before=before,
        after=after,
    )
    page_entries, messages, budget_cut = _budgeted_messages(
        page_entries,
        response_budget=CHAT_HISTORY_RESPONSE_BUDGET_BYTES,
        message_budget=CHAT_HISTORY_MESSAGE_BUDGET_BYTES,
        newest_first=not after,
    )
    if budget_cut:
        has_more = True
    messages = _split_steered_turns(messages)
    summaries = await _chat_history_summaries(
        mgr,
        session_key,
        include_summaries=include_summaries,
    )
    if summaries:
        history_scope = "compacted"
    elif has_more:
        history_scope = "latest_window"
    else:
        history_scope = "complete"

    if not _control_ui_show_thinking(ctx):
        for message in messages:
            message.pop("has_thinking", None)
    payload: dict[str, Any] = {
        "messages": _annotate_transcript_attachment_downloads(
            messages,
            session_key=session_key,
        ),
        "has_more": has_more,
        "oldest_cursor": _chat_history_cursor(page_entries[0]) if page_entries else None,
        "newest_cursor": _chat_history_cursor(page_entries[-1]) if page_entries else None,
        "history_scope": history_scope,
        "loaded_count": len(page_entries),
        "page_size": limit,
        "canonical_available": canonical_available,
        "compaction_summaries": summaries,
    }
    if kind is not None:
        payload["kind"] = kind
    if not before:
        payload["in_flight_run"] = in_flight_run
        payload["pending_inputs"] = pending_inputs
    return payload


@_d.method("chat.entry", CONTROL_ONLY)
async def _handle_chat_entry(params: dict | None, ctx: RpcContext) -> dict:
    """The full message behind a ``chat.history`` placeholder.

    ``entryId`` is the placeholder's ``entry_id`` (a transcript id, or a
    message id for rows that have none).
    """
    raw_params = params or {}
    session_key = _canonical_webchat_session_key(raw_params.get("sessionKey"))
    entry_id = raw_params.get("entryId", raw_params.get("entry_id"))
    if entry_id in (None, ""):
        raise ValueError("params.entryId is required")
    mgr = _require_chat_session_manager(ctx)
    transcript, _ = await _chat_history_transcript(mgr, session_key, include_canonical=True)
    wanted = str(entry_id)
    for entry in transcript:
        ids = {str(getattr(entry, "id", "")), str(getattr(entry, "message_id", ""))}
        if wanted not in ids:
            continue
        projected = transcript_entries_to_chat_messages([entry], limit=None)
        if not projected:
            break
        message = projected[0]
        if not _control_ui_show_thinking(ctx):
            message.pop("has_thinking", None)
        size = _message_size(message)
        if size > _CHAT_ENTRY_MAX_BYTES:
            raise RpcHandlerError(
                ErrorCode.INVALID_REQUEST,
                "This message is too large to send in one frame.",
                details={
                    "code": ErrorDetailCode.PAYLOAD_TOO_LARGE.value,
                    "entry_id": entry_id,
                    "byte_size": size,
                },
            )
        _annotate_transcript_attachment_downloads([message], session_key=session_key)
        return {
            "sessionKey": session_key,
            "entry_id": entry_id,
            "byte_size": size,
            "message": message,
        }
    raise KeyError(f"Transcript entry not found: {entry_id}")


@_d.method("chat.inject")
async def _handle_chat_inject(params: dict | None, ctx: RpcContext) -> dict:
    if not isinstance(params, dict):
        raise ValueError("params required: sessionKey, role, content")
    for field in ("sessionKey", "role", "content"):
        if field not in params:
            raise ValueError(f"params.{field} is required")

    role = params["role"]
    if role not in ("user", "assistant", "system"):
        raise ValueError(f"Invalid role: {role}")

    session_key = _canonical_webchat_session_key(params["sessionKey"])

    if ctx.session_manager is None:
        raise RpcUnavailableError("No session manager available")

    storage = getattr(ctx.session_manager, "_storage", None)
    if storage is not None:
        existing = await storage.get_session(session_key)
        if existing is None:
            raise KeyError(f"Session not found: {session_key}")

    await ctx.session_manager.append_message(session_key, role=role, content=params["content"])
    return {"ok": True, "sessionKey": session_key}
