"""Issue #2895: http-fetch read the whole response before applying --max-bytes.

``_fetch`` did ``resp.read()`` and the cap was applied to the result, so it
bounded what was printed and nothing else: a large body was held in memory in
full, and a slow or endless stream was waited on until the skill runner's own
timeout killed the process with no output. The read is now capped at
``max_bytes + 1`` (the extra byte is what says "there was more"), so the
existing truncation marker still applies exactly as before.
"""

from __future__ import annotations

import importlib.util
import io
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "src/agentos/skills/bundled/http-fetch/scripts/http_fetch.py"
)


@pytest.fixture(scope="module")
def http_fetch() -> ModuleType:
    spec = importlib.util.spec_from_file_location("http_fetch_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["http_fetch_under_test"] = module
    spec.loader.exec_module(module)
    return module


# ── a loopback server that can trickle, stream forever, or send at once ────


class _Handler(BaseHTTPRequestHandler):
    """Behaviour is selected by path: /bulk/<n>, /trickle/<n>, /endless, /err/<n>."""

    served: int = 0
    lock = threading.Lock()

    def _count(self, n: int) -> None:
        with _Handler.lock:
            _Handler.served += n

    def _chunked(self, chunk: bytes, count: int | None, delay: float) -> None:
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        sent = 0
        while count is None or sent < count:
            try:
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            except OSError:
                return  # the client hung up: exactly what a capped read does
            self._count(len(chunk))
            sent += 1
            if delay:
                time.sleep(delay)
        try:
            self.wfile.write(b"0\r\n\r\n")
        except OSError:
            pass

    def do_GET(self) -> None:
        kind, _, arg = self.path.strip("/").partition("/")
        if kind == "bulk":
            body = b"B" * int(arg)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self._count(len(body))
        elif kind == "trickle":
            self.send_response(200)
            self._chunked(b"t" * 1024, int(arg), 0.05)
        elif kind == "endless":
            self.send_response(200)
            self._chunked(b"e" * 1024, None, 0.01)
        elif kind == "err":
            body = b"E" * int(arg)
            self.send_response(503)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self._count(len(body))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *_args: Any) -> None:
        pass


@pytest.fixture
def server() -> Any:
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    _Handler.served = 0
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


def _run(http_fetch: ModuleType, monkeypatch: pytest.MonkeyPatch, *args: str) -> int:
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    return http_fetch.main(list(args))


# ── the unit: _read_capped ─────────────────────────────────────────────────


class _Resp:
    """A response whose reads are recorded, with a body that may be infinite."""

    def __init__(self, body: bytes | None) -> None:
        self.body = body  # None = never ends
        self.pos = 0
        self.requested: list[int] = []

    def read(self, amt: int | None = None) -> bytes:
        assert amt is not None, "an uncapped read() is the bug"
        self.requested.append(amt)
        if self.body is None:
            return b"z" * amt
        chunk = self.body[self.pos : self.pos + amt]
        self.pos += len(chunk)
        return chunk


def test_read_capped_stops_one_byte_past_the_cap(http_fetch: ModuleType) -> None:
    resp = _Resp(b"x" * 10_000)

    out = http_fetch._read_capped(resp, 100)

    assert len(out) == 101
    assert sum(resp.requested) == 101


def test_read_capped_returns_a_short_body_whole(http_fetch: ModuleType) -> None:
    resp = _Resp(b"short")

    assert http_fetch._read_capped(resp, 100) == b"short"


def test_read_capped_returns_a_body_exactly_at_the_cap_whole(http_fetch: ModuleType) -> None:
    resp = _Resp(b"x" * 100)

    out = http_fetch._read_capped(resp, 100)

    assert len(out) == 100, "no extra byte exists, so nothing is truncated"


def test_read_capped_terminates_on_an_endless_body(http_fetch: ModuleType) -> None:
    resp = _Resp(None)

    out = http_fetch._read_capped(resp, 5_000)

    assert len(out) == 5_001


def test_read_capped_never_asks_for_more_than_a_chunk_at_a_time(http_fetch: ModuleType) -> None:
    """Chunked reads are what let a slow stream return as soon as the cap is
    met, instead of one read that blocks until ``cap`` bytes have trickled."""
    resp = _Resp(None)

    http_fetch._read_capped(resp, 1_000_000)

    assert max(resp.requested) <= http_fetch._READ_CHUNK_BYTES
    assert sum(resp.requested) == 1_000_001


def test_read_capped_handles_an_empty_body(http_fetch: ModuleType) -> None:
    assert http_fetch._read_capped(_Resp(b""), 100) == b""


def test_read_capped_treats_a_negative_cap_as_zero(http_fetch: ModuleType) -> None:
    resp = _Resp(b"x" * 10)

    assert len(http_fetch._read_capped(resp, -5)) == 1


# ── the report: a slow stream no longer runs to the end ────────────────────


