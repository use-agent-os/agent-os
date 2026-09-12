#!/usr/bin/env python3
"""Read-only Uniswap V4 LP inspection.

This file NEVER signs and never imports a signing path — that is what makes it safe to
allowlist wholesale. All writes live in ``lp_write.py``.

It touches ``UNIV4_LP_PRIVATE_KEY`` in exactly one place, ``resolve_owner``, and only to
answer "which wallet is mine" when ``positions`` is run without ``--owner``. That import
is ``unilp.account``, which holds curve arithmetic and address derivation and has no
``sign_digest`` in it, so no argument to this script can reach a signature.

    python3 scripts/lp_read.py pools     --token <addr> [--include-v3] [--all-pools]
    python3 scripts/lp_read.py pool      --id <poolId>
    python3 scripts/lp_read.py position  --token-id <id>
    python3 scripts/lp_read.py positions [--owner <addr>]   omit = the configured wallet
    python3 scripts/lp_read.py launcher  --token <addr>
    python3 scripts/lp_read.py ticks     --pool <poolId> --mcap-lower <usd> --mcap-upper <usd>
    python3 scripts/lp_read.py price     --tokens <a,b,...>

Global flags: --chain <key|id> (default robinhood)  --rpc <url>  --json
              --mode logs|ticks  (how reserves are read; see chain.rangeMode)
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from unilp.abi_defs import (  # noqa: E402
    ERC20_ABI,
    POSITION_MANAGER_ABI,
    STATE_VIEW_ABI,
    TOPIC_ERC721_TRANSFER,
    TOPIC_MODIFY_LIQUIDITY,
    V3_FACTORY_ABI,
    V3_POOL_ABI,
)
from unilp.chains import ENV_SIGNER, resolve_chain, resolve_signer_address  # noqa: E402
from unilp.fmt import (  # noqa: E402
    die,
    fmt_band,
    fmt_units,
    fmt_usd,
    heading,
    json_safe,
    opt_float,
    opt_int,
    opt_str,
    parse_args,
    render_kv,
    render_table,
    require_arg,
    short,
    short_id,
)
from unilp.hexutil import checksum_address, pad, to_hex  # noqa: E402
from unilp.launchers import (  # noqa: E402
    derive_pool_candidates,
    is_locker_address,
    label_address,
    launchers_for,
    probe_position_ids,
    queryable_launchers,
    resolve_launcher,
)
from unilp.prices import fetch_usd_prices, price_unavailable_message  # noqa: E402
from unilp.rpc import RpcClient  # noqa: E402
from unilp.v4_math import (  # noqa: E402
    get_amounts_for_liquidity_at_ticks,
    get_sqrt_ratio_at_tick,
    mcap_at_tick,
    mcap_band_for_range,
    pull_off_current_tick,
    range_status,
    snap_tick,
    tick_at_mcap,
    token_price_in_quote_at_tick,
)
from unilp.v4_pool import (  # noqa: E402
    NATIVE,
    aggregate_ranges,
    compute_pool_id,
    decode_position_info,
    derive_vanilla_candidates,
    find_pools_for_token,
    format_fee,
    format_hook_flags,
    get_fees_owed,
    get_logs_chunked,
    get_pool_init,
    get_pool_ranges,
    is_dynamic_fee,
    normalize_pool_key,
    pool_id_matches_truncated,
    walk_tick_ranges,
)

USAGE = """
senior-unilp-manager — read-only Uniswap V4 LP inspection

  pools     --token <addr> [--quote <addr>] [--min-tvl <usd>] [--max-pools 60] [--all-pools]
            [--include-v3] [--scan-logs] [--no-hook]
            --no-hook only looks for pools with no hook, by deriving their poolIds at the
            conventional fee tiers. One multicall, no log scan, same speed on every chain.
  pool      --id <poolId> [--token <addr>] [--ranges <n>]
            --token lets the PoolKey be derived from the launchpad registry — or, for a
            hook-less pool, from the conventional fee tiers — instead of an Initialize log
            scan, which chains that cap eth_getLogs ranges (Base) cannot serve.
            --currency0 <addr> --currency1 <addr> --fee <n> --tick-spacing <n> [--hooks <addr>]
            gives the PoolKey outright: no lookup at all, works for any pool on any chain.
            It is checked by recomputing the poolId. --hooks defaults to no hook.
  position  --token-id <id> [--no-fees]
  positions [--owner <addr>] [--include-empty]
            Leave --owner off for the wallet UNIV4_LP_PRIVATE_KEY derives; --signer-env
            <VAR> reads a different variable. The key is only ever used to derive that
            address — this script has no signing path to reach.
  launcher  --token <addr>            which launchpad deployed it, its pool, who holds the LP
  ticks     --pool <poolId>
            (--mcap-lower <usd> --mcap-upper <usd> | --tick-lower <t> --tick-upper <t>)
            --from-current  keep the range off the current tick so it stays single-sided.
                            Use it whenever one end of the band is "the price right now":
                            snapping outward would otherwise straddle the tick and the
                            position would come back needing both currencies.
                            The two --mcap flags may be given in either order.
  price     --tokens <addr,addr,...>

Global: --chain <key|id>  (default robinhood)   --rpc <url>   --json
        --mode logs|ticks   how reserves are read. "logs" replays ModifyLiquidity and
                            attributes each range to an owner; "ticks" walks the tick bitmap
                            (no logs, merges overlapping positions into segments).
                            Default: logs on Robinhood, ticks on Base.
