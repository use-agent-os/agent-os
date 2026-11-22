"""Bridge orders (``docs/bridge.md``) from quote to the destination deposit.

Same faked stack as ``test_send_tools.py``: AgentOS DEX's bridge endpoints
are faked (``FakeDexBridge``), the origin chain moves balances when the
signed call to the bridge contract lands, and the destination chain carries
the fill the fake reports. Each flow is checked from order to ledger.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from agentos.trading import guardrails
from agentos.trading.chains import BASE, NATIVE_ADDRESS, ROBINHOOD, checksum_address
from agentos.trading.evm import SEL_APPROVE
from agentos.trading.service import TradingError, TradingService
from agentos.trading.sync import bridge_destination_chain_id
from tests.test_trading.fakes import (
    AGENTOS_BRIDGE,
    BRIDGE_COMMIT,
    BRIDGE_NATIVE,
    SEL_BRIDGE,
    USDC,
    USDG,
    FakeChain,
    FakeDexBridge,
    bridge_expected_out,
    decode_fake_raw,
    transfer_log,
)

#: Who pays the wallet on the destination chain (a bridge's own relayer).
SOLVER = "0x4cd00e387622c35bddb9b4c962c136462338bc31"
ETH_OUT = bridge_expected_out(10**16)
USD_OUT = bridge_expected_out(25 * 10**6)
DEST_TX = "0x" + "d00d".rjust(64, "0")


def _events(service: TradingService, name: str) -> list[dict[str, Any]]:
    return [payload for event, payload in service.events if event == name]  # type: ignore[attr-defined]


def _wire_bridge_effects(chain: FakeChain, wallet: str, *, status: int = 1) -> None:
    """Make a signed approve or bridge call land the way a node would."""

    def on_send(raw: str) -> str:
        tx = decode_fake_raw(raw)
        chain._seq += 1
        tx_hash = "0x" + format(0xB41D00 + chain._seq, "x").rjust(64, "0")
        gas_wei = 21_000 * 10**8
        sender = wallet.lower()
        chain.nonces[sender] = tx.get("nonce", 0) + 1
        chain.native[sender] -= gas_wei
        chain.record_transaction(tx_hash, {**tx, "from": wallet})
        data = str(tx.get("data") or "0x")
        logs: list[dict[str, Any]] = []
        if status != 1:
            chain.receipt(tx_hash, status=0, gas_used=21_000, gas_price=10**8)
            return tx_hash
        if data.startswith(SEL_APPROVE):
            chain.apply_approve(sender, tx)
        elif data.startswith(SEL_BRIDGE):
            # bridge(BridgeParams): word 1 is the token, word 2 the gross amount.
            token = "0x" + data[74:138][-40:]
            amount = int(data[138:202], 16)
            target = str(tx["to"]).lower()
            if token == BRIDGE_NATIVE:
                value = int(tx["value"])
                chain.native[sender] -= value
                chain.native[target] = chain.native.get(target, 0) + value
            else:
                chain.set_erc20(token, sender, chain.get_erc20(token, sender) - amount)
                chain.set_erc20(token, target, chain.get_erc20(token, target) + amount)
                chain.set_allowance(token, sender, target, 0)
                logs = [
                    transfer_log(
                        tx_hash=tx_hash,
                        log_index=3,
                        block=chain.block,
                        token=token,
                        sender=sender,
                        recipient=target,
                        amount=amount,
                    )
                ]
        chain.receipt(tx_hash, status=1, gas_used=21_000, gas_price=10**8, logs=logs)
        return tx_hash

    chain.on_send = on_send


def _land_fill(
    chain: FakeChain, wallet: str, *, token: str, amount: int, tx_hash: str = DEST_TX
) -> None:
    """The bridge's fill on the destination chain: a receipt, a transaction, the balance."""
    sender = SOLVER
    if token == NATIVE_ADDRESS:
        chain.set_native(wallet, chain.native.get(wallet.lower(), 0) + amount)
        chain.transactions[tx_hash.lower()] = {
            "hash": tx_hash,
            "from": sender,
            "to": wallet,
            "input": "0x",
            "value": hex(amount),
        }
        chain.receipt(tx_hash, status=1, gas_used=21_000, gas_price=10**8)
        return
    chain.set_erc20(token, wallet, chain.get_erc20(token, wallet) + amount)
    chain.transactions[tx_hash.lower()] = {
        "hash": tx_hash,
        "from": sender,
        "to": token,
        "input": "0x",
        "value": "0x0",
    }
    chain.receipt(
        tx_hash,
        status=1,
        gas_used=50_000,
        gas_price=10**8,
        logs=[
            transfer_log(
                tx_hash=tx_hash,
                log_index=5,
                block=chain.block,
                token=token,
                sender=sender,
                recipient=wallet.lower(),
                amount=amount,
            )
        ],
    )


async def _bridge(service: TradingService, **kw: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "origin": BASE,
        "destination": ROBINHOOD,
        "token": "ETH",
        "wallet": None,
        "amount_in": "0.01",
        "amount_pct": None,
        "amount_usd": None,
        "initiator": "manual",
        "session_key": None,
        "note": None,
        "wait": True,
    }
    params.update(kw)
    return await service.bridge(**params)


@pytest.fixture
def robinhood_tokens(robinhood_chain: FakeChain) -> FakeChain:
    robinhood_chain.tokens[USDG] = ("USDG", "Global Dollar", 6)
    return robinhood_chain


@pytest.fixture
def fast_fill(funded_service: TradingService) -> TradingService:
    """A tracker that never sleeps for real."""

    async def nap(_seconds: float) -> None:
        await asyncio.sleep(0)

    funded_service._bridge_sleep = nap
    return funded_service


