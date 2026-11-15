"""What a swap provider's ``permitData`` must be before the vault key signs it.

A Uniswap Trading API quote may carry EIP-712 typed data for the wallet to
sign. Signed as it came, that is a blank cheque: a hostile or tampered
response can put any signable authorization there — an EIP-3009
``TransferWithAuthorization`` of the wallet's whole USDC balance, a Permit2
allowance to an attacker for every token, a DAI or EIP-2612 permit on
another chain — and the signature leaves for the provider's ``/swap`` before
any check on the swap transaction runs (WAL-07).

So the typed data is untrusted input, judged against the order:

* the domain is the canonical Permit2 contract on the order's chain (name
  ``Permit2``, no version or salt);
* the type is exactly Permit2's ``PermitSingle`` (allowance) or
  ``PermitTransferFrom`` (signature transfer) — nothing else is signed;
* the token is the order's sell token, the amount no more than the order's,
  the spender a Universal Router pinned for the chain, and every deadline
  and expiration in the future and at most :data:`MAX_PERMIT_LIFETIME_S`
  away; nonces fit their type.

What gets signed is rebuilt from the checked fields, never the provider's
object: nothing the check did not read reaches the signer.
"""

from __future__ import annotations

import re
from typing import Any

from agentos.trading.providers import PERMIT2

#: Permit2, the same address on every chain Uniswap deploys to (lowercase).
PERMIT2_ADDRESS = PERMIT2.lower()
#: The longest a permit's deadline or allowance expiration may reach. A swap
#: signs and broadcasts within seconds; nothing it needs outlives an hour.
MAX_PERMIT_LIFETIME_S = 3600

_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")

DOMAIN_TYPE = [
    {"name": "name", "type": "string"},
    {"name": "chainId", "type": "uint256"},
    {"name": "verifyingContract", "type": "address"},
]
PERMIT_SINGLE_TYPES: dict[str, list[dict[str, str]]] = {
    "PermitSingle": [
        {"name": "details", "type": "PermitDetails"},
        {"name": "spender", "type": "address"},
        {"name": "sigDeadline", "type": "uint256"},
    ],
    "PermitDetails": [
        {"name": "token", "type": "address"},
        {"name": "amount", "type": "uint160"},
        {"name": "expiration", "type": "uint48"},
        {"name": "nonce", "type": "uint48"},
    ],
}
PERMIT_TRANSFER_FROM_TYPES: dict[str, list[dict[str, str]]] = {
    "PermitTransferFrom": [
        {"name": "permitted", "type": "TokenPermissions"},
        {"name": "spender", "type": "address"},
        {"name": "nonce", "type": "uint256"},
        {"name": "deadline", "type": "uint256"},
    ],
    "TokenPermissions": [
        {"name": "token", "type": "address"},
        {"name": "amount", "type": "uint256"},
    ],
}
_SCHEMAS: dict[str, dict[str, list[dict[str, str]]]] = {
    "PermitSingle": PERMIT_SINGLE_TYPES,
    "PermitTransferFrom": PERMIT_TRANSFER_FROM_TYPES,
}


class PermitRefusedError(ValueError):
    """The typed data does not bind to the order; it must not be signed."""


def _fields(fields: Any) -> list[dict[str, str]] | None:
    if not isinstance(fields, list):
        return None
    out: list[dict[str, str]] = []
    for item in fields:
        if not isinstance(item, dict) or set(item) != {"name", "type"}:
            return None
        out.append({"name": str(item["name"]), "type": str(item["type"])})
    return out


def _uint(value: Any, bits: int, what: str) -> int:
    """An unsigned integer of ``bits`` bits, from an int or a decimal string only."""
    if isinstance(value, bool):
        raise PermitRefusedError(f"{what} is not an integer")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.isdigit() and value.isascii():
        number = int(value)
    else:
        raise PermitRefusedError(f"{what} is not an integer")
    if number < 0 or number >= 1 << bits:
        raise PermitRefusedError(f"{what} does not fit uint{bits}")
    return number


