"""``wait-for-text.sh`` validates ``--interval`` the way it validates its siblings.

``--timeout`` and ``--lines`` were checked before the first poll; ``--interval``
was handed straight to ``sleep`` at the bottom of the loop. ``sleep`` only runs
after a poll that did not match, so a bad interval was reported or ignored
depending on whether the pattern was already on the pane -- and when it was
reported it arrived as a ``sleep`` diagnostic under exit 1, the same code the
script uses for "timed out waiting for pattern". ``--interval 0`` passed every
check and turned the poll into a busy loop against the tmux server.

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
    / "wait-for-text.sh"
)

REJECTED = (
    "abc",  # not a number at all
    "-5",  # negative; reached `sleep` as an option, not a value
    "0",  # busy loop
    "0.0",
    ".0",
    "00",
    "1,5",  # decimal comma
    "",  # `--interval` with an empty value
    "1 2",
    "1e-2",  # exponent: GNU sleep takes it, BSD sleep does not
    "0x1",
    "5s",  # a suffix `sleep` accepts but the flag is documented in seconds
)

ACCEPTED = ("0.5", "1", ".25", "2.0", "10", "0.001", "00.5")

# Counts every tmux invocation so a test can assert the script rejected the
# argument before it polled anything.
_WRAPPER = """\
tmux() {
  echo x >> "$TMUX_CALLS"
  case "$*" in
    *capture-pane*) cat "$PANE_FIXTURE"; return 0 ;;
  esac
  return 0
}
export -f tmux
exec bash "$SCRIPT" "$@"
"""


def _no_usable_bash() -> bool:
    """Whether the shell cases can run here.

    The script is bash and the skill declares ``"os": ["darwin", "linux"]``; on
    the Windows runner ``bash`` is the WSL stub, which exits non-zero without
    running anything. The source-level test at the bottom runs everywhere.
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


@pytest.fixture
def run(tmp_path: Path):
    """Run the script against a stubbed tmux; returns the result and poll count."""
    wrapper = tmp_path / "run.sh"
    wrapper.write_text(_WRAPPER, encoding="utf-8")
    pane = tmp_path / "pane.txt"
    calls = tmp_path / "calls.txt"

    def _run(*args: str, pane_text: str = "nothing here\n"):
        pane.write_text(pane_text, encoding="utf-8")
        calls.write_text("", encoding="utf-8")
        env = {
            **os.environ,
            "SCRIPT": str(SCRIPT),
            "PANE_FIXTURE": str(pane),
            "TMUX_CALLS": str(calls),
        }
        result = subprocess.run(
            ["bash", str(wrapper), *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        polls = len([ln for ln in calls.read_text(encoding="utf-8").splitlines() if ln])
        return result, polls

    return _run


@pytest.mark.parametrize("interval", REJECTED)
def test_a_bad_interval_is_refused_before_the_first_poll(run, interval: str) -> None:
    if _no_usable_bash():
        return

    result, polls = run("-t", "s", "-p", "READY", "-T", "2", "-i", interval)

    assert result.returncode == 1
    assert "interval must be a positive number of seconds" in result.stderr
    assert polls == 0, "the script polled tmux before validating the interval"


@pytest.mark.parametrize("interval", REJECTED)
def test_a_bad_interval_is_refused_even_when_the_pattern_is_already_there(
    run, interval: str
) -> None:
    """The direction the issue did not report, and the one that stayed silent.

    ``sleep`` sits after the match check, so a pane that already shows the
    pattern exits 0 on the first poll and the bad flag was never reached.
    """
    if _no_usable_bash():
        return

    result, _ = run("-t", "s", "-p", "READY", "-T", "2", "-i", interval, pane_text="READY\n")

    assert result.returncode == 1
    assert "interval must be a positive number of seconds" in result.stderr


@pytest.mark.parametrize("interval", ACCEPTED)
def test_a_positive_interval_is_accepted(run, interval: str) -> None:
    if _no_usable_bash():
        return

    result, _ = run("-t", "s", "-p", "READY", "-T", "2", "-i", interval, pane_text="READY\n")

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


def test_the_default_interval_still_polls_and_still_finds_the_pattern(run) -> None:
    """No ``-i`` at all: the documented 0.5s default must survive the guard."""
    if _no_usable_bash():
        return

    result, polls = run("-t", "s", "-p", "READY", "-T", "2", pane_text="READY\n")

    assert result.returncode == 0, result.stderr
    assert polls == 1


def test_a_missing_pattern_still_times_out_with_its_own_message(run) -> None:
    """The guard must not turn a real timeout into an argument error."""
    if _no_usable_bash():
        return

    result, polls = run("-t", "s", "-p", "READY", "-T", "1", "-i", "0.25")

    assert result.returncode == 1
    assert "Timed out after 1s waiting for pattern: READY" in result.stderr
    assert "interval must be" not in result.stderr
    assert polls >= 2, "the poll loop should have run more than once"


def test_the_siblings_are_still_validated(run) -> None:
    """Guard: these passed before the fix and must keep passing."""
    if _no_usable_bash():
        return

    bad_timeout, timeout_polls = run("-t", "s", "-p", "READY", "-T", "abc")
    assert bad_timeout.returncode == 1
    assert "timeout must be an integer number of seconds" in bad_timeout.stderr
    assert timeout_polls == 0

    bad_lines, lines_polls = run("-t", "s", "-p", "READY", "-l", "abc")
    assert bad_lines.returncode == 1
    assert "lines must be an integer" in bad_lines.stderr
    assert lines_polls == 0


def test_the_required_arguments_are_still_required(run) -> None:
    """Guard: a missing target must not be reported as a bad interval."""
    if _no_usable_bash():
        return

    result, _ = run("-p", "READY", "-i", "0.5")

    assert result.returncode == 1
    assert "target and pattern are required" in result.stderr


def test_the_interval_is_checked_before_the_poll_loop() -> None:
    """Runs on every platform, including the one that cannot execute the script.

    The behaviour above is what matters; this pins the placement the tests rely
    on -- the guard has to sit with the other argument checks, above the poll
    loop, or a bad value is still only caught once ``sleep`` is reached.
    """
    text = SCRIPT.read_text(encoding="utf-8")

    guard = text.index("interval must be a positive number of seconds")
    assert guard < text.index("while true; do"), "the guard must precede the poll loop"
    assert guard < text.index('sleep "$interval"')