"""

# ---------------------------------------------------------------------------
# Token metadata
# ---------------------------------------------------------------------------

_meta_cache: dict[str, dict] = {}


def token_meta(client, chain: dict, address: str | None) -> dict:
    addr = (address or NATIVE).lower()
    cached = _meta_cache.get(addr)
    if cached is not None:
        return cached

    if addr == NATIVE:
        meta = {
            "address": NATIVE,
            "symbol": chain["nativeCurrency"]["symbol"],
            "name": chain["nativeCurrency"]["name"],
            "decimals": chain["nativeCurrency"]["decimals"],
            "totalSupply": None,
            "isNative": True,
        }
        _meta_cache[addr] = meta
        return meta

    checksummed = checksum_address(addr)
    symbol, name, decimals, total_supply = client.multicall([
        {"address": checksummed, "abi": ERC20_ABI, "functionName": fn}
        for fn in ("symbol", "name", "decimals", "totalSupply")
    ])

    meta = {
        "address": checksummed,
        "symbol": symbol["result"] if symbol["status"] == "success" else short(checksummed),
        "name": name["result"] if name["status"] == "success" else "",
        "decimals": int(decimals["result"]) if decimals["status"] == "success" else 18,
        "totalSupply": (int(total_supply["result"])
                        if total_supply["status"] == "success" else None),
        "isNative": False,
    }
    _meta_cache[addr] = meta
    return meta


def mcap_context(pool_key: dict, token: str, meta0: dict, meta1: dict, prices: dict) -> dict:
    """Everything needed to turn a tick into a market cap for ``token`` in a pool."""
    token_is_currency1 = pool_key["currency1"].lower() == token.lower()
    token_meta_ref = meta1 if token_is_currency1 else meta0
    quote_meta_ref = meta0 if token_is_currency1 else meta1
    supply = token_meta_ref["totalSupply"]
    return {
        "tokenIsCurrency1": token_is_currency1,
        "decimals0": meta0["decimals"],
        "decimals1": meta1["decimals"],
        "totalSupply": supply if supply is not None else 0,
        "tokenDecimals": token_meta_ref["decimals"],
        "quoteUsd": prices.get(quote_meta_ref["address"].lower()),
        "tickSpacing": pool_key["tickSpacing"],
        "hasSupply": supply is not None and supply > 0,
    }


def _math_kwargs(ctx: dict, **overrides) -> dict:
    """Adapt the JS-shaped ctx object to the math module's keyword arguments.

    The ctx dict keeps its camelCase keys because it is serialised verbatim by
    ``pool --json``; the diff against the Node build compares those key names.
    """
    out = {
        "token_is_currency1": ctx["tokenIsCurrency1"],
        "decimals0": ctx["decimals0"],
        "decimals1": ctx["decimals1"],
        "total_supply": ctx["totalSupply"],
        "token_decimals": ctx["tokenDecimals"],
        "quote_usd": ctx["quoteUsd"],
    }
    out.update(overrides)
    return out


def band_for(tick_lower: int, tick_upper: int, ctx: dict | None) -> dict | None:
    if not ctx or not ctx["hasSupply"] or ctx["quoteUsd"] is None:
        return None
    return mcap_band_for_range(tick_lower, tick_upper,
                               tick_spacing=ctx["tickSpacing"], **_math_kwargs(ctx))


# ---------------------------------------------------------------------------
# Pool loading
# ---------------------------------------------------------------------------


_POOL_KEY_ARGS = ("currency0", "currency1", "fee", "tick-spacing")


def pool_key_from_args(args: dict) -> dict | None:
    """A PoolKey spelled out on the command line, or None if it was not.

    The escape hatch for a pool no discovery path can reach: it needs no RPC and no
    registry, and the caller verifies it by recomputing the poolId — so a typo cannot
    silently address the wrong pool. ``--hooks`` defaults to the zero address, which is
    the whole point: a hook-less pool is exactly the case registries cannot describe.
    """
    given = {name: opt_str(args, name) for name in _POOL_KEY_ARGS}
    present = [name for name, value in given.items() if value is not None]
    if not present:
        return None
    if len(present) != len(_POOL_KEY_ARGS):
        missing = ", ".join(f"--{name}" for name in _POOL_KEY_ARGS if name not in present)
        raise RuntimeError(
            f"an explicit PoolKey needs all of --currency0 --currency1 --fee "
            f"--tick-spacing (missing: {missing}). --hooks is optional and defaults to "
            f"the zero address (no hook)."
        )
    # base 0 so a dynamic-fee pool can be given as 0x800000 as well as 8388608.
    try:
        fee = int(given["fee"], 0)
        tick_spacing = int(given["tick-spacing"], 0)
    except ValueError as exc:
        raise RuntimeError(
            f"--fee and --tick-spacing must be integers (hex accepted with an 0x "
            f"prefix): {exc}"
        ) from exc
    return normalize_pool_key({
        "currency0": given["currency0"],
        "currency1": given["currency1"],
        "fee": fee,
        "tickSpacing": tick_spacing,
        "hooks": opt_str(args, "hooks") or NATIVE,
    })


def pool_key_for_id(client, chain: dict, pool_id: str, args: dict | None = None) -> dict | None:
    """Recover the PoolKey behind a poolId.

    A poolId is a keccak hash, so it cannot be inverted — the PoolKey normally comes
    from the pool's Initialize log. On a chain that will not serve a wide eth_getLogs
    range that scan is thousands of requests, so cheaper routes are tried first:
    a PoolKey spelled out on the command line (free, and works for any pool anywhere),
    then ``--token``, which derives candidates from the launchpad registry and — for a
    hook-less pool, which no registry knows about — from the conventional fee tiers.
    Both derivations are keccak-only; confirming them costs at most one multicall.
    """
    args = args or {}

    explicit = pool_key_from_args(args)
    if explicit:
        derived_id = compute_pool_id(explicit)
        if derived_id.lower() != pool_id.lower():
            raise RuntimeError(
                f"the PoolKey given on the command line does not describe {pool_id}.\n"
                f"  given PoolKey hashes to {derived_id}\n"
                f"  currency0={explicit['currency0']} currency1={explicit['currency1']} "
                f"fee={explicit['fee']} tickSpacing={explicit['tickSpacing']} "
                f"hooks={explicit['hooks']}\n"
                "  Check the fee (a dynamic-fee pool is 0x800000, not the live lpFee), "
                "the tickSpacing,\n"
                "  --hooks, and that currency0 < currency1 by address — a PoolKey is "
                "order-sensitive."
            )
        return explicit

    if chain["logScan"].get("supportsFullRange") is not False:
        init = get_pool_init(client, chain, pool_id)
        return init["poolKey"] if init else None

    raw_token = opt_str(args, "token")
    if raw_token:
        token = checksum_address(raw_token)
        found = resolve_launcher(client, chain, token)
        if found:
            hit = next(
                (c for c in derive_pool_candidates(chain, token, hook=found["hook"],
                                                   numeraire=found["numeraire"])
                 if c["poolId"].lower() == pool_id.lower()),
                None,
            )
            if hit:
                return hit["poolKey"]
        # No launcher, or none of its pools is this one — the pool may simply have no
        # hook, which puts it outside every registry. That candidate set is pure keccak,
        # so trying it costs nothing but a few hashes.
        hit = next(
            (c for c in derive_vanilla_candidates(chain, token)
             if c["poolId"].lower() == pool_id.lower()),
            None,
        )
        if hit:
            return hit["poolKey"]
        raise RuntimeError(
            f"could not derive the PoolKey for {pool_id} from token {token} on "
            f"{chain['name']}. It is not a launch pool of any known launchpad, and it "
            f"is not a hook-less pool pairing this token with a known quote currency at "
            f"a conventional fee tier.\n"
            f"  Spell the PoolKey out to skip discovery entirely:\n"
            f"    --currency0 <addr> --currency1 <addr> --fee <n> --tick-spacing <n> "
            f"[--hooks <addr>]\n"
            f"  Or pass --scan-logs to fall back to an Initialize log scan (very slow "
            f"on this chain)."
        )

    if args.get("scan-logs"):
        init = get_pool_init(client, chain, pool_id)
        return init["poolKey"] if init else None

    raise RuntimeError(
        f"{chain['name']} cannot serve a wide eth_getLogs range, so the PoolKey for "
        f"{pool_id} cannot be looked up directly. Any of these works:\n"
        f"  --currency0 <addr> --currency1 <addr> --fee <n> --tick-spacing <n> "
        f"[--hooks <addr>]\n"
        f"                          give the PoolKey directly — no lookup, any pool\n"
        f"  --token <address>       derive it from the launchpad registry, or from the\n"
        f"                          hook-less fee tiers (fast)\n"
        f"  --scan-logs             scan anyway (very slow)"
    )


def load_pool(client, chain: dict, pool_id: str, pool_key_hint: dict | None = None,
              args: dict | None = None) -> dict:
    pool_key = pool_key_hint or pool_key_for_id(client, chain, pool_id, args)
    if not pool_key:
        raise RuntimeError(f"no Initialize event found for pool {pool_id}")

    slot0, liquidity = client.multicall([
        {"address": chain["stateView"], "abi": STATE_VIEW_ABI,
         "functionName": "getSlot0", "args": [pool_id]},
        {"address": chain["stateView"], "abi": STATE_VIEW_ABI,
         "functionName": "getLiquidity", "args": [pool_id]},
    ], allow_failure=False)

    sqrt_price_x96, tick, protocol_fee, lp_fee = slot0["result"]
    return {
        "poolId": pool_id,
        "poolKey": pool_key,
        "sqrtPriceX96": int(sqrt_price_x96),
        "tick": int(tick),
        "protocolFee": int(protocol_fee),
        "lpFee": int(lp_fee),
        "activeLiquidity": int(liquidity["result"]),
    }


# ---------------------------------------------------------------------------
# JSON shaping — the Node build stringified every BigInt; Python has one int type,
# so the values that were BigInt there are stringified explicitly here. The diff
# against the Node output is what keeps these in step.
# ---------------------------------------------------------------------------


def _pool_json(pool: dict) -> dict:
    return {
        "poolId": pool["poolId"],
        "poolKey": pool["poolKey"],
        "sqrtPriceX96": str(pool["sqrtPriceX96"]),
        "tick": pool["tick"],
        "protocolFee": pool["protocolFee"],
        "lpFee": pool["lpFee"],
        "activeLiquidity": str(pool["activeLiquidity"]),
    }


def _range_json(r: dict, ctx: dict | None = None, with_band: bool = False) -> dict:
    out = {
        "tickLower": r["tickLower"],
        "tickUpper": r["tickUpper"],
        "salt": r.get("salt"),
        "owner": r.get("owner"),
        "liquidity": str(r["liquidity"]),
        "amount0": str(r["amount0"]),
        "amount1": str(r["amount1"]),
        "status": r["status"],
    }
    if with_band:
        out["mcapBand"] = band_for(r["tickLower"], r["tickUpper"], ctx)
    return out


def _check_json(check: dict) -> dict:
    return {
        "activeSum": str(check["activeSum"]),
        "onChain": None if check["onChain"] is None else str(check["onChain"]),
        "ok": check["ok"],
    }


def _res_json(res: dict) -> dict:
    return {
        "ranges": [_range_json(r) for r in res["ranges"]],
        "amount0": str(res["amount0"]),
        "amount1": str(res["amount1"]),
        "eventCount": res["eventCount"],
        "mode": res["mode"],
        "truncated": res["truncated"],
        "check": _check_json(res["check"]),
    }


def emit(payload) -> None:
    print(json.dumps(json_safe(payload), indent=2))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def discover_via_launcher(client, chain: dict, token: str) -> dict | None:
    """Find a token's pools without scanning PoolManager logs.

    A launcher-deployed token's pool is fully determined by its hook, so the poolId can
    be derived and confirmed with a single getSlot0. This is the only workable path on
    Base, where a log scan is thousands of chunked requests.

    Returns None when the token was not launched by a known launcher, so the caller can
    fall back to the log scan.
    """
    found = resolve_launcher(client, chain, token)
    if not found:
        return None

    candidates = derive_pool_candidates(chain, token, hook=found["hook"],
                                        numeraire=found["numeraire"])
    if not candidates:
        return {"launcher": found, "inits": []}

    slot0s = client.multicall([
        {"address": chain["stateView"], "abi": STATE_VIEW_ABI,
         "functionName": "getSlot0", "args": [c["poolId"]]}
        for c in candidates
    ])

    inits = []
    for candidate, slot0 in zip(candidates, slot0s):
        # An uninitialized pool reads back as sqrtPriceX96 = 0 rather than reverting.
        if slot0["status"] != "success" or int(slot0["result"][0]) == 0:
            continue
        inits.append({**candidate, "blockNumber": 0, "transactionHash": None})
    return {"launcher": found, "inits": inits}


def _confirm_candidates(client, chain: dict, candidates: list[dict]) -> list[dict]:
    """Keep the candidate poolIds that are actually initialized on chain.

    One multicall regardless of how many candidates there are. An uninitialized pool
    reads back as sqrtPriceX96 = 0 rather than reverting.
    """
    if not candidates:
        return []
    slot0s = client.multicall([
        {"address": chain["stateView"], "abi": STATE_VIEW_ABI,
         "functionName": "getSlot0", "args": [c["poolId"]]}
        for c in candidates
    ])
    out = []
    for candidate, slot0 in zip(candidates, slot0s):
        if slot0["status"] != "success" or int(slot0["result"][0]) == 0:
            continue
        out.append({**candidate, "blockNumber": 0, "transactionHash": None})
    return out


def discover_vanilla_pools(client, chain: dict, token: str,
                           quotes: list[str] | None = None) -> list[dict]:
    """Hook-less pools holding ``token``, without touching a single log.

    Same trick as ``discover_via_launcher``, but the hook is pinned to the zero address
    instead of coming from a registry, so it needs no launchpad and works identically on
    every chain. Only finds pools paired with a known quote currency at a conventional
    fee tier — it is a fast probe, not an exhaustive index.
    """
    candidates = derive_vanilla_candidates(chain, token, quotes=quotes)
    return _confirm_candidates(client, chain, candidates)


def cmd_pools(client, chain: dict, args: dict) -> None:
    token = checksum_address(require_arg(args, "token", "token address"))
    min_tvl = -1.0 if args.get("all-pools") else opt_float(args, "min-tvl", 1.0)
    mode = opt_str(args, "mode") or chain.get("rangeMode") or "logs"
    raw_quote = opt_str(args, "quote")

    inits: list = []
    launcher = None
    no_hook_only = bool(args.get("no-hook"))
    used_vanilla = False
    # --quote lets the hook-less probe reach a pairing currency this chain's registry
    # does not list; without it every known quote is tried.
    vanilla_quotes = [checksum_address(raw_quote)] if raw_quote else None

    if no_hook_only:
        # Deliberately skips both the registry and the log scan: this asks one narrow
        # question — does a hook-less pool exist — and answers it in a single multicall
        # on any chain, whatever its eth_getLogs limits.
        used_vanilla = True
        inits = discover_vanilla_pools(client, chain, token, quotes=vanilla_quotes)
    else:
        if not args.get("scan-logs"):
            via_launcher = discover_via_launcher(client, chain, token)
            if via_launcher:
                launcher = via_launcher["launcher"]
                inits = via_launcher["inits"]

    used_registry = launcher is not None
    if not used_registry and not no_hook_only:
        # No launcher claims this token, so fall back to the Initialize log scan. On a
        # chain that cannot serve wide ranges that is thousands of sequential requests,
        # so probe for a hook-less pool first — a poolId with no hook is only a few
        # keccaks away, and one multicall settles it.
        if chain["logScan"].get("supportsFullRange") is False and not args.get("scan-logs"):
            used_vanilla = True
            inits = discover_vanilla_pools(client, chain, token, quotes=vanilla_quotes)
            if not inits:
                chunks = math.ceil(24_000_000 / chain["logScan"].get("chunkBlocks", 9_000))
                print(f"\nNo known launchpad on {chain['name']} deployed {token}, no "
                      "hook-less pool pairs it with a known quote currency, and this "
                      "chain cannot serve a wide eth_getLogs range.")
                print(f"A full Initialize scan here is ~{chunks} sequential requests and "
                      "will take a very long time.")
                print("\nOptions:")
                print("  --quote <address>     probe a hook-less pool against this "
                      "pairing currency")
                print("  --scan-logs           do the full scan anyway")
                print("  pool --id <poolId>    inspect a pool directly if you know its id")
                print("  launcher --token <a>  check which launchpad deployed a token")
                if launchers_for(chain):
                    names = ", ".join(entry["name"] for entry in launchers_for(chain))
                    print(f"\nKnown launchpads on {chain['name']}: {names}")
                if args.get("include-v3"):
                    report_v3(client, chain, token)
                sys.exit(2)
        else:
            inits = find_pools_for_token(client, chain, token)

    if not inits:
        kind = "hook-less Uniswap v4 pools" if no_hook_only else "Uniswap v4 pools"
        print(f"\nNo {kind} found for {token} on {chain['name']}.")
        if used_registry:
            print(f"  {launcher['name']} lists this token (hook {launcher['hook']}) but "
                  "no derived pool is initialized.")
            print("  Pass --scan-logs to fall back to a full log scan.")
        elif no_hook_only:
            print("  Probed every known quote currency at the conventional fee tiers "
                  "(0.01% / 0.05% / 0.30% / 1.00%).")
            print("  A pool paired with something else, or opened at an unusual fee or "
                  "tickSpacing,")
            print("  will not show up here. Options:")
            print("    --quote <address>   probe against a specific pairing currency")
            print("    (drop --no-hook)    full discovery via launchpad registry / logs")
        if args.get("include-v3"):
            report_v3(client, chain, token)
        return

    discovered = len(inits)
    if raw_quote:
        quote = checksum_address(raw_quote).lower()
        inits = [i for i in inits
                 if quote in (i["poolKey"]["currency0"].lower(),
                              i["poolKey"]["currency1"].lower())]
        if not inits:
            print(f"\nNone of the {discovered} v4 pools for {token} are paired with "
                  f"{raw_quote}.")
            return

    # Reserves cost one full log scan per pool. A quote asset like WETH sits in tens of
    # thousands of pools here, so refuse rather than grinding for an hour.
    max_pools = opt_int(args, "max-pools", 60)
    if len(inits) > max_pools and mode != "ticks":
        print(f"\n{len(inits)} v4 pools reference {token} on {chain['name']}.")
        print(f"That is more than --max-pools ({max_pools}), and each pool costs a full "
              "log scan.")
        print("\nNarrow it down, or raise the cap:")
        print("  --quote <address>     only pools paired with this currency")
        print("  --max-pools <n>       raise the cap (slow: roughly 1-2 s per pool)")
        print("\nThis token is probably a quote asset on this chain. To inspect one pool:")
        print("  lp_read.py pool --id <poolId>")
        sys.exit(2)

    # Gather metadata + prices once for every currency involved.
    currencies = [token.lower()]
    for init in inits:
        for key in ("currency0", "currency1"):
            lowered = init["poolKey"][key].lower()
            if lowered not in currencies:
                currencies.append(lowered)
    metas = {c: token_meta(client, chain, c) for c in currencies}
    prices = fetch_usd_prices(chain, currencies)

    # One multicall for every pool's slot0 + liquidity, and one JSON-RPC batch for every
    # pool's ModifyLiquidity history. Doing this per pool takes minutes on 40 pools.
    states = client.multicall([
        call
        for init in inits
        for call in (
            {"address": chain["stateView"], "abi": STATE_VIEW_ABI,
             "functionName": "getSlot0", "args": [init["poolId"]]},
            {"address": chain["stateView"], "abi": STATE_VIEW_ABI,
             "functionName": "getLiquidity", "args": [init["poolId"]]},
        )
    ])

    log_batches = None
    if mode != "ticks":
        log_batches = client.batch([
            {"method": "eth_getLogs", "params": [{
                "address": chain["poolManager"],
                "fromBlock": to_hex(chain["logScan"].get("fromBlock", 0)),
                "toBlock": "latest",
                "topics": [TOPIC_MODIFY_LIQUIDITY, init["poolId"]],
            }]}
            for init in inits
        ])

    detailed = []
    for i, init in enumerate(inits):
        slot0 = states[i * 2]
        liq = states[i * 2 + 1]
        logs = log_batches[i] if log_batches else None
        # A pool id we saw in an Initialize log but cannot read now — skip it rather
        # than aborting the whole sweep.
        if slot0["status"] != "success":
            continue
        if mode != "ticks" and not isinstance(logs, list):
            continue

        sqrt_price_x96, tick, protocol_fee, lp_fee = slot0["result"]
        pool = {
            "poolId": init["poolId"],
            "poolKey": init["poolKey"],
            "sqrtPriceX96": int(sqrt_price_x96),
            "tick": int(tick),
            "protocolFee": int(protocol_fee),
            "lpFee": int(lp_fee),
            "activeLiquidity": int(liq["result"]) if liq["status"] == "success" else None,
        }
        res = (walk_tick_ranges(client, chain, init["poolId"], pool) if mode == "ticks"
               else aggregate_ranges(logs, pool))
        m0 = metas[pool["poolKey"]["currency0"].lower()]
        m1 = metas[pool["poolKey"]["currency1"].lower()]
        p0 = prices.get(m0["address"].lower())
        p1 = prices.get(m1["address"].lower())
        usd0 = None if p0 is None else float(res["amount0"]) / 10 ** m0["decimals"] * p0
        usd1 = None if p1 is None else float(res["amount1"]) / 10 ** m1["decimals"] * p1
        tvl = None if usd0 is None and usd1 is None else (usd0 or 0) + (usd1 or 0)

        ctx = mcap_context(pool["poolKey"], token, m0, m1, prices)
        detailed.append({"init": init, "pool": pool, "res": res, "m0": m0, "m1": m1,
                         "tvl": tvl, "ctx": ctx})

    detailed.sort(key=lambda d: -1 if d["tvl"] is None else d["tvl"], reverse=True)

    shown = [d for d in detailed if min_tvl < 0 or (d["tvl"] or 0) >= min_tvl]
    hidden = len(detailed) - len(shown)

    if args.get("json"):
        emit({
            "chain": chain["key"],
            "token": token,
            "pools": [{
                "poolId": d["init"]["poolId"],
                "poolKey": d["pool"]["poolKey"],
                "tick": d["pool"]["tick"],
                "lpFee": d["pool"]["lpFee"],
                "amount0": str(d["res"]["amount0"]),
                "amount1": str(d["res"]["amount1"]),
                "symbol0": d["m0"]["symbol"],
                "symbol1": d["m1"]["symbol"],
                "tvlUsd": d["tvl"],
                "liquidityCheck": _check_json(d["res"]["check"]),
                "ranges": [_range_json(r, d["ctx"], with_band=True)
                           for r in d["res"]["ranges"]],
            } for d in detailed],
        })
        return

    rows = [{
        "pool": short_id(d["init"]["poolId"]),
        "pair": f"{d['m0']['symbol']}/{d['m1']['symbol']}",
        "fee": format_fee(d["pool"]["poolKey"]["fee"], d["pool"]["lpFee"]),
        "spacing": str(d["pool"]["poolKey"]["tickSpacing"]),
        "hook": "—" if d["pool"]["poolKey"]["hooks"] == NATIVE
                else short(d["pool"]["poolKey"]["hooks"]),
        "tick": str(d["pool"]["tick"]),
        "amount0": fmt_units(d["res"]["amount0"], d["m0"]["decimals"], 4),
        "amount1": fmt_units(d["res"]["amount1"], d["m1"]["decimals"], 4),
        "tvl": fmt_usd(d["tvl"]),
        "mcap": fmt_band(band_for(*pool_band_ticks(d), d["ctx"])),
    } for d in shown]

    token_meta_ref = metas[token.lower()]
    print(heading(f"v4 pools holding {token_meta_ref['symbol']} on {chain['name']}"))
    if used_registry:
        print(f"  launched by {launcher['name']} — pool(s) derived from its registry, "
              f"no log scan. hook {launcher['hook']}")
    elif used_vanilla:
        print("  hook-less probe — poolIds derived with hooks = 0 across the "
              "conventional fee tiers, no log scan.")
        print("  Only pools paired with a known quote currency are covered; this is "
              "not an exhaustive index.")
    print(render_table([
        {"key": "pool", "label": "poolId"},
        {"key": "pair", "label": "pair"},
        {"key": "fee", "label": "fee"},
        {"key": "spacing", "label": "spc", "align": "right"},
        {"key": "hook", "label": "hook"},
        {"key": "tick", "label": "tick", "align": "right"},
        {"key": "amount0", "label": "reserve0", "align": "right"},
        {"key": "amount1", "label": "reserve1", "align": "right"},
        {"key": "tvl", "label": "TVL", "align": "right"},
        {"key": "mcap", "label": "mcap span (all ranges)"},
    ], rows))
    if hidden > 0:
        print(f"\n  {hidden} dust pool(s) below {fmt_usd(min_tvl)} omitted — pass "
              "--all-pools to see them.")
    if raw_quote:
        print(f"  filtered to pools paired with {checksum_address(raw_quote)} "
              f"({discovered} pools reference this token in total).")

    # Per-range detail for the deepest pool, which is what people actually want.
    top = shown[0] if shown else (detailed[0] if detailed else None)
    if top:
        print(heading(f"ranges in the deepest pool {short_id(top['init']['poolId'])}"))
        print_ranges(top["res"], top["m0"], top["m1"], top["ctx"], chain)
        print_recommendation(top, chain, len(detailed))

    if args.get("include-v3"):
        report_v3(client, chain, token)


def print_recommendation(top: dict, chain: dict, total: int) -> None:
    """Name the pool to work in and spell out the next command.

    A launched token has dozens of pools and all but one are dust — picking among them by
    eye is how a caller ends up sizing a position in a $60 pool at an 85% fee. Deepest TVL
    is the answer often enough to be worth stating outright, with the id in full so the
    next command is a copy rather than a lookup.
    """
    pool_id = top["init"]["poolId"]
    hooks = top["pool"]["poolKey"]["hooks"]
    hooked = hooks != NATIVE
    label = label_address(chain, hooks) if hooked else None
    pair = f"{top['m0']['symbol']}/{top['m1']['symbol']}"

    print(f"\n  recommended pool : {pool_id}")
    print(f"                     {pair}, TVL {fmt_usd(top['tvl'])}, deepest of "
          f"{total} pool(s) holding this token")
    if hooked:
        print(f"                     hook {hooks}"
              f"{f' ({label})' if label else ''} — mint needs --allow-hooked (see §8)")
    else:
        print("                     no hook — mint takes it without --allow-hooked")
    print("\n  next: turn a market-cap target into ticks, then dry-run the mint")
    print(f"    lp_read.py  ticks --pool {short_id(pool_id)} "
          "--mcap-lower <usd> --mcap-upper <usd> --from-current")
    print(f"    lp_write.py mint  --pool {short_id(pool_id)} "
          "--tick-lower <t> --tick-upper <t> (--amount0 <n> | --amount1 <n>)"
          f"{' --allow-hooked' if hooked else ''}")
    print("  Use the poolId in full above; --from-current keeps a range that starts at "
          "today's price single-sided.")


def pool_band_ticks(d: dict) -> tuple[int, int]:
    """The band spanned by the union of a pool's live ranges."""
    live = d["res"]["ranges"]
    if not live:
        return d["pool"]["tick"], d["pool"]["tick"]
    return (min(r["tickLower"] for r in live), max(r["tickUpper"] for r in live))