def _address(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _ADDRESS.match(value):
        raise PermitRefusedError(f"{what} is not an address")
    return value.lower()


def _exact_keys(value: Any, keys: set[str], what: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise PermitRefusedError(f"{what} must carry exactly {', '.join(sorted(keys))}")
    return value


def _within(value: int, now: int, what: str) -> int:
    if value <= now:
        raise PermitRefusedError(f"{what} is already past")
    if value > now + MAX_PERMIT_LIFETIME_S:
        raise PermitRefusedError(f"{what} is more than {MAX_PERMIT_LIFETIME_S // 60} minutes away")
    return value


def _checksum(address: str) -> str:
    from eth_utils import to_checksum_address

    return str(to_checksum_address(address))


def check_permit(
    permit: Any,
    *,
    chain_id: int,
    token: str,
    native: bool,
    amount_raw: int,
    spenders: frozenset[str],
    now: int,
) -> dict[str, Any]:
    """The typed data to sign for this order, rebuilt; :class:`PermitRefusedError` otherwise.

    ``token`` / ``amount_raw`` are the order's sell side, ``spenders`` the
    lowercase contracts a permit may name (the chain's pinned Universal
    Routers; empty refuses everything), ``now`` unix seconds.
    """
    if native:
        raise PermitRefusedError("a sale of the gas coin needs no permit")
    if not isinstance(permit, dict):
        raise PermitRefusedError("permitData is not an object")
    domain = _exact_keys(permit.get("domain"), {"name", "chainId", "verifyingContract"}, "domain")
    if domain["name"] != "Permit2":
        raise PermitRefusedError(f"domain {domain['name']!r} is not Permit2")
    if _uint(domain["chainId"], 256, "domain chainId") != int(chain_id):
        raise PermitRefusedError(f"domain chainId {domain['chainId']} is not the order's chain")
    if _address(domain["verifyingContract"], "verifyingContract") != PERMIT2_ADDRESS:
        raise PermitRefusedError("verifyingContract is not the canonical Permit2")

    types = permit.get("types")
    if not isinstance(types, dict):
        raise PermitRefusedError("types is not an object")
    declared = dict(types)
    if "EIP712Domain" in declared and _fields(declared.pop("EIP712Domain")) != DOMAIN_TYPE:
        raise PermitRefusedError("EIP712Domain is not Permit2's domain")
    primary = next(
        (
            name
            for name, schema in _SCHEMAS.items()
            if set(declared) == set(schema)
            and all(_fields(declared[k]) == schema[k] for k in schema)
        ),
        None,
    )
    if primary is None:
        raise PermitRefusedError(
            f"type {sorted(declared)} is not a Permit2 PermitSingle or PermitTransferFrom"
        )
    stated = permit.get("primaryType")
    if stated is not None and stated != primary:
        raise PermitRefusedError(f"primaryType {stated!r} is not {primary}")

    values = permit.get("values", permit.get("message"))
    if primary == "PermitSingle":
        message = _exact_keys(values, {"details", "spender", "sigDeadline"}, "PermitSingle")
        details = _exact_keys(
            message["details"], {"token", "amount", "expiration", "nonce"}, "PermitDetails"
        )
        permitted_token = _address(details["token"], "permit token")
        amount = _uint(details["amount"], 160, "permit amount")
        expiration = _within(_uint(details["expiration"], 48, "expiration"), now, "expiration")
        nonce = _uint(details["nonce"], 48, "nonce")
        deadline = _within(_uint(message["sigDeadline"], 256, "sigDeadline"), now, "sigDeadline")
    else:
        message = _exact_keys(
            values, {"permitted", "spender", "nonce", "deadline"}, "PermitTransferFrom"
        )
        permissions = _exact_keys(message["permitted"], {"token", "amount"}, "TokenPermissions")
        permitted_token = _address(permissions["token"], "permit token")
        amount = _uint(permissions["amount"], 256, "permit amount")
        nonce = _uint(message["nonce"], 256, "nonce")
        deadline = _within(_uint(message["deadline"], 256, "deadline"), now, "deadline")
        expiration = deadline
    spender = _address(message["spender"], "spender")

    if permitted_token != token.lower():
        raise PermitRefusedError("permit token is not the token being sold")
    if amount <= 0 or amount > int(amount_raw):
        raise PermitRefusedError(f"permit amount {amount} exceeds the order's {int(amount_raw)}")
    if spender not in spenders:
        raise PermitRefusedError(f"spender {spender} is not a router pinned for this chain")

    rebuilt_domain = {
        "name": "Permit2",
        "chainId": int(chain_id),
        "verifyingContract": _checksum(PERMIT2_ADDRESS),
    }
    if primary == "PermitSingle":
        rebuilt_values: dict[str, Any] = {
            "details": {
                "token": _checksum(permitted_token),
                "amount": amount,
                "expiration": expiration,
                "nonce": nonce,
            },
            "spender": _checksum(spender),
            "sigDeadline": deadline,
        }
    else:
        rebuilt_values = {
            "permitted": {"token": _checksum(permitted_token), "amount": amount},
            "spender": _checksum(spender),
            "nonce": nonce,
            "deadline": deadline,
        }
    return {
        "domain": rebuilt_domain,
        "types": {k: [dict(f) for f in v] for k, v in _SCHEMAS[primary].items()},
        "primaryType": primary,
        "values": rebuilt_values,
    }
