"""``senior-unilp-manager`` must reject a bare value-taking ``--flag`` (#1705).

``parse_args`` turns ``--slippage-bps`` with no value into ``True``; read back
with ``int(args.get(...) or DEFAULT)`` that became ``1`` and silently replaced
the default in a script that broadcasts transactions. The sibling
``poolsdotfun-token-launcher`` already routes its flags through ``opt_str`` /
``opt_int`` / ``opt_float``; this pins the older vendored copy to the same
contract, both at the helper level and through the real call sites.
"""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

import pytest

_SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "agentos"
    / "skills"
    / "bundled"
    / "senior-unilp-manager"
    / "scripts"
)


def _load(name: str):
    """Import a module from the skill's scripts dir without touching PATH."""
    entry = str(_SCRIPTS)
    added = entry not in sys.path
    if added:
        sys.path.insert(0, entry)
    try:
        return importlib.import_module(name)
    finally:
        if added:
            sys.path.remove(entry)


@pytest.fixture(scope="module")
def fmt():
    return _load("unilp.fmt")


@pytest.fixture(scope="module")
def lp_write():
    return _load("lp_write")


@pytest.fixture(scope="module")
def lp_read():
    return _load("lp_read")


# --- helpers ----------------------------------------------------------------


def test_parse_args_still_yields_true_for_a_bare_flag(fmt) -> None:
    """The sentinel the coercers exist to catch."""
    assert fmt.parse_args(["mint", "--slippage-bps"])["slippage-bps"] is True
    assert fmt.parse_args(["--slippage-bps", "--json"])["slippage-bps"] is True


@pytest.mark.parametrize("helper", ["opt_str", "opt_int", "opt_float"])
def test_bare_flag_is_an_error_naming_the_flag(fmt, helper: str) -> None:
    fn = getattr(fmt, helper)
    args = {"slippage-bps": True}
    call = (
        (lambda: fn(args, "slippage-bps"))
        if helper == "opt_str"
        else (lambda: fn(args, "slippage-bps", 50))
    )
    with pytest.raises(ValueError, match=r"--slippage-bps needs a value"):
        call()


def test_absent_flag_returns_the_default(fmt) -> None:
    assert fmt.opt_str({}, "owner") is None
    assert fmt.opt_int({}, "slippage-bps", 50) == 50
    assert fmt.opt_float({}, "gas-multiplier", 1.2) == 1.2


def test_given_flag_is_parsed(fmt) -> None:
    args = fmt.parse_args(["--slippage-bps", "75", "--gas-multiplier=1.5", "--owner", " 0xabc "])
    assert fmt.opt_int(args, "slippage-bps", 50) == 75
    assert fmt.opt_float(args, "gas-multiplier", 1.2) == 1.5
    assert fmt.opt_str(args, "owner") == "0xabc"


def test_non_numeric_value_is_an_error_naming_the_flag(fmt) -> None:
    with pytest.raises(ValueError, match=r"--slippage-bps must be a whole number"):
        fmt.opt_int({"slippage-bps": "lots"}, "slippage-bps", 50)
    with pytest.raises(ValueError, match=r"--gas-multiplier must be a number"):
        fmt.opt_float({"gas-multiplier": "fast"}, "gas-multiplier", 1.2)


def test_unilp_helpers_match_the_poolsfun_port(fmt) -> None:
    """Same vendored origin, same contract — keep the two copies from drifting."""
    # poolsfun lives in another skill dir; compare source text rather than importing
    # both packages into one interpreter.
    pools_src = (
        _SCRIPTS.parent.parent / "poolsdotfun-token-launcher" / "scripts" / "poolsfun" / "fmt.py"
    ).read_text(encoding="utf-8")
    unilp_src = (_SCRIPTS / "unilp" / "fmt.py").read_text(encoding="utf-8")
    for helper in ("def opt_str(", "def opt_int(", "def opt_float("):
        assert helper in unilp_src
        assert helper in pools_src


# --- real call sites ----------------------------------------------------------


def test_deadline_secs_bare_flag_no_longer_means_one_second(lp_write) -> None:
    assert lp_write.deadline_offset({}) == lp_write.DEFAULT_DEADLINE_SECS
    assert lp_write.deadline_offset({"deadline-secs": "600"}) == 600
    with pytest.raises(ValueError, match=r"--deadline-secs needs a value"):
        lp_write.deadline_offset({"deadline-secs": True})


def test_from_bare_flag_is_rejected_before_touching_the_environment(lp_write, monkeypatch) -> None:
    def _boom(*_a, **_k):  # pragma: no cover - must not be reached
        raise AssertionError("resolve_private_key was called")

    monkeypatch.setattr(lp_write, "resolve_private_key", _boom)
    with pytest.raises(ValueError, match=r"--from needs a value"):
        lp_write.resolve_signer({"from": True})
    with pytest.raises(ValueError, match=r"--signer-env needs a value"):
        lp_write.resolve_signer({"signer-env": True})


def test_liquidity_bare_flag_is_rejected(lp_write) -> None:
    with pytest.raises(ValueError, match=r"--liquidity needs a value"):
        lp_write.size_liquidity(0, 0, 0, {"liquidity": True}, {}, {})
    with pytest.raises(ValueError, match=r"--amount0 needs a value"):
        lp_write.size_liquidity(0, 0, 0, {"amount0": True}, {}, {})


def test_pool_key_from_args_rejects_a_bare_fee(lp_read) -> None:
    args = {
        "currency0": "0x" + "11" * 20,
        "currency1": "0x" + "22" * 20,
        "fee": True,
        "tick-spacing": "60",
    }
    with pytest.raises(ValueError, match=r"--fee needs a value"):
        lp_read.pool_key_from_args(args)


def test_pool_key_from_args_still_absent_when_nothing_given(lp_read) -> None:
    assert lp_read.pool_key_from_args({"_": []}) is None


# --- source guard --------------------------------------------------------------

# Every flag that is a genuine on/off switch. Anything else read through a raw
# ``args.get`` / ``args[...]`` is a value-taking flag that skipped the coercers.
_SWITCHES = {
    "_",
    "json",
    "help",
    "h",
    "broadcast",
    "confirm",
    "all",
    "all-pools",
    "alert-only",
    "allow-hooked",
    "allow-odd-tier",
    "from-current",
    "include-empty",
    "include-v3",
    "no-fees",
    "no-hook",
    "scan-logs",
}

_RAW_READ = re.compile(r"""(?<!\w)args(?:\.get\(|\[)\s*["']([^"']+)["']""")


@pytest.mark.parametrize("script", ["lp_write.py", "lp_read.py", "ratchet.py"])
def test_no_value_taking_flag_is_read_raw(script: str) -> None:
    source = (_SCRIPTS / script).read_text(encoding="utf-8")
    offenders = sorted({name for name in _RAW_READ.findall(source) if name not in _SWITCHES})
    assert offenders == [], f"{script} reads value-taking flags raw: {offenders}"
