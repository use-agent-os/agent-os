"""Chain registry for the trading subsystem.

Two mainnets only: Base (8453) and Robinhood Chain (4663). Everything the
rest of the package needs to know about a chain lives here — default RPC,
explorer, well-known token addresses and the slugs the price sources use —
so adding a chain later is a table row, not a code change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

NATIVE_ADDRESS = "0x0000000000000000000000000000000000000000"
# Multicall3's canonical address. Same bytecode, with ``aggregate3``, on Base and
# Robinhood Chain (eth_getCode on both public nodes, 2026-11-07).
MULTICALL3 = "0xca11bde05977b3631167028862be2a173976ca11"


@dataclass(frozen=True)
class ChainSpec:
    chain_id: int
    key: str
    name: str
    native_symbol: str
    native_name: str
    rpc_url: str
    explorer_url: str
    usdc: str | None
    weth: str | None
    dexscreener_slug: str
    coingecko_platform: str | None
    # Block time hint (seconds) for receipt polling and log chunking.
    block_time_s: float = 2.0
    # Largest eth_getLogs span the public RPC tolerates per call.
    max_log_span: int = 2000
    # Span for a *sparse* topic filter (one wallet's Approval logs), where
    # the answer is a handful of rows however wide the window. Measured on
    # dRPC 2026-09-20: Base takes 100k blocks in ~7 s and refuses 1M with an
    # HTTP 500; Robinhood Chain (0.1 s blocks) caps at "200000 addresses ×
    # blocks", which with no address filter is 40k. The scanner halves on a
    # refusal, so these are ceilings, not promises.
    approval_log_span: int = 20_000
    aliases: tuple[str, ...] = field(default_factory=tuple)
    # A Blockscout instance indexing this chain, used only to *discover* which
    # ERC-20s a wallet holds; every balance is then read from the RPC.
    blockscout_url: str | None = None
    # Multicall3 on this chain: many ``balanceOf`` become one ``eth_call``.
    multicall3: str | None = None
    # Gas ceilings for anything this desk signs. A fee quote comes from the
    # node (and a gas limit sometimes from the provider); neither is allowed
    # to name a number above these — a wild tip, a base-fee spike or an
    # inflated limit is refused, never clamped upward. L2 defaults: tips are
    # well under a gwei, base fees a few hundred mwei, a swap well under 1M gas.
    max_priority_fee_wei: int = 2 * 10**9
    max_fee_per_gas_wei: int = 50 * 10**9
    max_gas_limit: int = 3_000_000

    def tx_url(self, tx_hash: str) -> str:
        return f"{self.explorer_url}/tx/{tx_hash}"

    def address_url(self, address: str) -> str:
        return f"{self.explorer_url}/address/{address}"

    def token_url(self, address: str) -> str:
        return f"{self.explorer_url}/token/{address}"

    def to_dict(self) -> dict[str, object]:
        return {
            "chainId": self.chain_id,
            "key": self.key,
            "name": self.name,
            "native": self.native_symbol,
            "explorer": self.explorer_url,
            "rpcUrl": self.rpc_url,
        }


BASE = ChainSpec(
    chain_id=8453,
    key="base",
    name="Base",
    native_symbol="ETH",
    native_name="Ether",
    rpc_url="https://mainnet.base.org",
    explorer_url="https://basescan.org",
    usdc="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    weth="0x4200000000000000000000000000000000000006",
    dexscreener_slug="base",
    coingecko_platform="base",
    block_time_s=2.0,
    max_log_span=2000,
    approval_log_span=100_000,
    aliases=("base-mainnet", "8453"),
    blockscout_url="https://base.blockscout.com",
    multicall3=MULTICALL3,
)

ROBINHOOD = ChainSpec(
    chain_id=4663,
    key="robinhood",
    name="Robinhood Chain",
    native_symbol="ETH",
    native_name="Ether",
    rpc_url="https://rpc.mainnet.chain.robinhood.com",
    explorer_url="https://robinhoodchain.blockscout.com",
    # Resolved lazily from the CoinGecko list (see prices.py); the chain is
    # young and the canonical USDC/WETH addresses are not hard-coded here on
    # purpose — the list is the authoritative registry.
    usdc=None,
    weth=None,
    dexscreener_slug="robinhood",
    coingecko_platform="robinhood",
    block_time_s=0.1,
    max_log_span=2000,
    approval_log_span=40_000,
    aliases=("robinhood-chain", "hood", "4663"),
    # robinhoodchain.blockscout.com answers every non-browser request with a
    # Cloudflare challenge (403, checked 2026-09-15, any User-Agent), so there
    # is no discovery here: the sweep and the token registry are what find
    # a wallet's tokens on this chain.
    blockscout_url=None,
    multicall3=MULTICALL3,
)

CHAINS: dict[int, ChainSpec] = {BASE.chain_id: BASE, ROBINHOOD.chain_id: ROBINHOOD}


class UnsupportedChainError(ValueError):
    pass


def chain_by_id(chain_id: int | str) -> ChainSpec:
    try:
        cid = int(chain_id)
    except (TypeError, ValueError) as exc:
        raise UnsupportedChainError(f"Unsupported chain: {chain_id!r}") from exc
    spec = CHAINS.get(cid)
    if spec is None:
        raise UnsupportedChainError(f"Unsupported chain id: {cid}")
    return spec


def resolve_chain(value: int | str | None) -> ChainSpec:
    """Accept a chain id, key or alias (``base``, ``robinhood``, ``4663``)."""
    if value is None:
        raise UnsupportedChainError("chain is required")
    if isinstance(value, int):
        return chain_by_id(value)
    text = str(value).strip().lower()
    if text.isdigit():
        return chain_by_id(int(text))
    for spec in CHAINS.values():
        if text == spec.key or text in spec.aliases or text == spec.name.lower():
            return spec
    raise UnsupportedChainError(f"Unsupported chain: {value!r}")


# Environment fallbacks (the user runs dRPC endpoints through these).
RPC_ENV_VARS: dict[int, str] = {8453: "RPC_BASE_URL", 4663: "RPC_ROBINHOOD_URL"}


def rpc_url_for(spec: ChainSpec, overrides: dict[str, str] | None) -> str:
    """The RPC URL to use: config override, then env var, then the public default."""
    if overrides:
        for key in (str(spec.chain_id), spec.key):
            url = (overrides.get(key) or "").strip()
            if url:
                return url
    env_name = RPC_ENV_VARS.get(spec.chain_id)
    if env_name:
        url = os.environ.get(env_name, "").strip()
        if url:
            return url
    return spec.rpc_url


def redact_rpc_url(url: str) -> str:
    """Hide the path and query of an RPC URL: provider keys live there (dRPC, Alchemy, Infura)."""
    parsed = urlsplit(url.strip())
    if not parsed.scheme or not parsed.netloc:
        return "…" if url.strip() else ""
    host = parsed.hostname or parsed.netloc
    if parsed.port:
        host = f"{host}:{parsed.port}"
    tail = "/…" if (parsed.path.strip("/") or parsed.query) else ""
    return f"{parsed.scheme}://{host}{tail}"


def is_native(address: str | None) -> bool:
    return not address or address.lower() in {NATIVE_ADDRESS, "eth", "native"}


def normalize_address(address: str) -> str:
    """Lower-case a 0x address after a strict shape check.

    Mixed case is an EIP-55 claim and is verified: a recipient or spender
    pasted with one wrong letter would otherwise pass as a different, valid
    address and the tokens would be gone. All-lowercase and all-uppercase
    make no claim and are accepted as they are.
    """
    text = (address or "").strip()
    if is_native(text):
        return NATIVE_ADDRESS
    if not text.startswith("0x") or len(text) != 42:
        raise ValueError(f"Not an EVM address: {address!r}")
    body = text[2:]
    try:
        int(body, 16)
    except ValueError as exc:
        raise ValueError(f"Not an EVM address: {address!r}") from exc
    if body != body.lower() and body != body.upper() and not _is_checksummed(text):
        raise ValueError(f"address checksum does not match; re-copy it: {address!r}")
    return text.lower()


def _is_checksummed(address: str) -> bool:
    try:
        from eth_utils import is_checksum_address
    except ImportError:  # pragma: no cover - eth_utils ships with eth-account
        return True
    return bool(is_checksum_address(address))


def checksum_address(address: str) -> str:
    """EIP-55 checksum for display; falls back to lowercase when unavailable."""
    normalized = normalize_address(address)
    try:
        from eth_utils import to_checksum_address

        return str(to_checksum_address(normalized))
    except Exception:  # pragma: no cover - eth_utils ships with eth-account
        return normalized
