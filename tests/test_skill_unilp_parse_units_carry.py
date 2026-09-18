"""A rounded fraction digit that carries must not add a whole token unit.

``parse_units`` ports viem's ``parseUnits``, which rounds excess fraction digits
half-up. When the rounded digit carries out of its column viem writes
``${BigInt(left) + 1n}0``.padStart(left.length + 1, "0") — the trailing zero is
part of what gets padded. The port padded the digits alone and appended the zero
afterwards, so the fraction came out one character too long whenever the carry
did not widen ``left``; the length check below it read that as a carry into the
integer part and added 10**decimals to the amount.

``166.5205930005705566699`` at 18 decimals therefore parsed as 167.52… ETH, and
``0.0095`` at 3 decimals as 1.010 instead of 0.010. Both vendored copies of the
module (``senior-unilp-manager`` and ``poolsdotfun-token-launcher``) carry the
same code and both are exercised here; amounts reach them straight from CLI
flags on scripts that broadcast transactions.
"""

from __future__ import annotations

import importlib
import json
import random
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest

_BUNDLED = Path(__file__).resolve().parents[1] / "src" / "agentos" / "skills" / "bundled"
_UNILP_SCRIPTS = _BUNDLED / "senior-unilp-manager" / "scripts"
_POOLS_SCRIPTS = _BUNDLED / "poolsdotfun-token-launcher" / "scripts"


def _load(scripts: Path, name: str):
    """Import a module from a skill's scripts dir without touching PATH."""
    entry = str(scripts)
    added = entry not in sys.path
    if added:
        sys.path.insert(0, entry)
    try:
        return importlib.import_module(name)
    finally:
        if added:
            sys.path.remove(entry)


@pytest.fixture(scope="module")
def hexutil():
    return _load(_UNILP_SCRIPTS, "unilp.hexutil")


@pytest.fixture(scope="module")
def pools_hexutil():
    return _load(_POOLS_SCRIPTS, "poolsfun.hexutil")


def _half_up(value: str, decimals: int) -> int:
    """What the amount is worth, decided exactly instead of in binary floats."""
    return int(Decimal(value).scaleb(decimals).quantize(Decimal(1), rounding=ROUND_HALF_UP))


# value, decimals, expected base units
_CARRY_CASES = [
    ("0.0095", 3, 10),  # left "00" -> carry stays in the fraction
    ("0.00096", 4, 10),
    ("0.0999", 3, 100),  # left "09" -> widens by one digit, still no integer carry
    ("0.0000000000000000095", 18, 10),  # 9.5 wei of an 18-decimal token
    ("4.0962", 2, 410),
    ("166.5205930005705566699", 18, 166520593000570556670),
    ("9.095", 2, 910),
]


@pytest.mark.parametrize(("value", "decimals", "expected"), _CARRY_CASES)
def test_a_carrying_digit_stays_inside_the_fraction(
    hexutil, value: str, decimals: int, expected: int
) -> None:
    assert hexutil.parse_units(value, decimals) == expected


@pytest.mark.parametrize(("value", "decimals", "expected"), _CARRY_CASES)
def test_the_launcher_copy_rounds_the_same_way(
    pools_hexutil, value: str, decimals: int, expected: int
) -> None:
    """The two vendored copies are the same file; neither may drift."""
    assert pools_hexutil.parse_units(value, decimals) == expected


@pytest.mark.parametrize(("value", "decimals", "expected"), _CARRY_CASES)
def test_the_rounded_amount_is_the_exact_half_up_amount(
    hexutil, value: str, decimals: int, expected: int
) -> None:
    """Pin the cases to decimal arithmetic, not just to each other."""
    assert expected == _half_up(value, decimals)
    assert hexutil.parse_units(value, decimals) == _half_up(value, decimals)


def test_a_carry_that_fills_every_fraction_digit_still_reaches_the_integer(hexutil) -> None:
    """The opposite direction: the integer carry this fix removes must survive
    where it is genuinely owed."""
    assert hexutil.parse_units("0.9995", 3) == 1000
    assert hexutil.parse_units("1.9999999999999999999", 18) == 2 * 10**18
    assert hexutil.parse_units("0.99999999999999999999", 18) == 10**18