def owner_cell(chain: dict, owner: str | None) -> str:
    """Owner cell: a protocol name when we know the address, else a short address."""
    if not owner:
        return "—"
    label = label_address(chain, owner)
    locked = " LOCKED" if is_locker_address(chain, owner) else ""
    return f"{label or short(owner)}{locked}"


def print_ranges(res: dict, m0: dict, m1: dict, ctx: dict, chain: dict) -> None:
    has_owners = any(r.get("owner") for r in res["ranges"])
    rows = [{
        "ticks": f"{r['tickLower']} → {r['tickUpper']}",
        "mcap": fmt_band(band_for(r["tickLower"], r["tickUpper"], ctx)),
        "liquidity": str(r["liquidity"]),
        "amount0": fmt_units(r["amount0"], m0["decimals"], 4),
        "amount1": fmt_units(r["amount1"], m1["decimals"], 4),
        "owner": owner_cell(chain, r.get("owner")),
        "status": "IN RANGE" if r["status"] == "in-range" else f"{r['status']} range",
    } for r in res["ranges"]]

    columns = [
        {"key": "ticks", "label": "ticks"},
        {"key": "mcap", "label": "added from mcap → to mcap"},
        {"key": "amount0", "label": m0["symbol"], "align": "right"},
        {"key": "amount1", "label": m1["symbol"], "align": "right"},
        {"key": "liquidity", "label": "liquidity", "align": "right"},
    ]
    if has_owners:
        columns.append({"key": "owner", "label": "owner"})
    columns.append({"key": "status", "label": "status"})
    print(render_table(columns, rows))

    check = res["check"]
    if check["onChain"] is not None:
        verdict = "OK" if check["ok"] else "MISMATCH (numbers below are not trustworthy)"
        print(f"\n  self-check: Σ active liquidity {check['activeSum']} vs "
              f"StateView.getLiquidity {check['onChain']} — {verdict}")
    if res["mode"] == "ticks":
        print("  read from the tick bitmap, not from logs: rows are merged liquidity "
              "segments, not individual LP positions, so there is no per-owner "
              "attribution. Totals are exact. Pass --mode logs for attribution.")
    if res["truncated"]:
        t = res["truncated"]
        print(f"  NOTE: scanned {t['scannedWords']} of {t['fullWords']} bitmap words "
              f"(centred on the current tick) — liquidity outside words "
              f"{t['fromWord']}..{t['toWord']} is NOT included.")
    print("  reserves exclude uncollected LP fees and protocol fees.")


