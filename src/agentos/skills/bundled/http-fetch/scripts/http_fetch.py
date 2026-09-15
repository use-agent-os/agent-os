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

# U+2026. Three bytes once encoded, which is the whole reason the cap below
# has to reserve room for it rather than append it to a full buffer.
_TRUNCATION_MARKER = "\u2026".encode()


def _truncate(raw: bytes, max_bytes: int) -> bytes:
    """Cut ``raw`` so the UTF-8 written to stdout stays inside ``max_bytes``.

    Two things have to come out of the budget instead of being added to it:
    the marker, and any character the cut lands in the middle of -- a partial
    sequence decodes to U+FFFD, which is three bytes again on the way back
    out, so it is dropped rather than replaced. An invalid byte *inside* the
    kept region is left alone for the lossy decode to replace, as documented;
    only an incomplete sequence at the cut itself is trimmed.
    """
    cap = max(max_bytes, 0)
    if len(raw) <= cap:
        return raw
    room_for_marker = cap >= len(_TRUNCATION_MARKER)
    keep = raw[: cap - len(_TRUNCATION_MARKER)] if room_for_marker else raw[:cap]
    while keep:
        try:
            keep.decode("utf-8")
        except UnicodeDecodeError as exc:
            if exc.end >= len(keep):
                keep = keep[: exc.start]
                continue
        break
    return keep + _TRUNCATION_MARKER if room_for_marker else keep


def _fetch(
    url: str,
    method: str,
    body: bytes,
    timeout: float,
) -> tuple[int, bytes, str]:
    """Return ``(status, body_bytes, reason)``. Raises on network errors."""
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
            return resp.status, resp.read(), resp.reason
    except urllib.error.HTTPError as exc:
        # Non-2xx: still return the body so callers can inspect.
        return exc.code, (exc.read() if hasattr(exc, "read") else b""), exc.reason


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
        status, raw, reason = _fetch(url, method, body, args.timeout)
    except urllib.error.URLError as exc:
        print(f"URLError: {exc.reason}", file=sys.stderr)
        return 2
    except TimeoutError:
        print(f"timeout after {args.timeout}s", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — surface, don't crash
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    raw = _truncate(raw, args.max_bytes)

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
