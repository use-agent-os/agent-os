"""Spot a reply that wrote a tool call out as text, so nothing ran.

Some models, instead of emitting a native tool call, end their reply with the
call spelled out: a JSON object in a code fence, ReAct ``Action:`` /
``Action Input:`` lines, an "I'll call ``read_file``" line followed by the
arguments, or tool markup the provider could not turn into a call. The turn
then ends with that text and the user is left waiting for work that never
started.

:func:`detect_text_tool_call` recognises that shape so the engine can retry the
model call once with a nudge. It is deliberately conservative, because a false
positive costs a model call and can push the model into running a tool the user
only asked about:

* the payload must be the *last* thing in the reply -- a call is where a
  turn stops, an example is usually followed by more prose;
* it must name a tool that was offered on this request, exactly;
* JSON must be call-shaped (a name key plus an arguments key and nothing that
  reads like a schema or a document), or bare arguments right after a short
  announcement line that names the tool;
* the text just before the payload must not read as an explanation
  ("for example", "the format is", "you can", "here is the output", ...).

:class:`TextToolCallStreamGate` is the streaming half. Text deltas reach the
user as they arrive, so by the time the reply ends, a written-out call would
already be on screen. The gate holds back only the stretch of text that could
turn into such a payload (an announcement line, a JSON-ish fence, a line that
opens with ``{``, ``Action:``, tool markup or an offered tool name) and lets
everything else through at once. When the stretch turns out to be ordinary
text it is released; when the reply ends on it, the engine either discards it
and retries, or releases it.

Everything here is pure string work; nothing touches the engine state.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from agentos.provider.types import StopReason, normalize_stop_reason

#: Replies longer than this are never scanned.
MAX_SCAN_CHARS = 256_000
#: An announcement ("Let me call read_file:") is one short line.
MAX_ANNOUNCEMENT_CHARS = 240
#: The gate gives up holding a candidate payload past this size.
MAX_HOLD_CHARS = 128_000
#: How far back from the payload the explanation cues are looked for.
_CUE_WINDOW_CHARS = 400
#: Bound on JSON parse attempts when locating a trailing bare payload.
_MAX_JSON_STARTS = 64

_JSON_FENCE_LANGS = frozenset(
    {"", "json", "jsonc", "json5", "tool", "tool_call", "tool_code", "function_call", "xml"}
)
_NAME_KEYS = ("name", "tool", "tool_name", "function", "action", "recipient_name")
_ARGS_KEYS = (
    "arguments",
    "args",
    "parameters",
    "params",
    "input",
    "action_input",
    "tool_input",
)
_EXTRA_CALL_KEYS = frozenset({"type", "id", "call_id", "tool_call_id"})
_ALLOWED_CALL_KEYS = frozenset(_NAME_KEYS) | frozenset(_ARGS_KEYS) | _EXTRA_CALL_KEYS

_FENCE_RE = re.compile(r"^[ \t]*(```+|~~~+)[ \t]*([A-Za-z0-9_+.-]*)[ \t]*$")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]*")
_NAMED_ARGS_RE = re.compile(
    r"^\s*`?([A-Za-z_][A-Za-z0-9_.:-]*)`?\s*(\()?\s*(\{.*\})\s*(\))?\s*;?\s*$",
    re.DOTALL,
)
_INTENT_RE = re.compile(
    r"^(?:[-*>]\s*)?(?:"
    r"i(?:'ll|’ll| will| am going to|'m going to|’m going to| need to| should| can)"
    r"|let me|let's|let’s"
    r"|now(?:,)?(?: i(?:'ll|’ll| will))?"
    r"|next(?:,)?(?: i(?:'ll|’ll| will))?"
    r"|first(?:,)?(?: i(?:'ll|’ll| will))?"
    r"|calling|using|running|invoking|executing"
    r")\b",
    re.IGNORECASE,
)
# Phrases that frame the payload as an explanation of how a call looks. Any of
# them close before the payload blocks a match.
_EXPLANATION_CUE_RE = re.compile(
    r"\b(?:"
    r"for example|example|examples|e\.g\.|for instance|such as|sample|template"
    r"|format|syntax|schema|signature"
    r"|looks? like|would look|should look|might look"
    r"|you (?:can|could|would|should|need|might|may|will)|you'd|you’d|you'll|you’ll"
    r"|how to|usage|documentation|docs|literal|literally|verbatim"
    r"|explain\w*|describ\w*|illustrat\w*|demonstrat\w*|show you|is used|are used"
    r")\b",
    re.IGNORECASE,
)
# Phrases that present the payload as something being shown (an output, a log
# entry) rather than run. Only checked on the line right before the payload,
# so analysis earlier in the reply ("the test output shows ...") does not count.
_PRESENTING_CUE_RE = re.compile(
    r"\b(?:"
    r"here(?:'s|’s| is| are)|below|the following|output|result|results|response"
    r"|returned|returns|shown|log|logs|history|transcript"
    r")\b",
    re.IGNORECASE,
)
_REACT_ACTION_RE = re.compile(r"^[ \t]*(?:\*\*)?Action(?:\*\*)?[ \t]*:[ \t]*(.*?)[ \t]*$", re.M)
_REACT_INPUT_RE = re.compile(r"^[ \t]*(?:\*\*)?Action[ \t]*Input(?:\*\*)?[ \t]*:", re.M | re.I)
_REACT_AFTER_RE = re.compile(
    r"^[ \t]*(?:\*\*)?(?:Observation|Final[ \t]*Answer)(?:\*\*)?[ \t]*:[ \t]*(\S.*)?$",
    re.M | re.I,
)
_ACTION_LINE_RE = re.compile(r"(?:\*\*)?Action(?:\*\*)?[ \t]*:[ \t]*")
_MARKUP_OPEN_RE = re.compile(
    r"<\s*(?:tool_calls?|function_calls?|tool_use|invoke|function)\b[^>]*>|<\s*function\s*=",
    re.IGNORECASE,
)
_MARKUP_NAME_RES = (
    re.compile(r"""["']name["']\s*:\s*["']([^"']+)["']"""),
    re.compile(r"""\bname\s*=\s*["']([^"']+)["']"""),
    re.compile(r"<\s*function\s*=\s*([A-Za-z_][A-Za-z0-9_.:-]*)"),
)
_MARKUP_PREFIXES = (
    "<tool_call",
    "<tool_calls",
    "<function_call",
    "<function_calls",
    "<tool_use",
    "<invoke",
    "<function",
)
_INTENT_PREFIX_WORDS = (
    "i'll",
    "i’ll",
    "i will",
    "i am going to",
    "i'm going to",
    "i’m going to",
    "i need to",
    "i should",
    "i can",
    "let me",
    "let's",
    "let’s",
    "now",
    "next",
    "first",
    "calling",
    "using",
    "running",
    "invoking",
    "executing",
)


@dataclass(frozen=True)
class TextToolCallMatch:
    """A written-out tool call at the end of a reply."""

    tool_name: str
    #: ``json_call``, ``named_args``, ``announced_args``, ``react`` or ``markup``.
    shape: str
    #: Offset in the reply where the payload (or its announcement line) begins.
    start: int
    fenced: bool = False


def offered_tool_names(tools: Iterable[Any] | None) -> frozenset[str]:
    """Names of the tool definitions sent with a request."""

    names: set[str] = set()
    for tool in tools or ():
        name = getattr(tool, "name", None)
        if isinstance(name, str) and name.strip():
            names.add(name.strip())
    return frozenset(names)


def detect_text_tool_call(text: str, tool_names: Iterable[str]) -> TextToolCallMatch | None:
    """Return the written-out tool call that ends ``text``, if there is one.

    ``tool_names`` are the tools offered on the request that produced the
    reply. Only call this for a reply that produced no tool call at all.
    """

    names = frozenset(name for name in tool_names if name)
    if not names or not text or len(text) > MAX_SCAN_CHARS:
        return None
    body = text.rstrip()
    if not body:
        return None
    for finder in (_find_trailing_fence, _find_react, _find_markup, _find_trailing_bare):
        match = finder(body, names)
        if match is not None:
            return match
    return None


#: Stop reasons that mean the model finished its reply on its own. A ReAct-style
#: model is often stopped by an ``Observation:`` stop sequence, and some
#: providers report ``tool_use`` even when no call came through.
_NORMAL_STOPS = frozenset({StopReason.END_TURN, StopReason.STOP_SEQUENCE, StopReason.TOOL_USE})


def text_tool_call_to_retry(
    *,
    text: str,
    gate: TextToolCallStreamGate,
    produced_tool_calls: bool,
    stream_complete: bool,
    stop_reason: str | None,
) -> TextToolCallMatch | None:
    """The written-out call to retry for, when this reply qualifies.

    The reply must have finished normally, produced no tool call (native or
    promoted from text by the provider), and end with a written-out call the
    ``gate`` kept off the screen -- a call the user already saw is not
    retried, since the retry could not take it back.
    """

    if produced_tool_calls or not stream_complete:
        return None
    if normalize_stop_reason(stop_reason) not in _NORMAL_STOPS:
        return None
    hold_start = gate.hold_start
    if hold_start is None:
        return None
    match = detect_text_tool_call(text, gate.tool_names)
    if match is None or match.start < hold_start:
        return None
    return match


def text_tool_call_nudge(match: TextToolCallMatch) -> str:
    """Instruction added to the retried request (never stored in history)."""

    return (
        f"Your previous reply wrote a call to the `{match.tool_name}` tool out as text, "
        "so the tool did not run and nothing happened. If you meant to use the tool, "
        "call it now through the tool-calling interface instead of writing the call "
        "as JSON, code or markup. If you were only showing the user an example, say "
        "so in prose instead."
    )


# ── payload finders ─────────────────────────────────────────────────────────


def _find_trailing_fence(body: str, names: frozenset[str]) -> TextToolCallMatch | None:
    lines = body.split("\n")
    closing = _FENCE_RE.match(lines[-1])
    if closing is None or closing.group(2) or len(lines) < 2:
        return None
    marker = closing.group(1)
    offsets = _line_offsets(lines)
    # The nearest opener above the closing line, with the same fence marker.
    for index in range(len(lines) - 2, -1, -1):
        opener = _FENCE_RE.match(lines[index])
        if opener is None:
            continue
        if opener.group(1)[:3] != marker[:3]:
            return None
        if not opener.group(2):
            # A bare fence line above the closing one could itself be a
            # closing line; only accept it when the fences above pair up.
            if _fence_lines_before(lines, index) % 2:
                return None
        if opener.group(2).lower() not in _JSON_FENCE_LANGS:
            return None
        payload = "\n".join(lines[index + 1 : -1]).strip()
        start = offsets[index]
        return _match_payload(body, start, payload, names, fenced=True)
    return None


def _find_trailing_bare(body: str, names: frozenset[str]) -> TextToolCallMatch | None:
    decoder = json.JSONDecoder()
    lines = body.split("\n")
    offsets = _line_offsets(lines)
    if _fence_lines_before(lines, len(lines)) % 2:
        return None  # the reply ends inside an unclosed fence
    attempts = 0
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped:
            continue
        first = stripped[0]
        start = offsets[index]
        if first in "{[":
            attempts += 1
            if attempts > _MAX_JSON_STARTS:
                return None
            json_start = start + (len(line) - len(stripped))
            try:
                _, end = decoder.raw_decode(body, json_start)
            except json.JSONDecodeError:
                continue
            if body[end:].strip():
                continue
            payload = body[json_start:].strip()
            return _match_payload(body, start, payload, names, fenced=False)
        if first.isalpha() or first in "_`":
            ident = _IDENT_RE.match(stripped.lstrip("`"))
            if ident is None or ident.group(0) not in names:
                continue  # cheap check before matching the rest of the reply
            named = _NAMED_ARGS_RE.match(body[start:])
            if named is not None and named.group(1) in names:
                payload = body[start:].strip()
                return _match_payload(body, start, payload, names, fenced=False)
    return None


def _find_react(body: str, names: frozenset[str]) -> TextToolCallMatch | None:
    actions = list(_REACT_ACTION_RE.finditer(body))
    if not actions:
        return None
    action = actions[-1]
    tool_name = _clean_name(action.group(1))
    if tool_name not in names:
        return None
    after = body[action.end() :]
    action_input = _REACT_INPUT_RE.search(after)
    if action_input is None or after[: action_input.start()].strip():
        return None
    # A filled-in observation or a final answer means the reply went on past
    # the call: that is a worked example, not a call waiting to run.
    if any(m.group(1) for m in _REACT_AFTER_RE.finditer(after)):
        return None
    if _fence_lines_before(body[: action.start()].split("\n"), None) % 2:
        return None  # inside a code fence: showing the format
    if _has_explanation_cue(body[: action.start()]):
        return None
    return TextToolCallMatch(tool_name=tool_name, shape="react", start=action.start())


def _find_markup(body: str, names: frozenset[str]) -> TextToolCallMatch | None:
    # Markup must run to the end of the reply. Take the first opener that
    # starts a line, so a multi-call block counts as one payload.
    opener = next(
        (
            m
            for m in _MARKUP_OPEN_RE.finditer(body)
            if not body[body.rfind("\n", 0, m.start()) + 1 : m.start()].strip()
        ),
        None,
    )
    if opener is None:
        return None
    segment = body[opener.start() :]
    if not segment.rstrip().endswith((">", "}", "]")):
        return None
    if _fence_lines_before(body[: opener.start()].split("\n"), None) % 2:
        return None
    tool_name = _markup_tool_name(segment, names)
    if tool_name is None:
        return None
    if _has_explanation_cue(body[: opener.start()]):
        return None
    return TextToolCallMatch(tool_name=tool_name, shape="markup", start=opener.start())


def _match_payload(
    body: str,
    start: int,
    payload: str,
    names: frozenset[str],
    *,
    fenced: bool,
) -> TextToolCallMatch | None:
    """Classify one trailing payload that begins at ``start`` in ``body``."""

    if not payload:
        return None
    preamble = body[:start]
    parsed = _load_json(payload)
    if parsed is not _NOT_JSON:
        tool_name = _call_name(parsed, names)
        if tool_name is not None:
            if _has_explanation_cue(preamble):
                return None
            return TextToolCallMatch(tool_name, "json_call", start, fenced)
        if isinstance(parsed, dict) and parsed:
            return _announced_args(preamble, names, fenced=fenced)
        return None
    named = _NAMED_ARGS_RE.match(payload)
    if named is not None and named.group(1) in names:
        if bool(named.group(2)) != bool(named.group(4)):
            return None
        if not isinstance(_load_json(named.group(3)), dict):
            return None
        if _has_explanation_cue(preamble):
            return None
        return TextToolCallMatch(named.group(1), "named_args", start, fenced)
    if fenced and _MARKUP_OPEN_RE.match(payload.lstrip()):
        tool_name = _markup_tool_name(payload, names)
        if tool_name is not None and not _has_explanation_cue(preamble):
            return TextToolCallMatch(tool_name, "markup", start, fenced)
    return None


def _announced_args(
    preamble: str,
    names: frozenset[str],
    *,
    fenced: bool,
) -> TextToolCallMatch | None:
    """Bare arguments only count right after a line that announces the tool."""

    stripped = preamble.rstrip()
    if not stripped:
        return None
    line_start = stripped.rfind("\n") + 1
    line = stripped[line_start:].strip()
    tool_name = announced_tool(line, names)
    if tool_name is None or _has_explanation_cue(stripped):
        return None
    # The announcement line is where the call begins.
    return TextToolCallMatch(tool_name, "announced_args", line_start, fenced)


# ── shape helpers ───────────────────────────────────────────────────────────


_NOT_JSON = object()


def _load_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return _NOT_JSON


def _call_name(value: Any, names: frozenset[str]) -> str | None:
    """Tool name when ``value`` is a call (or a list of calls) to offered tools."""

    if isinstance(value, list):
        if not value:
            return None
        found = [_call_name(item, names) for item in value]
        if any(name is None for name in found):
            return None
        return found[0]
    if not isinstance(value, dict) or not value:
        return None
    if set(value) <= {"tool_calls", "type", "id"} and "tool_calls" in value:
        return _call_name(value["tool_calls"], names)
    nested = value.get("function")
    if isinstance(nested, dict) and set(value) <= {"function", "type", "id", "call_id"}:
        return _call_name(nested, names)
    if not set(value) <= _ALLOWED_CALL_KEYS:
        return None
    name_values = [value[key] for key in _NAME_KEYS if key in value]
    arg_values = [value[key] for key in _ARGS_KEYS if key in value]
    if len(name_values) != 1 or len(arg_values) != 1:
        return None
    raw_name, arguments = name_values[0], arg_values[0]
    if not isinstance(raw_name, str):
        return None
    tool_name = _clean_name(raw_name)
    if tool_name not in names:
        return None
    if isinstance(arguments, str):
        arguments = _load_json(arguments) if arguments.strip() else {}
    if not isinstance(arguments, dict):
        return None
    if _looks_like_schema(arguments):
        return None
    return tool_name


def _looks_like_schema(arguments: dict[str, Any]) -> bool:
    return arguments.get("type") == "object" and isinstance(arguments.get("properties"), dict)


def _clean_name(raw: str) -> str:
    name = raw.strip().strip("`'\"*").strip()
    if name.startswith("functions."):
        name = name[len("functions.") :]
    return name.rstrip(":").strip()


def _markup_tool_name(segment: str, names: frozenset[str]) -> str | None:
    for pattern in _MARKUP_NAME_RES:
        for match in pattern.finditer(segment):
            name = _clean_name(match.group(1))
            if name in names:
                return name
    return None


def announced_tool(line: str, names: frozenset[str]) -> str | None:
    """Tool named by an announcement line ("Let me call `read_file`:")."""

    text = line.strip()
    if not text or len(text) > MAX_ANNOUNCEMENT_CHARS:
        return None
    label = _clean_name(text)
    if label in names:
        return label  # a bare tool label on its own line
    if _INTENT_RE.match(text) is None:
        return None
    for token in _IDENT_RE.finditer(text.replace("`", " ")):
        cleaned = token.group(0).rstrip(".:-")
        if cleaned in names:
            return cleaned
    return None


def _has_explanation_cue(preamble: str) -> bool:
    window = preamble[-_CUE_WINDOW_CHARS:]
    if _EXPLANATION_CUE_RE.search(window) is not None:
        return True
    stripped = window.rstrip()
    last_line = stripped[stripped.rfind("\n") + 1 :]
    return _PRESENTING_CUE_RE.search(last_line) is not None


def _line_offsets(lines: list[str]) -> list[int]:
    offsets: list[int] = []
    position = 0
    for line in lines:
        offsets.append(position)
        position += len(line) + 1
    return offsets


def _fence_lines_before(lines: list[str], stop: int | None) -> int:
    return sum(1 for line in lines[:stop] if _FENCE_RE.match(line))


# ── streaming gate ──────────────────────────────────────────────────────────


@dataclass
class _PayloadScan:
    """Incremental scan of one held payload, so each delta is looked at once."""

    #: ``fence``, ``brackets`` or ``to_end`` (ReAct and markup run to the end).
    kind: str
    #: Absolute offset scanned up to.
    scan: int
    fence_marker: str = ""
    depth: int = 0
    started: bool = False
    in_string: bool = False
    escaped: bool = False
    #: Absolute offset just past the payload, once it is complete.
    end: int | None = None


class TextToolCallStreamGate:
    """Hold back streamed text that may be a written-out tool call.

    :meth:`push` takes each text delta and returns the part that can be shown
    now. Text from the start of a candidate payload is held until it is
    clearly ordinary text (then released by a later :meth:`push`) or the
    reply ends. At the end, :attr:`hold_start` says where the held stretch
    begins; the caller either drops it (and retries) or shows it via
    :meth:`release`.

    Offsets are absolute positions in the reply. Only the text not yet shown
    (at most the current line, or the held stretch) is kept, and every delta
    is scanned once, so the cost stays linear in the reply length.
    """

    def __init__(self, tool_names: Iterable[str]) -> None:
        self._names = frozenset(name for name in tool_names if name)
        self._win = ""  # reply text from ``_win_start`` to the end
        self._win_start = 0
        self._total = 0
        self._emitted = 0
        self._scan = 0  # start of the line being classified
        self._plain_line = False  # rest of the current line is decided plain
        self._foreign_fence: str | None = None
        self._hold: int | None = None
        self._payload: _PayloadScan | None = None
        self._passthrough = not self._names

    @property
    def tool_names(self) -> frozenset[str]:
        return self._names

    @property
    def hold_start(self) -> int | None:
        """Offset in the reply where the held text begins, if any is held."""

        if self._passthrough or self._emitted >= self._total:
            return None
        return self._emitted

    @property
    def held_text(self) -> str:
        return self._slice(self._emitted, self._total)

    def push(self, delta: str) -> str:
        if not delta:
            return ""
        if self._passthrough:
            self._total += len(delta)
            self._emitted = self._total
            return delta
        self._win += delta
        self._total += len(delta)
        if self._total > MAX_SCAN_CHARS:
            return self.release()
        out = self._emit_to(self._advance())
        self._trim()
        return out

    def release(self) -> str:
        """Show everything held and stop holding for the rest of the reply."""

        self._passthrough = True
        out = self._emit_to(self._total)
        self._win, self._win_start = "", self._total
        return out

    def discard(self) -> str:
        """Drop the held text (it will not be shown) and return it."""

        held = self.held_text
        self._emitted = self._total
        self._passthrough = True
        self._win, self._win_start = "", self._total
        return held

    # ── window helpers ──────────────────────────────────────────────────

    def _slice(self, start: int, end: int) -> str:
        return self._win[start - self._win_start : end - self._win_start]

    def _find(self, needle: str, start: int) -> int:
        index = self._win.find(needle, start - self._win_start)
        return -1 if index < 0 else index + self._win_start

    def _emit_to(self, end: int) -> str:
        if end <= self._emitted:
            return ""
        out = self._slice(self._emitted, end)
        self._emitted = end
        return out

    def _trim(self) -> None:
        keep = self._emitted
        if self._hold is not None:
            keep = min(keep, self._hold)
        if not self._plain_line:
            keep = min(keep, self._scan)
        if keep > self._win_start:
            self._win = self._win[keep - self._win_start :]
            self._win_start = keep

    # ── line scanner ────────────────────────────────────────────────────

    def _advance(self) -> int:
        """Scan line by line from ``_scan``; text before the result may be shown."""

        while True:
            if self._hold is not None:
                release_to = self._judge_hold(self._hold)
                if release_to is None:
                    return self._hold
                self._hold = None
                self._payload = None
                self._scan = release_to
                # Released mid-line (after a closing brace): the rest of that
                # line is ordinary text.
                self._plain_line = (
                    release_to > 0 and self._slice(release_to - 1, release_to) != "\n"
                )
                continue
            newline = self._find("\n", self._scan)
            complete = newline >= 0
            if self._plain_line:
                if not complete:
                    return self._total
                self._plain_line = False
                self._scan = newline + 1
                continue
            line = self._slice(self._scan, newline if complete else self._total)
            verdict = self._classify_line(line, complete=complete)
            if verdict == "open":
                self._hold = self._scan
                continue
            if verdict == "wait":
                return self._scan
            if not complete:
                self._plain_line = True
                return self._total
            self._scan = newline + 1

    def _classify_line(self, line: str, *, complete: bool, peek: bool = False) -> str:
        """``open`` (hold from here), ``wait`` (undecided) or ``plain``."""

        stripped = line.strip()
        if self._foreign_fence is not None:
            if not complete:
                return "plain" if stripped else "wait"
            fence = _FENCE_RE.match(line)
            if (
                not peek
                and fence is not None
                and fence.group(1)[:3] == self._foreign_fence
                and not fence.group(2)
            ):
                self._foreign_fence = None
            return "plain"
        if not stripped:
            return "plain" if complete else "wait"
        if stripped[0] in "`~":
            if not complete:
                return "wait"  # a fence's language is only known at the newline
            fence = _FENCE_RE.match(line)
            if fence is None:
                return self._classify_words(stripped, complete=complete)
            if fence.group(2).lower() in _JSON_FENCE_LANGS:
                return "open"
            if not peek:
                self._foreign_fence = fence.group(1)[:3]
            return "plain"
        if stripped[0] == "{":
            return "open"
        if stripped[0] == "[":
            rest = stripped[1:].lstrip()
            if not rest:
                return "plain" if complete else "wait"
            return "open" if rest[0] == "{" else "plain"
        if stripped[0] == "<":
            lowered = stripped.lower().replace(" ", "")
            if any(lowered.startswith(prefix) for prefix in _MARKUP_PREFIXES):
                return "open"
            if not complete and any(prefix.startswith(lowered) for prefix in _MARKUP_PREFIXES):
                return "wait"
            return "plain"
        return self._classify_words(stripped, complete=complete)

    def _classify_words(self, stripped: str, *, complete: bool) -> str:
        bare = stripped.lstrip("*`> -").rstrip()
        lowered = bare.lower()
        # ReAct action line naming an offered tool.
        action = _ACTION_LINE_RE.match(bare)
        if action is not None:
            if not complete:
                return "wait"
            return "open" if _clean_name(bare[action.end() :]) in self._names else "plain"
        if not complete and len(lowered) < len("action:") and "action:".startswith(lowered):
            return "wait"
        # An offered tool name written as a call, or alone as a label line.
        ident = _IDENT_RE.match(bare)
        if ident is not None:
            word = ident.group(0).rstrip(".:-")
            after = bare[ident.end() :].lstrip()
            if word in self._names:
                if after[:1] in ("(", "{"):
                    return "open"
                if not after:
                    return "open" if complete else "wait"
            elif (
                not complete
                and not after
                and any(name.startswith(ident.group(0)) for name in self._names)
            ):
                return "wait"
        # An announcement line ("Let me call read_file:").
        if _INTENT_RE.match(bare):
            if not complete:
                return "wait" if len(stripped) <= MAX_ANNOUNCEMENT_CHARS else "plain"
            return "open" if announced_tool(stripped, self._names) else "plain"
        if (
            not complete
            and len(lowered) < 16
            and any(word.startswith(lowered) for word in _INTENT_PREFIX_WORDS)
        ):
            return "wait"
        return "plain"

    # ── held payload ────────────────────────────────────────────────────

    def _judge_hold(self, hold: int) -> int | None:
        """Where to release a held stretch, or ``None`` to keep holding."""

        if self._total - hold > MAX_HOLD_CHARS:
            return self._total
        if self._payload is None:
            started = self._start_payload(hold)
            if started is not None:
                return started  # not a payload after all: release to here
            if self._payload is None:
                return None  # still undecided
        payload = self._payload
        if payload.kind == "fence":
            self._scan_fence(payload)
        elif payload.kind == "brackets":
            self._scan_brackets(payload)
        if payload.end is None:
            return None
        tail = self._slice(payload.end, self._total)
        if payload.kind == "brackets":
            tail = tail.lstrip(");")
        if tail.strip():
            # More text after the payload: it is not the end of the reply, so
            # it is not a call waiting to run.
            return payload.end
        return None

    def _start_payload(self, hold: int) -> int | None:
        """Set :attr:`_payload` once its first line is known.

        Returns a release offset when the held line turns out not to lead
        into a payload, otherwise ``None`` (with or without a payload set).
        """

        first_newline = self._find("\n", hold)
        first_end = first_newline if first_newline >= 0 else self._total
        first_line = self._slice(hold, first_end)
        payload_start = hold
        if self._is_announcement_line(first_line):
            if first_newline < 0:
                return None
            # Skip blank lines after the announcement, then look at the payload.
            position = first_newline + 1
            while position < self._total and self._slice(position, position + 1) in " \t\r\n":
                position += 1
            if position >= self._total:
                return None
            payload_newline = self._find("\n", position)
            line_end = payload_newline if payload_newline >= 0 else self._total
            # The first 512 characters decide what kind of line this is.
            payload_line = self._slice(position, min(line_end, position + 512))
            verdict = self._classify_line(payload_line, complete=payload_newline >= 0, peek=True)
            if verdict == "wait":
                return None
            if verdict != "open" or self._is_announcement_line(payload_line):
                return first_newline + 1
            payload_start = position
        lead = self._slice(payload_start, min(self._total, payload_start + 512)).lstrip()
        if lead[:1] in ("`", "~"):
            opener_end = self._find("\n", payload_start)
            if opener_end < 0:
                return None
            fence = _FENCE_RE.match(self._slice(payload_start, opener_end))
            marker = fence.group(1)[:3] if fence is not None else lead[:3]
            self._payload = _PayloadScan("fence", scan=opener_end + 1, fence_marker=marker)
        elif lead[:1] == "<" or _ACTION_LINE_RE.match(lead.lstrip("*`> -")):
            self._payload = _PayloadScan("to_end", scan=payload_start)
        else:
            self._payload = _PayloadScan("brackets", scan=payload_start)
        return None

    def _scan_fence(self, payload: _PayloadScan) -> None:
        while payload.end is None:
            newline = self._find("\n", payload.scan)
            if newline < 0:
                return
            line = self._slice(payload.scan, newline)
            fence = _FENCE_RE.match(line)
            if fence is not None and fence.group(1)[:3] == payload.fence_marker:
                if not fence.group(2):
                    payload.end = newline
                    return
            payload.scan = newline + 1

    def _scan_brackets(self, payload: _PayloadScan) -> None:
        chunk = self._slice(payload.scan, self._total)
        for offset, char in enumerate(chunk):
            if payload.in_string:
                if payload.escaped:
                    payload.escaped = False
                elif char == "\\":
                    payload.escaped = True
                elif char == '"':
                    payload.in_string = False
                continue
            if char == '"' and payload.started:
                payload.in_string = True
            elif char in "{[":
                payload.started = True
                payload.depth += 1
            elif char in "}]" and payload.started:
                payload.depth -= 1
                if payload.depth == 0:
                    payload.end = payload.scan + offset + 1
                    payload.scan = payload.end
                    return
        payload.scan = self._total

    def _is_announcement_line(self, line: str) -> bool:
        stripped = line.strip()
        if not stripped or stripped[0] in "{[<`~":
            return False
        bare = stripped.lstrip("*`> -")
        if _ACTION_LINE_RE.match(bare):
            return False
        ident = _IDENT_RE.match(bare)
        if ident is not None and ident.group(0).rstrip(".:-") in self._names:
            if bare[ident.end() :].lstrip()[:1] in ("(", "{"):
                return False
        return True
