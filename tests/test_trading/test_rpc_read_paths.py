"""Read paths that must not call the chain when they have nothing to learn (issue #3976).

Every JSON-RPC item is billed on a metered endpoint, so these count the items
the fake nodes receive (``FakeChain.calls``) on a pinned clock:

- ``recover_submitted``: about five receipt polls per stuck order per pass.
"""

from __future__ import annotations

import time

import pytest

from agentos.trading import service as service_module
from agentos.trading.chains import BASE
from agentos.trading.service import TradingService
from tests.test_trading.fakes import USDC, WETH, FakeChain
from tests.test_trading.test_service import _wire_swap_effects


def _rpc(chain: FakeChain, method: str) -> int:
    return sum(1 for call in chain.calls if call["method"] == method)


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
