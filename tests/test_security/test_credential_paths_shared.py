"""Issues #2621 and #2623: the two credential-path lists had drifted.

``sensitive_paths.py`` derived its *file* entries from ``redact.py`` "so the
two layers cannot drift", but its *directory* entries were a separate list.
#1138 added ``~/.azure``, ``~/.config/gh``, ``~/.anthropic`` and ``~/.openai``
to the sandbox and nothing to redaction, so ``read_file`` on
``~/.azure/service_principal_entries.json`` was blocked while ``cat`` of it
handed the model ``client_secret`` intact -- the exact move
``reads_credential_file``'s docstring exists to stop.

The sandbox's own ``~/.docker/config`` entry, meanwhile, matched nothing that
exists: Docker writes ``config.json``, and the segment-anchored prefix match
accepts only the exact path or the path plus ``/``.

One list now, ``CREDENTIAL_HOME_DIRS`` in ``redact.py``, feeds both layers.
Six common credential files the assignment pass could already handle are
gated for masking -- but deliberately *not* blocked, because they sit in
directories full of build configuration an agent needs to read.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentos import redact
from agentos.sandbox import sensitive_paths

HOME = str(Path.home())


def home(rel: str) -> str:
    return f"{HOME}/{rel}"


# ── the two layers cannot drift ─────────────────────────────────────────────


@pytest.mark.parametrize("directory", redact.CREDENTIAL_HOME_DIRS)
def test_every_shared_directory_is_blocked_by_the_sandbox(directory: str) -> None:
    assert sensitive_paths.is_sensitive_path(home(f"{directory}/anything")) == f"~/{directory}"


@pytest.mark.parametrize("directory", redact.CREDENTIAL_HOME_DIRS)
def test_every_shared_directory_gates_a_shell_read(directory: str) -> None:
    assert redact.reads_credential_file(f"cat ~/{directory}/anything") is True
    assert redact.reads_credential_file(f"cat {home(directory)}/anything") is True


def test_the_sandbox_prefix_list_is_derived_not_copied() -> None:
    """A directory added to one layer reaches the other by construction."""
    derived = [p[2:] for p in sensitive_paths._BASE_SENSITIVE_PREFIXES if p.startswith("~/")]

    for directory in redact.CREDENTIAL_HOME_DIRS:
        assert directory in derived


# ── #2621: the drift that leaked ────────────────────────────────────────────


AZURE_SP = '[{"client_id": "abc", "client_secret": "sp-hunter2-secret", "tenant": "t"}]\n'
AZURE_MSAL = '{"RefreshToken": {"x": {"secret": "0.AXYAmsal-refresh-hunter2"}}}\n'


@pytest.mark.parametrize(
    ("path", "content", "secret"),
    [
        ("~/.azure/service_principal_entries.json", AZURE_SP, "sp-hunter2-secret"),
        ("~/.azure/msal_token_cache.json", AZURE_MSAL, "msal-refresh-hunter2"),
    ],
)
def test_a_file_the_sandbox_blocks_is_now_masked_when_cat_reaches_it(
    path: str, content: str, secret: str
) -> None:
    assert sensitive_paths.is_sensitive_path(path.replace("~", HOME)) is not None
    assert redact.reads_credential_file(f"cat {path}") is True
    assert secret not in redact.redact_terminal_output(content, f"cat {path}")


@pytest.mark.parametrize(
    "path",
    ["~/.config/gh/hosts.yml", "~/.anthropic/config", "~/.openai/key", "~/.password-store/x.gpg"],
)
def test_the_directories_1138_added_to_the_sandbox_now_gate_the_shell_too(path: str) -> None:
    assert redact.reads_credential_file(f"cat {path}") is True


# ── #2623: the entry that matched nothing ───────────────────────────────────


def test_docker_config_json_is_blocked() -> None:
    """The file Docker actually writes."""
    assert sensitive_paths.is_sensitive_path(home(".docker/config.json")) == "~/.docker"


def test_docker_contexts_are_blocked_too() -> None:
    """A remote-daemon context carries client TLS keys; the whole directory
    is credential material, like ``~/.aws`` and ``~/.kube`` beside it."""
    assert sensitive_paths.is_sensitive_path(home(".docker/contexts/meta/x/meta.json")) is not None


def test_docker_config_json_via_the_shell_is_gated() -> None:
    """Gated, as it was before -- ``.docker`` was already in the redaction
    list; #2623 is the sandbox side. Note what gating does *not* buy here:
    Docker stores ``"auth": "<base64 user:password>"``, and ``auth`` is not a
    name the assignment pass knows, so the value is not masked by it. The
    hard block on ``read_file`` is what protects this file today."""
    assert redact.reads_credential_file("cat ~/.docker/config.json") is True


# ── coverage: files the pass could already handle, now gated ────────────────


COVERAGE = [
    ("~/.my.cnf", "[client]\nuser=root\npassword=mysql-hunter2-x\n", "mysql-hunter2-x"),
    ("~/.gradle/gradle.properties", "signing.password=gradle-hunter2-x\n", "gradle-hunter2-x"),
    ("./gradle.properties", "ossrhPassword=gradle-hunter2-y\n", "gradle-hunter2-y"),
    ("~/.yarnrc.yml", "npmAuthToken: yarn-hunter2-token-x\n", "yarn-hunter2-token-x"),
    ("./service-account.json", '{"private_key_id": "pkid-hunter2-x"}\n', "pkid-hunter2-x"),
    ("~/.boto", "[Credentials]\naws_secret_access_key = boto-hunter2-x\n", "boto-hunter2-x"),
    ("~/.s3cfg", "[default]\nsecret_key = s3cfg-hunter2-x\n", "s3cfg-hunter2-x"),
]


@pytest.mark.parametrize(("path", "content", "secret"), COVERAGE)
def test_a_common_credential_file_is_gated_and_its_secret_masked(
    path: str, content: str, secret: str
) -> None:
    command = f"cat {path}"

    assert redact.reads_credential_file(command) is True
    assert secret not in redact.redact_terminal_output(content, command)


@pytest.mark.parametrize(("path", "content", "secret"), COVERAGE)
def test_the_same_file_is_masked_on_the_file_read_surface(
    path: str, content: str, secret: str
) -> None:
    assert secret not in redact.redact_file_output(content, path=path)


@pytest.mark.parametrize(
    "name",
    [
        "service-account.json",
        "service_account.json",
        "serviceaccount.json",
        "my-project-service-account-key.json",
        "SERVICE-ACCOUNT.JSON",
    ],
)
def test_a_service_account_key_by_any_of_its_names_is_gated(name: str) -> None:
    assert redact.reads_credential_file(f"cat ./keys/{name}") is True


@pytest.mark.parametrize("name", ["account.json", "service.json", "service-account.txt"])
def test_a_near_miss_of_the_service_account_pattern_is_not_gated(name: str) -> None:
    assert redact.reads_credential_file(f"cat ./{name}") is False


@pytest.mark.parametrize(
    "path",
    [
        "~/.terraform.d/credentials.tfrc.json",
        "~/.cargo/credentials.toml",
        "~/.m2/settings.xml",
    ],
)
def test_the_files_that_also_need_new_value_shapes_are_at_least_gated(path: str) -> None:
    """Gating is necessary but not sufficient here: a bare ``token`` key and
    an XML ``<password>`` element are shapes the assignment pass does not
    know. That is a separate change; this pins that the gate no longer stands
    in its way."""
    assert redact.reads_credential_file(f"cat {path}") is True


# ── mask, do not block ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "./gradle.properties",
        "~/.gradle/gradle.properties",
        "~/.m2/settings.xml",
        "~/.cargo/config.toml",
        "~/.terraform.d/plugin-cache/x",
        "./service-account.json",
        "~/.my.cnf",
    ],
)
def test_a_redaction_only_path_is_not_hard_blocked(path: str) -> None:
    """These sit among build configuration an agent has to read. They get the
    assignment pass, not the denylist -- blocking ``./gradle.properties``
    would break every Gradle project."""
    assert sensitive_paths.is_sensitive_path(path.replace("~", HOME)) is None


def test_redaction_only_names_do_not_leak_into_the_sandbox_file_list() -> None:
    for name in redact._REDACT_ONLY_CREDENTIAL_FILE_NAMES:
        assert name not in redact.CREDENTIAL_FILE_NAMES
        assert f"/{name}" not in sensitive_paths._SENSITIVE_SUFFIXES


# ── matching rules ──────────────────────────────────────────────────────────


def test_a_multi_segment_entry_matches_only_as_a_contiguous_run() -> None:
    assert redact.reads_credential_file("cat ~/.config/gh/hosts.yml") is True
    assert redact.reads_credential_file("cat ~/.config/other/gh/hosts.yml") is False
    assert redact.reads_credential_file("cat ./gh/hosts.yml") is False


def test_a_single_segment_entry_matches_under_any_parent() -> None:
    """Unchanged from before: ``.aws`` anywhere in a path is ``.aws``."""
    assert redact.reads_credential_file("cat /srv/backup/.aws/credentials") is True
    assert redact.reads_credential_file("cat C:/Users/u/.kube/config") is True


# ── found while writing the above: Windows-native paths were invisible ──────


@pytest.mark.parametrize(
    "command",
    [
        "type C:\\Users\\u\\.aws\\credentials",
        "Get-Content C:\\Users\\u\\.ssh\\id_rsa",
        "type C:\\Users\\u\\.azure\\x.json",
        "type \\\\server\\share\\.aws\\credentials",
        "more C:\\Users\\u\\.my.cnf",
    ],
)
def test_an_unquoted_windows_native_path_is_gated(command: str) -> None:
    """``shlex`` in POSIX mode ate the backslashes, so ``C:\\Users\\u\\.aws``
    read as ``C:Usersu.aws`` and matched nothing. The sandbox's own command
    scanner already saw these; the redaction gate now does too."""
    assert redact.reads_credential_file(command) is True


def test_a_windows_native_path_is_masked_end_to_end() -> None:
    content = "[default]\naws_secret_access_key = win-hunter2-secret-x\n"

    out = redact.redact_terminal_output(content, "type C:\\Users\\u\\.aws\\credentials")

    assert "win-hunter2-secret-x" not in out


def test_quoted_and_forward_slash_windows_paths_still_work() -> None:
    assert redact.reads_credential_file('type "C:\\Users\\u\\.aws\\credentials"') is True
    assert redact.reads_credential_file("type C:/Users/u/.aws/credentials") is True


def test_a_backslash_escape_in_a_posix_command_does_not_invent_a_match() -> None:
    """``\\`` on POSIX is still an escape; the literal reading is *added*,
    not substituted, and an escaped space in an ordinary path is ordinary."""
    assert redact.reads_credential_file("cat ./my\\ notes.txt") is False


def test_the_windows_gcloud_location_is_covered_on_both_layers() -> None:
    """The Cloud SDK keeps its credentials under ``%APPDATA%\\gcloud`` on
    Windows, not ``~/.config/gcloud``."""
    path = "AppData/Roaming/gcloud/credentials.db"

    assert redact.reads_credential_file(f"cat ~/{path}") is True
    assert sensitive_paths.is_sensitive_path(home(path)) == "~/AppData/Roaming/gcloud"


def test_directory_matching_is_case_insensitive() -> None:
    assert redact.reads_credential_file("cat ~/.AWS/credentials") is True
    assert redact.reads_credential_file("cat ~/AppData/Roaming/GCloud/x") is True


def test_a_credential_directory_name_as_a_file_name_is_not_a_match() -> None:
    """Only directory segments count; a file called ``.aws`` is just a file."""
    assert redact.reads_credential_file("cat ./.aws") is False


# ── what must not change ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "cat ~/.aws/credentials",
        "cat ~/.npmrc",
        "cat .env",
        "cat ./config/.env.production",
        "cat ~/.ssh/id_rsa",
        "cat ~/.kube/config",
        "cat ~/.gnupg/x",
        "cat ~/.config/gcloud/credentials.db",
    ],
)
def test_previously_gated_reads_are_still_gated(command: str) -> None:
    assert redact.reads_credential_file(command) is True


@pytest.mark.parametrize(
    "command", ["cat notes.txt", "cat src/main.py", "ls -la", "cat ./config/settings.py", ""]
)
def test_ordinary_reads_are_still_not_gated(command: str) -> None:
    assert redact.reads_credential_file(command) is False


@pytest.mark.parametrize(
    "path", ["~/.my.cnf", "./gradle.properties", "~/.azure/x.json", "./service-account.json"]
)
def test_a_newly_gated_path_is_never_treated_as_source_code(path: str) -> None:
    """Source-code paths skip the assignment pass; a credential file must not."""
    assert redact._is_source_code_path(path) is False


def test_source_code_under_no_credential_directory_is_still_source_code() -> None:
    assert redact._is_source_code_path("src/agentos/redact.py") is True
    assert redact._is_source_code_path("C:\\repo\\app.ts") is True


def test_the_vault_token_entry_survives_the_derivation() -> None:
    """A file, not a directory, so it is listed beside the derived entries
    rather than in the shared directory list."""
    assert "~/.vault-token" in sensitive_paths._BASE_SENSITIVE_PREFIXES
    assert sensitive_paths.is_sensitive_path(home(".vault-token")) == "~/.vault-token"


def test_env_var_spelling_of_home_still_resolves_for_a_shared_entry() -> None:
    """``$HOME/.azure`` reaches the syscall as ``~/.azure``; #985's expansion
    must apply to the derived entries exactly as it did to the literal ones."""
    if os.name == "nt":
        pytest.skip("$HOME expansion is exercised on POSIX runners")
    assert sensitive_paths.sensitive_path_in_text("cat $HOME/.azure/x.json") == "~/.azure"
