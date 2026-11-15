from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from agentos.trading.vault import (
    BadPasswordError,
    Vault,
    VaultAlreadyInitializedError,
    VaultLockedError,
    VaultNotInitializedError,
    WalletNotFoundError,
)
from tests.test_trading.conftest import FAST_KDF, PASSWORD


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def _assert_private(path: Path, mode: int) -> None:
    """POSIX permission bits; Windows keeps its ACLs and reports 0o666/0o777."""
    if os.name != "nt":
        assert _mode(path) == mode


class TestSetupAndUnlock:
    def test_status_before_setup(self, vault: Vault) -> None:
        status = vault.status()
        assert status["initialized"] is False
        assert status["unlocked"] is False
        assert status["walletCount"] == 0
        with pytest.raises(VaultNotInitializedError):
            vault.list()

    def test_setup_auto_writes_unlock_key_with_private_modes(
        self, vault: Vault, vault_root: Path
    ) -> None:
        vault.setup(PASSWORD, "auto")
        assert vault.initialized and vault.unlocked
        assert vault.unlock_mode == "auto"
        _assert_private(vault_root, 0o700)
        _assert_private(vault.index_path, 0o600)
        _assert_private(vault.unlock_path, 0o600)
        assert vault.unlock_path.read_text() == PASSWORD
        with pytest.raises(VaultAlreadyInitializedError):
            vault.setup(PASSWORD, "auto")

    def test_setup_manual_keeps_nothing_on_disk(self, vault: Vault) -> None:
        vault.setup(PASSWORD, "manual")
        assert not vault.unlock_path.exists()
        assert vault.unlock_mode == "manual"
        assert "verifier" in json.loads(vault.index_path.read_text())

    def test_short_password_rejected(self, vault: Vault) -> None:
        with pytest.raises(ValueError):
            vault.setup("short", "auto")

    def test_auto_unlock_from_file(self, vault_root: Path) -> None:
        Vault(vault_root, **FAST_KDF).setup(PASSWORD, "auto")
        fresh = Vault(vault_root, **FAST_KDF)
        assert not fresh.unlocked
        assert fresh.try_auto_unlock() is True
        assert fresh.unlocked

    def test_auto_unlock_refuses_manual_mode(self, vault_root: Path) -> None:
        Vault(vault_root, **FAST_KDF).setup(PASSWORD, "manual")
        fresh = Vault(vault_root, **FAST_KDF)
        assert fresh.try_auto_unlock() is False
        with pytest.raises(BadPasswordError):
            fresh.unlock("wrong password!")
        fresh.unlock(PASSWORD)
        assert fresh.unlocked
        fresh.lock()
        assert not fresh.unlocked

    def test_switch_modes(self, vault: Vault) -> None:
        vault.setup(PASSWORD, "auto")
        vault.set_unlock_mode("manual", PASSWORD)
        assert not vault.unlock_path.exists()
        with pytest.raises(BadPasswordError):
            vault.set_unlock_mode("auto", "not the password")
        vault.set_unlock_mode("auto", PASSWORD)
        assert vault.unlock_path.read_text() == PASSWORD


class TestWallets:
    def test_create_import_export_roundtrip(self, vault: Vault, vault_root: Path) -> None:
        vault.setup(PASSWORD, "auto")
        first = vault.create("Main")
        assert first.address.startswith("0x") and len(first.address) == 42
        assert vault.primary_address() == first.address
        _assert_private(vault.keystore_path(first.address), 0o600)

        exported = vault.export(first.address, PASSWORD, "privateKey")
        assert exported.startswith("0x") and len(exported) == 66
        with pytest.raises(BadPasswordError):
            vault.export(first.address, "wrong password!!", "privateKey")

        keystore = vault.export(first.address, PASSWORD, "keystore")
        assert json.loads(keystore)["version"] == 3

        other = Vault(vault_root.parent / "other", **FAST_KDF)
        other.setup("another password", "manual")
        imported = other.import_private_key("Copy", exported)
        assert imported.address == first.address
        assert imported.imported is True
        with pytest.raises(Exception, match="already exists"):
            other.import_private_key("Copy", exported)
        with pytest.raises(Exception, match="already exists"):
            other.import_keystore("From keystore", keystore, PASSWORD)

    def test_import_keystore_duplicate_and_bad_password(self, vault: Vault) -> None:
        vault.setup(PASSWORD, "auto")
        record = vault.create("A")
        keystore = vault.export(record.address, PASSWORD, "keystore")
        with pytest.raises(Exception, match="already exists"):
            vault.import_keystore("dup", keystore, PASSWORD)
        vault.remove(record.address, PASSWORD)
        with pytest.raises(BadPasswordError):
            vault.import_keystore("again", keystore, "not it either")
        again = vault.import_keystore("again", keystore, PASSWORD)
        assert again.address == record.address

    def test_locked_vault_cannot_create_or_sign(self, vault: Vault) -> None:
        vault.setup(PASSWORD, "manual")
        record = vault.create("A")
        vault.lock()
        with pytest.raises(VaultLockedError):
            vault.create("B")
        with pytest.raises(VaultLockedError):
            vault.private_key(record.address)
        vault.unlock(PASSWORD)
        assert len(vault.private_key(record.address)) == 32

    def test_rename_primary_remove(self, vault: Vault) -> None:
        vault.setup(PASSWORD, "auto")
        a = vault.create("A")
        b = vault.create("B")
        assert vault.primary_address() == a.address
        vault.set_primary(b.address)
        assert vault.primary_address() == b.address
        assert vault.rename(a.address, "Alpha").label == "Alpha"
        assert vault.resolve("alpha").address == a.address
        assert vault.resolve(None).address == b.address
        with pytest.raises(WalletNotFoundError):
            vault.resolve("nope")
        with pytest.raises(BadPasswordError):
            vault.remove(b.address, "wrong wrong wrong")
        vault.remove(b.address, PASSWORD)
        assert vault.primary_address() == a.address
        assert not vault.keystore_path(b.address).exists()
        with pytest.raises(WalletNotFoundError):
            vault.get(b.address)

    def test_change_password_reencrypts_everything(self, vault: Vault, vault_root: Path) -> None:
        vault.setup(PASSWORD, "auto")
        record = vault.create("A")
        key = vault.private_key(record.address)
        vault.change_password(PASSWORD, "brand new password")
        assert vault.unlock_path.read_text() == "brand new password"
        fresh = Vault(vault_root, **FAST_KDF)
        with pytest.raises(BadPasswordError):
            fresh.unlock(PASSWORD)
        fresh.unlock("brand new password")
        assert fresh.private_key(record.address) == key

    def test_created_block_stamp(self, vault: Vault) -> None:
        vault.setup(PASSWORD, "auto")
        record = vault.create("A")
        vault.set_created_block(record.address, 8453, 12345)
        assert vault.get(record.address).created_block == {"8453": 12345}
        assert vault.get(record.address).to_dict(primary=True)["createdBlock"] == {"8453": 12345}

    def test_bad_private_key_input(self, vault: Vault) -> None:
        vault.setup(PASSWORD, "auto")
        with pytest.raises(ValueError):
            vault.import_private_key("x", "0x1234")
        with pytest.raises(ValueError):
            vault.import_private_key("x", "zz" * 32)
        with pytest.raises(ValueError):
            vault.import_keystore("x", "not json", PASSWORD)


