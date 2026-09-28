"""Chain → ledger synchronisation.

For every wallet × chain: sweep ERC-20 ``Transfer`` logs since the last
synced block, classify each transaction (deposit / withdraw / external
swap — our own swaps are recorded by the service when they confirm), price
it at its block time, and keep the FIFO lots current. Native ETH has no
logs, so a change in the native balance that the ledger cannot explain is
booked as a deposit or withdrawal.

Three rules keep the ledger honest:

* **Entries are the truth; lots are derived.** A position's lots and realized
  rows can be replayed from its entries at any time (:meth:`_replay_lots`).
* **One opening balance per position**, and it covers only what the chain
  sweep cannot explain: ``balance − (scanned in − scanned out)``. It is
  recomputed after every sweep, so a transfer scanned later shrinks or
  removes it instead of being counted twice. Openings are never booked
  before the first sweep of a wallet/chain has run.
* **A full rebuild is atomic and resumable.** The sweep lands in shadow
  tables (``rebuild_logs``/``rebuild_state``) while the live ledger keeps
  serving; the swap-in happens in one transaction; an interrupted sweep
  resumes from where it stopped.
* **A failed read is not a zero.** A ``balanceOf`` the node did not answer
  leaves the stored balance as it was and marks the wallet/chain read
  ``partial`` (``chain_reads``), so a flaky RPC can neither empty a holding
  nor rewrite its opening.

Which ERC-20s get read is the union of what the ledger has met and what an
indexer (``discovery.py``) says the wallet holds; the indexer only ever adds
addresses to the scan set, every number still comes from the RPC.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

from agentos.trading.chains import NATIVE_ADDRESS, ChainSpec, checksum_address
from agentos.trading.decode import spender_label
from agentos.trading.evm import EvmClient, TransferLog
from agentos.trading.ledger import OPENING_NOTE, Ledger
from agentos.trading.pnl import per_raw, sell_fifo, to_human
from agentos.trading.prices import PriceService, TokenMeta
from agentos.trading.vault import WalletRecord

log = structlog.get_logger(__name__)

TokenMetaFn = Callable[[ChainSpec, str], Awaitable[TokenMeta]]
EvmFn = Callable[[ChainSpec], EvmClient]
WatchFn = Callable[[ChainSpec], Awaitable[list[str]]]
# (chain, wallet address) -> ERC-20 addresses an indexer says the wallet holds.
DiscoverFn = Callable[[ChainSpec, str], Awaitable[list[str]]]

# ``chain_reads.status`` vocabulary.
READ_OK = "ok"
READ_PARTIAL = "partial"
READ_FAILED = "failed"

# An imported wallet's first sweep: about a day on Base, an hour on Robinhood.
DEFAULT_INITIAL_LOOKBACK = 50_000
DEFAULT_FULL_LOOKBACK = 2_000_000
# A rebuild sweeps backwards in windows this many node-spans wide, so
# progress is persisted often enough that an interruption costs little.
REBUILD_WINDOW_SPANS = 10
# Ignore native dust drift below this (gas of a tx we did not see).
NATIVE_DUST_WEI = 10**13
# An incremental pass re-reads this many blocks below the last synced one.
# ``latest`` comes from a load-balanced endpoint whose nodes disagree by a
# block or two; a log served by a lagging node would otherwise be skipped
# for good. Entries dedupe on (wallet, kind, tx_hash, log_index), so the
# overlap costs a little bandwidth and never a double booking.
SYNC_OVERLAP_BLOCKS = 10

Priced = tuple[float | None, str]

# Dollar-pegged tokens whose price is the most trustworthy leg of a swap.
STABLE_SYMBOLS = frozenset({"USDC", "USDT", "USDG", "DAI"})
# A deposit of an unlisted token from a stranger is a lot at cost zero.
AIRDROP_SOURCE = "airdrop"


@dataclass
class BalanceRead:
    """What one ERC-20 balance refresh did to the ledger."""

    # Some stored ``raw`` moved.
    changed: bool = False
    # Tokens the node did not answer for; their rows were left as they were.
    failed: list[str] = field(default_factory=list)


@dataclass
class TxGroup:
    """One transaction's net effect on a wallet, as seen through Transfer logs."""

    tx_hash: str
    block: int
    log_index: int
    ts: float
    ins: dict[str, int] = field(default_factory=dict)
    outs: dict[str, int] = field(default_factory=dict)
    # Every address that sent a Transfer in this transaction. The wallet's
    # own presence here means it took part (a self-transfer, a contract it
    # called); an inbound token whose senders are all strangers is an airdrop.
    senders: set[str] = field(default_factory=set)


