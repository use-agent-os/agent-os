#!/usr/bin/env python3
"""Unattended take-profit for a one-sided Uniswap V4 position.

A one-sided position is a limit order that fills gradually. This runner watches one fill and,
at each milestone, exits the whole position, keeps what has already converted, and redeploys
only the unconverted remainder into a narrower range running from the current price to the
original far edge. It ratchets: converted value never goes back to work, and a price
retracement leaves the position sitting still rather than unwinding.

    arm    --token-id <id> [--steps 30,60,100]      plan a mandate, then --confirm it
    tick   [--id <m>|--all] [--broadcast]           reconcile and fire; what cron runs
    status --id <m>      list      disarm --id <m>      clear-attention --id <m>

Why this is a separate entrypoint. ``lp_read.py`` never imports a signing path, which is what
makes it safe to allowlist wholesale. ``lp_write.py`` is the attended path and still refuses
to broadcast without a PLAN_HASH a human echoed back. This file is the third thing: it reads,
it writes, and it runs with nobody watching. Keeping it separate puts that boundary somewhere
a reviewer can see it.

The authorization it uses is narrow by construction — see ``lp_write.MandateAuthorization``.
Arming is still an attended, hash-confirmed act; what the mandate buys is the right to replay
*that* approval against a plan proven to fall inside it.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lp_write  # noqa: E402
from unilp.chains import resolve_chain  # noqa: E402
from unilp.fmt import (  # noqa: E402
    die,
    fmt_units,
    heading,
    opt_int,
    opt_str,
    parse_args,
    render_kv,
    require_arg,
    short_id,
)
from unilp.journal import (  # noqa: E402
    SCHEMA_VERSION,
    MandateStore,
    canonical_json,
    mandate_id,
    state_root,
)
from unilp.ratchet_math import (  # noqa: E402
    CURRENCY0,
    RangeExhaustedError,
    due_milestone,
    far_edge_tick,
    fills_as_tick_rises,
    milestone_thresholds,
    parse_steps,
    principal_for_range,
    rearm_range,
    remaining_principal,
    tick_for_remaining,
)
from unilp.reconcile import (  # noqa: E402
    AMBIGUOUS,
    LANDED,
    NOT_SENT,
    PENDING,
    UNAVAILABLE,
    find_minted_token_id,
    position_truth,
    reconcile,
)
from unilp.rpc import RpcClient  # noqa: E402
from unilp.simulate import net_transfers  # noqa: E402
from unilp.v4_actions import build_ratchet_plan, describe_actions  # noqa: E402
from unilp.v4_math import (  # noqa: E402
    get_amounts_for_liquidity,
    get_liquidity_for_amount0,
    get_liquidity_for_amount1,
    get_sqrt_ratio_at_tick,
    raw_price_at_tick,
)
from unilp.v4_pool import compute_pool_id, format_hook_flags  # noqa: E402

USAGE = """
senior-unilp-manager — ratchet: unattended take-profit on a one-sided position

  arm     --token-id <id> [--steps 30,60,100] [--slippage-bps 100] [--allow-hooked]
          [--label <name>] [--max-tick-drift <n>] [--deadline-secs 1200]
          [--max-principal-per-fire <human>] [--max-fee-per-gas <wei>] [--expires-days <n>]
          Dry run prints the mandate table and a MANDATE_HASH; re-run with
          --confirm <MANDATE_HASH> to arm it.
  tick    [--id <m> | --all] [--broadcast] [--json] [--alert-only]
          Reconcile against the chain and fire any milestone that is due. Without
          --broadcast this is a full dry run of the transaction, and sends nothing.
          --alert-only (with --json) ends a tick that found nothing on the gate line
          {"wakeAgent": false}, so a cron script job records the run but delivers
          no message. A tick that fired, halted, or needs a human prints a summary
          above the JSON and is delivered as usual.
  status  --id <m> [--json]
  list    [--json]
  disarm  --id <m>
  clear-attention --id <m> [--token-id <new>]
          Acknowledge a NEEDS_ATTENTION mandate and re-arm it. Pass --token-id when a
          fire landed but the runner could not work out which position replaced the
          old one; it is validated against the mandate's pool, signer and far edge
          before being adopted.

Global: --chain <key|id>  (default robinhood)   --rpc <url>   --signer-env <VAR>
        --from <addr>     plan-only, no key, can never broadcast

