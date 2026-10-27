"""Tool calls a model wrote as text instead of emitting them natively.

Some models served over an OpenAI-compatible endpoint fall back to writing
their tool call into the assistant text. This module is the one definition of
what such a call looks like. Two consumers share it:

* the provider promotes the calls it finds into synthetic tool-use events, and
* :mod:`agentos.engine.tool_text_compat` holds the same regions back from the
  user while the reply streams.

Because both sides read the same format table, markup is hidden from the user
only when it was executed; anything that is not promoted is shown as written.

Recognised forms (``_FORMATS``):

* ``<invoke name="...">`` with ``<parameter name="...">`` children, bare or
  inside a ``<minimax:tool_call>``, ``<tool_call(s)>``, ``<tvoe_calls>`` or
  pipe-prefixed DSML wrapper;
* ``<tool_call>{"name": ..., "arguments": {...}}</tool_call>``;
* ``<function=name><parameter=key>value</parameter></function>``, bare or
  wrapped;
* channel markup ending in a ``to=functions.<name>`` commentary message;
* a trailing ``tool_name{...}`` line (the plain-JSON fallback).

Safety rules, applied in :func:`promote_text_tool_calls`:

* a region must start a line (at most three spaces of indent) or directly
  follow another region, and never inside a fenced code block, so an example
  quoted in a code fence or inline code is never run;
* the reply must be one block of calls, optionally with prose before and
  after it; prose between calls means the model is explaining, not calling;
* every call must name a tool offered this turn, names are capped at
  :data:`MAX_TOOL_NAME_CHARS` and each region at :data:`MAX_PAYLOAD_BYTES`;
* it is all or nothing: one bad region and nothing is promoted.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Any, Literal

MAX_TOOL_NAME_CHARS = 120
MAX_PAYLOAD_BYTES = 256 * 1024

# DSML prefixes every tag with a pipe-delimited namespace, in either the ASCII
# or the fullwidth pipe: <｜DSML｜invoke ...>. Every other variant has none.
_DSML = r"(?:[|｜]DSML[|｜])?"
_FLAGS = re.DOTALL | re.IGNORECASE

_WRAPPER_TAGS = r"(?:minimax:tool_call|tool_calls?|tvoe_calls)"
_WRAPPER_OPEN_RE = re.compile(rf"<{_DSML}{_WRAPPER_TAGS}\s*>", _FLAGS)
_WRAPPER_CLOSE_RE = re.compile(rf"\s*</{_DSML}{_WRAPPER_TAGS}\s*>", _FLAGS)

_INVOKE_OPEN_RE = re.compile(rf"<{_DSML}invoke\s+name\s*=\s*[\"']([^\"']*)[\"']\s*>", _FLAGS)
_INVOKE_CLOSE_RE = re.compile(rf"</{_DSML}invoke\s*>", _FLAGS)
_INVOKE_PARAM_RE = re.compile(
    rf"<{_DSML}parameter\s+name\s*=\s*[\"']([^\"']+)[\"']([^>]*)>(.*?)</{_DSML}parameter\s*>",
    _FLAGS,
)
# DSML marks each parameter as a literal string or as JSON to decode.
_STRING_ATTR_RE = re.compile(r"\bstring\s*=\s*[\"'](true|false)[\"']", re.IGNORECASE)

_FUNCTION_OPEN_RE = re.compile(r"<function=([^>\s]+)\s*>", _FLAGS)
_FUNCTION_CLOSE_RE = re.compile(r"</function\s*>", _FLAGS)
_FUNCTION_PARAM_RE = re.compile(r"<parameter=([^>\s]+)\s*>(.*?)</parameter\s*>", _FLAGS)

# Channel markup: optional leaked analysis messages, then a commentary message
# addressed to ``functions.<name>`` whose body is the JSON arguments.
_CHANNEL_HEAD_RE = re.compile(
    r"(?:<\|start\|>assistant\s*)?"
    r"(?:<\|channel\|>analysis\s*<\|message\|>(?:(?!<\|).)*<\|end\|>"
    r"\s*(?:<\|start\|>assistant\s*)?)*"
    r"(?:to=functions\.(?P<pre>[^\s<]+)\s*<\|channel\|>commentary"
    r"|<\|channel\|>commentary\s+to=functions\.(?P<post>[^\s<]+))"
    r"\s*(?:<\|constrain\|>\s*)?(?:json\s*)?<\|message\|>",
    re.DOTALL,
)
_CHANNEL_END_RE = re.compile(r"<\|(?:call|end)\|>")
_CHANNEL_DEAD_RE = re.compile(r"<\|channel\|>final\b|<\|return\|>")

_FENCE_RE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")
_MAX_INDENT = 3

_PLAIN_JSON_TOOL_CALL_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_.:-]*)\s*(\{.*\})\s*$", re.DOTALL)
_PLAIN_JSON_TOOL_PREFIX_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_.:-]*)\s*(?=\{)")

_JSON = json.JSONDecoder()


@dataclass(frozen=True)
class TextToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class TextToolRegion:
    """One call-shaped span of assistant text.

    ``calls`` is empty when the span looks like a call but its payload could
    not be read; ``complete`` is false when the text ended inside the span.
    """

    start: int
    end: int
    calls: tuple[TextToolCall, ...]
    complete: bool

    @property
    def valid(self) -> bool:
        return self.complete and bool(self.calls)


@dataclass(frozen=True)
class TextToolPromotion:
    """Calls to run, and the reply text left for the user once they are removed."""

    calls: tuple[TextToolCall, ...]
    visible_text: str
    source: Literal["markup", "plain_json"]


# ---------------------------------------------------------------------------
# Format table
# ---------------------------------------------------------------------------

_Scan = Callable[[str, int, re.Match[str]], TextToolRegion]


@dataclass(frozen=True)
class _Format:
    name: str
    # Lowercase literals a line must start with to be this format. The stream
    # guard holds text from the first line that starts with one of them.
    prefixes: tuple[str, ...]
    header: re.Pattern[str]
    scan: _Scan
    # Given text from the opener on, has the header had its chance to match?
    header_settled: Callable[[str], bool]


def _settled_at_first_gt(text: str) -> bool:
    return ">" in text


def _settled_channel(text: str) -> bool:
    return _CHANNEL_DEAD_RE.search(text) is not None


def _trim_one_newline(raw: str) -> str:
    if raw.startswith("\r\n"):
        raw = raw[2:]
    elif raw.startswith("\n"):
        raw = raw[1:]
    if raw.endswith("\r\n"):
        raw = raw[:-2]
    elif raw.endswith("\n") or raw.endswith("\r"):
        raw = raw[:-1]
    return raw


def _invoke_parameter_value(attributes: str, raw: str) -> Any:
    raw = _trim_one_newline(raw)
    attr = _STRING_ATTR_RE.search(attributes)
    if attr is None or attr.group(1).lower() == "true":
        return raw
    # string="false" means the body is JSON and the tool schema expects the
    # decoded value in its real type. A mislabelled non-JSON body keeps the
    # literal rather than losing it.
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def _region(
    start: int, end: int, calls: list[TextToolCall] | None, complete: bool
) -> TextToolRegion:
    return TextToolRegion(start, end, tuple(calls or ()), complete)


def _scan_invoke(text: str, start: int, opener: re.Match[str]) -> TextToolRegion:
    close = _INVOKE_CLOSE_RE.search(text, opener.end())
    if close is None:
        return _region(start, len(text), None, False)
    body = text[opener.end() : close.start()]
    arguments = {
        m.group(1).strip(): _invoke_parameter_value(m.group(2), m.group(3))
        for m in _INVOKE_PARAM_RE.finditer(body)
    }
    name = opener.group(1).strip()
    calls = [TextToolCall(name, arguments)] if name else None
    return _region(start, close.end(), calls, True)


def _scan_function(text: str, start: int, opener: re.Match[str]) -> TextToolRegion:
    close = _FUNCTION_CLOSE_RE.search(text, opener.end())
    if close is None:
        return _region(start, len(text), None, False)
    body = text[opener.end() : close.start()]
    arguments = {
        m.group(1).strip(): _trim_one_newline(m.group(2)) for m in _FUNCTION_PARAM_RE.finditer(body)
    }
    return _region(start, close.end(), [TextToolCall(opener.group(1), arguments)], True)


def _json_call(value: Any) -> TextToolCall | None:
    if not isinstance(value, dict):
        return None
    name = value.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    arguments = value.get("arguments", value.get("parameters", {}))
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except ValueError:
            return None
    if not isinstance(arguments, dict):
        return None
    return TextToolCall(name.strip(), arguments)


def _json_calls(value: Any) -> list[TextToolCall] | None:
    items = value if isinstance(value, list) else [value]
    calls = [_json_call(item) for item in items]
    if not calls or any(call is None for call in calls):
        return None
    return [call for call in calls if call is not None]


def _scan_wrapper(text: str, start: int, opener: re.Match[str]) -> TextToolRegion:
    """A wrapper holds invoke blocks, function blocks or one JSON payload."""
    calls: list[TextToolCall] = []
    pos = opener.end()
    while True:
        item_start = _skip_whitespace(text, pos)
        inner = _INVOKE_OPEN_RE.match(text, item_start) or _FUNCTION_OPEN_RE.match(text, item_start)
        if inner is not None:
            scan = _scan_invoke if inner.re is _INVOKE_OPEN_RE else _scan_function
            item = scan(text, item_start, inner)
        elif text.startswith(("{", "["), item_start):
            try:
                value, end = _JSON.raw_decode(text, item_start)
            except ValueError:
                return _wrapper_fallback(text, start, item_start)
            json_calls = _json_calls(value)
            item = _region(item_start, end, json_calls, True)
        else:
            break
        if not item.valid:
            return _wrapper_fallback(text, start, item_start)
        calls.extend(item.calls)
        pos = item.end
    close = _WRAPPER_CLOSE_RE.match(text, pos)
    if close is not None:
        return _region(start, close.end(), calls or None, True)
    if calls:
        # The closing tag is optional (MiniMax often drops it): the region
        # ends with its last complete call.
        return _region(start, pos, calls, True)
    return _wrapper_fallback(text, start, pos)


def _wrapper_fallback(text: str, start: int, pos: int) -> TextToolRegion:
    """An unreadable wrapper: it ends at the next closing tag, or runs to the end."""
    close = _WRAPPER_CLOSE_RE.search(text, pos)
    if close is None:
        return _region(start, len(text), None, False)
    return _region(start, close.end(), None, True)


def _scan_channel(text: str, start: int, head: re.Match[str]) -> TextToolRegion:
    end = _CHANNEL_END_RE.search(text, head.end())
    if end is None:
        return _region(start, len(text), None, False)
    name = head.group("pre") or head.group("post")
    try:
        arguments = json.loads(text[head.end() : end.start()].strip() or "{}")
    except ValueError:
        arguments = None
    calls = [TextToolCall(name, arguments)] if isinstance(arguments, dict) else None
    return _region(start, end.end(), calls, True)


_FORMATS: tuple[_Format, ...] = (
    _Format(
        name="wrapper",
        prefixes=(
            "<minimax:tool_call",
            "<tool_call",
            "<tvoe_calls",
            "<|dsml|tool_call",
            "<｜dsml｜tool_call",
        ),
        header=_WRAPPER_OPEN_RE,
        scan=_scan_wrapper,
        header_settled=_settled_at_first_gt,
    ),
    _Format(
        name="invoke",
        prefixes=("<invoke", "<|dsml|invoke", "<｜dsml｜invoke"),
        header=_INVOKE_OPEN_RE,
        scan=_scan_invoke,
        header_settled=_settled_at_first_gt,
    ),
    _Format(
        name="function",
        prefixes=("<function=",),
        header=_FUNCTION_OPEN_RE,
        scan=_scan_function,
        header_settled=_settled_at_first_gt,
    ),
    _Format(
        name="channel",
        prefixes=("<|start|>assistant", "<|channel|>"),
        header=_CHANNEL_HEAD_RE,
        scan=_scan_channel,
        header_settled=_settled_channel,
    ),
)
_ALL_PREFIXES = tuple(prefix for fmt in _FORMATS for prefix in fmt.prefixes)


# ---------------------------------------------------------------------------
# Line and code-fence helpers shared with the stream guard
# ---------------------------------------------------------------------------

Fence = tuple[str, int]


def next_fence_state(fence: Fence | None, line: str) -> Fence | None:
    """Fence state after ``line`` (a complete line without its newline)."""
    match = _FENCE_RE.fullmatch(line.rstrip("\r"))
    if fence is None:
        if match is None:
            return None
        marker, info = match.group(1), match.group(2)
        if marker[0] == "`" and "`" in info:
            return None
        return marker[0], len(marker)
    if (
        match is not None
        and match.group(1)[0] == fence[0]
        and len(match.group(1)) >= fence[1]
        and not match.group(2).strip()
    ):
        return None
    return fence


def opener_status(line_head: str, *, complete: bool) -> Literal["open", "maybe", "no"]:
    """Whether a line starting with ``line_head`` opens a call-shaped region.

    ``maybe`` means the head is still too short to tell; it is only returned
    while the line is incomplete.
    """
    body = line_head.lstrip(" ")
    if len(line_head) - len(body) > _MAX_INDENT:
        return "no"
    lowered = body.lower()
    if lowered.startswith(_ALL_PREFIXES):
        return "open"
    if not complete and any(prefix.startswith(lowered) for prefix in _ALL_PREFIXES):
        return "maybe"
    return "no"


def header_status(text: str) -> Literal["valid", "invalid", "pending"]:
    """Classify held text that starts (after whitespace) with an opener prefix."""
    start = _skip_whitespace(text, 0)
    lowered = text[start : start + 32].lower()
    for fmt in _FORMATS:
        if not lowered.startswith(fmt.prefixes):
            continue
        if fmt.header.match(text, start) is not None:
            return "valid"
        if not fmt.header_settled(text[start:]):
            return "pending"
    return "invalid"


def _skip_whitespace(text: str, pos: int) -> int:
    while pos < len(text) and text[pos].isspace():
        pos += 1
    return pos


def _skip_horizontal(text: str, pos: int) -> int:
    while pos < len(text) and text[pos] in " \t":
        pos += 1
    return pos


def _region_at(text: str, pos: int) -> TextToolRegion | None:
    lowered = text[pos : pos + 32].lower()
    for fmt in _FORMATS:
        if not lowered.startswith(fmt.prefixes):
            continue
        header = fmt.header.match(text, pos)
        if header is not None:
            return fmt.scan(text, pos, header)
    return None


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


def find_text_tool_regions(text: str) -> list[TextToolRegion]:
    """Every call-shaped region in ``text``, in order, skipping fenced code.

    A region starts a line (at most three spaces of indent) or directly
    follows the previous region. Text inside a region is not checked for
    fences, so a call whose argument contains a code block stays one region.
    """
    regions: list[TextToolRegion] = []
    fence: Fence | None = None
    pos = 0
    while pos < len(text):
        newline = text.find("\n", pos)
        line_end = len(text) if newline < 0 else newline
        if fence is None and opener_status(text[pos:line_end], complete=True) == "open":
            region = _region_at(text, _skip_horizontal(text, pos))
            while region is not None:
                regions.append(region)
                if region.end >= len(text):
                    return regions
                pos = region.end
                region = _region_at(text, _skip_horizontal(text, pos))
            newline = text.find("\n", pos)
            if newline < 0:
                break
            pos = newline + 1
            continue
        fence = next_fence_state(fence, text[pos:line_end])
        if newline < 0:
            break
        pos = newline + 1
    return regions


def parse_text_tool_calls(text: str) -> list[TextToolCall]:
    """Every readable call in the text, without any promotion rules applied."""
    return [call for region in find_text_tool_regions(text) for call in region.calls]


def _without_regions(text: str, regions: list[TextToolRegion]) -> str:
    """Text with ``regions`` cut out: the lead keeps its shape, the rest is tidied."""
    lead = text[: regions[0].start].rstrip()
    rest = [text[a.end : b.start].strip() for a, b in zip(regions, regions[1:], strict=False)]
    rest.append(text[regions[-1].end :].strip())
    return "\n\n".join(part for part in (lead, *rest) if part)


def strip_text_tool_regions(text: str) -> str:
    """Remove every readable call region; unreadable ones stay visible."""
    valid = [region for region in find_text_tool_regions(text) if region.valid]
    return _without_regions(text, valid) if valid else text


# ---------------------------------------------------------------------------
# Promotion
# ---------------------------------------------------------------------------


def _payload_fits(text: str) -> bool:
    if len(text) * 4 <= MAX_PAYLOAD_BYTES:
        return True
    return len(text.encode("utf-8")) <= MAX_PAYLOAD_BYTES


def _resolve_call(call: TextToolCall, allowed: Collection[str]) -> TextToolCall | None:
    """The call under the offered tool name it means, or ``None`` if it names none.

    A decorated name (``functions.read_file``, ``Read_File``, ``read_file:0``)
    resolves the same way a native call's name does, so text-form calls are not
    held to a stricter rule than native ones.
    """
    if len(call.name) > MAX_TOOL_NAME_CHARS:
        return None
    if call.name in allowed:
        return call
    # Imported here: the provider layer must not load the tool registry at import.
    from agentos.tools.tool_name_resolution import resolve_tool_name_among

    resolution = resolve_tool_name_among(call.name, allowed)
    if resolution is None:
        return None
    return TextToolCall(resolution.resolved, call.arguments)


_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


def _coerce_value(value: Any, schema: Any) -> Any:
    """Decode a string argument whose schema type is not a string."""
    if not isinstance(value, str) or not isinstance(schema, Mapping):
        return value
    declared = schema.get("type")
    types = [
        t for t in (declared if isinstance(declared, list) else [declared]) if isinstance(t, str)
    ]
    if "string" in types or not any(t in _JSON_TYPES for t in types):
        return value
    try:
        decoded = json.loads(value)
    except ValueError:
        return value
    for type_name in types:
        expected = _JSON_TYPES.get(type_name)
        if expected is None:
            continue
        if isinstance(decoded, bool) and bool not in expected:
            continue
        if isinstance(decoded, expected):
            return decoded
    return value


def _coerce_call(call: TextToolCall, properties: Mapping[str, Any] | None) -> TextToolCall:
    if not properties:
        return call
    arguments = {
        key: _coerce_value(value, properties.get(key)) for key, value in call.arguments.items()
    }
    return TextToolCall(call.name, arguments)


def _promote_regions(
    text: str, regions: list[TextToolRegion], allowed: Collection[str]
) -> TextToolPromotion | None:
    for region in regions:
        if not region.valid or not _payload_fits(text[region.start : region.end]):
            return None
    resolved = [_resolve_call(call, allowed) for region in regions for call in region.calls]
    if any(call is None for call in resolved):
        return None
    for before, after in zip(regions, regions[1:], strict=False):
        if text[before.end : after.start].strip():
            return None
    calls = tuple(call for call in resolved if call is not None)
    return TextToolPromotion(calls, _without_regions(text, regions), "markup")


def _fenced_or_inline_code_at(text: str, pos: int) -> bool:
    fence: Fence | None = None
    line_start = 0
    while True:
        newline = text.find("\n", line_start)
        line_end = len(text) if newline < 0 else newline
        if line_start <= pos <= line_end:
            return fence is not None or text.count("`", line_start, pos) % 2 == 1
        fence = next_fence_state(fence, text[line_start:line_end])
        if newline < 0:
            return False
        line_start = newline + 1


def parse_plain_json_tool_call(text: str) -> tuple[str, dict[str, Any], int] | None:
    """A reply ending in ``tool_name{...}``: its name, arguments and start offset."""
    non_empty = [line for line in text.splitlines() if line.strip()]
    if non_empty:
        last_line = non_empty[-1]
        for candidate, offset in ((text, 0), (last_line, text.rfind(last_line))):
            match = _PLAIN_JSON_TOOL_CALL_RE.match(candidate)
            if match is None:
                continue
            try:
                arguments = json.loads(match.group(2))
            except ValueError:
                break
            if not isinstance(arguments, dict):
                break
            return match.group(1), arguments, offset + match.start(1)
    for match in reversed(list(_PLAIN_JSON_TOOL_PREFIX_RE.finditer(text))):
        try:
            arguments, end = _JSON.raw_decode(text, match.end())
        except ValueError:
            continue
        if text[end:].strip() or not isinstance(arguments, dict):
            continue
        return match.group(1), arguments, match.start()
    return None


def _promote_plain_json(text: str, allowed: Collection[str]) -> TextToolPromotion | None:
    parsed = parse_plain_json_tool_call(text)
    if parsed is None:
        return None
    name, arguments, start = parsed
    call = _resolve_call(TextToolCall(name, arguments), allowed)
    if call is None or not _payload_fits(text[start:]):
        return None
    if _fenced_or_inline_code_at(text, start):
        return None
    return TextToolPromotion((call,), text[:start].rstrip(), "plain_json")


def promote_text_tool_calls(
    text: str,
    allowed_tool_names: Collection[str],
    *,
    schemas: Mapping[str, Mapping[str, Any]] | None = None,
) -> TextToolPromotion | None:
    """Turn a reply that is a text-form tool call into calls to run.

    Returns ``None`` when the reply must be shown as written. ``schemas``
    maps a tool name to its JSON-schema ``properties``; a string argument
    whose declared type is not a string is decoded (``"3"`` to ``3``).
    """
    if not text or not allowed_tool_names:
        return None
    regions = find_text_tool_regions(text)
    if regions:
        promotion = _promote_regions(text, regions, allowed_tool_names)
    else:
        # Call-shaped markup that is not promoted must stay visible, so the
        # plain-JSON fallback only runs on a reply with no markup at all.
        promotion = _promote_plain_json(text, allowed_tool_names)
    if promotion is None or not schemas:
        return promotion
    calls = tuple(_coerce_call(call, schemas.get(call.name)) for call in promotion.calls)
    return TextToolPromotion(calls, promotion.visible_text, promotion.source)


def has_unpromoted_text_tool_calls(text: str, allowed_tool_names: Collection[str]) -> bool:
    """True when ``text`` holds call-shaped markup that would not be run.

    Use it on a finished reply: a true result means the model wrote a tool
    call as text and nothing executed it (unknown tool, malformed or truncated
    payload, prose between calls, or a call that was never offered).
    """
    regions = find_text_tool_regions(text)
    return bool(regions) and promote_text_tool_calls(text, allowed_tool_names) is None
