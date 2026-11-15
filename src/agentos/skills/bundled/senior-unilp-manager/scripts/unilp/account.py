"""secp256k1 curve arithmetic and address derivation — everything except signing.

This module exists to be *importable by the read-only path*. ``lp_read.py`` needs one
thing from the key material — "which wallet is mine" — and that answer is a public one:
an address is derived from a private key by a one-way function, and nothing here can
produce a signature.

The split is the whole point. ``secp256k1.py`` imports this module and adds RFC 6979 and
``sign_digest`` on top; a caller that imports *this* module gets no path to a signature no
matter what it passes. So ``lp_read.py`` resolving your own address does not make it
capable of moving funds, and that stays true by construction rather than by review.

The non-constant-time caveat from ``secp256k1.py`` applies here too: pure-Python
big-integer arithmetic leaks timing. It is the same hot-wallet mitigation — and scalar
multiplication by a private key happens here, in ``public_key``, whichever module is
doing the importing.
"""

from __future__ import annotations

from .hexutil import checksum_address, strip0x
from .keccak import keccak256

# Curve parameters (SEC 2, secp256k1). a = 0, b = 7.
P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8
G = (GX, GY)
HALF_N = N // 2
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


# ---------------------------------------------------------------------------
# Curve arithmetic, in Jacobian coordinates
# ---------------------------------------------------------------------------
#
# Affine addition needs a modular inverse per step; at 256 doublings that dominates the
# runtime. Jacobian coordinates defer all of it to a single inverse at the end.
# The point at infinity is any triple with y == 0.


def _jacobian_double(point: tuple[int, int, int]) -> tuple[int, int, int]:
    x, y, z = point
    if not y:
        return (0, 0, 0)
    ysq = (y * y) % P
    s = (4 * x * ysq) % P
    m = (3 * x * x) % P  # + a*z^4, and a == 0 on this curve
    nx = (m * m - 2 * s) % P
    ny = (m * (s - nx) - 8 * ysq * ysq) % P
    nz = (2 * y * z) % P
    return (nx, ny, nz)


def _jacobian_add(p: tuple[int, int, int], q: tuple[int, int, int]) -> tuple[int, int, int]:
    if not p[1]:
        return q
    if not q[1]:
        return p
    u1 = (p[0] * q[2] * q[2]) % P
    u2 = (q[0] * p[2] * p[2]) % P
    s1 = (p[1] * q[2] * q[2] * q[2]) % P
    s2 = (q[1] * p[2] * p[2] * p[2]) % P
    if u1 == u2:
        # Same x: either the same point (double it) or a point and its negation (infinity).
        return _jacobian_double(p) if s1 == s2 else (0, 0, 1)
    h = u2 - u1
    r = s2 - s1
    h2 = (h * h) % P
    h3 = (h * h2) % P
    u1h2 = (u1 * h2) % P
    nx = (r * r - h3 - 2 * u1h2) % P
    ny = (r * (u1h2 - nx) - s1 * h3) % P
    nz = (h * p[2] * q[2]) % P
    return (nx, ny, nz)


def _jacobian_multiply(point: tuple[int, int, int], scalar: int) -> tuple[int, int, int]:
    if point[1] == 0 or scalar == 0:
        return (0, 0, 1)
    if scalar == 1:
        return point
    if scalar < 0 or scalar >= N:
        return _jacobian_multiply(point, scalar % N)
    half = _jacobian_multiply(point, scalar // 2)
    if scalar % 2 == 0:
        return _jacobian_double(half)
    return _jacobian_add(_jacobian_double(half), point)


def _to_affine(point: tuple[int, int, int]) -> tuple[int, int] | None:
    x, y, z = point
    if z == 0 or y == 0:
        return None
    inv = pow(z, -1, P)
    inv2 = (inv * inv) % P
    return ((x * inv2) % P, (y * inv2 % P) * inv % P)


def multiply(point: tuple[int, int], scalar: int) -> tuple[int, int] | None:
    """Scalar multiplication on the curve. ``None`` is the point at infinity."""
    return _to_affine(_jacobian_multiply((point[0], point[1], 1), scalar))


def add(a: tuple[int, int], b: tuple[int, int]) -> tuple[int, int] | None:
    """Point addition. ``None`` is the point at infinity."""
    return _to_affine(_jacobian_add((a[0], a[1], 1), (b[0], b[1], 1)))


# ---------------------------------------------------------------------------
# Keys and addresses
# ---------------------------------------------------------------------------


def normalize_private_key(private_key: str | bytes | int) -> int:
    """Parse a key in any accepted shape and range-check it.

    ``0`` and anything at or above the group order are not valid scalars; a node would
    accept the resulting signature and it would recover to the wrong address.
    """
    if isinstance(private_key, int):
        value = private_key
    elif isinstance(private_key, (bytes, bytearray)):
        value = int.from_bytes(private_key, "big")
    else:
        text = strip0x(private_key.strip().strip('"').strip("'"))
        if len(text) != 64:
            raise ValueError("private key must be 32 bytes")
        # Never let int() see a malformed key: its ValueError embeds the offending
        # string, and the top-level handler prints str(exc) into the transcript.
        if any(character not in _HEX_DIGITS for character in text):
            raise ValueError("private key is not valid hex (32 bytes, 0-9/a-f)")
        value = int(text, 16)
    if not 1 <= value < N:
        raise ValueError("private key out of range for secp256k1")
    return value


def public_key(private_key: str | bytes | int) -> tuple[int, int]:
    point = multiply(G, normalize_private_key(private_key))
    if point is None:  # Unreachable for an in-range scalar; a guard, not a branch.
        raise ValueError("private key produced the point at infinity")
    return point


def address_from_public_key(point: tuple[int, int]) -> str:
    """Last 20 bytes of keccak over the 64-byte uncompressed key, EIP-55 checksummed."""
    body = point[0].to_bytes(32, "big") + point[1].to_bytes(32, "big")
    return checksum_address("0x" + strip0x(keccak256(body))[24:])


def account_from_private_key(private_key: str | bytes | int) -> dict:
    """The signer identity. Deliberately does not carry the key in the returned dict."""
    point = public_key(private_key)
    return {
        "address": address_from_public_key(point),
        "publicKey": "0x" + point[0].to_bytes(32, "big").hex() + point[1].to_bytes(32, "big").hex(),
    }
