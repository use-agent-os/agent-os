"""Small shared output helpers for scriptable CLI commands."""

from __future__ import annotations

import json
import sys
from typing import Any

import typer


def _write_encoded_json(text: str, *, err: bool = False) -> None:
    stream = sys.stderr if err else sys.stdout
    line = text + "\n"
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        try:
            buffer.write(line.encode("utf-8"))
            buffer.flush()
            return
        except (AttributeError, OSError, TypeError):
            pass
    try:
        stream.write(line)
        stream.flush()
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "utf-8"
        encoded = line.encode(encoding, errors="replace").decode(encoding)
        stream.write(encoded)
        stream.flush()


def print_json(payload: Any) -> None:
    """Print JSON payload to stdout using the AgentOS CLI contract."""

    text = json.dumps(payload, ensure_ascii=False, default=str)
    _write_encoded_json(text, err=False)


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
        text = json.dumps(
            error_payload(message, code=code, details=details),
            ensure_ascii=False,
            default=str,
        )
        _write_encoded_json(text, err=True)
    else:
        typer.secho(f"Error: {message}", fg=typer.colors.RED, err=True)
