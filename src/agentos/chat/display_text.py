"""Display strip for assistant text, shared by every surface that shows it.

A behavioural port of the web console's assistant-text sanitiser
(``frontend/src/views/chat/logic.ts``: ``stripDirectiveTags``,
``stripGeneratedArtifactMarkers``, ``stripProtocolTextLeak`` and
``stripAssistantText``) so the web console, the desktop app and channel
deliveries hide the same transport details: reply-threading directives,
generated-artifact omission markers and tool-scaffold text a model leaked into
its prose. Keep the two in lockstep; ``tests/test_chat/test_display_text.py``
carries the web console's cases.

The whole-text functions reproduce the JavaScript regex semantics, not
Python's: ``\\s``, ``trim()`` and ``trimEnd()`` use the ECMAScript whitespace
set, and ``\\b`` / the ``i`` flag are ASCII-only (a non-``u`` JS regex), so
every pattern is compiled with ``re.ASCII`` and an explicit whitespace class.

Tool-call markup a model wrote as text (``<invoke>``, ``<tool_call>``, ...)
is deliberately not hidden here. The engine cuts the calls it ran from the
reply (``agentos.engine.tool_text_compat``) and leaves every other call
visible, so whatever markup reaches this layer never ran and must stay on
screen. Only scaffolding that is not a call format is trimmed.

:class:`AssistantTextStreamSanitizer` applies the same strip to a stream of
``text_delta`` chunks, holding back only the suffix a later chunk could still
turn into something the strip removes.
"""

from __future__ import annotations

import enum
import re

# ECMAScript WhiteSpace + LineTerminator, i.e. what JS ``\s``, ``trim()`` and
# ``trimEnd()`` match. Python's ``\s`` / ``str.strip()`` also take U+001C-U+001F
# and U+0085 but not U+FEFF, so the JS set is spelled out.
_JS_WS_CHARS = (
    "\t\n\x0b\x0c\r \xa0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000\ufeff"
)
_WS = "[" + "".join(f"\\u{ord(char):04x}" for char in _JS_WS_CHARS) + "]"
_JS_WORD_BOUNDARY = r"\b"  # with re.ASCII this is JS's [A-Za-z0-9_] boundary
# Mid-stream a ``\b`` at the end of the text so far is only tentative: the next
# chunk may continue the word. The streaming checks demand a real non-word char.
_STREAM_WORD_BOUNDARY = r"(?=[^A-Za-z0-9_])"
_FLAGS = re.ASCII
_FLAGS_I = re.ASCII | re.IGNORECASE


def _js(source: str, flags: int, *, boundary: str = _JS_WORD_BOUNDARY) -> re.Pattern[str]:
    """Compile a pattern copied verbatim from logic.ts with JS semantics.

    Only ``\\s`` (never used inside a character class here) and ``\\b`` need
    translating; everything else in these patterns means the same in both.
    """
    return re.compile(source.replace("\\b", boundary).replace("\\s", _WS), flags)


def _prefixes(word: str) -> str:
    """Alternation matching every prefix of ``word``, the empty one included."""
    return "(?:" + "|".join(re.escape(word[:end]) for end in range(len(word), -1, -1)) + ")"


_DIRECTIVE_TAG_RE = _js(r"\[\[\s*(?:reply_to_current|reply_to\s*:\s*[^\]\n]+)\s*\]\]\s*", _FLAGS)
# logic.ts matches ``(?:^|\s*)\[generated artifact omitted:\s*[^\]\n]+?\]\s*``.
# Retrying that leading ``\s*`` at every position is quadratic on a long
# whitespace run (in both engines), so only the marker itself is matched here
# and the whitespace run before it is walked back over -- the same removals.
_GENERATED_ARTIFACT_MARKER_RE = _js(r"\[generated artifact omitted:\s*[^\]\n]+?\]\s*", _FLAGS_I)
# stripGeneratedArtifactMarkers only runs when this exact (case-sensitive)
# text is present, even though the regex itself is case-insensitive.
_GENERATED_ARTIFACT_GUARD = "[generated artifact omitted:"
_PROTOCOL_TEXT_MARKER = r"<\s*(?:effect_calls\b|details\b|angle\s+brackets\b)"
_PROTOCOL_TEXT_MARKER_RE = _js(_PROTOCOL_TEXT_MARKER, _FLAGS_I)