class WalletSyncer:
    def __init__(
        self,
        ledger: Ledger,
        prices: PriceService,
        *,
        evm_for: EvmFn,
        token_meta: TokenMetaFn,
        watch_tokens: WatchFn | None = None,
        discover_tokens: DiscoverFn | None = None,
        discovery_truncated: Callable[[ChainSpec, str], bool] | None = None,
        now: Callable[[], float] = time.time,
        initial_lookback: int = DEFAULT_INITIAL_LOOKBACK,
        full_lookback: int = DEFAULT_FULL_LOOKBACK,
    ) -> None:
        self.ledger = ledger
        self.prices = prices
        self._evm_for = evm_for
        self._token_meta = token_meta
        self._watch_tokens = watch_tokens
        self._discover_tokens = discover_tokens
        self._discovery_truncated = discovery_truncated
        self._now = now
        self.initial_lookback = initial_lookback
        self.full_lookback = full_lookback
        self._block_ts: dict[tuple[int, int], int] = {}
        # Progress of the rebuild in flight (for trading.status), else None.
        self.rebuild_progress: dict[str, Any] | None = None
        # Whether the pass in flight reads hidden (junk) tokens too.
        self.include_hidden = False

    # ── public ─────────────────────────────────────────────────────────

    async def sync(
        self,
        wallet: WalletRecord,
        chain: ChainSpec,
        *,
        full: bool = False,
        include_hidden: bool = False,
    ) -> bool:
        """One incremental pass for a wallet/chain; True if the ledger moved.

        Hidden (junk) tokens stay out of the balance read, the opening
        reconciliation and the price snapshot unless ``include_hidden`` — the
        service passes it once an hour, so junk still cannot drift for long.
        Their transfers are always recorded: the sweep does not know what is
        junk, and must not.
        """
        self.include_hidden = include_hidden
        address = wallet.key
        # A rebuild that was interrupted (gateway restart) finishes first: its
        # shadow sweep is already partly done, and the live ledger it will
        # replace may be missing what the interruption cost.
        if full or self.ledger.rebuild_state(chain.chain_id, address) is not None:
            return await self.rebuild(wallet, chain)
        evm = self._evm_for(chain)
        latest = await evm.block_number()
        state = self.ledger.sync_state(chain.chain_id, address)
        if state is None:
            created = wallet.created_block.get(str(chain.chain_id))
            if created is not None:
                start = int(created)
            else:
                start = max(0, latest - self.initial_lookback)
            oldest: int | None = start
        else:
            oldest = state.get("oldest_block")
            floor = int(oldest) if oldest is not None else 0
            start = max(floor, int(state["last_block"]) + 1 - SYNC_OVERLAP_BLOCKS)
        changed = False
        if start <= latest:
            logs = await evm.transfer_logs(address, from_block=start, to_block=latest)
            changed = await self._record_transfers(wallet, chain, logs) or changed
        changed = await self._reconcile_native(wallet, chain, evm) or changed
        read = await self._refresh_balances(wallet, chain, evm)
        changed = read.changed or changed
        changed = await self._reconcile_openings(wallet, chain) or changed
        await self._snapshot_prices(wallet, chain)
        self.ledger.set_sync_state(chain.chain_id, address, last_block=latest, oldest_block=oldest)
        self._record_read(chain, address, read)
        return changed

    def _record_read(self, chain: ChainSpec, wallet: str, read: BalanceRead) -> None:
        reasons: list[str] = []
        if read.failed:
            reasons.append(f"{len(read.failed)} token balance(s) could not be read")
        if self._discovery_truncated is not None and self._discovery_truncated(chain, wallet):
            # The indexer listed more tokens than discovery reads, so what was
            # swept is a subset of the wallet. Saying so is the difference
            # between "you hold nothing else" and "we stopped counting"
            # (#3506).
            reasons.append("the token indexer listed more tokens than one sweep reads")
        if reasons:
            self.ledger.set_chain_read(chain.chain_id, wallet, READ_PARTIAL, "; ".join(reasons))
        else:
            self.ledger.set_chain_read(chain.chain_id, wallet, READ_OK)

    def record_failure(self, chain: ChainSpec, wallet: str, reason: str) -> None:
        """The node could not be reached at all: every row on the chain is last-good."""
        self.ledger.set_chain_read(chain.chain_id, wallet, READ_FAILED, reason[:200])

    async def rebuild(self, wallet: WalletRecord, chain: ChainSpec) -> bool:
        """Rebuild one wallet/chain from the chain: sweep into the shadow, then swap in."""
        evm = self._evm_for(chain)
        address = wallet.key
        state = self.ledger.rebuild_state(chain.chain_id, address)
        if state is None:
            latest = await evm.block_number()
            created = wallet.created_block.get(str(chain.chain_id))
            if created is not None:
                target_from = int(created)
            else:
                target_from = max(0, latest - self.full_lookback)
            scanned_down_to = latest + 1
            self.ledger.clear_rebuild(chain.chain_id, address)
            self.ledger.set_rebuild_state(
                chain.chain_id,
                address,
                latest=latest,
                target_from=target_from,
                scanned_down_to=scanned_down_to,
            )
        else:
            latest = int(state["latest"])
            target_from = int(state["target_from"])
            scanned_down_to = int(state["scanned_down_to"])

        window = max(1, evm.max_log_span) * REBUILD_WINDOW_SPANS
        total = max(1, latest + 1 - target_from)
        try:
            while scanned_down_to > target_from:
                lo = max(target_from, scanned_down_to - window)
                self._progress(chain, address, latest, target_from, scanned_down_to, "sweep")
                logs = await evm.transfer_logs(address, from_block=lo, to_block=scanned_down_to - 1)
                self.ledger.add_rebuild_logs(
                    chain.chain_id,
                    address,
                    [
                        {
                            "tx_hash": t.tx_hash,
                            "log_index": t.log_index,
                            "block_number": t.block_number,
                            "token": t.token,
                            "sender": t.sender,
                            "recipient": t.recipient,
                            "amount": t.amount,
                        }
                        for t in logs
                    ],
                )
                scanned_down_to = lo
                self.ledger.set_rebuild_state(
                    chain.chain_id,
                    address,
                    latest=latest,
                    target_from=target_from,
                    scanned_down_to=scanned_down_to,
                )
            self._progress(chain, address, latest, target_from, target_from, "book")
            await self._swap_in(wallet, chain, evm, latest=latest, oldest=target_from)
        finally:
            self.rebuild_progress = None
        log.info("trading.rebuilt", chain=chain.key, wallet=address, blocks=total)
        return True

    def _progress(
        self, chain: ChainSpec, wallet: str, latest: int, target: int, down_to: int, phase: str
    ) -> None:
        span = max(1, latest + 1 - target)
        done = max(0, latest + 1 - down_to)
        self.rebuild_progress = {
            "chainId": chain.chain_id,
            "wallet": wallet,
            "latest": latest,
            "from": target,
            "scannedDownTo": down_to,
            "pct": round(min(100.0, 100.0 * done / span), 1),
            "phase": phase,
        }

    # ── rebuild: swap-in ───────────────────────────────────────────────

    async def _swap_in(
        self, wallet: WalletRecord, chain: ChainSpec, evm: EvmClient, *, latest: int, oldest: int
    ) -> None:
        address = wallet.key
        logs = [
            TransferLog(
                tx_hash=str(r["tx_hash"]),
                log_index=int(r["log_index"]),
                block_number=int(r["block_number"]),
                token=str(r["token"]),
                sender=str(r["sender"]),
                recipient=str(r["recipient"]),
                amount=int(r["amount"]),
            )
            for r in self.ledger.rebuild_logs(chain.chain_id, address)
        ]
        # Our own confirmed orders, by what they did. Every kind keeps its
        # transaction out of the chain-derived groups (the order row is the
        # richer record: initiator, session, gas), but only a swap is booked
        # as one. A send is a withdrawal; a revoke moved nothing but gas —
        # its amount_raw is the allowance it cleared, often 2**256-1, and
        # replayed as a swap it would sell every lot the wallet holds.
        orders = self.ledger.confirmed_orders(address, chain.chain_id)
        own_tx = {str(o["tx_hash"]).lower() for o in orders}
        foreign = [t for t in logs if t.tx_hash not in own_tx]
        groups = await self._group_transfers(chain, address, foreign)
        swaps = [o for o in orders if _order_kind(o) == "swap"]
        sends = [o for o in orders if _order_kind(o) == "send"]
        revokes = [o for o in orders if _order_kind(o) == "revoke"]
        lp_writes = [o for o in orders if _order_kind(o).startswith("lp_")]

        # Every event in time order: scanned transactions and our own orders.
        events: list[tuple[float, int, str, Any]] = [(g.ts, g.block, "tx", g) for g in groups]
        for o in swaps:
            events.append((float(o["updated_at"]), 0, "order", o))
        for o in sends:
            events.append((float(o["updated_at"]), 0, "send", o))
        for o in revokes:
            events.append((float(o["updated_at"]), 0, "revoke", o))
        for o in lp_writes:
            events.append((float(o["updated_at"]), 0, "lp", o))
        events.sort(key=lambda e: (e[0], e[1]))

        # What the chain holds now, and how much of it the events explain.
        native_now = await evm.get_balance(wallet.address)
        tokens: set[str] = {t.token for t in logs}
        for o in orders:
            for key in ("token_in", "token_out", "delivered_token"):
                token = str(o.get(key) or "").lower()
                if token and token != NATIVE_ADDRESS:
                    tokens.add(token)
        for o in lp_writes:
            ins, outs, _gas = _lp_moves(o)
            tokens.update(t for t, _a, _i in [*ins, *outs] if t != NATIVE_ADDRESS)
        if self._watch_tokens is not None:
            tokens.update(t.lower() for t in await self._watch_tokens(chain) if t)
        tokens.update(await self._discovered(chain, wallet))
        tokens.discard(NATIVE_ADDRESS)
        read_now = await evm.erc20_balances(wallet.address, sorted(tokens)) if tokens else {}
        # A token the node did not answer for gets no opening and keeps the
        # balance row it had: unknown is not zero.
        unread = sorted(t for t, raw in read_now.items() if raw is None)
        erc20_now: dict[str, int] = {t: raw for t, raw in read_now.items() if raw is not None}
        net: dict[str, int] = defaultdict(int)
        for g in groups:
            for token, amount in g.ins.items():
                net[token] += amount
            for token, amount in g.outs.items():
                net[token] -= amount
        for o in swaps:
            spent, received, token_in, token_out, _gas = _order_legs(o)
            net[token_in] -= spent
            net[token_out] += received
        for o in sends:
            token, amount, _recipient, _gas = _send_leg(o)
            net[token] -= amount
        for o in lp_writes:
            ins, outs, _gas = _lp_moves(o)
            for token, amount, _index in ins:
                net[token] += amount
            for token, amount, _index in outs:
                net[token] -= amount
        openings: dict[str, int] = {}
        for token, balance in {**erc20_now, NATIVE_ADDRESS: native_now}.items():
            unexplained = int(balance) - net.get(token, 0)
            if unexplained > 0:
                openings[token] = unexplained
        first_ts = events[0][0] if events else self._now()
        opening_ts = min(first_ts - 1, self._now())

        # Resolve every price and token before touching the ledger, so the
        # transaction below never waits on the network.
        metas: dict[str, TokenMeta] = {}
        priced: dict[tuple[str, int], Priced] = {}

        async def prepare(token: str, ts: float) -> None:
            if token not in metas:
                metas[token] = await self._token_meta(chain, token)
            key = (token, int(ts))
            if key not in priced:
                priced[key] = await self._price_for(chain, token, ts)

        for token in openings:
            await prepare(token, opening_ts)
        for _ts, _block, kind, payload in events:
            if kind == "tx":
                for token in [*payload.ins, *payload.outs]:
                    await prepare(token, payload.ts)
            elif kind == "order":
                _spent, _received, token_in, token_out, _gas = _order_legs(payload)
                await prepare(token_in, _ts)
                await prepare(token_out, _ts)
                await prepare(NATIVE_ADDRESS, _ts)
            elif kind == "send":
                token, _amount, _recipient, _gas = _send_leg(payload)
                await prepare(token, _ts)
                await prepare(NATIVE_ADDRESS, _ts)
            elif kind == "lp":
                ins, outs, _gas = _lp_moves(payload)
                for token, _amount, _index in [*ins, *outs]:
                    await prepare(token, _ts)
                await prepare(NATIVE_ADDRESS, _ts)
            else:
                await prepare(NATIVE_ADDRESS, _ts)

        def gas_usd_at(gas_wei: int, ts: float) -> float | None:
            eth_price = priced[(NATIVE_ADDRESS, int(ts))][0]
            if eth_price is None or not gas_wei:
                return None
            return float(to_human(gas_wei, 18)) * eth_price

        # Everything the booking helpers await below is answered from
        # ``metas``/``priced`` and never reaches the network, so no other
        # coroutine runs while this transaction is open: the ledger's
        # transaction depth is process-wide, and a write from elsewhere would
        # otherwise join this group and be lost with it on failure.
        with self.ledger.transaction():
            self.ledger.delete_chain_history(address, chain.chain_id)
            for token, amount in openings.items():
                await self._book_deposit(
                    wallet,
                    chain,
                    token,
                    amount,
                    opening_ts,
                    tx_hash=None,
                    log_index=0,
                    note=OPENING_NOTE,
                    meta=metas[token],
                    priced=priced[(token, int(opening_ts))],
                )
            for ts, _block, kind, payload in events:
                if kind == "tx":
                    await self._book_group(wallet, chain, payload, metas=metas, priced=priced)
                elif kind == "order":
                    spent, received, token_in, token_out, gas_wei = _order_legs(payload)
                    await self._book_swap(
                        wallet,
                        chain,
                        token_in=token_in,
                        amount_in=spent,
                        token_out=token_out,
                        amount_out=received,
                        ts=ts,
                        tx_hash=str(payload["tx_hash"]),
                        log_index=0,
                        initiator=str(payload.get("initiator") or "agent"),
                        gas_usd=gas_usd_at(gas_wei, ts),
                        order_id=str(payload["order_id"]),
                        session_key=payload.get("session_key"),
                        note=payload.get("note"),
                        metas=metas,
                        priced=priced,
                    )
                elif kind == "send":
                    token, amount, recipient, gas_wei = _send_leg(payload)
                    tx_hash = str(payload["tx_hash"]).lower()
                    # The same log index the settlement used, so a send booked
                    # live and one booked here are the same entry.
                    log_index = next(
                        (
                            t.log_index
                            for t in logs
                            if t.tx_hash == tx_hash
                            and t.token == token
                            and t.sender == address
                            and t.recipient == recipient
                        ),
                        0,
                    )
                    await self._book_withdraw(
                        wallet,
                        chain,
                        token,
                        amount,
                        ts,
                        tx_hash=tx_hash,
                        log_index=log_index,
                        note=payload.get("note") or f"sent to {checksum_address(recipient)}",
                        meta=metas[token],
                        priced=priced[(token, int(ts))],
                        initiator=str(payload.get("initiator") or "agent"),
                        order_id=str(payload["order_id"]),
                        session_key=payload.get("session_key"),
                        gas_usd=gas_usd_at(int(payload.get("gas_wei") or 0), ts),
                    )
                elif kind == "lp":
                    ins, outs, gas_wei = _lp_moves(payload)
                    await self._book_lp(
                        wallet,
                        chain,
                        kind=_order_kind(payload),
                        ins=ins,
                        outs=outs,
                        ts=ts,
                        tx_hash=str(payload["tx_hash"]).lower(),
                        initiator=str(payload.get("initiator") or "agent"),
                        gas_usd=gas_usd_at(gas_wei, ts),
                        order_id=str(payload["order_id"]),
                        session_key=payload.get("session_key"),
                        note=payload.get("note") or _lp_note(payload),
                        metas=metas,
                        priced=priced,
                    )
                else:
                    spender = str(payload.get("recipient") or "").lower()
                    self.ledger.insert_entry(
                        ts=ts,
                        chain_id=chain.chain_id,
                        wallet=address,
                        kind="approval",
                        tx_hash=str(payload["tx_hash"]),
                        log_index=0,
                        token_in=str(payload["token_in"]).lower(),
                        amount_in_raw=0,
                        gas_usd=gas_usd_at(int(payload.get("gas_wei") or 0), ts),
                        initiator=str(payload.get("initiator") or "agent"),
                        order_id=str(payload["order_id"]),
                        session_key=payload.get("session_key"),
                        note=payload.get("note")
                        or f"revoked {spender_label(spender) or checksum_address(spender)}",
                    )
            for token, raw in erc20_now.items():
                self.ledger.set_balance(chain.chain_id, address, token, raw)
            self.ledger.set_balance(chain.chain_id, address, NATIVE_ADDRESS, native_now)
            self.ledger.set_sync_state(
                chain.chain_id, address, last_block=latest, oldest_block=oldest, full=True
            )
            self.ledger.clear_rebuild(chain.chain_id, address)
        self._record_read(chain, address, BalanceRead(changed=True, failed=unread))
        await self._snapshot_prices(wallet, chain)

    # ── transfers ──────────────────────────────────────────────────────

    async def _group_transfers(
        self, chain: ChainSpec, address: str, logs: list[TransferLog]
    ) -> list[TxGroup]:
        by_tx: dict[str, list[TransferLog]] = defaultdict(list)
        for entry in logs:
            by_tx[entry.tx_hash].append(entry)
        groups: list[TxGroup] = []
        for tx_hash, group in by_tx.items():
            ins: dict[str, int] = defaultdict(int)
            outs: dict[str, int] = defaultdict(int)
            senders: set[str] = set()
            for transfer in group:
                senders.add(transfer.sender)
                if transfer.recipient == address and transfer.sender != address:
                    ins[transfer.token] += transfer.amount
                elif transfer.sender == address and transfer.recipient != address:
                    outs[transfer.token] += transfer.amount
            ins = {k: v for k, v in ins.items() if v > 0}
            outs = {k: v for k, v in outs.items() if v > 0}
            if not ins and not outs:
                continue
            block = group[0].block_number
            groups.append(
                TxGroup(
                    tx_hash=tx_hash,
                    block=block,
                    log_index=min(t.log_index for t in group),
                    ts=await self._timestamp(chain, block),
                    ins=dict(ins),
                    outs=dict(outs),
                    senders=senders,
                )
            )
        groups.sort(key=lambda g: (g.block, g.log_index))
        return groups

    async def _record_transfers(
        self, wallet: WalletRecord, chain: ChainSpec, logs: list[TransferLog]
    ) -> bool:
        address = wallet.key
        in_flight = {
            str(o.get("tx_hash") or "").lower()
            for o in self.ledger.list_orders(status="submitted,approved", wallet=address)
            if o.get("tx_hash")
        }
        changed = False
        for group in await self._group_transfers(chain, address, logs):
            if self.ledger.entries_for_tx(address, group.tx_hash):
                continue  # our own confirmed order, or already synced
            if group.tx_hash in in_flight:
                continue  # the confirmation path records it
            changed = await self._book_group(wallet, chain, group) or changed
        return changed

    async def _book_group(
        self,
        wallet: WalletRecord,
        chain: ChainSpec,
        group: TxGroup,
        *,
        metas: dict[str, TokenMeta] | None = None,
        priced: dict[tuple[str, int], Priced] | None = None,
    ) -> bool:
        changed = False
        ins, outs = group.ins, group.outs
        if ins and not outs:
            for token, amount in ins.items():
                meta = _pick(metas, token) or await self._token_meta(chain, token)
                # Unlisted tokens that arrive from strangers are airdrops:
                # nothing was paid, so the lot costs nothing, whatever a
                # thin pool says it is worth. The wallet's own hand in the
                # transaction (a self-send, a contract it called) makes it
                # a real acquisition.
                airdrop = not meta.native and not meta.verified and wallet.key not in group.senders
                if await self._book_deposit(
                    wallet,
                    chain,
                    token,
                    amount,
                    group.ts,
                    tx_hash=group.tx_hash,
                    log_index=group.log_index,
                    meta=meta,
                    priced=_pick(priced, (token, int(group.ts))),
                    airdrop=airdrop,
                ):
                    changed = True
        elif outs and not ins:
            for token, amount in outs.items():
                if await self._book_withdraw(
                    wallet,
                    chain,
                    token,
                    amount,
                    group.ts,
                    tx_hash=group.tx_hash,
                    log_index=group.log_index,
                    meta=_pick(metas, token),
                    priced=_pick(priced, (token, int(group.ts))),
                ):
                    changed = True
        else:
            token_in, amount_in = max(outs.items(), key=lambda kv: kv[1])
            token_out, amount_out = max(ins.items(), key=lambda kv: kv[1])
            if await self._book_swap(
                wallet,
                chain,
                token_in=token_in,
                amount_in=amount_in,
                token_out=token_out,
                amount_out=amount_out,
                ts=group.ts,
                tx_hash=group.tx_hash,
                log_index=group.log_index,
                initiator="external",
                metas=metas,
                priced=priced,
            ):
                changed = True
        return changed

    async def _timestamp(self, chain: ChainSpec, block: int) -> float:
        key = (chain.chain_id, block)
        cached = self._block_ts.get(key)
        if cached is not None:
            return float(cached)
        evm = self._evm_for(chain)
        ts = await evm.block_timestamp(block)
        if ts is None:
            ts = int(self._now())
        if len(self._block_ts) > 4096:
            self._block_ts.clear()
        self._block_ts[key] = ts
        return float(ts)

    # ── booking helpers (shared with the service) ──────────────────────

    async def _price_for(self, chain: ChainSpec, token: str, ts: float) -> Priced:
        """USD price of one whole token at ``ts`` and where it came from."""
        recent = abs(self._now() - ts) < 3600
        if not recent:
            historical = await self.prices.price_at(chain, token, int(ts))
            if historical is not None:
                return historical, "historical"
        spot = await self.prices.price(chain, token)
        if spot is not None:
            return spot, "spot" if recent else "approx"
        return None, "unknown"

    async def _book_deposit(
        self,
        wallet: WalletRecord,
        chain: ChainSpec,
        token: str,
        amount: int,
        ts: float,
        *,
        tx_hash: str | None,
        log_index: int,
        note: str | None = None,
        meta: TokenMeta | None = None,
        priced: Priced | None = None,
        airdrop: bool = False,
    ) -> bool:
        """Book an inbound amount as a deposit and open its lot.

        An ``airdrop`` keeps the spot price for information but has no
        value and a lot at cost zero: the whole holding is unrealized gain.
        """
        meta = meta or await self._token_meta(chain, token)
        price, source = priced if priced is not None else await self._price_for(chain, token, ts)
        value = float(to_human(amount, meta.decimals)) * price if price is not None else None
        if airdrop:
            value, source = None, AIRDROP_SOURCE
        entry_id = self.ledger.insert_entry(
            ts=ts,
            chain_id=chain.chain_id,
            wallet=wallet.key,
            kind="deposit",
            tx_hash=tx_hash,
            log_index=log_index,
            token_out=token,
            amount_out_raw=amount,
            value_usd=value,
            price_out_usd=price,
            cost_basis_source=source,
            initiator="external",
            note=note,
        )
        if entry_id is None:
            return False
        self.ledger.add_lot(
            chain.chain_id,
            wallet.key,
            token,
            amount_raw=amount,
            cost_usd_per_raw=(
                per_raw(price, meta.decimals) if value is not None and price is not None else 0.0
            ),
            acquired_at=ts,
            entry_id=entry_id,
        )
        return True

    async def _book_withdraw(
        self,
        wallet: WalletRecord,
        chain: ChainSpec,
        token: str,
        amount: int,
        ts: float,
        *,
        tx_hash: str | None,
        log_index: int,
        note: str | None = None,
        meta: TokenMeta | None = None,
        priced: Priced | None = None,
        initiator: str = "external",
        order_id: str | None = None,
        session_key: str | None = None,
        gas_usd: float | None = None,
    ) -> bool:
        meta = meta or await self._token_meta(chain, token)
        price, source = priced if priced is not None else await self._price_for(chain, token, ts)
        value = float(to_human(amount, meta.decimals)) * price if price is not None else None
        entry_id = self.ledger.insert_entry(
            ts=ts,
            chain_id=chain.chain_id,
            wallet=wallet.key,
            kind="withdraw",
            tx_hash=tx_hash,
            log_index=log_index,
            token_in=token,
            amount_in_raw=amount,
            value_usd=value,
            price_in_usd=price,
            cost_basis_source=source,
            initiator=initiator,
            order_id=order_id,
            session_key=session_key,
            gas_usd=gas_usd,
            note=note,
        )
        if entry_id is None:
            return False
        self._consume(chain, wallet.key, token, amount, value, ts, entry_id)
        return True

    async def _book_lp(
        self,
        wallet: WalletRecord,
        chain: ChainSpec,
        *,
        kind: str,
        ins: list[tuple[str, int, int]],
        outs: list[tuple[str, int, int]],
        ts: float,
        tx_hash: str | None,
        initiator: str,
        gas_usd: float | None = None,
        order_id: str | None = None,
        session_key: str | None = None,
        note: str | None = None,
        metas: dict[str, TokenMeta] | None = None,
        priced: dict[tuple[str, int], Priced] | None = None,
    ) -> bool:
        """Book a Uniswap V4 LP write: one entry of ``kind`` per token that moved.

        ``ins``/``outs`` are ``(token, amount_raw, log_index)``. The lots follow
        the deposit/withdraw convention: a token that came out of the pool
        (fees, principal) opens a lot at the price of the moment, a token put
        into the pool is consumed FIFO like a withdrawal (realizing its PnL).
        Gas rides on the first entry; a write that moved nothing (a collect
        with no fees) still books one entry, for its gas.
        """
        booked = False
        gas = gas_usd
        moves = [(True, *m) for m in ins if m[1] > 0] + [(False, *m) for m in outs if m[1] > 0]
        if not moves:
            entry_id = self.ledger.insert_entry(
                ts=ts,
                chain_id=chain.chain_id,
                wallet=wallet.key,
                kind=kind,
                tx_hash=tx_hash,
                log_index=0,
                gas_usd=gas,
                initiator=initiator,
                order_id=order_id,
                session_key=session_key,
                note=note,
            )
            return entry_id is not None
        for inbound, token, amount, log_index in moves:
            meta = _pick(metas, token) or await self._token_meta(chain, token)
            pin = _pick(priced, (token, int(ts)))
            price, source = pin if pin is not None else await self._price_for(chain, token, ts)
            value = float(to_human(amount, meta.decimals)) * price if price is not None else None
            side: dict[str, Any] = (
                {"token_out": token, "amount_out_raw": amount, "price_out_usd": price}
                if inbound
                else {"token_in": token, "amount_in_raw": amount, "price_in_usd": price}
            )
            entry_id = self.ledger.insert_entry(
                ts=ts,
                chain_id=chain.chain_id,
                wallet=wallet.key,
                kind=kind,
                tx_hash=tx_hash,
                log_index=log_index,
                value_usd=value,
                gas_usd=gas,
                cost_basis_source=source,
                initiator=initiator,
                order_id=order_id,
                session_key=session_key,
                note=note,
                **side,
            )
            gas = None
            if entry_id is None:
                continue
            booked = True
            if inbound:
                self.ledger.add_lot(
                    chain.chain_id,
                    wallet.key,
                    token,
                    amount_raw=amount,
                    cost_usd_per_raw=(
                        per_raw(price, meta.decimals)
                        if value is not None and price is not None
                        else 0.0
                    ),
                    acquired_at=ts,
                    entry_id=entry_id,
                )
            else:
                self._consume(chain, wallet.key, token, amount, value, ts, entry_id)
        return booked

    async def _book_swap(
        self,
        wallet: WalletRecord,
        chain: ChainSpec,
        *,
        token_in: str,
        amount_in: int,
        token_out: str,
        amount_out: int,
        ts: float,
        tx_hash: str | None,
        log_index: int,
        initiator: str,
        gas_usd: float | None = None,
        order_id: str | None = None,
        session_key: str | None = None,
        note: str | None = None,
        metas: dict[str, TokenMeta] | None = None,
        priced: dict[tuple[str, int], Priced] | None = None,
    ) -> bool:
        meta_in = _pick(metas, token_in) or await self._token_meta(chain, token_in)
        meta_out = _pick(metas, token_out) or await self._token_meta(chain, token_out)
        pin = _pick(priced, (token_in, int(ts)))
        pout = _pick(priced, (token_out, int(ts)))
        price_in, source_in = pin if pin is not None else await self._price_for(chain, token_in, ts)
        price_out, source_out = (
            pout if pout is not None else await self._price_for(chain, token_out, ts)
        )
        value, leg = swap_value(
            chain,
            meta_in=meta_in,
            amount_in=amount_in,
            price_in=price_in,
            meta_out=meta_out,
            amount_out=amount_out,
            price_out=price_out,
        )
        if value is None:
            source = "unknown"
        else:
            source = f"{source_in if leg == 'in' else source_out}:{leg}"
        entry_id = self.ledger.insert_entry(
            ts=ts,
            chain_id=chain.chain_id,
            wallet=wallet.key,
            kind="swap",
            tx_hash=tx_hash,
            log_index=log_index,
            token_in=token_in,
            amount_in_raw=amount_in,
            token_out=token_out,
            amount_out_raw=amount_out,
            value_usd=value,
            gas_usd=gas_usd,
            price_in_usd=price_in,
            price_out_usd=price_out,
            cost_basis_source=source,
            initiator=initiator,
            order_id=order_id,
            session_key=session_key,
            note=note,
        )
        if entry_id is None:
            return False
        self._consume(chain, wallet.key, token_in, amount_in, value, ts, entry_id)
        if amount_out > 0:
            cost_per_raw = (value / amount_out) if value is not None else 0.0
            self.ledger.add_lot(
                chain.chain_id,
                wallet.key,
                token_out,
                amount_raw=amount_out,
                cost_usd_per_raw=cost_per_raw,
                acquired_at=ts,
                entry_id=entry_id,
            )
        return True

    def _consume(
        self,
        chain: ChainSpec,
        wallet: str,
        token: str,
        amount: int,
        proceeds_usd: float | None,
        ts: float,
        entry_id: int,
    ) -> None:
        lots = self.ledger.open_lots(chain.chain_id, wallet, token)
        result = sell_fifo(lots, amount, proceeds_usd)
        self.ledger.save_lots(lots)
        matched = amount - result.unmatched_raw
        if matched > 0:
            self.ledger.add_realized(
                entry_id=entry_id,
                chain_id=chain.chain_id,
                wallet=wallet,
                token=token,
                amount_raw=matched,
                proceeds_usd=result.proceeds_usd,
                cost_usd=result.cost_usd,
                ts=ts,
            )

    # ── openings ───────────────────────────────────────────────────────

    async def _reconcile_openings(
        self, wallet: WalletRecord, chain: ChainSpec, tokens: list[str] | None = None
    ) -> bool:
        """Make each ERC-20 position's opening entry equal what the sweep cannot explain.

        ``balance − (scanned in − scanned out)``: zero or less means the sweep
        explains everything and any opening goes; a change replays the
        position's lots so FIFO cost basis follows the entries.
        """
        address = wallet.key
        wanted = tokens if tokens is not None else await self.tokens_of_interest(address, chain)
        changed = False
        for token in wanted:
            token = token.lower()
            if token == NATIVE_ADDRESS:
                continue
            balance = self.ledger.get_balance(chain.chain_id, address, token)
            if balance is None:
                continue
            net = self.ledger.scanned_net_raw(chain.chain_id, address, token)
            target = max(0, int(balance) - net)
            existing = self.ledger.opening_entry(chain.chain_id, address, token)
            current = int(existing["amount_out_raw"]) if existing else 0
            if target == current:
                continue
            others = [
                e
                for e in self.ledger.token_entries(chain.chain_id, address, token)
                if not (e["tx_hash"] is None and e.get("note") == OPENING_NOTE)
            ]
            first_ts = min((float(e["ts"]) for e in others), default=None)
            ts = min(first_ts - 1, self._now()) if first_ts is not None else self._now()
            meta = await self._token_meta(chain, token)
            price = await self._price_for(chain, token, ts) if target > 0 else (None, "unknown")
            with self.ledger.transaction():
                if existing:
                    self.ledger.delete_entry(int(existing["id"]))
                if target > 0:
                    await self._book_deposit(
                        wallet,
                        chain,
                        token,
                        target,
                        ts,
                        tx_hash=None,
                        log_index=0,
                        note=OPENING_NOTE,
                        meta=meta,
                        priced=price,
                    )
                self._replay_lots(chain, address, token)
            changed = True
        return changed

    def _replay_lots(self, chain: ChainSpec, wallet: str, token: str) -> None:
        """Rebuild one position's lots and realized rows from its entries, oldest first."""
        self.ledger.reset_token_lots(chain.chain_id, wallet, token)
        for e in self.ledger.token_entries(chain.chain_id, wallet, token):
            ts = float(e["ts"])
            entry_id = int(e["id"])
            value = e.get("value_usd")
            if e.get("token_in") == token and e.get("amount_in_raw"):
                self._consume(
                    chain,
                    wallet,
                    token,
                    int(e["amount_in_raw"]),
                    float(value) if value is not None else None,
                    ts,
                    entry_id,
                )
            if e.get("token_out") == token and e.get("amount_out_raw"):
                amount = int(e["amount_out_raw"])
                cost_per_raw = (float(value) / amount) if value is not None and amount else 0.0
                self.ledger.add_lot(
                    chain.chain_id,
                    wallet,
                    token,
                    amount_raw=amount,
                    cost_usd_per_raw=cost_per_raw,
                    acquired_at=ts,
                    entry_id=entry_id,
                )

    # ── native / balances / snapshots ──────────────────────────────────

    def _has_open_orders(self, wallet: WalletRecord, chain: ChainSpec) -> bool:
        return any(
            int(o["chain_id"]) == chain.chain_id
            for o in self.ledger.list_orders(status="submitted,approved", wallet=wallet.key)
        )

    async def _reconcile_native(
        self, wallet: WalletRecord, chain: ChainSpec, evm: EvmClient
    ) -> bool:
        # A swap in flight moves the native balance before its confirm books
        # it. Booking that delta here as a withdrawal (or a deposit) would
        # count the swap twice; the order's own settlement refreshes the
        # cache, so skip the wallet until it has settled.
        if self._has_open_orders(wallet, chain):
            return False
        balance = await evm.get_balance(wallet.address)
        cached = self.ledger.get_balance(chain.chain_id, wallet.key, NATIVE_ADDRESS)
        changed = False
        ts = self._now()
        if cached is None:
            opened = self.ledger.opening_entry(chain.chain_id, wallet.key, NATIVE_ADDRESS)
            if balance > 0 and opened is None:
                changed = await self._book_deposit(
                    wallet,
                    chain,
                    NATIVE_ADDRESS,
                    balance,
                    ts,
                    tx_hash=None,
                    log_index=0,
                    note=OPENING_NOTE,
                )
        else:
            diff = balance - cached
            if diff > NATIVE_DUST_WEI:
                changed = await self._book_deposit(
                    wallet, chain, NATIVE_ADDRESS, diff, ts, tx_hash=None, log_index=int(ts)
                )
            elif diff < -NATIVE_DUST_WEI:
                changed = await self._book_withdraw(
                    wallet, chain, NATIVE_ADDRESS, -diff, ts, tx_hash=None, log_index=int(ts)
                )
        self.ledger.set_balance(chain.chain_id, wallet.key, NATIVE_ADDRESS, balance)
        return changed

    async def tokens_of_interest(self, wallet: str, chain: ChainSpec) -> list[str]:
        """ERC-20s worth reading for a wallet: held, seen, well-known, or indexed."""
        tokens: set[str] = set()
        for pos in self.ledger.positions(wallet):
            if pos.chain_id == chain.chain_id and pos.token != NATIVE_ADDRESS:
                tokens.add(pos.token)
        for row in self.ledger.balances(wallet, chain.chain_id):
            if row["token"] != NATIVE_ADDRESS:
                tokens.add(str(row["token"]))
        if self._watch_tokens is not None:
            for token in await self._watch_tokens(chain):
                if token and token != NATIVE_ADDRESS:
                    tokens.add(token.lower())
        tokens.update(await self._discovered(chain, wallet))
        tokens.discard(NATIVE_ADDRESS)
        if not self.include_hidden:
            tokens -= self.ledger.hidden_tokens(chain.chain_id)
        return sorted(tokens)

    async def _discovered(self, chain: ChainSpec, wallet: str | WalletRecord) -> set[str]:
        """What an indexer says the wallet holds; empty when there is no answer."""
        if self._discover_tokens is None:
            return set()
        address = wallet.address if isinstance(wallet, WalletRecord) else wallet
        try:
            found = await self._discover_tokens(chain, address)
        except Exception as exc:  # discovery is advisory; the sweep must not fail on it
            log.debug("trading.discovery_error", chain=chain.key, error=str(exc))
            return set()
        return {t.lower() for t in found if t}

    async def ensure_opening(
        self, wallet: WalletRecord, chain: ChainSpec, token: str, evm: EvmClient
    ) -> bool:
        """First sight of a token in a wallet: read it, and book the unexplained part.

        Before the first sweep of this wallet/chain nothing is booked — the
        sweep owns openings, otherwise the same tokens would be counted once
        here and again when their transfers are scanned.
        """
        if token == NATIVE_ADDRESS:
            return await self._reconcile_native(wallet, chain, evm)
        raw = await evm.erc20_balance_of(token, wallet.address)
        self.ledger.set_balance(chain.chain_id, wallet.key, token, raw)
        if self.ledger.sync_state(chain.chain_id, wallet.key) is None:
            return False
        return await self._reconcile_openings(wallet, chain, tokens=[token])

    async def _refresh_balances(
        self, wallet: WalletRecord, chain: ChainSpec, evm: EvmClient
    ) -> BalanceRead:
        """Re-read every ERC-20 of interest; a failed read leaves its row alone."""
        read = BalanceRead()
        tokens = await self.tokens_of_interest(wallet.key, chain)
        if not tokens:
            return read
        balances = await evm.erc20_balances(wallet.address, tokens)
        for token, raw in balances.items():
            if raw is None:
                read.failed.append(token)
                continue
            before = self.ledger.get_balance(chain.chain_id, wallet.key, token)
            if before != raw:
                read.changed = True
            self.ledger.set_balance(chain.chain_id, wallet.key, token, raw)
        if read.failed:
            log.warning(
                "trading.balance_read_incomplete",
                chain=chain.key,
                wallet=wallet.address,
                failed=len(read.failed),
                scanned=len(tokens),
            )
        return read

    async def _snapshot_prices(self, wallet: WalletRecord, chain: ChainSpec) -> None:
        tokens = [
            t
            for t in await self.tokens_of_interest(wallet.key, chain)
            if (self.ledger.get_balance(chain.chain_id, wallet.key, t) or 0) > 0
        ]
        wanted = [*tokens, NATIVE_ADDRESS]
        prices = await self.prices.prices(chain, wanted)
        ts = float(int(self._now()))
        for token, info in prices.items():
            if info.price_usd is not None:
                self.ledger.add_snapshot(chain.chain_id, token, ts, info.price_usd)


