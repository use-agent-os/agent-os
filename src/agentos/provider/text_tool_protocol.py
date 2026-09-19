"""Parser for tool calls a model encoded as text instead of emitting natively.

Several models fall back to writing an ``<invoke name="...">`` block into the
assistant text, each wrapping it in its own marker: MiniMax's
``<minimax:tool_call>``, the ``<tvoe_calls>`` typo-wrapper, DSML's
pipe-prefixed ``<｜DSML｜tool_calls>``, or no wrapper at all.

:mod:`agentos.engine.tool_text_compat` already recognises all of those and
scrubs them from what the user sees. This module must stay at least as wide,
because what gets hidden from the user and what gets executed cannot be two
different definitions of "this is a tool call" — a narrower parser here means
the markup disappears from the transcript and the action silently never runs.

Detection is therefore carried by a well-formed ``<invoke name="...">`` …
``</invoke>`` pair rather than by the wrapper, and the caller filters the
result against the tool names it actually offered.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

# DSML prefixes every tag with a pipe-delimited namespace, in either the ASCII
# or the fullwidth pipe: <｜DSML｜invoke ...>. Every other variant has none.
_TAG_PREFIX = r"(?:[|｜]\s*DSML\s*[|｜]\s*)?"
_INVOKE_RE = re.compile(
    rf"<\s*{_TAG_PREFIX}invoke\s+name\s*=\s*[\"']([^\"']+)[\"']\s*>"
    rf"(.*?)"
    rf"<\s*/\s*{_TAG_PREFIX}invoke\s*>",
    re.DOTALL | re.IGNORECASE,
)
_PARAM_RE = re.compile(
    rf"<\s*{_TAG_PREFIX}parameter\s+name\s*=\s*[\"']([^\"']+)[\"']([^>]*)>"
    rf"(.*?)"
    rf"<\s*/\s*{_TAG_PREFIX}parameter\s*>",
    re.DOTALL | re.IGNORECASE,
)
# DSML marks each parameter as a literal string or as JSON to decode.
_STRING_ATTR_RE = re.compile(r"\bstring\s*=\s*[\"'](true|false)[\"']", re.IGNORECASE)


@dataclass(frozen=True)
class TextToolCall:
    name: str
    arguments: dict[str, Any]


def _parameter_value(attributes: str, raw: str) -> Any:
    if raw.startswith("\r\n"):
        raw = raw[2:]
    elif raw.startswith("\n"):
        raw = raw[1:]

    if raw.endswith("\r\n"):
        raw = raw[:-2]
    elif raw.endswith("\n") or raw.endswith("\r"):
        raw = raw[:-1]

    attr = _STRING_ATTR_RE.search(attributes)
    if attr is None or attr.group(1).lower() == "true":
        return raw
    # string="false" means the body is JSON and the tool schema expects the
    # decoded value in its real type — a list of rows, not an escaped string
    # of one. A model that mislabels a non-JSON body keeps the literal rather
    # than losing it.
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def parse_text_tool_calls(text: str) -> list[TextToolCall]:
    """Extract text-encoded tool invocations from assistant text."""
    calls: list[TextToolCall] = []
    for invoke_match in _INVOKE_RE.finditer(text):
        name = invoke_match.group(1).strip()
        if not name:
            continue
        arguments: dict[str, Any] = {}
        for param_match in _PARAM_RE.finditer(invoke_match.group(2)):
            key = param_match.group(1).strip()
            arguments[key] = _parameter_value(param_match.group(2), param_match.group(3))
        calls.append(TextToolCall(name=name, arguments=arguments))
    return calls
