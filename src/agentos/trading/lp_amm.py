"""Liquidity positions outside Uniswap V4: Uniswap v2/v3, Aero and Aerodrome.

``lp.py`` reads Uniswap V4. This module reads the other venues a wallet on
Base or Robinhood Chain is likely to have liquidity in, and returns them in
the same ``Position`` shape (``docs/lp-book.md``):

* **concentrated** -- Uniswap v3 and Aero / Aerodrome Slipstream. A position is an
  NFT of an *enumerable* manager, so a wallet's ids are read straight off the
  chain (``balanceOf`` + ``tokenOfOwnerByIndex``): no log scan, no indexer.
* **pair** -- Uniswap v2 and Aero / Aerodrome basic pools. A position is a balance of the
  pair's own ERC-20, so the candidates are the tokens the ledger already knows
  the wallet holds; each is checked against its factory once and remembered.
* **staked** -- an Aero or Aerodrome position deposited in a gauge no longer
  sits in the wallet. The gauges can be listed (Aero: one per pool of its
  factories, at a computable address; Aerodrome: through its Voter), so a
  *deep* read asks each one; between deep reads only the gauges a wallet was
  last seen in are asked.

The whole read is a handful of Multicall3 round trips. What never changes
(which token is a pair, where a pool lives, the gauge directory, the ids a
wallet held when its count and newest id were these) is kept in an
:class:`AmmStore` under the state directory, so a wallet with nothing to
show costs one round trip per chain.

Every address below was checked on chain (2026-11-18): each position manager
answers ``factory()`` with the factory listed beside it, and Aero's are the
ones its security page lists (``app.aero.xyz/docs/security``).
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from agentos.trading import lp
from agentos.trading.chains import NATIVE_ADDRESS

log = structlog.get_logger(__name__)

UNISWAP_V4 = "uniswap_v4"
UNISWAP_V3 = "uniswap_v3"
UNISWAP_V2 = "uniswap_v2"
AERO_CL = "aero_slipstream"
AERO_V2 = "aero_basic"
AERODROME_CL = "aerodrome_slipstream"
AERODROME_V2 = "aerodrome_v2"

#: Display names, and the order protocols are listed in. Aerodrome became
#: Aero on 2026-10-21: the old pools still hold liquidity (their gauges
#: stopped earning that day), so both generations are read.
PROTOCOL_NAMES: dict[str, str] = {
    UNISWAP_V4: "Uniswap V4",
    UNISWAP_V3: "Uniswap V3",
    UNISWAP_V2: "Uniswap V2",
    AERO_CL: "Aero Slipstream",
    AERO_V2: "Aero Basic",
    AERODROME_CL: "Aerodrome Slipstream",
    AERODROME_V2: "Aerodrome V2",
}
CONCENTRATED = "concentrated"
PAIR = "pair"
#: A gauge directory read off a Voter holds both kinds.
VOTER = "voter"

#: NFTs enumerated per wallet and manager (the newest); more is flagged partial.
MAX_NFTS_PER_WALLET = 300
#: Tokens of a wallet checked for being a pair in one read; the rest wait for
#: the next one (an airdrop-spam wallet holds hundreds).
MAX_NEW_TOKENS = 200
#: Seconds the price lookup of one read may take before rows go unpriced.
PRICE_TIMEOUT_S = 12.0
STORE_VERSION = 2

_Q128 = 1 << 128
_U256 = 1 << 256


@dataclass(frozen=True)
class ClDeployment:
    """One concentrated-liquidity deployment: an NFT manager and its pool factory."""

    protocol: str
    manager: str
    factory: str
    #: Slipstream keys a pool by tick spacing; Uniswap v3 by fee tier.
    by_spacing: bool = False


@dataclass(frozen=True)
class PairDeployment:
    protocol: str
    factory: str
    #: How the factory names a pair: ``getPair(a, b)`` (Uniswap),
    #: ``getPool(a, b, stable)`` (Aerodrome) or ``getPool(a, b)`` (Aero).
    lookup: str = "getPair"
    #: Aero has one factory per curve; ``None`` where the pool itself says.
    stable: bool | None = None
    #: How unclaimed fees of an unstaked balance are read: ``None`` (they
    #: compound into the reserves), ``"index"`` or ``"pending"``.
    fees: str | None = None
    fee_pct: str | None = None


@dataclass(frozen=True)
class GaugeSource:
    """Where one family of gauges is listed, and how a stake in it is read."""

    key: str
    #: ``VOTER``: ``lister.pools(i)`` / ``lister.gauges(pool)``, the kind told
    #: by the gauge. Otherwise ``lister`` is a pool factory (``allPools(i)``)
    #: and the gauge address is ``gauge_factory.computeGaugeAddress(pool)``.
    kind: str
    lister: str
    reward_token: str
    gauge_factory: str = ""
    manager: str = ""
    #: Aero gauges answer ``earned(account, ids[])``; Aerodrome's ``earned(account, id)``.
    batched_rewards: bool = False
    #: No gauge is added and nothing new is staked: asked once per wallet.
    frozen: bool = False
    #: A token whose price stands in for the reward token's (xAERO redeems 1:1 for AERO).
    priced_as: str = ""


@dataclass(frozen=True)
class Deployments:
    cl: tuple[ClDeployment, ...] = ()
    pairs: tuple[PairDeployment, ...] = ()
    gauges: tuple[GaugeSource, ...] = ()


_AERO_MANAGER = "0x4857db576ee0e45f10525ec59527bfd10beec60b"
_AERO_STABLE = "0x78589e876edcebdce72ee3436ab3c9ad3a7cef2d"
_AERO_VOLATILE = "0x8e6a9fcd1d638cc760ffba397605fe3355de76d2"
_AERO_CL_GAUGES = "0xf437f62053acb121665b8fdbfea8f214fb8e589d"


def _aero(
    cl_factory: str, stable_gauges: str, volatile_gauges: str, receipt: str, priced_as: str = ""
) -> tuple[ClDeployment, tuple[PairDeployment, ...], tuple[GaugeSource, ...]]:
    """Aero's deployment on one chain (the manager and pool factories are the same everywhere)."""
    return (
        ClDeployment(AERO_CL, _AERO_MANAGER, cl_factory, by_spacing=True),
        (
            PairDeployment(AERO_V2, _AERO_STABLE, lookup="getPool", stable=True, fees="pending"),
            PairDeployment(AERO_V2, _AERO_VOLATILE, lookup="getPool", stable=False, fees="pending"),
        ),
        (
            GaugeSource(
                "aero-cl",
                CONCENTRATED,
                cl_factory,
                receipt,
                gauge_factory=_AERO_CL_GAUGES,
                manager=_AERO_MANAGER,
                batched_rewards=True,
                priced_as=priced_as,
            ),
            GaugeSource(
                "aero-stable",
                PAIR,
                _AERO_STABLE,
                receipt,
                gauge_factory=stable_gauges,
                priced_as=priced_as,
            ),
            GaugeSource(
                "aero-volatile",
                PAIR,
                _AERO_VOLATILE,
                receipt,
                gauge_factory=volatile_gauges,
                priced_as=priced_as,
            ),
        ),
    )


