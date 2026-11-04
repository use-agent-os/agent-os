"""Offline doubles for every network dependency of ``agentos.trading``.

One ``httpx.MockTransport`` routes by host: JSON-RPC nodes, the AgentOS
Aggregator, the Uniswap Trading API, DexScreener, CoinGecko and
GeckoTerminal. Tests configure the fakes and assert on what was requested.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx

from agentos.trading.aggregator import ALLOWANCE_HOLDER
from agentos.trading.aggregator import NATIVE_SENTINEL as AGG_NATIVE
from agentos.trading.evm import (
    APPROVAL_TOPIC,
    SEL_ALLOWANCE,
    SEL_APPROVE,
    SEL_BALANCE_OF,
    SEL_DECIMALS,
    SEL_NAME,
    SEL_SYMBOL,
    SEL_TRANSFER,
    TRANSFER_TOPIC,
    pad_address,
    pad_uint,
)
from agentos.trading.providers import PERMIT2

WALLET = "0x1111111111111111111111111111111111111111"
OTHER = "0x2222222222222222222222222222222222222222"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
WETH = "0x4200000000000000000000000000000000000006"
AAPL = "0xaaaa000000000000000000000000000000000001"
#: Uniswap's Trading API proxy — where its swaps are sent in the no-Permit2 flow.
ROUTER = "0x0000000085e102724e78ecd2f45dc9ca239affad"
#: The Universal Router pinned for Base (``uniswap.UNIVERSAL_ROUTERS``).
UNIVERSAL_ROUTER = "0x6ff5693b99212da76ad316178a184ab56d299b43"
#: Every contract a fake node should treat as "a swap landed here".
SWAP_TARGETS: frozenset[str] = frozenset({ROUTER, UNIVERSAL_ROUTER, ALLOWANCE_HOLDER})


def approve_calldata(spender: str, amount: int) -> str:
    """``approve(spender, amount)`` as the Trading API's check_approval returns it."""
    return SEL_APPROVE + pad_address(spender) + pad_uint(amount)


def _enc_uint(value: int) -> str:
    return "0x" + format(value, "x").rjust(64, "0")


