"""Uniswap Trading API client (classic routing).

Base URL ``https://trade-api.gateway.uniswap.org/v1``. The client uses the
no-Permit2 workflow (``x-permit2-disabled: true``): approvals are a plain
ERC-20 ``approve`` to Uniswap's proxy, ``/quote`` returns no ``permitData``
and ``/swap`` targets the proxy. A ``permitData`` that comes back anyway is
untrusted input: the service signs it (EIP-712) only when it is a Permit2
permit bound to the order -- the order's chain, sell token and amount, a
pinned Universal Router, a short deadline (``agentos.trading.permits``) --
and otherwise fails the order without signing anything.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from agentos import __version__
from agentos.trading.evm import USER_AGENT

DecisionOrigin = Literal["human_mediated", "autonomous"]

BASE_URL = "https://trade-api.gateway.uniswap.org/v1"
PROXY_SPENDER = "0x0000000085E102724e78eCd2F45DC9cA239Affad"
#: The Universal Routers a Trading API swap may be sent ``to``, per chain,
#: lowercase. Pinned here rather than read from the response: the service
#: refuses to sign a swap whose target is not in this table (or the proxy
#: above), so a chain without an entry cannot swap through Uniswap at all
#: until someone verifies its routers and adds them. A chain holds a set
#: because the Trading API moves between router versions: since 2026-10 it
#: sends swaps to v2.1.2 on both chains. Sources: ``Uniswap/universal-router``
#: ``deploy-addresses/base.json`` and ``robinhood.json``; on Robinhood Chain
#: ``/v1/swap`` answered ``to = 0x204F...`` on 2026-10-06 and both 4663
#: addresses hold code.
UNIVERSAL_ROUTERS: dict[int, frozenset[str]] = {
    8453: frozenset(
        {
            "0x6ff5693b99212da76ad316178a184ab56d299b43",  # v2.0
            "0xd6145b2d3f379919e8cdeda7b97e37c4b2ca9c40",  # v2.1.2
        }
    ),
    4663: frozenset(
        {
            "0x8876789976decbfcbbbe364623c63652db8c0904",  # v2.1.1
            "0x204faca1764b154221e35c0d20abb3c525710498",  # v2.1.2
        }
    ),
}
CLASSIC_PROTOCOLS = ["V2", "V3", "V4"]
ACCEPTED_ROUTINGS = frozenset({"CLASSIC", "WRAP", "UNWRAP"})
QUOTE_FRESH_SECONDS = 30.0
RETRYABLE_ERROR_CODES = frozenset({"UpstreamTimeoutError"})

_ERROR_TEXT = {
    "QuoteAmountTooLowError": "Amount is below the minimum Uniswap can quote",
    "NoRouteFoundError": "No route found for this pair",
    "UnsupportedTokenError": "Uniswap does not route this token on this chain",
    "UnsupportedChainError": "Uniswap does not support this chain",
    "UniswapXNotSupportedOnChainError": "UniswapX is not available on this chain",
    "UpstreamTimeoutError": "Uniswap routing timed out, try again",
    "ResourceNotFound": "No quotes available",
}


class UniswapError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "trading.uniswap",
        status: int | None = None,
        retryable: bool = False,
        error_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable
        self.error_code = error_code


class UniswapAuthError(UniswapError):
    def __init__(self, message: str = "Uniswap API key was rejected") -> None:
        super().__init__(message, code="trading.no_api_key", status=401)


@dataclass
class Quote:
    """A classic quote plus the bookkeeping the service needs around it."""

    request_id: str
    routing: str
    chain_id: int
    swapper: str
    token_in: str
    token_out: str
    amount_in_raw: int
    amount_out_raw: int
    min_out_raw: int
    slippage_pct: float | None
    price_impact_pct: float | None
    gas_fee_wei: int | None
    gas_fee_usd: float | None
    quote_id: str | None
    raw_quote: dict[str, Any]
    raw_response: dict[str, Any]
    permit_data: dict[str, Any] | None
    fetched_at: float

    @property
    def age_s(self) -> float:
        return max(0.0, time.time() - self.fetched_at)

    @property
    def fresh(self) -> bool:
        return self.age_s < QUOTE_FRESH_SECONDS


def _f(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _i(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(str(value))
    except (TypeError, ValueError):
        return None


class UniswapClient:
    def __init__(
        self,
        api_key: str,
        *,
        http: httpx.AsyncClient | None = None,
        base_url: str = BASE_URL,
        timeout: float = 15.0,
        permit2_disabled: bool = True,
        max_retries: int = 2,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._own_http = http is None
        self._http = http or httpx.AsyncClient(timeout=timeout)
        self._timeout = timeout
        self.permit2_disabled = permit2_disabled
        self._max_retries = max(0, int(max_retries))

    async def aclose(self) -> None:
        if self._own_http:
            await self._http.aclose()

    @staticmethod
    def agent_info(decision_origin: DecisionOrigin) -> str:
        """The ``x-agent-info`` attribution header (analytics only)."""
        return json.dumps(
            {
                "integration_name": "agentos",
                "decision_origin": decision_origin,
                "version": __version__,
            },
            separators=(",", ":"),
            ensure_ascii=True,
        )

    def _headers(self, decision_origin: DecisionOrigin) -> dict[str, str]:
        headers = {
            "x-api-key": self.api_key,
            "content-type": "application/json",
            "accept": "application/json",
            "user-agent": USER_AGENT,
            "x-agent-info": self.agent_info(decision_origin),
        }
        if self.permit2_disabled:
            headers["x-permit2-disabled"] = "true"
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        decision_origin: DecisionOrigin = "human_mediated",
        **kwargs: Any,
    ) -> Any:
        if not self.api_key:
            raise UniswapAuthError("No Uniswap API key configured")
        url = f"{self.base_url}{path}"
        attempt = 0
        while True:
            try:
                response = await self._http.request(
                    method,
                    url,
                    headers=self._headers(decision_origin),
                    timeout=self._timeout,
                    **kwargs,
                )
            except httpx.HTTPError as exc:
                if attempt < self._max_retries:
                    attempt += 1
                    await asyncio.sleep(0.5 * attempt)
                    continue
                raise UniswapError(f"Uniswap API unreachable: {exc}", retryable=True) from exc
            if response.status_code == 429 and attempt < self._max_retries:
                attempt += 1
                await asyncio.sleep(0.5 * (2**attempt))
                continue
            return self._parse(response, path)

    @staticmethod
    def _parse(response: httpx.Response, path: str) -> Any:
        status = response.status_code
        try:
            body: Any = response.json()
        except ValueError:
            body = None
        if status == 401 or status == 403:
            raise UniswapAuthError()
        if status == 429:
            raise UniswapError("Uniswap rate limit exceeded", status=429, retryable=True)
        if status >= 400:
            error_code = None
            detail = None
            if isinstance(body, dict):
                error_code = body.get("errorCode") or body.get("error")
                detail = body.get("detail") or body.get("message")
            text = _ERROR_TEXT.get(str(error_code or ""), None)
            message = text or (str(detail) if detail else f"Uniswap API {path} failed ({status})")
            code = "trading.no_route" if status == 404 else "trading.uniswap"
            if error_code == "UnsupportedChainError":
                code = "trading.unsupported_chain"
            raise UniswapError(
                message,
                code=code,
                status=status,
                retryable=str(error_code) in RETRYABLE_ERROR_CODES or status >= 500,
                error_code=str(error_code) if error_code else None,
            )
        if body is None:
            raise UniswapError(f"Uniswap API {path} returned no JSON", status=status)
        return body

    # ── endpoints ──────────────────────────────────────────────────────

    async def check_approval(
        self,
        *,
        chain_id: int,
        wallet: str,
        token: str,
        amount_raw: int,
        decision_origin: DecisionOrigin = "human_mediated",
    ) -> dict[str, Any] | None:
        """The approval transaction to send, or ``None`` when already approved."""
        body = await self._request(
            "POST",
            "/check_approval",
            decision_origin=decision_origin,
            json={
                "walletAddress": wallet,
                "token": token,
                "amount": str(amount_raw),
                "chainId": chain_id,
                "includeGasInfo": True,
            },
        )
        approval = body.get("approval") if isinstance(body, dict) else None
        return approval if isinstance(approval, dict) and approval.get("data") else None

    async def quote(
        self,
        *,
        chain_id: int,
        swapper: str,
        token_in: str,
        token_out: str,
        amount_raw: int,
        slippage_pct: float | None = None,
        decision_origin: DecisionOrigin = "human_mediated",
    ) -> Quote:
        payload: dict[str, Any] = {
            "type": "EXACT_INPUT",
            "amount": str(amount_raw),
            "tokenInChainId": chain_id,
            "tokenOutChainId": chain_id,
            "tokenIn": token_in,
            "tokenOut": token_out,
            "swapper": swapper,
            "routingPreference": "BEST_PRICE",
            "protocols": list(CLASSIC_PROTOCOLS),
            "urgency": "normal",
        }
        if slippage_pct is None:
            payload["autoSlippage"] = "DEFAULT"
        else:
            payload["slippageTolerance"] = round(float(slippage_pct), 2)
        body = await self._request("POST", "/quote", decision_origin=decision_origin, json=payload)
        if not isinstance(body, dict) or not isinstance(body.get("quote"), dict):
            raise UniswapError("Uniswap quote response is malformed")
        routing = str(body.get("routing") or "")
        if routing not in ACCEPTED_ROUTINGS:
            raise UniswapError(
                f"Unsupported routing {routing or 'unknown'} (only classic swaps are enabled)",
                code="trading.no_route",
            )
        raw = body["quote"]
        inp = raw.get("input") or {}
        out = raw.get("output") or {}
        amount_out = _i(out.get("amount")) or 0
        min_out = _i(out.get("minimumAmount"))
        permit = body.get("permitData")
        return Quote(
            request_id=str(body.get("requestId") or ""),
            routing=routing,
            chain_id=int(raw.get("chainId") or chain_id),
            swapper=str(raw.get("swapper") or swapper),
            token_in=str(inp.get("token") or token_in),
            token_out=str(out.get("token") or token_out),
            amount_in_raw=_i(inp.get("amount")) or amount_raw,
            amount_out_raw=amount_out,
            min_out_raw=min_out if min_out is not None else amount_out,
            slippage_pct=_f(raw.get("slippage")),
            price_impact_pct=_f(raw.get("priceImpact")),
            gas_fee_wei=_i(raw.get("gasFee")),
            gas_fee_usd=_f(raw.get("gasFeeUSD")),
            quote_id=str(raw.get("quoteId")) if raw.get("quoteId") else None,
            raw_quote=raw,
            raw_response=body,
            permit_data=permit if isinstance(permit, dict) else None,
            fetched_at=time.time(),
        )

    async def swap(
        self,
        quote: Quote,
        *,
        signature: str | None = None,
        deadline: int | None = None,
        simulate: bool = True,
        decision_origin: DecisionOrigin = "human_mediated",
    ) -> dict[str, Any]:
        """The transaction to sign for ``quote``.

        The body is the whole ``/quote`` response spread out (``requestId``,
        ``routing``, ``quote``…) with the null permit fields stripped: the
        API rejects both ``permitData: null`` and a ``{quote: response}``
        wrapper.
        """
        payload: dict[str, Any] = {
            k: v
            for k, v in quote.raw_response.items()
            if not (k in ("permitData", "permitTransaction", "permitGasFee") and v is None)
        }
        payload["quote"] = quote.raw_quote
        payload["refreshGasPrice"] = True
        payload["simulateTransaction"] = simulate
        payload["urgency"] = "normal"
        if deadline is not None:
            payload["deadline"] = int(deadline)
        if quote.permit_data is not None:
            if not signature:
                raise UniswapError("quote carries permitData but no signature was provided")
            payload["permitData"] = quote.permit_data
            payload["signature"] = signature
        else:
            payload.pop("permitData", None)
            payload.pop("signature", None)
        body = await self._request("POST", "/swap", decision_origin=decision_origin, json=payload)
        tx = body.get("swap") if isinstance(body, dict) else None
        if not isinstance(tx, dict):
            raise UniswapError("Uniswap swap response has no transaction")
        validate_transaction(tx)
        return tx

    async def swap_status(self, *, chain_id: int, tx_hash: str) -> str:
        body = await self._request(
            "GET", "/swaps", params={"txHashes": tx_hash, "chainId": chain_id}
        )
        swaps = body.get("swaps") if isinstance(body, dict) else None
        if isinstance(swaps, list) and swaps:
            first = swaps[0]
            if isinstance(first, dict):
                return str(first.get("status") or "NOT_FOUND")
        return "NOT_FOUND"

    async def probe(self, *, chain_id: int = 8453) -> tuple[bool, float | None, str | None]:
        """Does the key work? A 401 says no; 200/404 say yes."""
        started = time.monotonic()
        try:
            await self._request(
                "POST",
                "/quote",
                json={
                    "type": "EXACT_INPUT",
                    "amount": "1000000",
                    "tokenInChainId": chain_id,
                    "tokenOutChainId": chain_id,
                    "tokenIn": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                    "tokenOut": "0x4200000000000000000000000000000000000006",
                    "swapper": "0x0000000000000000000000000000000000000001",
                    "autoSlippage": "DEFAULT",
                    "routingPreference": "BEST_PRICE",
                    "protocols": list(CLASSIC_PROTOCOLS),
                },
            )
        except UniswapAuthError as exc:
            return False, None, str(exc)
        except UniswapError as exc:
            latency = (time.monotonic() - started) * 1000
            if exc.status == 404 or exc.status == 400:
                return True, latency, None
            return False, latency, str(exc)
        return True, (time.monotonic() - started) * 1000, None


def _is_address(value: Any) -> bool:
    text = str(value or "")
    if not text.startswith("0x") or len(text) != 42:
        return False
    try:
        int(text[2:], 16)
    except ValueError:
        return False
    return True


def validate_transaction(tx: dict[str, Any]) -> None:
    """Refuse a swap transaction that is not safe to sign as-is."""
    data = str(tx.get("data") or "")
    if not data.startswith("0x") or len(data) <= 2:
        raise UniswapError("Uniswap swap transaction has empty calldata", code="trading.tx_failed")
    try:
        int(data[2:], 16)
    except ValueError as exc:
        raise UniswapError("Uniswap swap calldata is not hex", code="trading.tx_failed") from exc
    if not _is_address(tx.get("to")):
        raise UniswapError("Uniswap swap transaction has no valid 'to'", code="trading.tx_failed")
    if not _is_address(tx.get("from")):
        raise UniswapError("Uniswap swap transaction has no valid 'from'", code="trading.tx_failed")
    if tx.get("value") is None:
        raise UniswapError("Uniswap swap transaction has no 'value'", code="trading.tx_failed")
    if tx.get("maxFeePerGas") and tx.get("gasPrice"):
        raise UniswapError(
            "Uniswap swap transaction sets both maxFeePerGas and gasPrice", code="trading.tx_failed"
        )