_BASE_AERO = _aero(
    "0x9cb220581b91b0c83223012fe69688b92ad07a76",
    "0x1e0f776e88649285f99e65fa599ad6e0f9d97800",
    "0x9fbcf2df0dc553ce40ef63963bff465b76792b7e",
    "0x6cdda0e730744dc6ff67654b35350adad05ecee7",
    priced_as="0x3f5bf3edac59d650d8fdf9657a71f2f904a3a4ee",
)
_ROBINHOOD_AERO = _aero(
    "0xc63d7338c178e805c544b8f897066bd3bbcbe136",
    "0x1733cd126a227066963e9b636ad49898d1224741",
    "0xcce24256a8a6070a9e308f23e044c87c343f98cd",
    "0x9e148c5716b5aadcfd3b71ceae9ee7a9e22c29e2",
)

DEPLOYMENTS: dict[int, Deployments] = {
    8453: Deployments(
        cl=(
            ClDeployment(
                UNISWAP_V3,
                "0x03a520b32c04bf3beef7beb72e919cf822ed34f1",
                "0x33128a8fc17869897dce68ed026d694621f6fdfd",
            ),
            _BASE_AERO[0],
            # Aerodrome redeployed Slipstream twice; positions live in all three.
            ClDeployment(
                AERODROME_CL,
                "0x827922686190790b37229fd06084350e74485b72",
                "0x5e7bb104d84c7cb9b682aac2f3d509f5f406809a",
                by_spacing=True,
            ),
            ClDeployment(
                AERODROME_CL,
                "0xa990c6a764b73bf43cee5bb40339c3322fb9d55f",
                "0xade65c38cd4849adba595a4323a8c7ddfe89716a",
                by_spacing=True,
            ),
            ClDeployment(
                AERODROME_CL,
                "0xe1f8cd9ac4e4a65f54f38a5cdafca44f6dd68b53",
                "0xf8f2eb4940cfe7d13603dddd87f123820fc061ef",
                by_spacing=True,
            ),
        ),
        pairs=(
            PairDeployment(
                UNISWAP_V2, "0x8909dc15e40173ff4699343b6eb8132c65e18ec6", fee_pct="0.3%"
            ),
            *_BASE_AERO[1],
            PairDeployment(
                AERODROME_V2,
                "0x420dd381b31aef6683db6b902084cb0ffece40da",
                lookup="getPoolByCurve",
                fees="index",
            ),
        ),
        gauges=(
            *_BASE_AERO[2],
            GaugeSource(
                "aerodrome",
                VOTER,
                "0x16613524e02ad97edfef371bc883f2f5d6c480a5",
                reward_token="0x940181a94a35a4569e4529a3cdfb74e38fd98631",
                frozen=True,
            ),
        ),
    ),
    4663: Deployments(
        cl=(
            ClDeployment(
                UNISWAP_V3,
                "0x73991a25c818bf1f1128deaab1492d45638de0d3",
                "0x1f7d7550b1b028f7571e69a784071f0205fd2efa",
            ),
            _ROBINHOOD_AERO[0],
        ),
        pairs=(
            PairDeployment(
                UNISWAP_V2, "0x8bceaa40b9acdfaedf85adf4ff01f5ad6517937f", fee_pct="0.3%"
            ),
            *_ROBINHOOD_AERO[1],
        ),
        gauges=_ROBINHOOD_AERO[2],
    ),
}


# ── ABI fragments (only what is read) ───────────────────────────────────────