Milestones are measured against the ORIGINAL principal: 100k with --steps 30,60,100 fires
when 70k, then 40k, then 0 is left. Three fires, then the mandate completes.
"""

STATE_ARMED = "ARMED"
STATE_FIRE_SENT = "FIRE_SENT"
STATE_COMPLETE = "COMPLETE"
STATE_NEEDS_ATTENTION = "NEEDS_ATTENTION"
STATE_DISARMED = "DISARMED"
STATE_EXPIRED = "EXPIRED"

TERMINAL_STATES = {STATE_COMPLETE, STATE_DISARMED, STATE_EXPIRED, STATE_NEEDS_ATTENTION}

# A tick skips NEEDS_ATTENTION, but an arm must not: that mandate is waiting for a human and
# still owns its position. The two sets are deliberately different, not an oversight.
FINISHED_STATES = {STATE_COMPLETE, STATE_DISARMED, STATE_EXPIRED}

# What makes two mandates the same *instruction*. Everything else in ``immutable`` is either
# a measurement taken at arm time — ``originalPrincipalRaw`` is read off the chain and moves
# with the price — or a display string. Counting those as terms would mean an honest re-run
# of the same setup could never be recognised as one.
POLICY_FIELDS: tuple[str, ...] = (
    "chainId",
    "positionManager",
    "poolId",
    "signer",
    "principal",
    "farEdgeTick",
    "stepsBps",
)

DEFAULT_MAX_ATTEMPTS = 5


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _big(value) -> int:
    """Read back an integer the journal may have stored as a string."""
    return int(str(value))


def _token_map(info0: dict, info1: dict) -> dict:
    return {info0["address"].lower(): info0, info1["address"].lower(): info1}


def _principal_info(mandate: dict, info0: dict, info1: dict) -> tuple[dict, dict]:
    """``(principal, harvested)`` token info, in that order."""
    if mandate["immutable"]["principal"] == CURRENCY0:
        return info0, info1
    return info1, info0


def _emit(payload: dict) -> None:
    print(json.dumps(payload, indent=2, default=str))


# Actions that mean the mandate is exactly where it was left. A watchdog that
# reports these is reporting that it has nothing to report.
QUIET_ACTIONS = frozenset({"noop", "skipped", "waiting", "deferred"})


def is_notable(outcome: dict) -> bool:
    """Whether one tick outcome is worth waking a human for.

    NEEDS_ATTENTION is checked apart from the action on purpose: it is a terminal
    state, so every tick after the halt reports a plain ``noop`` while the mandate
    sits there waiting for a person. Filtering on the action alone would silence
    the one alarm that must never go quiet.
    """
    if outcome.get("state") == STATE_NEEDS_ATTENTION:
        return True
    return outcome.get("action") not in QUIET_ACTIONS


def tick_summary_line(outcome: dict) -> str:
    """One human-readable line for a notable mandate, for a chat alert.

    The JSON below it carries everything; this is what someone reads on a phone.
    """
    parts = [f"{str(outcome.get('action', '?')).upper()} {outcome['mandateId'][:8]}"]
    state = outcome.get("state")
    if state and state != STATE_ARMED:
        parts.append(f"state={state}")
    if outcome.get("tokenId"):
        parts.append(f"#{outcome['tokenId']}")
    if outcome.get("txHash"):
        parts.append(f"tx {outcome['txHash']}")
    detail = outcome.get("reason") or outcome.get("note") or ""
    line = "  ".join(parts)
    return f"{line} — {detail}" if detail else line


def _client(chain: dict, args: dict) -> RpcClient:
    return RpcClient(chain, opt_str(args, "rpc"))


def _now() -> int:
    return int(time.time())


# Events whose loss cannot cause the runner to act wrongly, because the state they carry is
# re-derived from the chain on the next tick. Listing them out rather than defaulting to
# "ignore" means a future event type cannot be added and silently skipped by the replay.
REPLAY_IGNORED = {
    "armed", "plan.built", "plan.rejected", "tick.noop", "tick.dryrun",
    "milestone.reached", "reconcile.adopted", "complete", "needs_attention",
}


def replay_tail(mandate: dict, records: list[dict]) -> dict:
    """Re-apply journalled records the mandate file never got to see.

    Every side effect appends its outcome and fsyncs *before* the mandate file is replaced,
    so a crash in that window leaves the log ahead of the view. Most of those records need
    no replay: the state they describe — a fire that landed, a milestone that came due — is
    re-derived from the chain a moment later, and re-deriving is more trustworthy than
    replaying anyway.

    ``tx.sent`` is the exception, and it is the one that matters. Lose it and the mandate
    reads ``ARMED`` with no fire in flight, so the next tick plans that same milestone again
    and broadcasts a second transaction. The single-transaction design keeps that from
    duplicating anything — both transactions burn the *same* NFT, so whichever loses reverts
    — but the survivor is then a transaction the mandate has no record of, and the recovery
    scan starts from the wrong block and lands in ``NEEDS_ATTENTION``. Cheaper to not lose
    the record.
    """
    for record in records:
        event = record.get("event")
        if event == "tx.sent":
            mandate["state"] = STATE_FIRE_SENT
            mandate["currentFire"] = record.get("currentFire") or mandate.get("currentFire")
            if record.get("milestonesFired") is not None:
                mandate["milestonesFired"] = int(record["milestonesFired"])
        elif event == "tx.dropped":
            # Ordering matters: a tail of [tx.sent, tx.dropped] must end at ARMED.
            mandate["state"] = STATE_ARMED
            mandate["currentFire"] = None
            if record.get("attempt") is not None:
                mandate["attempt"] = int(record["attempt"])
        elif event == "expired":
            mandate["state"] = STATE_EXPIRED
        elif event == "disarmed":
            # The one an operator would notice: a disarm that crashed before the replace
            # would otherwise leave a mandate live that they were told was off.
            mandate["state"] = STATE_DISARMED
        elif event == "state.changed" and record.get("to"):
            mandate["state"] = str(record["to"])
            mandate["currentFire"] = None
        elif event not in REPLAY_IGNORED:
            raise RuntimeError(
                f"journal holds an unreplayed {event!r} record the runner does not know how "
                "to interpret. Refusing to run on a state it cannot reconstruct."
            )
    return mandate


# ---------------------------------------------------------------------------
# arm
# ---------------------------------------------------------------------------


def build_mandate(client, chain: dict, args: dict, signer: dict) -> dict:
    token_id = int(require_arg(args, "token-id", "the position NFT to ratchet"))
    position = lp_write.load_position_for_write(client, chain, token_id)
    pool_key = position["poolKey"]
    lp_write.hook_gate(pool_key, args)

    if position["owner"] and position["owner"].lower() != signer["address"].lower():
        raise RuntimeError(
            f"position #{token_id} is owned by {position['owner']}, not {signer['address']}. "
            "A mandate can only manage a position its own signer holds."
        )
    if position["liquidity"] <= 0:
        raise RuntimeError(f"position #{token_id} holds no liquidity — nothing to ratchet")

    pool = lp_write.load_pool_state(client, chain, position["poolId"], pool_key)
    principal = principal_for_range(pool["tick"], position["tickLower"],
                                    position["tickUpper"])
    far_edge = far_edge_tick(position["tickLower"], position["tickUpper"], principal)
    original = remaining_principal(pool["sqrtPriceX96"], position["tickLower"],
                                   position["tickUpper"], position["liquidity"], principal)
    if original <= 0:
        raise RuntimeError(
            "the position holds none of its principal currency already — it is fully "
            "converted, so there is nothing for a ratchet to do"
        )

    steps = parse_steps(opt_str(args, "steps"))
    thresholds = milestone_thresholds(original, steps)

    expires_days = opt_str(args, "expires-days")
    max_per_fire = opt_str(args, "max-principal-per-fire")
    max_fee_per_gas = opt_str(args, "max-fee-per-gas")
    info0 = lp_write.token_info(client, chain, pool_key["currency0"])
    info1 = lp_write.token_info(client, chain, pool_key["currency1"])
    principal_info = info0 if principal == CURRENCY0 else info1

    immutable = {
        "schemaVersion": SCHEMA_VERSION,
        "chainId": chain["chainId"],
        "chainKey": chain["key"],
        "positionManager": chain["positionManager"],
        # poolId IS keccak(PoolKey), so pinning it pins the currencies, the fee, the tick
        # spacing and — the one that matters for an unattended run — the hook address.
        "poolId": position["poolId"],
        "poolKey": pool_key,
        "signer": signer["address"],
        "originalTokenId": token_id,
        "principal": principal,
        "farEdgeTick": far_edge,
        "originalTickLower": position["tickLower"],
        "originalTickUpper": position["tickUpper"],
        "originalPrincipalRaw": str(original),
        "stepsBps": steps,
        "label": opt_str(args, "label") or "",
    }

    return {
        "immutable": immutable,
        "bounds": {
            "maxSlippageBps": opt_int(args, "slippage-bps", lp_write.DEFAULT_SLIPPAGE_BPS),
            "maxDeadlineSecs": lp_write.deadline_offset(args),
            "maxTickDrift": opt_int(args, "max-tick-drift", pool_key["tickSpacing"]),
            "allowHooked": bool(args.get("allow-hooked")),
            "maxAttempts": opt_int(args, "max-attempts", DEFAULT_MAX_ATTEMPTS),
            "maxPrincipalRawPerFire": (
                str(lp_write.parse_amount(max_per_fire, principal_info["decimals"]))
                if max_per_fire else None
            ),
            "maxFeePerGasWei": (
                str(int(max_fee_per_gas.replace("_", ""))) if max_fee_per_gas else None
            ),
            "expiresAt": _now() + int(float(expires_days) * 86_400) if expires_days else None,
        },
        "state": STATE_ARMED,
        "tokenId": token_id,
        "tickLower": position["tickLower"],
        "tickUpper": position["tickUpper"],
        "liquidity": str(position["liquidity"]),
        "thresholds": [str(t) for t in thresholds],
        "milestonesFired": 0,
        "currentFire": None,
        "realized": {"amount0": "0", "amount1": "0"},
        "lastSeq": 0,
        "createdAt": _now(),
        "history": [],
    }


def find_position_conflict(root, chain_key: str, ident: str, immutable: dict) -> dict | None:
    """A live mandate already driving this position, or ``None``.

    ``mandate_id`` hashes ``label`` along with the terms, so arming one position twice under
    two names yields two ids, two files and two mandates that each intend to burn the same
    NFT. ``store.exists()`` cannot see that — the ids differ by construction — so uniqueness
    per position has to be a scan. It is cheap: arming is rare and human-supervised.

    A mandate that no longer hashes to its own filename raises out of here rather than being
    skipped. That file might BE the duplicate, and what an arm authorizes is unattended
    broadcasts; refusing to proceed past an unreadable one is the only honest option.
    """
    root = Path(root)
    token_id = int(immutable["originalTokenId"])
    for other in MandateStore.list_ids(root, str(chain_key)):
        if other == ident:
            continue
        mandate = MandateStore(root, str(chain_key), other).load()
        if not mandate or mandate.get("state") in FINISHED_STATES:
            continue
        # The CURRENT tokenId, not the original: a mandate rolls forward onto the position
        # each fire mints, and that new one is just as taken as the one it started with.
        if int(mandate.get("tokenId", -1)) != token_id:
            continue
        theirs = mandate.get("immutable") or {}
        differs = [name for name in POLICY_FIELDS
                   if canonical_json(theirs.get(name)) != canonical_json(immutable.get(name))]
        return {
            "id": other,
            "state": mandate.get("state"),
            "sameTerms": not differs,
            "differs": differs,
            "milestonesFired": int(mandate.get("milestonesFired") or 0),
            "label": theirs.get("label") or "",
        }
    return None


def pending_remint_mandates(root, chain_key: str, signer: str, pool_id: str) -> list[str]:
    """Mandates halted because a landed fire's replacement position was never identified.

    The one duplicate :func:`find_position_conflict` structurally cannot catch: such a
    mandate still names the tokenId that was burned, so arming the position it actually
    minted collides with nothing. Arming it would restart the milestone schedule against a
    remainder, which is the mistake the halt note warns about — hence a warning here, not a
    refusal, since a second position in the same pool is perfectly legitimate.
    """
    root = Path(root)
    found = []
    for other in MandateStore.list_ids(root, str(chain_key)):
        mandate = MandateStore(root, str(chain_key), other).load()
        if not mandate or mandate.get("state") != STATE_NEEDS_ATTENTION:
            continue
        imm = mandate.get("immutable") or {}
        if str(imm.get("signer") or "").lower() != str(signer).lower():
            continue
        if str(imm.get("poolId") or "").lower() != str(pool_id).lower():
            continue
        history = mandate.get("history") or []
        if history and history[-1].get("newTokenId") is None:
            found.append(other)
    return sorted(found)


def cmd_arm(client, chain: dict, args: dict, signer: dict) -> None:
    mandate = build_mandate(client, chain, args, signer)
    imm = mandate["immutable"]
    ident = mandate_id(imm)

    # Before the table, not after --confirm: a duplicate is not something the operator should
    # find out about only once they have read a screenful and typed the hash back.
    conflict = find_position_conflict(state_root(), chain["key"], ident, imm)
    if conflict is not None:
        if not conflict["sameTerms"]:
            raise RuntimeError(
                f"position #{imm['originalTokenId']} is already armed as {conflict['id']} "
                f"[{conflict['state']}], and this arm differs in: "
                f"{', '.join(conflict['differs'])}. Both mandates would burn the same NFT. "
                f"`disarm --id {conflict['id']}` first if these are the terms you want."
            )
        label = f"  ({conflict['label']})" if conflict["label"] else ""
        print(f"\n  Position #{imm['originalTokenId']} is already armed as "
              f"{conflict['id']}{label}  [{conflict['state']}, "
              f"{conflict['milestonesFired']} of {len(imm['stepsBps'])} milestones fired].")
        print("  Identical terms — nothing created, nothing changed. Re-running the same "
              "setup is not an error.")
        print(f"  `status --id {conflict['id']}` to inspect it, "
              f"`disarm --id {conflict['id']}` to replace it.")
        return

    pool = lp_write.load_pool_state(client, chain, imm["poolId"], imm["poolKey"])
    info0 = lp_write.token_info(client, chain, imm["poolKey"]["currency0"])
    info1 = lp_write.token_info(client, chain, imm["poolKey"]["currency1"])
    principal_info, harvest_info = _principal_info(mandate, info0, info1)
    principal = imm["principal"]
    original = _big(imm["originalPrincipalRaw"])

    known = {k.lower() for k in (chain.get("knownQuotes") or [])}
    if principal_info["address"].lower() in known:
        intent = f"limit BUY {harvest_info['symbol']} with {principal_info['symbol']}"
    elif harvest_info["address"].lower() in known:
        intent = f"limit SELL {principal_info['symbol']} for {harvest_info['symbol']}"
    else:
        intent = (f"selling {principal_info['symbol']} for {harvest_info['symbol']} "
                  "(neither side is a known quote — the label is a guess, the geometry is not)")

    rows = [
        ["mandate", ident + (f"  ({imm['label']})" if imm["label"] else "")],
        ["signer", signer["address"] + ("  (plan-only, --from)" if signer["simulateOnly"]
                                        else "")],
        ["position", f"#{imm['originalTokenId']}  ticks {imm['originalTickLower']} → "
                     f"{imm['originalTickUpper']}"],
        ["pool", f"{short_id(imm['poolId'])}  {info0['symbol']}/{info1['symbol']}"],
        ["hooks", "none" if imm["poolKey"]["hooks"] == lp_write.NATIVE
                  else f"{imm['poolKey']['hooks']} "
                       f"{format_hook_flags(imm['poolKey']['hooks'])}"],
        ["reading", intent],
        ["token0", f"{info0['symbol']}  {info0['address']}"],
        ["token1", f"{info1['symbol']}  {info1['address']}"],
        ["principal", f"{principal} = {principal_info['symbol']}  "
                      "(the side that is redeployed each fire)"],
        ["harvested", f"{harvest_info['symbol']}  (stays in the wallet as realized profit)"],
        ["far edge", f"tick {imm['farEdgeTick']}  "
                     f"({'tickUpper' if principal == CURRENCY0 else 'tickLower'})"],
        ["fills as tick", "RISES" if fills_as_tick_rises(principal) else "FALLS"],
        ["current tick", str(pool["tick"])],
        ["original principal", f"{fmt_units(original, principal_info['decimals'])} "
                               f"{principal_info['symbol']}"],
    ]

    for index, (step, threshold) in enumerate(zip(imm["stepsBps"], mandate["thresholds"])):
        target = _big(threshold)
        trigger = tick_for_remaining(mandate["tickLower"], mandate["tickUpper"],
                                     _big(mandate["liquidity"]), target, principal)
        note = "exit only, no re-arm" if index == len(imm["stepsBps"]) - 1 else "re-arm"
        rows.append([
            f"milestone {step / 100:g}%",
            f"fires when ≤ {fmt_units(target, principal_info['decimals'])} "
            f"{principal_info['symbol']} is left  →  tick {trigger}  "
            f"(price {raw_price_at_tick(trigger):.6g})  [{note}]",
        ])

    bounds = mandate["bounds"]
    rows.extend([
        ["slippage", f"{bounds['maxSlippageBps']} bps"],
        ["max tick drift", str(bounds["maxTickDrift"])],
        ["hooked pool", "allowed" if bounds["allowHooked"] else "refused"],
        ["max principal / fire", "unlimited" if not bounds["maxPrincipalRawPerFire"]
         else f"{fmt_units(_big(bounds['maxPrincipalRawPerFire']), principal_info['decimals'])}"
              f" {principal_info['symbol']}"],
        ["max fee per gas", bounds["maxFeePerGasWei"] or "unlimited"],
        ["expires", "never" if not bounds["expiresAt"]
         else time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(bounds["expiresAt"]))],
        ["state dir", str(state_root() / "ratchet" / chain["key"])],
    ])

    print(heading(f"ratchet mandate on {chain['name']}"))
    print(render_kv(rows))
    print("\n  Once armed, `tick --broadcast` may send these transactions with nobody "
          "watching.")
    print("  It is bound to this exact tokenId, pool, hook and signer, and to the "
          "milestones above.")

    stranded = pending_remint_mandates(state_root(), chain["key"], signer["address"],
                                       imm["poolId"])
    if stranded:
        print(f"\n  WARNING: mandate(s) {', '.join(stranded)} landed a fire in this pool but "
              "never identified the position it minted.")
        print(f"  If #{imm['originalTokenId']} is that position, arming it here restarts the "
              "milestone schedule against what is left of the principal.")
        print("  Hand it back instead:  clear-attention --id <m> --token-id "
              f"{imm['originalTokenId']}")

    print(f"\n  MANDATE_HASH: {ident}")

    if args.get("confirm") != ident:
        if args.get("confirm"):
            raise RuntimeError(
                f'--confirm mismatch: got "{args["confirm"]}", this mandate is "{ident}". '
                "The parameters changed since it was shown; re-read the table above."
            )
        print("\n  DRY RUN — nothing armed.")
        print(f"  To arm, re-run the identical command with:  --confirm {ident}")
        return
    if signer["simulateOnly"]:
        raise RuntimeError("--from is plan-only; drop it and set the signing key to arm")

    store = MandateStore(state_root(), chain["key"], ident)
    with store.lock() as acquired:
        if not acquired:
            raise RuntimeError(f"another process holds mandate {ident}")
        if store.exists():
            raise RuntimeError(
                f"mandate {ident} already exists — `status --id {ident}` to inspect it, "
                f"`disarm --id {ident}` to replace it"
            )
        store.append({"event": "armed", "state": STATE_ARMED,
                      "tokenId": mandate["tokenId"],
                      "thresholds": mandate["thresholds"]})
        mandate["lastSeq"] = store.last_seq()
        store.save(mandate)
    print("\n  ARMED. Wire it to cron with:  ratchet.py tick --all --broadcast --json")


# ---------------------------------------------------------------------------
# Planning a fire
# ---------------------------------------------------------------------------


def plan_fire(client, chain: dict, mandate: dict, milestone_index: int) -> dict:
    """Everything the fire needs, computed from fresh chain state.

    Returns a dict with the plan, the amounts, and the re-arm decision. Raises on anything
    that means the mandate should stop rather than send.
    """
    imm = mandate["immutable"]
    principal = imm["principal"]
    bps = int(mandate["bounds"]["maxSlippageBps"])

    position = lp_write.load_position_for_write(client, chain, mandate["tokenId"])
    if position["poolId"] != imm["poolId"]:
        raise RuntimeError(
            f"position #{mandate['tokenId']} now reports pool {position['poolId']}, "
            f"the mandate is pinned to {imm['poolId']}"
        )
    lp_write.hook_gate(position["poolKey"], {"allow-hooked": mandate["bounds"]["allowHooked"]})
    if position["liquidity"] <= 0:
        raise RuntimeError("position holds no liquidity")

    pool = lp_write.load_pool_state(client, chain, imm["poolId"], position["poolKey"])
    sqrt_lower = get_sqrt_ratio_at_tick(position["tickLower"])
    sqrt_upper = get_sqrt_ratio_at_tick(position["tickUpper"])
    exiting = get_amounts_for_liquidity(pool["sqrtPriceX96"], sqrt_lower, sqrt_upper,
                                        position["liquidity"])
    amount0_min = lp_write.with_slippage_down(exiting["amount0"], bps)
    amount1_min = lp_write.with_slippage_down(exiting["amount1"], bps)

    remainder = exiting["amount0"] if principal == CURRENCY0 else exiting["amount1"]
    cap = mandate["bounds"].get("maxPrincipalRawPerFire")
    if cap and remainder > _big(cap):
        raise RuntimeError(
            f"the fire would redeploy {remainder} of the principal, over the "
            f"{_big(cap)} per-fire bound"
        )

    final = milestone_index >= len(imm["stepsBps"]) - 1
    remint = None
    new_lower = new_upper = None
    required = {"amount0": 0, "amount1": 0}

    if not final:
        try:
            new_lower, new_upper = rearm_range(pool["tick"], imm["farEdgeTick"],
                                               position["poolKey"]["tickSpacing"], principal)
            # Haircut first, size second. The mint is funded by the delta the decrease just
            # credited inside the same unlock, so redeploying the full remainder would put
            # the net delta one rounding step underwater and TAKE_PAIR would revert.
            budget = lp_write.with_slippage_down(remainder, bps)
            sqrt_new_lower = get_sqrt_ratio_at_tick(new_lower)
            sqrt_new_upper = get_sqrt_ratio_at_tick(new_upper)
            liquidity_new = (
                get_liquidity_for_amount0(sqrt_new_lower, sqrt_new_upper, budget)
                if principal == CURRENCY0
                else get_liquidity_for_amount1(sqrt_new_lower, sqrt_new_upper, budget)
            )
            if liquidity_new <= 0:
                raise RangeExhaustedError(
                    "the remainder is too small to make a position in the re-armed range"
                )
            required = get_amounts_for_liquidity(pool["sqrtPriceX96"], sqrt_new_lower,
                                                 sqrt_new_upper, liquidity_new, True)
            other = required["amount1"] if principal == CURRENCY0 else required["amount0"]
            if other != 0:
                raise RuntimeError(
                    f"re-armed range {new_lower} → {new_upper} is not one-sided at tick "
                    f"{pool['tick']}: it would also need {other} of the harvested currency. "
                    "Refusing to send."
                )
            need = required["amount0"] if principal == CURRENCY0 else required["amount1"]
            if need > remainder:
                raise RuntimeError(
                    f"re-mint would need {need} but the exit only frees {remainder}"
                )
            remint = {
                "tickLower": new_lower,
                "tickUpper": new_upper,
                "liquidity": liquidity_new,
                "amount0Max": (lp_write.with_slippage_up(required["amount0"], bps)
                               if required["amount0"] else 0),
                "amount1Max": (lp_write.with_slippage_up(required["amount1"], bps)
                               if required["amount1"] else 0),
            }
        except RangeExhaustedError:
            # The price ran past the far edge, or what is left cannot fill a tickSpacing.
            # Either way there is nothing to re-arm, so this fire is the last one.
            final = True
            remint = None

    plan = build_ratchet_plan(
        position["poolKey"], mandate["tokenId"], position["liquidity"],
        amount0_min, amount1_min, imm["signer"], remint,
    )

    return {
        "position": position,
        "pool": pool,
        "plan": plan,
        "exiting": exiting,
        "required": required,
        "remainder": remainder,
        "amount0Min": amount0_min,
        "amount1Min": amount1_min,
        "remint": remint,
        "final": final,
        "newLower": new_lower,
        "newUpper": new_upper,
    }


def hash_fields(chain: dict, mandate: dict, fire: dict, milestone_index: int,
                deadline_secs: int) -> dict:
    remint = fire["remint"]
    return {
        "chainId": chain["chainId"],
        "to": chain["positionManager"],
        "cmd": "ratchet",
        "mandateId": mandate_id(mandate["immutable"]),
        "milestone": int(milestone_index),
        "tokenId": lp_write.Big(mandate["tokenId"]),
        "liquidity": lp_write.Big(fire["position"]["liquidity"]),
        "amount0Min": lp_write.Big(fire["amount0Min"]),
        "amount1Min": lp_write.Big(fire["amount1Min"]),
        "remintTickLower": remint["tickLower"] if remint else None,
        "remintTickUpper": remint["tickUpper"] if remint else None,
        "remintLiquidity": lp_write.Big(remint["liquidity"]) if remint else None,
        "remintAmount0Max": lp_write.Big(remint["amount0Max"]) if remint else None,
        "remintAmount1Max": lp_write.Big(remint["amount1Max"]) if remint else None,
        "recipient": mandate["immutable"]["signer"],
        "deadlineSecs": int(deadline_secs),
        "signer": mandate["immutable"]["signer"],
    }


def make_predicate(chain: dict, mandate: dict, milestone_index: int):
    """The bounds check that stands in for a human echoing back the PLAN_HASH.

    It runs at the gate inside ``run_plan``, after the simulation and before the send, and
    checks ``ctx["hashFields"]`` — the same dict the hash was computed from. Selftest tier 6
    pins that this dict covers every calldata-affecting parameter, which makes checking the
    fields strictly stronger than comparing the digest string.
    """
    imm = mandate["immutable"]
    bounds = mandate["bounds"]

    def check(client, chain_arg, args, signer, ctx, hash_value):
        fields = ctx["hashFields"]

        def want(key, expected):
            if fields.get(key) != expected:
                raise RuntimeError(
                    f"mandate refuses the plan: {key} is {fields.get(key)!r}, "
                    f"expected {expected!r}"
                )

        want("cmd", "ratchet")
        want("chainId", imm["chainId"])
        want("to", imm["positionManager"])
        want("mandateId", mandate_id(imm))
        want("milestone", int(milestone_index))
        want("recipient", imm["signer"])
        want("signer", imm["signer"])
        if int(fields["tokenId"]) != int(mandate["tokenId"]):
            raise RuntimeError("mandate refuses the plan: tokenId is not the pinned position")
        if signer["address"].lower() != imm["signer"].lower():
            raise RuntimeError(
                f"mandate refuses the plan: signing key derives {signer['address']}, the "
                f"mandate was armed by {imm['signer']}"
            )
        if signer["simulateOnly"]:
            raise RuntimeError("mandate refuses the plan: plan-only signer cannot broadcast")
        if int(fields["deadlineSecs"]) > int(bounds["maxDeadlineSecs"]):
            raise RuntimeError("mandate refuses the plan: deadline is longer than approved")
        if milestone_index < int(mandate["milestonesFired"]):
            raise RuntimeError(
                f"mandate refuses the plan: milestone {milestone_index} already fired"
            )
        if bounds.get("expiresAt") and _now() > int(bounds["expiresAt"]):
            raise RuntimeError("mandate refuses the plan: the mandate has expired")

        # Re-read the chain rather than trusting the planner that ran moments ago.
        position = lp_write.load_position_for_write(client, chain_arg, mandate["tokenId"])
        if position["poolId"] != imm["poolId"]:
            raise RuntimeError("mandate refuses the plan: the position changed pool")
        if compute_pool_id(position["poolKey"]) != imm["poolId"]:
            raise RuntimeError("mandate refuses the plan: PoolKey no longer hashes to poolId")
        lp_write.hook_gate(position["poolKey"], {"allow-hooked": bounds["allowHooked"]})
        if int(fields["liquidity"]) != int(position["liquidity"]):
            raise RuntimeError(
                "mandate refuses the plan: it does not remove the position's full liquidity"
            )

        pool = lp_write.load_pool_state(client, chain_arg, imm["poolId"], position["poolKey"])
        exiting = get_amounts_for_liquidity(
            pool["sqrtPriceX96"], get_sqrt_ratio_at_tick(position["tickLower"]),
            get_sqrt_ratio_at_tick(position["tickUpper"]), position["liquidity"],
        )
        floor0 = lp_write.with_slippage_down(exiting["amount0"], bounds["maxSlippageBps"])
        floor1 = lp_write.with_slippage_down(exiting["amount1"], bounds["maxSlippageBps"])
        if int(fields["amount0Min"]) < floor0 or int(fields["amount1Min"]) < floor1:
            raise RuntimeError(
                "mandate refuses the plan: slippage floors are looser than approved"
            )

        if fields["remintTickLower"] is None:
            return  # exit-only fire; nothing further to bind

        principal = imm["principal"]
        # The far edge never moves; only the near edge tracks the price.
        if principal == CURRENCY0:
            want("remintTickUpper", imm["farEdgeTick"])
        else:
            want("remintTickLower", imm["farEdgeTick"])

        fresh_lower, fresh_upper = rearm_range(
            pool["tick"], imm["farEdgeTick"], position["poolKey"]["tickSpacing"], principal
        )
        drift = int(bounds["maxTickDrift"])
        near_planned = (int(fields["remintTickLower"]) if principal == CURRENCY0
                        else int(fields["remintTickUpper"]))
        near_fresh = fresh_lower if principal == CURRENCY0 else fresh_upper
        if abs(near_fresh - near_planned) > drift:
            raise RuntimeError(
                f"mandate refuses the plan: the pool moved, re-arm edge would now be "
                f"{near_fresh} not {near_planned} (drift > {drift})"
            )
        # The direction that actually breaks the add: for a currency0 re-arm the price
        # rising past the planned lower tick turns the range two-sided and the mint reverts.
        # Drift the other way is harmless, so this is asserted one-sided rather than by
        # tightening maxTickDrift, which would abort on the harmless direction too.
        if principal == CURRENCY0 and pool["tick"] >= near_planned:
            raise RuntimeError(
                f"mandate refuses the plan: tick {pool['tick']} has reached the re-arm "
                f"lower edge {near_planned}; the range would not be one-sided"
            )
        if principal != CURRENCY0 and pool["tick"] < near_planned:
            raise RuntimeError(
                f"mandate refuses the plan: tick {pool['tick']} is below the re-arm "
                f"upper edge {near_planned}; the range would not be one-sided"
            )

        # The cheapest possible proof that the re-mint really is one-sided.
        other_max = (fields["remintAmount1Max"] if principal == CURRENCY0
                     else fields["remintAmount0Max"])
        if int(other_max) != 0:
            raise RuntimeError(
                "mandate refuses the plan: the re-mint would spend the harvested currency"
            )

        # Everything above binds the *shape* of the re-mint. This binds its size, which is
        # the invariant the whole design rests on: the mint is funded entirely by the delta
        # the decrease just credited inside the same unlock, and that is what lets the plan
        # omit a SETTLE leg. Recomputed here from the liquidity actually in the calldata
        # rather than taken from the planner, because reproducing the planner's arithmetic
        # is precisely what a gate is for.
        remint_liquidity = int(fields["remintLiquidity"])
        if remint_liquidity <= 0:
            raise RuntimeError("mandate refuses the plan: the re-mint carries no liquidity")
        need = get_amounts_for_liquidity(
            pool["sqrtPriceX96"],
            get_sqrt_ratio_at_tick(int(fields["remintTickLower"])),
            get_sqrt_ratio_at_tick(int(fields["remintTickUpper"])),
            remint_liquidity, True,
        )
        need_principal = need["amount0"] if principal == CURRENCY0 else need["amount1"]
        need_other = need["amount1"] if principal == CURRENCY0 else need["amount0"]
        free_principal = (exiting["amount0"] if principal == CURRENCY0
                          else exiting["amount1"])
        if need_other != 0:
            # Unreachable while the directional assertion above holds — a range that needs
            # the harvested currency is one that contains the current price, which that
            # check already refused. Kept as a tripwire, derived from amounts rather than
            # from a tick comparison, so that loosening the check above fails loudly here
            # instead of quietly letting a two-sided mint through. No test can isolate it
            # without first disabling its neighbour.
            raise RuntimeError(
                f"mandate refuses the plan: at tick {pool['tick']} that liquidity would "
                f"also need {need_other} of the harvested currency — the range is not "
                "one-sided any more"
            )
        if need_principal > free_principal:
            raise RuntimeError(
                f"mandate refuses the plan: the re-mint needs {need_principal} of the "
                f"principal but the exit only frees {free_principal}"
            )
        principal_max = (fields["remintAmount0Max"] if principal == CURRENCY0
                         else fields["remintAmount1Max"])
        if int(principal_max) > free_principal:
            raise RuntimeError(
                f"mandate refuses the plan: it authorizes spending up to {principal_max} "
                f"on the re-mint, more than the {free_principal} the exit frees"
            )

    return check


# ---------------------------------------------------------------------------
# tick
# ---------------------------------------------------------------------------


def fire_rows(chain: dict, mandate: dict, fire: dict, info0: dict, info1: dict,
              milestone_index: int) -> list:
    imm = mandate["immutable"]
    principal_info, harvest_info = _principal_info(mandate, info0, info1)
    step = imm["stepsBps"][milestone_index] / 100
    return [
        ["mandate", mandate_id(imm) + (f"  ({imm['label']})" if imm["label"] else "")],
        ["milestone", f"{step:g}%  ({milestone_index + 1} of {len(imm['stepsBps'])})"],
        ["position", f"#{mandate['tokenId']}  ticks {fire['position']['tickLower']} → "
                     f"{fire['position']['tickUpper']}"],
        ["current tick", str(fire["pool"]["tick"])],
        ["exiting", f"{fmt_units(fire['exiting']['amount0'], info0['decimals'])} "
                    f"{info0['symbol']} + "
                    f"{fmt_units(fire['exiting']['amount1'], info1['decimals'])} "
                    f"{info1['symbol']} + fees"],
        ["principal left", f"{fmt_units(fire['remainder'], principal_info['decimals'])} "
                           f"{principal_info['symbol']}"],
        ["re-arm", "none — final milestone, the position is closed for good"
                   if fire["final"] else
                   f"{fmt_units(fire['required']['amount0'] or fire['required']['amount1'], principal_info['decimals'])} "  # noqa: E501
                   f"{principal_info['symbol']} into ticks {fire['newLower']} → "
                   f"{fire['newUpper']}"],
        ["harvested to wallet", f"{harvest_info['symbol']} (plus fees in both currencies)"],
        ["recipient", imm["signer"]],
        ["actions", describe_actions(fire["plan"]["actions"])],
        ["authorized by", "mandate (unattended) — no PLAN_HASH echo"],
    ]


def run_fire(client, chain: dict, args: dict, signer: dict, store: MandateStore,
             mandate: dict, milestone_index: int, broadcast: bool) -> dict:
    """Plan, journal, and (when broadcasting) send one milestone's transaction."""
    imm = mandate["immutable"]
    fire = plan_fire(client, chain, mandate, milestone_index)
    info0 = lp_write.token_info(client, chain, imm["poolKey"]["currency0"])
    info1 = lp_write.token_info(client, chain, imm["poolKey"]["currency1"])

    deadline_secs = int(mandate["bounds"]["maxDeadlineSecs"])
    block = client.get_block()
    deadline = int(block["timestamp"], 16) + deadline_secs
    fields = hash_fields(chain, mandate, fire, milestone_index, deadline_secs)
    plan_hash_value = lp_write.plan_hash(fields)
    fire_id = f"{mandate_id(imm)}:{milestone_index}:{mandate.get('attempt', 0)}"

    principal_info, harvest_info = _principal_info(mandate, info0, info1)
    principal_delta = fire["remainder"] - (
        fire["required"]["amount0"] if imm["principal"] == CURRENCY0
        else fire["required"]["amount1"]
    )
    harvested = (fire["exiting"]["amount1"] if imm["principal"] == CURRENCY0
                 else fire["exiting"]["amount0"])

    store.append({
        "event": "plan.built", "fireId": fire_id, "milestone": milestone_index,
        "planHash": plan_hash_value, "tick": fire["pool"]["tick"],
        "remainderRaw": str(fire["remainder"]), "final": fire["final"],
        "remintTickLower": fire["newLower"], "remintTickUpper": fire["newUpper"],
    })

    write_args = dict(args)
    write_args["deadline-secs"] = deadline_secs
    write_args["broadcast"] = bool(broadcast)
    write_args.pop("confirm", None)
    if mandate["bounds"].get("maxFeePerGasWei"):
        write_args["max-fee-per-gas"] = mandate["bounds"]["maxFeePerGasWei"]

    sent_record: dict = {}

    def on_sent(info: dict) -> None:
        # Durability point: the hash and nonce reach the disk before the broadcast leaves
        # this process. A crash here is recoverable; a broadcast with nothing written is not.
        sent_record.update(info)
        current = {
            "fireId": fire_id, "milestone": milestone_index,
            "attempt": int(mandate.get("attempt", 0)),
            "planHash": plan_hash_value, "final": fire["final"],
            "plannedTick": fire["pool"]["tick"],
            "plannedRemainderRaw": str(fire["remainder"]),
            "remintTickLower": fire["newLower"], "remintTickUpper": fire["newUpper"],
            "tx": {k: (str(v) if isinstance(v, int) and abs(v) > 2**53 else v)
                   for k, v in info.items()},
        }
        mandate["state"] = STATE_FIRE_SENT
        mandate["currentFire"] = current
        # The whole fire record goes into the log line, not just the hash. This record has
        # to be able to rebuild `currentFire` on its own: if the crash lands between here
        # and the save below, it is the only surviving evidence that anything was signed,
        # and `replay_tail` has nothing else to work from.
        mandate["lastSeq"] = store.append({
            "event": "tx.sent", "fireId": fire_id, "from": STATE_ARMED,
            "to": STATE_FIRE_SENT, "currentFire": current,
            "milestonesFired": int(mandate["milestonesFired"]), **info,
        })
        store.save(mandate)

    ctx = {
        "title": f"ratchet fire — milestone {imm['stepsBps'][milestone_index] / 100:g}% "
                 f"on position #{mandate['tokenId']}",
        "tokenMap": _token_map(info0, info1),
        "rows": fire_rows(chain, mandate, fire, info0, info1, milestone_index),
        "hashFields": fields,
        "data": lp_write.encode_call(fire["plan"], deadline),
        "value": fire["plan"]["value"],
        "expected": {
            principal_info["address"].lower(): {"amount": principal_delta, "mode": "atLeast"},
            harvest_info["address"].lower(): {"amount": harvested, "mode": "atLeast"},
        },
        "rebuildData": lambda d: lp_write.encode_call(fire["plan"], d),
        "onSent": on_sent,
    }

    authorization = None
    if broadcast:
        authorization = lp_write.MandateAuthorization(
            mandate_id=mandate_id(imm), fire_id=fire_id,
            predicate=make_predicate(chain, mandate, milestone_index),
        )

    receipt = lp_write.run_plan(client, chain, write_args, signer, ctx,
                                authorization=authorization)
    return {"fire": fire, "receipt": receipt, "fireId": fire_id,
            "planHash": plan_hash_value, "sent": sent_record,
            "info0": info0, "info1": info1}


