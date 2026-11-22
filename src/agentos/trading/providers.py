"""Swap providers: the protocol the order pipeline talks to.

The service only ever talks to a :class:`SwapProvider`: quote, approval
transaction (if any), swap transaction. There is one implementation, the
AgentOS DEX (:mod:`agentos.trading.aggregator`), which needs no key and
quotes 0x and Uniswap as its two liquidity sources. A provider normalises its
own quirks — native-token sentinel, quote freshness, slippage units — so the
order pipeline stays provider-agnostic.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from agentos.trading.chains import ChainSpec
from agentos.trading.evm import EvmClient

#: Who decided an order: a person at a client, or the agent on its own.
DecisionOrigin = Literal["human_mediated", "autonomous"]

ProviderId = Literal["aggregator"]
PROVIDER_IDS: tuple[str, ...] = ("aggregator",)
DEFAULT_PROVIDER_ID = "aggregator"
PROVIDER_LABELS: dict[str, str] = {
    "aggregator": "AgentOS DEX",
    # Not a provider any more (removed 2026-11): the label of order rows the
    # Uniswap Trading API provider filled while it existed.
    "uniswap": "Uniswap",
    # Not swap providers: the bridge an order of kind ``bridge`` runs through
    # (AgentOS DEX picks one per quote, ``bridge.BRIDGE_SOURCES``).
    "relay": "Relay",
    "across": "Across",
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
class SwapFee:
    """The fee a provider takes on the swap itself (not gas).

    ``symbol`` names the token the fee is paid in (the sell token), as the
    provider spells it; ``amount_raw`` is in that token's base units and
    ``amount`` its decimal form.
    """

    bps: int | None
    symbol: str
    amount_raw: int | None
    amount: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "bps": self.bps,
            "pct": self.bps / 100.0 if self.bps is not None else None,
            "symbol": self.symbol,
            "amount": self.amount,
            "amountRaw": str(self.amount_raw) if self.amount_raw is not None else None,
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
    # Whether ``price_impact_pct`` was checked against the engine's own feed
    # (both sides priced). A provider's figure alone is shown, never trusted:
    # the agent guardrails read an unchecked impact as unknown.
    impact_checked: bool = False
    # The provider's own fee on the swap (the AgentOS DEX fee on the
    # aggregator); ``None`` when the provider charges none or says nothing.
    swap_fee: SwapFee | None = None

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
        decision_origin: DecisionOrigin,
    ) -> dict[str, Any]: ...


# Canonical Permit2, the same address on every chain Uniswap deploys to.
PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"


def _is_address(value: Any) -> bool:
    text = str(value or "")
    if not text.startswith("0x") or len(text) != 42:
        return False
    try:
        int(text[2:], 16)
    except ValueError:
        return False
    return True


def validate_transaction(tx: dict[str, Any]) -> None:
    """Refuse a swap transaction that is not safe to sign as-is."""
    data = str(tx.get("data") or "")
    if not data.startswith("0x") or len(data) <= 2:
        raise ProviderError("trading.tx_failed", "Swap transaction has empty calldata")
    try:
        int(data[2:], 16)
    except ValueError as exc:
        raise ProviderError("trading.tx_failed", "Swap calldata is not hex") from exc
    if not _is_address(tx.get("to")):
        raise ProviderError("trading.tx_failed", "Swap transaction has no valid 'to'")
    if not _is_address(tx.get("from")):
        raise ProviderError("trading.tx_failed", "Swap transaction has no valid 'from'")
    if tx.get("value") is None:
        raise ProviderError("trading.tx_failed", "Swap transaction has no 'value'")
    if tx.get("maxFeePerGas") and tx.get("gasPrice"):
        raise ProviderError(
            "trading.tx_failed", "Swap transaction sets both maxFeePerGas and gasPrice"
        )


def provider_label(provider_id: str) -> str:
    return PROVIDER_LABELS.get(provider_id, provider_id)
