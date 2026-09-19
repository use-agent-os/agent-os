#!/usr/bin/env python3
"""pools.fun write commands — launch a token, collect creator fees.

Every state-changing command follows the same two-step protocol:

    1. Run it. Nothing is sent. A plan is printed, ending in a PLAN_HASH.
    2. Re-run it with ``--broadcast --confirm <PLAN_HASH>``.

The hash covers everything that determines what lands on-chain, so a plan that
changed between the two steps — a moved price feed, a different salt, an edited
name — produces a different hash and the broadcast refuses. It is not a
formality: a token launch is irreversible and mints a fixed supply into a
permanently locked pool, so there is no way to undo a typo in a symbol.

A launch needs three things: a name, a symbol, and (optionally) an image.
Everything else has a default, and every default is printed in the plan.
"""

from __future__ import annotations

import json
import shlex
import sys
import time
from pathlib import Path
from typing import Any

from poolsfun.abi_codec import encode_function_data
from poolsfun.chains import (
    CHAIN,
    ENV_SIGNER,
    EXPLORER,
    PARTY_FACTORY,
    PARTY_LOCKER,
    asset_label,
    resolve_paired_asset,
    resolve_private_key,
)
from poolsfun.factory import ERC20_ABI, PARTY_LOCKER_ABI, decode_token_launched, explain_revert
from poolsfun.fmt import (
    die,
    fmt_units,
    fmt_usd,
    heading,
    json_safe,
    opt_float,
    opt_int,
    opt_str,
    parse_args,
    render_kv,
    require_arg,
)
from poolsfun.hexutil import checksum_address, parse_units
from poolsfun.metadata import resolve_metadata_uri
from poolsfun.plan import action_hash, plan_launch, verify_plan_unchanged
from poolsfun.rpc import RpcClient, RpcError
from poolsfun.tx import prepare_transaction, receipt_status, send_transaction, wait_for_receipt

# Bundled scripts run under AgentOS's own interpreter; the path insert only
# matters in a source checkout where the package is not installed (#2804).
_SRC_ROOT = str(Path(__file__).resolve().parents[5])
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)
from agentos.skills.stdio import configure_utf8_stdio  # noqa: E402

USAGE = """
pools_write.py — launch a pools.fun token, manage creator fees

  launch --name <name> --symbol <sym> [--image <path>]
      Launch a token. Dry-run by default; add --broadcast --confirm <hash> to send.

      Identity      --name --symbol --image <path>  (image needs PINATA_JWT)
                    --description --website --twitter
                    --metadata-uri <uri>   use a URI you already host
                    --pin-metadata         pin the JSON even with no image
      Market        --paired weth|usdg     default weth
                    --dev-buy <eth>        buy at launch with native ETH (WETH pairs)
                    --dev-buy-asset <amt>  buy with the paired ERC20 (run `approve` first)
                    --slippage-bps <n>     default 100 (1%)
      Advanced      --fee-recipient <addr> default: you
                    --salt <bytes32>       default: mined for you
                    --deadline-secs <n>    default 1200
                    --allow-fallback-tick  launch even if the price feed is degraded
                    --max-salt-attempts <n>  default 5000

  approve [--paired weth|usdg] [--amount <n>]
      Approve the factory to pull the paired asset for an ERC20 dev buy.

  collect <token>            Sweep LP fees into the locker.
  claim <token>              Pay out your share of what the locker holds.
  collect-and-claim <token>  Both, in one transaction. Usually what you want.
  set-fee-recipient <token> --recipient <addr>
      Redirect future creator-fee payouts.

Common flags
  --broadcast --confirm <hash>   actually send
  --from <addr>                  plan for another wallet; can never broadcast
  --json                         machine-readable output
  --gas-multiplier <f>           default 1.3
  --max-fee-gwei <n>             refuse to send above this gas price

The launch price is fixed by the protocol (~$10k FDV) and cannot be set.
Requires POOLSFUN_PRIVATE_KEY. PINATA_JWT is optional (only for --image).
""".rstrip()

