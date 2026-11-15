"""DCA mandates: recurring buys the trading engine runs by itself.

``docs/dca.md`` is the contract. This module is the pure part of it: the
schedule arithmetic (drift-free ``next_run_at``), the sizing of a run against
the cap, the reasons a run is skipped, and the card payload
(``application/vnd.agentos.dca+json``). It imports nothing from the service,
so the CLI and tests can use it without a wallet, a network or a ledger; the
runner itself lives on :class:`agentos.trading.service.TradingService`, which
owns the swap pipeline every buy goes through.

Times are epoch seconds (floats) everywhere except the payload, which carries
ISO-8601 UTC strings. USD ``None`` means unknown, never ``0``.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from agentos.trading.chains import ChainSpec, checksum_address
from agentos.trading.pnl import format_amount, to_human

DCA_MIME = "application/vnd.agentos.dca+json"
PAYLOAD_VERSION = 1
MANDATE_KIND = "dca"
MANDATE_ID_PREFIX = "dca_"

STATUSES = (
    "awaiting_approval",
    "active",
    "paused",
    "completed",
    "stopped",
    "rejected",
    "expired",
)
LIVE_STATUSES = frozenset({"awaiting_approval", "active", "paused"})
TERMINAL_STATUSES = frozenset({"completed", "stopped", "rejected", "expired"})

# ``pending`` is the one status the contract's run list does not name: a buy
# whose order is on its way to the chain (quoted, approved or submitted). It
# becomes ``filled`` or ``failed`` when that order settles.
RUN_STATUSES = ("pending", "filled", "parked", "skipped", "failed", "expired", "rejected")
RUN_OPEN_STATUSES = frozenset({"pending", "parked"})
SKIP_REASONS = ("max_price", "daily_cap", "insufficient_balance", "cap_reached")
# Skips that say something is wrong (and count toward the auto-pause), as
# opposed to a price guard or a full cap that are the mandate working.
BAD_SKIPS = frozenset({"daily_cap", "insufficient_balance"})

MIN_EVERY_SECONDS = 60
#: A remainder of the cap under this is not worth a buy: the mandate is done.
#: Capped at half a buy by :func:`dust_for`, so a mandate of tiny buys still runs.
DUST_USD = 0.5
#: The smallest buy a mandate may be set to.
MIN_USD_PER_RUN = 0.01
#: Consecutive bad runs (failed, or skipped for balance / daily cap) that pause.
BAD_STREAK_LIMIT = 3
#: How long an agent's proposal waits for the user before it expires.
PENDING_TTL = 86_400
#: Runs a card shows (newest first).
HISTORY_LIMIT = 50
#: A ``pending`` run that never got an order (the process died between the
#: two writes) is written off as failed after this long.
ORPHAN_RUN_S = 3_600.0
MAX_NAME_LENGTH = 80

_UNITS = (
    (604_800, "week"),
    (86_400, "day"),
    (3_600, "hour"),
    (60, "minute"),
    (1, "second"),
)


def new_mandate_id() -> str:
    return MANDATE_ID_PREFIX + uuid.uuid4().hex[:8]


def iso(ts: float | None) -> str | None:
    """Epoch seconds → ``2026-09-28T09:30:00Z`` (``None`` stays ``None``)."""
    if ts is None:
        return None
    return datetime.fromtimestamp(float(ts), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def schedule_label(every_seconds: int) -> str:
    """The card's label for an interval: ``86400`` → ``"every day"``.

    The largest unit that divides the interval exactly wins: ``21600`` →
    ``"every 6 hours"``, ``90`` → ``"every 90 seconds"``.
    """
    every = int(every_seconds)
    if every <= 0:
        raise ValueError("every_seconds must be positive")
    for size, unit in _UNITS:
        if every % size == 0:
            count = every // size
            return f"every {unit}" if count == 1 else f"every {count} {unit}s"
    return f"every {every} seconds"  # pragma: no cover - the 1 s unit always divides


def next_run_after(anchor: float, every: float, now: float) -> float:
    """``anchor + k * every`` for the smallest ``k >= 0`` that is after ``now``.

    Drift-free: a run that fired late does not push the schedule back, and a
    gap of several intervals comes out as the next slot, not a burst.
    """
    step = float(every)
    if step <= 0:
        raise ValueError("every must be positive")
    start = float(anchor)
    if now < start:
        return start
    k = math.floor((float(now) - start) / step) + 1
    candidate = start + k * step
    while candidate <= now:  # float rounding on the division
        k += 1
        candidate = start + k * step
    return candidate


def run_size(usd_per_run: float, cap: float, spent: float, reserved: float) -> float:
    """What the next buy may spend: ``usd_per_run``, or what is left of the cap.

    Rounded down to the cent, so a run never pushes the total past the cap;
    ``0.0`` when nothing is left.
    """
    left = float(cap) - float(spent) - float(reserved)
    size = min(float(usd_per_run), left)
    if size <= 0:
        return 0.0
    return math.floor(size * 100 + 1e-6) / 100


def dust_for(usd_per_run: float) -> float:
    """The remainder of the cap under which a mandate is done: ``min($0.50, half a buy)``.

    Rounded down to the cent (never under one cent) like :func:`run_size`, so
    ``run_size(...) < dust_for(...)`` holds exactly when the remainder itself
    is under it: a run skipped for the cap always finds the mandate complete.
    """
    dust = min(DUST_USD, float(usd_per_run) / 2)
    return max(0.01, math.floor(dust * 100 + 1e-6) / 100)


def run_note(name: str, buy: int, runs_max: int | None) -> str:
    """The order note: ``"DCA ETH · buy 3/30"`` (``∞`` without a run limit)."""
    return f"{name} · buy {buy}/{runs_max if runs_max else '∞'}"


def usd_text(value: float) -> str:
    """``150`` → ``"$150"``, ``3120.5`` → ``"$3,120.50"``, ``0.05`` → ``"$0.05"``."""
    number = float(value)
    if abs(number - round(number)) < 0.005:
        return f"${number:,.0f}"
    return f"${number:,.2f}"


def reason(code: str, detail: str | None = None) -> str:
    """How a run's reason is stored: ``"<code>: <detail>"`` (or just the code)."""
    return f"{code}: {detail}" if detail else code


