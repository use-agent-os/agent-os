"""Uniswap v2/v3, Aero and Aerodrome positions (``agentos.trading.lp_amm``), offline."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agentos.trading import lp, lp_amm

from .amm_world import (
    AERO,
    AERO_CL,
    AERO_VOLATILE,
    FOO,
    OLD_AERO,
    OLD_CL,
    OLD_V2,
    OTHER,
    UNI_V2,
    UNI_V3,
    USDC,
    WALLET,
    WETH,
    XAERO,
    AmmWorld,
    ClPool,
    Nft,
    Pair,
    amm_env,
    growth_for,
)

LIQ = 10**15


def weth_usdc_pool(
    world: AmmWorld, dep: lp_amm.ClDeployment, key: int, tick: int = -200_000
) -> str:
    return world.add_cl_pool(dep, ClPool("", WETH, USDC, key, tick))


def read(
    world: AmmWorld,
    owners: list[str] | None = None,
    prices: dict[str, float] | None = None,
    **kwargs: Any,
) -> lp_amm.AmmRead:
    return lp_amm.read_positions(amm_env(world, prices), owners or [WALLET], **kwargs)


def test_a_wallet_with_nothing_costs_one_round_trip() -> None:
    world = AmmWorld()
    result = read(world, store=lp_amm.AmmStore())
    assert result.rows == [] and world.rounds == 1
    # One balanceOf per position manager, and nothing else.
    assert world.asked == ["balanceOf"] * len(lp_amm.DEPLOYMENTS[8453].cl)


def test_uniswap_v3_position_amounts_and_uncollected_fees() -> None:
    world = AmmWorld()
    pool = weth_usdc_pool(world, UNI_V3, 500)
    world.cl_pools[pool].growth0 = growth_for(4 * 10**15, LIQ)
    world.cl_pools[pool].growth1 = growth_for(9_000_000, LIQ)
    world.mint(UNI_V3, 77, Nft(WALLET, WETH, USDC, 500, -201_000, -199_000, LIQ, owed0=10**15))
    row = read(world).rows[0]
    assert row["protocol"] == {
        "key": "uniswap_v3",
        "name": "Uniswap V3",
        "kind": "concentrated",
    }
    assert row["tokenId"] == "77" and row["status"] == "in-range" and row["staked"] is False
    assert row["pool"]["feePct"] == "0.05%" and row["pool"]["tickSpacing"] == 10
    assert row["token"]["symbol"] == "WETH" and row["quote"]["symbol"] == "USDC"
    # tokensOwed (0.001 WETH) plus what grew inside the range (0.004 WETH, 9 USDC).
    assert row["fees"]["base"]["raw"] == str(5 * 10**15)
    assert row["fees"]["quote"]["raw"] == "9000000"
    assert row["fees"]["usd"] == pytest.approx(0.005 * 2500 + 9)
    expected = LIB_amounts(world, pool, -201_000, -199_000)
    assert row["principal"]["base"]["raw"] == str(expected["amount0"])
    assert row["principal"]["quote"]["raw"] == str(expected["amount1"])
    assert row["valueUsd"] == pytest.approx(row["principal"]["usd"] + row["fees"]["usd"])


def LIB_amounts(world: AmmWorld, pool: str, lower: int, upper: int) -> dict[str, int]:  # noqa: N802
    lib = lp.unilp()
    sqrt = lib.v4_math.get_sqrt_ratio_at_tick(world.cl_pools[pool].tick)
    return dict(lib.v4_math.get_amounts_for_liquidity_at_ticks(sqrt, lower, upper, LIQ))


def test_fees_outside_the_range_use_the_tick_counters() -> None:
    """Below the range, the growth inside is lower.outside - upper.outside (mod 2**256)."""
    world = AmmWorld()
    pool = weth_usdc_pool(world, UNI_V3, 3000, tick=-205_000)
    state = world.cl_pools[pool]
    state.growth0, state.growth1 = 10 * LIQ, 0
    grown = growth_for(7 * 10**12, LIQ)
    # Counters wrap: upper.outside is "ahead" of lower.outside by design.
    state.ticks = {-201_000: (5, 0), -199_000: ((5 - grown) % (1 << 256), 0)}
    world.mint(UNI_V3, 5, Nft(WALLET, WETH, USDC, 3000, -201_000, -199_000, LIQ))
    row = read(world).rows[0]
    assert row["status"] == "below-range"
    assert row["fees"]["base"]["raw"] == str(7 * 10**12)


def test_emptied_nfts_are_dropped_and_not_asked_again_until_a_deep_read() -> None:
    world = AmmWorld()
    weth_usdc_pool(world, UNI_V3, 500)
    world.mint(UNI_V3, 1, Nft(WALLET, WETH, USDC, 500, -201_000, -199_000, 0))
    world.mint(UNI_V3, 2, Nft(WALLET, WETH, USDC, 500, -201_000, -199_000, LIQ))
    store = lp_amm.AmmStore()
    assert [r["tokenId"] for r in read(world, store=store).rows] == ["2"]
    world.asked.clear()
    assert [r["tokenId"] for r in read(world, store=store).rows] == ["2"]
    # The ids come from the cache (count and newest id unchanged) and #1 is skipped.
    assert world.asked.count("tokenOfOwnerByIndex") == 1 and world.asked.count("positions") == 1
    # Liquidity added back to #1 is seen by the next deep read.
    world.nfts[UNI_V3.manager][1].liquidity = LIQ
    assert [r["tokenId"] for r in read(world, store=store).rows] == ["2"]
    assert sorted(r["tokenId"] for r in read(world, store=store, deep=True).rows) == ["1", "2"]


def test_a_new_mint_with_the_same_count_is_noticed() -> None:
    """Burn one, mint one: the count is unchanged, the newest id is not."""
    world = AmmWorld()
    weth_usdc_pool(world, UNI_V3, 500)
    world.mint(UNI_V3, 10, Nft(WALLET, WETH, USDC, 500, -201_000, -199_000, LIQ))
    store = lp_amm.AmmStore()
    read(world, store=store)
    del world.nfts[UNI_V3.manager][10]
    world.mint(UNI_V3, 11, Nft(WALLET, WETH, USDC, 500, -201_000, -199_000, LIQ))
    assert [r["tokenId"] for r in read(world, store=store).rows] == ["11"]


def test_aero_staked_position_is_found_by_a_deep_read_and_kept_by_light_ones() -> None:
    world = AmmWorld()
    pool = world.add_cl_pool(AERO_CL, ClPool("", WETH, USDC, 100, -200_000, fee=420))
    world.mint(AERO_CL, 900, Nft(WALLET, WETH, USDC, 100, -201_000, -199_000, LIQ, owed1=3_000_000))
    gauge = world.add_gauge("aero-cl", pool)
    world.stake_nft(gauge, AERO_CL, 900)
    world.gauges[gauge].earned[900] = 8 * 10**18
    world.gauges[gauge].deferred[WALLET] = 2 * 10**18
    store = lp_amm.AmmStore()
    # Not in the wallet and no stake known: a light read cannot see it.
    assert read(world, store=store).rows == []
    deep = read(world, store=store, deep=True)
    assert deep.deep and not deep.full and deep.stakes == {(WALLET, gauge)}
    row = deep.rows[0]
    assert row["protocol"]["key"] == "aero_slipstream" and row["staked"] is True
    assert row["gauge"].lower() == gauge and row["owner"]["address"].lower() == WALLET
    assert row["pool"]["feePct"] == "0.042%"
    # earned(account, [id]) includes the account's deferred emissions: taken off.
    assert row["rewards"]["token"]["symbol"] == "xAERO"
    assert row["rewards"]["amount"]["raw"] == str(8 * 10**18)
    # xAERO has no quote of its own; it redeems 1:1 for AERO.
    assert row["rewards"]["amount"]["usd"] == pytest.approx(4.0)
    # A staked position's trading fees are the gauge's: only what was owed counts.
    assert row["fees"]["quote"]["raw"] == "3000000" and row["fees"]["base"]["raw"] == "0"
    assert row["valueUsd"] == pytest.approx(row["principal"]["usd"] + 3.0 + 4.0)

    world.asked.clear()
    light = read(world, store=store, stakes=deep.stakes)
    assert [r["tokenId"] for r in light.rows] == ["900"] and light.stakes == deep.stakes
    # Only the one gauge is asked, not the directory.
    assert world.asked.count("stakedValues") == 1 and "allPoolsLength" not in world.asked


def test_a_frozen_source_is_only_walked_by_a_full_read() -> None:
    world = AmmWorld()
    pair = world.add_pair(
        OLD_V2,
        Pair("", WETH, USDC, 10**20, 25 * 10**10, 10**18, stable=False),
    )
    gauge = world.add_gauge("aerodrome", pair)
    world.gauges[gauge].staked[WALLET] = 10**17
    world.gauges[gauge].earned[WALLET] = 5 * 10**18
    store = lp_amm.AmmStore()
    deep = read(world, store=store, deep=True, prices={WETH: 2500.0, USDC: 1.0, OLD_AERO: 0.4})
    assert deep.rows == [] and deep.deep and not deep.full and "length" not in world.asked
    env = amm_env(world, {WETH: 2500.0, USDC: 1.0, OLD_AERO: 0.4})
    full = lp_amm.read_positions(env, [WALLET], full=True, store=store)
    assert full.full and full.stakes == {(WALLET, gauge)}
    row = full.rows[0]
    assert row["protocol"]["key"] == "aerodrome_v2" and row["staked"] is True
    assert row["status"] == "full-range" and row["tokenId"] is None and row["range"] is None
    assert row["pool"]["feePct"] == "volatile" and row["share"] == pytest.approx(0.1)
    assert row["principal"]["base"]["raw"] == str(10**19)
    assert row["rewards"]["token"]["symbol"] == "AERO"
    assert row["rewards"]["amount"]["usd"] == pytest.approx(2.0)
    assert row["fees"] is None


def test_uniswap_v2_pair_from_the_ledgers_holdings() -> None:
    world = AmmWorld()
    pair = world.add_pair(UNI_V2, Pair("", WETH, USDC, 4 * 10**18, 10**10, 10**12))
    world.pairs[pair].balances[WALLET] = 25 * 10**10
    store = lp_amm.AmmStore()
    result = read(world, held={WALLET: {pair, WETH}}, store=store)
    row = result.rows[0]
    assert row["protocol"] == {"key": "uniswap_v2", "name": "Uniswap V2", "kind": "pair"}
    assert row["pool"]["feePct"] == "0.3%" and row["pool"]["poolId"].lower() == pair
    assert row["share"] == pytest.approx(0.25)
    assert row["principal"]["base"]["raw"] == str(10**18)
    assert row["principal"]["quote"]["raw"] == str(25 * 10**8)
    assert row["valueUsd"] == pytest.approx(2500 + 2500)
    # Fees compound into the reserves: there is nothing to collect.
    assert row["fees"] is None
    assert store.pairs[pair] is not None and store.pairs[WETH] is None
    # Next time neither token is asked what it is.
    world.asked.clear()
    read(world, held={WALLET: {pair, WETH}}, store=store)
    assert "factory" not in world.asked and "getPair" not in world.asked


def test_a_token_claiming_a_known_factory_is_not_believed() -> None:
    """Anything can answer ``factory()``; the factory itself must name the pair."""
    world = AmmWorld()
    fake = world.add_pair(UNI_V2, Pair("", WETH, USDC, 10**30, 10**30, 10**6))
    world.pairs[fake].balances[WALLET] = 10**6
    world.pairs[fake].factory = "0x" + "99" * 20  # the real factory never made it...
    world.pairs[fake].claims = UNI_V2.factory  # ...but it says so
    store = lp_amm.AmmStore()
    assert read(world, held={WALLET: {fake}}, store=store).rows == []
    assert store.pairs[fake] is None


def test_aero_basic_pool_reports_pending_fees() -> None:
    world = AmmWorld()
    pair = world.add_pair(AERO_VOLATILE, Pair("", FOO, USDC, 10**24, 10**12, 10**18))
    world.pairs[pair].balances[WALLET] = 10**17
    world.pairs[pair].pending[WALLET] = (5 * 10**18, 2_000_000)
    row = read(world, held={WALLET: {pair}}).rows[0]
    assert row["protocol"]["key"] == "aero_basic" and row["pool"]["feePct"] == "volatile"
    # FOO has no quote: it is valued at the pool's own ratio (1e-6 USDC each).
    assert row["token"]["symbol"] == "FOO" and row["token"]["priceUsd"] == pytest.approx(1.0)
    assert row["fees"]["base"]["raw"] == str(5 * 10**18)
    assert row["fees"]["usd"] == pytest.approx(5.0 + 2.0)
    assert row["valueUsd"] == pytest.approx(100_000 + 100_000 + 7.0)


def test_aerodrome_v2_unstaked_fees_follow_the_fee_index() -> None:
    world = AmmWorld()
    pair = world.add_pair(OLD_V2, Pair("", WETH, USDC, 10**20, 25 * 10**10, 10**18, stable=True))
    state = world.pairs[pair]
    state.balances[WALLET] = 10**17
    state.claimable[WALLET] = (7, 11)
    state.index = (3 * 10**18, 5 * 10**6)
    state.supply_index[WALLET] = (10**18, 10**6)
    row = read(world, held={WALLET: {pair}}).rows[0]
    assert row["protocol"]["key"] == "aerodrome_v2" and row["pool"]["feePct"] == "stable"
    # claimable + balance * (index - supplyIndex) / 1e18
    assert row["fees"]["base"]["raw"] == str(7 + 10**17 * 2)
    assert row["fees"]["quote"]["raw"] == str(11 + 10**17 * 4 * 10**6 // 10**18)


def test_a_node_fault_keeps_the_known_ids_and_flags_the_read() -> None:
    world = AmmWorld()
    weth_usdc_pool(world, OLD_CL, 100)
    world.mint(OLD_CL, 4, Nft(WALLET, WETH, USDC, 100, -201_000, -199_000, LIQ))
    store = lp_amm.AmmStore()
    assert len(read(world, store=store).rows) == 1
    world.faulty.add(OLD_CL.manager)
    env = amm_env(world)
    again = lp_amm.read_positions(env, [WALLET], store=store)
    # The count could not be read: the cached id is still tried, and it fails too.
    assert again.rows == [] and env.partial
    assert any("could not read how many Aerodrome Slipstream" in w for w in env.warnings)
    # Nothing was forgotten: the next healthy read has it again without re-listing.
    world.faulty.clear()
    world.asked.clear()
    assert len(read(world, store=store).rows) == 1
    assert world.asked.count("tokenOfOwnerByIndex") == 1


def test_two_wallets_share_the_same_round_trips() -> None:
    world = AmmWorld()
    weth_usdc_pool(world, UNI_V3, 500)
    world.mint(UNI_V3, 1, Nft(WALLET, WETH, USDC, 500, -201_000, -199_000, LIQ))
    world.mint(UNI_V3, 2, Nft(OTHER, WETH, USDC, 500, -201_000, -199_000, LIQ))
    one = AmmWorld()
    weth_usdc_pool(one, UNI_V3, 500)
    one.mint(UNI_V3, 1, Nft(WALLET, WETH, USDC, 500, -201_000, -199_000, LIQ))
    read(one)
    result = read(world, [WALLET, OTHER])
    assert sorted(r["tokenId"] for r in result.rows) == ["1", "2"]
    assert {r["owner"]["address"].lower() for r in result.rows} == {WALLET, OTHER}
    assert world.rounds == one.rounds


def test_the_store_survives_a_restart(tmp_path: Path) -> None:
    world = AmmWorld()
    pool = world.add_cl_pool(AERO_CL, ClPool("", WETH, USDC, 100, -200_000))
    world.mint(AERO_CL, 9, Nft(WALLET, WETH, USDC, 100, -201_000, -199_000, LIQ))
    world.add_gauge("aero-cl", pool)
    path = tmp_path / "lp" / "amm-8453.json"
    read(world, store=lp_amm.AmmStore.load(path), deep=True)
    reloaded = lp_amm.AmmStore.load(path)
    assert reloaded.cursors["aero-cl"] == 1 and len(reloaded.gauges) == 1
    assert reloaded.nfts[f"{AERO_CL.manager}:{WALLET}"]["ids"] == [9]
    assert list(reloaded.pools.values()) == [pool]
    # A file of another version (or a broken one) is ignored, not trusted.
    path.write_text('{"version": 0, "pairs": {"x": null}}', encoding="utf-8")
    assert lp_amm.AmmStore.load(path).pairs == {}


def test_reward_tokens_and_deployments_are_consistent() -> None:
    for chain_id, dep in lp_amm.DEPLOYMENTS.items():
        managers = [d.manager for d in dep.cl]
        assert len(managers) == len(set(managers)), chain_id
        factories = [d.factory for d in dep.pairs]
        assert len(factories) == len(set(factories)), chain_id
        for source in dep.gauges:
            assert source.reward_token and source.lister == source.lister.lower()
            if source.kind == lp_amm.CONCENTRATED:
                assert source.manager in managers and source.gauge_factory
            if source.kind == lp_amm.PAIR:
                assert source.lister in factories and source.gauge_factory
        for d in dep.cl:
            assert d.protocol in lp_amm.PROTOCOL_NAMES
    assert XAERO == lp_amm.DEPLOYMENTS[8453].gauges[0].reward_token
    assert AERO == lp_amm.DEPLOYMENTS[8453].gauges[0].priced_as