def _order_kind(order: dict[str, Any]) -> str:
    return str(order.get("kind") or "swap")


def _lp_settlement(order: dict[str, Any]) -> dict[str, Any]:
    try:
        plan = json.loads(str(order.get("quote_json") or "{}"))
    except ValueError:
        return {}
    settled = plan.get("settlement") if isinstance(plan, dict) else None
    return settled if isinstance(settled, dict) else {}


def _lp_moves(
    order: dict[str, Any],
) -> tuple[list[tuple[str, int, int]], list[tuple[str, int, int]], int]:
    """(ins, outs, gas_wei) of a confirmed LP write, as its settlement recorded them."""
    settled = _lp_settlement(order)

    def moves(key: str) -> list[tuple[str, int, int]]:
        out = []
        for item in settled.get(key) or []:
            try:
                token, amount, index = item
                out.append((str(token).lower(), int(amount), int(index)))
            except (TypeError, ValueError):
                continue
        return out

    return moves("ins"), moves("outs"), int(order.get("gas_wei") or 0)


def lp_note(kind: str, plan: dict[str, Any]) -> str:
    """The ledger's default label for an LP write (``docs/lp-write.md``, desk ledger)."""
    token_id = plan.get("tokenId")
    if kind == "lp_collect":
        return f"Collect fees · #{token_id}"
    if kind == "lp_remove":
        pct = plan.get("pct")
        share = f" · {float(pct):g}%" if pct is not None else ""
        return f"Remove liquidity · #{token_id}{share}"
    pair = "/".join(str((plan.get(k) or {}).get("symbol") or "?") for k in ("token", "quote"))
    usd = (plan.get("expected") or {}).get("usd")
    worth = f" · ${float(usd):,.2f}" if isinstance(usd, int | float) else ""
    where = f" · #{token_id}" if token_id else ""
    return f"Add liquidity · {pair}{worth}{where}"