class TestKeystoreImportBounds:
    def test_hostile_kdf_parameters_are_refused_before_decrypting(self, vault: Vault) -> None:
        vault.setup(PASSWORD, "auto")
        hostile = {
            "version": 3,
            "address": "0" * 40,
            "crypto": {
                "cipher": "aes-128-ctr",
                "ciphertext": "00",
                "cipherparams": {"iv": "00" * 16},
                "kdf": "scrypt",
                "kdfparams": {"n": 2**30, "r": 8, "p": 1, "dklen": 32, "salt": "00" * 32},
                "mac": "00" * 32,
            },
        }
        with pytest.raises(ValueError, match="too expensive"):
            vault.import_keystore("evil", json.dumps(hostile), "pw")
        hostile["crypto"]["kdf"] = "pbkdf2"
        hostile["crypto"]["kdfparams"] = {"c": 2**28, "dklen": 32, "prf": "hmac-sha256"}
        with pytest.raises(ValueError, match="too expensive"):
            vault.import_keystore("evil", json.dumps(hostile), "pw")
        hostile["crypto"]["kdf"] = "argon2"
        with pytest.raises(ValueError, match="not supported"):
            vault.import_keystore("evil", json.dumps(hostile), "pw")
        with pytest.raises(ValueError, match="no crypto"):
            vault.import_keystore("evil", json.dumps({"version": 3}), "pw")


class TestLedgerSealKey:
    """The ledger seal key: as protected as the wallet keys, stable for the vault's life."""

    def test_only_in_memory_while_unlocked_and_stable(self, vault_root: Path) -> None:
        vault = Vault(vault_root, **FAST_KDF)
        vault.setup(PASSWORD, "manual")
        key = vault.ledger_seal_key()
        assert key is not None and len(key) == 32
        assert vault.take_new_seal() is False  # a new vault has nothing to adopt
        assert key.hex() not in vault.index_path.read_text(encoding="utf-8")
        vault.lock()
        assert vault.ledger_seal_key() is None
        vault.unlock(PASSWORD)
        assert vault.ledger_seal_key() == key
        vault.change_password(PASSWORD, "another long password")
        assert vault.ledger_seal_key() == key
        restarted = Vault(vault_root, **FAST_KDF)
        assert restarted.ledger_seal_key() is None
        restarted.unlock("another long password")
        assert restarted.ledger_seal_key() == key

    def test_a_vault_from_before_seals_gets_one_at_its_next_unlock(self, vault_root: Path) -> None:
        Vault(vault_root, **FAST_KDF).setup(PASSWORD, "auto")
        index = json.loads((vault_root / "index.json").read_text(encoding="utf-8"))
        index.pop("ledger_seal")
        (vault_root / "index.json").write_text(json.dumps(index), encoding="utf-8")
        old = Vault(vault_root, **FAST_KDF)
        assert old.try_auto_unlock() is True
        assert old.ledger_seal_key() is not None
        assert old.take_new_seal() is True and old.take_new_seal() is False
        again = Vault(vault_root, **FAST_KDF)
        again.unlock(PASSWORD)
        assert again.ledger_seal_key() == old.ledger_seal_key()
        assert again.take_new_seal() is False

    def test_a_removed_and_reimported_key_is_another_incarnation(self, vault: Vault) -> None:
        vault.setup(PASSWORD, "auto")
        record = vault.import_private_key("A", "0x" + "4c" * 32)
        first = vault.incarnation(record.address)
        assert first is not None
        vault.remove(record.address, PASSWORD)
        assert vault.incarnation(record.address) is None
        again = vault.import_private_key("A again", "0x" + "4c" * 32)
        assert again.address == record.address
        assert vault.incarnation(again.address) not in (None, first)
