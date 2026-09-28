"""``to_hex(value, size)`` must produce exactly ``size`` bytes — or refuse.

``to_hex`` in the vendored ``hexutil`` modules (``senior-unilp-manager`` and the
byte-identical ``poolsdotfun-token-launcher`` copy) documents itself as an
explicit port of viem's ``toHex(value, {size})`` and says it renders "optionally
left-padded to ``size`` bytes", but it never checked that the value *fits*. A
value wider than ``size`` bytes was returned as-is — often with an odd hex-digit
count, so not even a whole number of bytes — while ``pad()`` one function above
already refuses exactly that (``ValueError: value is N bytes, cannot pad to M``)
and viem throws ``SizeExceedsPaddingSizeError``.

The silent form is the dangerous one: ``encode_unlock_data`` in ``v4_actions``
builds Uniswap v4 ``modifyLiquidities`` calldata as
``concat_hex([to_hex(a, size=1) for a in actions])``, so an out-of-range action
byte injected an odd-length nibble into the concatenated blob. ``to_bytes``
silently left-padded it and **every byte after it shifted**, the ``abi.encode``d
length word disagreed with the action count, and a caller pairing N actions with
N params was broadcasting a blob claiming N+1 actions — no error anywhere.

The unsized form is deliberately untouched: ``to_hex(256)`` is ``"0x100"``,
the JSON-RPC *quantity* encoding (see the docstring note about ``fromBlock``).
"""

from __future__ import annotations

import importlib
import sys
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


# value, size — every one wider than ``size`` bytes.
_OVERSIZED_CASES = [
    (256, 1),  # 2 bytes into 1: the odd-length '0x100' case
    (300, 1),  # '0x12c' — 3 digits, not a whole byte string
    (65536, 2),  # 3 bytes into 2
    (2**256, 32),  # one uint256 past the edge
    (b"\x01\x02\x03", 2),  # 3 bytes into 2
    (b"\x01\x02\x03", 1),  # 3 bytes into 1
    ("0x1234", 1),  # str form: 2 bytes into 1
    ("1234", 1),  # str form without the 0x prefix
]


@pytest.mark.parametrize(("value", "size"), _OVERSIZED_CASES)
def test_a_value_wider_than_size_is_refused(hexutil, value, size) -> None:
    """Padding is not truncation: the call is refused, exactly like ``pad()``."""
    with pytest.raises(ValueError, match=r"cannot fit in"):
        hexutil.to_hex(value, size)


@pytest.mark.parametrize(("value", "size"), _OVERSIZED_CASES)
def test_the_launcher_copy_refuses_the_same_way(pools_hexutil, value, size) -> None:
    """The two vendored copies are the same file; neither may drift."""
    with pytest.raises(ValueError, match=r"cannot fit in"):
        pools_hexutil.to_hex(value, size)


@pytest.mark.parametrize(
    ("value", "size"),
    [(256, 1), (300, 1), (65536, 2), (2**256, 32)],
)
def test_pad_refuses_an_oversized_value_too(hexutil, value, size) -> None:
    """The inconsistency inside the module is the bug: both must refuse.

    ``pad()`` already did (``ValueError: value is N bytes, cannot pad to M``);
    ``to_hex(value, size)`` is the call that silently returned wrong-width hex.
    """
    body = hexutil.to_bytes(hexutil.to_hex(value) if isinstance(value, int) else value)
    with pytest.raises(ValueError, match=r"cannot pad to"):
        hexutil.pad(body, size)


@pytest.mark.parametrize(
    ("value", "size", "expected"),
    [
        (255, 1, "0xff"),  # exactly at the edge
        (0, 1, "0x00"),
        (2**256 - 1, 32, "0x" + "f" * 64),  # the other edge
        (0, 32, "0x" + "0" * 64),
        (b"", 1, "0x00"),  # padding still applies
        (b"", 32, "0x" + "0" * 64),
        (b"\x01\x02", 2, "0x0102"),
        (b"\x01\x02", 4, "0x00000102"),  # pads out to width
        ("0xff", 1, "0xff"),
        ("ff", 1, "0xff"),
    ],
)
def test_an_in_range_value_renders_at_exactly_size_bytes(hexutil, value, size, expected) -> None:
    assert hexutil.to_hex(value, size) == expected
    body = expected[2:]
    assert len(body) == size * 2  # exactly ``size`` bytes wide, never odd
    assert len(body) % 2 == 0