def split_reason(text: str | None) -> tuple[str | None, str | None]:
    """A stored reason → ``(code, human detail)``."""
    if not text:
        return None, None
    head, sep, tail = str(text).partition(": ")
    if sep and head and " " not in head:
        return head, tail or head
    return None, str(text)


def pause_reason(runs: int, code: str | None, quote_symbol: str) -> str:
    """``statusReason`` of an auto-pause: ``"paused after 3 runs: insufficient USDC"``."""
    if code == "insufficient_balance":
        why = f"insufficient {quote_symbol or 'balance'}"
    elif code == "daily_cap":
        why = "daily cap reached"
    elif code:
        why = f"failed ({code})"
    else:
        why = "failed"
    return f"paused after {runs} runs: {why}"


def validate_terms(
    *,
    usd_per_run: float,
    cap_usd: float,
    runs_max: int | None,
    every_seconds: int,
    max_price_usd: float | None,
    creating: bool,
) -> str | None:
    """The first thing wrong with a mandate's terms, or ``None``."""
    if not _finite(usd_per_run) or usd_per_run <= 0:
        return "usdPerRun must be greater than zero"
    if usd_per_run < MIN_USD_PER_RUN - 1e-9:
        return f"usdPerRun must be at least {usd_text(MIN_USD_PER_RUN)}"
    if not _finite(cap_usd) or cap_usd <= 0:
        return "capUsd must be greater than zero"
    if creating and cap_usd < usd_per_run:
        return "capUsd must be at least usdPerRun"
    if runs_max is not None and int(runs_max) < 1:
        return "runsMax must be at least 1"
    if int(every_seconds) < MIN_EVERY_SECONDS:
        return f"everySeconds must be at least {MIN_EVERY_SECONDS}"
    if max_price_usd is not None and (not _finite(max_price_usd) or max_price_usd <= 0):
        return "maxPriceUsd must be greater than zero"
    return None


