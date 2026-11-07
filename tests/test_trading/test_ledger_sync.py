from __future__ import annotations

import sqlite3

import httpx
import pytest

from agentos.trading import chains
from agentos.trading.chains import BASE, NATIVE_ADDRESS, ROBINHOOD
from agentos.trading.evm import EvmClient
from agentos.trading.ledger import SCHEMA_VERSION, Ledger, local_day
from agentos.trading.prices import PriceService, TokenMeta, native_token
from agentos.trading.sync import SYNC_OVERLAP_BLOCKS, WalletSyncer, summarize_transfers
from agentos.trading.vault import WalletRecord
from tests.test_trading.fakes import AAPL, OTHER, USDC, WALLET, WETH, FakeChain, FakePrices


class TestLedger:
    def test_entries_are_idempotent_and_ordered(self, ledger: Ledger) -> None:
        first = ledger.insert_entry(
            ts=10,
            chain_id=8453,
            wallet=WALLET,
            kind="deposit",
            tx_hash="0xA",
            log_index=1,
            token_out=USDC,
            amount_out_raw=5 * 10**6,
            value_usd=5.0,
            initiator="external",
        )
        dup = ledger.insert_entry(
            ts=10,
            chain_id=8453,
            wallet=WALLET,
            kind="deposit",
            tx_hash="0xa",
            log_index=1,
            token_out=USDC,
            amount_out_raw=5 * 10**6,
        )
        second = ledger.insert_entry(
            ts=20,
            chain_id=8453,
            wallet=WALLET,
            kind="withdraw",
            tx_hash="0xB",
            log_index=0,
            token_in=USDC,
            amount_in_raw=1,
        )
        assert first and second and dup is None
        assert ledger.entry_exists(WALLET, "deposit", "0xA", 1)
        rows = ledger.list_entries(wallet=WALLET)
        assert [r["kind"] for r in rows] == ["withdraw", "deposit"]
        assert ledger.list_entries(kind="deposit")[0]["amount_out_raw"] == str(5 * 10**6)
        assert ledger.list_entries(before=15)[0]["kind"] == "deposit"
        assert ledger.list_entries(chain_id=4663) == []
        assert len(ledger.entries_for_tx(WALLET, "0xa")) == 1

    def test_big_amounts_survive(self, ledger: Ledger) -> None:
        huge = 2**200
        ledger.add_lot(
            8453,
            WALLET,
            USDC,
            amount_raw=huge,
            cost_usd_per_raw=1e-30,
            acquired_at=1,
            entry_id=None,
        )
        assert ledger.open_lots(8453, WALLET, USDC)[0].amount_raw == huge
        assert ledger.positions(WALLET)[0].amount_raw == huge

    def test_lots_positions_realized(self, ledger: Ledger) -> None:
        ledger.add_lot(
            8453, WALLET, USDC, amount_raw=100, cost_usd_per_raw=1.0, acquired_at=1, entry_id=1
        )
        ledger.add_lot(
            8453, WALLET, USDC, amount_raw=50, cost_usd_per_raw=2.0, acquired_at=2, entry_id=2
        )
        ledger.add_lot(
            8453, OTHER, USDC, amount_raw=7, cost_usd_per_raw=1.0, acquired_at=3, entry_id=3
        )
        positions = {(p.wallet, p.token): p for p in ledger.positions()}
        assert positions[(WALLET, USDC)].amount_raw == 150
        assert positions[(WALLET, USDC)].cost_usd == pytest.approx(200.0)
        assert len(ledger.positions(WALLET)) == 1
        lots = ledger.open_lots(8453, WALLET, USDC)
        lots[0].amount_raw = 0
        ledger.save_lots(lots)
        assert [lot.amount_raw for lot in ledger.open_lots(8453, WALLET, USDC)] == [50]
        ledger.add_realized(
            entry_id=9,
            chain_id=8453,
            wallet=WALLET,
            token=USDC,
            amount_raw=100,
            proceeds_usd=150.0,
            cost_usd=100.0,
            ts=5,
        )
        assert ledger.realized_by_position(WALLET)[(8453, WALLET, USDC)] == pytest.approx(50.0)
        ledger.update_lot_cost(lots[1].lot_id or 0, 3.0)
        assert ledger.open_lots(8453, WALLET, USDC)[0].cost_usd_per_raw == 3.0

    def test_orders_lifecycle_and_expiry(self, ledger: Ledger) -> None:
        base = {
            "chain_id": 8453,
            "wallet": WALLET,
            "token_in": USDC,
            "token_out": WETH,
            "amount_raw": "1",
            "amount_human": "0.000001",
            "initiator": "agent",
        }
        ledger.insert_order(
            {
                **base,
                "order_id": "o1",
                "created_at": 1,
                "updated_at": 1,
                "status": "awaiting_approval",
                "expires_at": 100,
            }
        )
        ledger.insert_order(
            {
                **base,
                "order_id": "o2",
                "created_at": 2,
                "updated_at": 2,
                "status": "awaiting_approval",
                "expires_at": 500,
            }
        )
        ledger.insert_order(
            {**base, "order_id": "o3", "created_at": 3, "updated_at": 3, "status": "confirmed"}
        )
        assert ledger.count_orders("awaiting_approval") == 2
        assert [o["order_id"] for o in ledger.list_orders()] == ["o3", "o2", "o1"]
        assert [o["order_id"] for o in ledger.list_orders(status="confirmed,expired")] == ["o3"]
        expired = ledger.expire_orders(now=200)
        assert [o["order_id"] for o in expired] == ["o1"]
        assert ledger.get_order("o1")["status"] == "expired"
        assert ledger.count_orders("awaiting_approval") == 1
        updated = ledger.update_order("o2", status="approved", reason=None)
        assert updated and updated["status"] == "approved"
        assert ledger.get_order("nope") is None

    def test_daily_spend_balances_snapshots_sync_state(self, ledger: Ledger) -> None:
        day = local_day(0)
        ledger.add_daily_spend(WALLET, 10.0, day)
        ledger.add_daily_spend(WALLET, 2.5, day)
        assert ledger.spent_today(WALLET, day) == pytest.approx(12.5)
        assert ledger.spent_today(OTHER, day) == 0.0
        ledger.set_balance(8453, WALLET, USDC, 5)
        ledger.set_balance(8453, WALLET, USDC, 7)
        assert ledger.get_balance(8453, WALLET, USDC) == 7
        assert ledger.get_balance(8453, WALLET, WETH) is None
        assert len(ledger.balances(WALLET, 8453)) == 1
        ledger.add_snapshot(8453, USDC, 100, 1.0)
        ledger.add_snapshot(8453, USDC, 200, 1.1)
        assert [s["price_usd"] for s in ledger.snapshots(8453, USDC, since=150)] == [1.1]
        ledger.prune_snapshots(older_than=150)
        assert len(ledger.snapshots(8453, USDC, since=0)) == 1
        assert ledger.sync_state(8453, WALLET) is None
        ledger.set_sync_state(8453, WALLET, last_block=99, oldest_block=1)
        assert ledger.sync_state(8453, WALLET)["last_block"] == 99

    def test_tokens_and_wallet_mirror(self, ledger: Ledger) -> None:
        ledger.upsert_token(8453, USDC, symbol="USDC", name="USD Coin", decimals=6, verified=True)
        ledger.upsert_token(8453, USDC, symbol="", name="", decimals=6, logo_url="https://l")
        row = ledger.get_token(8453, USDC)
        assert (
            row
            and row["symbol"] == "USDC"
            and row["logo_url"] == "https://l"
            and row["verified"] == 1
        )
        assert len(ledger.tokens(8453)) == 1 and ledger.tokens(4663) == []
        ledger.upsert_wallet(WALLET, label="A", is_primary=True, created_at=1.0)
        ledger.set_balance(8453, WALLET, USDC, 1)
        ledger.remove_wallet(WALLET)
        assert ledger.balances(WALLET) == []

    def test_delete_chain_history(self, ledger: Ledger) -> None:
        ledger.insert_entry(
            ts=1, chain_id=8453, wallet=WALLET, kind="deposit", tx_hash="0x1", log_index=0
        )
        ledger.insert_entry(
            ts=1, chain_id=4663, wallet=WALLET, kind="deposit", tx_hash="0x2", log_index=0
        )
        ledger.add_lot(
            8453, WALLET, USDC, amount_raw=1, cost_usd_per_raw=1, acquired_at=1, entry_id=None
        )
        ledger.delete_chain_history(WALLET, 8453)
        assert [e["chain_id"] for e in ledger.list_entries(wallet=WALLET)] == [4663]
        assert ledger.positions(WALLET) == []


