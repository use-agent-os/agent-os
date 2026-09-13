"""`agentos --version` — the CLI's own answer for "which build is this".

Before this existed the version was only reachable from outside the tool
(`uv tool list`, `pip show`), which is exactly what issue #1364 reports.
"""

from __future__ import annotations

from typer.testing import CliRunner

from agentos import __version__
from agentos.cli.main import app

runner = CliRunner()


def test_version_flag_prints_the_installed_version_and_exits_zero() -> None:
    """The flag answers with the version alone and succeeds.

    `agentos --version` previously failed with `No such option: --version`,
    so there was no in-tool way to read the version at all.
    """
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_version_flag_is_answered_before_the_subcommand_is_resolved() -> None:
    """The option is eager, so it wins over command resolution.

    Asserting this with a name that is not a command is what makes the
    eagerness visible: a non-eager option would let Click fail on the unknown
    command first and the version would never print.
    """
    result = runner.invoke(app, ["--version", "not-a-real-command"])

    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_bare_invocation_still_shows_help() -> None:
    """Adding a root callback must not change the no-argument behaviour."""
    result = runner.invoke(app, [])

    assert "Usage:" in result.stdout


def test_version_flag_is_listed_in_help() -> None:
    """A flag nobody can discover does not solve the reporter's problem."""
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "--version" in result.stdout


def test_subcommands_still_dispatch_through_the_new_callback() -> None:
    """The root callback sits in front of every command; it must stay inert."""
    for path in (["doctor", "--help"], ["config", "--help"], ["sessions", "--help"]):
        result = runner.invoke(app, path)
        assert result.exit_code == 0, path


def test_short_version_alias() -> None:
    """`-V` is the conventional short form (the macOS app probes with the long one)."""
    result = runner.invoke(app, ["-V"])

    assert result.exit_code == 0
    assert result.stdout.strip() == __version__