def completion_reason(
    *,
    usd_per_run: float,
    cap_usd: float,
    spent_usd: float,
    reserved_usd: float,
    runs_done: int,
    runs_max: int | None,
    in_flight: int,
) -> str | None:
    """``"cap reached"`` / ``"runs reached"`` when the mandate is done, else ``None``.

    Never while a buy is still open: a parked order that is later rejected
    would leave a completed mandate with money it was meant to spend.
    """
    if in_flight > 0 or reserved_usd >= 0.01:
        return None
    if runs_max is not None and runs_done >= int(runs_max):
        return "runs reached"
    if run_size(usd_per_run, cap_usd, spent_usd, 0.0) < dust_for(usd_per_run):
        return "cap reached"
    return None


# ── payload ────────────────────────────────────────────────────────────────


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def finite(value: Any) -> float | None:
    """A JSON-safe number: ``None`` for missing, NaN or infinite."""
    if value is None or not _finite(value):
        return None
    return float(value)


def _clean(value: Any) -> Any:
    """JSON-safe and free of float noise: 12 significant digits, no NaN/Infinity."""
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    if isinstance(value, float):
        number = finite(value)
        return float(f"{number:.12g}") if number is not None else None
    return value


#: Public name of :func:`_clean` for the other card modules (``triggers.py``).
clean = _clean


def chain_json(spec: ChainSpec) -> dict[str, Any]:
    """The LP cards' ``Chain`` (``docs/lp-cards.md``)."""
    return {"id": spec.chain_id, "key": spec.key, "name": spec.name, "explorer": spec.explorer_url}


def token_json(meta: Mapping[str, Any], price: float | None) -> dict[str, Any]:
    """The LP cards' ``Token``: address, symbol, decimals, priceUsd — and ``verified``.

    ``verified`` travels with the symbol so an approval view can tell the
    token a mandate or trigger names from another contract answering the
    same ``symbol()`` (WAL-08).
    """
    address = str(meta.get("address") or "")
    native = bool(meta.get("native"))
    return {
        "address": address if native or not address else checksum_address(address),
        "symbol": str(meta.get("symbol") or ""),
        "decimals": int(meta.get("decimals", 18)),
        "priceUsd": finite(price),
        "verified": bool(meta.get("verified")) or native,
    }


def amount_json(raw: int, decimals: int, price: float | None) -> dict[str, Any]:
    """The LP cards' ``Amount``: raw string, exact human string, USD (``0`` only for 0)."""
    raw = int(raw)
    if raw == 0:
        usd: float | None = 0.0
    elif price is None:
        usd = None
    else:
        usd = finite(float(to_human(raw, decimals)) * float(price))
    return {
        "raw": str(raw),
        "human": format_amount(raw, decimals, max_places=max(0, decimals)),
        "usd": usd,
    }


def wallet_json(address: str, label: str | None) -> dict[str, Any]:
    """The LP cards' ``Wallet``; ``inApp`` is whether the vault still holds it."""
    return {
        "address": checksum_address(address),
        "label": label or None,
        "inApp": label is not None,
    }


def run_payload(
    row: Mapping[str, Any],
    order_row: Mapping[str, Any] | None,
    *,
    chain: ChainSpec | None = None,
    token_decimals: int = 18,
    token_price: float | None = None,
    gas_usd: float | None = None,
) -> dict[str, Any]:
    """One ``Run`` of the payload from its row and (when it placed one) its order."""
    code, detail = split_reason(row.get("reason"))
    status = str(row["status"])
    usd = finite(row.get("usd"))
    price = finite(row.get("price_usd"))
    amount: dict[str, Any] | None = None
    tx_hash = order_row.get("tx_hash") if order_row else None
    if order_row is not None and status == "filled":
        received = int(order_row.get("received_out_raw") or 0)
        value = finite(order_row.get("value_usd"))
        amount = amount_json(received, token_decimals, token_price)
        if value is not None:
            usd = value
            human = float(to_human(received, token_decimals))
            price = value / human if human > 0 else price
    return {
        "n": int(row["n"]),
        "at": iso(float(row["at"])),
        "manual": bool(row.get("manual")),
        "status": status,
        "reason": detail,
        "reasonCode": code,
        "usd": usd,
        "amount": amount,
        "priceUsd": finite(price),
        "orderId": row.get("order_id"),
        "txHash": tx_hash,
        "explorerUrl": chain.tx_url(str(tx_hash)) if chain is not None and tx_hash else None,
        "gasUsd": finite(gas_usd),
    }