@pytest.fixture
def prices_fake() -> FakePrices:
    prices = FakePrices()
    prices.spot[("base", USDC)] = 1.0
    prices.spot[("base", WETH)] = 2000.0
    prices.history[USDC] = 1.0
    prices.history[WETH] = 1500.0
    prices.lists["base"] = [
        {"chainId": 8453, "address": USDC, "symbol": "USDC", "name": "USD Coin", "decimals": 6},
        {
            "chainId": 8453,
            "address": WETH,
            "symbol": "WETH",
            "name": "Wrapped Ether",
            "decimals": 18,
        },
    ]
    return prices


@pytest.fixture
def chain() -> FakeChain:
    c = FakeChain(chain_id=8453, block=10_000)
    c.tokens[USDC] = ("USDC", "USD Coin", 6)
    c.tokens[WETH] = ("WETH", "Wrapped Ether", 18)
    return c


@pytest.fixture
async def syncer(ledger: Ledger, chain: FakeChain, prices_fake: FakePrices):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "mainnet.base.org":
            return chain.handle(request)
        return prices_fake.handle(request)

    clock = {"now": 1_700_100_000.0}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        prices = PriceService(http=http, ttl_s=0, now=lambda: clock["now"])
        evm = EvmClient("https://mainnet.base.org", http=http)

        async def token_meta(spec, address: str) -> TokenMeta:
            if address == NATIVE_ADDRESS:
                return native_token(spec)
            known = await prices.known_token(spec, address)
            if known:
                ledger.upsert_token(
                    spec.chain_id,
                    address,
                    symbol=known.symbol,
                    name=known.name,
                    decimals=known.decimals,
                    verified=True,
                )
                return known
            return TokenMeta(spec.chain_id, address, "", "", 18)

        async def watch(spec) -> list[str]:
            return [USDC, WETH]

        s = WalletSyncer(
            ledger,
            prices,
            evm_for=lambda spec: evm,
            token_meta=token_meta,
            watch_tokens=watch,
            now=lambda: clock["now"],
        )
        s.clock = clock  # type: ignore[attr-defined]
        yield s


def _wallet(created_block: int | None = 9_000) -> WalletRecord:
    record = WalletRecord(address=WALLET, label="W", created_at=1.0, imported=created_block is None)
    if created_block is not None:
        record.created_block["8453"] = created_block
    return record


