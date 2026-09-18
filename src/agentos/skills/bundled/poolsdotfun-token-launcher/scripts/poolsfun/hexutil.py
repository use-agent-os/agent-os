"""Hex, address and fixed-point helpers — the non-ABI half of what viem provided.

Two groups live here, and the second is the dangerous one:

* mechanical conversions (``to_hex``, ``pad``, ``concat_hex``, EIP-55 checksums);
* arithmetic that must reproduce **JavaScript** semantics rather than Python's.

Python and JS disagree in three places this port touches, and every disagreement
produces a wrong number rather than an exception:

* ``BigInt`` division truncates toward zero; Python ``//`` floors. Identical for
  non-negative operands, off by one for negatives — see :func:`div_trunc`.
* ``Math.round`` is half-up toward +inf; Python ``round`` is banker's rounding, so
  ``round(0.5)`` is ``0``. See :func:`js_round`.
* ``BigInt.asIntN`` / ``asUintN`` have no Python equivalent at all. See
  :func:`as_int_n` / :func:`as_uint_n`.
"""

from __future__ import annotations

from .keccak import keccak256_bytes

MAX_UINT128 = (1 << 128) - 1
MAX_UINT160 = (1 << 160) - 1
MAX_UINT256 = (1 << 256) - 1
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


# ---------------------------------------------------------------------------
# JS-semantics arithmetic
# ---------------------------------------------------------------------------

def div_trunc(numerator: int, denominator: int) -> int:
    """Integer division that truncates toward zero, like JS ``BigInt`` division.

    ``-7 // 2`` is ``-4`` in Python but ``-3n`` in JavaScript. Every division
    ported from the Node source goes through here, because the ported code was
    written against the JS behaviour.
    """
    quotient = abs(numerator) // abs(denominator)
    return -quotient if (numerator < 0) != (denominator < 0) else quotient


def js_round(value: float) -> int:
    """``Math.round``: ties go toward positive infinity.

    Python's ``round`` is banker's rounding, so ``round(0.5) == 0`` and
    ``round(2.5) == 2``. JS gives ``1`` and ``3``. ``snapTick(mode='nearest')`` and
    the gas multiplier both depend on this.
    """
    import math

    return math.floor(value + 0.5)


def as_int_n(bits: int, value: int) -> int:
    """``BigInt.asIntN`` — reinterpret the low ``bits`` of ``value`` as signed."""
    masked = value & ((1 << bits) - 1)
    if masked >= (1 << (bits - 1)):
        return masked - (1 << bits)
    return masked


def as_uint_n(bits: int, value: int) -> int:
    """``BigInt.asUintN`` — reinterpret the low ``bits`` of ``value`` as unsigned.

    Also the way to reproduce a deliberately-wrapping subtraction: the fee-growth
    delta in ``getFeesOwed`` relies on ``uint256`` underflow, and a plain Python
    subtraction there yields a large negative number instead of the intended wrap.
    """
    return value & ((1 << bits) - 1)


# ---------------------------------------------------------------------------
# Hex
# ---------------------------------------------------------------------------

def strip0x(value: str) -> str:
    return value[2:] if value[:2].lower() == "0x" else value