DEFAULT_DEADLINE_SECS = 1200
DEFAULT_SLIPPAGE_BPS = 100
DEFAULT_GAS_MULTIPLIER = 1.3
# The reference launch burned 6.1M gas: pool creation, an NFT mint and a swap in
# one transaction. Estimates can come in tight against that, and a launch that
# runs out of gas still costs the fee, so the multiplier default is generous.
LAUNCH_GAS_FLOOR = 7_000_000


# ── signer ──────────────────────────────────────────────────────────────────
def resolve_signer(args: dict) -> dict:
    """Who signs, or planning-only when ``--from`` names someone else.

    ``--from`` yields ``simulateOnly``, which every broadcast path checks. It is
    what lets a launch be planned on a machine that holds no key at all.
    """
    if args.get("from") and args["from"] is not True:
        return {"address": checksum_address(args["from"]), "privateKey": None,
                "simulateOnly": True, "signerEnv": None}
    signer_env = args.get("signer-env")
    env_name = signer_env if isinstance(signer_env, str) else ENV_SIGNER
    private_key = resolve_private_key(env_name)
    from poolsfun.account import account_from_private_key
    account = account_from_private_key(private_key)
    return {"address": account["address"], "privateKey": private_key,
            "simulateOnly": False, "signerEnv": env_name}


def _gas_opts(args: dict) -> dict:
    cap_gwei = opt_float(args, "max-fee-gwei", 0.0, minimum=0.0)
    return {
        "gas_multiplier": opt_float(args, "gas-multiplier", DEFAULT_GAS_MULTIPLIER,
                                    minimum=1.0),
        "max_fee_cap": int(cap_gwei * 10**9) if cap_gwei > 0 else None,
    }


def _confirmed(args: dict, plan_hash_value: str) -> bool:
    """Whether this invocation is authorised to broadcast this exact plan."""
    if not args.get("broadcast"):
        return False
    confirm = args.get("confirm")
    if not confirm or confirm is True:
        raise RuntimeError(
            f"--broadcast needs --confirm {plan_hash_value}\n"
            "  Re-run with both flags to send. The hash proves you are confirming the "
            "plan you just read."
        )
    if str(confirm).lower().strip() != plan_hash_value.lower():
        raise RuntimeError(
            f"--confirm {confirm} does not match this plan ({plan_hash_value}).\n"
            "  The plan changed since you last read it. Read the new one above, then "
            "confirm that hash."
        )
    return True


def _send(client: RpcClient, signer: dict, to: str, data: str, args: dict,
          value: int = 0, gas_floor: int | None = None) -> dict:
    """Prepare, sign, broadcast, wait. Shared by every write command."""
    if signer["simulateOnly"] or not signer["privateKey"]:
        raise RuntimeError("--from is planning mode; it cannot broadcast. Remove it.")
    opts = _gas_opts(args)
    tx = prepare_transaction(client, CHAIN, signer["address"], to, data, value=value,
                             gas_multiplier=opts["gas_multiplier"],
                             max_fee_cap=opts["max_fee_cap"])
    if gas_floor and tx["gas"] < gas_floor:
        tx["gas"] = gas_floor
    print(f"\n  sending… (gas {tx['gas']:,}, "
          f"maxFee {tx['maxFeePerGas'] / 10**9:.3f} gwei, nonce {tx['nonce']})")
    tx_hash = send_transaction(client, CHAIN, tx, signer["privateKey"])
    print(f"  tx {tx_hash}")
    print(f"  {EXPLORER}/tx/{tx_hash}")
    receipt = wait_for_receipt(client, tx_hash)
    status = receipt_status(receipt)
    print(f"  {status}  (gas used {int(receipt.get('gasUsed', '0x0'), 16):,})")
    if status != "success":
        raise RuntimeError(f"transaction reverted: {EXPLORER}/tx/{tx_hash}")
    return receipt


