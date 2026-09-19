"""Prompt-injection defense primitives.

Ingress points:

* :func:`wrap_untrusted` — wrap tool output, file read, or channel
  inbound content in ``<untrusted source='...'>...</untrusted>`` before
  it enters the LLM context. Inner payload is XML-escaped so attempts to
  close the tag or inject sibling ``<system>`` / ``<available_skills>``
  elements fall through as inert entities.
* :func:`wrap_untrusted_boundary` — same envelope for bulk external
  content (web pages, HTTP bodies) the model must read verbatim: only
  nested ``<untrusted``/``</untrusted>`` markers are escaped, the rest
  of the payload passes through unmodified.
* :func:`xml_escape` — public escaping helper reused by
  :mod:`agentos.skills.filter` when assembling ``<available_skills>`` from
  skill metadata.
* :func:`is_untrusted_fragment` — structural check: does the given text
  contain a well-formed ``<untrusted ...>...</untrusted>`` envelope?
* :func:`extract_tool_call_refusal_reason` — ingress-path enforcement:
  when the engine is about to execute a tool call, inspect the origin
  trace; if it lies inside an untrusted block, return the structured
  refusal reason ``tool_call_inside_untrusted`` (else ``None``).

Invariants:

* ``wrap_untrusted`` is pure and allocates no locks, sockets, files.
* ``extract_tool_call_refusal_reason`` never raises — it returns a
  string reason or ``None``.
* The ``REFUSAL_REASON_TOOL_CALL_IN_UNTRUSTED`` constant is the single
  source of truth for the refusal reason string.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Final

REFUSAL_REASON_TOOL_CALL_IN_UNTRUSTED: Final[str] = "tool_call_inside_untrusted"

_UNTRUSTED_OPEN = re.compile(
    r"<untrusted(\s+source=['\"][^'\"<>]*['\"])?\s*>",
    re.IGNORECASE,
)
_UNTRUSTED_CLOSE = re.compile(r"</untrusted\s*>", re.IGNORECASE)
_UNTRUSTED_PAIR = re.compile(
    r"<untrusted(?:\s+source=['\"][^'\"<>]*['\"])?\s*>.*?</untrusted\s*>",
    re.IGNORECASE | re.DOTALL,
)

# Structural signals embedded in content are markers for the tool-execution
# ingress refusal. These are deliberately generic — the goal is "if an LLM
# generates a tool_use whose origin trace lies inside untrusted content,
# refuse". Callers pass the origin trace (context around the tool call).
_TOOL_CALL_MARKERS: Final[tuple[str, ...]] = (
    "<tool_use",
    "<tool_call",
    "<function_call",
    '"tool":',
    '"function":',
)


# ---------------------------------------------------------------------------
# Classified injection patterns
# ---------------------------------------------------------------------------
#
# INJECTION_PATTERNS covers four threat classes distilled from public
# prompt-injection corpora (Simon Willison's taxonomy, GARAK benchmark,
# Anthropic red-team reports):
#
# * prompt_override — the attacker instructs the model to disregard the
#   system prompt, reset the conversation, or adopt a "new policy".
# * role_hijack — the attacker claims elevated identity (system, admin,
#   supervisor, operator) to trick the model into granting scope that
#   belongs to higher trust tiers.
# * exfiltration — the attacker coerces the model into leaking secrets,
#   credentials, keys, or the system prompt itself to an outbound channel.
# * invisible_char — the attacker smuggles zero-width, bidi, or
#   right-to-left-override characters to hide malicious content from
#   human reviewers while the tokenizer sees the full payload.
#
# Each pattern is written with ``re.IGNORECASE`` unless case is
# load-bearing. Patterns are deliberately anchored on *intent phrases*
# (verb + object) rather than single tokens so benign chat messages
# mentioning the word "ignore" or "system" do not false-positive.

_PROMPT_OVERRIDE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(
        r"ignore\s+(all\s+)?(prior|previous|preceding|above)"
        r"\s+(instructions?|rules?|prompts?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(disregard|forget|void|discard)\s+(all\s+|the\s+)?(system\s+)?"
        r"(prompt|policy|rules?|instructions?)",
        re.IGNORECASE,
    ),
    re.compile(r"(system\s+)?override\s*[:#>*-]+(\s*\w+)?", re.IGNORECASE),
    re.compile(
        r"(new|reset|fresh)\s+(system\s+)?"
        r"(prompt|conversation|context|instructions?)\s*[:=]",
        re.IGNORECASE,
    ),
    re.compile(
        r"from\s+now\s+on[,\s]+(?:you\s+are\s+|act\s+as\s+|behave\s+as\s+)",
        re.IGNORECASE,
    ),
)

_ROLE_HIJACK_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(
        r"\b(user|caller|you)\s+(is|are)\s+(now\s+)?"
        r"(root|admin|supervisor|operator|sudoer)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\[(system|assistant|operator|admin|root)\][:>\s]", re.IGNORECASE),
    re.compile(
        r"pretend\s+(you\s+are|to\s+be)\s+(the\s+)?"
        r"(gateway|admin|root|operator|supervisor|system)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"---\s*begin\s+(impersonation|role\s*swap|role\s*change)",
        re.IGNORECASE,
    ),
)

_EXFILTRATION_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(
        r"(dump|leak|print|show|reveal|expose|exfiltrate)\s+(the\s+)?"
        r"(system\s+prompt|secrets?|api[_\s-]?keys?|credentials?"
        r"|env(ironment)?\s+vars?|\.env)",
        re.IGNORECASE,
    ),
    re.compile(
        r"send\s+(the\s+)?(secrets?|keys?|credentials?|system\s+prompt|conversation)"
        r"\s+to\s+https?://",
        re.IGNORECASE,
    ),
    re.compile(
        r"(curl|wget|fetch)\s+https?://[\w.-]+/[\w\-/?=&]*\?[\w=&]*"
        r"(secret|key|token|password)",
        re.IGNORECASE,
    ),
)

# Invisible codepoints that can be used to split an intent phrase past the
# regexes above. Every one of them is normalized to a space before the
# text patterns run (#690), so this is the *widest* set.
#
# Categories covered:
#   U+00AD      SOFT HYPHEN
#   U+200B-F    zero-width space / ZWNJ / ZWJ / bidi marks
#   U+202A-E    bidi markers (LTR/RTL override, pop directional)
#   U+2060-4    word joiner, function application, invisible operators
#   U+2066-9    bidi isolates (first strong, pop directional)
#   U+FEFF      BOM / zero-width no-break space
_INVISIBLE_CODEPOINTS_RE: Final[re.Pattern[str]] = re.compile(
    "[\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]"
)

# The ``invisible_char`` threat class is the subset that has no business in
# ordinary text. Two joiners are left out because they build legitimate
# content and, in enforce mode, one of them used to blank the whole payload
# (#2120): ZWJ (U+200D) glues every compound emoji — family, profession,
# skin-tone and flag sequences — and ZWNJ (U+200C) shapes Persian, Arabic
# and Indic words. A leading BOM (U+FEFF) is what every UTF-8 file that has
# been through Excel or Notepad starts with, so it is stripped before this
# class is matched; a BOM anywhere else is still a smuggling signal. All
# three still go through the normalization pass above, so they cannot be
# used to split an intent phrase — the #690 bypass stays closed.
_INVISIBLE_CHAR_THREAT_RE: Final[re.Pattern[str]] = re.compile(
    "[\u00ad\u200b\u200e\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]"
)

_INVISIBLE_CHAR_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (_INVISIBLE_CHAR_THREAT_RE,)

INJECTION_PATTERNS: Final[dict[str, tuple[re.Pattern[str], ...]]] = {
    "prompt_override": _PROMPT_OVERRIDE_PATTERNS,
    "role_hijack": _ROLE_HIJACK_PATTERNS,
    "exfiltration": _EXFILTRATION_PATTERNS,
    "invisible_char": _INVISIBLE_CHAR_PATTERNS,
}


@dataclass(frozen=True)
class InjectionFinding:
    """One report-only or enforce-mode prompt-injection scan hit."""

    source: str
    threat_class: str
    mode: str
    ts: str

    def asdict(self) -> dict[str, str]:
        return asdict(self)


def classify_injection(text: str) -> list[str]:
    """Return sorted labels of threat classes whose regex matched ``text``.

    Empty list means the text did not trip any :data:`INJECTION_PATTERNS`
    regex — callers can treat an empty result as "structurally benign"
    (subject to the usual caveat that regex defense is a blunt first
    line, not the only line — see :func:`wrap_untrusted` for the
    structural envelope).

    Invisible characters (soft hyphens, word joiners, zero-width spaces,
    bidi markers, etc.) that split intent phrases are normalized to a
    space before matching *non*-invisible-character patterns, so
    ``ignore\u00adall prior instructions`` is caught as
    ``prompt_override``. The ``invisible_char`` class is matched against
    the **original** text so the smuggling technique itself is reported —
    minus a leading BOM and the ZWJ/ZWNJ joiners that legitimate emoji and
    scripts are built from (see ``_INVISIBLE_CHAR_THREAT_RE``).
    """

    if not text:
        return []

    # Normalize invisible codepoints to space so they don't break
    # word-boundary patterns — see _INVISIBLE_CODEPOINTS_RE.
    normalized = _INVISIBLE_CODEPOINTS_RE.sub(" ", text)
    original = text.removeprefix("\ufeff")

    hits: set[str] = set()
    for threat_class, patterns in INJECTION_PATTERNS.items():
        search_text = normalized if threat_class != "invisible_char" else original
        for pattern in patterns:
            if pattern.search(search_text):
                hits.add(threat_class)
                break
    return sorted(hits)


def scan_for_injection(
    content: str,
    source: str,
    *,
    mode: str = "report",
) -> tuple[str, list[InjectionFinding]]:
    """Scan untrusted prompt content and optionally sanitize enforce-mode hits.

    ``report`` mode never changes the content. ``enforce`` mode replaces any
    matched content with a compact blocked marker. ``off`` mode performs no
    scanning and returns the original content.
    """

    normalized_mode = mode if mode in {"off", "report", "enforce"} else "report"
    if normalized_mode == "off" or not content:
        return content, []

    threat_classes = classify_injection(content)
    if not threat_classes:
        return content, []

    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    findings = [
        InjectionFinding(
            source=source,
            threat_class=threat_class,
            mode=normalized_mode,
            ts=ts,
        )
        for threat_class in threat_classes
    ]
    if normalized_mode == "enforce":
        return f"[BLOCKED: unsafe prompt content removed from {source}]", findings
    return content, findings


def xml_escape(text: str) -> str:
    """Escape the five XML characters: ``&``, ``<``, ``>``, ``"``, ``'``.

    This is the public helper :mod:`agentos.skills.filter` uses when
    building ``<available_skills>``. It is deliberately conservative:
    ``&`` is escaped first to avoid double-escaping the entity
    references introduced by the later substitutions.
    """

    if not isinstance(text, str):
        text = str(text)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def wrap_untrusted(content: str, source: str) -> str:
    """Wrap ``content`` in ``<untrusted source='...'>...</untrusted>``.

    Both the ``source`` attribute value and the inner ``content`` are
    XML-escaped so closing-tag injection, CDATA bypass, and attribute
    escape attempts become inert entities. Whitespace around the
    envelope is intentionally preserved so the wrapper is
    concatenation-safe in prompt assembly.
    """

    escaped_source = xml_escape(source)
    escaped_content = xml_escape(content)
    return f"<untrusted source='{escaped_source}'>{escaped_content}</untrusted>"


# A close tag is matched on the ``untrusted`` word boundary rather than on a
# following ``>``: an end tag may carry anything up to the terminator, and both
# HTML parsers and a model reading the assembled prompt read
# ``</untrusted foo>`` as closing the block. Requiring ``\s*>`` let those
# spellings through verbatim. The second pattern catches an unterminated
# ``</untrusted``, which the first cannot match for want of a ``>``.
_UNTRUSTED_CLOSE_IN_CONTENT = re.compile(r"<\s*/\s*untrusted\b[^>]*>", re.IGNORECASE)
_UNTRUSTED_CLOSE_UNTERMINATED = re.compile(r"<\s*/\s*untrusted\b", re.IGNORECASE)
_UNTRUSTED_OPEN_IN_CONTENT = re.compile(r"<\s*untrusted\b", re.IGNORECASE)


def _escape_tag_angles(match: re.Match[str]) -> str:
    """Entity-escape a matched tag's own angle brackets, keeping its interior."""
    return f"&lt;{match.group(0)[1:-1]}&gt;"


