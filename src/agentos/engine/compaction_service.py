"""One compaction service for every context-overflow path.

Before this module the engine had five compaction paths with different rules
(in-turn preflight, provider-overflow retry, T3-upgrade, before-turn preflight,
emergency request-scoped override). They are now one service with:

* **one trigger** -- the live request estimate against ``window * ratio``
  (``AgentConfig.context_overflow_threshold``, 0.85 by default). A provider
  that still rejects a request as too long invokes the same service with
  ``force=True`` and the window it reported;
* **one shape** -- a single summary exchange followed by the current turn,
  nothing else replayed::

      user:      "[Context summary]\\n<summary>"
      assistant: "Understood. Continuing from summary."
      <current turn: its input message, then its tool rounds, verbatim>

  A user/assistant pair keeps strict role alternation, which every provider
  accepts (a lone system-style note would need a mid-conversation ``system``
  role that several providers reject). The current turn is kept byte for
  byte -- images, per-block thinking with signatures, tool pairs. Only when
  the current turn alone does not fit are its *oldest complete tool rounds*
  folded into the summary as well; the input message and the latest round
  are always kept;
* **one summarizer** -- it goes through :class:`~agentos.provider.LLMProvider`
  (``provider.chat``), never raw HTTP, and falls back to a deterministic
  digest when the provider fails;
* **one summary format** -- the summarizer writes fixed sections
  (:data:`SUMMARY_SECTIONS`) and, on a later compaction, revises the previous
  summary instead of starting over. The files the compacted tool calls read
  and modified are appended by the engine (:mod:`agentos.engine.compaction_files`),
  merged with the previous summary's lists, so the model can neither lose nor
  invent them;
* **never mid tool-round** -- a transcript that ends on an assistant
  ``tool_use`` (results not appended yet) is never compacted, and a cut never
  separates a ``tool_use`` from its ``tool_result``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import structlog

from agentos.engine.cache_break_monitor import notify_compaction
from agentos.engine.compaction_files import (
    FILE_ACTIVITY_MAX_TOKENS,
    FileActivity,
    attach_file_activity,
    file_activity_from_messages,
    merge_file_activity,
    split_file_activity,
)
from agentos.provider.types import (
    ChatConfig,
    ContentBlockCompaction,
    ContentBlockDocument,
    ContentBlockImage,
    ContentBlockText,
    ContentBlockToolResult,
    ContentBlockToolUse,
    Message,
)
from agentos.session.compaction_lifecycle import (
    COMPACTION_CHUNK_SUMMARIZED_EVENT,
    COMPACTION_SUMMARY_VERIFIED_EVENT,
    COMPACTION_TRIGGERED_EVENT,
    compaction_effect_payload,
    compaction_lifecycle_payload,
    new_compaction_id,
)
from agentos.session.keys import is_subagent_key

log = structlog.get_logger(__name__)

DEFAULT_COMPACTION_RATIO = 0.85
CONTEXT_SUMMARY_HEADER = "[Context summary]"
SUMMARY_ACK = "Understood. Continuing from summary."
SUMMARY_MAX_TOKENS = 2048
# A compaction must take at least this share off the request to be applied.
_MIN_GAIN_RATIO = 0.10
# Smallest summary the gain pre-check assumes the summarizer could write.
_OPTIMISTIC_SUMMARY_TOKENS = 512
_SUMMARY_TIMEOUT_SECONDS = 90.0
_PREVIEW_CHARS = 240
_FALLBACK_LINE_CHARS = 200
_FALLBACK_MAX_CHARS = 12_000
_FALLBACK_MIN_CHARS = 1_000
_FALLBACK_FLOOR_CHARS = 512
# Grace on top of the summarizer's own timeout before the service stops
# waiting for it and uses the deterministic digest.
_SUMMARY_TIMEOUT_GRACE_SECONDS = 5.0

# The fixed sections of every summary, with what belongs in each. A summary
# replaces the history it covers, so these are the things a resumed session
# cannot do without.
SUMMARY_SECTIONS: tuple[tuple[str, str], ...] = (
    ("Goal", "what the user is trying to achieve overall, including their latest request"),
    (
        "Constraints and preferences",
        "requirements, limits, conventions and style the user asked for",
    ),
    ("Done", "steps that are finished, each with its outcome, so they are not repeated"),
    ("In progress", "work that was started and not finished, and how far it got"),
    ("Blocked", "anything that is stuck, what it waits on, and what would unblock it"),
    ("Key decisions", "choices that were made and the reason for each"),
    ("Next steps", "the concrete actions that come next, in order"),
    (
        "Critical context",
        "exact identifiers later steps depend on: paths, commands, function names, "
        "error messages, numbers, IDs, URLs -- copied verbatim",
    ),
)

# The summary format, shared by every compaction path. Callers with their own
# identifier policy (the session-level ``/compact``) append it to this.
SUMMARY_FORMAT_PROMPT = (
    "You are a conversation compactor. What you write replaces the conversation "
    "you are given, and the same assistant resumes the work from it, so anything "
    "you leave out is lost.\n\n"
    "Reply with exactly these sections, in this order. Open each one with a "
    "Markdown heading `## <section name>` spelled as below, then give short "
    "bullet points; write `- None` under a section that has nothing:\n"
    + "".join(f"- {name}: {guidance}.\n" for name, guidance in SUMMARY_SECTIONS)
    + "\nWhen an earlier summary is supplied, it is your starting point: keep "
    "its points that still hold, move work that has since finished into Done, "
    "remove what was superseded, and add what the newer conversation brings. "
    "Revise it; do not start over and do not drop finished work.\n"
    "Do not add a list of files read or modified; the engine appends one.\n"
    "Be brief, favour recent work over old history, and write the bullet text "
    "in the same language as the conversation."
)
_IDENTIFIER_RULE = (
    "IMPORTANT: Preserve all opaque identifiers exactly as written -- UUIDs, "
    "hashes, IDs, tokens, hostnames, IPs, ports, URLs, file names and paths. Do "
    "NOT shorten, reconstruct, or paraphrase any identifier."
)
_SUMMARY_SYSTEM_PROMPT = f"{SUMMARY_FORMAT_PROMPT}\n\n{_IDENTIFIER_RULE}"
# The model's share of the summary budget; the engine's file block takes the rest.
SUMMARY_TEXT_MAX_TOKENS = SUMMARY_MAX_TOKENS - FILE_ACTIVITY_MAX_TOKENS

CompactionStatus = Literal["compacted", "skipped", "failed"]


class _ChatProvider(Protocol):
    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> Any: ...


Summarizer = Callable[[list[Message], str | None], Awaitable[tuple[str, str]]]


@dataclass
class CompactionResult:
    """Outcome of one :meth:`CompactionService.compact` call."""

    status: CompactionStatus
    messages: list[Message]
    turn_input_index: int
    reason: str | None = None
    summary: str = ""
    summary_source: str = "skipped"
    compaction_id: str | None = None
    removed_count: int = 0
    kept_count: int = 0
    folded_rounds: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    kept_entries: list[dict[str, Any]] = field(default_factory=list)
    # Files read / modified up to the summary (previous summaries included);
    # also rendered at the end of :attr:`summary`.
    file_activity: FileActivity | None = None

    @property
    def compacted(self) -> bool:
        return self.status == "compacted"


# ---------------------------------------------------------------------------
# Pure policy helpers
# ---------------------------------------------------------------------------


def trigger_limit_tokens(
    window_tokens: int,
    ratio: float,
    budget_tokens: int | None = None,
) -> float:
    """The request size compaction keeps under: ``window * ratio``, or the
    provider's own request budget when that is tighter.

    The providers rewrite (compact) any payload over their effective proof
    budget on every request; triggering compaction below that budget keeps
    the requests the providers see untouched, so their prompt cache holds.
    """
    limit = float(window_tokens) * ratio
    if budget_tokens is not None and budget_tokens > 0:
        limit = min(limit, float(budget_tokens))
    return limit


def should_compact(
    estimated_tokens: int,
    window_tokens: int,
    ratio: float,
    budget_tokens: int | None = None,
) -> bool:
    """The single trigger: the live request estimate is over the limit."""
    if window_tokens <= 0:
        return False
    return estimated_tokens > trigger_limit_tokens(window_tokens, ratio, budget_tokens)


def is_ephemeral_session(session_key: str | None) -> bool:
    """Subagent and cron sessions: compacted in memory, never checkpointed.

    Uses :func:`agentos.session.keys.is_subagent_key` so both the legacy
    ``subagent:`` keys and the canonical ``agent:<id>:subagent:<run>`` keys
    match (a ``startswith("subagent:")`` test missed the latter).
    """
    key = (session_key or "").strip().lower()
    if not key:
        return False
    return is_subagent_key(key) or key.startswith("cron:")


def ends_mid_tool_round(messages: Sequence[Message]) -> bool:
    """True while the last assistant ``tool_use`` still waits for its results."""
    if not messages:
        return False
    last = messages[-1]
    return (
        last.role == "assistant"
        and isinstance(last.content, list)
        and any(isinstance(block, ContentBlockToolUse) for block in last.content)
    )


def is_summary_pair(messages: Sequence[Message], index: int = 0) -> bool:
    if len(messages) < index + 2:
        return False
    first, second = messages[index], messages[index + 1]
    return (
        first.role == "user"
        and isinstance(first.content, str)
        and first.content.startswith(CONTEXT_SUMMARY_HEADER)
        and second.role == "assistant"
        and second.content == SUMMARY_ACK
    )


def summary_messages(summary: str) -> list[Message]:
    return [
        Message(role="user", content=f"{CONTEXT_SUMMARY_HEADER}\n{summary}"),
        Message(role="assistant", content=SUMMARY_ACK),
    ]


def _current_turn_rounds(current: Sequence[Message]) -> tuple[int, list[tuple[int, int]]]:
    """Split the current turn into its input group and complete tool rounds.

    Returns ``(input_end, rounds)``: ``current[:input_end]`` are the leading
    user input messages; every round ``(start, end)`` starts at an assistant
    message and runs until the next assistant message, so a ``tool_use`` and
    its ``tool_result`` messages always share a round.
    """
    input_end = 0
    while input_end < len(current) and current[input_end].role == "user":
        input_end += 1
    rounds: list[tuple[int, int]] = []
    start: int | None = None
    for index in range(input_end, len(current)):
        if current[index].role == "assistant":
            if start is not None:
                rounds.append((start, index))
            start = index
    if start is not None:
        rounds.append((start, len(current)))
    return input_end, rounds


# ---------------------------------------------------------------------------
# Summarizer
# ---------------------------------------------------------------------------


def _preview(text: str, max_chars: int = _PREVIEW_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max_chars - head
    return f"{text[:head]}\n[...omitted {len(text) - max_chars} chars...]\n{text[-tail:]}"


def _json_preview(value: Any) -> str:
    try:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        rendered = str(value)
    if len(rendered) <= _PREVIEW_CHARS:
        return rendered
    digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()[:16]
    return f"<json chars={len(rendered)} sha256={digest} preview={_preview(rendered)!r}>"


def render_messages_for_summary(messages: Sequence[Message]) -> str:
    """Readable text for the summarizer: tool payloads as previews, no bytes."""
    lines: list[str] = []
    for message in messages:
        if isinstance(message.content, str):
            lines.append(f"[{message.role}]: {message.content}")
            continue
        parts: list[str] = []
        for block in message.content:
            if isinstance(block, ContentBlockText):
                if block.text.strip():
                    parts.append(block.text)
            elif isinstance(block, ContentBlockToolUse):
                parts.append(f"[tool_use {block.name} id={block.id}] {_json_preview(block.input)}")
            elif isinstance(block, ContentBlockToolResult):
                content = (
                    block.content
                    if isinstance(block.content, str)
                    else _json_preview(block.content)
                )
                digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
                parts.append(
                    f"[tool_result id={block.tool_use_id} is_error={block.is_error} "
                    f"chars={len(content)} sha256={digest}] {_preview(content)}"
                )
            elif isinstance(block, ContentBlockImage):
                parts.append(f"[image: {block.media_type}]")
            elif isinstance(block, ContentBlockDocument):
                parts.append(f"[document: {block.title or block.media_type}]")
            elif isinstance(block, ContentBlockCompaction):
                if block.content:
                    parts.append(block.content)
            # Reasoning (thinking / redacted thinking) is not summarized.
        if parts:
            lines.append(f"[{message.role}]: " + "\n".join(parts))
    return "\n\n".join(lines)


def fallback_max_chars(window_tokens: int) -> int:
    """Upper bound on a deterministic digest for a model *window_tokens* wide.

    A digest keeps up to 200 chars per message, so on a history of many
    short messages it is barely smaller than what it replaces; merged over
    several chunks, or nested in the next digest as "previous context", it
    could even outgrow the window. One eighth of the window (in chars, at 4
    chars per token) and never more than ``_FALLBACK_MAX_CHARS`` keeps a
    fallback compaction a real reduction.
    """
    if window_tokens <= 0:
        return _FALLBACK_MAX_CHARS
    return max(_FALLBACK_MIN_CHARS, min(_FALLBACK_MAX_CHARS, window_tokens * 4 // 8))


def _clip_middle(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    marker = "\n[...omitted...]\n"
    keep = max(0, max_chars - len(marker))
    head = keep // 2
    return text[:head] + marker + text[len(text) - (keep - head) :]


def fallback_summary(
    messages: Sequence[Message],
    previous_summary: str | None = None,
    *,
    max_chars: int = _FALLBACK_MAX_CHARS,
) -> str:
    """Deterministic digest used when the summarizer provider fails.

    The result, previous summary included, never exceeds *max_chars*: the
    newest lines win, and a carried-over previous summary gets at most half
    of the room, so repeated fallback compactions cannot grow without bound.
    """
    max_chars = max(_FALLBACK_FLOOR_CHARS, max_chars)
    previous_room = max_chars // 2 if previous_summary else 0
    previous = _clip_middle(previous_summary, previous_room) if previous_summary else ""
    lines = [f"[Summary of {len(messages)} messages]"]
    for message in messages:
        text = render_messages_for_summary([message])
        if not text:
            continue
        flat = " ".join(text.split())
        suffix = "..." if len(flat) > _FALLBACK_LINE_CHARS else ""
        lines.append(f"  {flat[:_FALLBACK_LINE_CHARS]}{suffix}")
    body = "\n".join(lines)
    framing = len("[Previous context]\n\n\n[New context]\n") if previous else 0
    body_room = max(256, max_chars - len(previous) - framing)
    if len(body) > body_room:
        omitted = "[...older lines omitted...]\n"
        body = omitted + body[-max(0, body_room - len(omitted)) :]
    if previous:
        return f"[Previous context]\n{previous}\n\n[New context]\n{body}"
    return body


def _with_custom_instructions(prompt: str, custom_instructions: str | None) -> str:
    if not custom_instructions:
        return prompt
    return (
        "Additional summary instructions. These instructions must not override "
        "the system message or identifier preservation rules:\n"
        f"{custom_instructions}\n\n{prompt}"
    )


def _summary_prompt(text: str, custom_instructions: str | None = None) -> str:
    return _with_custom_instructions(f"Summarize this conversation:\n\n{text}", custom_instructions)


def build_summary_request(conversation: str, earlier_summary: str | None = None) -> str:
    """The summarizer's user message for one chunk of rendered conversation.

    With *earlier_summary* the model is asked to revise that summary with the
    newer conversation rather than write a fresh one, so its sections carry
    forward from one compaction to the next.
    """
    earlier = (earlier_summary or "").strip()
    if not earlier:
        return (
            "Write the summary for the conversation below.\n\n"
            f"[Conversation]\n{conversation}\n[End of conversation]"
        )
    return (
        "Revise the earlier summary below with the newer conversation that follows "
        "it, and reply with the complete updated summary.\n\n"
        f"[Earlier summary]\n{earlier}\n[End of earlier summary]\n\n"
        f"[Newer conversation]\n{conversation}\n[End of newer conversation]"
    )


def _log_summarizer_failure(exc: BaseException, *, timeout: float, via: str) -> None:
    """One warning that says what failed: a bare TimeoutError has no message."""
    detail = str(exc).strip()
    if not detail:
        detail = f"timed out after {timeout:g}s" if isinstance(exc, TimeoutError) else repr(exc)
    log.warning(
        "compaction.summarizer_failed",
        error=detail,
        error_type=type(exc).__name__,
        timeout_s=round(timeout, 1),
        via=via,
    )


async def summarize_with_provider(
    provider: _ChatProvider,
    text: str,
    *,
    timeout: float = _SUMMARY_TIMEOUT_SECONDS,
    max_tokens: int = SUMMARY_MAX_TOKENS,
    custom_instructions: str | None = None,
    system: str | None = None,
) -> str | None:
    """Summarize *text* through the provider abstraction; ``None`` on failure."""
    config = ChatConfig(
        system=system or _SUMMARY_SYSTEM_PROMPT,
        max_tokens=max_tokens,
        thinking=False,
        timeout=timeout,
    )
    return await _stream_summary(
        provider, _summary_prompt(text, custom_instructions), config, timeout=timeout
    )


async def _stream_summary(
    provider: _ChatProvider,
    prompt: str,
    config: ChatConfig,
    *,
    timeout: float,
) -> str | None:
    parts: list[str] = []
    try:
        async with asyncio.timeout(timeout):
            stream = provider.chat([Message(role="user", content=prompt)], None, config)
            async for event in stream:
                kind = getattr(event, "kind", None)
                if kind == "text_delta":
                    parts.append(str(getattr(event, "text", "") or ""))
                elif kind == "error":
                    log.warning(
                        "compaction.summarizer_provider_error",
                        error=str(getattr(event, "message", "") or "")
                        or str(getattr(event, "code", "") or "provider error"),
                    )
                    return None
                elif kind == "done":
                    break
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - summarizer failure falls back
        _log_summarizer_failure(exc, timeout=timeout, via="provider")
        return None
    summary = "".join(parts).strip()
    return summary or None


def _summary_chat_config(timeout: float, system: str = _SUMMARY_SYSTEM_PROMPT) -> ChatConfig:
    """The structured-summary call: fixed sections, bounded, no thinking."""
    return ChatConfig(
        system=system,
        max_tokens=SUMMARY_TEXT_MAX_TOKENS,
        thinking=False,
        timeout=timeout,
    )


async def summarize_structured_with_provider(
    provider: _ChatProvider,
    conversation: str,
    *,
    earlier_summary: str | None = None,
    timeout: float = _SUMMARY_TIMEOUT_SECONDS,
    custom_instructions: str | None = None,
    identifier_instruction: str | None = None,
) -> str | None:
    """One structured-summary call for callers outside :class:`CompactionService`.

    Same sections, request shape and output bound as the engine's own
    compaction; *identifier_instruction* replaces the default identifier rule
    (empty means none), and *custom_instructions* go into the user message.
    """
    rule = _IDENTIFIER_RULE if identifier_instruction is None else identifier_instruction
    system = f"{SUMMARY_FORMAT_PROMPT}\n\n{rule}" if rule else SUMMARY_FORMAT_PROMPT
    prompt = _with_custom_instructions(
        build_summary_request(conversation, earlier_summary), custom_instructions
    )
    return await _stream_summary(
        provider, prompt, _summary_chat_config(timeout, system), timeout=timeout
    )


ChunkSummarizer = Callable[[str, float], Awaitable[str | None]]

COMPACTION_AUX_TASK = "compaction"
DEFAULT_SUMMARY_TIMEOUT_SECONDS = 45.0


def auxiliary_chunk_summarizer(
    *,
    hint: tuple[str, str] = ("", ""),
    session_key: str | None = None,
) -> ChunkSummarizer:
    """Summarize through the auxiliary side-task client (task ``compaction``).

    The auxiliary path picks the cheap model an operator configured for side
    work (``[auxiliary.tasks.compaction]``, ``[auxiliary]``, else the
    router's cheapest text tier passed as *hint*), records the spend under
    ``aux:compaction`` and never sends thinking -- the turn's own model and
    prompt cache are left alone.
    """

    async def _summarize(text: str, timeout: float) -> str | None:
        from agentos.provider.auxiliary import get_auxiliary_client

        try:
            result = await get_auxiliary_client().complete(
                task=COMPACTION_AUX_TASK,
                messages=[Message(role="user", content=text)],
                preferred_provider=hint[0],
                preferred_model=hint[1],
                chat_config=_summary_chat_config(timeout),
                timeout=timeout,
                session_key=session_key,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - summarizer failure falls back
            _log_summarizer_failure(exc, timeout=timeout, via="auxiliary")
            return None
        summary = (result.text or "").strip()
        return summary or None

    return _summarize


def _provider_chunk_summarizer(provider: _ChatProvider) -> ChunkSummarizer:
    async def _summarize(text: str, timeout: float) -> str | None:
        return await _stream_summary(provider, text, _summary_chat_config(timeout), timeout=timeout)

    return _summarize


def _chunk_messages(messages: Sequence[Message], max_chars: int) -> list[list[Message]]:
    chunks: list[list[Message]] = []
    current: list[Message] = []
    size = 0
    for message in messages:
        rendered = len(render_messages_for_summary([message]))
        if current and size + rendered > max_chars:
            chunks.append(current)
            current, size = [], 0
        current.append(message)
        size += rendered
    if current:
        chunks.append(current)
    return chunks


async def summarize_messages(
    summarizer: _ChatProvider | ChunkSummarizer | None,
    messages: list[Message],
    previous_summary: str | None,
    *,
    window_tokens: int,
    timeout: float = _SUMMARY_TIMEOUT_SECONDS,
) -> tuple[str, str]:
    """Return ``(summary, source)`` with source ``llm`` / ``mixed`` / ``fallback``.

    *summarizer* is an ``LLMProvider`` or a chunk summarizer such as
    :func:`auxiliary_chunk_summarizer`; a chunk summarizer receives the whole
    user message built by :func:`build_summary_request`. *timeout* bounds the
    whole call, not each chunk; after the first failure the remaining chunks
    use the deterministic digest instead of waiting out the timeout again.

    The chunks are summarized in order, each one revising the summary so far
    (the first one revises *previous_summary*), so the result is one summary
    with one set of sections rather than one per chunk.
    """
    digest_chars = fallback_max_chars(window_tokens)
    if summarizer is None:
        return fallback_summary(messages, previous_summary, max_chars=digest_chars), "fallback"
    chunk_summarizer: ChunkSummarizer
    if hasattr(summarizer, "chat"):
        chunk_summarizer = _provider_chunk_summarizer(summarizer)  # type: ignore[arg-type]
    else:
        chunk_summarizer = summarizer  # type: ignore[assignment]
    # Keep each summarizer input well inside the model window.
    max_chars = max(8_000, min(400_000, int(window_tokens * 4 * 0.5)))
    chunks = _chunk_messages(messages, max_chars)
    # The digests of all chunks share one bound, so a fallback over many
    # chunks is no larger than a fallback over one.
    chunk_digest_chars = max(_FALLBACK_FLOOR_CHARS, digest_chars // max(1, len(chunks)))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.01, timeout)
    llm_available = True
    rolled: str | None = None  # the model's summary so far
    digests: list[str] = []
    llm_chunks = 0
    for chunk in chunks:
        remaining = deadline - loop.time()
        result = None
        if llm_available and remaining > 0:
            request = build_summary_request(
                render_messages_for_summary(chunk),
                rolled if rolled is not None else previous_summary,
            )
            result = await chunk_summarizer(request, remaining)
            llm_available = result is not None
        if result:
            rolled = result
            llm_chunks += 1
            continue
        # The earlier summary rides along in the first digest unless the
        # model's summary (which already covers it) comes first.
        carried = previous_summary if rolled is None and not digests else None
        digests.append(fallback_summary(chunk, carried, max_chars=chunk_digest_chars))
    summaries = ([rolled] if rolled is not None else []) + digests
    if len(summaries) == 1:
        merged = summaries[0]
    else:
        merged = "[Merged context summary]" + "".join(
            f"\n\n--- Part {index + 1} ---\n{summary}" for index, summary in enumerate(summaries)
        )
    if llm_chunks == len(chunks):
        source = "llm"
    elif llm_chunks:
        source = "mixed"
    else:
        source = "fallback"
        if len(merged) > digest_chars:
            # Newest context wins, as in a single-chunk digest.
            omitted = "[...older context omitted...]\n"
            merged = omitted + merged[-max(0, digest_chars - len(omitted)) :]
    return merged, source


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class CompactionService:
    """Compact a live transcript to one summary exchange plus the current turn."""

    def __init__(
        self,
        *,
        provider: _ChatProvider | None,
        ratio: float = DEFAULT_COMPACTION_RATIO,
        session_key: str | None = None,
        summarizer: Summarizer | None = None,
        timeout: float = _SUMMARY_TIMEOUT_SECONDS,
        phase: str = "agent_inline_overflow",
        chunk_summarizer: ChunkSummarizer | None = None,
    ) -> None:
        self._provider = provider
        self._chunk_summarizer = chunk_summarizer
        self._ratio = ratio if 0.0 < ratio <= 1.0 else DEFAULT_COMPACTION_RATIO
        self._session_key = session_key
        self._summarizer = summarizer
        self._timeout = timeout
        self._phase = phase

    @property
    def ratio(self) -> float:
        return self._ratio

    def should_compact(
        self,
        estimated_tokens: int,
        window_tokens: int,
        budget_tokens: int | None = None,
    ) -> bool:
        return should_compact(estimated_tokens, window_tokens, self._ratio, budget_tokens)

    async def _summarize(
        self,
        messages: list[Message],
        previous_summary: str | None,
        window_tokens: int,
    ) -> tuple[str, str]:
        """Summarize *messages*; a failing or hung summarizer yields the digest.

        The summarizer already falls back per chunk when the provider fails,
        but an exception or a call that outlives its own timeout used to fail
        the whole compaction, so the history was not reduced at all. Both now
        fall back to the bounded deterministic digest.
        """
        try:
            async with asyncio.timeout(self._timeout + _SUMMARY_TIMEOUT_GRACE_SECONDS):
                if self._summarizer is not None:
                    return await self._summarizer(messages, previous_summary)
                return await summarize_messages(
                    self._chunk_summarizer or self._provider,
                    messages,
                    previous_summary,
                    window_tokens=window_tokens,
                    timeout=self._timeout,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the digest is the fallback
            _log_summarizer_failure(exc, timeout=self._timeout, via="service")
        digest = fallback_summary(
            messages,
            previous_summary,
            max_chars=fallback_max_chars(window_tokens),
        )
        return digest, "fallback"

    def _decline(
        self,
        unchanged: CompactionResult,
        compaction_id: str,
        reason: str,
        **payload: Any,
    ) -> CompactionResult:
        """Leave the transcript as is, say why once, and persist nothing."""
        log.info(
            "compaction.declined",
            session_key=self._session_key,
            reason=reason,
            **payload,
        )
        unchanged.reason = reason
        unchanged.compaction_id = compaction_id
        self._notify("skipped", compaction_id, reason=reason, **payload)
        return unchanged

    def _notify(self, status: str, compaction_id: str, **payload: Any) -> None:
        if not self._session_key:
            return
        reason = payload.get("reason")
        notify_compaction(
            self._session_key,
            source="automatic",
            phase=self._phase,
            status=status,
            **payload,
            **compaction_effect_payload(
                status=status,
                reason=reason if isinstance(reason, str) else None,
                user_visible=status != "skipped",
            ),
            **compaction_lifecycle_payload(compaction_id, COMPACTION_TRIGGERED_EVENT),
        )

    async def compact(
        self,
        messages: list[Message],
        *,
        turn_input_index: int,
        estimated_tokens: int,
        window_tokens: int,
        message_tokens: Callable[[Sequence[Message]], int],
        force: bool = False,
        input_entry: dict[str, Any] | None = None,
        budget_tokens: int | None = None,
        proactive: bool = False,
    ) -> CompactionResult:
        """Compact *messages* when over the trigger (or when *force* is set).

        *message_tokens* estimates the provider size of a message list; the
        rest of the request (system prompt, tools, config) is taken as
        ``estimated_tokens - message_tokens(messages)``. *input_entry* is the
        persisted user row of this turn (``{"role", "content"}``) that a
        durable rewrite keeps; the agent passes it through ``kept_entries``.

        *proactive* is the between-turns background job: it runs below the
        trigger (the caller already decided it is worth it), keeps every
        decline rule, and never folds rounds of the kept turn -- that turn is
        persisted already and stays byte for byte.
        """
        unchanged = CompactionResult(
            status="skipped",
            messages=messages,
            turn_input_index=turn_input_index,
            tokens_before=estimated_tokens,
            tokens_after=estimated_tokens,
        )
        if (
            not force
            and not proactive
            and not self.should_compact(estimated_tokens, window_tokens, budget_tokens)
        ):
            unchanged.reason = "under_threshold"
            return unchanged
        compaction_id = new_compaction_id()
        if ends_mid_tool_round(messages):
            # Results are not appended yet; compacting now could separate a
            # tool_use from its results. The next request checks again.
            unchanged.reason = "mid_tool_round"
            unchanged.compaction_id = compaction_id
            self._notify("skipped", compaction_id, reason="mid_tool_round")
            return unchanged

        split = max(0, min(turn_input_index, len(messages)))
        prefix = list(messages[:split])
        current = list(messages[split:])
        previous_summary: str | None = None
        previous_files: FileActivity | None = None
        if is_summary_pair(prefix):
            previous_summary = str(prefix[0].content)[len(CONTEXT_SUMMARY_HEADER) :].lstrip("\n")
            # The engine's file block is merged below, never handed to the
            # summarizer: the model revises the text, the engine the lists.
            previous_summary, previous_files = split_file_activity(previous_summary)
            prefix = prefix[2:]

        overhead = max(0, estimated_tokens - message_tokens(messages))
        limit = trigger_limit_tokens(window_tokens, self._ratio, budget_tokens)
        budget = max(1, int(limit) - overhead - SUMMARY_MAX_TOKENS)
        input_end, rounds = _current_turn_rounds(current)
        keep_from = 0
        folded_rounds = 0
        if not proactive and message_tokens(current) > budget * 0.75 and len(rounds) > 1:
            # The current turn alone does not leave room: fold its oldest
            # complete tool rounds, always keeping the input and latest round.
            target = budget * 0.5
            kept_rounds = [rounds[-1]]
            for round_span in reversed(rounds[:-1]):
                candidate = [round_span, *kept_rounds]
                kept = current[:input_end] + [
                    message for start, end in candidate for message in current[start:end]
                ]
                if message_tokens(kept) > target:
                    break
                kept_rounds = candidate
            keep_from = kept_rounds[0][0]
            folded_rounds = sum(1 for span in rounds if span[0] < keep_from)
        folded_current = current[input_end:keep_from] if keep_from else []
        kept_current = current[:input_end] + current[keep_from:] if keep_from else current
        to_fold = prefix + folded_current
        if not force and overhead > limit:
            # System prompt + tools alone are over the limit: no compaction
            # can help, and blaming the current turn would mislead.
            return self._decline(
                unchanged,
                compaction_id,
                "fixed_context_too_large",
                tokens_before=estimated_tokens,
                fixed_tokens=overhead,
                limit_tokens=int(limit),
                context_window_tokens=window_tokens,
                hint=(
                    "system prompt and tool definitions alone exceed the compaction "
                    "limit; preflight_compact_ratio or the model context window is "
                    "set too low"
                ),
            )
        if not force and overhead + message_tokens(kept_current) > limit:
            # Preflight: the kept current turn alone is over the limit (a
            # single huge tool result of this turn, say), so summarizing --
            # and durably rewriting -- older history would cost that context
            # and still not let this request through. (A forced call -- the
            # provider already refused -- carries the provider's numbers
            # instead of a live estimate and always tries.)
            return self._decline(
                unchanged,
                compaction_id,
                "current_turn_too_large",
                tokens_before=estimated_tokens,
                context_window_tokens=window_tokens,
            )
        if not to_fold:
            unchanged.reason = "nothing_to_compact"
            unchanged.compaction_id = compaction_id
            self._notify("skipped", compaction_id, reason="nothing_to_compact")
            return unchanged
        # Required gain: a compaction that does not take at least 10% off the
        # request is not worth a summarizer call, a durable rewrite and a
        # cache miss. Checked first against an optimistic summary size, so a
        # hopeless attempt never reaches the summarizer, and again on the
        # real summary below.
        min_gain = max(1, int(estimated_tokens * _MIN_GAIN_RATIO)) if not force else 1
        folded_tokens = message_tokens(to_fold)
        optimistic_summary = min(_OPTIMISTIC_SUMMARY_TOKENS, folded_tokens // 4)
        best_case = message_tokens(messages) - message_tokens(kept_current) - optimistic_summary
        if best_case < min_gain:
            return self._decline(
                unchanged,
                compaction_id,
                "not_worth_compacting",
                tokens_before=estimated_tokens,
                best_case_gain_tokens=max(0, best_case),
                context_window_tokens=window_tokens,
            )

        self._notify(
            "started",
            compaction_id,
            tokens_before=estimated_tokens,
            context_window_tokens=window_tokens,
        )
        try:
            summary, source = await self._summarize(to_fold, previous_summary, window_tokens)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced as a failed compaction
            log.warning("compaction.failed", error=str(exc), session_key=self._session_key)
            self._notify(
                "failed",
                compaction_id,
                reason="compaction_failed",
                message=str(exc),
                tokens_before=estimated_tokens,
                context_window_tokens=window_tokens,
            )
            return CompactionResult(
                status="failed",
                messages=messages,
                turn_input_index=turn_input_index,
                reason="compaction_failed",
                compaction_id=compaction_id,
                tokens_before=estimated_tokens,
            )
        # A file block in the model's own text is dropped: only the engine's
        # record of the compacted tool calls says which files were touched.
        summary, _ = split_file_activity(summary)
        if not summary.strip():
            self._notify(
                "failed",
                compaction_id,
                reason="empty_summary_rejected",
                tokens_before=estimated_tokens,
                context_window_tokens=window_tokens,
            )
            return CompactionResult(
                status="failed",
                messages=messages,
                turn_input_index=turn_input_index,
                reason="empty_summary_rejected",
                compaction_id=compaction_id,
                tokens_before=estimated_tokens,
            )

        file_activity = merge_file_activity(previous_files, file_activity_from_messages(to_fold))
        summary = attach_file_activity(summary, file_activity)
        compacted = [*summary_messages(summary), *kept_current]
        tokens_after = overhead + message_tokens(compacted)
        if message_tokens(messages) - message_tokens(compacted) < min_gain:
            # The summary (an LLM answer or the fallback digest) did not buy
            # enough room: keep the transcript, persist nothing, send as is.
            return self._decline(
                unchanged,
                compaction_id,
                "compaction_not_smaller",
                tokens_before=estimated_tokens,
                tokens_after=tokens_after,
                summary_source=source,
                context_window_tokens=window_tokens,
            )
        removed_count = len(to_fold) + (2 if previous_summary is not None else 0)
        result = CompactionResult(
            status="compacted",
            messages=compacted,
            turn_input_index=2,
            summary=summary,
            summary_source=source,
            compaction_id=compaction_id,
            removed_count=removed_count,
            kept_count=len(kept_current),
            folded_rounds=folded_rounds,
            tokens_before=estimated_tokens,
            tokens_after=tokens_after,
            kept_entries=[dict(input_entry)] if input_entry else [],
            file_activity=None if file_activity.is_empty else file_activity,
        )
        for event in (COMPACTION_CHUNK_SUMMARIZED_EVENT, COMPACTION_SUMMARY_VERIFIED_EVENT):
            if not self._session_key:
                break
            notify_compaction(
                self._session_key,
                source="automatic",
                phase=self._phase,
                status="observed",
                context_window_tokens=window_tokens,
                tokens_before=estimated_tokens,
                tokens_after=tokens_after,
                removed_count=removed_count,
                kept_count=len(kept_current),
                summary_len=len(summary),
                summary_source=source,
                **compaction_effect_payload(status="observed"),
                **compaction_lifecycle_payload(compaction_id, event),
            )
        log.info(
            "compaction.compacted",
            session_key=self._session_key,
            removed=removed_count,
            kept=len(kept_current),
            folded_rounds=folded_rounds,
            summary_source=source,
            tokens_before=estimated_tokens,
            tokens_after=tokens_after,
            files_read=len(file_activity.read),
            files_modified=len(file_activity.modified),
        )
        return result
