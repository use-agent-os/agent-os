"""WAL-01: the state directory alone no longer yields the vault password.

The audit's proof of concept read every file under ``<state>`` (one recursive
``grep``/``cp``/``tar`` of the parent of ``wallets/``) and found the vault
password in plain text in ``unlock.key`` beside the keystores it decrypts.
``unlock.key`` now holds it sealed under a key kept outside the state root;
these tests port that read and the migration of an existing plain-text file.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from agentos.trading.vault import BadPasswordError, Vault
from tests.test_trading.conftest import FAST_KDF

PASSWORD = "DUMMYvaultPW_5555555555"


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


def _every_byte_under(root: Path) -> bytes:
    """What a recursive read of *root* hands back: every file's contents."""
    chunks: list[bytes] = []
    for directory, _dirs, files in os.walk(root):
        for name in files:
            chunks.append((Path(directory) / name).read_bytes())
    return b"\n".join(chunks)


def _write_legacy_plaintext(vault: Vault, password: str) -> None:
    with vault.unlock_path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(password)


def test_recursive_read_of_the_state_dir_does_not_carry_the_password(state: Path) -> None:
    vault = _vault(state)
    vault.setup(PASSWORD, "auto")
    vault.create("main")
    dump = _every_byte_under(state)
    assert PASSWORD.encode() not in dump
    # The file itself is not a password either: it does not open the vault.
    with pytest.raises((BadPasswordError, ValueError)):
        _vault(state).unlock(vault.unlock_path.read_text(encoding="utf-8"))
    # ... while the gateway still auto-unlocks after a restart.
    fresh = _vault(state)
    assert fresh.try_auto_unlock() is True


def test_the_sealing_key_lives_outside_the_state_dir(state: Path, kek_dir: Path) -> None:
    _vault(state).setup(PASSWORD, "auto")
    kek_files = list(kek_dir.iterdir())
    assert len(kek_files) == 1
    assert state not in kek_files[0].parents
    if os.name != "nt":
        assert kek_files[0].stat().st_mode & 0o077 == 0
        assert kek_dir.stat().st_mode & 0o077 == 0


def test_state_dir_without_its_key_does_not_unlock(state: Path, kek_dir: Path) -> None:
    """A copy of the state dir (backup, exfiltrated tree) is not enough."""
    _vault(state).setup(PASSWORD, "auto")
    for path in kek_dir.iterdir():
        path.unlink()
    fresh = _vault(state)
    assert fresh.try_auto_unlock() is False  # never raises
    assert fresh.unlocked is False


def test_legacy_plaintext_unlock_key_is_migrated(state: Path) -> None:
    vault = _vault(state)
    vault.setup(PASSWORD, "auto")
    _write_legacy_plaintext(vault, PASSWORD)  # a vault written before sealing
    assert vault.status()["unlockKeyProtection"] == "plaintext"

    upgraded = _vault(state)
    assert upgraded.try_auto_unlock() is True
    assert PASSWORD.encode() not in _every_byte_under(state)
    assert upgraded.status()["unlockKeyProtection"].startswith("sealed:")
    assert _vault(state).try_auto_unlock() is True


def test_operator_unlock_reseals_when_the_key_was_lost(state: Path, kek_dir: Path) -> None:
    """A container recreated without its home: one manual unlock restores auto unlock."""
    _vault(state).setup(PASSWORD, "auto")
    for path in kek_dir.iterdir():
        path.unlink()
    vault = _vault(state)
    assert vault.try_auto_unlock() is False
    vault.unlock(PASSWORD)
    assert _vault(state).try_auto_unlock() is True


def test_a_deleted_unlock_key_is_not_recreated_by_unlock(state: Path) -> None:
    vault = _vault(state)
    vault.setup(PASSWORD, "auto")
    vault.unlock_path.unlink()
    vault.lock()
    vault.unlock(PASSWORD)
    assert not vault.unlock_path.exists()


def test_tampered_unlock_key_fails_closed(state: Path) -> None:
    vault = _vault(state)
    vault.setup(PASSWORD, "auto")
    text = vault.unlock_path.read_text(encoding="utf-8")
    vault.unlock_path.write_text(text.replace('"ciphertext": "', '"ciphertext": "AAAA'))
    assert _vault(state).try_auto_unlock() is False


def test_manual_mode_removes_the_sealing_key(state: Path, kek_dir: Path) -> None:
    vault = _vault(state)
    vault.setup(PASSWORD, "auto")
    assert list(kek_dir.iterdir())
    vault.set_unlock_mode("manual", PASSWORD)
    assert not vault.unlock_path.exists()
    assert list(kek_dir.iterdir()) == []


def test_change_password_rotates_the_sealing_key(state: Path, kek_dir: Path) -> None:
    vault = _vault(state)
    vault.setup(PASSWORD, "auto")
    before = {p.name for p in kek_dir.iterdir()}
    vault.change_password(PASSWORD, PASSWORD + "-rotated")
    after = {p.name for p in kek_dir.iterdir()}
    assert len(after) == 1 and after != before
    assert _vault(state).try_auto_unlock() is True


def test_injected_kek_keeps_nothing_on_disk_but_the_sealed_file(
    state: Path, kek_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTOS_VAULT_KEK", "ab" * 32)
    vault = _vault(state)
    vault.setup(PASSWORD, "auto")
    assert vault.status()["unlockKeyProtection"] == "sealed:env"
    assert not kek_dir.exists() or list(kek_dir.iterdir()) == []
    assert _vault(state).try_auto_unlock() is True
    monkeypatch.delenv("AGENTOS_VAULT_KEK")
    assert _vault(state).try_auto_unlock() is False


def test_the_sealing_key_is_on_the_sensitive_floor(state: Path, kek_dir: Path) -> None:
    from agentos.sandbox.sensitive_paths import is_sensitive_path

    _vault(state).setup(PASSWORD, "auto")
    (kek_file,) = kek_dir.iterdir()
    assert is_sensitive_path(str(kek_file)) is not None


def test_the_vault_kek_env_never_reaches_a_child() -> None:
    from agentos.env_policy import GATEWAY_CREDENTIAL_ENV, WRITE_DENYLIST

    assert "AGENTOS_VAULT_KEK" in GATEWAY_CREDENTIAL_ENV
    assert "AGENTOS_VAULT_KEK_DIR" in WRITE_DENYLIST


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Data Protection API")
def test_windows_seals_the_key_with_dpapi(state: Path, kek_dir: Path) -> None:
    vault = _vault(state)
    vault.setup(PASSWORD, "auto")
    assert vault.status()["unlockKeyProtection"] == "sealed:dpapi"
    (blob,) = kek_dir.iterdir()
    assert blob.suffix == ".dpapi"
    assert len(blob.read_bytes()) > 32  # a DPAPI blob, not the raw key
    assert _vault(state).try_auto_unlock() is True