def mandate_json(
    row: Mapping[str, Any],
    *,
    chain: ChainSpec,
    wallet: dict[str, Any],
    token: Mapping[str, Any],
    quote: Mapping[str, Any],
    token_price: float | None,
    quote_price: float | None,
    spend: tuple[float, float, int, float],
    runs: list[dict[str, Any]],
    attempts: int,
    approval_threshold_usd: float,
    daily_cap_usd: float,
    default_slippage_pct: float | None,
) -> dict[str, Any]:
    """The ``Mandate`` object. ``runs`` are ready ``Run`` dicts, newest first."""
    spent, reserved, received_raw, gas = spend
    cap = float(row["cap_usd"])
    usd_per_run = float(row["usd_per_run"])
    decimals = int(token.get("decimals", 18))
    acquired = amount_json(received_raw, decimals, token_price)
    human = float(to_human(received_raw, decimals))
    avg = spent / human if human > 0 and spent > 0 else None
    current = finite(token_price)
    vs_avg = (current - avg) / avg * 100.0 if avg and current is not None else None
    unrealized = acquired["usd"] - spent if acquired["usd"] is not None else None
    slippage = row.get("slippage_pct")
    runs_max = row.get("runs_max")
    return {
        "id": row["mandate_id"],
        "name": row["name"],
        "status": row["status"],
        "statusReason": row.get("status_reason"),
        "chain": chain_json(chain),
        "wallet": wallet,
        "token": token_json(token, token_price),
        "quote": token_json(quote, quote_price),
        "schedule": {
            "everySeconds": int(row["every_seconds"]),
            "label": schedule_label(int(row["every_seconds"])),
            "startNow": bool(row.get("start_now", 1)),
            "anchorAt": iso(row.get("anchor_at")),
            "nextRunAt": iso(row.get("next_run_at")),
            "lastRunAt": iso(row.get("last_run_at")),
        },
        "budget": {
            "usdPerRun": usd_per_run,
            "capUsd": cap,
            "spentUsd": round(spent, 6),
            "reservedUsd": round(reserved, 6),
            "remainingUsd": round(max(0.0, cap - spent - reserved), 6),
            "progress": round(min(1.0, max(0.0, spent / cap)), 6) if cap > 0 else 0.0,
        },
        "runs": {
            "done": int(row.get("runs_done") or 0),
            "max": int(runs_max) if runs_max is not None else None,
            "skipped": int(row.get("runs_skipped") or 0),
            "failed": int(row.get("runs_failed") or 0),
            "attempts": int(attempts),
        },
        "guards": {
            "maxPriceUsd": finite(row.get("max_price_usd")),
            "approvalThresholdUsd": float(approval_threshold_usd),
            "dailyCapUsd": float(daily_cap_usd),
            "slippagePct": finite(slippage if slippage is not None else default_slippage_pct),
            "buysNeedApproval": usd_per_run > float(approval_threshold_usd),
        },
        "acquired": {
            "amount": acquired,
            "avgPriceUsd": finite(avg),
            "currentPriceUsd": current,
            "vsAvgPct": finite(vs_avg),
            "unrealizedUsd": finite(unrealized),
            "gasUsd": round(gas, 6),
        },
        "history": runs[:HISTORY_LIMIT],
        "initiator": row["initiator"],
        "sessionKey": row.get("session_key"),
        "createdAt": iso(float(row["created_at"])),
        "updatedAt": iso(float(row["updated_at"])),
        "approvedAt": iso(row.get("approved_at")),
        "expiresAt": iso(row.get("expires_at")),
    }


