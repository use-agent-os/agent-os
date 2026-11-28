"""The desktop app's keychain-held key: ``unlock.key`` with no key on disk.

With the ``file`` store, one process reading every file this user owns (the
state directory and the key directory) still recovers the vault password. A
gateway the desktop app spawns is handed the key over a pipe instead, so the
key is in no file. These tests cover the handoff, the move of an existing
vault onto it, and that a gateway without the key never seals the password
back under a key on disk.
"""

from __future__ import annotations

import os
import secrets
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentos.trading import unlock_key
from agentos.trading.vault import Vault
from tests.test_trading.conftest import FAST_KDF

PASSWORD = "DUMMYvaultPW_7777777777"
NEW_PASSWORD = "DUMMYvaultPW_8888888888"


@pytest.fixture(autouse=True)
def _no_handoff_left_behind() -> Iterator[None]:
    unlock_key.clear_handoff_kek()
    yield
    unlock_key.clear_handoff_kek()


@pytest.fixture
def kek_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "elsewhere" / "vault-keys"
    monkeypatch.setenv("AGENTOS_VAULT_KEK_DIR", str(directory))
    monkeypatch.delenv("AGENTOS_VAULT_KEK", raising=False)
    return directory


@pytest.fixture
def state(tmp_path: Path, kek_dir: Path) -> Path:
    return tmp_path / "state"


def _vault(state: Path) -> Vault:
    return Vault(state / "wallets", **FAST_KDF)


def _hand_over(kek_hex: str | None = None) -> str:
    """Hand a key to this process the way the desktop app does: over a pipe."""
    kek_hex = kek_hex or secrets.token_hex(32)
    read_fd, write_fd = os.pipe()
    os.write(write_fd, kek_hex.encode("ascii") + b"\n")
    os.close(write_fd)
    env = {unlock_key.KEK_FD_ENV: str(read_fd)}
    assert unlock_key.load_handoff_kek(env) is True
    assert env == {}
    with pytest.raises(OSError):
        os.fstat(read_fd)  # closed: no child can inherit it
    return kek_hex


def _every_byte_under(*roots: Path) -> bytes:
    chunks: list[bytes] = []
    for root in roots:
        for directory, _dirs, files in os.walk(root):
            for name in files:
                chunks.append((Path(directory) / name).read_bytes())
    return b"\n".join(chunks)


def _key_files(kek_dir: Path) -> list[Path]:
    return sorted(kek_dir.glob("*")) if kek_dir.is_dir() else []


# ── the handoff itself ──────────────────────────────────────────────────────


def test_a_key_file_is_read_once_and_deleted(tmp_path: Path) -> None:
    path = tmp_path / "vault-kek.reexec-1"
    path.write_text(secrets.token_hex(32) + "\n", encoding="ascii")
    env = {unlock_key.KEK_FILE_ENV: str(path)}
    assert unlock_key.load_handoff_kek(env) is True
    assert env == {}
    assert not path.exists()


@pytest.mark.skipif(not hasattr(socket, "socketpair"), reason="needs socketpair")
def test_a_key_arrives_over_the_socket_pair_node_uses_for_a_pipe() -> None:
    # Node's `stdio: 'pipe'` is a UNIX socket pair on macOS and Linux, not a
    # FIFO: the app's key reaches the gateway on one of those.
    ours, theirs = socket.socketpair()
    kek_hex = secrets.token_hex(32)
    ours.sendall(kek_hex.encode("ascii") + b"\n")
    ours.close()
    fd = theirs.detach()
    env = {unlock_key.KEK_FD_ENV: str(fd)}
    assert unlock_key.load_handoff_kek(env) is True
    with pytest.raises(OSError):
        os.fstat(fd)


