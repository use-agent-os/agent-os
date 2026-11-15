"""A malformed skill signing key must never be printed back (WAL-21).

``senior-unilp-manager`` checked only the ``0x`` prefix and the 66-character
length of ``UNIV4_LP_PRIVATE_KEY``; a key with one mistyped character then
reached ``int(text, 16)``, whose ``ValueError`` embeds the offending string, and
the top-level handler printed ``str(exc)`` to stderr — the agent's tool output,
the transcript and, for the ratchet cron job, the job log. Sixty-three correct
nibbles out of 64 is a brute-forceable key.

This pins every entry point that resolves a signer, in both hot-wallet skills,
and the shared ``normalize_private_key`` helper the write path signs through.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

_BUNDLED = Path(__file__).resolve().parents[1] / "src" / "agentos" / "skills" / "bundled"
_UNILP = _BUNDLED / "senior-unilp-manager" / "scripts"
_POOLS = _BUNDLED / "poolsdotfun-token-launcher" / "scripts"

# Dummy values, never real keys: the right length, one non-hex character.
_NIBBLES = "5" * 63 + "g"
_BAD_KEYS = ["0x" + _NIBBLES, _NIBBLES, "0x" + "a" * 62 + "_1", "0x" + " " + "7" * 63]


def _load(scripts: Path, name: str):
    entry = str(scripts)
    added = entry not in sys.path
    if added:
        sys.path.insert(0, entry)
    try:
        return importlib.import_module(name)
    finally:
        if added:
            sys.path.remove(entry)


def _leaks(text: str, key: str) -> bool:
    body = key[2:] if key.startswith("0x") else key
    # 16 consecutive nibbles is already far more than any honest message carries.
    return any(body[i : i + 16] in text for i in range(0, len(body) - 15))


def _run(script: Path, argv: list[str], env_name: str, key: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.endswith("_PRIVATE_KEY")}
    env[env_name] = key
    # Point the dotenv loader at nothing so a developer's real ~/.agentos/.env is never read.
    env["UNILP_ENV"] = str(Path(os.devnull))
    env["POOLSFUN_ENV"] = str(Path(os.devnull))
    env["HOME"] = env["USERPROFILE"] = str(Path(env.get("AGENTOS_STATE_DIR") or script.parent))
    return subprocess.run(
        [sys.executable, str(script), *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=str(script.parent),
        timeout=60,
    )


@pytest.mark.parametrize("key", _BAD_KEYS)
@pytest.mark.parametrize(
    ("script", "argv"),
    [
        (_UNILP / "lp_write.py", ["address"]),
        (_UNILP / "lp_write.py", ["address", "--json"]),
        (_UNILP / "lp_read.py", ["positions", "--rpc", "http://127.0.0.1:9"]),
        (_UNILP / "ratchet.py", ["list", "--rpc", "http://127.0.0.1:9"]),
    ],
    ids=["lp_write-address", "lp_write-address-json", "lp_read-positions", "ratchet-list"],
)
def test_unilp_entry_points_never_print_a_malformed_key(script, argv, key) -> None:
    result = _run(script, argv, "UNIV4_LP_PRIVATE_KEY", key)
    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert not _leaks(output, key), output
    assert "UNIV4_LP_PRIVATE_KEY" in output


def test_unilp_custom_signer_env_never_prints_its_value() -> None:
    value = "a" * 63 + "z"
    result = _run(
        _UNILP / "lp_write.py", ["address", "--signer-env", "OTHER_SECRET"], "OTHER_SECRET", value
    )
    assert result.returncode != 0
    assert not _leaks(result.stdout + result.stderr, value)


@pytest.mark.parametrize(
    ("scripts", "module", "env_name"),
    [(_UNILP, "unilp", "UNIV4_LP_PRIVATE_KEY"), (_POOLS, "poolsfun", "POOLSFUN_PRIVATE_KEY")],
)
@pytest.mark.parametrize("key", _BAD_KEYS)
def test_resolve_signer_address_refuses_without_echo(
    scripts, module, env_name, key, monkeypatch
) -> None:
    chains = _load(scripts, f"{module}.chains")
    monkeypatch.setattr(chains, "_env_loaded", True)
    monkeypatch.setenv(env_name, key)
    with pytest.raises(RuntimeError) as caught:
        chains.resolve_signer_address(env_name)
    assert env_name in str(caught.value)
    assert not _leaks(str(caught.value), key)


@pytest.mark.parametrize(("scripts", "module"), [(_UNILP, "unilp"), (_POOLS, "poolsfun")])
@pytest.mark.parametrize("key", _BAD_KEYS)
def test_normalize_private_key_error_carries_no_key_material(scripts, module, key) -> None:
    """The signing path calls this directly, bypassing ``resolve_private_key``."""
    account = _load(scripts, f"{module}.account")
    secp = _load(scripts, f"{module}.secp256k1")
    for call in (
        lambda: account.normalize_private_key(key),
        lambda: account.account_from_private_key(key),
        lambda: secp.sign_digest(key, b"\x01" * 32),
    ):
        with pytest.raises(ValueError) as caught:
            call()
        assert not _leaks(str(caught.value), key), str(caught.value)


@pytest.mark.parametrize(("scripts", "module"), [(_UNILP, "unilp"), (_POOLS, "poolsfun")])
def test_a_valid_dummy_key_still_resolves(scripts, module) -> None:
    account = _load(scripts, f"{module}.account")
    key = "0x" + "5" * 64
    address = account.account_from_private_key(key)["address"]
    assert address.startswith("0x") and len(address) == 42
    assert account.normalize_private_key(key.upper().replace("0X", "0x")) == int("5" * 64, 16)
