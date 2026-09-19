"""Small shared output helpers for scriptable CLI commands."""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

import typer


def _write_json_text(text: str, *, err: bool = False) -> None:
    """Write a JSON line to the requested stream, surviving non-UTF-8 terminals.

    ``json.dumps(..., ensure_ascii=False)`` emits raw non-ASCII characters
    (em-dashes, emoji, non-Latin text). On Windows code pages (cp1252, cp437)
    or other non-UTF-8 default encodings, ``stream.write`` raises
    ``UnicodeEncodeError``. We write UTF-8 bytes straight to the underlying
    binary buffer when one exists (lossless, keeps the JSON contract intact);
    otherwise we fall back to the text layer using ``errors="backslashreplace"``
    so no data is silently destroyed (``replace`` would turn an em-dash into
    ``?``, ``backslashreplace`` keeps it as a ``\\u2014`` escape).
    """
    stream: TextIO = sys.stderr if err else sys.stdout
    line = text + "\n"

    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        try:
            buffer.write(line.encode("utf-8"))
            buffer.flush()
            return
        except (AttributeError, OSError, ValueError):
            # Buffer closed or not writable — fall through to the text layer.
            pass

    encoding = getattr(stream, "encoding", None) or "utf-8"
    # Lossless: unencodable chars become \\uXXXX escapes, not "?".
    stream.write(line.encode(encoding, errors="backslashreplace").decode(encoding))
    stream.flush()


def print_json(payload: Any) -> None:
    """Print JSON payload to stdout using the AgentOS CLI contract."""

    _write_json_text(json.dumps(payload, ensure_ascii=False, default=str))


def print_text(text: str) -> None:
    """Print machine-readable text (CSV) to stdout exactly as given.

    Not through the Rich console: that wraps a redirected stdout at 80 columns,
    which splits a long CSV row into two records, and reads ``[...]`` as markup.
    """

    _write_json_text(text)


def error_payload(
    message: str,
    *,
    code: str | None = None,
    details: Any | None = None,
) -> dict[str, Any]:
    """Build the small AgentOS-owned JSON error envelope."""

    error: dict[str, Any] = {"message": message}
    if code:
        error["code"] = code
    if details is not None:
        error["details"] = details
    return {"error": error}


def emit_error(
    message: str,
    *,
    json_output: bool = False,
    code: str | None = None,
    details: Any | None = None,
) -> None:
    """Emit an error to stderr without polluting JSON stdout."""

    if json_output:
        _write_json_text(
            json.dumps(
                error_payload(message, code=code, details=details),
                ensure_ascii=False,
                default=str,
            ),
            err=True,
        )
    else:
        typer.secho(f"Error: {message}", fg=typer.colors.RED, err=True)
