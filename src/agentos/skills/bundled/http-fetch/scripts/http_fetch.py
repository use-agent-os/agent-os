#!/usr/bin/env python3
"""Direct HTTP request — meta-skill entrypoint.

Writes the response body to stdout. Non-2xx HTTP responses exit 1
with the status code on stderr; network failures exit 2.

Used by meta-skills to skip a full sub-Agent loop just to GET/POST a
URL. Not a crawler, not a browser — single request, single body.
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request
from typing import Any

# Read the body this many bytes at a time, so a capped read of a slow stream
# returns as soon as the cap is met instead of waiting for one giant read.
_READ_CHUNK_BYTES = 65_536


def _read_capped(resp: Any, max_bytes: int) -> bytes:
    """Read at most ``max_bytes + 1`` bytes of *resp* and stop.

    ``--max-bytes`` used to be applied after ``resp.read()`` had pulled the
    whole body, so it capped what was printed and nothing else: a large
    download was held in memory in full, and a slow or endless stream (SSE, a
    log tail, a server that trickles) was waited on until the skill runner's
    own timeout killed the process with no output at all (#2895). Reading one
    byte past the cap is what tells the caller the body was longer, so the
    existing truncation marker still applies exactly as before.
    """
    limit = max(max_bytes, 0) + 1
    chunks: list[bytes] = []
    total = 0
    while total < limit:
        chunk = resp.read(min(_READ_CHUNK_BYTES, limit - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _fetch(
    url: str,
    method: str,
    body: bytes,
    timeout: float,
    max_bytes: int,
) -> tuple[int, bytes, str]:
    """Return ``(status, body_bytes, reason)``. Raises on network errors.

    ``body_bytes`` is at most ``max_bytes + 1`` long: the connection is
    closed once the cap is met rather than drained to the end.
    """
    req = urllib.request.Request(  # noqa: S310 — URL is operator-supplied per turn
        url,
        data=body if body else None,
        method=method,
    )
    if not body:
        # Strip Content-Length urllib auto-adds when data=b"" — some servers
        # reject it on GET.
        req.headers.pop("Content-length", None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.status, _read_capped(resp, max_bytes), resp.reason
    except urllib.error.HTTPError as exc:
        # Non-2xx: still return the body so callers can inspect -- capped the
        # same way, an error page can be as large as any other.
        with exc:
            return (
                exc.code,
                (_read_capped(exc, max_bytes) if hasattr(exc, "read") else b""),
                exc.reason,
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--method", default="GET")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--max-bytes", type=int, default=2_000_000)
    args = parser.parse_args(argv)

    method = (args.method or "GET").upper()
    valid_methods = {"GET", "POST", "PUT", "DELETE", "HEAD", "PATCH"}
    if method not in valid_methods:
        print(
            f"unsupported method {method!r}; valid: {sorted(valid_methods)!r}",
            file=sys.stderr,
        )
        return 2

    url = args.url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        print(
            f"invalid url {url!r}: must start with http:// or https://",
            file=sys.stderr,
        )
        return 2

    # Body comes from stdin (per the SKILL.md entrypoint contract).
    body = sys.stdin.buffer.read() if not sys.stdin.isatty() else b""

    try:
        status, raw, reason = _fetch(url, method, body, args.timeout, args.max_bytes)
    except urllib.error.URLError as exc:
        print(f"URLError: {exc.reason}", file=sys.stderr)
        return 2
    except TimeoutError:
        print(f"timeout after {args.timeout}s", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — surface, don't crash
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if len(raw) > args.max_bytes:
        raw = raw[: args.max_bytes - 1] + b"\xe2\x80\xa6"  # … (truncation marker)

    # Lossy decode — meta-skill DAGs need string output for templating.
    text = raw.decode("utf-8", errors="replace")
    sys.stdout.write(text)

    if not (200 <= status < 300):
        preview = text[:200].replace("\n", " ")
        print(f"HTTP {status}: {reason}: {preview}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
