"""Long-lived prompt-toolkit Application driver for the chat REPL.

Owns a single
`prompt_toolkit.Application` per surface, exposes submitted lines through an
asyncio queue, and routes toolbar state updates through the existing
`_toolbar_context` dict in `prompt.py`.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from prompt_toolkit.application import Application
from prompt_toolkit.auto_suggest import AutoSuggest
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer
from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import ANSI, HTML, to_formatted_text
from prompt_toolkit.history import FileHistory, History
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import (
    Float,
    FloatContainer,
    HSplit,
    Layout,
    VSplit,
    Window,
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.mouse_events import MouseEventType

from agentos.cli.tui.terminal.paste import (
    pasted_content_summary,
    should_collapse_pasted_content,
)
from agentos.engine.commands import Surface


class LockedFileHistory(FileHistory):
    """FileHistory with a single-writer guard for concurrent producers.

    The chat REPL has multiple potential writers: the long-lived input task
    plus any auxiliary prompts spawned by tool flows. FileHistory.store_string
    opens, writes, and closes the file per call; without serialization two
    writers can interleave bytes on multi-threaded or yielding I/O paths.

    The guard is a threading.Lock because store_string is sync — sync I/O
    serialization is what is actually required, and a threading.Lock blocks
    at the syscall layer regardless of whether the call originates from the
    asyncio loop's thread or a worker thread. Within single-threaded asyncio
    two coroutines cannot interleave a sync call, but holding the lock makes
    the single-writer contract explicit in the API surface and guards against
    future multi-thread additions (e.g. running history writes via run_in_executor).
    """

    def __init__(self, filename) -> None:
        super().__init__(filename)
        self._write_lock = threading.Lock()

    def store_string(self, string: str) -> None:
        with self._write_lock:
            super().store_string(string)


if TYPE_CHECKING:
    from prompt_toolkit.input.base import Input
    from prompt_toolkit.output.base import Output


# Sentinel pushed onto the submit queue to signal Ctrl-D / EOF.
_EOF_SENTINEL: object = object()


# Time window for Ctrl-C double-press shutdown detection. A second
# Ctrl-C arriving within this many seconds of the previous one triggers
# the registered shutdown callback (drain + exit). Outside the window,
# Ctrl-C behaves as the existing single-press cancel-or-clear. Module-
# level so tests can monkeypatch it if they need a tighter window.
_DOUBLE_CTRL_C_WINDOW_S: float = 1.5
_ACTIVE_INPUT_PREFIX_WIDTH: int = 7
_MAX_INPUT_HEIGHT: int = 10
_MOUSE_SCROLL_LINES: int = 11


class _TranscriptControl(FormattedTextControl):
    """Formatted transcript content with wheel events routed to chat scroll state."""

    def __init__(self, *args, scroll: Callable[[int], None], **kwargs) -> None:  # noqa: ANN002, ANN003
        super().__init__(*args, **kwargs)
        self._scroll = scroll

    def mouse_handler(self, mouse_event):  # type: ignore[no-untyped-def]
        if mouse_event.event_type == MouseEventType.SCROLL_UP:
            self._scroll(_MOUSE_SCROLL_LINES)
            return None
        if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
            self._scroll(-_MOUSE_SCROLL_LINES)
            return None
        return super().mouse_handler(mouse_event)


def _fullscreen_env() -> bool | None:
    """Tri-state read of the ``AGENTOS_CHAT_FULLSCREEN`` override.

    Returns ``True``/``False`` when the variable is set to a recognized
    truthy/falsy value, or ``None`` when unset (so the caller falls back to
    its own default). The full-screen transcript-pane surface renders the
    assistant transcript inside a scrollable prompt-toolkit pane above a
    permanently-pinned input frame (Claude Code style) instead of streaming
    to native terminal scrollback. See issue #46 (issue 1).
    """
    raw = os.environ.get("AGENTOS_CHAT_FULLSCREEN")
    if raw is None:
        return None
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off", ""}:
        return False
    return None


def resolve_chat_fullscreen(explicit: bool | None = None) -> bool:
    """Resolve whether an ``agentos chat`` surface should run full-screen.

    Precedence: an ``explicit`` argument wins; otherwise the
    ``AGENTOS_CHAT_FULLSCREEN`` override wins; otherwise full-screen is the
    default for an interactive TTY (a real ``agentos chat`` session) and is
    off for non-TTY / piped contexts (tests, redirected output) so those
    keep the plain native-scrollback behavior.
    """
    if explicit is not None:
        return explicit
    env = _fullscreen_env()
    if env is not None:
        return env
    try:
        return bool(sys.stdout.isatty())
    except Exception:
        return False


def _build_key_bindings() -> KeyBindings:
    """Key bindings shared with the legacy PromptSession driver.

    Ctrl-C clears the current buffer (matches `prompt.py:_key_bindings`).
    Ctrl-D exits the Application by emitting the EOF sentinel on the buffer's
    accept handler so the submit queue surfaces None upstream.
    Ctrl-G invokes the registered cancel callback so the chat REPL can
    cancel an in-flight turn task without tearing down the input surface.
    """
    bindings = KeyBindings()

    @bindings.add("enter")
    def _submit(event) -> None:  # type: ignore[no-untyped-def]
        event.current_buffer.validate_and_handle()

    @bindings.add("escape", "enter")
    @bindings.add("c-j")
    def _newline(event) -> None:  # type: ignore[no-untyped-def]
        # Alt+Enter arrives as Escape followed by Enter. Terminals that can
        # distinguish Shift+Enter commonly emit LF, the same sequence as
        # Ctrl+J, so Ctrl+J is also the portable fallback.
        event.current_buffer.newline(copy_margin=False)

    @bindings.add(Keys.Home)
    @bindings.add("c-a")
    def _start_of_line(event) -> None:  # type: ignore[no-untyped-def]
        buffer = event.current_buffer
        buffer.cursor_position += buffer.document.get_start_of_line_position()

    @bindings.add(Keys.End)
    @bindings.add("c-e")
    def _end_of_line(event) -> None:  # type: ignore[no-untyped-def]
        buffer = event.current_buffer
        buffer.cursor_position += buffer.document.get_end_of_line_position()

    @bindings.add("c-c")
    def _ctrl_c(event) -> None:  # type: ignore[no-untyped-def]
        # Advertised contract (chat_cmd.py banners): "Ctrl+C cancels the
        # current turn or clears input". Invoke the registered cancel
        # callback first — when a turn is in flight the chat REPL cancels
        # it; when idle the callback is a no-op (the chat REPL guards on
        # `turn_task is None or turn_task.done()`). Either way, resetting
        # the buffer matches both advertised behaviors: a fresh prompt for
        # the next input after cancel, or clearing typed-but-unsent text
        # when idle.
        #
        # A SECOND Ctrl-C within `_DOUBLE_CTRL_C_WINDOW_S` of the
        # previous press triggers the registered shutdown callback, which
        # the chat REPL wires to drain the pending deque, finalize any
        # in-flight turn, and exit cleanly. The single-press behavior
        # (cancel + clear buffer) still runs first so the user's typed
        # text is cleared in both cases. When no shutdown callback is
        # registered the double-press detection falls back to single-
        # press behavior — there is no shutdown contract to fire.
        app = event.app
        chat_app = getattr(app, "_chat_application", None)
        if chat_app is not None:
            chat_app._invoke_cancel_callback()
            chat_app._clear_pasted_content()
        app.current_buffer.reset()
        if chat_app is not None:
            chat_app._record_ctrl_c_and_maybe_shutdown()

    @bindings.add("c-d")
    def _eof(event) -> None:  # type: ignore[no-untyped-def]
        # Signal EOF to the consumer; the Application keeps running so the
        # interactive_session() context manager owns lifecycle teardown.
        app = event.app
        chat_app = getattr(app, "_chat_application", None)
        if chat_app is not None:
            chat_app._clear_pasted_content()
            chat_app._emit_eof()
        # Reset the buffer so a fresh prompt repaints after the consumer
        # decides what to do with the EOF signal.
        event.app.current_buffer.reset()

    @bindings.add("c-g")
    def _cancel_turn(event) -> None:  # type: ignore[no-untyped-def]
        # Invoke the registered cancel callback. The chat REPL registers a
        # callable that cancels the in-flight turn task; if no callback is
        # registered this is a no-op so the binding is safe at idle.
        app = event.app
        chat_app = getattr(app, "_chat_application", None)
        if chat_app is not None:
            chat_app._invoke_cancel_callback()

    @bindings.add(Keys.BracketedPaste)
    def _bracketed_paste(event) -> None:  # type: ignore[no-untyped-def]
        app = event.app
        chat_app = getattr(app, "_chat_application", None)
        if chat_app is not None:
            chat_app._insert_pasted_content(event.current_buffer, event.data)
            return
        event.current_buffer.insert_text(event.data)

    def _page_lines(chat_app) -> int:  # type: ignore[no-untyped-def]
        """A page ≈ the transcript pane's visible height (fallback 10)."""
        window = getattr(chat_app, "_transcript_window", None)
        info = getattr(window, "render_info", None) if window else None
        height = getattr(info, "window_height", None)
        return max(1, (height - 1)) if isinstance(height, int) and height > 1 else 10

    @bindings.add(Keys.PageUp)
    def _scroll_up(event) -> None:  # type: ignore[no-untyped-def]
        # Scroll the full-screen transcript pane up into history. No-op on
        # the native-scrollback surface (guarded in scroll_transcript).
        chat_app = getattr(event.app, "_chat_application", None)
        if chat_app is not None:
            chat_app.scroll_transcript(_page_lines(chat_app))

    @bindings.add(Keys.PageDown)
    def _scroll_down(event) -> None:  # type: ignore[no-untyped-def]
        chat_app = getattr(event.app, "_chat_application", None)
        if chat_app is not None:
            chat_app.scroll_transcript(-_page_lines(chat_app))

    return bindings