# ── launch ──────────────────────────────────────────────────────────────────
def cmd_launch(client: RpcClient, args: dict) -> None:
    name = require_arg(args, "name", "token name")
    symbol = require_arg(args, "symbol", "token symbol")
    signer = resolve_signer(args)
    creator = signer["address"]
    paired = resolve_paired_asset(args.get("paired"))

    fee_recipient_raw = opt_str(args, "fee-recipient")
    fee_recipient = checksum_address(fee_recipient_raw) if fee_recipient_raw else creator

    dev_buy_wei = _amount(args.get("dev-buy"), 18, "dev-buy")
    dev_buy_asset = _amount(args.get("dev-buy-asset"), 18, "dev-buy-asset")
    # Slippage is bounded, not merely parsed. A negative value sets devBuyMinOut
    # *above* the exact simulated fill, producing a plan that hashes and renders
    # normally and then reverts DevBuyTooLittle() on-chain, burning the full ~6.1M
    # launch gas. The floor is the whole point of the flag.
    slippage_bps = opt_int(args, "slippage-bps", DEFAULT_SLIPPAGE_BPS,
                           minimum=0, maximum=9_999)
    deadline_secs = opt_int(args, "deadline-secs", DEFAULT_DEADLINE_SECS, minimum=60)
    max_salt_attempts = opt_int(args, "max-salt-attempts", 5000, minimum=1)

    if dev_buy_asset:
        _require_allowance(client, paired, creator, dev_buy_asset)

    # Resolve metadata before planning: the URI feeds the CREATE2 address, so a
    # salt mined against a placeholder would be wrong for the real launch.
    uri, doc, how = resolve_metadata_uri(
        name=name, symbol=symbol,
        metadata_uri=opt_str(args, "metadata-uri"),
        image=opt_str(args, "image"),
        description=opt_str(args, "description"),
        website=opt_str(args, "website"),
        twitter=opt_str(args, "twitter"),
        pin=bool(args.get("pin-metadata")))

    deadline = int(time.time()) + deadline_secs
    plan = plan_launch(
        client, factory=PARTY_FACTORY, name=name, symbol=symbol, metadata_uri=uri,
        paired_asset=paired, creator=creator, fee_recipient=fee_recipient,
        deadline=deadline, dev_buy_wei=dev_buy_wei, dev_buy_asset=dev_buy_asset,
        slippage_bps=slippage_bps, allow_fallback_tick=bool(args.get("allow-fallback-tick")),
        salt=opt_str(args, "salt"), max_salt_attempts=max_salt_attempts)
    plan["metadataHow"] = how
    plan["metadataDoc"] = doc

    if args.get("json"):
        print(json.dumps(json_safe(plan), indent=2))
    else:
        _render_launch_plan(plan, args, signer)

    if not _confirmed(args, plan["planHash"]):
        if not args.get("json"):
            print("\n  Nothing was sent. To execute:")
            print(f"    {_replay_command(args)} --broadcast --confirm {plan['planHash']}")
        return

    # Re-plan against current chain state and require it to be identical. The
    # price feed moves on its own schedule; between reading a plan and confirming
    # it the tick can change, and launching at a tick the user never saw is
    # exactly what the hash exists to prevent.
    fresh = plan_launch(
        client, factory=PARTY_FACTORY, name=name, symbol=symbol, metadata_uri=uri,
        paired_asset=paired, creator=creator, fee_recipient=fee_recipient,
        deadline=int(time.time()) + deadline_secs, dev_buy_wei=dev_buy_wei,
        dev_buy_asset=dev_buy_asset, slippage_bps=slippage_bps,
        allow_fallback_tick=bool(args.get("allow-fallback-tick")),
        salt=plan["salt"], max_salt_attempts=1)
    verify_plan_unchanged(fresh, plan["planHash"])

    from poolsfun.factory import encode_launch
    data = encode_launch(name, symbol, uri, fresh["salt"], paired,
                         fresh["expectedStartTick"], fresh["deadline"], creator,
                         fee_recipient, dev_buy_asset, fresh["devBuyMinOut"])
    receipt = _send(client, signer, PARTY_FACTORY, data, args,
                    value=dev_buy_wei, gas_floor=LAUNCH_GAS_FLOOR)

    launched = decode_token_launched(receipt.get("logs") or [], PARTY_FACTORY)
    print(heading("launched"))
    pairs = [("token", launched["token"] if launched else fresh["token"]),
             ("pool", launched["pool"] if launched else fresh["pool"]),
             ("explorer", f"{EXPLORER}/token/{launched['token'] if launched else fresh['token']}")]
    if launched and launched["devBuyAmountOut"]:
        pairs.append(("you received",
                      f"{fmt_units(launched['devBuyAmountOut'], 18)} {symbol}"))
    print(render_kv(pairs))


