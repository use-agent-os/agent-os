"""Issue #2617: ``is_env_dump_command`` judged a segment by its first token alone.

The gate decides whether terminal output gets the name-driven assignment pass.
``tokens[0] in {"env", "printenv", "set", "export", "declare"}`` looked at
nothing before that token and nothing after it, which was wrong both ways:

- ``sudo printenv`` and ``/usr/bin/env`` were not dumps, so the environment
  reached the model with only shape matching -- a ``DATABASE_PASSWORD`` that
  is not ``sk-``/JWT/PEM-shaped went straight through;
- ``set -e``, ``export X=y && ...`` and ``env python3 build.py`` *were* dumps,
  so the output of whatever followed -- a ``cat`` of source, a build log --
  got the pass that ``redact.py`` itself calls "mostly false positives" on
  code, and ``secret_key = self._secret_key`` came back as ``secret_key = ***``.

The command is now found under any wrapper that runs it and by its basename,
and its arguments decide what it does.
"""

from __future__ import annotations

import pytest

from agentos import redact

# An opaque value: the shape pass would catch a vendor-prefixed token with no
# help from the gate, so only an opaque one proves the assignment pass ran.
OPAQUE_PASSWORD = "hunter2-prod-9f2b7c41"
ENV_OUTPUT = f"HOME=/root\nDATABASE_PASSWORD={OPAQUE_PASSWORD}\nPATH=/usr/bin\n"

# Source with a credential-*named* identifier that is not a credential. The
# assignment pass masks it; a code reader must not.
SOURCE_OUTPUT = "class Settings:\n    secret_key = self._secret_key\n"

WRAPPERS = [
    "sudo",
    "sudo -E",
    "sudo -u root",
    "sudo -n",
    "doas",
    "doas -u root",
    "command",
    "exec",
    "nohup",
    "nice",
    "nice -n 10",
    "time",
    "busybox",
]
DUMPS = ["env", "printenv", "/usr/bin/env", "/usr/bin/printenv", "/bin/env"]


# ── the command is found wherever it is ─────────────────────────────────────


@pytest.mark.parametrize("command", DUMPS)
def test_a_dump_by_absolute_path_is_a_dump(command: str) -> None:
    assert redact.is_env_dump_command(command) is True


@pytest.mark.parametrize("wrapper", WRAPPERS)
@pytest.mark.parametrize("dump", ["env", "printenv", "/usr/bin/env"])
def test_a_dump_under_a_wrapper_is_a_dump(wrapper: str, dump: str) -> None:
    assert redact.is_env_dump_command(f"{wrapper} {dump}") is True


def test_wrappers_nest() -> None:
    assert redact.is_env_dump_command("sudo -E nice -n 5 env") is True
    assert redact.is_env_dump_command("nohup command printenv") is True


@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_a_wrapper_with_nothing_to_run_is_not_a_dump(wrapper: str) -> None:
    assert redact.is_env_dump_command(wrapper) is False


def test_a_wrapper_option_argument_is_not_mistaken_for_the_command() -> None:
    """``sudo -u root`` takes ``root`` as the user, not as a program."""
    assert redact.is_env_dump_command("sudo -u root printenv") is True
    assert redact.is_env_dump_command("sudo -u root ls") is False


def test_sudo_dash_n_takes_no_argument_unlike_nice_dash_n() -> None:
    """The same option letter means different things per wrapper, which is
    why the argument-taking options are listed per wrapper."""
    assert redact.is_env_dump_command("sudo -n printenv") is True
    assert redact.is_env_dump_command("nice -n 10 printenv") is True


def test_double_dash_ends_wrapper_options() -> None:
    assert redact.is_env_dump_command("sudo -- printenv") is True
    assert redact.is_env_dump_command("env -- printenv") is False, "env runs printenv here"


# ── arguments decide what the command does ──────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "env python3 build.py",
        "env FOO=bar ./run.sh",
        "env -i PATH=/bin sh -c 'echo hi'",
        "/usr/bin/env python3 -m pytest",
        "/usr/bin/env bash",
        "env -S 'python3 x.py'",
        "env --split-string=python3",
        "sudo env FOO=1 make",
    ],
)
def test_env_running_a_program_is_not_a_dump(command: str) -> None:
    assert redact.is_env_dump_command(command) is False


@pytest.mark.parametrize(
    "command",
    [
        "env",
        "env -0",
        "env -i",
        "env -u HOME",
        "env --unset=HOME",
        "env FOO=bar",
        "env -C /tmp",
        "env --",
    ],
)
def test_env_with_only_options_and_assignments_is_a_dump(command: str) -> None:
    assert redact.is_env_dump_command(command) is True


@pytest.mark.parametrize("command", ["printenv HOME", "printenv -0", "printenv --null HOME PATH"])
def test_printenv_with_names_is_still_a_dump(command: str) -> None:
    """It prints those variables' values, which is environment output."""
    assert redact.is_env_dump_command(command) is True