def adopt_landed_fire(client, chain: dict, store: MandateStore, mandate: dict,
                      receipt: dict | None, new_token_id: int | None = None) -> str:
    """Move a mandate forward after its fire is known to be on chain.

    ``new_token_id`` is the escape hatch for the one case the oracles cannot close: the fire
    landed, but neither the receipt nor the log scan could name the replacement position.
    ``clear-attention --token-id`` supplies it, having validated it first — that path is
    attended, so a human is the oracle. Everything downstream of the identification is the
    same code either way, which is the point of threading it through here rather than
    reimplementing the accounting in the command.
    """
    imm = mandate["immutable"]
    current = mandate.get("currentFire") or {}
    final = bool(current.get("final"))
    old_token_id = int(mandate["tokenId"])

    if not final and new_token_id is None:
        if receipt:
            minted = net_transfers(receipt.get("logs") or [], imm["signer"])["mintedNfts"]
            candidates = [int(n["id"]) for n in minted if int(n["id"]) != old_token_id]
            if len(candidates) == 1:
                new_token_id = candidates[0]
        if new_token_id is None:
            # Oracle C. Deliberately gives up rather than guessing: a duplicate mint would
            # deploy the remainder twice, which no amount of convenience is worth.
            sent = current.get("tx") or {}
            from_block = sent.get("sentAtBlock")
            if from_block is not None:
                new_token_id = find_minted_token_id(
                    client, chain, imm["signer"], int(from_block), exclude={old_token_id}
                )
        if new_token_id is None:
            mandate["state"] = STATE_NEEDS_ATTENTION
            mandate["note"] = (
                "the fire landed but the replacement position could not be identified. "
                "Find the new tokenId with `lp_read.py positions --owner <signer>`, then "
                "`clear-attention --id <m> --token-id <new>` to hand it back. Do not arm a "
                "fresh mandate instead: arm would treat the remainder as a new original "
                "principal and rebase every milestone still to come."
            )
            mandate["lastSeq"] = store.append({
                "event": "needs_attention", "reason": "remint tokenId unknown",
                "fireId": current.get("fireId"),
            })
            store.save(mandate)
            return STATE_NEEDS_ATTENTION

    fired_through = int(current.get("milestone", mandate["milestonesFired"]))
    mandate["milestonesFired"] = fired_through + 1
    mandate["attempt"] = 0  # a landed fire clears the retry budget for the next milestone
    mandate["history"] = (mandate.get("history") or []) + [{
        "fireId": current.get("fireId"),
        "milestone": current.get("milestone"),
        "planHash": current.get("planHash"),
        "txHash": (current.get("tx") or {}).get("hash"),
        "oldTokenId": old_token_id,
        "newTokenId": new_token_id,
        "at": _now(),
    }]
    mandate["currentFire"] = None

    if final or new_token_id is None:
        mandate["state"] = STATE_COMPLETE
        mandate["tokenId"] = old_token_id
        mandate["liquidity"] = "0"
        mandate["lastSeq"] = store.append({
            "event": "complete", "fireId": current.get("fireId"),
            "milestonesFired": mandate["milestonesFired"],
        })
        store.save(mandate)
        return STATE_COMPLETE

    position = lp_write.load_position_for_write(client, chain, new_token_id)
    mandate["tokenId"] = new_token_id
    mandate["tickLower"] = position["tickLower"]
    mandate["tickUpper"] = position["tickUpper"]
    mandate["liquidity"] = str(position["liquidity"])
    mandate["state"] = STATE_ARMED
    mandate["lastSeq"] = store.append({
        "event": "reconcile.adopted", "fireId": current.get("fireId"),
        "newTokenId": new_token_id, "ticks": [position["tickLower"], position["tickUpper"]],
        "liquidity": str(position["liquidity"]), "to": STATE_ARMED,
    })
    store.save(mandate)
    return STATE_ARMED


