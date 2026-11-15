"""Guardrails and approvals bind to exactly what gets signed (WAL-06/07/12/23).

Each class ports one audit PoC onto the repository fakes: no chain RPC, no
network, no real keys. On the base commit every test here fails the way the
audit observed; after the fix it passes.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from agentos.trading import permits
from agentos.trading import service as service_module
from agentos.trading.chains import BASE
from agentos.trading.providers import PERMIT2
from agentos.trading.service import TradingService
from agentos.trading.uniswap import UNIVERSAL_ROUTERS
from tests.test_trading.fakes import (
    USDC,
    WETH,
    FakeAggregator,
    FakeChain,
    FakePrices,
    FakeUniswap,
)
from tests.test_trading.test_service import _wire_swap_effects


def _events(service: TradingService, name: str) -> list[dict[str, Any]]:
    return [payload for event, payload in service.events if event == name]  # type: ignore[attr-defined]


async def _agent_swap(service: TradingService, **extra: Any) -> dict[str, Any]:
    orders = await service.swap(
        chain=BASE,
        wallets=None,
        token_in=extra.pop("token_in", "USDC"),
        token_out=extra.pop("token_out", "WETH"),
        amount_in=extra.pop("amount_in", "10"),
        amount_pct=extra.pop("amount_pct", None),
        slippage_pct=None,
        initiator=extra.pop("initiator", "agent"),
        session_key=None,
        note=None,
        wait=True,
        **extra,
    )
    return orders[0]


class TestCapBeforeClientQuotePark:
    """WAL-23: a client-declared ``expectedOutRaw`` must not turn a cap refusal into a park."""

    @pytest.mark.parametrize("cap", [0.0, 5.0])
    async def test_over_cap_agent_swap_with_a_stale_client_quote_is_rejected(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        fake_aggregator: FakeAggregator,
        cap: float,
    ) -> None:
        service = funded_service
        service.config.daily_cap_usd = cap
        # A little over 2x the default 0.5 % slippage above the fresh quote.
        declared = fake_aggregator.amount_out * 102 // 100
        order = await _agent_swap(service, expected_out_raw=declared)
        assert order["status"] == "rejected"
        assert "daily cap" in str(order["reason"])
        assert base_chain.sent == []
        assert _events(service, "trading.approval.requested") == []

    async def test_under_cap_stale_client_quote_still_parks_and_names_every_reason(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        fake_aggregator: FakeAggregator,
    ) -> None:
        service = funded_service
        service.config.approval_threshold_usd = 5.0  # the $10 order is also above it
        declared = fake_aggregator.amount_out * 102 // 100
        order = await _agent_swap(service, expected_out_raw=declared)
        assert order["status"] == "awaiting_approval"
        assert "since you confirmed" in order["reason"]
        assert "above the 5.00 USD threshold" in order["reason"]
        assert base_chain.sent == []


class TestUnpricedSellSideFailsClosed:
    """WAL-06: an agent swap is never valued from the buy side alone, on any provider."""

    @pytest.mark.parametrize("usdc_price", [None, 0.0])
    async def test_uniswap_swap_of_an_unpriced_holding_waits_for_a_human(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        fake_prices: FakePrices,
        fake_uniswap: FakeUniswap,
        usdc_price: float | None,
    ) -> None:
        # The v3-08 PoC: the sell side (here the whole USDC balance) has no
        # feed price; the buy side is a token whose own pool says it is
        # worth next to nothing; Uniswap reports a benign 0.12 % impact.
        service = funded_service
        service.config.provider = "uniswap"
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_swap_effects(base_chain, wallet)
        if usdc_price is None:
            fake_prices.spot.pop(("base", USDC))
        else:
            fake_prices.spot[("base", USDC)] = usdc_price
        fake_prices.spot[("base", WETH)] = 0.0001
        order = await _agent_swap(service, amount_in=None, amount_pct=100.0)
        assert order["status"] == "awaiting_approval"
        assert order["reason"] == "value unknown (no price)"
        # Unknown, not "$0": nothing on the row reads as a tiny order.
        assert order["valueUsd"] is None
        assert base_chain.sent == []
        assert base_chain.get_erc20(USDC, wallet) == 1_000 * 10**6
        assert service.limits(wallet)["spentTodayUsd"] == 0.0

    async def test_uniswap_impact_is_cross_checked_against_the_feed(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        fake_uniswap: FakeUniswap,
    ) -> None:
        # Both sides priced: $10 of USDC for 0.0005 WETH ($1 at the fake's
        # $2,000) is a 90 % loss whatever Uniswap's own priceImpact says.
        service = funded_service
        service.config.provider = "uniswap"
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_swap_effects(base_chain, wallet)
        assert fake_uniswap.price_impact == pytest.approx(0.12)
        order = await _agent_swap(service)
        assert order["status"] == "awaiting_approval"
        assert order["reason"].startswith("price impact 90.00% is above")
        assert order["priceImpactPct"] == pytest.approx(90.0)
        assert base_chain.sent == []

    async def test_uniswap_impact_with_an_unpriced_buy_side_is_unknown(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        fake_prices: FakePrices,
        fake_uniswap: FakeUniswap,
    ) -> None:
        service = funded_service
        service.config.provider = "uniswap"
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_swap_effects(base_chain, wallet)
        fake_prices.spot.pop(("base", WETH))
        order = await _agent_swap(service)
        # Sized by what leaves the wallet; the provider's impact alone decides nothing.
        assert order["valueUsd"] == pytest.approx(10.0)
        assert order["status"] == "awaiting_approval"
        assert order["reason"] == "price impact unknown (no reference price for one side)"
        assert base_chain.sent == []

    async def test_a_consistent_uniswap_quote_still_runs(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        fake_uniswap: FakeUniswap,
    ) -> None:
        service = funded_service
        service.config.provider = "uniswap"
        wallet = service.test_wallet  # type: ignore[attr-defined]
        fake_uniswap.amount_out = 5 * 10**15  # $10 for $10
        _wire_swap_effects(base_chain, wallet, out_amount=5 * 10**15)
        order = await _agent_swap(service)
        assert order["status"] == "confirmed"
        assert service.limits(wallet)["spentTodayUsd"] == pytest.approx(10.0)

    async def test_aggregator_still_parks_and_reports_the_value_as_unknown(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        fake_prices: FakePrices,
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_swap_effects(base_chain, wallet)
        fake_prices.spot.pop(("base", USDC))
        order = await _agent_swap(service)
        assert order["status"] == "awaiting_approval"
        assert order["valueUsd"] is None
        assert base_chain.sent == []


# ── WAL-07: provider permitData is checked against the order before signing ─────

ATTACKER = "0x" + "b9" * 20
UNIVERSAL_ROUTER_BASE = "0xD6145b2D3F379919E8CdEdA7B97E37c4b2Ca9C40"
DUMMY_KEY = bytes.fromhex("33" * 32)


def _permit_single(**over: Any) -> dict[str, Any]:
    now = int(time.time())
    details = {
        "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "amount": str(10 * 10**6),
        "expiration": str(now + 1800),
        "nonce": "0",
    }
    details.update(over.pop("details", {}))
    values = {"details": details, "spender": UNIVERSAL_ROUTER_BASE, "sigDeadline": str(now + 1800)}
    values.update(over.pop("values", {}))
    permit: dict[str, Any] = {
        "domain": {"name": "Permit2", "chainId": 8453, "verifyingContract": PERMIT2},
        "types": json.loads(json.dumps(permits.PERMIT_SINGLE_TYPES)),
        "values": values,
    }
    permit.update(over)
    return permit


def _hostile(wallet: str) -> dict[str, dict[str, Any]]:
    """The three payload classes the v3-09 PoC got the vault to sign."""
    m = (1 << 256) - 1
    return {
        "eip3009_full_balance": {
            "domain": {
                "name": "USD Coin",
                "version": "2",
                "chainId": 8453,
                "verifyingContract": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            },
            "types": {
                "TransferWithAuthorization": [
                    {"name": "from", "type": "address"},
                    {"name": "to", "type": "address"},
                    {"name": "value", "type": "uint256"},
                    {"name": "validAfter", "type": "uint256"},
                    {"name": "validBefore", "type": "uint256"},
                    {"name": "nonce", "type": "bytes32"},
                ]
            },
            "values": {
                "from": wallet,
                "to": ATTACKER,
                "value": 1_000 * 10**6,
                "validAfter": 0,
                "validBefore": m,
                "nonce": "0x" + "88" * 32,
            },
        },
        "permit2_unlimited_attacker_spender": _permit_single(
            details={"amount": (1 << 160) - 1, "expiration": (1 << 48) - 1},
            values={"spender": ATTACKER, "sigDeadline": m},
        ),
        "eip2612_other_chain_other_token": {
            "domain": {
                "name": "Other",
                "version": "1",
                "chainId": 1,
                "verifyingContract": "0x" + "c0" * 20,
            },
            "types": {
                "Permit": [
                    {"name": "owner", "type": "address"},
                    {"name": "spender", "type": "address"},
                    {"name": "value", "type": "uint256"},
                    {"name": "nonce", "type": "uint256"},
                    {"name": "deadline", "type": "uint256"},
                ]
            },
            "values": {"owner": wallet, "spender": ATTACKER, "value": m, "nonce": 0, "deadline": m},
        },
    }


def _check(permit: Any, **over: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "chain_id": 8453,
        "token": USDC,
        "native": False,
        "amount_raw": 10 * 10**6,
        "spenders": frozenset(r.lower() for r in UNIVERSAL_ROUTERS[8453]),
        "now": int(time.time()),
    }
    kwargs.update(over)
    return permits.check_permit(permit, **kwargs)


def _recover(permit: dict[str, Any], signature: str) -> str:
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    types = {k: v for k, v in permit["types"].items() if k != "EIP712Domain"}
    signable = encode_typed_data(
        domain_data=permit["domain"], message_types=types, message_data=permit["values"]
    )
    return str(Account.recover_message(signable, signature=signature))


class TestPermitCheck:
    @pytest.mark.parametrize(
        "case",
        [
            "eip3009_full_balance",
            "permit2_unlimited_attacker_spender",
            "eip2612_other_chain_other_token",
        ],
    )
    def test_the_audit_payloads_are_refused(self, case: str) -> None:
        with pytest.raises(permits.PermitRefusedError):
            _check(_hostile("0x" + "11" * 20)[case])

    @pytest.mark.parametrize(
        ("over", "match"),
        [
            ({"domain": {"name": "Permit2", "chainId": 1, "verifyingContract": PERMIT2}}, "chain"),
            (
                {"domain": {"name": "Permit2", "chainId": 8453, "verifyingContract": ATTACKER}},
                "canonical Permit2",
            ),
            (
                {
                    "domain": {
                        "name": "Permit2",
                        "version": "1",
                        "chainId": 8453,
                        "verifyingContract": PERMIT2,
                    }
                },
                "exactly",
            ),
            ({"values": {"spender": ATTACKER}}, "not a router"),
            ({"details": {"token": "0x4200000000000000000000000000000000000006"}}, "token"),
            ({"details": {"amount": str(10 * 10**6 + 1)}}, "exceeds"),
            ({"details": {"amount": str((1 << 160) - 1)}}, "exceeds"),
            ({"details": {"amount": "0"}}, "exceeds"),
            ({"details": {"expiration": str((1 << 48) - 1)}}, "minutes away"),
            ({"details": {"expiration": "1"}}, "past"),
            ({"values": {"sigDeadline": str((1 << 256) - 1)}}, "minutes away"),
            ({"details": {"nonce": str(1 << 48)}}, "uint48"),
            ({"details": {"amount": "0x989680"}}, "integer"),
            ({"values": {"extra": "1"}}, "exactly"),
            ({"primaryType": "PermitBatch"}, "primaryType"),
        ],
    )
    def test_each_binding_rule_refuses(self, over: dict[str, Any], match: str) -> None:
        with pytest.raises(permits.PermitRefusedError, match=match):
            _check(_permit_single(**over))

    def test_other_types_are_refused(self) -> None:
        permit = _permit_single()
        permit["types"]["PermitDetails"][1]["type"] = "uint256"
        with pytest.raises(permits.PermitRefusedError, match="not a Permit2"):
            _check(permit)
        with pytest.raises(permits.PermitRefusedError, match="gas coin"):
            _check(_permit_single(), native=True)
        with pytest.raises(permits.PermitRefusedError, match="not a router"):
            _check(_permit_single(), spenders=frozenset())

    def test_a_bound_permit_is_rebuilt_and_signs_as_the_original(self) -> None:
        from eth_account import Account

        original = _permit_single()
        checked = _check(original)
        assert checked["primaryType"] == "PermitSingle"
        assert checked["values"]["details"]["amount"] == 10 * 10**6
        signature = service_module._sign_permit(checked, DUMMY_KEY)
        wallet = Account.from_key(DUMMY_KEY).address
        assert _recover(checked, signature) == wallet
        # The same struct the provider asked for: its own object verifies too.
        assert _recover(original, signature) == wallet

    def test_permit_transfer_from_is_checked_the_same_way(self) -> None:
        now = int(time.time())
        permit = {
            "domain": {"name": "Permit2", "chainId": 8453, "verifyingContract": PERMIT2},
            "types": json.loads(json.dumps(permits.PERMIT_TRANSFER_FROM_TYPES)),
            "values": {
                "permitted": {"token": USDC, "amount": str(10 * 10**6)},
                "spender": UNIVERSAL_ROUTER_BASE,
                "nonce": str(2**200),
                "deadline": str(now + 600),
            },
        }
        assert _check(permit)["primaryType"] == "PermitTransferFrom"
        permit["values"]["permitted"]["amount"] = str(10**30)
        with pytest.raises(permits.PermitRefusedError, match="exceeds"):
            _check(permit)


class TestPermitOnTheSwapPath:
    """End to end: an agent swap whose Uniswap quote carries ``permitData``."""

    def _setup(
        self, service: TradingService, base_chain: FakeChain, fake_uniswap: FakeUniswap
    ) -> list[dict[str, Any]]:
        service.config.provider = "uniswap"
        fake_uniswap.amount_out = 5 * 10**15
        _wire_swap_effects(base_chain, service.test_wallet, out_amount=5 * 10**15)  # type: ignore[attr-defined]
        signed: list[dict[str, Any]] = []

        def spy(permit: dict[str, Any], key: bytes) -> str:
            signed.append(permit)
            return service_module._sign_permit(permit, key)

        service._sign_permit = spy  # type: ignore[method-assign]
        return signed

    @staticmethod
    def _swap_bodies(fake_uniswap: FakeUniswap) -> list[dict[str, Any]]:
        return [
            json.loads(r.content) for r in fake_uniswap.requests if r.url.path.endswith("/swap")
        ]

    @pytest.mark.parametrize(
        "case",
        [
            "eip3009_full_balance",
            "permit2_unlimited_attacker_spender",
            "eip2612_other_chain_other_token",
        ],
    )
    async def test_a_hostile_permit_is_never_signed(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        fake_uniswap: FakeUniswap,
        case: str,
    ) -> None:
        service = funded_service
        signed = self._setup(service, base_chain, fake_uniswap)
        fake_uniswap.permit_data = _hostile(service.test_wallet)[case]  # type: ignore[attr-defined]
        order = await _agent_swap(service)
        assert order["status"] == "failed"
        assert order["reason"].startswith("trading.permit_refused")
        assert signed == []
        # Nothing reached the provider's /swap: no signature exists to send.
        assert self._swap_bodies(fake_uniswap) == []
        assert base_chain.sent == []

    async def test_a_bound_permit_is_signed_rebuilt_and_the_swap_runs(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        fake_uniswap: FakeUniswap,
    ) -> None:
        service = funded_service
        signed = self._setup(service, base_chain, fake_uniswap)
        fake_uniswap.permit_data = _permit_single(primaryType="PermitSingle")
        order = await _agent_swap(service)
        assert order["status"] == "confirmed"
        assert len(signed) == 1 and signed[0]["values"]["details"]["amount"] == 10 * 10**6
        body = self._swap_bodies(fake_uniswap)[-1]
        wallet = service.test_wallet  # type: ignore[attr-defined]
        assert _recover(signed[0], body["signature"]).lower() == wallet.lower()