def mandate_warnings(
    row: Mapping[str, Any],
    *,
    token_symbol: str,
    quote_symbol: str,
    token_price: float | None,
    quote_balance_usd: float | None,
    spend: tuple[float, float, int, float],
    approval_threshold_usd: float,
) -> list[str]:
    """The one-line warnings a card shows above its footer."""
    if row["status"] not in LIVE_STATUSES:
        return []
    out: list[str] = []
    usd_per_run = float(row["usd_per_run"])
    if usd_per_run > float(approval_threshold_usd):
        out.append(
            f"each buy of {usd_text(usd_per_run)} is above the "
            f"{usd_text(approval_threshold_usd)} approval threshold and will wait for you"
        )
    if row.get("max_price_usd") is not None and token_price is None:
        out.append("price unknown: max-price guard cannot be checked")
    if quote_balance_usd is not None and usd_per_run > 0:
        spent, reserved, _, _ = spend
        left = max(0.0, float(row["cap_usd"]) - spent - reserved)
        worth_a_buy = run_size(usd_per_run, left, 0.0, 0.0) >= dust_for(usd_per_run)
        buys_left = math.ceil(left / usd_per_run - 1e-9) if worth_a_buy else 0
        runs_max = row.get("runs_max")
        if runs_max is not None:
            buys_left = min(buys_left, max(0, int(runs_max) - int(row.get("runs_done") or 0)))
        covers = math.floor(quote_balance_usd / usd_per_run + 1e-9)
        if covers < buys_left:
            noun = "buy" if covers == 1 else "buys"
            out.append(f"{quote_symbol or 'balance'} balance covers {covers} more {noun}")
    return out


def envelope(
    kind: str,
    *,
    fetched_at: float,
    warnings: Iterable[str] = (),
    request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "version": PAYLOAD_VERSION,
        "kind": kind,
        "fetchedAt": iso(fetched_at),
        "warnings": list(warnings),
    }
    if request is not None:
        out["request"] = request
    return out


def mandate_payload(
    mandate: dict[str, Any],
    *,
    fetched_at: float,
    warnings: Iterable[str] = (),
    run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """``kind = "mandate"``: one card; ``run`` only in the answer to a buy-now."""
    payload = envelope(
        "mandate",
        fetched_at=fetched_at,
        warnings=warnings,
        request={"kind": "get", "params": {"mandateId": mandate["id"]}},
    )
    payload["mandate"] = mandate
    if run is not None:
        payload["run"] = run
    cleaned: dict[str, Any] = _clean(payload)
    return cleaned


def mandates_payload(
    mandates: list[dict[str, Any]],
    *,
    fetched_at: float,
    all: bool = False,
    wallet: str | None = None,
    warnings: Iterable[str] = (),
) -> dict[str, Any]:
    """``kind = "mandates"``: live ones first, then newest first, with totals."""
    ordered = sorted(mandates, key=lambda m: str(m.get("createdAt") or ""), reverse=True)
    ordered.sort(key=lambda m: m["status"] not in LIVE_STATUSES)
    acquired: float | None = 0.0
    for m in ordered:
        usd = m["acquired"]["amount"]["usd"]
        if usd is None:
            acquired = None
            break
        acquired = (acquired or 0.0) + float(usd)
    params: dict[str, Any] = {"all": bool(all)}
    if wallet:
        params["wallet"] = wallet
    payload = envelope(
        "mandates",
        fetched_at=fetched_at,
        warnings=warnings,
        request={"kind": "list", "params": params},
    )
    payload["mandates"] = ordered
    payload["totals"] = {
        "count": len(ordered),
        "active": sum(1 for m in ordered if m["status"] == "active"),
        "spentUsd": round(sum((float(m["budget"]["spentUsd"]) for m in ordered), 0.0), 6),
        "capUsd": round(sum((float(m["budget"]["capUsd"]) for m in ordered), 0.0), 6),
        "acquiredUsd": round(acquired, 6) if acquired is not None else None,
    }
    cleaned: dict[str, Any] = _clean(payload)
    return cleaned
