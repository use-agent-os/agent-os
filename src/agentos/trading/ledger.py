"""SQLite ledger for wallets, tokens, history, lots, orders and limits.

Raw token amounts are stored as decimal strings (uint256 does not fit in a
SQLite integer). Sums happen in Python. The whole file can be deleted and
rebuilt from the chain by :mod:`agentos.trading.sync`; the only state that
is *not* recoverable from chain is the initiator of a swap (manual/agent)
and the order rows, which is why they live in the same database.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from agentos.paths import state_dir
from agentos.trading.chains import NATIVE_ADDRESS
from agentos.trading.pnl import Lot

log = structlog.get_logger(__name__)

SCHEMA_VERSION = 9

# Version 6 repair: a native receipt booked as a separate deposit is matched
# to its swap within this many seconds, and a phantom withdrawal to the swap
# it double-counts within the same window.
REPAIR_WINDOW_S = 120.0
# A repaired receipt may exceed the quote's expected amount by this much
# (positive slippage); more than that is not the same swap.
REPAIR_MAX_OVER_EXPECTED = 1.05
# A phantom withdrawal matches a swap's spent + gas within this fraction.
REPAIR_WITHDRAW_TOLERANCE = 0.01
REPAIR_PENDING_MESSAGE = "full sync required"

OPENING_NOTE = "opening balance"

ENTRY_KINDS = (
    "swap",
    "deposit",
    "withdraw",
    "approval",
    "gas",
    "unwrap",
    "lp_collect",
    "lp_remove",
    "lp_add",
)
# What an order does. A swap trades one token for another through a
# provider; a send moves one token to an address the user named; a revoke
# sets an ERC-20 allowance back to zero. Sends in one multisend share a
# batch_id and are decided together. The lp_* kinds are Uniswap V4 position
# writes (docs/lp-write.md): their plan lives in quote_json. A bridge moves
# one token to the other chain through Relay: the row is the origin side
# (chain_id, token_in/out, recipient = the Relay solver) and the destination
# side, quote and fill live in quote_json (docs/bridge.md).
LP_ORDER_KINDS = ("lp_collect", "lp_remove", "lp_add")
ORDER_KINDS = ("swap", "send", "revoke", "bridge", *LP_ORDER_KINDS)
# LP orders that bring tokens in: they spend nothing, so they never count
# toward an agent's daily cap while they are in flight.
_NON_SPENDING_KINDS = ("lp_collect", "lp_remove")
ORDER_STATUSES = (
    "quoted",
    "awaiting_approval",
    "approved",
    "rejected",
    "expired",
    "submitted",
    "confirmed",
    "failed",
)
ORDER_OPEN_STATUSES = frozenset({"awaiting_approval", "approved", "submitted"})
ORDER_FINAL_STATUSES = frozenset({"rejected", "expired", "confirmed", "failed"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS wallets (
    address TEXT PRIMARY KEY,
    label TEXT NOT NULL DEFAULT '',
    is_primary INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    created_block_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS tokens (
    chain_id INTEGER NOT NULL,
    address TEXT NOT NULL,
    symbol TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    decimals INTEGER NOT NULL DEFAULT 18,
    logo_url TEXT,
    is_native INTEGER NOT NULL DEFAULT 0,
    verified INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    -- Junk that landed in a wallet uninvited. Hidden tokens keep their
    -- ledger rows (the balance is real) but stay out of balances, portfolio,
    -- history and the sync's fast lane. hidden_by is 'auto' or 'user'; a
    -- user's choice is never overwritten by the classifier.
    hidden INTEGER NOT NULL DEFAULT 0,
    hidden_by TEXT,
    -- The wallet acted on this token on purpose (quote, swap, explicit
    -- resolve): it is never auto-hidden again.
    touched INTEGER NOT NULL DEFAULT 0,
    classified_at REAL,
    PRIMARY KEY (chain_id, address)
);

CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    chain_id INTEGER NOT NULL,
    wallet TEXT NOT NULL,
    kind TEXT NOT NULL,
    tx_hash TEXT,
    log_index INTEGER NOT NULL DEFAULT 0,
    block_number INTEGER,
    token_in TEXT,
    amount_in_raw TEXT,
    token_out TEXT,
    amount_out_raw TEXT,
    value_usd REAL,
    gas_usd REAL,
    price_in_usd REAL,
    price_out_usd REAL,
    cost_basis_source TEXT,
    initiator TEXT NOT NULL DEFAULT 'external',
    order_id TEXT,
    session_key TEXT,
    note TEXT,
    UNIQUE (wallet, kind, tx_hash, log_index)
);
CREATE INDEX IF NOT EXISTS idx_entries_wallet_ts ON entries (wallet, ts DESC);
CREATE INDEX IF NOT EXISTS idx_entries_chain_ts ON entries (chain_id, ts DESC);

CREATE TABLE IF NOT EXISTS lots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id INTEGER NOT NULL,
    wallet TEXT NOT NULL,
    token TEXT NOT NULL,
    amount_raw_remaining TEXT NOT NULL,
    cost_usd_per_raw REAL NOT NULL,
    acquired_at REAL NOT NULL,
    entry_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_lots_pos ON lots (chain_id, wallet, token);

CREATE TABLE IF NOT EXISTS realized (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id INTEGER,
    chain_id INTEGER NOT NULL,
    wallet TEXT NOT NULL,
    token TEXT NOT NULL,
    amount_raw TEXT NOT NULL,
    proceeds_usd REAL NOT NULL,
    cost_usd REAL NOT NULL,
    pnl_usd REAL NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_realized_pos ON realized (chain_id, wallet, token);

CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    chain_id INTEGER NOT NULL,
    wallet TEXT NOT NULL,
    token_in TEXT NOT NULL,
    token_out TEXT NOT NULL,
    amount_raw TEXT NOT NULL,
    amount_human TEXT NOT NULL,
    expected_out_raw TEXT,
    min_out_raw TEXT,
    value_usd REAL,
    price_impact_pct REAL,
    gas_usd REAL,
    status TEXT NOT NULL,
    reason TEXT,
    initiator TEXT NOT NULL,
    session_key TEXT,
    note TEXT,
    quote_json TEXT,
    slippage_pct REAL,
    tx_hash TEXT,
    approval_tx_hash TEXT,
    expires_at REAL,
    received_out_raw TEXT,
    spent_in_raw TEXT,
    gas_wei TEXT,
    delivered_token TEXT,
    provider TEXT,
    kind TEXT NOT NULL DEFAULT 'swap',
    -- send: where the tokens go; revoke: the spender losing its allowance.
    recipient TEXT,
    batch_id TEXT,
    -- A caller's own idempotency key: the same key from the same wallet
    -- (and, for a multisend, to the same recipient) is the same order.
    client_order_id TEXT,
    -- The DCA mandate that placed this order (docs/dca.md), if any.
    mandate_id TEXT,
    -- The price trigger that fired this order (docs/triggers.md), if any.
    trigger_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_wallet ON orders (wallet, created_at DESC);

-- Recurring buys the engine runs by itself (docs/dca.md). The cap, the
-- schedule and the stop rules live here, not in a prompt; spent_usd is a
-- cache of the confirmed value of the mandate's orders.
CREATE TABLE IF NOT EXISTS mandates (
    mandate_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'dca',
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    status_reason TEXT,
    chain_id INTEGER NOT NULL,
    wallet TEXT NOT NULL,
    token_out TEXT NOT NULL,
    token_in TEXT NOT NULL,
    usd_per_run REAL NOT NULL,
    cap_usd REAL NOT NULL,
    runs_max INTEGER,
    every_seconds INTEGER NOT NULL,
    max_price_usd REAL,
    slippage_pct REAL,
    start_now INTEGER NOT NULL DEFAULT 1,
    anchor_at REAL,
    next_run_at REAL,
    last_run_at REAL,
    spent_usd REAL NOT NULL DEFAULT 0,
    runs_done INTEGER NOT NULL DEFAULT 0,
    runs_skipped INTEGER NOT NULL DEFAULT 0,
    runs_failed INTEGER NOT NULL DEFAULT 0,
    bad_streak INTEGER NOT NULL DEFAULT 0,
    initiator TEXT NOT NULL,
    session_key TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    approved_at REAL,
    expires_at REAL
);
CREATE INDEX IF NOT EXISTS idx_mandates_status ON mandates (status, created_at DESC);

-- One row per attempt of a mandate, whatever came of it.
CREATE TABLE IF NOT EXISTS mandate_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    mandate_id TEXT NOT NULL,
    n INTEGER NOT NULL,
    at REAL NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    usd REAL,
    price_usd REAL,
    order_id TEXT,
    manual INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_mandate_runs ON mandate_runs (mandate_id, n DESC);

-- Conditional orders the engine watches and fires by itself (docs/triggers.md).
-- The condition, the size and the confirmation state live here; a fire is a
-- trigger_fires row written before its order.
CREATE TABLE IF NOT EXISTS triggers (
    trigger_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    status_reason TEXT,
    chain_id INTEGER NOT NULL,
    wallet TEXT NOT NULL,
    token TEXT NOT NULL,
    quote TEXT NOT NULL,
    direction TEXT NOT NULL,
    price_usd REAL,
    trail_pct REAL,
    from_price_usd REAL,
    amount_usd REAL,
    amount_pct REAL,
    amount_raw TEXT,
    slippage_pct REAL,
    confirm_ticks INTEGER NOT NULL DEFAULT 2,
    hits INTEGER NOT NULL DEFAULT 0,
    peak_price_usd REAL,
    armed_at REAL,
    armed_price_usd REAL,
    last_price_usd REAL,
    last_checked_at REAL,
    triggered_at REAL,
    fires_failed INTEGER NOT NULL DEFAULT 0,
    bad_streak INTEGER NOT NULL DEFAULT 0,
    valid_until REAL,
    initiator TEXT NOT NULL,
    session_key TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    approved_at REAL,
    expires_at REAL,
    -- A bracket's leg (docs/brackets.md): the group, its name, 'tp' | 'sl'.
    -- NULL for a plain trigger.
    group_id TEXT,
    group_name TEXT,
    leg TEXT
);
CREATE INDEX IF NOT EXISTS idx_triggers_status ON triggers (status, created_at DESC);

-- One row per attempt of a trigger (an alert, an order, a skip), manual or not.
CREATE TABLE IF NOT EXISTS trigger_fires (
    fire_id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger_id TEXT NOT NULL,
    n INTEGER NOT NULL,
    at REAL NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    price_usd REAL,
    order_id TEXT,
    manual INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_trigger_fires ON trigger_fires (trigger_id, n DESC);

-- ERC-20 allowances this wallet has granted, as seen in its Approval logs.
-- Only the (token, spender) pairs are remembered; the live amount is read
-- from the chain every time it is asked for, so a revoke never leaves a
-- stale "still approved" behind.
CREATE TABLE IF NOT EXISTS allowances (
    chain_id INTEGER NOT NULL,
    wallet TEXT NOT NULL,
    token TEXT NOT NULL,
    spender TEXT NOT NULL,
    first_block INTEGER NOT NULL,
    last_block INTEGER NOT NULL,
    last_tx_hash TEXT,
    PRIMARY KEY (chain_id, wallet, token, spender)
);
CREATE TABLE IF NOT EXISTS allowance_scan (
    chain_id INTEGER NOT NULL,
    wallet TEXT NOT NULL,
    last_block INTEGER NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (chain_id, wallet)
);

CREATE TABLE IF NOT EXISTS daily_spend (
    wallet TEXT NOT NULL,
    day TEXT NOT NULL,
    spent_usd REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (wallet, day)
);

CREATE TABLE IF NOT EXISTS sync_state (
    chain_id INTEGER NOT NULL,
    wallet TEXT NOT NULL,
    last_block INTEGER NOT NULL,
    oldest_block INTEGER,
    updated_at REAL NOT NULL,
    -- When the last *full* rebuild of this wallet/chain swapped in; NULL
    -- until one has. A data repair (ledger_repairs) is only settled once a
    -- rebuild newer than it has re-derived lots and realized rows.
    full_synced_at REAL,
    PRIMARY KEY (chain_id, wallet)
);

-- Migrations that rewrote booked data, one row per wallet/chain touched.
-- The derived rows (lots, realized) are not patched in place; a full sync
-- re-derives them, and repair_pending() nags until one has run.
CREATE TABLE IF NOT EXISTS ledger_repairs (
    version INTEGER NOT NULL,
    wallet TEXT NOT NULL,
    chain_id INTEGER NOT NULL,
    at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS price_snapshots (
    chain_id INTEGER NOT NULL,
    token TEXT NOT NULL,
    ts REAL NOT NULL,
    price_usd REAL NOT NULL,
    PRIMARY KEY (chain_id, token, ts)
);

CREATE TABLE IF NOT EXISTS balances (
    chain_id INTEGER NOT NULL,
    wallet TEXT NOT NULL,
    token TEXT NOT NULL,
    raw TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (chain_id, wallet, token)
);

-- How the last chain read of each wallet/chain went. A balance row is only
-- as fresh as this says: 'ok' means every token was re-read, 'partial' that
-- some reads failed and their rows are last-good, 'failed' that the node
-- could not be reached at all and every row on the chain is last-good.
CREATE TABLE IF NOT EXISTS chain_reads (
    chain_id INTEGER NOT NULL,
    wallet TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    read_at REAL NOT NULL,
    PRIMARY KEY (chain_id, wallet)
);

-- A full rebuild sweeps into these shadow tables and swaps in only when the
-- sweep is complete, so the live ledger keeps serving meanwhile and an
-- interrupted rebuild resumes from scanned_down_to instead of starting over.
CREATE TABLE IF NOT EXISTS rebuild_state (
    chain_id INTEGER NOT NULL,
    wallet TEXT NOT NULL,
    latest INTEGER NOT NULL,
    target_from INTEGER NOT NULL,
    scanned_down_to INTEGER NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (chain_id, wallet)
);
CREATE TABLE IF NOT EXISTS rebuild_logs (
    chain_id INTEGER NOT NULL,
    wallet TEXT NOT NULL,
    tx_hash TEXT NOT NULL,
    log_index INTEGER NOT NULL,
    block_number INTEGER NOT NULL,
    token TEXT NOT NULL,
    sender TEXT NOT NULL,
    recipient TEXT NOT NULL,
    amount TEXT NOT NULL,
    PRIMARY KEY (chain_id, wallet, tx_hash, log_index)
);
"""

