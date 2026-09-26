"""Transaction simulation via ``eth_simulateV1``, with fallbacks — port of ``simulate.mjs``.

``eth_simulateV1`` with ``traceTransfers`` returns the decoded logs of the call, which is
the only way to learn the exact token amounts a ``modifyLiquidities`` call will move: the
function itself returns nothing, so a plain ``eth_call`` says only "did not revert". On a
hooked pool the hook can change what is owed, so the simulated transfers are the
authoritative numbers — not the local math.
"""

from __future__ import annotations

import os

from .abi_codec import decode_event_log
from .hexutil import checksum_address
from .rpc import RpcError

NATIVE = "0x0000000000000000000000000000000000000000"

_TRANSFER_ABI = [
    {"type": "event", "name": "Transfer", "inputs": [
        {"name": "from", "type": "address", "indexed": True},
        {"name": "to", "type": "address", "indexed": True},
        {"name": "value", "type": "uint256", "indexed": False},
    ]},
]

_ERC721_TRANSFER_ABI = [
    {"type": "event", "name": "Transfer", "inputs": [
        {"name": "from", "type": "address", "indexed": True},
        {"name": "to", "type": "address", "indexed": True},
        {"name": "id", "type": "uint256", "indexed": True},
    ]},
]


def simulate_call(client, call: dict) -> dict:
    """Simulate one call. Never raises — a failed simulation is a result, not an error.

    ``revert`` is populated only when the contract answered with one; a node that
    refused the call comes back as ``refused`` instead, because a node fault is not
    evidence about the contract.
    """
    payload = {
        "from": checksum_address(call["from"]),
        "to": checksum_address(call["to"]),
        "data": call.get("data") or "0x",
    }
    value = int(call.get("value") or 0)
    if value > 0:
        payload["value"] = hex(value)

    try:
        result = client.request(
            "eth_simulateV1",
            [{"blockStateCalls": [{"calls": [payload]}],
              "traceTransfers": True, "validation": False}, "latest"],
        )
        block = result[0] if isinstance(result, list) else result
        entry = ((block or {}).get("calls") or [None])[0]
        if entry:
            ok = entry.get("status") == "0x1"
            return {
                "ok": ok,
                "gasUsed": int(entry["gasUsed"], 16) if entry.get("gasUsed") else None,
                "logs": entry.get("logs") or [],
                "revert": None if ok else {
                    "data": (entry.get("error") or {}).get("data"),
                    "message": (entry.get("error") or {}).get("message"),
                },
                "method": "eth_simulateV1",
            }
    except Exception as exc:  # noqa: BLE001 — provider does not support it; fall through
        if os.environ.get("UNILP_DEBUG"):
            print(f"  [sim] eth_simulateV1 unavailable: {exc}")

    # Fallback: plain eth_call. Tells us revert-or-not and nothing else.
    try:
        client.call(payload["to"], payload["data"], "latest", payload["from"])
        return {"ok": True, "gasUsed": None, "logs": [], "revert": None,
                "method": "eth_call (no transfer trace)"}
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, RpcError) and exc.answered:
            return {
                "ok": False,
                "gasUsed": None,
                "logs": [],
                "revert": {"data": exc.data, "message": str(exc)},
                "method": "eth_call (no transfer trace)",
            }
        # The node refused the call -- a rate limit, a transient internal error, a
        # transport failure. Nothing was learned about the contract, so it must not
        # be reported as a revert: a node fault never names one.
        return {
            "ok": False,
            "gasUsed": None,
            "logs": [],
            "revert": None,
            "refused": str(exc),
            "method": "eth_call (no transfer trace)",
        }


def net_transfers(logs: list, account: str) -> dict:
    """Net ERC-20 / native movement for ``account``, derived from the simulated logs.

    With ``traceTransfers`` on, native ETH moves are reported as Transfer logs from the
    zero address, so both asset kinds land in the same map keyed by lowercased currency.
    """
    who = account.lower()
    net: dict[str, int] = {}
    minted_nfts: list[dict] = []

    for log in logs:
        topics = log.get("topics") or []
        if not topics:
            continue
        # ERC-721 Transfer has 4 topics (id is indexed); ERC-20 has 3.
        if len(topics) == 4:
            try:
                decoded = decode_event_log(_ERC721_TRANSFER_ABI, topics, log.get("data") or "0x")
                args = decoded["args"]
            except Exception:  # noqa: BLE001 — not a Transfer; the trace carries every log
                continue
            if args["to"].lower() == who:
                minted_nfts.append({"contract": checksum_address(log["address"]), "id": args["id"]})
            continue
        try:
            args = decode_event_log(_TRANSFER_ABI, topics, log.get("data") or "0x")["args"]
        except Exception:  # noqa: BLE001
            continue
        token = log["address"].lower()
        previous = net.get(token, 0)
        if args["from"].lower() == who:
            net[token] = previous - args["value"]
        elif args["to"].lower() == who:
            net[token] = previous + args["value"]
    return {"net": net, "mintedNfts": minted_nfts}


# Selectors worth naming inline, with what they usually mean in this flow. Anything not
# listed falls through to a note pointing at the raw data.
KNOWN_ERRORS = {
    # Permit2 — these come from the SETTLE leg, i.e. AFTER the pool and its hook accepted
    # the liquidity change. Seeing one means the pool is fine and the approvals are not.
    "0xd81b2f2e": "AllowanceExpired(uint256) — Permit2 approval expired or was never "
                 "set. Run `approve`.",
    "0xf96fb071": "InsufficientAllowance(uint256) — Permit2 allowance too low. Run `approve`.",
    "0x8baa579f": "InvalidSignature() — Permit2",
    # v4-core
    "0x5212cba1": "CurrencyNotSettled() — an action left an open delta; the action list is wrong",
    "0x6f5ffb7e": "ContractLocked()",
    "0x486aa307": "PoolNotInitialized()",
    "0x4c085bf1": "DeltaNotPositive(address)",
    "0x3351b260": "DeltaNotNegative(address)",
    "0xb0ec849e": "NonzeroNativeValue() — sent ETH to a pool with no native currency",
    "0x1e048e1d": "InvalidHookResponse() — the hook returned the wrong selector",
    "0x0a85dc29": "HookNotImplemented()",
    # v4-periphery
    "0x8b063d73": "DeadlinePassed(uint256)",
    "0x7983c051": "MaximumAmountExceeded(uint128,uint128) — raise --slippage-bps",
    "0x8f5d532e": "MinimumAmountInsufficient(uint128,uint128) — raise --slippage-bps",
    "0x5354b3d5": "NotApproved(address) — signer is not the position owner or operator",
}


def describe_revert(revert: dict | None) -> str:
    """Best-effort human-readable revert reason."""
    if not revert:
        return ""
    parts = []
    if revert.get("message"):
        parts.append(revert["message"])
    data = revert.get("data")
    if data and data != "0x":
        selector = data[:10]
        if data.startswith("0x08c379a0"):  # Error(string) — the common readable case
            try:
                body = data[10:]
                length = int(body[64:128], 16)
                parts.append('"' + bytes.fromhex(body[128:128 + length * 2]).decode("utf-8") + '"')
            except (ValueError, UnicodeDecodeError):
                parts.append(f"selector {selector}")
        elif selector in KNOWN_ERRORS:
            parts.append(f"{selector} = {KNOWN_ERRORS[selector]}")
        else:
            parts.append(
                f"selector {selector} (unknown — look it up at "
                f"https://openchain.xyz/signatures?query={selector}, or decode against the "
                f"hook ABI). raw: {data[:74]}"
            )
    return " ".join(parts)
