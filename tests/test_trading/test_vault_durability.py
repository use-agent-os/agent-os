"""The vault survives an interrupted write: a half-done password change, a power cut.

``change_password`` rewrote the keystores one by one and the verifier last. A
failure in between (a full disk, a kill) left some wallets under the new
password and the rest, with the verifier, under the old one, and since an
unlock decrypts every keystore, neither password opened the vault again.
Nothing the vault wrote was flushed to disk either, so a power cut right after
a wallet was created could leave its only keystore empty.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agentos.trading import unlock_key
from agentos.trading import vault as vault_module
from agentos.trading.vault import BadPasswordError, Vault
from tests.test_trading.conftest import FAST_KDF, PASSWORD

NEW_PASSWORD = "a brand new password"


def _vault_with_wallets(root: Path, count: int = 3) -> tuple[Vault, dict[str, bytes]]:
    vault = Vault(root, **FAST_KDF)
    vault.setup(PASSWORD, "manual")
    keys = {}
    for i in range(count):
        record = vault.create(f"w{i}")
        keys[record.address] = vault.private_key(record.address)
    return vault, keys


def _assert_opens(root: Path, password: str, keys: dict[str, bytes]) -> None:
    fresh = Vault(root, **FAST_KDF)
    fresh.unlock(password)
    assert {r.address: fresh.private_key(r.address) for r in fresh.list()} == keys
    # Every keystore on disk is under that one password, not just the cache.
    for address in keys:
        exported = fresh.export(address, password, "keystore")
        assert Vault._decrypt(json.loads(exported), password) == keys[address]
    assert not list(root.glob("*.rekey"))
    assert "rekey_pending" not in json.loads((root / "index.json").read_text())


def test_change_password_failing_midway_keeps_the_old_password(
    vault_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, keys = _vault_with_wallets(vault_root)
    real = vault_module._write_private
    calls = {"keystores": 0}

    def flaky(path: Path, text: str) -> None:
        if path.name.startswith("0x"):
            calls["keystores"] += 1
            if calls["keystores"] == 2:
                raise OSError(28, "No space left on device")
        real(path, text)

    monkeypatch.setattr(vault_module, "_write_private", flaky)
    with pytest.raises(OSError):
        vault.change_password(PASSWORD, NEW_PASSWORD)
    monkeypatch.setattr(vault_module, "_write_private", real)

    _assert_opens(vault_root, PASSWORD, keys)
    with pytest.raises(BadPasswordError):
        Vault(vault_root, **FAST_KDF).unlock(NEW_PASSWORD)
    # The vault that saw the failure still holds the old password too.
    record = vault.create("after")
    keys[record.address] = vault.private_key(record.address)
    _assert_opens(vault_root, PASSWORD, keys)


def test_change_password_killed_after_the_commit_finishes_on_the_next_unlock(
    vault_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, keys = _vault_with_wallets(vault_root)
    real_replace = os.replace
    promoted = {"count": 0}

    class Killed(BaseException):
        pass

    def dying_replace(src: str | Path, dst: str | Path) -> None:
        # The index (the commit point) lands; the process dies while the
        # staged keystores are being moved into place.
        if str(src).endswith(".rekey"):
            promoted["count"] += 1
            if promoted["count"] == 2:
                raise Killed
        real_replace(src, dst)

    monkeypatch.setattr(vault_module.os, "replace", dying_replace)
    with pytest.raises(Killed):
        vault.change_password(PASSWORD, NEW_PASSWORD)
    monkeypatch.setattr(vault_module.os, "replace", real_replace)

    with pytest.raises(BadPasswordError):
        Vault(vault_root, **FAST_KDF).unlock(PASSWORD)
    _assert_opens(vault_root, NEW_PASSWORD, keys)


def test_change_password_still_reencrypts_everything(vault_root: Path) -> None:
    vault, keys = _vault_with_wallets(vault_root)
    vault.change_password(PASSWORD, NEW_PASSWORD)
    _assert_opens(vault_root, NEW_PASSWORD, keys)
    with pytest.raises(BadPasswordError):
        Vault(vault_root, **FAST_KDF).unlock(PASSWORD)


def test_keystore_and_index_writes_are_flushed_to_disk(
    vault_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced_files: list[int] = []
    synced_dirs: list[Path] = []
    monkeypatch.setattr(vault_module, "fsync_file", synced_files.append)
    monkeypatch.setattr(vault_module, "fsync_dir", synced_dirs.append)

    vault = Vault(vault_root, **FAST_KDF)
    vault.setup(PASSWORD, "manual")
    synced_files.clear()
    synced_dirs.clear()
    vault.create("w0")
    # The keystore, then the index: each flushed before its rename, and the
    # directory after it.
    assert len(synced_files) == 2
    assert synced_dirs == [vault_root, vault_root]


def test_sealing_key_file_is_flushed_to_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kek_dir = tmp_path / "vault-keys"
    monkeypatch.setenv("AGENTOS_VAULT_KEK_DIR", str(kek_dir))
    monkeypatch.delenv("AGENTOS_VAULT_KEK", raising=False)
    synced: list[int] = []
    synced_dirs: list[Path] = []
    monkeypatch.setattr(unlock_key, "fsync_file", synced.append)
    monkeypatch.setattr(unlock_key, "fsync_dir", synced_dirs.append)
    unlock_key.seal_password(PASSWORD)
    assert len(synced) == 1
    assert synced_dirs == [kek_dir]


def test_moving_the_key_directory_keeps_auto_unlock_and_moves_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setting ``AGENTOS_VAULT_KEK_DIR`` on an existing vault is not a lockout."""
    legacy = tmp_path / "home" / "vault-keys"
    moved = tmp_path / "separate-mount" / "vault-keys"
    monkeypatch.delenv("AGENTOS_VAULT_KEK", raising=False)
    monkeypatch.delenv("AGENTOS_VAULT_KEK_DIR", raising=False)
    monkeypatch.setattr("agentos.paths.default_vault_kek_dir", lambda: legacy)
    monkeypatch.setattr(unlock_key, "default_vault_kek_dir", lambda: legacy)
    monkeypatch.setattr(unlock_key, "dpapi_available", lambda: False)

    root = tmp_path / "state" / "wallets"
    vault = Vault(root, **FAST_KDF)
    vault.setup(PASSWORD, "auto")
    address = vault.create("w0").address
    assert len(list(legacy.iterdir())) == 1

    monkeypatch.setenv("AGENTOS_VAULT_KEK_DIR", str(moved))
    restarted = Vault(root, **FAST_KDF)
    assert restarted.try_auto_unlock() is True
    assert restarted.private_key(address) == vault.private_key(address)
    # Sealed again under a key in the new directory; the old key is gone.
    assert len(list(moved.iterdir())) == 1
    assert list(legacy.iterdir()) == []
    assert Vault(root, **FAST_KDF).try_auto_unlock() is True