def _enc_string(text: str) -> str:
    raw = text.encode()
    body = (
        (32).to_bytes(32, "big")
        + len(raw).to_bytes(32, "big")
        + raw.ljust((len(raw) + 31) // 32 * 32, b"\x00")
    )
    return "0x" + body.hex()


def transfer_log(
    *,
    tx_hash: str,
    log_index: int,
    block: int,
    token: str,
    sender: str,
    recipient: str,
    amount: int,
) -> dict[str, Any]:
    return {
        "address": token,
        "topics": [TRANSFER_TOPIC, "0x" + pad_address(sender), "0x" + pad_address(recipient)],
        "data": _enc_uint(amount),
        "blockNumber": hex(block),
        "transactionHash": tx_hash,
        "logIndex": hex(log_index),
    }


def approval_log(
    *,
    tx_hash: str,
    log_index: int,
    block: int,
    token: str,
    owner: str,
    spender: str,
    amount: int,
) -> dict[str, Any]:
    return {
        "address": token,
        "topics": [APPROVAL_TOPIC, "0x" + pad_address(owner), "0x" + pad_address(spender)],
        "data": _enc_uint(amount),
        "blockNumber": hex(block),
        "transactionHash": tx_hash,
        "logIndex": hex(log_index),
    }


@dataclass
class FakeChain:
    chain_id: int
    block: int = 1_000
    native: dict[str, int] = field(default_factory=dict)
    erc20: dict[str, dict[str, int]] = field(default_factory=dict)
    tokens: dict[str, tuple[str, str, int]] = field(default_factory=dict)
    logs: list[dict[str, Any]] = field(default_factory=list)
    receipts: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: ``eth_getTransactionByHash`` answers, keyed by lowercase hash.
    transactions: dict[str, dict[str, Any]] = field(default_factory=dict)
    nonces: dict[str, int] = field(default_factory=dict)
    sent: list[str] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    # Raise a JSON-RPC error for eth_getLogs spans wider than this (forces chunk halving).
    max_log_span: int | None = None
    on_send: Callable[[str], str] | None = None
    #: ``"token:owner:spender" -> amount``, as ``approve`` leaves it.
    allowances: dict[str, int] = field(default_factory=dict)
    #: Answer eth_getBalance per (address, block) instead of from ``native``.
    #: Lets a test play a load-balanced node whose "latest" is a block behind.
    balance_at: Callable[[str, str], int] | None = None
    batch_supported: bool = True
    block_timestamps: dict[int, int] = field(default_factory=dict)
    fee_history: bool = True
    #: What ``eth_feeHistory`` reports: the latest base fee, and one 50th
    #: percentile reward per block (the engine takes their median).
    base_fee: int = 10**8
    fee_rewards: list[int] = field(default_factory=lambda: [10**6])
    revert_calls: bool = False
    # Tokens whose ``balanceOf`` the node refuses (per-item batch failure).
    fail_balance_of: set[str] = field(default_factory=set)
    # JSON-RPC methods that fail outright (a node outage for that method).
    fail_methods: set[str] = field(default_factory=set)
    #: Receipts as the sealed block has them, keyed by lowercase hash: until
    #: the head is past the receipt's block, ``receipts`` answers instead
    #: (Base's flashblock preconfirmation, whose ``l1Fee`` is provisional).
    sealed: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Blocks the head moves on every ``eth_blockNumber`` (0 = a still chain).
    head_step: int = 0
    _seq: int = 0

    def set_native(self, address: str, wei: int) -> None:
        self.native[address.lower()] = wei

    def set_erc20(self, token: str, address: str, raw: int) -> None:
        self.erc20.setdefault(token.lower(), {})[address.lower()] = raw

    @staticmethod
    def _allowance_key(token: str, owner: str, spender: str) -> str:
        return f"{token.lower()}:{owner.lower()}:{spender.lower()}"

    def set_allowance(self, token: str, owner: str, spender: str, amount: int) -> None:
        self.allowances[self._allowance_key(token, owner, spender)] = amount

    def get_allowance(self, token: str, owner: str, spender: str) -> int:
        return self.allowances.get(self._allowance_key(token, owner, spender), 0)

    def apply_approve(self, owner: str, tx: dict[str, Any]) -> bool:
        """Record what an ``approve(spender, amount)`` calldata grants."""
        data = str(tx.get("data") or "")
        if not data.startswith(SEL_APPROVE) or len(data) < 138:
            return False
        self.set_allowance(
            str(tx.get("to") or ""), owner, "0x" + data[10:74][-40:], int(data[74:138], 16)
        )
        return True

    def apply_transfer(self, sender: str, tx: dict[str, Any]) -> tuple[str, int] | None:
        """Move what a ``transfer(to, amount)`` calldata says; ``(recipient, amount)`` or None."""
        data = str(tx.get("data") or "")
        if not data.startswith(SEL_TRANSFER) or len(data) < 138:
            return None
        token = str(tx.get("to") or "").lower()
        recipient = "0x" + data[10:74][-40:]
        amount = int(data[74:138], 16)
        self.set_erc20(token, sender, self.get_erc20(token, sender) - amount)
        self.set_erc20(token, recipient, self.get_erc20(token, recipient) + amount)
        return recipient, amount

    def record_transaction(self, tx_hash: str, tx: dict[str, Any]) -> None:
        """Remember a signed tx so ``eth_getTransactionByHash`` can answer for it."""
        self.transactions[tx_hash.lower()] = {
            "hash": tx_hash,
            "from": str(tx.get("from") or ""),
            "to": str(tx.get("to") or ""),
            "input": str(tx.get("data") or "0x"),
            "value": hex(int(tx.get("value") or 0)),
            "nonce": hex(int(tx.get("nonce") or 0)),
        }

    def get_erc20(self, token: str, address: str) -> int:
        return self.erc20.get(token.lower(), {}).get(address.lower(), 0)

    def add_transfer(
        self,
        *,
        token: str,
        sender: str,
        recipient: str,
        amount: int,
        block: int | None = None,
        tx_hash: str | None = None,
        log_index: int = 0,
    ) -> str:
        self._seq += 1
        tx_hash = tx_hash or ("0x" + format(self._seq, "x").rjust(64, "0"))
        block = block if block is not None else self.block
        self.logs.append(
            transfer_log(
                tx_hash=tx_hash,
                log_index=log_index,
                block=block,
                token=token.lower(),
                sender=sender.lower(),
                recipient=recipient.lower(),
                amount=amount,
            )
        )
        return tx_hash

    def _error(self, req_id: Any, message: str, code: int = -32000) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

    def handle_one(self, req: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(req)
        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params") or []
        try:
            result = self._dispatch(str(method), params)
        except _RpcFailError as exc:
            return self._error(req_id, str(exc), exc.code)
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def _dispatch(self, method: str, params: list[Any]) -> Any:
        if method in self.fail_methods:
            raise _RpcFailError("node unavailable", -32603)
        if method == "eth_chainId":
            return hex(self.chain_id)
        if method == "eth_blockNumber":
            self.block += self.head_step
            return hex(self.block)
        if method == "eth_getBalance":
            address = str(params[0]).lower()
            block = str(params[1]) if len(params) > 1 else "latest"
            if self.balance_at is not None:
                return hex(self.balance_at(address, block))
            return hex(self.native.get(address, 0))
        if method == "eth_getBlockByNumber":
            number = int(str(params[0]), 16) if str(params[0]).startswith("0x") else self.block
            ts = self.block_timestamps.get(number, 1_700_000_000 + number * 2)
            return {"number": hex(number), "timestamp": hex(ts)}
        if method == "eth_call":
            return self._eth_call(params[0])
        if method == "eth_getLogs":
            return self._get_logs(params[0])
        if method == "eth_getTransactionCount":
            return hex(self.nonces.get(str(params[0]).lower(), 0))
        if method == "eth_estimateGas":
            if self.revert_calls:
                raise _RpcFailError("execution reverted")
            return hex(150_000)
        if method == "eth_feeHistory":
            if not self.fee_history:
                raise _RpcFailError("method not supported", -32601)
            return {
                "baseFeePerGas": [hex(self.base_fee)] * (len(self.fee_rewards) + 1),
                "reward": [[hex(r)] for r in self.fee_rewards],
            }
        if method == "eth_gasPrice":
            return hex(2 * self.base_fee)
        if method == "eth_sendRawTransaction":
            raw = str(params[0])
            self.sent.append(raw)
            if self.on_send is not None:
                return self.on_send(raw)
            self._seq += 1
            return "0x" + format(0xABC000 + self._seq, "x").rjust(64, "0")
        if method == "eth_getTransactionReceipt":
            key = str(params[0]).lower()
            final = self.sealed.get(key)
            if final is not None and self.block > int(str(final["blockNumber"]), 16):
                return final
            return self.receipts.get(key)
        if method == "eth_getTransactionByHash":
            return self.transactions.get(str(params[0]).lower())
        if method == "eth_getCode":
            # A node reports bytecode only where a contract actually is. The
            # fake has to as well, or nothing can test "not on this chain".
            address = str(params[0]).lower()
            known = address in self.tokens or address in self.erc20 or address in self.native
            return "0x6000" if known else "0x"
        raise _RpcFailError(f"unknown method {method}", -32601)

    def _eth_call(self, tx: dict[str, Any]) -> str:
        if self.revert_calls and tx.get("from"):
            raise _RpcFailError("execution reverted")
        to = str(tx.get("to") or "").lower()
        data = str(tx.get("data") or "")
        selector = data[:10]
        if selector == SEL_BALANCE_OF:
            if to in {t.lower() for t in self.fail_balance_of}:
                raise _RpcFailError("execution timeout", -32000)
            owner = "0x" + data[10:74][-40:]
            return _enc_uint(self.get_erc20(to, owner))
        if selector == SEL_ALLOWANCE:
            owner = "0x" + data[10:74][-40:]
            spender = "0x" + data[74:138][-40:]
            return _enc_uint(self.get_allowance(to, owner, spender))
        if selector == SEL_TRANSFER:
            # Simulated transfer: revert when the sender cannot cover it.
            sender = str(tx.get("from") or "").lower()
            if sender and self.get_erc20(to, sender) < int(data[74:138], 16):
                raise _RpcFailError("execution reverted: ERC20: transfer amount exceeds balance")
            return _enc_uint(1)
        meta = self.tokens.get(to)
        if selector == SEL_SYMBOL:
            return _enc_string(meta[0]) if meta else "0x"
        if selector == SEL_NAME:
            return _enc_string(meta[1]) if meta else "0x"
        if selector == SEL_DECIMALS:
            return _enc_uint(meta[2]) if meta else "0x"
        return "0x"

    def _get_logs(self, flt: dict[str, Any]) -> list[dict[str, Any]]:
        start = int(str(flt.get("fromBlock", "0x0")), 16)
        end = int(str(flt.get("toBlock", hex(self.block))), 16)
        if self.max_log_span is not None and end - start + 1 > self.max_log_span:
            raise _RpcFailError("query returned more than 10000 results", -32005)
        topics = flt.get("topics") or []
        out = []
        for log in self.logs:
            block = int(str(log["blockNumber"]), 16)
            if block < start or block > end:
                continue
            ok = True
            for index, wanted in enumerate(topics):
                if wanted is None:
                    continue
                actual = log["topics"][index] if index < len(log["topics"]) else None
                options = wanted if isinstance(wanted, list) else [wanted]
                if actual is None or actual.lower() not in [o.lower() for o in options]:
                    ok = False
                    break
            if ok:
                out.append(log)
        return out

    def handle(self, request: httpx.Request) -> httpx.Response:
        if not request.headers.get("user-agent", "").startswith("agentos-trading/"):
            return httpx.Response(403, text="forbidden")
        payload = json.loads(request.content or b"null")
        if isinstance(payload, list):
            if not self.batch_supported:
                return httpx.Response(200, json=self.handle_one(payload[0]))
            return httpx.Response(200, json=[self.handle_one(item) for item in payload])
        return httpx.Response(200, json=self.handle_one(payload))

    def receipt(
        self,
        tx_hash: str,
        *,
        status: int = 1,
        gas_used: int = 100_000,
        gas_price: int = 10**8,
        logs: list[dict[str, Any]] | None = None,
        block: int | None = None,
    ) -> dict[str, Any]:
        receipt = {
            "transactionHash": tx_hash,
            "status": hex(status),
            "gasUsed": hex(gas_used),
            "effectiveGasPrice": hex(gas_price),
            "blockNumber": hex(block if block is not None else self.block),
            "logs": logs or [],
        }
        self.receipts[tx_hash.lower()] = receipt
        return receipt


class _RpcFailError(Exception):
    def __init__(self, message: str, code: int = -32000) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class FakeUniswap:
    api_key: str = "test-key"
    amount_out: int = 500_000_000_000_000  # 0.0005 WETH for the default quote
    min_out: int | None = None
    approval_needed: bool = False
    approval_spender: str = PERMIT2
    permit_data: dict[str, Any] | None = None
    price_impact: float = 0.12
    gas_fee_usd: str = "0.05"
    routing: str = "CLASSIC"
    quote_error: tuple[int, dict[str, Any]] | None = None
    swap_error: tuple[int, dict[str, Any]] | None = None
    tx_to: str = ROUTER
    tx_value: str = "0"
    requests: list[httpx.Request] = field(default_factory=list)
    quotes: int = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("x-api-key") != self.api_key:
            return httpx.Response(401, json={"errorCode": "Unauthorized"})
        path = request.url.path.removeprefix("/v1")
        body = json.loads(request.content or b"{}") if request.content else {}
        if path == "/check_approval":
            approval = None
            if self.approval_needed:
                # What the Trading API really sends: approve(Permit2, amount).
                approval = {
                    "to": body["token"],
                    "from": body["walletAddress"],
                    "data": approve_calldata(self.approval_spender, int(body["amount"])),
                    "value": "0",
                    "chainId": body["chainId"],
                    "gasLimit": "60000",
                }
            return httpx.Response(
                200, json={"requestId": "r-appr", "approval": approval, "cancel": None}
            )
        if path == "/quote":
            self.quotes += 1
            if self.quote_error is not None:
                status, payload = self.quote_error
                return httpx.Response(status, json=payload)
            amount_out = self.amount_out
            min_out = self.min_out if self.min_out is not None else amount_out * 995 // 1000
            quote = {
                "chainId": body["tokenInChainId"],
                "swapper": body["swapper"],
                "input": {"amount": body["amount"], "token": body["tokenIn"]},
                "output": {
                    "amount": str(amount_out),
                    "token": body["tokenOut"],
                    "minimumAmount": str(min_out),
                    "recipient": body["swapper"],
                },
                "slippage": body.get("slippageTolerance", 0.5),
                "tradeType": "EXACT_INPUT",
                "gasFee": "5000000000000",
                "gasFeeUSD": self.gas_fee_usd,
                "priceImpact": self.price_impact,
                "quoteId": f"q-{self.quotes}",
                "blockNumber": "1000",
            }
            return httpx.Response(
                200,
                json={
                    "requestId": f"req-{self.quotes}",
                    "routing": self.routing,
                    "quote": quote,
                    "permitData": self.permit_data,
                },
            )
        if path == "/swap":
            if self.swap_error is not None:
                status, payload = self.swap_error
                return httpx.Response(status, json=payload)
            quote = body["quote"]
            return httpx.Response(
                200,
                json={
                    "requestId": "req-swap",
                    "swap": {
                        "to": self.tx_to,
                        "from": quote["swapper"],
                        "data": "0x3593564c" + "11" * 32,
                        "value": self.tx_value,
                        "chainId": quote["chainId"],
                        "gasLimit": "250000",
                        "maxFeePerGas": "200000000",
                        "maxPriorityFeePerGas": "1000000",
                    },
                },
            )
        if path == "/swaps":
            return httpx.Response(200, json={"swaps": [{"status": "SUCCESS"}]})
        return httpx.Response(404, json={"errorCode": "ResourceNotFound"})


@dataclass
class FakeAggregator:
    """The AgentOS Aggregator: one GET answers with price *and* calldata.

    Knob names mirror :class:`FakeUniswap` where the concept is the same, so
    a test can be pointed at either provider. ``approval_spender`` defaults
    to ``tx_to`` because that relation — spender is the swap target — is what
    the real API guarantees and what the provider refuses to sign without.
    """

    # 0.005 WETH ($10 at the fake's $2,000) for the default 10 USDC quote.
    # Unlike Uniswap, this provider publishes no price impact, so the engine
    # derives one from these amounts — they have to add up.
    amount_out: int = 5_000_000_000_000_000
    min_out: int | None = None
    approval_needed: bool = False
    approval_spender: str | None = None  # None => the same contract as tx_to
    gas_estimate: str = "288079"
    gas_cost_native: str = "0.00001"
    slippage_bps: int = 50
    route_source: str = "Uniswap_V4"
    liquidity: bool = True
    expires_in_s: float = 30.0
    configured: bool = True
    quote_error: tuple[int, dict[str, Any]] | None = None
    tx_to: str = ALLOWANCE_HOLDER  # what the live API sends every swap to
    tx_value: str = "0"
    tx_data: str = "0x2213bc0b" + "22" * 32
    requests: list[httpx.Request] = field(default_factory=list)
    quotes: int = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/health":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "version": "0.1.0",
                    "chains": [8453, 4663],
                    "configured": self.configured,
                },
            )
        if path not in ("/v1/quote", "/v1/price"):
            return httpx.Response(404, json={"error": {"code": "INVALID_REQUEST"}})
        self.quotes += 1
        if self.quote_error is not None:
            status, payload = self.quote_error
            return httpx.Response(status, json=payload)
        params = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        sell, buy = params["sell"], params["buy"]
        amount_raw = int(params["sellAmountRaw"])
        taker = params.get("taker", WALLET)
        if not self.liquidity:
            return httpx.Response(
                200,
                json={
                    "chainId": int(params["chain"]),
                    "sellToken": self._token(sell),
                    "buyToken": self._token(buy),
                    "sellAmount": {"raw": str(amount_raw), "formatted": None},
                    "buyAmount": None,
                    "minBuyAmount": None,
                    "price": None,
                    "fee": None,
                    "route": [],
                    "liquidityAvailable": False,
                    "explanation": "No route for this pair right now.",
                },
            )
        min_out = self.min_out if self.min_out is not None else self.amount_out * 995 // 1000
        spender = (self.approval_spender or self.tx_to).lower()
        approval: dict[str, Any] | None = None
        if self.approval_needed and sell.lower() != AGG_NATIVE.lower():
            approval = {
                "required": True,
                "token": sell.lower(),
                "spender": spender,
                "currentAllowance": "0",
                "amountNeeded": str(amount_raw),
                "transaction": {
                    "to": sell.lower(),
                    "data": approve_calldata(spender, amount_raw),
                    "value": "0",
                },
                "explanation": "Send this approve transaction first.",
            }
        expires = datetime.now(tz=UTC) + timedelta(seconds=self.expires_in_s)
        return httpx.Response(
            200,
            json={
                "chainId": int(params["chain"]),
                "sellToken": self._token(sell),
                "buyToken": self._token(buy),
                "sellAmount": {"raw": str(amount_raw), "formatted": None},
                "buyAmount": {"raw": str(self.amount_out), "formatted": None},
                "minBuyAmount": {"raw": str(min_out), "formatted": None},
                "price": "1",
                "fee": {"bps": 20, "token": "ETH", "amount": {"raw": "1", "formatted": None}},
                "gas": {
                    "estimate": self.gas_estimate,
                    "gasPriceWei": "5040000",
                    "estimatedCostNative": self.gas_cost_native,
                },
                "route": [{"source": self.route_source, "proportionBps": 10000}],
                "slippageBps": int(params.get("slippageBps", self.slippage_bps)),
                "liquidityAvailable": True,
                "cached": False,
                "recipient": taker,
                "approval": approval,
                "transaction": {
                    "to": self.tx_to,
                    "data": self.tx_data,
                    "value": self.tx_value,
                    "gas": self.gas_estimate,
                    "gasPrice": "5040000",
                },
                "expiresAt": expires.isoformat().replace("+00:00", "Z"),
                "issues": {"balance": None, "simulationIncomplete": False},
                "explanation": "Sell for about something via a pool.",
            },
        )

    @staticmethod
    def _token(address: str) -> dict[str, Any]:
        decimals = {USDC: 6}.get(address.lower(), 18)
        return {"address": address.lower(), "symbol": "TKN", "decimals": decimals}


