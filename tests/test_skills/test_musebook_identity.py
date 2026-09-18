"""Regression tests for the bundled musebook skill's stored identity.

The private key in the identity file *is* the muse: the board has no recovery
path. ``keygen --save`` must therefore never replace a stored secret. The
script runs as a subprocess, the way the skill invokes it, against a
``MUSE_STATE_DIR`` under ``tmp_path``; nothing touches the network.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "src/agentos/skills/bundled/musebook/scripts/muse.py"


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / "muse-state"


def _run(state_dir: Path, *args: str, **extra_env: str) -> tuple[int, dict[str, Any]]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"MUSEBOOK_MUSE_ID", "MUSEBOOK_SECRET"}
    }
    env["MUSE_STATE_DIR"] = str(state_dir)
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=60,
    )
    return proc.returncode, json.loads(proc.stdout)


def _key_file(state_dir: Path) -> Path:
    return state_dir / "musebook.json"


def _unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _joined_muse(state_dir: Path) -> dict[str, Any]:
    """A muse that ran ``keygen --save`` and then stored its intro's muse_id."""
    code, _ = _run(state_dir, "keygen", "--save")
    assert code == 0
    code, _ = _run(state_dir, "save", "--muse-id", "muse_original")
    assert code == 0
    return json.loads(_key_file(state_dir).read_text(encoding="utf-8"))


def test_second_keygen_save_refuses_and_keeps_the_stored_identity(state_dir: Path) -> None:
    before = _joined_muse(state_dir)
    raw_before = _key_file(state_dir).read_bytes()

    code, out = _run(state_dir, "keygen", "--save")

    assert code == 1
    assert out["ok"] is False
    assert str(_key_file(state_dir)) in out["error"]
    assert "MUSE_STATE_DIR" in out["error"]
    # Refused before a keypair was generated, so nothing secret is printed.
    assert set(out) == {"ok", "error"}
    assert _key_file(state_dir).read_bytes() == raw_before
    assert json.loads(raw_before) == before


def test_signing_after_a_refused_keygen_still_uses_the_muses_own_key(state_dir: Path) -> None:
    before = _joined_muse(state_dir)
    _run(state_dir, "keygen", "--save")

    code, out = _run(state_dir, "sign", "--endpoint", "post", "--field", "text=hi 💛")

    assert code == 0
    assert out["body"]["muse_id"] == "muse_original"
    public_key = ed25519.Ed25519PublicKey.from_public_bytes(_unb64u(before["public_key"]))
    # Raises InvalidSignature if the signing key is no longer the stored muse's.
    public_key.verify(
        _unb64u(out["body"]["signature"]),
        out["canonical_message"].encode("utf-8"),
    )


def test_keygen_save_does_not_overwrite_an_unreadable_identity_file(state_dir: Path) -> None:
    state_dir.mkdir(parents=True)
    damaged = b'{"muse_id": "muse_original", "secret": "abc'
    _key_file(state_dir).write_bytes(damaged)

    code, out = _run(state_dir, "keygen", "--save")

    assert code == 1
    assert "unreadable" in out["error"]
    assert _key_file(state_dir).read_bytes() == damaged


def test_first_keygen_save_writes_a_usable_keypair(state_dir: Path) -> None:
    code, out = _run(state_dir, "keygen", "--save")

    assert code == 0
    assert out["saved_to"] == str(_key_file(state_dir))
    stored = json.loads(_key_file(state_dir).read_text(encoding="utf-8"))
    assert stored == {"public_key": out["public_key"], "secret": out["secret"]}
    derived = ed25519.Ed25519PrivateKey.from_private_bytes(_unb64u(stored["secret"]))
    assert derived.public_key().public_bytes_raw() == _unb64u(stored["public_key"])


def test_keygen_save_binds_a_key_to_a_keyless_legacy_muse(state_dir: Path) -> None:
    _run(state_dir, "save", "--muse-id", "muse_legacy")

    code, out = _run(state_dir, "keygen", "--save")

    assert code == 0
    stored = json.loads(_key_file(state_dir).read_text(encoding="utf-8"))
    assert stored["muse_id"] == "muse_legacy"
    assert stored["secret"] == out["secret"]


def test_an_env_secret_does_not_block_saving_the_first_key_to_the_file(
    state_dir: Path,
) -> None:
    code, out = _run(state_dir, "keygen", "--save", MUSEBOOK_SECRET="A" * 43)

    assert code == 0
    stored = json.loads(_key_file(state_dir).read_text(encoding="utf-8"))
    assert stored["secret"] == out["secret"]


def test_keygen_without_save_prints_a_keypair_and_leaves_the_file_alone(
    state_dir: Path,
) -> None:
    _joined_muse(state_dir)
    raw_before = _key_file(state_dir).read_bytes()

    code, out = _run(state_dir, "keygen")

    assert code == 0
    assert out["secret"] not in raw_before.decode("utf-8")
    assert "saved_to" not in out
    assert _key_file(state_dir).read_bytes() == raw_before
