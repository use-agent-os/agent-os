"""Keep text-form tool calls out of what the user sees, but only once they run.

What counts as a text-form tool call is defined once, in
:mod:`agentos.provider.text_tool_protocol`. The provider promotes such a reply
into synthetic tool-use events; this module hides the same regions from the
streamed text and from the stored reply. Anything the provider did not
promote is released unchanged, so markup is never hidden without running.
"""

from __future__ import annotations

from agentos.provider.text_tool_protocol import (
    Fence,
    find_text_tool_regions,
    header_status,
    next_fence_state,
    opener_status,
    parse_plain_json_tool_call,
    promote_text_tool_calls,
)


def strip_synthetic_tool_call_text(text: str, tool_name: str) -> str:
    """Remove a trailing ``tool_name{...}`` that was promoted into a tool call."""
    if not text:
        return text
    parsed = parse_plain_json_tool_call(text)
    if parsed is None or parsed[0] != tool_name:
        return text
    return text[: parsed[2]].rstrip()


def strip_synthetic_tool_call_suffix(text: str, tool_names: list[str]) -> str:
    """The reply text once the calls promoted from it are removed.

    ``tool_names`` are the tools the provider synthesized from this text.
    Promoting again against exactly those names reproduces the provider's
    decision, so only what actually ran is cut.
    """
    if not text or not tool_names:
        return text
    promotion = promote_text_tool_calls(text, set(tool_names))
    return text if promotion is None else promotion.visible_text


# Only the start of a line matters for fence detection; a fence line is short.
_LINE_KEEP = 512


def _whitespace_run_start(text: str, end: int) -> int:
    start = end
    while start > 0 and text[start - 1].isspace():
        start -= 1
    return start


class ProtocolTextLeakGuard:
    """Streamed-text guard for text-form tool calls.

    Text is passed through until a line opens a call-shaped region outside a
    code fence. From there on everything is held until the turn decides:

    * :meth:`flush_before_tool_use` with ``synthetic_from_text=True`` means
      the provider promoted the reply, so the held regions are dropped and
      any prose after them is released;
    * any other flush releases the held text unchanged.

    A held line whose opening tag turns out not to match any format is
    released as soon as that is clear.
    """

    def __init__(self) -> None:
        self._reset()

    def _reset(self) -> None:
        # Undecided tail: a whitespace run ending the text, or the start of a
        # line that may still become an opener.
        self._pending = ""
        # Emitted text of the current line, and the fence state before it.
        self._line = ""
        self._fence: Fence | None = None
        self._held: str | None = None
        self._header_checked = False
        self._emitted_any = False

    def _advance(self, emitted: str) -> None:
        if not emitted:
            return
        self._emitted_any = True
        if "\n" not in emitted:
            self._line = (self._line + emitted)[:_LINE_KEEP]
            return
        *complete, tail = (self._line + emitted).split("\n")
        for line in complete:
            self._fence = next_fence_state(self._fence, line)
        self._line = tail[:_LINE_KEEP]

    def _decide(self, buf: str) -> tuple[int, bool]:
        """How much of ``buf`` is visible, and whether the rest is a held region."""
        line, fence = self._line, self._fence
        pos = 0
        while True:
            newline = buf.find("\n", pos)
            if line == "" and fence is None:
                head = buf[pos:] if newline < 0 else buf[pos:newline]
                status = opener_status(head, complete=newline >= 0)
                if status != "no":
                    return _whitespace_run_start(buf, pos), status == "open"
            if newline < 0:
                break
            fence = next_fence_state(fence, line + buf[pos:newline])
            line = ""
            pos = newline + 1
        # Hold back a trailing run of blank lines: it is dropped if a hidden
        # region follows it, and released with the next visible text otherwise.
        visible_end = len(buf.rstrip())
        if "\n" in buf[visible_end:]:
            return visible_end, False
        return len(buf), False

    def _feed(self, text: str) -> str:
        buf = self._pending + text
        self._pending = ""
        cut, hold = self._decide(buf)
        visible = buf[:cut]
        self._advance(visible)
        if hold:
            self._held = buf[cut:]
            self._header_checked = False
            return visible + self._check_header()
        self._pending = buf[cut:]
        return visible

    def _check_header(self) -> str:
        """Release the held text if its opener turned out not to be a call."""
        held = self._held
        if held is None or self._header_checked:
            return ""
        status = header_status(held)
        if status == "pending":
            return ""
        if status == "valid":
            self._header_checked = True
            return ""
        # Emit the false opener's line as plain text, then keep scanning.
        self._held = None
        start = len(held) - len(held.lstrip())
        newline = held.find("\n", start)
        line = held if newline < 0 else held[:newline]
        self._advance(line)
        return line + ("" if newline < 0 else self._feed(held[newline:]))

    def push(self, text: str) -> str:
        if not text:
            return ""
        if self._held is None:
            return self._feed(text)
        self._held += text
        return self._check_header()

    def flush(self) -> str:
        """Release everything still held: nothing was promoted from it."""
        released = self._pending + (self._held or "")
        self._reset()
        return released

    def flush_before_tool_use(self, *, synthetic_from_text: bool = False) -> str:
        """Settle held text before a tool call starts.

        A synthetic call means the provider promoted the held regions, so they
        are cut and only the prose around them is returned. A native call
        leaves any held markup unexecuted, so it is released as written.
        """
        held = self._held
        if not synthetic_from_text or held is None:
            return self.flush()
        emitted_any = self._emitted_any
        self._reset()
        valid = [region for region in find_text_tool_regions(held) if region.valid]
        if not valid:
            return held
        rest = [held[a.end : b.start].strip() for a, b in zip(valid, valid[1:], strict=False)]
        rest.append(held[valid[-1].end :].strip())
        lead = held[: valid[0].start].strip()
        visible = "\n\n".join(part for part in (lead, *rest) if part)
        if visible and emitted_any:
            return "\n\n" + visible
        return visible
