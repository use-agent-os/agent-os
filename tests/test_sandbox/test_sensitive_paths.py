from __future__ import annotations

from pathlib import Path

from agentos.sandbox.sensitive_paths import (
    _is_root_target,
    is_sensitive_path,
    sensitive_path_in_text,
    sensitive_path_marker,
    sensitive_target_in_command,
)


def test_sensitive_path_matches_nested_home_prefixes_with_native_separators() -> None:
    assert is_sensitive_path(str(Path.home() / ".ssh" / "id_rsa")) == "~/.ssh"
    assert is_sensitive_path(str(Path.home() / ".aws" / "credentials")) == "~/.aws"
    assert is_sensitive_path(str(Path.home() / ".config" / "gh" / "hosts.yml")) == "~/.config/gh"
    assert is_sensitive_path(str(Path.home() / ".anthropic" / "token")) == "~/.anthropic"
    assert is_sensitive_path(str(Path.home() / ".openai" / "api_key")) == "~/.openai"
    assert is_sensitive_path(str(Path.home() / ".vault-token")) == "~/.vault-token"


def test_sensitive_path_suffix_matches_vault_token() -> None:
    assert is_sensitive_path("/var/secrets/.vault-token") == "/.vault-token"
    assert is_sensitive_path(r"C:\secrets\.vault-token") == "/.vault-token"


def test_sensitive_path_in_text_matches_native_separator_paths() -> None:
    key_path = Path.home() / ".ssh" / "id_rsa"

    assert sensitive_path_in_text(f"type {key_path}") == "~/.ssh"


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
    assert (
        sensitive_path_in_text(
            f"cat {workspace / '.env.local'}",
            workspace=workspace,
        )
        in {"/.env.local", "/.env*"}
    )


def test_sensitive_command_targets_honor_active_workspace_exception() -> None:
    workspace = Path("/root/.agentos/workspace")

    assert (
        sensitive_target_in_command(
            f"rm {workspace / 'scratch.txt'}",
            workspace=workspace,
        )
        is None
    )
    assert (
        sensitive_target_in_command(
            f"rm {workspace / '.env'}",
            workspace=workspace,
        )
        in {"/.env", "/.env*"}
    )


def test_windows_rooted_workspace_targets_keep_leaf_secret_blocks() -> None:
    workspace = Path("/root/.agentos/workspace")

    assert (
        sensitive_target_in_command(
            r"rm \root\.agentos\workspace\scratch.txt",
            workspace=workspace,
        )
        is None
    )
    assert (
        sensitive_target_in_command(
            r"rm \root\.agentos\workspace\.env",
            workspace=workspace,
        )
        in {"/.env", "/.env*"}
    )


def test_posix_sensitive_paths_stay_blocked_on_windows_runners() -> None:
    workspace = Path("/root/.agentos/workspace")

    assert sensitive_path_in_text("cat /dev/sda 2>/dev/null") == "/dev"
    assert (
        sensitive_path_in_text("cat /root/.ssh/id_rsa", workspace=workspace)
        == "~/.ssh"
    )


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
