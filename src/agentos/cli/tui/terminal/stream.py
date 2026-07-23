"""Terminal Rich streaming renderer for chat responses."""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal

from rich.text import Text

from agentos.cli.chat.turn import TurnResult, UsageCounter, UsageSummary
from agentos.cli.tui.terminal.markdown_stream import (
    MarkdownStreamRenderer,
    markdown_enabled,
    render_markup_to_ansi,
)
from agentos.cli.tui.terminal.prompt import DEFAULT_ASSISTANT_LABEL, _toolbar_context
from agentos.cli.ui import ACCENT, ACCENT_SOFT, console, error_panel

__all__ = [
    "StreamingRenderer",
    "TurnResult",
    "UsageCounter",
    "UsageSummary",
    "WaitingIndicator",
    "_summarize_args",
]

# ESC-introduced terminal sequences: CSI (cursor / SGR / mode), OSC (title,
# clipboard via OSC 52, hyperlink), DCS (programmable strings), plus 2-char
# ESC sequences (e.g. ESC c full reset, ESC 7 save cursor). Stripped before
# any model text reaches the terminal so the response cannot drive the
# emulator — clear screen, hide cursor, write to clipboard, change title,
# emit DA queries that the terminal answers back as input, etc.
_ANSI_RE = re.compile(
    r"\x1b(?:"
    r"\[[0-?]*[ -/]*[@-~]"          # CSI ... final byte
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC ... BEL or ST
    r"|P[^\x1b]*\x1b\\"            # DCS ... ST
    r"|[@-Z\\-_]"                  # 2-char ESC (RIS, IND, NEL, ...)
    r")"
)
# C0 control bytes minus tab/newline; line-feed and tab are kept because
# Markdown content legitimately uses them. Carriage return is dropped
# (overwrite-line attack), as are backspace, bell, and form feed.
_C0_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

# Inline reply directives the LLM emits per system-prompt instructions
# (``[[reply_to_current]]`` / ``[[reply_to: <id>]]``). The WebUI strips
# these with the same regex (``chat.js:_DIRECTIVE_TAG_RE``); the CLI was
# previously rendering them verbatim, leaking an internal control marker
# into the conversation. ``_DirectiveStreamSanitizer`` below applies the
# regex incrementally so a tag split across two ``text_delta`` events
# still gets dropped instead of slipping through as ``[[reply_``.
_DIRECTIVE_TAG_RE = re.compile(
    r"\[\[\s*(?:reply_to_current|reply_to\s*:\s*[^\]\n]+)\s*\]\]\s*"
)
_DIRECTIVE_TAG_BUFFER_LIMIT = 256


def _capture_console_print(*objects: Any, **kwargs: Any) -> str:
    """Render Rich output to a string so callers can choose the write path."""
    with console.capture() as capture:
        console.print(*objects, **kwargs)
    return capture.get()


def _sanitize_stream_text(delta: str) -> str:
    """Strip ANSI escapes and dangerous C0 controls from streamed model text.

    The token stream is written straight to ``console.file`` so the terminal
    can render long CJK content without Live's cursor-up overflow bug. That
    bypasses Rich's Markdown layer, which previously did the escaping for us,
    so untrusted model output could otherwise execute terminal control
    sequences (OSC 52 clipboard writes, title rewrites, ``\\r`` line
    overwrites, mode toggles, DA queries that the terminal answers back as
    user input). Keep ``\\n`` and ``\\t`` since Markdown bullets and tables
    rely on them.
    """
    return _C0_RE.sub("", _ANSI_RE.sub("", delta))