def tick_one(client, chain: dict, args: dict, signer: dict, ident: str,
             broadcast: bool) -> dict:
    """One mandate, one tick. Never raises for an expected condition — reports it."""
    store = MandateStore(state_root(), chain["key"], ident)
    result = {"mandateId": ident}

    with store.lock() as acquired:
        if not acquired:
            # Overlapping cron ticks are normal during a fire that is waiting on a receipt.
            return {**result, "action": "skipped", "reason": "another tick holds the lock"}

        mandate = store.load()
        if mandate is None:
            return {**result, "action": "missing", "reason": "no such mandate"}

        # The log is the write-ahead record; the mandate file is the view. If the view is
        # behind, a crash landed between the outcome record and the replace.
        behind = store.tail(int(mandate.get("lastSeq") or 0))
        if behind:
            result["replayed"] = [r.get("event") for r in behind]
            mandate = replay_tail(mandate, behind)
            mandate["lastSeq"] = store.last_seq()
            store.save(mandate)

        state = mandate.get("state")
        result["state"] = state
        if state in TERMINAL_STATES:
            return {**result, "action": "noop", "reason": f"state is {state}",
                    "note": mandate.get("note")}

        expires_at = mandate["bounds"].get("expiresAt")
        if expires_at and _now() > int(expires_at) and state == STATE_ARMED:
            mandate["state"] = STATE_EXPIRED
            mandate["lastSeq"] = store.append({"event": "expired"})
            store.save(mandate)
            return {**result, "action": "expired", "state": STATE_EXPIRED}

        # --- reconcile an in-flight fire ---------------------------------
        if state == STATE_FIRE_SENT:
            current = mandate.get("currentFire") or {}
            verdict = reconcile(client, chain, mandate["immutable"]["signer"],
                                int(mandate["tokenId"]), current.get("tx"))
            result["truth"] = verdict["truth"]
            result["reason"] = verdict["reason"]

            if verdict["truth"] == PENDING:
                return {**result, "action": "waiting"}
            if verdict["truth"] == UNAVAILABLE:
                # The node did not answer, so nothing was learned and nothing changes. Left
                # deliberately as a plain retry rather than NEEDS_ATTENTION: a five-minute
                # cron will meet a flaky RPC sooner or later, and halting on one would mean
                # every blip costs a manual `clear-attention`.
                store.append({"event": "tick.noop", "reason": verdict["reason"]})
                return {**result, "action": "deferred"}
            if verdict["truth"] == AMBIGUOUS:
                mandate["state"] = STATE_NEEDS_ATTENTION
                mandate["note"] = verdict["reason"]
                mandate["lastSeq"] = store.append({"event": "needs_attention",
                                                   "reason": verdict["reason"]})
                store.save(mandate)
                return {**result, "action": "halted", "state": STATE_NEEDS_ATTENTION}
            if verdict["truth"] == LANDED:
                receipt = None
                tx_hash = (current.get("tx") or {}).get("hash")
                if tx_hash:
                    # _send raises before returning the receipt when a transaction reverts,
                    # so re-fetch it by the hash the journal already holds.
                    receipt = client.get_receipt(tx_hash)
                new_state = adopt_landed_fire(client, chain, store, mandate, receipt)
                result["state"] = new_state
                # Kept as its own field, not as `action`: the tick carries on to re-evaluate
                # and would otherwise overwrite this with "noop", hiding a completed fire
                # from whoever reads the cron summary.
                result["adopted"] = True
                result["action"] = "adopted"
                result["tokenId"] = mandate["tokenId"]
                if new_state != STATE_ARMED:
                    return result
                state = STATE_ARMED
            elif verdict["truth"] == NOT_SENT:
                # The counter lives on the mandate, not on currentFire: currentFire is
                # cleared on every retry, so counting there would reset to zero each round
                # and maxAttempts would never be reached.
                attempt = int(mandate.get("attempt", 0)) + 1
                if attempt > int(mandate["bounds"]["maxAttempts"]):
                    mandate["state"] = STATE_NEEDS_ATTENTION
                    mandate["note"] = (f"{attempt - 1} attempts all failed to land: "
                                       f"{verdict['reason']}")
                    mandate["lastSeq"] = store.append({"event": "needs_attention",
                                                       "reason": mandate["note"]})
                    store.save(mandate)
                    return {**result, "action": "halted", "state": STATE_NEEDS_ATTENTION}
                mandate["state"] = STATE_ARMED
                mandate["attempt"] = attempt
                mandate["currentFire"] = None
                mandate["lastSeq"] = store.append({
                    "event": "tx.dropped", "reason": verdict["reason"], "attempt": attempt,
                })
                store.save(mandate)
                result["attempt"] = attempt
                # Report where the mandate ended up, not where it started; `result["state"]`
                # was seeded from the entry state and the tick carries on from here.
                result["state"] = STATE_ARMED
                state = STATE_ARMED

        # --- is a milestone due? -----------------------------------------
        imm = mandate["immutable"]
        try:
            position = lp_write.load_position_for_write(client, chain,
                                                        int(mandate["tokenId"]))
        except RuntimeError as exc:
            mandate["state"] = STATE_NEEDS_ATTENTION
            mandate["note"] = str(exc)
            mandate["lastSeq"] = store.append({"event": "needs_attention",
                                               "reason": str(exc)})
            store.save(mandate)
            return {**result, "action": "halted", "state": STATE_NEEDS_ATTENTION,
                    "reason": str(exc)}

        pool = lp_write.load_pool_state(client, chain, imm["poolId"], position["poolKey"])
        remaining = remaining_principal(pool["sqrtPriceX96"], position["tickLower"],
                                        position["tickUpper"], position["liquidity"],
                                        imm["principal"])
        thresholds = [_big(t) for t in mandate["thresholds"]]
        due = due_milestone(thresholds, int(mandate["milestonesFired"]), remaining)

        result.update({
            "tick": pool["tick"], "tokenId": mandate["tokenId"],
            "remainingRaw": str(remaining),
            "milestonesFired": mandate["milestonesFired"],
            "nextThresholdRaw": (str(thresholds[mandate["milestonesFired"]])
                                 if mandate["milestonesFired"] < len(thresholds) else None),
        })

        if due is None:
            store.append({"event": "tick.noop", "tick": pool["tick"],
                          "remainingRaw": str(remaining)})
            return {**result, "action": "noop", "reason": "no milestone due"}

        # A gap that clears several levels fires only the deepest; the ones it skipped are
        # satisfied by the same reading, so they are recorded as fired rather than replayed.
        result["milestone"] = due
        result["skipped"] = list(range(int(mandate["milestonesFired"]), due))
        mandate["milestonesFired"] = due
        store.append({"event": "milestone.reached", "milestone": due,
                      "skipped": result["skipped"], "tick": pool["tick"],
                      "remainingRaw": str(remaining)})

        try:
            outcome = run_fire(client, chain, args, signer, store, mandate, due, broadcast)
        except SystemExit as exc:
            # run_plan exits 2 when the simulation reverts, and cmd_* do the same on missing
            # approvals. For a runner those are retryable conditions, not process death.
            reason = f"simulation refused the fire (exit {exc.code})"
            mandate["lastSeq"] = store.append({"event": "plan.rejected", "reason": reason,
                                               "milestone": due})
            store.save(mandate)
            return {**result, "action": "rejected", "reason": reason}
        except RuntimeError as exc:
            mandate["lastSeq"] = store.append({"event": "plan.rejected", "reason": str(exc),
                                               "milestone": due})
            store.save(mandate)
            return {**result, "action": "rejected", "reason": str(exc)}

        result["planHash"] = outcome["planHash"]
        if not broadcast:
            store.append({"event": "tick.dryrun", "milestone": due,
                          "planHash": outcome["planHash"]})
            return {**result, "action": "dry-run"}

        result["txHash"] = (outcome["sent"] or {}).get("hash")
        new_state = adopt_landed_fire(client, chain, store, mandate, outcome["receipt"])
        result["state"] = new_state
        result["tokenId"] = mandate["tokenId"]
        return {**result, "action": "fired"}


