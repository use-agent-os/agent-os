"""Sends, multisends, allowance review/revoke, the decoder and the network probe.

Same faked stack as ``test_service.py``: the chain moves balances when a
signed transaction lands, so each flow is checked from order to ledger.
"""

from __future__ import annotations

import asyncio

import pytest

from agentos.trading import guardrails
from agentos.trading.chains import BASE, NATIVE_ADDRESS, checksum_address
from agentos.trading.decode import decode_calldata, describe_call, tx_summary
from agentos.trading.evm import (
    SEL_APPROVE,
    SEL_TRANSFER,
    UINT256_MAX,
    encode_approve,
    encode_transfer,
    is_unlimited,
)
from agentos.trading.service import TradingError, TradingService
from tests.test_trading.fakes import (
    OTHER,
    ROUTER,
    USDC,
    WETH,
    FakeChain,
    approval_log,
    decode_fake_raw,
    transfer_log,
)

THIRD = "0x3333333333333333333333333333333333333333"
PERMIT2 = "0x000000000022d473030f116ddee9f6b43ac78ba3"


def _events(service: TradingService, name: str) -> list[dict]:
    return [payload for event, payload in service.events if event == name]  # type: ignore[attr-defined]


def _wire_send_effects(chain: FakeChain, wallet: str, *, status: int = 1) -> None:
    """Make a signed send or revoke land the way a node would: balances move, a receipt appears."""

    def on_send(raw: str) -> str:
        tx = decode_fake_raw(raw)
        chain._seq += 1
        tx_hash = "0x" + format(0xBEEF00 + chain._seq, "x").rjust(64, "0")
        gas_wei = 21_000 * 10**8
        sender = wallet.lower()
        chain.nonces[sender] = tx.get("nonce", 0) + 1
        chain.native[sender] -= gas_wei
        chain.record_transaction(tx_hash, {**tx, "from": wallet})
        logs = []
        data = str(tx.get("data") or "0x")
        if status != 1:
            chain.receipt(tx_hash, status=0, gas_used=21_000, gas_price=10**8)
            return tx_hash
        if data == "0x":
            value = int(tx["value"])
            chain.native[sender] -= value
            to = str(tx["to"]).lower()
            chain.native[to] = chain.native.get(to, 0) + value
        elif data.startswith(SEL_TRANSFER):
            moved = chain.apply_transfer(sender, tx)
            assert moved is not None
            recipient, amount = moved
            logs = [
                transfer_log(
                    tx_hash=tx_hash,
                    log_index=7,
                    block=chain.block,
                    token=str(tx["to"]).lower(),
                    sender=sender,
                    recipient=recipient,
                    amount=amount,
                )
            ]
        elif data.startswith(SEL_APPROVE):
            chain.apply_approve(sender, tx)
        chain.receipt(tx_hash, status=1, gas_used=21_000, gas_price=10**8, logs=logs)
        return tx_hash

    chain.on_send = on_send


async def _send(service: TradingService, **kw: object) -> list[dict]:
    params: dict = {
        "chain": BASE,
        "wallet": None,
        "token": "USDC",
        "recipients": [{"to": OTHER, "amount": "10"}],
        "initiator": "manual",
        "session_key": None,
        "note": None,
        "wait": True,
    }
    params.update(kw)
    return await service.send(**params)