def _lp_note(order: dict[str, Any]) -> str:
    try:
        plan = json.loads(str(order.get("quote_json") or "{}"))
    except ValueError:
        plan = {}
    return lp_note(_order_kind(order), plan if isinstance(plan, dict) else {})


def _send_leg(order: dict[str, Any]) -> tuple[str, int, str, int]:
    """(token, amount_raw, recipient, gas_wei) of a confirmed send."""
    token = str(order["token_in"]).lower()
    amount = int(order.get("spent_in_raw") or order.get("amount_raw") or 0)
    recipient = str(order.get("recipient") or "").lower()
    gas_wei = int(order.get("gas_wei") or 0)
    return token, amount, recipient, gas_wei


def _order_legs(order: dict[str, Any]) -> tuple[int, int, str, str, int]:
    """(spent_raw, received_raw, token_in, token_out, gas_wei) of a confirmed *swap*.

    A native receipt below the quote's minimum that the settlement did not
    flag as a short fill is a mis-measured balance diff, not a bad trade
    (the swap would have reverted): the quote's expected amount stands in
    for it, so a rebuild does not re-derive a lot priced off a stray number.
    """
    spent = int(order.get("spent_in_raw") or order.get("amount_raw") or 0)
    received = int(order.get("received_out_raw") or order.get("expected_out_raw") or 0)
    token_in = str(order["token_in"]).lower()
    token_out = str(order.get("delivered_token") or order["token_out"]).lower()
    gas_wei = int(order.get("gas_wei") or 0)
    min_out = int(order.get("min_out_raw") or 0)
    expected = int(order.get("expected_out_raw") or 0)
    if (
        token_out == NATIVE_ADDRESS
        and str(order.get("status") or "") == "confirmed"
        and 0 < received < min_out
        and expected > 0
        and "short fill" not in str(order.get("reason") or "")
    ):
        log.warning(
            "trading.native_receipt_implausible",
            order=order.get("order_id"),
            received=received,
            min_out=min_out,
            using=expected,
        )
        received = expected
    return spent, received, token_in, token_out, gas_wei


