"""Read calldata and receipts back into words.

Pure functions over hex strings and JSON-RPC shapes. The selector table is
deliberately small — the ERC-20 surface, WETH, Permit2 and the two routers
the desk actually signs for — because a guess dressed up as a decode is
worse than an honest "unknown function". Anything not in the table comes
back as its selector with the calldata word count, and the receipt's
Transfer/Approval logs still tell what moved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentos.trading.evm import (
    UINT256_MAX,
    ApprovalLog,
    TransferLog,
    decode_uint,
    is_unlimited,
    receipt_approvals,
    receipt_transfers,
)

# selector -> (name, static argument types, or None when the ABI is dynamic)
KNOWN_SELECTORS: dict[str, tuple[str, list[str] | None]] = {
    "0xa9059cbb": ("transfer", ["address", "uint256"]),
    "0x095ea7b3": ("approve", ["address", "uint256"]),
    "0x23b872dd": ("transferFrom", ["address", "address", "uint256"]),
    "0x2e1a7d4d": ("withdraw", ["uint256"]),  # WETH9
    "0xd0e30db0": ("deposit", []),  # WETH9
    "0x3593564c": ("execute", None),  # Uniswap Universal Router (with deadline)
    "0x24856bc3": ("execute", None),  # Uniswap Universal Router
    "0x87517c45": ("approve", ["address", "address", "uint160", "uint48"]),  # Permit2
    "0x2b67b570": ("permit", None),  # Permit2 permit(owner, PermitSingle, sig)
    "0x30f28b7a": ("permitTransferFrom", None),  # Permit2
    "0x36c78516": ("transferFrom", None),  # Permit2 batch transferFrom
    "0x0d58b1db": ("multiSend", None),  # Gnosis MultiSend
    "0xdd46508f": ("modifyLiquidities", None),  # Uniswap V4 PositionManager
}

# Names for spenders the desk knows: a revoke list should say "Permit2", not 0x0000…ba3.
KNOWN_SPENDERS: dict[str, str] = {
    "0x000000000022d473030f116ddee9f6b43ac78ba3": "Permit2",
    "0x0000000085e102724e78ecd2f45dc9ca239affad": "Uniswap Trading API proxy",
    "0x6ff5693b99212da76ad316178a184ab56d299b43": "Uniswap Universal Router",
    # v2.1.2, what the Trading API sends swaps to since 2026-10 (docs/markets.md).
    "0xd6145b2d3f379919e8cdeda7b97e37c4b2ca9c40": "Uniswap Universal Router v2.1.2",
    "0x8876789976decbfcbbbe364623c63652db8c0904": "Uniswap Universal Router (Robinhood Chain)",
    "0x204faca1764b154221e35c0d20abb3c525710498": "Uniswap Universal Router v2.1.2 (Robinhood)",
    "0x2626664c2603336e57b271c5c0b26f421741e481": "Uniswap V3 SwapRouter02",
    # What the AgentOS Aggregator's approvals name (0x AllowanceHolder), seen live 2026-09-20.
    "0x0000000000001ff3684f28c67538d4d072c22734": "AgentOS Aggregator (0x AllowanceHolder)",
    # What its approvals name in contract mode: AgentOSRouter, same address on Base and Robinhood.
    "0x4cf93bac8920b1efc92b0d2c84dcbe56802e530c": "AgentOS Aggregator (AgentOS Router)",
    "0x6131b5fae19ea4f9d964eac0408e4408b66337b5": "KyberSwap MetaAggregationRouter",
    # The V4 PositionManager LP writes approve through Permit2 (docs/lp-write.md).
    "0x7c5f5a4bbd8fd63184577525326123b519429bdc": "Uniswap V4 PositionManager (Base)",
    "0x58daec3116aae6d93017baaea7749052e8a04fa7": "Uniswap V4 PositionManager (Robinhood Chain)",
}


def spender_label(address: str) -> str | None:
    return KNOWN_SPENDERS.get((address or "").lower())


@dataclass
class DecodedCall:
    selector: str
    function: str | None
    args: list[dict[str, Any]] = field(default_factory=list)
    known: bool = False
    """The calldata is a recognised function *and* its static arguments parsed."""
    words: int = 0
    """32-byte words after the selector — a size hint for unknown calls."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "selector": self.selector,
            "function": self.function,
            "args": list(self.args),
            "known": self.known,
            "words": self.words,
        }


def _word(body: str, index: int) -> str | None:
    start = index * 64
    chunk = body[start : start + 64]
    return chunk if len(chunk) == 64 else None


def _decode_arg(kind: str, word: str) -> Any:
    if kind == "address":
        return "0x" + word[-40:]
    if kind.startswith("uint") or kind.startswith("int"):
        return str(int(word, 16))
    if kind == "bool":
        return int(word, 16) != 0
    return "0x" + word