class TestGuardrailsForTransfers:
    def test_manual_transfers_are_the_users_call(self) -> None:
        verdict = guardrails.evaluate_transfer(
            initiator="manual", value_usd=1e9, daily_cap_usd=10.0, spent_today_usd=0.0
        )
        assert verdict.decision == "allow"

    def test_agent_transfers_always_wait(self) -> None:
        verdict = guardrails.evaluate_transfer(
            initiator="agent", value_usd=1.0, daily_cap_usd=1000.0, spent_today_usd=0.0
        )
        assert verdict.decision == "needs_approval" and "for good" in verdict.reason
        unpriced = guardrails.evaluate_transfer(
            initiator="agent", value_usd=None, daily_cap_usd=1000.0, spent_today_usd=0.0
        )
        assert unpriced.decision == "needs_approval" and "unknown" in unpriced.reason

    def test_cap_still_bites(self) -> None:
        over = guardrails.evaluate_transfer(
            initiator="agent", value_usd=600.0, daily_cap_usd=1000.0, spent_today_usd=500.0
        )
        assert over.decision == "blocked_daily_cap"
        off = guardrails.evaluate_transfer(
            initiator="agent", value_usd=1.0, daily_cap_usd=0.0, spent_today_usd=0.0
        )
        assert off.decision == "blocked_daily_cap" and "switched off" in off.reason

    def test_revoke(self) -> None:
        assert guardrails.evaluate_revoke(initiator="manual").decision == "allow"
        assert guardrails.evaluate_revoke(initiator="agent").decision == "needs_approval"


class TestDecoder:
    def test_transfer_and_approve_calldata(self) -> None:
        call = decode_calldata(encode_transfer(OTHER, 5 * 10**6))
        assert call.known and call.function == "transfer"
        assert call.args[0]["value"] == OTHER and call.args[1]["value"] == str(5 * 10**6)
        assert "transfer 5000000 units" in describe_call(call, to=USDC, value_wei=0)
        unlimited = decode_calldata(encode_approve(PERMIT2, UINT256_MAX))
        assert unlimited.args[1]["unlimited"] is True
        assert "unlimited" in describe_call(unlimited, to=USDC, value_wei=0)
        assert is_unlimited(2**255) and not is_unlimited(10**24)

    def test_unknown_and_plain(self) -> None:
        plain = decode_calldata("0x")
        assert plain.known and plain.function is None
        assert describe_call(plain, to=OTHER, value_wei=5).startswith("send 5 wei")
        mystery = decode_calldata("0xdeadbeef" + "00" * 64)
        assert not mystery.known and mystery.function is None and mystery.words == 2
        assert "0xdeadbeef" in describe_call(mystery, to=ROUTER, value_wei=0)
        router = decode_calldata("0x3593564c" + "11" * 32)
        assert router.function == "execute" and not router.known
        assert decode_calldata("0xzz").known is False
        truncated = decode_calldata(SEL_TRANSFER + "00" * 10)
        assert truncated.function == "transfer" and not truncated.known

    def test_tx_summary(self) -> None:
        pending = tx_summary({"hash": "0xAB", "from": OTHER, "value": "0x5"}, None)
        assert pending["status"] == "pending" and pending["valueWei"] == "5"
        mined = tx_summary(
            {"hash": "0xab", "from": OTHER, "to": USDC, "value": "0x0"},
            {
                "status": "0x1",
                "blockNumber": "0x10",
                "gasUsed": "0x5208",
                "effectiveGasPrice": "0x2",
            },
        )
        assert mined["status"] == "success" and mined["gasWei"] == str(21_000 * 2)
        assert mined["l1FeeWei"] is None
        # An OP-stack receipt: what the sender paid includes the L1 data fee.
        op = tx_summary(
            {"hash": "0xab"},
            {
                "status": "0x1",
                "blockNumber": "0x10",
                "gasUsed": "0x5208",
                "effectiveGasPrice": "0x2",
                "l1Fee": "0x64",
            },
        )
        assert op["gasWei"] == str(21_000 * 2 + 100) and op["l1FeeWei"] == "100"
        reverted = tx_summary({}, {"status": "0x0", "blockNumber": "0x10"})
        assert reverted["status"] == "reverted"