def cmd_pool(client, chain: dict, args: dict) -> None:
    pool_id = require_arg(args, "id", "poolId")
    pool = load_pool(client, chain, pool_id, None, args)
    m0 = token_meta(client, chain, pool["poolKey"]["currency0"])
    m1 = token_meta(client, chain, pool["poolKey"]["currency1"])
    prices = fetch_usd_prices(chain, [m0["address"], m1["address"]])
    res = get_pool_ranges(client, chain, pool_id, pool, mode=opt_str(args, "mode"))

    # Default the mcap perspective to whichever side is not a known quote asset.
    known = chain.get("knownQuotes") or {}
    token = m0["address"] if known.get(m1["address"].lower()) else m1["address"]
    ctx = mcap_context(pool["poolKey"], token, m0, m1, prices)
    token_meta_ref = m1 if ctx["tokenIsCurrency1"] else m0

    p0 = prices.get(m0["address"].lower())
    p1 = prices.get(m1["address"].lower())
    tvl = ((0 if p0 is None else float(res["amount0"]) / 10 ** m0["decimals"] * p0)
           + (0 if p1 is None else float(res["amount1"]) / 10 ** m1["decimals"] * p1))

    if args.get("json"):
        emit({"pool": _pool_json(pool), "res": _res_json(res), "tvl": tvl,
              "ctx": {**ctx, "totalSupply": str(ctx["totalSupply"])}})
        return

    recomputed = compute_pool_id(pool["poolKey"])
    hook_label = label_address(chain, pool["poolKey"]["hooks"])
    dynamic_note = ("  [PoolKey.fee = 0x800000]" if is_dynamic_fee(pool["poolKey"]["fee"])
                    else "")
    unit_price = mcap_at_tick(
        pool["tick"], **_math_kwargs(ctx, total_supply=10 ** int(ctx["tokenDecimals"]))
    )
    print(heading(f"pool {pool_id}"))
    print(render_kv([
        ("chain", f"{chain['name']} ({chain['chainId']})"),
        ("currency0", f"{m0['symbol']}  {m0['address']}"
                      f"{'  (native)' if m0['isNative'] else ''}"),
        ("currency1", f"{m1['symbol']}  {m1['address']}"),
        ("fee", format_fee(pool["poolKey"]["fee"], pool["lpFee"]) + dynamic_note),
        ("tickSpacing", str(pool["poolKey"]["tickSpacing"])),
        ("hooks", "none" if pool["poolKey"]["hooks"] == NATIVE
                  else f"{pool['poolKey']['hooks']}"
                       f"{f'  ({hook_label})' if hook_label else ''}"),
        ("hook flags", format_hook_flags(pool["poolKey"]["hooks"])),
        ("sqrtPriceX96", str(pool["sqrtPriceX96"])),
        ("tick", str(pool["tick"])),
        ("active liquidity", str(pool["activeLiquidity"])),
        ("poolId recompute",
         f"{recomputed} {'OK' if recomputed.lower() == pool_id.lower() else 'MISMATCH'}"),
        ("reserve0", f"{fmt_units(res['amount0'], m0['decimals'])} {m0['symbol']}"),
        ("reserve1", f"{fmt_units(res['amount1'], m1['decimals'])} {m1['symbol']}"),
        ("TVL", fmt_usd(tvl)),
        (f"{token_meta_ref['symbol']} price",
         "n/a" if ctx["quoteUsd"] is None else f"{fmt_usd(unit_price)} / token"),
        (f"{token_meta_ref['symbol']} mcap",
         fmt_usd(mcap_at_tick(pool["tick"], **_math_kwargs(ctx)))),
        ("reserves read via",
         "tick bitmap (no logs)" if res["mode"] == "ticks"
         else f"ModifyLiquidity logs ({res['eventCount']} events)"),
    ]))

    limit = opt_int(args, "ranges", len(res["ranges"]))
    unit = "liquidity segments" if res["mode"] == "ticks" else "liquidity ranges"
    print(heading(f"{unit} ({min(limit, len(res['ranges']))} of {len(res['ranges'])})"))
    print_ranges({**res, "ranges": res["ranges"][:limit]}, m0, m1, ctx, chain)