class _DirectiveStreamSanitizer:
    """Drop inline ``[[reply_to_current]]`` directives from streamed text.

    Mirrors ``gateway.channel_dispatch._DirectiveTagStreamSanitizer``: the
    runtime threads replies via a control marker that operators reading
    the chat should not see. Tags are removed whether they arrive in one
    ``text_delta`` event or split across chunks; a possibly-partial
    ``[[`` suffix is buffered until the closing ``]]`` (or a newline /
    256-byte cap) arrives so partial matches cannot leak.
    """

    def __init__(self) -> None:
        self._pending = ""

    def clean(self, chunk: str) -> str:
        text = self._pending + chunk
        self._pending = ""
        cleaned = _DIRECTIVE_TAG_RE.sub("", text)
        start = cleaned.rfind("[[")
        if start == -1:
            return cleaned
        suffix = cleaned[start:]
        if (
            "]]" not in suffix
            and "\n" not in suffix
            and len(suffix) <= _DIRECTIVE_TAG_BUFFER_LIMIT
        ):
            self._pending = suffix
            return cleaned[:start]
        return cleaned

    def flush(self) -> str:
        pending = self._pending
        self._pending = ""
        return _DIRECTIVE_TAG_RE.sub("", pending)


def _summarize_args(name: str, args: dict | None) -> str:
    """Return a short human-readable summary of a tool call's key argument.

    Only the tool names that actually exist in the builtin registry are handled;
    all others return an empty string so unknown tools still show correctly.
    """
    if not args:
        return ""
    if name in {"exec_command", "background_process"}:
        cmd = args.get("command") or args.get("cmd") or ""
        return str(cmd)[:60] if cmd else ""
    if name == "execute_code":
        code = args.get("code") or args.get("source") or ""
        first_line = str(code).split("\n", 1)[0]
        return first_line[:60] if first_line else ""
    if name in {"read_file", "write_file", "list_dir", "apply_patch"}:
        path = (
            args.get("path")
            or args.get("file_path")
            or args.get("target")
            or ""
        )
        return str(path)[-50:] if path else ""
    if name == "web_search":
        query = args.get("query") or ""
        return str(query)[:60] if query else ""
    if name == "web_fetch":
        url = args.get("url") or args.get("uri") or ""
        return str(url)[:60] if url else ""
    return ""


class _ToolCallStrip:
    """Coalesces repeated tool calls of the same name into a summary line.

    Rules:
    - Calls 1 and 2 in a run of the same name print normally.
    - On call 3: print "· {name} ×3" once; suppress further prints for
      that run while counting.
    - On name-change, finalize(), or error: if count > 3, flush a
      "· {prev} ×{count} total {sec}s" row. Exactly three calls already
      have a compact repeat row and do not need a duplicate final line.
    """

    def __init__(self) -> None:
        self._pending: dict[str, tuple[str, str, float]] = {}  # id → (name, summary, start_ts)
        self._run_name: str | None = None
        self._run_count: int = 0
        self._run_start: float = 0.0
        self._coalesced: bool = False  # True once the ×3 line has been printed

    @staticmethod
    def _write_payload(payload: str) -> None:
        if not payload:
            return
        console.file.write(payload)
        console.file.flush()

    def _flush_run_payload(self) -> str:
        payload = ""
        if self._run_name is not None and self._run_count > 3:
            elapsed = time.monotonic() - self._run_start
            payload += _capture_console_print(
                f"[{ACCENT}]▸[/] [dim]{self._run_name} "
                f"×{self._run_count} total {elapsed:.1f}s[/dim]"
            )
        self._run_name = None
        self._run_count = 0
        self._run_start = 0.0
        self._coalesced = False
        return payload

    def _flush_run(self) -> None:
        self._write_payload(self._flush_run_payload())

    def record_start_payload(
        self,
        name: str,
        summary: str,
        tool_use_id: str | None,
    ) -> str:
        payload = ""
        ts = time.monotonic()
        tid = tool_use_id or f"_anon_{ts}"
        self._pending[tid] = (name, summary, ts)

        if self._run_name != name:
            payload += self._flush_run_payload()
            self._run_name = name
            self._run_count = 0
            self._run_start = ts

        self._run_count += 1

        if self._run_count <= 2:
            suffix = f" {summary}" if summary else ""
            payload += _capture_console_print(f"[{ACCENT}]▸[/] [dim]{name}{suffix}[/dim]")
        elif self._run_count == 3:
            self._coalesced = True
            payload += _capture_console_print(f"[{ACCENT}]▸[/] [dim]{name} ×3[/dim]")
        # count > 3 and already coalesced: suppress output, keep counting
        return payload

    def record_start(self, name: str, summary: str, tool_use_id: str | None) -> None:
        self._write_payload(self.record_start_payload(name, summary, tool_use_id))

    def record_finish_payload(
        self,
        tool_use_id: str | None,
        *,
        success: bool,
        elapsed: float | None = None,
        error: str | None = None,
    ) -> str:
        payload = ""
        entry = self._pending.pop(tool_use_id or "", None)
        if error:
            # Flush any active run, then print an error row.
            if self._run_name is not None and self._run_count > 3:
                payload += self._flush_run_payload()
            name = entry[0] if entry else (tool_use_id or "tool")
            payload += _capture_console_print(f"[red]✗[/] [dim]{name}: {error}[/dim]")
        return payload

    def record_finish(
        self,
        tool_use_id: str | None,
        *,
        success: bool,
        elapsed: float | None = None,
        error: str | None = None,
    ) -> None:
        self._write_payload(
            self.record_finish_payload(
                tool_use_id,
                success=success,
                elapsed=elapsed,
                error=error,
            )
        )

    def flush(self) -> None:
        """Call before finalizing the turn to close any open coalesced run."""
        self._flush_run()

    def flush_payload(self) -> str:
        """Return any pending coalesced run row without writing it."""
        return self._flush_run_payload()


