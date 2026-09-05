"""The bundled cron-watcher scripts.

Their whole contract is "print only what is new, print nothing otherwise" —
that is what makes a cron script job stay quiet — so these drive each script
end to end over a local HTTP server (loopback only, mirroring real deployments)
and assert on stdout and exit code.

Historical note: these tests used to drive the scripts over ``file://`` URLs.
That window was closed by the URL-scheme guard added for #1065 — the scripts
now reject any scheme other than http(s), so the fixtures moved to a loopback
HTTP server.
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

# The cron-watcher scripts use urllib.request.urlopen to fetch their URLs.
# On Windows GitHub Actions runners the network stack is flaky under heavy
# parallel test load (WinError 10106: service provider could not be loaded).
# The functional tests need a live server; the security tests (rejecting
# file://) run against a mock and do not need it.
_run_via_http_server = pytest.mark.skipif(
    sys.platform == "win32",
    reason="HTTPServer flaky on Windows CI runners (WinError 10106)",
)

SCRIPTS = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "agentos"
    / "skills"
    / "bundled"
    / "cron-watchers"
    / "scripts"
)

RSS = """<?xml version="1.0"?><rss><channel>
<item><title>First post</title><link>https://example.com/1</link><guid>1</guid></item>
<item><title>Second post</title><link>https://example.com/2</link><guid>2</guid></item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><id>tag:a</id><title>Atom one</title><link href="https://example.com/a"/></entry>
</feed>"""


class _ContentHandler(BaseHTTPRequestHandler):
    """Serves whatever the test put in the registry under the request path."""

    content: dict[str, bytes] = {}

    def log_message(self, format, *args):
        pass  # keep pytest output clean

    def do_GET(self):
        body = _ContentHandler.content.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def http_server() -> Generator[tuple[str, Path], None, None]:
    """A loopback HTTP server; yields ``(base_url, state_dir_holder)``.

    Content is registered under a path key via the returned dict-like store so
    that a test can rewrite the "feed" between runs (watchers must report what
    changed between two GETs of the same URL).
    """
    _ContentHandler.content.clear()

    class _ReuseHTTPServer(HTTPServer):
        allow_reuse_address = True

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = _ReuseHTTPServer(("127.0.0.1", port), _ContentHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}", _ContentHandler.content
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path / "state"))
    return tmp_path


def _run(script: str, *args: str, env_home: Path):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "AGENTOS_STATE_DIR": str(env_home / "state"),
            "HOME": str(env_home),
        },
        timeout=60,
    )


def _feed(content: dict[str, bytes], name: str, body: str) -> str:
    """Register ``body`` under a unique path and return its loopback URL."""
    content[f"/{name}"] = body.encode("utf-8")
    return f"/{name}"


def _url(base: str, path: str) -> str:
    return f"{base}{path}"


# ── watch_rss ───────────────────────────────────────────────────────────────


@_run_via_http_server
def test_rss_first_run_is_silent(state_dir, http_server):
    base, content = http_server
    path = _feed(content, "feed.xml", RSS)

    result = _run("watch_rss.py", "--url", _url(base, path), "--name", "t", env_home=state_dir)

    assert result.returncode == 0
    assert result.stdout == ""


@_run_via_http_server
def test_rss_first_run_can_report_everything(state_dir, http_server):
    base, content = http_server
    path = _feed(content, "feed.xml", RSS)

    result = _run(
        "watch_rss.py",
        "--url", _url(base, path),
        "--name", "t",
        "--first-run-reports",
        env_home=state_dir,
    )

    assert result.returncode == 0
    assert "First post" in result.stdout
    assert "Second post" in result.stdout


@_run_via_http_server
def test_rss_reports_only_what_is_new(state_dir, http_server):
    base, content = http_server
    path = _feed(content, "feed.xml", RSS)
    _run("watch_rss.py", "--url", _url(base, path), "--name", "t", env_home=state_dir)

    _feed(
        content,
        "feed.xml",
        RSS.replace(
            "</channel>",
            "<item><title>Third post</title><link>https://example.com/3</link>"
            "<guid>3</guid></item></channel>",
        ),
    )
    result = _run("watch_rss.py", "--url", _url(base, path), "--name", "t", env_home=state_dir)

    assert result.returncode == 0
    assert "Third post" in result.stdout
    control = _run(
        "watch_rss.py",
        "--url", _url(base, path),
        "--name", "t",
        env_home=state_dir,
    )  # negative control: rerun is silent
    assert control.stdout == ""


@_run_via_http_server
def test_rss_unchanged_feed_stays_silent(state_dir, http_server):
    base, content = http_server
    path = _feed(content, "feed.xml", RSS)
    _run("watch_rss.py", "--url", _url(base, path), "--name", "t", env_home=state_dir)

    result = _run("watch_rss.py", "--url", _url(base, path), "--name", "t", env_home=state_dir)

    assert result.returncode == 0
    assert result.stdout == ""


@_run_via_http_server
def test_rss_reads_atom_entries(state_dir, http_server):
    base, content = http_server
    path = _feed(content, "atom.xml", ATOM)

    result = _run(
        "watch_rss.py",
        "--url", _url(base, path),
        "--name", "a",
        "--first-run-reports",
        env_home=state_dir,
    )

    assert result.returncode == 0
    assert "Atom one" in result.stdout


@_run_via_http_server
def test_rss_fails_loudly_on_a_broken_feed(state_dir, http_server):
    base, content = http_server
    path = _feed(content, "broken.xml", "not xml at all")

    result = _run("watch_rss.py", "--url", _url(base, path), "--name", "b", env_home=state_dir)

    assert result.returncode == 1
    assert "not valid XML" in result.stderr


@_run_via_http_server
def test_watermarks_are_per_name(state_dir, http_server):
    base, content = http_server
    path = _feed(content, "feed.xml", RSS)
    _run("watch_rss.py", "--url", _url(base, path), "--name", "one", env_home=state_dir)

    result = _run(
        "watch_rss.py",
        "--url", _url(base, path),
        "--name", "two",
        "--first-run-reports",
        env_home=state_dir,
    )

    assert "First post" in result.stdout


# ── watch_http_json ─────────────────────────────────────────────────────────


def _events(content: dict[str, bytes], items: list[dict]) -> str:
    return _feed(content, "events.json", json.dumps({"data": {"events": items}}))


@_run_via_http_server
def test_json_reports_only_new_items(state_dir, http_server):
    base, content = http_server
    path = _events(content, [{"event_id": "a1", "title": "Deploy finished"}])
    args = (
        "--url", _url(base, path),
        "--name", "j",
        "--id-field", "event_id",
        "--items-path", "data.events",
    )
    _run("watch_http_json.py", *args, env_home=state_dir)

    _events(content, [{"event_id": "a2", "title": "Alert cleared"}])
    result = _run("watch_http_json.py", *args, env_home=state_dir)

    assert result.returncode == 0
    assert result.stdout.strip() == "- Alert cleared"


@_run_via_http_server
def test_json_accepts_a_top_level_list(state_dir, http_server):
    base, content = http_server
    path = _feed(content, "list.json", json.dumps([{"id": "x", "name": "thing"}]))

    result = _run(
        "watch_http_json.py",
        "--url",
        _url(base, path),
        "--name",
        "l",
        "--first-run-reports",
        env_home=state_dir,
    )

    assert result.returncode == 0
    assert "thing" in result.stdout


@_run_via_http_server
def test_json_reports_the_requested_fields(state_dir, http_server):
    base, content = http_server
    path = _events(content, [{"event_id": "a1", "title": "t", "sev": "high"}])

    result = _run(
        "watch_http_json.py",
        "--url",
        _url(base, path),
        "--name",
        "f",
        "--id-field",
        "event_id",
        "--items-path",
        "data.events",
        "--field",
        "sev",
        "--first-run-reports",
        env_home=state_dir,
    )

    assert "sev='high'" in result.stdout


@_run_via_http_server
def test_json_fails_when_the_path_holds_no_list(state_dir, http_server):
    base, content = http_server
    path = _events(content, [])

    result = _run(
        "watch_http_json.py",
        "--url",
        _url(base, path),
        "--name",
        "n",
        "--items-path",
        "data.missing",
        env_home=state_dir,
    )

    assert result.returncode == 1
    assert "Expected a list" in result.stderr


# ── watch_github ────────────────────────────────────────────────────────────


def test_github_rejects_a_malformed_repo(state_dir):
    result = _run("watch_github.py", "--repo", "not-a-repo", env_home=state_dir)

    assert result.returncode == 1

    assert "owner/name" in result.stderr