def load_position(client, chain: dict, token_id: int) -> dict:
    pool_and_info, liquidity, owner = client.multicall([
        {"address": chain["positionManager"], "abi": POSITION_MANAGER_ABI,
         "functionName": fn, "args": [int(token_id)]}
        for fn in ("getPoolAndPositionInfo", "getPositionLiquidity", "ownerOf")
    ])
    if pool_and_info["status"] != "success":
        raise RuntimeError(f"tokenId {token_id} does not exist on {chain['name']}")

    raw_key, info = pool_and_info["result"]
    pool_key = normalize_pool_key(raw_key)
    decoded = decode_position_info(info)
    pool_id = compute_pool_id(pool_key)
    return {
        "tokenId": int(token_id),
        "poolKey": pool_key,
        "poolId": pool_id,
        **decoded,
        "poolIdMatches": pool_id_matches_truncated(pool_id, decoded["truncatedPoolId"]),
        "liquidity": int(liquidity["result"]) if liquidity["status"] == "success" else 0,
        "owner": owner["result"] if owner["status"] == "success" else None,
    }


def cmd_position(client, chain: dict, args: dict) -> None:
    token_id = int(require_arg(args, "token-id", "position NFT id"))
    pos = load_position(client, chain, token_id)
    pool = load_pool(client, chain, pos["poolId"], pos["poolKey"])
    m0 = token_meta(client, chain, pool["poolKey"]["currency0"])
    m1 = token_meta(client, chain, pool["poolKey"]["currency1"])
    prices = fetch_usd_prices(chain, [m0["address"], m1["address"]])

    principal = get_amounts_for_liquidity_at_ticks(
        pool["sqrtPriceX96"], pos["tickLower"], pos["tickUpper"], pos["liquidity"]
    )
    fees = {"fees0": 0, "fees1": 0}
    if not args.get("no-fees") and pos["liquidity"] > 0:
        try:
            fees = get_fees_owed(client, chain, pos["poolId"], pos["tickLower"],
                                 pos["tickUpper"], pos["tokenId"])
        except Exception:  # noqa: BLE001 — fees are a nice-to-have, never fatal
            fees = {"fees0": None, "fees1": None}

    known = chain.get("knownQuotes") or {}
    token = m0["address"] if known.get(m1["address"].lower()) else m1["address"]
    ctx = mcap_context(pool["poolKey"], token, m0, m1, prices)
    band = band_for(pos["tickLower"], pos["tickUpper"], ctx)
    p0 = prices.get(m0["address"].lower())
    p1 = prices.get(m1["address"].lower())

    def usd(amount0: int, amount1: int) -> float:
        return ((0 if p0 is None else float(amount0) / 10 ** m0["decimals"] * p0)
                + (0 if p1 is None else float(amount1) / 10 ** m1["decimals"] * p1))

    if args.get("json"):
        pos_json = {**pos, "tokenId": str(pos["tokenId"]),
                    "liquidity": str(pos["liquidity"])}
        fees_json = {k: (None if v is None else str(v)) for k, v in fees.items()}
        emit({"pos": pos_json, "pool": _pool_json(pool),
              "principal": {"amount0": str(principal["amount0"]),
                            "amount1": str(principal["amount1"])},
              "fees": fees_json, "band": band})
        return

    owner_label = label_address(chain, pos["owner"]) if pos["owner"] else None
    if pos["owner"] is None:
        owner_text = "(burned)"
    elif owner_label:
        lock = " — LP is LOCKED" if is_locker_address(chain, pos["owner"]) else ""
        owner_text = f"{pos['owner']}  ({owner_label}{lock})"
    else:
        owner_text = pos["owner"]

    if fees["fees0"] is None:
        fees_text = "n/a"
        fees_usd_text = "n/a"
        total_text = fmt_usd(usd(principal["amount0"], principal["amount1"]))
    else:
        fees_text = (f"{fmt_units(fees['fees0'], m0['decimals'])} {m0['symbol']} + "
                     f"{fmt_units(fees['fees1'], m1['decimals'])} {m1['symbol']}")
        fees_usd_text = fmt_usd(usd(fees["fees0"], fees["fees1"]))
        total_text = fmt_usd(usd(principal["amount0"] + fees["fees0"],
                                 principal["amount1"] + fees["fees1"]))

    print(heading(f"position #{token_id} on {chain['name']}"))
    print(render_kv([
        ("owner", owner_text),
        ("pair", f"{m0['symbol']} / {m1['symbol']}"),
        ("currency0", f"{m0['address']}{'  (native ETH)' if m0['isNative'] else ''}"),
        ("currency1", m1["address"]),
        ("fee", format_fee(pool["poolKey"]["fee"], pool["lpFee"])),
        ("tickSpacing", str(pool["poolKey"]["tickSpacing"])),
        ("hooks", "none" if pool["poolKey"]["hooks"] == NATIVE
                  else f"{pool['poolKey']['hooks']}  "
                       f"{format_hook_flags(pool['poolKey']['hooks'])}"),
        ("poolId", f"{pos['poolId']} "
                   f"{'OK' if pos['poolIdMatches'] else 'MISMATCH vs packed info'}"),
        ("ticks", f"{pos['tickLower']} → {pos['tickUpper']}"),
        ("added from mcap", fmt_band(band)),
        ("current tick", f"{pool['tick']}  "
                         f"({range_status(pool['tick'], pos['tickLower'], pos['tickUpper'])})"),
        ("current mcap", fmt_usd(mcap_at_tick(pool["tick"], **_math_kwargs(ctx)))),
        ("liquidity", str(pos["liquidity"])),
        ("hasSubscriber", "true" if pos["hasSubscriber"] else "false"),
        ("principal", f"{fmt_units(principal['amount0'], m0['decimals'])} {m0['symbol']} + "
                      f"{fmt_units(principal['amount1'], m1['decimals'])} {m1['symbol']}"),
        ("principal USD", fmt_usd(usd(principal["amount0"], principal["amount1"]))),
        ("fees owed", fees_text),
        ("fees USD", fees_usd_text),
        ("total USD", total_text),
    ]))