def test_a_carry_with_no_digits_to_its_left_reaches_the_integer(hexutil) -> None:
    """``decimals=1`` leaves ``left`` empty; 0.95 is one tenth, not eleven."""
    assert hexutil.parse_units("0.95", 1) == 10
    assert hexutil.parse_units("0.05", 1) == 1


def test_rounding_without_a_carry_is_untouched(hexutil) -> None:
    assert hexutil.parse_units("12.3456", 2) == 1235
    assert hexutil.parse_units("1234.56789", 6) == 1234567890
    assert hexutil.parse_units("0.0000001", 6) == 0


def test_a_negative_amount_keeps_its_sign_through_the_carry(hexutil) -> None:
    assert hexutil.parse_units("-0.0095", 3) == -10
    assert hexutil.parse_units("-0.9995", 3) == -1000


def test_zero_decimals_still_folds_the_fraction_into_the_integer(hexutil) -> None:
    assert hexutil.parse_units("1.5", 0) == 2
    assert hexutil.parse_units("1.49", 0) == 1


def _golden_vectors() -> list[tuple[str, int, str]]:
    raw = json.loads((_UNILP_SCRIPTS / "golden_vectors.json").read_text())
    return [(c["value"], c["decimals"], c["expected"]) for c in raw["parse_units"]]


@pytest.mark.parametrize(("value", "decimals", "expected"), _golden_vectors())
def test_the_harvested_viem_vectors_still_hold(
    hexutil, value: str, decimals: int, expected: str
) -> None:
    """The vectors harvested from real viem; they never covered this carry."""
    assert str(hexutil.parse_units(value, decimals)) == expected


def test_a_deterministic_sweep_matches_exact_half_up_rounding(hexutil) -> None:
    """Seeded, offline, no network: 2000 amounts across the decimals in use."""
    rng = random.Random(7)
    mismatches: list[tuple[str, int, int, int]] = []
    for _ in range(2000):
        decimals = rng.choice([0, 1, 2, 3, 6, 8, 18])
        integer = str(rng.randint(0, 10 ** rng.randint(1, 3) - 1))
        fraction = "".join(rng.choice("0000123456789") for _ in range(rng.randint(0, decimals + 3)))
        value = f"{integer}.{fraction}" if fraction else integer
        got = hexutil.parse_units(value, decimals)
        want = _half_up(value, decimals)
        if got != want:
            mismatches.append((value, decimals, got, want))
    assert mismatches == []


def test_the_cli_amount_parser_carries_the_fix(hexutil) -> None:
    """``parse_amount`` is what the transaction-building flags actually call."""
    assert hexutil.parse_amount("0.0095", 3) == 10
    assert hexutil.parse_amount("166.5205930005705566699", 18) == 166520593000570556670
    # The raw-base-units suffix bypasses rounding entirely and must stay verbatim.
    assert hexutil.parse_amount("1500000000000000000w", 18) == 1500000000000000000
    assert hexutil.parse_amount("1_000w", 18) == 1000


def test_the_launcher_amount_flag_carries_the_fix() -> None:
    """Through ``pools_write._amount``, the helper behind ``--dev-buy``/``--amount``."""
    pools_write = _load(_POOLS_SCRIPTS, "pools_write")

    assert pools_write._amount("0.0095", 3, "dev-buy") == 10
    assert pools_write._amount("4.0962", 2, "dev-buy") == 410
    assert pools_write._amount(None, 18, "dev-buy") == 0


def test_format_units_round_trips_a_carried_amount(hexutil) -> None:
    """The rendered amount an operator confirms must be the one that was parsed."""
    assert hexutil.format_units(hexutil.parse_units("0.0095", 3), 3) == "0.01"
    assert hexutil.format_units(hexutil.parse_units("4.0962", 2), 2) == "4.1"