class TestWalletSyncer:
    async def test_deposit_withdraw_and_external_swap(
        self, syncer: WalletSyncer, chain: FakeChain, ledger: Ledger
    ) -> None:
        # Historical deposit: priced through CoinGecko (block 9_100 -> ts 1_700_018_200, old).
        chain.add_transfer(
            token=USDC, sender=OTHER, recipient=WALLET, amount=100 * 10**6, block=9_100
        )
        # An external swap in one tx: 50 USDC out, 0.03 WETH in.
        tx = chain.add_transfer(
            token=USDC, sender=WALLET, recipient=OTHER, amount=50 * 10**6, block=9_200
        )
        chain.add_transfer(
            token=WETH,
            sender=OTHER,
            recipient=WALLET,
            amount=3 * 10**16,
            block=9_200,
            tx_hash=tx,
            log_index=1,
        )
        # A withdrawal.
        chain.add_transfer(token=WETH, sender=WALLET, recipient=OTHER, amount=10**16, block=9_300)
        chain.set_erc20(USDC, WALLET, 50 * 10**6)
        chain.set_erc20(WETH, WALLET, 2 * 10**16)
        chain.set_native(WALLET, 0)

        changed = await syncer.sync(_wallet(), BASE)
        assert changed is True
        kinds = [e["kind"] for e in ledger.list_entries(wallet=WALLET)]
        assert kinds == ["withdraw", "swap", "deposit"]
        deposit = ledger.list_entries(wallet=WALLET, kind="deposit")[0]
        assert deposit["cost_basis_source"] == "historical"
        assert deposit["value_usd"] == pytest.approx(100.0)
        swap = ledger.list_entries(wallet=WALLET, kind="swap")[0]
        assert swap["initiator"] == "external"
        assert swap["token_in"] == USDC and swap["token_out"] == WETH
        assert swap["value_usd"] == pytest.approx(50.0)
        positions = {p.token: p for p in ledger.positions(WALLET)}
        assert positions[USDC].amount_raw == 50 * 10**6
        assert positions[USDC].cost_usd == pytest.approx(50.0)
        # WETH lot cost = the 50 USD that bought 0.03, minus the 0.01 sold.
        assert positions[WETH].amount_raw == 2 * 10**16
        assert positions[WETH].cost_usd == pytest.approx(50.0 * 2 / 3)
        realized = ledger.realized_by_position(WALLET)
        assert realized[(8453, WALLET, USDC)] == pytest.approx(0.0)
        # Sold 0.01 WETH bought at 1666.67/ETH for 1500 (historical) -> loss.
        assert realized[(8453, WALLET, WETH)] == pytest.approx(15.0 - 50.0 / 3, abs=0.01)
        assert ledger.sync_state(8453, WALLET)["last_block"] == 10_000
        assert ledger.get_balance(8453, WALLET, USDC) == 50 * 10**6
        assert ledger.get_balance(8453, WALLET, NATIVE_ADDRESS) == 0
        # Snapshots exist for the held tokens.
        assert ledger.snapshots(8453, USDC, since=0)

        # Second sync is incremental and idempotent.
        assert await syncer.sync(_wallet(), BASE) is False
        assert len(ledger.list_entries(wallet=WALLET)) == 3

    async def test_native_balance_reconciliation(
        self, syncer: WalletSyncer, chain: FakeChain, ledger: Ledger
    ) -> None:
        chain.set_native(WALLET, 10**18)
        await syncer.sync(_wallet(), BASE)
        opening = ledger.list_entries(wallet=WALLET)[0]
        assert opening["kind"] == "deposit" and opening["note"] == "opening balance"
        assert opening["value_usd"] == pytest.approx(2000.0)
        chain.set_native(WALLET, 15 * 10**17)
        await syncer.sync(_wallet(), BASE)
        assert ledger.list_entries(wallet=WALLET)[0]["amount_out_raw"] == str(5 * 10**17)
        chain.set_native(WALLET, 10**18)
        chain.block += 1
        await syncer.sync(_wallet(), BASE)
        latest = ledger.list_entries(wallet=WALLET)[0]
        assert latest["kind"] == "withdraw" and latest["amount_in_raw"] == str(5 * 10**17)
        pos = {p.token: p for p in ledger.positions(WALLET)}[NATIVE_ADDRESS]
        assert pos.amount_raw == 10**18
        # Dust drift (gas) is ignored.
        chain.set_native(WALLET, 10**18 - 10**12)
        chain.block += 1
        assert await syncer.sync(_wallet(), BASE) is False

    async def test_imported_wallet_lookback_and_full_resync(
        self, syncer: WalletSyncer, chain: FakeChain, ledger: Ledger
    ) -> None:
        syncer.initial_lookback = 500
        chain.add_transfer(
            token=USDC, sender=OTHER, recipient=WALLET, amount=1, block=9_000
        )  # too old
        chain.add_transfer(token=USDC, sender=OTHER, recipient=WALLET, amount=2, block=9_900)
        chain.set_erc20(USDC, WALLET, 3)
        await syncer.sync(_wallet(created_block=None), BASE)
        deposits = ledger.list_entries(wallet=WALLET, kind="deposit")
        # The log inside the window is booked as a transfer; the older 1 unit the
        # sweep did not see shows up as an "opening balance" from the on-chain read.
        assert sorted((e["amount_out_raw"], e["note"]) for e in deposits) == [
            ("1", "opening balance"),
            ("2", None),
        ]
        assert ledger.sync_state(8453, WALLET)["oldest_block"] == 9_500
        syncer.full_lookback = 5_000
        await syncer.sync(_wallet(created_block=None), BASE, full=True)
        deposits = ledger.list_entries(wallet=WALLET, kind="deposit")
        assert sorted((e["amount_out_raw"], e["note"]) for e in deposits) == [
            ("1", None),
            ("2", None),
        ]

    async def test_in_flight_order_is_left_to_confirmation(
        self, syncer: WalletSyncer, chain: FakeChain, ledger: Ledger
    ) -> None:
        tx = chain.add_transfer(token=USDC, sender=WALLET, recipient=OTHER, amount=5, block=9_950)
        ledger.insert_order(
            {
                "order_id": "o1",
                "created_at": 1,
                "updated_at": 1,
                "chain_id": 8453,
                "wallet": WALLET,
                "token_in": USDC,
                "token_out": WETH,
                "amount_raw": "5",
                "amount_human": "0.000005",
                "status": "submitted",
                "initiator": "agent",
                "tx_hash": tx,
            }
        )
        await syncer.sync(_wallet(), BASE)
        assert ledger.list_entries(wallet=WALLET, kind="withdraw") == []

    async def test_unknown_price_books_zero_cost(
        self, syncer: WalletSyncer, chain: FakeChain, ledger: Ledger
    ) -> None:
        # An unlisted token from a stranger is an airdrop (see test_booking.py);
        # with no price at all it is still a lot at cost zero.
        chain.add_transfer(token=AAPL, sender=OTHER, recipient=WALLET, amount=10**18, block=9_990)
        await syncer.sync(_wallet(), BASE)
        entry = ledger.list_entries(wallet=WALLET, kind="deposit")[0]
        assert entry["value_usd"] is None and entry["cost_basis_source"] == "airdrop"
        assert entry["price_out_usd"] is None
        assert ledger.positions(WALLET)[0].cost_usd == 0.0

    def test_summarize_helper(self) -> None:
        from agentos.trading.evm import TransferLog

        logs = [
            TransferLog("0x1", 0, 1, USDC, OTHER, WALLET, 5),
            TransferLog("0x1", 1, 1, USDC, WALLET, OTHER, 2),
        ]
        assert summarize_transfers(logs, WALLET) == {"in": {USDC: 5}, "out": {USDC: 2}}

    def test_robinhood_chain_spec_has_no_hardcoded_weth(self) -> None:
        assert ROBINHOOD.weth is None and BASE.weth is not None