@dataclass
class FakePrices:
    """DexScreener spot prices + CoinGecko lists/history + GeckoTerminal candles."""

    spot: dict[tuple[str, str], float] = field(default_factory=dict)  # (slug, token) -> usd
    lists: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # platform -> tokens
    history: dict[str, float] = field(default_factory=dict)  # token -> usd at any past ts
    candles: list[list[float]] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.url.host
        path = request.url.path
        if host == "api.dexscreener.com":
            if path.startswith("/tokens/v1/"):
                _, _, _, slug, addresses = path.split("/", 4)
                pairs = []
                for address in addresses.split(","):
                    price = self.spot.get((slug, address.lower()))
                    if price is None:
                        continue
                    pairs.append(self._pair(slug, address.lower(), price))
                return httpx.Response(200, json=pairs)
            if path == "/latest/dex/search":
                query = parse_qs(request.url.query.decode()).get("q", [""])[0].lower()
                pairs = [
                    self._pair(slug, token, price)
                    for (slug, token), price in self.spot.items()
                    if query in token
                ]
                return httpx.Response(200, json={"pairs": pairs})
        if host == "tokens.coingecko.com":
            platform = path.split("/")[1]
            return httpx.Response(200, json={"tokens": self.lists.get(platform, [])})
        if host == "api.coingecko.com":
            parts = path.split("/")
            token = parts[6].lower() if len(parts) > 6 else ""
            price = self.history.get(token)
            if price is None:
                return httpx.Response(404, json={"error": "not found"})
            query = parse_qs(request.url.query.decode())
            ts = int(query.get("from", ["0"])[0]) + 6 * 3600
            return httpx.Response(200, json={"prices": [[ts * 1000, price]]})
        if host == "api.geckoterminal.com":
            return httpx.Response(200, json={"data": {"attributes": {"ohlcv_list": self.candles}}})
        return httpx.Response(404)

    @staticmethod
    def _pair(slug: str, token: str, price: float) -> dict[str, Any]:
        return {
            "chainId": slug,
            "pairAddress": "0xpair" + token[-6:],
            "url": f"https://dexscreener.com/{slug}/0xpair",
            "baseToken": {"address": token, "symbol": "TKN", "name": "Token"},
            "quoteToken": {"symbol": "USDC", "name": "USD Coin"},
            "priceUsd": str(price),
            "priceNative": str(price / 2000),
            "marketCap": price * 1_000_000,
            "dexId": "uniswap",
            "labels": ["v4"],
            "priceChange": {"h24": 2.5},
            "liquidity": {"usd": 1_000_000},
            "volume": {"h24": 50_000},
            "info": {"imageUrl": "https://img/" + token},
        }


