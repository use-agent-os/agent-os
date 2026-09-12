from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from agentos.redact import CREDENTIAL_FILE_NAMES
from agentos.sandbox.sensitive_paths import (
    _HOST_CREDENTIAL_FILES,
    _is_root_target,
    is_sensitive_path,
    sensitive_path_in_text,
    sensitive_path_marker,
    sensitive_target_in_command,
)


def test_sensitive_path_matches_nested_home_prefixes_with_native_separators() -> None:
    assert is_sensitive_path(str(Path.home() / ".ssh" / "id_rsa")) == "~/.ssh"
    assert is_sensitive_path(str(Path.home() / ".aws" / "credentials")) == "~/.aws"


def test_sensitive_path_in_text_matches_native_separator_paths() -> None:
    key_path = Path.home() / ".ssh" / "id_rsa"

    assert sensitive_path_in_text(f'type "{key_path}"') == "~/.ssh"


def test_active_workspace_under_root_is_not_blocked_by_root_prefix() -> None:
    workspace = Path("/root/.agentos/workspace")

    assert (
        sensitive_path_marker(
            str(workspace / "notes" / "plan.md"),
            workspace=workspace,
        )
        is None
    )
    assert (
        sensitive_path_in_text(
            f"cat {workspace / 'notes' / 'plan.md'}",
            workspace=workspace,
        )
        is None
    )


def test_active_workspace_exception_keeps_leaf_secret_blocks() -> None:
    workspace = Path("/root/.agentos/workspace")

    assert sensitive_path_marker(str(workspace / ".env"), workspace=workspace) in {
        "/.env",
        "/.env*",
    }
    assert sensitive_path_marker(str(workspace / "id_rsa"), workspace=workspace) == "/id_rsa"
    assert sensitive_path_in_text(
        f"cat {workspace / '.env.local'}",
        workspace=workspace,
    ) in {"/.env.local", "/.env*"}


def test_sensitive_command_targets_honor_active_workspace_exception() -> None:
    workspace = Path("/root/.agentos/workspace")

    assert (
        sensitive_target_in_command(
            f"rm {workspace / 'scratch.txt'}",
            workspace=workspace,
        )
        is None
    )
    assert sensitive_target_in_command(
        f"rm {workspace / '.env'}",
        workspace=workspace,
    ) in {"/.env", "/.env*"}


def test_windows_rooted_workspace_targets_keep_leaf_secret_blocks() -> None:
    workspace = Path("/root/.agentos/workspace")

    assert (
        sensitive_target_in_command(
            r"rm \root\.agentos\workspace\scratch.txt",
            workspace=workspace,
        )
        is None
    )
    assert sensitive_target_in_command(
        r"rm \root\.agentos\workspace\.env",
        workspace=workspace,
    ) in {"/.env", "/.env*"}


def test_posix_sensitive_paths_stay_blocked_on_windows_runners() -> None:
    workspace = Path("/root/.agentos/workspace")

    assert sensitive_path_in_text("cat /dev/sda 2>/dev/null") == "/dev"
    assert sensitive_path_in_text("cat /root/.ssh/id_rsa", workspace=workspace) == "~/.ssh"


def test_every_rm_in_a_compound_command_is_checked() -> None:
    """Issue #676: a benign leading ``rm`` must not shadow a later one.

    Each shell separator ends one ``rm`` invocation, so ``rm /tmp/ok; rm -rf
    /root`` yields both targets and the sensitive one wins.
    """
    workspace = Path("/workspace")

    for separator in (";", "&&", "||", "|", "&", "\n"):
        command = f"rm /tmp/ok {separator} rm -rf /root"
        assert sensitive_target_in_command(command, workspace=workspace) == "/root", command

    assert (
        sensitive_target_in_command(
            "rm /tmp/ok; shutil.rmtree('/etc/ssl')",
            workspace=workspace,
        )
        == "/etc"
    )


def test_sensitive_reads_in_a_later_segment_are_blocked_at_the_tool_boundary() -> None:
    """Issue #676: the delete-intent scan only sees ``rm`` targets, so a
    non-destructive second segment (``cat /root/.bash_history``) is caught by
    the text scan ``exec_command`` runs alongside it, not by this one."""
    workspace = Path("/workspace")

    assert sensitive_target_in_command("rm /tmp/ok; ls /root", workspace=workspace) is None
    assert sensitive_path_in_text("rm /tmp/ok; ls /root", workspace=workspace) == "/root"
    assert (
        sensitive_path_in_text("rm /tmp/ok; cat /root/.bash_history", workspace=workspace)
        == "/root"
    )


