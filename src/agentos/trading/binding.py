"""What an operator's approval of a parked order releases, as one digest.

Approving used to name a bare order id. For a multisend that id expanded to
every awaiting leg of its batch while the request, and every surface that
shows one order, named only that leg (WAL-12) — and nothing tied the
approval to the order's terms at all: a row changed between the moment it
was shown and the moment it was signed went out as changed.

So an approval binds to the terms it releases:

* :func:`approval_digest` hashes every leg one approve releases — chain,
  wallet, tokens, raw amounts, recipient, provider, slippage, the floor the
  order was judged at, its plan, its expiry — in creation order. The engine
  shows it beside the legs (``approval.digest``); a client echoes it on
  ``trading.orders.approve``. A batch is never approved without it; any
  change to any leg, or a leg added or dropped, changes it.
* The engine keeps each claimed leg's own digest in memory, beside the
  signer, and checks it again where the transaction is built and where it is
  signed (``TradingService._execute`` and ``_send``). A send or revoke
  additionally carries the exact ``to``/``value``/``data`` it may sign.

The binding lives in the approval itself, not in the ledger: a row edited
behind the engine's back after the approval is refused at signing, whatever
else the ledger says.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from agentos.trading.chains import is_native
from agentos.trading.evm import encode_approve, encode_transfer

#: Bumped whenever :func:`leg_terms` changes shape, so an old digest never matches.
DIGEST_VERSION = 1


def _address(value: Any) -> str | None:
    return str(value).lower() if value else None


def _int_text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    try:
        return str(int(str(value)))
    except ValueError:
        # Not a number: kept as it is, so it still changes the digest.
        return f"?{value}"


def _number(value: Any) -> float | str | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return f"?{value}"


def _plan(raw: Any) -> Any:
    """``quote_json`` as data: the client quote of a swap, the plan of an LP write or bridge."""
    if raw is None or raw == "":
        return None
    try:
        plan = json.loads(str(raw))
    except ValueError:
        return f"?{raw}"
    if isinstance(plan, dict):
        # Written only after the order settles; never part of what was approved.
        plan.pop("settlement", None)
    return plan


def leg_terms(row: dict[str, Any]) -> dict[str, Any]:
    """Everything about one order row that an approval of it agrees to."""
    return {
        "orderId": str(row.get("order_id") or ""),
        "kind": str(row.get("kind") or "swap"),
        "chainId": int(row.get("chain_id") or 0),
        "wallet": _address(row.get("wallet")),
        "tokenIn": _address(row.get("token_in")),
        "tokenOut": _address(row.get("token_out")),
        "amountRaw": _int_text(row.get("amount_raw")),
        "recipient": _address(row.get("recipient")),
        "provider": str(row.get("provider") or ""),
        "initiator": str(row.get("initiator") or ""),
        "batchId": row.get("batch_id"),
        "slippagePct": _number(row.get("slippage_pct")),
        "expectedOutRaw": _int_text(row.get("expected_out_raw")),
        "minOutRaw": _int_text(row.get("min_out_raw")),
        "valueUsd": _number(row.get("value_usd")),
        "expiresAt": _number(row.get("expires_at")),
        "mandateId": row.get("mandate_id"),
        "triggerId": row.get("trigger_id"),
        "note": row.get("note"),
        "plan": _plan(row.get("quote_json")),
    }


def approval_digest(rows: Iterable[dict[str, Any]]) -> str:
    """The sha256 (hex) of every leg one approval releases, in the order given."""
    canonical = json.dumps(
        {"v": DIGEST_VERSION, "legs": [leg_terms(r) for r in rows]},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def expected_tx(row: dict[str, Any]) -> dict[str, Any] | None:
    """The exact ``to``/``value``/``data`` a send or revoke leg may sign; ``None`` otherwise.

    A send and a revoke are fully determined by their row, so the approval
    can pin the transaction itself. A swap, bridge or LP write is built at
    execution (a fresh quote, a re-validated plan); those are held to the
    approved terms by the provider pins, the floor and the plan hash.
    """
    kind = str(row.get("kind") or "swap")
    token = str(row.get("token_in") or "").lower()
    recipient = str(row.get("recipient") or "").lower()
    if kind not in ("send", "revoke") or not token or not recipient:
        return None
    amount = int(str(row.get("amount_raw") or "0"))
    if kind == "revoke":
        return {"to": token, "value": 0, "data": encode_approve(recipient, 0).lower()}
    if is_native(token):
        return {"to": recipient, "value": amount, "data": "0x"}
    return {"to": token, "value": 0, "data": encode_transfer(recipient, amount).lower()}