@dataclass
class FakeIndexer:
    """A Blockscout ``/api/v2/addresses/{addr}/token-balances`` endpoint."""

    # wallet (lower) -> [(token address, raw balance, token type)]
    holdings: dict[str, list[tuple[str, int, str]]] = field(default_factory=dict)
    status: int = 200
    requests: list[httpx.Request] = field(default_factory=list)

    def hold(self, wallet: str, token: str, raw: int, kind: str = "ERC-20") -> None:
        self.holdings.setdefault(wallet.lower(), []).append((token, raw, kind))

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, text="nope")
        parts = request.url.path.split("/")
        if len(parts) < 6 or parts[-1] != "token-balances":
            return httpx.Response(404, json={"message": "Not found"})
        rows = self.holdings.get(parts[-2].lower())
        if rows is None:
            return httpx.Response(404, json={"message": "Not found"})
        return httpx.Response(
            200,
            json=[
                {
                    "token": {"address": token, "type": kind, "symbol": "X", "decimals": "18"},
                    "value": str(raw),
                }
                for token, raw, kind in rows
            ],
        )


RELAY_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "relay"
#: The Relay solver the recorded quotes deposit to.
RELAY_SOLVER = "0x4cd00e387622c35bddb9b4c962c136462338bc31"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
NATIVE_ZERO = "0x0000000000000000000000000000000000000000"