def test_bare_root_delete_targets_are_hard_blocked() -> None:
    """Issue #563: ``rm -rf /`` has no sensitive *prefix*, so the prefix list
    never matched it and the whole-filesystem wipe reached the approval
    prompt instead of the hard block."""
    workspace = Path("/workspace")

    for command in (
        "rm -rf /",
        "rm -fr /",
        'rm -rf "/"',
        "rm -rf / --no-preserve-root",
        "rm -rf /.",
        "rm -rf /..",
        "rm -rf //",
    ):
        assert sensitive_target_in_command(command, workspace=workspace) == "/", command


def test_root_glob_delete_targets_are_hard_blocked() -> None:
    """``rm -rf /*`` expands to every top-level entry, so it is a root wipe
    even though the literal token is not ``/``. ``*`` is not the only spelling:
    ``/**``, ``/?*``, ``/.*`` and ``/[a-z]*`` sweep the same ground."""
    workspace = Path("/workspace")

    for command in (
        "rm -rf /*",
        "rm -rf /*/*",
        "rm -rf /./*",
        "rm -rf /**",
        "rm -rf /?*",
        "rm -rf /.*",
        "rm -rf /[a-z]*",
    ):
        assert sensitive_target_in_command(command, workspace=workspace) == "/", command


def test_narrowed_top_level_globs_are_not_root_wipes() -> None:
    """A glob carrying literal text names a subset, not the whole level —
    ``rm -rf /tmp*`` must not need ``/elevated full``."""
    workspace = Path("/workspace")

    for command in ("rm -rf /tmp*", "rm -rf /var/log*", "rm -rf /workspace/*", "rm -rf /[abc]"):
        assert sensitive_target_in_command(command, workspace=workspace) is None, command


def test_root_wipe_in_a_later_command_segment_is_hard_blocked() -> None:
    """A benign approved first target must not smuggle a root wipe past the
    hard block."""
    workspace = Path("/workspace")

    for separator in (";", "&&", "||", "|", "&", "\n"):
        command = f"rm /tmp/ok {separator} rm -rf /"
        assert sensitive_target_in_command(command, workspace=workspace) == "/", command


def test_python_flavoured_root_deletes_are_hard_blocked() -> None:
    workspace = Path("/workspace")

    assert sensitive_target_in_command("shutil.rmtree('/')", workspace=workspace) == "/"
    assert sensitive_target_in_command('os.rmdir("/")', workspace=workspace) == "/"


def test_ordinary_delete_targets_are_not_read_as_root() -> None:
    workspace = Path("/workspace")

    for command in ("rm /tmp/ok", "rm -rf ./build", "rm *", "rm -rf /workspace/dist"):
        assert sensitive_target_in_command(command, workspace=workspace) is None, command


def test_root_stays_readable_outside_the_destructive_intent_scan() -> None:
    """The root block is deliberately scoped to delete intents: listing or
    reading ``/`` is harmless and must not be hard-blocked."""
    workspace = Path("/workspace")

    assert is_sensitive_path("/") is None
    assert sensitive_path_marker("/", workspace=workspace) is None
    assert sensitive_path_in_text("ls /", workspace=workspace) is None
    assert sensitive_path_in_text("df -h /", workspace=workspace) is None
    assert sensitive_target_in_command("ls /", workspace=workspace) is None


def test_root_target_detection_covers_windows_drive_roots() -> None:
    """Windows runners resolve ``/`` to a drive root, so the raw ``/`` never
    reaches the segment check there."""
    for target in ("/", "//", "/.", "/..", "/*", "/*/*", "C:\\", "C:/", "c:/*", "D:/./*"):
        assert _is_root_target(target) is True, target

    for target in (
        "",
        "*",
        "-",
        "/etc",
        "/tmp*",
        "/workspace/dist",
        "C:/Users",
        "relative/path",
    ):
        assert _is_root_target(target) is False, target


