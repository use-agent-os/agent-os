"""The musedin skill signs with the user's musebook ed25519 identity.

``scripts/musedin.py`` reuses the keypair the ``musebook`` skill stores, so it
must sign exactly the bytes MusedIn verifies, take its credentials in a fixed
order, and leave that identity alone:

1. the canonical message matches muse.txt section 2 (UTF-8 byte lengths, keys
   sorted, the envelope and ``x402_payment`` left out);
2. credentials resolve flag, then env, then the stored file;
3. the script never writes ``musebook.json`` and never prints the secret.
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

SCRIPT = (
    Path(__file__).resolve().parent.parent / "src/agentos/skills/bundled/musedin/scripts/musedin.py"
)

# RFC 8032 section 7.1, test 1: a seed with a known public key.
SEED_HEX = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
PUBLIC_HEX = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


SECRET = _b64u(bytes.fromhex(SEED_HEX))
OTHER_SECRET = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


@pytest.fixture(scope="module")
def musedin() -> ModuleType:
    spec = importlib.util.spec_from_file_location("musedin_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["musedin_under_test"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "muse"
    monkeypatch.setenv("MUSE_STATE_DIR", str(target))
    for var in ("AGENTOS_STATE_DIR", "AGENTOS_HOME", "MUSEBOOK_MUSE_ID", "MUSEBOOK_SECRET"):
        monkeypatch.delenv(var, raising=False)
    return target


def _store(state_dir: Path, **identity: str) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "musebook.json"
    path.write_text(json.dumps(identity) + "\n", encoding="utf-8")
    return path


def _args(**flags: Any) -> argparse.Namespace:
    return argparse.Namespace(muse_id=flags.get("muse_id"), secret=flags.get("secret"), timeout=5)


# ── 1. the canonical message ────────────────────────────────────────────────

# Built with MusedIn's own reference (lib/sign.js canonicalMessage) for the
# same fields: parent_id sorts before text, "héllo 👋\nline" is 16 UTF-8 bytes
# (é is 2, the emoji 4) though only 12 characters, and neither the envelope
# nor x402_payment appears.
VECTOR = (
    "musedin-v1\npost\n1700000000000\nabcdefghijklmnop\nmuse_abc\n"
    "parent_id:2:42\ntext:16:héllo 👋\nline"
)


def test_the_canonical_message_matches_the_reference_vector(musedin: ModuleType) -> None:
    fields = {
        "text": "héllo 👋\nline",
        "parent_id": "42",
        "x402_payment": "proof",
        "signature": "x",
    }

    message = musedin.canonical_message(
        "post", "1700000000000", "abcdefghijklmnop", "muse_abc", fields
    )

    assert message == VECTOR


def test_lengths_are_utf8_bytes_not_characters(musedin: ModuleType) -> None:
    message = musedin.canonical_message("post", "1", "n" * 16, "m", {"text": "é"})

    assert message.endswith("\ntext:2:é")


def test_no_fields_means_no_trailing_newline(musedin: ModuleType) -> None:
    message = musedin.canonical_message("whoami", "1", "n" * 16, "m", {})

    assert message == "musedin-v1\nwhoami\n1\n" + "n" * 16 + "\nm"


def test_the_signature_verifies_and_x402_payment_stays_unsigned(musedin: ModuleType) -> None:
    message, body = musedin.sign_fields(
        "post", "muse_abc", SECRET, {"text": "hi", "x402_payment": "proof"}
    )

    assert "x402_payment" not in message
    assert "x402_payment" not in body
    public = ed25519.Ed25519PublicKey.from_public_bytes(bytes.fromhex(PUBLIC_HEX))
    signature = base64.urlsafe_b64decode(body["signature"] + "=" * (-len(body["signature"]) % 4))
    assert len(signature) == 64
    public.verify(signature, message.encode("utf-8"))
    assert 16 <= len(body["nonce"]) <= 128


# ── 2. credential resolution: flag, then env, then file ─────────────────────


def test_the_stored_file_is_used_when_nothing_else_is_set(
    musedin: ModuleType, state_dir: Path
) -> None:
    _store(state_dir, muse_id="from_file", secret=SECRET)

    assert musedin.resolve_credentials(_args()) == ("from_file", SECRET)


def test_env_overrides_the_file(
    musedin: ModuleType, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store(state_dir, muse_id="from_file", secret=SECRET)
    monkeypatch.setenv("MUSEBOOK_MUSE_ID", "from_env")
    monkeypatch.setenv("MUSEBOOK_SECRET", OTHER_SECRET)

    assert musedin.resolve_credentials(_args()) == ("from_env", OTHER_SECRET)


def test_flags_override_env_and_file(
    musedin: ModuleType, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store(state_dir, muse_id="from_file", secret=SECRET)
    monkeypatch.setenv("MUSEBOOK_MUSE_ID", "from_env")
    monkeypatch.setenv("MUSEBOOK_SECRET", OTHER_SECRET)

    resolved = musedin.resolve_credentials(_args(muse_id="from_flag", secret=SECRET))

    assert resolved == ("from_flag", SECRET)


def test_each_field_falls_through_on_its_own(
    musedin: ModuleType, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store(state_dir, muse_id="from_file", secret=SECRET)
    monkeypatch.setenv("MUSEBOOK_SECRET", OTHER_SECRET)

    assert musedin.resolve_credentials(_args(muse_id="from_flag")) == ("from_flag", OTHER_SECRET)


# ── 3. read-only identity, secret never printed ─────────────────────────────


def _snapshot(directory: Path) -> dict[str, bytes]:
    if not directory.exists():
        return {}
    return {p.name: p.read_bytes() for p in directory.iterdir()}


@pytest.mark.parametrize(
    "argv",
    [
        ["sign", "--endpoint", "post", "--field", "text=hi"],
        ["whoami"],
        ["post", "--text", "hi"],
        ["join", "--headline", "builds things"],
        ["call", "--endpoint", "react", "--field", "post_id=1"],
    ],
)
def test_commands_never_write_the_identity_or_print_the_secret(
    musedin: ModuleType,
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    path = _store(state_dir, muse_id="muse_abc", secret=SECRET)
    before = _snapshot(state_dir)
    mtime = path.stat().st_mtime_ns
    # A refusal is the path that echoes the most back (the canonical message).
    monkeypatch.setattr(
        musedin, "request", lambda *a, **k: {"ok": False, "status": 401, "error": "bad"}
    )

    musedin.main(argv)

    assert _snapshot(state_dir) == before
    assert path.stat().st_mtime_ns == mtime
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err
    assert SEED_HEX not in out.out + out.err


def test_no_identity_refuses_without_creating_anything(
    musedin: ModuleType,
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def no_network(*a: Any, **k: Any) -> dict[str, Any]:
        raise AssertionError("must not send a request without an identity")

    monkeypatch.setattr(musedin, "request", no_network)

    assert musedin.main(["whoami"]) == 1

    assert not state_dir.exists()
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_a_secret_passed_by_flag_is_not_echoed(
    musedin: ModuleType,
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(musedin, "request", lambda *a, **k: {"ok": False, "status": 401})

    musedin.main(["whoami", "--muse-id", "muse_abc", "--secret", SECRET])

    assert SECRET not in capsys.readouterr().out
    assert not state_dir.exists()
