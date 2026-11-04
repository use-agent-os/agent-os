"""Swap providers: one protocol, two implementations.

The service only ever talks to a :class:`SwapProvider`: quote, approval
transaction (if any), swap transaction. The AgentOS Aggregator is the
default and needs no key; Uniswap's Trading API is the fallback, and needs
one. Each provider normalises its own quirks — native-token sentinel, quote
freshness, slippage units — so the order pipeline stays provider-agnostic.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from agentos.trading.chains import NATIVE_ADDRESS, ChainSpec, is_native
from agentos.trading.evm import EvmClient
from agentos.trading.uniswap import (
    PROXY_SPENDER,
    UNIVERSAL_ROUTERS,
    DecisionOrigin,
    Quote,
    UniswapAuthError,
    UniswapClient,
    UniswapError,
)

ProviderId = Literal["aggregator", "uniswap"]
#: Ordered: the default comes first, everywhere a client lists providers.
PROVIDER_IDS: tuple[str, ...] = ("aggregator", "uniswap")
DEFAULT_PROVIDER_ID = "aggregator"
PROVIDER_LABELS: dict[str, str] = {
    "aggregator": "AgentOS Aggregator",
    "uniswap": "Uniswap",
    # Not a swap provider: the bridge an order of kind ``bridge`` runs through.
    "relay": "Relay",
}


class ProviderError(RuntimeError):
    """A provider failure with a stable error code for the RPC layer."""

    def __init__(
        self, code: str, message: str, *, retryable: bool = False, details: Any = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.details = details


@dataclass
class ProbeResult:
    ok: bool
    latency_ms: float | None
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "latencyMs": round(self.latency_ms, 1) if self.latency_ms is not None else None,
            "error": self.error,
        }


@dataclass
class ProviderQuote:
    """A provider-neutral quote. ``raw`` is the provider's own object."""

    provider: str
    chain_id: int
    swapper: str
    token_in: str  # lowercase; NATIVE_ADDRESS for the gas token
    token_out: str
    amount_in_raw: int
    amount_out_raw: int
    min_out_raw: int
    slippage_pct: float | None
    price_impact_pct: float | None
    gas_usd: float | None
    quote_id: str | None
    routing: str
    fetched_at: float
    fresh_for_s: float
    raw: Any
    warnings: list[str] = field(default_factory=list)
    # Gas cost in the chain's own coin, for providers that price gas in ETH
    # rather than dollars. The service converts it once it has a native price.
    gas_native: float | None = None

    @property
    def age_s(self) -> float:
        return max(0.0, time.time() - self.fetched_at)

    @property
    def fresh(self) -> bool:
        return self.age_s < self.fresh_for_s


class SwapProvider(Protocol):
    id: str
    label: str
    needs_api_key: bool

    async def probe(self, *, chain: ChainSpec) -> ProbeResult: ...

    async def quote(
        self,
        *,
        chain: ChainSpec,
        swapper: str,
        token_in: str,
        token_out: str,
        amount_raw: int,
        slippage_pct: float | None,
        decision_origin: DecisionOrigin,
    ) -> ProviderQuote: ...

    async def approval_tx(
        self, quote: ProviderQuote, *, evm: EvmClient, decision_origin: DecisionOrigin
    ) -> dict[str, Any] | None: ...

    def trusted_spenders(self, chain: ChainSpec, quote: ProviderQuote) -> frozenset[str]:
        """The only addresses an ERC-20 approval for this provider may name.

        The service decodes every approval it is asked to sign and refuses a
        spender outside this set: a provider response (or a tampered one)
        cannot make the wallet approve an arbitrary contract.
        """
        ...

    def trusted_targets(self, chain: ChainSpec, quote: ProviderQuote) -> frozenset[str]:
        """The only addresses (lowercase) a swap transaction may be sent ``to``.

        Pinned per provider and chain, never read from the quote: calldata
        is opaque, so the contract it is sent to is the one thing the
        service can still hold a provider to. An empty set refuses every
        swap on that chain.
        """
        ...

    async def build(
        self,
        quote: ProviderQuote,
        *,
        deadline: int,
        sign_permit: Callable[[dict[str, Any]], str] | None,
        decision_origin: DecisionOrigin,
    ) -> dict[str, Any]: ...


# Canonical Permit2, the same address on every chain Uniswap deploys to.
PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"


# ── Uniswap ────────────────────────────────────────────────────────────────