class WaitingIndicator:
    """Turn-lifetime waiting renderable: spinner frame + verb + elapsed.

    The instance is parked in ``_toolbar_context['status']`` for the whole
    turn — while the model is thinking pre-token, while tokens stream, and
    while tool calls run — so the user always sees an "agent is working"
    signal pinned above the input frame. The prompt-toolkit input header
    reads the slot on every redraw and calls :meth:`toolbar_text` to pull
    the live frame; combined with the ``PromptSession``'s
    ``refresh_interval`` this gives the assistant reply row a real Braille
    spinner without mounting any Rich ``Live`` region (which had
    historically caused ghost-panel artefacts on Windows PowerShell).

    The verb tuple and dwell duration are mirrored in the gateway-side
    ``chat.js`` (``CAP_VERBS`` / ``CAP_DWELL_MS``) so the CLI and
    WebUI present a single brand vocabulary.
    """

    # kept in sync with chat.js CAP_VERBS — calmer sensory register so
    # the chip reads as deliberate work-in-progress instead of aggressive
    # action when the conversation around it is plain prose.
    _verbs = (
        "Watching", "Tracking", "Sensing", "Pulsing",
        "Thinking", "Drafting", "Polishing",
    )
    _verb_dwell_seconds = 2.5
    _spinner_frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    _spinner_frame_seconds = 0.1

    def __init__(self, started_at: float) -> None:
        self._started = started_at

    def _elapsed(self) -> float:
        return max(0.0, time.monotonic() - self._started)

    def _verb(self, elapsed: float) -> str:
        idx = int(elapsed / self._verb_dwell_seconds) % len(self._verbs)
        return self._verbs[idx]

    def _frame(self, elapsed: float) -> str:
        idx = int(elapsed / self._spinner_frame_seconds) % len(self._spinner_frames)
        return self._spinner_frames[idx]

    def toolbar_text(self) -> str:
        """Plain-text body for the prompt-toolkit assistant status row."""
        elapsed = self._elapsed()
        return f"{self._frame(elapsed)} {self._verb(elapsed)} · {elapsed:0.1f}s"

    def __rich__(self) -> Text:
        elapsed = self._elapsed()
        return Text.assemble(
            (f"{self._frame(elapsed)} ", ACCENT),
            (self._verb(elapsed), ACCENT_SOFT),
            (f" · {elapsed:0.1f}s", "dim"),
            ("  ·  Ctrl+C cancels", "dim"),
        )


