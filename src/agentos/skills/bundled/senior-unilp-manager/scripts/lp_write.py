#!/usr/bin/env python3
"""Uniswap V4 LP write operations.

EVERY subcommand is a dry run unless BOTH ``--broadcast`` and ``--confirm <PLAN_HASH>``
are given. The dry run prints a parameter table plus a PLAN_HASH; broadcasting requires
echoing that hash back, so the transaction that gets signed is provably the one the
human approved.

    python3 scripts/lp_write.py approve  --token <addr>
    python3 scripts/lp_write.py create-pool --token0 <addr> --token1 <addr> --fee 3000 \\
                                         --tick-spacing 60 --price <n>
    python3 scripts/lp_write.py mint     --pool <poolId> --tick-lower <t> --tick-upper <t> \\
                                         --amount1 <n>
    python3 scripts/lp_write.py increase --token-id <id> --amount1 <n>
    python3 scripts/lp_write.py decrease --token-id <id> --pct 50
    python3 scripts/lp_write.py collect  --token-id <id>
    python3 scripts/lp_write.py burn     --token-id <id>

Signing:  the key is read from ``UNIV4_LP_PRIVATE_KEY`` in the environment and from
nowhere else — never from a command-line flag, because a key on a command line lands in
shell history and in the agent transcript. Only the derived address is ever printed.
``--from <addr>`` plans as any address with no key present and can never broadcast.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lp_read import pool_key_for_id  # noqa: E402  (read-only; imports no signing path)
from unilp.abi_codec import encode_function_data  # noqa: E402
from unilp.abi_defs import (  # noqa: E402
    ERC20_ABI,
    PERMIT2_ABI,
    POSITION_MANAGER_ABI,
    STATE_VIEW_ABI,
)
from unilp.chains import (  # noqa: E402
    ENV_SIGNER,
    resolve_chain,
    resolve_private_key,
    resolve_signer_address,
)
from unilp.fmt import (  # noqa: E402
    die,
    fmt_units,
    heading,
    opt_float,
    opt_int,
    opt_str,
    parse_amount,
    parse_args,
    render_kv,
    require_arg,
    short,
    short_id,
)
from unilp.hexutil import checksum_address, js_round, to_hex  # noqa: E402
from unilp.keccak import keccak256  # noqa: E402
from unilp.rpc import RpcClient  # noqa: E402
from unilp.secp256k1 import account_from_private_key  # noqa: E402
from unilp.simulate import describe_revert, net_transfers, simulate_call  # noqa: E402
from unilp.tx import (  # noqa: E402
    DEFAULT_GAS_MULTIPLIER,
    prepare_transaction,
    receipt_status,
    send_transaction,
    wait_for_receipt,
)
from unilp.v4_actions import (  # noqa: E402
    build_burn_plan,
    build_collect_plan,
    build_decrease_plan,
    build_increase_plan,
    build_mint_plan,
    describe_actions,
    encode_unlock_data,
    is_native_currency,
    pool_key_tuple,
)
from unilp.v4_math import (  # noqa: E402
    get_amounts_for_liquidity,
    get_liquidity_for_amount0,
    get_liquidity_for_amount1,
    get_liquidity_for_amounts,
    get_sqrt_ratio_at_tick,
    range_status,
    snap_tick,
    tick_at_price,
    token_price_in_quote_at_tick,
)
from unilp.v4_pool import (  # noqa: E402
    NATIVE,
    VANILLA_FEE_TIERS,
    compute_pool_id,
    decode_hook_flags,
    decode_position_info,
    format_fee,
    format_hook_flags,
    is_dynamic_fee,
    normalize_pool_key,
    sort_currencies,
)

USAGE = """
senior-unilp-manager — Uniswap V4 LP writes (DRY RUN unless --broadcast --confirm <HASH>)

  approve  --token <addr> [--amount max] [--expiration-days 30]
  create-pool --token0 <addr|native> --token1 <addr> --fee <n> --tick-spacing <n>
           (--tick <t> | --price <currency1 per currency0>) [--allow-odd-tier]
           hook-less pools only; the starting price can never be changed afterwards
  mint     --pool <poolId> (--tick-lower <t> --tick-upper <t>)
           (--amount0 <n> | --amount1 <n> | --liquidity <raw>)
           [--slippage-bps 100] [--recipient <addr>] [--allow-hooked]
  increase --token-id <id> (--amount0 <n> | --amount1 <n> | --liquidity <raw>)
           [--slippage-bps 100] [--recipient <addr>]   recipient = native SWEEP refund
  decrease --token-id <id> (--pct <0-100> | --liquidity <raw>) [--slippage-bps 100]
           [--recipient <addr>]
  collect  --token-id <id> [--recipient <addr>]
  burn     --token-id <id> [--slippage-bps 100] [--recipient <addr>]
  address  [--json]        print the wallet UNIV4_LP_PRIVATE_KEY derives; no chain, no send

Safety:  --broadcast --confirm <PLAN_HASH>   both required to send
         --from <addr>            simulate only, no key needed
         --max-tick-drift <n>     default = 1 tickSpacing
         --deadline-secs <n>      default 1200
         --gas-multiplier <f>     default 1.25
Signing: the key comes from UNIV4_LP_PRIVATE_KEY in the environment. Never pass a key
         on the command line. Override the variable name with --signer-env <VAR>.
Global:  --chain <key|id>  --rpc <url>
"""

DEFAULT_SLIPPAGE_BPS = 100
DEFAULT_DEADLINE_SECS = 1200
MAX_UINT160 = 2**160 - 1
MAX_UINT256 = 2**256 - 1
# 100% in hundredths of a bip, and the int16 ceiling the PoolManager enforces on
# tickSpacing. Both are validated locally so create-pool fails with a sentence rather
# than an unnamed custom-error selector out of the simulation.
MAX_STATIC_FEE = 1_000_000
MAX_TICK_SPACING = 32_767


# ---------------------------------------------------------------------------
# PLAN_HASH — binds the approved parameters to the broadcast
# ---------------------------------------------------------------------------


class Big(int):
    """An int that serialises into PLAN_HASH as a JSON *string*.

    The Node build hashed ``JSON.stringify`` output, where a BigInt was stringified but a
    Number was not — so ``chainId`` landed as ``8453`` while ``liquidity`` landed as
    ``"1234"``. Python has one integer type, so the distinction has to be reintroduced by
    hand or every hash diverges from the documented ones.
    """

    __slots__ = ()


def plan_hash(fields: dict) -> str:
    """First 4 bytes of keccak over the canonically serialised parameters.

    Excludes the *absolute* deadline and the gas price so a re-run minutes later still
    matches the hash the user approved — but binds the deadline *offset*, which is a flag
    the user gave. It includes everything that changes what the transaction DOES, and
    everything that loosens a guard the user saw when they approved (the Permit2
    expiration, the tick-drift bound). A parameter that reaches the calldata without
    reaching this hash can be swapped between the approved dry run and the broadcast.
    """
    canonical = json.dumps(
        {key: (str(value) if isinstance(value, Big) else value)
         for key, value in sorted(fields.items())},
        separators=(",", ":"),
    )
    return keccak256(to_hex(canonical.encode("utf-8")))[2:10]


# ---------------------------------------------------------------------------
# Mandate authorization — the ONLY way a broadcast happens without a human hash
# ---------------------------------------------------------------------------

_ARGV_ENTRY = False
"""True once :func:`main` has run, i.e. this process is the interactive CLI.

