"""WAL-09: wallet material the shape/name passes miss is masked in tool output.

A skill hot-wallet key is a bare ``0x``+64-hex string, the vault password is a
bare line, and a keystore's ``ciphertext`` field name is not a credential name,
so ``printenv UNIV4_LP_PRIVATE_KEY``, ``echo $KEY`` and (under ``/elevated
full``) ``cat unlock.key`` / ``cat keystore.json`` handed the secret to the
model verbatim. The fix masks by registered *value* and by key-shaped context,
without blindly masking tx hashes, which are also 64 hex.
"""

from __future__ import annotations

import pytest

from agentos import redact

KEY = "0x" + "55" * 32
BARE = "55" * 32
TX_HASH = "0x" + "ab" * 32
MNEMONIC = "legal winner thank year wave sausage worth useful legal winner thank yellow"


@pytest.fixture(autouse=True)
def _clean_registry():
    # getattr so the behavioral tests (unlock.key, keystore, mnemonic) still run
    # and fail — rather than error — on a tree without the registry API.
    clear = getattr(redact, "clear_registered_secret_values", lambda: None)
    clear()
    yield
    clear()


def _register(*values: str) -> None:
    register = getattr(redact, "register_secret_values", None)
    if register is None:
        pytest.skip("register_secret_values not available on this tree")
    register(*values)


@pytest.mark.parametrize(
    "command",
    ["printenv UNIV4_LP_PRIVATE_KEY", "echo $UNIV4_LP_PRIVATE_KEY", "env", "ls"],
)
def test_registered_key_is_masked_however_it_is_printed(command: str) -> None:
    _register(KEY)
    out = redact.redact_terminal_output(KEY + "\n", command)
    assert KEY not in out
    assert BARE not in out  # the 0x-less form is registered too


def test_registered_key_without_0x_is_masked() -> None:
    _register(KEY)
    assert BARE not in redact.redact_terminal_output(f"key={BARE}\n", "cat note.txt")


def test_an_unregistered_tx_hash_is_not_masked() -> None:
    """A 64-hex value that is not a known secret (a tx hash) stays readable."""
    _register(KEY)
    out = redact.redact_terminal_output(f"mined {TX_HASH}\n", "cat log.txt")
    assert TX_HASH in out


def test_vault_password_file_is_masked_whole() -> None:
    out = redact.redact_terminal_output(
        "Tr0ub4dor-vault-pw\n", "cat /home/x/.agentos/wallets/unlock.key"
    )
    assert "Tr0ub4dor-vault-pw" not in out


def test_read_file_masks_unlock_key() -> None:
    masked = redact.redact_file_output("Tr0ub4dor-vault-pw\n", path="/x/wallets/unlock.key")
    assert "Tr0ub4dor" not in masked


def test_keystore_ciphertext_and_mac_are_masked() -> None:
    keystore = (
        '{"version":3,"crypto":{"cipher":"aes-128-ctr","ciphertext":"'
        + "de" * 32
        + '","cipherparams":{"iv":"'
        + "11" * 16
        + '"},"mac":"'
        + "ad" * 32
        + '","kdf":"scrypt"}}'
    )
    for out in (
        redact.redact_file_output(keystore, path="/x/0xabc.json"),
        redact.redact_terminal_output(keystore + "\n", "cat /x/wallets/0xabc.json"),
    ):
        assert "de" * 32 not in out
        assert "ad" * 32 not in out
        assert "11" * 16 not in out


@pytest.mark.parametrize(
    "length",
    [12, 24],
)
def test_bip39_mnemonic_is_masked(length: int) -> None:
    words = MNEMONIC.split()[:12]
    phrase = " ".join((words * 2)[:length])
    out = redact.redact_terminal_output(f"seed: {phrase}\n", "cat seed.txt")
    assert phrase not in out


def test_ordinary_prose_is_not_mistaken_for_a_mnemonic() -> None:
    prose = "the quick brown fox jumps over the lazy dog and then again today"
    assert "quick" in redact.redact_terminal_output(prose, "echo hello")


def test_registration_is_idempotent_and_additive() -> None:
    _register(KEY, None, "short")  # short/None ignored
    _register(KEY)
    out = redact.redact_terminal_output(f"{KEY} short\n", "ls")
    assert KEY not in out
    assert "short" in out  # too short to register


def test_known_vendor_key_still_masked_by_shape() -> None:
    openai = "sk-proj-" + "a" * 40
    assert openai not in redact.redact_terminal_output(openai + "\n", "printenv OPENAI_API_KEY")


def test_env_store_registers_a_secret_named_value(monkeypatch, tmp_path) -> None:
    """Setting a hot-wallet key through env_store makes it maskable (WAL-09)."""
    from agentos import env_store

    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path))
    env_store.set_env_var("POOLSFUN_PRIVATE_KEY", KEY)
    assert KEY not in redact.redact_terminal_output(KEY + "\n", "printenv POOLSFUN_PRIVATE_KEY")
