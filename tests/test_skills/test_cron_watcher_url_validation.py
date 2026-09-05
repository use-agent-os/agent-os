"""Security tests: verify cron-watcher scripts reject non-http(s) URL schemes.

The watch_rss.py and watch_http_json.py scripts accept --url from the operator
and feed it directly to urllib.request.urlopen.  file:// and other schemes let
a malicious operator read arbitrary local files — this is the bug reported in
#1065.  The fix is a validate_http_url() guard that is tested here.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SCRIPTS = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "agentos"
    / "skills"
    / "bundled"
    / "cron-watchers"
    / "scripts"
)


# ── Local HTTP server that serves static test content ──────────────────────

RSS_FEED = b"""<?xml version="1.0"?><rss><channel>
<item><title>Test item</title><link>https://example.com/1</link><guid>1</guid></item>
</channel></rss>"""

JSON_FEED = json.dumps({
    "data": {"events": [{"event_id": "a1", "title": "Deploy finished"}]}
}).encode()


class _Handler(BaseHTTPRequestHandler):
    """Minimal handler that serves fixed responses and tracks requests."""

    requests: list[str] = []

    def log_message(self, format, *args):
        pass  # suppress console noise in tests

    def do_GET(self):
        _Handler.requests.append(self.path)
        if self.path == "/rss":
            body = RSS_FEED
            ctype = "application/xml"
        elif self.path == "/json":
            body = JSON_FEED
            ctype = "application/json"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def http_server() -> Generator[str, None, None]:
    """Start a local HTTP server on a random free port and return its base URL."""
    _Handler.requests.clear()

    # Pick a free port by binding, then close that probe socket and rebind via
    # HTTPServer on the same port.  SO_REUSEADDR lets us race-free reuse in case
    # the kernel reuses the port before HTTPServer starts.
    class _ReuseHTTPServer(HTTPServer):
        allow_reuse_address = True

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = _ReuseHTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    yield base
    server.shutdown()
    sock.close()


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path / "state"))
    return tmp_path


def _run(script: str, *args: str):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


# ── Valid http(s) URLs work ─────────────────────────────────────────────────

def test_rss_accepts_http_url(http_server, state_dir):
    result = _run(
        "watch_rss.py",
        "--url", f"{http_server}/rss",
        "--name", "t",
        "--first-run-reports",
    )

    assert result.returncode == 0, result.stderr
    assert "Test item" in result.stdout


def test_json_accepts_http_url(http_server, state_dir):
    result = _run(
        "watch_http_json.py",
        "--url", f"{http_server}/json",
        "--name", "j",
        "--id-field", "event_id",
        "--items-path", "data.events",
        "--first-run-reports",
    )

    assert result.returncode == 0, result.stderr
    assert "Deploy finished" in result.stdout


# ── file:// is rejected ─────────────────────────────────────────────────────

def test_rss_rejects_file_scheme(http_server, state_dir):
    file_url = f"{http_server}/rss".replace("http://", "file://")
    result = _run("watch_rss.py", "--url", file_url, "--name", "f")

    assert result.returncode == 1
    assert "file://" in result.stderr or "scheme" in result.stderr.lower()


def test_json_rejects_file_scheme(http_server, state_dir):
    result = _run(
        "watch_http_json.py",
        "--url", f"{http_server}/json".replace("http://", "file://"),
        "--name", "j",
        "--id-field", "event_id",
    )

    assert result.returncode == 1
    assert "file://" in result.stderr or "scheme" in result.stderr.lower()


# ── ftp:// is rejected ─────────────────────────────────────────────────────

def test_rss_rejects_ftp_scheme():
    result = _run("watch_rss.py", "--url", "ftp://ftp.example.com/feed.xml", "--name", "f")

    assert result.returncode == 1
    assert "ftp://" in result.stderr or "scheme" in result.stderr.lower()


def test_json_rejects_ftp_scheme():
    result = _run(
        "watch_http_json.py",
        "--url", "ftp://ftp.example.com/data.json",
        "--name", "j",
        "--id-field", "event_id",
    )

    assert result.returncode == 1
    assert "ftp://" in result.stderr or "scheme" in result.stderr.lower()


# ── http://localhost / 127.0.0.1 is accepted (localhost is http, not file) ─

def test_rss_accepts_localhost(http_server):
    # 127.0.0.1 is a legitimate loopback address for local dev — not a file read.
    result = _run(
        "watch_rss.py",
        "--url", f"{http_server}/rss",
        "--name", "l",
        "--first-run-reports",
    )

    assert result.returncode == 0, result.stderr


# ── empty / missing URL is rejected ────────────────────────────────────────

def test_rss_rejects_empty_url():
    result = _run("watch_rss.py", "--url", "", "--name", "e")

    assert result.returncode == 1
    assert "empty" in result.stderr.lower()


def test_json_rejects_empty_url():
    result = _run("watch_http_json.py", "--url", "", "--name", "e", "--id-field", "id")

    assert result.returncode == 1
    assert "empty" in result.stderr.lower()
