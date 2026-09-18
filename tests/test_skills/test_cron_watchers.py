"""The bundled cron-watcher scripts.

Their whole contract is "print only what is new, print nothing otherwise" —
that is what makes a cron script job stay quiet — so these drive each script
end to end and assert on stdout and exit code.

Fixtures are served over a loopback HTTP server rather than ``file://``: the
watchers only speak ``http(s)`` now, because a watcher that accepts any scheme
``urlopen`` supports is an arbitrary local-file reader (Issue #1065). Loopback
keeps the run offline and the port is picked by the OS.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
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

RSS = """<?xml version="1.0"?><rss><channel>
<item><title>First post</title><link>https://example.com/1</link><guid>1</guid></item>
<item><title>Second post</title><link>https://example.com/2</link><guid>2</guid></item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><id>tag:a</id><title>Atom one</title><link href="https://example.com/a"/></entry>
</feed>"""


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path / "state"))
    return tmp_path


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # keep pytest's captured stderr about the test, not the server


@pytest.fixture
def base_url(state_dir):
    """Serve ``state_dir`` over loopback HTTP and yield its base URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_QuietHandler, directory=str(state_dir)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def _run(script: str, *args: str, env_home: Path) -> subprocess.CompletedProcess[str]:
    # A deliberately small environment, so a watcher cannot reach the
    # developer's own state. Windows is the exception: a child started without
    # the system variables cannot initialise Winsock, and every fetch then dies
    # with `WinError 10106` before it reaches the fixture server. State stays
    # isolated either way — the watermark store reads AGENTOS_STATE_DIR first.
    if os.name == "nt":
        env = dict(os.environ)
        env["USERPROFILE"] = str(env_home)
    else:
        env = {"PATH": "/usr/bin:/bin"}
    env["AGENTOS_STATE_DIR"] = str(env_home / "state")
    env["HOME"] = str(env_home)
    # Loopback must never go through a proxy the runner happens to configure.
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def _feed(tmp_path: Path, base: str, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return f"{base}/{name}"


# ── watch_rss ───────────────────────────────────────────────────────────────


def test_rss_first_run_is_silent(state_dir, base_url):
    url = _feed(state_dir, base_url, "feed.xml", RSS)

    result = _run("watch_rss.py", "--url", url, "--name", "t", env_home=state_dir)

    assert result.returncode == 0
    assert result.stdout == ""


def test_rss_first_run_can_report_everything(state_dir, base_url):
    url = _feed(state_dir, base_url, "feed.xml", RSS)

    result = _run(
        "watch_rss.py", "--url", url, "--name", "t", "--first-run-reports", env_home=state_dir
    )

    assert result.returncode == 0
    assert "First post" in result.stdout
    assert "Second post" in result.stdout


def test_rss_reports_only_what_is_new(state_dir, base_url):
    url = _feed(state_dir, base_url, "feed.xml", RSS)
    _run("watch_rss.py", "--url", url, "--name", "t", env_home=state_dir)

    _feed(
        state_dir,
        base_url,
        "feed.xml",
        RSS.replace(
            "</channel>",
            "<item><title>Third post</title><link>https://example.com/3</link>"
            "<guid>3</guid></item></channel>",
        ),
    )
    result = _run("watch_rss.py", "--url", url, "--name", "t", env_home=state_dir)

    assert result.returncode == 0
    assert "Third post" in result.stdout
    assert "First post" not in result.stdout


def test_rss_unchanged_feed_stays_silent(state_dir, base_url):
    url = _feed(state_dir, base_url, "feed.xml", RSS)
    _run("watch_rss.py", "--url", url, "--name", "t", env_home=state_dir)

    result = _run("watch_rss.py", "--url", url, "--name", "t", env_home=state_dir)

    assert result.returncode == 0
    assert result.stdout == ""


def test_rss_reads_atom_entries(state_dir, base_url):
    url = _feed(state_dir, base_url, "atom.xml", ATOM)

    result = _run(
        "watch_rss.py", "--url", url, "--name", "a", "--first-run-reports", env_home=state_dir
    )

    assert result.returncode == 0
    assert "Atom one" in result.stdout


def test_rss_prioritizes_atom_alternate_link_over_self(state_dir, base_url):
    atom_multi_link = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>tag:multi</id>
    <title>Post with self and alternate</title>
    <link rel="self" href="https://example.com/feed.atom"/>
    <link rel="alternate" type="text/html" href="https://example.com/post-permalink"/>
  </entry>
</feed>"""
    url = _feed(state_dir, base_url, "multi.xml", atom_multi_link)

    result = _run(
        "watch_rss.py", "--url", url, "--name", "m", "--first-run-reports", env_home=state_dir
    )

    assert result.returncode == 0
    assert "https://example.com/post-permalink" in result.stdout
    assert "https://example.com/feed.atom" not in result.stdout


def test_rss_fails_loudly_on_a_broken_feed(state_dir, base_url):
    url = _feed(state_dir, base_url, "broken.xml", "not xml at all")

    result = _run("watch_rss.py", "--url", url, "--name", "b", env_home=state_dir)

    assert result.returncode == 1
    assert "not valid XML" in result.stderr


def test_watermarks_are_per_name(state_dir, base_url):
    url = _feed(state_dir, base_url, "feed.xml", RSS)
    _run("watch_rss.py", "--url", url, "--name", "one", env_home=state_dir)

    result = _run(
        "watch_rss.py", "--url", url, "--name", "two", "--first-run-reports", env_home=state_dir
    )

    assert "First post" in result.stdout


# ── watch_http_json ─────────────────────────────────────────────────────────


def _events(tmp_path: Path, base: str, items: list[dict]) -> str:
    return _feed(tmp_path, base, "events.json", json.dumps({"data": {"events": items}}))


def test_json_reports_only_new_items(state_dir, base_url):
    url = _events(state_dir, base_url, [{"event_id": "a1", "title": "Deploy finished"}])
    args = ("--url", url, "--name", "j", "--id-field", "event_id", "--items-path", "data.events")
    _run("watch_http_json.py", *args, env_home=state_dir)

    _events(state_dir, base_url, [{"event_id": "a2", "title": "Alert cleared"}])
    result = _run("watch_http_json.py", *args, env_home=state_dir)

    assert result.returncode == 0
    assert result.stdout.strip() == "- Alert cleared"


def test_json_accepts_a_top_level_list(state_dir, base_url):
    url = _feed(state_dir, base_url, "list.json", json.dumps([{"id": "x", "name": "thing"}]))

    result = _run(
        "watch_http_json.py",
        "--url",
        url,
        "--name",
        "l",
        "--first-run-reports",
        env_home=state_dir,
    )

    assert result.returncode == 0
    assert "thing" in result.stdout


def test_json_reports_the_requested_fields(state_dir, base_url):
    url = _events(state_dir, base_url, [{"event_id": "a1", "title": "t", "sev": "high"}])

    result = _run(
        "watch_http_json.py",
        "--url",
        url,
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


def test_json_fails_when_the_path_holds_no_list(state_dir, base_url):
    url = _events(state_dir, base_url, [])

    result = _run(
        "watch_http_json.py",
        "--url",
        url,
        "--name",
        "n",
        "--items-path",
        "data.missing",
        env_home=state_dir,
    )

    assert result.returncode == 1
    assert "Expected a list" in result.stderr


# ── URL scheme guard (Issue #1065) ──────────────────────────────────────────


@pytest.mark.parametrize("script", ["watch_rss.py", "watch_http_json.py"])
def test_watcher_refuses_a_file_url(script, state_dir):
    # urlopen speaks file:// as happily as http://, so an unguarded --url made
    # every watcher an arbitrary local-file reader that reported the contents
    # on each run. Asserted at the real entry point, not just at the helper.
    secret = state_dir / "secret.json"
    secret.write_text('[{"id": "1", "name": "leaked"}]', encoding="utf-8")

    result = _run(script, "--url", secret.as_uri(), "--name", "t", env_home=state_dir)

    assert result.returncode == 1
    assert "must be http:// or https://" in result.stderr
    assert "leaked" not in result.stdout


@pytest.mark.parametrize("url", ["ftp://example.com/x", "data:application/json,[]", "/etc/passwd"])
def test_watcher_refuses_other_non_http_schemes(url, state_dir):
    result = _run("watch_http_json.py", "--url", url, "--name", "t", env_home=state_dir)

    assert result.returncode == 1
    assert "Refusing to fetch" in result.stderr


# ── watch_github ────────────────────────────────────────────────────────────


def test_github_rejects_a_malformed_repo(state_dir):
    result = _run("watch_github.py", "--repo", "not-a-repo", env_home=state_dir)

    assert result.returncode == 1
    assert "owner/name" in result.stderr


def test_github_rejects_an_unknown_scope(state_dir):
    result = _run("watch_github.py", "--repo", "o/n", "--scope", "stars", env_home=state_dir)

    assert result.returncode == 2  # argparse rejects the choice


# ── --limit must not consume the backlog (Issue #1674) ──────────────────────


def _watermark_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("_watermark", SCRIPTS / "_watermark.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_select_new_commits_only_the_ids_it_reports(state_dir):
    """`select_new` used to mark every fresh id seen while the watcher printed
    only the first ``--limit`` of them, so a busy feed lost the surplus for
    good. The limit caps one run's report; the rest comes back next run.

    Feeds list newest first, so ``ids`` here is newest first too and a capped
    run drains from the old end: the deferred ids are the newest, which stay on
    the page longest."""
    watermark = _watermark_module()
    ids = [f"item-{i:02d}" for i in range(25, 0, -1)]  # item-25 (newest) .. item-01
    oldest_five = ids[-5:]

    assert watermark.select_new("demo", oldest_five, limit=10) == []  # first run adopts silently
    second = watermark.select_new("demo", ids, limit=10)
    third = watermark.select_new("demo", ids, limit=10)
    fourth = watermark.select_new("demo", ids, limit=10)

    assert second == ids[10:20]  # item-15 .. item-06: the oldest unseen ten
    assert third == ids[0:10]  # item-25 .. item-16
    assert fourth == []
    assert watermark.load_seen("demo") == [*oldest_five, *ids[10:20], *ids[0:10]]


def test_select_new_without_a_limit_reports_everything(state_dir):
    watermark = _watermark_module()
    watermark.select_new("all", ["a"])

    assert watermark.select_new("all", ["a", "b", "c"]) == ["b", "c"]
    assert watermark.select_new("all", ["a", "b", "c"]) == []


def test_select_new_first_run_still_adopts_the_whole_feed_silently(state_dir):
    """The silent first run is deliberate: it must not leave a backlog behind."""
    watermark = _watermark_module()
    ids = [f"item-{i:02d}" for i in range(30)]

    assert watermark.select_new("quiet", ids, limit=10) == []
    assert watermark.load_seen("quiet") == ids
    assert watermark.select_new("quiet", ids, limit=10) == []


def test_select_new_first_run_reports_respects_the_limit_too(state_dir):
    watermark = _watermark_module()
    ids = [f"item-{i:02d}" for i in range(12)]

    first = watermark.select_new("loud", ids, limit=10, first_run_reports=True)
    second = watermark.select_new("loud", ids, limit=10, first_run_reports=True)

    assert first == ids[2:]
    assert second == ids[:2]


# ── an empty first run is still a run (Issue #1946) ─────────────────────────


def test_select_new_reports_the_first_item_on_a_feed_that_started_empty(state_dir):
    """A watcher adopted onto an empty feed -- a fresh repo, a drained queue --
    used to read its own empty watermark back as "never ran", so the second run
    counted as the first and silently adopted the first real item instead of
    reporting it. The item was gone for good: by the third run the state was no
    longer empty."""
    watermark = _watermark_module()

    assert watermark.select_new("empty", []) == []  # adopts an empty feed, silently
    assert watermark.select_new("empty", ["item-1"]) == ["item-1"]
    assert watermark.select_new("empty", ["item-1"]) == []


def test_select_new_treats_a_written_watermark_as_having_run(state_dir):
    watermark = _watermark_module()
    watermark.select_new("empty", [])

    assert watermark.watermark_path("empty").exists()
    assert watermark.load_seen("empty") == []


def test_select_new_adopts_silently_when_the_watermark_is_unreadable(state_dir):
    """Corrupt state is not a record of what was reported, so it must not be
    read as "ran before" -- that would dump the whole feed into the chat."""
    watermark = _watermark_module()
    path = watermark.watermark_path("broken")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert watermark.select_new("broken", ["a", "b"]) == []
    assert watermark.select_new("broken", ["a", "b", "c"]) == ["c"]


def test_select_new_deduplicates_ids_within_one_poll(state_dir):
    """A feed that lists the same entry twice should report it once and spend
    one slot of the remembered-ids budget, not two."""
    watermark = _watermark_module()

    assert watermark.select_new("dupes", ["a", "a", "b"], first_run_reports=True) == ["a", "b"]
    assert watermark.load_seen("dupes") == ["a", "b"]
    assert watermark.select_new("dupes", ["a", "b", "c", "c"]) == ["c"]
    assert watermark.load_seen("dupes") == ["a", "b", "c"]


def test_save_seen_collapses_duplicates_so_the_budget_is_not_wasted(state_dir):
    """The trim keeps the newest MAX_REMEMBERED_IDS. A repeated id holding
    several of those slots shortens the watcher's real memory, so an older id
    falls off the end sooner and can be reported a second time."""
    watermark = _watermark_module()

    watermark.save_seen("budget", ["a", "b", "a", "c", "b"])

    assert watermark.load_seen("budget") == ["a", "b", "c"]


def test_duplicates_do_not_consume_the_limit_twice(state_dir):
    """The cap counts distinct fresh ids: a duplicate must not push a real item
    out of this run's report."""
    watermark = _watermark_module()
    watermark.select_new("cap", [])

    assert watermark.select_new("cap", ["a", "a", "b"], limit=2) == ["a", "b"]


def test_rss_reports_the_first_item_on_a_feed_that_started_empty(state_dir, base_url):
    """The same swallowed-item bug, at the real entry point."""
    empty = """<?xml version="1.0"?><rss><channel></channel></rss>"""
    url = _feed(state_dir, base_url, "feed.xml", empty)
    first = _run("watch_rss.py", "--url", url, "--name", "e", env_home=state_dir)

    _feed(
        state_dir,
        base_url,
        "feed.xml",
        empty.replace(
            "</channel>",
            "<item><title>First real post</title><guid>1</guid></item></channel>",
        ),
    )
    second = _run("watch_rss.py", "--url", url, "--name", "e", env_home=state_dir)

    assert first.returncode == 0 and first.stdout == ""
    assert second.returncode == 0
    assert second.stdout.strip() == "- First real post"


@pytest.mark.parametrize("limit", ["0", "-1", "ten"])
def test_limit_must_be_a_positive_integer(limit, state_dir):
    """A cap of 0 or less would report nothing and commit nothing, forever."""
    result = _run(
        "watch_rss.py",
        "--url",
        "http://127.0.0.1:1/x",
        "--name",
        "t",
        "--limit",
        limit,
        env_home=state_dir,
    )

    assert result.returncode == 2  # argparse rejects the value
    assert "--limit" in result.stderr


def test_rss_surplus_past_the_limit_is_reported_on_the_next_run(state_dir, base_url):
    url = _feed(state_dir, base_url, "feed.xml", RSS)
    _run("watch_rss.py", "--url", url, "--name", "t", env_home=state_dir)
    _feed(
        state_dir,
        base_url,
        "feed.xml",
        RSS.replace(
            "</channel>",
            "<item><title>Third post</title><guid>3</guid></item>"
            "<item><title>Fourth post</title><guid>4</guid></item></channel>",
        ),
    )

    first = _run("watch_rss.py", "--url", url, "--name", "t", "--limit", "1", env_home=state_dir)
    second = _run("watch_rss.py", "--url", url, "--name", "t", "--limit", "1", env_home=state_dir)
    third = _run("watch_rss.py", "--url", url, "--name", "t", "--limit", "1", env_home=state_dir)

    # The feed lists newest last here, so the tail -- Fourth -- is drained first.
    assert first.returncode == 0 and first.stdout.strip() == "- Fourth post"
    assert second.returncode == 0 and second.stdout.strip() == "- Third post"
    assert third.returncode == 0 and third.stdout == ""
