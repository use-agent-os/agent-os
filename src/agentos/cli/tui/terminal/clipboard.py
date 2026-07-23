"""Best-effort system clipboard for the terminal chat surface.

The full-screen transcript pane copies mouse-selected text through this
helper so users can lift chat content without leaving the app. Terminal
emulators also honor OSC 52, but a native tool is preferred because it is
not gated by emulator-specific allowlists.

Selection of the concrete mechanism is done once at module import:
  1. macOS ``pbcopy`` (always present on Darwin).
  2. Linux/BSD ``wl-copy`` → ``xclip`` → ``xsel``.
  3. Windows ``clip``.
  4. OSC 52 escape written to ``/dev/tty`` (works in most xterm-class
     emulators when the former are missing).
If no mechanism is available the copy is a silent no-op — chat stays
functional, the selection highlight still renders, and the user can still
fall back to their emulator's own copy gesture.
"""

from __future__ import annotations

import base64
import shutil
import subprocess
import sys
from collections.abc import Callable

__all__ = ["copy_to_system_clipboard"]


def _pbcopy(text: str) -> bool:
    try:
        subprocess.run(
            ["pbcopy"],
            input=text.encode("utf-8", errors="replace"),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


def _wl_copy(text: str) -> bool:
    try:
        subprocess.run(
            ["wl-copy"],
            input=text.encode("utf-8", errors="replace"),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


def _xclip(text: str) -> bool:
    try:
        subprocess.run(
            ["xclip", "-selection", "clipboard"],
            input=text.encode("utf-8", errors="replace"),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


def _xsel(text: str) -> bool:
    try:
        subprocess.run(
            ["xsel", "--clipboard", "--input"],
            input=text.encode("utf-8", errors="replace"),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


def _windows_clip(text: str) -> bool:
    try:
        # ``clip`` reads from stdin; encoding must match the console code page.
        # ``utf-8`` works on all supported Windows builds when the input is
        # written as bytes because ``clip`` accepts raw byte streams.
        subprocess.run(
            ["clip"],
            input=text.encode("utf-8", errors="replace"),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


def _osc52(text: str) -> bool:
    """Write an OSC 52 escape directly to the controlling terminal.

    Bypasses prompt-toolkit's output so the escape is not swallowed when
    the full-screen app holds the terminal. Silently reports failure when
    no tty is available (tests, piped output).
    """
    if not sys.stdout.isatty():
        return False
    try:
        payload = base64.b64encode(text.encode("utf-8", errors="replace")).decode("ascii")
        # ``\x07`` (BEL) terminator is the most portable variant.
        sequence = f"\x1b]52;c;{payload}\x07"
        with open("/dev/tty", "w", encoding="utf-8", errors="replace") as tty:
            tty.write(sequence)
            tty.flush()
        return True
    except Exception:
        return False


def _pick_writer() -> Callable[[str], bool] | None:
    """Choose the first available clipboard writer for this platform."""
    if sys.platform == "darwin" and shutil.which("pbcopy"):
        return _pbcopy
    if sys.platform.startswith("linux") or sys.platform.startswith("freebsd"):
        # Wayland first (wl-copy), then X11 (xclip / xsel). Only probe the
        # tools that are actually installed so headless boxes fall through.
        if shutil.which("wl-copy"):
            return _wl_copy
        if shutil.which("xclip"):
            return _xclip
        if shutil.which("xsel"):
            return _xsel
    if sys.platform == "win32":
        return _windows_clip
    # Generic fallback: OSC 52. Always offered — the call itself gates on
    # having a usable tty.
    return _osc52


_WRITER: Callable[[str], bool] | None = _pick_writer()


def copy_to_system_clipboard(text: str) -> bool:
    """Copy ``text`` to the OS clipboard. Returns True on (likely) success.

    "Likely" because clipboard daemons can deny the write even when the
    helper binary exists; the chat UI treats this as fire-and-forget and
    does not surface an error on a False return.
    """
    if not text:
        return False
    if _WRITER is None:
        return False
    try:
        return bool(_WRITER(text))
    except Exception:
        return False