# looksLikeProtocolTextSuffix's patterns, keyed so the stream can remember
# which ones it has already seen in a growing hold.
_PROTOCOL_TEXT_PATTERNS = {
    "standalone": r"<\s*(?:effect_calls|angle\s+brackets)\s*>",
    "details": r"<\s*details\s*>\s*<\s*summary\s*>\s*View areas around line\b",
}
# A suffix is a leak when every pattern of any one rule is present.
_PROTOCOL_LEAK_RULES: tuple[tuple[str, ...], ...] = (
    ("standalone",),
    ("details",),
)
_PROTOCOL_CHECKS = {key: _js(source, _FLAGS_I) for key, source in _PROTOCOL_TEXT_PATTERNS.items()}
_STREAM_PROTOCOL_CHECKS = {
    key: _js(source, _FLAGS_I, boundary=_STREAM_WORD_BOUNDARY)
    for key, source in _PROTOCOL_TEXT_PATTERNS.items()
}
_STREAM_PROTOCOL_TEXT_MARKER_RE = _js(
    _PROTOCOL_TEXT_MARKER, _FLAGS_I, boundary=_STREAM_WORD_BOUNDARY
)

# Prefixes (used with fullmatch) of text the strip could still remove once
# more chunks arrive: a directive, an omission marker, a protocol marker.
_DIRECTIVE_PREFIX_RE = re.compile(
    rf"\[(?:\[{_WS}*(?:{_prefixes('reply_to_current')}"
    rf"|reply_to_current{_WS}*\]?"
    rf"|reply_to{_WS}*(?::{_WS}*(?:[^\]\n]+{_WS}*\]?)?)?))?",
    _FLAGS,
)
_GENERATED_ARTIFACT_PREFIX_RE = re.compile(
    rf"\[(?:{_prefixes('generated artifact omitted')}"
    rf"|generated artifact omitted:{_WS}*[^\]\n]*)",
    _FLAGS_I,
)
_PROTOCOL_MARKER_KEYWORDS = (
    "effect_calls",
    "details",
    "angle",
)
_PROTOCOL_MARKER_PREFIX_RE = re.compile(
    rf"<{_WS}*(?:"
    + "|".join(_prefixes(keyword) for keyword in _PROTOCOL_MARKER_KEYWORDS)
    + rf"|angle{_WS}+{_prefixes('brackets')})",
    _FLAGS_I,
)

_DIRECTIVE_HOLD_LIMIT = 256
_GENERATED_ARTIFACT_HOLD_LIMIT = 512
_PROTOCOL_MARKER_HOLD_LIMIT = 256
_PROTOCOL_HOLD_LIMIT = 64 * 1024
# How far back a protocol check rescans a growing hold, so a pattern split
# across chunks is still seen without rescanning the whole hold every chunk.
# A longer split pattern is still caught by the exact check at flush.
_PROTOCOL_RESCAN_WINDOW = 1024


def strip_directive_tags(text: str | None) -> str:
    """Strip reply-threading directives (``[[reply_to_current]]``) for display."""
    return _DIRECTIVE_TAG_RE.sub("", text or "").lstrip("\n")


def strip_generated_artifact_markers(text: str | None) -> str:
    """Remove generated-artifact omission markers and normalise whitespace."""
    value = text or ""
    if _GENERATED_ARTIFACT_GUARD not in value:
        return value
    value = value.replace("\r\n", "\n")
    value, _, _ = _cut_spans(value, _generated_artifact_marker_spans(value), 0)
    value = re.sub(r"[ \t]{2,}", " ", value)
    return re.sub(r"\n{3,}", "\n\n", value).strip(_JS_WS_CHARS)


def looks_like_protocol_text_suffix(suffix: str) -> bool:
    """True when ``suffix`` positively matches a tool-protocol leak."""
    return _is_protocol_leak(
        {key for key, check in _PROTOCOL_CHECKS.items() if check.search(suffix)}
    )


def strip_protocol_text_leak(text: str | None) -> str:
    """Trim a suffix that positively matches a tool-protocol leak.

    Only the first protocol marker counts, and only when the text from it to
    the end looks like a leak; ordinary angle-bracket prose is left alone.
    """
    value = text or ""
    match = _PROTOCOL_TEXT_MARKER_RE.search(value)
    if match is None or not looks_like_protocol_text_suffix(value[match.start() :]):
        return value
    return value[: match.start()].rstrip(_JS_WS_CHARS)


def strip_assistant_text(text: str | None) -> str:
    """The one display strip for assistant text.

    Generated-artifact omission markers, then reply directives, then any
    tool-protocol leak -- the same order as the web console.
    """
    return strip_protocol_text_leak(
        strip_directive_tags(strip_generated_artifact_markers(text or ""))
    )


def _is_protocol_leak(found: set[str]) -> bool:
    return any(all(key in found for key in rule) for rule in _PROTOCOL_LEAK_RULES)


def _directive_spans(text: str) -> list[tuple[int, int]]:
    return [match.span() for match in _DIRECTIVE_TAG_RE.finditer(text)]


