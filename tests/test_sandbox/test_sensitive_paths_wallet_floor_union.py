"""The wallet floor is the union of every fix's additions, ancestors included.

Each wallet fix put its own files on the sensitive-path floor: config backups
(WAL-17), the trading ledger with its sidecars and the pre-upgrade snapshots
(WAL-05), the operator's config approval (WAL-02), the approval queue and the
skill mandate store (WAL-10), gmgn-cli's credential home (WAL-04), the
vault's sealing key outside the state root (WAL-01). The recursive-ancestor
rule (WAL-01) must reach every one of them: a recursive read of a directory
above any of them is a read of it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentos.sandbox.sensitive_paths import is_sensitive_path, sensitive_path_in_text


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "agentos-state"
    root.mkdir()
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(root))
    monkeypatch.setenv("AGENTOS_VAULT_KEK_DIR", str(tmp_path / "elsewhere" / "vault-keys"))
    return root


# Relative to the state root.
STATE_FILES = [
    "config.toml.backup.20261115T000000",
    "config-approval.toml",
    "state/trading.sqlite",
    "state/trading.sqlite-wal",
    "state/trading.sqlite-shm",
    "state/trading.sqlite-journal",
    "state/snapshots/pre-upgrade/trading.sqlite",
    "state/approval_queue.sqlite",
    "state/approval_queue.sqlite-wal",
    "skill-signing/unilp-mandates.json",
    "wallets/unlock.key",
]


@pytest.mark.parametrize("relative", STATE_FILES)
def test_every_addition_is_on_the_floor(state: Path, relative: str) -> None:
    assert is_sensitive_path(str(state / Path(relative))) is not None, relative


def test_the_kek_dir_and_gmgn_home_are_on_the_floor(state: Path, tmp_path: Path) -> None:
    from agentos.paths import vault_kek_dir

    assert is_sensitive_path(str(vault_kek_dir() / "kek")) is not None
    assert is_sensitive_path(str(Path.home() / ".config" / "gmgn" / ".env")) is not None


def _make(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")


@pytest.mark.skipif(os.name != "posix", reason="POSIX shell semantics")
@pytest.mark.parametrize("relative", [r for r in STATE_FILES if not r.startswith("wallets/")])
def test_a_recursive_read_above_any_addition_is_refused(
    state: Path, tmp_path: Path, relative: str
) -> None:
    """Only that one protected file exists: it alone makes its ancestors sensitive."""
    _make(state / Path(relative))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    for command in (
        f"grep -r . {state}",
        f"tar -cf /tmp/x.tar -C {tmp_path} .",
        f"cp -r {state} x",
    ):
        assert sensitive_path_in_text(command, workspace=workspace, cwd=workspace), command


@pytest.mark.skipif(os.name != "posix", reason="POSIX shell semantics")
def test_a_recursive_read_above_the_kek_dir_is_refused(state: Path, tmp_path: Path) -> None:
    from agentos.paths import vault_kek_dir

    _make(vault_kek_dir() / "kek")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    above = vault_kek_dir().parent.parent
    assert sensitive_path_in_text(f"grep -r . {above}", workspace=workspace, cwd=workspace)


@pytest.mark.skipif(os.name != "posix", reason="POSIX shell semantics")
def test_recursive_work_elsewhere_still_runs(state: Path, tmp_path: Path) -> None:
    other = tmp_path / "project"
    _make(other / "src" / "main.py")
    assert sensitive_path_in_text(f"grep -r TODO {other}", workspace=other, cwd=other) is None