def to_bytes(value: str | bytes | bytearray) -> bytes:
    """Coerce a hex string or bytes-like to ``bytes``."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    body = strip0x(value)
    if len(body) % 2:
        body = "0" + body
    return bytes.fromhex(body)


def to_hex(value: int | bytes | bytearray | str, size: int | None = None) -> str:
    """Render as a ``0x`` hex string, optionally left-padded to ``size`` bytes.

    Mirrors viem's ``toHex(value, {size})``. A negative integer is encoded as its
    two's-complement over ``size`` bytes, which requires ``size``.
    """
    if isinstance(value, (bytes, bytearray)):
        body = bytes(value).hex()
        if size is not None:
            body = body.rjust(size * 2, "0")
        return "0x" + body
    if isinstance(value, str):
        body = strip0x(value)
        if size is not None:
            body = body.rjust(size * 2, "0")
        return "0x" + body
    if value < 0:
        if size is None:
            raise ValueError("to_hex of a negative integer requires an explicit size")
        value = as_uint_n(size * 8, value)
    body = format(value, "x")
    if size is not None:
        return "0x" + body.rjust(size * 2, "0")
    # Unsized: minimal hex, exactly like viem's toHex(bigint) — `toHex(1n)` is "0x1",
    # not "0x01". This is the JSON-RPC *quantity* encoding, and a node rejects
    # "fromBlock": "0x00" outright. Callers that need whole bytes pass `size`.
    return "0x" + body


def pad(value: str | bytes | bytearray, size: int = 32, right: bool = False) -> str:
    """Left-pad (or right-pad) a hex value to ``size`` bytes."""
    body = to_bytes(value).hex()
    target = size * 2
    if len(body) > target:
        raise ValueError(f"value is {len(body) // 2} bytes, cannot pad to {size}")
    return "0x" + (body.ljust(target, "0") if right else body.rjust(target, "0"))


def concat_hex(parts: list[str]) -> str:
    return "0x" + "".join(strip0x(p) for p in parts)


# ---------------------------------------------------------------------------
# Addresses
# ---------------------------------------------------------------------------

def checksum_address(address: str) -> str:
    """EIP-55 checksummed form of ``address``.

    The lowercasing is load-bearing: the launchpad registry tables are written in
    mixed case, and hashing a mixed-case string produces a different digest and
    therefore a wrong checksum. Getting this wrong corrupts poolId derivation,
    because the PoolKey encoding runs through this function.
    """
    body = strip0x(address).lower()
    if len(body) != 40:
        raise ValueError(f"not a 20-byte address: {address!r}")
    try:
        int(body, 16)
    except ValueError:
        raise ValueError(f"address has non-hex characters: {address!r}") from None
    digest = keccak256_bytes(body.encode("ascii")).hex()
    return "0x" + "".join(
        char.upper() if int(digest[i], 16) >= 8 else char
        for i, char in enumerate(body)
    )


def is_same_address(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return strip0x(a).lower() == strip0x(b).lower()


def is_zero_address(address: str | None) -> bool:
    return is_same_address(address, ZERO_ADDRESS)


# ---------------------------------------------------------------------------
# Fixed-point <-> human amounts
# ---------------------------------------------------------------------------

def parse_units(value: str, decimals: int) -> int:
    """Human decimal string -> base units, reproducing viem's ``parseUnits``.

    viem **rounds** excess fraction digits half-up, with carry into the integer
    part, rather than truncating: ``parse_units("0.9999999", 6)`` is ``1000000``,
    not ``999999``. A truncating implementation silently sizes an amount smaller
    than the one the user approved, and the plan hash still validates because it
    hashes the already-parsed value.
    """
    text = str(value).strip()
    negative = text.startswith("-")
    if negative:
        text = text[1:]
    if "." in text:
        integer, fraction = text.split(".", 1)
    else:
        integer, fraction = text, "0"
    integer = integer or "0"
    fraction = fraction.rstrip("0")

    if decimals == 0:
        # Round the whole fraction to 0 or 1 and fold it into the integer.
        if fraction and js_round(float("0." + fraction)) == 1:
            integer = str(int(integer) + 1)
        fraction = ""
    elif len(fraction) > decimals:
        left = fraction[: decimals - 1]
        unit = fraction[decimals - 1: decimals]
        right = fraction[decimals:]
        rounded = js_round(float(f"{unit}.{right}"))
        if rounded > 9:
            # The rounded digit carried; bump the digit to its left and reset.
            # The trailing zero is inside what gets padded: viem writes
            # ``${BigInt(left) + 1n}0``.padStart(left.length + 1, "0"). Padding
            # the digits alone and then appending the zero makes the fraction
            # one character too long whenever the carry does not widen ``left``,
            # which the length check below reads as a carry into the integer.
            fraction = (str(int(left or "0") + 1) + "0").rjust(len(left) + 1, "0")
        else:
            fraction = f"{left}{rounded}"
        if len(fraction) > decimals:
            fraction = fraction[1:]
            integer = str(int(integer) + 1)
        fraction = fraction[:decimals]
    else:
        fraction = fraction.ljust(decimals, "0")

    result = int(f"{integer}{fraction}" or "0")
    return -result if negative else result


def format_units(value: int, decimals: int) -> str:
    """Base units -> human decimal string, reproducing viem's ``formatUnits``.

    Trailing zeroes in the fraction are dropped, and a value with no fractional
    remainder renders with no decimal point at all.
    """
    text = str(value)
    negative = text.startswith("-")
    if negative:
        text = text[1:]
    text = text.rjust(decimals, "0")
    integer = text[: len(text) - decimals]
    fraction = text[len(text) - decimals:].rstrip("0") if decimals else ""
    sign = "-" if negative else ""
    return f"{sign}{integer or '0'}" + (f".{fraction}" if fraction else "")


def parse_amount(text: str, decimals: int) -> int:
    """Parse a CLI amount: human units, or raw base units with a ``w`` suffix.

    ``1.5`` is 1.5 tokens; ``1500000000000000000w`` is that many base units
    verbatim. Underscores are stripped so long literals stay readable.
    """
    cleaned = str(text).strip().replace("_", "")
    if cleaned.lower().endswith("w"):
        return int(cleaned[:-1])
    return parse_units(cleaned, decimals)