def cmd_tick(client, chain: dict, args: dict, signer: dict) -> None:
    broadcast = bool(args.get("broadcast"))
    if args.get("all"):
        idents = MandateStore.list_ids(state_root(), chain["key"])
    else:
        idents = [require_arg(args, "id", "mandate id, or use --all")]

    results = []
    for ident in idents:
        try:
            if args.get("json"):
                # Keep the machine-readable channel clean: the human-facing plan table and
                # simulation trace still go to the log, just not to stdout.
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    outcome = tick_one(client, chain, args, signer, ident, broadcast)
                outcome["output"] = buffer.getvalue()
            else:
                outcome = tick_one(client, chain, args, signer, ident, broadcast)
        except Exception as exc:  # noqa: BLE001 — one bad mandate must not stop the others
            outcome = {"mandateId": ident, "action": "error", "reason": str(exc)}
        results.append(outcome)

    if args.get("json"):
        payload = {"chain": chain["key"], "broadcast": broadcast, "results": results}
        if not args.get("alert-only"):
            _emit(payload)
            return
        notable = [outcome for outcome in results if is_notable(outcome)]
        payload["notable"] = len(notable)
        payload["quiet"] = len(results) - len(notable)
        if notable:
            # Summary first, JSON last: the cron gate reads the final line, and an
            # alert that must be delivered has to end in something that is not one.
            # Pretty-printed JSON ends in "}", which no gate reader mistakes for a gate.
            for outcome in notable:
                print(tick_summary_line(outcome))
            print()
            _emit(payload)
        else:
            _emit(payload)
            # The run record keeps the whole payload for whoever goes looking; this
            # last line is what keeps a tick with no news off the channel.
            print(json.dumps({"wakeAgent": False}))
        return

    if not idents:
        print(f"\nNo ratchet mandates on {chain['name']}.")
        return
    print(heading(f"ratchet tick on {chain['name']}"
                  f"{'' if broadcast else '  (DRY RUN — nothing sent)'}"))
    for outcome in results:
        rows = [
            ["mandate", outcome["mandateId"]],
            ["action", outcome.get("action", "?")],
            ["state", outcome.get("state", "?")],
            ["detail", outcome.get("reason") or outcome.get("note") or ""],
        ]
        if outcome.get("adopted"):
            rows.insert(2, ["adopted", f"a landed fire was reconciled; position is now "
                                       f"#{outcome.get('tokenId')}"])
        if outcome.get("txHash"):
            rows.insert(2, ["tx", outcome["txHash"]])
        print(render_kv(rows))
        if outcome.get("action") in ("halted", "error"):
            print("  ^ needs a human. `status --id <id>` for the full record.")


