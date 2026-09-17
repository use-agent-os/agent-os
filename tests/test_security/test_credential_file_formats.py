"""Issue #2620: ``.pgpass`` and ``.netrc`` are named credential files whose
passwords still reached the model.

Both are in ``_CREDENTIAL_FILE_NAMES``, so ``reads_credential_file`` fired
for ``cat ~/.pgpass`` and the assignment pass ran -- and found nothing,
because neither format has a ``name=value`` in it. ``.pgpass`` is five
positional ``:``-fields with the password last; ``.netrc`` introduces each
value with a keyword (``password P``). The only two files in the set whose
whole purpose is a plaintext password were the two the pass could not read.

Each now gets a rule keyed on the file's basename, on both surfaces the
file can reach the model through: ``redact_terminal_output`` (``cat``) and
``redact_file_output`` (``read_file`` outside default mode).
"""

from __future__ import annotations

import pytest

from agentos import redact

PG_PASSWORD = "pgpass-hunter2-9f2b"
PGPASS = f"db.host:5432:app:appuser:{PG_PASSWORD}\n"

NETRC_PASSWORD = "netrc-hunter2-7c41"
NETRC = f"machine api.example.com login me password {NETRC_PASSWORD}\n"


# ── the issue's reproduction, on both surfaces ──────────────────────────────


@pytest.mark.parametrize(
    "command", ["cat ~/.pgpass", "cat /home/u/.pgpass", "less ~/.pgpass", "head -n 5 ~/.pgpass"]
)
def test_a_pgpass_read_in_the_shell_masks_the_password(command: str) -> None:
    out = redact.redact_terminal_output(PGPASS, command)

    assert PG_PASSWORD not in out
    assert out.startswith("db.host:5432:app:appuser:"), "the four public fields stay"


@pytest.mark.parametrize("command", ["cat ~/.netrc", "cat ~/_netrc", "cat C:/Users/u/_netrc"])
def test_a_netrc_read_in_the_shell_masks_the_password(command: str) -> None:
    out = redact.redact_terminal_output(NETRC, command)

    assert NETRC_PASSWORD not in out
    assert "machine api.example.com login me password " in out


@pytest.mark.parametrize("path", ["/home/u/.pgpass", "~/.pgpass", "C:\\Users\\u\\.pgpass"])
def test_a_pgpass_read_as_a_file_masks_the_password(path: str) -> None:
    out = redact.redact_file_output(PGPASS, path=path)

    assert PG_PASSWORD not in out
    assert "«redacted" in out, "file reads use the non-reusable sentinel"


@pytest.mark.parametrize("path", ["/home/u/.netrc", "C:\\Users\\u\\_netrc"])
def test_a_netrc_read_as_a_file_masks_the_password(path: str) -> None:
    out = redact.redact_file_output(NETRC, path=path)

    assert NETRC_PASSWORD not in out
    assert "«redacted" in out


def test_the_gate_was_already_firing_for_both() -> None:
    """Pins the diagnosis: this was never a gate problem."""
    assert redact.reads_credential_file("cat ~/.pgpass") is True
    assert redact.reads_credential_file("cat ~/.netrc") is True


# ── .pgpass format ──────────────────────────────────────────────────────────


def test_every_line_of_a_pgpass_is_masked() -> None:
    text = (
        "prod.db:5432:app:deploy:first-hunter2-aaaa\n"
        "*:*:*:postgres:second-hunter2-bbbb\n"
        "localhost:5433:test:me:third-hunter2-cccc\n"
    )

    out = redact.redact_terminal_output(text, "cat ~/.pgpass")

    for secret in ("first-hunter2-aaaa", "second-hunter2-bbbb", "third-hunter2-cccc"):
        assert secret not in out
    assert out.count("\n") == 3


def test_pgpass_comments_and_blank_lines_are_left_alone() -> None:
    text = "# production\n\n   # indented comment\ndb:5432:app:u:secret-hunter2-x\n"

    out = redact.redact_terminal_output(text, "cat ~/.pgpass")

    assert out.startswith("# production\n\n   # indented comment\n")
    assert "secret-hunter2-x" not in out


def test_a_pgpass_password_with_escaped_colons_is_masked_whole() -> None:
    """``\\:`` is a literal colon inside a field, not a separator; the fifth
    field runs to the end of the line, escapes included."""
    text = "db:5432:app:u:pa\\:ss\\:word-hunter2\n"

    out = redact.redact_terminal_output(text, "cat ~/.pgpass")

    assert "word-hunter2" not in out
    assert "pa\\:ss" not in out
    assert out.startswith("db:5432:app:u:")


def test_an_escaped_backslash_before_a_colon_does_not_hide_the_separator() -> None:
    """``\\\\:`` is a literal backslash followed by a real separator."""
    text = "db:5432:app:us\\\\:secret-hunter2-y\n"

    out = redact.redact_terminal_output(text, "cat ~/.pgpass")

    assert "secret-hunter2-y" not in out
    assert out.startswith("db:5432:app:us\\\\:")


def test_a_malformed_pgpass_line_with_too_few_fields_is_untouched() -> None:
    text = "just:three:fields\n"

    assert redact.redact_terminal_output(text, "cat ~/.pgpass") == text


def test_an_empty_pgpass_password_is_untouched() -> None:
    text = "db:5432:app:u:\n"

    assert redact.redact_terminal_output(text, "cat ~/.pgpass") == text


def test_pgpass_line_endings_are_preserved() -> None:
    text = "db:5432:app:u:secret-hunter2-z\r\nother:1:d:v:secret-hunter2-w"

    out = redact.redact_terminal_output(text, "cat ~/.pgpass")

    assert "\r\n" in out
    assert not out.endswith("\n"), "an unterminated last line stays unterminated"
    assert "secret-hunter2-z" not in out and "secret-hunter2-w" not in out


