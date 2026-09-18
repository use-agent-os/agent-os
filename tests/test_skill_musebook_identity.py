"""Issue #2674: the musebook identity file -- a private key -- was written carelessly.

``save_identity`` used ``path.write_text`` and then ``os.chmod(path, 0o600)``:

1. the file was created at its final path with the umask's mode (``0644``,
   world-readable) and narrowed only afterwards;
2. an interruption mid-write left a truncated or empty file where the identity
   had been, with no recovery -- the private key *is* the muse's name;
3. the state directory was created with the umask's mode;
4. a corrupt identity file was read as ``{}``, so ``post --save-identity``
   (which passes only ``muse_id``) wrote it back without its ``secret``;
5. ``save --secret`` persisted anything at all.

The write now goes through a ``0600`` temp file in the same directory, is
flushed to disk, and is renamed over the target; the directory is ``0700``; a
corrupt file is an error; and a secret is validated before it is written.

The issue's remaining point -- that ``AGENTOS_STATE_DIR`` should not get
``state/`` appended -- is not taken: the variable is the AgentOS *home*
everywhere else in the tree (``agentos.paths``, the gateway's ``state_dir``
default, cron-watchers), and the tests below pin muse to that.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = (
    Path(__file__).resolve().parent.parent / "src/agentos/skills/bundled/musebook/scripts/muse.py"
)
POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="Windows honours only the read-only bit")

# a valid 32-byte seed, as unpadded base64url
GOOD_SECRET = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


@pytest.fixture(scope="module")
def muse() -> ModuleType:
    spec = importlib.util.spec_from_file_location("muse_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["muse_under_test"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "muse"
    monkeypatch.setenv("MUSE_STATE_DIR", str(target))
    for var in ("AGENTOS_STATE_DIR", "AGENTOS_HOME", "MUSEBOOK_MUSE_ID", "MUSEBOOK_SECRET"):
        monkeypatch.delenv(var, raising=False)
    return target


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _stray_temp_files(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name.startswith(".musebook.json."))


# ── 1. private from inception ───────────────────────────────────────────────


@POSIX_ONLY
def test_the_file_is_0600_before_it_reaches_its_final_path(
    muse: ModuleType, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of the temp-file write: the mode is right at the moment the
    bytes land, not after a window in which the key sat world-readable."""
    seen: list[int] = []
    real_replace = os.replace

    def replace(src: str, dst: str) -> None:
        seen.append(_mode(Path(src)))
        real_replace(src, dst)

    monkeypatch.setattr(muse.os, "replace", replace)

    muse.save_identity(muse_id="m", secret=GOOD_SECRET)

    assert seen == [0o600]


@POSIX_ONLY
def test_the_final_file_is_0600(muse: ModuleType, state_dir: Path) -> None:
    path = muse.save_identity(muse_id="m")

    assert _mode(path) == 0o600


@POSIX_ONLY
def test_the_state_directory_is_0700(muse: ModuleType, state_dir: Path) -> None:
    muse.save_identity(muse_id="m")

    assert _mode(state_dir) == 0o700


@POSIX_ONLY
def test_an_existing_looser_directory_is_tightened(muse: ModuleType, state_dir: Path) -> None:
    state_dir.mkdir(parents=True)
    state_dir.chmod(0o755)

    muse.save_identity(muse_id="m")

    assert _mode(state_dir) == 0o700


@POSIX_ONLY
def test_an_existing_looser_file_is_tightened(muse: ModuleType, state_dir: Path) -> None:
    state_dir.mkdir(parents=True)
    path = state_dir / "musebook.json"
    path.write_text('{"muse_id": "old"}\n', encoding="utf-8")
    path.chmod(0o644)

    muse.save_identity(secret=GOOD_SECRET)

    assert _mode(path) == 0o600