def _generated_artifact_marker_spans(text: str) -> list[tuple[int, int]]:
    """Spans logic.ts's GENERATED_ARTIFACT_MARKER_RE removes from ``text``."""
    spans: list[tuple[int, int]] = []
    previous_end = 0
    for match in _GENERATED_ARTIFACT_MARKER_RE.finditer(text):
        start = _whitespace_run_start(text, match.start(), len(text), previous_end)
        spans.append((start, match.end()))
        previous_end = match.end()
    return spans


def _whitespace_run_start(text: str, end: int, limit: int, floor: int = 0) -> int:
    """Start of the whitespace run ending at ``end``, within ``limit`` and ``floor``."""
    floor = max(floor, end - limit)
    return floor + len(text[floor:end].rstrip(_JS_WS_CHARS))


def _cut_spans(text: str, spans: list[tuple[int, int]], floor: int) -> tuple[str, int, bool]:
    """Cut ``spans`` (ordered, disjoint) out of ``text``, tracking cut points.

    Returns the text, ``floor`` carried into its coordinates and raised to the
    last cut point, and whether the last span ran to the end of ``text``.
    """
    pieces: list[str] = []
    position = 0  # end of the last span, in ``text``
    kept = 0  # length of the result so far, i.e. the last cut point
    removed_before_floor = 0
    matched = False
    for start, end in spans:
        removed_before_floor += max(0, min(end, floor) - start)
        pieces.append(text[position:start])
        kept += start - position
        position = end
        matched = True
    pieces.append(text[position:])
    reached_end = matched and position == len(text)
    return "".join(pieces), max(floor - removed_before_floor, kept), reached_end


def _tag_hold_start(text: str, floor: int) -> int:
    """Where the suffix a later chunk could complete into a tag begins.

    That is the earliest ``[`` whose suffix is still a directive or omission
    marker prefix (within their hold limits), plus the whitespace before it,
    which the marker pattern consumes. With no such ``[`` the trailing
    whitespace run is held alone, for the same reason. The held suffix is
    re-read as raw text next time, so it never reaches back past ``floor``,
    the last point where a tag was removed from ``text``.
    """
    start = len(text)
    candidate = text.find("[", max(floor, len(text) - _GENERATED_ARTIFACT_HOLD_LIMIT))
    while candidate != -1:
        if (
            len(text) - candidate <= _DIRECTIVE_HOLD_LIMIT
            and _DIRECTIVE_PREFIX_RE.fullmatch(text, candidate)
        ) or _GENERATED_ARTIFACT_PREFIX_RE.fullmatch(text, candidate):
            start = candidate
            break
        candidate = text.find("[", candidate + 1)
    return _whitespace_run_start(text, start, _GENERATED_ARTIFACT_HOLD_LIMIT, floor)


def _protocol_marker_hold_start(text: str) -> int:
    """Where a trailing, not yet complete scaffold marker (``<det``) begins."""
    candidate = text.find("<", max(0, len(text) - _PROTOCOL_MARKER_HOLD_LIMIT))
    while candidate != -1:
        if _PROTOCOL_MARKER_PREFIX_RE.fullmatch(text, candidate):
            return _whitespace_run_start(text, candidate, _PROTOCOL_MARKER_HOLD_LIMIT)
        candidate = text.find("<", candidate + 1)
    return len(text)


class _ProtocolMode(enum.Enum):
    SCAN = "scan"  # no protocol marker yet
    HOLD = "hold"  # a marker arrived; holding until it is confirmed or cleared
    DISCARD = "discard"  # confirmed leak; the rest of the segment is dropped
    PASS = "pass"  # the hold outgrew its limit and was released