@pytest.mark.parametrize("payload", ["", "not-hex", "ab" * 31, "ab" * 33])
def test_a_malformed_key_is_ignored_and_still_scrubbed(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "bad"
    path.write_text(payload, encoding="ascii")
    env = {unlock_key.KEK_FILE_ENV: str(path), unlock_key.KEK_FD_ENV: "not-a-number"}
    assert unlock_key.load_handoff_kek(env) is False
    assert env == {}
    assert not path.exists()
    assert unlock_key.handoff_kek_held() is False


def test_a_descriptor_that_is_not_a_pipe_is_left_alone(tmp_path: Path) -> None:
    # A stale variable after a re-exec may name a descriptor that is a log
    # file or a lock by now: it must be neither read as a key nor closed.
    path = tmp_path / "some.log"
    path.write_text(secrets.token_hex(32), encoding="ascii")
    fd = os.open(path, os.O_RDONLY)
    try:
        assert unlock_key.load_handoff_kek({unlock_key.KEK_FD_ENV: str(fd)}) is False
        os.fstat(fd)  # still open
    finally:
        os.close(fd)


def test_the_key_survives_a_reexec_through_a_one_shot_file(tmp_path: Path) -> None:
    kek_hex = _hand_over()
    path = unlock_key.export_handoff_kek_file(tmp_path / "wallets")
    assert path is not None
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    unlock_key.clear_handoff_kek()  # the new image starts with nothing
    assert unlock_key.load_handoff_kek({unlock_key.KEK_FILE_ENV: str(path)}) is True
    assert not path.exists()
    assert unlock_key.export_handoff_kek_file(tmp_path / "wallets").read_text().strip() == kek_hex  # type: ignore[union-attr]


def test_nothing_is_exported_without_a_key(tmp_path: Path) -> None:
    assert unlock_key.export_handoff_kek_file(tmp_path / "wallets") is None
    assert not (tmp_path / "wallets").exists()


# ── the vault under the app's key ───────────────────────────────────────────


def test_no_file_this_user_owns_opens_the_vault(state: Path, kek_dir: Path) -> None:
    kek_hex = _hand_over()
    vault = _vault(state)
    vault.setup(PASSWORD, "auto")
    vault.create("main")
    assert vault.status()["unlockKeyProtection"] == "sealed:handoff"
    assert _key_files(kek_dir) == []
    dump = _every_byte_under(state, kek_dir)
    assert PASSWORD.encode() not in dump
    assert kek_hex.encode() not in dump
    assert bytes.fromhex(kek_hex) not in dump


def test_a_gateway_the_app_spawned_unlocks_on_its_own(state: Path) -> None:
    kek_hex = _hand_over()
    _vault(state).setup(PASSWORD, "auto")

    unlock_key.clear_handoff_kek()  # the gateway restarts under the app
    _hand_over(kek_hex)
    reopened = _vault(state)
    assert reopened.try_auto_unlock() is True


def test_a_gateway_without_the_key_stays_locked(state: Path, kek_dir: Path) -> None:
    _hand_over()
    _vault(state).setup(PASSWORD, "auto")

    unlock_key.clear_handoff_kek()  # `agentos gateway run`, no app
    reopened = _vault(state)
    assert reopened.try_auto_unlock() is False
    assert reopened.unlocked is False


def test_an_existing_vault_moves_onto_the_apps_key(state: Path, kek_dir: Path) -> None:
    vault = _vault(state)
    vault.setup(PASSWORD, "auto")
    assert vault.status()["unlockKeyProtection"] == "sealed:file"
    assert len(_key_files(kek_dir)) == 1

    _hand_over()  # the app now starts this gateway
    reopened = _vault(state)
    assert reopened.try_auto_unlock() is True
    assert reopened.status()["unlockKeyProtection"] == "sealed:handoff"
    assert _key_files(kek_dir) == []  # the key on disk is gone


def test_an_injected_key_is_not_replaced_by_the_apps(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTOS_VAULT_KEK", secrets.token_hex(32))
    _hand_over()
    vault = _vault(state)
    vault.setup(PASSWORD, "auto")
    assert vault.status()["unlockKeyProtection"] == "sealed:env"
    reopened = _vault(state)
    assert reopened.try_auto_unlock() is True
    assert reopened.status()["unlockKeyProtection"] == "sealed:env"


# ── a gateway without the key never puts the password back on disk ──────────


def test_unlocking_without_the_key_leaves_the_file_to_the_app(state: Path, kek_dir: Path) -> None:
    kek_hex = _hand_over()
    _vault(state).setup(PASSWORD, "auto")
    sealed = (state / "wallets" / "unlock.key").read_bytes()

    unlock_key.clear_handoff_kek()
    cli_gateway = _vault(state)
    cli_gateway.unlock(PASSWORD)
    assert cli_gateway.unlocked is True
    assert (state / "wallets" / "unlock.key").read_bytes() == sealed
    assert _key_files(kek_dir) == []

    _hand_over(kek_hex)  # back under the app: still opens on its own
    assert _vault(state).try_auto_unlock() is True


def test_a_password_change_without_the_key_is_stored_at_the_next_app_unlock(
    state: Path, kek_dir: Path
) -> None:
    kek_hex = _hand_over()
    _vault(state).setup(PASSWORD, "auto")

    unlock_key.clear_handoff_kek()
    cli_gateway = _vault(state)
    cli_gateway.change_password(PASSWORD, NEW_PASSWORD)
    assert _key_files(kek_dir) == []  # not sealed under a key on disk

    _hand_over(kek_hex)
    under_app = _vault(state)
    assert under_app.try_auto_unlock() is False  # the stored password is the old one
    under_app.unlock(NEW_PASSWORD)  # which seals the new one again
    assert _vault(state).try_auto_unlock() is True
    assert _key_files(kek_dir) == []


def test_a_lost_app_key_is_replaced_at_the_next_unlock(state: Path, kek_dir: Path) -> None:
    _hand_over()
    _vault(state).setup(PASSWORD, "auto")

    unlock_key.clear_handoff_kek()
    replacement = _hand_over()  # the keychain item was reset
    vault = _vault(state)
    assert vault.try_auto_unlock() is False
    vault.unlock(PASSWORD)

    unlock_key.clear_handoff_kek()
    _hand_over(replacement)
    assert _vault(state).try_auto_unlock() is True


def test_switching_to_auto_without_the_key_is_the_operators_call(
    state: Path, kek_dir: Path
) -> None:
    # `agentos wallet mode auto` in a gateway the app did not start is an
    # explicit, password-proven request for auto unlock there: it gets a key
    # on disk, as before.
    _hand_over()
    _vault(state).setup(PASSWORD, "auto")
    unlock_key.clear_handoff_kek()
    vault = _vault(state)
    vault.set_unlock_mode("auto", PASSWORD)
    assert vault.status()["unlockKeyProtection"] == "sealed:file"
    assert len(_key_files(kek_dir)) == 1
