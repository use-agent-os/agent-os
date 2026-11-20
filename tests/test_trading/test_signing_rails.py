"""The rails around signing: pinned targets, gas caps, record-before-send,
idempotent orders, atomic settlement and the short-fill alert."""

from __future__ import annotations

import sqlite3

import httpx
import pytest

from agentos.trading import service as service_module
from agentos.trading.aggregator import AGENTOS_ROUTER, ALLOWANCE_HOLDER, AggregatorProvider
from agentos.trading.chains import BASE, NATIVE_ADDRESS
from agentos.trading.evm import EvmClient, EvmRpcError, EvmTransportError
from agentos.trading.ledger import Ledger
from agentos.trading.service import TradingError, TradingService, _local_tx_hash, _sign_tx
from tests.test_trading.fakes import (
    OTHER,
    USDC,
    WALLET,
    WETH,
    FakeAggregator,
    FakeChain,
    FakePrices,
    approve_calldata,
    decode_fake_raw,
)
from tests.test_trading.test_service import _wire_swap_effects

WETH_RH = "0xaaaa000000000000000000000000000000000003"
NEW_GATEWAY_CONTRACT = "0x7777777777777777777777777777777777777777"


async def _swap(service: TradingService, **overrides: object) -> dict:
    params: dict = {
        "chain": BASE,
        "wallets": None,
        "token_in": "USDC",
        "token_out": "WETH",
        "amount_in": "10",
        "amount_pct": None,
        "slippage_pct": None,
        "initiator": "manual",
        "session_key": None,
        "note": None,
        "wait": True,
    }
    params.update(overrides)
    return (await service.swap(**params))[0]


