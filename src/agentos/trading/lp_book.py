"""The liquidity book: every LP position of the vault's wallets, cached.

The desktop's Liquidity tab reads this, the way a wallet tracker lists DeFi
positions: Uniswap V4 (``lp.py``) plus Uniswap v2/v3, Aero and Aerodrome
(``lp_amm.py``), on Base and Robinhood Chain. ``docs/lp-book.md`` is the
contract.

It is built to cost little RPC:

* **Demand driven.** Nothing is read until someone asks (``trading.lp.book``).
  A read answers at once from the ledger's cache and, when that cache is
  older than ``BOOK_TTL_S``, starts one refresh in the background; its end is
  announced as ``trading.changed`` (``reason: "lp"``) so clients re-read. A
  closed tab costs nothing.
* **One pass for every wallet.** A refresh batches all wallets of a chain
  into the same multicalls; the two chains run side by side.
* **Warm starts.** What the last read found (V4 token ids, which gauges hold
  a stake) seeds the next one, so the V4 log search and the walk over every
  gauge only run when something is missing: on a wallet's first read, after
  the wallet's balances moved, on an explicit refresh, or a few times a day.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from agentos.paths import state_dir
from agentos.trading import lp, lp_amm
from agentos.trading.chains import ChainSpec, is_native

if TYPE_CHECKING:  # pragma: no cover
    from agentos.trading.service import TradingService

log = structlog.get_logger(__name__)

PAYLOAD_VERSION = 1
#: A cached read older than this is refreshed when someone looks at the book.
BOOK_TTL_S = 60.0
#: An explicit refresh (or one after the wallet's balances moved) is honoured
#: at most this often.
FORCED_MIN_S = 10.0
#: While a wallet/chain has never been read (its chain may be down), a new
#: attempt starts at most this often.
RETRY_MIN_S = 30.0
#: Every gauge is asked again this often...
DEEP_TTL_S = 6 * 3600.0
#: ...or this soon after the wallet's balances moved or a refresh was asked for.
DEEP_MIN_S = 300.0
#: Wall-clock budget of the V4 search inside one refresh (``lp.POSITIONS_BUDGET_S``).
V4_BUDGET_S = 25.0
#: A V4 search that could not account for every NFT (the node refused the
#: logs, or the budget ran out) is not run again for this long; meanwhile a
#: refresh re-reads the positions it does know. An explicit refresh searches.
V4_SEARCH_RETRY_S = 900.0
#: A chain still reading this long after a refresh started keeps its old rows.
CHAIN_TIMEOUT_S = 600.0
#: Token metadata (symbol, decimals, supply) is re-read after this long.
META_TTL_S = 3600.0
#: Rows one answer lists; the totals still cover every position.
MAX_ROWS = 200

_GROUP = {"above-range": 0, "below-range": 0, "in-range": 1, "full-range": 1, "closed": 2}
_CLI_HINT = "; pass --budget-seconds to look further"
_CLI_WAIT_HINT = "; pass --budget-seconds to wait longer"


@dataclass
class _ChainResult:
    spec: ChainSpec
    rows: list[dict[str, Any]] = field(default_factory=list)
    block: int = 0
    partial: bool = False
    warnings: list[str] = field(default_factory=list)
    #: Which halves answered: a half that did not keeps its cached rows.
    v4_ok: bool = False
    amm: lp_amm.AmmRead | None = None


class LpBook:
    """Reads, caches and serves the vault's liquidity positions."""

    def __init__(self, service: TradingService, *, now: Callable[[], float] = time.time) -> None:
        self.service = service
        self._now = now
        self._task: asyncio.Task[None] | None = None
        self._started_at = 0.0
        #: Wallets (lower-cased; ``"*"`` for all) whose balances moved since
        #: their gauges were last walked.
        self._dirty: set[str] = set()
        self._stores: dict[int, lp_amm.AmmStore] = {}
        self._metas: dict[int, tuple[float, dict[str, dict[str, Any]]]] = {}
        #: Chain id -> why its last refresh failed outright.
        self._errors: dict[int, str] = {}
        #: Chain id -> when its V4 search last came back incomplete.
        self._v4_short_at: dict[int, float] = {}
        self._again = False

    # ── what the service tells the book ────────────────────────────────

    def mark_dirty(self, wallet: str | None = None) -> None:
        """A wallet's balances moved (or an order filled): look at its gauges again."""
        self._dirty.add(wallet.lower() if wallet else "*")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutting down
                pass

    @property
    def refreshing(self) -> bool:
        return self._task is not None and not self._task.done()

    # ── the V4 ids every read shares ───────────────────────────────────

    def _amm_store(self, chain_id: int) -> lp_amm.AmmStore:
        store = self._stores.get(chain_id)
        if store is None:
            store = lp_amm.AmmStore.load(state_dir("lp", f"amm-{chain_id}.json"))
            self._stores[chain_id] = store
        return store

    def v4_ids(self, spec: ChainSpec, owners: list[str]) -> tuple[int, ...]:
        """The V4 position ids the last reads found for ``owners`` (``ChainEnv.known_ids``)."""
        nfts = self._amm_store(spec.chain_id).nfts
        return tuple(
            sorted(
                {
                    int(i)
                    for owner in owners
                    for i in (nfts.get(_v4_slot(owner)) or {}).get("ids") or []
                }
            )
        )

    def learn_v4(
        self, spec: ChainSpec, owners: list[str], found: list[dict[str, Any]], *, partial: bool
    ) -> bool:
        """Keep the ids a V4 read ``found`` for ``owners``; whether any is new.

        A complete read replaces a wallet's ids; an incomplete one only adds,
        since it proves what it found, not what it missed.
        """
        store = self._amm_store(spec.chain_id)
        learned = False
        for owner in owners:
            before = (store.nfts.get(_v4_slot(owner)) or {}).get("ids") or []
            ids = {int(p["tokenId"]) for p in found if _held_by(p, owner)}
            if partial:
                ids |= {int(i) for i in before}
            if sorted(ids) != before:
                learned = learned or bool(ids - {int(i) for i in before})
                store.nfts[_v4_slot(owner)] = {"ids": sorted(ids)}
                store.changed = True
        store.save()
        return learned

    # ── reading ────────────────────────────────────────────────────────

    async def read(
        self,
        *,
        wallet: str | None = None,
        chain: ChainSpec | None = None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """The cached book (one wallet and/or chain when named); refreshes when stale."""
        owners = self._owners()
        scope = owners
        if wallet:
            address = self.service.vault.resolve(wallet).address.lower()
            scope = {address: owners.get(address)}
        if owners and self._due(owners, refresh):
            self._start(refresh)
        return self._payload(scope, chain)

    def _owners(self) -> dict[str, str | None]:
        vault = self.service.vault
        if not vault.initialized:
            return {}
        return {record.address.lower(): record.label for record in vault.list()}

    def _due(self, owners: dict[str, str | None], forced: bool) -> bool:
        if self.refreshing:
            return False
        now = self._now()
        reads = self._reads()
        ages = [
            now - float(reads[(spec.chain_id, owner)]["read_at"])
            if (spec.chain_id, owner) in reads
            else None
            for spec in self.service.chains()
            for owner in owners
        ]
        if any(age is None for age in ages):
            return now - self._started_at >= RETRY_MIN_S
        oldest = max((age for age in ages if age is not None), default=0.0)
        if oldest >= BOOK_TTL_S:
            return True
        moved = bool(self._dirty & ({"*"} | set(owners)))
        return (forced or moved) and now - self._started_at >= FORCED_MIN_S

    def _reads(self) -> dict[tuple[int, str], dict[str, Any]]:
        return {(int(r["chain_id"]), str(r["wallet"])): r for r in self.service.ledger.lp_reads()}

    def _start(self, forced: bool) -> None:
        self._started_at = self._now()
        self._task = asyncio.get_running_loop().create_task(
            self._refresh(forced), name="trading-lp-book"
        )

    # ── refreshing ─────────────────────────────────────────────────────

    async def refresh(self, *, forced: bool = False) -> None:
        """Run one refresh now and wait for it (tests and the CLI use this)."""
        if not self.refreshing:
            self._start(forced)
        task = self._task
        if task is not None:
            await task

    async def _refresh(self, forced: bool) -> None:
        owners = list(self._owners())
        try:
            if owners:
                dirty, self._dirty = self._dirty, set()
                reads = self._reads()
                await asyncio.gather(
                    *(
                        self._refresh_chain(spec, owners, reads, dirty, forced)
                        for spec in self.service.chains()
                    )
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a refresh must never take the engine down
            log.warning("trading.lp_book_failed", error=str(exc))
        finally:
            again, self._again = self._again, False
            if again:
                # A position left its wallet between two reads: it may have
                # gone into a gauge, so the next look walks them at once.
                self._started_at = 0.0
            await self.service._emit("trading.changed", {"reason": "lp"})

    def _deep_plan(
        self,
        spec: ChainSpec,
        owners: list[str],
        reads: dict[tuple[int, str], dict[str, Any]],
        dirty: set[str],
        forced: bool,
    ) -> tuple[bool, bool]:
        """(deep, full): whether to walk the live gauges, and the frozen ones too."""
        now = self._now()
        deep = full = False
        for owner in owners:
            read = reads.get((spec.chain_id, owner))
            deep_at = read.get("deep_at") if read else None
            if deep_at is None:
                # Never walked for this wallet: everything, old gauges included.
                return True, True
            age = now - float(deep_at)
            moved = forced or "*" in dirty or owner in dirty
            if age >= DEEP_TTL_S or (moved and age >= DEEP_MIN_S):
                deep = True
            if forced and age >= DEEP_MIN_S:
                full = True
        return deep or full, full

    async def _refresh_chain(
        self,
        spec: ChainSpec,
        owners: list[str],
        reads: dict[tuple[int, str], dict[str, Any]],
        dirty: set[str],
        forced: bool,
    ) -> None:
        ledger = self.service.ledger
        old_rows = ledger.lp_positions(chain_id=spec.chain_id)
        deep, full = self._deep_plan(spec, owners, reads, dirty, forced)
        if not deep:
            # Put back the marks this refresh did not act on (too soon after the last walk).
            self._dirty |= dirty & ({"*"} | set(owners))
        try:
            work = asyncio.ensure_future(
                self._read_chain(spec, owners, old_rows, deep=deep, full=full, forced=forced)
            )
            result = await asyncio.wait_for(asyncio.shield(work), timeout=CHAIN_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one chain down still leaves the other
            error = lp._as_trading_error(exc, spec) if not isinstance(exc, TimeoutError) else None
            self._errors[spec.chain_id] = str(error) if error else "the read timed out"
            log.info("trading.lp_book_chain_failed", chain=spec.key, error=str(exc))
            return
        self._errors.pop(spec.chain_id, None)
        self._store(spec, owners, old_rows, result, deep=deep)

    async def _read_chain(
        self,
        spec: ChainSpec,
        owners: list[str],
        old_rows: list[dict[str, Any]],
        *,
        deep: bool,
        full: bool,
        forced: bool = False,
    ) -> _ChainResult:
        loop = asyncio.get_running_loop()
        service = self.service
        v4_env = lp._engine_env(service, spec, loop)
        amm_env = lp._engine_env(service, spec, loop)
        stamp, metas = self._metas.get(spec.chain_id, (0.0, {}))
        if self._now() - stamp >= META_TTL_S:
            stamp, metas = self._now(), {}
            self._metas[spec.chain_id] = (stamp, metas)
        # Both halves share one token-metadata cache, kept between refreshes.
        v4_env._metas = metas
        amm_env._metas = metas
        stakes = {
            (_owner(row), str(row["gauge"]).lower())
            for row in old_rows
            if row.get("staked") and row.get("gauge")
        }
        held: dict[str, set[str]] = {owner: set() for owner in owners}
        for balance in service.ledger.balances(chain_id=spec.chain_id):
            owner = str(balance["wallet"]).lower()
            token = str(balance["token"]).lower()
            if owner in held and not is_native(token) and int(balance["raw"] or 0) > 0:
                held[owner].add(token)
        store = self._amm_store(spec.chain_id)
        # Every V4 NFT the wallets held last time, emptied ones included: the
        # search is only skipped when they account for the whole balance.
        v4_env.known_ids = self.v4_ids(spec, owners)
        short_at = self._v4_short_at.get(spec.chain_id)
        v4_env.known_only = (
            not forced and short_at is not None and self._now() - short_at < V4_SEARCH_RETRY_S
        )
        result = _ChainResult(spec=spec)

        def failed(half: str, exc: Exception) -> None:
            result.partial = True
            result.warnings.append(
                f"{spec.name}: {half} could not be read ({lp._as_trading_error(exc, spec)}); "
                "showing the last known positions"
            )

        def run() -> None:
            started = time.monotonic()
            lp.start(v4_env)
            amm_env.block, amm_env.fetched_at = v4_env.block, v4_env.fetched_at
            reserve = min(
                max(V4_BUDGET_S * lp.POSITIONS_RESERVE_SHARE, lp.POSITIONS_RESERVE_MIN_S),
                lp.POSITIONS_RESERVE_MAX_S,
            )
            v4_env.deadline = started + V4_BUDGET_S - reserve
            v4_env.budget_end = started + V4_BUDGET_S
            v4_env.budget_s = V4_BUDGET_S
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="lp-book") as pool:
                v4 = pool.submit(lp.build_chain_positions, v4_env, owners, False)
                amm = pool.submit(
                    lp_amm.read_positions,
                    amm_env,
                    owners,
                    held=held,
                    stakes=stakes,
                    deep=deep,
                    full=full,
                    store=store,
                )
                try:
                    result.rows += [lp_amm.v4_row(row) for row in v4.result()]
                    result.v4_ok = True
                except Exception as exc:  # noqa: BLE001 - the other half still counts
                    failed("Uniswap V4", exc)
                try:
                    result.amm = amm.result()
                    result.rows += result.amm.rows
                except Exception as exc:  # noqa: BLE001 - the other half still counts
                    failed("the other protocols", exc)
            if result.v4_ok:
                learned = self.learn_v4(
                    spec, owners, v4_env.progress.found or [], partial=v4_env.partial
                )
                if not v4_env.partial or learned:
                    # A search that is getting somewhere (a throttled node gives
                    # up a few wallets per time budget) goes on at the next refresh.
                    self._v4_short_at.pop(spec.chain_id, None)
                elif not v4_env.known_only:
                    self._v4_short_at[spec.chain_id] = self._now()
            result.block = v4_env.block
            result.partial = result.partial or v4_env.partial or amm_env.partial
            for env in (v4_env, amm_env):
                for warning in env.warnings:
                    text = warning.replace(_CLI_HINT, "").replace(_CLI_WAIT_HINT, "")
                    if text not in result.warnings:
                        result.warnings.append(text)

        await asyncio.to_thread(run)
        return result

    def _store(
        self,
        spec: ChainSpec,
        owners: list[str],
        old_rows: list[dict[str, Any]],
        result: _ChainResult,
        *,
        deep: bool,
    ) -> None:
        rows = list(result.rows)
        # A half that failed keeps what the cache had for it.
        for row in old_rows:
            is_v4 = _protocol(row) == lp_amm.UNISWAP_V4
            if (is_v4 and not result.v4_ok) or (not is_v4 and result.amm is None):
                rows.append(row)
        walked = result.amm is not None and result.amm.deep
        if result.amm is not None and not walked and lp_amm.DEPLOYMENTS[spec.chain_id].gauges:
            new_ids = {str(row["id"]) for row in rows}
            for row in old_rows:
                gone = str(row.get("id")) not in new_ids
                if gone and not row.get("staked") and _protocol(row) != lp_amm.UNISWAP_V4:
                    self._dirty.add(_owner(row))
                    self._again = True
        now = self._now()
        for owner in owners:
            self.service.ledger.replace_lp_positions(
                spec.chain_id,
                owner,
                [row for row in rows if _owner(row) == owner],
                read_at=now,
                deep_at=now if walked else None,
                block=result.block,
                partial=result.partial,
                warnings=result.warnings,
            )

    # ── the answer ─────────────────────────────────────────────────────

    def _payload(self, scope: dict[str, str | None], chain: ChainSpec | None) -> dict[str, Any]:
        service = self.service
        specs = [chain] if chain is not None else service.chains()
        reads = self._reads()
        rows: list[dict[str, Any]] = []
        for spec in specs:
            for row in service.ledger.lp_positions(chain_id=spec.chain_id):
                if _owner(row) in scope:
                    rows.append(row)
        rows = sort_rows(rows)
        warnings: list[str] = []
        partial = False
        chains: list[dict[str, Any]] = []
        read_ats: list[float] = []
        ready = bool(scope)
        for spec in specs:
            seen = [reads[(spec.chain_id, o)] for o in scope if (spec.chain_id, o) in reads]
            if len(seen) < len(scope):
                ready = False
            read_at = min((float(r["read_at"]) for r in seen), default=None)
            if read_at is not None:
                read_ats.append(read_at)
            for read in seen:
                partial = partial or bool(read.get("partial"))
                for warning in _warnings(read):
                    if warning not in warnings:
                        warnings.append(warning)
            error = self._errors.get(spec.chain_id)
            if error:
                partial = True
                warnings.append(
                    f"{spec.name} could not be refreshed ({error}); "
                    "showing the last known positions"
                )
            chains.append(
                {
                    **lp.chain_json(spec),
                    "readAt": read_at,
                    "asOfBlock": max((int(r.get("block") or 0) for r in seen), default=0),
                }
            )
        if len(rows) > MAX_ROWS:
            warnings.append(
                f"showing {MAX_ROWS} of {len(rows)} positions (out of range first, then by "
                f"value); the totals cover all {len(rows)}"
            )
        return lp._payload(
            {
                "version": PAYLOAD_VERSION,
                "kind": "book",
                "ready": ready,
                "refreshing": self.refreshing,
                "readAt": min(read_ats) if read_ats and ready else None,
                "partialScan": partial,
                "warnings": warnings,
                "wallets": [
                    {"address": lp.unilp().hexutil.checksum_address(o), "label": label}
                    for o, label in scope.items()
                ],
                "chains": chains,
                "protocols": protocol_totals(rows),
                "positions": rows[:MAX_ROWS],
                "totals": totals(rows),
            }
        )


def _v4_slot(owner: str) -> str:
    return f"{lp_amm.UNISWAP_V4}:{owner.lower()}"


def _held_by(position: dict[str, Any], owner: str) -> bool:
    return str(position.get("owner") or "").lower() == owner.lower()


def _protocol(row: dict[str, Any]) -> str:
    return str((row.get("protocol") or {}).get("key") or "")


def _owner(row: dict[str, Any]) -> str:
    return str((row.get("owner") or {}).get("address") or "").lower()


def _warnings(read: dict[str, Any]) -> list[str]:
    try:
        found = json.loads(read.get("warnings_json") or "[]")
    except ValueError:
        return []
    return [str(w) for w in found] if isinstance(found, list) else []


def sort_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Out-of-range first, then in-range and full-range, then closed; each by value desc."""

    def key(row: dict[str, Any]) -> tuple[int, int, float, str]:
        value = row.get("valueUsd")
        return (
            _GROUP.get(str(row.get("status")), 3),
            1 if value is None else 0,
            -(value or 0.0),
            str(row.get("id")),
        )

    return sorted(rows, key=key)


def _usd(row: dict[str, Any], *path: str) -> float | None:
    node: Any = row
    for part in path:
        node = node.get(part) if isinstance(node, dict) else None
    return lp.finite(node)


def totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Sums over every row; a part no row could price is ``None``, never ``0``."""
    values = [v for row in rows if (v := lp.finite(row.get("valueUsd"))) is not None]
    fees = [v for row in rows if (v := _usd(row, "fees", "usd")) is not None]
    rewards = [v for row in rows if (v := _usd(row, "rewards", "amount", "usd")) is not None]
    return {
        "valueUsd": lp.finite(sum(values)) if values or not rows else None,
        "feesUsd": lp.finite(sum(fees)) if fees or not rows else None,
        "rewardsUsd": lp.finite(sum(rewards)) if rewards or not rows else None,
        "count": len(rows),
        "outOfRange": sum(1 for row in rows if row.get("status") in ("above-range", "below-range")),
        "unpriced": len(rows) - len(values),
    }


def protocol_totals(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One line per protocol that has a position, in ``lp_amm.PROTOCOL_NAMES`` order."""
    out = []
    for key, name in lp_amm.PROTOCOL_NAMES.items():
        mine = [row for row in rows if _protocol(row) == key]
        if not mine:
            continue
        values = [v for row in mine if (v := lp.finite(row.get("valueUsd"))) is not None]
        out.append(
            {
                "key": key,
                "name": name,
                "count": len(mine),
                "valueUsd": lp.finite(sum(values)) if values else None,
            }
        )
    return out