A CLI process may never construct a :class:`MandateAuthorization`. That is not a policy
check that a future refactor could forget — it makes the two modes disjoint by construction.
"""


class MandateAuthorization:
    """A programmatic stand-in for the ``--confirm <PLAN_HASH>`` a human echoes back.

    ``run_plan`` normally refuses to broadcast unless the caller passes back the exact hash
    printed by the dry run they just showed a human. An unattended runner has nobody to echo
    it, so it supplies one of these instead: the human approved a *mandate* once, and this
    object carries the predicate that decides whether the plan in hand falls inside it.

    Reachability from ``lp_write.py``'s own CLI is blocked five independent ways —
    keyword-only parameter, ``main()`` never passes one, an ``isinstance`` gate that fails
    closed, the ``_ARGV_ENTRY`` guard below, and the pre-existing "``--broadcast`` requires
    ``--confirm``" check in ``main``. ``lp_write.py --broadcast`` still needs a human hash.
    """

    __slots__ = ("mandate_id", "fire_id", "_predicate")

    def __init__(self, *, mandate_id: str, fire_id: str, predicate) -> None:
        if _ARGV_ENTRY:
            raise RuntimeError(
                "lp_write.py's CLI can never construct a MandateAuthorization; --broadcast "
                "requires --confirm <PLAN_HASH> echoed back by a human"
            )
        if not callable(predicate):
            raise TypeError("MandateAuthorization predicate must be callable")
        self.mandate_id = str(mandate_id)
        self.fire_id = str(fire_id)
        self._predicate = predicate

    def authorize(self, client, chain: dict, args: dict, signer: dict, ctx: dict,
                  hash_value: str) -> None:
        """Raise unless every bound holds. Returning normally means "send it"."""
        self._predicate(client, chain, args, signer, ctx, hash_value)


# ---------------------------------------------------------------------------
# Signer
# ---------------------------------------------------------------------------


def resolve_signer(args: dict) -> dict:
    """Either a real key (from the environment) or a plan-only address."""
    sender = opt_str(args, "from")
    if sender:
        return {"address": checksum_address(sender), "privateKey": None,
                "simulateOnly": True}
    signer_env = opt_str(args, "signer-env") or ENV_SIGNER
    private_key = resolve_private_key(signer_env)
    account = account_from_private_key(private_key)
    # Only the derived address is ever surfaced; the key itself is never logged.
    return {"address": account["address"], "privateKey": private_key,
            "simulateOnly": False, "signerEnv": signer_env}


# ---------------------------------------------------------------------------
# Shared loading
# ---------------------------------------------------------------------------


def _iso(seconds: int) -> str:
    """``Date#toISOString`` in the format the Node build printed, milliseconds and all.

    ``time.gmtime`` rather than ``datetime``: this skill declares ``python3`` as its only
    binary requirement and must run on whatever the host has, and ``datetime.UTC`` only
    exists from 3.11. macOS still ships 3.9.
    """
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(seconds))


def token_info(client, chain: dict, address: str) -> dict:
    if (address or "").lower() == NATIVE:
        return {"address": NATIVE, "symbol": chain["nativeCurrency"]["symbol"],
                "decimals": chain["nativeCurrency"]["decimals"], "isNative": True}
    checksummed = checksum_address(address)
    symbol, decimals = client.multicall([
        {"address": checksummed, "abi": ERC20_ABI, "functionName": "symbol"},
        {"address": checksummed, "abi": ERC20_ABI, "functionName": "decimals"},
    ])
    return {
        "address": checksummed,
        "symbol": symbol["result"] if symbol["status"] == "success" else short(checksummed),
        "decimals": int(decimals["result"]) if decimals["status"] == "success" else 18,
        "isNative": False,
    }


def load_pool_state(client, chain: dict, pool_id: str, pool_key: dict) -> dict:
    slot0, liquidity = client.multicall([
        {"address": chain["stateView"], "abi": STATE_VIEW_ABI,
         "functionName": "getSlot0", "args": [pool_id]},
        {"address": chain["stateView"], "abi": STATE_VIEW_ABI,
         "functionName": "getLiquidity", "args": [pool_id]},
    ], allow_failure=False)
    sqrt_price, tick, _protocol_fee, lp_fee = slot0["result"]
    return {
        "poolId": pool_id,
        "poolKey": pool_key,
        "sqrtPriceX96": int(sqrt_price),
        "tick": int(tick),
        "lpFee": int(lp_fee),
        "activeLiquidity": int(liquidity["result"]),
    }


def pool_is_initialized(client, chain: dict, pool_id: str) -> bool:
    """Does this poolId exist yet?

    An uninitialized pool is a zeroed mapping entry rather than a revert, so getSlot0
    succeeds and returns sqrtPriceX96 = 0 — the same test ``lp_read.py`` uses to filter
    derived pool candidates. Kept separate from :func:`load_pool_state`, which would
    otherwise have to invent a meaning for a zero price.
    """
    slot0 = client.multicall([
        {"address": chain["stateView"], "abi": STATE_VIEW_ABI,
         "functionName": "getSlot0", "args": [pool_id]},
    ], allow_failure=False)[0]
    return int(slot0["result"][0]) != 0


def _currency_arg(args: dict, name: str) -> str:
    """A PoolKey currency from the CLI: an address, or ``native``/``eth`` for ETH."""
    value = str(require_arg(args, name, "token address, or 'native' for ETH")).strip()
    if value.lower() in ("native", "eth", "0", "0x0"):
        return NATIVE
    return checksum_address(value)


def load_position_for_write(client, chain: dict, token_id: int) -> dict:
    pool_and_info, liquidity, owner = client.multicall([
        {"address": chain["positionManager"], "abi": POSITION_MANAGER_ABI,
         "functionName": "getPoolAndPositionInfo", "args": [int(token_id)]},
        {"address": chain["positionManager"], "abi": POSITION_MANAGER_ABI,
         "functionName": "getPositionLiquidity", "args": [int(token_id)]},
        {"address": chain["positionManager"], "abi": POSITION_MANAGER_ABI,
         "functionName": "ownerOf", "args": [int(token_id)]},
    ])
    if pool_and_info["status"] != "success":
        raise RuntimeError(f"tokenId {token_id} does not exist on {chain['name']}")
    raw_key, info = pool_and_info["result"]
    pool_key = normalize_pool_key(raw_key)
    # getPoolAndPositionInfo is a mapping read, so a BURNED tokenId comes back with status
    # "success" and an all-zero PoolKey rather than reverting. Without this check the caller
    # goes on to compute_pool_id(zeros) and builds a plan against a pool that cannot exist,
    # failing later with an unrelated revert instead of saying the position is gone.
    if (pool_key["currency0"] == NATIVE and pool_key["currency1"] == NATIVE
            and int(pool_key["fee"]) == 0 and int(pool_key["tickSpacing"]) == 0):
        raise RuntimeError(
            f"tokenId {token_id} has an empty PoolKey on {chain['name']} — the position was "
            "burned, or that id was never minted here"
        )
    return {
        "tokenId": int(token_id),
        "poolKey": pool_key,
        "poolId": compute_pool_id(pool_key),
        **decode_position_info(int(info)),
        "liquidity": int(liquidity["result"]) if liquidity["status"] == "success" else 0,
        "owner": owner["result"] if owner["status"] == "success" else None,
    }


# ---------------------------------------------------------------------------
# Allowances
# ---------------------------------------------------------------------------


def check_allowances(client, chain: dict, owner: str, currencies: list[str]) -> list[dict]:
    rows = []
    for currency in currencies:
        if is_native_currency(currency):
            rows.append({"currency": currency, "native": True, "ok": True})
            continue
        token = checksum_address(currency)
        erc20_allowance, permit2_allowance, balance = client.multicall([
            {"address": token, "abi": ERC20_ABI, "functionName": "allowance",
             "args": [owner, checksum_address(chain["permit2"])]},
            {"address": checksum_address(chain["permit2"]), "abi": PERMIT2_ABI,
             "functionName": "allowance",
             "args": [owner, token, checksum_address(chain["positionManager"])]},
            {"address": token, "abi": ERC20_ABI, "functionName": "balanceOf",
             "args": [owner]},
        ], allow_failure=False)
        amount, expiration, _nonce = permit2_allowance["result"]
        rows.append({
            "currency": token,
            "native": False,
            "erc20ToPermit2": int(erc20_allowance["result"]),
            "permit2ToPosm": int(amount),
            "permit2Expiration": int(expiration),
            "balance": int(balance["result"]),
        })
    return rows


def allowance_problem(row: dict, needed: int, now_secs: int) -> str | None:
    if row["native"]:
        return None
    if row["balance"] < needed:
        return f"balance {row['balance']} < required {needed}"
    if row["erc20ToPermit2"] < needed:
        return f"ERC20 approval to Permit2 is {row['erc20ToPermit2']}, need {needed}"
    if row["permit2ToPosm"] < needed:
        return f"Permit2 approval to PositionManager is {row['permit2ToPosm']}, need {needed}"
    if row["permit2Expiration"] != 0 and row["permit2Expiration"] < now_secs:
        return f"Permit2 approval expired at {_iso(row['permit2Expiration'])}"
    return None


# ---------------------------------------------------------------------------
# The shared dry-run / broadcast pipeline
# ---------------------------------------------------------------------------


def run_plan(client, chain: dict, args: dict, signer: dict, ctx: dict, *,
             authorization: MandateAuthorization | None = None):
    """Simulate, print, gate on the confirmed hash, then (only then) broadcast.

    ``authorization`` replaces the human hash echo for an unattended mandate run; see
    :class:`MandateAuthorization`. It is keyword-only and unreachable from this file's CLI.
    """
    hash_value = plan_hash(ctx["hashFields"])
    token_map = ctx.get("tokenMap") or {}

    print(heading(ctx["title"]))
    print(render_kv(ctx["rows"]))

    # --- simulate -----------------------------------------------------------
    sim = simulate_call(client, {"from": signer["address"], "to": chain["positionManager"],
                                 "data": ctx["data"], "value": ctx.get("value", 0)})

    print(heading("simulation"))
    if not sim["ok"]:
        print(f"  method   : {sim['method']}")
        print("  result   : REVERTED")
        print(f"  reason   : {describe_revert(sim['revert']) or '(no revert data returned)'}")
        print("\n  Nothing was sent. Fix the parameters (or the approvals) and re-run.")
        sys.exit(2)

    traced = net_transfers(sim["logs"], signer["address"])
    net, minted = traced["net"], traced["mintedNfts"]
    print(f"  method   : {sim['method']}")
    print("  result   : OK")
    if sim["gasUsed"] is not None:
        print(f"  gas used : {sim['gasUsed']}")
    if net:
        print("  wallet deltas (from the simulated transfer trace):")
        for token, delta in net.items():
            info = token_map.get(token) or {"symbol": short(token), "decimals": 18}
            sign = "-" if delta < 0 else "+"
            print(f"    {sign}{fmt_units(abs(delta), info['decimals'])} {info['symbol']}")
    elif sim["method"].startswith("eth_simulateV1"):
        print("  wallet deltas: none traced")
    for nft in minted:
        print(f"  position NFT received: tokenId {nft['id']}")

    # The trace is the truth. Locally computed amounts can be wrong when a hook takes a
    # delta, when a token is fee-on-transfer, or — as with SmokeV4 on Robinhood Chain —
    # when `transfer` returns true, emits nothing, and moves nothing.
    if ctx.get("expected") and sim["method"].startswith("eth_simulateV1"):
        warnings = []
        for token, spec in ctx["expected"].items():
            want = spec["amount"]
            if want == 0:
                continue
            got = net.get(token, 0)
            # On a withdrawal the trace legitimately exceeds the principal, because
            # TAKE_PAIR also sweeps accrued fees. Only a shortfall is a problem there.
            if spec["mode"] == "atLeast":
                bad = got * 200 < want * 199
            else:
                bad = abs(got - want) * 200 > abs(want)
            if bad:
                info = token_map.get(token) or {"symbol": short(token), "decimals": 18}
                warnings.append(
                    f"    {info['symbol']}: computed {fmt_units(abs(want), info['decimals'])}, "
                    f"trace shows {fmt_units(abs(got), info['decimals'])}"
                )
        if warnings:
            print("\n  WARNING — simulated transfers disagree with the local calculation "
                  "by >0.5%:")
            print("\n".join(warnings))
            print("    Causes: a hook taking a delta, a fee-on-transfer token, or a token "
                  "whose transfer is a no-op.")
            print("    Trust the trace, not the table above.")

    print(f"\n  PLAN_HASH: {hash_value}")

    # --- gate ---------------------------------------------------------------
    if not args.get("broadcast"):
        print("\n  DRY RUN — nothing sent.")
        print(f"  To execute, re-run the exact same command with:  "
              f"--broadcast --confirm {hash_value}")
        return None
    # Confirm is checked before anything else about the signer: a stale hash means the
    # plan the human approved is not the plan in hand, and that is the worse failure.
    if authorization is not None:
        if not isinstance(authorization, MandateAuthorization):
            raise RuntimeError(
                "authorization must be a MandateAuthorization; refusing to broadcast on a "
                f"{type(authorization).__name__}"
            )
        if args.get("confirm"):
            raise RuntimeError(
                "--confirm and a mandate authorization are mutually exclusive: one of them "
                "is stale, and guessing which would defeat both"
            )
        authorization.authorize(client, chain, args, signer, ctx, hash_value)
    elif args.get("confirm") != hash_value:
        raise RuntimeError(
            f'--confirm mismatch: got "{args.get("confirm") or "(none)"}", plan is '
            f'"{hash_value}".\n'
            "  The parameters changed since the plan was approved, or the hash was not "
            "passed through.\n"
            "  Re-run without --broadcast, show the fresh plan to the user, and confirm again."
        )
    if signer["simulateOnly"]:
        raise RuntimeError("--from is simulate-only; drop it and set UNIV4_LP_PRIVATE_KEY "
                           "to broadcast")

    # --- revalidate ---------------------------------------------------------
    if ctx.get("revalidate"):
        ctx["revalidate"]()

    # --- send ---------------------------------------------------------------
    block = client.get_block()
    deadline = int(block["timestamp"], 16) + deadline_offset(args)
    data = ctx["rebuildData"](deadline) if ctx.get("rebuildData") else ctx["data"]

    return _send(client, chain, args, signer, chain["positionManager"], data,
                 ctx.get("value", 0), on_sent=ctx.get("onSent"))


def _send(client, chain: dict, args: dict, signer: dict, to: str, data: str,
          value: int = 0, label: str = "", on_sent=None) -> dict:
    """Build, sign, broadcast and wait. Shared by run_plan and the approve legs.

    ``on_sent`` is passed straight through to :func:`send_transaction` so an unattended
    caller can persist the hash and nonce before the broadcast leaves the process.
    """
    max_fee_cap = opt_str(args, "max-fee-per-gas")
    tx = prepare_transaction(
        client, chain, signer["address"], to, data, value,
        gas_multiplier=opt_float(args, "gas-multiplier", DEFAULT_GAS_MULTIPLIER),
        max_fee_cap=int(max_fee_cap.replace("_", "")) if max_fee_cap else None,
    )
    prefix = f"  {label} " if label else "\n  "
    tx_hash = send_transaction(client, chain, tx, signer["privateKey"],
                               on_hash=lambda h: print(f"{prefix}sent: {h}"),
                               on_sent=on_sent)
    receipt = wait_for_receipt(client, tx_hash)
    status = receipt_status(receipt)
    print(f"  status: {status}  block: {int(receipt['blockNumber'], 16)}  "
          f"gas: {int(receipt['gasUsed'], 16)}")
    if status != "success":
        raise RuntimeError("transaction reverted on chain")
    return receipt


def encode_call(plan: dict, deadline: int) -> str:
    """Encode modifyLiquidities calldata for a plan at a given deadline."""
    return encode_function_data(
        POSITION_MANAGER_ABI, "modifyLiquidities",
        [encode_unlock_data(plan["actions"], plan["params"]), int(deadline)],
    )


def deadline_offset(args: dict) -> int:
    """Seconds from now, not the absolute deadline. PLAN_HASH binds this; see plan_hash."""
    return opt_int(args, "deadline-secs", DEFAULT_DEADLINE_SECS)


def with_slippage_up(amount: int, bps: int) -> int:
    return int(amount) * (10_000 + int(bps)) // 10_000 + 1


def with_slippage_down(amount: int, bps: int) -> int:
    return int(amount) * (10_000 - int(bps)) // 10_000


def hook_gate(pool_key: dict, args: dict) -> None:
    if decode_hook_flags(pool_key["hooks"])["hasHook"] and not args.get("allow-hooked"):
        raise RuntimeError(
            f"pool has hook {pool_key['hooks']} {format_hook_flags(pool_key['hooks'])}.\n"
            "  A hook can revert or take a delta on add/remove. Re-run with --allow-hooked "
            "once you accept that,\n"
            "  and trust the simulated transfer amounts over the locally computed ones."
        )


def _pct(part: int, whole: int) -> str:
    if whole == 0:
        return "0.00"
    return f"{js_round(part / whole * 100 * 100) / 100:.2f}"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_approve(client, chain: dict, args: dict, signer: dict) -> None:
    token = checksum_address(require_arg(args, "token", "ERC20 address"))
    info = token_info(client, chain, token)
    if info["isNative"]:
        raise RuntimeError("native ETH needs no approval")

    raw_amount = opt_str(args, "amount")
    amount = (parse_amount(raw_amount, info["decimals"])
              if raw_amount and raw_amount != "max" else MAX_UINT160)
    expiration_days = opt_int(args, "expiration-days", 30)
    now_secs = int(client.get_block()["timestamp"], 16)
    expiration = now_secs + expiration_days * 86400

    row = check_allowances(client, chain, signer["address"], [token])[0]
    need_erc20 = row["erc20ToPermit2"] < amount
    # Renew a day early: an approval that expires mid-plan reverts in the SETTLE leg,
    # after the pool has already accepted the liquidity change.
    need_permit2 = row["permit2ToPosm"] < amount or row["permit2Expiration"] < now_secs + 86400

    will_send = " + ".join(filter(None, [
        "ERC20.approve(Permit2, max)" if need_erc20 else None,
        "Permit2.approve(token, PositionManager)" if need_permit2 else None,
    ])) or "nothing — already approved"

    print(heading(f"approve {info['symbol']} for Uniswap v4 on {chain['name']}"))
    print(render_kv([
        ["signer", signer["address"]],
        ["token", f"{info['symbol']}  {token}"],
        ["balance", fmt_units(row["balance"], info["decimals"])],
        ["ERC20 → Permit2",
         f"{'sufficient' if row['erc20ToPermit2'] >= amount else 'INSUFFICIENT'} "
         f"({row['erc20ToPermit2']})"],
        ["Permit2 → PosM",
         f"{row['permit2ToPosm']} exp "
         f"{'(unset)' if row['permit2Expiration'] == 0 else _iso(row['permit2Expiration'])}"],
        ["will send", will_send],
        ["new expiration", _iso(expiration)],
    ]))

    if not need_erc20 and not need_permit2:
        print("\n  Already approved. Nothing to do.")
        return

    # expirationDays, not the absolute expiration: the absolute value moves with the block
    # timestamp between the dry run and the broadcast, the flag the user approved does not.
    hash_value = plan_hash({
        "chainId": chain["chainId"], "cmd": "approve", "token": token, "amount": Big(amount),
        "expirationDays": expiration_days, "needErc20": need_erc20,
        "needPermit2": need_permit2, "signer": signer["address"],
    })
    print(f"\n  PLAN_HASH: {hash_value}")
    if not args.get("broadcast"):
        print("\n  DRY RUN — nothing sent.")
        print(f"  To execute:  --broadcast --confirm {hash_value}")
        return
    if signer["simulateOnly"]:
        raise RuntimeError("--from is simulate-only; set UNIV4_LP_PRIVATE_KEY to broadcast")
    if args.get("confirm") != hash_value:
        raise RuntimeError(
            f'--confirm mismatch: got "{args.get("confirm") or "(none)"}", '
            f'plan is "{hash_value}"'
        )

    if need_erc20:
        _send(client, chain, args, signer, token,
              encode_function_data(ERC20_ABI, "approve",
                                   [checksum_address(chain["permit2"]), MAX_UINT256]),
              label="ERC20.approve")
    if need_permit2:
        _send(client, chain, args, signer, checksum_address(chain["permit2"]),
              encode_function_data(PERMIT2_ABI, "approve",
                                   [token, checksum_address(chain["positionManager"]),
                                    amount, expiration]),
              label="Permit2.approve")
    print("  approvals complete")


def size_liquidity(sqrt_p: int, sqrt_lower: int, sqrt_upper: int, args: dict,
                   info0: dict, info1: dict) -> int:
    """Resolve the liquidity to add from whichever sizing flag the user gave."""
    liquidity = opt_str(args, "liquidity")
    if liquidity:
        return int(liquidity.replace("_", ""))

    raw0 = opt_str(args, "amount0")
    raw1 = opt_str(args, "amount1")
    has0 = raw0 is not None
    has1 = raw1 is not None
    if not has0 and not has1:
        raise RuntimeError("need one of --amount0, --amount1 or --liquidity")

    amount0 = parse_amount(raw0, info0["decimals"]) if raw0 is not None else 0
    amount1 = parse_amount(raw1, info1["decimals"]) if raw1 is not None else 0

    if has0 and has1:
        return get_liquidity_for_amounts(sqrt_p, sqrt_lower, sqrt_upper, amount0, amount1)

    # Single-sided sizing. If the range needs the OTHER currency at the current price, the
    # amount given cannot be deployed — say so instead of silently minting zero liquidity.
    if has0:
        if sqrt_p >= sqrt_upper:
            raise RuntimeError(
                f"range is entirely below the current price: it takes {info1['symbol']}, "
                f"not {info0['symbol']}"
            )
        return get_liquidity_for_amount0(max(sqrt_p, sqrt_lower), sqrt_upper, amount0)
    if sqrt_p <= sqrt_lower:
        raise RuntimeError(
            f"range is entirely above the current price: it takes {info0['symbol']}, "
            f"not {info1['symbol']}"
        )
    return get_liquidity_for_amount1(sqrt_lower, min(sqrt_p, sqrt_upper), amount1)


def _token_map(info0: dict, info1: dict) -> dict:
    return {info0["address"].lower(): info0, info1["address"].lower(): info1}


def cmd_create_pool(client, chain: dict, args: dict, signer: dict) -> dict | None:
    """Initialize a hook-less v4 pool. Moves no tokens; fixes the starting price forever.

    ``initializePool`` is a plain PositionManager function, not a ``modifyLiquidities``
    action — there is no INITIALIZE_POOL byte in the Actions enum. That keeps the target
    address the same as every other write here, so nothing about ``run_plan`` changes.
    """
    token_a = _currency_arg(args, "token0")
    token_b = _currency_arg(args, "token1")
    if token_a.lower() == token_b.lower():
        raise RuntimeError("--token0 and --token1 are the same currency")
    currency0, currency1 = sort_currencies(token_a, token_b)

    fee = int(require_arg(args, "fee", "fee in hundredths of a bip, e.g. 3000 for 0.30%"))
    tick_spacing = int(require_arg(args, "tick-spacing"))
    if is_dynamic_fee(fee):
        raise RuntimeError(
            f"fee {fee} sets the dynamic-fee flag (0x800000). A dynamic-fee pool has to be "
            "driven by a hook, and this command only opens hook-less pools."
        )
    if not 0 <= fee <= MAX_STATIC_FEE:
        raise RuntimeError(f"--fee must be between 0 and {MAX_STATIC_FEE} (= 100%), got {fee}")
    if not 1 <= tick_spacing <= MAX_TICK_SPACING:
        raise RuntimeError(f"--tick-spacing must be between 1 and {MAX_TICK_SPACING}, "
                           f"got {tick_spacing}")
    # A pool opened off the conventional tiers is invisible to `lp_read.py pools`, which
    # derives hook-less candidates from VANILLA_FEE_TIERS only. Creating a pool you cannot
    # then find again is a bad enough trap to be opt-in.
    odd_tier = (fee, tick_spacing) not in VANILLA_FEE_TIERS
    if odd_tier and not args.get("allow-odd-tier"):
        tiers = ", ".join(f"{f}/{s}" for f, s in VANILLA_FEE_TIERS)
        raise RuntimeError(
            f"fee {fee} with tickSpacing {tick_spacing} is not one of the conventional tiers "
            f"({tiers}).\n"
            "  v4 allows it, but `lp_read.py pools` only searches those tiers, so the pool "
            "would not show up\n"
            "  in discovery afterwards. Re-run with --allow-odd-tier if that is deliberate."
        )

    pool_key = normalize_pool_key({
        "currency0": currency0, "currency1": currency1, "fee": fee,
        "tickSpacing": tick_spacing, "hooks": NATIVE,
    })
    pool_id = compute_pool_id(pool_key)
    info0 = token_info(client, chain, currency0)
    info1 = token_info(client, chain, currency1)

    # --- starting price ------------------------------------------------------
    # v4 does not require the initial tick to sit on the tickSpacing grid — only position
    # boundaries do — so neither branch snaps. --price still lands on the 1.0001^n grid,
    # which is why the effective price is echoed back before anything is signed.
    # opt_str refuses a valueless `--tick` outright: parse_args turns it into True, and
    # int(True) is 1 — a silently wrong starting price is exactly the failure to avoid.
    raw_tick = opt_str(args, "tick")
    raw_price = opt_str(args, "price")
    if (raw_tick is None) == (raw_price is None):
        raise RuntimeError("give exactly one of --tick <t> or --price <currency1 per currency0>")
    if raw_tick is not None:
        tick = int(raw_tick)
        price_source = "--tick"
    else:
        tick = tick_at_price(float(raw_price), info0["decimals"], info1["decimals"])
        price_source = f"--price {raw_price}"
    sqrt_price_x96 = get_sqrt_ratio_at_tick(tick)

    price_1_per_0 = token_price_in_quote_at_tick(tick, False, info0["decimals"],
                                                 info1["decimals"])
    price_0_per_1 = token_price_in_quote_at_tick(tick, True, info0["decimals"],
                                                 info1["decimals"])

    if pool_is_initialized(client, chain, pool_id):
        state = load_pool_state(client, chain, pool_id, pool_key)
        print(heading(f"pool already exists on {chain['name']}"))
        print(render_kv([
            ["poolId", pool_id],
            ["pair", f"{info0['symbol']}/{info1['symbol']}"],
            ["fee", format_fee(pool_key["fee"], state["lpFee"])],
            ["tickSpacing", str(tick_spacing)],
            ["current tick", str(state["tick"])],
        ]))
        print("\n  Nothing to do — a pool can only be initialized once. To add liquidity:")
        print(f"    python3 scripts/lp_read.py pool --id {pool_id}   (read it first)")
        print(f"    python3 scripts/lp_write.py mint --pool {pool_id} --tick-lower <t> "
              "--tick-upper <t> --amount1 <n>")
        return None

    data = encode_function_data(POSITION_MANAGER_ABI, "initializePool",
                               [pool_key_tuple(pool_key), sqrt_price_x96])

    rows = [
        ["signer", signer["address"] + ("  (simulate-only, --from)"
                                        if signer["simulateOnly"] else "")],
        ["poolId", pool_id],
        ["currency0", f"{info0['symbol']}  {currency0}"],
        ["currency1", f"{info1['symbol']}  {currency1}"],
        ["fee", format_fee(fee) + ("  (NON-STANDARD TIER)" if odd_tier else "")],
        ["tickSpacing", str(tick_spacing)],
        ["hooks", "none (0x0) — fixed, this command never opens a hooked pool"],
        ["start tick", f"{tick}  (from {price_source})"],
        ["sqrtPriceX96", str(sqrt_price_x96)],
        ["start price", f"1 {info0['symbol']} = {price_1_per_0:.10g} {info1['symbol']}"],
        ["", f"1 {info1['symbol']} = {price_0_per_1:.10g} {info0['symbol']}"],
        ["moves", "no tokens — initialize only; liquidity comes later via mint"],
    ]

    # Printed ahead of the table rather than after it because run_plan owns everything from
    # the heading down. It reads as an instruction for how to look at the rows below.
    print("\n  READ THE START PRICE BELOW. A pool is initialized once and its starting tick "
          "can never\n  be changed; if it is off the real market, the first swap or mint "
          "against it is an\n  arbitrage at your expense, and the only remedy is a different "
          "pool.")

    def revalidate() -> None:
        # Somebody else can initialize the same PoolKey between the dry run and the
        # broadcast — the tx would revert, but failing here says why.
        if pool_is_initialized(client, chain, pool_id):
            raise RuntimeError(
                f"pool {pool_id} was initialized by someone else since the plan was made. "
                "Re-run to read its current price."
            )

    return run_plan(client, chain, args, signer, {
        "title": f"create pool {info0['symbol']}/{info1['symbol']} on {chain['name']}",
        "rows": rows,
        "tokenMap": _token_map(info0, info1),
        "hashFields": {
            "chainId": chain["chainId"], "to": chain["positionManager"], "cmd": "create-pool",
            "currency0": currency0, "currency1": currency1, "fee": fee,
            "tickSpacing": tick_spacing, "hooks": pool_key["hooks"], "poolId": pool_id,
            "tick": tick, "sqrtPriceX96": Big(sqrt_price_x96),
            "signer": signer["address"],
        },
        # No unlockData and no deadline in the calldata, so there is nothing to rebuild at
        # send time and deadlineSecs is deliberately absent from the hash above.
        "data": data,
        "value": 0,
        "revalidate": revalidate,
    })


def cmd_mint(client, chain: dict, args: dict, signer: dict, *,
             authorization: MandateAuthorization | None = None) -> dict | None:
    pool_id = require_arg(args, "pool", "poolId")
    pool_key = pool_key_for_id(client, chain, pool_id, args)
    if not pool_key:
        raise RuntimeError(f"no Initialize event for pool {pool_id} — is the pool created?")
    hook_gate(pool_key, args)

    pool = load_pool_state(client, chain, pool_id, pool_key)
    info0 = token_info(client, chain, pool_key["currency0"])
    info1 = token_info(client, chain, pool_key["currency1"])

    tick_lower = snap_tick(int(require_arg(args, "tick-lower")), pool_key["tickSpacing"], "down")
    tick_upper = snap_tick(int(require_arg(args, "tick-upper")), pool_key["tickSpacing"], "up")
    if tick_lower >= tick_upper:
        raise RuntimeError("--tick-lower must be below --tick-upper after snapping to "
                           "tickSpacing")

    sqrt_lower = get_sqrt_ratio_at_tick(tick_lower)
    sqrt_upper = get_sqrt_ratio_at_tick(tick_upper)
    liquidity = size_liquidity(pool["sqrtPriceX96"], sqrt_lower, sqrt_upper, args, info0, info1)
    if liquidity <= 0:
        raise RuntimeError("computed liquidity is zero — increase the amount or widen the range")

    # Size with liquidity rounded DOWN, then recompute what we owe rounding UP, then add
    # slippage. Doing it the other way round produces MaximumAmountExceeded reverts.
    required = get_amounts_for_liquidity(pool["sqrtPriceX96"], sqrt_lower, sqrt_upper,
                                         liquidity, True)
    bps = opt_int(args, "slippage-bps", DEFAULT_SLIPPAGE_BPS)
    amount0_max = 0 if required["amount0"] == 0 else with_slippage_up(required["amount0"], bps)
    amount1_max = 0 if required["amount1"] == 0 else with_slippage_up(required["amount1"], bps)

    recipient = opt_str(args, "recipient")
    recipient = checksum_address(recipient) if recipient else signer["address"]
    plan = build_mint_plan(pool_key, tick_lower, tick_upper, liquidity,
                           amount0_max, amount1_max, recipient)

    max_drift = opt_int(args, "max-tick-drift", pool_key["tickSpacing"])
    now_secs = int(client.get_block()["timestamp"], 16)
    allowances = check_allowances(client, chain, signer["address"],
                                  [pool_key["currency0"], pool_key["currency1"]])
    problems = [p for p in (allowance_problem(allowances[0], amount0_max, now_secs),
                            allowance_problem(allowances[1], amount1_max, now_secs)) if p]

    rows = [
        ["signer", signer["address"] + ("  (simulate-only, --from)"
                                        if signer["simulateOnly"] else "")],
        ["pool", f"{short_id(pool_id)}  {info0['symbol']}/{info1['symbol']}"],
        ["fee", format_fee(pool_key["fee"], pool["lpFee"])],
        ["tickSpacing", str(pool_key["tickSpacing"])],
        ["hooks", "none" if pool_key["hooks"] == NATIVE
                  else f"{pool_key['hooks']} {format_hook_flags(pool_key['hooks'])}"],
        ["current tick", str(pool["tick"])],
        ["tick range", f"{tick_lower} → {tick_upper}  "
                       f"({range_status(pool['tick'], tick_lower, tick_upper)})"],
        ["liquidity", str(liquidity)],
        ["requires", f"{fmt_units(required['amount0'], info0['decimals'])} {info0['symbol']} + "
                     f"{fmt_units(required['amount1'], info1['decimals'])} {info1['symbol']}"],
        ["amount0Max", f"{fmt_units(amount0_max, info0['decimals'])} {info0['symbol']}  "
                       f"(+{bps} bps)"],
        ["amount1Max", f"{fmt_units(amount1_max, info1['decimals'])} {info1['symbol']}  "
                       f"(+{bps} bps)"],
        ["recipient", recipient],
        ["max tick drift", f"{max_drift}  (re-checked against the pool just before sending)"],
        ["actions", describe_actions(plan["actions"])],
        ["msg.value", "0" if plan["value"] == 0
                      else f"{fmt_units(plan['value'], 18)} {chain['nativeCurrency']['symbol']}"],
        ["approvals", "OK" if not problems else f"MISSING — {'; '.join(problems)}"],
    ]

    if problems and not signer["simulateOnly"]:
        print(heading("mint — blocked on approvals"))
        print(render_kv(rows))
        print("\n  Run this first:\n    python3 scripts/lp_write.py approve --token <address> "
              "--broadcast --confirm <hash>")
        sys.exit(2)
    if problems:
        # --from is a planning mode: keep going so the simulation still reports what the
        # pool (and its hook) does. The hook callbacks fire inside MINT_POSITION, which
        # runs before SETTLE_PAIR tries to move any tokens — so a hook rejection surfaces
        # here even though the settle leg will fail for lack of approvals.
        print("\n  NOTE: approvals are missing, but --from is simulate-only. The simulation "
              "will still show")
        print("  whether the pool/hook accepts the add; expect the SETTLE_PAIR leg to fail "
              "afterwards.")

    deadline = now_secs + deadline_offset(args)
    planned_tick = pool["tick"]

    def revalidate() -> None:
        fresh = load_pool_state(client, chain, pool_id, pool_key)
        drift = abs(fresh["tick"] - planned_tick)
        if drift > max_drift:
            raise RuntimeError(
                f"pool moved: tick {planned_tick} → {fresh['tick']} "
                f"(drift {drift} > {max_drift}). Re-plan."
            )

    return run_plan(client, chain, args, signer, {
        "title": "mint position",
        "rows": rows,
        "tokenMap": _token_map(info0, info1),
        "hashFields": {
            "chainId": chain["chainId"], "to": chain["positionManager"], "cmd": "mint",
            "poolId": pool_id, "tickLower": tick_lower, "tickUpper": tick_upper,
            "liquidity": Big(liquidity), "amount0Max": Big(amount0_max),
            "amount1Max": Big(amount1_max), "recipient": recipient,
            "maxTickDrift": max_drift, "deadlineSecs": deadline_offset(args),
            "signer": signer["address"],
        },
        "data": encode_call(plan, deadline),
        "value": plan["value"],
        "expected": {
            info0["address"].lower(): {"amount": -required["amount0"], "mode": "exact"},
            info1["address"].lower(): {"amount": -required["amount1"], "mode": "exact"},
        },
        "rebuildData": lambda d: encode_call(plan, d),
        "revalidate": revalidate,
    }, authorization=authorization)


def cmd_increase(client, chain: dict, args: dict, signer: dict) -> None:
    token_id = int(require_arg(args, "token-id"))
    pos = load_position_for_write(client, chain, token_id)
    hook_gate(pos["poolKey"], args)
    pool = load_pool_state(client, chain, pos["poolId"], pos["poolKey"])
    info0 = token_info(client, chain, pos["poolKey"]["currency0"])
    info1 = token_info(client, chain, pos["poolKey"]["currency1"])

    sqrt_lower = get_sqrt_ratio_at_tick(pos["tickLower"])
    sqrt_upper = get_sqrt_ratio_at_tick(pos["tickUpper"])
    liquidity = size_liquidity(pool["sqrtPriceX96"], sqrt_lower, sqrt_upper, args, info0, info1)
    if liquidity <= 0:
        raise RuntimeError("computed liquidity is zero")

    required = get_amounts_for_liquidity(pool["sqrtPriceX96"], sqrt_lower, sqrt_upper,
                                         liquidity, True)
    bps = opt_int(args, "slippage-bps", DEFAULT_SLIPPAGE_BPS)
    amount0_max = 0 if required["amount0"] == 0 else with_slippage_up(required["amount0"], bps)
    amount1_max = 0 if required["amount1"] == 0 else with_slippage_up(required["amount1"], bps)

    recipient = opt_str(args, "recipient")
    recipient = checksum_address(recipient) if recipient else signer["address"]
    plan = build_increase_plan(pos["poolKey"], pos["tokenId"], liquidity,
                               amount0_max, amount1_max, recipient)
    deadline = int(client.get_block()["timestamp"], 16) + deadline_offset(args)
    # On a native-currency0 pool the plan ends in SWEEP, which refunds the unspent part of
    # msg.value (amount0Max minus what the pool actually took) to this address. Elsewhere
    # it is inert — but it is still hashed, so the field set stays the same per command.
    sweeps_native = is_native_currency(pos["poolKey"]["currency0"])

    run_plan(client, chain, args, signer, {
        "title": f"increase liquidity on position #{token_id}",
        "tokenMap": _token_map(info0, info1),
        "rows": [
            ["signer", signer["address"]],
            ["owner", pos["owner"] or "(burned)"],
            ["pool", f"{short_id(pos['poolId'])}  {info0['symbol']}/{info1['symbol']}"],
            ["ticks", f"{pos['tickLower']} → {pos['tickUpper']}  "
                      f"({range_status(pool['tick'], pos['tickLower'], pos['tickUpper'])})"],
            ["liquidity now", str(pos["liquidity"])],
            ["liquidity added", str(liquidity)],
            ["requires",
             f"{fmt_units(required['amount0'], info0['decimals'])} {info0['symbol']} + "
             f"{fmt_units(required['amount1'], info1['decimals'])} {info1['symbol']}"],
            ["amount0Max", fmt_units(amount0_max, info0["decimals"])],
            ["amount1Max", fmt_units(amount1_max, info1["decimals"])],
            ["recipient", f"{recipient}  " + (
                f"(SWEEP — unspent {chain['nativeCurrency']['symbol']} is refunded here)"
                if sweeps_native else "(unused — this pool has no native side)")],
            ["actions", describe_actions(plan["actions"])],
        ],
        "hashFields": {
            "chainId": chain["chainId"], "to": chain["positionManager"], "cmd": "increase",
            "tokenId": Big(pos["tokenId"]), "liquidity": Big(liquidity),
            "amount0Max": Big(amount0_max), "amount1Max": Big(amount1_max),
            "recipient": recipient, "deadlineSecs": deadline_offset(args),
            "signer": signer["address"],
        },
        "data": encode_call(plan, deadline),
        "value": plan["value"],
        "expected": {
            info0["address"].lower(): {"amount": -required["amount0"], "mode": "exact"},
            info1["address"].lower(): {"amount": -required["amount1"], "mode": "exact"},
        },
        "rebuildData": lambda d: encode_call(plan, d),
    })


def cmd_decrease(client, chain: dict, args: dict, signer: dict) -> None:
    token_id = int(require_arg(args, "token-id"))
    pos = load_position_for_write(client, chain, token_id)
    hook_gate(pos["poolKey"], args)
    pool = load_pool_state(client, chain, pos["poolId"], pos["poolKey"])
    info0 = token_info(client, chain, pos["poolKey"]["currency0"])
    info1 = token_info(client, chain, pos["poolKey"]["currency1"])

    raw_liquidity = opt_str(args, "liquidity")
    if raw_liquidity:
        liquidity = int(raw_liquidity.replace("_", ""))
    else:
        pct = float(require_arg(args, "pct", "0-100"))
        if not 0 < pct <= 100:
            raise RuntimeError("--pct must be in (0, 100]")
        # js_round, not round(): Python rounds halves to even, so --pct 12.5 would pick a
        # different liquidity here than the Node build did.
        liquidity = pos["liquidity"] * js_round(pct * 100) // 10_000
    if liquidity <= 0:
        raise RuntimeError("nothing to remove")
    if liquidity > pos["liquidity"]:
        raise RuntimeError(f"position only has {pos['liquidity']} liquidity")

    expected = get_amounts_for_liquidity(
        pool["sqrtPriceX96"], get_sqrt_ratio_at_tick(pos["tickLower"]),
        get_sqrt_ratio_at_tick(pos["tickUpper"]), liquidity,
    )
    bps = opt_int(args, "slippage-bps", DEFAULT_SLIPPAGE_BPS)
    amount0_min = with_slippage_down(expected["amount0"], bps)
    amount1_min = with_slippage_down(expected["amount1"], bps)

    recipient = opt_str(args, "recipient")
    recipient = checksum_address(recipient) if recipient else signer["address"]
    plan = build_decrease_plan(pos["poolKey"], pos["tokenId"], liquidity,
                               amount0_min, amount1_min, recipient)
    deadline = int(client.get_block()["timestamp"], 16) + deadline_offset(args)

    run_plan(client, chain, args, signer, {
        "title": f"decrease liquidity on position #{token_id}",
        "tokenMap": _token_map(info0, info1),
        "rows": [
            ["signer", signer["address"]],
            ["owner", pos["owner"] or "(burned)"],
            ["pool", f"{short_id(pos['poolId'])}  {info0['symbol']}/{info1['symbol']}"],
            ["ticks", f"{pos['tickLower']} → {pos['tickUpper']}  "
                      f"({range_status(pool['tick'], pos['tickLower'], pos['tickUpper'])})"],
            ["liquidity now", str(pos["liquidity"])],
            ["liquidity removed",
             f"{liquidity}  ({_pct(liquidity, pos['liquidity'])}%)"],
            ["expected out",
             f"{fmt_units(expected['amount0'], info0['decimals'])} {info0['symbol']} + "
             f"{fmt_units(expected['amount1'], info1['decimals'])} {info1['symbol']}"],
            ["amount0Min", f"{fmt_units(amount0_min, info0['decimals'])}  (-{bps} bps)"],
            ["amount1Min", f"{fmt_units(amount1_min, info1['decimals'])}  (-{bps} bps)"],
            ["recipient", recipient],
            ["actions", f"{describe_actions(plan['actions'])}  "
                        "(TAKE_PAIR also collects accrued fees)"],
        ],
        "hashFields": {
            "chainId": chain["chainId"], "to": chain["positionManager"], "cmd": "decrease",
            "tokenId": Big(pos["tokenId"]), "liquidity": Big(liquidity),
            "amount0Min": Big(amount0_min), "amount1Min": Big(amount1_min),
            "recipient": recipient, "deadlineSecs": deadline_offset(args),
            "signer": signer["address"],
        },
        "data": encode_call(plan, deadline),
        "value": 0,
        "expected": {
            info0["address"].lower(): {"amount": expected["amount0"], "mode": "atLeast"},
            info1["address"].lower(): {"amount": expected["amount1"], "mode": "atLeast"},
        },
        "rebuildData": lambda d: encode_call(plan, d),
    })


def cmd_collect(client, chain: dict, args: dict, signer: dict) -> None:
    token_id = int(require_arg(args, "token-id"))
    pos = load_position_for_write(client, chain, token_id)
    hook_gate(pos["poolKey"], args)
    info0 = token_info(client, chain, pos["poolKey"]["currency0"])
    info1 = token_info(client, chain, pos["poolKey"]["currency1"])

    recipient = opt_str(args, "recipient")
    recipient = checksum_address(recipient) if recipient else signer["address"]
    plan = build_collect_plan(pos["poolKey"], pos["tokenId"], recipient)
    deadline = int(client.get_block()["timestamp"], 16) + deadline_offset(args)

    run_plan(client, chain, args, signer, {
        "title": f"collect fees from position #{token_id}",
        "tokenMap": _token_map(info0, info1),
        "rows": [
            ["signer", signer["address"]],
            ["owner", pos["owner"] or "(burned)"],
            ["pool", f"{short_id(pos['poolId'])}  {info0['symbol']}/{info1['symbol']}"],
            ["ticks", f"{pos['tickLower']} → {pos['tickUpper']}"],
            ["liquidity", f"{pos['liquidity']}  (unchanged — this is a 0-liquidity decrease)"],
            ["recipient", recipient],
            ["actions", describe_actions(plan["actions"])],
        ],
        "hashFields": {
            "chainId": chain["chainId"], "to": chain["positionManager"], "cmd": "collect",
            "tokenId": Big(pos["tokenId"]), "recipient": recipient,
            "deadlineSecs": deadline_offset(args), "signer": signer["address"],
        },
        "data": encode_call(plan, deadline),
        "value": 0,
        "rebuildData": lambda d: encode_call(plan, d),
    })


def cmd_burn(client, chain: dict, args: dict, signer: dict, *,
             authorization: MandateAuthorization | None = None) -> dict | None:
    token_id = int(require_arg(args, "token-id"))
    pos = load_position_for_write(client, chain, token_id)
    hook_gate(pos["poolKey"], args)
    pool = load_pool_state(client, chain, pos["poolId"], pos["poolKey"])
    info0 = token_info(client, chain, pos["poolKey"]["currency0"])
    info1 = token_info(client, chain, pos["poolKey"]["currency1"])

    expected = get_amounts_for_liquidity(
        pool["sqrtPriceX96"], get_sqrt_ratio_at_tick(pos["tickLower"]),
        get_sqrt_ratio_at_tick(pos["tickUpper"]), pos["liquidity"],
    )
    bps = opt_int(args, "slippage-bps", DEFAULT_SLIPPAGE_BPS)
    amount0_min = with_slippage_down(expected["amount0"], bps)
    amount1_min = with_slippage_down(expected["amount1"], bps)

    recipient = opt_str(args, "recipient")
    recipient = checksum_address(recipient) if recipient else signer["address"]
    plan = build_burn_plan(pos["poolKey"], pos["tokenId"], pos["liquidity"],
                           amount0_min, amount1_min, recipient)
    deadline = int(client.get_block()["timestamp"], 16) + deadline_offset(args)

    return run_plan(client, chain, args, signer, {
        "title": f"burn position #{token_id} (full exit)",
        "tokenMap": _token_map(info0, info1),
        "rows": [
            ["signer", signer["address"]],
            ["owner", pos["owner"] or "(burned)"],
            ["pool", f"{short_id(pos['poolId'])}  {info0['symbol']}/{info1['symbol']}"],
            ["ticks", f"{pos['tickLower']} → {pos['tickUpper']}"],
            ["liquidity", str(pos["liquidity"])],
            ["expected out",
             f"{fmt_units(expected['amount0'], info0['decimals'])} {info0['symbol']} + "
             f"{fmt_units(expected['amount1'], info1['decimals'])} {info1['symbol']} + fees"],
            ["recipient", recipient],
            ["actions", describe_actions(plan["actions"])],
            ["WARNING", "the NFT is destroyed — this cannot be undone"],
        ],
        "hashFields": {
            "chainId": chain["chainId"], "to": chain["positionManager"], "cmd": "burn",
            "tokenId": Big(pos["tokenId"]), "liquidity": Big(pos["liquidity"]),
            "amount0Min": Big(amount0_min), "amount1Min": Big(amount1_min),
            "recipient": recipient, "deadlineSecs": deadline_offset(args),
            "signer": signer["address"],
        },
        "data": encode_call(plan, deadline),
        "value": 0,
        "rebuildData": lambda d: encode_call(plan, d),
    }, authorization=authorization)


def cmd_address(args: dict) -> None:
    """Print the wallet ``UNIV4_LP_PRIVATE_KEY`` derives. No chain, no plan, no send.

    Kept here rather than in ``lp_read.py`` because this is the script that already holds
    the key, so nothing about the read/write boundary moves to add it. ``lp_read.py
    positions`` resolves the same address on its own; this command is for the times you
    want the address itself — to check which wallet is configured, or to hand to another
    tool. The key is never printed.
    """
    signer_env = opt_str(args, "signer-env") or ENV_SIGNER
    address = resolve_signer_address(signer_env)
    if args.get("json"):
        print(json.dumps({"address": address, "signerEnv": signer_env}, indent=2))
    else:
        print(address)


COMMANDS = {
    "approve": cmd_approve,
    "create-pool": cmd_create_pool,
    "mint": cmd_mint,
    "increase": cmd_increase,
    "decrease": cmd_decrease,
    "collect": cmd_collect,
    "burn": cmd_burn,
}


def main() -> None:
    # From here on this process is the interactive CLI, and MandateAuthorization refuses to
    # be constructed. The unattended runner imports this module and never calls main().
    global _ARGV_ENTRY
    _ARGV_ENTRY = True

    args = parse_args(sys.argv[1:])
    command = args["_"][0] if args["_"] else None
    if not command or args.get("help") or args.get("h"):
        print(USAGE)
        sys.exit(0 if command else 1)
    # Answered before a chain or an RpcClient is built: this command reads one env var and
    # does arithmetic, so it must not fail because an RPC endpoint is unset or unreachable.
    if command == "address":
        cmd_address(args)
        return

    if args.get("broadcast") and not args.get("confirm"):
        raise RuntimeError("--broadcast requires --confirm <PLAN_HASH>. Run the dry run "
                           "first and show the plan to the user.")

    handler = COMMANDS.get(command)
    if not handler:
        raise RuntimeError(f'unknown command "{command}"\n{USAGE}')

    chain = resolve_chain(opt_str(args, "chain"))
    client = RpcClient(chain, opt_str(args, "rpc"))
    handler(client, chain, args, resolve_signer(args))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 — one exit path, no traceback for the user
        die(exc)