class TestPinnedSwapTargets:
    async def test_a_native_sell_to_an_attacker_is_refused_unsigned(
        self, funded_service: TradingService, base_chain: FakeChain, fake_aggregator: FakeAggregator
    ) -> None:
        """No approval anchors a native sell, so the target pin is all there is."""
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_swap_effects(base_chain, wallet, out_token=USDC, out_amount=10 * 10**6)
        fake_aggregator.tx_to = OTHER  # "0xattacker"
        fake_aggregator.tx_value = str(10**15)
        order = await _swap(service, token_in="ETH", token_out="USDC", amount_in="0.001")
        assert order["status"] == "failed", order
        assert "not a contract this desk trusts" in order["reason"]
        assert OTHER in order["reason"]
        assert base_chain.sent == []

    async def test_an_erc20_sell_to_the_allowance_holder_is_signed(
        self, funded_service: TradingService, base_chain: FakeChain, fake_aggregator: FakeAggregator
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_swap_effects(base_chain, wallet, out_amount=5 * 10**15)
        fake_aggregator.approval_needed = True
        assert fake_aggregator.tx_to == ALLOWANCE_HOLDER
        order = await _swap(service)
        assert order["status"] == "confirmed", order
        assert decode_fake_raw(base_chain.sent[-1])["to"].lower() == ALLOWANCE_HOLDER

    @pytest.mark.parametrize(
        ("token_in", "token_out", "amount_in", "needs_approval"),
        [("USDC", "WETH", "10", True), ("ETH", "USDC", "0.001", False)],
    )
    async def test_a_sell_through_the_agentos_router_is_signed(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        fake_aggregator: FakeAggregator,
        token_in: str,
        token_out: str,
        amount_in: str,
        needs_approval: bool,
    ) -> None:
        """Contract mode: the gateway names the AgentOS Router as spender and target."""
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        if token_out == "USDC":
            _wire_swap_effects(base_chain, wallet, out_token=USDC, out_amount=10 * 10**6)
            fake_aggregator.tx_value = str(10**15)
        else:
            _wire_swap_effects(base_chain, wallet, out_amount=5 * 10**15)
        fake_aggregator.approval_needed = needs_approval
        fake_aggregator.tx_to = AGENTOS_ROUTER
        order = await _swap(service, token_in=token_in, token_out=token_out, amount_in=amount_in)
        assert order["status"] == "confirmed", order
        assert decode_fake_raw(base_chain.sent[-1])["to"].lower() == AGENTOS_ROUTER
        if needs_approval:
            approve = decode_fake_raw(base_chain.sent[0])
            assert approve["to"].lower() == USDC.lower()
            assert AGENTOS_ROUTER[2:] in approve["data"].lower()

    @pytest.mark.parametrize(
        ("token_in", "token_out", "amount_in", "needs_approval"),
        [("USDC", "WETH", "10", True), ("ETH", "USDC", "0.001", False)],
    )
    async def test_a_contract_from_the_config_file_is_signed_for(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        fake_aggregator: FakeAggregator,
        token_in: str,
        token_out: str,
        amount_in: str,
        needs_approval: bool,
    ) -> None:
        """A gateway moved to a contract this build does not know: the operator adds it."""
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        if token_out == "USDC":
            _wire_swap_effects(base_chain, wallet, out_token=USDC, out_amount=10 * 10**6)
            fake_aggregator.tx_value = str(10**15)
        else:
            _wire_swap_effects(base_chain, wallet, out_amount=5 * 10**15)
        fake_aggregator.approval_needed = needs_approval
        fake_aggregator.tx_to = NEW_GATEWAY_CONTRACT
        # Before it is configured: refused unsigned, at the spender or target pin.
        order = await _swap(service, token_in=token_in, token_out=token_out, amount_in=amount_in)
        assert order["status"] == "failed", order
        assert "not a contract this desk trusts" in order["reason"]
        assert "trading.aggregator_trusted_spenders" in order["reason"]
        assert base_chain.sent == []
        # Configured (as a config.set would: the value goes through the validator).
        service.config.aggregator_trusted_spenders = [
            NEW_GATEWAY_CONTRACT.upper().replace("0X", "0x")
        ]
        order = await _swap(service, token_in=token_in, token_out=token_out, amount_in=amount_in)
        assert order["status"] == "confirmed", order
        assert decode_fake_raw(base_chain.sent[-1])["to"].lower() == NEW_GATEWAY_CONTRACT
        # And taken out again: the next order is refused without a restart.
        service.config.aggregator_trusted_spenders = []
        sent = len(base_chain.sent)
        order = await _swap(service, token_in=token_in, token_out=token_out, amount_in=amount_in)
        assert order["status"] == "failed", order
        assert len(base_chain.sent) == sent

    async def test_the_aggregator_pins_the_allowance_holder_for_every_sell(
        self, http: httpx.AsyncClient, fake_aggregator: FakeAggregator
    ) -> None:
        from agentos.trading.aggregator import AggregatorClient

        provider = AggregatorProvider(AggregatorClient(http=http))
        for token_in in (NATIVE_ADDRESS, USDC):
            quote = await provider.quote(
                chain=BASE,
                swapper=WALLET,
                token_in=token_in,
                token_out=WETH if token_in == USDC else USDC,
                amount_raw=10**6,
                slippage_pct=None,
                decision_origin="human_mediated",
            )
            assert provider.trusted_targets(BASE, quote) == frozenset(
                {ALLOWANCE_HOLDER, AGENTOS_ROUTER}
            )


class TestGasCaps:
    async def test_the_tip_is_the_median_reward_capped_at_the_chain_ceiling(
        self, http: httpx.AsyncClient, base_chain: FakeChain
    ) -> None:
        client = EvmClient(
            "https://mainnet.base.org",
            http=http,
            max_priority_fee_wei=BASE.max_priority_fee_wei,
            max_fee_per_gas_wei=BASE.max_fee_per_gas_wei,
        )
        # One absurd block does not set the price: the median does.
        base_chain.fee_rewards = [10**6, 10**6, 10**18]
        max_fee, tip = await client.fee_data()
        assert tip == 10**6 and max_fee == 2 * base_chain.base_fee + 10**6
        # Every block absurd: the cap, not the node, decides.
        base_chain.fee_rewards = [10**18]
        max_fee, tip = await client.fee_data()
        assert tip == BASE.max_priority_fee_wei == 2 * 10**9
        # A base-fee spike is capped the same way, and maxFee >= tip holds.
        base_chain.base_fee = 400 * 10**9
        max_fee, tip = await client.fee_data()
        assert max_fee == BASE.max_fee_per_gas_wei == 50 * 10**9 and tip <= max_fee
        # The eth_gasPrice fallback goes through the same caps.
        base_chain.fee_history = False
        max_fee, tip = await client.fee_data()
        assert max_fee == 50 * 10**9 and tip == 2 * 10**9
        history = [c for c in base_chain.calls if c["method"] == "eth_feeHistory"]
        assert history and history[0]["params"] == [10, "latest", [50]]

    async def test_a_provider_gas_limit_above_the_cap_is_refused_not_clamped(
        self, funded_service: TradingService, base_chain: FakeChain, fake_aggregator: FakeAggregator
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_swap_effects(base_chain, wallet)
        fake_aggregator.gas_estimate = "5000000"
        order = await _swap(service)
        assert order["status"] == "failed", order
        assert "gas limit 6000000 exceeds cap 3000000" in order["reason"]
        assert base_chain.sent == []

    async def test_provider_fee_fields_are_ignored(
        self, funded_service: TradingService, base_chain: FakeChain
    ) -> None:
        """The quote's transaction names a gasPrice of its own; the node decides."""
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_swap_effects(base_chain, wallet)
        base_chain.fee_rewards = [3 * 10**6]
        order = await _swap(service)
        assert order["status"] == "confirmed", order
        signed = decode_fake_raw(base_chain.sent[-1])
        assert signed["maxPriorityFeePerGas"] == 3 * 10**6
        assert signed["maxFeePerGas"] == 2 * base_chain.base_fee + 3 * 10**6
        assert "gasPrice" not in signed
        assert signed["gas"] == int(288079 * 1.2)  # the provider's estimate + 20 %, under the cap

    async def test_gas_above_two_dollars_on_a_ten_dollar_order_is_refused(
        self, funded_service: TradingService, base_chain: FakeChain
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_swap_effects(base_chain, wallet)
        # 100 gwei base fee -> capped at 50 gwei; ~346k gas -> ~0.017 ETH = ~$35.
        base_chain.base_fee = 100 * 10**9
        order = await _swap(service)
        assert order["status"] == "failed", order
        assert order["reason"].startswith("trading.gas_too_high:")
        assert "$2.00 ceiling" in order["reason"]
        assert base_chain.sent == []
        # The ceiling scales with the order: at $1,000 it is $50, and $35 passes.
        base_chain.set_erc20(USDC, wallet, 5_000 * 10**6)
        order = await _swap(service, amount_in="1000")
        assert order["status"] != "failed" or "gas_too_high" not in order["reason"]


class TestRecordBeforeSend:
    def test_the_local_hash_is_the_one_eth_account_computes(self) -> None:
        from eth_account import Account

        key = bytes.fromhex("11" * 32)
        tx = {
            "chainId": 8453,
            "nonce": 3,
            "to": "0x" + "22" * 20,
            "value": 1,
            "data": "0x",
            "gas": 21_000,
            "maxFeePerGas": 10**9,
            "maxPriorityFeePerGas": 10**6,
            "type": 2,
        }
        raw = _sign_tx(tx, key)
        assert _local_tx_hash(raw) == "0x" + Account.sign_transaction(tx, key).hash.hex()

    @pytest.mark.parametrize("error", [EvmTransportError, EvmRpcError])
    async def test_a_refused_broadcast_leaves_a_submitted_row_with_its_hash(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        monkeypatch: pytest.MonkeyPatch,
        error: type[Exception],
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_swap_effects(base_chain, wallet)
        evm = service.evm(BASE)
        raws: list[str] = []

        async def refuse(raw: str) -> str:
            raws.append(raw)
            raise error("node down")

        monkeypatch.setattr(evm, "send_raw_transaction", refuse)
        order = await _swap(service, wait=False)
        # Recorded before the broadcast, under the hash the transaction has.
        assert order["status"] == "submitted", order
        assert order["txHash"] == _local_tx_hash(raws[0])
        assert order["reason"] == "broadcast rejected: node down"
        order_id = order["orderId"]
        # Nothing after the fact may call it failed: not an exception in the
        # pipeline, not a settlement pass that finds no receipt yet.
        await service._fail_order(order_id, TradingError("trading.rpc", "later"))
        assert service.get_order(order_id)["status"] == "submitted"
        await service._confirm(order_id, order["txHash"], None, timeout_s=0.01)
        row = service.get_order(order_id)
        assert row["status"] == "submitted" and row["reason"] == "broadcast rejected: node down"
        # ...until the shorter give-up for a refused broadcast: ten minutes, not six hours.
        service._now = lambda: __import__("time").time() + 601
        await service._confirm(order_id, order["txHash"], None, timeout_s=0.01)
        row = service.get_order(order_id)
        assert row["status"] == "failed"
        assert row["reason"] == "transaction never mined (node down)"

    async def test_a_refused_broadcast_that_did_land_is_settled(
        self, funded_service: TradingService, base_chain: FakeChain, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The node said no, the chain said yes: the recorded hash finds the receipt."""
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_swap_effects(base_chain, wallet, out_amount=5 * 10**15)
        evm = service.evm(BASE)
        inner = base_chain.on_send

        async def accept_then_refuse(raw: str) -> str:
            node_hash = inner(raw)
            # The receipt lives under the hash this transaction really has.
            base_chain.receipts[_local_tx_hash(raw)] = base_chain.receipts.pop(node_hash)
            raise EvmTransportError("502 from the balancer")

        monkeypatch.setattr(evm, "send_raw_transaction", accept_then_refuse)
        order = await _swap(service, wait=False)
        assert order["status"] == "submitted"
        # Drop the in-process watcher, as a restart would, and let housekeeping find it.
        await service.stop()
        await service.recover_submitted()
        recovered = service.get_order(order["orderId"])
        assert recovered["status"] == "confirmed" and recovered["receivedOut"] == "0.005"

    async def test_the_approval_hash_is_recorded_before_its_broadcast(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        fake_aggregator: FakeAggregator,
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_swap_effects(base_chain, wallet)
        fake_aggregator.approval_needed = True
        # (field, hash, how many transactions the node had seen at that moment)
        seen: list[tuple[str, str, int]] = []
        real_update = service.ledger.update_order

        def spy(order_id: str, **fields):  # type: ignore[no-untyped-def]
            for name in ("approval_tx_hash", "tx_hash"):
                if fields.get(name):
                    seen.append((name, str(fields[name]), len(base_chain.sent)))
            return real_update(order_id, **fields)

        service.ledger.update_order = spy  # type: ignore[method-assign]
        order = await _swap(service)
        assert order["status"] == "confirmed", order
        first_approval = next(s for s in seen if s[0] == "approval_tx_hash")
        first_swap = next(s for s in seen if s[0] == "tx_hash")
        # Each hash was written before the node saw its transaction, and it
        # is the hash the raw bytes really have.
        assert first_approval == ("approval_tx_hash", _local_tx_hash(base_chain.sent[0]), 0)
        assert first_swap == ("tx_hash", _local_tx_hash(base_chain.sent[1]), 1)
        # What was signed is our own approve(spender, amount), not the provider's bytes.
        approval = decode_fake_raw(base_chain.sent[0])
        assert approval["data"] == approve_calldata(ALLOWANCE_HOLDER, 10 * 10**6)

    async def test_recovery_treats_any_row_with_a_hash_as_submitted(
        self, funded_service: TradingService, base_chain: FakeChain
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_swap_effects(base_chain, wallet, out_amount=5 * 10**15)
        order = await _swap(service)
        assert order["status"] == "confirmed"
        # A crash could not leave this row (the flip and the hash are one
        # write) but a future status might: a hash means "may be on chain".
        service.ledger.update_order(
            order["orderId"], status="approved", received_out_raw=None, spent_in_raw=None
        )
        await service.recover_submitted()
        assert service.get_order(order["orderId"])["status"] == "confirmed"


class TestIdempotentOrders:
    async def test_the_same_client_order_id_creates_one_swap(
        self, funded_service: TradingService, base_chain: FakeChain
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_swap_effects(base_chain, wallet)
        first = await _swap(service, client_order_id="agent:main:swap-1")
        again = await _swap(service, client_order_id="agent:main:swap-1")
        assert first["orderId"] == again["orderId"]
        assert again["clientOrderId"] == "agent:main:swap-1"
        assert again["status"] == first["status"] == "confirmed"
        assert len(base_chain.sent) == 1
        assert len(service.ledger.list_orders(wallet=wallet)) == 1
        # A different key is a different order.
        other = await _swap(service, client_order_id="agent:main:swap-2")
        assert other["orderId"] != first["orderId"]
        assert len(base_chain.sent) == 2

    async def test_the_same_client_order_id_creates_one_send(
        self, funded_service: TradingService, base_chain: FakeChain
    ) -> None:
        from tests.test_trading.test_send_tools import _wire_send_effects

        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_send_effects(base_chain, wallet)
        recipients = [{"to": OTHER, "amount": "1"}, {"to": WALLET, "amount": "2"}]
        first = await service.send(
            chain=BASE,
            wallet=None,
            token="USDC",
            recipients=recipients,
            initiator="manual",
            session_key=None,
            note=None,
            wait=True,
            client_order_id="pay-run-7",
        )
        again = await service.send(
            chain=BASE,
            wallet=None,
            token="USDC",
            recipients=recipients,
            initiator="manual",
            session_key=None,
            note=None,
            wait=True,
            client_order_id="pay-run-7",
        )
        assert [o["orderId"] for o in again] == [o["orderId"] for o in first]
        assert len(first) == 2 and len(base_chain.sent) == 2

    async def test_a_bad_client_order_id_is_refused_before_anything_happens(
        self, funded_service: TradingService
    ) -> None:
        for bad in ("a" * 65, "has space", "semi;colon", "ünïcode"):
            with pytest.raises(TradingError) as info:
                await _swap(funded_service, client_order_id=bad)
            assert info.value.code == "trading.invalid"
        assert funded_service.ledger.list_orders() == []

    def test_the_ledger_enforces_the_key_per_wallet_and_recipient(self, ledger: Ledger) -> None:
        base = {
            "created_at": 1,
            "updated_at": 1,
            "chain_id": 8453,
            "wallet": WALLET,
            "token_in": USDC,
            "token_out": USDC,
            "amount_raw": "1",
            "amount_human": "0.000001",
            "status": "quoted",
            "initiator": "agent",
            "client_order_id": "k",
        }
        ledger.insert_order({**base, "order_id": "a", "kind": "send", "recipient": OTHER})
        ledger.insert_order({**base, "order_id": "b", "kind": "send", "recipient": WALLET})
        ledger.insert_order({**base, "order_id": "c", "wallet": OTHER})
        with pytest.raises(sqlite3.IntegrityError):
            ledger.insert_order({**base, "order_id": "d", "kind": "send", "recipient": OTHER})
        assert [o["order_id"] for o in ledger.find_orders_by_client_id("k")] == ["a", "b", "c"]
        # Rows without a key never collide with each other.
        ledger.insert_order({**base, "order_id": "e", "client_order_id": None})
        ledger.insert_order({**base, "order_id": "f", "client_order_id": None})


class TestAtomicSettlement:
    def test_the_flip_and_the_spend_land_together_or_not_at_all(
        self, ledger: Ledger, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ledger.insert_order(
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
                "status": "submitted",
                "initiator": "agent",
                "value_usd": 10.0,
            }
        )

        def broken(*args: object, **kwargs: object) -> None:
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(ledger, "add_daily_spend", broken)
        with pytest.raises(sqlite3.OperationalError):
            ledger.settle_order(
                "o1",
                expect_status="submitted",
                spend=(WALLET, 10.0, "2026-09-20"),
                status="confirmed",
            )
        assert ledger.get_order("o1")["status"] == "submitted"  # type: ignore[index]
        assert ledger.spent_today(WALLET, "2026-09-20") == 0.0
        monkeypatch.undo()
        row = ledger.settle_order(
            "o1", expect_status="submitted", spend=(WALLET, 10.0, "2026-09-20"), status="confirmed"
        )
        assert row and row["status"] == "confirmed"
        assert ledger.spent_today(WALLET, "2026-09-20") == 10.0
        # The loser of the compare-and-set counts nothing.
        assert (
            ledger.settle_order(
                "o1",
                expect_status="submitted",
                spend=(WALLET, 10.0, "2026-09-20"),
                status="confirmed",
            )
            is None
        )
        assert ledger.spent_today(WALLET, "2026-09-20") == 10.0

    async def test_a_failed_spend_write_leaves_the_swap_for_the_next_pass(
        self, funded_service: TradingService, base_chain: FakeChain, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_swap_effects(base_chain, wallet, out_amount=5 * 10**15)

        def broken(*args: object, **kwargs: object) -> None:
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(service.ledger, "add_daily_spend", broken)
        order = await _swap(service, initiator="agent")
        assert order["status"] == "submitted", order
        assert (order["reason"] or "").startswith("settling:")
        assert service.ledger.spent_today(wallet) == 0.0
        monkeypatch.undo()
        await service.recover_submitted()
        recovered = service.get_order(order["orderId"])
        assert recovered["status"] == "confirmed"
        assert service.limits(wallet)["spentTodayUsd"] == pytest.approx(10.0)
        swaps = [e for e in service.ledger.list_entries(wallet=wallet) if e["kind"] == "swap"]
        assert len(swaps) == 1


class TestGuardrailValue:
    async def test_the_order_is_worth_the_larger_of_its_two_sides(
        self, funded_service: TradingService, fake_prices: FakePrices
    ) -> None:
        """A sell-side feed at a hundredth of the truth cannot shrink the order."""
        service = funded_service
        fake_prices.spot[("base", USDC)] = 0.01
        quote = await service.quote(
            chain=BASE, wallet=None, token_in="USDC", token_out="WETH", amount_in="10"
        )
        assert quote["valueUsd"] == pytest.approx(10.0)  # 0.005 WETH at $2,000, not $0.10
        # And an agent order of that size is judged at the larger value too.
        service.config.approval_threshold_usd = 5.0
        orders = await service.swap(
            chain=BASE,
            wallets=None,
            token_in="USDC",
            token_out="WETH",
            amount_in="10",
            amount_pct=None,
            slippage_pct=None,
            initiator="agent",
            session_key=None,
            note=None,
        )
        assert orders[0]["status"] == "awaiting_approval"
        assert orders[0]["valueUsd"] == pytest.approx(10.0)

    async def test_one_unpriced_side_falls_back_to_the_other(
        self, funded_service: TradingService, fake_prices: FakePrices
    ) -> None:
        service = funded_service
        del fake_prices.spot[("base", USDC)]
        quote = await service.quote(
            chain=BASE, wallet=None, token_in="WETH", token_out="USDC", amount_in="0.001"
        )
        assert quote["valueUsd"] == pytest.approx(2.0)


class TestShortFill:
    async def test_a_short_fill_is_confirmed_and_says_so(
        self, funded_service: TradingService, base_chain: FakeChain, fake_aggregator: FakeAggregator
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        # Quoted 0.005 WETH (min 0.004975); the chain delivers 0.0005.
        _wire_swap_effects(base_chain, wallet, out_amount=5 * 10**14)
        order = await _swap(service)
        assert order["status"] == "confirmed", order
        assert order["receivedOut"] == "0.0005"
        min_out = fake_aggregator.amount_out * 995 // 1000
        assert order["reason"] == f"short fill: received {5 * 10**14} below min {min_out}"
        # A full fill carries no note at all.
        _wire_swap_effects(base_chain, wallet, out_amount=5 * 10**15)
        order = await _swap(service)
        assert order["status"] == "confirmed" and order["reason"] is None
        assert service_module.BROADCAST_REJECTED not in (order["reason"] or "")