def resolve_owner(args: dict) -> str:
    """``--owner``, or the wallet the signing key derives when it is left off.

    "Show my positions" should not require the user to recite their own address, and the
    address is already implied by the key the skill is configured with. This is the one
    place in this file that touches ``UNIV4_LP_PRIVATE_KEY``, it reads it only to derive
    the address, and the key never enters a variable here — see
    ``chains.resolve_signer_address`` for why that stays incapable of signing.
    """
    given = opt_str(args, "owner")
    if given:
        return given
    signer_env = opt_str(args, "signer-env") or ENV_SIGNER
    try:
        return resolve_signer_address(signer_env)
    except RuntimeError as exc:
        die(f"{exc}\n\nOr name the wallet directly: positions --owner <address>")


def cmd_positions(client, chain: dict, args: dict) -> None:
    owner = checksum_address(resolve_owner(args))

    # This command has no registry shortcut: finding a wallet's NFTs means scanning
    # ERC-721 Transfer logs, which a chain with a hard getLogs range cap cannot serve in
    # reasonable time. Refuse up front rather than appearing to hang.
    if chain["logScan"].get("supportsFullRange") is False and not args.get("scan-logs"):
        chunks = math.ceil(24_000_000 / chain["logScan"].get("chunkBlocks", 9_000))
        print(f"\n{chain['name']} caps eth_getLogs ranges, so enumerating a wallet's v4 "
              f"positions needs ~{chunks} sequential requests.")
        print("\nOptions:")
        print("  position --token-id <id>   inspect one position directly (no log scan)")
        print("  launcher --token <addr>    find a launched token's locked positions")
        print("  --scan-logs                scan anyway (very slow)")
        sys.exit(2)

    # There is no enumerable index on the v4 PositionManager and nextTokenId is in the
    # hundreds of thousands, so scan inbound ERC-721 Transfers and re-verify ownership.
    logs = get_logs_chunked(client, chain, chain["positionManager"],
                            [TOPIC_ERC721_TRANSFER, None, pad(owner.lower(), size=32)])
    candidates: list[int] = []
    for log in logs:
        token_id = int(log["topics"][3], 16)
        if token_id not in candidates:
            candidates.append(token_id)

    if not candidates:
        print(f"\nNo v4 positions ever received by {owner} on {chain['name']}.")
        return

    live = []
    for start in range(0, len(candidates), 100):
        window = candidates[start:start + 100]
        results = client.multicall([
            {"address": chain["positionManager"], "abi": POSITION_MANAGER_ABI,
             "functionName": fn, "args": [token_id]}
            for token_id in window
            # ownerOf reverts on burned ids, which is normal here.
            for fn in ("ownerOf", "getPositionLiquidity", "getPoolAndPositionInfo")
        ])
        for j, token_id in enumerate(window):
            own, liq, info = results[j * 3:j * 3 + 3]
            if own["status"] != "success" or own["result"].lower() != owner.lower():
                continue
            if info["status"] != "success":
                continue
            liquidity = int(liq["result"]) if liq["status"] == "success" else 0
            if liquidity == 0 and not args.get("include-empty"):
                continue
            raw_key, packed = info["result"]
            live.append({"tokenId": token_id, "poolKey": normalize_pool_key(raw_key),
                         "liquidity": liquidity, **decode_position_info(packed)})

    if not live:
        print(f"\n{owner} currently owns no v4 positions with liquidity on "
              f"{chain['name']} ({len(candidates)} tokenId(s) seen historically). "
              "Pass --include-empty to list drained ones.")
        return

    rows = []
    for p in live:
        pool_id = compute_pool_id(p["poolKey"])
        pool = load_pool(client, chain, pool_id, p["poolKey"])
        m0 = token_meta(client, chain, p["poolKey"]["currency0"])
        m1 = token_meta(client, chain, p["poolKey"]["currency1"])
        prices = fetch_usd_prices(chain, [m0["address"], m1["address"]])
        known = chain.get("knownQuotes") or {}
        token = m0["address"] if known.get(m1["address"].lower()) else m1["address"]
        ctx = mcap_context(p["poolKey"], token, m0, m1, prices)
        amounts = get_amounts_for_liquidity_at_ticks(
            pool["sqrtPriceX96"], p["tickLower"], p["tickUpper"], p["liquidity"]
        )
        p0 = prices.get(m0["address"].lower())
        p1 = prices.get(m1["address"].lower())
        rows.append({
            "tokenId": str(p["tokenId"]),
            "pair": f"{m0['symbol']}/{m1['symbol']}",
            "ticks": f"{p['tickLower']} → {p['tickUpper']}",
            "mcap": fmt_band(band_for(p["tickLower"], p["tickUpper"], ctx)),
            "status": range_status(pool["tick"], p["tickLower"], p["tickUpper"]),
            "amount0": fmt_units(amounts["amount0"], m0["decimals"], 4),
            "amount1": fmt_units(amounts["amount1"], m1["decimals"], 4),
            "usd": fmt_usd(
                (0 if p0 is None else float(amounts["amount0"]) / 10 ** m0["decimals"] * p0)
                + (0 if p1 is None else float(amounts["amount1"]) / 10 ** m1["decimals"] * p1)
            ),
        })

    if args.get("json"):
        emit({"owner": owner,
              "positions": [{**p, "tokenId": str(p["tokenId"]),
                             "liquidity": str(p["liquidity"])} for p in live]})
        return

    print(heading(f"v4 positions owned by {owner}"))
    print(render_table([
        {"key": "tokenId", "label": "tokenId", "align": "right"},
        {"key": "pair", "label": "pair"},
        {"key": "ticks", "label": "ticks"},
        {"key": "mcap", "label": "added from mcap → to mcap"},
        {"key": "status", "label": "status"},
        {"key": "amount0", "label": "amount0", "align": "right"},
        {"key": "amount1", "label": "amount1", "align": "right"},
        {"key": "usd", "label": "USD", "align": "right"},
    ], rows))