def relay_quote_fixtures() -> dict[tuple[int, int, str], dict[str, Any]]:
    """The recorded ``/quote/v2`` bodies, keyed by (origin, destination, origin currency)."""
    out: dict[tuple[int, int, str], dict[str, Any]] = {}
    for name in RELAY_FIXTURES.glob("quote_*.json"):
        body = json.loads(name.read_text(encoding="utf-8"))
        details = body["details"]
        origin = int(details["currencyIn"]["currency"]["chainId"])
        destination = int(details["currencyOut"]["currency"]["chainId"])
        token = str(details["currencyIn"]["currency"]["address"]).lower()
        out[(origin, destination, token)] = body
    return out


@dataclass
class FakeRelay:
    """Relay's quote and status endpoints, answered from the recorded fixtures.

    A quote is the fixture for the route, re-addressed to the caller (user,
    recipient, ``from`` on every step, the ERC-20 deposit's depositor word)
    and re-sized to the amount asked (``currencyIn``, ``currencyOut`` by the
    fixture's rate times ``out_scale``, the native deposit's value, the
    approve and deposit amount words). Every quote gets a fresh request id,
    as Relay does. Statuses are played per request id in order; the last one
    repeats, and an unknown id is ``waiting``.
    """

    quotes: dict[tuple[int, int, str], dict[str, Any]] = field(default_factory=relay_quote_fixtures)
    statuses: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    #: One status sequence for whatever request id is asked (tests rarely know it).
    default_statuses: list[dict[str, Any]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    quote_error: tuple[int, dict[str, Any]] | None = None
    status_error: tuple[int, dict[str, Any]] | None = None
    mutate_quote: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    out_scale: float = 1.0
    request_ids: list[str] = field(default_factory=list)
    _seq: int = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/quote/v2":
            body = json.loads(request.content or b"{}")
            self.calls.append({"path": path, "body": body})
            if self.quote_error is not None:
                status, payload = self.quote_error
                return httpx.Response(status, json=payload)
            key = (
                int(body["originChainId"]),
                int(body["destinationChainId"]),
                str(body["originCurrency"]).lower(),
            )
            template = self.quotes.get(key)
            if template is None:
                return httpx.Response(400, json={"message": "No routes found for this request"})
            return httpx.Response(200, json=self._quote(template, body))
        if path == "/intents/status/v3":
            request_id = str(request.url.params.get("requestId") or "")
            self.calls.append({"path": path, "requestId": request_id})
            if self.status_error is not None:
                status, payload = self.status_error
                return httpx.Response(status, json=payload)
            sequence = self.statuses.get(request_id)
            if sequence is None:
                sequence = self.default_statuses
            if not sequence:
                return httpx.Response(200, json={"status": "waiting"})
            answer = sequence.pop(0) if len(sequence) > 1 else sequence[0]
            return httpx.Response(200, json=answer)
        return httpx.Response(404, json={"message": f"no fake for {path}"})

    def _quote(self, template: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        quote = json.loads(json.dumps(template))
        self._seq += 1
        request_id = "0x" + format(0xFEED0000 + self._seq, "x").rjust(64, "0")
        self.request_ids.append(request_id)
        user = str(body["user"])
        amount = int(body["amount"])
        details = quote["details"]
        template_in = int(details["currencyIn"]["amount"])
        template_out = int(details["currencyOut"]["amount"])
        out = int(amount * template_out / template_in * self.out_scale)
        details["currencyIn"]["amount"] = str(amount)
        details["currencyOut"]["amount"] = str(out)
        details["currencyOut"]["amountFormatted"] = None
        details["recipient"] = str(body["recipient"])
        details["sender"] = user
        native = str(body["originCurrency"]).lower() == NATIVE_ZERO
        for step in quote["steps"]:
            step["requestId"] = request_id
            for item in step["items"]:
                data = item["data"]
                data["from"] = user
                if step["id"] == "deposit":
                    if native:
                        data["value"] = str(amount)
                    else:
                        calldata = str(data["data"])
                        data["data"] = (
                            calldata[:10]
                            + pad_address(user)
                            + calldata[74:138]
                            + pad_uint(amount)
                            + calldata[202:]
                        )
                elif step["id"] == "approve":
                    data["data"] = approve_calldata(RELAY_SOLVER, amount)
                item.setdefault("check", {})["endpoint"] = (
                    f"/intents/status/v3?requestId={request_id}"
                )
        if self.mutate_quote is not None:
            quote = self.mutate_quote(quote)
        return quote


def make_transport(
    *,
    chains: dict[str, FakeChain],
    uniswap: FakeUniswap,
    prices: FakePrices,
    aggregator: FakeAggregator | None = None,
    indexer: FakeIndexer | None = None,
    relay: FakeRelay | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host in chains:
            return chains[host].handle(request)
        if host == "agg.useagentos.dev":
            return (aggregator or FakeAggregator()).handle(request)
        if host == "api.relay.link":
            return (relay or FakeRelay()).handle(request)
        if host == "trade-api.gateway.uniswap.org":
            return uniswap.handle(request)
        if host.endswith("blockscout.com"):
            return indexer.handle(request) if indexer else httpx.Response(404)
        return prices.handle(request)

    return httpx.MockTransport(handler)


def fake_sign_tx(tx: dict[str, Any], key: bytes) -> str:
    """Encode the tx dict as the "raw" transaction so tests can inspect it."""
    return "0x" + json.dumps(tx, sort_keys=True).encode().hex()


def decode_fake_raw(raw: str) -> dict[str, Any]:
    return json.loads(bytes.fromhex(raw.removeprefix("0x")).decode())
