"""Environment-variable spellings must not slip past the sensitive-path block.

Tool dispatch ends in a shell, so `$HOME/.ssh/config` is the real
`~/.ssh/config` by the time anything opens it. The static scanner only ever saw
the literal text, so every prefix in ``_SENSITIVE_PREFIXES`` had a second,
unguarded spelling — a display-vs-executor drift on a layer documented as a
hard block that survives user approval.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentos.sandbox.sensitive_paths import (
    sensitive_path_in_text,
    sensitive_path_marker,
    sensitive_target_in_command,
)


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin ``$HOME`` to the same directory ``~`` expands to.

    Windows resolves ``~`` through ``USERPROFILE``, so the two spellings only
    line up once ``HOME`` is set explicitly.
    """
    resolved = Path.home()
    monkeypatch.setenv("HOME", str(resolved))
    return resolved


@pytest.mark.parametrize(
    ("suffix", "marker"),
    [
        (".ssh/config", "~/.ssh"),
        (".aws/credentials", "~/.aws"),
        (".azure/accessTokens.json", "~/.azure"),
        (".config/gcloud/application_default_credentials.json", "~/.config/gcloud"),
        (".kube/config", "~/.kube"),
        (".gnupg/secring.gpg", "~/.gnupg"),
        (".password-store/bank.gpg", "~/.password-store"),
        (".netrc", "~/.netrc"),
        (".npmrc", "~/.npmrc"),
    ],
)
@pytest.mark.parametrize("spelling", ["$HOME", "${HOME}"])
def test_env_var_home_reads_are_blocked_like_tilde(
    home: Path, suffix: str, marker: str, spelling: str
) -> None:
    assert sensitive_path_in_text(f"cat ~/{suffix}") == marker
    assert sensitive_path_in_text(f"cat {spelling}/{suffix}") == marker


def test_env_var_home_exfiltration_is_blocked(home: Path) -> None:
    assert sensitive_path_in_text("cp $HOME/.aws/credentials /tmp/leak.txt") == "~/.aws"
    # The directory, not ``~/.docker/config``: Docker's file is ``config.json``,
    # which a segment-anchored ``~/.docker/config`` entry never matched (#2623).
    assert sensitive_path_in_text("cat ${HOME}/.docker/config.json") == "~/.docker"


@pytest.mark.parametrize("spelling", ["$HOME", "${HOME}"])
def test_env_var_home_deletes_are_blocked(home: Path, spelling: str) -> None:
    assert sensitive_target_in_command(f"rm {spelling}/.ssh") == "~/.ssh"
    assert sensitive_target_in_command(f"rm -rf {spelling}/.aws") == "~/.aws"


def test_custom_exported_variable_is_expanded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MYHOME", str(Path.home()))

    assert sensitive_path_in_text("cat $MYHOME/.ssh/config") == "~/.ssh"
    assert sensitive_target_in_command("rm -rf ${MYHOME}/.gnupg") == "~/.gnupg"


def test_variable_pointing_at_a_system_prefix_is_expanded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SYSCONF", "/etc")

    assert sensitive_path_in_text("cat $SYSCONF/shadow") == "/etc"
    assert sensitive_target_in_command("rm $SYSCONF/hosts") == "/etc"


def test_sensitive_path_marker_expands_a_bare_token(home: Path) -> None:
    assert sensitive_path_marker("$HOME/.ssh/config") == "~/.ssh"
    assert sensitive_path_marker("${HOME}/.aws/credentials") == "~/.aws"


def test_undefined_variables_stay_unexpanded_and_do_not_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AGENTOS_NOT_A_REAL_VAR", raising=False)

    assert sensitive_path_in_text("cat $AGENTOS_NOT_A_REAL_VAR/notes.txt") is None
    assert sensitive_path_in_text("ls ${AGENTOS_NOT_A_REAL_VAR}") is None


def test_ordinary_text_with_a_dollar_sign_is_untouched(home: Path) -> None:
    assert sensitive_path_in_text("echo 'total is $5'") is None
    assert sensitive_path_in_text("git commit -m 'cost $3 per run'") is None
    assert sensitive_path_in_text("ls $HOME") is None


def test_workspace_exception_survives_the_variable_spelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = Path("/root/.agentos/workspace")
    monkeypatch.setenv("WS", str(workspace))

    assert sensitive_path_in_text("cat $WS/notes/plan.md", workspace=workspace) is None
    assert sensitive_path_marker("$WS/notes/plan.md", workspace=workspace) is None
    # The leaf blocks inside the workspace stay in force.
    assert sensitive_path_marker("$WS/id_rsa", workspace=workspace) == "/id_rsa"


def test_variable_spelling_is_blocked_at_the_shell_tool_boundary(home: Path) -> None:
    """The report's real exploit path: `exec_command` must refuse the command."""
    from agentos.tools.builtin.shell import _sensitive_shell_block

    for command in (
        "cat $HOME/.aws/credentials",
        "cp $HOME/.kube/config /tmp/leak.txt",
        "cat ${HOME}/.gnupg/secring.gpg",
        "rm -rf $HOME/.ssh",
    ):
        blocked = _sensitive_shell_block("exec_command", command)
        assert blocked is not None, command
        assert '"reason": "sensitive_path"' in blocked


def test_root_wipe_hidden_behind_a_variable_is_still_a_root_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ROOTDIR", "/")

    assert sensitive_target_in_command("rm -rf /") == "/"
    assert sensitive_target_in_command("rm -rf $ROOTDIR") == "/"
    assert sensitive_target_in_command("rm -rf ${ROOTDIR}") == "/"
    assert sensitive_target_in_command("rm -rf /tmp/scratch") is None