def wrap_untrusted_boundary(content: str, source: str) -> str:
    """Wrap bulk external content in the untrusted envelope, readably.

    Unlike :func:`wrap_untrusted`, only the envelope boundaries are
    neutralized: nested ``<untrusted``/``</untrusted>`` markers in the
    payload are entity-escaped so the envelope cannot be closed early or
    forged, while the rest of the content (markdown, HTML entities, code)
    passes through verbatim for the model to read. Use this for
    full-document ingest such as web pages and HTTP bodies, and
    :func:`wrap_untrusted` for short config-injected text where full XML
    escaping costs nothing.

    The close tag is escaped before the open tag: the close pattern also
    contains the ``untrusted`` token, so the reverse order would corrupt
    close markers into half-escaped fragments.
    """

    escaped_source = xml_escape(source)
    safe = _UNTRUSTED_CLOSE_IN_CONTENT.sub(_escape_tag_angles, content)
    safe = _UNTRUSTED_CLOSE_UNTERMINATED.sub("&lt;/untrusted", safe)
    safe = _UNTRUSTED_OPEN_IN_CONTENT.sub("&lt;untrusted", safe)
    return f"<untrusted source='{escaped_source}'>{safe}</untrusted>"


def is_untrusted_fragment(text: str) -> bool:
    """Return ``True`` iff ``text`` contains a matched untrusted envelope.

    Matching is order-aware: the open tag must precede the close tag
    and both must be present. This is a structural check, not a
    security decision — callers should still pass the fragment through
    :func:`extract_tool_call_refusal_reason` before acting on it.
    """

    if not text:
        return False
    return bool(_UNTRUSTED_PAIR.search(text))