# ── .netrc format ───────────────────────────────────────────────────────────


def test_a_multi_line_netrc_entry_is_masked() -> None:
    text = "machine api.example.com\n  login me\n  password multi-hunter2-a\n"

    out = redact.redact_terminal_output(text, "cat ~/.netrc")

    assert "multi-hunter2-a" not in out
    assert "  login me\n" in out


def test_netrc_account_is_a_second_password_and_is_masked() -> None:
    text = "machine h login u password pass-hunter2-b account acct-hunter2-c\n"

    out = redact.redact_terminal_output(text, "cat ~/.netrc")

    assert "pass-hunter2-b" not in out
    assert "acct-hunter2-c" not in out


def test_netrc_passwd_spelling_is_masked() -> None:
    text = "machine h login u passwd alt-hunter2-d\n"

    assert "alt-hunter2-d" not in redact.redact_terminal_output(text, "cat ~/.netrc")


def test_a_quoted_netrc_password_is_masked_including_the_quotes() -> None:
    """curl reads quoted values with spaces; the whole quoted token is the secret."""
    text = 'machine h login u password "two words hunter2"\n'

    out = redact.redact_terminal_output(text, "cat ~/.netrc")

    assert "two words hunter2" not in out
    assert "two" not in out.split("password", 1)[1], "no part of the quoted value survives"


def test_netrc_login_and_machine_are_not_masked() -> None:
    """Usernames and hosts are not credentials; masking them would break the
    agent's ability to reason about which entry is which."""
    text = "machine api.example.com login deploy-user password p-hunter2-e\n"

    out = redact.redact_terminal_output(text, "cat ~/.netrc")

    assert "machine api.example.com login deploy-user password " in out


def test_netrc_keyword_matching_is_case_insensitive() -> None:
    text = "MACHINE h LOGIN u PASSWORD upper-hunter2-f\n"

    assert "upper-hunter2-f" not in redact.redact_terminal_output(text, "cat ~/.netrc")


def test_a_netrc_default_entry_is_masked() -> None:
    text = "default login anonymous password anon-hunter2-g\n"

    assert "anon-hunter2-g" not in redact.redact_terminal_output(text, "cat ~/.netrc")


def test_the_word_password_inside_a_value_does_not_start_a_new_match() -> None:
    """``\\b`` anchors the keyword; ``mypassword`` is not ``password``."""
    text = "machine h login mypassword password real-hunter2-h\n"

    out = redact.redact_terminal_output(text, "cat ~/.netrc")

    assert "login mypassword password " in out
    assert "real-hunter2-h" not in out


# ── scope: nothing else changes ─────────────────────────────────────────────


def test_pgpass_shaped_output_from_an_unrelated_command_is_untouched() -> None:
    """A colon-delimited line is only a password when the file is ``.pgpass``."""
    text = "host:5432:db:user:not-a-secret-here\n"

    assert redact.redact_terminal_output(text, "cat notes.txt") == text
    assert redact.redact_terminal_output(text, "psql -c 'select 1'") == text


def test_netrc_keywords_in_ordinary_output_are_untouched() -> None:
    text = "the form has a password field and an account page\n"

    assert redact.redact_terminal_output(text, "cat README.md") == text


def test_a_file_named_like_pgpass_but_not_pgpass_is_untouched() -> None:
    text = "host:5432:db:user:value\n"

    assert redact.redact_terminal_output(text, "cat ~/.pgpass.example") == text
    assert redact.redact_file_output(text, path="/x/pgpass.txt") == text


def test_the_format_rule_runs_only_on_the_named_operand_not_every_argument() -> None:
    """``cp ~/.pgpass /tmp/x`` names the file; the rule keys on that name and
    applies to the output, which is what the caller asked for."""
    assert redact._credential_file_formats_in("cp ~/.pgpass /tmp/x") == {"pgpass"}
    assert redact._credential_file_formats_in("cat ~/.pgpass ~/.netrc") == {"pgpass", "netrc"}
    assert redact._credential_file_formats_in("cat --pgpass x") == set()


def test_the_assignment_pass_still_runs_after_the_format_rule() -> None:
    """Both passes apply: a ``.netrc`` with an ``export`` line pasted in it
    still has that line's value masked by the pass that always ran."""
    text = "machine h login u password p-hunter2-i\nAWS_SECRET_ACCESS_KEY=aws-hunter2-jjjjjjjj\n"

    out = redact.redact_terminal_output(text, "cat ~/.netrc")

    assert "p-hunter2-i" not in out
    assert "aws-hunter2-jjjjjjjj" not in out


def test_the_kill_switch_disables_the_format_rule_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(redact, "_REDACT_ENABLED", False)

    assert redact.redact_terminal_output(PGPASS, "cat ~/.pgpass") == PGPASS
    assert redact.redact_file_output(PGPASS, path="~/.pgpass") == PGPASS


def test_force_overrides_the_kill_switch_for_the_format_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(redact, "_REDACT_ENABLED", False)

    assert PG_PASSWORD not in redact.redact_terminal_output(PGPASS, "cat ~/.pgpass", force=True)


def test_the_file_sentinel_is_non_reusable() -> None:
    """A round trip through the model must not write a truncated password
    back into ``.pgpass``; the file surface uses the invalid-as-a-value form."""
    out = redact.redact_file_output(PGPASS, path="/home/u/.pgpass")

    assert "«redacted" in out
    assert "***" not in out


def test_empty_output_is_returned_as_is() -> None:
    assert redact.redact_terminal_output("", "cat ~/.pgpass") == ""
    assert redact.redact_file_output("", path="~/.netrc") == ""