def _render_launch_plan(plan: dict, args: dict, signer: dict) -> None:
    paired = plan["pairedAsset"]
    dev_wei, dev_asset = plan["devBuyValueWei"], plan["devBuyAmountIn"]
    default = "   [default]"

    def mark(is_default: bool) -> str:
        return default if is_default else ""

    print(heading("launch plan — nothing sent yet"))
    pairs: list[tuple[str, str]] = [
        ("name / symbol", f"{plan['name']} / {plan['symbol']}"),
        ("metadata", f"{_trim(plan['metadataUri'])}  ({plan['metadataHow']})"),
        ("paired asset", f"{asset_label(paired)}  {paired}"
                         + mark(not args.get("paired"))),
        ("salt", f"{plan['salt'][:12]}…{plan['salt'][-4:]}  "
                 f"(mined, {plan['saltAttempts']} tries)"),
        ("token address", f"{plan['token']}  (predicted)"),
        ("pool address", plan["pool"]),
        ("start tick", f"{plan['expectedStartTick']}  "
                       f"({'live feed' if plan['tickLive'] else 'FALLBACK'})"),
        ("token price", fmt_usd(plan["tokenPriceUsd"])),
        ("launch FDV", f"{fmt_usd(plan['fdvUsd'])}   protocol-fixed"),
        ("supply", f"{plan['supply'] // 10**18:,}   protocol-fixed"),
        ("fee tier", "1%, full range   protocol-fixed"),
        ("LP", f"minted to the locker, permanently; creator = {_short(plan['creator'])}"),
        ("creator", plan["creator"] + ("  (signer)" if not signer["simulateOnly"]
                                       else "  (--from, planning only)")),
        ("fee recipient", plan["feeRecipient"]
         + mark(not args.get("fee-recipient"))),
    ]
    if dev_wei:
        pairs += [
            ("dev buy", f"{fmt_units(dev_wei, 18)} ETH (native)"),
            ("you receive", f"~{fmt_units(plan['devBuyOut'], 18)} {plan['symbol']}  "
                            f"({plan['supplyPct']:.4f}% of supply)"),
            ("min accepted", f"{fmt_units(plan['devBuyMinOut'], 18)} "
                             f"({plan['slippageBps'] / 100:.2f}% slippage)"
                             + mark(not args.get("slippage-bps"))),
        ]
    elif dev_asset:
        pairs += [
            ("dev buy", f"{fmt_units(dev_asset, 18)} {asset_label(paired)} (ERC20)"),
            ("you receive", f"~{fmt_units(plan['devBuyOut'], 18)} {plan['symbol']}  "
                            f"({plan['supplyPct']:.4f}% of supply)"),
            ("min accepted", fmt_units(plan["devBuyMinOut"], 18)),
        ]
    else:
        pairs.append(("dev buy", "0 — you receive no tokens at launch" + default))
    pairs.append(("deadline", f"+{int(args.get('deadline-secs') or DEFAULT_DEADLINE_SECS)}s"
                              + mark(not args.get("deadline-secs"))))
    print(render_kv(pairs))
    print(f"\n  PLAN_HASH  {plan['planHash']}")


