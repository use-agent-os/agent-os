"""``Vault.create_batch`` and the default wallet name."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from eth_account import Account
from eth_account.hdaccount import ETHEREUM_DEFAULT_PATH, key_from_seed, seed_from_mnemonic

from agentos.trading.vault import (
    MAX_BATCH_CREATE,
    Vault,
    VaultError,
    VaultLockedError,
    WalletExistsError,
    default_label,
)
from tests.test_trading.conftest import PASSWORD


def test_default_label_is_first_five_and_last_four() -> None:
    assert default_label("0xAbCdEf0000000000000000000000000000001234") == "0xAbC_1234"


def test_unlabelled_wallet_gets_the_address_name(vault: Vault) -> None:
    vault.setup(PASSWORD, "manual")
    made = vault.create("")
    assert made.label == default_label(made.address)
    imported = vault.import_private_key("  ", "0x" + "55" * 32)
    assert imported.label == default_label(imported.address)
    # A given name still wins.
    assert vault.create("Main").label == "Main"


def test_batch_makes_wallets_restorable_from_their_phrase(vault: Vault, vault_root: Path) -> None:
    vault.setup(PASSWORD, "manual")
    minted = vault.create_batch(3)
    assert len(minted) == 3
    assert len({m.record.address for m in minted}) == 3
    index = json.loads(vault.index_path.read_text())
    assert [w["address"] for w in index["wallets"]] == [m.record.address for m in minted]
    assert index["primary"] == minted[0].record.address
    for m in minted:
        words = m.mnemonic.split()
        assert len(words) == 12
        # The phrase restores the same address any wallet app would derive.
        key = key_from_seed(seed_from_mnemonic(m.mnemonic, ""), ETHEREUM_DEFAULT_PATH)
        assert "0x" + key.hex() == m.private_key
        assert Account.from_key(key).address == m.record.address
        assert m.record.label == default_label(m.record.address)
        assert m.record.imported is False
        # The keystore holds the key under the vault password; no phrase is stored.
        keystore = vault.keystore_path(m.record.address).read_text()
        assert m.mnemonic not in keystore
        assert "0x" + bytes(Account.decrypt(json.loads(keystore), PASSWORD)).hex() == m.private_key
        assert m.private_key not in repr(m) and m.mnemonic not in repr(m)
    assert m.mnemonic not in vault.index_path.read_text()
    # Still signable after a lock and unlock.
    vault.lock()
    vault.unlock(PASSWORD)
    assert "0x" + vault.private_key(minted[1].record.address).hex() == minted[1].private_key


def test_batch_label_numbers_the_wallets(vault: Vault) -> None:
    vault.setup(PASSWORD, "manual")
    vault.create("First")
    minted = vault.create_batch(2, "  Sniper ")
    assert [m.record.label for m in minted] == ["Sniper 1", "Sniper 2"]
    assert vault.primary_address() != minted[0].record.address  # primary unchanged


@pytest.mark.parametrize("count", [0, -1, MAX_BATCH_CREATE + 1, True, 2.0])
def test_batch_count_bounds(vault: Vault, count: object) -> None:
    vault.setup(PASSWORD, "manual")
    with pytest.raises(ValueError):
        vault.create_batch(count)  # type: ignore[arg-type]
    assert vault.list() == []


def test_batch_needs_an_open_vault(vault: Vault) -> None:
    vault.setup(PASSWORD, "manual")
    vault.lock()
    with pytest.raises(VaultLockedError):
        vault.create_batch(2)


def test_batch_refuses_when_the_password_changed_midway(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault.setup(PASSWORD, "manual")
    real = vault._encrypt
    calls = {"n": 0}

    def encrypt(key: bytes, password: str) -> dict:
        calls["n"] += 1
        if calls["n"] == 2:
            vault.change_password(PASSWORD, "a brand new password")
        return real(key, password)

    monkeypatch.setattr(vault, "_encrypt", encrypt)
    with pytest.raises(VaultError, match="password changed"):
        vault.create_batch(3)
    assert vault.list() == []
    assert not any(p.name.startswith("0x") for p in vault.root.iterdir())


def test_batch_rolls_back_keystores_when_the_index_write_fails(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault.setup(PASSWORD, "manual")

    def boom(_index: dict) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(vault, "_write_index", boom)
    with pytest.raises(OSError):
        vault.create_batch(2)
    monkeypatch.undo()
    assert vault.list() == []
    assert not any(p.name.startswith("0x") for p in vault.root.iterdir())


def test_duplicate_import_is_a_wallet_exists_error(vault: Vault) -> None:
    vault.setup(PASSWORD, "manual")
    vault.import_private_key("a", "0x" + "55" * 32)
    with pytest.raises(WalletExistsError):
        vault.import_private_key("b", "55" * 32)