class TestSend:
    async def test_manual_erc20_send_books_a_withdrawal(
        self, funded_service: TradingService, base_chain: FakeChain
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_send_effects(base_chain, wallet)
        orders = await _send(service, note="rent")
        assert len(orders) == 1
        order = orders[0]
        assert order["kind"] == "send" and order["status"] == "confirmed", order
        assert order["recipient"] == checksum_address(OTHER) and order["batchId"] is None
        assert order["amountIn"] == "10" and order["tokenIn"]["symbol"] == "USDC"
        assert order["valueUsd"] == pytest.approx(10.0) and order["txHash"]
        tx = decode_fake_raw(base_chain.sent[0])
        assert tx["to"].lower() == USDC and tx["data"] == encode_transfer(OTHER, 10 * 10**6)
        assert tx["value"] == 0 and tx["type"] == 2
        assert base_chain.get_erc20(USDC, OTHER) == 10 * 10**6
        # Booked as a withdrawal under the order, with the Transfer's own log index.
        entries = service.ledger.list_entries(wallet=wallet.lower(), kind="withdraw")
        assert len(entries) == 1
        entry = entries[0]
        assert entry["order_id"] == order["orderId"] and entry["initiator"] == "manual"
        assert entry["log_index"] == 7 and entry["note"] == "rent"
        assert entry["amount_in_raw"] == str(10 * 10**6) and entry["gas_usd"] is not None
        # The cache moved with it; a later sync will not book the transfer again.
        assert service.ledger.get_balance(8453, wallet.lower(), USDC) == 990 * 10**6
        # Manual sends never count against the agent's cap.
        assert service.limits(wallet)["spentTodayUsd"] == 0.0
        assert _events(service, "trading.order.finished")[-1]["order"]["status"] == "confirmed"

    async def test_manual_native_send(
        self, funded_service: TradingService, base_chain: FakeChain
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_send_effects(base_chain, wallet)
        orders = await _send(
            service, token="ETH", recipients=[{"to": OTHER, "amountUsd": 20}], note=None
        )
        order = orders[0]
        assert order["status"] == "confirmed" and order["tokenIn"]["native"] is True
        # $20 at the fake's $2,000: 0.01 ETH, by value, with empty calldata.
        assert order["amountIn"] == "0.01"
        tx = decode_fake_raw(base_chain.sent[0])
        assert tx["to"].lower() == OTHER and tx["data"] == "0x" and tx["value"] == 10**16
        assert base_chain.native[OTHER] == 10**16
        entry = service.ledger.list_entries(wallet=wallet.lower(), kind="withdraw")[0]
        assert entry["token_in"] == NATIVE_ADDRESS and entry["note"].startswith("sent to 0x")
        cached = service.ledger.get_balance(8453, wallet.lower(), NATIVE_ADDRESS)
        assert cached == base_chain.native[wallet.lower()]

    async def test_validation(self, funded_service: TradingService) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        with pytest.raises(TradingError, match="at least one recipient"):
            await _send(service, recipients=[])
        with pytest.raises(TradingError, match="zero address"):
            await _send(service, recipients=[{"to": NATIVE_ADDRESS, "amount": "1"}])
        with pytest.raises(TradingError, match="sending wallet itself"):
            await _send(service, recipients=[{"to": wallet, "amount": "1"}])
        with pytest.raises(TradingError, match="appears twice"):
            await _send(
                service,
                recipients=[
                    {"to": OTHER, "amount": "1"},
                    {"to": checksum_address(OTHER), "amount": "2"},
                ],
            )
        with pytest.raises(TradingError, match="exactly one of amount"):
            await _send(service, recipients=[{"to": OTHER, "amount": "1", "amountUsd": 1}])
        with pytest.raises(TradingError, match="Not an EVM address"):
            await _send(service, recipients=[{"to": "vitalik.eth", "amount": "1"}])
        with pytest.raises(TradingError, match="greater than zero"):
            await _send(service, recipients=[{"to": OTHER, "amount": "0"}])
        with pytest.raises(TradingError, match="at most 200"):
            await _send(
                service,
                recipients=[{"to": f"0x{n:040x}", "amount": "1"} for n in range(1, 202)],
            )
        with pytest.raises(TradingError, match="the send needs"):
            await _send(service, recipients=[{"to": OTHER, "amount": "5000"}])
        # Nothing was written for a refused send.
        assert service.list_orders()["orders"] == []

    async def test_agent_multisend_is_one_decision(
        self, funded_service: TradingService, base_chain: FakeChain
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_send_effects(base_chain, wallet)
        orders = await _send(
            service,
            recipients=[
                {"to": OTHER, "amount": "10"},
                {"to": THIRD, "amount": "20"},
                {"to": ROUTER, "amountUsd": 30},
            ],
            initiator="agent",
            session_key="agent:main:webchat:desk",
            wait=False,
        )
        assert [o["status"] for o in orders] == ["awaiting_approval"] * 3
        batch_id = orders[0]["batchId"]
        assert batch_id and all(o["batchId"] == batch_id for o in orders)
        assert all("for good" in o["reason"] for o in orders)
        # Sixty dollars in flight, under the threshold — and still parked.
        assert service.limits(wallet)["spentTodayUsd"] == pytest.approx(60.0)
        asked = _events(service, "trading.approval.requested")[-1]
        assert asked["batchId"] == batch_id and len(asked["orders"]) == 3
        assert service.list_orders()["pendingApprovals"] == 1
        assert [o["orderId"] for o in service.batch(batch_id)] == [o["orderId"] for o in orders]
        assert base_chain.sent == []
        # One approval releases all three legs, so it must name all three (WAL-12).
        with pytest.raises(TradingError) as bare:
            await service.approve(orders[1]["orderId"], wait=True)
        assert bare.value.code == "trading.approval_binding_required"
        assert base_chain.sent == []
        digest = service.batch_approval(batch_id)["digest"]
        assert asked["approval"]["digest"] == digest and len(asked["approval"]["legs"]) == 3
        # Approving the second leg with the batch's digest runs the whole batch, in order.
        waiter = asyncio.create_task(service.wait_order(orders[2]["orderId"], timeout_s=5))
        await asyncio.sleep(0.01)
        approved = await service.approve(orders[1]["orderId"], wait=True, approval_digest=digest)
        assert approved["status"] == "confirmed"
        assert (await waiter)["status"] == "confirmed"
        assert [o["status"] for o in service.batch(batch_id)] == ["confirmed"] * 3
        sent = [decode_fake_raw(raw) for raw in base_chain.sent]
        assert [tx["nonce"] for tx in sent] == [0, 1, 2]
        assert base_chain.get_erc20(USDC, OTHER) == 10 * 10**6
        assert base_chain.get_erc20(USDC, THIRD) == 20 * 10**6
        assert base_chain.get_erc20(USDC, ROUTER) == 30 * 10**6
        assert service.limits(wallet)["spentTodayUsd"] == pytest.approx(60.0)
        entries = service.ledger.list_entries(wallet=wallet.lower(), kind="withdraw")
        assert len(entries) == 3 and {e["initiator"] for e in entries} == {"agent"}
        assert {e["session_key"] for e in entries} == {"agent:main:webchat:desk"}
        with pytest.raises(TradingError, match="is confirmed"):
            await service.approve(orders[0]["orderId"])

    async def test_reject_refuses_the_whole_batch(self, funded_service: TradingService) -> None:
        service = funded_service
        orders = await _send(
            service,
            recipients=[{"to": OTHER, "amount": "1"}, {"to": THIRD, "amount": "2"}],
            initiator="agent",
            wait=False,
        )
        rejected = await service.reject(orders[1]["orderId"], "wrong list")
        assert rejected["status"] == "rejected" and rejected["reason"] == "user: wrong list"
        assert [o["status"] for o in service.batch(orders[0]["batchId"])] == ["rejected"] * 2
        assert len([e for e in _events(service, "trading.order.finished")]) == 2
        assert service.list_orders()["pendingApprovals"] == 0

    async def test_agent_send_over_the_cap_is_refused(self, funded_service: TradingService) -> None:
        service = funded_service
        service.config.daily_cap_usd = 25.0
        orders = await _send(
            service,
            recipients=[{"to": OTHER, "amount": "20"}, {"to": THIRD, "amount": "20"}],
            initiator="agent",
            wait=False,
        )
        assert [o["status"] for o in orders] == ["rejected"] * 2
        assert all(o["reason"].startswith("daily cap") for o in orders)
        service.config.daily_cap_usd = 0.0
        orders = await _send(service, initiator="agent", wait=False)
        assert orders[0]["status"] == "rejected" and "switched off" in orders[0]["reason"]

    async def test_a_leg_that_fails_does_not_stop_the_rest(
        self, funded_service: TradingService, base_chain: FakeChain
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_send_effects(base_chain, wallet)
        base_chain.revert_calls = True
        orders = await _send(
            service,
            recipients=[{"to": OTHER, "amount": "1"}, {"to": THIRD, "amount": "2"}],
        )
        assert [o["status"] for o in orders] == ["failed", "failed"]
        assert all("simulation reverted" in o["reason"] for o in orders)
        base_chain.revert_calls = False
        orders = await _send(service, recipients=[{"to": OTHER, "amount": "1"}])
        assert orders[0]["status"] == "confirmed"

    async def test_send_books_the_sealed_l1_fee_not_the_provisional_one(
        self, funded_service: TradingService, base_chain: FakeChain
    ) -> None:
        # Live on Base the first receipt a node serves is a flashblock
        # preconfirmation whose l1Fee differs from the sealed block's; gas is
        # booked from the receipt read once the head is past its block.
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_send_effects(base_chain, wallet)
        wired = base_chain.on_send
        assert wired is not None
        provisional, final = 2_957_077_405, 5_063_072_155

        def on_send(raw: str) -> str:
            tx_hash = wired(raw)
            receipt = base_chain.receipts[tx_hash.lower()]
            receipt["l1Fee"] = hex(provisional)
            base_chain.sealed[tx_hash.lower()] = {**receipt, "l1Fee": hex(final)}
            base_chain.head_step = 1
            return tx_hash

        base_chain.on_send = on_send
        orders = await _send(service, token="ETH", recipients=[{"to": OTHER, "amount": "0.01"}])
        assert orders[0]["status"] == "confirmed", orders[0]
        row = service.ledger.get_order(orders[0]["orderId"])
        assert row is not None and row["gas_wei"] == str(21_000 * 10**8 + final)
        entry = service.ledger.list_entries(wallet=wallet.lower(), kind="withdraw")[0]
        # $2,000 ETH in the fakes: the gas figure is the sealed one.
        assert entry["gas_usd"] == pytest.approx((21_000 * 10**8 + final) / 1e18 * 2_000)

    async def test_reverted_send_is_failed_with_its_gas(
        self, funded_service: TradingService, base_chain: FakeChain
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_send_effects(base_chain, wallet, status=0)
        orders = await _send(service)
        assert orders[0]["status"] == "failed" and "reverted on-chain" in orders[0]["reason"]
        gas = service.ledger.list_entries(wallet=wallet.lower(), kind="gas")
        assert len(gas) == 1 and gas[0]["note"] == "reverted send"
        assert service.ledger.list_entries(wallet=wallet.lower(), kind="withdraw") == []

    async def test_recovery_settles_a_send_after_restart(
        self, funded_service: TradingService, base_chain: FakeChain
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_send_effects(base_chain, wallet)
        orders = await _send(service, wait=False)
        # The confirm task is dropped, as a restart would drop it.
        for task in list(service._confirm_tasks):
            task.cancel()
        service._confirm_tasks.clear()
        assert service.get_order(orders[0]["orderId"])["status"] == "submitted"
        await service.recover_submitted()
        recovered = service.get_order(orders[0]["orderId"])
        assert recovered["status"] == "confirmed"
        assert len(service.ledger.list_entries(wallet=wallet.lower(), kind="withdraw")) == 1


class TestAllowancesAndRevoke:
    async def test_scan_read_and_revoke(
        self, funded_service: TradingService, base_chain: FakeChain
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_send_effects(base_chain, wallet)
        base_chain.set_allowance(USDC, wallet, PERMIT2, UINT256_MAX)
        base_chain.set_allowance(WETH, wallet, ROUTER, 5 * 10**17)
        base_chain.set_erc20(WETH, wallet, 10**18)
        base_chain.logs.extend(
            [
                approval_log(
                    tx_hash="0x" + "a1" * 32,
                    log_index=1,
                    block=900,
                    token=USDC,
                    owner=wallet,
                    spender=PERMIT2,
                    amount=UINT256_MAX,
                ),
                approval_log(
                    tx_hash="0x" + "a2" * 32,
                    log_index=2,
                    block=950,
                    token=WETH,
                    owner=wallet,
                    spender=ROUTER,
                    amount=5 * 10**17,
                ),
                # Granted long ago and since revoked: reads as zero, never listed.
                approval_log(
                    tx_hash="0x" + "a3" * 32,
                    log_index=3,
                    block=960,
                    token=USDC,
                    owner=wallet,
                    spender=OTHER,
                    amount=1,
                ),
                # Someone else's grant to our wallet is not our exposure.
                approval_log(
                    tx_hash="0x" + "a4" * 32,
                    log_index=4,
                    block=970,
                    token=USDC,
                    owner=OTHER,
                    spender=wallet,
                    amount=1,
                ),
            ]
        )
        # The wallet was created at block 1000 in this fixture; let the scan reach back.
        service.vault.set_created_block(wallet, 8453, 800)
        record = service.vault.get(wallet)
        record.created_block["8453"] = 800
        result = await service.allowances(BASE, None, wait=True)
        rows = result["allowances"]
        assert result["count"] == 2 and result["unlimitedCount"] == 1
        assert result["scannedTo"] == base_chain.block and result["scanning"] is False
        first, second = rows
        assert first["token"]["symbol"] == "USDC" and first["unlimited"] is True
        assert first["spender"] == checksum_address(PERMIT2) and first["spenderLabel"] == "Permit2"
        assert first["allowance"] == "unlimited" and first["balance"] == "1000"
        # Exposure is what the spender could actually take: the whole balance.
        assert first["exposureUsd"] == pytest.approx(1000.0)
        assert second["token"]["symbol"] == "WETH" and second["allowance"] == "0.5"
        assert second["spenderLabel"] == "Uniswap Trading API proxy"
        assert second["exposureUsd"] == pytest.approx(1000.0)
        assert second["explorerUrl"] == "https://basescan.org/tx/0x" + "a2" * 32
        # Second call is incremental: no new logs, same answer.
        again = await service.allowances(BASE, None, wait=True)
        assert [a["spender"] for a in again["allowances"]] == [a["spender"] for a in rows]

        # A manual revoke executes; the row is gone on the next read.
        order = await service.revoke(
            chain=BASE,
            wallet=None,
            token="USDC",
            spender=PERMIT2,
            initiator="manual",
            session_key=None,
            note=None,
            wait=True,
        )
        assert order["kind"] == "revoke" and order["status"] == "confirmed"
        assert order["recipient"] == checksum_address(PERMIT2)
        assert order["recipientLabel"] == "Permit2" and order["amountIn"] == "unlimited"
        tx = decode_fake_raw(base_chain.sent[-1])
        assert tx["to"].lower() == USDC and tx["data"] == encode_approve(PERMIT2, 0)
        assert base_chain.get_allowance(USDC, wallet, PERMIT2) == 0
        after = await service.allowances(BASE, None, wait=True)
        assert [a["token"]["symbol"] for a in after["allowances"]] == ["WETH"]
        approvals = service.ledger.list_entries(wallet=wallet.lower(), kind="approval")
        assert approvals and approvals[-1]["note"] == "revoked Permit2"
        with pytest.raises(TradingError, match="has no allowance"):
            await service.revoke(
                chain=BASE,
                wallet=None,
                token="USDC",
                spender=PERMIT2,
                initiator="manual",
                session_key=None,
                note=None,
            )
        with pytest.raises(TradingError, match="gas coin"):
            await service.revoke(
                chain=BASE,
                wallet=None,
                token="ETH",
                spender=PERMIT2,
                initiator="manual",
                session_key=None,
                note=None,
            )

    async def test_agent_revoke_waits_for_a_human(
        self, funded_service: TradingService, base_chain: FakeChain
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_send_effects(base_chain, wallet)
        base_chain.set_allowance(USDC, wallet, ROUTER, 10**6)
        order = await service.revoke(
            chain=BASE,
            wallet=None,
            token=USDC,
            spender=ROUTER,
            initiator="agent",
            session_key="s",
            note="clean up",
        )
        assert order["status"] == "awaiting_approval" and order["valueUsd"] == 0.0
        assert base_chain.sent == []
        approved = await service.approve(order["orderId"], wait=True)
        assert approved["status"] == "confirmed"
        assert base_chain.get_allowance(USDC, wallet, ROUTER) == 0
        # Gas only: nothing was spent against the cap.
        assert service.limits(wallet)["spentTodayUsd"] == 0.0


class TestDecodeAndNetwork:
    async def test_decode_calldata_and_hash(
        self, funded_service: TradingService, base_chain: FakeChain
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_send_effects(base_chain, wallet)
        raw = await service.decode(BASE, data=encode_approve(PERMIT2, UINT256_MAX), to=USDC)
        assert raw["call"]["function"] == "approve" and raw["decoded"]["unlimited"] is True
        assert raw["decoded"]["counterpartyLabel"] == "Permit2"
        assert raw["decoded"]["token"]["symbol"] == "USDC" and raw["tx"] is None
        orders = await _send(service)
        tx_hash = orders[0]["txHash"]
        result = await service.decode(BASE, tx_hash=tx_hash)
        assert result["description"].startswith("transfer 10000000 units")
        assert result["decoded"]["amount"] == "10" and result["decoded"]["function"] == "transfer"
        assert result["decoded"]["counterparty"] == checksum_address(OTHER)
        assert result["tx"]["status"] == "success" and result["tx"]["from"] == wallet.lower()
        assert result["transfers"][0]["amount"] == "10"
        assert result["transfers"][0]["to"] == checksum_address(OTHER)
        assert result["wallets"] == [wallet]
        assert result["explorerUrl"] == BASE.tx_url(tx_hash)
        with pytest.raises(TradingError, match="no transaction"):
            await service.decode(BASE, tx_hash="0x" + "00" * 32)
        with pytest.raises(TradingError, match="not a transaction hash"):
            await service.decode(BASE, tx_hash="0x123")
        with pytest.raises(TradingError, match="txHash or data"):
            await service.decode(BASE)

    async def test_network_reports_head_gas_and_latency(
        self, service: TradingService, base_chain: FakeChain, robinhood_chain: FakeChain
    ) -> None:
        result = await service.network()
        rows = {row["chainId"]: row for row in result["chains"]}
        base = rows[8453]
        assert base["blockNumber"] == base_chain.block and base["latencyMs"] is not None
        assert base["priorityFeeGwei"] == pytest.approx(0.001)
        assert base["baseFeeGwei"] is not None and base["rpcUrl"] == "https://mainnet.base.org"
        # The fake stamps blocks far in the past: an honest probe calls that stale.
        assert base["blockAgeS"] is not None and base["healthy"] is False
        assert rows[4663]["blockNumber"] == robinhood_chain.block
        # Cached: a node outage is not seen until the cache lapses or a fresh read is asked.
        base_chain.fail_methods.add("eth_getBlockByNumber")
        assert (await service.network())["chains"][0]["error"] is None
        fresh = await service.network(fresh=True)
        assert fresh["chains"][0]["error"] and fresh["chains"][0]["healthy"] is False