def _fn(name: str, inputs: Iterable[str], outputs: Iterable[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "name": name,
        "stateMutability": "view",
        "inputs": [{"type": t} for t in inputs],
        "outputs": [{"type": t} for t in outputs],
    }


_POSITION_TAIL = ["int24", "int24", "uint128", "uint256", "uint256", "uint128", "uint128"]
NFT_ABI = [
    _fn("balanceOf", ["address"], ["uint256"]),
    _fn("tokenOfOwnerByIndex", ["address", "uint256"], ["uint256"]),
]
#: ``positions(id)``: nonce, operator, token0, token1, fee, tickLower, tickUpper,
#: liquidity, feeGrowthInside0Last, feeGrowthInside1Last, tokensOwed0, tokensOwed1.
V3_MANAGER_ABI = [
    _fn("positions", ["uint256"], ["uint96", "address", "address", "address", "uint24"]),
]
V3_MANAGER_ABI[0]["outputs"] += [{"type": t} for t in _POSITION_TAIL]
#: Slipstream's has ``int24 tickSpacing`` where Uniswap has ``uint24 fee``.
SLIPSTREAM_MANAGER_ABI = [
    _fn("positions", ["uint256"], ["uint96", "address", "address", "address", "int24"]),
]
SLIPSTREAM_MANAGER_ABI[0]["outputs"] += [{"type": t} for t in _POSITION_TAIL]

_POOL_COMMON = [
    _fn("liquidity", [], ["uint128"]),
    _fn("feeGrowthGlobal0X128", [], ["uint256"]),
    _fn("feeGrowthGlobal1X128", [], ["uint256"]),
    _fn("fee", [], ["uint24"]),
    _fn("tickSpacing", [], ["int24"]),
]
V3_POOL_ABI = [
    _fn("slot0", [], ["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"]),
    # liquidityGross, liquidityNet, feeGrowthOutside0, feeGrowthOutside1, ...
    _fn(
        "ticks",
        ["int24"],
        ["uint128", "int128", "uint256", "uint256", "int56", "uint160", "uint32", "bool"],
    ),
    *_POOL_COMMON,
]
SLIPSTREAM_POOL_ABI = [
    _fn("slot0", [], ["uint160", "int24", "uint16", "uint16", "uint16", "bool"]),
    # liquidityGross, liquidityNet, stakedLiquidityNet, feeGrowthOutside0, feeGrowthOutside1, ...
    _fn(
        "ticks",
        ["int24"],
        [
            "uint128",
            "int128",
            "int128",
            "uint256",
            "uint256",
            "uint256",
            "int56",
            "uint160",
            "uint32",
            "bool",
        ],
    ),
    *_POOL_COMMON,
]
V3_FACTORY_ABI = [_fn("getPool", ["address", "address", "uint24"], ["address"])]
SLIPSTREAM_FACTORY_ABI = [_fn("getPool", ["address", "address", "int24"], ["address"])]
V2_FACTORY_ABI = [_fn("getPair", ["address", "address"], ["address"])]
POOL_FACTORY_ABI = [_fn("getPool", ["address", "address"], ["address"])]
CURVE_FACTORY_ABI = [_fn("getPool", ["address", "address", "bool"], ["address"])]
LISTING_ABI = [
    _fn("allPoolsLength", [], ["uint256"]),
    _fn("allPools", ["uint256"], ["address"]),
]
PAIR_ABI = [
    _fn("factory", [], ["address"]),
    _fn("token0", [], ["address"]),
    _fn("token1", [], ["address"]),
    _fn("stable", [], ["bool"]),
    # Uniswap packs (uint112, uint112, uint32), the others three uint256: three words either way.
    _fn("getReserves", [], ["uint256", "uint256", "uint256"]),
    _fn("totalSupply", [], ["uint256"]),
    _fn("balanceOf", ["address"], ["uint256"]),
    _fn("claimable0", ["address"], ["uint256"]),
    _fn("claimable1", ["address"], ["uint256"]),
    _fn("index0", [], ["uint256"]),
    _fn("index1", [], ["uint256"]),
    _fn("supplyIndex0", ["address"], ["uint256"]),
    _fn("supplyIndex1", ["address"], ["uint256"]),
    _fn("pendingFees", ["address"], ["uint256", "uint256"]),
]
VOTER_ABI = [
    _fn("length", [], ["uint256"]),
    _fn("pools", ["uint256"], ["address"]),
    _fn("gauges", ["address"], ["address"]),
]
GAUGE_FACTORY_ABI = [_fn("computeGaugeAddress", ["address"], ["address"])]
CL_GAUGE_ABI = [
    _fn("nft", [], ["address"]),
    _fn("stakedValues", ["address"], ["uint256[]"]),
    # Aerodrome: what a position earned since its last checkpoint, and what
    # was booked for it before that.
    _fn("earned", ["address", "uint256"], ["uint256"]),
    _fn("rewards", ["uint256"], ["uint256"]),
    _fn("deferredEmissions", ["address"], ["uint256"]),
]
#: Aero: one call for any set of ids; the answer includes the account's
#: deferred emissions, which are read apart and taken off each position.
CL_GAUGE_BATCHED_ABI = [_fn("earned", ["address", "uint256[]"], ["uint256"])]
V2_GAUGE_ABI = [
    _fn("balanceOf", ["address"], ["uint256"]),
    _fn("earned", ["address"], ["uint256"]),
]


# ── what a chain's reads learnt for good ────────────────────────────────────


@dataclass
class AmmStore:
    """Facts that do not change, kept across reads (and restarts) per chain.

    ``pairs``: token -> pair info, or ``None`` for "not a pair" (so a wallet's
    ordinary tokens are asked once, not every minute). ``pools``: pool key ->
    address. ``gauges``: the gauge directory, ``cursors`` saying how far each
    source's listing was read. ``nfts``: per manager and wallet, the ids it
    held when its count and newest id were what they are now, and which of
    them are spent.
    """

    path: Path | None = None
    pairs: dict[str, dict[str, Any] | None] = field(default_factory=dict)
    pools: dict[str, str] = field(default_factory=dict)
    gauges: list[dict[str, str]] = field(default_factory=list)
    cursors: dict[str, int] = field(default_factory=dict)
    nfts: dict[str, dict[str, Any]] = field(default_factory=dict)
    changed: bool = False
    _by_gauge: dict[str, dict[str, str]] = field(default_factory=dict, repr=False)

    @classmethod
    def load(cls, path: Path | None) -> AmmStore:
        store = cls(path=path)
        if path is None:
            return store
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return store
        if not isinstance(data, dict) or data.get("version") != STORE_VERSION:
            return store
        pairs = data.get("pairs")
        pools = data.get("pools")
        gauges = data.get("gauges")
        cursors = data.get("cursors")
        nfts = data.get("nfts")
        if isinstance(pairs, dict):
            store.pairs = {str(k): (v if isinstance(v, dict) else None) for k, v in pairs.items()}
        if isinstance(pools, dict):
            store.pools = {str(k): str(v) for k, v in pools.items()}
        if isinstance(gauges, list) and isinstance(cursors, dict):
            store.gauges = [g for g in gauges if isinstance(g, dict) and g.get("gauge")]
            store.cursors = {str(k): int(v) for k, v in cursors.items()}
        if isinstance(nfts, dict):
            store.nfts = {str(k): v for k, v in nfts.items() if isinstance(v, dict)}
        return store

    def save(self) -> None:
        if self.path is None or not self.changed:
            return
        body = {
            "version": STORE_VERSION,
            "pairs": self.pairs,
            "pools": self.pools,
            "gauges": self.gauges,
            "cursors": self.cursors,
            "nfts": self.nfts,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".amm-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(body, handle, separators=(",", ":"))
            os.replace(tmp, self.path)
            self.changed = False
        except OSError as exc:
            log.debug("trading.lp_store_save_failed", error=str(exc))

    def add_gauge(self, entry: dict[str, str]) -> None:
        if entry["gauge"] not in self._index():
            self.gauges.append(entry)
            self._by_gauge[entry["gauge"]] = entry
        self.changed = True

    def gauge(self, address: str) -> dict[str, str] | None:
        return self._index().get(address.lower())

    def _index(self) -> dict[str, dict[str, str]]:
        if len(self._by_gauge) != len(self.gauges):
            self._by_gauge = {g["gauge"]: g for g in self.gauges}
        return self._by_gauge


@dataclass
class AmmRead:
    """One chain's classic-AMM positions, and what the next read should know."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    #: (owner, gauge) pairs with something staked, lower-cased.
    stakes: set[tuple[str, str]] = field(default_factory=set)
    #: Every gauge of the live sources was asked (trivially so with none).
    deep: bool = False
    #: ...and every gauge of the frozen ones as well.
    full: bool = False


class _Batch:
    """Named calls in, results by name out: one multicall round trip."""

    def __init__(self, env: lp.ChainEnv) -> None:
        self.env = env
        self._keys: list[Any] = []
        self._seen: set[Any] = set()
        self._calls: list[dict[str, Any]] = []

    def add(self, key: Any, address: str, abi: list[dict[str, Any]], name: str, *args: Any) -> None:
        """Queue one call; a key already queued is not asked twice."""
        if key in self._seen:
            return
        self._seen.add(key)
        self._keys.append(key)
        self._calls.append(
            {
                "address": _cs(self.env, address),
                "abi": abi,
                "functionName": name,
                "args": list(args),
            }
        )

    def run(self) -> dict[Any, dict[str, Any]]:
        if not self._calls:
            return {}
        return dict(zip(self._keys, lp.multicall(self.env, self._calls), strict=True))


def _cs(env: lp.ChainEnv, address: str) -> str:
    return str(env.lib.hexutil.checksum_address(address))


def _ok(result: dict[str, Any] | None) -> bool:
    return result is not None and result.get("status") == "success"


def _faulted(result: dict[str, Any] | None) -> bool:
    """The node failed the call (as opposed to the contract reverting it)."""
    return result is None or (
        result.get("status") != "success" and result.get("error") == lp._NODE_FAULT
    )


def _address(result: dict[str, Any] | None) -> str | None:
    if not _ok(result):
        return None
    assert result is not None
    value = str(result["result"]).lower()
    return None if int(value, 16) == 0 else value


def _int(result: dict[str, Any] | None, default: int = 0) -> int:
    if not _ok(result):
        return default
    assert result is not None
    return int(result["result"])


def _pool_slot(dep: ClDeployment, token0: str, token1: str, key: int) -> str:
    return f"{dep.factory}:{token0.lower()}:{token1.lower()}:{key}"


def _nft_slot(manager: str, owner: str) -> str:
    return f"{manager}:{owner}"


# ── the gauge directory ─────────────────────────────────────────────────────


def update_gauges(
    env: lp.ChainEnv, dep: Deployments, source: GaugeSource, store: AmmStore, length: int
) -> None:
    """Extend ``source``'s part of the gauge directory to ``length`` listed pools.

    Built once (two or three multicalls over every pool the source lists),
    then only the pools added since are asked. The cursor stops at the first
    index the node failed on, so a flaky read is retried, not skipped for good.
    """
    cursor = store.cursors.get(source.key, 0)
    if length <= cursor:
        return
    by_voter = source.kind == VOTER
    indexes = list(range(cursor, length))
    batch = _Batch(env)
    for i in indexes:
        if by_voter:
            batch.add(i, source.lister, VOTER_ABI, "pools", i)
        else:
            batch.add(i, source.lister, LISTING_ABI, "allPools", i)
    pools = batch.run()
    batch = _Batch(env)
    for i in indexes:
        pool = _address(pools.get(i))
        if pool and by_voter:
            batch.add(i, source.lister, VOTER_ABI, "gauges", _cs(env, pool))
        elif pool:
            batch.add(
                i, source.gauge_factory, GAUGE_FACTORY_ABI, "computeGaugeAddress", _cs(env, pool)
            )
    gauges = batch.run()
    kinds: dict[Any, dict[str, Any]] = {}
    if by_voter:
        batch = _Batch(env)
        for i in indexes:
            gauge = _address(gauges.get(i))
            if gauge:
                batch.add(i, gauge, CL_GAUGE_ABI, "nft")
        kinds = batch.run()
    managers = {d.manager for d in dep.cl}
    for i in indexes:
        if _faulted(pools.get(i)):
            break
        pool = _address(pools.get(i))
        gauge = None
        if pool is not None:
            if _faulted(gauges.get(i)):
                break
            gauge = _address(gauges.get(i))
        if pool is not None and gauge is not None:
            kind, manager = source.kind, source.manager
            if by_voter:
                told = kinds.get(i)
                if _faulted(told):
                    break
                # ``nft()`` reverts on a v2 gauge: its stake is the pool's ERC-20.
                kind, manager = (CONCENTRATED, _address(told) or "") if _ok(told) else (PAIR, "")
            # A gauge over a manager this module does not read is left out.
            if kind == PAIR or manager in managers:
                store.add_gauge(
                    {
                        "pool": pool,
                        "gauge": gauge,
                        "kind": kind,
                        "manager": manager,
                        "source": source.key,
                    }
                )
        store.cursors[source.key] = i + 1
    store.changed = True


# ── the read ────────────────────────────────────────────────────────────────


def read_positions(
    env: lp.ChainEnv,
    owners: list[str],
    *,
    held: dict[str, set[str]] | None = None,
    stakes: Iterable[tuple[str, str]] = (),
    deep: bool = False,
    full: bool = False,
    store: AmmStore | None = None,
) -> AmmRead:
    """Every Uniswap v2/v3, Aero and Aerodrome position of ``owners`` on ``env``'s chain.

    ``held``: owner -> ERC-20s the ledger says it holds (pair candidates).
    ``stakes``: (owner, gauge) pairs seen staked last time. With ``deep`` every
    gauge that can still take a stake is asked instead, and emptied NFTs are
    looked at again; ``full`` adds the frozen gauges (Aerodrome's, which no
    one stakes into any more). ``env`` must be started.
    """
    dep = DEPLOYMENTS.get(env.spec.chain_id)
    out = AmmRead()
    owners = list(dict.fromkeys(o.lower() for o in owners))
    if dep is None or not owners:
        return out
    store = store if store is not None else AmmStore()
    held = {o.lower(): {t.lower() for t in ts} for o, ts in (held or {}).items()}
    known = {(o.lower(), g.lower()) for o, g in stakes}
    try:
        _Reader(env, dep, owners, held, known, deep or full, full, store, out).run()
    finally:
        store.save()
    return out


class _Reader:
    """One read, in four multicall rounds (see :func:`read_positions`)."""

    def __init__(
        self,
        env: lp.ChainEnv,
        dep: Deployments,
        owners: list[str],
        held: dict[str, set[str]],
        stakes: set[tuple[str, str]],
        deep: bool,
        full: bool,
        store: AmmStore,
        out: AmmRead,
    ) -> None:
        self.env = env
        self.dep = dep
        self.owners = owners
        self.held = held
        self.stakes = stakes
        self.deep = deep
        self.full = full
        self.store = store
        self.out = out
        self.by_manager = {d.manager: d for d in dep.cl}
        self.pair_factories = {d.factory: d for d in dep.pairs}
        self.sources = {s.key: s for s in dep.gauges}
        #: (manager, owner) -> the ids it holds (``None`` until enumerated).
        self.listing: dict[tuple[str, str], list[int] | None] = {}
        #: (manager, tokenId) -> (owner, gauge entry or ``None``).
        self.nft_jobs: dict[tuple[str, int], tuple[str, dict[str, str] | None]] = {}
        #: (owner, gauge address) -> staked LP-token balance.
        self.pair_stakes: dict[tuple[str, str], int] = {}
        self.pair_jobs: list[tuple[str, str]] = []
        self.positions: dict[tuple[str, int], dict[str, Any]] = {}

    def name(self, protocol: str) -> str:
        return PROTOCOL_NAMES[protocol]

    def cs(self, address: str) -> str:
        return _cs(self.env, address)

    def partial(self, message: str) -> None:
        self.env.partial = True
        self.env.warn(f"{self.env.spec.name}: {message}")

    # -- round 1 ------------------------------------------------------------

    def run(self) -> None:
        env, store = self.env, self.store
        scanned = [s for s in self.dep.gauges if self.deep and (self.full or not s.frozen)]
        candidates = sorted(
            {t for o in self.owners for t in self.held.get(o, ())} - store.pairs.keys()
        )
        if len(candidates) > MAX_NEW_TOKENS:
            self.partial(
                f"{len(candidates) - MAX_NEW_TOKENS} held token(s) are not yet checked for "
                "being LP tokens; the next refresh continues"
            )
            candidates = candidates[:MAX_NEW_TOKENS]
        batch = _Batch(env)
        for d in self.dep.cl:
            for owner in self.owners:
                slot = (d.manager, owner)
                batch.add(("count", *slot), d.manager, NFT_ABI, "balanceOf", self.cs(owner))
                cached = store.nfts.get(_nft_slot(*slot))
                if cached and int(cached.get("count") or 0) > 0:
                    batch.add(
                        ("last", *slot),
                        d.manager,
                        NFT_ABI,
                        "tokenOfOwnerByIndex",
                        self.cs(owner),
                        int(cached["count"]) - 1,
                    )
        for source in scanned:
            if source.kind == VOTER:
                batch.add(("listed", source.key), source.lister, VOTER_ABI, "length")
            else:
                batch.add(("listed", source.key), source.lister, LISTING_ABI, "allPoolsLength")
        for token in candidates:
            for fn in ("factory", "token0", "token1"):
                batch.add(("kind", token, fn), token, PAIR_ABI, fn)
        first = batch.run()

        complete: set[str] = set()
        for source in scanned:
            listed = first.get(("listed", source.key))
            if _ok(listed):
                update_gauges(env, self.dep, source, store, _int(listed))
                if store.cursors.get(source.key, 0) >= _int(listed):
                    complete.add(source.key)
        live = {s.key for s in self.dep.gauges if not s.frozen}
        frozen = {s.key for s in self.dep.gauges if s.frozen}
        self.out.deep = self.deep and live <= complete
        self.out.full = self.full and self.out.deep and frozen <= complete

        second = self.round_two(first, candidates, complete)
        third = self.round_three(second)
        fourth = self.round_four(third)
        self.build(third, fourth)

    # -- round 2: ids, pair checks, stakes --------------------------------------

    def round_two(
        self, first: dict[Any, dict[str, Any]], candidates: list[str], scanned: set[str]
    ) -> dict[Any, dict[str, Any]]:
        env, store = self.env, self.store
        batch = _Batch(env)
        for d in self.dep.cl:
            for owner in self.owners:
                self.enumerate(batch, first, d, owner)
        proposed: dict[str, tuple[PairDeployment, str, str]] = {}
        for token in candidates:
            token0 = _address(first.get(("kind", token, "token0")))
            token1 = _address(first.get(("kind", token, "token1")))
            factory = _address(first.get(("kind", token, "factory")))
            pair_dep = self.pair_factories.get(factory or "")
            if pair_dep is None or token0 is None or token1 is None:
                if not any(_faulted(first.get(("kind", token, n))) for n in ("factory", "token0")):
                    store.pairs[token] = None
                    store.changed = True
                continue
            # Any contract can claim a factory: the factory is asked whether
            # this is its pair for those two tokens before a reserve is believed.
            proposed[token] = (pair_dep, token0, token1)
            args = (self.cs(token0), self.cs(token1))
            if pair_dep.lookup == "getPoolByCurve":
                for curve in (True, False):
                    batch.add(
                        ("pair", token, curve),
                        pair_dep.factory,
                        CURVE_FACTORY_ABI,
                        "getPool",
                        *args,
                        curve,
                    )
            elif pair_dep.lookup == "getPool":
                batch.add(
                    ("pair", token, False), pair_dep.factory, POOL_FACTORY_ABI, "getPool", *args
                )
            else:
                batch.add(
                    ("pair", token, False), pair_dep.factory, V2_FACTORY_ABI, "getPair", *args
                )

        asked: list[tuple[str, dict[str, str]]] = [
            (owner, gauge)
            for gauge in store.gauges
            if gauge.get("source") in scanned
            for owner in self.owners
        ]
        for owner, address in sorted(self.stakes):
            known = store.gauge(address)
            if known is not None and owner in self.owners and known.get("source") not in scanned:
                asked.append((owner, known))
        for owner, gauge in asked:
            key = ("staked", gauge["gauge"], owner)
            if gauge["kind"] == CONCENTRATED:
                batch.add(key, gauge["gauge"], CL_GAUGE_ABI, "stakedValues", self.cs(owner))
            else:
                batch.add(key, gauge["gauge"], V2_GAUGE_ABI, "balanceOf", self.cs(owner))
        second = batch.run()

        for token, (pair_dep, token0, token1) in proposed.items():
            answers = {c: second.get(("pair", token, c)) for c in (True, False)}
            matched: bool | None = next(
                (c for c, a in answers.items() if _address(a) == token), None
            )
            if matched is None:
                if not any(a is not None and _faulted(a) for a in answers.values()):
                    store.pairs[token] = None
                    store.changed = True
                continue
            store.pairs[token] = {
                "protocol": pair_dep.protocol,
                "factory": pair_dep.factory,
                "token0": token0,
                "token1": token1,
                "stable": matched if pair_dep.lookup == "getPoolByCurve" else pair_dep.stable,
            }
            store.changed = True

        for (manager, owner), ids in list(self.listing.items()):
            if ids is None:
                self.adopt_listing(first, second, manager, owner)

        for owner, gauge in asked:
            result = second.get(("staked", gauge["gauge"], owner))
            if not _ok(result):
                if _faulted(result):
                    self.partial(f"a gauge could not be read for {self.cs(owner)}")
                    # Keep asking it: one failed read must not forget a stake.
                    if (owner, gauge["gauge"]) in self.stakes:
                        self.out.stakes.add((owner, gauge["gauge"]))
                continue
            assert result is not None
            if gauge["kind"] == CONCENTRATED:
                ids = [int(i) for i in result["result"] or []]
                if ids:
                    self.out.stakes.add((owner, gauge["gauge"]))
                for token_id in ids:
                    self.nft_jobs[(gauge["manager"], token_id)] = (owner, gauge)
            elif _int(result) > 0:
                self.out.stakes.add((owner, gauge["gauge"]))
                self.pair_stakes[(owner, gauge["gauge"])] = _int(result)
        return second

    def enumerate(
        self, batch: _Batch, first: dict[Any, dict[str, Any]], d: ClDeployment, owner: str
    ) -> None:
        """Decide how ``owner``'s ids under ``d`` are learnt: cache, or one call per index."""
        slot = (d.manager, owner)
        counted = first.get(("count", *slot))
        cached = self.store.nfts.get(_nft_slot(*slot)) or {}
        remembered = [int(i) for i in cached.get("ids") or []]
        if not _ok(counted):
            self.partial(
                f"could not read how many {self.name(d.protocol)} positions {self.cs(owner)} holds"
            )
            self.listing[slot] = remembered
            return
        count = _int(counted)
        if count > MAX_NFTS_PER_WALLET:
            self.partial(
                f"{self.cs(owner)} holds {count} {self.name(d.protocol)} NFTs; the newest "
                f"{MAX_NFTS_PER_WALLET} are read"
            )
        if count == 0:
            self.listing[slot] = []
            return
        last = first.get(("last", *slot))
        if (
            count == int(cached.get("count") or 0)
            and _ok(last)
            and _int(last) == int(cached.get("last") or -1)
        ):
            # Same count and same newest id: an enumerable set that lost or
            # gained anything differs in one of the two.
            self.listing[slot] = remembered
            return
        self.listing[slot] = None
        for index in range(max(0, count - MAX_NFTS_PER_WALLET), count):
            batch.add(
                ("id", *slot, index),
                d.manager,
                NFT_ABI,
                "tokenOfOwnerByIndex",
                self.cs(owner),
                index,
            )

    def adopt_listing(
        self,
        first: dict[Any, dict[str, Any]],
        second: dict[Any, dict[str, Any]],
        manager: str,
        owner: str,
    ) -> None:
        count = _int(first.get(("count", manager, owner)))
        found: list[int] = []
        complete = True
        for index in range(max(0, count - MAX_NFTS_PER_WALLET), count):
            result = second.get(("id", manager, owner, index))
            if _ok(result):
                found.append(_int(result))
            else:
                complete = False
        self.listing[(manager, owner)] = found
        if not complete:
            self.partial(
                f"some {self.name(self.by_manager[manager].protocol)} positions of "
                f"{self.cs(owner)} could not be listed"
            )
            return
        previous = self.store.nfts.get(_nft_slot(manager, owner)) or {}
        kept = set(found)
        self.store.nfts[_nft_slot(manager, owner)] = {
            "count": count,
            "last": found[-1] if found else -1,
            "ids": found,
            "spent": [i for i in previous.get("spent") or [] if i in kept],
        }
        self.store.changed = True

    # -- round 3: the positions themselves --------------------------------------

    def round_three(self, second: dict[Any, dict[str, Any]]) -> dict[Any, dict[str, Any]]:
        env, store = self.env, self.store
        for (manager, owner), ids in self.listing.items():
            spent = set((store.nfts.get(_nft_slot(manager, owner)) or {}).get("spent") or [])
            for token_id in ids or []:
                # A position with nothing in it and nothing owed is asked again
                # on a deep read only: a wallet keeps years of emptied NFTs.
                if token_id in spent and not self.deep:
                    continue
                self.nft_jobs.setdefault((manager, token_id), (owner, None))
        batch = _Batch(env)
        for (manager, token_id), (owner, staked_at) in self.nft_jobs.items():
            d = self.by_manager[manager]
            abi = SLIPSTREAM_MANAGER_ABI if d.by_spacing else V3_MANAGER_ABI
            batch.add(("pos", manager, token_id), manager, abi, "positions", token_id)
            if staked_at is None:
                continue
            address = staked_at["gauge"]
            who = self.cs(owner)
            if self.sources[staked_at["source"]].batched_rewards:
                batch.add(
                    ("earned", manager, token_id),
                    address,
                    CL_GAUGE_BATCHED_ABI,
                    "earned",
                    who,
                    [token_id],
                )
                batch.add(
                    ("deferred", address, owner), address, CL_GAUGE_ABI, "deferredEmissions", who
                )
            else:
                batch.add(
                    ("earned", manager, token_id), address, CL_GAUGE_ABI, "earned", who, token_id
                )
                batch.add(("booked", manager, token_id), address, CL_GAUGE_ABI, "rewards", token_id)

        self.pair_jobs = sorted(
            (owner, token)
            for owner in self.owners
            for token in self.held.get(owner, ())
            if store.pairs.get(token)
        )
        for owner, pair in self.pair_jobs:
            info = store.pairs[pair]
            assert info is not None
            self.read_pool(batch, pair)
            who = self.cs(owner)
            batch.add(("bal", pair, owner), pair, PAIR_ABI, "balanceOf", who)
            mode = self.fee_mode(info)
            if mode == "pending":
                batch.add(("fee", pair, owner, "pendingFees"), pair, PAIR_ABI, "pendingFees", who)
            elif mode == "index":
                for fn in ("claimable0", "claimable1", "supplyIndex0", "supplyIndex1"):
                    batch.add(("fee", pair, owner, fn), pair, PAIR_ABI, fn, who)
                for fn in ("index0", "index1"):
                    batch.add(("pool", pair, fn), pair, PAIR_ABI, fn)
        for owner, address in self.pair_stakes:
            staked_in = store.gauge(address)
            assert staked_in is not None
            pair = staked_in["pool"]
            self.read_pool(batch, pair)
            if store.pairs.get(pair) is None:
                # A listed pool needs no factory check; its tokens do need reading.
                for fn in ("token0", "token1", "stable"):
                    batch.add(("pool", pair, fn), pair, PAIR_ABI, fn)
            batch.add(("gearned", address, owner), address, V2_GAUGE_ABI, "earned", self.cs(owner))
        third = batch.run()

        for _, address in self.pair_stakes:
            staked_in = store.gauge(address)
            assert staked_in is not None
            self.learn_staked_pool(third, staked_in)
        return third

    def read_pool(self, batch: _Batch, pair: str) -> None:
        for fn in ("totalSupply", "getReserves"):
            batch.add(("pool", pair, fn), pair, PAIR_ABI, fn)

    def fee_mode(self, info: dict[str, Any]) -> str | None:
        found = self.pair_factories.get(str(info.get("factory") or ""))
        return found.fees if found else None

    def learn_staked_pool(self, third: dict[Any, dict[str, Any]], gauge: dict[str, str]) -> None:
        """Remember the tokens of a pool a wallet is staked in (it may hold none of its LP)."""
        pair = gauge["pool"]
        if self.store.pairs.get(pair) is not None:
            return
        token0 = _address(third.get(("pool", pair, "token0")))
        token1 = _address(third.get(("pool", pair, "token1")))
        if token0 is None or token1 is None:
            return
        source = self.sources[gauge["source"]]
        if source.kind == VOTER:
            flag = third.get(("pool", pair, "stable"))
            legacy = next((d for d in self.dep.pairs if d.lookup == "getPoolByCurve"), None)
            if not _ok(flag) or legacy is None:
                return
            assert flag is not None
            found, stable = legacy, bool(flag["result"])
        else:
            listed = self.pair_factories.get(source.lister)
            if listed is None:
                return
            found, stable = listed, bool(listed.stable)
        self.store.pairs[pair] = {
            "protocol": found.protocol,
            "factory": found.factory,
            "token0": token0,
            "token1": token1,
            "stable": stable,
        }
        self.store.changed = True

    # -- round 4: pools of the concentrated positions ---------------------------

    def round_four(self, third: dict[Any, dict[str, Any]]) -> dict[Any, dict[str, Any]]:
        env, store = self.env, self.store
        batch = _Batch(env)
        for (manager, token_id), (owner, staked_at) in self.nft_jobs.items():
            result = third.get(("pos", manager, token_id))
            if not _ok(result):
                if _faulted(result):
                    self.partial(f"position #{token_id} could not be read")
                continue
            assert result is not None
            values = list(result["result"])
            d = self.by_manager[manager]
            pos: dict[str, Any] = {
                "tokenId": token_id,
                "owner": owner,
                "gauge": staked_at,
                "deployment": d,
                "token0": str(values[2]).lower(),
                "token1": str(values[3]).lower(),
                "key": int(values[4]),
                "tickLower": int(values[5]),
                "tickUpper": int(values[6]),
                "liquidity": int(values[7]),
                "last0": int(values[8]),
                "last1": int(values[9]),
                "owed0": int(values[10]),
                "owed1": int(values[11]),
            }
            empty = pos["liquidity"] == 0 and pos["owed0"] == 0 and pos["owed1"] == 0
            remembered = store.nfts.get(_nft_slot(manager, owner))
            if remembered is not None and staked_at is None:
                spent = list(remembered.get("spent") or [])
                if empty != (token_id in spent):
                    remembered["spent"] = (
                        [*spent, token_id] if empty else [i for i in spent if i != token_id]
                    )
                    store.changed = True
            if empty:
                continue
            self.positions[(manager, token_id)] = pos
            where = _pool_slot(d, pos["token0"], pos["token1"], pos["key"])
            if where not in store.pools:
                batch.add(
                    ("where", where),
                    d.factory,
                    SLIPSTREAM_FACTORY_ABI if d.by_spacing else V3_FACTORY_ABI,
                    "getPool",
                    self.cs(pos["token0"]),
                    self.cs(pos["token1"]),
                    pos["key"],
                )
        for key, result in batch.run().items():
            pool = _address(result)
            if pool:
                store.pools[key[1]] = pool
                store.changed = True

        batch = _Batch(env)
        for pos in self.positions.values():
            d = pos["deployment"]
            pool = store.pools.get(_pool_slot(d, pos["token0"], pos["token1"], pos["key"]))
            pos["pool"] = pool
            if pool is None:
                continue
            abi = SLIPSTREAM_POOL_ABI if d.by_spacing else V3_POOL_ABI
            for fn in (
                "slot0",
                "liquidity",
                "feeGrowthGlobal0X128",
                "feeGrowthGlobal1X128",
                "fee" if d.by_spacing else "tickSpacing",
            ):
                batch.add(("state", pool, fn), pool, abi, fn)
            for tick in (pos["tickLower"], pos["tickUpper"]):
                batch.add(("tick", pool, tick), pool, abi, "ticks", tick)
        return batch.run()

    # -- rows --------------------------------------------------------------------

    def build(self, third: dict[Any, dict[str, Any]], fourth: dict[Any, dict[str, Any]]) -> None:
        env, store = self.env, self.store
        tokens: list[str] = []
        for pos in self.positions.values():
            tokens += [pos["token0"], pos["token1"]]
        staked_pools = [g["pool"] for _, a in self.pair_stakes if (g := store.gauge(a))]
        for pair in [*(p for _, p in self.pair_jobs), *staked_pools]:
            info = store.pairs.get(pair)
            if info:
                tokens += [info["token0"], info["token1"]]
        if not tokens:
            return
        for source in self.dep.gauges:
            tokens += [t for t in (source.reward_token, source.priced_as) if t]
        lp.token_metas(env, tokens)
        lp.usd_prices(env, tokens, timeout=PRICE_TIMEOUT_S)
        env.prices_frozen = True

        for (manager, token_id), pos in self.positions.items():
            reward = None
            staked_at = pos["gauge"]
            if staked_at is not None:
                source = self.sources[staked_at["source"]]
                earned = _int(third.get(("earned", manager, token_id)))
                if source.batched_rewards:
                    deferred = third.get(("deferred", staked_at["gauge"], pos["owner"]))
                    earned = max(earned - _int(deferred), 0)
                else:
                    earned += _int(third.get(("booked", manager, token_id)))
                reward = (source, earned)
            row = _concentrated_row(env, pos, fourth, reward)
            if row is not None:
                self.out.rows.append(row)
        for owner, pair in self.pair_jobs:
            info = store.pairs[pair]
            assert info is not None
            balance = _int(third.get(("bal", pair, owner)))
            if balance <= 0:
                continue
            fees = _pair_fees(third, pair, owner, balance, self.fee_mode(info))
            found = self.pair_factories.get(str(info.get("factory") or ""))
            row = _pair_row(env, info, found, pair, owner, balance, third, fees=fees)
            if row is not None:
                self.out.rows.append(row)
        for (owner, address), balance in self.pair_stakes.items():
            staked_in = store.gauge(address)
            assert staked_in is not None
            info = store.pairs.get(staked_in["pool"])
            if info is None:
                self.partial(f"a staked pool could not be read for {self.cs(owner)}")
                continue
            source = self.sources[staked_in["source"]]
            reward = (source, _int(third.get(("gearned", address, owner))))
            found = self.pair_factories.get(str(info.get("factory") or ""))
            row = _pair_row(
                env,
                info,
                found,
                staked_in["pool"],
                owner,
                balance,
                third,
                gauge=address,
                reward=reward,
            )
            if row is not None:
                self.out.rows.append(row)


def _pair_fees(
    results: dict[Any, dict[str, Any]], pair: str, owner: str, balance: int, mode: str | None
) -> tuple[int, int] | None:
    """Unclaimed trading fees of an unstaked pair balance (``None``: nothing to claim apart).

    Aero's pool answers ``pendingFees``. Aerodrome's books ``claimable`` only
    when a balance moves; what accrued since is the balance times the growth
    of the pool's fee index.
    """
    if mode == "pending":
        pending = results.get(("fee", pair, owner, "pendingFees"))
        if not _ok(pending):
            return None
        assert pending is not None
        return int(pending["result"][0]), int(pending["result"][1])
    if mode != "index":
        return None
    parts = []
    for n in ("0", "1"):
        claimable = results.get(("fee", pair, owner, f"claimable{n}"))
        index = results.get(("pool", pair, f"index{n}"))
        supply_index = results.get(("fee", pair, owner, f"supplyIndex{n}"))
        if not (_ok(claimable) and _ok(index) and _ok(supply_index)):
            return None
        growth = max(_int(index) - _int(supply_index), 0)
        parts.append(_int(claimable) + balance * growth // 10**18)
    return parts[0], parts[1]


def _protocol_json(protocol: str, kind: str) -> dict[str, Any]:
    return {"key": protocol, "name": PROTOCOL_NAMES[protocol], "kind": kind}


def row_id(chain_id: int, protocol: str, position_id: str, owner: str) -> str:
    return f"{chain_id}:{protocol}:{position_id.lower()}:{owner.lower()}"


def _reward_json(
    env: lp.ChainEnv, reward: tuple[GaugeSource, int] | None
) -> tuple[dict[str, Any] | None, float | None]:
    """(``rewards`` block, its USD value or ``None``) for a staked position."""
    if reward is None:
        return None, 0.0
    source, raw = reward
    token = source.reward_token
    meta = lp.token_meta(env, token)
    prices = lp.usd_prices(env, [t for t in (token, source.priced_as) if t])
    price = prices.get(token.lower())
    if price is None and source.priced_as:
        price = prices.get(source.priced_as.lower())
    amount = lp.amount_json(raw, meta["decimals"], price)
    block = {
        "token": {
            "address": meta["address"],
            "symbol": meta["symbol"],
            "decimals": meta["decimals"],
            "priceUsd": price,
        },
        "amount": amount,
    }
    return block, amount["usd"]


def _concentrated_row(
    env: lp.ChainEnv,
    pos: dict[str, Any],
    fourth: dict[Any, dict[str, Any]],
    reward: tuple[GaugeSource, int] | None,
) -> dict[str, Any] | None:
    d: ClDeployment = pos["deployment"]
    pool = pos.get("pool")
    token_id = int(pos["tokenId"])
    slot0 = fourth.get(("state", pool, "slot0")) if pool else None
    if pool is None or not _ok(slot0):
        env.partial = True
        env.warn(f"{env.spec.name}: the pool of #{token_id} could not be read")
        return None
    assert slot0 is not None
    sqrt_price, tick = int(slot0["result"][0]), int(slot0["result"][1])
    if d.by_spacing:
        spacing, fee = pos["key"], _int(fourth.get(("state", pool, "fee")))
    else:
        spacing, fee = _int(fourth.get(("state", pool, "tickSpacing"))), pos["key"]
    pool_key = {
        "currency0": _cs(env, pos["token0"]),
        "currency1": _cs(env, pos["token1"]),
        "fee": fee,
        "tickSpacing": spacing,
        "hooks": NATIVE_ADDRESS,
    }
    state = {
        "poolId": _cs(env, pool),
        "poolKey": pool_key,
        "sqrtPriceX96": sqrt_price,
        "tick": tick,
        "activeLiquidity": _int(fourth.get(("state", pool, "liquidity"))),
    }
    gauge = pos["gauge"]
    fees: tuple[int, int] | None = (pos["owed0"], pos["owed1"])
    if gauge is None and pos["liquidity"] > 0:
        # A staked position's trading fees go to the gauge; only what was
        # already owed to it is the holder's.
        fees = _fees_owed(pos, tick, pool, fourth, d.by_spacing)
        if fees is None:
            env.warn(f"uncollected fees of #{token_id} could not be read")
    side = lp.with_implied_base_price(lp.make_side(env, pool_key), state, liquid_only=False)
    row = lp.position_json(env, pos, state, side, fees)
    rewards, rewards_usd = _reward_json(env, reward)
    if rewards_usd is not None and row["valueUsd"] is not None:
        row["valueUsd"] = lp.finite(row["valueUsd"] + rewards_usd)
    row.update(
        {
            "id": row_id(env.spec.chain_id, d.protocol, f"{d.manager}-{token_id}", pos["owner"]),
            "protocol": _protocol_json(d.protocol, CONCENTRATED),
            "manager": _cs(env, d.manager),
            "staked": gauge is not None,
            "gauge": _cs(env, gauge["gauge"]) if gauge else None,
            "rewards": rewards,
            "share": None,
        }
    )
    return row


def _fees_owed(
    pos: dict[str, Any],
    tick: int,
    pool: str,
    results: dict[Any, dict[str, Any]],
    slipstream: bool,
) -> tuple[int, int] | None:
    """(fees0, fees1) a v3-style position can collect now.

    ``tokensOwed`` plus what accrued since the position was last touched:
    liquidity times the growth of the fee counter inside its range. The
    counters wrap, so every subtraction is mod 2**256.
    """
    lower = results.get(("tick", pool, pos["tickLower"]))
    upper = results.get(("tick", pool, pos["tickUpper"]))
    globals_ = [results.get(("state", pool, f"feeGrowthGlobal{n}X128")) for n in ("0", "1")]
    if not (_ok(lower) and _ok(upper) and all(_ok(g) for g in globals_)):
        return None
    assert lower is not None and upper is not None
    at = 3 if slipstream else 2
    owed = []
    for n in (0, 1):
        total = _int(globals_[n])
        outside_lower = int(lower["result"][at + n])
        outside_upper = int(upper["result"][at + n])
        below = outside_lower if tick >= pos["tickLower"] else (total - outside_lower) % _U256
        above = outside_upper if tick < pos["tickUpper"] else (total - outside_upper) % _U256
        inside = (total - below - above) % _U256
        grown = (inside - pos[f"last{n}"]) % _U256
        owed.append(pos[f"owed{n}"] + pos["liquidity"] * grown // _Q128)
    return owed[0], owed[1]


def _pair_row(
    env: lp.ChainEnv,
    info: dict[str, Any],
    deployment: PairDeployment | None,
    pair: str,
    owner: str,
    balance: int,
    results: dict[Any, dict[str, Any]],
    *,
    fees: tuple[int, int] | None = None,
    gauge: str | None = None,
    reward: tuple[GaugeSource, int] | None = None,
) -> dict[str, Any] | None:
    supply = _int(results.get(("pool", pair, "totalSupply")))
    reserves = results.get(("pool", pair, "getReserves"))
    if supply <= 0 or not _ok(reserves):
        env.partial = True
        env.warn(f"{env.spec.name}: the reserves of pool {_cs(env, pair)} could not be read")
        return None
    assert reserves is not None
    reserve0, reserve1 = int(reserves["result"][0]), int(reserves["result"][1])
    protocol = str(info["protocol"])
    pool_key = {
        "currency0": _cs(env, info["token0"]),
        "currency1": _cs(env, info["token1"]),
        "fee": 0,
        "tickSpacing": 0,
        "hooks": NATIVE_ADDRESS,
    }
    side = lp.make_side(env, pool_key)
    base_reserve, quote_reserve = side.split(reserve0, reserve1)
    base_units = base_reserve / 10 ** side.base["decimals"]
    quote_units = quote_reserve / 10 ** side.quote["decimals"]
    # A side no source prices is valued at the pool's own ratio.
    if side.base_usd is None and side.quote_usd is not None and base_units > 0:
        side.base_usd = lp.finite(quote_units / base_units * side.quote_usd)
    elif side.quote_usd is None and side.base_usd is not None and quote_units > 0:
        side.quote_usd = lp.finite(base_units / quote_units * side.base_usd)

    def amounts(raw0: int, raw1: int) -> dict[str, Any]:
        base_raw, quote_raw = side.split(raw0, raw1)
        block: dict[str, Any] = {
            "base": lp.amount_json(base_raw, side.base["decimals"], side.base_usd),
            "quote": lp.amount_json(quote_raw, side.quote["decimals"], side.quote_usd),
        }
        block["usd"] = lp._sum_usd(block["base"]["usd"], block["quote"]["usd"])
        return block

    principal = amounts(reserve0 * balance // supply, reserve1 * balance // supply)
    fees_json = amounts(*fees) if fees is not None else None
    rewards, rewards_usd = _reward_json(env, reward)
    value = principal["usd"]
    for extra in (fees_json["usd"] if fees_json else 0.0, rewards_usd):
        if value is not None and extra is not None:
            value = lp.finite(value + extra)
    if info.get("stable") is None:
        fee_pct = deployment.fee_pct if deployment else None
    else:
        fee_pct = "stable" if info["stable"] else "volatile"
    return {
        "chain": lp.chain_json(env.spec),
        "tokenId": None,
        "owner": lp.wallet_json(env, owner),
        "token": side.token_json("base"),
        "quote": side.token_json("quote"),
        "pool": {
            "poolId": _cs(env, pair),
            "hook": None,
            "tickSpacing": None,
            "feePct": fee_pct,
            "tick": None,
            "liquidity": str(supply),
            "priceUsd": side.base_usd,
            "mcapUsd": None,
            "tvlUsd": amounts(reserve0, reserve1)["usd"],
        },
        "range": None,
        "status": "full-range",
        "liquidity": str(balance),
        "principal": principal,
        # Uniswap v2 fees compound into the reserves: there is nothing to collect.
        "fees": fees_json,
        "valueUsd": value,
        "band": None,
        "distancePct": None,
        "id": row_id(env.spec.chain_id, protocol, gauge or pair, owner),
        "protocol": _protocol_json(protocol, PAIR),
        "manager": None,
        "staked": gauge is not None,
        "gauge": _cs(env, gauge) if gauge else None,
        "rewards": rewards,
        "share": lp.finite(balance / supply),
    }


def v4_row(row: dict[str, Any]) -> dict[str, Any]:
    """A ``lp.position_json`` row of Uniswap V4, with the book's extra fields."""
    chain_id = int(row["chain"]["id"])
    owner = str((row.get("owner") or {}).get("address") or "")
    return {
        **row,
        "id": row_id(chain_id, UNISWAP_V4, str(row["tokenId"]), owner),
        "protocol": _protocol_json(UNISWAP_V4, CONCENTRATED),
        "manager": None,
        "staked": False,
        "gauge": None,
        "rewards": None,
        "share": None,
    }