class StreamingRenderer:
    """One streaming renderer for gateway and standalone responses.

    Strategy: a turn-lifetime assistant status indicator in the input
    header (no Rich ``Live`` instance) — mounted from turn start through
    the last token / tool call and cleared only at finalize / error /
    cancel — plus a token stream that writes deltas straight to the
    terminal through a line-buffered markdown pass
    (:mod:`agentos.cli.tui.terminal.markdown_stream`). There is no
    post-stream re-render — the streamed text is the final view, matching
    how Claude Code, codex, aider, and other agent CLIs present model
    output. The markdown pass preserves that contract: it buffers each
    delta only until a newline arrives, styles the completed line once
    (headings, quotes, rules, lists, tables, fenced code, inline
    ``**bold**`` / ``*italic*`` / ``~~strike~~`` / ``code`` / links), and
    never repaints — avoiding the Rich ``Live`` + ``Markdown`` + ``Panel``
    update loop, which leaked ghost panel borders on Windows PowerShell
    and other terminals whenever the rendered height grew past the
    visible viewport (CJK width-measurement made the overflow common),
    and also avoiding the doubled output a one-shot re-render produces.

    The waiting indicator lives in the prompt-toolkit
    input-header slot via the shared ``_toolbar_context['status']`` key. We
    park a live :class:`WaitingIndicator` instance there and the header
    callable pulls the current spinner frame / verb / elapsed on every redraw.
    Mutating that key here keeps the indicator owned by the renderer while
    reusing the prompt-toolkit input header for display.
    """

    def __init__(
        self,
        *,
        title: str | None = None,
        output_handle: Any | None = None,
    ) -> None:
        # ``title`` defaults to the shared assistant label sourced from
        # ``_toolbar_context["assistant_label"]`` (or ``AGENTOS_ASSISTANT_LABEL``,
        # falling back to ``agentos``) so every caller renders the same
        # speaker name without each site repeating the literal. The
        # sentinel-None pattern lets us read the live value at construction
        # time (Python evaluates plain defaults once at def time, which
        # would freeze a stale label for the whole process).
        if title is None:
            title = str(
                _toolbar_context.get("assistant_label") or DEFAULT_ASSISTANT_LABEL
            )
        self.title = title
        self.buffer = ""
        self.started_at = time.monotonic()
        self._waiting_active = False
        self._stream_started = False
        self._status_line_open = False
        self._visible_output = False
        self._strip = _ToolCallStrip()
        self._directive_sanitizer = _DirectiveStreamSanitizer()
        # Line-buffered markdown pass over the sanitized stream. Disabled
        # under NO_COLOR / non-color consoles so piped output stays plain.
        # The raw text (pre-markdown) is what lands in ``self.buffer`` so
        # downstream consumers (``/save``, transcript markdown) keep the
        # source of truth; the styled form only reaches the terminal.
        self._markdown = MarkdownStreamRenderer(enabled=markdown_enabled())
        # Optional TUI output handle. When provided, async callers can route
        # token writes through `aappend_text` so the output mutex serializes the
        # write-and-flush with concurrent slash-handler / input-echo writes.
        self._output_handle: Any | None = output_handle
        self._stream_output_cm: Any | None = None
        self._stream_write: Any | None = None

    def _write_payload(self, payload: str) -> None:
        if not payload:
            return
        console.file.write(payload)
        console.file.flush()

    async def _awrite_payload(self, payload: str) -> None:
        if not payload:
            return
        if self._stream_write is not None:
            self._stream_write(payload)
            return
        stream_output = getattr(self._output_handle, "stream_output", None)
        if callable(stream_output):
            self._stream_output_cm = stream_output()
            self._stream_write = await self._stream_output_cm.__aenter__()
            self._stream_write(payload)
            return
        if self._output_handle is not None:
            await self._output_handle.write_through(payload)
        else:
            self._write_payload(payload)

    async def _aclose_stream_output(self) -> None:
        cm = self._stream_output_cm
        self._stream_output_cm = None
        self._stream_write = None
        if cm is not None:
            await cm.__aexit__(None, None, None)

    async def aclose(self) -> None:
        """Release any async stream-output region still owned by this renderer."""
        await self._aclose_stream_output()

    def __enter__(self) -> StreamingRenderer:
        self.started_at = time.monotonic()
        self._start_waiting()
        return self

    def __exit__(self, exc_type, exc, tb) -> Literal[False]:
        # Drain any line the markdown pass still holds (the with-block
        # pattern has no explicit finalize call, so without this the
        # trailing partial line would silently vanish).
        flushed = self._flush_markdown()
        if flushed:
            self._write_payload(flushed)
        self.stop()
        return False

    def _open_assistant_status_line(self) -> str:
        """Return the assistant marker once, when visible text actually starts."""
        if self._status_line_open:
            return ""
        self._status_line_open = True
        self._visible_output = True
        return f"◢ {self.title}\n"

    def _start_waiting(self) -> None:
        if self._waiting_active:
            return
        _toolbar_context["status"] = WaitingIndicator(started_at=self.started_at)
        self._waiting_active = True

    def _stop_waiting(self) -> None:
        if not self._waiting_active:
            return
        _toolbar_context["status"] = None
        self._waiting_active = False

    def _begin_stream(self) -> str:
        """Return the assistant marker once, when visible text actually starts.

        Called only once the first visible text delta lands, so control-only
        chunks do not leave empty reply chrome in scrollback. The waiting
        indicator stays mounted in the input header — it is the turn-lifetime
        "agent is working" signal, only cleared by :meth:`stop` (finalize /
        error / cancel), not by the first token.
        """
        if self._stream_started:
            return ""
        marker = self._open_assistant_status_line()
        self._stream_started = True
        return marker

    def _render_stream_payload(self, visible: str) -> str:
        """Render a sanitized text delta for terminal display.

        Runs the line-buffered markdown pass and converts the resulting
        Rich markup to ANSI. When markdown is disabled (``NO_COLOR`` /
        non-color console) the raw text is returned unchanged so model
        bytes reach the terminal verbatim — no Rich markup parsing, no
        tab expansion. The raw ``visible`` text still lands in
        ``self.buffer``; only the *display* form is styled.
        """
        if not self._markdown.enabled:
            return visible
        return render_markup_to_ansi(self._markdown.feed(visible))

    def _flush_markdown(self) -> str:
        """Emit any trailing partial line the markdown pass still holds."""
        if not self._markdown.enabled:
            return self._markdown.flush()
        return render_markup_to_ansi(self._markdown.flush())

    def _end_stream_line(self) -> str:
        """Ensure subsequent console.print starts on a fresh line."""
        # Flush any pending partial directive tag so it cannot leak after
        # the turn ends (e.g. the model emits a bare ``[[`` at the tail).
        # Whatever survives the regex pass is written verbatim — by
        # definition no complete directive was in there.
        payload = ""
        trailing = self._directive_sanitizer.flush()
        if trailing:
            payload += self._begin_stream()
            payload += trailing
            self.buffer += trailing
            self._visible_output = True
        # Drain the markdown line buffer (no trailing newline added — the
        # block below owns line termination).
        payload += self._flush_markdown()
        if self._stream_started and self.buffer and not self.buffer.endswith("\n"):
            payload += "\n"
        return payload

    def append_text(self, delta: str) -> None:
        if not delta:
            return
        safe = _sanitize_stream_text(delta)
        if not safe:
            return
        visible = self._directive_sanitizer.clean(safe)
        if not visible:
            # Chunk was a control directive or partial ``[[`` suffix the
            # sanitizer is still buffering; skip the marker side-effect so
            # an empty ``◢ agentos  `` doesn't print for a directive-only
            # turn.
            return
        # Sanitized text becomes the source of truth for the live stream
        # and for ``TurnResult.text`` (used by ``/save`` and transcript
        # markdown), so raw model bytes that contain ANSI cannot resurface
        # via downstream consumers.
        self.buffer += visible
        payload = self._begin_stream() + self._render_stream_payload(visible)
        self._visible_output = True
        # Write straight to the underlying stream: no Rich markup parsing
        # (model output may contain ``[bracket]`` sequences), no auto-wrap
        # cursor math, no Live repaint loop. The terminal handles wrapping.
        self._write_payload(payload)

    async def aappend_text(self, delta: str) -> None:
        """Async sibling of `append_text` that routes through the output mutex.

        Mirrors the sync path's sanitization, directive stripping, and
        buffer accounting, then delegates the write-and-flush to
        ``TuiOutputHandle.write_through`` which holds the output lock for
        the microsecond write window. When no output handle was attached the
        call falls back to the direct sync write so callers can use a
        single async API without paying for a lock that isn't wired.
        """
        if not delta:
            return
        safe = _sanitize_stream_text(delta)
        if not safe:
            return
        visible = self._directive_sanitizer.clean(safe)
        if not visible:
            return
        self.buffer += visible
        payload = self._begin_stream() + self._render_stream_payload(visible)
        self._visible_output = True
        await self._awrite_payload(payload)

    def pulse(self) -> None:
        """Refresh visible feedback when the stream is alive but quiet.

        Pre-token: the waiting indicator's own refresh loop keeps the elapsed
        counter alive; we just make sure it is still mounted. Mid-stream the
        arriving tokens are the progress signal, but the indicator stays
        mounted too (it is the turn-lifetime "agent is working" signal), so
        pulse only re-mounts if something cleared it externally.
        """
        self._start_waiting()

    def error(self, message: str) -> None:
        payload = self._end_stream_line()
        self.stop()
        self._visible_output = True
        payload += _capture_console_print(error_panel(message))
        self._write_payload(payload)

    async def aerror(self, message: str) -> None:
        payload = self._end_stream_line()
        self.stop()
        self._visible_output = True
        payload += _capture_console_print(error_panel(message))
        try:
            await self._awrite_payload(payload)
        finally:
            await self._aclose_stream_output()

    def status(self, message: str, *, style: str = "dim") -> None:
        payload = self._end_stream_line()
        self._visible_output = True
        payload += _capture_console_print(Text(message, style=style))
        self._write_payload(payload)

    async def astatus(self, message: str, *, style: str = "dim") -> None:
        payload = self._end_stream_line()
        self._visible_output = True
        payload += _capture_console_print(Text(message, style=style))
        await self._awrite_payload(payload)

    def tool_start(
        self,
        name: str,
        args: dict | None = None,
        tool_use_id: str | None = None,
    ) -> None:
        payload = self._end_stream_line()
        summary = _summarize_args(name, args)
        self._visible_output = True
        payload += self._strip.record_start_payload(name, summary, tool_use_id)
        self._write_payload(payload)

    async def atool_start(
        self,
        name: str,
        args: dict | None = None,
        tool_use_id: str | None = None,
    ) -> None:
        payload = self._end_stream_line()
        summary = _summarize_args(name, args)
        self._visible_output = True
        payload += self._strip.record_start_payload(name, summary, tool_use_id)
        await self._awrite_payload(payload)

    def tool_finished(
        self,
        tool_use_id: str | None,
        *,
        success: bool,
        elapsed: float | None = None,
        error: str | None = None,
    ) -> None:
        self._strip.record_finish(
            tool_use_id,
            success=success,
            elapsed=elapsed,
            error=error,
        )

    async def atool_finished(
        self,
        tool_use_id: str | None,
        *,
        success: bool,
        elapsed: float | None = None,
        error: str | None = None,
    ) -> None:
        payload = self._strip.record_finish_payload(
            tool_use_id,
            success=success,
            elapsed=elapsed,
            error=error,
        )
        await self._awrite_payload(payload)

    def tool_call(self, name: str, args: Any | None = None) -> None:
        """Backward-compatible shim — delegates to tool_start."""
        self.tool_start(name, args if isinstance(args, dict) else None, None)

    def finalize(
        self,
        usage: UsageSummary | None = None,
        *,
        cancelled: bool = False,
    ) -> None:
        payload = self._strip.flush_payload()
        payload += self._end_stream_line()
        payload += self._finalize_tail_payload(usage, cancelled=cancelled)
        self._write_payload(payload)

    async def afinalize(
        self,
        usage: UsageSummary | None = None,
        *,
        cancelled: bool = False,
    ) -> None:
        payload = self._strip.flush_payload()
        payload += self._end_stream_line()
        payload += self._finalize_tail_payload(usage, cancelled=cancelled)
        try:
            await self._awrite_payload(payload)
        finally:
            await self._aclose_stream_output()

    def _finalize_tail_payload(
        self,
        usage: UsageSummary | None = None,
        *,
        cancelled: bool = False,
    ) -> str:
        self.stop()
        payload = ""
        elapsed = time.monotonic() - self.started_at
        if cancelled:
            self._visible_output = True
            payload += _capture_console_print("[yellow]turn cancelled[/yellow]")
        if not self._visible_output and not self.buffer:
            return payload
        # Two-line footer: meta on the first line (model · elapsed), usage
        # on the second (tokens · cost). Splitting keeps the metadata
        # block from wrapping into 3-4 display lines on 80-100 column CJK
        # terminals where every Chinese character occupies two columns.
        meta_line = self._footer_meta_line(usage, elapsed)
        usage_line = self._footer_usage_line(usage)
        if meta_line:
            payload += _capture_console_print(f"[dim]{meta_line}[/dim]")
        if usage_line:
            payload += _capture_console_print(f"[dim]{usage_line}[/dim]")
        return payload

    def footer(
        self,
        usage: UsageSummary | None,
        elapsed: float,
    ) -> str:
        """Compatibility surface: the legacy single-string footer.

        Tests and external callers may still ask for the flat
        representation. We join the two structural lines with ``·`` so
        the legacy contract (one ``·``-joined string) answers
        correctly while on-screen rendering uses :meth:`finalize` to
        emit them on separate lines.
        """
        meta_line = self._footer_meta_line(usage, elapsed)
        usage_line = self._footer_usage_line(usage)
        if meta_line and usage_line:
            return f"{meta_line} · {usage_line}"
        return meta_line or usage_line

    def _footer_meta_line(
        self,
        usage: UsageSummary | None,
        elapsed: float,
    ) -> str:
        parts: list[str] = []
        if usage and usage.model:
            parts.append(usage.model)
        parts.append(f"{elapsed:.1f}s")
        return " · ".join(parts)

    def _footer_usage_line(self, usage: UsageSummary | None) -> str:
        if usage is None:
            return ""
        parts: list[str] = []
        if usage.input_tokens or usage.output_tokens:
            parts.append(f"{usage.input_tokens:,} in / {usage.output_tokens:,} out")
        if usage.cached_tokens:
            parts.append(f"{usage.cached_tokens:,} cached")
        if usage.reasoning_tokens:
            parts.append(f"{usage.reasoning_tokens:,} think")
        if usage.cost_usd:
            parts.append(f"${usage.cost_usd:.6f}")
        if usage.aggregate:
            parts.append("aggregate")
        return " · ".join(parts)

    def stop(self) -> None:
        self._stop_waiting()

    def start(self) -> None:
        """Resume the turn-lifetime waiting indicator after an external pause.

        The indicator is mounted for the whole turn (pre-token, mid-stream,
        tool calls), so ``start`` simply re-mounts it; :meth:`_start_waiting`
        is idempotent when it is already active.
        """
        self._start_waiting()

    @contextmanager
    def paused(self) -> Iterator[None]:
        """Suspend the indicator during an inline approval (which owns the
        screen), restoring it on exit so the turn signal resumes."""
        had_waiting = self._waiting_active
        self.stop()
        try:
            yield
        finally:
            if had_waiting:
                self._start_waiting()