@POSIX_ONLY
def test_the_umask_does_not_leak_into_either_mode(
    muse: ModuleType, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = os.umask(0o000)
    try:
        path = muse.save_identity(muse_id="m")
    finally:
        os.umask(previous)

    assert _mode(path) == 0o600
    assert _mode(state_dir) == 0o700


# ── 2. all or nothing ───────────────────────────────────────────────────────


def test_a_failed_rename_leaves_the_previous_identity_untouched(
    muse: ModuleType, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir.mkdir(parents=True)
    path = state_dir / "musebook.json"
    before = '{\n  "muse_id": "keep",\n  "secret": "' + GOOD_SECRET + '"\n}\n'
    path.write_text(before, encoding="utf-8")

    def boom(src: str, dst: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(muse.os, "replace", boom)

    with pytest.raises(OSError, match="disk full"):
        muse.save_identity(muse_id="new")

    assert path.read_text(encoding="utf-8") == before
    assert _stray_temp_files(state_dir) == []


def test_a_failed_write_leaves_no_temp_file_behind(
    muse: ModuleType, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Handle:
        def __init__(self, fd: int) -> None:
            os.close(fd)

        def __enter__(self) -> Handle:
            return self

        def __exit__(self, *_exc: Any) -> None:
            return None

        def write(self, _text: str) -> int:
            raise OSError("write failed")

    monkeypatch.setattr(muse.os, "fdopen", lambda fd, *a, **k: Handle(fd))

    with pytest.raises(OSError, match="write failed"):
        muse.save_identity(muse_id="m")

    assert _stray_temp_files(state_dir) == []


def test_an_interrupt_mid_write_leaves_no_second_copy_of_the_key(
    muse: ModuleType, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``KeyboardInterrupt`` is a ``BaseException``. An ``except Exception``
    cleanup would leave the 0600 temp file -- a full copy of the private key
    -- lying beside the real one."""

    def interrupted(src: str, dst: str) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(muse.os, "replace", interrupted)

    with pytest.raises(KeyboardInterrupt):
        muse.save_identity(muse_id="m", secret=GOOD_SECRET)

    assert _stray_temp_files(state_dir) == []


def test_the_content_is_flushed_to_disk_before_the_rename(
    muse: ModuleType, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Power loss is in the issue's list; a rename can be durable before the
    data it points at unless the data is synced first."""
    order: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(muse.os, "fsync", lambda fd: order.append("fsync") or real_fsync(fd))
    monkeypatch.setattr(
        muse.os, "replace", lambda s, d: order.append("replace") or real_replace(s, d)
    )

    muse.save_identity(muse_id="m")

    assert order.index("fsync") < order.index("replace")


def test_a_successful_save_leaves_no_temp_file(muse: ModuleType, state_dir: Path) -> None:
    muse.save_identity(muse_id="m")
    muse.save_identity(secret=GOOD_SECRET)

    assert _stray_temp_files(state_dir) == []
    assert sorted(p.name for p in state_dir.iterdir()) == ["musebook.json"]


def test_the_temp_file_is_created_in_the_target_directory(
    muse: ModuleType, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rename is only atomic within one filesystem."""
    seen: list[Path] = []
    real_replace = os.replace

    def replace(src: str, dst: str) -> None:
        seen.append(Path(src).parent)
        real_replace(src, dst)

    monkeypatch.setattr(muse.os, "replace", replace)

    muse.save_identity(muse_id="m")

    assert seen == [state_dir]


# ── 4. a corrupt identity is an error, not an empty start ───────────────────


@pytest.mark.parametrize(
    "content",
    ["{not json", "", "   ", '["a", "list"]', '"a string"', "42", "null"],
)
def test_a_corrupt_or_non_object_identity_refuses_to_save(
    muse: ModuleType, state_dir: Path, content: str
) -> None:
    state_dir.mkdir(parents=True)
    path = state_dir / "musebook.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(SystemExit, match=str(path).replace("\\", "\\\\")):
        muse.save_identity(muse_id="m")

    assert path.read_text(encoding="utf-8") == content, "nothing was written over it"


def test_the_issues_clobber_scenario_no_longer_loses_the_secret(
    muse: ModuleType, state_dir: Path
) -> None:
    """``post --save-identity`` passes only ``muse_id``. Against a corrupt file
    that used to merge into ``{}`` and write the identity back secretless."""
    state_dir.mkdir(parents=True)
    path = state_dir / "musebook.json"
    path.write_text(
        '{"muse_id": "m", "secret": "' + GOOD_SECRET + '"', encoding="utf-8"
    )  # truncated

    with pytest.raises(SystemExit):
        muse.save_identity(muse_id="m")

    assert GOOD_SECRET in path.read_text(encoding="utf-8")


@POSIX_ONLY
def test_an_unreadable_identity_refuses_to_save(muse: ModuleType, state_dir: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root can read anything")
    state_dir.mkdir(parents=True)
    path = state_dir / "musebook.json"
    path.write_text('{"muse_id": "m"}', encoding="utf-8")
    path.chmod(0o000)
    try:
        with pytest.raises(SystemExit, match="unreadable"):
            muse.save_identity(secret=GOOD_SECRET)
    finally:
        path.chmod(0o600)


# ── the merge semantics are unchanged ───────────────────────────────────────


def test_fields_merge_into_an_existing_identity(muse: ModuleType, state_dir: Path) -> None:
    muse.save_identity(muse_id="m", secret=GOOD_SECRET)
    path = muse.save_identity(public_key="pk")

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "muse_id": "m",
        "secret": GOOD_SECRET,
        "public_key": "pk",
    }


def test_a_new_value_replaces_the_old_and_an_empty_one_is_ignored(
    muse: ModuleType, state_dir: Path
) -> None:
    muse.save_identity(muse_id="old", secret=GOOD_SECRET)
    path = muse.save_identity(muse_id="new", secret="")

    assert json.loads(path.read_text(encoding="utf-8")) == {"muse_id": "new", "secret": GOOD_SECRET}


def test_the_file_is_sorted_indented_and_newline_terminated(
    muse: ModuleType, state_dir: Path
) -> None:
    path = muse.save_identity(secret="s", muse_id="m")

    assert path.read_text(encoding="utf-8") == '{\n  "muse_id": "m",\n  "secret": "s"\n}\n'


def test_a_saved_identity_loads_back(muse: ModuleType, state_dir: Path) -> None:
    muse.save_identity(muse_id="m", secret=GOOD_SECRET)

    assert muse.load_identity() == {"muse_id": "m", "secret": GOOD_SECRET}


def test_parent_directories_are_created(
    muse: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deep = tmp_path / "a" / "b" / "muse"
    monkeypatch.setenv("MUSE_STATE_DIR", str(deep))

    path = muse.save_identity(muse_id="m")

    assert path == deep / "musebook.json"
    assert path.is_file()


# ── 5. the state root ───────────────────────────────────────────────────────


def _watermark_module() -> ModuleType:
    """The cron-watchers helper, loaded from its own file like ``muse`` is."""
    script = SCRIPT.parent.parent.parent / "cron-watchers" / "scripts" / "_watermark.py"
    spec = importlib.util.spec_from_file_location("watermark_under_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_agentos_state_dir_is_the_agentos_home(
    muse: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``AGENTOS_STATE_DIR`` replaces ``~/.agentos``; runtime state is its
    ``state`` subdirectory, exactly as ``agentos.paths.state_dir`` resolves it.
    (Issue #2674's fifth point reads the variable as the ``state`` directory
    itself; the rest of the tree does not, and muse must not diverge.)"""
    from agentos import paths

    monkeypatch.delenv("MUSE_STATE_DIR", raising=False)
    monkeypatch.delenv("AGENTOS_HOME", raising=False)
    monkeypatch.setenv("AGENTOS_STATE_DIR", "/srv/agentos")

    assert muse.state_root() == paths.state_dir("muse")
    assert muse.state_root().as_posix() == "/srv/agentos/state/muse"


def test_agentos_home_resolves_the_same_way(
    muse: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MUSE_STATE_DIR", raising=False)
    monkeypatch.delenv("AGENTOS_STATE_DIR", raising=False)
    monkeypatch.setenv("AGENTOS_HOME", "/srv/agentos")

    assert muse.state_root().as_posix() == "/srv/agentos/state/muse"


def test_agentos_state_dir_wins_over_agentos_home(
    muse: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MUSE_STATE_DIR", raising=False)
    monkeypatch.setenv("AGENTOS_STATE_DIR", "/explicit")
    monkeypatch.setenv("AGENTOS_HOME", "/home/other")

    assert muse.state_root().as_posix() == "/explicit/state/muse"


def test_muse_state_dir_wins_over_both(muse: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUSE_STATE_DIR", "/mine")
    monkeypatch.setenv("AGENTOS_STATE_DIR", "/explicit")
    monkeypatch.setenv("AGENTOS_HOME", "/home/other")

    assert muse.state_root().as_posix() == "/mine"


def test_the_default_is_under_the_home_directory(
    muse: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentos import paths

    for var in ("MUSE_STATE_DIR", "AGENTOS_STATE_DIR", "AGENTOS_HOME"):
        monkeypatch.delenv(var, raising=False)

    assert muse.state_root() == paths.state_dir("muse")
    assert muse.state_root().name == "muse"
    assert muse.state_root().parent.name == "state"


def test_the_state_root_agrees_with_cron_watchers(
    muse: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both skills keep state as siblings under the same ``state`` directory;
    asserted against the real ``_watermark`` module, not a re-statement."""
    watermark = _watermark_module()
    monkeypatch.delenv("MUSE_STATE_DIR", raising=False)
    monkeypatch.delenv("AGENTOS_HOME", raising=False)
    monkeypatch.setenv("AGENTOS_STATE_DIR", "/srv/agentos")

    assert muse.state_root().parent == watermark._state_root().parent
    assert muse.state_root().parent.as_posix() == "/srv/agentos/state"


# ── 6. cmd_save validates the secret ────────────────────────────────────────


def _args(**kwargs: Any) -> Any:
    import argparse

    defaults: dict[str, Any] = {"muse_id": None, "secret": None, "public_key": None}
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


@pytest.mark.parametrize(
    "secret",
    [
        "short",  # too few bytes
        GOOD_SECRET + "AAAA",  # too many
        "not base64url!!",  # not decodable
        "AAAA",  # 3 bytes
    ],
)
def test_an_invalid_secret_is_refused_before_anything_is_written(
    muse: ModuleType, state_dir: Path, secret: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        muse.cmd_save(_args(secret=secret))

    assert not state_dir.exists() or not (state_dir / "musebook.json").exists()


def test_an_invalid_secret_does_not_overwrite_a_good_one(muse: ModuleType, state_dir: Path) -> None:
    muse.save_identity(muse_id="m", secret=GOOD_SECRET)

    with pytest.raises(SystemExit):
        muse.cmd_save(_args(secret="short"))

    assert muse.load_identity()["secret"] == GOOD_SECRET


def test_a_valid_secret_is_saved(
    muse: ModuleType, state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert muse.cmd_save(_args(secret=GOOD_SECRET, muse_id="m")) == 0

    assert muse.load_identity() == {"muse_id": "m", "secret": GOOD_SECRET}
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_saving_only_a_muse_id_does_not_require_a_secret(
    muse: ModuleType, state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert muse.cmd_save(_args(muse_id="m")) == 0

    assert muse.load_identity() == {"muse_id": "m"}


def test_saving_nothing_is_still_refused(muse: ModuleType, state_dir: Path) -> None:
    with pytest.raises(SystemExit, match="at least one of"):
        muse.cmd_save(_args())