class TestGuardrail:
    def test_manual_bridges_run_and_agent_bridges_wait(self) -> None:
        manual = guardrails.evaluate_bridge(
            initiator="manual", value_usd=1e9, daily_cap_usd=10.0, spent_today_usd=0.0
        )
        assert manual.decision == "allow"
        agent = guardrails.evaluate_bridge(
            initiator="agent", value_usd=1.0, daily_cap_usd=1000.0, spent_today_usd=0.0
        )
        assert agent.decision == "needs_approval"
        assert agent.reason == "a bridge moves funds to the other chain for good; it waits for you"
        unpriced = guardrails.evaluate_bridge(
            initiator="agent", value_usd=None, daily_cap_usd=1000.0, spent_today_usd=0.0
        )
        assert unpriced.decision == "needs_approval" and "a bridge always waits" in unpriced.reason

    def test_the_daily_cap_still_bites(self) -> None:
        over = guardrails.evaluate_bridge(
            initiator="agent", value_usd=600.0, daily_cap_usd=1000.0, spent_today_usd=500.0
        )
        assert over.decision == "blocked_daily_cap"
        off = guardrails.evaluate_bridge(
            initiator="agent", value_usd=1.0, daily_cap_usd=0.0, spent_today_usd=0.0
        )
        assert off.decision == "blocked_daily_cap"


class TestQuote:
    async def test_quote_prices_the_route_without_creating_anything(
        self,
        funded_service: TradingService,
        robinhood_tokens: FakeChain,
        fake_bridge: FakeDexBridge,
    ) -> None:
        service = funded_service
        quote = await service.bridge_quote(
            origin=BASE, destination=ROBINHOOD, token="USDC", amount_in="25", initiator="agent"
        )
        assert quote["route"] == {"from": "base", "to": "robinhood", "asset": "USD"}
        assert quote["tokenIn"]["symbol"] == "USDC" and quote["tokenOut"]["symbol"] == "USDG"
        assert quote["tokenOut"]["chainId"] == 4663
        assert quote["amountIn"] == "25" and quote["amountInRaw"] == str(25 * 10**6)
        assert 24.0 < float(quote["expectedOut"]) < 25.0
        assert quote["expectedOutRaw"] == str(USD_OUT)
        # The least it may deliver: the bridge's own floor, less the 1% re-quote tolerance.
        assert quote["minOutRaw"] == str(int(int(USD_OUT * BRIDGE_COMMIT) * 0.99))
        # 5 bps to AgentOS, the rest of the shortfall to the bridge, in dollars.
        assert quote["fee"] == {"bps": 5, "amount": "0.0125", "amountRaw": "12500"}
        assert quote["fees"]["appUsd"] == pytest.approx(0.0125)
        assert quote["fees"]["relayerUsd"] == pytest.approx((24_987_500 - USD_OUT) / 10**6)
        assert quote["feesUsd"] == pytest.approx((25 * 10**6 - USD_OUT) / 10**6)
        assert quote["rate"] == pytest.approx(USD_OUT / (25 * 10**6))
        assert quote["refundGuaranteed"] is True and quote["timeEstimateSeconds"] == 1
        # A preview has no order behind it: nothing to track, nothing to sign.
        assert quote["requestId"] is None and fake_bridge.request_ids == []
        assert "solver" not in quote
        assert [(r["source"], r["selected"]) for r in quote["routes"]] == [
            ("relay", True),
            ("across", False),
        ]
        assert quote["routes"][0]["expectedOut"] == quote["expectedOut"]
        assert quote["valueUsd"] == pytest.approx(25.0)
        assert quote["balance"] == "1000"
        assert quote["guard"]["decision"] == "needs_approval"
        assert quote["provider"] == "relay" and quote["providerLabel"] == "Relay"
        assert quote["source"] == "relay" and quote["sourceLabel"] == "Relay"
        assert service.list_orders()["orders"] == []
        assert fake_bridge.calls[-1]["params"] == {
            "originChain": "8453",
            "destinationChain": "4663",
            "sellToken": USDC,
            "buyToken": USDG,
            "amount": str(25 * 10**6),
        }

    async def test_quote_says_which_bridge_and_whether_it_refunds(
        self,
        funded_service: TradingService,
        robinhood_tokens: FakeChain,
        fake_bridge: FakeDexBridge,
    ) -> None:
        fake_bridge.source = "across"
        fake_bridge.refund_guaranteed = False
        quote = await funded_service.bridge_quote(
            origin=BASE, destination=ROBINHOOD, token="USDC", amount_in="25"
        )
        assert quote["provider"] == "across" and quote["providerLabel"] == "Across"
        assert quote["refundGuaranteed"] is False and quote["timeEstimateSeconds"] == 5

    async def test_an_amount_no_bridge_carries_is_no_route(
        self,
        funded_service: TradingService,
        robinhood_tokens: FakeChain,
        fake_bridge: FakeDexBridge,
    ) -> None:
        from tests.test_trading.fakes import bridge_fixture

        fake_bridge.quote_error = (400, bridge_fixture("error_no_route"))
        with pytest.raises(TradingError) as info:
            await funded_service.bridge_quote(
                origin=BASE, destination=ROBINHOOD, token="ETH", amount_in="0.000000000000001"
            )
        assert info.value.code == "trading.bridge.no_route"
        assert [r["source"] for r in info.value.details["routes"]] == ["relay", "across"]

    async def test_unsupported_routes_are_refused_with_the_list(
        self, funded_service: TradingService
    ) -> None:
        with pytest.raises(TradingError) as info:
            await funded_service.bridge_quote(
                origin=BASE, destination=ROBINHOOD, token="WETH", amount_in="1"
            )
        assert info.value.code == "trading.bridge.unsupported_route"
        assert {r["tokenIn"] for r in info.value.details["supportedRoutes"]} == {
            "ETH",
            "USDC",
            "USDG",
        }
        with pytest.raises(TradingError, match="between Base and Robinhood Chain only"):
            await funded_service.bridge_quote(
                origin=BASE, destination=BASE, token="ETH", amount_in="1"
            )

    async def test_quote_refuses_more_than_the_wallet_holds(
        self, funded_service: TradingService, robinhood_tokens: FakeChain
    ) -> None:
        with pytest.raises(TradingError) as info:
            await funded_service.bridge_quote(
                origin=BASE, destination=ROBINHOOD, token="ETH", amount_in="2"
            )
        assert info.value.code == "trading.insufficient_balance"