class TestLedgerRules:
    """The rules that keep the ledger honest across sweeps and rebuilds."""

    async def test_failed_balance_read_keeps_last_good_and_opening(
        self, syncer: WalletSyncer, chain: FakeChain, ledger: Ledger
    ) -> None:
        """A balanceOf the node does not answer must not become 0 in the ledger.

        Before this rule, a flaky RPC wrote 0, and the opening reconciliation
        then read "the wallet holds nothing" and deleted the position's lots.
        """
        wallet = _wallet(created_block=None)
        syncer.initial_lookback = 500
        chain.set_erc20(USDC, WALLET, 5)
        await syncer.sync(wallet, BASE)
        assert ledger.get_balance(8453, WALLET, USDC) == 5
        opening = ledger.opening_entry(8453, WALLET, USDC)
        assert opening is not None and opening["amount_out_raw"] == "5"
        assert ledger.chain_reads(WALLET, 8453)[0]["status"] == "ok"

        chain.fail_balance_of.add(USDC)
        await syncer.sync(wallet, BASE)
        # The row is last-good, the opening still stands, and the read says so.
        assert ledger.get_balance(8453, WALLET, USDC) == 5
        assert ledger.opening_entry(8453, WALLET, USDC) == opening
        assert [p.amount_raw for p in ledger.positions(WALLET) if p.token == USDC] == [5]
        read = ledger.chain_reads(WALLET, 8453)[0]
        assert read["status"] == "partial" and "1 token" in read["reason"]

        chain.fail_balance_of.clear()
        await syncer.sync(wallet, BASE)
        assert ledger.chain_reads(WALLET, 8453)[0]["status"] == "ok"

    async def test_rebuild_skips_unreadable_token(
        self, syncer: WalletSyncer, chain: FakeChain, ledger: Ledger
    ) -> None:
        wallet = _wallet(created_block=None)
        syncer.full_lookback = 500
        chain.set_erc20(USDC, WALLET, 7)
        chain.set_erc20(WETH, WALLET, 3)
        chain.fail_balance_of.add(WETH)
        await syncer.sync(wallet, BASE, full=True)
        # USDC got its opening; WETH got neither an opening nor a phantom 0.
        assert ledger.opening_entry(8453, WALLET, USDC) is not None
        assert ledger.opening_entry(8453, WALLET, WETH) is None
        assert ledger.get_balance(8453, WALLET, WETH) is None
        assert ledger.chain_reads(WALLET, 8453)[0]["status"] == "partial"

    async def test_discovery_widens_the_scan_set(
        self, ledger: Ledger, chain: FakeChain, prices_fake: FakePrices
    ) -> None:
        """A token the sweep never saw, but an indexer knows, is read and booked."""
        hidden = "0x9999000000000000000000000000000000000001"
        chain.tokens[hidden] = ("HID", "Hidden", 18)
        chain.set_erc20(hidden, WALLET, 11)
        asked: list[tuple[int, str]] = []

        async def discover(spec, address: str) -> list[str]:
            asked.append((spec.chain_id, address))
            return [hidden.upper()]  # any case; the syncer lower-cases

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "mainnet.base.org":
                return chain.handle(request)
            return prices_fake.handle(request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            prices = PriceService(http=http, ttl_s=0)
            evm = EvmClient("https://mainnet.base.org", http=http)

            async def token_meta(spec, address: str) -> TokenMeta:
                if address == NATIVE_ADDRESS:
                    return native_token(spec)
                return TokenMeta(spec.chain_id, address, "HID", "Hidden", 18)

            syncer = WalletSyncer(
                ledger,
                prices,
                evm_for=lambda spec: evm,
                token_meta=token_meta,
                discover_tokens=discover,
            )
            wallet = _wallet(created_block=None)
            syncer.initial_lookback = 100
            await syncer.sync(wallet, BASE)
        assert asked and asked[0] == (8453, wallet.address)
        assert ledger.get_balance(8453, WALLET, hidden) == 11
        opening = ledger.opening_entry(8453, WALLET, hidden)
        assert opening is not None and opening["amount_out_raw"] == "11"

    async def test_discovery_failure_is_advisory(
        self, syncer: WalletSyncer, chain: FakeChain, ledger: Ledger
    ) -> None:
        async def broken(spec, address: str) -> list[str]:
            raise RuntimeError("indexer down")

        syncer._discover_tokens = broken
        wallet = _wallet(created_block=None)
        chain.set_erc20(USDC, WALLET, 2)
        await syncer.sync(wallet, BASE)
        assert ledger.get_balance(8453, WALLET, USDC) == 2
        assert ledger.chain_reads(WALLET, 8453)[0]["status"] == "ok"

    async def test_opening_is_never_double_counted(
        self, syncer: WalletSyncer, chain: FakeChain, ledger: Ledger
    ) -> None:
        evm = syncer._evm_for(BASE)
        wallet = _wallet(created_block=None)
        chain.set_erc20(USDC, WALLET, 3)
        # Before the first sweep nothing is booked: the sweep owns openings.
        assert await syncer.ensure_opening(wallet, BASE, USDC, evm) is False
        assert ledger.list_entries(wallet=WALLET) == []
        assert ledger.get_balance(8453, WALLET, USDC) == 3

        # The sweep sees 2 of the 3 arrive; the other 1 becomes the opening.
        syncer.initial_lookback = 500
        chain.add_transfer(token=USDC, sender=OTHER, recipient=WALLET, amount=2, block=9_900)
        await syncer.sync(wallet, BASE)
        deposits = ledger.list_entries(wallet=WALLET, kind="deposit")
        assert sorted((e["amount_out_raw"], e["note"]) for e in deposits) == [
            ("1", "opening balance"),
            ("2", None),
        ]
        # A second opening for the same position is refused by the schema.
        assert (
            ledger.insert_entry(
                ts=1.0,
                chain_id=8453,
                wallet=WALLET,
                kind="deposit",
                tx_hash=None,
                log_index=5,
                token_out=USDC,
                amount_out_raw=9,
                note="opening balance",
            )
            is None
        )
        # Re-reading the balance (the balances RPC path) changes nothing.
        assert await syncer.ensure_opening(wallet, BASE, USDC, evm) is False
        assert len(ledger.list_entries(wallet=WALLET, kind="deposit")) == 2

        # A rebuild that reaches the older transfer explains everything: the
        # opening goes and each transfer appears exactly once.
        chain.add_transfer(token=USDC, sender=OTHER, recipient=WALLET, amount=1, block=9_000)
        syncer.full_lookback = 5_000
        await syncer.sync(wallet, BASE, full=True)
        deposits = ledger.list_entries(wallet=WALLET, kind="deposit")
        assert sorted((e["amount_out_raw"], e["note"]) for e in deposits) == [
            ("1", None),
            ("2", None),
        ]
        positions = {p.token: p for p in ledger.positions(WALLET)}
        assert positions[USDC].amount_raw == 3

    async def test_opening_shrinks_when_history_grows_and_lots_replay(
        self, syncer: WalletSyncer, chain: FakeChain, ledger: Ledger
    ) -> None:
        wallet = _wallet(created_block=None)
        syncer.initial_lookback = 100
        chain.set_erc20(USDC, WALLET, 10)
        await syncer.sync(wallet, BASE)  # nothing scanned: opening 10
        opening = ledger.opening_entry(8453, WALLET, USDC)
        assert opening and opening["amount_out_raw"] == "10"
        # 4 of those 10 turn out to have been withdrawn and 14 deposited in a
        # block the first sweep skipped; a rebuild reconciles: opening 0, and
        # the withdrawal is consumed FIFO from the deposit's lot on replay.
        chain.add_transfer(token=USDC, sender=OTHER, recipient=WALLET, amount=14, block=9_950)
        chain.add_transfer(token=USDC, sender=WALLET, recipient=OTHER, amount=4, block=9_960)
        syncer.full_lookback = 200
        await syncer.sync(wallet, BASE, full=True)
        assert ledger.opening_entry(8453, WALLET, USDC) is None
        kinds = sorted(e["kind"] for e in ledger.list_entries(wallet=WALLET))
        assert kinds == ["deposit", "withdraw"]
        positions = {p.token: p for p in ledger.positions(WALLET)}
        assert positions[USDC].amount_raw == 10

    async def test_rebuild_is_atomic_and_resumable(
        self, syncer: WalletSyncer, chain: FakeChain, ledger: Ledger
    ) -> None:
        evm = syncer._evm_for(BASE)
        wallet = _wallet(created_block=None)
        syncer.initial_lookback = 100
        syncer.full_lookback = 4_000
        evm.max_log_span = 100  # rebuild windows of 1_000 blocks -> 4 windows
        chain.add_transfer(token=USDC, sender=OTHER, recipient=WALLET, amount=7, block=6_500)
        chain.add_transfer(token=USDC, sender=OTHER, recipient=WALLET, amount=5, block=9_950)
        chain.set_erc20(USDC, WALLET, 12)
        await syncer.sync(wallet, BASE)
        before = ledger.list_entries(wallet=WALLET)
        live_ids = {e["id"] for e in before}

        # Interrupt the sweep after two windows: the live ledger is untouched
        # and the shadow remembers how far it got.
        original = evm.transfer_logs
        calls = {"n": 0}

        async def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 3:
                raise ConnectionError("gateway restarted")
            return await original(*args, **kwargs)

        evm.transfer_logs = flaky  # type: ignore[method-assign]
        with pytest.raises(ConnectionError):
            await syncer.sync(wallet, BASE, full=True)
        assert {e["id"] for e in ledger.list_entries(wallet=WALLET)} == live_ids
        state = ledger.rebuild_state(8453, WALLET)
        assert state is not None and state["scanned_down_to"] == 10_001 - 2_000
        assert syncer.rebuild_progress is None

        # A plain sync resumes the pending rebuild and finishes it.
        evm.transfer_logs = original  # type: ignore[method-assign]
        await syncer.sync(wallet, BASE)
        assert ledger.rebuild_state(8453, WALLET) is None
        assert ledger.rebuild_logs(8453, WALLET) == []
        deposits = ledger.list_entries(wallet=WALLET, kind="deposit")
        assert sorted((e["amount_out_raw"], e["note"]) for e in deposits) == [
            ("5", None),
            ("7", None),
        ]
        assert ledger.sync_state(8453, WALLET)["oldest_block"] == 6_000
        # Only the last two windows were re-swept after the resume.
        assert calls["n"] == 3

    async def test_rebuild_keeps_own_confirmed_swaps(
        self, syncer: WalletSyncer, chain: FakeChain, ledger: Ledger
    ) -> None:
        wallet = _wallet(created_block=8_000)
        # Our confirmed order: 0.0001 ETH -> 0.248936 USDC. The chain only shows
        # the USDC Transfer (native input has no log).
        tx = chain.add_transfer(
            token=USDC, sender=OTHER, recipient=WALLET, amount=248_936, block=9_990
        )
        ledger.insert_order(
            {
                "order_id": "ord_1",
                "created_at": 1_700_099_900.0,
                "updated_at": 1_700_099_990.0,
                "chain_id": 8453,
                "wallet": WALLET,
                "token_in": NATIVE_ADDRESS,
                "token_out": USDC,
                "amount_raw": str(10**14),
                "amount_human": "0.0001",
                "status": "confirmed",
                "initiator": "agent",
                "session_key": "agent:main:webchat:desk",
                "tx_hash": tx,
                "spent_in_raw": str(10**14),
                "received_out_raw": "248936",
                "gas_wei": str(10**12),
                "delivered_token": USDC,
            }
        )
        chain.set_erc20(USDC, WALLET, 248_936)
        chain.set_native(WALLET, 10**14)  # what is left after the swap

        await syncer.sync(wallet, BASE, full=True)
        entries = ledger.list_entries(wallet=WALLET)
        swaps = [e for e in entries if e["kind"] == "swap"]
        assert len(swaps) == 1
        swap = swaps[0]
        assert swap["tx_hash"] == tx and swap["initiator"] == "agent"
        assert swap["order_id"] == "ord_1" and swap["session_key"] == "agent:main:webchat:desk"
        assert swap["amount_in_raw"] == str(10**14) and swap["amount_out_raw"] == "248936"
        assert swap["gas_usd"] == pytest.approx(0.000001 * 2000.0)
        # The USDC that arrived through the swap is not also a deposit.
        assert [e for e in entries if e["kind"] == "deposit" and e["token_out"] == USDC] == []
        # The native opening covers what the swap spent plus what is left.
        native_opening = ledger.opening_entry(8453, WALLET, NATIVE_ADDRESS)
        assert native_opening and native_opening["amount_out_raw"] == str(2 * 10**14)
        positions = {p.token: p for p in ledger.positions(WALLET)}
        assert positions[NATIVE_ADDRESS].amount_raw == 10**14
        assert positions[USDC].amount_raw == 248_936
        # And the same events stay unique on the next incremental sync.
        assert await syncer.sync(wallet, BASE) is False
        assert len(ledger.list_entries(wallet=WALLET)) == len(entries)

    async def test_rebuild_books_sends_as_withdrawals_and_revokes_as_gas_only(
        self, syncer: WalletSyncer, chain: FakeChain, ledger: Ledger
    ) -> None:
        """A confirmed send or revoke must never be replayed as a swap.

        A revoke's ``amount_raw`` is the allowance it cleared — 2**256-1 for
        the usual unlimited grant — and it has no ``spent_in_raw``. Fed into
        the swap path it "sold" every lot the wallet had, and a send came out
        as a swap for nothing. Now the send is the same withdrawal the
        settlement booked, and the revoke contributes its gas and nothing
        else.
        """
        wallet = _wallet(created_block=8_000)
        # 100 USDC arrived from outside, then 30 went out through our own send.
        chain.add_transfer(
            token=USDC, sender=OTHER, recipient=WALLET, amount=100 * 10**6, block=9_100
        )
        send_tx = chain.add_transfer(
            token=USDC, sender=WALLET, recipient=OTHER, amount=30 * 10**6, block=9_900, log_index=7
        )
        revoke_tx = "0x" + "ab" * 32
        common = {
            "created_at": 1_700_099_900.0,
            "chain_id": 8453,
            "wallet": WALLET,
            "status": "confirmed",
            "initiator": "agent",
            "session_key": "agent:main:webchat:desk",
        }
        ledger.insert_order(
            {
                **common,
                "order_id": "ord_send",
                "updated_at": 1_700_099_950.0,
                "token_in": USDC,
                "token_out": USDC,
                "amount_raw": str(30 * 10**6),
                "amount_human": "30",
                "kind": "send",
                "recipient": OTHER,
                "tx_hash": send_tx,
                "spent_in_raw": str(30 * 10**6),
                "gas_wei": str(10**12),
            }
        )
        ledger.insert_order(
            {
                **common,
                "order_id": "ord_revoke",
                "updated_at": 1_700_099_990.0,
                "token_in": USDC,
                "token_out": USDC,
                "amount_raw": str(2**256 - 1),
                "amount_human": "unlimited",
                "kind": "revoke",
                "recipient": "0x0000000000001ff3684f28c67538d4d072c22734",
                "tx_hash": revoke_tx,
                "gas_wei": str(2 * 10**12),
            }
        )
        chain.set_erc20(USDC, WALLET, 70 * 10**6)
        chain.set_native(WALLET, 10**16)

        await syncer.sync(wallet, BASE, full=True)
        entries = ledger.list_entries(wallet=WALLET)
        assert [e["kind"] for e in entries if e["kind"] == "swap"] == []
        by_order = {e["order_id"]: e for e in entries if e.get("order_id")}
        send = by_order["ord_send"]
        assert send["kind"] == "withdraw" and send["tx_hash"] == send_tx
        assert send["token_in"] == USDC and send["amount_in_raw"] == str(30 * 10**6)
        assert send["initiator"] == "agent" and send["session_key"] == "agent:main:webchat:desk"
        assert send["log_index"] == 7  # the Transfer's own index, as the settlement books it
        assert send["note"] == f"sent to {chains.checksum_address(OTHER)}"
        assert send["gas_usd"] == pytest.approx(0.000001 * 2000.0)
        revoke = by_order["ord_revoke"]
        assert revoke["kind"] == "approval" and revoke["tx_hash"] == revoke_tx
        assert revoke["amount_in_raw"] == "0" and revoke["amount_out_raw"] is None
        assert revoke["gas_usd"] == pytest.approx(0.000002 * 2000.0)
        assert "AgentOS Aggregator" in revoke["note"]
        # The send's Transfer log is not booked a second time from the chain.
        assert len([e for e in entries if e["tx_hash"] == send_tx]) == 1
        # Lots: 100 in, 30 out, nothing sold by the revoke; no USDC opening needed.
        positions = {p.token: p for p in ledger.positions(WALLET)}
        assert positions[USDC].amount_raw == 70 * 10**6
        assert positions[USDC].cost_usd == pytest.approx(70.0)
        assert ledger.opening_entry(8453, WALLET, USDC) is None
        assert positions[NATIVE_ADDRESS].amount_raw == 10**16
        # And the next incremental pass finds nothing new.
        assert await syncer.sync(wallet, BASE) is False
        assert len(ledger.list_entries(wallet=WALLET)) == len(entries)


class TestLedgerMigration:
    def test_version_two_gains_the_hidden_columns(self, tmp_path) -> None:
        path = tmp_path / "trading.sqlite"
        first = Ledger(path)
        first.upsert_token(8453, USDC, symbol="USDC", name="USD Coin", decimals=6)
        for column in ("hidden", "hidden_by", "touched", "classified_at"):
            first._conn.execute(f"ALTER TABLE tokens DROP COLUMN {column}")  # noqa: S608
        first._conn.execute("UPDATE schema_version SET version = 2")
        first.close()
        reopened = Ledger(path)
        row = reopened.get_token(8453, USDC)
        assert row and row["hidden"] == 0 and row["touched"] == 0
        version = reopened._conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert version == SCHEMA_VERSION
        reopened.close()

    def test_version_three_gains_order_kinds(self, tmp_path) -> None:
        """A pre-send ledger's swap orders read back as kind='swap' with no recipient."""
        path = tmp_path / "trading.sqlite"
        first = Ledger(path)
        first.insert_order(
            {
                "order_id": "o1",
                "created_at": 1,
                "updated_at": 1,
                "chain_id": 8453,
                "wallet": WALLET,
                "token_in": USDC,
                "token_out": WETH,
                "amount_raw": "1",
                "amount_human": "0.000001",
                "status": "confirmed",
                "initiator": "agent",
            }
        )
        first._conn.execute("DROP INDEX idx_orders_batch")
        first._conn.execute("DROP INDEX idx_orders_client")
        for column in ("kind", "recipient", "batch_id", "client_order_id"):
            first._conn.execute(f"ALTER TABLE orders DROP COLUMN {column}")  # noqa: S608
        first._conn.execute("DROP TABLE allowances")
        first._conn.execute("DROP TABLE allowance_scan")
        first._conn.execute("UPDATE schema_version SET version = 3")
        first.close()
        reopened = Ledger(path)
        row = reopened.get_order("o1")
        assert row and row["kind"] == "swap" and row["recipient"] is None
        assert row["batch_id"] is None
        # Version 5: the idempotency key column and its unique index arrived too.
        assert row["client_order_id"] is None
        reopened.insert_order({**row, "order_id": "o2", "client_order_id": "k1"})
        with pytest.raises(sqlite3.IntegrityError):
            reopened.insert_order({**row, "order_id": "o3", "client_order_id": "k1"})
        assert [o["order_id"] for o in reopened.find_orders_by_client_id("k1")] == ["o2"]
        reopened.upsert_allowance(8453, WALLET, USDC, OTHER, block=5, tx_hash="0xaa")
        assert [a["spender"] for a in reopened.allowances(8453, WALLET)] == [OTHER]
        reopened.close()

    def test_batches_and_allowance_cache(self, ledger: Ledger) -> None:
        base = {
            "chain_id": 8453,
            "wallet": WALLET,
            "token_in": USDC,
            "token_out": USDC,
            "amount_raw": "1",
            "amount_human": "0.000001",
            "initiator": "agent",
            "kind": "send",
            "status": "awaiting_approval",
        }
        for n in (1, 2, 3):
            ledger.insert_order(
                {
                    **base,
                    "order_id": f"s{n}",
                    "created_at": n,
                    "updated_at": n,
                    "recipient": OTHER,
                    "batch_id": "bat_1",
                }
            )
        ledger.insert_order(
            {**base, "order_id": "solo", "created_at": 9, "updated_at": 9, "recipient": OTHER}
        )
        # Three legs of one batch plus one lone send are two decisions, not four.
        assert ledger.count_orders("awaiting_approval") == 2
        assert [o["order_id"] for o in ledger.batch_orders("bat_1")] == ["s1", "s2", "s3"]
        assert [o["order_id"] for o in ledger.list_orders(kind="send")] == [
            "solo",
            "s3",
            "s2",
            "s1",
        ]
        assert ledger.list_orders(kind="swap") == []

        ledger.upsert_allowance(8453, WALLET, USDC, OTHER, block=10, tx_hash="0xaa")
        ledger.upsert_allowance(8453, WALLET, USDC, OTHER, block=12, tx_hash="0xbb")
        # An older log never rolls the cache back.
        ledger.upsert_allowance(8453, WALLET, USDC, OTHER, block=8, tx_hash="0xcc")
        rows = ledger.allowances(8453, WALLET)
        assert len(rows) == 1
        assert rows[0]["first_block"] == 10 and rows[0]["last_block"] == 12
        assert rows[0]["last_tx_hash"] == "0xbb"
        assert ledger.allowance_scan(8453, WALLET) is None
        ledger.set_allowance_scan(8453, WALLET, last_block=12)
        scan = ledger.allowance_scan(8453, WALLET)
        assert scan and scan["last_block"] == 12
        ledger.delete_allowance(8453, WALLET, USDC, OTHER)
        assert ledger.allowances(8453, WALLET) == []

    def test_hidden_helpers(self, ledger: Ledger) -> None:
        ledger.upsert_token(8453, USDC, symbol="USDC", name="USD Coin", decimals=6)
        ledger.upsert_token(8453, WETH, symbol="WETH", name="Wrapped Ether", decimals=18)
        ledger.set_token_hidden(8453, USDC, True, by="auto", classified_at=1.0)
        ledger.set_token_hidden(8453, WETH, True, by="user", classified_at=1.0)
        assert ledger.hidden_tokens(8453) == {USDC, WETH} and ledger.hidden_tokens(4663) == set()
        # The classifier cannot reverse the user; the user can reverse anything.
        ledger.set_token_hidden(8453, WETH, False, by="auto", classified_at=2.0)
        assert WETH in ledger.hidden_tokens(8453)
        ledger.set_token_hidden(8453, USDC, False, by="user", classified_at=2.0)
        assert ledger.get_token(8453, USDC)["hidden_by"] == "user"  # type: ignore[index]
        # A deliberate act marks a token touched and shows it — unless the
        # user hid it: a user's hide outranks a trade as it outranks the classifier.
        ledger.touch_token(8453, WETH)
        row = ledger.get_token(8453, WETH)
        assert row and row["hidden"] == 1 and row["touched"] == 1 and row["hidden_by"] == "user"
        ledger.upsert_token(8453, AAPL, symbol="JUNK", name="Junk", decimals=18)
        ledger.set_token_hidden(8453, AAPL, True, by="auto", classified_at=3.0)
        ledger.touch_token(8453, AAPL)
        row = ledger.get_token(8453, AAPL)
        assert row and row["hidden"] == 0 and row["touched"] == 1 and row["hidden_by"] is None
        # "Spent" means an outgoing entry or any order naming the token.
        assert ledger.token_was_spent(8453, USDC) is False
        ledger.insert_entry(
            ts=1.0,
            chain_id=8453,
            wallet=WALLET,
            kind="withdraw",
            tx_hash="0x" + "2" * 64,
            log_index=0,
            token_in=USDC,
            amount_in_raw=1,
        )
        assert ledger.token_was_spent(8453, USDC) is True

    def test_version_one_duplicate_openings_are_collapsed(self, tmp_path) -> None:
        path = tmp_path / "trading.sqlite"
        first = Ledger(path)
        # Downgrade to the old schema shape: drop the index, mark version 1.
        first._conn.execute("DROP INDEX IF EXISTS idx_entries_opening")
        first._conn.execute("UPDATE schema_version SET version = 1")
        for ts in (1.0, 2.0):
            eid = first.insert_entry(
                ts=ts,
                chain_id=8453,
                wallet=WALLET,
                kind="deposit",
                tx_hash=None,
                log_index=int(ts),
                token_out=USDC,
                amount_out_raw=5,
                note="opening balance",
            )
            first.add_lot(
                8453, WALLET, USDC, amount_raw=5, cost_usd_per_raw=0, acquired_at=ts, entry_id=eid
            )
        first.close()
        reopened = Ledger(path)
        openings = [
            e for e in reopened.list_entries(wallet=WALLET) if e["note"] == "opening balance"
        ]
        assert len(openings) == 1 and openings[0]["ts"] == 2.0
        assert sum(p.amount_raw for p in reopened.positions(WALLET)) == 5
        reopened.close()


class TestOrderRails:
    def _order(self, ledger: Ledger, order_id: str, **extra: object) -> None:
        row = {
            "order_id": order_id,
            "created_at": 1_000.0,
            "updated_at": 1_000.0,
            "chain_id": 8453,
            "wallet": WALLET,
            "token_in": USDC,
            "token_out": WETH,
            "amount_raw": "10",
            "amount_human": "10",
            "status": "awaiting_approval",
            "initiator": "agent",
            "value_usd": 50.0,
        }
        row.update(extra)
        ledger.insert_order(row)

    def test_update_order_compare_and_set(self, ledger: Ledger) -> None:
        self._order(ledger, "o1")
        first = ledger.update_order("o1", expect_status="awaiting_approval", status="approved")
        second = ledger.update_order("o1", expect_status="awaiting_approval", status="approved")
        assert first is not None and first["status"] == "approved"
        assert second is None
        # Without an expectation it is a plain update, as before.
        assert ledger.update_order("o1", reason="x")["reason"] == "x"

    def test_expire_orders_is_status_guarded(self, ledger: Ledger) -> None:
        self._order(ledger, "late", expires_at=10.0)
        self._order(ledger, "done", expires_at=10.0, status="approved")
        expired = ledger.expire_orders(now=20.0)
        assert [o["order_id"] for o in expired] == ["late"]
        assert ledger.get_order("done")["status"] == "approved"

    def test_open_agent_value_counts_in_flight_orders_only(self, ledger: Ledger) -> None:
        self._order(ledger, "parked", value_usd=100.0)
        self._order(ledger, "sent", status="submitted", value_usd=20.0)
        self._order(ledger, "done", status="confirmed", value_usd=999.0)
        self._order(ledger, "manual", status="submitted", initiator="manual", value_usd=999.0)
        self._order(ledger, "fresh", status="quoted", value_usd=5.0, created_at=5_000.0)
        self._order(ledger, "stale", status="quoted", value_usd=7.0, created_at=100.0)
        assert ledger.open_agent_value_usd(WALLET, now=5_100.0) == pytest.approx(125.0)
        assert ledger.open_agent_value_usd(
            WALLET, exclude_order_id="parked", now=5_100.0
        ) == pytest.approx(25.0)
        assert ledger.open_agent_value_usd(OTHER, now=5_100.0) == 0.0

    def test_remove_wallet_forgets_orders_and_spend(self, ledger: Ledger) -> None:
        self._order(ledger, "o1", status="confirmed")
        ledger.add_daily_spend(WALLET, 40.0, "2026-09-15")
        ledger.remove_wallet(WALLET)
        assert ledger.get_order("o1") is None
        assert ledger.spent_today(WALLET, "2026-09-15") == 0.0
        assert ledger.list_orders(wallet=WALLET) == []

    def test_remove_wallet_forgets_allowances_and_their_scan(self, ledger: Ledger) -> None:
        """A re-imported wallet must not inherit a stale allowance list, nor a scan cursor."""
        ledger.upsert_allowance(8453, WALLET, USDC, OTHER, block=100, tx_hash="0x" + "1" * 64)
        ledger.upsert_allowance(8453, OTHER, USDC, WALLET, block=100, tx_hash="0x" + "2" * 64)
        ledger.set_allowance_scan(8453, WALLET, last_block=100)
        ledger.set_allowance_scan(8453, OTHER, last_block=100)
        ledger.remove_wallet(WALLET)
        assert ledger.allowances(8453, WALLET) == []
        assert ledger.allowance_scan(8453, WALLET) is None
        # Another wallet's rows are untouched.
        assert len(ledger.allowances(8453, OTHER)) == 1
        assert ledger.allowance_scan(8453, OTHER) is not None


class TestSyncRails:
    async def test_native_reconciliation_waits_for_open_orders(
        self, syncer: WalletSyncer, chain: FakeChain, ledger: Ledger
    ) -> None:
        chain.set_native(WALLET, 10**18)
        await syncer.sync(_wallet(), BASE)
        # A swap in flight has already moved the ETH; sync must not book it.
        ledger.insert_order(
            {
                "order_id": "inflight",
                "created_at": 1.0,
                "updated_at": 1.0,
                "chain_id": 8453,
                "wallet": WALLET,
                "token_in": NATIVE_ADDRESS,
                "token_out": USDC,
                "amount_raw": str(5 * 10**17),
                "amount_human": "0.5",
                "status": "submitted",
                "initiator": "agent",
            }
        )
        chain.set_native(WALLET, 5 * 10**17)
        chain.block += 1
        before = len(ledger.list_entries(wallet=WALLET))
        await syncer.sync(_wallet(), BASE)
        assert len(ledger.list_entries(wallet=WALLET)) == before
        assert ledger.get_balance(8453, WALLET, NATIVE_ADDRESS) == 10**18
        # Once the order settles, the next pass books whatever is still unexplained.
        ledger.update_order("inflight", status="confirmed")
        chain.block += 1
        await syncer.sync(_wallet(), BASE)
        latest = ledger.list_entries(wallet=WALLET)[0]
        assert latest["kind"] == "withdraw"

    async def test_incremental_sync_rescans_an_overlap_without_double_booking(
        self, syncer: WalletSyncer, chain: FakeChain, ledger: Ledger
    ) -> None:
        """A log a lagging node served late is picked up; a log seen twice is booked once.

        ``latest`` comes from a load-balanced endpoint. The node that answers
        the next ``eth_getLogs`` may not have the block yet, and a sync that
        resumed strictly at ``last_block + 1`` would skip that log for good.
        """
        chain.add_transfer(token=USDC, sender=OTHER, recipient=WALLET, amount=5, block=9_990)
        chain.set_erc20(USDC, WALLET, 5)
        await syncer.sync(_wallet(), BASE)
        assert ledger.sync_state(8453, WALLET)["last_block"] == 10_000
        deposits = ledger.list_entries(wallet=WALLET, kind="deposit")
        assert [e["amount_out_raw"] for e in deposits if e["tx_hash"]] == ["5"]

        # A transfer at block 9_995 the lagging node did not have on the first
        # pass, plus a balance that now includes it.
        chain.add_transfer(token=USDC, sender=OTHER, recipient=WALLET, amount=7, block=9_995)
        chain.set_erc20(USDC, WALLET, 12)
        chain.block = 10_001
        await syncer.sync(_wallet(), BASE)
        spans = [
            (int(c["params"][0]["fromBlock"], 16), int(c["params"][0]["toBlock"], 16))
            for c in chain.calls
            if c["method"] == "eth_getLogs"
        ]
        assert spans[-1] == (10_001 - SYNC_OVERLAP_BLOCKS, 10_001)
        deposits = ledger.list_entries(wallet=WALLET, kind="deposit")
        assert sorted(e["amount_out_raw"] for e in deposits if e["tx_hash"]) == ["5", "7"]
        # The late log is explained by the sweep, so no opening covers it.
        assert ledger.opening_entry(8453, WALLET, USDC) is None
        assert {p.token: p.amount_raw for p in ledger.positions(WALLET)}[USDC] == 12

        # A third pass re-reads the same overlap and books nothing twice.
        chain.block = 10_002
        assert await syncer.sync(_wallet(), BASE) is False
        deposits = ledger.list_entries(wallet=WALLET, kind="deposit")
        assert sorted(e["amount_out_raw"] for e in deposits if e["tx_hash"]) == ["5", "7"]


def _balance_reads(chain: FakeChain) -> set[str]:
    """The tokens this node was asked ``balanceOf`` for since ``chain.calls`` was cleared."""
    return {
        c["params"][0]["to"].lower()
        for c in chain.calls
        if c["method"] == "eth_call" and c["params"][0]["data"].startswith("0x70a08231")
    }


class TestBalanceReadsPerPass:
    """An ordinary pass reads what the wallet holds, not every token the ledger has met.

    7 wallets × 108 tokens every 30 s came to 2.3M ``eth_call`` a day on Base alone,
    with about 9 in 10 of them answering zero. The ``eth_getLogs`` sweep sees every
    token that moves, so a token read as zero only needs reading again once it does.
    """

    @pytest.fixture
    async def settled(self, syncer: WalletSyncer, chain: FakeChain) -> WalletSyncer:
        """A wallet past its first pass, holding USDC and no WETH."""
        chain.set_erc20(USDC, WALLET, 5)
        await syncer.sync(_wallet(), BASE)
        assert _balance_reads(chain) == {USDC, WETH}
        chain.calls.clear()
        return syncer

    async def test_ordinary_pass_skips_a_token_read_as_zero(
        self, settled: WalletSyncer, chain: FakeChain
    ) -> None:
        await settled.sync(_wallet(), BASE)
        assert _balance_reads(chain) == {USDC}

    async def test_a_transfer_brings_a_zero_token_back(
        self, settled: WalletSyncer, chain: FakeChain, ledger: Ledger
    ) -> None:
        chain.add_transfer(token=WETH, sender=OTHER, recipient=WALLET, amount=7, block=10_001)
        chain.set_erc20(WETH, WALLET, 7)
        chain.block = 10_001
        await settled.sync(_wallet(), BASE)
        assert WETH in _balance_reads(chain)
        assert ledger.get_balance(8453, WALLET, WETH) == 7

    async def test_a_held_token_is_read_until_it_reads_zero(
        self, settled: WalletSyncer, chain: FakeChain, ledger: Ledger
    ) -> None:
        chain.set_erc20(USDC, WALLET, 0)
        await settled.sync(_wallet(), BASE)
        assert USDC in _balance_reads(chain)
        assert ledger.get_balance(8453, WALLET, USDC) == 0
        chain.calls.clear()
        await settled.sync(_wallet(), BASE)
        assert _balance_reads(chain) == set()

    async def test_a_token_the_ledger_just_met_is_read_on_the_next_pass(
        self,
        settled: WalletSyncer,
        chain: FakeChain,
        ledger: Ledger,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No row yet means never read: a wallet may hold it from before its lookback."""

        async def watch(spec) -> list[str]:
            return [USDC, WETH, AAPL]

        monkeypatch.setattr(settled, "_watch_tokens", watch)
        chain.tokens[AAPL] = ("AAPL", "Apple", 18)
        chain.set_erc20(AAPL, WALLET, 9)
        await settled.sync(_wallet(), BASE)
        assert AAPL in _balance_reads(chain)
        assert ledger.get_balance(8453, WALLET, AAPL) == 9

    async def test_a_balance_that_moved_without_a_log_waits_for_the_hourly_pass(
        self, settled: WalletSyncer, chain: FakeChain, ledger: Ledger
    ) -> None:
        chain.set_erc20(WETH, WALLET, 3)
        await settled.sync(_wallet(), BASE)
        assert ledger.get_balance(8453, WALLET, WETH) == 0
        await settled.sync(_wallet(), BASE, include_hidden=True)
        assert ledger.get_balance(8453, WALLET, WETH) == 3

    async def test_a_lagging_zero_on_a_token_that_arrived_does_not_stick(
        self, settled: WalletSyncer, chain: FakeChain, ledger: Ledger
    ) -> None:
        """Bulk reads may come from another node: its zero can trail the log it follows."""
        chain.add_transfer(token=WETH, sender=OTHER, recipient=WALLET, amount=7, block=10_001)
        chain.block = 10_001
        await settled.sync(_wallet(), BASE)  # the balance read has not caught up
        assert ledger.get_balance(8453, WALLET, WETH) == 0

        chain.set_erc20(WETH, WALLET, 7)
        chain.block = 10_100  # past the overlap: the sweep will not see this transfer again
        await settled.sync(_wallet(), BASE)
        assert ledger.get_balance(8453, WALLET, WETH) == 7

    async def test_read_all_reads_every_token(
        self, settled: WalletSyncer, chain: FakeChain
    ) -> None:
        await settled.sync(_wallet(), BASE, read_all=True)
        assert _balance_reads(chain) == {USDC, WETH}