class AssistantTextStreamSanitizer:
    """Streaming :func:`strip_assistant_text` for one text segment.

    Feed the ``text_delta`` chunks of one segment (the text between two tool
    calls) to :meth:`feed` and emit what it returns; call :meth:`flush` at the
    segment's end (before a tool event, at stream end) and emit that too.
    Joined, the output equals ``strip_assistant_text(segment)`` up to
    whitespace: the whole-text whitespace normalisation is not applied.

    Held back between chunks:

    * a suffix that could still become a ``[[reply_to...]]`` directive (up to
      256 chars) or a ``[generated artifact omitted: ...]`` marker (up to
      512), and the whitespace run before it;
    * the trailing whitespace run, since a marker's pattern also eats the
      whitespace in front of it;
    * once a scaffold marker such as ``<details`` appears, everything from it
      on: dropped for good when it turns out to be a leak, released at flush
      (or past 64 KiB) when it does not.

    Past a hold limit the text is released as it is. One gap the stream
    cannot close without seeing ahead: the whole-text strip removes markers
    in any letter case once the lowercase ``[generated artifact omitted:``
    appears anywhere, so a differently cased marker that precedes the first
    lowercase one is removed there but kept here.
    """

    def __init__(self) -> None:
        self._reset()

    def _reset(self) -> None:
        # Tag stage: unresolved raw suffix and the omission-marker guard.
        self._tag_pending = ""
        self._eat_whitespace = False
        self._seen_marker_guard = False
        # Leading newlines are dropped until real text arrives.
        self._at_segment_start = True
        # Protocol stage.
        self._mode = _ProtocolMode.SCAN
        self._marker_pending = ""
        self._held: list[str] = []
        self._held_size = 0
        self._held_window = ""  # the hold's tail, rescanned with each chunk
        self._held_found: set[str] = set()

    def feed(self, chunk: str | None) -> str:
        """Take the next chunk; return the text that is safe to emit now."""
        if not chunk or self._mode is _ProtocolMode.DISCARD:
            return ""
        text = self._drop_leading_newlines(self._strip_tags(chunk))
        return self._guard_protocol(text, final=False)

    def flush(self) -> str:
        """End the segment: return held text that proved legitimate, then reset."""
        try:
            if self._mode is _ProtocolMode.DISCARD:
                return ""
            # Whatever the tag stage still holds never completed, so it stays.
            text = self._drop_leading_newlines(self._tag_pending)
            return self._guard_protocol(text, final=True)
        finally:
            self._reset()

    def _strip_tags(self, chunk: str) -> str:
        if self._eat_whitespace:
            # The tag removed last time ended the buffer; its trailing
            # whitespace pattern would have taken this whitespace too.
            chunk = chunk.lstrip(_JS_WS_CHARS)
            if not chunk:
                return ""
            self._eat_whitespace = False
        text = self._tag_pending + chunk
        if _GENERATED_ARTIFACT_GUARD in text:
            self._seen_marker_guard = True
        # Markers first, then directives: the whole-text order.
        floor, marker_reached_end = 0, False
        if self._seen_marker_guard:
            text, floor, marker_reached_end = _cut_spans(
                text, _generated_artifact_marker_spans(text), floor
            )
        text, floor, directive_reached_end = _cut_spans(text, _directive_spans(text), floor)
        self._eat_whitespace = marker_reached_end or directive_reached_end
        hold = _tag_hold_start(text, floor)
        self._tag_pending = text[hold:]
        return text[:hold]

    def _drop_leading_newlines(self, text: str) -> str:
        if self._at_segment_start and text:
            text = text.lstrip("\n")
            self._at_segment_start = not text
        return text

    def _guard_protocol(self, text: str, *, final: bool) -> str:
        if self._mode is _ProtocolMode.PASS:
            return text
        if self._mode is _ProtocolMode.HOLD:
            return self._settle_hold(text, final=final)
        tail = self._marker_pending + text
        self._marker_pending = ""
        if final:
            return strip_protocol_text_leak(tail)
        match = _STREAM_PROTOCOL_TEXT_MARKER_RE.search(tail)
        if match is None:
            hold = _protocol_marker_hold_start(tail)
            self._marker_pending = tail[hold:]
            return tail[:hold]
        # The first marker decides, as in strip_protocol_text_leak: hold
        # everything from it (and the whitespace before it) until it settles.
        hold = _whitespace_run_start(tail, match.start(), _PROTOCOL_HOLD_LIMIT)
        self._mode = _ProtocolMode.HOLD
        return tail[:hold] + self._settle_hold(tail[hold:], final=False)

    def _settle_hold(self, text: str, *, final: bool) -> str:
        """Add ``text`` to the protocol hold; return what it releases, if anything."""
        self._held.append(text)
        self._held_size += len(text)
        if final:
            held = "".join(self._held)
            return "" if looks_like_protocol_text_suffix(held) else held
        # Each check only ever turns on, so only the new text (and enough of
        # the old to catch a pattern split across chunks) needs scanning.
        window = self._held_window + text
        for key, check in _STREAM_PROTOCOL_CHECKS.items():
            if key not in self._held_found and check.search(window):
                self._held_found.add(key)
        # Every pattern starts with "<", so nothing before the first one counts.
        window = window[-_PROTOCOL_RESCAN_WINDOW:]
        self._held_window = window[window.find("<") :] if "<" in window else ""
        if _is_protocol_leak(self._held_found):
            self._mode = _ProtocolMode.DISCARD
            self._held = []
            return ""
        if self._held_size > _PROTOCOL_HOLD_LIMIT:
            released = "".join(self._held)
            self._held = []
            self._mode = _ProtocolMode.PASS
            return released
        return ""
