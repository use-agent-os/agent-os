"""``find-sessions.sh --query`` filters session names, and nothing else.

The help text calls ``--query`` a "case-insensitive substring to filter session
names", but the filter was a ``grep -i`` over whole ``list-sessions`` rows —
name, attached flag and creation date, tab-separated — with the query read as a
basic regular expression. So ``-q sep`` matched every creation date, ``-q 1``
matched the attached column, and a session named ``notes[draft]`` reported as
missing because ``[draft]`` was a character class.

``tmux`` is stubbed with an exported shell function rather than a file on PATH,
so these run wherever ``bash`` does and never touch a real tmux server.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "agentos"
    / "skills"
    / "bundled"
    / "tmux"
    / "scripts"
    / "find-sessions.sh"
)

# name \t session_attached \t session_created_string
_SESSIONS = (
    "claude-main\t1\tWed Sep 16 10:00:00 2026\n"
    "worker-2\t0\tWed Sep 16 10:05:00 2026\n"
    "notes[draft]\t0\tWed Sep 16 10:06:00 2026\n"
    "api.v2\t0\tWed Sep 16 10:07:00 2026\n"
    "apiXv2\t0\tWed Sep 16 10:08:00 2026\n"
    "my session\t0\tWed Sep 16 10:09:00 2026\n"
)


def _no_usable_bash() -> bool:
    """Whether the shell cases can run here.

    The script is bash and the skill declares ``"os": ["darwin", "linux"]``; on
    the Windows runner ``bash`` is the WSL stub, which exits non-zero without
    running anything. ``tests/test_install_scripts.py`` returns early on the
    same platform for ``install_source.sh``. The source-level test at the
    bottom of this file runs everywhere.
    """
    if not sys.platform.startswith("win"):
        return False
    try:
        probe = subprocess.run(
            ["bash", "-c", "printf ok"], capture_output=True, text=True, check=False
        )
    except OSError:
        return True
    return probe.returncode != 0 or probe.stdout.strip() != "ok"


_WRAPPER = """\
tmux() {
  case "$*" in
    *list-sessions*) cat "$SESSIONS_FIXTURE"; return 0 ;;
  esac
  return 1
}
export -f tmux
exec bash "$SCRIPT" "$@"
"""


@pytest.fixture(scope="module")
def wrapper(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    base = tmp_path_factory.mktemp("find-sessions")
    fixture = base / "sessions.txt"
    fixture.write_text(_SESSIONS, encoding="utf-8")
    script = base / "run.sh"
    script.write_text(_WRAPPER, encoding="utf-8")
    return {
        "wrapper": str(script),
        "env": {"SESSIONS_FIXTURE": str(fixture), "SCRIPT": str(SCRIPT)},
    }


def _names(wrapper: dict[str, str], *args: str) -> list[str]:
    """The session names the script reports, in order."""
    env = {**os.environ, **wrapper["env"]}  # type: ignore[dict-item]
    result = subprocess.run(
        ["bash", wrapper["wrapper"], *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return [
        line.strip()[2:].split(" (", 1)[0]
        for line in result.stdout.splitlines()
        if line.startswith("  - ")
    ]


def test_a_query_matches_the_session_name_case_insensitively(wrapper) -> None:
    if _no_usable_bash():
        return

    assert _names(wrapper, "-q", "WORKER") == ["worker-2"]
    assert _names(wrapper, "-q", "claude") == ["claude-main"]
    assert _names(wrapper, "-q", "session") == ["my session"]


def test_a_word_that_only_appears_in_the_creation_date_matches_nothing(wrapper) -> None:
    """Every row was created in Sep; none of these sessions is named that."""
    if _no_usable_bash():
        return

    assert _names(wrapper, "-q", "sep") == []
    assert _names(wrapper, "-q", "wed") == []


def test_the_attached_flag_is_not_part_of_the_name(wrapper) -> None:
    """``1``/``0`` sit in their own column and used to match every row."""
    if _no_usable_bash():
        return

    assert _names(wrapper, "-q", "1") == []
    assert _names(wrapper, "-q", "0") == []


def test_a_name_holding_regex_metacharacters_is_found(wrapper) -> None:
    if _no_usable_bash():
        return

    assert _names(wrapper, "-q", "notes[draft]") == ["notes[draft]"]
    assert _names(wrapper, "-q", "[draft]") == ["notes[draft]"]


def test_a_dot_in_a_query_is_a_dot_and_not_any_character(wrapper) -> None:
    if _no_usable_bash():
        return

    assert _names(wrapper, "-q", "api.v2") == ["api.v2"]


def test_a_star_in_a_query_finds_nothing_rather_than_everything(wrapper) -> None:
    """No session is named with a ``*``; BRE would have made this a repeat."""
    if _no_usable_bash():
        return

    assert _names(wrapper, "-q", "worker*") == []


def test_listing_without_a_query_is_unchanged(wrapper) -> None:
    if _no_usable_bash():
        return

    assert _names(wrapper) == [
        "claude-main",
        "worker-2",
        "notes[draft]",
        "api.v2",
        "apiXv2",
        "my session",
    ]


def test_a_query_that_matches_nothing_says_so_and_still_succeeds(wrapper) -> None:
    if _no_usable_bash():
        return

    env = {**os.environ, **wrapper["env"]}  # type: ignore[dict-item]
    result = subprocess.run(
        ["bash", wrapper["wrapper"], "-q", "nothing-is-called-this"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "No sessions found" in result.stdout


def test_the_attached_label_still_reflects_the_flag(wrapper) -> None:
    if _no_usable_bash():
        return

    env = {**os.environ, **wrapper["env"]}  # type: ignore[dict-item]
    result = subprocess.run(
        ["bash", wrapper["wrapper"]],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert "claude-main (attached, started Wed Sep 16 10:00:00 2026)" in result.stdout
    assert "worker-2 (detached, started Wed Sep 16 10:05:00 2026)" in result.stdout


def test_the_filter_reads_the_name_field_and_not_the_whole_row() -> None:
    """Runs on every platform, including the one that cannot execute the script.

    The behaviour above is what matters; this pins the two properties those
    tests depend on for the runner that cannot exercise them.
    """
    text = SCRIPT.read_text(encoding="utf-8")

    assert "index(tolower($1), q)" in text, "the filter must match the name field"
    assert 'grep -i -- "$query"' not in text, "the whole-row grep must be gone"
