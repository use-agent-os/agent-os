"""Integrity seals on the ledger rows the signer acts on.

The trading ledger is a SQLite file. The sensitive-path floor keeps an agent's
shell and file tools off it, but the floor is a text scan, not an OS boundary:
a same-user process that reaches the file can rewrite a parked order's
recipient, a mandate's token or a trigger's size, and the engine used to sign
whatever the row said when it was read back. So the engine no longer trusts a
row it reads back. Every row that can lead to a signature carries a *seal*:
HMAC-SHA256 over the row's money terms, keyed by a secret that never reaches
agent-readable disk in the clear.

The key
    :meth:`agentos.trading.vault.Vault.ledger_seal_key`, derived from the
    vault's own password-encrypted secret. It exists only in gateway memory,
    only while the vault is unlocked, and survives restarts and password
    changes. A process that can forge a seal could already decrypt the
    wallet keys, so the seal is exactly as strong as the vault.

What is sealed
    The *terms* (:data:`TERMS`): every column that decides what is signed or
    how much may be spent: wallet, chain, tokens, amounts, recipient,
    slippage, floors, the bridge/LP plan (``quote_json``, which carries the LP
    ``planHash`` over the calldata), batch membership and expiry. Never the
    state the engine moves on its own (status, counters, a trail's peak), so a
    seal never has to be rewritten while the vault is locked. The seal also
    binds the wallet's *incarnation* (when the vault learned that key), so a
    row sealed for a wallet that was removed and imported again is a mismatch.

When
    An order is sealed by the ledger itself on insert, and re-sealed on every
    engine write to one of its terms, but only after the old seal still
    verified: a write never launders an out-of-band edit. A mandate or a
    trigger is sealed when the operator approves it (or creates it as the
    operator). An agent's proposal stays unsealed until then, so a status the
    agent flips to ``active`` in SQL has no seal.

Where it is checked
    Right before anything signs: :meth:`TradingService._execute` checks the
    row it builds the transaction from, ``_send`` checks the ledger again
    right before the signature, and the DCA and trigger loops check a
    mandate or trigger before they place its order. ``missing`` or
    ``mismatch`` refuses and alerts (``trading.integrity.alert``).

Composition with approval binding
    This layer answers "is this row what the engine wrote?". It does not
    answer "is this what the operator was shown?"; an approval record that
    binds the operator's decision to transaction parameters does that, and the
    two compose: check the approval record against the sealed row, then sign.
    :func:`terms_digest` is the stable, unkeyed fingerprint of a row's terms
    for an approval record or a UI to carry.

Token metadata
    A token row's ``decimals`` (and native flag) is sealed on every engine
    write. :meth:`TradingService._token_meta` uses a cached row only when its
    seal holds, and otherwise asks the token list or the chain again: an
    edited ``decimals`` would otherwise mis-size an amount or value a large
    order at a few cents under the approval threshold and the daily cap.

What it does not stop
    A replay of an earlier state the engine itself sealed (the row and its
    seal copied back together). The sealer remembers every seal it wrote in
    this process, so a replay is caught until the gateway restarts; across a
    restart only the floor stands in the way. Spend accounting
    (``daily_spend``, a mandate's confirmed orders) is not sealed: deleting a
    row cannot be detected by a per-row seal. Display-only columns (a token's
    symbol and ``verified`` flag, an order's note) are not sealed either.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable, Mapping
from typing import Any, Literal

SEAL_VERSION = "s1"

#: The kinds of sealed row, with their table and id column.
TABLES: dict[str, tuple[str, str]] = {
    "order": ("orders", "order_id"),
    "mandate": ("mandates", "mandate_id"),
    "trigger": ("triggers", "trigger_id"),
}

ORDER_TERMS: tuple[str, ...] = (
    "order_id",
    "kind",
    "chain_id",
    "wallet",
    "token_in",
    "token_out",
    "amount_raw",
    # What the approval card shows as the amount; it must stay the signed one.
    "amount_human",
    "expected_out_raw",
    "min_out_raw",
    "slippage_pct",
    "value_usd",
    "provider",
    "recipient",
    "batch_id",
    "initiator",
    "mandate_id",
    "trigger_id",
    "quote_json",
    "expires_at",
    "created_at",
)
MANDATE_TERMS: tuple[str, ...] = (
    "mandate_id",
    "kind",
    "chain_id",
    "wallet",
    "token_in",
    "token_out",
    "usd_per_run",
    "cap_usd",
    "runs_max",
    "every_seconds",
    "max_price_usd",
    "slippage_pct",
    "initiator",
    "created_at",
    "approved_at",
)
TRIGGER_TERMS: tuple[str, ...] = (
    "trigger_id",
    "kind",
    "chain_id",
    "wallet",
    "token",
    "quote",
    "direction",
    "price_usd",
    "trail_pct",
    "from_price_usd",
    "amount_usd",
    "amount_pct",
    "amount_raw",
    "slippage_pct",
    "confirm_ticks",
    "valid_until",
    "initiator",
    "created_at",
    "approved_at",
    "group_id",
    "leg",
)
#: A token's cached metadata: ``decimals`` turns every human amount into the
#: raw one that is signed and every raw amount into the dollars the guardrails
#: judge. Only what each engine write overwrites outright is sealed, so a write
#: can never carry an edited value over into a fresh seal.
TOKEN_KIND = "token"
TOKEN_TERMS: tuple[str, ...] = ("chain_id", "address", "decimals", "is_native")
TERMS: dict[str, tuple[str, ...]] = {
    "order": ORDER_TERMS,
    "mandate": MANDATE_TERMS,
    "trigger": TRIGGER_TERMS,
    TOKEN_KIND: TOKEN_TERMS,
}


def token_id(chain_id: int, address: str) -> str:
    return f"{int(chain_id)}:{str(address).lower()}"


def row_id(kind: str, row: Mapping[str, Any]) -> str:
    """The id a seal of ``kind`` is stored (and remembered) under."""
    if kind == TOKEN_KIND:
        return token_id(int(row.get("chain_id") or 0), str(row.get("address") or ""))
    return str(row.get(TABLES[kind][1]) or "")


#: ``ok``: the seal is the engine's; ``missing``: there is none; ``mismatch``:
#: the row (or the seal) changed outside the engine, or the seal was replayed.
Verdict = Literal["ok", "missing", "mismatch"]


def _value(value: Any) -> Any:
    """A column value as it is hashed: the row as SQLite hands it back.

    Seals are always computed from a row read back from the database, so the
    storage class is already settled (REAL comes back ``float``, TEXT ``str``);
    ``bytes`` is the one type JSON cannot carry and a column should never hold.
    """
    if isinstance(value, bytes):
        return {"hex": value.hex()}
    return value


def canonical_terms(kind: str, row: Mapping[str, Any], *, incarnation: str | None) -> bytes:
    """The bytes a seal covers: the version, the kind, the wallet's incarnation, the terms."""
    fields = TERMS[kind]
    body = {
        "v": SEAL_VERSION,
        "kind": kind,
        "incarnation": incarnation,
        "terms": [[name, _value(row.get(name))] for name in fields],
    }
    return json.dumps(body, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def terms_digest(kind: str, row: Mapping[str, Any]) -> str:
    """An unkeyed SHA-256 of a row's terms, for an approval record or a UI to bind to."""
    return hashlib.sha256(canonical_terms(kind, row, incarnation=None)).hexdigest()


def terms_touched(kind: str, fields: Mapping[str, Any] | set[str] | tuple[str, ...]) -> bool:
    """Whether a write of ``fields`` changes one of ``kind``'s sealed terms."""
    terms = TERMS[kind]
    return any(name in terms for name in fields)


class Sealer:
    """Seals and checks rows with the vault's ledger key.

    ``key_source`` answers the key, or ``None`` while the vault is locked (a
    seal can then be neither made nor checked). ``incarnation`` answers the
    incarnation of a wallet the vault holds, or ``None`` for one it does not.
    """

    def __init__(
        self,
        key_source: Callable[[], bytes | None],
        incarnation: Callable[[str], str | None],
    ) -> None:
        self._key_source = key_source
        self._incarnation = incarnation
        # Every seal this process wrote (or dropped: ``None``), by row. A row
        # restored together with an older seal of its own is caught here.
        self._latest: dict[tuple[str, str], str | None] = {}

    @property
    def available(self) -> bool:
        return self._key_source() is not None

    def _mac(self, key: bytes, kind: str, row: Mapping[str, Any]) -> str:
        wallet = str(row.get("wallet") or "")
        message = canonical_terms(kind, row, incarnation=self._incarnation(wallet))
        return f"{SEAL_VERSION}:{hmac.new(key, message, hashlib.sha256).hexdigest()}"

    def seal(self, kind: str, row: Mapping[str, Any]) -> str | None:
        """The seal for ``row``, or ``None`` while there is no key."""
        key = self._key_source()
        if key is None:
            return None
        return self._mac(key, kind, row)

    def check(self, kind: str, row: Mapping[str, Any], seal: str | None) -> Verdict | None:
        """``ok`` / ``missing`` / ``mismatch``; ``None`` while there is no key to check with."""
        key = self._key_source()
        if key is None:
            return None
        if not seal:
            return "missing"
        expected = self._mac(key, kind, row)
        if not hmac.compare_digest(expected, str(seal)):
            return "mismatch"
        ident = (kind, row_id(kind, row))
        if ident in self._latest and self._latest[ident] != seal:
            return "mismatch"
        return "ok"

    def remember(self, kind: str, ident: str, seal: str | None) -> None:
        """Record what this process last wrote for a row (``None``: it dropped the seal)."""
        self._latest[(kind, ident)] = seal