def decode_calldata(data: str | None) -> DecodedCall:
    """The function and static arguments behind ``data``; honest about the rest."""
    text = (data or "").strip().lower()
    if text in ("", "0x"):
        return DecodedCall(selector="0x", function=None, known=True, words=0)
    if not text.startswith("0x"):
        text = "0x" + text
    selector = text[:10]
    body = text[10:]
    try:
        int(body or "0", 16)
    except ValueError:
        return DecodedCall(selector=selector, function=None, known=False, words=0)
    words = len(body) // 64
    entry = KNOWN_SELECTORS.get(selector)
    if entry is None:
        return DecodedCall(selector=selector, function=None, known=False, words=words)
    name, types = entry
    if types is None:
        return DecodedCall(selector=selector, function=name, known=False, words=words)
    args: list[dict[str, Any]] = []
    for index, kind in enumerate(types):
        word = _word(body, index)
        if word is None:
            return DecodedCall(selector=selector, function=name, known=False, words=words)
        value = _decode_arg(kind, word)
        arg: dict[str, Any] = {"type": kind, "value": value}
        if kind.startswith("uint") and isinstance(value, str):
            arg["unlimited"] = is_unlimited(int(value)) if int(value) <= UINT256_MAX else False
        args.append(arg)
    return DecodedCall(selector=selector, function=name, args=args, known=True, words=words)


def describe_call(call: DecodedCall, *, to: str | None, value_wei: int) -> str:
    """One line a person can read: what this transaction asks the chain to do."""
    target = (to or "").lower()
    if call.selector == "0x":
        return f"send {value_wei} wei to {target or 'nobody'}"
    if call.known and call.function == "transfer":
        return f"transfer {call.args[1]['value']} units of {target} to {call.args[0]['value']}"
    if call.known and call.function == "approve" and len(call.args) == 2:
        amount = "unlimited" if call.args[1].get("unlimited") else f"{call.args[1]['value']} units"
        return f"approve {call.args[0]['value']} to spend {amount} of {target}"
    if call.known and call.function == "transferFrom":
        return (
            f"transferFrom {call.args[0]['value']} → {call.args[1]['value']}: "
            f"{call.args[2]['value']} units of {target}"
        )
    if call.function == "withdraw" and call.known:
        return f"unwrap {call.args[0]['value']} wei of WETH at {target}"
    if call.function == "deposit":
        return f"wrap {value_wei} wei into WETH at {target}"
    if call.function:
        return f"call {call.function} on {target}"
    return f"call {call.selector} on {target} ({call.words} words)"


def receipt_movements(
    receipt: dict[str, Any] | None,
) -> tuple[list[TransferLog], list[ApprovalLog]]:
    if not receipt:
        return [], []
    return receipt_transfers(receipt), receipt_approvals(receipt)


def tx_summary(tx: dict[str, Any] | None, receipt: dict[str, Any] | None) -> dict[str, Any]:
    """The envelope of a mined (or pending) transaction, as JSON-RPC gave it."""
    tx = tx or {}
    receipt = receipt or {}
    status_hex = receipt.get("status")
    status: str
    if not receipt or not receipt.get("blockNumber"):
        status = "pending"
    else:
        status = "success" if decode_uint(str(status_hex or "0x0")) == 1 else "reverted"
    gas_used = decode_uint(str(receipt.get("gasUsed") or "0x0")) if receipt else 0
    gas_price = decode_uint(str(receipt.get("effectiveGasPrice") or tx.get("gasPrice") or "0x0"))
    # OP-stack receipts (Base, Robinhood Chain) carry the L1 data fee on top
    # of ``gasUsed × price``; what the sender paid is the sum of the two.
    l1_fee = decode_uint(str(receipt.get("l1Fee") or "0x0")) if receipt else 0
    gas_wei = gas_used * gas_price + l1_fee if gas_used and gas_price else 0
    return {
        "hash": str(tx.get("hash") or receipt.get("transactionHash") or "").lower() or None,
        "from": str(tx.get("from") or receipt.get("from") or "").lower() or None,
        "to": str(tx.get("to") or receipt.get("to") or "").lower() or None,
        "valueWei": str(decode_uint(str(tx.get("value") or "0x0"))),
        "nonce": decode_uint(str(tx.get("nonce") or "0x0")) if tx.get("nonce") else None,
        "blockNumber": decode_uint(str(receipt.get("blockNumber") or "0x0")) or None,
        "status": status,
        "gasUsed": gas_used or None,
        "gasPriceWei": str(gas_price) if gas_price else None,
        "l1FeeWei": str(l1_fee) if l1_fee else None,
        "gasWei": str(gas_wei) if gas_wei else None,
    }