def _replay_command(args: dict) -> str:
    """The copy-paste line that executes this plan.

    Values go through ``shlex.quote``, not hand-rolled double quotes. A token
    name is attacker-influenced text — it can arrive from a pasted ticker or a
    chat message — and double quotes do not neuter ``$(…)`` or backticks in a
    shell. Printing an unquoted name would turn "here is my token name" into
    command execution for whoever runs the suggested line.
    """
    parts = ["python3 pools_write.py launch"]
    for key, value in args.items():
        if key in ("_", "broadcast", "confirm", "json"):
            continue
        if value is True:
            parts.append(f"--{key}")
        else:
            parts.append(f"--{key} {shlex.quote(str(value))}")
    return " ".join(parts)


# ── fees ────────────────────────────────────────────────────────────────────
def _locker_write(client: RpcClient, args: dict, function_name: str,
                  command: str) -> None:
    """Shared body for collect/claim/collectAndClaim.

    ``function_name`` is the ABI method; ``command`` is what the user types. The
    two differ (``collectAndClaim`` vs ``collect-and-claim``) and printing the
    wrong one gives the user a copy-paste line that does not run.
    """
    signer = resolve_signer(args)
    positional = args["_"][1:] if len(args["_"]) > 1 else []
    raw = args.get("token") or (positional[0] if positional else None)
    if not raw or raw is True:
        raise ValueError(f"usage: pools_write.py {command} <token address>")
    token = checksum_address(raw)

    info = client.read(PARTY_LOCKER, PARTY_LOCKER_ABI, "getPoolInfo", [token])
    creator = info["creator"] if isinstance(info, dict) else info[2]
    recipient = info["feeRecipient"] if isinstance(info, dict) else info[3]
    if int(creator, 16) == 0:
        raise RuntimeError(f"{token} is not registered with the pools.fun locker.")

    digest = action_hash(function_name, {
        "token": token, "caller": signer["address"], "locker": PARTY_LOCKER,
        "chainId": CHAIN["chainId"]})

    print(heading(f"{command} — nothing sent yet"))
    print(render_kv([("token", token), ("locker", PARTY_LOCKER),
                     ("creator", creator), ("fee recipient", recipient),
                     ("caller", signer["address"])]))
    print(f"\n  PLAN_HASH  {digest}")
    if not _confirmed(args, digest):
        print("\n  Nothing was sent. To execute:")
        print(f"    python3 pools_write.py {command} {token} "
              f"--broadcast --confirm {digest}")
        return

    data = encode_function_data(PARTY_LOCKER_ABI, function_name, [token])
    _send(client, signer, PARTY_LOCKER, data, args)


def cmd_collect(client: RpcClient, args: dict) -> None:
    _locker_write(client, args, "collect", "collect")


def cmd_claim(client: RpcClient, args: dict) -> None:
    _locker_write(client, args, "claim", "claim")


def cmd_collect_and_claim(client: RpcClient, args: dict) -> None:
    _locker_write(client, args, "collectAndClaim", "collect-and-claim")


def cmd_set_fee_recipient(client: RpcClient, args: dict) -> None:
    signer = resolve_signer(args)
    positional = args["_"][1:] if len(args["_"]) > 1 else []
    raw = args.get("token") or (positional[0] if positional else None)
    if not raw or raw is True:
        raise ValueError("usage: pools_write.py set-fee-recipient <token> --recipient <addr>")
    token = checksum_address(raw)
    recipient = checksum_address(require_arg(args, "recipient", "new payout address"))

    info = client.read(PARTY_LOCKER, PARTY_LOCKER_ABI, "getPoolInfo", [token])
    current = info["feeRecipient"] if isinstance(info, dict) else info[3]

    digest = action_hash("setFeeRecipient", {
        "token": token, "recipient": recipient, "caller": signer["address"],
        "locker": PARTY_LOCKER, "chainId": CHAIN["chainId"]})

    print(heading("set fee recipient — nothing sent yet"))
    print(render_kv([("token", token), ("current", current), ("new", recipient),
                     ("caller", signer["address"])]))
    print(f"\n  PLAN_HASH  {digest}")
    if not _confirmed(args, digest):
        print("\n  Nothing was sent. To execute:")
        print(f"    python3 pools_write.py set-fee-recipient {token} "
              f"--recipient {recipient} --broadcast --confirm {digest}")
        return

    data = encode_function_data(PARTY_LOCKER_ABI, "setFeeRecipient", [token, recipient])
    _send(client, signer, PARTY_LOCKER, data, args)