def extract_tool_call_refusal_reason(text: str) -> str | None:
    """Return a structured refusal reason when a tool call is embedded in
    untrusted content; otherwise ``None``.

    The caller is expected to pass the *origin trace* of the tool call —
    typically the assistant message chunk that produced the tool_use
    block, enriched with surrounding context. If that trace contains a
    tool-call marker inside an ``<untrusted>...</untrusted>`` span, the
    refusal is returned.

    Return value is a plain string so the engine can wrap it in its own
    structured ``{status: 'refused', reason: ...}`` payload without
    dragging a dataclass import into the agent loop.
    """

    if not text or not is_untrusted_fragment(text):
        return None
    for match in _UNTRUSTED_PAIR.finditer(text):
        inside = match.group(0)
        lowered = inside.lower()
        for marker in _TOOL_CALL_MARKERS:
            if marker.lower() in lowered:
                return REFUSAL_REASON_TOOL_CALL_IN_UNTRUSTED
    return None


__all__ = [
    "INJECTION_PATTERNS",
    "InjectionFinding",
    "REFUSAL_REASON_TOOL_CALL_IN_UNTRUSTED",
    "classify_injection",
    "extract_tool_call_refusal_reason",
    "is_untrusted_fragment",
    "scan_for_injection",
    "wrap_untrusted",
    "wrap_untrusted_boundary",
    "xml_escape",
]