def cmd_launcher(client, chain: dict, args: dict) -> None:
    token = checksum_address(require_arg(args, "token", "token address"))
    found = resolve_launcher(client, chain, token)
    meta = token_meta(client, chain, token)

    if not found:
        known = queryable_launchers(chain)
        if args.get("json"):
            emit({"chain": chain["key"], "token": token, "launcher": None})
            return
        print(heading(f"launcher for {meta['symbol']} on {chain['name']}"))
        print(f"  No known launchpad on {chain['name']} claims {token}.")
        if not known:
            print("  No launchpads are registered for this chain — "
                  "see assets/v4-reference.md.")
        else:
            print(f"  Checked: {', '.join(entry['name'] for entry in known)}.")
        print("  It may predate these registries, or use a launcher not in the registry.")
        print(f"  Fall back to: lp_read.py pools --token {token} "
              f"--chain {chain['key']} --scan-logs")
        return

    # Confirm which derived candidates are real pools.
    candidates = derive_pool_candidates(chain, token, hook=found["hook"],
                                        numeraire=found["numeraire"])
    slot0s = client.multicall([
        {"address": chain["stateView"], "abi": STATE_VIEW_ABI,
         "functionName": "getSlot0", "args": [c["poolId"]]}
        for c in candidates
    ])
    live = [c for c, slot0 in zip(candidates, slot0s)
            if slot0["status"] == "success" and int(slot0["result"][0]) != 0]

    positions = (probe_position_ids(client, chain, found["locker"], token)
                 if found["locker"] else [])

    if args.get("json"):
        emit({"chain": chain["key"], "token": token, "symbol": meta["symbol"],
              "launcher": found, "pools": live,
              "positions": [{**p, "tokenId": str(p["tokenId"]),
                             "liquidity": None if p["liquidity"] is None
                             else str(p["liquidity"])} for p in positions]})
        return

    hook_label = label_address(chain, found["hook"]) or "(version not in registry)"
    locker_label = label_address(chain, found["locker"]) or "" if found["locker"] else ""
    quote_label = ((chain.get("knownQuotes") or {}).get(found["numeraire"].lower(), "")
                   if found["numeraire"] else "")
    print(heading(f"launcher for {meta['symbol']} on {chain['name']}"))
    print(render_kv([
        ("token", f"{meta['symbol']}  {token}"),
        ("launcher", f"{found['name']}  ({found['launcher']})"),
        ("hook", f"{found['hook']}  {hook_label}"),
        ("hook flags", format_hook_flags(found["hook"])),
        ("LP locker", f"{found['locker']}  {locker_label}" if found["locker"]
                      else "none (LP is held on the PoolManager directly)"),
        ("numeraire", f"{found['numeraire']}  {quote_label}" if found["numeraire"]
                      else "not published — derived from known quotes"),
        ("LP custody",
         "PositionManager NFT held by the locker — permanently locked" if found["locker"]
         else "minted on the PoolManager by the initializer — no NFT"),
        ("docs", found.get("docs") or "—"),
    ]))

    print(heading(f"derived pools ({len(live)} of {len(candidates)} candidates initialized)"))
    print(render_table([
        {"key": "poolId", "label": "poolId"},
        {"key": "pair", "label": "currency0 / currency1"},
        {"key": "fee", "label": "fee"},
        {"key": "spacing", "label": "spc", "align": "right"},
    ], [{
        "poolId": c["poolId"],
        "pair": f"{short(c['poolKey']['currency0'])} / {short(c['poolKey']['currency1'])}",
        "fee": format_fee(c["poolKey"]["fee"]),
        "spacing": str(c["poolKey"]["tickSpacing"]),
    } for c in live]))

    if found["locker"]:
        print(heading(f"locked LP positions ({len(positions)})"))
        print(render_table([
            {"key": "tokenId", "label": "tokenId", "align": "right"},
            {"key": "ticks", "label": "ticks"},
            {"key": "liquidity", "label": "liquidity", "align": "right"},
            {"key": "poolId", "label": "poolId"},
            {"key": "holder", "label": "held by"},
        ], [{
            "tokenId": str(p["tokenId"]),
            "ticks": "—" if p["tickLower"] is None
                     else f"{p['tickLower']} → {p['tickUpper']}",
            "liquidity": "—" if p["liquidity"] is None else str(p["liquidity"]),
            "poolId": short_id(p["poolId"]),
            "holder": owner_cell(chain, found["locker"]),
        } for p in positions]))
        if positions:
            print(f"\n  inspect one with:  lp_read.py position "
                  f"--token-id {positions[0]['tokenId']} --chain {chain['key']}")
        else:
            print("\n  the locker published no position id for this token in a shape "
                  "we could verify.")

    if live:
        # --token is what lets `pool` recover the PoolKey without an Initialize scan.
        print(f"\n  full pool detail:  lp_read.py pool --id {live[0]['poolId']} "
              f"--token {token} --chain {chain['key']}")