# One opening balance per position: the amount the chain sweep cannot explain.
_OPENING_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_entries_opening ON entries "
    "(chain_id, wallet, token_out) WHERE tx_hash IS NULL AND note = 'opening balance'"
)
# Created after the version-4 migration has added the column it indexes.
_BATCH_INDEX = "CREATE INDEX IF NOT EXISTS idx_orders_batch ON orders (batch_id)"
# One order per (client key, wallet, recipient). The recipient is part of
# the key because a multisend is one client key fanned out over N rows for
# the same wallet; without it the second leg could never be inserted.
_CLIENT_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_client ON orders "
    "(client_order_id, wallet, COALESCE(recipient, '')) WHERE client_order_id IS NOT NULL"
)
# Created after the version-7 migration has added ``orders.mandate_id``.
_MANDATE_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_orders_mandate ON orders (mandate_id) "
    "WHERE mandate_id IS NOT NULL"
)
# Created after the version-8 migration has added ``orders.trigger_id``.
_TRIGGER_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_orders_trigger ON orders (trigger_id) "
    "WHERE trigger_id IS NOT NULL"
)
# Created after the version-9 migration has added ``triggers.group_id``.
_GROUP_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_triggers_group ON triggers (group_id) "
    "WHERE group_id IS NOT NULL"
)
#: ``list_triggers(legs=…)``: plain triggers only, bracket legs only, or both.
TRIGGER_LEG_FILTERS = ("exclude", "only", "all")
# A mandate run whose order is still open: waiting for the chain (pending)
# or for the user (parked).
RUN_OPEN_STATUSES = ("pending", "parked")
# A trigger fire whose order is still open, likewise.
FIRE_OPEN_STATUSES = ("pending", "parked")


def default_ledger_path() -> Path:
    return state_dir("trading.sqlite")


def new_order_id() -> str:
    return "ord_" + uuid.uuid4().hex[:12]


def new_batch_id() -> str:
    return "bat_" + uuid.uuid4().hex[:12]


def local_day(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts if ts is not None else time.time()))


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(r) for r in cursor.fetchall()]


@dataclass
class Position:
    chain_id: int
    wallet: str
    token: str
    amount_raw: int
    cost_usd: float


