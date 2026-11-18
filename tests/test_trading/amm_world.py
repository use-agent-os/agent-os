"""An offline chain of classic AMMs for ``agentos.trading.lp_amm``.

Stands in for the skill library's ``RpcClient`` at ``multicall`` level, the
way ``lp_world.World`` does for Uniswap V4. It is built on the *real*
deployment table (``lp_amm.DEPLOYMENTS``), so a test exercises the addresses
and the lookups the engine ships with. ``rounds`` counts multicall round
trips and ``asked`` the function names, which is what the RPC-budget tests
assert on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentos.trading import lp, lp_amm
from agentos.trading.chains import BASE, ChainSpec

LIB = lp.unilp()
Q128 = 1 << 128

WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
FOO = "0xf00df00df00df00df00df00df00df00df00df00d"
AERO = "0x3f5bf3edac59d650d8fdf9657a71f2f904a3a4ee"
XAERO = "0x6cdda0e730744dc6ff67654b35350adad05ecee7"
OLD_AERO = "0x940181a94a35a4569e4529a3cdfb74e38fd98631"
WALLET = "0x1111111111111111111111111111111111111111"
OTHER = "0x2222222222222222222222222222222222222222"

DEP = lp_amm.DEPLOYMENTS[8453]
UNI_V3 = next(d for d in DEP.cl if d.protocol == lp_amm.UNISWAP_V3)
AERO_CL = next(d for d in DEP.cl if d.protocol == lp_amm.AERO_CL)
OLD_CL = next(d for d in DEP.cl if d.protocol == lp_amm.AERODROME_CL)
UNI_V2 = next(d for d in DEP.pairs if d.protocol == lp_amm.UNISWAP_V2)
AERO_VOLATILE = next(d for d in DEP.pairs if d.protocol == lp_amm.AERO_V2 and d.stable is False)
OLD_V2 = next(d for d in DEP.pairs if d.protocol == lp_amm.AERODROME_V2)
SOURCES = {s.key: s for s in DEP.gauges}


def addr(n: int) -> str:
    return "0x" + f"{n:040x}"


@dataclass
class Nft:
    owner: str
    token0: str
    token1: str
    key: int
    tick_lower: int
    tick_upper: int
    liquidity: int
    last0: int = 0
    last1: int = 0
    owed0: int = 0
    owed1: int = 0


@dataclass
class ClPool:
    factory: str
    token0: str
    token1: str
    key: int
    tick: int
    liquidity: int = 10**18
    growth0: int = 0
    growth1: int = 0
    fee: int = 500
    spacing: int = 10
    #: tick -> (feeGrowthOutside0, feeGrowthOutside1)
    ticks: dict[int, tuple[int, int]] = field(default_factory=dict)


@dataclass
class Pair:
    factory: str
    token0: str
    token1: str
    reserve0: int
    reserve1: int
    supply: int
    stable: bool = False
    balances: dict[str, int] = field(default_factory=dict)
    pending: dict[str, tuple[int, int]] = field(default_factory=dict)
    claimable: dict[str, tuple[int, int]] = field(default_factory=dict)
    index: tuple[int, int] = (0, 0)
    supply_index: dict[str, tuple[int, int]] = field(default_factory=dict)
    #: What ``factory()`` claims, when a token lies about where it came from.
    claims: str | None = None


@dataclass
class Gauge:
    source: str
    pool: str
    #: Concentrated: depositor -> token ids. Pair: depositor -> staked balance.
    staked: dict[str, Any] = field(default_factory=dict)
    earned: dict[Any, int] = field(default_factory=dict)
    booked: dict[int, int] = field(default_factory=dict)
    deferred: dict[str, int] = field(default_factory=dict)


@dataclass
class AmmWorld:
    spec: ChainSpec = BASE
    block: int = 58_000_000
    tokens: dict[str, tuple[str, int, int]] = field(
        default_factory=lambda: {
            WETH: ("WETH", 18, 10**24),
            USDC: ("USDC", 6, 10**15),
            FOO: ("FOO", 18, 10**27),
            AERO: ("AERO", 18, 10**27),
            XAERO: ("xAERO", 18, 10**24),
            OLD_AERO: ("AERO", 18, 10**27),
        }
    )
    #: manager -> tokenId -> Nft
    nfts: dict[str, dict[int, Nft]] = field(default_factory=dict)
    cl_pools: dict[str, ClPool] = field(default_factory=dict)
    pairs: dict[str, Pair] = field(default_factory=dict)
    gauges: dict[str, Gauge] = field(default_factory=dict)
    #: Voter listing (Aerodrome): pools in order.
    voter_pools: list[str] = field(default_factory=list)
    #: Addresses whose every call fails as a node fault (not a revert).
    faulty: set[str] = field(default_factory=set)
    rounds: int = 0
    asked: list[str] = field(default_factory=list)
    #: Anything this world does not know is handed here (a V4 ``World``).
    fallback: Any = None

    def __getattr__(self, name: str) -> Any:
        # The raw JSON-RPC surface (logs, batches) belongs to the V4 world.
        fallback = self.__dict__.get("fallback")
        if fallback is None or name.startswith("__"):
            raise AttributeError(name)
        return getattr(fallback, name)

    # -- building ------------------------------------------------------------

    def add_cl_pool(self, dep: lp_amm.ClDeployment, pool: ClPool) -> str:
        address = addr(0xC100 + len(self.cl_pools))
        pool.factory = dep.factory
        self.cl_pools[address] = pool
        return address

    def mint(self, dep: lp_amm.ClDeployment, token_id: int, nft: Nft) -> None:
        self.nfts.setdefault(dep.manager, {})[token_id] = nft

    def add_pair(self, dep: lp_amm.PairDeployment, pair: Pair) -> str:
        address = addr(0xBA00 + len(self.pairs))
        pair.factory = dep.factory
        self.pairs[address] = pair
        return address

    def add_gauge(self, source: str, pool: str) -> str:
        address = addr(0x6A00 + len(self.gauges))
        self.gauges[address] = Gauge(source=source, pool=pool)
        if SOURCES[source].kind == lp_amm.VOTER:
            self.voter_pools.append(pool)
        return address

    def stake_nft(self, gauge: str, dep: lp_amm.ClDeployment, token_id: int) -> None:
        nft = self.nfts[dep.manager][token_id]
        self.gauges[gauge].staked.setdefault(nft.owner, []).append(token_id)
        # The wallet no longer holds it: the gauge does.
        nft.owner = f"staked:{nft.owner}"

    # -- the RpcClient surface -----------------------------------------------

    def block_number(self) -> int:
        return self.block

    def multicall(
        self, calls: list[dict[str, Any]], allow_failure: bool = True, block: str = "latest"
    ) -> list[dict[str, Any]]:
        self.rounds += 1
        out = []
        for call in calls:
            target = str(call["address"]).lower()
            self.asked.append(call["functionName"])
            if target in self.faulty:
                out.append({"status": "failure", "result": None, "error": lp._NODE_FAULT})
                continue
            try:
                out.append({"status": "success", "result": self._answer(target, call)})
            except LookupError as exc:
                out.append({"status": "failure", "result": None, "error": str(exc)})
        return out

    def _held(self, manager: str, owner: str) -> list[int]:
        return sorted(i for i, n in self.nfts.get(manager, {}).items() if n.owner == owner)

    def _listed(self, lister: str) -> list[str]:
        """Pools a factory lists, in creation order."""
        pools = [a for a, p in self.cl_pools.items() if p.factory == lister]
        return pools + [a for a, p in self.pairs.items() if p.factory == lister]

    def _answer(self, target: str, call: dict[str, Any]) -> Any:  # noqa: C901 - a dispatch table
        name = call["functionName"]
        args = call.get("args") or []
        first = str(args[0]).lower() if args and isinstance(args[0], str) else None
        if target in self.nfts or target in {d.manager for d in DEP.cl}:
            held = self._held(target, first or "")
            if name == "balanceOf":
                return len(held)
            if name == "tokenOfOwnerByIndex":
                index = int(args[1])
                if index >= len(held):
                    raise LookupError("EnumerableSet: index out of bounds")
                return held[index]
            if name == "positions":
                nft = self.nfts.get(target, {}).get(int(args[0]))
                if nft is None:
                    raise LookupError("ID")
                return [
                    0,
                    lp.NATIVE_ADDRESS,
                    nft.token0,
                    nft.token1,
                    nft.key,
                    nft.tick_lower,
                    nft.tick_upper,
                    nft.liquidity,
                    nft.last0,
                    nft.last1,
                    nft.owed0,
                    nft.owed1,
                ]
        if target in self.cl_pools:
            pool = self.cl_pools[target]
            if name == "slot0":
                return [LIB.v4_math.get_sqrt_ratio_at_tick(pool.tick), pool.tick, 0, 0, 0, True]
            if name == "ticks":
                outside = pool.ticks.get(int(args[0]), (0, 0))
                slipstream = any(d.factory == pool.factory and d.by_spacing for d in DEP.cl)
                head = [0, 0, 0] if slipstream else [0, 0]
                return [*head, outside[0], outside[1], 0, 0, 0, 0, True]
            return {
                "liquidity": pool.liquidity,
                "feeGrowthGlobal0X128": pool.growth0,
                "feeGrowthGlobal1X128": pool.growth1,
                "fee": pool.fee,
                "tickSpacing": pool.spacing,
            }[name]
        if target in self.pairs:
            pair = self.pairs[target]
            who = first or ""
            n = 0 if name.endswith("0") else 1
            if name == "factory":
                return pair.claims or pair.factory
            if name in ("token0", "token1"):
                return pair.token0 if n == 0 else pair.token1
            if name == "stable":
                if pair.factory != OLD_V2.factory:
                    raise LookupError("no stable()")
                return pair.stable
            if name == "getReserves":
                return [pair.reserve0, pair.reserve1, 0]
            if name == "totalSupply":
                return pair.supply
            if name == "balanceOf":
                return pair.balances.get(who, 0)
            if name == "pendingFees":
                return list(pair.pending.get(who, (0, 0)))
            if name in ("claimable0", "claimable1"):
                return pair.claimable.get(who, (0, 0))[n]
            if name in ("index0", "index1"):
                return pair.index[n]
            if name in ("supplyIndex0", "supplyIndex1"):
                return pair.supply_index.get(who, (0, 0))[n]
        if target in self.gauges:
            return self._gauge(self.gauges[target], name, args, first or "")
        for dep in DEP.cl:
            if target == dep.factory and name == "getPool":
                wanted = (str(args[0]).lower(), str(args[1]).lower(), int(args[2]))
                for address, pool in self.cl_pools.items():
                    if (pool.token0, pool.token1, pool.key) == wanted and pool.factory == target:
                        return address
                return lp.NATIVE_ADDRESS
        for pair_dep in DEP.pairs:
            if target == pair_dep.factory and name in ("getPair", "getPool"):
                wanted_pair = (str(args[0]).lower(), str(args[1]).lower())
                curve = bool(args[2]) if len(args) > 2 else None
                for address, pair in self.pairs.items():
                    same = (pair.token0, pair.token1) == wanted_pair and pair.factory == target
                    if same and (curve is None or curve == pair.stable):
                        return address
                return lp.NATIVE_ADDRESS
        for source in DEP.gauges:
            if source.kind == lp_amm.VOTER and target == source.lister:
                if name == "length":
                    return len(self.voter_pools)
                if name == "pools":
                    return self.voter_pools[int(args[0])]
                if name == "gauges":
                    return next(
                        (a for a, g in self.gauges.items() if g.pool == first), lp.NATIVE_ADDRESS
                    )
            elif target == source.lister and name == "allPoolsLength":
                return len(self._listed(target))
            elif target == source.lister and name == "allPools":
                return self._listed(target)[int(args[0])]
            elif target == source.gauge_factory and name == "computeGaugeAddress":
                # Deterministic: an address even for a pool with no gauge deployed.
                found = next((a for a, g in self.gauges.items() if g.pool == first), None)
                return found or addr(0xDEAD0000 + int(first or "0x0", 16) % 0xFFFF)
        meta = self.tokens.get(target)
        if meta is not None and name in ("symbol", "decimals", "totalSupply"):
            return meta[("symbol", "decimals", "totalSupply").index(name)]
        if self.fallback is not None:
            return self.fallback._answer(call)
        raise LookupError("empty return — no contract at that address")

    def _gauge(self, gauge: Gauge, name: str, args: list[Any], who: str) -> Any:
        source = SOURCES[gauge.source]
        concentrated = gauge.pool in self.cl_pools
        if name == "nft":
            if not concentrated:
                raise LookupError("reverted")
            pool = self.cl_pools[gauge.pool]
            return next(d.manager for d in DEP.cl if d.factory == pool.factory)
        if name == "stakedValues":
            return list(gauge.staked.get(who, []))
        if name == "balanceOf":
            return int(gauge.staked.get(who, 0))
        if name == "deferredEmissions":
            return gauge.deferred.get(who, 0)
        if name == "rewards":
            return gauge.booked.get(int(args[0]), 0)
        if name == "earned":
            if not concentrated:
                return gauge.earned.get(who, 0)
            if source.batched_rewards:
                ids = [int(i) for i in args[1]]
                return sum(gauge.earned.get(i, 0) for i in ids) + gauge.deferred.get(who, 0)
            if int(args[1]) not in gauge.staked.get(who, []):
                raise LookupError("NA")
            return gauge.earned.get(int(args[1]), 0)
        raise LookupError(f"unhandled {name}")


def amm_env(world: AmmWorld, prices: dict[str, float] | None = None) -> lp.ChainEnv:
    table = {WETH: 2500.0, USDC: 1.0, AERO: 0.5} if prices is None else prices
    known = {k.lower(): v for k, v in table.items()}
    env = lp.ChainEnv(
        spec=world.spec,
        client=world,
        prices=lambda addresses: {a.lower(): known.get(a.lower()) for a in addresses},
        lib=LIB,
        vault={WALLET: "Main"},
    )
    lp.start(env)
    return env


def growth_for(fees: int, liquidity: int) -> int:
    """The fee-growth delta that pays ``fees`` to ``liquidity`` (rounded up)."""
    return -(-fees * Q128 // liquidity)