# ---------------------------------------------------------------------------
# status / list / disarm
# ---------------------------------------------------------------------------


def cmd_list(client, chain: dict, args: dict, signer: dict) -> None:
    idents = MandateStore.list_ids(state_root(), chain["key"])
    rows = []
    for ident in idents:
        store = MandateStore(state_root(), chain["key"], ident)
        try:
            mandate = store.load()
        except RuntimeError as exc:
            rows.append({"mandateId": ident, "state": "UNREADABLE", "note": str(exc)})
            continue
        if mandate is None:
            continue
        imm = mandate["immutable"]
        rows.append({
            "mandateId": ident,
            "label": imm.get("label") or "",
            "state": mandate["state"],
            "tokenId": mandate["tokenId"],
            "principal": imm["principal"],
            "milestonesFired": mandate["milestonesFired"],
            "steps": imm["stepsBps"],
        })

    if args.get("json"):
        _emit({"chain": chain["key"], "mandates": rows})
        return
    if not rows:
        print(f"\nNo ratchet mandates on {chain['name']}.")
        print(f"State dir: {state_root() / 'ratchet' / chain['key']}")
        return
    print(heading(f"ratchet mandates on {chain['name']}"))
    for row in rows:
        print(render_kv([[key, str(value)] for key, value in row.items()]))


