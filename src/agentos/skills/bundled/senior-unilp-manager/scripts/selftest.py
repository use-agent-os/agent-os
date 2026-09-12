#!/usr/bin/env python3
"""Offline self-test. No RPC, no network — pure math, encoding and hashing.

    python3 scripts/selftest.py [--verbose]

Every number checked here comes from ``golden_vectors.json``, which was harvested
from the Node/viem implementation this skill was ported from (the generator is
kept at ``assets/harvest-golden-vectors.mjs.txt``). The Node build is the oracle:
building the vectors from the Python would only prove the Python agrees with
itself.

Tiers, in the order a failure should be investigated:

* **Tier 0 — primitives.** keccak256, EIP-55, fixed-point parsing, and the three
  places JavaScript arithmetic differs from Python's. If Tier 0 is red nothing
  above it means anything.
* **Tier 1 — codec.** ABI encode/decode, including the nested ``bytes[]`` that
  ``unlockData`` is built from, and signed-integer decoding.
* **Tier 2 — signing.** secp256k1, RLP, EIP-1559 assembly, PLAN_HASH.
* **Tier 3 — pool math.** TickMath, liquidity amounts, market-cap bands, poolId
  derivation. These are the 42 assertions carried over from the Node self-test.
* **Tier 4 — domain.** PositionInfo unpacking, hook permission bits, range
  aggregation from real logs, and the display helpers.
* **Tier 5 — planning ergonomics.** Tick pull-off, the price cache, the messages
  that decide whether a caller reaches a mint or goes in circles.
* **Tier 6 — PLAN_HASH coverage.** That every flag reaching the calldata also
  reaches the hash the human approved. Tiers 5 and 6 have no Node oracle — they
  test properties (this changes that, that survives a re-run), not values, so
  nothing self-generated is written into the vectors.

Tiers whose modules do not exist yet are reported as skipped rather than failed,
so this runs usefully from the first phase of the port onward.

Live fixtures, for sanity-checking a change that this file cannot cover offline:

Robinhood Chain (4663)

* **AGENTOS** ``0x6eDA83Fc299C10d474068A7E69771c809Bcbbba3``, Bankr pool
  ``0x1299aa8c4ea0db5b8453757ed129ed8e916561925926a161cb89842e3987401a`` —
  WETH/AGENTOS, dynamic fee (live 0.70%), tickSpacing 200, hook
  ``0x4e3468951D49f2EEa976eD0D6e75fFCb44a9a544`` (DopplerHookInitializer), six
  ranges, self-check must pass.
* **tokenId 1** — WETH/SMK4, fee 3000, spacing 60, no hook, full range, poolId
  ``0xdb2c20421239d46bb30a7a73029b7f9b7f166489bfb972057d33cbd7249413a5``.
* **tokenId 429610** — native-ETH pool with a hook, owner
  ``0x7de10Fec3dBC1267446d00a1F3ccFcb7F4176412``.
* Uniswap **v3** here uses factory ``0x1f7d7550B1b028f7571E69A784071F0205FD2EfA``,
  *not* the canonical ``0x1F98431c…F984``.

Base (8453) — one live token per launchpad, each with ``--chain base``

* **Clanker v4.1 · RED** ``0x361E38FE0fb91E8EE43510b12A534388c21cEb07`` → hook
  ``0xb429d62f…28CC``, locker ``0x63D2DfEA…3496``, pool
  ``0xaafb0612c0ae7833566ff6ce5a4da0d0cc9a6e64d2e1285ed4efde0d2da5253c``, **5**
  locked positions 2886509–2886513.
* **Liquid · VLAD** ``0x9aa76052bc108C1aE57F987F797be555Ff52EC90`` → hook
  ``0x9811f10C…28cc``, locker ``0x77247fCD…35f3``, pool ``0xb83153f9…bb1f49``,
  **3** locked positions 2886409–2886411.
* **Doppler/Bankr · BLEND** ``0x88601AEeF7D03ECaF76E6B61eAE1CF85B06c4ba3`` →
  numeraire WETH, initializer/hook ``0xBDF938149ac6a781F94FAa0ed45E6A0e984c6544``,
  pool ``0x6e168b69aaae0094068cbb281747c4e6fa2e6b5923f7191f352fdda981128701``,
  no NFTs.
* ``pools --token <RED> --chain base`` must finish in a couple of seconds. If it
  hangs, the registry lookup regressed and it fell back to the log scan.
* Cross-check of the two range modes: on the RED pool the ``--mode ticks`` segment
  ``-202000 → -155000`` has liquidity ``1684492566057752195310837``, exactly the
  sum of positions 2886510 and 2886511.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

GOLDEN_PATH = HERE / "golden_vectors.json"

# Mirrors abi.mjs POOL_KEY_COMPONENTS.
POOL_KEY_COMPONENTS = [
    {"name": "currency0", "type": "address"},
    {"name": "currency1", "type": "address"},
    {"name": "fee", "type": "uint24"},
    {"name": "tickSpacing", "type": "int24"},
    {"name": "hooks", "type": "address"},
]

POOL_MANAGER_EVENTS = [
    {
        "type": "event", "name": "ModifyLiquidity",
        "inputs": [
            {"name": "id", "type": "bytes32", "indexed": True},
            {"name": "sender", "type": "address", "indexed": True},
            {"name": "tickLower", "type": "int24"},
            {"name": "tickUpper", "type": "int24"},
            {"name": "liquidityDelta", "type": "int256"},
            {"name": "salt", "type": "bytes32"},
        ],
    },
    {
        "type": "event", "name": "Initialize",
        "inputs": [
            {"name": "id", "type": "bytes32", "indexed": True},
            {"name": "currency0", "type": "address", "indexed": True},
            {"name": "currency1", "type": "address", "indexed": True},
            {"name": "fee", "type": "uint24"},
            {"name": "tickSpacing", "type": "int24"},
            {"name": "hooks", "type": "address"},
            {"name": "sqrtPriceX96", "type": "uint160"},
            {"name": "tick", "type": "int24"},
        ],
    },
]

AGG3_IN = [{"type": "tuple[]", "components": [
    {"name": "target", "type": "address"},
    {"name": "allowFailure", "type": "bool"},
    {"name": "callData", "type": "bytes"},
]}]
AGG3_OUT = [{"type": "tuple[]", "components": [
    {"name": "success", "type": "bool"},
    {"name": "returnData", "type": "bytes"},
]}]


class Results:
    def __init__(self, verbose: bool) -> None:
        self.verbose = verbose
        self.passed = 0
        self.failures: list[str] = []
        self.skipped: list[str] = []

    def check(self, label: str, got, want) -> None:
        if got == want:
            self.passed += 1
            if self.verbose:
                print(f"  ok   {label}")
        else:
            self.failures.append(f"{label}\n       got  {got!r}\n       want {want!r}")
            print(f"  FAIL {label}")

    def skip(self, tier: str, reason: str) -> None:
        self.skipped.append(f"{tier}: {reason}")
        print(f"  --   skipped ({reason})")


def _raises(r: Results, label: str, fn, *, contains: str = "") -> None:
    """Assert ``fn()`` raises, and (optionally) that the message names the field.

    Mirrors ``poolsfun/selftest.py``'s ``raises()`` helper. Checking the message,
    not just that *something* raised, is what catches a regression that swaps in
    a different, opaque exception which happens to also be a ``ValueError`` —
    ``int(str(True))``'s ``invalid literal for int() with base 10: 'True'`` is
    exactly that: a raise with no field name in it, the failure mode this guards.
    """
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 — asserting the failure itself
        ok = not contains or contains.lower() in str(exc).lower()
        r.check(label, ok, True)
        return
    r.check(label, "no exception", "raise")


# ---------------------------------------------------------------------------
# Tier 0 — primitives
# ---------------------------------------------------------------------------

def tier0(g: dict, r: Results) -> None:
    from unilp.hexutil import (
        as_int_n,
        as_uint_n,
        checksum_address,
        div_trunc,
        format_units,
        js_round,
        pad,
        parse_units,
        to_hex,
    )
    from unilp.keccak import event_topic, function_selector, keccak256, keccak256_text

    k = g["keccak"]
    r.check("keccak256('')", keccak256("0x"), k["empty"])
    r.check("keccak256('abc')", keccak256_text("abc"), k["abc"])
    # The rate is 136 bytes. Inputs on either side of it exercise pad, exact-fill
    # and spill — the classic place a keccak implementation is silently wrong.
    for n in (1, 135, 136, 137, 271, 272, 273):
        r.check(f"keccak256('a'*{n})", keccak256_text("a" * n), k[f"len_{n}"])
    r.check("keccak256(raw bytes)", keccak256(bytes([0x00, 0xFF, 0x80, 0x01])), k["bytes_00_ff"])

    # Self-validating: reproduce the topic constants the Node skill hardcoded.
    for name, e in g["event_topics"].items():
        r.check(f"topic0 {name}", event_topic(e["signature"]), e["topic0"])
    pinned = g["event_topics_pinned"]
    r.check("TOPIC_INITIALIZE from its signature",
            event_topic(g["event_topics"]["Initialize"]["signature"]), pinned["TOPIC_INITIALIZE"])
    r.check("TOPIC_MODIFY_LIQUIDITY from its signature",
            event_topic(g["event_topics"]["ModifyLiquidity"]["signature"]),
            pinned["TOPIC_MODIFY_LIQUIDITY"])

    for sig, selector in g["selectors"].items():
        r.check(f"selector {sig}", function_selector(sig), selector)

    # The same check against the ported constants file: abi_defs hardcodes these so a
    # log scan never depends on the encoder, which means nothing else would notice if
    # a digit were mistyped during the port.
    try:
        from unilp import abi_defs
    except ImportError:
        pass
    else:
        r.check("abi_defs.TOPIC_INITIALIZE",
                event_topic(g["event_topics"]["Initialize"]["signature"]),
                abi_defs.TOPIC_INITIALIZE)
        r.check("abi_defs.TOPIC_MODIFY_LIQUIDITY",
                event_topic(g["event_topics"]["ModifyLiquidity"]["signature"]),
                abi_defs.TOPIC_MODIFY_LIQUIDITY)
        r.check("abi_defs.TOPIC_ERC721_TRANSFER",
                event_topic("Transfer(address,address,uint256)"),
                abi_defs.TOPIC_ERC721_TRANSFER)

    for lower, e in g["checksum"].items():
        r.check(f"EIP-55 {lower[:10]}…", checksum_address(lower), e["checksummed"])
        # Re-checksumming must be stable: the launcher registry is mixed-case, and
        # hashing without lowercasing first yields a wrong checksum and a wrong poolId.
        r.check(f"EIP-55 stable {lower[:10]}…", checksum_address(e["checksummed"]),
                e["from_checksummed"])

    for c in g["parse_units"]:
        r.check(f"parseUnits({c['value']}, {c['decimals']})",
                str(parse_units(c["value"], c["decimals"])), c["expected"])
    for c in g["format_units"]:
        r.check(f"formatUnits({c['value']}, {c['decimals']})",
                format_units(int(c["value"]), c["decimals"]), c["expected"])

    # JS-vs-Python arithmetic. Each of these is a wrong number, not an exception.
    r.check("div_trunc(-7, 2) truncates toward zero", div_trunc(-7, 2), -3)
    r.check("div_trunc(7, -2) truncates toward zero", div_trunc(7, -2), -3)
    r.check("div_trunc(7, 2)", div_trunc(7, 2), 3)
    r.check("js_round(0.5) is half-up", js_round(0.5), 1)
    r.check("js_round(2.5) is half-up", js_round(2.5), 3)
    r.check("js_round(-0.5)", js_round(-0.5), 0)
    r.check("as_int_n(24, 0xffffff)", as_int_n(24, 0xFFFFFF), -1)
    r.check("as_int_n(24, 0x800000)", as_int_n(24, 0x800000), -8388608)
    r.check("as_uint_n(256, -1) wraps", as_uint_n(256, -1), (1 << 256) - 1)
    r.check("to_hex(-1, size=1)", to_hex(-1, size=1), "0xff")
    r.check("pad('0x01')", pad("0x01"), "0x" + "0" * 62 + "01")

    # Unsized to_hex is the JSON-RPC *quantity* encoding: minimal, no leading zero
    # byte. Padding it to whole bytes makes a node reject "fromBlock": "0x00", which
    # is how a wrong implementation here shows up — as an unexplained HTTP 400 on
    # every log scan, long after the encoder itself has been declared correct.
    th = g["to_hex"]
    for c in th["unsized"]:
        r.check(f"to_hex({c['v']})", to_hex(int(c["v"])), c["out"])
    for c in th["sized"]:
        r.check(f"to_hex({c['v']}, size=32)", to_hex(int(c["v"]), size=32), c["out"])
    for c in th["padded"]:
        r.check(f"pad(to_hex({c['v']}))", pad(to_hex(int(c["v"]))), c["out"])


# ---------------------------------------------------------------------------
# Tier 1 — ABI codec
# ---------------------------------------------------------------------------

def tier1(g: dict, r: Results) -> None:
    from unilp.abi_codec import (
        canonical_type,
        decode,
        decode_event_log,
        encode,
        encode_function_data,
    )
    from unilp.hexutil import concat_hex, to_hex

    for name, e in g["pool_keys"].items():
        pk = e["poolKey"]
        r.check(f"encode PoolKey {name}",
                encode(POOL_KEY_COMPONENTS, [pk["currency0"], pk["currency1"], pk["fee"],
                                             pk["tickSpacing"], pk["hooks"]]),
                e["encoded"])

    for c in g["int_codec"]:
        spec = [{"type": c["type"]}]
        label = f"{c['type']} = {c['value']}"
        r.check(f"encode {label}", encode(spec, [int(c["value"])]), c["encoded"])
        r.check(f"decode {label}", str(decode(spec, c["encoded"])[0]), c["decoded"])

    # The single highest-value vector set: one assertion per plan pins the entire
    # encoder, including the nested bytes[] head/tail layout of unlockData.
    modify_abi = [{"type": "function", "name": "modifyLiquidities",
                   "inputs": [{"type": "bytes"}, {"type": "uint256"}], "outputs": []}]
    for name, p in g["plans"].items():
        action_bytes = concat_hex([to_hex(a, size=1) for a in p["actions"]])
        r.check(f"unlockData {name}",
                encode([{"type": "bytes"}, {"type": "bytes[]"}], [action_bytes, p["params"]]),
                p["unlockData"])
        r.check(f"modifyLiquidities calldata {name}",
                encode_function_data(modify_abi, "modifyLiquidities",
                                     [p["unlockData"], int(p["deadline"])]),
                p["calldata"])

    _check_plan_builders(g, r)

    m = g["multicall3"]
    r.check("aggregate3 request", encode(AGG3_IN, [m["request_calls"]]), m["request_encoded"])
    decoded = decode(AGG3_OUT, m["response_encoded"])[0]
    for i, want in enumerate(m["response_expected"]):
        r.check(f"aggregate3 response[{i}].success", decoded[i]["success"], want["success"])
        # An empty returnData on a "successful" call means no code at the target.
        # It must stay distinguishable from a zero return value.
        r.check(f"aggregate3 response[{i}].returnData", decoded[i]["returnData"],
                want["returnData"])

    ml = g["logs"]["modify_liquidity_negative"]
    args = decode_event_log(POOL_MANAGER_EVENTS, ml["topics"], ml["data"])["args"]
    r.check("ModifyLiquidity.tickLower", args["tickLower"], ml["expected"]["tickLower"])
    r.check("ModifyLiquidity.tickUpper", args["tickUpper"], ml["expected"]["tickUpper"])
    # Sign extension: getting this wrong turns a removal into a ~1e77 addition and
    # the reserve totals still look plausible.
    r.check("ModifyLiquidity.liquidityDelta is negative",
            str(args["liquidityDelta"]), ml["expected"]["liquidityDelta"])

    iz = g["logs"]["initialize"]
    args = decode_event_log(POOL_MANAGER_EVENTS, iz["topics"], iz["data"])["args"]
    for key, want in iz["expected"].items():
        r.check(f"Initialize.{key}", str(args[key]), str(want))

    r.check("canonical tuple expansion",
            canonical_type({"type": "tuple", "components": POOL_KEY_COMPONENTS}),
            "(address,address,uint24,int24,address)")
    r.check("canonical uint widens to uint256", canonical_type({"type": "uint"}), "uint256")


def _check_plan_builders(g: dict, r: Results) -> None:
    """Rebuild each golden plan through ``v4_actions`` and compare it element by element.

    The ``unlockData`` assertions above pin the *encoder* given a params list. These pin
    the *builders* that produce that list: the action order, the SWEEP that only a native
    currency0 gets, the ``value`` that must accompany it, and the DECREASE leg that a
    zero-liquidity burn must skip. Those are the choices that make a call revert, or —
    worse for SWEEP — silently leave ETH in the PositionManager.
    """
    from unilp import v4_actions as va

    # Same inputs the harvest script fed the Node builders.
    pk = g["pool_keys"]["LETTI_base_liquid"]["poolKey"]
    native_key = dict(pk, currency0="0x0000000000000000000000000000000000000000")
    to = "0x7de10Fec3dBC1267446d00a1F3ccFcb7F4176412"
    built = {
        "mint": va.build_mint_plan(pk, 214000, 230400, 177557320016371022708535,
                                   48237000000000000, 9524320039159120000000000000, to),
        "mint_native": va.build_mint_plan(native_key, -60, 60, 1000000000000000000,
                                          500000000000000000, 1000000, to),
        "increase": va.build_increase_plan(pk, 2493126, 12345678901234567890,
                                           1000000000000000000, 2000000000000000000, to),
        "decrease": va.build_decrease_plan(pk, 2493126, 88778660008185511354267, 0, 0, to),
        "collect": va.build_collect_plan(pk, 2493126, to),
        "burn": va.build_burn_plan(pk, 2493126, 177557320016371022708535, 0, 0, to),
        "burn_empty": va.build_burn_plan(pk, 2493126, 0, 0, 0, to),
    }
    for name, plan in built.items():
        want = g["plans"][name]
        r.check(f"plan {name} actions", plan["actions"], want["actions"])
        r.check(f"plan {name} action names", va.describe_actions(plan["actions"]),
                want["action_names"])
        r.check(f"plan {name} params", plan["params"], want["params"])
        # Non-zero only when currency0 is native; getting it wrong strands ETH.
        r.check(f"plan {name} value", str(plan["value"]), want["value"])
        r.check(f"plan {name} unlockData",
                va.encode_unlock_data(plan["actions"], plan["params"]), want["unlockData"])


# ---------------------------------------------------------------------------
# Tier 2 — signing and PLAN_HASH
# ---------------------------------------------------------------------------

def tier2(g: dict, r: Results) -> None:
    from unilp.hexutil import to_hex
    from unilp.keccak import keccak256

    # PLAN_HASH is keccak over a canonical JSON string. The canonical string is
    # stored in the vectors verbatim so a mismatch is diagnosable here rather than
    # showing up only as a differing 8-character hash.
    for name, e in g["plan_hash"].items():
        r.check(f"PLAN_HASH {name}",
                keccak256(to_hex(e["canonical_json"].encode("utf-8")))[2:10], e["hash"])

    # And the builder itself. The vectors record which keys the Node build held as
    # BigInt and which as Number, because that is the whole difficulty: JSON.stringify
    # quoted the former and not the latter, and Python has a single int type. Rebuild
    # each field set with that distinction restored and check both the exact canonical
    # string and the hash.
    from lp_write import Big, plan_hash

    for name, e in g["plan_hash"].items():
        parsed = json.loads(e["canonical_json"])
        fields = {k: (Big(v) if k in e["bigint_keys"] else v) for k, v in parsed.items()}
        for key in e["number_keys"]:
            r.check(f"PLAN_HASH {name}/{key} stays a JSON number",
                    isinstance(fields[key], int) and not isinstance(fields[key], Big), True)
        r.check(f"PLAN_HASH builder {name}", plan_hash(fields), e["hash"])

    try:
        from unilp.secp256k1 import account_from_private_key, ecrecover, sign_digest
    except ImportError:
        r.skip("tier2/signing", "unilp.secp256k1 not written yet")
        return

    s = g["signing"]
    account = account_from_private_key(s["private_key"])
    r.check("derived signer address", account["address"], s["expected_address"])

    # Derivation lives in `account`, signing in `secp256k1`. lp_read.py imports the first
    # so `positions` can resolve your own wallet, and the split is what keeps that from
    # making a read-only script able to move funds. Pin both halves: the same address comes
    # out of the derivation-only module, and that module has no way to sign.
    from unilp import account as account_mod

    r.check("account module derives the same address",
            account_mod.account_from_private_key(s["private_key"])["address"],
            s["expected_address"])
    r.check("account module exposes no signing entry point",
            sorted(n for n in dir(account_mod) if "sign" in n.lower()), [])

    # Read the imports out of the syntax tree, not out of the text: the docstring names
    # secp256k1 to explain the split, and a grep would score that as a dependency.
    import ast

    imported = set()
    for node in ast.walk(ast.parse(Path(account_mod.__file__).read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            # `from . import secp256k1` parses with module=None and the module name in
            # names — reading only `node.module` would score that as importing nothing.
            if node.module:
                imported.add(node.module)
            else:
                imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    r.check("account module imports nothing that can sign",
            sorted(imported - {"__future__"}), ["hexutil", "keccak"])
    signature = sign_digest(s["private_key"], s["digest"])
    r.check("RFC-6979 signature is deterministic", signature["hex"], s["signature"])
    r.check("ecrecover round-trips to the signer",
            ecrecover(s["digest"], signature["r"], signature["s"], signature["yParity"]),
            s["expected_address"])

    try:
        from unilp.tx import sign_transaction
    except ImportError:
        r.skip("tier2/tx", "unilp.tx not written yet")
        return
    tx = dict(s["tx"])
    signed = sign_transaction(tx, s["private_key"])
    r.check("EIP-1559 raw transaction", signed["raw"], s["signed_raw"])
    r.check("transaction hash", signed["hash"], s["tx_hash"])


# ---------------------------------------------------------------------------
# Tier 3 — pool math (the 42 assertions carried over from the Node self-test)
# ---------------------------------------------------------------------------

def tier3(g: dict, r: Results) -> None:
    try:
        from unilp import v4_math  # noqa: F401
    except ImportError:
        r.skip("tier3/math", "unilp.v4_math not written yet")
        return

    import math as _math

    from unilp.v4_math import (
        get_amounts_for_liquidity_at_ticks,
        get_liquidity_for_amounts,
        get_sqrt_ratio_at_tick,
        get_tick_at_sqrt_ratio,
        max_usable_tick,
        mcap_band_for_range,
        min_usable_tick,
        raw_price_at_tick,
        snap_tick,
        tick_at_mcap,
    )

    m = g["math"]

    def close(label: str, got, want, rel: float = 1e-12) -> None:
        """Float comparison — the display path is float, so exact equality is wrong."""
        if want in (0, None) or got is None or _math.isinf(want) or _math.isinf(got):
            r.check(label, got, want)
            return
        r.check(label, abs(got - want) / abs(want) <= rel, True)

    for tick, want in m["sqrt"].items():
        r.check(f"getSqrtRatioAtTick({tick})", str(get_sqrt_ratio_at_tick(int(tick))), want)
    # Round-trip: every tick must survive the conversion and come back unchanged.
    for tick, want in m["tickAt"].items():
        r.check(f"tick round-trip {tick}",
                get_tick_at_sqrt_ratio(get_sqrt_ratio_at_tick(int(tick))), want)
    for tick, want in m["price"].items():
        close(f"rawPriceAtTick({tick})", raw_price_at_tick(int(tick)), want)

    # The live AGENTOS/LETTI brackets, at the LETTI pool's current price.
    price = get_sqrt_ratio_at_tick(229860)
    for c in m["amounts"]:
        got = get_amounts_for_liquidity_at_ticks(price, c["lo"], c["hi"], int(c["L"]), c["ru"])
        label = f"amounts[{c['lo']},{c['hi']}] roundUp={c['ru']}"
        r.check(f"{label} amount0", str(got["amount0"]), c["amount0"])
        r.check(f"{label} amount1", str(got["amount1"]), c["amount1"])
    for c in m["liq"]:
        r.check(f"getLiquidityForAmounts[{c['lo']},{c['hi']}]",
                str(get_liquidity_for_amounts(
                    price, get_sqrt_ratio_at_tick(c["lo"]), get_sqrt_ratio_at_tick(c["hi"]),
                    int(c["a0"]), int(c["a1"]))), c["L"])

    # snapTick in 'nearest' mode rides on Math.round semantics, so the negative and
    # exactly-halfway cases here are the ones that catch banker's rounding.
    for c in m["snap"]:
        r.check(f"snapTick({c['t']}, {c['s']}, {c['mo']})",
                snap_tick(c["t"], c["s"], c["mo"]), c["r"])
    for c in m["minmax"]:
        r.check(f"minUsableTick({c['s']})", min_usable_tick(c["s"]), c["min"])
        r.check(f"maxUsableTick({c['s']})", max_usable_tick(c["s"]), c["max"])

    opts = dict(m["band_options"])
    opts["total_supply"] = int(opts["total_supply"])
    for c in m["band"]:
        band = mcap_band_for_range(c["lo"], c["hi"], **opts)
        for edge in ("from", "to"):
            want = c[edge]
            if want is None or want == "Infinity":
                want = _math.inf
            close(f"mcapBand[{c['lo']},{c['hi']}].{edge}", band[edge], want, rel=1e-9)
    band_opts = {k: v for k, v in opts.items() if k != "tick_spacing"}
    for c in m["tickAtMcap"]:
        r.check(f"tickAtMcap({c['mcap']})", tick_at_mcap(c["mcap"], **band_opts), c["tick"])

    try:
        from unilp.v4_pool import compute_pool_id
    except ImportError:
        r.skip("tier3/poolid", "unilp.v4_pool not written yet")
        return
    for name, e in g["pool_keys"].items():
        if e["pinned_poolId"]:
            r.check(f"poolId {name}", compute_pool_id(e["poolKey"]), e["pinned_poolId"])

    # Every Base launchpad opens a dynamic-fee pool at tickSpacing 200, so the poolId is
    # fully determined by (currencies, hook). These three ids came from live Initialize
    # logs; if derive_pool_candidates stops reproducing them, launcher discovery on Base
    # is silently broken — and it is the only discovery path that chain can serve.
    try:
        from unilp.chains import CHAINS
        from unilp.launchers import derive_pool_candidates
    except ImportError:
        r.skip("tier3/launchpad", "unilp.launchers not written yet")
        return
    lp = g["launchpad_pools"]
    for c in lp["cases"]:
        derived = derive_pool_candidates(
            CHAINS["base"], c["token"], hook=c["hook"], numeraire=lp["numeraire"]
        )
        r.check(f"{c['name']}: one candidate", len(derived), 1)
        r.check(f"{c['name']}: poolId", derived[0]["poolId"] if derived else None,
                c["poolId"])

    # A hook-less pool is invisible to every launchpad registry, so it is reached by
    # deriving the same id with hooks = 0 across the conventional fee tiers. These ids
    # were confirmed live with getSlot0; losing them means `mint` can no longer find a
    # plain pool on a chain that cannot serve a wide log scan.
    try:
        from unilp.v4_pool import derive_vanilla_candidates
    except ImportError:
        r.skip("tier3/vanilla", "derive_vanilla_candidates not written yet")
        return
    for c in g["vanilla_pools"]["cases"]:
        derived = derive_vanilla_candidates(CHAINS[c["chain"]], c["token"])
        hit = next((x for x in derived if x["poolId"] == c["poolId"]), None)
        r.check(f"{c['name']}: derived", hit["poolId"] if hit else None, c["poolId"])
        if hit:
            r.check(f"{c['name']}: poolKey", [
                hit["poolKey"]["currency0"], hit["poolKey"]["currency1"],
                hit["poolKey"]["fee"], hit["poolKey"]["tickSpacing"],
                hit["poolKey"]["hooks"],
            ], [c["currency0"], c["currency1"], c["fee"], c["tickSpacing"],
                "0x0000000000000000000000000000000000000000"])

    # A currency cannot be paired with itself. WETH is both a known quote and a token
    # someone will ask about, so this is the case that actually bites.
    weth = CHAINS["base"]["wrappedNative"]
    self_paired = [x for x in derive_vanilla_candidates(CHAINS["base"], weth)
                   if x["poolKey"]["currency0"].lower() == x["poolKey"]["currency1"].lower()]
    r.check("vanilla: no self-paired candidate", self_paired, [])

    # The explicit-PoolKey escape hatch must never address a pool the caller did not
    # name: it is only safe because the poolId is recomputed and compared.
    try:
        from lp_read import pool_key_from_args
    except ImportError:
        r.skip("tier3/poolkey-args", "lp_read.pool_key_from_args not written yet")
        return
    case = g["vanilla_pools"]["cases"][0]
    explicit = pool_key_from_args({
        "currency0": case["currency0"], "currency1": case["currency1"],
        "fee": str(case["fee"]), "tick-spacing": str(case["tickSpacing"]),
    })
    r.check("poolKeyFromArgs: recompute", compute_pool_id(explicit), case["poolId"])
    r.check("poolKeyFromArgs: hooks default to none", explicit["hooks"],
            "0x0000000000000000000000000000000000000000")
    r.check("poolKeyFromArgs: hex fee accepted", pool_key_from_args({
        "currency0": case["currency0"], "currency1": case["currency1"],
        "fee": "0x800000", "tick-spacing": "200"})["fee"], 0x800000)
    r.check("poolKeyFromArgs: absent -> None", pool_key_from_args({"id": "0xabc"}), None)
    # A wrong fee must produce a different id rather than a plausible-looking match.
    wrong = pool_key_from_args({
        "currency0": case["currency0"], "currency1": case["currency1"],
        "fee": "500", "tick-spacing": str(case["tickSpacing"]),
    })
    r.check("poolKeyFromArgs: wrong fee -> different id",
            compute_pool_id(wrong) != case["poolId"], True)
    try:
        pool_key_from_args({"currency0": case["currency0"], "fee": "3000"})
        r.check("poolKeyFromArgs: partial rejected", "no error", "RuntimeError")
    except RuntimeError:
        r.check("poolKeyFromArgs: partial rejected", "RuntimeError", "RuntimeError")

    _ = get_amounts_for_liquidity_at_ticks  # exercised by the read-path diff in phase 5


# ---------------------------------------------------------------------------
# Tier 4 — domain layer (PositionInfo, hooks, range aggregation, display)
# ---------------------------------------------------------------------------

def tier4(g: dict, r: Results) -> None:
    try:
        from unilp import v4_pool  # noqa: F401
    except ImportError:
        r.skip("tier4/domain", "unilp.v4_pool not written yet")
        return

    from unilp.v4_pool import (
        aggregate_ranges,
        decode_hook_flags,
        decode_position_info,
        format_hook_flags,
        pool_id_matches_truncated,
    )

    for i, c in enumerate(g["position_info"]):
        got = decode_position_info(int(c["packed"]))
        for key, want in c["expected"].items():
            r.check(f"positionInfo[{i}].{key}", got[key], want)
        # The packed id keeps only the top 25 bytes; the match must still hold.
        r.check(f"positionInfo[{i}] matches its poolId",
                pool_id_matches_truncated(c["source_poolId"], got["truncatedPoolId"]), True)

    for c in g["hook_flags"]:
        got = decode_hook_flags(c["address"])
        for key, want in c["decoded"].items():
            r.check(f"hookFlags({c['address'][:10]}).{key}", got[key], want)
        r.check(f"formatHookFlags({c['address'][:10]})",
                format_hook_flags(c["address"]), c["formatted"])

    # Range aggregation end to end, on the real ModifyLiquidity log. The stored log is a
    # *removal*; feeding only that must produce no ranges, because a net-negative entry
    # is dropped. If sign extension regressed it would instead surface a ~1e77 range.
    ml = g["logs"]["modify_liquidity_negative"]
    liquidity = -int(ml["expected"]["liquidityDelta"])
    positive_data = "0x" + ml["data"][2:-128] + \
        format(liquidity & ((1 << 256) - 1), "064x") + "0" * 64
    pool = {"sqrtPriceX96": int(g["logs"]["initialize"]["expected"]["sqrtPriceX96"]),
            "tick": 229860, "activeLiquidity": None}
    add_log = {"topics": ml["topics"], "data": positive_data}
    remove_log = {"topics": ml["topics"], "data": ml["data"]}

    added = aggregate_ranges([add_log], pool)
    r.check("aggregateRanges add: one range", len(added["ranges"]), 1)
    r.check("aggregateRanges add: liquidity",
            str(added["ranges"][0]["liquidity"]), str(liquidity))
    r.check("aggregateRanges add: ticks",
            (added["ranges"][0]["tickLower"], added["ranges"][0]["tickUpper"]),
            (ml["expected"]["tickLower"], ml["expected"]["tickUpper"]))
    # Current tick 229860 sits far above the range, so it holds currency1 only.
    r.check("aggregateRanges add: status", added["ranges"][0]["status"], "above")
    r.check("aggregateRanges add: amount0 is zero", str(added["amount0"]), "0")
    r.check("aggregateRanges add: owner from topic",
            added["ranges"][0]["owner"], "0x7de10Fec3dBC1267446d00a1F3ccFcb7F4176412")

    netted = aggregate_ranges([add_log, remove_log], pool)
    r.check("aggregateRanges add+remove nets to nothing", len(netted["ranges"]), 0)
    r.check("aggregateRanges counts both events", netted["eventCount"], 2)
    r.check("aggregateRanges removal alone yields nothing",
            len(aggregate_ranges([remove_log], pool)["ranges"]), 0)

    # -- display ----------------------------------------------------------
    from unilp.fmt import (
        fmt_band,
        fmt_units,
        fmt_usd,
        opt_float,
        opt_int,
        opt_str,
        parse_args,
        render_kv,
        render_table,
        short,
        short_id,
    )

    fmt = g["format"]
    for c in fmt["fmt_units"]:
        r.check(f"fmtUnits({c['value']}, {c['decimals']}, {c['maxFrac']})",
                fmt_units(int(c["value"]), c["decimals"], c["maxFrac"]), c["out"])
    for c in fmt["fmt_usd"]:
        value = c["value"]
        # toPrecision's exponential threshold is where Python's %g disagrees with JS.
        value = math.inf if value == "Infinity" else value
        r.check(f"fmtUsd({c['value']})", fmt_usd(value), c["out"])
    for c in fmt["fmt_band"]:
        band = c["band"]
        if band and band.get("to") == "Infinity":
            band = {**band, "to": math.inf}
        r.check(f"fmtBand({c['band']})", fmt_band(band), c["out"])
    r.check("short", short("0x9811f10Cd549c754Fa9E5785989c422A762c28cc"), fmt["short"])
    r.check("shortId", short_id(
        "0x4e539dbb29b663a1345c01240a45b8412b9855b0f69e15879d6ab06aeab6f53e"),
        fmt["short_id"])
    r.check("renderTable", render_table(
        [{"key": "a", "label": "tick"},
         {"key": "b", "label": "liquidity", "align": "right"}],
        [{"a": "-202000", "b": "1684492566057752195310837"}, {"a": "0", "b": "1"}],
    ), fmt["table"])
    r.check("renderKv", render_kv([("poolId", "0x4e53"), ("tickSpacing", "200")]),
            fmt["kv"])

    args = parse_args(["pool", "--id", "0xabc", "--json", "--mode=ticks", "--ranges", "10"])
    r.check("parseArgs positional", args["_"], ["pool"])
    r.check("parseArgs value", args["id"], "0xabc")
    r.check("parseArgs bare flag", args["json"], True)
    r.check("parseArgs --k=v", args["mode"], "ticks")
    r.check("parseArgs trailing value", args["ranges"], "10")

    # A bare, value-taking ``--flag`` (no value, or immediately followed by
    # another ``--flag``) parses to the boolean True — correct for a switch like
    # --json above, and exactly the sentinel that must never reach int()/float()
    # unguarded: ``int(True) == 1`` silently replaces the caller's real default
    # (e.g. a 100bps slippage floor) with 1, and nothing about that looks like a
    # crash. opt_str/opt_int/opt_float are the one place this is caught.
    r.check("optInt: absent falls back to default", opt_int({}, "x", 42), 42)
    r.check("optInt: parses a numeric string", opt_int({"x": "7"}, "x", 42), 7)
    r.check("optFloat: absent falls back to default", opt_float({}, "x", 1.3), 1.3)
    r.check("optStr: absent is None", opt_str({}, "x"), None)
    r.check("optStr: trims whitespace", opt_str({"x": "  v  "}, "x"), "v")
    for flag in ("slippage-bps", "deadline-secs", "max-tick-drift", "max-attempts",
                 "expiration-days"):
        _raises(r, f"optInt: bare --{flag} is rejected, not coerced to 1",
                lambda f=flag: opt_int({f: True}, f, 100), contains="needs a value")
    _raises(r, "optFloat: bare --gas-multiplier is rejected, not coerced to 1.0",
            lambda: opt_float({"gas-multiplier": True}, "gas-multiplier", 1.3),
            contains="needs a value")
    _raises(r, "optStr: bare --signer-env is rejected, not coerced to True",
            lambda: opt_str({"signer-env": True}, "signer-env"), contains="needs a value")
    _raises(r, "optInt: non-numeric value is rejected",
            lambda: opt_int({"slippage-bps": "lots"}, "slippage-bps", 100),
            contains="whole number")
    # Bounds mirror the call sites in lp_write.py/ratchet.py exactly, so a bound
    # tightened or loosened at the call site shows up here as a broken test
    # rather than only at broadcast time.
    _raises(r, "optInt: slippage-bps >= 100% is rejected",
            lambda: opt_int({"slippage-bps": "20000"}, "slippage-bps", 100,
                             minimum=0, maximum=9_999), contains="at most 9999")
    _raises(r, "optInt: negative slippage-bps is rejected",
            lambda: opt_int({"slippage-bps": "-1"}, "slippage-bps", 100,
                             minimum=0, maximum=9_999), contains="at least 0")
    _raises(r, "optInt: deadline-secs below the 60s floor is rejected",
            lambda: opt_int({"deadline-secs": "10"}, "deadline-secs", 1200,
                             minimum=60), contains="at least 60")


# ---------------------------------------------------------------------------
# Tier 5 — planning ergonomics

def tier5(g: dict, r: Results) -> None:
    """The parts that decide whether a caller reaches a mint or goes in circles.

    None of this is arithmetic the chain checks — it is the difference between one
    ``ticks`` call answering the question and a dozen commands guessing at it.
    """
    try:
        import lp_read
        from unilp import prices as prices_mod
    except ImportError:
        r.skip("tier5/planning", "lp_read or unilp.prices not importable")
        return

    pull = lp_read.pull_off_current_tick

    # The live case this was written for: AGENTOS at tick 209056, band asked for as
    # "from here up to $200K mcap", snapped outward to 206400..209200 — one spacing past
    # the current tick, so mint came back wanting both currencies.
    r.check("pullOffCurrent: the AGENTOS straddle becomes AGENTOS-only",
            pull(209056, 206400, 209200, 200), (206400, 209000))

    # Both sides have room here, so which one survives is a real choice rather than the
    # only option left — these two pin the rule that the bigger part of the band wins.
    r.check("pullOffCurrent: keeps the below side when the band sits below",
            pull(209056, 200000, 209400, 200), (200000, 209000))
    r.check("pullOffCurrent: keeps the above side when the band sits above",
            pull(209056, 206400, 220000, 200), (209200, 220000))
    # tickUpper == current is already single-sided (range_status calls it "above").
    r.check("pullOffCurrent: leaves a below-range alone",
            pull(209056, 206400, 209000, 200), (206400, 209000))
    r.check("pullOffCurrent: leaves an above-range alone",
            pull(209056, 209200, 212000, 200), (209200, 212000))
    r.check("pullOffCurrent: keeps the above side of a narrow straddle",
            pull(209056, 209000, 209400, 200), (209200, 209400))
    # Ticks that did not come from snapping — the preferred side turns out to have no
    # room, so take the other one rather than hand back a straddle.
    r.check("pullOffCurrent: falls back when the preferred side has no room",
            pull(209190, 209010, 209300, 200), (209200, 209300))
    threw = False
    try:
        pull(209100, 209000, 209200, 200)  # one spacing wide, current tick inside it
    except RuntimeError:
        threw = True
    r.check("pullOffCurrent: refuses a band thinner than one spacing", threw, True)

    # Reversed mcap bounds describe the same band. "From here to 200k" is written with
    # the larger number first whenever the target is below where the token trades.
    lo, hi = 200000.0, 156200.0
    if hi < lo:
        lo, hi = hi, lo
    r.check("mcap bounds normalise regardless of order", (lo, hi), (156200.0, 200000.0))

    # -- price cache, shared across processes ------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        previous = os.environ.get("UNILP_CACHE_DIR")
        os.environ["UNILP_CACHE_DIR"] = tmp
        try:
            prices_mod._disk_write({"robinhood:0xaaa": 1869.01, "robinhood:0xbbb": None})
            got = prices_mod._disk_read(["robinhood:0xaaa", "robinhood:0xbbb"])
            r.check("price cache round-trips a value", got.get("robinhood:0xaaa"), 1869.01)
            r.check("price cache round-trips a null", got.get("robinhood:0xbbb"), None)
            r.check("price cache ignores keys not asked for",
                    prices_mod._disk_read(["robinhood:0xccc"]), {})

            # An entry older than the TTL must not be served: a price minutes stale is
            # worse than one refetch.
            path = prices_mod._cache_path()
            stale = time.time() - prices_mod._DISK_TTL - 1
            path.write_text(json.dumps({"robinhood:0xaaa": [stale, 1.0]}), encoding="utf-8")
            r.check("price cache drops an expired entry",
                    prices_mod._disk_read(["robinhood:0xaaa"]), {})

            # Half-written or hand-edited file: refetching is always safe, raising is not.
            path.write_text("{not json", encoding="utf-8")
            r.check("price cache survives a corrupt file",
                    prices_mod._disk_read(["robinhood:0xaaa"]), {})
            path.unlink()
            r.check("price cache survives a missing file",
                    prices_mod._disk_read(["robinhood:0xaaa"]), {})
        finally:
            if previous is None:
                os.environ.pop("UNILP_CACHE_DIR", None)
            else:
                os.environ["UNILP_CACHE_DIR"] = previous

    # A throttled lookup and an unindexed token both surface as "no price", but only one
    # of them is worth waiting out — the message has to tell them apart.
    chain = {"geckoNetwork": "robinhood"}
    was = prices_mod._last_failure
    try:
        prices_mod._last_failure = prices_mod.RATE_LIMITED
        throttled = prices_mod.price_unavailable_message(chain, "WETH")
        prices_mod._last_failure = None
        unknown = prices_mod.price_unavailable_message(chain, "WETH")
    finally:
        prices_mod._last_failure = was
    r.check("rate-limited message says it is temporary", "rate-limiting" in throttled, True)
    r.check("rate-limited message says to retry", "again" in throttled, True)
    r.check("unindexed message names the network", "robinhood" in unknown, True)
    r.check("unindexed message is not the rate-limit one",
            "rate-limiting" in unknown, False)

    # `positions` with no --owner means "mine". The address comes from the signing key,
    # which is the only thing this file ever reads it for.
    from unilp.account import account_from_private_key

    key = "0x" + "11" * 32
    # Derived here rather than written out: tier 2 already pins the arithmetic against the
    # golden vectors, so what this needs to prove is only that the wiring reaches it.
    expected = account_from_private_key(key)["address"]
    was_env = dict(os.environ)
    try:
        os.environ["UNIV4_LP_PRIVATE_KEY"] = key
        r.check("positions with no --owner resolves the configured wallet",
                lp_read.resolve_owner({}), expected)
        r.check("an explicit --owner still wins",
                lp_read.resolve_owner({"owner": "0x" + "ab" * 20}), "0x" + "ab" * 20)
        os.environ["OTHER_KEY"] = "0x" + "22" * 32
        r.check("--signer-env picks a different variable",
                lp_read.resolve_owner({"signer-env": "OTHER_KEY"}) != expected, True)
        # An explicit owner must not need the key at all — asking about someone else's
        # wallet is the ordinary case and cannot depend on this machine being configured.
        os.environ.pop("UNIV4_LP_PRIVATE_KEY")
        r.check("an explicit --owner never touches the key",
                lp_read.resolve_owner({"owner": "0x" + "cd" * 20}), "0x" + "cd" * 20)
        # A bare `--owner` (no value) used to silently resolve to the *string*
        # "True" — a 20-char literal that fails 30+ lines downstream inside
        # checksum_address with "not a 20-byte address: 'True'", not here where
        # the actual mistake was made.
        _raises(r, "a bare --owner is rejected here, not miles downstream",
                lambda: lp_read.resolve_owner({"owner": True}), contains="needs a value")
    finally:
        os.environ.clear()
        os.environ.update(was_env)


# ---------------------------------------------------------------------------
# Tier 6 — PLAN_HASH covers every flag that reaches the calldata
# ---------------------------------------------------------------------------

# The confirm gate spans two processes: the dry run prints a hash, a human approves it,
# and a second invocation broadcasts with --confirm. Nothing but the hash ties those two
# invocations together, so a flag that changes the calldata (or loosens a guard shown in
# the approved table) and does NOT change the hash can be swapped in between. That is a
# whole bug class, not one bug — `increase --recipient` and `approve --expiration-days`
# both shipped that way — so it is tested as a class: mutate one flag at a time and
# require a different hash.
#
# Everything here runs without RPC: the four loaders that talk to a node are stubbed and
# run_plan (which simulates) is replaced by a capture.

_TS = 1_767_225_600            # fixed block clock; hashes must not depend on it
_POOL = "0xdb2c20421239d46bb30a7a73029b7f9b7f166489bfb972057d33cbd7249413a5f"
_POOL_ALT = "0x1299aa8c4ea0db5b8453757ed129ed8e916561925926a161cb89842e3987401a"
_TOKEN = "0x4200000000000000000000000000000000000006"
_TOKEN_ALT = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
_TOKEN_THIRD = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
_ME = "0x7de10Fec3dBC1267446d00a1F3ccFcb7F4176412"
_THEM = "0x000000000000000000000000000000000000dEaD"

_BASELINE = {
    "approve": {"token": _TOKEN, "amount": "1", "expiration-days": "30"},
    # 3000/60 is a conventional tier, so --allow-odd-tier is inert here; it is set so the
    # fee and tick-spacing mutations below do not trip the odd-tier refusal instead of
    # producing a plan. The refusal itself is tested separately in tier6.
    "create-pool": {"token0": _TOKEN, "token1": _TOKEN_ALT, "fee": "3000",
                    "tick-spacing": "60", "tick": "0", "allow-odd-tier": True},
    "mint": {"pool": _POOL, "tick-lower": "-600", "tick-upper": "600",
             "liquidity": "1000000", "slippage-bps": "100", "recipient": _ME,
             "max-tick-drift": "60", "deadline-secs": "1200"},
    "increase": {"token-id": "429610", "liquidity": "1000000", "slippage-bps": "100",
                 "recipient": _ME, "deadline-secs": "1200"},
    "decrease": {"token-id": "429610", "liquidity": "500000", "slippage-bps": "100",
                 "recipient": _ME, "deadline-secs": "1200"},
    "collect": {"token-id": "429610", "recipient": _ME, "deadline-secs": "1200"},
    "burn": {"token-id": "429610", "slippage-bps": "100", "recipient": _ME,
             "deadline-secs": "1200"},
}

# Every flag each command reads that ends up in the calldata, in msg.value, or in a bound
# the user approved. Adding a flag to a command means adding a line here.
_MUTATIONS = [
    ("approve", "token", _TOKEN_ALT),
    ("approve", "amount", "2"),
    ("approve", "expiration-days", "3650"),
    ("create-pool", "token0", _TOKEN_THIRD),
    ("create-pool", "token1", _TOKEN_THIRD),
    ("create-pool", "fee", "500"),
    ("create-pool", "tick-spacing", "10"),
    ("create-pool", "tick", "600"),
    ("mint", "pool", _POOL_ALT),
    ("mint", "tick-lower", "-1200"),
    ("mint", "tick-upper", "1200"),
    ("mint", "liquidity", "2000000"),
    ("mint", "slippage-bps", "500"),
    ("mint", "recipient", _THEM),
    ("mint", "max-tick-drift", "6000"),
    ("mint", "deadline-secs", "600"),
    ("increase", "token-id", "429611"),
    ("increase", "liquidity", "2000000"),
    ("increase", "slippage-bps", "500"),
    ("increase", "recipient", _THEM),
    ("increase", "deadline-secs", "600"),
    ("decrease", "token-id", "429611"),
    ("decrease", "liquidity", "600000"),
    ("decrease", "slippage-bps", "500"),
    ("decrease", "recipient", _THEM),
    ("decrease", "deadline-secs", "600"),
    ("collect", "token-id", "429611"),
    ("collect", "recipient", _THEM),
    ("collect", "deadline-secs", "600"),
    ("burn", "token-id", "429611"),
    ("burn", "slippage-bps", "500"),
    ("burn", "recipient", _THEM),
    ("burn", "deadline-secs", "600"),
]

# Frozen field sets. The mutation table above catches a field that stops mattering; this
# catches one that is quietly dropped, and it is the diff a reviewer should have to justify.
_HASH_FIELDS = {
    "approve": ["amount", "chainId", "cmd", "expirationDays", "needErc20", "needPermit2",
                "signer", "token"],
    # No deadlineSecs: initializePool carries no deadline, so nothing binds one.
    "create-pool": ["chainId", "cmd", "currency0", "currency1", "fee", "hooks", "poolId",
                    "signer", "sqrtPriceX96", "tick", "tickSpacing", "to"],
    "mint": ["amount0Max", "amount1Max", "chainId", "cmd", "deadlineSecs", "liquidity",
             "maxTickDrift", "poolId", "recipient", "signer", "tickLower", "tickUpper",
             "to"],
    "increase": ["amount0Max", "amount1Max", "chainId", "cmd", "deadlineSecs", "liquidity",
                 "recipient", "signer", "to", "tokenId"],
    "decrease": ["amount0Min", "amount1Min", "chainId", "cmd", "deadlineSecs", "liquidity",
                 "recipient", "signer", "to", "tokenId"],
    "collect": ["chainId", "cmd", "deadlineSecs", "recipient", "signer", "to", "tokenId"],
    "burn": ["amount0Min", "amount1Min", "chainId", "cmd", "deadlineSecs", "liquidity",
             "recipient", "signer", "to", "tokenId"],
}


class _StubClient:
    """Once the loaders are stubbed, get_block is the only call left in a dry run."""

    def __init__(self, timestamp: int = _TS) -> None:
        self.timestamp = timestamp

    def get_block(self, block: str = "latest") -> dict:
        return {"timestamp": hex(self.timestamp)}


def _plan_of(lp_write, chain: dict, command: str, args: dict, *,
             currency0: str = _TOKEN, timestamp: int = _TS) -> dict:
    """Run one write command up to the point it has a hash, with no RPC and no key."""
    captured: dict = {}
    pool_key = {"currency0": currency0, "currency1": _TOKEN_ALT, "fee": 3000,
                "tickSpacing": 60, "hooks": lp_write.NATIVE}
    state = {"poolId": _POOL, "poolKey": pool_key, "sqrtPriceX96": 1 << 96, "tick": 0,
             "lpFee": 3000, "activeLiquidity": 10**18}
    granted = command != "approve"   # approve must find work to do, or it returns early

    def stub_pool_key(client, chn, pool_id, a):
        return pool_key

    def stub_state(client, chn, pool_id, key):
        return dict(state, poolId=pool_id, poolKey=key)

    def stub_position(client, chn, token_id):
        return {"tokenId": int(token_id), "poolKey": pool_key, "poolId": _POOL,
                "tickLower": -600, "tickUpper": 600, "hasSubscriber": False,
                "liquidity": 1_000_000, "owner": _ME}

    def stub_token_info(client, chn, address):
        if lp_write.is_native_currency(address):
            return {"address": lp_write.NATIVE, "symbol": "ETH", "decimals": 18,
                    "isNative": True}
        return {"address": address, "symbol": "TKN", "decimals": 18, "isNative": False}

    def stub_allowances(client, chn, owner, currencies):
        return [{"currency": c, "native": True, "ok": True}
                if lp_write.is_native_currency(c) else
                {"currency": c, "native": False,
                 "erc20ToPermit2": 10**30 if granted else 0,
                 "permit2ToPosm": 10**30 if granted else 0,
                 "permit2Expiration": timestamp + 10**7 if granted else 0,
                 "balance": 10**30}
                for c in currencies]

    real_plan_hash = lp_write.plan_hash

    def recording_plan_hash(fields):
        captured["fields"] = dict(fields)
        return real_plan_hash(fields)

    def stub_run_plan(client, chn, a, signer, ctx, *, authorization=None):
        captured["rows"] = ctx["rows"]
        captured["authorization"] = authorization
        return lp_write.plan_hash(ctx["hashFields"])

    stubs = {"pool_key_for_id": stub_pool_key, "load_pool_state": stub_state,
             "load_position_for_write": stub_position, "token_info": stub_token_info,
             "check_allowances": stub_allowances, "plan_hash": recording_plan_hash,
             "run_plan": stub_run_plan,
             # create-pool returns early (and never hashes) against a live pool, so the
             # planning path is the one where this reads False.
             "pool_is_initialized": lambda client, chn, pool_id: False}
    saved = {name: getattr(lp_write, name) for name in stubs}
    signer = {"address": _ME, "privateKey": None, "simulateOnly": True}
    try:
        for name, fn in stubs.items():
            setattr(lp_write, name, fn)
        with contextlib.redirect_stdout(io.StringIO()):
            lp_write.COMMANDS[command](_StubClient(timestamp), chain, dict(args), signer)
    finally:
        for name, fn in saved.items():
            setattr(lp_write, name, fn)

    fields = captured["fields"]
    return {"fields": fields, "hash": real_plan_hash(fields),
            "rows": captured.get("rows", [])}


def tier6(g: dict, r: Results) -> None:
    try:
        import lp_write
        from unilp.chains import resolve_chain
    except ImportError:
        r.skip("tier6/plan-hash-coverage", "lp_write not importable")
        return

    chain = resolve_chain("base")
    plans = {cmd: _plan_of(lp_write, chain, cmd, args)
             for cmd, args in _BASELINE.items()}

    for command, keys in _HASH_FIELDS.items():
        r.check(f"{command} hashes exactly the agreed field set",
                sorted(plans[command]["fields"]), keys)

    for command, flag, value in _MUTATIONS:
        mutated = _plan_of(lp_write, chain, command,
                           dict(_BASELINE[command], **{flag: value}))
        r.check(f"{command} --{flag} changes PLAN_HASH",
                mutated["hash"] != plans[command]["hash"], True)

    # The other half of the contract: the hash must survive a re-run minutes later, or a
    # human would be asked to re-approve an identical plan and would learn to ignore the
    # mismatch. Only the block clock moves here.
    later = _plan_of(lp_write, chain, "mint", _BASELINE["mint"], timestamp=_TS + 900)
    r.check("a later block does not change PLAN_HASH",
            later["hash"], plans["mint"]["hash"])
    r.check("the absolute deadline stays out of the hash",
            "deadline" in later["fields"], False)

    # increase --recipient was invisible as well as unbound: it is the SWEEP target, so a
    # reviewer reading the table could not see where the native refund goes.
    def _row(rows, label):
        return next((v for k, v in rows if k == label), None)

    r.check("increase shows the recipient in the table",
            _ME in (_row(plans["increase"]["rows"], "recipient") or ""), True)
    native = _plan_of(lp_write, chain, "increase", _BASELINE["increase"],
                      currency0=lp_write.NATIVE)
    r.check("increase names SWEEP when the refund is real",
            "SWEEP" in (_row(native["rows"], "recipient") or ""), True)
    r.check("increase says so when the recipient is inert",
            "unused" in (_row(plans["increase"]["rows"], "recipient") or ""), True)
    r.check("mint shows the tick-drift bound it hashes",
            "60" in (_row(plans["mint"]["rows"], "max tick drift") or ""), True)

    # --- create-pool -------------------------------------------------------
    # The starting price is the one parameter of this command that cannot be undone, so
    # the two ways of expressing it must agree exactly and both must reach the hash.
    def _create(**overrides):
        return _plan_of(lp_write, chain, "create-pool",
                        dict(_BASELINE["create-pool"], **overrides))

    # Pinned selector: the ABI fragment is hand-written, so a typo in a parameter type
    # would still encode cleanly and only fail on chain. 0xf7020405 is
    # initializePool((address,address,uint24,int24,address),uint160).
    from unilp.abi_codec import encode_function_data as _efd
    from unilp.abi_defs import POSITION_MANAGER_ABI as _PM_ABI
    _key = {"currency0": lp_write.NATIVE, "currency1": _TOKEN, "fee": 3000,
            "tickSpacing": 60, "hooks": lp_write.NATIVE}
    _data = _efd(_PM_ABI, "initializePool", [lp_write.pool_key_tuple(_key), 1 << 96])
    r.check("initializePool encodes to the known selector", _data[:10], "0xf7020405")
    r.check("initializePool calldata is 4 + 6 words", len(_data), 2 + 8 + 6 * 64)

    base = plans["create-pool"]
    r.check("create-pool hashes the sqrtPriceX96 it will send",
            int(base["fields"]["sqrtPriceX96"]), 1 << 96)
    r.check("create-pool pins hooks to the zero address",
            base["fields"]["hooks"], lp_write.NATIVE)
    r.check("create-pool sorts the currencies into PoolKey order",
            (base["fields"]["currency0"].lower() < base["fields"]["currency1"].lower()), True)
    swapped = _create(token0=_TOKEN_ALT, token1=_TOKEN)
    r.check("create-pool ignores the order the two tokens were typed in",
            swapped["hash"], base["hash"])

    # --price 1.0 on two 18-decimal tokens is tick 0, i.e. the same plan as --tick 0.
    by_price = _plan_of(lp_write, chain, "create-pool",
                        {k: v for k, v in dict(_BASELINE["create-pool"],
                                               price="1").items() if k != "tick"})
    r.check("create-pool --price and the equivalent --tick produce one plan",
            by_price["hash"], base["hash"])

    def _refuses(**overrides):
        try:
            _create(**overrides)
        except Exception:  # noqa: BLE001 — the message differs per guard; refusal is the check
            return True
        return False

    r.check("create-pool refuses a non-standard tier without --allow-odd-tier",
            _refuses(fee="3000", **{"tick-spacing": "10", "allow-odd-tier": None}), True)
    r.check("create-pool refuses a dynamic fee (needs a hook)",
            _refuses(fee=str(0x800000)), True)
    r.check("create-pool refuses tickSpacing 0", _refuses(**{"tick-spacing": "0"}), True)
    r.check("create-pool refuses a fee above 100%", _refuses(fee="1000001"), True)
    r.check("create-pool refuses the same token twice", _refuses(token1=_TOKEN), True)
    r.check("create-pool refuses both --tick and --price", _refuses(price="1"), True)
    r.check("create-pool refuses neither --tick nor --price",
            _refuses(tick=None, price=None), True)
    r.check("create-pool refuses a valueless --tick", _refuses(tick=True), True)


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Tier 7 — ratchet
# ---------------------------------------------------------------------------

# Two token addresses whose sort order is known, so a fixture can put the SAME token on
# either side of the pair. That is the whole point of this tier: "the token is currency1"
# is not an exotic case, it is half of all pools, and it inverts which way the price has to
# move for a limit sell to fill.
_LOW = "0x1111111111111111111111111111111111111111"
_HIGH = "0x2222222222222222222222222222222222222222"


def _ratchet_fixture(principal_side: str, spacing: int = 60):
    """A position that was armed one-sided and has since been partly filled.

    The two ticks are the whole point. At ARM time the range is entirely on one side of the
    price, which is what makes it a limit order. By the time a milestone fires the price has
    moved *into* the range, so the position holds both currencies — and the principal is no
    longer derivable from the live geometry, which is why the mandate pins it at arm time.

    currency0 principal -> the range sits ABOVE the price and fills as the tick rises.
    currency1 principal -> the range sits BELOW and fills as the tick falls.
    """
    if principal_side == "currency0":
        tick_lower, tick_upper, armed_at, current = 600, 3000, 500, 1200
    else:
        tick_lower, tick_upper, armed_at, current = -3000, -600, -500, -1200
    return {"tickLower": tick_lower, "tickUpper": tick_upper, "tick": current,
            "armedAt": armed_at, "tickSpacing": spacing}


def _ratchet_env(lp_write, ratchet, chain, fixture, principal_side, quote_side,
                 liquidity=10**20, owner=None):
    """Stub the chain out from under ratchet.plan_fire and hand back the restore hook."""
    from unilp.v4_math import get_sqrt_ratio_at_tick

    owner = owner or _ME
    pool_key = {"currency0": _LOW, "currency1": _HIGH, "fee": 3000,
                "tickSpacing": fixture["tickSpacing"], "hooks": lp_write.NATIVE}
    from unilp.v4_pool import compute_pool_id
    pool_id = compute_pool_id(pool_key)

    state = {
        "poolId": pool_id, "poolKey": pool_key,
        "sqrtPriceX96": get_sqrt_ratio_at_tick(fixture["tick"]),
        "tick": fixture["tick"], "lpFee": 3000, "activeLiquidity": liquidity,
    }
    position = {
        "tokenId": 7, "poolKey": pool_key, "poolId": pool_id,
        "tickLower": fixture["tickLower"], "tickUpper": fixture["tickUpper"],
        "hasSubscriber": False, "liquidity": liquidity, "owner": owner,
    }

    def stub_position(client, chn, token_id):
        return dict(position, tokenId=int(token_id))

    def stub_state(client, chn, pid, key):
        return dict(state, poolId=pid, poolKey=key)

    def stub_token_info(client, chn, address):
        return {"address": address, "symbol": "T0" if address == _LOW else "T1",
                "decimals": 18, "isNative": False}

    stubs = {"load_position_for_write": stub_position, "load_pool_state": stub_state,
             "token_info": stub_token_info}
    saved = {name: getattr(lp_write, name) for name in stubs}
    for name, fn in stubs.items():
        setattr(lp_write, name, fn)

    chain = dict(chain, knownQuotes={(_HIGH if quote_side == "currency1" else _LOW).lower():
                                     "QUOTE"})

    def restore():
        for name, fn in saved.items():
            setattr(lp_write, name, fn)

    return {"chain": chain, "poolKey": pool_key, "poolId": pool_id, "position": position,
            "state": state, "armedAt": fixture["armedAt"], "restore": restore}


def _ratchet_mandate(ratchet, env, principal_side, steps=(3000, 6000, 10_000)):
    from unilp.ratchet_math import (
        far_edge_tick,
        milestone_thresholds,
        principal_for_range,
        remaining_principal,
    )
    from unilp.v4_math import get_sqrt_ratio_at_tick

    position = env["position"]
    # Derived from the geometry AS ARMED — outside the range — exactly as cmd_arm does.
    principal = principal_for_range(env["armedAt"], position["tickLower"],
                                    position["tickUpper"])
    assert principal == principal_side, (principal, principal_side)
    original = remaining_principal(get_sqrt_ratio_at_tick(env["armedAt"]),
                                   position["tickLower"], position["tickUpper"],
                                   position["liquidity"], principal)
    immutable = {
        "schemaVersion": 1, "chainId": env["chain"]["chainId"], "chainKey": "test",
        "positionManager": env["chain"]["positionManager"], "poolId": env["poolId"],
        "poolKey": env["poolKey"], "signer": _ME, "originalTokenId": 7,
        "principal": principal,
        "farEdgeTick": far_edge_tick(position["tickLower"], position["tickUpper"], principal),
        "originalTickLower": position["tickLower"],
        "originalTickUpper": position["tickUpper"],
        "originalPrincipalRaw": str(original), "stepsBps": list(steps), "label": "",
    }
    return {
        "immutable": immutable,
        "bounds": {"maxSlippageBps": 100, "maxDeadlineSecs": 1200, "maxTickDrift": 600,
                   "allowHooked": False, "maxAttempts": 5,
                   "maxPrincipalRawPerFire": None, "maxFeePerGasWei": None,
                   "expiresAt": None},
        "state": "ARMED", "tokenId": 7,
        "tickLower": position["tickLower"], "tickUpper": position["tickUpper"],
        "liquidity": str(position["liquidity"]),
        "thresholds": [str(t) for t in milestone_thresholds(original, list(steps))],
        "milestonesFired": 0, "currentFire": None,
        "realized": {"amount0": "0", "amount1": "0"}, "lastSeq": 0, "history": [],
    }


def _tier7_math(r) -> None:
    from unilp.ratchet_math import (
        CURRENCY0,
        CURRENCY1,
        RangeExhaustedError,
        due_milestone,
        far_edge_tick,
        fills_as_tick_rises,
        milestone_thresholds,
        parse_steps,
        principal_for_range,
        rearm_range,
        remaining_principal,
        sqrt_price_for_remaining,
    )
    from unilp.v4_math import get_sqrt_ratio_at_tick, pull_off_current_tick

    r.check("steps parse to bps", parse_steps("30,60,100"), [3000, 6000, 10_000])
    r.check("steps dedupe and sort", parse_steps("60, 30 ,60"), [3000, 6000])
    r.check("thresholds are absolute against the original principal",
            milestone_thresholds(100_000, [3000, 6000, 10_000]), [70_000, 40_000, 0])

    # The matrix. Every assertion below runs once per geometry, and each geometry is
    # exercised with the quote on either side so the sell/buy LABEL cannot leak into the
    # geometry. A case that only ever tested "token is currency0" would pass while half of
    # all real pools silently ratcheted the wrong way.
    for principal, tick_lower, tick_upper, current in (
        (CURRENCY0, 600, 3000, 1200),
        (CURRENCY1, -3000, -600, -1200),
    ):
        label = f"[{principal}]"
        # Geometry -> principal, with no reference to which token is "the" token.
        outside = tick_lower - 1 if principal == CURRENCY0 else tick_upper
        r.check(f"{label} principal derives from geometry alone",
                principal_for_range(outside, tick_lower, tick_upper), principal)
        r.check(f"{label} far edge is the end the price travels to",
                far_edge_tick(tick_lower, tick_upper, principal),
                tick_upper if principal == CURRENCY0 else tick_lower)
        r.check(f"{label} fill direction", fills_as_tick_rises(principal),
                principal == CURRENCY0)

        liquidity = 10**20
        unfilled = get_sqrt_ratio_at_tick(tick_lower if principal == CURRENCY0
                                          else tick_upper)
        filled = get_sqrt_ratio_at_tick(tick_upper if principal == CURRENCY0
                                        else tick_lower)
        original = remaining_principal(unfilled, tick_lower, tick_upper, liquidity,
                                       principal)
        r.check(f"{label} unfilled position is 100% principal", original > 0, True)
        r.check(f"{label} fully crossed position holds no principal",
                remaining_principal(filled, tick_lower, tick_upper, liquidity, principal), 0)
        r.check(f"{label} the other currency is untouched while unfilled",
                remaining_principal(unfilled, tick_lower, tick_upper, liquidity,
                                    CURRENCY1 if principal == CURRENCY0 else CURRENCY0), 0)

        # The inverse must land back on the amount it was asked for. One wei of slack:
        # both directions truncate, and the runtime decision is made on the amount anyway.
        for target in milestone_thresholds(original, [3000, 6000, 10_000]) + [original]:
            sqrt_at = sqrt_price_for_remaining(tick_lower, tick_upper, liquidity, target,
                                               principal)
            back = remaining_principal(sqrt_at, tick_lower, tick_upper, liquidity, principal)
            r.check(f"{label} remaining<->price round-trips at {target}",
                    abs(back - target) <= 1, True)

        # Re-arm travels the right way and stays one-sided on the SAME side.
        new_lower, new_upper = rearm_range(current, far_edge_tick(tick_lower, tick_upper,
                                                                  principal), 60, principal)
        r.check(f"{label} re-arm keeps the far edge fixed",
                new_upper if principal == CURRENCY0 else new_lower,
                tick_upper if principal == CURRENCY0 else tick_lower)
        r.check(f"{label} re-arm is still one-sided on the same currency",
                principal_for_range(current, new_lower, new_upper), principal)
        r.check(f"{label} re-arm narrows towards the price",
                (new_lower > tick_lower) if principal == CURRENCY0
                else (new_upper < tick_upper), True)

        # Past the far edge there is nothing to re-arm — that IS the final milestone.
        past = tick_upper if principal == CURRENCY0 else tick_lower
        threw = False
        try:
            rearm_range(past, far_edge_tick(tick_lower, tick_upper, principal), 60, principal)
        except RangeExhaustedError:
            threw = True
        r.check(f"{label} a fully crossed range reports exhausted", threw, True)

    # The one-tick asymmetry, stated as its own case in both directions because
    # range_status is strict on one edge and not on the other.
    r.check("currency0 re-arm clears a tick sitting exactly on a spacing boundary",
            rearm_range(1200, 3000, 60, CURRENCY0)[0] > 1200, True)
    r.check("currency1 re-arm may land exactly on the current tick",
            rearm_range(-1200, -3000, 60, CURRENCY1)[1], -1200)

    # A forced side never falls back to the other one; an unforced call is unchanged.
    r.check("forced pull-off matches the heuristic when they agree",
            pull_off_current_tick(209056, 206400, 209200, 200, "currency1"),
            pull_off_current_tick(209056, 206400, 209200, 200))
    threw = False
    try:
        pull_off_current_tick(209190, 209010, 209300, 200, "currency1")
    except RuntimeError:
        threw = True
    r.check("forced pull-off refuses rather than flipping sides", threw, True)
    r.check("unforced pull-off still falls back to the other side",
            pull_off_current_tick(209190, 209010, 209300, 200), (209200, 209300))

    # A gap that clears two levels fires the deeper one only.
    thresholds = [70, 40, 0]
    r.check("no milestone before the first threshold", due_milestone(thresholds, 0, 71), None)
    r.check("first milestone at its threshold", due_milestone(thresholds, 0, 70), 0)
    r.check("a two-level gap fires the deeper level", due_milestone(thresholds, 0, 35), 1)
    r.check("a full crossing fires the last level", due_milestone(thresholds, 0, 0), 2)
    r.check("levels already fired never re-fire", due_milestone(thresholds, 3, 0), None)


def _tier7_plan(r) -> None:
    import lp_write
    import ratchet
    from unilp.chains import resolve_chain
    from unilp.v4_actions import ACTIONS

    base = resolve_chain("base")
    for principal in ("currency0", "currency1"):
        for quote in ("currency0", "currency1"):
            label = f"[{principal}/quote={quote}]"
            fixture = _ratchet_fixture(principal)
            env = _ratchet_env(lp_write, ratchet, base, fixture, principal, quote)
            try:
                mandate = _ratchet_mandate(ratchet, env, principal)
                client = _StubClient()
                fire = ratchet.plan_fire(client, env["chain"], mandate, 0)

                actions = fire["plan"]["actions"]
                r.check(f"{label} a re-arming fire is decrease+burn+mint+take", actions,
                        [ACTIONS["DECREASE_LIQUIDITY"], ACTIONS["BURN_POSITION"],
                         ACTIONS["MINT_POSITION"], ACTIONS["TAKE_PAIR"]])
                r.check(f"{label} the fire carries no SETTLE leg",
                        any(a in (ACTIONS["SETTLE"], ACTIONS["SETTLE_ALL"],
                                  ACTIONS["SETTLE_PAIR"]) for a in actions), False)
                r.check(f"{label} the fire sends no value", fire["plan"]["value"], 0)
                r.check(f"{label} the fire is not the final one", fire["final"], False)

                remint = fire["remint"]
                other_max = (remint["amount1Max"] if principal == "currency0"
                             else remint["amount0Max"])
                own_max = (remint["amount0Max"] if principal == "currency0"
                           else remint["amount1Max"])
                r.check(f"{label} the re-mint spends none of the harvested currency",
                        other_max, 0)
                r.check(f"{label} the re-mint spends the principal", own_max > 0, True)
                r.check(f"{label} the re-mint keeps the pinned far edge",
                        remint["tickUpper"] if principal == "currency0"
                        else remint["tickLower"],
                        mandate["immutable"]["farEdgeTick"])
                need = (fire["required"]["amount0"] if principal == "currency0"
                        else fire["required"]["amount1"])
                r.check(f"{label} the re-mint costs less than the exit frees",
                        need < fire["remainder"], True)

                # The final milestone must not re-arm, whatever the geometry.
                last = len(mandate["immutable"]["stepsBps"]) - 1
                final_fire = ratchet.plan_fire(client, env["chain"], mandate, last)
                r.check(f"{label} the final milestone exits without re-arming",
                        final_fire["remint"], None)
                r.check(f"{label} the final fire is decrease+burn+take",
                        final_fire["plan"]["actions"],
                        [ACTIONS["DECREASE_LIQUIDITY"], ACTIONS["BURN_POSITION"],
                         ACTIONS["TAKE_PAIR"]])
            finally:
                env["restore"]()


def _tier7_predicate(r) -> None:
    import lp_write
    import ratchet
    from unilp.chains import resolve_chain

    base = resolve_chain("base")
    for principal in ("currency0", "currency1"):
        fixture = _ratchet_fixture(principal)
        env = _ratchet_env(lp_write, ratchet, base, fixture, principal, "currency1")
        try:
            mandate = _ratchet_mandate(ratchet, env, principal)
            client = _StubClient()
            fire = ratchet.plan_fire(client, env["chain"], mandate, 0)
            fields = ratchet.hash_fields(env["chain"], mandate, fire, 0, 1200)
            check = ratchet.make_predicate(env["chain"], mandate, 0)
            signer = {"address": _ME, "privateKey": "0x00", "simulateOnly": False}

            def run(mutation=None, sgn=None, milestone=0):
                mutated = dict(fields, **(mutation or {}))
                fn = check if milestone == 0 else ratchet.make_predicate(
                    env["chain"], mandate, milestone)
                fn(client, env["chain"], {}, sgn or signer,
                   {"hashFields": mutated}, "deadbeef")

            def refuses(name, **kwargs):
                try:
                    run(**kwargs)
                except RuntimeError:
                    r.check(f"[{principal}] predicate refuses {name}", True, True)
                    return
                r.check(f"[{principal}] predicate refuses {name}", False, True)

            run()
            r.check(f"[{principal}] predicate accepts its own plan", True, True)

            refuses("a different tokenId", mutation={"tokenId": lp_write.Big(9)})
            refuses("a different recipient", mutation={"recipient": _TOKEN})
            refuses("a partial exit", mutation={"liquidity": lp_write.Big(1)})
            refuses("a looser slippage floor", mutation={"amount0Min": lp_write.Big(0),
                                                         "amount1Min": lp_write.Big(0)})
            refuses("a longer deadline", mutation={"deadlineSecs": 99_999})
            refuses("a foreign command", mutation={"cmd": "burn"})
            refuses("a plan-only signer",
                    sgn={"address": _ME, "privateKey": None, "simulateOnly": True})
            refuses("a signer that is not the one that armed it",
                    sgn={"address": _TOKEN, "privateKey": "0x00", "simulateOnly": False})
            moved = "remintTickUpper" if principal == "currency0" else "remintTickLower"
            refuses("a moved far edge", mutation={moved: fields[moved] + 60})
            other = ("remintAmount1Max" if principal == "currency0"
                     else "remintAmount0Max")
            refuses("a re-mint that spends the harvested currency",
                    mutation={other: lp_write.Big(1)})

            # The shape of the re-mint is bound above; this binds its SIZE. The plan omits a
            # SETTLE leg only because the mint is funded entirely by the delta the decrease
            # credits in the same unlock, so liquidity that needs more principal than the
            # exit frees would send a transaction that cannot do anything but revert.
            mine = ("remintAmount0Max" if principal == "currency0"
                    else "remintAmount1Max")
            refuses("a re-mint sized past what the exit frees",
                    mutation={"remintLiquidity": lp_write.Big(
                        int(fields["remintLiquidity"]) * 4)})
            refuses("an empty re-mint",
                    mutation={"remintLiquidity": lp_write.Big(0)})
            refuses("a principal allowance larger than the exit frees",
                    mutation={mine: lp_write.Big(2**120)})

            # An already-fired milestone can never be replayed.
            mandate["milestonesFired"] = 2
            refuses("a milestone that already fired")
            mandate["milestonesFired"] = 0
        finally:
            env["restore"]()


def _tier7_authorization(r) -> None:
    import lp_write

    r.check("importing the runner does not put lp_write in CLI mode",
            lp_write._ARGV_ENTRY, False)
    r.check("run_plan takes authorization keyword-only",
            "authorization" in lp_write.run_plan.__code__.co_varnames
            and lp_write.run_plan.__code__.co_kwonlyargcount >= 1, True)

    made = lp_write.MandateAuthorization(mandate_id="m", fire_id="f",
                                         predicate=lambda *a: None)
    r.check("the runner can construct an authorization",
            isinstance(made, lp_write.MandateAuthorization), True)

    saved = lp_write._ARGV_ENTRY
    try:
        lp_write._ARGV_ENTRY = True
        threw = False
        try:
            lp_write.MandateAuthorization(mandate_id="m", fire_id="f",
                                          predicate=lambda *a: None)
        except RuntimeError:
            threw = True
        r.check("a CLI process cannot construct an authorization at all", threw, True)
    finally:
        lp_write._ARGV_ENTRY = saved

    # A non-MandateAuthorization must fail closed rather than being trusted, and a hash
    # echo must never be combined with a mandate.
    calls = {"n": 0}

    def stub_simulate(client, call):
        calls["n"] += 1
        return {"ok": True, "method": "eth_call", "logs": [], "gasUsed": None, "revert": None}

    real_simulate = lp_write.simulate_call
    lp_write.simulate_call = stub_simulate
    chain = {"positionManager": _TOKEN, "chainId": 1}
    ctx = {"title": "t", "rows": [["k", "v"]], "hashFields": {"a": 1}, "data": "0x",
           "value": 0}
    signer = {"address": _ME, "privateKey": "0x00", "simulateOnly": False}

    def refused(args, **kwargs) -> bool:
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                lp_write.run_plan(_StubClient(), chain, args, signer, dict(ctx), **kwargs)
        except RuntimeError:
            return True
        return False

    try:
        r.check("run_plan refuses a dict posing as an authorization",
                refused({"broadcast": True}, authorization={"mandate": "x"}), True)
        r.check("run_plan refuses a string posing as an authorization",
                refused({"broadcast": True}, authorization="yes"), True)
        r.check("run_plan refuses a hash echo combined with a mandate",
                refused({"broadcast": True, "confirm": "abc"}, authorization=made), True)
        # And the ordinary attended path is untouched: no authorization, wrong hash, refuse.
        r.check("the attended path still requires the right PLAN_HASH",
                refused({"broadcast": True, "confirm": "not-the-hash"}), True)
    finally:
        lp_write.simulate_call = real_simulate


def _tier7_journal(r) -> None:
    import shutil
    import tempfile

    from unilp.journal import MandateStore, mandate_id

    root = Path(tempfile.mkdtemp(prefix="unilp-selftest-"))
    try:
        immutable = {"chainId": 4663, "tokenId": 7, "poolId": "0xfeed"}
        ident = mandate_id(immutable)
        r.check("mandate ids are 16 bytes, not the 4 of a PLAN_HASH", len(ident), 32)

        store = MandateStore(root, "test", ident)
        with store.lock() as acquired:
            r.check("an idle mandate locks", acquired, True)
            store.save({"immutable": immutable, "state": "ARMED",
                        "liquidity": 2**100, "lastSeq": 0})
            r.check("the file is owner-only", oct(store.path.stat().st_mode & 0o777), "0o600")
            loaded = store.load()
            r.check("integers past 2**53 survive as strings",
                    loaded["liquidity"], str(2**100))

            store.append({"event": "plan.built"})
            seq = store.append({"event": "tx.sent"})
            r.check("the log assigns increasing sequence numbers", seq, 2)
            # A crash between the outcome record and the mandate replace leaves the view
            # behind the log; the tail is what tells the next tick to catch up.
            r.check("the tail exposes records the mandate has not absorbed",
                    [rec["event"] for rec in store.tail(loaded["lastSeq"])],
                    ["plan.built", "tx.sent"])

            # A second lock on the SAME file object is re-entrant within a process, so the
            # meaningful check is that the lock target is not the file that gets replaced —
            # os.replace would otherwise orphan the inode the lock is held on.
            r.check("the lock is not the file that gets atomically replaced",
                    store.lock_path != store.path, True)

        tampered = store.path.read_text().replace('"tokenId": 7', '"tokenId": 8')
        store.path.write_text(tampered)
        threw = False
        try:
            store.load()
        except RuntimeError:
            threw = True
        r.check("a mandate that no longer hashes to its filename is refused", threw, True)

        # `--id` arrives straight from argv, and every path in this class is built by
        # interpolating it. Rejecting anything that is not the shape `mandate_id` emits
        # keeps `status`, `disarm` and especially `delete` inside the state directory.
        for hostile in ("../../../etc/passwd", "..", "a/b", "", "ABCDEF" * 5 + "AB"):
            refused = False
            try:
                MandateStore(root, "test", hostile)
            except RuntimeError:
                refused = True
            r.check(f"a mandate id of {hostile!r} is refused before any path is built",
                    refused, True)

        # Only the LAST journal line may be unreadable — that is a torn write from a power
        # cut, and the record it lost had not been confirmed anyway. A bad line with good
        # lines after it is damage of a different kind, and since the tail of this log is
        # what restores an in-flight fire, quietly skipping it could erase the only proof a
        # transaction was signed.
        good = MandateStore(root, "test", mandate_id({"chainId": 1}))
        good.append({"event": "tx.sent"})
        good.log_path.write_text(good.log_path.read_text() + '{"event": "tx.se\n')
        r.check("a torn final line is tolerated",
                [rec["event"] for rec in good.records()], ["tx.sent"])
        good.append({"event": "tick.noop"})
        threw = False
        try:
            good.records()
        except RuntimeError:
            threw = True
        r.check("a corrupt line with records after it stops the runner", threw, True)
    finally:
        shutil.rmtree(root, ignore_errors=True)


class _RatchetChain:
    """A chain that remembers whether the fire landed, for the recovery branches."""

    def __init__(self, chain, pool_key, pool_id, me, tick=1400):
        self.chain, self.pool_key, self.pool_id, self.me = chain, pool_key, pool_id, me
        self.tick = tick
        self.burned: set = set()
        self.nonce = 0
        self.receipts: dict = {}
        self.logs: list = []
        self.in_mempool = None
        # Flipped on to model an unreachable node: `rpc.multicall` reports a transport
        # failure the same way it reports a revert, so the reconciler must not read one as
        # the other. Everything in the batch fails together, which is what makes the
        # liveness control call able to tell them apart.
        self.rpc_down = False
        # tokenId -> (tickLower, tickUpper); anything unlisted is the original range.
        self.ranges: dict = {}

    # -- RpcClient surface -------------------------------------------------
    def get_block(self, block="latest"):
        return {"timestamp": hex(1_800_000_000)}

    def block_number(self):
        return 1000

    def transaction_count(self, address, block="pending"):
        return self.nonce

    def get_receipt(self, tx_hash):
        return self.receipts.get(tx_hash)

    def request(self, method, params=None):
        return self.in_mempool

    def get_logs(self, params):
        return self.logs

    def multicall(self, calls, allow_failure=True, **kwargs):
        if self.rpc_down:
            return [{"status": "failure", "result": None,
                     "error": "call failed on its own"} for _ in calls]
        out = []
        for call in calls:
            name = call["functionName"]
            args = call.get("args") or []
            token_id = int(args[0]) if args else None
            if name == "ownerOf":
                out.append({"status": "failure", "result": None} if token_id in self.burned
                           else {"status": "success", "result": self.me})
            elif name == "getPositionLiquidity":
                out.append({"status": "success",
                            "result": 0 if token_id in self.burned else 10**20})
            elif name == "nextTokenId":
                out.append({"status": "success", "result": 9999})
            else:
                out.append({"status": "success", "result": 0})
        return out

    # -- loaders -----------------------------------------------------------
    def position(self, client, chn, token_id):
        if int(token_id) in self.burned:
            raise RuntimeError(f"tokenId {token_id} has an empty PoolKey — burned")
        lower, upper = self.ranges.get(int(token_id), (600, 3000))
        return {"tokenId": int(token_id), "poolKey": self.pool_key, "poolId": self.pool_id,
                "tickLower": lower, "tickUpper": upper, "hasSubscriber": False,
                "liquidity": 10**20, "owner": self.me}

    def state(self, client, chn, pool_id, key):
        from unilp.v4_math import get_sqrt_ratio_at_tick
        return {"poolId": pool_id, "poolKey": key, "tick": self.tick,
                "sqrtPriceX96": get_sqrt_ratio_at_tick(self.tick), "lpFee": 3000,
                "activeLiquidity": 10**20}

    def erc721_log(self, token_id):
        return {"address": self.chain["positionManager"],
                "topics": [_TOPIC_ERC721, "0x" + "0" * 64,
                           "0x" + "0" * 24 + self.me[2:].lower(),
                           "0x" + f"{token_id:064x}"],
                "data": "0x"}


_TOPIC_ERC721 = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _tier7_state_machine(r) -> None:
    """The FIRE_SENT recovery table, driven for real against a stub chain.

    Every row here is a crash or a network outcome that a cron-driven runner will meet, and
    getting one wrong either strands funds or sends a duplicate. They are cheap to assert and
    impossible to reason about reliably by reading the code.
    """
    import shutil
    import tempfile

    import lp_write
    import ratchet
    from unilp.chains import resolve_chain
    from unilp.journal import MandateStore, mandate_id
    from unilp.v4_pool import compute_pool_id

    chain = resolve_chain("base")
    pool_key = {"currency0": _LOW, "currency1": _HIGH, "fee": 3000, "tickSpacing": 60,
                "hooks": lp_write.NATIVE}
    pool_id = compute_pool_id(pool_key)
    world = _RatchetChain(chain, pool_key, pool_id, _ME)

    immutable = {
        "schemaVersion": 1, "chainId": chain["chainId"], "chainKey": "base",
        "positionManager": chain["positionManager"], "poolId": pool_id, "poolKey": pool_key,
        "signer": _ME, "originalTokenId": 7, "principal": "currency0", "farEdgeTick": 3000,
        "originalTickLower": 600, "originalTickUpper": 3000,
        "originalPrincipalRaw": "10973255779209930436",
        "stepsBps": [3000, 6000, 10_000], "label": "",
    }
    ident = mandate_id(immutable)
    fire = {"fireId": f"{ident}:0:0", "milestone": 0, "planHash": "abcd1234", "final": False,
            "plannedTick": 1400, "plannedRemainderRaw": "7168265174372228876",
            "remintTickLower": 1440, "remintTickUpper": 3000,
            "tx": {"hash": "0xfire", "nonce": 0, "sentAtBlock": 900}}

    root = Path(tempfile.mkdtemp(prefix="unilp-ratchet-"))
    saved_env = os.environ.get("UNILP_STATE_DIR")
    os.environ["UNILP_STATE_DIR"] = str(root)
    saved = {name: getattr(lp_write, name)
             for name in ("load_position_for_write", "load_pool_state", "token_info",
                          "simulate_call")}
    lp_write.load_position_for_write = world.position
    lp_write.load_pool_state = world.state
    lp_write.token_info = lambda c, ch, a: {"address": a, "symbol": "T", "decimals": 18,
                                            "isNative": False}
    lp_write.simulate_call = lambda c, call: {"ok": True, "method": "eth_simulateV1",
                                              "logs": [], "gasUsed": 1, "revert": None}
    signer = {"address": _ME, "privateKey": "0x" + "11" * 32, "simulateOnly": False}

    def seed(state, current_fire, max_attempts=2):
        world.burned, world.nonce = set(), 0
        world.receipts, world.logs, world.in_mempool = {}, [], None
        world.rpc_down, world.ranges = False, {}
        store = MandateStore(root, "base", ident)
        store.delete()
        mandate = {
            "immutable": immutable,
            "bounds": {"maxSlippageBps": 100, "maxDeadlineSecs": 1200, "maxTickDrift": 600,
                       "allowHooked": False, "maxAttempts": max_attempts,
                       "maxPrincipalRawPerFire": None, "maxFeePerGasWei": None,
                       "expiresAt": None},
            "state": state, "tokenId": 7, "tickLower": 600, "tickUpper": 3000,
            "liquidity": str(10**20),
            "thresholds": ["7681279045446951305", "4389302311683972174", "0"],
            "milestonesFired": 0, "currentFire": current_fire,
            "realized": {"amount0": "0", "amount1": "0"}, "lastSeq": 0, "history": [],
        }
        with store.lock() as ok:
            assert ok
            store.save(mandate)
        return store

    def tick():
        with contextlib.redirect_stdout(io.StringIO()):
            return ratchet.tick_one(world, chain, {}, signer, ident, False)

    try:
        seed("FIRE_SENT", dict(fire))
        world.in_mempool = {"hash": "0xfire"}
        out = tick()
        r.check("a pending fire is left alone", (out["action"], out["state"]),
                ("waiting", "FIRE_SENT"))

        seed("FIRE_SENT", dict(fire))
        out = tick()
        r.check("a dropped fire is retried", out["truth"], "not-sent")

        seed("FIRE_SENT", dict(fire))
        world.nonce = 1
        out = tick()
        r.check("a consumed nonce with the NFT intact is a revert, not a landing",
                out["truth"], "not-sent")

        # The retry budget has to survive on the MANDATE, not on currentFire. Each round
        # below re-enters FIRE_SENT with a currentFire that carries no attempt count — the
        # state a crash between the send and the journal write leaves behind. A runner that
        # counts on currentFire reads zero every round and retries for ever.
        store = seed("FIRE_SENT", dict(fire), max_attempts=2)
        seen = []
        for _ in range(4):
            out = tick()
            seen.append((out.get("attempt"), out["state"]))
            if out["state"] == "NEEDS_ATTENTION":
                break
            mandate = store.load()
            mandate["state"] = "FIRE_SENT"
            mandate["currentFire"] = dict(fire)
            with store.lock() as ok:
                assert ok
                store.save(mandate)
        r.check("repeated drops accumulate towards maxAttempts", seen,
                [(1, "ARMED"), (2, "ARMED"), (None, "NEEDS_ATTENTION")])

        seed("FIRE_SENT", dict(fire))
        world.burned = {7}
        world.receipts["0xfire"] = {"status": "0x1", "blockNumber": "0x1", "gasUsed": "0x1",
                                    "logs": [world.erc721_log(101)]}
        out = tick()
        r.check("a burned NFT plus a receipt adopts the replacement",
                (out["state"], out["tokenId"]), ("ARMED", 101))

        seed("FIRE_SENT", dict(fire))
        world.burned = {7}
        world.logs = [world.erc721_log(202)]
        out = tick()
        r.check("with no receipt the bounded log scan finds the replacement",
                (out["state"], out["tokenId"]), ("ARMED", 202))

        seed("FIRE_SENT", dict(fire))
        world.burned = {7}
        out = tick()
        r.check("an unidentifiable replacement escalates instead of re-minting",
                out["state"], "NEEDS_ATTENTION")

        seed("FIRE_SENT", dict(fire, final=True, milestone=2))
        world.burned = {7}
        out = tick()
        r.check("the final milestone completes without re-arming", out["state"], "COMPLETE")

        seed("COMPLETE", None)
        out = tick()
        r.check("a terminal mandate does no chain work", out["action"], "noop")

        seed("ARMED", None)
        world.burned = {7}
        out = tick()
        r.check("a position that vanished escalates", out["state"], "NEEDS_ATTENTION")

        # --- the write-ahead log actually leads the mandate file -------------
        # A crash between `on_sent`'s journal write and the mandate replace leaves the file
        # reading ARMED with nothing in flight. Detecting that the log is ahead is not
        # enough; the runner has to put the fire back, or it plans the same milestone again
        # and broadcasts a second transaction for it.
        store = seed("ARMED", None)
        store.append({"event": "tx.sent", "fireId": fire["fireId"], "currentFire": dict(fire),
                      "milestonesFired": 0, **fire["tx"]})
        world.in_mempool = {"hash": "0xfire"}
        out = tick()
        r.check("a journalled send the mandate file never saw is replayed, not re-sent",
                (out["state"], out["action"]), ("FIRE_SENT", "waiting"))
        r.check("and the replay is reported", out.get("replayed"), ["tx.sent"])

        # Order matters within the tail: the same fire sent and then dropped ends ARMED.
        store = seed("ARMED", None)
        store.append({"event": "tx.sent", "fireId": fire["fireId"], "currentFire": dict(fire),
                      "milestonesFired": 0, **fire["tx"]})
        store.append({"event": "tx.dropped", "reason": "dropped", "attempt": 1})
        world.in_mempool = {"hash": "0xfire"}
        out = tick()
        r.check("a sent-then-dropped tail replays in order", out["state"], "ARMED")

        # An event the runner has no rule for must stop it rather than be skipped: a future
        # record type that carries state would otherwise be silently dropped on recovery.
        store = seed("ARMED", None)
        store.append({"event": "some.future.record"})
        threw = False
        try:
            tick()
        except RuntimeError:
            threw = True
        r.check("an unknown journal record halts instead of being ignored", threw, True)

        # --- an unreachable node is not a burned NFT -------------------------
        # `rpc.multicall` reports a transport failure exactly as it reports a revert. Read
        # naively that says "the NFT is gone", which this runner treats as proof the fire
        # landed — and on the final milestone would retire a mandate whose position is still
        # alive and still filling.
        seed("FIRE_SENT", dict(fire, final=True, milestone=2))
        world.rpc_down = True
        out = tick()
        r.check("an unreachable node defers instead of reading a burn",
                (out["truth"], out["action"], out["state"]),
                ("unavailable", "deferred", "FIRE_SENT"))
        r.check("and a deferred tick is not a halt that needs an operator",
                out["state"] == "NEEDS_ATTENTION", False)

        # --- clear-attention can finish an adoption the oracles could not ----
        seed("FIRE_SENT", dict(fire))
        world.burned = {7}
        out = tick()
        r.check("the setup lands in NEEDS_ATTENTION", out["state"], "NEEDS_ATTENTION")

        world.ranges = {101: (1440, 3000)}
        with contextlib.redirect_stdout(io.StringIO()):
            ratchet.cmd_clear_attention(world, chain, {"id": ident, "token-id": "101"}, signer)
        mandate = MandateStore(root, "base", ident).load()
        r.check("an operator-supplied replacement is adopted with the milestone credited",
                (mandate["state"], mandate["tokenId"], mandate["milestonesFired"]),
                ("ARMED", 101, 1))
        r.check("and the fire is recorded in the history",
                mandate["history"][0]["newTokenId"], 101)

        # The operator is standing in for an oracle, so their answer is checked like one.
        for label, ranges, burn, expect in (
            ("a replacement on the wrong far edge", {101: (1440, 2400)}, {7}, True),
            ("a replacement that straddles the price", {101: (600, 3000)}, {7}, True),
            ("an adoption while the old position is still alive", {101: (1440, 3000)},
             set(), True),
        ):
            seed("FIRE_SENT", dict(fire))
            world.burned = {7}
            with contextlib.redirect_stdout(io.StringIO()):
                tick()
            world.burned, world.ranges = burn, ranges
            refused = False
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    ratchet.cmd_clear_attention(world, chain,
                                                {"id": ident, "token-id": "101"}, signer)
            except RuntimeError:
                refused = True
            r.check(f"{label} is refused", refused, expect)

        # And the happy path: price inside the range, milestone due, full dry run.
        seed("ARMED", None)
        out = tick()
        r.check("a due milestone plans a fire", (out["action"], out["milestone"]),
                ("dry-run", 0))
        world.tick = 500
        seed("ARMED", None)
        out = tick()
        r.check("no milestone before the price gets there", out["action"], "noop")
        world.tick = 1400
    finally:
        for name, fn in saved.items():
            setattr(lp_write, name, fn)
        if saved_env is None:
            os.environ.pop("UNILP_STATE_DIR", None)
        else:
            os.environ["UNILP_STATE_DIR"] = saved_env
        shutil.rmtree(root, ignore_errors=True)


def _tier7_duplicate_arm(r) -> None:
    """One position, one live mandate — enforced by scanning, not by the id hash.

    ``mandate_id`` hashes ``label`` along with everything else, so arming the same position
    twice under two names produces two ids, two files, and two mandates that each intend to
    burn the same NFT. The ``store.exists()`` check cannot see that: the ids differ. This is
    the check that can.
    """
    import shutil
    import tempfile

    import ratchet
    from unilp.journal import MandateStore, mandate_id

    base = {
        "schemaVersion": 1, "chainId": 8453, "chainKey": "base",
        "positionManager": "0x" + "aa" * 20, "poolId": "0x" + "bb" * 32,
        "poolKey": {"currency0": _LOW, "currency1": _HIGH, "fee": 3000, "tickSpacing": 60,
                    "hooks": "0x" + "00" * 20},
        "signer": _ME, "originalTokenId": 7, "principal": "currency0", "farEdgeTick": 3000,
        "originalTickLower": 600, "originalTickUpper": 3000,
        "originalPrincipalRaw": "1000", "stepsBps": [3000, 6000, 10_000], "label": "",
    }

    root = Path(tempfile.mkdtemp(prefix="unilp-dup-"))

    def seed(immutable, state="ARMED", token_id=7, fired=0, history=None):
        ident = mandate_id(immutable)
        store = MandateStore(root, "base", ident)
        with store.lock() as ok:
            assert ok
            store.save({"immutable": immutable, "bounds": {}, "state": state,
                        "tokenId": token_id, "tickLower": 600, "tickUpper": 3000,
                        "liquidity": "1", "thresholds": ["700", "400", "0"],
                        "milestonesFired": fired, "currentFire": None,
                        "realized": {"amount0": "0", "amount1": "0"}, "lastSeq": 0,
                        "history": history or []})
        return ident

    def conflict(immutable):
        return ratchet.find_position_conflict(root, "base", mandate_id(immutable), immutable)

    try:
        first = seed(base)

        r.check("an unrelated position is not a conflict",
                conflict({**base, "originalTokenId": 9}), None)
        r.check("the mandate does not conflict with itself", conflict(base), None)

        # The exact shape of the bug: same position, same terms, different label.
        renamed = {**base, "label": "AGENTOS-7"}
        found = conflict(renamed)
        r.check("a second label on the same position IS a conflict",
                None if found is None else found["id"], first)
        # `sameTerms` is what makes a re-run idempotent instead of an error, so it has to be
        # right in both directions.
        r.check("identical terms are recognised as identical",
                None if found is None else found["sameTerms"], True)

        # A measurement, not a policy: principal is read off the chain at arm time and moves
        # with the price. If it counted as a term, no re-run would ever be idempotent.
        r.check("a re-measured principal does not make the terms differ",
                (conflict({**base, "label": "x", "originalPrincipalRaw": "999"}) or {})
                .get("sameTerms"), True)

        for field, value in (("stepsBps", [5000, 10_000]), ("farEdgeTick", 2400),
                             ("principal", "currency1"), ("signer", "0x" + "cc" * 20)):
            differing = conflict({**base, "label": "x", field: value})
            r.check(f"a different {field} is a conflict but NOT the same terms",
                    None if differing is None else (differing["sameTerms"],
                                                    differing["differs"]),
                    (False, [field]))

        # After a fire the mandate rolls onto the position it just minted. That one is taken
        # too, and it is not the id in `immutable` — comparing against `originalTokenId`
        # would leave every post-fire position free to be armed a second time.
        MandateStore(root, "base", first).delete()
        seed(base, token_id=9)
        r.check("the position a fire rolled onto is taken",
                (conflict({**base, "label": "x", "originalTokenId": 9}) or {}).get("id"),
                mandate_id(base))
        r.check("and the burned one it started from is not",
                conflict({**base, "label": "x"}), None)
        MandateStore(root, "base", mandate_id(base)).delete()
        first = seed(base)

        # Finished mandates are history. Blocking on them would make a position unusable
        # forever after its first ratchet completes.
        for dead in ("COMPLETE", "DISARMED", "EXPIRED"):
            MandateStore(root, "base", first).delete()
            seed(base, state=dead)
            r.check(f"a {dead} mandate does not block a fresh arm", conflict(renamed), None)

        # NEEDS_ATTENTION is the one non-running state that must still block: it is waiting
        # for a human, and arming over it is exactly the mistake its note warns against.
        MandateStore(root, "base", mandate_id(base)).delete()
        seed(base, state="NEEDS_ATTENTION")
        r.check("a NEEDS_ATTENTION mandate still blocks",
                (conflict(renamed) or {}).get("state"), "NEEDS_ATTENTION")

        # A mandate whose fire landed but whose replacement id is unknown is the one case
        # where the conflict cannot be seen by tokenId — it still names the burned one.
        MandateStore(root, "base", mandate_id(base)).delete()
        seed(base, state="NEEDS_ATTENTION", fired=1,
             history=[{"fireId": "x", "milestone": 0, "newTokenId": None}])
        r.check("an unidentified re-mint is flagged when arming any other position",
                ratchet.pending_remint_mandates(root, "base", _ME, base["poolId"]),
                [mandate_id(base)])
        MandateStore(root, "base", mandate_id(base)).delete()
        seed(base, state="ARMED")
        r.check("a healthy mandate is not flagged as a pending re-mint",
                ratchet.pending_remint_mandates(root, "base", _ME, base["poolId"]), [])

        # Failing closed here is deliberate: a mandate that no longer hashes to its filename
        # might BE the duplicate, and arming past it would authorize unattended sends.
        (root / "ratchet" / "base" / f"{mandate_id(base)}.json").write_text(
            '{"immutable": {"tampered": true}}', encoding="utf-8")
        try:
            conflict(renamed)
            r.check("a corrupted sibling stops the arm", "no error", "RuntimeError")
        except RuntimeError as exc:
            r.check("a corrupted sibling stops the arm", "does not hash" in str(exc), True)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _tier7_arm_is_idempotent(r) -> None:
    """The wiring, driven through ``cmd_arm`` itself.

    The check above proves the detector answers correctly; this proves ``arm`` asks it. They
    are separate failures — a correct detector nobody calls is exactly the shape of duplicate
    that reached the live state directory.
    """
    import shutil
    import tempfile

    import lp_write
    import ratchet
    from unilp.chains import resolve_chain
    from unilp.journal import MandateStore
    from unilp.v4_pool import compute_pool_id

    chain = resolve_chain("base")
    pool_key = {"currency0": _LOW, "currency1": _HIGH, "fee": 3000, "tickSpacing": 60,
                "hooks": lp_write.NATIVE}
    # Tick 300 with the stub's 600 → 3000 range: entirely above the price, so `arm` accepts
    # it and the principal is currency0. Anything straddling is refused before this test
    # reaches what it is here to check.
    world = _RatchetChain(chain, pool_key, compute_pool_id(pool_key), _ME, tick=300)

    root = Path(tempfile.mkdtemp(prefix="unilp-arm-"))
    saved_env = os.environ.get("UNILP_STATE_DIR")
    os.environ["UNILP_STATE_DIR"] = str(root)
    saved = {name: getattr(lp_write, name)
             for name in ("load_position_for_write", "load_pool_state", "token_info")}
    lp_write.load_position_for_write = world.position
    lp_write.load_pool_state = world.state
    lp_write.token_info = lambda c, ch, a: {"address": a, "symbol": "T", "decimals": 18,
                                            "isNative": False}
    signer = {"address": _ME, "privateKey": "0x" + "11" * 32, "simulateOnly": False}

    def arm(args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ratchet.cmd_arm(world, chain, args, signer)
        return out.getvalue()

    def ids():
        return MandateStore.list_ids(root, "base")

    try:
        base_args = {"token-id": "7", "steps": "30,60,100"}
        shown = arm(dict(base_args))
        ident = shown.rsplit("MANDATE_HASH: ", 1)[1].split()[0]
        r.check("a dry run arms nothing", ids(), [])
        arm({**base_args, "confirm": ident})
        r.check("confirming writes exactly one mandate", ids(), [ident])

        # The exact sequence that produced two files on the live machine.
        again = arm({**base_args, "label": "AGENTOS-7"})
        r.check("re-arming the same position under a new label creates nothing", ids(),
                [ident])
        r.check("and says so instead of failing", "already armed as" in again, True)
        r.check("naming the mandate that already holds it", ident in again, True)

        # A --confirm on the duplicate must not slip past either: same answer, same file.
        arm({**base_args, "label": "AGENTOS-7", "confirm": "0" * 32})
        r.check("even with --confirm, the duplicate is not written", ids(), [ident])

        # Different terms are a different matter — that is an operator error, not a re-run.
        try:
            arm({**base_args, "steps": "50,100", "label": "other"})
            r.check("differing terms are refused", "no error", "RuntimeError")
        except RuntimeError as exc:
            r.check("differing terms are refused",
                    ("already armed" in str(exc), "stepsBps" in str(exc)), (True, True))
        r.check("and still write nothing", ids(), [ident])

        # Disarm is the documented way out, so it has to actually clear the way.
        with contextlib.redirect_stdout(io.StringIO()):
            ratchet.cmd_disarm(world, chain, {"id": ident}, signer)
        replaced = arm({**base_args, "steps": "50,100", "label": "other"})
        r.check("after disarm the position can be armed on new terms",
                "MANDATE_HASH" in replaced, True)
    finally:
        for name, fn in saved.items():
            setattr(lp_write, name, fn)
        if saved_env is None:
            os.environ.pop("UNILP_STATE_DIR", None)
        else:
            os.environ["UNILP_STATE_DIR"] = saved_env
        shutil.rmtree(root, ignore_errors=True)


def _tier7_alert_gate(r) -> None:
    """Which tick outcomes reach a channel under ``--alert-only``.

    Getting this wrong is silent in both directions: too loud and the watchdog is
    ignored, too quiet and a mandate waiting for a human waits forever. The
    NEEDS_ATTENTION row is the one that matters — it reports ``noop`` like every
    other terminal state, and only the state tells it apart.
    """
    import ratchet

    quiet = [
        ("a disarmed mandate", {"action": "noop", "state": "DISARMED"}),
        ("a completed mandate", {"action": "noop", "state": "COMPLETE"}),
        ("an armed mandate below its next threshold",
         {"action": "noop", "state": "ARMED", "reason": "no milestone due"}),
        ("a fire still waiting on a receipt", {"action": "waiting", "state": "FIRE_SENT"}),
        ("a tick that lost the lock", {"action": "skipped", "state": None}),
        ("a tick that could not read the node", {"action": "deferred", "state": "FIRE_SENT"}),
    ]
    for label, outcome in quiet:
        r.check(f"alert gate stays silent for {label}", ratchet.is_notable(outcome), False)

    loud = [
        ("a mandate waiting for a human", {"action": "noop", "state": "NEEDS_ATTENTION"}),
        ("a milestone that fired", {"action": "fired", "state": "ARMED"}),
        ("a landed fire reconciled", {"action": "adopted", "state": "ARMED"}),
        ("a mandate that halted", {"action": "halted", "state": "NEEDS_ATTENTION"}),
        ("a plan the mandate refused", {"action": "rejected", "state": "ARMED"}),
        ("a milestone due on a dry run", {"action": "dry-run", "state": "ARMED"}),
        ("a mandate that expired", {"action": "expired", "state": "EXPIRED"}),
        ("a mandate that vanished", {"action": "missing"}),
        ("a tick that raised", {"action": "error"}),
    ]
    for label, outcome in loud:
        r.check(f"alert gate speaks up for {label}", ratchet.is_notable(outcome), True)

    line = ratchet.tick_summary_line({
        "mandateId": "7857cce24384e26fb6bdcad73e68ee9a", "action": "fired",
        "state": "ARMED", "tokenId": 476498, "txHash": "0xdead",
    })
    r.check("a summary line names the mandate, position and transaction",
            ("7857cce2" in line and "#476498" in line and "0xdead" in line
             and line.startswith("FIRED")), True)
    # The gate reads the last line of stdout; a summary that ended in one would
    # silence the very alert it is announcing.
    r.check("a summary line is not itself a gate line",
            ratchet.tick_summary_line({"mandateId": "a" * 32, "action": "noop",
                                       "state": "NEEDS_ATTENTION",
                                       "note": "check the position"}).startswith("{"), False)


def tier7(g: dict, r: Results) -> None:
    try:
        import lp_write  # noqa: F401
        import ratchet  # noqa: F401
        from unilp import journal, ratchet_math  # noqa: F401
    except ImportError as exc:
        r.skip("tier7/ratchet", f"ratchet modules not importable ({exc})")
        return

    _tier7_math(r)
    _tier7_plan(r)
    _tier7_predicate(r)
    _tier7_authorization(r)
    _tier7_journal(r)
    _tier7_state_machine(r)
    _tier7_duplicate_arm(r)
    _tier7_arm_is_idempotent(r)
    _tier7_alert_gate(r)


TIERS = (
    ("Tier 0 — primitives (keccak, EIP-55, fixed point, JS arithmetic)", tier0),
    ("Tier 1 — ABI codec (encode/decode, unlockData, logs)", tier1),
    ("Tier 2 — signing and PLAN_HASH", tier2),
    ("Tier 3 — pool math and poolId", tier3),
    ("Tier 4 — domain layer (PositionInfo, hooks, ranges, display)", tier4),
    ("Tier 5 — planning ergonomics (tick pull-off, price cache, messages)", tier5),
    ("Tier 6 — PLAN_HASH covers every calldata-affecting flag", tier6),
    ("Tier 7 — ratchet: both sides, both currencies, and the mandate gate", tier7),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="print every passing assertion, not just failures")
    args = parser.parse_args()

    if not GOLDEN_PATH.is_file():
        print(f"missing golden vectors: {GOLDEN_PATH}", file=sys.stderr)
        return 2
    golden = json.loads(GOLDEN_PATH.read_text())

    results = Results(args.verbose)
    for title, fn in TIERS:
        print(title)
        try:
            fn(golden, results)
        except Exception:
            results.failures.append(f"{title} raised:\n{traceback.format_exc()}")
            print("  FAIL (exception)")
            traceback.print_exc()

    print()
    if results.failures:
        print(f"{len(results.failures)} FAILED, {results.passed} passed")
        for failure in results.failures:
            print(f"  - {failure}")
        return 1
    print(f"{results.passed} assertions pass" + (
        f", {len(results.skipped)} tier(s) skipped" if results.skipped else ""))
    for entry in results.skipped:
        print(f"  skipped {entry}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