def cmd_ticks(client, chain: dict, args: dict) -> None:
    pool_id = require_arg(args, "pool", "poolId")
    pool = load_pool(client, chain, pool_id, None, args)
    m0 = token_meta(client, chain, pool["poolKey"]["currency0"])
    m1 = token_meta(client, chain, pool["poolKey"]["currency1"])
    prices = fetch_usd_prices(chain, [m0["address"], m1["address"]])

    known = chain.get("knownQuotes") or {}
    raw_token = opt_str(args, "token")
    if raw_token:
        token = checksum_address(raw_token)
    else:
        token = m0["address"] if known.get(m1["address"].lower()) else m1["address"]
    ctx = mcap_context(pool["poolKey"], token, m0, m1, prices)
    token_meta_ref = m1 if ctx["tokenIsCurrency1"] else m0
    spacing = pool["poolKey"]["tickSpacing"]

    raw_lower = opt_str(args, "tick-lower")
    raw_upper = opt_str(args, "tick-upper")
    if raw_lower is not None and raw_upper is not None:
        tick_lower = snap_tick(int(raw_lower), spacing, "down")
        tick_upper = snap_tick(int(raw_upper), spacing, "up")
    else:
        lo = float(require_arg(args, "mcap-lower", "lower market cap in USD"))
        hi = float(require_arg(args, "mcap-upper", "upper market cap in USD"))
        if not (lo > 0 and hi > 0):
            raise ValueError("--mcap-lower and --mcap-upper must both be > 0")
        # Given the two ends of a band in either order, take the band. Which one is the
        # bigger number depends on whether the target sits above or below where the token
        # trades now, and refusing the reversed pair only sends the caller round again.
        if hi < lo:
            lo, hi = hi, lo
        if hi == lo:
            raise ValueError("--mcap-lower and --mcap-upper describe a zero-width band")
        if not ctx["hasSupply"]:
            raise RuntimeError(f"cannot read totalSupply for {token_meta_ref['symbol']}")
        if ctx["quoteUsd"] is None:
            raise RuntimeError(price_unavailable_message(chain, quote_symbol=(
                m0["symbol"] if ctx["tokenIsCurrency1"] else m1["symbol"])))

        t_lo = tick_at_mcap(lo, **_math_kwargs(ctx))
        t_hi = tick_at_mcap(hi, **_math_kwargs(ctx))
        # Higher mcap == lower tick when the token is currency1; normalise then snap out.
        a, b = (t_lo, t_hi) if t_lo <= t_hi else (t_hi, t_lo)
        tick_lower = snap_tick(a, spacing, "down")
        tick_upper = snap_tick(b, spacing, "up")
        if args.get("from-current"):
            tick_lower, tick_upper = pull_off_current_tick(
                pool["tick"], tick_lower, tick_upper, spacing
            )

    band = mcap_band_for_range(tick_lower, tick_upper, tick_spacing=ctx["tickSpacing"],
                               **_math_kwargs(ctx))
    status = range_status(pool["tick"], tick_lower, tick_upper)
    if status == "below":
        side_note = f"single-sided: 100% {m0['symbol']} (currency0)"
    elif status == "above":
        side_note = f"single-sided: 100% {m1['symbol']} (currency1)"
    else:
        side_note = "two-sided: needs both currencies"

    if args.get("json"):
        emit({"tickLower": tick_lower, "tickUpper": tick_upper, "band": band,
              "status": status, "spacing": spacing})
        return

    price_kwargs = {"token_is_currency1": ctx["tokenIsCurrency1"],
                    "decimals0": ctx["decimals0"], "decimals1": ctx["decimals1"]}
    print(heading(f"tick range for pool {short_id(pool_id)}"))
    print(render_kv([
        ("pair", f"{m0['symbol']} / {m1['symbol']}"),
        ("token perspective",
         f"{token_meta_ref['symbol']} (currency{'1' if ctx['tokenIsCurrency1'] else '0'})"),
        ("tickSpacing", str(spacing)),
        ("current tick", str(pool["tick"])),
        ("current mcap", fmt_usd(mcap_at_tick(pool["tick"], **_math_kwargs(ctx)))),
        ("--tick-lower", str(tick_lower)),
        ("--tick-upper", str(tick_upper)),
        ("mcap band", fmt_band(band)),
        ("price at tickLower",
         _js_number(token_price_in_quote_at_tick(tick_lower, **price_kwargs))),
        ("price at tickUpper",
         _js_number(token_price_in_quote_at_tick(tick_upper, **price_kwargs))),
        ("sqrtPriceX96 lower", str(get_sqrt_ratio_at_tick(tick_lower))),
        ("sqrtPriceX96 upper", str(get_sqrt_ratio_at_tick(tick_upper))),
        ("position type", side_note),
    ]))


def _js_number(value: float) -> str:
    """Render a float the way ``String(number)`` does in JavaScript."""
    if value != value or math.isinf(value):
        return "Infinity" if value > 0 else ("-Infinity" if value < 0 else "NaN")
    if value == int(value) and abs(value) < 1e21:
        return str(int(value))
    # Python renders exponents zero-padded ("1e-07"); JavaScript does not ("1e-7").
    return repr(value).replace("e-0", "e-").replace("e+0", "e+")


def cmd_price(client, chain: dict, args: dict) -> None:
    tokens = [t.strip() for t in
              str(require_arg(args, "tokens", "comma-separated addresses")).split(",")]
    prices = fetch_usd_prices(chain, tokens)
    if args.get("json"):
        emit(prices)
        return
    print(heading(f"USD prices on {chain['name']}"))
    print(render_table(
        [{"key": "token", "label": "token"},
         {"key": "price", "label": "USD", "align": "right"}],
        [{"token": t, "price": fmt_usd(prices.get(t.lower()))} for t in tokens],
    ))


# ---------------------------------------------------------------------------
# Uniswap V3 (secondary — v4 is the main event on these chains)
# ---------------------------------------------------------------------------


def report_v3(client, chain: dict, token: str) -> None:
    if not chain.get("v3Factory"):
        print(f"\n  no Uniswap v3 factory registered for {chain['name']}")
        return
    quotes = [q for q in (chain.get("knownQuotes") or {}) if q != NATIVE]
    found = []
    for quote in quotes:
        for fee in chain["v3FeeTiers"]:
            try:
                pool = client.read(chain["v3Factory"], V3_FACTORY_ABI, "getPool",
                                   [checksum_address(token), checksum_address(quote), fee])
            except Exception:  # noqa: BLE001 — a missing tier is not an error
                continue
            if not pool or pool.lower() == NATIVE:
                continue
            liq, t0, t1 = client.multicall([
                {"address": pool, "abi": V3_POOL_ABI, "functionName": fn}
                for fn in ("liquidity", "token0", "token1")
            ], allow_failure=False)
            t0, t1 = t0["result"], t1["result"]
            m0 = token_meta(client, chain, t0)
            m1 = token_meta(client, chain, t1)
            b0, b1 = client.multicall([
                {"address": t0, "abi": ERC20_ABI, "functionName": "balanceOf",
                 "args": [pool]},
                {"address": t1, "abi": ERC20_ABI, "functionName": "balanceOf",
                 "args": [pool]},
            ], allow_failure=False)
            liquidity = int(liq["result"])
            found.append({
                "pool": pool,
                "pair": f"{m0['symbol']}/{m1['symbol']}",
                "fee": f"{fee / 10000:.2f}%",
                "liquidity": str(liquidity),
                "balance0": fmt_units(b0["result"], m0["decimals"], 4),
                "balance1": fmt_units(b1["result"], m1["decimals"], 4),
                "note": "DEAD (liquidity = 0)" if liquidity == 0 else "",
            })

    print(heading(f"Uniswap v3 pools (factory {chain['v3Factory']})"))
    print(render_table([
        {"key": "pool", "label": "pool"},
        {"key": "pair", "label": "pair"},
        {"key": "fee", "label": "fee"},
        {"key": "balance0", "label": "balance0", "align": "right"},
        {"key": "balance1", "label": "balance1", "align": "right"},
        {"key": "liquidity", "label": "liquidity", "align": "right"},
        {"key": "note", "label": ""},
    ], found))
    if found:
        print("\n  v3 balances are raw token balances of the pool contract "
              "(they include uncollected fees).")


# ---------------------------------------------------------------------------

COMMANDS = {
    "pools": cmd_pools,
    "pool": cmd_pool,
    "position": cmd_position,
    "positions": cmd_positions,
    "launcher": cmd_launcher,
    "ticks": cmd_ticks,
    "price": cmd_price,
}


def main() -> None:
    args = parse_args(sys.argv[1:])
    command = args["_"][0] if args["_"] else None
    if not command or args.get("help") or args.get("h"):
        print(USAGE)
        sys.exit(0 if command else 1)

    chain = resolve_chain(opt_str(args, "chain"))
    client = RpcClient(chain, opt_str(args, "rpc"))

    handler = COMMANDS.get(command)
    if handler is None:
        raise ValueError(f'unknown command "{command}"\n{USAGE}')
    handler(client, chain, args)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 — one place to render every failure
        die(exc)