def cmd_status(client, chain: dict, args: dict, signer: dict) -> None:
    ident = require_arg(args, "id", "mandate id")
    store = MandateStore(state_root(), chain["key"], ident)
    mandate = store.load()
    if mandate is None:
        raise RuntimeError(f"no mandate {ident} on {chain['name']}")

    if args.get("json"):
        _emit({"mandate": mandate, "log": store.records()})
        return

    imm = mandate["immutable"]
    info0 = lp_write.token_info(client, chain, imm["poolKey"]["currency0"])
    info1 = lp_write.token_info(client, chain, imm["poolKey"]["currency1"])
    principal_info, harvest_info = _principal_info(mandate, info0, info1)

    print(heading(f"ratchet mandate {ident} on {chain['name']}"))
    print(render_kv([
        ["label", imm.get("label") or "(none)"],
        ["state", mandate["state"]],
        ["note", mandate.get("note") or ""],
        ["position", f"#{mandate['tokenId']}  ticks {mandate['tickLower']} → "
                     f"{mandate['tickUpper']}"],
        ["principal", f"{imm['principal']} = {principal_info['symbol']}"],
        ["harvested", harvest_info["symbol"]],
        ["far edge", str(imm["farEdgeTick"])],
        ["original principal",
         f"{fmt_units(_big(imm['originalPrincipalRaw']), principal_info['decimals'])} "
         f"{principal_info['symbol']}"],
        ["milestones", f"{mandate['milestonesFired']} of {len(imm['stepsBps'])} fired"],
        ["thresholds", ", ".join(
            fmt_units(_big(t), principal_info["decimals"]) for t in mandate["thresholds"])],
        ["in flight", json.dumps(mandate.get("currentFire")) if mandate.get("currentFire")
         else "none"],
        ["log", str(store.log_path)],
    ]))
    history = mandate.get("history") or []
    if history:
        print(heading("fires"))
        for entry in history:
            print(render_kv([[key, str(value)] for key, value in entry.items()]))


