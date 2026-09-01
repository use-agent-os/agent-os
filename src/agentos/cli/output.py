"""Small shared output helpers for scriptable CLI commands."""

from __future__ import annotations

import json
import sys
from typing import Any

import typer


def _safe_echo_utf8(text: str, *, err: bool = False) -> None:
    """Write *text* as a complete UTF-8 line, surviving non-UTF-8 streams.

    Primary path: encode to UTF-8 and write raw bytes to the underlying
    ``sys.stdout.buffer`` (or ``sys.stderr.buffer``).  This bypasses the
    stream's own encoding so non-ASCII characters are never mangled by a
    narrow code-page (cp1252 / cp437 on Windows).

    Fallback: when ``.buffer`` is absent — which happens under pytest's
    ``capsys``, Click's ``CliRunner``, or any other wrapper that replaces
    the standard streams with pure-Python ``StringIO`` objects — we write
    through the text stream with ``errors="replace"`` so the JSON contract
    (one payload, one line, on stdout) survives even if individual
    characters are substituted with U+FFFD.
    """
    stream = sys.stderr if err else sys.stdout
    buf = getattr(stream, "buffer", None)
    if buf is not None:
        buf.write(text.encode("utf-8"))
        buf.write(b"\n")
        buf.flush()
    else:
        # capsys / CliRunner — no raw buffer available.
        # Re-encode through the text stream; replace un-encodable chars
        # rather than crashing.
        encoded = text.encode(stream.encoding or "utf-8", errors="replace")
        stream.write(encoded.decode(stream.encoding or "utf-8", errors="replace"))
        stream.write("\n")
        stream.flush()


def print_json(payload: Any) -> None:
    """Print JSON payload to stdout using the AgentOS CLI contract."""

    _safe_echo_utf8(json.dumps(payload, ensure_ascii=False, default=str))


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
        _safe_echo_utf8(
            json.dumps(
                error_payload(message, code=code, details=details),
                ensure_ascii=False,
                default=str,
            ),
            err=True,
        )
    else:
        typer.secho(f"Error: {message}", fg=typer.colors.RED, err=True)