class Ledger:
    def __init__(self, path: str | Path | None = None) -> None:
        target = Path(path) if path is not None else default_ledger_path()
        self.path = target
        self._lock = threading.RLock()
        if str(target) != ":memory:":
            target.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            os.fspath(target), timeout=30.0, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        if str(target) != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._txn_depth = 0
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            row = self._conn.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
                )
            else:
                version = int(row["version"])
                if version < 2:
                    self._dedupe_openings()
                if version < 3:
                    self._add_token_columns()
                if version < 4:
                    self._add_order_kind_columns()
                if version < 5:
                    self._add_order_client_column()
                if version < 6:
                    self._add_full_synced_column()
                    self._repair_native_receipts()
                if version < 7:
                    self._add_order_mandate_column()
                if version < 8:
                    self._add_order_trigger_column()
                if version < 9:
                    self._add_trigger_group_columns()
                if version < SCHEMA_VERSION:
                    self._conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
            self._conn.execute(_OPENING_INDEX)
            self._conn.execute(_BATCH_INDEX)
            self._conn.execute(_CLIENT_INDEX)
            self._conn.execute(_MANDATE_INDEX)
            self._conn.execute(_TRIGGER_INDEX)
            self._conn.execute(_GROUP_INDEX)
            self._commit()

    def _add_token_columns(self) -> None:
        """Version 3: the hidden/touched columns on ``tokens``."""
        have = {str(r["name"]) for r in self._conn.execute("PRAGMA table_info(tokens)")}
        for column, ddl in (
            ("hidden", "INTEGER NOT NULL DEFAULT 0"),
            ("hidden_by", "TEXT"),
            ("touched", "INTEGER NOT NULL DEFAULT 0"),
            ("classified_at", "REAL"),
        ):
            if column not in have:
                self._conn.execute(f"ALTER TABLE tokens ADD COLUMN {column} {ddl}")  # noqa: S608

    def _add_order_kind_columns(self) -> None:
        """Version 4: sends and revokes share the orders table with swaps."""
        have = {str(r["name"]) for r in self._conn.execute("PRAGMA table_info(orders)")}
        for column, ddl in (
            ("kind", "TEXT NOT NULL DEFAULT 'swap'"),
            ("recipient", "TEXT"),
            ("batch_id", "TEXT"),
        ):
            if column not in have:
                self._conn.execute(f"ALTER TABLE orders ADD COLUMN {column} {ddl}")  # noqa: S608

    def _add_order_client_column(self) -> None:
        """Version 5: a caller-supplied idempotency key on ``orders``."""
        have = {str(r["name"]) for r in self._conn.execute("PRAGMA table_info(orders)")}
        if "client_order_id" not in have:
            self._conn.execute("ALTER TABLE orders ADD COLUMN client_order_id TEXT")

    def _add_order_mandate_column(self) -> None:
        """Version 7: the DCA mandate an order was placed by (``docs/dca.md``)."""
        have = {str(r["name"]) for r in self._conn.execute("PRAGMA table_info(orders)")}
        if "mandate_id" not in have:
            self._conn.execute("ALTER TABLE orders ADD COLUMN mandate_id TEXT")

    def _add_order_trigger_column(self) -> None:
        """Version 8: the price trigger an order was fired by (``docs/triggers.md``)."""
        have = {str(r["name"]) for r in self._conn.execute("PRAGMA table_info(orders)")}
        if "trigger_id" not in have:
            self._conn.execute("ALTER TABLE orders ADD COLUMN trigger_id TEXT")

    def _add_trigger_group_columns(self) -> None:
        """Version 9: a trigger may be one leg of a bracket (``docs/brackets.md``)."""
        have = {str(r["name"]) for r in self._conn.execute("PRAGMA table_info(triggers)")}
        for column in ("group_id", "group_name", "leg"):
            if column not in have:
                self._conn.execute(f"ALTER TABLE triggers ADD COLUMN {column} TEXT")  # noqa: S608

    def _add_full_synced_column(self) -> None:
        """Version 6: when the last full rebuild of a wallet/chain landed."""
        have = {str(r["name"]) for r in self._conn.execute("PRAGMA table_info(sync_state)")}
        if "full_synced_at" not in have:
            self._conn.execute("ALTER TABLE sync_state ADD COLUMN full_synced_at REAL")

    def _repair_native_receipts(self) -> None:
        """Version 6: undo two ways the native leg of an own swap was double-booked.

        Before the fix, a swap *into* native could settle with a bogus
        ``received_out_raw`` (a stray number such as the gas paid) while the
        real receipt was booked seconds later, by the native reconciliation,
        as an external deposit with no tx hash. The lot for that deposit
        then carried the swap's whole cost, so the position looked bought at
        tens of thousands of dollars per ETH. Here the deposit is folded back
        into the swap (received = deposit + gas) when the sum lands between
        the quote's minimum and its expected amount plus slippage.

        The mirror image: a swap *out of* native was followed by a phantom
        external withdrawal equal to what the swap spent plus its gas. Those
        rows go too.

        Only ``orders`` and ``entries`` are corrected; ``lots`` and
        ``realized`` derived from them are left for the full rebuild, which
        re-derives everything from the orders and the chain. The repaired
        wallet/chains are recorded in ``ledger_repairs`` so
        :meth:`repair_pending` can say so until that rebuild has run.
        Idempotent: a repaired order no longer matches, a deleted row is gone.
        """
        repaired: set[tuple[str, int]] = set()
        with self.transaction():
            for order in _rows(
                self._conn.execute(
                    "SELECT * FROM orders WHERE kind = 'swap' AND status = 'confirmed' "
                    "AND token_out = ? AND COALESCE(delivered_token, ?) = ? "
                    "AND received_out_raw IS NOT NULL AND min_out_raw IS NOT NULL",
                    (NATIVE_ADDRESS, NATIVE_ADDRESS, NATIVE_ADDRESS),
                )
            ):
                if self._repair_native_receipt(order):
                    repaired.add((str(order["wallet"]), int(order["chain_id"])))
            for row in _rows(
                self._conn.execute(
                    "SELECT * FROM entries WHERE kind = 'withdraw' AND tx_hash IS NULL "
                    "AND token_in = ? AND note IS NULL ORDER BY ts, id",
                    (NATIVE_ADDRESS,),
                )
            ):
                if self._repair_phantom_withdraw(row):
                    repaired.add((str(row["wallet"]), int(row["chain_id"])))
            now = time.time()
            for wallet, chain_id in sorted(repaired):
                self._conn.execute(
                    "INSERT INTO ledger_repairs (version, wallet, chain_id, at) "
                    "VALUES (?, ?, ?, ?)",
                    (6, wallet, chain_id, now),
                )
        if repaired:
            log.warning(
                "trading.ledger_repaired",
                version=6,
                positions=[f"{w}@{c}" for w, c in sorted(repaired)],
                action="run `agentos trade sync --full` to re-derive lots and realized P&L",
            )

    def _repair_native_receipt(self, order: dict[str, Any]) -> bool:
        received_before = int(order["received_out_raw"])
        min_out = int(order["min_out_raw"])
        if received_before >= min_out:
            return False
        order_id = str(order["order_id"])
        swap = self._conn.execute(
            "SELECT id, ts FROM entries WHERE order_id = ? AND kind = 'swap' ORDER BY id LIMIT 1",
            (order_id,),
        ).fetchone()
        if swap is None:
            log.warning("trading.ledger_repair_skipped", order=order_id, why="no swap entry")
            return False
        deposit = self._conn.execute(
            "SELECT id, amount_out_raw FROM entries WHERE kind = 'deposit' AND tx_hash IS NULL "
            "AND token_out = ? AND note IS NULL AND wallet = ? AND chain_id = ? "
            "AND ts >= ? AND ts <= ? ORDER BY ts, id LIMIT 1",
            (
                NATIVE_ADDRESS,
                str(order["wallet"]),
                int(order["chain_id"]),
                float(swap["ts"]),
                float(swap["ts"]) + REPAIR_WINDOW_S,
            ),
        ).fetchone()
        if deposit is None:
            log.warning("trading.ledger_repair_skipped", order=order_id, why="no deposit")
            return False
        received = int(deposit["amount_out_raw"]) + int(order.get("gas_wei") or 0)
        expected = int(order.get("expected_out_raw") or 0)
        ceiling = expected * REPAIR_MAX_OVER_EXPECTED if expected > 0 else None
        if received < min_out or (ceiling is not None and received > ceiling):
            log.warning(
                "trading.ledger_repair_skipped",
                order=order_id,
                why="deposit + gas outside the quote",
                received=received,
                min_out=min_out,
                expected=expected,
            )
            return False
        self._conn.execute(
            "UPDATE orders SET received_out_raw = ? WHERE order_id = ?",
            (str(received), order_id),
        )
        self._conn.execute(
            "UPDATE entries SET amount_out_raw = ? WHERE id = ?",
            (str(received), int(swap["id"])),
        )
        self._delete_entry_rows(int(deposit["id"]))
        log.info(
            "trading.ledger_repaired_receipt",
            order=order_id,
            received_before=received_before,
            received=received,
            deposit_entry=int(deposit["id"]),
        )
        return True

    def _repair_phantom_withdraw(self, row: dict[str, Any]) -> bool:
        amount = int(row.get("amount_in_raw") or 0)
        if amount <= 0:
            return False
        ts = float(row["ts"])
        swaps = _rows(
            self._conn.execute(
                "SELECT e.id AS entry_id, e.order_id, o.spent_in_raw, o.gas_wei FROM entries e "
                "JOIN orders o ON o.order_id = e.order_id "
                "WHERE e.kind = 'swap' AND e.wallet = ? AND e.chain_id = ? AND e.token_in = ? "
                "AND e.ts >= ? AND e.ts <= ? ORDER BY e.ts DESC, e.id DESC",
                (
                    str(row["wallet"]),
                    int(row["chain_id"]),
                    NATIVE_ADDRESS,
                    ts - REPAIR_WINDOW_S,
                    ts,
                ),
            )
        )
        for swap in swaps:
            expected = int(swap.get("spent_in_raw") or 0) + int(swap.get("gas_wei") or 0)
            if expected <= 0:
                continue
            if abs(amount - expected) <= expected * REPAIR_WITHDRAW_TOLERANCE:
                self._delete_entry_rows(int(row["id"]))
                log.info(
                    "trading.ledger_repaired_withdraw",
                    entry=int(row["id"]),
                    order=swap["order_id"],
                    amount=amount,
                    swap_total=expected,
                )
                return True
        return False

    def repair_pending(self) -> str | None:
        """Why the operator should run a full sync, or ``None``.

        A data migration that rewrote booked rows leaves the derived lots
        and realized rows stale until a full rebuild newer than the repair
        has swapped in for every wallet/chain it touched.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM ledger_repairs r LEFT JOIN sync_state s "
                "ON s.wallet = r.wallet AND s.chain_id = r.chain_id "
                "WHERE s.full_synced_at IS NULL OR s.full_synced_at < r.at LIMIT 1"
            ).fetchone()
        return REPAIR_PENDING_MESSAGE if row is not None else None

    def _dedupe_openings(self) -> None:
        """Version 1 could book the same opening twice; keep the newest per position."""
        rows = _rows(
            self._conn.execute(
                "SELECT id, chain_id, wallet, token_out FROM entries WHERE tx_hash IS NULL "
                "AND note = ? ORDER BY ts DESC, id DESC",
                (OPENING_NOTE,),
            )
        )
        seen: set[tuple[int, str, str]] = set()
        for r in rows:
            key = (int(r["chain_id"]), str(r["wallet"]), str(r["token_out"]))
            if key in seen:
                self._delete_entry_rows(int(r["id"]))
            seen.add(key)

    def _commit(self) -> None:
        """Commit unless a :meth:`transaction` is open, which commits at its end."""
        if self._txn_depth == 0 and self._conn.in_transaction:
            self._conn.commit()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Group writes so they land together or not at all.

        Nested use joins the outer transaction. Any exception rolls the whole
        group back, and so does the process dying: SQLite never exposes a
        half-written group.
        """
        with self._lock:
            if self._txn_depth == 0:
                self._conn.execute("BEGIN IMMEDIATE")
            self._txn_depth += 1
            try:
                yield
            except BaseException:
                self._txn_depth -= 1
                if self._txn_depth == 0:
                    self._conn.rollback()
                raise
            else:
                self._txn_depth -= 1
                if self._txn_depth == 0:
                    self._commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── wallets ────────────────────────────────────────────────────────

    def upsert_wallet(
        self,
        address: str,
        *,
        label: str,
        is_primary: bool,
        created_at: float,
        created_block: dict[str, int] | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO wallets (address, label, is_primary, created_at, created_block_json) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(address) DO UPDATE SET label=excluded.label, "
                "is_primary=excluded.is_primary, created_block_json=excluded.created_block_json",
                (
                    address.lower(),
                    label,
                    1 if is_primary else 0,
                    created_at,
                    json.dumps(created_block or {}),
                ),
            )
            self._commit()

    def remove_wallet(self, address: str) -> None:
        key = address.lower()
        with self._lock:
            # Every wallet-keyed table: a re-import of the same address must
            # not inherit stale orders (which sync would re-book) or spend.
            for table in (
                "wallets",
                "entries",
                "lots",
                "realized",
                "sync_state",
                "balances",
                "chain_reads",
                "orders",
                "daily_spend",
                "rebuild_state",
                "rebuild_logs",
                "allowances",
                "allowance_scan",
                "ledger_repairs",
            ):
                column = "address" if table == "wallets" else "wallet"
                self._conn.execute(f"DELETE FROM {table} WHERE {column} = ?", (key,))  # noqa: S608
            self._commit()

    # ── tokens ─────────────────────────────────────────────────────────

    def upsert_token(
        self,
        chain_id: int,
        address: str,
        *,
        symbol: str,
        name: str,
        decimals: int,
        logo_url: str | None = None,
        is_native: bool = False,
        verified: bool = False,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO tokens (chain_id, address, symbol, name, decimals, logo_url, "
                "is_native, verified, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(chain_id, address) DO UPDATE SET "
                "symbol=CASE WHEN excluded.symbol != '' THEN excluded.symbol "
                "ELSE tokens.symbol END, "
                "name=CASE WHEN excluded.name != '' THEN excluded.name ELSE tokens.name END, "
                "decimals=excluded.decimals, "
                "logo_url=COALESCE(excluded.logo_url, tokens.logo_url), "
                "is_native=excluded.is_native, verified=MAX(tokens.verified, excluded.verified), "
                "updated_at=excluded.updated_at",
                (
                    chain_id,
                    address.lower(),
                    symbol,
                    name,
                    int(decimals),
                    logo_url,
                    1 if is_native else 0,
                    1 if verified else 0,
                    time.time(),
                ),
            )
            self._commit()

    def get_token(self, chain_id: int, address: str) -> dict[str, Any] | None:
        with self._lock:
            return _row(
                self._conn.execute(
                    "SELECT * FROM tokens WHERE chain_id = ? AND address = ?",
                    (chain_id, address.lower()),
                ).fetchone()
            )

    def tokens(self, chain_id: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if chain_id is None:
                return _rows(self._conn.execute("SELECT * FROM tokens"))
            return _rows(self._conn.execute("SELECT * FROM tokens WHERE chain_id = ?", (chain_id,)))

    # ── hidden tokens ──────────────────────────────────────────────────

    def hidden_tokens(self, chain_id: int | None = None) -> set[str]:
        """Addresses of every hidden token (on one chain, or all)."""
        with self._lock:
            if chain_id is None:
                rows = self._conn.execute("SELECT address FROM tokens WHERE hidden = 1")
            else:
                rows = self._conn.execute(
                    "SELECT address FROM tokens WHERE hidden = 1 AND chain_id = ?", (chain_id,)
                )
            return {str(r["address"]) for r in rows.fetchall()}

    def set_token_hidden(
        self, chain_id: int, address: str, hidden: bool, *, by: str, classified_at: float
    ) -> None:
        """Hide or show a token. ``by`` is ``'auto'`` (classifier) or ``'user'``.

        The classifier never overrides a user's choice; a user always can.
        """
        with self._lock:
            if by == "auto":
                self._conn.execute(
                    "UPDATE tokens SET hidden = ?, hidden_by = ?, classified_at = ? "
                    "WHERE chain_id = ? AND address = ? AND COALESCE(hidden_by, '') != 'user'",
                    (
                        1 if hidden else 0,
                        "auto" if hidden else None,
                        classified_at,
                        chain_id,
                        address.lower(),
                    ),
                )
            else:
                self._conn.execute(
                    "UPDATE tokens SET hidden = ?, hidden_by = 'user', classified_at = ? "
                    "WHERE chain_id = ? AND address = ?",
                    (1 if hidden else 0, classified_at, chain_id, address.lower()),
                )
            self._commit()

    def touch_token(self, chain_id: int, address: str) -> None:
        """The wallet acted on this token on purpose: it is shown, and stays shown.

        Unless the *user* hid it: a deliberate hide outranks a deliberate
        trade, the same way it outranks the classifier. The touch is still
        recorded so the classifier never gets to re-decide the token.
        """
        with self._lock:
            self._conn.execute(
                "UPDATE tokens SET touched = 1 WHERE chain_id = ? AND address = ?",
                (chain_id, address.lower()),
            )
            self._conn.execute(
                "UPDATE tokens SET hidden = 0, hidden_by = NULL "
                "WHERE chain_id = ? AND address = ? AND COALESCE(hidden_by, '') != 'user'",
                (chain_id, address.lower()),
            )
            self._commit()

    def token_was_spent(self, chain_id: int, address: str) -> bool:
        """Did any wallet ever send, sell or order this token? Junk only ever arrives."""
        key = address.lower()
        with self._lock:
            entry = self._conn.execute(
                "SELECT 1 FROM entries WHERE chain_id = ? AND token_in = ? LIMIT 1",
                (chain_id, key),
            ).fetchone()
            if entry is not None:
                return True
            order = self._conn.execute(
                "SELECT 1 FROM orders WHERE chain_id = ? AND (token_in = ? OR token_out = ?) "
                "LIMIT 1",
                (chain_id, key, key),
            ).fetchone()
            return order is not None

    # ── entries ────────────────────────────────────────────────────────

    def insert_entry(self, **fields: Any) -> int | None:
        """Insert one history entry; ``None`` when the same event already exists."""
        columns = [
            "ts",
            "chain_id",
            "wallet",
            "kind",
            "tx_hash",
            "log_index",
            "block_number",
            "token_in",
            "amount_in_raw",
            "token_out",
            "amount_out_raw",
            "value_usd",
            "gas_usd",
            "price_in_usd",
            "price_out_usd",
            "cost_basis_source",
            "initiator",
            "order_id",
            "session_key",
            "note",
        ]
        values = []
        for column in columns:
            value = fields.get(column)
            if column in {"amount_in_raw", "amount_out_raw"} and value is not None:
                value = str(int(value))
            if column in {"wallet", "token_in", "token_out", "tx_hash"} and isinstance(value, str):
                value = value.lower()
            if column == "initiator" and value is None:
                value = "external"
            if column == "log_index" and value is None:
                value = 0
            values.append(value)
        with self._lock:
            cursor = self._conn.execute(
                f"INSERT OR IGNORE INTO entries ({', '.join(columns)}) "  # noqa: S608
                f"VALUES ({', '.join('?' for _ in columns)})",
                values,
            )
            if cursor.rowcount == 0:
                return None
            self._commit()
            return int(cursor.lastrowid or 0)

    def entry_exists(self, wallet: str, kind: str, tx_hash: str | None, log_index: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM entries WHERE wallet = ? AND kind = ? AND tx_hash IS ? "
                "AND log_index = ?",
                (wallet.lower(), kind, tx_hash.lower() if tx_hash else None, log_index),
            ).fetchone()
            return row is not None

    def entries_for_tx(self, wallet: str, tx_hash: str) -> list[dict[str, Any]]:
        with self._lock:
            return _rows(
                self._conn.execute(
                    "SELECT * FROM entries WHERE wallet = ? AND tx_hash = ?",
                    (wallet.lower(), tx_hash.lower()),
                )
            )

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        with self._lock:
            return _row(
                self._conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
            )

    def update_entry(self, entry_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE entries SET {sets} WHERE id = ?",  # noqa: S608
                [*fields.values(), entry_id],
            )
            self._commit()

    def list_entries(
        self,
        *,
        wallet: str | None = None,
        chain_id: int | None = None,
        kind: str | None = None,
        limit: int = 100,
        before: float | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["1=1"]
        params: list[Any] = []
        if wallet:
            clauses.append("wallet = ?")
            params.append(wallet.lower())
        if chain_id is not None:
            clauses.append("chain_id = ?")
            params.append(chain_id)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if before is not None:
            clauses.append("ts < ?")
            params.append(before)
        params.append(max(1, min(int(limit), 1000)))
        with self._lock:
            return _rows(
                self._conn.execute(
                    f"SELECT * FROM entries WHERE {' AND '.join(clauses)} "  # noqa: S608
                    "ORDER BY ts DESC, id DESC LIMIT ?",
                    params,
                )
            )

    def opening_entry(self, chain_id: int, wallet: str, token: str) -> dict[str, Any] | None:
        with self._lock:
            return _row(
                self._conn.execute(
                    "SELECT * FROM entries WHERE chain_id = ? AND wallet = ? AND token_out = ? "
                    "AND tx_hash IS NULL AND note = ?",
                    (chain_id, wallet.lower(), token.lower(), OPENING_NOTE),
                ).fetchone()
            )

    def _delete_entry_rows(self, entry_id: int) -> None:
        self._conn.execute("DELETE FROM lots WHERE entry_id = ?", (entry_id,))
        self._conn.execute("DELETE FROM realized WHERE entry_id = ?", (entry_id,))
        self._conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))

    def delete_entry(self, entry_id: int) -> None:
        """Remove one entry with the lot and realized rows it produced."""
        with self._lock:
            self._delete_entry_rows(entry_id)
            self._commit()

    def token_entries(self, chain_id: int, wallet: str, token: str) -> list[dict[str, Any]]:
        """Every entry that moved ``token`` in this position, oldest first."""
        with self._lock:
            return _rows(
                self._conn.execute(
                    "SELECT * FROM entries WHERE chain_id = ? AND wallet = ? "
                    "AND (token_in = ? OR token_out = ?) ORDER BY ts, id",
                    (chain_id, wallet.lower(), token.lower(), token.lower()),
                )
            )

    def scanned_net_raw(self, chain_id: int, wallet: str, token: str) -> int:
        """Inbound minus outbound raw for ``token`` from every entry except the opening."""
        net = 0
        for e in self.token_entries(chain_id, wallet, token):
            if e["tx_hash"] is None and e.get("note") == OPENING_NOTE:
                continue
            if e.get("token_out") == token.lower() and e.get("amount_out_raw"):
                net += int(e["amount_out_raw"])
            if e.get("token_in") == token.lower() and e.get("amount_in_raw"):
                net -= int(e["amount_in_raw"])
        return net

    def reset_token_lots(self, chain_id: int, wallet: str, token: str) -> None:
        """Drop the derived lot/realized rows of one position ahead of a replay."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM lots WHERE chain_id = ? AND wallet = ? AND token = ?",
                (chain_id, wallet.lower(), token.lower()),
            )
            self._conn.execute(
                "DELETE FROM realized WHERE chain_id = ? AND wallet = ? AND token = ?",
                (chain_id, wallet.lower(), token.lower()),
            )
            self._commit()

    def confirmed_orders(self, wallet: str, chain_id: int) -> list[dict[str, Any]]:
        """Our own settled orders of every kind, oldest first.

        The part of history the chain cannot tell us. Callers must look at
        ``kind``: a send is a withdrawal and a revoke moves nothing but gas,
        so neither may be replayed as a swap.
        """
        with self._lock:
            return _rows(
                self._conn.execute(
                    "SELECT * FROM orders WHERE wallet = ? AND chain_id = ? "
                    "AND status = 'confirmed' AND tx_hash IS NOT NULL "
                    "ORDER BY updated_at, order_id",
                    (wallet.lower(), chain_id),
                )
            )

    # ── rebuild shadow ─────────────────────────────────────────────────

    def rebuild_state(self, chain_id: int, wallet: str) -> dict[str, Any] | None:
        with self._lock:
            return _row(
                self._conn.execute(
                    "SELECT * FROM rebuild_state WHERE chain_id = ? AND wallet = ?",
                    (chain_id, wallet.lower()),
                ).fetchone()
            )

    def set_rebuild_state(
        self, chain_id: int, wallet: str, *, latest: int, target_from: int, scanned_down_to: int
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO rebuild_state (chain_id, wallet, latest, target_from, "
                "scanned_down_to, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(chain_id, wallet) DO UPDATE SET latest = excluded.latest, "
                "target_from = excluded.target_from, scanned_down_to = excluded.scanned_down_to, "
                "updated_at = excluded.updated_at",
                (
                    chain_id,
                    wallet.lower(),
                    int(latest),
                    int(target_from),
                    int(scanned_down_to),
                    time.time(),
                ),
            )
            self._commit()

    def add_rebuild_logs(self, chain_id: int, wallet: str, logs: list[dict[str, Any]]) -> None:
        with self._lock:
            self._conn.executemany(
                "INSERT OR IGNORE INTO rebuild_logs (chain_id, wallet, tx_hash, log_index, "
                "block_number, token, sender, recipient, amount) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        chain_id,
                        wallet.lower(),
                        str(item["tx_hash"]).lower(),
                        int(item["log_index"]),
                        int(item["block_number"]),
                        str(item["token"]).lower(),
                        str(item["sender"]).lower(),
                        str(item["recipient"]).lower(),
                        str(int(item["amount"])),
                    )
                    for item in logs
                ],
            )
            self._commit()

    def rebuild_logs(self, chain_id: int, wallet: str) -> list[dict[str, Any]]:
        with self._lock:
            return _rows(
                self._conn.execute(
                    "SELECT * FROM rebuild_logs WHERE chain_id = ? AND wallet = ? "
                    "ORDER BY block_number, log_index",
                    (chain_id, wallet.lower()),
                )
            )

    def clear_rebuild(self, chain_id: int, wallet: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM rebuild_logs WHERE chain_id = ? AND wallet = ?",
                (chain_id, wallet.lower()),
            )
            self._conn.execute(
                "DELETE FROM rebuild_state WHERE chain_id = ? AND wallet = ?",
                (chain_id, wallet.lower()),
            )
            self._commit()

    def delete_chain_history(self, wallet: str, chain_id: int) -> None:
        """Drop chain-derived rows for one wallet/chain so a full resync can rebuild them."""
        key = wallet.lower()
        with self._lock:
            self._conn.execute(
                "DELETE FROM entries WHERE wallet = ? AND chain_id = ?", (key, chain_id)
            )
            self._conn.execute(
                "DELETE FROM lots WHERE wallet = ? AND chain_id = ?", (key, chain_id)
            )
            self._conn.execute(
                "DELETE FROM realized WHERE wallet = ? AND chain_id = ?", (key, chain_id)
            )
            self._conn.execute(
                "DELETE FROM sync_state WHERE wallet = ? AND chain_id = ?", (key, chain_id)
            )
            self._conn.execute(
                "DELETE FROM balances WHERE wallet = ? AND chain_id = ?", (key, chain_id)
            )
            self._commit()

    # ── lots / realized ────────────────────────────────────────────────

    def add_lot(
        self,
        chain_id: int,
        wallet: str,
        token: str,
        *,
        amount_raw: int,
        cost_usd_per_raw: float,
        acquired_at: float,
        entry_id: int | None,
    ) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO lots (chain_id, wallet, token, amount_raw_remaining, "
                "cost_usd_per_raw, acquired_at, entry_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    chain_id,
                    wallet.lower(),
                    token.lower(),
                    str(int(amount_raw)),
                    float(cost_usd_per_raw),
                    acquired_at,
                    entry_id,
                ),
            )
            self._commit()
            return int(cursor.lastrowid or 0)

    def open_lots(self, chain_id: int, wallet: str, token: str) -> list[Lot]:
        with self._lock:
            rows = _rows(
                self._conn.execute(
                    "SELECT * FROM lots WHERE chain_id = ? AND wallet = ? AND token = ? "
                    "AND CAST(amount_raw_remaining AS INTEGER) != 0 ORDER BY acquired_at, id",
                    (chain_id, wallet.lower(), token.lower()),
                )
            )
        return [
            Lot(
                lot_id=int(r["id"]),
                amount_raw=int(r["amount_raw_remaining"]),
                cost_usd_per_raw=float(r["cost_usd_per_raw"]),
                acquired_at=float(r["acquired_at"]),
            )
            for r in rows
            if int(r["amount_raw_remaining"]) > 0
        ]

    def save_lots(self, lots: list[Lot]) -> None:
        with self._lock:
            for lot in lots:
                if lot.lot_id is None:
                    continue
                self._conn.execute(
                    "UPDATE lots SET amount_raw_remaining = ? WHERE id = ?",
                    (str(max(0, int(lot.amount_raw))), lot.lot_id),
                )
            self._commit()

    def lots_for_entry(self, entry_id: int) -> list[dict[str, Any]]:
        with self._lock:
            return _rows(self._conn.execute("SELECT * FROM lots WHERE entry_id = ?", (entry_id,)))

    def update_lot_cost(self, lot_id: int, cost_usd_per_raw: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE lots SET cost_usd_per_raw = ? WHERE id = ?",
                (float(cost_usd_per_raw), lot_id),
            )
            self._commit()

    def positions(self, wallet: str | None = None) -> list[Position]:
        clause = "WHERE wallet = ?" if wallet else ""
        params: list[Any] = [wallet.lower()] if wallet else []
        with self._lock:
            rows = _rows(
                self._conn.execute(
                    f"SELECT chain_id, wallet, token, amount_raw_remaining, cost_usd_per_raw "  # noqa: S608
                    f"FROM lots {clause}",
                    params,
                )
            )
        agg: dict[tuple[int, str, str], Position] = {}
        for r in rows:
            amount = int(r["amount_raw_remaining"])
            if amount <= 0:
                continue
            key = (int(r["chain_id"]), str(r["wallet"]), str(r["token"]))
            pos = agg.get(key)
            if pos is None:
                pos = Position(key[0], key[1], key[2], 0, 0.0)
                agg[key] = pos
            pos.amount_raw += amount
            pos.cost_usd += amount * float(r["cost_usd_per_raw"])
        return list(agg.values())

    def add_realized(
        self,
        *,
        entry_id: int | None,
        chain_id: int,
        wallet: str,
        token: str,
        amount_raw: int,
        proceeds_usd: float,
        cost_usd: float,
        ts: float,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO realized (entry_id, chain_id, wallet, token, amount_raw, "
                "proceeds_usd, cost_usd, pnl_usd, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry_id,
                    chain_id,
                    wallet.lower(),
                    token.lower(),
                    str(int(amount_raw)),
                    float(proceeds_usd),
                    float(cost_usd),
                    float(proceeds_usd) - float(cost_usd),
                    ts,
                ),
            )
            self._commit()

    def realized_by_position(self, wallet: str | None = None) -> dict[tuple[int, str, str], float]:
        clause = "WHERE wallet = ?" if wallet else ""
        params: list[Any] = [wallet.lower()] if wallet else []
        with self._lock:
            rows = _rows(
                self._conn.execute(
                    f"SELECT chain_id, wallet, token, SUM(pnl_usd) AS pnl FROM realized {clause} "  # noqa: S608
                    "GROUP BY chain_id, wallet, token",
                    params,
                )
            )
        return {
            (int(r["chain_id"]), str(r["wallet"]), str(r["token"])): float(r["pnl"] or 0)
            for r in rows
        }

    def gas_total(self, wallet: str | None = None) -> float:
        clause = "WHERE wallet = ?" if wallet else ""
        params: list[Any] = [wallet.lower()] if wallet else []
        with self._lock:
            row = self._conn.execute(
                f"SELECT SUM(gas_usd) AS gas FROM entries {clause}",  # noqa: S608
                params,
            ).fetchone()
        return float(row["gas"] or 0.0) if row else 0.0

    # ── orders ─────────────────────────────────────────────────────────

    def insert_order(self, order: dict[str, Any]) -> None:
        """Insert an order row; a mandate's or trigger's order is tied to its run in the same write.

        A DCA run inserts its ``pending`` run row before it places the order
        (``docs/dca.md``), a trigger its ``pending`` fire (``docs/triggers.md``);
        the order that run or fire creates adopts it here, so the link between
        the two survives a crash between the insert and the pipeline's first
        answer.
        """
        columns = list(order.keys())
        with self.transaction():
            self._conn.execute(
                f"INSERT INTO orders ({', '.join(columns)}) "  # noqa: S608
                f"VALUES ({', '.join('?' for _ in columns)})",
                [order[c] for c in columns],
            )
            if order.get("mandate_id"):
                self._conn.execute(
                    "UPDATE mandate_runs SET order_id = ? WHERE run_id = ("
                    "SELECT run_id FROM mandate_runs WHERE mandate_id = ? "
                    "AND status = 'pending' AND order_id IS NULL "
                    "ORDER BY run_id DESC LIMIT 1)",
                    (order["order_id"], order["mandate_id"]),
                )
            if order.get("trigger_id"):
                self._conn.execute(
                    "UPDATE trigger_fires SET order_id = ? WHERE fire_id = ("
                    "SELECT fire_id FROM trigger_fires WHERE trigger_id = ? "
                    "AND status = 'pending' AND order_id IS NULL "
                    "ORDER BY fire_id DESC LIMIT 1)",
                    (order["order_id"], order["trigger_id"]),
                )

    def update_order(
        self, order_id: str, *, expect_status: str | None = None, **fields: Any
    ) -> dict[str, Any] | None:
        """Update an order; with ``expect_status`` it is a compare-and-set.

        Two approvals of the same order used to both pass the status check
        and both send a transaction. With ``expect_status`` the UPDATE only
        lands when the row is still in that status; the loser gets ``None``
        and must not proceed.
        """
        fields["updated_at"] = time.time()
        sets = ", ".join(f"{k} = ?" for k in fields)
        where = "order_id = ?"
        params: list[Any] = [*fields.values(), order_id]
        if expect_status is not None:
            where += " AND status = ?"
            params.append(expect_status)
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE orders SET {sets} WHERE {where}",  # noqa: S608
                params,
            )
            self._commit()
            if expect_status is not None and cursor.rowcount == 0:
                return None
        return self.get_order(order_id)

    def settle_order(
        self,
        order_id: str,
        *,
        expect_status: str,
        spend: tuple[str, float, str] | None = None,
        **fields: Any,
    ) -> dict[str, Any] | None:
        """Flip an order to its final status and count its spend, as one write.

        The compare-and-set on ``expect_status`` decides who settles; the
        winner's ``daily_spend`` upsert (``(wallet, usd, day)``) lands in the
        same transaction, so a crash or a failed write between the two can
        never leave a confirmed order that the cap has not seen — or the
        reverse. Returns the row, or ``None`` when someone else got there first.
        """
        with self.transaction():
            row = self.update_order(order_id, expect_status=expect_status, **fields)
            if row is not None and spend is not None:
                wallet, usd, day = spend
                self.add_daily_spend(wallet, usd, day)
        return row

    def find_orders_by_client_id(self, client_order_id: str) -> list[dict[str, Any]]:
        """Every order created under a caller's idempotency key, oldest first."""
        with self._lock:
            return _rows(
                self._conn.execute(
                    "SELECT * FROM orders WHERE client_order_id = ? ORDER BY rowid ASC",
                    (client_order_id,),
                )
            )

    def open_agent_value_usd(
        self, wallet: str, *, exclude_order_id: str | None = None, now: float | None = None
    ) -> float:
        """USD already committed by agent orders that are not settled yet.

        Counted toward the daily cap alongside confirmed spend, so a burst of
        orders fired before the first one confirms cannot each see an empty
        day. An order leaves this sum when it confirms (and joins the
        confirmed spend) or when it is rejected, expires or fails. A
        ``quoted`` row counts for an hour: that is an order between its quote
        and its verdict, racing this one; older than that it is a crash
        leftover, not money in flight.
        """
        ts = time.time() if now is None else now
        placeholders = ", ".join("?" for _ in ORDER_OPEN_STATUSES)
        free = ", ".join("?" for _ in _NON_SPENDING_KINDS)
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(value_usd), 0) AS usd FROM orders "
                "WHERE wallet = ? AND initiator = 'agent' AND order_id != ? AND "
                f"kind NOT IN ({free}) AND "
                f"(status IN ({placeholders}) OR (status = 'quoted' AND created_at > ?))",
                (
                    wallet.lower(),
                    exclude_order_id or "",
                    *_NON_SPENDING_KINDS,
                    *sorted(ORDER_OPEN_STATUSES),
                    ts - 3600,
                ),
            ).fetchone()
        return float(row["usd"]) if row and row["usd"] is not None else 0.0

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        with self._lock:
            return _row(
                self._conn.execute(
                    "SELECT * FROM orders WHERE order_id = ?", (order_id,)
                ).fetchone()
            )

    def list_orders(
        self,
        *,
        status: str | None = None,
        wallet: str | None = None,
        limit: int = 50,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["1=1"]
        params: list[Any] = []
        if status:
            statuses = [s.strip() for s in status.split(",") if s.strip()]
            clauses.append(f"status IN ({', '.join('?' for _ in statuses)})")
            params.extend(statuses)
        if wallet:
            clauses.append("wallet = ?")
            params.append(wallet.lower())
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        params.append(max(1, min(int(limit), 500)))
        with self._lock:
            return _rows(
                self._conn.execute(
                    f"SELECT * FROM orders WHERE {' AND '.join(clauses)} "  # noqa: S608
                    "ORDER BY created_at DESC LIMIT ?",
                    params,
                )
            )

    def batch_orders(self, batch_id: str) -> list[dict[str, Any]]:
        """Every order in a multisend, in the order they were created."""
        with self._lock:
            return _rows(
                self._conn.execute(
                    "SELECT * FROM orders WHERE batch_id = ? ORDER BY rowid ASC",
                    (batch_id,),
                )
            )

    def count_orders(self, status: str) -> int:
        """Orders in ``status``; a multisend counts once, however many rows it is."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(DISTINCT COALESCE(batch_id, order_id)) AS n FROM orders "
                "WHERE status = ?",
                (status,),
            ).fetchone()
        return int(row["n"]) if row else 0

    def expire_orders(self, now: float) -> list[dict[str, Any]]:
        with self._lock:
            rows = _rows(
                self._conn.execute(
                    "SELECT * FROM orders WHERE status = 'awaiting_approval' "
                    "AND expires_at IS NOT NULL AND expires_at <= ?",
                    (now,),
                )
            )
            expired: list[dict[str, Any]] = []
            for row in rows:
                # Status-guarded: an approval that landed between the SELECT
                # and this UPDATE must not be flipped back to expired.
                cursor = self._conn.execute(
                    "UPDATE orders SET status = 'expired', reason = 'expired', updated_at = ? "
                    "WHERE order_id = ? AND status = 'awaiting_approval'",
                    (now, row["order_id"]),
                )
                if cursor.rowcount:
                    expired.append({**row, "status": "expired", "reason": "expired"})
            self._commit()
        return expired

    # ── DCA mandates (docs/dca.md) ─────────────────────────────────────

    def insert_mandate(self, mandate: dict[str, Any]) -> None:
        columns = list(mandate.keys())
        with self._lock:
            self._conn.execute(
                f"INSERT INTO mandates ({', '.join(columns)}) "  # noqa: S608
                f"VALUES ({', '.join('?' for _ in columns)})",
                [mandate[c] for c in columns],
            )
            self._commit()

    def get_mandate(self, mandate_id: str) -> dict[str, Any] | None:
        with self._lock:
            return _row(
                self._conn.execute(
                    "SELECT * FROM mandates WHERE mandate_id = ?", (mandate_id,)
                ).fetchone()
            )

    def list_mandates(
        self,
        *,
        statuses: Iterable[str] | None = None,
        wallet: str | None = None,
        due_before: float | None = None,
    ) -> list[dict[str, Any]]:
        """Mandates, newest first; ``due_before`` keeps those whose next run is due."""
        clauses = ["1=1"]
        params: list[Any] = []
        if statuses is not None:
            wanted = list(statuses)
            if not wanted:
                return []
            clauses.append(f"status IN ({', '.join('?' for _ in wanted)})")
            params.extend(wanted)
        if wallet:
            clauses.append("wallet = ?")
            params.append(wallet.lower())
        if due_before is not None:
            clauses.append("next_run_at IS NOT NULL AND next_run_at <= ?")
            params.append(float(due_before))
        with self._lock:
            return _rows(
                self._conn.execute(
                    f"SELECT * FROM mandates WHERE {' AND '.join(clauses)} "  # noqa: S608
                    "ORDER BY created_at DESC, rowid DESC",
                    params,
                )
            )

    def update_mandate(
        self,
        mandate_id: str,
        *,
        now: float,
        expect_status: str | Iterable[str] | None = None,
        expect_next_run_at: float | None | object = ...,
        **fields: Any,
    ) -> dict[str, Any] | None:
        """Update a mandate; each ``expect_*`` makes it a compare-and-set.

        ``expect_status`` (one status or several) decides races between two
        writers of the state (approve vs. expire, pause vs. auto-pause);
        ``expect_next_run_at`` makes the schedule advance claim a run, so two
        passes that both saw it due cannot both buy. The loser gets ``None``.
        """
        fields["updated_at"] = float(now)
        sets = ", ".join(f"{k} = ?" for k in fields)
        where = "mandate_id = ?"
        params: list[Any] = [*fields.values(), mandate_id]
        guarded = False
        if expect_status is not None:
            wanted = [expect_status] if isinstance(expect_status, str) else list(expect_status)
            where += f" AND status IN ({', '.join('?' for _ in wanted)})"
            params.extend(wanted)
            guarded = True
        if expect_next_run_at is not ...:
            where += " AND next_run_at IS ?"
            params.append(expect_next_run_at)
            guarded = True
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE mandates SET {sets} WHERE {where}",  # noqa: S608
                params,
            )
            self._commit()
            if cursor.rowcount == 0 and guarded:
                return None
        return self.get_mandate(mandate_id)

    def add_mandate_counts(self, mandate_id: str, *, now: float, **deltas: int) -> None:
        """Add to the run counters (``runs_done``, ``runs_skipped``, …) in one write."""
        allowed = {"runs_done", "runs_skipped", "runs_failed", "bad_streak"}
        unknown = set(deltas) - allowed
        if unknown:
            raise ValueError(f"not a mandate counter: {sorted(unknown)}")
        if not deltas:
            return
        sets = ", ".join(f"{k} = {k} + ?" for k in deltas)
        with self._lock:
            self._conn.execute(
                f"UPDATE mandates SET {sets}, updated_at = ? WHERE mandate_id = ?",  # noqa: S608
                [*(int(v) for v in deltas.values()), float(now), mandate_id],
            )
            self._commit()

    def insert_run(self, run: dict[str, Any]) -> int:
        columns = list(run.keys())
        with self._lock:
            cursor = self._conn.execute(
                f"INSERT INTO mandate_runs ({', '.join(columns)}) "  # noqa: S608
                f"VALUES ({', '.join('?' for _ in columns)})",
                [run[c] for c in columns],
            )
            self._commit()
            return int(cursor.lastrowid or 0)

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        with self._lock:
            return _row(
                self._conn.execute(
                    "SELECT * FROM mandate_runs WHERE run_id = ?", (int(run_id),)
                ).fetchone()
            )

    def update_run(
        self, run_id: int, *, expect_status: Iterable[str] | None = None, **fields: Any
    ) -> dict[str, Any] | None:
        """Update a run; with ``expect_status`` only a run still in one of them moves."""
        sets = ", ".join(f"{k} = ?" for k in fields)
        where = "run_id = ?"
        params: list[Any] = [*fields.values(), int(run_id)]
        if expect_status is not None:
            wanted = list(expect_status)
            where += f" AND status IN ({', '.join('?' for _ in wanted)})"
            params.extend(wanted)
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE mandate_runs SET {sets} WHERE {where}",  # noqa: S608
                params,
            )
            self._commit()
            if expect_status is not None and cursor.rowcount == 0:
                return None
        return self.get_run(run_id)

    def list_runs(self, mandate_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """A mandate's runs, newest first."""
        with self._lock:
            return _rows(
                self._conn.execute(
                    "SELECT * FROM mandate_runs WHERE mandate_id = ? "
                    "ORDER BY n DESC, run_id DESC LIMIT ?",
                    (mandate_id, max(1, int(limit))),
                )
            )

    def run_attempts(self, mandate_id: str) -> int:
        """How many runs a mandate has had (the highest ``n``)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(n), 0) AS n FROM mandate_runs WHERE mandate_id = ?",
                (mandate_id,),
            ).fetchone()
        return int(row["n"]) if row else 0

    def open_runs(self, mandate_id: str | None = None) -> list[dict[str, Any]]:
        """Runs whose order has not settled yet, oldest first."""
        placeholders = ", ".join("?" for _ in RUN_OPEN_STATUSES)
        sql = f"SELECT * FROM mandate_runs WHERE status IN ({placeholders})"  # noqa: S608
        params: list[Any] = list(RUN_OPEN_STATUSES)
        if mandate_id is not None:
            sql += " AND mandate_id = ?"
            params.append(mandate_id)
        with self._lock:
            return _rows(self._conn.execute(sql + " ORDER BY run_id ASC", params))

    def run_for_order(self, order_id: str) -> dict[str, Any] | None:
        with self._lock:
            return _row(
                self._conn.execute(
                    "SELECT * FROM mandate_runs WHERE order_id = ? ORDER BY run_id DESC LIMIT 1",
                    (order_id,),
                ).fetchone()
            )

    def orders_for_mandate(self, mandate_id: str) -> list[dict[str, Any]]:
        """Every order a mandate placed, oldest first."""
        with self._lock:
            return _rows(
                self._conn.execute(
                    "SELECT * FROM orders WHERE mandate_id = ? ORDER BY created_at ASC, rowid ASC",
                    (mandate_id,),
                )
            )

    def mandate_spend(self, mandate_id: str) -> tuple[float, float, int, float]:
        """``(confirmed_usd, reserved_usd, received_out_raw, gas_usd)`` of a mandate.

        Read from its orders, not from the cached ``spent_usd``: confirmed is
        the value of confirmed orders, reserved the value of the ones still
        open (awaiting approval, approved, submitted), received the raw
        amount the confirmed ones delivered and gas every fee booked for any
        of them (approvals and reverted attempts included).
        """
        open_statuses = sorted(ORDER_OPEN_STATUSES)
        placeholders = ", ".join("?" for _ in open_statuses)
        with self._lock:
            sums = self._conn.execute(
                "SELECT "
                "COALESCE(SUM(CASE WHEN status = 'confirmed' THEN value_usd END), 0) AS spent, "
                f"COALESCE(SUM(CASE WHEN status IN ({placeholders}) THEN value_usd END), 0) "
                "AS reserved FROM orders WHERE mandate_id = ?",
                (*open_statuses, mandate_id),
            ).fetchone()
            received = self._conn.execute(
                "SELECT received_out_raw FROM orders WHERE mandate_id = ? "
                "AND status = 'confirmed' AND received_out_raw IS NOT NULL "
                "AND (delivered_token IS NULL OR delivered_token = token_out)",
                (mandate_id,),
            ).fetchall()
            gas = self._conn.execute(
                "SELECT COALESCE(SUM(e.gas_usd), 0) AS gas FROM entries e "
                "JOIN orders o ON o.order_id = e.order_id WHERE o.mandate_id = ?",
                (mandate_id,),
            ).fetchone()
        received_raw = sum(int(r["received_out_raw"] or 0) for r in received)
        return (
            float(sums["spent"] or 0.0),
            float(sums["reserved"] or 0.0),
            received_raw,
            float(gas["gas"] or 0.0),
        )

    def order_gas_usd(self, order_ids: Iterable[str]) -> dict[str, float]:
        """Gas booked per order (every entry that carries the order id)."""
        ids = list(dict.fromkeys(order_ids))
        if not ids:
            return {}
        placeholders = ", ".join("?" for _ in ids)
        with self._lock:
            rows = self._conn.execute(
                "SELECT order_id, SUM(gas_usd) AS gas FROM entries "  # noqa: S608
                f"WHERE order_id IN ({placeholders}) AND gas_usd IS NOT NULL GROUP BY order_id",
                ids,
            ).fetchall()
        return {str(r["order_id"]): float(r["gas"]) for r in rows}

    # ── price triggers (docs/triggers.md) ──────────────────────────────

    def insert_trigger(self, trigger: dict[str, Any]) -> None:
        columns = list(trigger.keys())
        with self._lock:
            self._conn.execute(
                f"INSERT INTO triggers ({', '.join(columns)}) "  # noqa: S608
                f"VALUES ({', '.join('?' for _ in columns)})",
                [trigger[c] for c in columns],
            )
            self._commit()

    def get_trigger(self, trigger_id: str) -> dict[str, Any] | None:
        with self._lock:
            return _row(
                self._conn.execute(
                    "SELECT * FROM triggers WHERE trigger_id = ?", (trigger_id,)
                ).fetchone()
            )

    def insert_triggers(self, rows: list[dict[str, Any]]) -> None:
        """Insert several triggers (a bracket's two legs) in one transaction: all or none."""
        with self.transaction():
            for row in rows:
                self.insert_trigger(row)

    def list_triggers(
        self,
        *,
        statuses: Iterable[str] | None = None,
        wallet: str | None = None,
        legs: str = "exclude",
    ) -> list[dict[str, Any]]:
        """Triggers, newest first.

        ``legs``: ``"exclude"`` (the default) leaves bracket legs out, so a
        plain trigger list never shows them; ``"only"`` lists legs alone;
        ``"all"`` both (the checker and the expirers).
        """
        if legs not in TRIGGER_LEG_FILTERS:
            raise ValueError(f"legs must be one of {', '.join(TRIGGER_LEG_FILTERS)}")
        clauses = ["1=1"]
        params: list[Any] = []
        if legs == "exclude":
            clauses.append("group_id IS NULL")
        elif legs == "only":
            clauses.append("group_id IS NOT NULL")
        if statuses is not None:
            wanted = list(statuses)
            if not wanted:
                return []
            clauses.append(f"status IN ({', '.join('?' for _ in wanted)})")
            params.extend(wanted)
        if wallet:
            clauses.append("wallet = ?")
            params.append(wallet.lower())
        with self._lock:
            return _rows(
                self._conn.execute(
                    f"SELECT * FROM triggers WHERE {' AND '.join(clauses)} "  # noqa: S608
                    "ORDER BY created_at DESC, rowid DESC",
                    params,
                )
            )

    def group_triggers(self, group_id: str) -> list[dict[str, Any]]:
        """A bracket's legs, take-profit first."""
        with self._lock:
            return _rows(
                self._conn.execute(
                    "SELECT * FROM triggers WHERE group_id = ? "
                    "ORDER BY CASE leg WHEN 'tp' THEN 0 WHEN 'sl' THEN 1 ELSE 2 END, rowid",
                    (group_id,),
                )
            )

    def list_groups(
        self,
        *,
        statuses: Iterable[str] | None = None,
        wallet: str | None = None,
    ) -> list[str]:
        """Bracket ids, newest first; with ``statuses``, those where any leg is in one."""
        clauses = ["group_id IS NOT NULL"]
        params: list[Any] = []
        if statuses is not None:
            wanted = list(statuses)
            if not wanted:
                return []
            clauses.append(f"status IN ({', '.join('?' for _ in wanted)})")
            params.extend(wanted)
        if wallet:
            clauses.append("wallet = ?")
            params.append(wallet.lower())
        with self._lock:
            rows = self._conn.execute(
                f"SELECT group_id, MAX(created_at) AS created, MAX(rowid) AS seq "  # noqa: S608
                f"FROM triggers WHERE {' AND '.join(clauses)} "
                "GROUP BY group_id ORDER BY created DESC, seq DESC",
                params,
            ).fetchall()
        return [str(r["group_id"]) for r in rows]

    def update_trigger(
        self,
        trigger_id: str,
        *,
        now: float,
        expect_status: str | Iterable[str] | None = None,
        **fields: Any,
    ) -> dict[str, Any] | None:
        """Update a trigger; with ``expect_status`` it is a compare-and-set.

        ``expect_status`` (one status or several) decides races between two
        writers of the state (a check that fires vs. a pause, a settling
        order vs. a stop); the loser gets ``None``.
        """
        fields["updated_at"] = float(now)
        sets = ", ".join(f"{k} = ?" for k in fields)
        where = "trigger_id = ?"
        params: list[Any] = [*fields.values(), trigger_id]
        if expect_status is not None:
            wanted = [expect_status] if isinstance(expect_status, str) else list(expect_status)
            where += f" AND status IN ({', '.join('?' for _ in wanted)})"
            params.extend(wanted)
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE triggers SET {sets} WHERE {where}",  # noqa: S608
                params,
            )
            self._commit()
            if cursor.rowcount == 0 and expect_status is not None:
                return None
        return self.get_trigger(trigger_id)

    def add_trigger_counts(self, trigger_id: str, *, now: float, **deltas: int) -> None:
        """Add to the counters (``fires_failed``, ``bad_streak``, ``hits``) in one write."""
        allowed = {"fires_failed", "bad_streak", "hits"}
        unknown = set(deltas) - allowed
        if unknown:
            raise ValueError(f"not a trigger counter: {sorted(unknown)}")
        if not deltas:
            return
        sets = ", ".join(f"{k} = {k} + ?" for k in deltas)
        with self._lock:
            self._conn.execute(
                f"UPDATE triggers SET {sets}, updated_at = ? WHERE trigger_id = ?",  # noqa: S608
                [*(int(v) for v in deltas.values()), float(now), trigger_id],
            )
            self._commit()

    def insert_fire(self, fire: dict[str, Any]) -> int:
        columns = list(fire.keys())
        with self._lock:
            cursor = self._conn.execute(
                f"INSERT INTO trigger_fires ({', '.join(columns)}) "  # noqa: S608
                f"VALUES ({', '.join('?' for _ in columns)})",
                [fire[c] for c in columns],
            )
            self._commit()
            return int(cursor.lastrowid or 0)

    def get_fire(self, fire_id: int) -> dict[str, Any] | None:
        with self._lock:
            return _row(
                self._conn.execute(
                    "SELECT * FROM trigger_fires WHERE fire_id = ?", (int(fire_id),)
                ).fetchone()
            )

    def update_fire(
        self, fire_id: int, *, expect_status: Iterable[str] | None = None, **fields: Any
    ) -> dict[str, Any] | None:
        """Update a fire; with ``expect_status`` only a fire still in one of them moves."""
        sets = ", ".join(f"{k} = ?" for k in fields)
        where = "fire_id = ?"
        params: list[Any] = [*fields.values(), int(fire_id)]
        if expect_status is not None:
            wanted = list(expect_status)
            where += f" AND status IN ({', '.join('?' for _ in wanted)})"
            params.extend(wanted)
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE trigger_fires SET {sets} WHERE {where}",  # noqa: S608
                params,
            )
            self._commit()
            if expect_status is not None and cursor.rowcount == 0:
                return None
        return self.get_fire(fire_id)

    def list_fires(self, trigger_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """A trigger's fires, newest first."""
        with self._lock:
            return _rows(
                self._conn.execute(
                    "SELECT * FROM trigger_fires WHERE trigger_id = ? "
                    "ORDER BY n DESC, fire_id DESC LIMIT ?",
                    (trigger_id, max(1, int(limit))),
                )
            )

    def fire_attempts(self, trigger_id: str) -> int:
        """How many fires a trigger has had (the highest ``n``)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(n), 0) AS n FROM trigger_fires WHERE trigger_id = ?",
                (trigger_id,),
            ).fetchone()
        return int(row["n"]) if row else 0

    def open_fires(self, trigger_id: str | None = None) -> list[dict[str, Any]]:
        """Fires whose order has not settled yet, oldest first."""
        placeholders = ", ".join("?" for _ in FIRE_OPEN_STATUSES)
        sql = f"SELECT * FROM trigger_fires WHERE status IN ({placeholders})"  # noqa: S608
        params: list[Any] = list(FIRE_OPEN_STATUSES)
        if trigger_id is not None:
            sql += " AND trigger_id = ?"
            params.append(trigger_id)
        with self._lock:
            return _rows(self._conn.execute(sql + " ORDER BY fire_id ASC", params))

    def fire_for_order(self, order_id: str) -> dict[str, Any] | None:
        with self._lock:
            return _row(
                self._conn.execute(
                    "SELECT * FROM trigger_fires WHERE order_id = ? ORDER BY fire_id DESC LIMIT 1",
                    (order_id,),
                ).fetchone()
            )

    def orders_for_trigger(self, trigger_id: str) -> list[dict[str, Any]]:
        """Every order a trigger placed, oldest first."""
        with self._lock:
            return _rows(
                self._conn.execute(
                    "SELECT * FROM orders WHERE trigger_id = ? ORDER BY created_at ASC, rowid ASC",
                    (trigger_id,),
                )
            )

    # ── daily spend ────────────────────────────────────────────────────

    def add_daily_spend(self, wallet: str, usd: float, day: str | None = None) -> None:
        day = day or local_day()
        with self._lock:
            self._conn.execute(
                "INSERT INTO daily_spend (wallet, day, spent_usd) VALUES (?, ?, ?) "
                "ON CONFLICT(wallet, day) DO UPDATE SET spent_usd = spent_usd + excluded.spent_usd",
                (wallet.lower(), day, float(usd)),
            )
            self._commit()

    def spent_today(self, wallet: str, day: str | None = None) -> float:
        day = day or local_day()
        with self._lock:
            row = self._conn.execute(
                "SELECT spent_usd FROM daily_spend WHERE wallet = ? AND day = ?",
                (wallet.lower(), day),
            ).fetchone()
        return float(row["spent_usd"]) if row else 0.0

    # ── allowances ─────────────────────────────────────────────────────

    def upsert_allowance(
        self,
        chain_id: int,
        wallet: str,
        token: str,
        spender: str,
        *,
        block: int,
        tx_hash: str | None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO allowances (chain_id, wallet, token, spender, first_block, "
                "last_block, last_tx_hash) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(chain_id, wallet, token, spender) DO UPDATE SET "
                "last_block = MAX(last_block, excluded.last_block), "
                "last_tx_hash = CASE WHEN excluded.last_block >= last_block "
                "THEN excluded.last_tx_hash ELSE last_tx_hash END",
                (
                    chain_id,
                    wallet.lower(),
                    token.lower(),
                    spender.lower(),
                    int(block),
                    int(block),
                    tx_hash.lower() if tx_hash else None,
                ),
            )
            self._commit()

    def allowances(self, chain_id: int, wallet: str) -> list[dict[str, Any]]:
        with self._lock:
            return _rows(
                self._conn.execute(
                    "SELECT * FROM allowances WHERE chain_id = ? AND wallet = ? "
                    "ORDER BY last_block DESC",
                    (chain_id, wallet.lower()),
                )
            )

    def delete_allowance(self, chain_id: int, wallet: str, token: str, spender: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM allowances WHERE chain_id = ? AND wallet = ? AND token = ? "
                "AND spender = ?",
                (chain_id, wallet.lower(), token.lower(), spender.lower()),
            )
            self._commit()

    def allowance_scan(self, chain_id: int, wallet: str) -> dict[str, Any] | None:
        with self._lock:
            return _row(
                self._conn.execute(
                    "SELECT * FROM allowance_scan WHERE chain_id = ? AND wallet = ?",
                    (chain_id, wallet.lower()),
                ).fetchone()
            )

    def clear_allowance_scan(self, chain_id: int, wallet: str) -> None:
        """Forget where the scan stopped (a full rescan starts from the wallet's start)."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM allowance_scan WHERE chain_id = ? AND wallet = ?",
                (chain_id, wallet.lower()),
            )
            self._commit()

    def set_allowance_scan(self, chain_id: int, wallet: str, *, last_block: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO allowance_scan (chain_id, wallet, last_block, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(chain_id, wallet) DO UPDATE SET "
                "last_block = excluded.last_block, updated_at = excluded.updated_at",
                (chain_id, wallet.lower(), int(last_block), time.time()),
            )
            self._commit()

    # ── sync state / balances / snapshots ──────────────────────────────

    def sync_state(self, chain_id: int, wallet: str) -> dict[str, Any] | None:
        with self._lock:
            return _row(
                self._conn.execute(
                    "SELECT * FROM sync_state WHERE chain_id = ? AND wallet = ?",
                    (chain_id, wallet.lower()),
                ).fetchone()
            )

    def set_sync_state(
        self,
        chain_id: int,
        wallet: str,
        *,
        last_block: int,
        oldest_block: int | None,
        full: bool = False,
    ) -> None:
        """Record where a sweep stopped; ``full`` also stamps ``full_synced_at``.

        An incremental pass keeps whatever full-sync stamp the row already
        has; only a rebuild's swap-in sets a new one.
        """
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO sync_state (chain_id, wallet, last_block, oldest_block, updated_at, "
                "full_synced_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(chain_id, wallet) DO UPDATE "
                "SET last_block = excluded.last_block, oldest_block = excluded.oldest_block, "
                "updated_at = excluded.updated_at, "
                "full_synced_at = COALESCE(excluded.full_synced_at, sync_state.full_synced_at)",
                (
                    chain_id,
                    wallet.lower(),
                    int(last_block),
                    oldest_block,
                    now,
                    now if full else None,
                ),
            )
            self._commit()

    def set_balance(self, chain_id: int, wallet: str, token: str, raw: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO balances (chain_id, wallet, token, raw, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(chain_id, wallet, token) DO UPDATE SET raw = excluded.raw, "
                "updated_at = excluded.updated_at",
                (chain_id, wallet.lower(), token.lower(), str(int(raw)), time.time()),
            )
            self._commit()

    def get_balance(self, chain_id: int, wallet: str, token: str) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT raw FROM balances WHERE chain_id = ? AND wallet = ? AND token = ?",
                (chain_id, wallet.lower(), token.lower()),
            ).fetchone()
        return int(row["raw"]) if row else None

    def balances(
        self, wallet: str | None = None, chain_id: int | None = None
    ) -> list[dict[str, Any]]:
        clauses = ["1=1"]
        params: list[Any] = []
        if wallet:
            clauses.append("wallet = ?")
            params.append(wallet.lower())
        if chain_id is not None:
            clauses.append("chain_id = ?")
            params.append(chain_id)
        with self._lock:
            return _rows(
                self._conn.execute(
                    f"SELECT * FROM balances WHERE {' AND '.join(clauses)}",  # noqa: S608
                    params,
                )
            )

    def set_chain_read(
        self, chain_id: int, wallet: str, status: str, reason: str | None = None
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO chain_reads (chain_id, wallet, status, reason, read_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(chain_id, wallet) DO UPDATE SET "
                "status = excluded.status, reason = excluded.reason, read_at = excluded.read_at",
                (chain_id, wallet.lower(), status, reason, time.time()),
            )
            self._commit()

    def chain_reads(
        self, wallet: str | None = None, chain_id: int | None = None
    ) -> list[dict[str, Any]]:
        clauses = ["1=1"]
        params: list[Any] = []
        if wallet:
            clauses.append("wallet = ?")
            params.append(wallet.lower())
        if chain_id is not None:
            clauses.append("chain_id = ?")
            params.append(chain_id)
        with self._lock:
            return _rows(
                self._conn.execute(
                    f"SELECT * FROM chain_reads WHERE {' AND '.join(clauses)}",  # noqa: S608
                    params,
                )
            )

    def add_snapshot(self, chain_id: int, token: str, ts: float, price_usd: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO price_snapshots (chain_id, token, ts, price_usd) "
                "VALUES (?, ?, ?, ?)",
                (chain_id, token.lower(), float(ts), float(price_usd)),
            )
            self._commit()

    def snapshots(self, chain_id: int, token: str, since: float) -> list[dict[str, Any]]:
        with self._lock:
            return _rows(
                self._conn.execute(
                    "SELECT ts, price_usd FROM price_snapshots WHERE chain_id = ? AND token = ? "
                    "AND ts >= ? ORDER BY ts",
                    (chain_id, token.lower(), since),
                )
            )

    def prune_snapshots(self, older_than: float) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM price_snapshots WHERE ts < ?", (older_than,))
            self._commit()