def cmd_disarm(client, chain: dict, args: dict, signer: dict) -> None:
    ident = require_arg(args, "id", "mandate id")
    store = MandateStore(state_root(), chain["key"], ident)
    with store.lock() as acquired:
        if not acquired:
            raise RuntimeError(f"mandate {ident} is being ticked right now — try again")
        mandate = store.load()
        if mandate is None:
            raise RuntimeError(f"no mandate {ident} on {chain['name']}")
        if mandate["state"] == STATE_FIRE_SENT:
            raise RuntimeError(
                f"mandate {ident} has a transaction in flight. Run `tick --id {ident}` "
                "until it settles, then disarm — disarming now would lose the only record "
                "of that hash."
            )
        mandate["state"] = STATE_DISARMED
        mandate["lastSeq"] = store.append({"event": "disarmed"})
        store.save(mandate)
    print(f"\nMandate {ident} disarmed. It will no longer fire.")
    print(f"Files kept for the record: {store.path}")


def check_replacement(client, chain: dict, mandate: dict, token_id: int) -> dict:
    """Everything that must hold before a mandate is pointed at a different position.

    An operator naming the replacement is standing in for an oracle that could not decide,
    so their answer gets checked the way the oracle's would have been. The mandate's whole
    authorization rests on the geometry being what it was armed against: same pool, same
    signer, same far edge, same side of the price. Any of those wrong and the milestones
    still to come would be measured against something else entirely.
    """
    imm = mandate["immutable"]
    position = lp_write.load_position_for_write(client, chain, int(token_id))
    if position["poolId"] != imm["poolId"] or compute_pool_id(position["poolKey"]) != \
            imm["poolId"]:
        raise RuntimeError(
            f"position #{token_id} is in pool {position['poolId']}, the mandate is pinned "
            f"to {imm['poolId']}"
        )
    if position["owner"] and position["owner"].lower() != imm["signer"].lower():
        raise RuntimeError(
            f"position #{token_id} is owned by {position['owner']}, not the mandate signer "
            f"{imm['signer']}"
        )
    if position["liquidity"] <= 0:
        raise RuntimeError(f"position #{token_id} holds no liquidity")

    pool = lp_write.load_pool_state(client, chain, imm["poolId"], position["poolKey"])
    principal = principal_for_range(pool["tick"], position["tickLower"],
                                    position["tickUpper"])
    if principal != imm["principal"]:
        raise RuntimeError(
            f"position #{token_id} is one-sided in {principal}, but the mandate ratchets "
            f"{imm['principal']} — it sits on the wrong side of the price"
        )
    edge = far_edge_tick(position["tickLower"], position["tickUpper"], principal)
    if edge != int(imm["farEdgeTick"]):
        raise RuntimeError(
            f"position #{token_id} runs to tick {edge}; the mandate's far edge is "
            f"{imm['farEdgeTick']}. A ratchet re-arm never moves that edge, so this is not "
            "the position the fire created."
        )
    return position


def cmd_clear_attention(client, chain: dict, args: dict, signer: dict) -> None:
    ident = require_arg(args, "id", "mandate id")
    replacement = opt_str(args, "token-id")
    store = MandateStore(state_root(), chain["key"], ident)
    with store.lock() as acquired:
        if not acquired:
            raise RuntimeError(f"mandate {ident} is being ticked right now — try again")
        mandate = store.load()
        if mandate is None:
            raise RuntimeError(f"no mandate {ident} on {chain['name']}")
        if mandate["state"] != STATE_NEEDS_ATTENTION:
            raise RuntimeError(f"mandate {ident} is {mandate['state']}, not "
                               f"{STATE_NEEDS_ATTENTION}")

        if replacement is not None:
            # Finishing an adoption the oracles could not. The old position must really be
            # gone — if it is still alive the fire did not land, and adopting would credit a
            # milestone that never fired and abandon a position the mandate still owns.
            new_token_id = int(replacement)
            if new_token_id == int(mandate["tokenId"]):
                raise RuntimeError(
                    "--token-id names the position the mandate already holds; drop the flag "
                    "to simply resume against it"
                )
            truth = position_truth(client, chain, int(mandate["tokenId"]),
                                   mandate["immutable"]["signer"])
            if truth.get("unreachable"):
                raise RuntimeError("could not read the old position from the node — retry")
            if not truth["burned"]:
                raise RuntimeError(
                    f"position #{mandate['tokenId']} is still alive, so no fire landed and "
                    "there is nothing to adopt. Re-run `tick` and let it reconcile."
                )
            check_replacement(client, chain, mandate, new_token_id)
            mandate["note"] = ""
            mandate["attempt"] = 0
            new_state = adopt_landed_fire(client, chain, store, mandate, None,
                                          new_token_id=new_token_id)
            print(f"\nMandate {ident} adopted position #{mandate['tokenId']} "
                  f"and is now {new_state}.")
            return

        position = lp_write.load_position_for_write(client, chain, int(mandate["tokenId"]))
        if position["liquidity"] <= 0:
            raise RuntimeError(
                f"position #{mandate['tokenId']} holds no liquidity; this mandate cannot be "
                "resumed. Disarm it and arm a fresh one against the current position."
            )
        mandate["tickLower"] = position["tickLower"]
        mandate["tickUpper"] = position["tickUpper"]
        mandate["liquidity"] = str(position["liquidity"])
        mandate["state"] = STATE_ARMED
        mandate["currentFire"] = None
        mandate["attempt"] = 0
        mandate["note"] = ""
        mandate["lastSeq"] = store.append({"event": "state.changed", "to": STATE_ARMED,
                                           "reason": "cleared by operator"})
        store.save(mandate)
    print(f"\nMandate {ident} is armed again against position #{mandate['tokenId']}.")


COMMANDS = {
    "arm": cmd_arm,
    "tick": cmd_tick,
    "status": cmd_status,
    "list": cmd_list,
    "disarm": cmd_disarm,
    "clear-attention": cmd_clear_attention,
}


def main() -> None:
    args = parse_args(sys.argv[1:])
    command = args["_"][0] if args["_"] else None
    if not command or args.get("help") or args.get("h"):
        print(USAGE)
        sys.exit(0 if command else 1)
    handler = COMMANDS.get(command)
    if handler is None:
        raise RuntimeError(f'unknown command "{command}"\n{USAGE}')

    chain = resolve_chain(opt_str(args, "chain"))
    client = _client(chain, args)
    signer = lp_write.resolve_signer(args)
    handler(client, chain, args, signer)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        die(exc)
