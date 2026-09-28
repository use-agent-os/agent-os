"""Issue #3504: a vault password with surrounding whitespace never auto-unlocked.

``setup`` writes the password to ``unlock.key`` exactly as it was set;
``try_auto_unlock`` read it back with ``.strip()``. A trailing newline — what
a paste, a file or ``$(cat secret)`` leaves behind — therefore produced a
different string, `unlock` rejected it, and the method, documented "Never
raises", returned ``False``. The desk stayed locked after every restart in
the mode whose whole purpose is that it does not.

The read now tries the file verbatim first and the stripped form second, so
what was written opens the vault and a file someone edited by hand still
does too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentos.trading.vault import Vault

PASSWORD = "hunter2-prod"

SURROUNDED = [
    pytest.param(f"{PASSWORD}\n", id="trailing_newline"),
    pytest.param(f"{PASSWORD}\r\n", id="trailing_crlf"),
    pytest.param(f"{PASSWORD} ", id="trailing_space"),
    pytest.param(f" {PASSWORD}", id="leading_space"),
    pytest.param(f"\t{PASSWORD}\t", id="surrounding_tabs"),
]


def _vault(tmp_path: Path) -> Vault:
    return Vault(root=tmp_path / "wallets")


def _set_up(tmp_path: Path, password: str) -> Vault:
    """A vault in ``auto`` mode, locked, as it is after a restart."""
    vault = _vault(tmp_path)
    vault.setup(password, unlock_mode="auto")
    vault.lock()
    return vault


# ── the report ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("password", SURROUNDED)
def test_a_password_with_surrounding_whitespace_auto_unlocks(tmp_path: Path, password: str) -> None:
    _set_up(tmp_path, password)

    assert _vault(tmp_path).try_auto_unlock() is True


@pytest.mark.parametrize("password", SURROUNDED)
def test_the_stored_file_still_holds_exactly_what_was_set(tmp_path: Path, password: str) -> None:
    """Nothing rewrites the user's password: the file holds those bytes.

    Read with ``newline=""`` on purpose -- ``read_text`` translates line
    endings, which is the very thing that used to corrupt a password holding
    one on Windows.
    """
    _set_up(tmp_path, password)

    with (tmp_path / "wallets" / "unlock.key").open(encoding="utf-8", newline="") as handle:
        assert handle.read() == password


@pytest.mark.parametrize("password", SURROUNDED)
def test_the_same_password_still_unlocks_by_hand(tmp_path: Path, password: str) -> None:
    """Typing it always worked; that must not change while the read is fixed."""
    _set_up(tmp_path, password)

    vault = _vault(tmp_path)
    vault.unlock(password)

    assert vault.unlocked is True


def test_a_hand_edited_file_with_a_stray_newline_still_works(tmp_path: Path) -> None:
    """Why the strip was there: an editor that appends a newline to
    ``unlock.key`` must not lock the desk out either."""
    _set_up(tmp_path, PASSWORD)
    (tmp_path / "wallets" / "unlock.key").write_text(f"{PASSWORD}\n", encoding="utf-8")

    assert _vault(tmp_path).try_auto_unlock() is True


# ── what must not change ────────────────────────────────────────────────────


def test_an_ordinary_password_auto_unlocks(tmp_path: Path) -> None:
    _set_up(tmp_path, PASSWORD)

    assert _vault(tmp_path).try_auto_unlock() is True


def test_a_password_with_inner_spaces_is_untouched(tmp_path: Path) -> None:
    _set_up(tmp_path, "hunter 2 prod pass")

    assert _vault(tmp_path).try_auto_unlock() is True


def test_a_wrong_password_in_the_file_does_not_unlock(tmp_path: Path) -> None:
    _set_up(tmp_path, PASSWORD)
    (tmp_path / "wallets" / "unlock.key").write_text("not-the-password", encoding="utf-8")

    vault = _vault(tmp_path)

    assert vault.try_auto_unlock() is False
    assert vault.unlocked is False


def test_an_empty_file_does_not_unlock(tmp_path: Path) -> None:
    _set_up(tmp_path, PASSWORD)
    (tmp_path / "wallets" / "unlock.key").write_text("   \n", encoding="utf-8")

    assert _vault(tmp_path).try_auto_unlock() is False


def test_manual_mode_never_auto_unlocks(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    vault.setup(PASSWORD, unlock_mode="manual")
    vault.lock()

    assert _vault(tmp_path).try_auto_unlock() is False
    assert not (tmp_path / "wallets" / "unlock.key").exists()


def test_an_uninitialized_vault_reports_false(tmp_path: Path) -> None:
    assert _vault(tmp_path).try_auto_unlock() is False


def test_an_already_unlocked_vault_short_circuits(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    vault.setup(PASSWORD, unlock_mode="auto")

    assert vault.unlocked is True
    assert vault.try_auto_unlock() is True