class TestCreate:
    async def test_agent_bridge_always_waits_for_a_human(
        self,
        funded_service: TradingService,
        robinhood_tokens: FakeChain,
        fake_bridge: FakeDexBridge,
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        order = await _bridge(
            service,
            token="USDC",
            amount_in="25",
            initiator="agent",
            session_key="agent:main:webchat:desk",
            note="user: move 25 USDC",
        )
        assert order["kind"] == "bridge" and order["status"] == "awaiting_approval"
        assert (
            order["reason"] == "a bridge moves funds to the other chain for good; it waits for you"
        )
        assert order["chainId"] == 8453 and order["tokenIn"]["symbol"] == "USDC"
        assert order["tokenOut"]["symbol"] == "USDG" and order["tokenOut"]["chainId"] == 4663
        assert order["amountIn"] == "25" and 24.0 < float(order["expectedOut"]) < 25.0
        assert order["recipient"] == checksum_address(AGENTOS_BRIDGE)
        assert order["recipientLabel"] == "AgentOS Bridge"
        assert order["provider"] == "relay" and order["providerLabel"] == "Relay"
        assert order["minOut"] and float(order["minOut"]) < float(order["expectedOut"])
        assert order["valueUsd"] == pytest.approx(25.0)
        assert order["expiresAt"] is not None
        bridge = order["bridge"]
        assert bridge["originChainId"] == 8453 and bridge["destinationChainId"] == 4663
        assert bridge["destinationChain"]["name"] == "Robinhood Chain"
        assert bridge["asset"] == "USD" and bridge["requestId"] == fake_bridge.request_ids[-1]
        assert bridge["fillStatus"] is None and bridge["depositTxHash"] is None
        assert bridge["feesUsd"] is not None and bridge["fees"]["appUsd"] == pytest.approx(0.0125)
        assert bridge["source"] == "relay" and bridge["sourceLabel"] == "Relay"
        assert bridge["refundGuaranteed"] is True and bridge["feeBps"] == 5
        # The plan remembers what was agreed to, and that it came through AgentOS DEX.
        plan = json.loads(service.ledger.get_order(order["orderId"])["quote_json"])  # type: ignore[index]
        assert plan["via"] == "agentos-dex" and plan["source"] == "relay"
        assert plan["approved"] == {
            "requestId": fake_bridge.request_ids[-1],
            "source": "relay",
            "expectedOutRaw": str(USD_OUT),
            "floorRaw": str(int(USD_OUT * 0.99)),
            "minOutRaw": str(int(int(USD_OUT * BRIDGE_COMMIT) * 0.99)),
        }
        assert "steps" not in plan["quote"] and plan["quote"]["bridge"] == AGENTOS_BRIDGE
        # In flight, it already counts against the agent's cap.
        assert service.ledger.open_agent_value_usd(wallet.lower()) == pytest.approx(25.0)
        asked = _events(service, "trading.approval.requested")
        assert asked and asked[-1]["order"]["orderId"] == order["orderId"]
        # Nothing was signed.
        assert service.get_order(order["orderId"])["txHash"] is None

    async def test_agent_bridge_over_the_cap_is_refused(
        self, funded_service: TradingService, robinhood_tokens: FakeChain
    ) -> None:
        service = funded_service
        service.config.daily_cap_usd = 20.0
        order = await _bridge(service, token="USDC", amount_in="25", initiator="agent")
        assert order["status"] == "rejected" and order["reason"].startswith("daily cap")

    async def test_client_order_id_makes_create_idempotent(
        self, funded_service: TradingService, robinhood_tokens: FakeChain
    ) -> None:
        service = funded_service
        first = await _bridge(
            service, token="USDC", amount_in="5", initiator="agent", client_order_id="br-1"
        )
        again = await _bridge(
            service, token="USDC", amount_in="5", initiator="agent", client_order_id="br-1"
        )
        assert again["orderId"] == first["orderId"]
        assert len(service.list_orders()["orders"]) == 1


class TestExecute:
    async def test_manual_native_bridge_lands_on_the_other_chain(
        self,
        fast_fill: TradingService,
        base_chain: FakeChain,
        robinhood_tokens: FakeChain,
        fake_bridge: FakeDexBridge,
    ) -> None:
        service = fast_fill
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_bridge_effects(base_chain, wallet)
        expected_out = ETH_OUT
        _land_fill(robinhood_tokens, wallet, token=NATIVE_ADDRESS, amount=expected_out)
        fake_bridge.default_statuses = [
            {"status": "pending"},
            {"status": "filled", "originTxHash": "0xaa", "destinationTxHash": DEST_TX},
        ]
        order = await _bridge(service, note="to Robinhood")
        assert order["status"] == "confirmed", order
        assert order["kind"] == "bridge" and order["reason"] is None
        # One quote to create, one fresh quote right before signing.
        quotes = [c for c in fake_bridge.calls if c["path"] == "/v1/bridge/quote"]
        assert len(quotes) == 2
        # Both name the wallet, and the second is pinned to the bridge that was shown.
        assert all(q["params"]["taker"] == wallet.lower() for q in quotes)
        assert "source" not in quotes[0]["params"] and quotes[1]["params"]["source"] == "relay"
        assert order["bridge"]["requestId"] == fake_bridge.request_ids[-1]
        # The fill is asked of the bridge the order ran on, by its tracking id.
        polls = [c["params"] for c in fake_bridge.calls if c["path"] == "/v1/bridge/status"]
        assert polls and all(
            p == {"source": "relay", "trackingId": fake_bridge.request_ids[-1]} for p in polls
        )
        # The call: the bridge contract, the whole amount as value, no approval.
        assert len(base_chain.sent) == 1
        tx = decode_fake_raw(base_chain.sent[0])
        assert tx["to"].lower() == AGENTOS_BRIDGE and tx["value"] == 10**16
        assert tx["data"].startswith(SEL_BRIDGE) and tx["type"] == 2
        assert order["approvalTxHash"] is None
        assert base_chain.native[AGENTOS_BRIDGE] == 10**16
        assert order["recipient"] == checksum_address(AGENTOS_BRIDGE)
        # Once sent, the floor is what the bridge committed to for this very quote.
        row = service.ledger.get_order(order["orderId"])
        assert row is not None and row["min_out_raw"] == str(int(ETH_OUT * BRIDGE_COMMIT))
        assert row["provider"] == "relay" and row["recipient"] == AGENTOS_BRIDGE
        bridge = order["bridge"]
        assert bridge["depositTxHash"] == order["txHash"]
        assert bridge["depositExplorerUrl"] == f"https://basescan.org/tx/{order['txHash']}"
        assert bridge["fillStatus"] == "success" and bridge["destinationTxHash"] == DEST_TX
        assert bridge["destinationExplorerUrl"] == f"https://robin.etherscan.io/tx/{DEST_TX}"
        assert float(order["receivedOut"]) == pytest.approx(expected_out / 10**18)
        # Origin: a withdrawal under the order; destination: a deposit under the same order.
        withdraws = service.ledger.list_entries(wallet=wallet.lower(), kind="withdraw")
        assert len(withdraws) == 1
        assert withdraws[0]["chain_id"] == 8453 and withdraws[0]["order_id"] == order["orderId"]
        assert withdraws[0]["note"] == "to Robinhood" and withdraws[0]["token_in"] == NATIVE_ADDRESS
        assert withdraws[0]["amount_in_raw"] == str(10**16) and withdraws[0]["gas_usd"]
        deposits = service.ledger.list_entries(wallet=wallet.lower(), kind="deposit")
        arrival = [d for d in deposits if d["order_id"] == order["orderId"]]
        assert len(arrival) == 1
        assert arrival[0]["chain_id"] == 4663 and arrival[0]["tx_hash"] == DEST_TX
        assert arrival[0]["amount_out_raw"] == str(expected_out)
        assert arrival[0]["note"] == "to Robinhood" and arrival[0]["initiator"] == "manual"
        # Both balance caches moved with it.
        assert (
            service.ledger.get_balance(8453, wallet.lower(), NATIVE_ADDRESS)
            == (base_chain.native[wallet.lower()])
        )
        assert (
            service.ledger.get_balance(4663, wallet.lower(), NATIVE_ADDRESS)
            == (robinhood_tokens.native[wallet.lower()])
        )
        # Manual bridges never count against the agent's cap.
        assert service.limits(wallet)["spentTodayUsd"] == 0.0
        assert _events(service, "trading.order.finished")[-1]["order"]["status"] == "confirmed"

    async def test_manual_erc20_bridge_approves_the_bridge_contract_then_calls_it(
        self,
        fast_fill: TradingService,
        base_chain: FakeChain,
        robinhood_tokens: FakeChain,
        fake_bridge: FakeDexBridge,
    ) -> None:
        service = fast_fill
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_bridge_effects(base_chain, wallet)
        expected_out = USD_OUT
        _land_fill(robinhood_tokens, wallet, token=USDG, amount=expected_out)
        fake_bridge.default_statuses = [
            {"status": "filled", "originTxHash": "0xaa", "destinationTxHash": DEST_TX}
        ]
        order = await _bridge(service, token="USDC", amount_in="25")
        assert order["status"] == "confirmed", order
        assert len(base_chain.sent) == 2
        approve = decode_fake_raw(base_chain.sent[0])
        deposit = decode_fake_raw(base_chain.sent[1])
        # The approval is the engine's own: exactly the order, to the bridge contract.
        assert approve["to"].lower() == USDC and approve["value"] == 0
        assert approve["data"] == SEL_APPROVE + AGENTOS_BRIDGE[2:].rjust(64, "0") + format(
            25 * 10**6, "x"
        ).rjust(64, "0")
        assert deposit["to"].lower() == AGENTOS_BRIDGE and deposit["value"] == 0
        assert deposit["data"].startswith(SEL_BRIDGE)
        # The contract pulls the whole amount, fee included.
        assert base_chain.get_erc20(USDC, AGENTOS_BRIDGE) == 25 * 10**6
        assert (
            order["approvalTxHash"]
            and order["bridge"]["approvalTxHash"] == (order["approvalTxHash"])
        )
        assert base_chain.get_erc20(USDC, wallet) == 975 * 10**6
        withdraw = service.ledger.list_entries(wallet=wallet.lower(), kind="withdraw")[0]
        assert withdraw["token_in"] == USDC and withdraw["log_index"] == 3
        assert withdraw["note"] == "bridged to Robinhood Chain (Relay)"
        approvals = service.ledger.list_entries(wallet=wallet.lower(), kind="approval")
        assert len(approvals) == 1 and approvals[0]["order_id"] == order["orderId"]
        arrival = [
            d
            for d in service.ledger.list_entries(wallet=wallet.lower(), kind="deposit")
            if d["order_id"] == order["orderId"]
        ][0]
        assert arrival["token_out"] == USDG and arrival["log_index"] == 5
        assert arrival["amount_out_raw"] == str(expected_out)
        assert arrival["note"] == "bridged from Base (Relay)"
        assert float(order["receivedOut"]) == pytest.approx(expected_out / 10**6)
        assert service.ledger.get_balance(4663, wallet.lower(), USDG) == expected_out

    async def test_a_sufficient_allowance_skips_the_approval(
        self,
        fast_fill: TradingService,
        base_chain: FakeChain,
        robinhood_tokens: FakeChain,
        fake_bridge: FakeDexBridge,
    ) -> None:
        service = fast_fill
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_bridge_effects(base_chain, wallet)
        base_chain.set_allowance(USDC, wallet, AGENTOS_BRIDGE, 100 * 10**6)
        _land_fill(robinhood_tokens, wallet, token=USDG, amount=bridge_expected_out(10 * 10**6))
        fake_bridge.default_statuses = [{"status": "filled", "destinationTxHash": DEST_TX}]
        order = await _bridge(service, token="USDC", amount_in="10")
        assert order["status"] == "confirmed"
        assert len(base_chain.sent) == 1 and order["approvalTxHash"] is None

    async def test_approved_agent_bridge_counts_its_spend_once(
        self,
        fast_fill: TradingService,
        base_chain: FakeChain,
        robinhood_tokens: FakeChain,
        fake_bridge: FakeDexBridge,
    ) -> None:
        service = fast_fill
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_bridge_effects(base_chain, wallet)
        _land_fill(robinhood_tokens, wallet, token=NATIVE_ADDRESS, amount=ETH_OUT)
        fake_bridge.default_statuses = [{"status": "filled", "destinationTxHash": DEST_TX}]
        parked = await _bridge(service, initiator="agent", session_key="agent:main:webchat:desk")
        assert parked["status"] == "awaiting_approval"
        approved = await service.approve(parked["orderId"], wait=True)
        assert approved["status"] == "confirmed", approved
        assert approved["initiator"] == "agent"
        assert service.limits(wallet)["spentTodayUsd"] == pytest.approx(20.0)
        assert service.ledger.open_agent_value_usd(wallet.lower()) == 0.0
        entries = service.ledger.list_entries(wallet=wallet.lower())
        booked = [e for e in entries if e["order_id"] == approved["orderId"]]
        assert {e["kind"] for e in booked} == {"withdraw", "deposit"}
        assert {e["session_key"] for e in booked} == {"agent:main:webchat:desk"}

    async def test_a_quote_that_moved_below_the_floor_asks_again(
        self,
        fast_fill: TradingService,
        base_chain: FakeChain,
        robinhood_tokens: FakeChain,
        fake_bridge: FakeDexBridge,
    ) -> None:
        service = fast_fill
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_bridge_effects(base_chain, wallet)
        parked = await _bridge(service, initiator="agent")
        first_expected = parked["expectedOut"]
        fake_bridge.out_scale = 0.95  # 5 % worse than approved, tolerance is 1 %
        again = await service.approve(parked["orderId"], wait=True)
        assert again["status"] == "awaiting_approval"
        assert "moved below the approved floor" in again["reason"]
        assert float(again["expectedOut"]) < float(first_expected)
        assert base_chain.sent == []
        # Approving the re-quoted order at the new price goes through.
        fake_bridge.default_statuses = [{"status": "filled", "destinationTxHash": DEST_TX}]
        _land_fill(
            robinhood_tokens,
            wallet,
            token=NATIVE_ADDRESS,
            amount=bridge_expected_out(10**16, scale=0.95),
        )
        done = await service.approve(parked["orderId"], wait=True)
        assert done["status"] == "confirmed" and len(base_chain.sent) == 1

    async def test_a_manual_bridge_whose_quote_moved_fails_price_moved(
        self,
        fast_fill: TradingService,
        base_chain: FakeChain,
        robinhood_tokens: FakeChain,
        fake_bridge: FakeDexBridge,
    ) -> None:
        service = fast_fill
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_bridge_effects(base_chain, wallet)
        calls = 0

        def worse_on_second(quote: dict[str, Any]) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            if calls >= 2:
                quote["buyAmount"] = str(int(int(quote["buyAmount"]) * 0.9))
                quote["minBuyAmount"] = str(int(int(quote["minBuyAmount"]) * 0.9))
            return quote

        fake_bridge.mutate_quote = worse_on_second
        order = await _bridge(service)
        assert order["status"] == "failed" and order["reason"].startswith("trading.price_moved")
        assert base_chain.sent == []

    @pytest.mark.parametrize(
        ("mutate", "code"),
        [
            (
                lambda q: q["transaction"].__setitem__(
                    "to", "0x2222222222222222222222222222222222222222"
                ),
                "trading.tx_failed",
            ),
            (
                lambda q: q["transaction"].__setitem__("data", "0x49290c1c" + "0" * 128),
                "trading.bridge.unsupported_step",
            ),
            (
                # The call would pay somebody else on the destination chain.
                lambda q: q["transaction"].__setitem__(
                    "data",
                    q["transaction"]["data"][: 10 + 6 * 64]
                    + "22".rjust(64, "0")
                    + q["transaction"]["data"][10 + 7 * 64 :],
                ),
                "trading.tx_failed",
            ),
            (lambda q: q["fee"].__setitem__("bps", 500), "trading.bridge.provider"),
        ],
    )
    async def test_a_quote_off_pattern_is_refused_unsigned(
        self,
        fast_fill: TradingService,
        base_chain: FakeChain,
        robinhood_tokens: FakeChain,
        fake_bridge: FakeDexBridge,
        mutate: Any,
        code: str,
    ) -> None:
        service = fast_fill
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_bridge_effects(base_chain, wallet)

        def tamper(quote: dict[str, Any]) -> dict[str, Any]:
            mutate(quote)
            return quote

        fake_bridge.mutate_quote = tamper
        with pytest.raises(TradingError) as info:
            await _bridge(service)
        assert info.value.code == code
        assert base_chain.sent == [] and service.list_orders()["orders"] == []

    async def test_a_bridge_that_commits_to_less_than_agreed_asks_again(
        self,
        fast_fill: TradingService,
        base_chain: FakeChain,
        robinhood_tokens: FakeChain,
        fake_bridge: FakeDexBridge,
    ) -> None:
        service = fast_fill
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_bridge_effects(base_chain, wallet)
        parked = await _bridge(service, initiator="agent")

        def looser(quote: dict[str, Any]) -> dict[str, Any]:
            # Same expected amount, but the bridge now stands behind 3% less of it.
            quote["minBuyAmount"] = str(int(int(quote["minBuyAmount"]) * 0.97))
            return quote

        fake_bridge.mutate_quote = looser
        again = await service.approve(parked["orderId"], wait=True)
        assert again["status"] == "awaiting_approval"
        assert "moved below the approved floor" in again["reason"]
        assert again["expectedOut"] == parked["expectedOut"]
        assert float(again["minOut"]) < float(parked["minOut"])
        assert base_chain.sent == []

    async def test_the_requote_is_pinned_to_the_bridge_that_was_approved(
        self,
        fast_fill: TradingService,
        base_chain: FakeChain,
        robinhood_tokens: FakeChain,
        fake_bridge: FakeDexBridge,
    ) -> None:
        service = fast_fill
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_bridge_effects(base_chain, wallet)
        fake_bridge.source = "across"
        fake_bridge.refund_guaranteed = False
        parked = await _bridge(service, token="USDC", amount_in="25", initiator="agent")
        assert parked["provider"] == "across" and parked["providerLabel"] == "Across"
        assert parked["bridge"]["refundGuaranteed"] is False
        # By the time the user approves, the gateway would pick the other bridge.
        fake_bridge.source = "relay"
        _land_fill(robinhood_tokens, wallet, token=USDG, amount=USD_OUT)
        fake_bridge.default_statuses = [{"status": "filled", "destinationTxHash": DEST_TX}]
        done = await service.approve(parked["orderId"], wait=True)
        assert done["status"] == "confirmed", done
        quotes = [c["params"] for c in fake_bridge.calls if c["path"] == "/v1/bridge/quote"]
        assert quotes[-1]["source"] == "across"
        assert done["provider"] == "across" and done["bridge"]["sourceLabel"] == "Across"
        # The signed call deposits into Across, and the fill is asked of Across.
        call = decode_fake_raw(base_chain.sent[-1])
        assert call["data"][10:74] == b"across".hex().ljust(64, "0")
        polls = [c["params"] for c in fake_bridge.calls if c["path"] == "/v1/bridge/status"]
        assert polls and {p["source"] for p in polls} == {"across"}
        withdraw = service.ledger.list_entries(wallet=wallet.lower(), kind="withdraw")[0]
        assert withdraw["note"] == "bridged to Robinhood Chain (Across)"

    async def test_a_pinned_bridge_without_a_route_fails_rather_than_switching(
        self,
        fast_fill: TradingService,
        base_chain: FakeChain,
        robinhood_tokens: FakeChain,
        fake_bridge: FakeDexBridge,
    ) -> None:
        from tests.test_trading.fakes import bridge_fixture

        service = fast_fill
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_bridge_effects(base_chain, wallet)
        parked = await _bridge(service, initiator="agent")
        fake_bridge.quote_error = (400, bridge_fixture("error_no_route"))
        order = await service.approve(parked["orderId"], wait=True)
        assert order["status"] == "failed"
        assert order["reason"].startswith("trading.bridge.no_route")
        assert base_chain.sent == []

    async def test_a_second_attempt_is_held_to_the_approved_floor_not_the_first_quotes(
        self,
        fast_fill: TradingService,
        base_chain: FakeChain,
        robinhood_tokens: FakeChain,
        fake_bridge: FakeDexBridge,
    ) -> None:
        """An approval that failed after re-quoting must not tighten the floor for the retry."""
        service = fast_fill
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_bridge_effects(base_chain, wallet, status=0)
        parked = await _bridge(service, token="USDC", amount_in="25", initiator="agent")
        # The approval transaction reverts: the order fails with the row already
        # holding the executed quote's own commitment as its floor.
        failed = await service.approve(parked["orderId"], wait=True)
        assert failed["status"] == "failed", failed
        row = service.ledger.get_order(parked["orderId"])
        assert row is not None and row["min_out_raw"] == str(int(USD_OUT * BRIDGE_COMMIT))
        # The same order put back and approved again, the price a hair lower
        # (inside the 1% tolerance): it must go through.
        service.ledger.update_order(parked["orderId"], status="awaiting_approval", reason=None)
        _wire_bridge_effects(base_chain, wallet)
        fake_bridge.out_scale = 0.999
        _land_fill(
            robinhood_tokens,
            wallet,
            token=USDG,
            amount=bridge_expected_out(25 * 10**6, scale=0.999),
        )
        fake_bridge.default_statuses = [{"status": "filled", "destinationTxHash": DEST_TX}]
        done = await service.approve(parked["orderId"], wait=True)
        assert done["status"] == "confirmed", done

    async def test_a_coin_fill_paid_from_inside_a_contract_call_books_the_quote(
        self,
        fast_fill: TradingService,
        base_chain: FakeChain,
        robinhood_tokens: FakeChain,
        fake_bridge: FakeDexBridge,
    ) -> None:
        """Across hands ETH over inside its own call: the fill tx carries no value."""
        service = fast_fill
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_bridge_effects(base_chain, wallet)
        fake_bridge.source = "across"
        _land_fill(robinhood_tokens, wallet, token=NATIVE_ADDRESS, amount=ETH_OUT)
        robinhood_tokens.transactions[DEST_TX.lower()].update(
            {"to": "0x" + "5e" * 20, "value": "0x0", "input": "0xdeadbeef"}
        )
        fake_bridge.default_statuses = [{"status": "filled", "destinationTxHash": DEST_TX}]
        order = await _bridge(service)
        assert order["status"] == "confirmed", order
        arrival = [
            d
            for d in service.ledger.list_entries(wallet=wallet.lower(), kind="deposit")
            if d["order_id"] == order["orderId"]
        ][0]
        # Not zero: the quoted amount stands in for a value the tx does not carry.
        assert arrival["amount_out_raw"] == str(ETH_OUT)
        assert float(order["receivedOut"]) == pytest.approx(ETH_OUT / 10**18)

    async def test_a_failure_is_only_final_once_it_has_lasted(
        self,
        fast_fill: TradingService,
        base_chain: FakeChain,
        robinhood_tokens: FakeChain,
        fake_bridge: FakeDexBridge,
    ) -> None:
        service = fast_fill
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_bridge_effects(base_chain, wallet)
        fake_bridge.default_statuses = [{"status": "failed"}]
        order = await _bridge(service, wait=False)
        for _ in range(100):
            await asyncio.sleep(0)
            row = service.ledger.get_order(order["orderId"])
            assert row is not None
            if json.loads(row["quote_json"])["fill"].get("failedSince"):
                break
        # The bridge says "failed", which on Relay may still be retried: not yet ours to report.
        live = service.get_order(order["orderId"])
        assert live["status"] == "submitted" and live["bridge"]["fillStatus"] == "failure"
        # It recovers: the fill lands after all.
        _land_fill(robinhood_tokens, wallet, token=NATIVE_ADDRESS, amount=ETH_OUT)
        fake_bridge.default_statuses = [{"status": "filled", "destinationTxHash": DEST_TX}]
        real_now = service._now
        service._now = lambda: real_now() + 5.0
        status = await service.bridge_status(order_id=order["orderId"])
        assert status["order"]["status"] == "confirmed"
        plan = json.loads(service.ledger.get_order(order["orderId"])["quote_json"])  # type: ignore[index]
        assert "failedSince" not in plan["fill"]
        await asyncio.gather(*list(service._confirm_tasks), return_exceptions=True)

    async def test_a_failure_that_lasts_fails_the_order(
        self,
        fast_fill: TradingService,
        base_chain: FakeChain,
        robinhood_tokens: FakeChain,
        fake_bridge: FakeDexBridge,
    ) -> None:
        service = fast_fill
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_bridge_effects(base_chain, wallet)
        service._bridge_failure_grace_s = 0.0
        fake_bridge.default_statuses = [{"status": "pending"}, {"status": "failed"}]
        order = await _bridge(service)
        assert order["status"] == "failed"
        assert order["reason"].startswith("Relay failure: the bridge did not deliver (requestId 0x")
        assert order["bridge"]["fillStatus"] == "failure"
        assert len(service.ledger.list_entries(wallet=wallet.lower(), kind="withdraw")) == 1

    async def test_an_order_parked_before_the_move_runs_through_the_gateway(
        self,
        fast_fill: TradingService,
        base_chain: FakeChain,
        robinhood_tokens: FakeChain,
        fake_bridge: FakeDexBridge,
    ) -> None:
        """A plan from when the engine called Relay itself: no ``via``, the solver as recipient."""
        service = fast_fill
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_bridge_effects(base_chain, wallet)
        parked = await _bridge(service, initiator="agent")
        row = service.ledger.get_order(parked["orderId"])
        assert row is not None
        plan = json.loads(row["quote_json"])
        legacy_floor = str(int(ETH_OUT * 0.99))
        plan.pop("via")
        plan.pop("source")
        plan["provider"] = "relay"
        plan["quote"] = {"requestId": "0x" + "1e" * 32, "solver": SOLVER, "feesUsd": 0.03}
        plan["approved"] = {
            "requestId": "0x" + "1e" * 32,
            "expectedOutRaw": str(ETH_OUT),
            "minOutRaw": legacy_floor,
        }
        service.ledger.update_order(
            parked["orderId"],
            recipient=SOLVER,
            min_out_raw=legacy_floor,
            quote_json=json.dumps(plan),
        )
        old = service.get_order(parked["orderId"])
        assert old["recipientLabel"] == "Relay solver" and old["provider"] == "relay"
        assert old["bridge"]["requestId"] == "0x" + "1e" * 32

        _land_fill(robinhood_tokens, wallet, token=NATIVE_ADDRESS, amount=ETH_OUT)
        fake_bridge.default_statuses = [{"status": "filled", "destinationTxHash": DEST_TX}]
        done = await service.approve(parked["orderId"], wait=True)
        assert done["status"] == "confirmed", done
        # It is not pinned (it never chose a bridge), nor held to a floor it never agreed.
        quotes = [c["params"] for c in fake_bridge.calls if c["path"] == "/v1/bridge/quote"]
        assert "source" not in quotes[-1]
        assert done["recipient"] == checksum_address(AGENTOS_BRIDGE)
        assert done["recipientLabel"] == "AgentOS Bridge"
        assert decode_fake_raw(base_chain.sent[-1])["to"].lower() == AGENTOS_BRIDGE

    async def test_a_reverted_deposit_fails_with_its_gas(
        self, fast_fill: TradingService, base_chain: FakeChain, robinhood_tokens: FakeChain
    ) -> None:
        service = fast_fill
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_bridge_effects(base_chain, wallet, status=0)
        order = await _bridge(service)
        assert order["status"] == "failed" and "reverted on-chain" in order["reason"]
        gas = service.ledger.list_entries(wallet=wallet.lower(), kind="gas")
        assert len(gas) == 1 and gas[0]["note"] == "reverted bridge deposit"
        assert service.ledger.list_entries(wallet=wallet.lower(), kind="withdraw") == []

    async def test_a_refund_fails_the_order_without_an_arrival(
        self,
        fast_fill: TradingService,
        base_chain: FakeChain,
        robinhood_tokens: FakeChain,
        fake_bridge: FakeDexBridge,
    ) -> None:
        service = fast_fill
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_bridge_effects(base_chain, wallet)
        fake_bridge.default_statuses = [{"status": "pending"}, {"status": "refunded"}]
        parked = await _bridge(service, initiator="agent")
        order = await service.approve(parked["orderId"], wait=True)
        assert order["status"] == "failed"
        assert order["reason"].startswith("Relay refund: the funds were returned")
        assert order["bridge"]["fillStatus"] == "refund"
        # The deposit left: it is booked. Nothing arrived, nothing spent for the cap.
        assert len(service.ledger.list_entries(wallet=wallet.lower(), kind="withdraw")) == 1
        deposits = service.ledger.list_entries(wallet=wallet.lower(), kind="deposit")
        assert [d for d in deposits if d["order_id"] == order["orderId"]] == []
        assert service.limits(wallet)["spentTodayUsd"] == 0.0

    async def test_the_order_stays_submitted_until_the_fill_lands(
        self,
        fast_fill: TradingService,
        base_chain: FakeChain,
        robinhood_tokens: FakeChain,
        fake_bridge: FakeDexBridge,
    ) -> None:
        service = fast_fill
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_bridge_effects(base_chain, wallet)
        fake_bridge.default_statuses = [{"status": "pending", "originTxHash": "0xaa"}]
        order = await _bridge(service, wait=False)
        assert order["status"] == "submitted"
        for _ in range(100):
            await asyncio.sleep(0)
            row = service.ledger.get_order(order["orderId"])
            assert row is not None
            plan = json.loads(row["quote_json"])
            if plan["execution"].get("depositBookedAt") and plan["fill"].get("attempts"):
                break
        live = service.get_order(order["orderId"])
        assert live["status"] == "submitted"
        assert live["reason"] == "waiting for the Relay fill on Robinhood Chain"
        assert live["bridge"]["fillStatus"] == "pending"
        assert len(service.ledger.list_entries(wallet=wallet.lower(), kind="withdraw")) == 1
        # The destination chain is "busy" for the sync while the fill is pending.
        record = service.vault.get(wallet)
        assert service.syncer._has_open_orders(record, ROBINHOOD) is True
        assert bridge_destination_chain_id(service.ledger.get_order(order["orderId"]) or {}) == 4663
        # Now the fill lands; a status read settles it.
        _land_fill(robinhood_tokens, wallet, token=NATIVE_ADDRESS, amount=ETH_OUT)
        fake_bridge.default_statuses = [{"status": "filled", "destinationTxHash": DEST_TX}]
        # A status read right after the tracker's poll is rate-limited: move the clock.
        real_now = service._now
        service._now = lambda: real_now() + 5.0
        status = await service.bridge_status(request_id=fake_bridge.request_ids[-1])
        assert status["order"]["orderId"] == order["orderId"]
        assert status["order"]["status"] == "confirmed"
        assert status["bridge"]["fillStatus"] == "success"
        assert service.syncer._has_open_orders(record, ROBINHOOD) is False
        await asyncio.gather(*list(service._confirm_tasks), return_exceptions=True)

    async def test_recovery_resumes_the_fill_after_a_restart(
        self,
        fast_fill: TradingService,
        base_chain: FakeChain,
        robinhood_tokens: FakeChain,
        fake_bridge: FakeDexBridge,
    ) -> None:
        service = fast_fill
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_bridge_effects(base_chain, wallet)
        fake_bridge.default_statuses = [{"status": "pending"}]
        order = await _bridge(service, wait=False)
        for _ in range(50):
            await asyncio.sleep(0)
            row = service.ledger.get_order(order["orderId"])
            assert row is not None
            if json.loads(row["quote_json"])["execution"].get("depositBookedAt"):
                break
        # The tracker is dropped, as a restart would drop it.
        for task in list(service._confirm_tasks):
            task.cancel()
        await asyncio.gather(*list(service._confirm_tasks), return_exceptions=True)
        service._confirm_tasks.clear()
        assert service.get_order(order["orderId"])["status"] == "submitted"
        _land_fill(robinhood_tokens, wallet, token=NATIVE_ADDRESS, amount=ETH_OUT)
        fake_bridge.default_statuses = [{"status": "filled", "destinationTxHash": DEST_TX}]
        await service.recover_submitted()
        await asyncio.gather(*list(service._confirm_tasks), return_exceptions=True)
        recovered = service.get_order(order["orderId"])
        assert recovered["status"] == "confirmed"
        # The origin leg was not booked a second time.
        assert len(service.ledger.list_entries(wallet=wallet.lower(), kind="withdraw")) == 1
        arrivals = [
            d
            for d in service.ledger.list_entries(wallet=wallet.lower(), kind="deposit")
            if d["order_id"] == order["orderId"]
        ]
        assert len(arrivals) == 1

    async def test_an_arrival_the_sync_already_booked_is_not_booked_twice(
        self,
        fast_fill: TradingService,
        base_chain: FakeChain,
        robinhood_tokens: FakeChain,
        fake_bridge: FakeDexBridge,
    ) -> None:
        service = fast_fill
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_bridge_effects(base_chain, wallet)
        amount = USD_OUT
        _land_fill(robinhood_tokens, wallet, token=USDG, amount=amount)
        record = service.vault.get(wallet)
        # The sweep saw the fill first and booked it as an external deposit.
        await service.syncer._book_deposit(
            record, ROBINHOOD, USDG, amount, 1_700_000_000.0, tx_hash=DEST_TX, log_index=5
        )
        fake_bridge.default_statuses = [{"status": "filled", "destinationTxHash": DEST_TX}]
        order = await _bridge(service, token="USDC", amount_in="25")
        assert order["status"] == "confirmed"
        deposits = [
            d
            for d in service.ledger.list_entries(wallet=wallet.lower(), kind="deposit")
            if d["tx_hash"] == DEST_TX
        ]
        assert len(deposits) == 1

    async def test_status_by_order_id_and_unknown_ids(
        self, funded_service: TradingService, robinhood_tokens: FakeChain
    ) -> None:
        service = funded_service
        parked = await _bridge(service, initiator="agent")
        status = await service.bridge_status(order_id=parked["orderId"])
        assert status["order"]["status"] == "awaiting_approval"
        assert status["bridge"]["fillStatus"] is None
        with pytest.raises(TradingError) as info:
            await service.bridge_status(request_id="0xnothing")
        assert info.value.code == "trading.bridge.not_found"
        with pytest.raises(TradingError, match="orderId or requestId"):
            await service.bridge_status()

    async def test_the_gateway_down_at_creation_is_a_provider_error(
        self,
        funded_service: TradingService,
        robinhood_tokens: FakeChain,
        fake_bridge: FakeDexBridge,
    ) -> None:
        fake_bridge.quote_error = (
            502,
            {"error": {"code": "UPSTREAM_ERROR", "message": "upstream"}},
        )
        with pytest.raises(TradingError) as info:
            await _bridge(funded_service)
        assert info.value.code == "trading.bridge.provider"


class TestValuedByTheEngineFeed:
    """WAL-06 variant: nothing the bridge says sizes a bridge for the guardrails."""

    @staticmethod
    def _cheap_relay(fake_bridge: FakeDexBridge) -> None:
        def mutate(quote: dict[str, Any]) -> dict[str, Any]:
            # A dollar figure the gateway does not send today; were it to,
            # the engine must still price the order itself.
            quote["sellAmountUsd"] = "0.01"
            return quote

        fake_bridge.mutate_quote = mutate

    async def test_unpriced_origin_is_valued_from_the_destination_feed(
        self,
        funded_service: TradingService,
        robinhood_tokens: FakeChain,
        fake_bridge: FakeDexBridge,
        fake_prices: Any,
    ) -> None:
        service = funded_service
        service.config.daily_cap_usd = 20.0
        self._cheap_relay(fake_bridge)
        fake_prices.spot.pop(("base", USDC))
        fake_prices.spot[("robinhood", USDG)] = 1.0
        order = await _bridge(service, token="USDC", amount_in="25", initiator="agent")
        # $25 by the destination chain's price of the same asset: over the $20 cap.
        assert order["status"] == "rejected" and order["reason"].startswith("daily cap")
        assert order["valueUsd"] == pytest.approx(25.0)

    async def test_unpriced_on_both_chains_is_unknown_not_the_bridges_number(
        self,
        funded_service: TradingService,
        robinhood_tokens: FakeChain,
        fake_bridge: FakeDexBridge,
        fake_prices: Any,
    ) -> None:
        service = funded_service
        self._cheap_relay(fake_bridge)
        fake_prices.spot.pop(("base", USDC))
        order = await _bridge(service, token="USDC", amount_in="25", initiator="agent")
        assert order["status"] == "awaiting_approval"
        assert order["valueUsd"] is None
        assert order["reason"] == "value unknown (no price); a bridge always waits"
