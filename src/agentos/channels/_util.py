"""Shared channel utilities: deduplication, rate limiting, retry logic.

Also hosts the ``ChannelAccessPolicy`` primitive that adapters declare to
describe their admit/deny semantics. Item-5 adapter adoptions wire the
``policy`` attribute through; future dispatch refactors will consume
``evaluate_policy`` directly.
"""

from __future__ import annotations

import asyncio
import math
import os
import random
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal

import httpx
import structlog

from agentos.channel_pairing import ChannelAdmission

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Channel access policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChannelAccessPolicy:
    """Per-adapter admit/deny declaration consumed by ``evaluate_policy``.

    The fields capture every dimension currently exercised by gateway
    dispatch (DM allow, group allow, mention requirement, sender allowlist)
    plus the ``allowlist`` slot reserved for item-5b/c/d/e per-adapter
    adoption. ``allowlist_enabled`` distinguishes an open policy from a
    strict policy whose allowlist is currently empty.  Without that explicit
    bit an approval-gated channel could not deny its very first sender.
    """

    dm_allowed: bool = True
    group_allowed: bool = True
    mention_required_in_group: bool = True
    allowlist: frozenset[str] = field(default_factory=frozenset)
    allowlist_enabled: bool = False


@dataclass(frozen=True, slots=True)
class AccessDecision:
    """Result of ``evaluate_policy`` — paired with a stable reason code."""

    admit: bool
    reason: Literal[
        "dm_admitted",
        "dm_denied",
        "group_admitted",
        "group_denied",
        "group_not_configured",
        "not_mentioned_in_group",
        "not_in_allowlist",
        "not_paired",
    ]
    admission: ChannelAdmission | None = None
    admission_validator: Callable[[ChannelAdmission], bool] | None = None


def evaluate_policy(
    policy: ChannelAccessPolicy,
    *,
    is_group: bool,
    mentioned: bool,
    sender_id: str = "",
) -> AccessDecision:
    """Evaluate a single inbound message against a channel's access policy.

    Pure function. Adapters provide the policy; dispatch provides the runtime
    inputs (``is_group``, ``mentioned``, ``sender_id``). ``ChannelAccessPolicy``
    instances must be tuned so this evaluator preserves each adapter's access
    baseline when that adapter adopts the shared evaluator.
    """
    if is_group:
        if not policy.group_allowed:
            return AccessDecision(admit=False, reason="group_denied")
        if policy.mention_required_in_group and not mentioned:
            return AccessDecision(admit=False, reason="not_mentioned_in_group")
        if (policy.allowlist_enabled or policy.allowlist) and sender_id not in policy.allowlist:
            return AccessDecision(admit=False, reason="not_in_allowlist")
        return AccessDecision(admit=True, reason="group_admitted")
    if not policy.dm_allowed:
        return AccessDecision(admit=False, reason="dm_denied")
    if (policy.allowlist_enabled or policy.allowlist) and sender_id not in policy.allowlist:
        return AccessDecision(admit=False, reason="not_in_allowlist")
    return AccessDecision(admit=True, reason="dm_admitted")


class EventDedupeCache:
    """Bounded set for deduplicating event IDs."""

    def __init__(self, max_size: int = 10_000) -> None:
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._max_size = max_size

    def check_and_add(self, event_id: str) -> bool:
        """Return True if the event_id is new (not a duplicate)."""
        if event_id in self._seen:
            self._seen.move_to_end(event_id)
            return False
        self._seen[event_id] = None
        if len(self._seen) > self._max_size:
            self._seen.popitem(last=False)
        return True

    def discard(self, event_id: str) -> None:
        """Allow an event whose handling failed to be retried."""
        self._seen.pop(event_id, None)


@dataclass
class RateLimiter:
    """Async token-bucket rate limiter for HTTP API calls."""

    max_tokens: int = 30
    refill_rate: float = 30.0  # tokens per second
    _tokens: float = field(init=False)
    _last_refill: float = field(init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def __post_init__(self) -> None:
        self._tokens = float(self.max_tokens)
        self._last_refill = time.monotonic()

    async def acquire(self) -> None:
        """Wait until a token is available."""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self.max_tokens, self._tokens + elapsed * self.refill_rate)
            self._last_refill = now
            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self.refill_rate
                await asyncio.sleep(wait)
                # Re-read the clock after the sleep and account for the wait
                # here. Leaving ``_last_refill`` at the pre-sleep reading let
                # the next caller refill the same interval a second time,
                # which admitted roughly twice the configured rate.
                now = time.monotonic()
                self._tokens = min(
                    self.max_tokens,
                    self._tokens + (now - self._last_refill) * self.refill_rate,
                )
                self._last_refill = now
                self._tokens = max(0.0, self._tokens - 1.0)
            else:
                self._tokens -= 1.0