# ── approve ─────────────────────────────────────────────────────────────────
def cmd_approve(client: RpcClient, args: dict) -> None:
    signer = resolve_signer(args)
    paired = resolve_paired_asset(args.get("paired"))
    amount_raw = _amount(args.get("amount"), 18, "amount")
    amount = amount_raw if amount_raw else 2**256 - 1
    current = client.read(paired, ERC20_ABI, "allowance",
                          [signer["address"], PARTY_FACTORY])

    digest = action_hash("approve", {
        "asset": paired, "spender": PARTY_FACTORY, "amount": amount,
        "owner": signer["address"], "chainId": CHAIN["chainId"]})

    print(heading("approve — nothing sent yet"))
    print(render_kv([
        ("asset", f"{asset_label(paired)}  {paired}"),
        ("spender", f"{PARTY_FACTORY}  (PartyFactory)"),
        ("current allowance", fmt_units(int(current), 18)),
        ("new allowance", "unlimited" if amount == 2**256 - 1 else fmt_units(amount, 18)),
        ("owner", signer["address"]),
    ]))
    print(f"\n  PLAN_HASH  {digest}")
    if not _confirmed(args, digest):
        print("\n  Nothing was sent. To execute:")
        print(f"    python3 pools_write.py approve --paired "
              f"{asset_label(paired).lower()} --broadcast --confirm {digest}")
        return

    data = encode_function_data(ERC20_ABI, "approve", [PARTY_FACTORY, amount])
    _send(client, signer, paired, data, args)


def _require_allowance(client: RpcClient, paired: str, owner: str, needed: int) -> None:
    allowance = int(client.read(paired, ERC20_ABI, "allowance", [owner, PARTY_FACTORY]))
    if allowance >= needed:
        return
    raise RuntimeError(
        f"an ERC20 dev buy needs the factory approved to pull "
        f"{fmt_units(needed, 18)} {asset_label(paired)}, but the allowance is "
        f"{fmt_units(allowance, 18)}.\n"
        f"  Run: python3 pools_write.py approve --paired {asset_label(paired).lower()}"
    )


# ── plumbing ────────────────────────────────────────────────────────────────
def _amount(value: Any, decimals: int, flag: str) -> int:
    if value is None:
        return 0
    if value is True:
        raise ValueError(f"--{flag} needs a value")
    amount = parse_units(str(value).strip(), decimals)
    if amount < 0:
        raise ValueError(f"--{flag} cannot be negative")
    return amount


def _trim(value: str, limit: int = 64) -> str:
    return value if len(value) <= limit else value[:limit - 1] + "…"


def _short(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}"


COMMANDS = {
    "launch": cmd_launch,
    "approve": cmd_approve,
    "collect": cmd_collect,
    "claim": cmd_claim,
    "collect-and-claim": cmd_collect_and_claim,
    "set-fee-recipient": cmd_set_fee_recipient,
}


def main(argv: list[str]) -> None:
    configure_utf8_stdio()
    args = parse_args(argv)
    command = args["_"][0] if args["_"] else None
    if not command or args.get("help") or command not in COMMANDS:
        print(USAGE)
        if command and command not in COMMANDS:
            print(f"\nunknown command: {command}")
            sys.exit(1)
        return
    client = RpcClient(rpc_url=opt_str(args, "rpc"), debug=bool(args.get("debug")))
    try:
        COMMANDS[command](client, args)
    except RpcError as exc:
        hint = explain_revert(exc.data)
        raise RuntimeError(hint or str(exc)) from exc


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except BaseException as exc:  # noqa: BLE001 — single top-level reporter
        die(exc)
