#!/usr/bin/env python3
"""pools.fun read commands — factory state, salt mining, launch simulation.

Nothing here can move funds. The module never imports ``poolsfun.secp256k1``, so
there is no signing function anywhere in its import graph; the most it can do
with ``POOLSFUN_PRIVATE_KEY`` is derive the public address from it, and that only
to answer "which wallet would this launch from". ``selftest.py`` asserts the
property rather than trusting the convention.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from poolsfun.chains import (
    EXPLORER,
    PARTY_FACTORY,
    PARTY_LOCKER,
    USDG,
    WETH,
    asset_label,
    resolve_paired_asset,
    resolve_signer_address,
)
from poolsfun.factory import (
    ERC20_ABI,
    PARTY_FACTORY_ABI,
    PARTY_LOCKER_ABI,
    TOTAL_SUPPLY,
    V3_POOL_ABI,
    mine_salt,
    price_from_tick,
    salt_hit_rate,
)
from poolsfun.fmt import (
    die,
    fmt_units,
    fmt_usd,
    heading,
    json_safe,
    opt_int,
    opt_str,
    parse_args,
    render_kv,
    render_table,
    require_arg,
)
from poolsfun.metadata import pinata_configured, resolve_metadata_uri
from poolsfun.plan import (
    plan_launch,
    read_factory_state,
    read_paired_curve,
    read_paired_usd,
    read_start_tick,
)
from poolsfun.rpc import RpcClient

# Bundled scripts run under AgentOS's own interpreter; the path insert only
# matters in a source checkout where the package is not installed (#2804).
_SRC_ROOT = str(Path(__file__).resolve().parents[5])
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)
from agentos.skills.stdio import configure_utf8_stdio  # noqa: E402

USAGE = """
pools_read.py — read pools.fun launch state (never signs, never spends)

  preflight [--paired weth|usdg]
      Can I launch right now, and at what price? Factory switches, the live
      launch tick, the resulting token price and FDV, and whether Pinata is set up.

  assets
      The factory's paired-asset allowlist and each asset's pricing curve.

  mine-salt --name <n> --symbol <s> [--metadata-uri <uri>] [--paired weth|usdg]
            [--deployer <addr>] [--max-salt-attempts <n>]
      Find a salt whose CREATE2 token address sorts below the paired asset.
      Required before any launch; `launch` does it for you.

  simulate --name <n> --symbol <s> [--dev-buy <eth>] [--paired weth|usdg]
           [--metadata-uri <uri>] [--from <addr>] [--salt <bytes32>]
      Dry-run the real launch via eth_call and report the exact token address,
      pool address and dev-buy fill. Costs nothing and needs no funds.

  token <address>
      A launched token: locker registration, pool, current tick and price, supply.

  fees <address>
      Creator fee position for a launched token.

  find-image [--session <id>] [--limit <n>]
      Locate an image the user attached in chat, so it can be passed to
      `launch --image`. AgentOS never tells you the path, and small images
      are never written to disk at all — this reports what is actually there.

Common flags
  --json          machine-readable output
  --paired        weth (default) or usdg
  --signer-env    read the wallet address from a different env var