def test_the_unsized_form_is_unchanged(hexutil) -> None:
    """``to_hex(256)`` is ``"0x100"``: the JSON-RPC quantity encoding, by contract.

    Callers that need whole bytes pass ``size``; ``fromBlock`` must stay minimal
    (``toHex(1n)`` is ``"0x1"``, not ``"0x01"``) or a node rejects ``"0x00"``.
    """
    assert hexutil.to_hex(256) == "0x100"
    assert hexutil.to_hex(255) == "0xff"
    assert hexutil.to_hex(1) == "0x1"
    assert hexutil.to_hex(0) == "0x0"
    assert hexutil.to_hex(2**256) == "0x1" + "0" * 64  # 65 digits — unsized is unbounded
    assert hexutil.to_hex(b"\x01\x02") == "0x0102"


def test_a_negative_integer_still_requires_an_explicit_size(hexutil) -> None:
    with pytest.raises(ValueError, match="negative integer requires an explicit size"):
        hexutil.to_hex(-1)
    # With an explicit size a negative is its two's complement over that width —
    # and so always exactly ``size`` bytes, never over-wide.
    assert hexutil.to_hex(-1, 4) == "0xffffffff"
    assert hexutil.to_hex(-1, 32) == "0x" + "f" * 64
    # -2**128 as 32-byte two's complement: 2**256 - 2**128, i.e. 16 f-pairs + 16 zero-pairs.
    assert hexutil.to_hex(-(2**128), 32) == "0x" + "f" * 32 + "0" * 32


def test_an_out_of_range_action_no_longer_builds_corrupt_unlock_data(hexutil) -> None:
    """The downstream bug: ``encode_unlock_data`` must fail at construction time.

    Before the fix, ``to_hex(256, size=1)`` returned ``"0x100"``, the odd digit
    count made ``to_bytes`` silently widen the blob, and the two ``unlockData``
    encodings diverged from index 193 on (actions length 3 instead of 2, payload
    ``0x01000b`` instead of ``0x000b``) — a blob claiming 3 actions and 2 params.
    """
    v4_actions = _load(_UNILP_SCRIPTS, "unilp.v4_actions")
    good = v4_actions.encode_unlock_data([0x00, 0x0B], ["0x", "0x"])
    assert isinstance(good, str) and good.startswith("0x")
    # An action byte outside 0x00–0xFF is a caller error and must surface now.
    with pytest.raises(ValueError, match=r"cannot fit in"):
        v4_actions.encode_unlock_data([256, 0x0B], ["0x", "0x"])
    with pytest.raises(ValueError, match=r"cannot fit in"):
        v4_actions.encode_unlock_data([300], ["0x"])
    # In-range actions still encode.
    assert isinstance(v4_actions.encode_unlock_data([0x00], ["0x"]), str)


def test_as_int_n_zero_bits_is_zero(hexutil) -> None:
    """JS ``BigInt.asIntN(0, x)`` is ``0n``; the port raised ``negative shift count``."""
    assert hexutil.as_int_n(0, 0) == 0
    assert hexutil.as_int_n(0, 5) == 0
    assert hexutil.as_int_n(0, -1) == 0
    assert hexutil.as_int_n(0, 2**256) == 0


def test_as_int_n_negative_width_has_a_real_message(hexutil) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        hexutil.as_int_n(-1, 5)
    with pytest.raises(ValueError, match="non-negative"):
        hexutil.as_uint_n(-1, 5)


def test_as_int_n_normal_width_is_untouched(hexutil) -> None:
    assert hexutil.as_int_n(8, 255) == -1
    assert hexutil.as_int_n(8, 128) == -128
    assert hexutil.as_int_n(8, 127) == 127
    assert hexutil.as_int_n(24, 0xFFFFFF) == -1  # all 24 bits set → -1 in int24
    assert hexutil.as_int_n(24, 0x7FFFFF) == 0x7FFFFF  # max positive int24
    assert hexutil.as_int_n(24, 0x800000) == -(1 << 23)  # min negative int24
    assert hexutil.as_uint_n(8, -1) == 255
    assert hexutil.as_uint_n(0, 5) == 0  # was already correct


def test_the_two_vendored_copies_stay_byte_identical() -> None:
    """The drift guard the carry fix (#2394) established for this module pair."""
    assert (_UNILP_SCRIPTS / "unilp" / "hexutil.py").read_text() == (
        _POOLS_SCRIPTS / "poolsfun" / "hexutil.py"
    ).read_text()