@pytest.mark.parametrize(
    "command",
    ["set -e", "set -x", "set +x", "set -euo pipefail", "set -o pipefail", "set --", "set -- a b"],
)
def test_set_with_arguments_is_not_a_dump(command: str) -> None:
    assert redact.is_env_dump_command(command) is False


def test_bare_set_is_a_dump() -> None:
    assert redact.is_env_dump_command("set") is True


@pytest.mark.parametrize(
    "command",
    [
        "export API_KEY=abc",
        "export PATH=$PATH:/opt/bin",
        "export FOO",
        "export -n FOO",
        "declare -A colors",
        "declare -i count=0",
        "declare -r NAME=x",
        "typeset -i n",
    ],
)
def test_a_builtin_setting_something_is_not_a_dump(command: str) -> None:
    assert redact.is_env_dump_command(command) is False


@pytest.mark.parametrize(
    "command",
    ["export", "export -p", "declare", "declare -p", "declare -x", "typeset", "typeset -p"],
)
def test_a_builtin_with_nothing_to_set_is_a_dump(command: str) -> None:
    assert redact.is_env_dump_command(command) is True


def test_declare_p_with_a_name_prints_that_value_and_stays_a_dump() -> None:
    assert redact.is_env_dump_command("declare -p API_KEY") is True
    assert redact.is_env_dump_command("declare -px API_KEY") is True
    assert redact.is_env_dump_command("declare -x API_KEY=1") is False, "-x without -p exports"


# ── the shapes an agent actually writes ─────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "set -euo pipefail\ncat src/config.py",
        "export DEBUG=1 && cat src/settings.py",
        "export PATH=$PATH:/opt/bin; make",
        "cd /srv && export FOO=bar && python app.py",
        "env python3 -c 'print(1)'",
    ],
)
def test_common_script_preambles_are_not_dumps(command: str) -> None:
    assert redact.is_env_dump_command(command) is False


@pytest.mark.parametrize(
    "command",
    ["set -e\nprintenv", "export FOO=1 && env", "cd /srv; sudo printenv", "true || /usr/bin/env"],
)
def test_a_dump_after_a_non_dump_segment_is_still_found(command: str) -> None:
    assert redact.is_env_dump_command(command) is True


# ── end to end: what reaches the model ──────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    ["sudo printenv", "/usr/bin/env", "command env", "exec env", "sudo -E /usr/bin/printenv"],
)
def test_a_previously_missed_dump_is_now_masked(command: str) -> None:
    out = redact.redact_terminal_output(ENV_OUTPUT, command)

    assert OPAQUE_PASSWORD not in out
    assert "PATH=/usr/bin" in out, "non-secret lines survive the pass"


@pytest.mark.parametrize("command", ["sudo printenv", "/usr/bin/env"])
def test_a_wrapped_dump_is_masked_exactly_like_the_bare_one(command: str) -> None:
    assert redact.redact_terminal_output(ENV_OUTPUT, command) == redact.redact_terminal_output(
        ENV_OUTPUT, "printenv"
    )


@pytest.mark.parametrize(
    "command",
    ["set -e\ncat src/settings.py", "export DEBUG=1 && cat src/settings.py", "env python3 show.py"],
)
def test_a_previously_flagged_preamble_no_longer_masks_source(command: str) -> None:
    assert redact.redact_terminal_output(SOURCE_OUTPUT, command) == SOURCE_OUTPUT


def test_a_shape_matched_token_is_masked_whatever_the_gate_says() -> None:
    """The gate only controls the assignment pass; shape matching is
    unconditional, so a vendor-prefixed key never depended on it."""
    pat = "ghp_" + "A" * 36
    out = redact.redact_terminal_output(f"TOKEN={pat}\n", "set -e")

    assert pat not in out


# ── what must not change ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("env", True),
        ("printenv | grep KEY", True),
        ("cat x && export", True),
        ("cat notes.txt", False),
        ("echo env", False),
        ("", False),
        (None, False),
        ("cd /srv/app\nprintenv", True),
        ("(printenv)", True),
        ("(cd /srv; printenv)", True),
        ('grep "(env)" notes.txt', False),
        ("echo 'set'\nls", False),
        ("node --env-file=.env app.js", False),
        ("printenv 'unterminated", True),
    ],
)
def test_the_existing_contract_holds(command: str | None, expected: bool) -> None:
    assert redact.is_env_dump_command(command) is expected


def test_a_command_named_like_a_dump_inside_a_path_is_not_one() -> None:
    """Only the basename matters, and only as the whole basename."""
    assert redact.is_env_dump_command("./scripts/env-check.sh") is False
    assert redact.is_env_dump_command("/opt/printenv-wrapper/run") is False
    assert redact.is_env_dump_command("cat env") is False


def test_case_is_not_significant_for_the_command_name() -> None:
    """Windows resolves ``PRINTENV`` the same as ``printenv``."""
    assert redact.is_env_dump_command("PRINTENV") is True
    assert redact.is_env_dump_command("SUDO ENV") is True