The RPC endpoint is fixed (Robinhood Chain, 4663) — no configuration needed.
"""


# ── helpers ─────────────────────────────────────────────────────────────────
def _resolve_actor(args: dict) -> tuple[str, str]:
    """The address a launch would come from, and where we learned it.

    Prefers an explicit ``--from``/``--deployer`` so every read command works with
    no key configured at all; falls back to the address the signing key derives.
    """
    explicit = args.get("from") or args.get("deployer")
    if explicit and explicit is not True:
        from poolsfun.hexutil import checksum_address
        return checksum_address(explicit), "--from"
    signer_env = args.get("signer-env")
    try:
        env_name = signer_env if isinstance(signer_env, str) else None
        return (resolve_signer_address(env_name) if env_name
                else resolve_signer_address()), "signer key"
    except RuntimeError as exc:
        raise RuntimeError(
            f"{exc}\n  Or pass --from <address> to plan for a wallet without a key here."
        ) from exc


def _emit(args: dict, payload: dict, render: Any) -> None:
    if args.get("json"):
        print(json.dumps(json_safe(payload), indent=2))
    else:
        render()


def _fdv_line(plan_like: dict) -> str:
    fdv = plan_like.get("fdvUsd")
    target = plan_like.get("targetFdvUsd")
    if fdv is None:
        return "n/a (price feed unavailable)"
    text = fmt_usd(fdv)
    if target:
        text += f"  (protocol target {fmt_usd(float(target))})"
    return text


# ── commands ────────────────────────────────────────────────────────────────
def cmd_preflight(client: RpcClient, args: dict) -> None:
    paired = resolve_paired_asset(args.get("paired"))
    state = read_factory_state(client)
    tick, live = read_start_tick(client, paired)
    price = read_paired_usd(client, paired)
    allowed = client.read(PARTY_FACTORY, PARTY_FACTORY_ABI,
                          "allowedPairedAsset", [paired])

    paired_usd = price["usd"] if price else None
    token_price = price_from_tick(tick) * paired_usd if paired_usd else None
    fdv = token_price * (TOTAL_SUPPLY / 10**18) if token_price else None
    # `paused is None` means the call failed, not that the factory is running.
    paused = state.get("paused")
    ready = bool(allowed) and paused is False and live

    payload = {
        "paused": state.get("paused"), "locker": state.get("locker"),
        "owner": state.get("owner"), "initialFdvUsd": state.get("initialFdvUsd"),
        "pairedAsset": paired, "allowed": bool(allowed),
        "startTick": tick, "tickLive": live,
        "pairedUsd": paired_usd, "tokenPriceUsd": token_price, "fdvUsd": fdv,
        "targetFdvUsd": state.get("initialFdvUsd"),
        "pinataConfigured": pinata_configured(), "readyToLaunch": ready,
    }

    def render() -> None:
        print(heading("pools.fun preflight"))
        print(render_kv([
            ("factory", PARTY_FACTORY),
            ("paused", "unknown — could not read the factory" if paused is None
             else ("yes — launches disabled" if paused else "no")),
            ("locker", state.get("locker") or "unset"),
            ("paired asset", f"{asset_label(paired)}  {paired}"),
            ("allowlisted", "yes" if allowed else "NO — cannot launch against this asset"),
            ("start tick", f"{tick}  ({'live feed' if live else 'FALLBACK — FDV off target'})"),
            (f"{asset_label(paired)} price", fmt_usd(paired_usd) if paired_usd else "n/a"),
            ("token price", fmt_usd(token_price) if token_price else "n/a"),
            ("launch FDV", _fdv_line(payload)),
            ("supply", f"{TOTAL_SUPPLY // 10**18:,} (fixed)"),
            ("fee tier", "1% full range (fixed)"),
            ("token image", "Pinata configured" if pinata_configured()
             else "no PINATA_JWT — launches work, but --image is unavailable"),
            ("ready to launch", "yes" if ready else "no — see above"),
        ]))
        print("\n  The launch price is protocol state, not an input: the factory derives")
        print(f"  the tick from its own feed to target ${state.get('initialFdvUsd')} FDV.")
        print("  No flag can change it. A dev buy only moves price up, after the open.")

    _emit(args, payload, render)


def cmd_assets(client: RpcClient, args: dict) -> None:
    rows = []
    payload = []
    for asset in (WETH, USDG):
        allowed = client.read(PARTY_FACTORY, PARTY_FACTORY_ABI,
                              "allowedPairedAsset", [asset])
        curve = read_paired_curve(client, asset)
        tick, live = (read_start_tick(client, asset) if allowed else (None, False))
        price = read_paired_usd(client, asset) if allowed else None
        payload.append({"asset": asset, "label": asset_label(asset),
                        "allowed": bool(allowed), "curve": curve,
                        "startTick": tick, "tickLive": live,
                        "usd": price["usd"] if price else None})
        rows.append({
            "asset": asset_label(asset),
            "address": asset,
            "allowed": "yes" if allowed else "no",
            "tick": "n/a" if tick is None else str(tick),
            "source": ("live" if live else "fallback") if allowed else "-",
            "usd": fmt_usd(price["usd"]) if price else "n/a",
            "maxAge": f"{curve['maxPriceAge'] // 3600}h" if curve["maxPriceAge"] else "-",
        })

    def render() -> None:
        print(heading("paired assets (the launch allowlist)"))
        print(render_table([
            {"key": "asset", "label": "ASSET"},
            {"key": "address", "label": "ADDRESS"},
            {"key": "allowed", "label": "ALLOWED"},
            {"key": "tick", "label": "TICK", "align": "right"},
            {"key": "source", "label": "SOURCE"},
            {"key": "usd", "label": "USD", "align": "right"},
            {"key": "maxAge", "label": "FEED AGE", "align": "right"},
        ], rows))
        print("\n  Only allowlisted assets can be launched against. A native-ETH dev buy")
        print("  works on WETH pairs only; USDG pairs need --dev-buy-asset + approve.")

    _emit(args, {"assets": payload}, render)


def cmd_mine_salt(client: RpcClient, args: dict) -> None:
    name = require_arg(args, "name", "token name")
    symbol = require_arg(args, "symbol", "token symbol")
    paired = resolve_paired_asset(args.get("paired"))
    deployer, _ = _resolve_actor(args)
    uri = opt_str(args, "metadata-uri")
    if not uri:
        uri, _doc, _how = resolve_metadata_uri(name=name, symbol=symbol)
    max_attempts = opt_int(args, "max-salt-attempts", 5000, minimum=1)

    started = time.time()
    salt, token, attempts = mine_salt(client, PARTY_FACTORY, deployer, name, symbol,
                                      uri, paired, max_attempts=max_attempts)
    payload = {"salt": salt, "saltInt": int(salt, 16), "token": token,
               "attempts": attempts, "deployer": deployer, "pairedAsset": paired,
               "metadataUri": uri, "seconds": round(time.time() - started, 2)}

    def render() -> None:
        print(heading("mined salt"))
        print(render_kv([
            ("salt", salt),
            ("token address", token),
            ("sorts below", f"{asset_label(paired)}  {paired}"),
            ("deployer", f"{deployer}  (the salt is bound to this address)"),
            ("attempts", f"{attempts}  (~{salt_hit_rate(paired) * 100:.1f}% of salts qualify)"),
            ("elapsed", f"{payload['seconds']}s"),
        ]))
        print("\n  This salt only works for this exact name, symbol, metadataUri and")
        print("  deployer. Change any of them and it must be re-mined.")

    _emit(args, payload, render)


def cmd_simulate(client: RpcClient, args: dict) -> None:
    name = require_arg(args, "name", "token name")
    symbol = require_arg(args, "symbol", "token symbol")
    if args.get("image"):
        raise RuntimeError(
            "simulate does not pin images — pinning is a write to an external service "
            "and this command is read-only.\n"
            "  Use `pools_write.py launch --image …` without --broadcast for a full "
            "dry run, or pass --metadata-uri here."
        )
    paired = resolve_paired_asset(args.get("paired"))
    creator, source = _resolve_actor(args)
    uri = opt_str(args, "metadata-uri")
    if not uri:
        uri, _doc, _how = resolve_metadata_uri(
            name=name, symbol=symbol, description=opt_str(args, "description"))

    dev_buy_wei = _parse_eth(args.get("dev-buy"))
    salt = opt_str(args, "salt")
    plan = plan_launch(
        client, factory=PARTY_FACTORY, name=name, symbol=symbol, metadata_uri=uri,
        paired_asset=paired, creator=creator, fee_recipient=creator,
        deadline=int(time.time()) + 1200, dev_buy_wei=dev_buy_wei, salt=salt,
        max_salt_attempts=opt_int(args, "max-salt-attempts", 5000, minimum=1),
        allow_fallback_tick=bool(args.get("allow-fallback-tick")))

    def render() -> None:
        print(heading("launch simulation (nothing was sent)"))
        pairs = [
            ("name / symbol", f"{name} / {symbol}"),
            ("would launch from", f"{creator}  ({source})"),
            ("token address", plan["token"]),
            ("pool address", plan["pool"]),
            ("salt", f"{plan['salt']}  ({plan['saltAttempts']} tries)"),
            ("start tick", f"{plan['expectedStartTick']}  "
                           f"({'live feed' if plan['tickLive'] else 'FALLBACK'})"),
            ("token price", fmt_usd(plan["tokenPriceUsd"])),
            ("launch FDV", _fdv_line(plan)),
        ]
        if dev_buy_wei:
            pairs += [
                ("dev buy", f"{fmt_units(dev_buy_wei, 18)} ETH"),
                ("you would receive", f"{fmt_units(plan['devBuyOut'], 18)} {symbol}"),
                ("share of supply", f"{plan['supplyPct']:.4f}%"),
                ("cost basis", fmt_usd(dev_buy_wei / 1e18 * (plan['pairedUsd'] or 0))),
            ]
        else:
            pairs.append(("dev buy", "0 — pass --dev-buy <eth> to buy at launch"))
        print(render_kv(pairs))
        print("\n  The dev-buy fill is exact, not an estimate: it is the pool's first")
        print("  swap inside the launch transaction, so nothing can front-run it.")

    _emit(args, plan, render)


def cmd_token(client: RpcClient, args: dict) -> None:
    from poolsfun.hexutil import checksum_address

    positional = args["_"][1:] if len(args["_"]) > 1 else []
    raw = args.get("token") or (positional[0] if positional else None)
    if not raw or raw is True:
        raise ValueError("usage: pools_read.py token <address>")
    token = checksum_address(raw)

    info = client.read(PARTY_LOCKER, PARTY_LOCKER_ABI, "getPoolInfo", [token])
    if isinstance(info, dict):
        paired, pool = info["pairedAsset"], info["pool"]
        creator, fee_recipient = info["creator"], info["feeRecipient"]
        token_ids = info["tokenIds"]
    else:
        paired, pool, creator, fee_recipient, token_ids = info
    if int(pool, 16) == 0:
        raise RuntimeError(
            f"{token} is not registered with the pools.fun locker — it was not "
            "launched through this factory."
        )

    name = client.read(token, ERC20_ABI, "name")
    symbol = client.read(token, ERC20_ABI, "symbol")
    supply = client.read(token, ERC20_ABI, "totalSupply")
    try:
        uri = client.read(token, ERC20_ABI, "metadataUri")
    except Exception:
        uri = None
    slot0 = client.read(pool, V3_POOL_ABI, "slot0")
    tick = int(slot0["tick"] if isinstance(slot0, dict) else slot0[1])
    price = read_paired_usd(client, paired)
    paired_usd = price["usd"] if price else None
    token_price = price_from_tick(tick) * paired_usd if paired_usd else None

    payload = {"token": token, "name": name, "symbol": symbol,
               "totalSupply": int(supply), "metadataUri": uri, "pool": pool,
               "pairedAsset": paired, "creator": creator,
               "feeRecipient": fee_recipient,
               "lpTokenIds": [int(t) for t in token_ids],
               "currentTick": tick, "tokenPriceUsd": token_price,
               "marketCapUsd": (token_price * int(supply) / 1e18) if token_price else None}

    def render() -> None:
        print(heading(f"{name} ({symbol})"))
        print(render_kv([
            ("token", token),
            ("pool", f"{pool}  1% fee"),
            ("paired with", f"{asset_label(paired)}  {paired}"),
            ("creator", creator),
            ("fee recipient", fee_recipient
             + ("  (same as creator)" if fee_recipient.lower() == creator.lower() else "")),
            ("LP position", f"NFT #{', #'.join(str(int(t)) for t in token_ids)} "
                            f"held by the locker (permanent)"),
            ("supply", f"{int(supply) // 10**18:,}"),
            ("current tick", str(tick)),
            ("token price", fmt_usd(token_price) if token_price else "n/a"),
            ("market cap", fmt_usd(payload["marketCapUsd"])
             if payload["marketCapUsd"] else "n/a"),
            ("metadata", uri or "n/a"),
            ("explorer", f"{EXPLORER}/token/{token}"),
        ]))

    _emit(args, payload, render)


def cmd_fees(client: RpcClient, args: dict) -> None:
    from poolsfun.hexutil import checksum_address

    positional = args["_"][1:] if len(args["_"]) > 1 else []
    raw = args.get("token") or (positional[0] if positional else None)
    if not raw or raw is True:
        raise ValueError("usage: pools_read.py fees <address>")
    token = checksum_address(raw)

    info = client.read(PARTY_LOCKER, PARTY_LOCKER_ABI, "getPoolInfo", [token])
    paired = info["pairedAsset"] if isinstance(info, dict) else info[0]
    creator = info["creator"] if isinstance(info, dict) else info[2]
    fee_recipient = info["feeRecipient"] if isinstance(info, dict) else info[3]
    if int(paired, 16) == 0:
        raise RuntimeError(f"{token} is not registered with the pools.fun locker.")

    splits = client.read(PARTY_LOCKER, PARTY_LOCKER_ABI, "getPoolSplits", [token])
    values = list(splits.values()) if isinstance(splits, dict) else list(splits)
    labels = ["creator", "protocol", "buyback", "community", "tokenCreator", "tokenProtocol"]
    split_map = {k: int(v) for k, v in zip(labels, values)}

    pending_token = client.read(token, ERC20_ABI, "balanceOf", [PARTY_LOCKER])
    pending_paired = client.read(paired, ERC20_ABI, "balanceOf", [PARTY_LOCKER])

    payload = {"token": token, "creator": creator, "feeRecipient": fee_recipient,
               "splitsBps": split_map, "pairedAsset": paired,
               "lockerTokenBalance": int(pending_token),
               "lockerPairedBalance": int(pending_paired)}

    def render() -> None:
        print(heading("creator fees"))
        print(render_kv([
            ("token", token),
            ("creator", creator),
            ("fee recipient", fee_recipient),
            ("your split", f"{split_map['creator'] / 100:.2f}% of trading fees"),
            ("locker holds", f"{fmt_units(int(pending_token), 18)} token  +  "
                             f"{fmt_units(int(pending_paired), 18)} {asset_label(paired)}"),
        ]))
        print("\n  Fees accrue inside the LP position until collected. Run")
        print("  `pools_write.py collect-and-claim <token>` to sweep and pay out.")
        print("  The locker balance above is shared across all launched tokens —")
        print("  it is an upper bound on what a collect would move, not your share.")

    _emit(args, payload, render)


def cmd_find_image(client: RpcClient, args: dict) -> None:
    """Locate a user-attached image on disk, or explain why there is none.

    Exists because AgentOS never tells the model where an attachment lives, so
    an agent asked to "use the image I just sent as the logo" has nothing to
    pass to --image and will otherwise guess at paths that do not exist.
    """
    import time as _time

    from poolsfun.metadata import (
        STALE_ATTACHMENT_SECONDS,
        agentos_media_root,
        find_attachment_images,
        find_chat_images,
        materialize_chat_image,
        sessions_db_path,
    )

    session = opt_str(args, "session")
    limit = opt_int(args, "limit", 10, minimum=1, maximum=200)
    extract = not args.get("no-extract")

    images = find_chat_images(session=session, limit=limit)
    now = _time.time()
    written: str | None = None
    error: str | None = None

    if images and extract:
        try:
            written = str(materialize_chat_image(images[0]))
        except Exception as exc:  # noqa: BLE001 — reported, not fatal
            error = str(exc)

    # Only if the transcript told us nothing: the media directory cannot say
    # which message a blob belongs to, so it is a hint, never an answer.
    staged = [] if images else find_attachment_images(limit=limit)

    payload = {"images": images, "found": len(images), "extractedTo": written,
               "extractError": error, "staged": staged,
               "sessionsDb": str(sessions_db_path())}

    def render() -> None:
        print(heading("images the user attached in chat"))
        if not images:
            print(f"  no image attachment found in {sessions_db_path()}")
            if staged:
                print(f"\n  {len(staged)} image(s) exist under "
                      f"{agentos_media_root()}/transcripts, but nothing in the")
                print("  transcript points at them — they belong to older sessions and are")
                print("  almost certainly NOT what the user just sent. Do not pin one.")
            print("\n  Pick one, in this order:")
            print("    - Ask the user for the image's path on their machine.")
            print("      (In the CLI they can run `! ls ~/Downloads/*.png`.)")
            print("    - If the logo is already hosted: `--metadata-uri ipfs://…`")
            print("    - Launch with no logo — but say so first; it cannot be added later.")
            return

        rows = []
        for image in images:
            age = now - image["sent_at"] if image["sent_at"] else None
            rows.append({
                "when": (_time.strftime("%m-%d %H:%M", _time.localtime(image["sent_at"]))
                         if image["sent_at"] else "?"),
                "age": _age(age),
                "name": str(image["name"])[:28],
                "type": image["mime"].split("/")[-1],
                "size": f"{image['bytes'] / 1024:.0f} KB",
                "src": image["source"],
                "session": image["session_key"].rsplit(":", 1)[-1][:12],
            })
        print(render_table([
            {"key": "when", "label": "WHEN"},
            {"key": "age", "label": "AGE", "align": "right"},
            {"key": "name", "label": "NAME"},
            {"key": "type", "label": "TYPE"},
            {"key": "size", "label": "SIZE", "align": "right"},
            {"key": "src", "label": "SOURCE"},
            {"key": "session", "label": "SESSION"},
        ], rows))

        newest = images[0]
        age = now - newest["sent_at"] if newest["sent_at"] else None
        if age is not None and age > STALE_ATTACHMENT_SECONDS:
            print(f"\n  WARNING: the newest attachment is {_age(age)} old, in session "
                  f"{newest['session_key'].rsplit(':', 1)[-1]}.")
            print("  That is probably NOT the image just sent. Confirm with the user")
            print("  before pinning it — a token's logo cannot be changed afterwards.")
        if error:
            print(f"\n  could not extract it: {error}")
        elif written:
            print(f"\n  newest written to: {written}")
            print("  Open it and confirm it is the right picture, then:")
            print(f"    pools_write.py launch --name … --symbol … --image {written}")

    _emit(args, payload, render)


def _age(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds // 60)}m"
    if seconds < 172800:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _parse_eth(value: Any) -> int:
    if value is None:
        return 0
    if value is True:
        raise ValueError("--dev-buy needs a value")
    from poolsfun.hexutil import parse_units
    amount = parse_units(str(value).strip(), 18)
    if amount < 0:
        raise ValueError("--dev-buy cannot be negative")
    return amount


COMMANDS = {
    "preflight": cmd_preflight,
    "assets": cmd_assets,
    "mine-salt": cmd_mine_salt,
    "simulate": cmd_simulate,
    "token": cmd_token,
    "fees": cmd_fees,
    "find-image": cmd_find_image,
}


def main(argv: list[str]) -> None:
    configure_utf8_stdio()
    args = parse_args(argv)
    command = args["_"][0] if args["_"] else None
    if not command or args.get("help") or command not in COMMANDS:
        print(USAGE.strip())
        if command and command not in COMMANDS:
            print(f"\nunknown command: {command}")
            sys.exit(1)
        return
    client = RpcClient(rpc_url=opt_str(args, "rpc"), debug=bool(args.get("debug")))
    COMMANDS[command](client, args)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except BaseException as exc:  # noqa: BLE001 — single top-level reporter
        die(exc)