class UniswapProvider:
    id = "uniswap"
    label = PROVIDER_LABELS["uniswap"]
    needs_api_key = True

    def __init__(self, client: UniswapClient) -> None:
        self.client = client

    @staticmethod
    def _addr(address: str) -> str:
        return NATIVE_ADDRESS if is_native(address) else address

    async def probe(self, *, chain: ChainSpec) -> ProbeResult:
        try:
            ok, latency, error = await self.client.probe(chain_id=chain.chain_id)
        except UniswapAuthError as exc:
            return ProbeResult(ok=False, latency_ms=None, error=str(exc))
        return ProbeResult(ok=ok, latency_ms=latency, error=error)

    async def quote(
        self,
        *,
        chain: ChainSpec,
        swapper: str,
        token_in: str,
        token_out: str,
        amount_raw: int,
        slippage_pct: float | None,
        decision_origin: DecisionOrigin,
    ) -> ProviderQuote:
        try:
            quote = await self.client.quote(
                chain_id=chain.chain_id,
                swapper=swapper,
                token_in=self._addr(token_in),
                token_out=self._addr(token_out),
                amount_raw=amount_raw,
                slippage_pct=slippage_pct,
                decision_origin=decision_origin,
            )
        except UniswapError as exc:
            raise ProviderError(
                exc.code, str(exc), retryable=exc.retryable, details={"errorCode": exc.error_code}
            ) from exc
        return ProviderQuote(
            provider=self.id,
            chain_id=chain.chain_id,
            swapper=swapper,
            token_in=self._addr(token_in).lower(),
            token_out=self._addr(token_out).lower(),
            amount_in_raw=quote.amount_in_raw,
            amount_out_raw=quote.amount_out_raw,
            min_out_raw=quote.min_out_raw,
            slippage_pct=quote.slippage_pct,
            price_impact_pct=quote.price_impact_pct,
            gas_usd=quote.gas_fee_usd,
            quote_id=quote.quote_id or quote.request_id,
            routing=quote.routing,
            fetched_at=quote.fetched_at,
            fresh_for_s=30.0,
            raw=quote,
        )

    async def approval_tx(
        self, quote: ProviderQuote, *, evm: EvmClient, decision_origin: DecisionOrigin
    ) -> dict[str, Any] | None:
        if is_native(quote.token_in):
            return None
        try:
            return await self.client.check_approval(
                chain_id=quote.chain_id,
                wallet=quote.swapper,
                token=quote.token_in,
                amount_raw=quote.amount_in_raw,
                decision_origin=decision_origin,
            )
        except UniswapError as exc:
            raise ProviderError(exc.code, str(exc), retryable=exc.retryable) from exc

    def trusted_spenders(self, chain: ChainSpec, quote: ProviderQuote) -> frozenset[str]:
        # The Trading API approves either Permit2 (classic routes with a
        # permit signature) or its allowance-holder proxy; nothing else.
        return frozenset({PERMIT2.lower(), PROXY_SPENDER.lower()})

    def trusted_targets(self, chain: ChainSpec, quote: ProviderQuote) -> frozenset[str]:
        # The no-Permit2 workflow sends the swap to the Trading API's proxy;
        # a permit route to the Universal Router. Both are pinned per chain
        # in ``UNIVERSAL_ROUTERS``: a chain without a verified router entry
        # gets an empty set, which refuses the swap rather than guessing.
        routers = UNIVERSAL_ROUTERS.get(chain.chain_id)
        if not routers:
            return frozenset()
        return frozenset({r.lower() for r in routers} | {PROXY_SPENDER.lower()})

    async def build(
        self,
        quote: ProviderQuote,
        *,
        deadline: int,
        sign_permit: Callable[[dict[str, Any]], str] | None,
        decision_origin: DecisionOrigin,
    ) -> dict[str, Any]:
        raw: Quote = quote.raw
        signature = None
        if raw.permit_data is not None:
            if sign_permit is None:
                raise ProviderError("trading.tx_failed", "quote needs a permit signature")
            signature = sign_permit(raw.permit_data)
        try:
            return await self.client.swap(
                raw, signature=signature, deadline=deadline, decision_origin=decision_origin
            )
        except UniswapError as exc:
            raise ProviderError(exc.code, str(exc), retryable=exc.retryable) from exc


def provider_label(provider_id: str) -> str:
    return PROVIDER_LABELS.get(provider_id, provider_id)