# ---------------------------------------------------------------------------
# Streaming resilience helpers (item 4)
# ---------------------------------------------------------------------------
#
# Slack and discord stream chat output by posting an "open" message and then
# editing it with each accumulated chunk. The previous inline implementations
# raced when a fast producer fired two edits concurrently and crashed the
# whole consumer when a single edit raised mid-stream. The two helpers below
# add the minimum-radius safety net: an in-flight serializer with push-back
# semantics, and an adaptive strike counter that flips a circuit when the
# remote keeps returning 429.


@dataclass
class StreamThrottle:
    """Serialize edit calls against an in-flight network round trip.

    Accumulates incoming chunks; ``maybe_flush`` sends the latest snapshot
    via ``post`` (first call) or ``edit`` (subsequent calls). The
    ``asyncio.Lock`` ensures a second flush cannot start while a first is
    awaiting the network. If a send raises, the accumulated text remains
    intact so the next ``maybe_flush`` retries with the same snapshot.
    """

    interval_s: float = 0.5
    _accumulated: str = field(default="", init=False)
    _last_flush: float = field(default=0.0, init=False)
    _opened: bool = field(default=False, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def add(self, text: str) -> None:
        self._accumulated += text

    @property
    def text(self) -> str:
        return self._accumulated

    @property
    def opened(self) -> bool:
        return self._opened

    async def maybe_flush(
        self,
        *,
        post: Callable[[str], Awaitable[Any]],
        edit: Callable[[str], Awaitable[Any]],
    ) -> Any | None:
        """Send the accumulated snapshot if the throttle window has elapsed."""
        if not self._accumulated:
            return None
        now = time.monotonic()
        if self._opened and now - self._last_flush < self.interval_s:
            return None
        async with self._lock:
            text = self._accumulated
            if not self._opened:
                result = await post(text)
                self._opened = True
            else:
                result = await edit(text)
            self._last_flush = time.monotonic()
            return result

    async def force_flush(
        self,
        *,
        post: Callable[[str], Awaitable[Any]],
        edit: Callable[[str], Awaitable[Any]],
    ) -> Any | None:
        """Final flush bypassing the throttle interval — call at end-of-stream."""
        if not self._accumulated:
            return None
        async with self._lock:
            text = self._accumulated
            if not self._opened:
                result = await post(text)
                self._opened = True
            else:
                result = await edit(text)
            self._last_flush = time.monotonic()
            return result


@dataclass
class FloodStrikeBackoff:
    """Sliding-window strike counter that flips a circuit after N 429s.

    Each ``record_429`` appends a strike timestamp; strikes older than
    ``decay_s`` are dropped before counting. Once ``cap`` consecutive
    strikes accumulate within the window, ``should_fallback`` returns True
    and one ``channel.flood_strike_backoff`` log entry is emitted. The
    fallback latch stays True until ``reset`` is called explicitly so the
    streaming consumer cannot oscillate in/out of fallback every chunk.
    """

    cap: int = 3
    decay_s: float = 30.0
    adapter: str = "unknown"
    _strikes: list[float] = field(default_factory=list, init=False)
    _fallback: bool = field(default=False, init=False)

    def record_429(self) -> None:
        now = time.monotonic()
        self._strikes = [t for t in self._strikes if now - t <= self.decay_s]
        self._strikes.append(now)
        if not self._fallback and len(self._strikes) >= self.cap:
            self._fallback = True
            log.warning(
                "channel.flood_strike_backoff",
                adapter=self.adapter,
                strikes=len(self._strikes),
                cap=self.cap,
                decay_s=self.decay_s,
            )

    def record_success(self) -> None:
        """Successful send drops accumulated strikes — does NOT clear fallback."""
        self._strikes.clear()

    def should_fallback(self) -> bool:
        return self._fallback

    def reset(self) -> None:
        """Operator/manual circuit reset."""
        self._strikes.clear()
        self._fallback = False


#: Upper bound on a server-supplied ``Retry-After``. The header is remote input
#: and a provider can legally ask for hours; a channel send parked that long is
#: worse for the caller than one that comes back rate-limited.
MAX_RETRY_AFTER_S = 300.0


def _retry_after_delay(header: str | None, fallback: float) -> float:
    """Resolve a ``Retry-After`` header to a sleep in seconds.

    RFC 7231 §7.1.3 allows either delay-seconds or an HTTP-date, so a bare
    ``float()`` turns a date-formatted rate-limit into a ``ValueError`` inside
    the retry loop. Anything that parses as neither — and any value that is
    negative, non-finite or already in the past — falls back to the caller's
    computed backoff; a usable value is clamped to ``MAX_RETRY_AFTER_S``.
    """
    if header is None:
        return fallback
    raw = header.strip()
    if not raw:
        return fallback
    try:
        delay = float(raw)
    except ValueError:
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return fallback
        # An HTTP-date carries no offset other than GMT (RFC 7231 §7.1.1.1).
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        delay = (when - datetime.now(UTC)).total_seconds()
    if not math.isfinite(delay) or delay < 0.0:
        return fallback
    return min(delay, MAX_RETRY_AFTER_S)


def check_channel_file_size(
    path: str | os.PathLike[str],
    limit_bytes: int,
    adapter_name: str,
) -> int:
    """Check that *path* does not exceed *limit_bytes*.

    Returns the file size in bytes on success.
    Raises ValueError with an actionable message if the file exceeds the ceiling.
    """
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise ValueError(f"Cannot read file size for {path}: {exc}") from exc
    if size > limit_bytes:
        limit_mb = limit_bytes // (1024 * 1024)
        raise ValueError(
            f"File too large: {size:,} bytes exceeds {adapter_name} "
            f"{limit_mb} MB upload ceiling ({limit_bytes:,} bytes)"
        )
    return size


async def retry_request(
    func: Callable[..., Awaitable[httpx.Response]],
    *args: Any,
    max_retries: int = 3,
    base_delay: float = 1.0,
    **kwargs: Any,
) -> httpx.Response:
    """Retry an httpx request with exponential backoff on transient errors."""
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = await func(*args, **kwargs)
            # Guarded like the 5xx branch below: on the final attempt the
            # rate-limited response is handed back instead of being slept on
            # and then discarded by the ``exhausted`` raise.
            if resp.status_code == 429 and attempt < max_retries:
                retry_after = _retry_after_delay(
                    resp.headers.get("Retry-After"), base_delay * (2**attempt)
                )
                log.warning("rate_limited", retry_after=retry_after, attempt=attempt)
                await asyncio.sleep(retry_after)
                continue
            if resp.status_code in {500, 502, 503, 504} and attempt < max_retries:
                delay = base_delay * (2**attempt) + random.random()
                log.warning("transient_error", status=resp.status_code, delay=delay)
                await asyncio.sleep(delay)
                continue
            return resp
        # ``ConnectTimeout``/``WriteTimeout``/``PoolTimeout`` descend from
        # ``TimeoutException``, a sibling of ``ConnectError`` under
        # ``TransportError`` — naming ``ReadTimeout`` alone let a DNS, TLS or
        # pool timeout escape the backoff entirely.
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            last_exc = exc
            if attempt < max_retries:
                delay = base_delay * (2**attempt) + random.random()
                await asyncio.sleep(delay)
                continue
            raise
    raise last_exc or RuntimeError("retry_request exhausted")


# ---------------------------------------------------------------------------
# Message-length chunking
# ---------------------------------------------------------------------------


#: A CommonMark fence line: up to three spaces of indent, then a run of at
#: least three backticks or tildes, then the optional info string.
#:
#: Counting ``"```"`` occurrences instead -- what this used to do -- gets two
#: ordinary cases wrong. A six-backtick fence (used when the code itself
#: contains ```` ``` ````) counts as two, so an unclosed one reads as balanced;
#: and a ``~~~`` fence, the other spelling CommonMark defines, is not seen at
#: all. Both leave the same half-open block in the chunk this guard exists to
#: prevent.
#:
#: A backtick fence's info string may not itself contain a backtick -- that is
#: CommonMark's rule, and without it a line like ``` ```a`b``` ``` (an inline
#: code span) reads as a fence whose "language tag" is most of the line. The
#: tag is carried onto every reopened chunk, so a wrong one duplicates real
#: text into the output. Tilde fences have no such restriction.
_FENCE_LINE_RE = re.compile(
    r"(?m)^ {0,3}(?:(?P<marker>`{3,})(?P<info>[^`\n]*)|(?P<tmarker>~{3,})(?P<tinfo>[^\n]*))$"
)


def _open_fence_at(segment: str, cut: int) -> tuple[int, str] | None:
    """Start and marker of a fence still open at *cut*.

    ``None`` when every fence opened before *cut* was also closed before it.
    A closing fence must use the same character and be at least as long as the
    one it closes, so ```` ``` ```` does not close a ```` `````` ```` block.
    """
    open_start: int | None = None
    open_marker = ""
    for match in _FENCE_LINE_RE.finditer(segment, 0, cut):
        marker = match.group("marker") or match.group("tmarker")
        if open_start is None:
            open_start, open_marker = match.start(), marker
        elif marker[0] == open_marker[0] and len(marker) >= len(open_marker):
            open_start, open_marker = None, ""
    if open_start is None:
        return None
    return open_start, open_marker


def _fence_info_string(segment: str, fence_start: int, marker: str, cut: int) -> str:
    """The opening fence's language tag, when its own line ends before *cut*.

    A bare fence whose line never ends inside the segment has no info string to
    carry: everything up to the next newline is body text, and treating it as a
    language tag would both reopen the block wrongly and make the reopener
    arbitrarily long.

    Only the first word is kept. That is the part CommonMark calls the
    language, and it is the part a renderer uses; carrying the rest would put
    an unbounded string on the front of every chunk after the first.
    """
    line_end = segment.find("\n", fence_start)
    if line_end < 0 or line_end >= cut:
        return ""
    info = segment[fence_start + len(marker) : line_end].strip()
    return info.split()[0] if info else ""


def _open_code_span_at(segment: str, cut: int) -> int | None:
    """Start of an inline code span still open at *cut*, or ``None``.

    A span is the inline sibling of a fence and the same delivery hazard: a
    chunk ending inside ``` ``x`` ``` carries an unclosed entity, and a
    platform that parses Markdown strictly rejects it. Only consulted once the
    fence check has come up empty, so the backtick runs seen here belong to
    spans or to fences that were already balanced before *cut* -- a balanced
    pair nets out either way.

    CommonMark closes a span only with a run of exactly the opening length, so
    ``` `` ``` does not close a single backtick.
    """
    open_start: int | None = None
    open_length = 0
    cursor = 0
    while cursor < cut:
        if segment[cursor] != "`":
            cursor += 1
            continue
        run_end = cursor
        while run_end < len(segment) and segment[run_end] == "`":
            run_end += 1
        run_length = run_end - cursor
        if open_start is None:
            open_start, open_length = cursor, run_length
        elif run_length == open_length:
            open_start, open_length = None, 0
        cursor = run_end
    return open_start


def _close_and_reopen_fence(
    segment: str,
    limit: int,
    length: Callable[[str], int],
    fence_start: int,
    marker: str,
    cut_hint: int,
) -> tuple[str, str] | None:
    """Close the open fence on the head and reopen it on the tail.

    Only for the case where the fence opens the segment, so backing the cut up
    to before it would empty the chunk.

    The cut is a fresh binary search rather than a reuse of the caller's
    word/line-boundary one, and it is bounded below by the end of the opening
    fence line: a cut that landed back inside the marker would hand the next
    call almost the same input and spin. ``None`` means no cut leaves the
    closed head within *limit* while still making progress, and the caller
    falls back to a plain cut -- an unbalanced chunk beats an endless loop.
    """
    info = _fence_info_string(segment, fence_start, marker, cut_hint)
    closer = f"\n{marker}"
    reopener = f"{marker}{info}\n"
    # Just past the opening marker -- never past the info string. A bare fence
    # whose line never ends inside the segment has no info string at all, and
    # counting the body as one would push the floor beyond any cut that fits.
    floor = fence_start + len(marker)
    low, high, cut = floor, len(segment) - 1, floor
    while low <= high:
        mid = (low + high) // 2
        if length(segment[:mid] + closer) <= limit:
            cut = mid
            low = mid + 1
        else:
            high = mid - 1
    if cut <= fence_start or length(segment[:cut] + closer) > limit:
        return None
    # The reopener is prepended to the tail, so a cut that consumes no more
    # than the reopener costs hands the next call a segment no shorter than
    # this one -- the caller loops until the tail is empty, and that never
    # happens. Under a limit that tight, a plain cut is the better trade: the
    # chunk is unbalanced but the message goes out.
    if cut <= len(reopener):
        return None
    tail = segment[cut:]
    if not tail:
        return None
    return segment[:cut] + closer, reopener + tail


def split_text_for_limit(
    segment: str,
    limit: int,
    *,
    measure: Callable[[str], int] | None = None,
) -> tuple[str, str]:
    """Split *segment* into the largest prefix that fits *limit*, plus the rest.

    Shared by every adapter with a platform message-length cap, so a
    truncated final reply isn't traded for a second, independently-drifting
    splitter per channel. ``measure`` overrides what's compared against
    *limit* — a channel that renders markdown to something longer than the
    source text (Telegram's HTML) passes a render-then-``len`` callable;
    plain-text channels (Discord) take the ``len`` default.

    The cut point is found by binary search and then nudged back to the
    nearest line/word boundary so a chunk doesn't end mid-word. A fenced
    code block split mid-fence would leave each half with an unbalanced
    fence — some platforms reject a message whose Markdown entities don't
    parse, turning a length problem into a delivery failure — so when a
    fence is still open at the cut:

    * if a line precedes the fence, the cut backs up to the start of that
      line and the whole block moves to the next chunk;
    * otherwise the fence opens the segment and there is nothing to back up
      to — backing up to zero would emit an empty chunk and no caller's loop
      would ever advance — so the block is closed on this chunk and reopened,
      info string and all, on the next;
    * unless no cut fits the closed chunk within *limit* while still
      shortening the tail, in which case one unbalanced chunk goes out. That
      beats a caller looping forever over a balance that cannot fit.

    An unclosed inline code span at the cut is the same hazard one level
    down, and the cut retreats to the span's start when there is one.

    Fences are matched as lines (up to three spaces of indent), not by
    counting backtick runs anywhere in the text, and a closing fence must use
    the same character at the same length or longer — so a ``` inside a
    `````` block is content, and a `~~~` fence is a fence.
    """
    length = measure if measure is not None else len
    if length(segment) <= limit:
        return segment, ""
    low, high, best = 1, len(segment) - 1, 1
    while low <= high:
        mid = (low + high) // 2
        if length(segment[:mid]) <= limit:
            best = mid
            low = mid + 1
        else:
            high = mid - 1
    cut = best
    for boundary in ("\n", " "):
        found = segment.rfind(boundary, 0, best)
        if found >= best // 2:
            cut = found + 1
            break
    open_fence = _open_fence_at(segment, cut)
    if open_fence is not None:
        fence_start, marker = open_fence
        newline_before_fence = segment.rfind("\n", 0, fence_start)
        candidate = newline_before_fence + 1 if newline_before_fence >= 0 else 0
        if candidate > 0:
            cut = candidate
        else:
            # The fence opens the segment itself, so there is no earlier line
            # to back up to -- ``candidate`` is legitimately 0, and the old
            # ``candidate > 0`` guard read that as "nothing to do" and emitted
            # a half-open block (Issue #2127). Backing up to 0 is not an option
            # either: an empty head makes every caller that splits until the
            # tail is empty spin forever. Close the fence on this chunk and
            # reopen it on the next instead.
            rebalanced = _close_and_reopen_fence(segment, limit, length, fence_start, marker, cut)
            if rebalanced is not None:
                return rebalanced
    else:
        # No fence is open, but an inline code span can be: the word-boundary
        # nudge above only fires for a boundary in the second half of the
        # chunk, so a long span starting early is cut straight through. Back
        # up to the span's own start -- it is a plain cut, so nothing is
        # synthesized and nothing can be lost. A span opening the segment has
        # nowhere to retreat to and is left alone rather than emptying the
        # chunk.
        span_start = _open_code_span_at(segment, cut)
        if span_start is not None and span_start > 0:
            cut = span_start
    return segment[:cut], segment[cut:]
