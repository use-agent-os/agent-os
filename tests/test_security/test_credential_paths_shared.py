"""Issue #2621: the sandbox denylist and the redaction gate had drifted.

``sensitive_paths.py`` derived its *file* entries from ``redact.py`` "so the
two layers cannot drift", but its *directory* entries were a separate list.
#1138 added ``~/.azure``, ``~/.config/gh``, ``~/.anthropic`` and
``~/.openai`` to the sandbox denylist and nothing to redaction, so
``read_file`` on ``~/.azure/service_principal_entries.json`` was blocked
while ``cat`` of the same file handed the model its ``client_secret``
intact -- the exact move ``reads_credential_file``'s own docstring exists to
stop.

One list, ``redact.CREDENTIAL_HOME_DIRS``, now feeds both layers. Six common
credential files the assignment pass could already handle (they use
``name=value``/``name: value`` shapes) are added to the shared file set, and
four directories that hold them are added to the shared directory set.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentos import redact
from agentos.sandbox import sensitive_paths

HOME = str(Path.home())


def home(rel: str) -> str:
    return f"{HOME}/{rel}"


# ── the two layers cannot drift (the issue's own suggested test) ───────────


@pytest.mark.parametrize("directory", redact.CREDENTIAL_HOME_DIRS)
def test_every_shared_directory_is_blocked_by_the_sandbox(directory: str) -> None:
    assert sensitive_paths.is_sensitive_path(home(f"{directory}/anything")) == f"~/{directory}"


@pytest.mark.parametrize("directory", redact.CREDENTIAL_HOME_DIRS)
def test_every_shared_directory_gates_a_shell_read(directory: str) -> None:
    assert redact.reads_credential_file(f"cat ~/{directory}/anything") is True


def test_the_sandbox_prefix_list_is_derived_not_copied() -> None:
    """A directory added to one layer reaches the other by construction."""
    derived = [
        p[2:] for p in sensitive_paths._BASE_SENSITIVE_PREFIXES if p.startswith("~/")
    ]

    for directory in redact.CREDENTIAL_HOME_DIRS:
        assert directory in derived


# ── the drift that leaked (#2621's own repro) ───────────────────────────────

AZURE_SP = '[{"client_id": "abc", "client_secret": "REPLACE_ME_secret", "tenant": "t"}]\n'
AZURE_SECRET = "REPLACE_ME_secret"


def test_a_directory_the_sandbox_blocks_is_now_masked_when_cat_reaches_it() -> None:
    path = "~/.azure/service_principal_entries.json"

    assert sensitive_paths.is_sensitive_path(path.replace("~", HOME)) is not None
    assert redact.reads_credential_file(f"cat {path}") is True
    assert AZURE_SECRET not in redact.redact_terminal_output(AZURE_SP, f"cat {path}")


@pytest.mark.parametrize(
    "path",
    ["~/.config/gh/hosts.yml", "~/.anthropic/config", "~/.openai/key", "~/.password-store/x.gpg"],
)
def test_the_directories_1138_added_to_the_sandbox_now_gate_the_shell_too(path: str) -> None:
    assert redact.reads_credential_file(f"cat {path}") is True


# ── coverage: files the assignment pass could already handle, now gated ────

COVERAGE = [
    ("~/.my.cnf", "[client]\nuser=root\npassword=REPLACE_ME_mysql\n", "REPLACE_ME_mysql"),
    (
        "~/.gradle/gradle.properties",
        "signing.password=REPLACE_ME_gradle\n",
        "REPLACE_ME_gradle",
    ),
    ("./gradle.properties", "ossrhPassword=REPLACE_ME_ossrh\n", "REPLACE_ME_ossrh"),
    ("~/.yarnrc.yml", "npmAuthToken: REPLACE_ME_npm\n", "REPLACE_ME_npm"),
    (
        "./service-account.json",
        '{"private_key_id": "REPLACE_ME_pkid"}\n',
        "REPLACE_ME_pkid",
    ),
    (
        "~/.boto",
        "[Credentials]\naws_secret_access_key = REPLACE_ME_boto\n",
        "REPLACE_ME_boto",
    ),
    ("~/.s3cfg", "[default]\nsecret_key = REPLACE_ME_s3cfg\n", "REPLACE_ME_s3cfg"),
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


def test_a_near_miss_of_the_service_account_name_is_not_gated() -> None:
    """Deliberately narrow: only the exact documented filename from the
    issue's own repro is recognized. A wildcard spelling is a separate,
    unfiled follow-up."""
    assert redact.reads_credential_file("cat ./my-project-service-account-key.json") is False


# ── mask, do not silently over-widen ────────────────────────────────────────


def test_a_multi_segment_entry_matches_only_as_a_contiguous_run() -> None:
    assert redact.reads_credential_file("cat ~/.config/gh/hosts.yml") is True
    assert redact.reads_credential_file("cat ~/.config/other/gh/hosts.yml") is False
    assert redact.reads_credential_file("cat ./gh/hosts.yml") is False


def test_a_single_segment_entry_matches_under_any_parent() -> None:
    """Unchanged from before: ``.aws`` anywhere in a path is ``.aws``."""
    assert redact.reads_credential_file("cat /srv/backup/.aws/credentials") is True


def test_directory_matching_is_case_insensitive() -> None:
    assert redact.reads_credential_file("cat ~/.AWS/credentials") is True


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
        "cat ~/.docker/config.json",
    ],
)
def test_previously_or_still_gated_reads_stay_gated(command: str) -> None:
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


def test_the_vault_token_entry_survives_the_derivation() -> None:
    """A file, not a directory, so it is listed beside the derived entries
    rather than in the shared directory list."""
    assert "~/.vault-token" in sensitive_paths._BASE_SENSITIVE_PREFIXES
    assert sensitive_paths.is_sensitive_path(home(".vault-token")) == "~/.vault-token"