def test_a_trickling_stream_returns_as_soon_as_the_cap_is_met(
    http_fetch: ModuleType, monkeypatch: pytest.MonkeyPatch, server: str, capsys: Any
) -> None:
    """300 chunks at 50 ms is a 15 s stream; the 2 KiB cap must not wait for it."""
    started = time.monotonic()

    rc = _run(http_fetch, monkeypatch, "--url", f"{server}/trickle/300", "--max-bytes", "2048")

    elapsed = time.monotonic() - started
    assert rc == 0
    assert elapsed < 7, f"waited {elapsed:.1f}s for a stream the cap should have cut short"
    out = capsys.readouterr().out
    assert len(out) == 2048
    assert out.endswith("…")


def test_a_trickling_stream_is_not_downloaded_past_the_cap(
    http_fetch: ModuleType, monkeypatch: pytest.MonkeyPatch, server: str, capsys: Any
) -> None:
    _run(http_fetch, monkeypatch, "--url", f"{server}/trickle/300", "--max-bytes", "2048")
    time.sleep(0.3)  # let the handler notice the closed socket

    # Whatever was in flight when the client closed, not the 300 KiB stream.
    assert _Handler.served < 64 * 1024


def test_an_endless_stream_terminates(
    http_fetch: ModuleType, monkeypatch: pytest.MonkeyPatch, server: str, capsys: Any
) -> None:
    """SSE, a log tail: the stream never ends. It used to hang until the skill
    runner's 60 s timeout killed the process with no output at all."""
    started = time.monotonic()

    rc = _run(http_fetch, monkeypatch, "--url", f"{server}/endless", "--max-bytes", "4096")

    assert rc == 0
    assert time.monotonic() - started < 7
    assert len(capsys.readouterr().out) == 4096


def test_a_large_body_is_not_held_in_memory_in_full(
    http_fetch: ModuleType, monkeypatch: pytest.MonkeyPatch, server: str, capsys: Any
) -> None:
    """The bytes handed to truncation are bounded by the cap, not by the body."""
    seen: list[int] = []
    real = http_fetch._read_capped

    def spy(resp: Any, max_bytes: int) -> bytes:
        data = real(resp, max_bytes)
        seen.append(len(data))
        return data

    monkeypatch.setattr(http_fetch, "_read_capped", spy)

    rc = _run(http_fetch, monkeypatch, "--url", f"{server}/bulk/5000000", "--max-bytes", "10000")

    assert rc == 0
    assert seen == [10_001]
    assert len(capsys.readouterr().out) == 10_000


# ── the existing contract is untouched ─────────────────────────────────────


def test_a_body_under_the_cap_arrives_whole_and_unmarked(
    http_fetch: ModuleType, monkeypatch: pytest.MonkeyPatch, server: str, capsys: Any
) -> None:
    rc = _run(http_fetch, monkeypatch, "--url", f"{server}/bulk/500", "--max-bytes", "10000")

    assert rc == 0
    out = capsys.readouterr().out
    assert out == "B" * 500


def test_a_body_exactly_at_the_cap_arrives_whole_and_unmarked(
    http_fetch: ModuleType, monkeypatch: pytest.MonkeyPatch, server: str, capsys: Any
) -> None:
    rc = _run(http_fetch, monkeypatch, "--url", f"{server}/bulk/1000", "--max-bytes", "1000")

    assert rc == 0
    assert capsys.readouterr().out == "B" * 1000


def test_a_body_over_the_cap_is_truncated_the_same_way_as_before(
    http_fetch: ModuleType, monkeypatch: pytest.MonkeyPatch, server: str, capsys: Any
) -> None:
    """``raw[: max_bytes - 1] + "…"`` -- deliberately not changed here (#2014, #2278)."""
    rc = _run(http_fetch, monkeypatch, "--url", f"{server}/bulk/1001", "--max-bytes", "1000")

    assert rc == 0
    out = capsys.readouterr().out
    assert out == "B" * 999 + "…"


def test_the_default_cap_still_applies(
    http_fetch: ModuleType, monkeypatch: pytest.MonkeyPatch, server: str, capsys: Any
) -> None:
    rc = _run(http_fetch, monkeypatch, "--url", f"{server}/bulk/2500000")

    assert rc == 0
    assert len(capsys.readouterr().out) == 2_000_000


def test_an_error_body_is_capped_too_and_still_exits_1(
    http_fetch: ModuleType, monkeypatch: pytest.MonkeyPatch, server: str, capsys: Any
) -> None:
    """A 503 page can be as large as any other; the cap applies on that path."""
    rc = _run(http_fetch, monkeypatch, "--url", f"{server}/err/300000", "--max-bytes", "2000")

    assert rc == 1
    captured = capsys.readouterr()
    assert len(captured.out) == 2000
    assert captured.err.startswith("HTTP 503")


def test_a_small_error_body_is_intact(
    http_fetch: ModuleType, monkeypatch: pytest.MonkeyPatch, server: str, capsys: Any
) -> None:
    rc = _run(http_fetch, monkeypatch, "--url", f"{server}/err/20", "--max-bytes", "2000")

    assert rc == 1
    assert capsys.readouterr().out == "E" * 20


def test_a_refused_connection_is_still_exit_2(
    http_fetch: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    rc = _run(http_fetch, monkeypatch, "--url", f"http://127.0.0.1:{port}/", "--timeout", "2")

    assert rc == 2
    assert capsys.readouterr().err.startswith("URLError")