class ChatApplication:
    """Long-lived prompt-toolkit Application wrapping a single BufferControl.

    The Application stays attached to its event loop for the lifetime of the
    REPL surface. Submitted lines are pushed into an `asyncio.Queue` and
    yielded by `submit_iter()`. Toolbar state lives in a shared dict the
    Application's `bottom_toolbar` callable reads on every redraw, so the
    caller can mutate it via `set_toolbar(...)` without restarting the
    Application.
    """

    def __init__(
        self,
        *,
        surface: Surface = Surface.CLI_GATEWAY,
        toolbar_context: dict[str, object | None],
        bottom_toolbar,
        style=None,
        input: Input | None = None,
        output: Output | None = None,
        completer: Completer | None = None,
        auto_suggest: AutoSuggest | None = None,
        history: History | None = None,
        complete_while_typing: bool = True,
        input_header=None,
        fullscreen: bool | None = None,
    ) -> None:
        self._surface = surface
        self._toolbar_context = toolbar_context
        # Full-screen transcript surface. `_transcript` accumulates the
        # ANSI-encoded conversation; a scrollable pane renders it above the
        # pinned input frame. `_transcript_follow` latches
        # auto-scroll-to-bottom; a user scroll up releases it.
        # `_transcript_scroll` is the from-bottom line offset while not
        # following. When ``fullscreen`` is not passed explicitly the
        # constructor default stays native (env override honored) so a bare
        # ``ChatApplication(...)`` — as tests build it — behaves as the plain
        # native-scrollback surface; the real chat entry point
        # (`open_terminal_surface`) resolves and passes the value.
        if fullscreen is None:
            env = _fullscreen_env()
            fullscreen = env if env is not None else False
        self._fullscreen: bool = fullscreen
        self._transcript: str = ""
        self._transcript_follow: bool = True
        self._transcript_scroll: int = 0
        self._transcript_window: Window | None = None
        self._submit_queue: asyncio.Queue[str | object] = asyncio.Queue()
        self._eof_seen = False
        # Set for the duration of the inline approval suspend window
        # (`prompt.py:prompt_approval_inline`). The output-lock acquirer
        # awaits the inverse before flushing turn-task output so concurrent
        # writes cannot collide with the inline approval `PromptSession`
        # while it owns the screen.
        self._approval_in_flight: asyncio.Event = asyncio.Event()
        # Inverse complement of `_approval_in_flight` — set when the
        # Application is idle (no approval in flight), cleared while an
        # approval owns the screen. Kept as an explicit Event (rather than
        # polling the inverse of `_approval_in_flight`) so consumers can
        # `await self._approval_idle.wait()` and yield to the event loop
        # without spinning. Starts set so the idle-by-default contract holds.
        self._approval_idle: asyncio.Event = asyncio.Event()
        self._approval_idle.set()
        # Serializes terminal write-and-flush only. Rich
        # rendering MUST happen outside this lock so panel rendering time
        # cannot starve concurrent input echo; callers render into a
        # `StringIO` first and acquire the lock for the microsecond
        # `write+flush` window. See `acquire_output` / `write_through`.
        self._output_lock: asyncio.Lock = asyncio.Lock()
        # Optional callback invoked by the Ctrl-G key binding so the chat
        # REPL can cancel an in-flight turn task without the binding needing
        # direct access to the task handle. Registered by the chat loop via
        # `set_cancel_callback`; `None` while no turn is in flight.
        self._cancel_callback: Callable[[], None] | None = None
        # Optional callback invoked when Ctrl-C is pressed twice within
        # `_DOUBLE_CTRL_C_WINDOW_S` of the previous press. The chat
        # REPL registers a callable that emits EOF on the submit queue so
        # the main loop's existing EOF path drains pending work and exits
        # cleanly. `None` falls back to single-press behavior — no double-
        # press detection runs because there is no shutdown contract.
        self._shutdown_callback: Callable[[], None] | None = None
        # Timestamp (`time.monotonic()`) of the most recent Ctrl-C press,
        # or `None` when no press has been seen this window. Used by
        # `_record_ctrl_c_and_maybe_shutdown` to decide whether the next
        # press is the "second of a double". Reset to `None` after a
        # double-press fires so a third press resets the window correctly.
        self._last_ctrl_c_at: float | None = None
        # Set while a streamed assistant turn owns one prompt-toolkit
        # terminal region. Related writes (slash output, approval resume
        # text, tool rows) reuse the same region instead of waiting behind
        # the stream's output lock and risking a deadlock before finalize.
        self._active_stream_writer: Callable[[str], None] | None = None
        # Large bracketed paste payloads are represented in the visible
        # input buffer by stable markers and expanded only when submitted.
        self._pasted_content: dict[str, str] = {}
        self._next_paste_index = 1

        self._buffer = Buffer(
            multiline=True,
            accept_handler=self._on_accept,
            completer=completer,
            auto_suggest=auto_suggest,
            history=history,
            complete_while_typing=complete_while_typing,
            enable_history_search=True,
        )

        def _toolbar_fragments():  # type: ignore[no-untyped-def]
            try:
                rendered = bottom_toolbar() if callable(bottom_toolbar) else bottom_toolbar
            except Exception:
                return []
            if rendered is None:
                return []
            return to_formatted_text(rendered)

        # Persistent left-side prompt prefix. Keep the current input row
        # identified as ``you`` even before text is typed; submitted transcript
        # rows are echoed separately by ``user_input_echo_payload``.
        from agentos.cli.ui import ACCENT, ACCENT_DIM  # noqa: PLC0415

        def _input_prefix_fragments():  # type: ignore[no-untyped-def]
            return to_formatted_text(
                HTML(
                    f"<style fg='{ACCENT}'>◢ </style>"
                    f"<style fg='{ACCENT}'><b>you</b></style>"
                    f"<style fg='{ACCENT}'>  </style>"
                )
            )

        def _input_prefix_width():  # type: ignore[no-untyped-def]
            return Dimension.exact(_ACTIVE_INPUT_PREFIX_WIDTH)

        def _input_height() -> Dimension:
            visible_lines = min(_MAX_INPUT_HEIGHT, self._buffer.document.line_count)
            return Dimension.exact(max(1, visible_lines))

        input_window = VSplit(
            [
                Window(
                    FormattedTextControl(_input_prefix_fragments),
                    width=_input_prefix_width,
                ),
                Window(
                    BufferControl(buffer=self._buffer),
                    height=_input_height,
                    dont_extend_height=True,
                ),
            ],
            height=_input_height,
        )
        toolbar_window = Window(
            FormattedTextControl(_toolbar_fragments),
            height=Dimension.exact(1),
            style="class:bottom-toolbar",
        )

        # Issue #46 §5: frame the active input row with a top and bottom rule
        # (Claude Code style) so the typing area reads as a distinct box between
        # the transcript and the bottom toolbar. A filled-char Window renders a
        # full-width horizontal rule in the muted accent tone.
        def _rule_window() -> Window:  # type: ignore[no-untyped-def]
            return Window(
                height=Dimension.exact(1),
                char="─",
                style=f"fg:{ACCENT_DIM}",
            )

        # Top-of-layout element that owns the vertical slack above the frame.
        #  * Full-screen surface: a scrollable pane that renders the ANSI
        #    conversation transcript and auto-scrolls to the newest line
        #    (cursor pinned to the last logical line; wrap_lines follows it).
        #  * Native-scrollback surface: a greedy empty spacer. In
        #    non-full-screen mode the renderer reserves the rows below the
        #    cursor (large on a fresh launch against a tall terminal); the
        #    spacer absorbs that slack so the framed input + toolbar stay
        #    compact and pinned to the bottom (the input buffer is
        #    `Dimension.exact(1)`, so it no longer balloons to fill the
        #    region — the cause of the over-tall frame).
        top_element: Window
        if self._fullscreen:
            top_element = Window(
                content=_TranscriptControl(
                    lambda: ANSI(self._transcript),
                    scroll=self.scroll_transcript_with_mouse,
                    get_cursor_position=self._transcript_cursor_position,
                    focusable=False,
                    show_cursor=False,
                ),
                wrap_lines=True,
                always_hide_cursor=True,
            )
            self._transcript_window = top_element
        else:
            top_element = Window()
        children: list = [top_element]
        if input_header is not None:

            def _header_fragments():  # type: ignore[no-untyped-def]
                try:
                    rendered = input_header() if callable(input_header) else input_header
                except Exception:
                    return []
                if rendered is None:
                    return []
                return to_formatted_text(rendered)

            children.append(
                Window(
                    FormattedTextControl(_header_fragments),
                    height=Dimension.exact(1),
                )
            )
        children.append(_rule_window())
        children.append(input_window)
        children.append(_rule_window())
        children.append(toolbar_window)
        root = FloatContainer(
            content=HSplit(children),
            floats=[
                Float(
                    left=_ACTIVE_INPUT_PREFIX_WIDTH,
                    ycursor=True,
                    content=CompletionsMenu(
                        max_height=8,
                        scroll_offset=1,
                        display_arrows=True,
                    ),
                )
            ],
        )
        layout = Layout(root)

        self._app: Application[None] = Application(
            layout=layout,
            key_bindings=_build_key_bindings(),
            style=style,
            full_screen=self._fullscreen,
            mouse_support=self._fullscreen,
            refresh_interval=0.1,
            input=input,
            output=output,
        )
        # Back-reference so key handlers can reach the ChatApplication.
        self._app._chat_application = self  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #

    @property
    def application(self) -> Application[None]:
        """Underlying prompt-toolkit Application (read-only handle)."""
        return self._app

    @property
    def surface(self) -> Surface:
        return self._surface

    @property
    def fullscreen(self) -> bool:
        """True when the full-screen transcript-pane surface is active."""
        return self._fullscreen

    # ------------------------------------------------------------------ #
    # Transcript pane (full-screen surface)                              #
    # ------------------------------------------------------------------ #

    def _transcript_cursor_position(self) -> Point:
        """Virtual cursor that drives the pane's auto-scroll.

        With ``wrap_lines`` on, prompt-toolkit scrolls to keep the control's
        cursor visible (``get_vertical_scroll`` is ignored). Parking the
        cursor on the last logical line follows the tail; lifting it by
        ``_transcript_scroll`` lines lets the user scroll back through
        history without following new output.
        """
        # Trailing newline yields an empty final line; anchor on the last
        # line that actually carries text so the tail is not a blank row.
        total_lines = self._transcript.count("\n")
        if self._transcript_follow:
            target = total_lines
        else:
            target = max(0, total_lines - self._transcript_scroll)
        return Point(x=0, y=target)

    def scroll_transcript(self, lines: int) -> None:
        """Scroll the transcript pane by ``lines`` logical lines.

        ``lines > 0`` scrolls up into history (releasing the auto-follow
        latch); ``lines < 0`` scrolls back toward the tail. Reaching the
        bottom re-latches follow so new output resumes auto-scrolling.
        """
        if not self._fullscreen:
            return
        total = self._transcript.count("\n")
        new_scroll = self._transcript_scroll + lines
        # Clamp: 0 == pinned to the tail, total-1 == top of history.
        new_scroll = max(0, min(new_scroll, max(0, total - 1)))
        self._transcript_scroll = new_scroll
        self._transcript_follow = new_scroll == 0
        try:
            self._app.invalidate()
        except Exception:
            pass

    def scroll_transcript_with_mouse(self, lines: int) -> None:
        """Scroll by wheel, compensating when releasing transcript follow."""
        # With ``wrap_lines=True`` prompt-toolkit only re-scrolls the pane when
        # the virtual cursor moves far enough to leave the current viewport.
        # Right at the follow->scroll transition the cursor is still pinned
        # near the tail, so the first wheel tick barely moves it and the pane
        # looks unresponsive ("have to wheel several times before it kicks
        # in"). Compensate by doubling the step on the releasing tick so the
        # cursor visibly exits the viewport on the first wheel event.
        self.scroll_transcript(lines * 2 if self._transcript_follow and lines > 0 else lines)

    def scroll_transcript_to_bottom(self) -> None:
        """Re-pin the transcript pane to the newest line (resume follow)."""
        if not self._fullscreen:
            return
        self._transcript_scroll = 0
        self._transcript_follow = True
        try:
            self._app.invalidate()
        except Exception:
            pass

    def append_transcript(self, text: str) -> None:
        """Append ANSI-encoded text to the full-screen transcript pane.

        Synchronous and ordered: callers append in the same order the
        native-scrollback surface would have written bytes, so streamed
        tokens, tool rows, Rich panels, and echoes stay interleaved
        correctly. Re-follows the tail and repaints.
        """
        if not text:
            return
        self._transcript += text
        # New output re-pins the view to the bottom (matches a terminal that
        # scrolls on write); an explicit user scroll re-latches follow=False.
        self._transcript_follow = True
        self._transcript_scroll = 0
        try:
            self._app.invalidate()
        except Exception:
            pass

    def set_toolbar(self, key: str, value: str | None) -> None:
        """Mutate the shared toolbar dict in place.

        The Application's `bottom_toolbar` callable re-reads this dict on
        every redraw, so a follow-up `invalidate()` (or any user keystroke)
        will pick up the new value. Callers that need an immediate repaint
        should call `self.application.invalidate()` after this.
        """
        self._toolbar_context[key] = value

    @property
    def approval_in_flight(self) -> asyncio.Event:
        """Event surface for the output-lock acquirer.

        The Event is set for the duration of the inline approval suspend
        window and cleared on resume. The output-lock holder waits on the
        Event's "cleared" state before flushing turn-task output so writes
        cannot collide with the inline approval `PromptSession`.
        """
        return self._approval_in_flight

    def set_approval_in_flight(self, value: bool) -> None:
        """Toggle the approval-in-flight / approval-idle Event pair.

        Called by `prompt.py:prompt_approval_inline` immediately around the
        suspend/inline-session/resume window. `_approval_in_flight` and
        `_approval_idle` are kept as mirror complements: one is set iff the
        other is cleared. This lets `wait_approval_idle` block on a real
        `asyncio.Event.wait()` (set semantics) instead of polling the
        inverse of `_approval_in_flight`.
        """
        if value:
            self._approval_in_flight.set()
            self._approval_idle.clear()
        else:
            self._approval_in_flight.clear()
            self._approval_idle.set()

    async def wait_approval_idle(self) -> None:
        """Wait until no approval is in flight.

        Returns immediately when no approval is active (the idle-by-default
        contract). Used by the output-lock acquirer; kept here so the
        Event contract is owned by the Application that mutates it.
        """
        await self._approval_idle.wait()

    # ------------------------------------------------------------------ #
    # Cancel callback                                                     #
    # ------------------------------------------------------------------ #

    def set_cancel_callback(self, cb: Callable[[], None] | None) -> None:
        """Register a callable invoked when Ctrl-G is pressed.

        The chat REPL registers a callable that cancels the in-flight turn
        task; passing `None` clears the registration so a stale binding
        cannot cancel a task that has already completed.
        """
        self._cancel_callback = cb

    def _invoke_cancel_callback(self) -> None:
        """Invoke the registered cancel callback if one is registered.

        Called from the Ctrl-G key binding; swallows any exception raised by
        the callback so a bad callback cannot kill the Application's run
        loop.
        """
        cb = self._cancel_callback
        if cb is None:
            return
        try:
            cb()
        except Exception:
            # The chat REPL surfaces its own diagnostics; the key binding
            # cannot afford to propagate exceptions or it will tear down the
            # input surface.
            pass

    # ------------------------------------------------------------------ #
    # Shutdown callback — Ctrl-C double-press                             #
    # ------------------------------------------------------------------ #

    def set_shutdown_callback(self, cb: Callable[[], None] | None) -> None:
        """Register a callable invoked on a Ctrl-C double-press.

        The chat REPL registers a callable that emits EOF on the submit
        queue so the main loop's existing EOF path drains pending work
        and finalizes any in-flight turn before exiting. Passing `None`
        clears the registration so the binding falls back to single-press
        behavior (no double-press detection runs).
        """
        self._shutdown_callback = cb

    def _invoke_shutdown_callback(self) -> None:
        """Invoke the registered shutdown callback if one is registered.

        Swallows any exception raised by the callback for the same reason
        `_invoke_cancel_callback` does — a bad callback cannot tear down
        the input surface.
        """
        cb = self._shutdown_callback
        if cb is None:
            return
        try:
            cb()
        except Exception:
            pass

    def _record_ctrl_c_and_maybe_shutdown(self) -> None:
        """Track Ctrl-C press timing; fire shutdown on the second press.

        Called from the Ctrl-C key binding AFTER the single-press cancel
        callback has fired and the input buffer has been reset, so both
        halves of the existing contract land before the shutdown path
        runs.

        Double-press detection only runs when a shutdown callback is
        registered — otherwise there is no shutdown contract to fire and
        the timestamp would just churn forever. The first press records
        `time.monotonic()`; a second press within
        `_DOUBLE_CTRL_C_WINDOW_S` of the recorded time invokes the
        shutdown callback and clears the timestamp so a third press
        resets the window (i.e. `_last_ctrl_c_at` becomes the third
        press's time, not the first press's). A late press (outside the
        window) becomes the start of a fresh window.
        """
        if self._shutdown_callback is None:
            return
        now = time.monotonic()
        last = self._last_ctrl_c_at
        if last is not None and (now - last) <= _DOUBLE_CTRL_C_WINDOW_S:
            # Second press inside the window — fire shutdown and reset
            # the window so a follow-up Ctrl-C starts a fresh pair.
            self._last_ctrl_c_at = None
            self._invoke_shutdown_callback()
            return
        # First press, or a late press outside the window — start a new
        # window from this timestamp.
        self._last_ctrl_c_at = now

    # ------------------------------------------------------------------ #
    # Output mutex                                                        #
    # ------------------------------------------------------------------ #

    @property
    def output_lock(self) -> asyncio.Lock:
        """Underlying `asyncio.Lock` for terminal write-and-flush.

        Exposed primarily for tests and advanced callers that need to
        compose the lock with custom logic. Routine callers should use
        `acquire_output()` (auto-gated on the approval-suspend window) or
        `write_through()` (full render-then-flush helper).
        """
        return self._output_lock

    @asynccontextmanager
    async def acquire_output(self):
        """Async context manager: acquire the output mutex with suspend gating.

        Contract:
          1. Awaits the output mutex.
          2. Inside the lock, awaits `wait_approval_idle()` so writes never
             collide with the inline approval `PromptSession` that owns the
             screen during the inline approval suspend window.
          3. Yields. The body MUST do write-and-flush only — Rich rendering
             belongs *outside* the lock (render into `StringIO` first) so a
             slow panel render cannot starve concurrent input echo.

        The order matters: we acquire the lock first so a concurrent writer
        cannot squeeze in between the idle wait and the lock acquisition;
        the idle wait runs while we already own the lock so we cannot let
        another writer through during the suspend window either.
        """
        async with self._output_lock:
            await self.wait_approval_idle()
            yield

    async def write_through(self, payload: str) -> None:
        """Write a pre-rendered payload to the terminal under the output lock.

        The payload is expected to be the result of rendering Rich content
        into a `StringIO` (or any other in-memory buffer) — this helper is
        the canonical "flush a rendered string to the terminal" entrypoint
        for any caller that wants the write-and-flush contract.

        Imports `console` lazily inside the function to avoid a top-level
        circular import with `cli.tui.stream`, which in turn pulls in
        `cli.tui.prompt` (which imports this module).
        """
        if not payload:
            return
        active_stream_writer = self._active_stream_writer
        if active_stream_writer is not None:
            await self.wait_approval_idle()
            if self._active_stream_writer is active_stream_writer:
                active_stream_writer(payload)
                return
        # Local import: `agentos.cli.ui` re-exports `console` from
        # `agentos.ui`. Lazy so the module-level import graph stays flat
        # and tests that monkeypatch `console.file` see the patched object.
        from prompt_toolkit.application.run_in_terminal import in_terminal  # noqa: PLC0415

        from agentos.cli.ui import console  # noqa: PLC0415

        async with self.acquire_output():
            if self._fullscreen and self._app.is_running:
                # Full-screen surface: append into the transcript pane rather
                # than suspending the app to write raw bytes to scrollback.
                self.append_transcript(payload)
                return
            if self._app.is_running:
                async with in_terminal():
                    self._app.output.write_raw(payload)
                    self._app.output.flush()
                return
            console.file.write(payload)
            console.file.flush()

    @asynccontextmanager
    async def stream_output(self):
        """Hold a single terminal region open for one streamed assistant turn.

        Token streams cannot safely enter/exit ``in_terminal()`` per chunk:
        each exit redraws the prompt and can erase a partial response line
        that has not ended with ``\n`` yet. This context hides the prompt once,
        yields a synchronous payload writer for the whole turn, and restores
        the prompt only after the renderer finalizes.
        """
        from prompt_toolkit.application.run_in_terminal import in_terminal  # noqa: PLC0415

        from agentos.cli.ui import console  # noqa: PLC0415

        async with self.acquire_output():
            if self._fullscreen and self._app.is_running:
                # Full-screen surface: the whole turn appends into the
                # transcript pane. No `in_terminal()` hold, so the input
                # frame stays pinned and visible while tokens stream.
                def _write(payload: str) -> None:
                    self.append_transcript(payload)

                self._active_stream_writer = _write
                try:
                    yield _write
                finally:
                    if self._active_stream_writer is _write:
                        self._active_stream_writer = None
                return
            if self._app.is_running:
                async with in_terminal():

                    def _write(payload: str) -> None:
                        if not payload:
                            return
                        self._app.output.write_raw(payload)
                        self._app.output.flush()

                    self._active_stream_writer = _write
                    try:
                        yield _write
                    finally:
                        if self._active_stream_writer is _write:
                            self._active_stream_writer = None
                    return

            def _write(payload: str) -> None:
                if not payload:
                    return
                console.file.write(payload)
                console.file.flush()

            self._active_stream_writer = _write
            try:
                yield _write
            finally:
                if self._active_stream_writer is _write:
                    self._active_stream_writer = None

    async def submit_iter(self) -> AsyncIterator[str]:
        """Yield each submitted line; terminates on Ctrl-D / EOF.

        Stops iteration when the EOF sentinel is dequeued so callers can
        write a natural `async for line in app.submit_iter():` loop.
        """
        while True:
            item = await self._submit_queue.get()
            if item is _EOF_SENTINEL:
                return
            assert isinstance(item, str)
            yield item

    async def next_line(self) -> str | None:
        """Return the next submitted line, or None on EOF.

        Mirrors the contract documented for the `interactive_session()`
        handle in `prompt.py`.
        """
        item = await self._submit_queue.get()
        if item is _EOF_SENTINEL:
            return None
        assert isinstance(item, str)
        return item

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #

    def _on_accept(self, buffer: Buffer) -> bool:
        text = self._expand_pasted_content(buffer.text)
        self._submit_queue.put_nowait(text)
        self._clear_pasted_content()
        # Returning False clears the buffer; the Application keeps running so
        # the next line can be accepted without re-entering an outer loop.
        return False

    def _insert_pasted_content(self, buffer: Buffer, text: str) -> None:
        if not should_collapse_pasted_content(text):
            buffer.insert_text(text)
            return
        marker = pasted_content_summary(text, index=self._next_paste_index)
        self._next_paste_index += 1
        self._pasted_content[marker] = text
        buffer.insert_text(marker)

    def _expand_pasted_content(self, text: str) -> str:
        for marker, content in self._pasted_content.items():
            text = text.replace(marker, content)
        return text

    def _clear_pasted_content(self) -> None:
        self._pasted_content.clear()

    def _emit_eof(self) -> None:
        if self._eof_seen:
            return
        self._eof_seen = True
        self._submit_queue.put_nowait(_EOF_SENTINEL)