@pytest.mark.parametrize(
    ("relative", "marker"),
    [
        (".config/gh/hosts.yml", "~/.config/gh"),
        (".config/gh/config.yml", "~/.config/gh"),
        (".anthropic/token", "~/.anthropic"),
        (".openai/api_key", "~/.openai"),
        (".vault-token", "~/.vault-token"),
    ],
)
def test_developer_credential_paths_in_home_are_sensitive(relative: str, marker: str) -> None:
    """The gh/Anthropic/OpenAI/Vault credential paths are guarded by default.

    ``gh`` in particular is run routinely by agents, so a live GitHub token
    lives in a path that used to sit outside the denylist.
    """
    target = Path.home()
    for part in relative.split("/"):
        target = target / part

    assert is_sensitive_path(str(target)) == marker


@pytest.mark.parametrize(
    "path",
    [
        "/var/secrets/.vault-token",
        "/opt/vault/.vault-token",
        r"C:\secrets\.vault-token",
    ],
)
def test_vault_token_outside_home_matches_the_suffix_entry(path: str) -> None:
    """``~/.vault-token`` only covers the documented home location.

    A Vault token written anywhere else is caught by the paired
    ``/.vault-token`` suffix entry, which is why both exist. Backslash
    spellings normalize, so this holds on every platform.
    """
    assert is_sensitive_path(path) == "/.vault-token"


@pytest.mark.parametrize(
    "relative",
    [
        ".config/ghost/config.json",
        ".config/gh-dash/config.yml",
        ".config/github/settings",
        ".anthropic-backup/token",
        ".openairc",
    ],
)
def test_new_prefixes_are_anchored_at_a_segment_boundary(relative: str) -> None:
    """A prefix must match a whole path segment, not a string prefix.

    Without segment anchoring ``~/.config/gh`` would swallow ``~/.config/ghost``
    and ``~/.config/gh-dash``, and ``~/.openai`` would swallow ``~/.openairc``.
    """
    target = Path.home()
    for part in relative.split("/"):
        target = target / part

    assert is_sensitive_path(str(target)) is None


@pytest.mark.parametrize(
    "relative",
    [
        ".config",
        ".config/nvim/init.lua",
        ".config/htop/htoprc",
        ".config/starship.toml",
    ],
)
def test_adding_config_gh_does_not_block_config_generally(relative: str) -> None:
    """``~/.config`` holds mostly harmless state and must stay readable.

    Only the ``gh`` subdirectory is sensitive; the parent and its siblings are
    not, so editor and shell configuration keeps working.
    """
    target = Path.home()
    for part in relative.split("/"):
        target = target / part

    assert is_sensitive_path(str(target)) is None


@pytest.mark.parametrize(
    "path",
    [
        "/var/secrets/vault-token",
        "/etc/vault/vault-token.bak",
    ],
)
def test_a_file_merely_named_vault_token_is_not_sensitive(path: str) -> None:
    """The leading dot is part of the suffix match.

    ``vault-token`` without it is an ordinary filename and must not be blocked,
    or the entry would over-reach onto unrelated files.
    """
    if path.startswith("/etc"):
        # ``/etc`` is already a sensitive prefix on its own account, so this
        # spelling proves nothing about the suffix; assert the reason instead.
        assert is_sensitive_path(path) == "/etc"
        return

    assert is_sensitive_path(path) is None


# --- host credential files (#981) -------------------------------------------
#
# `Path.home()` and `expanduser()` both read the platform's home variable, so
# pinning HOME/USERPROFILE fixes the expected marker on POSIX and Windows
# alike. Reading the real home would make these assertions depend on whoever
# runs them.

# Deliberately not under ``/home``: on macOS that prefix is an autofs mount
# point, so ``Path.resolve()`` pays an automount lookup -- 26ms a call here
# against 0.02ms elsewhere, which turned this file into a 35-second run once
# the parity cases started resolving several spellings each.
_FIXED_HOME = "/opt/agentos-test-home"


