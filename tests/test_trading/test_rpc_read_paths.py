"""Read paths that must not call the chain when they have nothing to learn (issue #3976).

Every JSON-RPC item is billed on a metered endpoint, so these count the items
the fake nodes receive (``FakeChain.calls``) on a pinned clock:

- the allowance review (``allowances`` / ``_scan_allowances``): an open view
  re-read itself through ``trading.changed`` as fast as the client debounced;
- trigger and bracket cards: one balance read per live trigger on every list;
- tokens that name no symbol: four metadata calls on every holdings read;
- ``recover_submitted``: about five receipt polls per stuck order per pass;
- the network strip: ``eth_feeHistory`` on every uncached call.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from agentos.trading import service as service_module
from agentos.trading.chains import BASE
from agentos.trading.evm import SEL_DECIMALS, SEL_NAME, SEL_SYMBOL
from agentos.trading.prices import TokenMeta
from agentos.trading.service import (
    ALLOWANCE_RESCAN_S,
    NAMELESS_TOKEN_TTL_S,
    NETWORK_FEE_TTL_S,
    TradingError,
    TradingService,
)
from tests.test_trading.conftest import PASSWORD
from tests.test_trading.fakes import (
    OTHER,
    USDC,
    WETH,
    FakeAggregator,
    FakeChain,
    FakePrices,
    approval_log,
)
from tests.test_trading.test_brackets import protect
from tests.test_trading.test_send_tools import _wire_send_effects
from tests.test_trading.test_service import _wire_swap_effects
from tests.test_trading.test_triggers import KEY, T0, Clock, World


def _rpc(chain: FakeChain, method: str) -> int:
    return sum(1 for call in chain.calls if call["method"] == method)


# ── A: the allowance review ────────────────────────────────────────────────


def _allowance_events(service: TradingService) -> list[dict[str, Any]]:
    return [
        payload
        for event, payload in service.events  # type: ignore[attr-defined]
        if event == "trading.changed" and payload.get("reason") == "allowances"
    ]


async def _scans_done(service: TradingService) -> None:
    """Let every background Approval-log pass finish."""
    await asyncio.gather(*service._allowance_scans.values(), return_exceptions=True)


@pytest.fixture
def review(funded_service: TradingService, base_chain: FakeChain) -> Clock:
    """A wallet whose Approval logs were scanned up to just behind the head.

    The head moves 50 blocks on every ``eth_blockNumber`` (Base makes about
    that many in ten seconds), so it has always moved when the review looks.
    """
    clock = Clock(T0)
    funded_service._now = clock
    record = funded_service.vault.resolve(None)
    funded_service.ledger.set_allowance_scan(
        BASE.chain_id, record.key, last_block=base_chain.block - 10
    )
    base_chain.head_step = 50
    base_chain.calls.clear()
    funded_service.events.clear()  # type: ignore[attr-defined]
    return clock


async def test_an_open_view_with_nothing_new_costs_one_look_a_minute(
    funded_service: TradingService, base_chain: FakeChain, review: Clock
) -> None:
    service, clock = funded_service, review
    first = await service.allowances(BASE, None)
    await _scans_done(service)
    assert _rpc(base_chain, "eth_blockNumber") == 1 and _rpc(base_chain, "eth_getLogs") == 1
    # A top-up that found nothing tells nobody: the re-read it used to cause
    # started the next top-up.
    assert _allowance_events(service) == []
    # The view stays open: the desktop re-reads on every poll and event.
    base_chain.calls.clear()
    for _ in range(11):
        clock.now += 5
        result = await service.allowances(BASE, None)
        await _scans_done(service)
    assert base_chain.calls == []  # no pairs: nothing to read live either
    assert _allowance_events(service) == []
    # The head is the one the review last read, and the scan is up to it.
    assert result["head"] == first["head"] == result["scannedTo"]
    assert result["scanning"] is False
    # A minute after the last look the review looks again, once.
    clock.now = T0 + ALLOWANCE_RESCAN_S
    again = await service.allowances(BASE, None)
    await _scans_done(service)
    assert _rpc(base_chain, "eth_blockNumber") == 1 and _rpc(base_chain, "eth_getLogs") == 1
    assert again["head"] > first["head"]
    assert (await service.allowances(BASE, None))["scannedTo"] == again["head"]


async def test_a_pass_that_adds_a_pair_tells_clients_once(
    funded_service: TradingService, base_chain: FakeChain, review: Clock
) -> None:
    service, clock = funded_service, review
    wallet = service.test_wallet  # type: ignore[attr-defined]
    base_chain.set_allowance(USDC, wallet, OTHER, 5 * 10**6)
    base_chain.logs.append(
        approval_log(
            tx_hash="0x" + "a1" * 32,
            log_index=1,
            block=base_chain.block + 1,
            token=USDC,
            owner=wallet,
            spender=OTHER,
            amount=5 * 10**6,
        )
    )
    await service.allowances(BASE, None)
    await _scans_done(service)
    assert len(_allowance_events(service)) == 1
    clock.now += 5
    base_chain.calls.clear()
    listed = await service.allowances(BASE, None)
    await _scans_done(service)
    assert [a["allowance"] for a in listed["allowances"]] == ["5"]
    # The re-read the event caused reads the amount live and nothing else.
    assert _rpc(base_chain, "eth_getLogs") == 0 and _rpc(base_chain, "eth_blockNumber") == 0
    assert len(_allowance_events(service)) == 1


async def test_a_catch_up_reports_scanning_and_tells_clients_when_it_ends(
    funded_service: TradingService, base_chain: FakeChain, review: Clock
) -> None:
    service = funded_service
    wallet = service.test_wallet  # type: ignore[attr-defined]
    record = service.vault.resolve(None)
    service.ledger.clear_allowance_scan(BASE.chain_id, record.key)
    base_chain.block = 400_000
    service.vault.set_created_block(wallet, BASE.chain_id, 50_000)
    first = await service.allowances(BASE, None)
    assert first["scanning"] is True and first["scanFrom"] == 50_000
    # Asked again while the pass runs: still scanning, no second pass, no head read.
    base_chain.calls.clear()
    second = await service.allowances(BASE, None)
    assert second["scanning"] is True and _rpc(base_chain, "eth_blockNumber") == 0
    assert len(service._allowance_scans) == 1
    await _scans_done(service)
    # Four windows of 100k blocks, one event when the catch-up ended.
    assert _rpc(base_chain, "eth_getLogs") == 4
    assert len(_allowance_events(service)) == 1
    done = await service.allowances(BASE, None)
    assert done["scanning"] is False and done["scannedTo"] == first["head"]
    assert len(_allowance_events(service)) == 1


async def test_full_and_wait_scan_to_the_head_at_once(
    funded_service: TradingService, base_chain: FakeChain, review: Clock
) -> None:
    service, clock = funded_service, review
    await service.allowances(BASE, None)
    await _scans_done(service)
    clock.now += 5
    base_chain.calls.clear()
    waited = await service.allowances(BASE, None, wait=True)
    assert _rpc(base_chain, "eth_blockNumber") == 1 and _rpc(base_chain, "eth_getLogs") == 1
    assert waited["scannedTo"] == waited["head"] and waited["scanning"] is False
    clock.now += 5
    base_chain.calls.clear()
    rescanned = await service.allowances(BASE, None, full=True, wait=True)
    assert _rpc(base_chain, "eth_blockNumber") == 1 and _rpc(base_chain, "eth_getLogs") == 1
    assert rescanned["scannedTo"] == rescanned["head"]


async def test_a_failing_window_is_not_retried_on_every_call(
    funded_service: TradingService, base_chain: FakeChain, review: Clock
) -> None:
    service, clock = funded_service, review
    base_chain.fail_methods.add("eth_getLogs")
    first = await service.allowances(BASE, None)
    await _scans_done(service)
    assert _rpc(base_chain, "eth_getLogs") > 0
    assert _allowance_events(service) == []  # a failed top-up changes nothing to show
    base_chain.calls.clear()
    for _ in range(11):
        clock.now += 5
        await service.allowances(BASE, None)
        await _scans_done(service)
    assert base_chain.calls == []
    # A minute after the failure the window is asked for again, and now it answers.
    base_chain.fail_methods.clear()
    clock.now += 5
    healed = await service.allowances(BASE, None)
    await _scans_done(service)
    assert _rpc(base_chain, "eth_getLogs") == 1
    assert (await service.allowances(BASE, None))["scannedTo"] == healed["head"] > first["head"]


async def test_a_failed_catch_up_tells_clients_once(
    funded_service: TradingService, base_chain: FakeChain, review: Clock
) -> None:
    service, clock = funded_service, review
    wallet = service.test_wallet  # type: ignore[attr-defined]
    record = service.vault.resolve(None)
    service.ledger.clear_allowance_scan(BASE.chain_id, record.key)
    base_chain.block = 400_000
    service.vault.set_created_block(wallet, BASE.chain_id, 50_000)
    base_chain.fail_methods.add("eth_getLogs")
    assert (await service.allowances(BASE, None))["scanning"] is True
    await _scans_done(service)
    assert len(_allowance_events(service)) == 1  # "scanning" is over
    base_chain.calls.clear()
    clock.now += 5
    after = await service.allowances(BASE, None)
    await _scans_done(service)
    assert after["scanning"] is False and base_chain.calls == []
    assert len(_allowance_events(service)) == 1


async def test_a_revoked_pair_disappears_on_the_next_call(
    funded_service: TradingService, base_chain: FakeChain, review: Clock
) -> None:
    service, clock = funded_service, review
    wallet = service.test_wallet  # type: ignore[attr-defined]
    record = service.vault.resolve(None)
    _wire_send_effects(base_chain, wallet)
    for spender, tx in ((OTHER, "a1"), (WETH, "a2")):
        base_chain.set_allowance(USDC, wallet, spender, 10**6)
        base_chain.logs.append(
            approval_log(
                tx_hash="0x" + tx * 32,
                log_index=1,
                block=base_chain.block + 1,
                token=USDC,
                owner=wallet,
                spender=spender,
                amount=10**6,
            )
        )
    await service.allowances(BASE, None)
    await _scans_done(service)
    clock.now += 5
    assert (await service.allowances(BASE, None))["count"] == 2
    # Revoked somewhere else: the live read finds zero, inside the interval.
    base_chain.set_allowance(USDC, wallet, WETH, 0)
    clock.now += 5
    base_chain.calls.clear()
    listed = await service.allowances(BASE, None)
    assert _rpc(base_chain, "eth_getLogs") == 0
    assert [a["spender"].lower() for a in listed["allowances"]] == [OTHER]
    assert len(service.ledger.allowances(BASE.chain_id, record.key)) == 1
    # Revoked here: gone on the next read, inside the interval too.
    clock.now += 5
    order = await service.revoke(
        chain=BASE,
        wallet=None,
        token="USDC",
        spender=OTHER,
        initiator="manual",
        session_key=None,
        note=None,
        wait=True,
    )
    assert order["status"] == "confirmed"
    clock.now += 5
    assert (await service.allowances(BASE, None))["allowances"] == []


async def test_an_approval_this_engine_sent_is_looked_for_at_once(
    funded_service: TradingService, base_chain: FakeChain, review: Clock
) -> None:
    service, clock = funded_service, review
    wallet = service.test_wallet  # type: ignore[attr-defined]
    record = service.vault.resolve(None)
    await service.allowances(BASE, None)
    await _scans_done(service)
    # A swap's approve lands half a minute later: its gas entry is the sign.
    clock.now += 30
    tx_hash = "0x" + "b1" * 32
    base_chain.set_allowance(USDC, wallet, OTHER, 7 * 10**6)
    base_chain.logs.append(
        approval_log(
            tx_hash=tx_hash,
            log_index=1,
            block=base_chain.block + 1,
            token=USDC,
            owner=wallet,
            spender=OTHER,
            amount=7 * 10**6,
        )
    )
    service.ledger.insert_entry(
        ts=clock.now,
        chain_id=BASE.chain_id,
        wallet=record.key,
        kind="approval",
        tx_hash=tx_hash,
        log_index=0,
        initiator="manual",
    )
    clock.now += 1
    base_chain.calls.clear()
    await service.allowances(BASE, None)
    await _scans_done(service)
    assert _rpc(base_chain, "eth_getLogs") == 1
    clock.now += 1
    base_chain.calls.clear()
    listed = await service.allowances(BASE, None)
    assert [a["allowance"] for a in listed["allowances"]] == ["7"]
    assert _rpc(base_chain, "eth_getLogs") == 0 and _rpc(base_chain, "eth_blockNumber") == 0


async def test_two_reviews_at_once_start_one_pass(
    funded_service: TradingService,
    base_chain: FakeChain,
    review: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = funded_service
    evm = service.evm(BASE)
    head = evm.block_number

    async def slow_head() -> int:
        await asyncio.sleep(0)  # a real node answers later: the other call runs meanwhile
        return await head()

    monkeypatch.setattr(evm, "block_number", slow_head)
    await asyncio.gather(service.allowances(BASE, None), service.allowances(BASE, None))
    await _scans_done(service)
    assert _rpc(base_chain, "eth_getLogs") == 1


# ── B: trigger and bracket cards ───────────────────────────────────────────


@pytest.fixture
async def world(
    service: TradingService,
    base_chain: FakeChain,
    fake_aggregator: FakeAggregator,
    fake_prices: FakePrices,
    monkeypatch: pytest.MonkeyPatch,
) -> World:
    monkeypatch.setattr(service_module, "CHAIN_CATCHUP_POLL_S", 0.0)
    clock = Clock()
    service._now = clock
    service.vault.setup(PASSWORD, "auto")
    wallet = (await service.import_wallet("Key main", private_key=KEY))["address"]
    base_chain.set_native(wallet, 10**18)
    base_chain.set_erc20(USDC, wallet, 1_000 * 10**6)
    base_chain.set_erc20(WETH, wallet, 10**17)  # 0.1 WETH
    return World(service, base_chain, fake_aggregator, fake_prices, clock, wallet)


def _balance_calls(chain: FakeChain) -> int:
    return len(chain.balance_reads()) + _rpc(chain, "eth_getBalance")


async def test_cards_show_the_ledgers_balance_without_a_chain_read(world: World) -> None:
    service, chain = world.service, world.chain
    trigger_id = (await world.create())["trigger"]["id"]  # sell 50 % of WETH under $1,900
    bracket_id = (await protect(world))["bracket"]["id"]
    # What the sync saw last; the chain says 0.1, so the figure shows where it came from.
    service.ledger.set_balance(BASE.chain_id, world.wallet, WETH, 3 * 10**16)
    chain.calls.clear()
    triggers = await service.trigger_list()
    brackets = await service.bracket_list()
    assert _balance_calls(chain) == 0
    (card,) = [t for t in triggers["triggers"] if t["id"] == trigger_id]
    assert card["market"]["balance"]["human"] == "0.03"
    (bracket,) = [b for b in brackets["brackets"] if b["id"] == bracket_id]
    assert bracket["takeProfit"]["market"]["balance"]["human"] == "0.03"
    assert bracket["stopLoss"]["market"]["balance"]["human"] == "0.03"


async def test_cards_read_the_chain_when_the_ledger_has_no_row(world: World) -> None:
    service, chain = world.service, world.chain
    await world.create()
    await protect(world)
    assert service.ledger.get_balance(BASE.chain_id, world.wallet, WETH) is None
    chain.calls.clear()
    triggers = await service.trigger_list()
    assert chain.balance_reads() == [WETH]
    assert triggers["triggers"][0]["market"]["balance"]["human"] == "0.1"
    chain.calls.clear()
    await service.bracket_list()
    assert chain.balance_reads() == [WETH, WETH]  # one per leg


async def test_a_fire_sizes_from_the_chain_not_the_card(world: World) -> None:
    service, chain = world.service, world.chain
    trigger_id = (await world.create())["trigger"]["id"]
    # The ledger still says 0.1 WETH; the wallet holds 0.04 when the trigger fires.
    service.ledger.set_balance(BASE.chain_id, world.wallet, WETH, 10**17)
    chain.set_erc20(WETH, world.wallet, 4 * 10**16)
    assert (await service.trigger_list())["triggers"][0]["market"]["balance"]["human"] == "0.1"
    chain.calls.clear()
    await world.check(1890)
    await world.check(1880)
    assert WETH in chain.balance_reads()
    (row,) = service.ledger.orders_for_trigger(trigger_id)
    order = service.get_order(str(row["order_id"]))
    assert order["status"] == "confirmed" and order["amountIn"] == "0.02"  # 50 % of 0.04


# ── C: tokens that name no symbol ──────────────────────────────────────────


#: A contract that answers ``symbol()``/``name()`` with an empty string.
NAMELESS = "0x5555555555555555555555555555555555555555"
#: An address with no code on the fake chain.
GHOST = "0x6666666666666666666666666666666666666666"


def _metadata_reads(chain: FakeChain, token: str) -> int:
    selectors = {SEL_SYMBOL, SEL_NAME, SEL_DECIMALS}
    return sum(
        1
        for call in chain.calls
        if call["method"] == "eth_call"
        and str(call["params"][0].get("to") or "").lower() == token
        and str(call["params"][0].get("data") or "") in selectors
    )


async def test_a_token_that_names_no_symbol_costs_its_metadata_once(
    funded_service: TradingService, base_chain: FakeChain
) -> None:
    service = funded_service
    clock = Clock(T0)
    service._now = clock
    wallet = service.test_wallet  # type: ignore[attr-defined]
    base_chain.tokens[NAMELESS] = ("", "", 9)
    base_chain.set_erc20(NAMELESS, wallet, 5 * 10**9)
    service.ledger.set_balance(BASE.chain_id, wallet, NAMELESS, 5 * 10**9)
    base_chain.calls.clear()
    await service.balances()
    assert _metadata_reads(base_chain, NAMELESS) == 3 and _rpc(base_chain, "eth_getCode") == 1
    base_chain.calls.clear()
    for _ in range(3):
        clock.now += 30
        rows = await service.balances(include_hidden=True)
        await service.portfolio(include_hidden=True)
    assert _metadata_reads(base_chain, NAMELESS) == 0 and _rpc(base_chain, "eth_getCode") == 0
    (row,) = [r for r in rows if r["token"]["address"] == NAMELESS]
    assert row["amount"] == "5" and row["token"]["symbol"] == ""
    # Asked again once the hour is up, in case the contract learnt a name.
    clock.now = T0 + NAMELESS_TOKEN_TTL_S + 1
    await service.balances()
    assert _metadata_reads(base_chain, NAMELESS) == 3 and _rpc(base_chain, "eth_getCode") == 1


async def test_no_code_and_an_unreachable_node_are_never_cached(
    funded_service: TradingService, base_chain: FakeChain
) -> None:
    service = funded_service
    for _ in range(2):
        with pytest.raises(TradingError, match="No contract"):
            await service.token_meta(BASE, GHOST)
    assert _metadata_reads(base_chain, GHOST) == 6 and _rpc(base_chain, "eth_getCode") == 2
    assert (BASE.chain_id, GHOST) not in service._nameless_until
    # A contract that names nothing, read while eth_getCode was failing: a
    # guess, asked again next time; cached once the node has answered.
    base_chain.tokens[NAMELESS] = ("", "", 18)
    base_chain.fail_methods.add("eth_getCode")
    await service.token_meta(BASE, NAMELESS)
    base_chain.fail_methods.clear()
    base_chain.calls.clear()
    await service.token_meta(BASE, NAMELESS)
    await service.token_meta(BASE, NAMELESS)
    assert _metadata_reads(base_chain, NAMELESS) == 3 and _rpc(base_chain, "eth_getCode") == 1


async def test_a_token_the_list_learns_later_is_named_at_once(
    funded_service: TradingService, base_chain: FakeChain, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = funded_service
    base_chain.tokens[NAMELESS] = ("", "", 9)
    assert (await service.token_meta(BASE, NAMELESS)).symbol == ""
    listed = TokenMeta(BASE.chain_id, NAMELESS, "LATE", "Late Listing", 9, verified=True)
    original = service.prices.known_token

    async def known_token(chain: Any, address: str) -> TokenMeta | None:
        if address.lower() == NAMELESS:
            return listed
        return await original(chain, address)

    monkeypatch.setattr(service.prices, "known_token", known_token)
    base_chain.calls.clear()
    meta = await service.token_meta(BASE, NAMELESS)
    assert meta.symbol == "LATE" and meta.verified is True
    assert _metadata_reads(base_chain, NAMELESS) == 0
    row = service.ledger.get_token(BASE.chain_id, NAMELESS)
    assert row is not None and row["symbol"] == "LATE" and row["verified"] == 1


# ── D: recovering submitted orders ─────────────────────────────────────────


async def test_a_stuck_order_costs_one_receipt_poll_per_pass(
    funded_service: TradingService, base_chain: FakeChain
) -> None:
    service = funded_service
    wallet = service.test_wallet  # type: ignore[attr-defined]
    now = service._now()
    service.ledger.insert_order(
        {
            "order_id": "ord_stuck",
            "created_at": now,
            "updated_at": now,
            "chain_id": BASE.chain_id,
            "wallet": wallet.lower(),
            "token_in": USDC,
            "token_out": WETH,
            "amount_raw": "1",
            "amount_human": "0.000001",
            "status": "submitted",
            "initiator": "manual",
            "tx_hash": "0x" + "cd" * 32,
        }
    )
    base_chain.calls.clear()
    started = time.monotonic()
    await service.recover_submitted()
    # It used to wait out a 5 s window at a 1.5 s poll, holding the DCA and trigger clock.
    assert time.monotonic() - started < 2.0
    assert _rpc(base_chain, "eth_getTransactionReceipt") == 1
    row = service.ledger.get_order("ord_stuck")
    assert row is not None and row["status"] == "submitted"
    assert row["reason"] == "waiting for the transaction to be mined"
    await service.recover_submitted()
    assert _rpc(base_chain, "eth_getTransactionReceipt") == 2


async def test_a_mined_order_is_settled_by_that_one_poll_from_its_sealed_receipt(
    funded_service: TradingService, base_chain: FakeChain, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service_module, "CHAIN_CATCHUP_POLL_S", 0.0)
    service = funded_service
    wallet = service.test_wallet  # type: ignore[attr-defined]
    _wire_swap_effects(base_chain, wallet)
    orders = await service.swap(
        chain=BASE,
        wallets=None,
        token_in="USDC",
        token_out="WETH",
        amount_in="10",
        amount_pct=None,
        slippage_pct=None,
        initiator="manual",
        session_key=None,
        note=None,
        wait=True,
    )
    order_id, tx_hash = orders[0]["orderId"], str(orders[0]["txHash"]).lower()
    # A restart dropped the confirm task: the order is ``submitted`` again.
    service.ledger.update_order(
        order_id, status="submitted", received_out_raw=None, spent_in_raw=None
    )
    # Base serves a preconfirmation first; its l1Fee is not the sealed block's.
    provisional, final = 2_957_077_405, 5_063_072_155
    receipt = base_chain.receipts[tx_hash]
    receipt["l1Fee"] = hex(provisional)
    base_chain.sealed[tx_hash] = {**receipt, "l1Fee": hex(final)}
    base_chain.head_step = 1
    base_chain.calls.clear()
    await service.recover_submitted()
    recovered = service.get_order(order_id)
    assert recovered["status"] == "confirmed" and recovered["receivedOut"] == "0.0005"
    row = service.ledger.get_order(order_id)
    assert row is not None and row["gas_wei"] == str(100_000 * 10**8 + final)
    # The one poll, then the sealed re-read once the head is past the block.
    assert _rpc(base_chain, "eth_getTransactionReceipt") == 2


# ── E: the network strip ───────────────────────────────────────────────────


async def test_the_network_strip_reads_the_fee_history_once_a_minute(
    service: TradingService, base_chain: FakeChain, robinhood_chain: FakeChain
) -> None:
    clock = Clock(T0)
    service._now = clock
    chains = (base_chain, robinhood_chain)

    def billed() -> dict[str, int]:
        out: dict[str, int] = {}
        for chain in chains:
            for call in chain.calls:
                out[call["method"]] = out.get(call["method"], 0) + 1
            chain.calls.clear()
        return out

    first = await service.network()
    assert billed() == {"eth_getBlockByNumber": 2, "eth_feeHistory": 2}
    clock.now += 20  # the desktop's poll, past the 10 s cache
    second = await service.network()
    assert billed() == {"eth_getBlockByNumber": 2}
    for a, b in zip(first["chains"], second["chains"], strict=True):
        assert b["priorityFeeGwei"] == a["priorityFeeGwei"] is not None
        assert b["baseFeeGwei"] == a["baseFeeGwei"] is not None
    clock.now += 20
    await service.network(fresh=True)
    assert billed() == {"eth_getBlockByNumber": 2, "eth_feeHistory": 2}
    clock.now += NETWORK_FEE_TTL_S
    await service.network()
    assert billed() == {"eth_getBlockByNumber": 2, "eth_feeHistory": 2}


async def test_signing_reads_its_own_fee_history(
    funded_service: TradingService, base_chain: FakeChain
) -> None:
    service = funded_service
    wallet = service.test_wallet  # type: ignore[attr-defined]
    _wire_swap_effects(base_chain, wallet)
    await service.network()
    base_chain.calls.clear()
    orders = await service.swap(
        chain=BASE,
        wallets=None,
        token_in="USDC",
        token_out="WETH",
        amount_in="10",
        amount_pct=None,
        slippage_pct=None,
        initiator="manual",
        session_key=None,
        note=None,
        wait=True,
    )
    assert orders[0]["status"] == "confirmed"
    assert _rpc(base_chain, "eth_feeHistory") >= 1
