"""Text from outside the operator, made safe to show to the operator.

A token's ``symbol()`` is whatever bytes its contract returns, an agent's
order fields are whatever the agent sent, and a provider's error is whatever
the provider wrote. Printed as they came, an ESC sequence recolours, moves or
erases what the operator is reading, a bidi override reorders it, and a
zero-width character makes two different strings look the same — on the
very surfaces an approval is decided from (WAL-13).

* :func:`clean_label` is for data the engine stores and serves (token
  symbols and names): controls are dropped, whitespace collapses to one
  space. Every client then shows the same, plain text.
* :func:`terminal_text` / :func:`display_safe` are for a terminal sink:
  every control, bidi and invisible formatting character is shown as an
  escape (``\\x1b``, ``\\u202e``) instead of being sent, so it is visible
  rather than acted on, and line breaks become spaces unless kept.
"""

from __future__ import annotations

import re
from typing import Any

#: Bidi embeddings/overrides/isolates and marks, the zero-width and invisible
#: formatting characters, and the line/paragraph separators.
_FORMAT = "\u061c\u180e\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff"
#: ... plus every C0 and C1 control and DEL.
UNSAFE_CHARS = re.compile(f"[\x00-\x1f\x7f-\x9f{_FORMAT}]")
_UNSAFE_KEEPING_NEWLINES = re.compile(f"[\x00-\x09\x0b-\x1f\x7f-\x9f{_FORMAT}]")
_SPACES = re.compile(r"[\t\n\r\f\v]+| {2,}")


def _visible(match: re.Match[str]) -> str:
    code = ord(match.group())
    if code in (0x09, 0x0A, 0x0D):
        return " "
    return f"\\x{code:02x}" if code <= 0xFF else f"\\u{code:04x}"


def terminal_text(value: Any, *, keep_newlines: bool = False) -> str:
    """``value`` as text a terminal can print as-is: unsafe characters escaped, visibly."""
    text = str(value)
    pattern = _UNSAFE_KEEPING_NEWLINES if keep_newlines else UNSAFE_CHARS
    return pattern.sub(_visible, text)


def display_safe(value: Any) -> Any:
    """``value`` with every string in it (keys included) passed through :func:`terminal_text`."""
    if isinstance(value, str):
        return terminal_text(value)
    if isinstance(value, dict):
        return {
            (terminal_text(k) if isinstance(k, str) else k): display_safe(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [display_safe(v) for v in value]
    if isinstance(value, tuple):
        return tuple(display_safe(v) for v in value)
    return value


def clean_label(value: Any) -> str:
    """A stored label (token symbol or name) with no control characters in it."""
    text = _SPACES.sub(" ", str(value or ""))
    return " ".join(UNSAFE_CHARS.sub("", text).split())
