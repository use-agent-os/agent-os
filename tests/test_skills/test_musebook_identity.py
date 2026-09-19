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

    # 2. AGENTOS_STATE_DIR overrides the AgentOS *home* (agentos.paths's
    #    default_agentos_home()), not the state directory itself -- every
    #    subsystem's runtime state lives one "state" segment below home
    #    (agentos.paths.state_dir()), and this skill matches that rather
    #    than being the one place in the tree state/muse isn't state/muse.
    monkeypatch.delenv("MUSE_STATE_DIR")
    assert muse.state_root() == agentos_state / "state" / "muse"

    # 3. AGENTOS_HOME resolves under home/state/muse the same way
    monkeypatch.delenv("AGENTOS_STATE_DIR")
    assert muse.state_root() == agentos_home / "state" / "muse"

    # 4. Default fallback under ~/.agentos/state/muse
    monkeypatch.delenv("AGENTOS_HOME")
    assert muse.state_root() == Path.home() / ".agentos" / "state" / "muse"


def test_save_identity_fsyncs_before_the_rename(
    muse, muse_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write must be durable, not just atomic.

    os.replace makes the rename atomic, but a crash right after it can still
    leave an empty key: the directory entry can reach disk before the temp
    file's own content does, if nothing forces that content out of the page
    cache first. fsync on the file, before the rename, closes that window.
    """
    calls: list[str] = []
    real_fsync = os.fsync

    def _tracking_fsync(fd: int) -> None:
        calls.append("fsync")
        real_fsync(fd)

    real_replace = os.replace

    def _tracking_replace(src, dst):
        calls.append("replace")
        real_replace(src, dst)

    monkeypatch.setattr(muse.os, "fsync", _tracking_fsync)
    monkeypatch.setattr(muse.os, "replace", _tracking_replace)

    pub, sec = muse.generate_keypair()
    muse.save_identity(public_key=pub, secret=sec)

    assert "fsync" in calls
    assert calls.index("fsync") < calls.index("replace")


@pytest.mark.skipif(os.name != "posix", reason="no directory file descriptors on Windows")
def test_save_identity_fsyncs_the_directory_after_the_rename(
    muse, muse_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rename itself must be durable too, or the whole write can vanish."""
    opened_dirs: list[str] = []
    real_open = os.open

    def _tracking_open(path, flags, *a, **kw):
        fd = real_open(path, flags, *a, **kw)
        if flags == os.O_RDONLY:
            opened_dirs.append(str(path))
        return fd

    monkeypatch.setattr(muse.os, "open", _tracking_open)

    pub, sec = muse.generate_keypair()
    muse.save_identity(public_key=pub, secret=sec)

    assert str(muse_state) in opened_dirs


def test_save_identity_cleans_up_the_temp_file_on_keyboard_interrupt(
    muse, muse_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Ctrl-C between mkstemp and os.replace must not leave a second,
    full, 0600 copy of the private key sitting beside the real file.

    KeyboardInterrupt is a BaseException, not an Exception -- an
    ``except Exception`` cleanup handler never sees it.
    """

    def _raise_keyboard_interrupt(src, dst):
        raise KeyboardInterrupt

    monkeypatch.setattr(muse.os, "replace", _raise_keyboard_interrupt)

    pub, sec = muse.generate_keypair()
    with pytest.raises(KeyboardInterrupt):
        muse.save_identity(public_key=pub, secret=sec)

    leftovers = list(muse_state.glob(".musebook.json.*"))
    assert leftovers == [], f"temp file(s) leaked a copy of the secret: {leftovers}"
