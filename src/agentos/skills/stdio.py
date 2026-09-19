"""UTF-8 stdio for bundled skill scripts, whatever the console code page.

A bundled script runs under the interpreter AgentOS itself runs on
(``{python}`` in a SKILL.md), so this module is importable from every one of
them, and the output it produces is read back by ``exec_command``, which
decodes it as UTF-8. The console in between is the problem: ``print`` encodes
through ``sys.stdout.encoding``, which on Windows is the console code page --
cp1252, cp936, cp932, cp437 -- and under ``PYTHONIOENCODING=ascii`` or a C
locale is not UTF-8 either. A result carrying one character outside that page
raised ``UnicodeEncodeError`` and the script died with exit 1, often after the
real work had already succeeded. The same code page was applied to a payload
read from ``sys.stdin`` and to a child's output read through
``subprocess.run(text=True)``.

This was fixed one batch of scripts at a time (#774, #1548, #1835, #2358),
each batch copying the same helper into each file. #2804 asked for the sweep
to be finished in one pass with the helper in a shared place; this is that
place.

Two shapes, both from #2358's convention:

* :func:`write_stdout` -- for a script that emits one result payload at the
  end. Writes the UTF-8 bytes to ``sys.stdout.buffer`` directly.
* :func:`configure_utf8_stdio` -- for a script that prints progressively, or
  through many ``print`` / ``sys.stdout.write`` sites. Reconfigures the text
  streams once, at the top of ``main``, so every later write encodes as UTF-8.
"""

from __future__ import annotations

import sys

#: Keyword arguments for a ``subprocess.run`` / ``Popen`` whose child emits
#: UTF-8 -- git, ffmpeg, the gmgn CLIs -- so its output is decoded as UTF-8
#: rather than through the locale ``text=True`` would inherit.
SUBPROCESS_UTF8: dict[str, str] = {"encoding": "utf-8", "errors": "replace"}


def write_stdout(text: str) -> None:
    """Write *text* to stdout as UTF-8, surviving a non-UTF-8 stdout encoding.

    The binary buffer is the primary path, matching what a script's ``--out``
    branch already does with ``encoding="utf-8"``. A stream without a usable
    ``buffer`` -- a wrapper, or a captured stdout -- still gets the text,
    escaped rather than lost.
    """
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        try:
            buffer.write(text.encode("utf-8"))
            buffer.flush()
            return
        except (AttributeError, OSError, ValueError):
            # Buffer closed or not writable -- fall through to the text layer.
            pass

    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    # Lossless: unencodable characters become \\uXXXX escapes, not "?".
    sys.stdout.write(text.encode(encoding, errors="backslashreplace").decode(encoding))
    sys.stdout.flush()


def configure_utf8_stdio(*, stdin: bool = False) -> None:
    """Make ``sys.stdout``/``sys.stderr`` -- and with *stdin*, ``sys.stdin`` -- UTF-8.

    Call once at the top of ``main`` before anything is read or written.
    Output uses ``backslashreplace`` so that even a lone surrogate is escaped
    rather than fatal; input uses ``replace`` so an undecodable byte becomes
    U+FFFD rather than an exception. A stream that cannot be reconfigured (a
    test capture, a closed or detached stream) is left as it is: this is a
    best-effort widening, never a reason to fail.
    """
    targets = [("stdout", "backslashreplace"), ("stderr", "backslashreplace")]
    if stdin:
        targets.append(("stdin", "replace"))
    for name, errors in targets:
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors=errors)
        except (ValueError, OSError, TypeError):
            # ValueError: stdin already read from, or the stream is detached.
            continue
