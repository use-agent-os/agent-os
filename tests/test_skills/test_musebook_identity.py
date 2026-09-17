"""Unit tests for the bundled musebook skill's identity storage and permissions.

Asserts:
- State directory creation uses mode 0700 and key file uses mode 0600 atomically.
- Existing identity fields are preserved during partial updates (e.g. intro save-identity).
- Malformed or non-dict existing identity files are not clobbered or wiped.
- cmd_save validates the ed25519 private key seed before writing.
- State root path resolution correctly handles AGENTOS_STATE_DIR and AGENTOS_HOME.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "src" / "agentos" / "skills" / "bundled" / "musebook" / "scripts" / "muse.py"


@pytest.fixture
def muse():
    spec = importlib.util.spec_from_file_location("muse", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def muse_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state_dir = tmp_path / "state" / "muse"
    monkeypatch.setenv("MUSE_STATE_DIR", str(state_dir))
    monkeypatch.delenv("MUSEBOOK_MUSE_ID", raising=False)
    monkeypatch.delenv("MUSEBOOK_SECRET", raising=False)
    return state_dir


def test_save_identity_creates_mode_0600_and_parent_0700(muse, muse_state: Path) -> None:
    pub, sec = muse.generate_keypair()
    saved = muse.save_identity(public_key=pub, secret=sec)

    assert saved.is_file()
    assert saved == muse_state / "musebook.json"

    data = json.loads(saved.read_text(encoding="utf-8"))
    assert data["public_key"] == pub
    assert data["secret"] == sec

    if os.name != "nt":
        file_mode = stat.S_IMODE(saved.stat().st_mode)
        parent_mode = stat.S_IMODE(saved.parent.stat().st_mode)
        assert file_mode == 0o600
        assert parent_mode == 0o700


def test_save_identity_atomic_update_preserves_fields(muse, muse_state: Path) -> None:
    pub, sec = muse.generate_keypair()
    muse.save_identity(public_key=pub, secret=sec)

    # Later call from intro --save-identity only supplies muse_id
    muse.save_identity(muse_id="muse_test123")

    loaded = muse.load_identity()
    assert loaded["public_key"] == pub
    assert loaded["secret"] == sec
    assert loaded["muse_id"] == "muse_test123"


def test_save_identity_does_not_clobber_corrupted_identity(muse, muse_state: Path) -> None:
    key_file = muse_state / "musebook.json"
    key_file.parent.mkdir(parents=True, exist_ok=True)
    corrupted_content = "{\ninvalid json here\n"
    key_file.write_text(corrupted_content, encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        muse.save_identity(muse_id="muse_new")

    assert "corrupted" in str(exc_info.value) or "unreadable" in str(exc_info.value)
    # The file must remain untouched and not be replaced by an empty/partial dict
    assert key_file.read_text(encoding="utf-8") == corrupted_content


def test_save_identity_does_not_clobber_non_dict_identity(muse, muse_state: Path) -> None:
    key_file = muse_state / "musebook.json"
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text('["not", "a", "json", "object"]\n', encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        muse.save_identity(muse_id="muse_new")

    assert "does not contain a JSON object" in str(exc_info.value)


def test_cmd_save_validates_secret(muse, muse_state: Path) -> None:
    parser = muse.build_parser()
    args_bad = parser.parse_args(["save", "--secret", "not-a-valid-ed25519-secret"])

    with pytest.raises(SystemExit):
        muse.cmd_save(args_bad)

    assert not (muse_state / "musebook.json").exists()

    pub, sec = muse.generate_keypair()
    args_good = parser.parse_args(["save", "--secret", sec, "--public-key", pub])
    exit_code = muse.cmd_save(args_good)
    assert exit_code == 0
    assert muse.load_identity()["secret"] == sec


def test_state_root_resolution_precedence(
    muse, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom_muse = tmp_path / "custom_muse"
    agentos_state = tmp_path / "var_state"
    agentos_home = tmp_path / "opt_home"

    # 1. MUSE_STATE_DIR takes highest precedence
    monkeypatch.setenv("MUSE_STATE_DIR", str(custom_muse))
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(agentos_state))
    monkeypatch.setenv("AGENTOS_HOME", str(agentos_home))
    assert muse.state_root() == custom_muse

    # 2. AGENTOS_STATE_DIR resolves directly under state dir as state/muse
    monkeypatch.delenv("MUSE_STATE_DIR")
    assert muse.state_root() == agentos_state / "muse"

    # 3. AGENTOS_HOME resolves under home/state/muse
    monkeypatch.delenv("AGENTOS_STATE_DIR")
    assert muse.state_root() == agentos_home / "state" / "muse"

    # 4. Default fallback under ~/.agentos/state/muse
    monkeypatch.delenv("AGENTOS_HOME")
    assert muse.state_root() == Path.home() / ".agentos" / "state" / "muse"