@pytest.fixture()
def fixed_home(monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", _FIXED_HOME)
    monkeypatch.setenv("USERPROFILE", _FIXED_HOME)
    return Path(_FIXED_HOME)


def test_new_credential_paths_are_caught_in_free_form_text(fixed_home: Path) -> None:
    """The text scanner is a separate code path from :func:`is_sensitive_path`.

    Shell commands reach the sandbox as free-form strings, so each new entry
    has to be reachable through the token scan as well as a resolved path.
    """
    home = fixed_home

    gh_hosts = home / ".config" / "gh" / "hosts.yml"
    anthropic_token = home / ".anthropic" / "token"
    openai_key = home / ".openai" / "api_key"
    vault_token = home / ".vault-token"
    nvim_init = home / ".config" / "nvim" / "init.lua"

    assert sensitive_path_in_text(f'cat "{gh_hosts}"') == "~/.config/gh"
    assert sensitive_path_in_text(f'cat "{anthropic_token}"') == "~/.anthropic"
    assert sensitive_path_in_text(f'cat "{openai_key}"') == "~/.openai"
    assert sensitive_path_in_text(f'cat "{vault_token}"') == "~/.vault-token"
    assert sensitive_path_in_text("cat /var/secrets/.vault-token") == "/.vault-token"

    # ``$HOME/X`` and ``~/X`` name the same file, so they must report the same
    # marker -- the parity property #985 exists to protect. Every spelling is
    # scanned and the home prefix wins outright; without that the ``$`` is
    # stripped as a token edge, the relative-looking ``HOME/.vault-token``
    # resolves through the leaf fallback, and the two spellings disagree.
    assert sensitive_path_in_text("cat $HOME/.vault-token") == "~/.vault-token"

    assert sensitive_path_in_text(f'nvim "{nvim_init}"') is None
    assert sensitive_path_in_text("cat /var/secrets/vault-token") is None


def test_host_credential_files_are_blocked_in_home(fixed_home: Path) -> None:
    for name in _HOST_CREDENTIAL_FILES:
        assert is_sensitive_path(str(fixed_home / name)) == f"~/{name}", name


def test_host_credential_files_are_blocked_as_tails_anywhere() -> None:
    # Deliberately wider than the home prefixes: moving one of these out of the
    # home directory must not shake the block off, and a project-local copy is
    # covered too. Same shape as the existing `/.env` tail.
    for name in _HOST_CREDENTIAL_FILES:
        assert is_sensitive_path(f"/srv/project/{name}") == f"/{name}", name


def test_host_credential_files_in_commands_are_blocked(fixed_home: Path) -> None:
    # Two different gates: sensitive_target_in_command covers DESTRUCTIVE
    # targets, sensitive_path_in_text covers a path merely appearing in a
    # command (a read). Both must catch these files.
    assert (
        sensitive_target_in_command(f"rm {fixed_home / '.git-credentials'}") == "~/.git-credentials"
    )
    assert sensitive_path_in_text(f"cat {fixed_home / '.pgpass'}") == "~/.pgpass"


def test_host_credential_tails_match_windows_separators() -> None:
    # Backslash spelling of a tail, written as a Windows-style path rather than
    # by replacing separators in a POSIX one -- that produces a string which is
    # not a path on either platform.
    assert is_sensitive_path(r"C:\srv\project\.pgpass") == "/.pgpass"
    assert sensitive_path_in_text(r"type C:\srv\project\.npmrc") == "/.npmrc"


def test_active_workspace_exception_keeps_credential_blocks(fixed_home: Path) -> None:
    # The workspace carve-out must not reopen credential files that happen to
    # sit inside it.
    workspace = Path("/srv/project")

    assert sensitive_target_in_command("rm /srv/project/scratch.txt", workspace=workspace) is None
    assert (
        sensitive_target_in_command("rm /srv/project/.git-credentials", workspace=workspace)
        == "/.git-credentials"
    )


def test_credential_file_list_stays_in_sync_with_redact() -> None:
    # The denylist derives from the redaction list on purpose; this fails if a
    # name is added to one layer and not carried to the other.
    assert set(_HOST_CREDENTIAL_FILES) == set(CREDENTIAL_FILE_NAMES) - {"credentials"}
    assert "credentials" not in _HOST_CREDENTIAL_FILES


def test_backslash_home_spelling_reports_the_prefix_not_the_tail(fixed_home: Path) -> None:
    """A backslash-spelled home path must still report the home prefix.

    Mixed separators are what `$HOME` expansion produces on Windows:
    `C:\\Users\\me` + `/` + `.npmrc`. `shlex.split` in POSIX mode eats the
    backslashes, leaving `C:Usersme/.npmrc` -- still carrying a `/`, so it
    matches the credential *tail* and short-circuits the scan before the
    intact token from `text.split()` is reached, reporting `/.npmrc` instead
    of `~/.npmrc`. Both block, but #985 exists to keep every spelling on the
    same marker.

    An all-backslash spelling does NOT reproduce this: every separator is
    eaten, the mangled token has no `/` left, and it matches nothing.
    """
    backslash_home = str(fixed_home).replace("/", "\\")
    for name in ("_netrc", ".npmrc", ".pgpass", ".git-credentials"):
        assert sensitive_path_in_text(f"cat {backslash_home}/{name}") == f"~/{name}", name


@pytest.mark.parametrize("spelling", ["$HOME", "${HOME}"])
@pytest.mark.parametrize("name", _HOST_CREDENTIAL_FILES)
def test_env_var_and_tilde_spellings_report_the_same_marker(
    fixed_home: Path, spelling: str, name: str
) -> None:
    """The two spellings of one file must not disagree about the marker.

    Deriving the credential names into both the home prefixes and the filename
    tails gave each name two possible answers, and which one came back depended
    on which spelling the scanner reached first. On Windows that broke the
    parity outright: ``$HOME`` expands to ``C:\\Users\\<name>``, POSIX-mode
    ``shlex.split`` eats the backslashes, and the drive-relative remainder falls
    through to the tail -- so ``$HOME/.netrc`` answered ``/.netrc`` while
    ``~/.netrc`` answered ``~/.netrc``.

    Asserting the two against each other rather than against a literal keeps
    this meaningful on every platform: the property is that they agree, not
    what the agreed value happens to be on the runner.
    """
    tilde = sensitive_path_in_text(f"cat ~/{name}")
    expanded = sensitive_path_in_text(f"cat {spelling}/{name}")

    assert tilde == f"~/{name}"
    assert expanded == tilde


def test_sensitive_path_marker_handles_unresolvable_home_runtimeerror() -> None:
    """Issue: sensitive_path_marker must not raise RuntimeError on tokens like ~\\.aws\\credentials.

    On POSIX systems, Path(r"~\\.aws\\credentials").expanduser() treats backslashes
    as usernames and raises RuntimeError. The scan must catch this and return the
    expected sensitive marker.
    """
    orig_expanduser = Path.expanduser

    def posix_like_expanduser(self: Path) -> Path:
        raw_str = str(self)
        if raw_str.startswith(("~\\", "~\\\\")):
            raise RuntimeError("Could not determine home directory.")
        if raw_str.startswith("~/"):
            return Path("/home/testuser") / raw_str[2:]
        return orig_expanduser(self)

    with patch.object(Path, "expanduser", posix_like_expanduser):
        assert sensitive_path_marker(r"~\.aws\credentials") == "~/.aws"
        assert sensitive_path_marker(r"~\.ssh\id_rsa") == "~/.ssh"
        assert sensitive_path_marker(r"~\.env") in {"/.env", "/.env*"}
        assert sensitive_path_marker(r"~\harmless.txt") is None
        assert sensitive_path_marker(r"~unknownuser\foo") is None


def test_sensitive_path_marker_and_is_sensitive_path_when_home_indeterminate() -> None:
    """When both Path.expanduser and Path.home fail with RuntimeError, scans return a verdict."""
    with (
        patch.object(
            Path,
            "expanduser",
            side_effect=RuntimeError("Could not determine home directory."),
        ),
        patch.object(
            Path,
            "home",
            side_effect=RuntimeError("Could not determine home directory."),
        ),
    ):
        assert sensitive_path_marker(r"~\.aws\credentials") == "~/.aws"
        assert sensitive_path_marker("~/.aws/credentials") == "~/.aws"
        assert is_sensitive_path(r"~\.aws\credentials") == "~/.aws"
        assert is_sensitive_path("~/.aws/credentials") == "~/.aws"
        assert sensitive_path_marker(r"~\harmless.txt") is None
        assert sensitive_path_marker("~unknown/file") is None


def test_sensitive_path_in_text_matches_embedded_backslash_tilde_path() -> None:
    assert sensitive_path_in_text(r"cat ~\.aws\credentials") == "~/.aws"
    assert sensitive_path_in_text(r"AWS_CONFIG_FILE=~\.aws\credentials") == "~/.aws"
    assert sensitive_path_in_text(r"--config=~\.aws\credentials") == "~/.aws"
    assert sensitive_path_in_text(r"cat ~\.ssh\id_rsa") == "~/.ssh"
    assert sensitive_path_in_text(r"--key=~\.ssh\id_rsa") == "/id_rsa"