def _leg_rank(chain: ChainSpec, meta: TokenMeta) -> int:
    """How much to trust a leg's price: stable 3, native 2, listed 1, else 0."""
    if meta.native:
        return 2
    address = meta.address.lower()
    if chain.usdc and address == chain.usdc.lower():
        return 3
    if meta.verified and meta.symbol.upper() in STABLE_SYMBOLS:
        return 3
    return 1 if meta.verified else 0


def swap_value(
    chain: ChainSpec,
    *,
    meta_in: TokenMeta,
    amount_in: int,
    price_in: float | None,
    meta_out: TokenMeta,
    amount_out: int,
    price_out: float | None,
) -> tuple[float | None, str]:
    """USD value of a swap by its most reliable priced leg, and which leg (``in``/``out``).

    A stablecoin leg is the trade's dollar amount to the cent; the native
    coin's price is deep and everywhere; a listed token's is usually fine;
    an unlisted token's comes from whatever pool DexScreener found and can
    be off by orders of magnitude. Both proceeds of the sold leg and cost
    of the bought lot are this one number. Equal trust goes to the in-leg.
    ``(None, "in")`` when neither leg has a price.
    """
    best: tuple[int, str, float] | None = None
    for leg, meta, amount, price in (
        ("in", meta_in, amount_in, price_in),
        ("out", meta_out, amount_out, price_out),
    ):
        if price is None:
            continue
        rank = _leg_rank(chain, meta)
        if best is None or rank > best[0]:
            best = (rank, leg, float(to_human(amount, meta.decimals)) * price)
    if best is None:
        return None, "in"
    return best[2], best[1]


def _pick(table: dict[Any, Any] | None, key: Any) -> Any:
    return table.get(key) if table else None


def summarize_transfers(logs: list[TransferLog], wallet: str) -> dict[str, dict[str, int]]:
    """Net in/out per token for a wallet (test helper and debugging aid)."""
    ins: dict[str, int] = defaultdict(int)
    outs: dict[str, int] = defaultdict(int)
    for transfer in logs:
        if transfer.recipient == wallet:
            ins[transfer.token] += transfer.amount
        if transfer.sender == wallet:
            outs[transfer.token] += transfer.amount
    return {"in": dict(ins), "out": dict(outs)}
