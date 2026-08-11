"""Streaming helpers for models that interleave reasoning into content text."""

from __future__ import annotations


class ThinkTagStreamSplitter:
    """Split a streaming text delta sequence into visible text and <think> reasoning.

    Models with ``reasoning_format == "think_tags"`` interleave reasoning into
    the ordinary content stream as ``<think>…</think>`` blocks. Feeding every
    content delta through this splitter keeps reasoning out of the user-visible
    text even when a tag is cut across chunk boundaries: any trailing prefix of
    a tag is held back until the next chunk (or ``flush``) resolves it.
    """

    _OPEN = "<think>"
    _CLOSE = "</think>"

    def __init__(self) -> None:
        self._in_think = False
        self._pending = ""

    def feed(self, chunk: str) -> tuple[str, str]:
        """Return ``(visible_text, thinking_text)`` extracted from ``chunk``."""
        text = self._pending + chunk
        self._pending = ""
        text_out: list[str] = []
        think_out: list[str] = []
        while text:
            tag = self._CLOSE if self._in_think else self._OPEN
            idx = text.find(tag)
            if idx != -1:
                (think_out if self._in_think else text_out).append(text[:idx])
                text = text[idx + len(tag) :]
                self._in_think = not self._in_think
                continue
            keep = len(text)
            for probe in range(min(len(tag) - 1, len(text)), 0, -1):
                if tag.startswith(text[-probe:]):
                    keep = len(text) - probe
                    break
            (think_out if self._in_think else text_out).append(text[:keep])
            self._pending = text[keep:]
            break
        return "".join(text_out), "".join(think_out)

    def flush(self) -> tuple[str, str]:
        """Resolve any held-back partial tag at end of stream."""
        pending, self._pending = self._pending, ""
        if not pending:
            return "", ""
        return ("", pending) if self._in_think else (pending, "")
