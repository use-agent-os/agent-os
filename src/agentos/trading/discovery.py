"""Token discovery: which ERC-20s does a wallet hold?

The chain sweep only sees ``Transfer`` logs inside the blocks it has scanned,
so a wallet imported today does not know about a token it received last
year until a full rebuild runs. An indexer already knows. Blockscout's
``/api/v2/addresses/{address}/token-balances`` lists every ERC-20 an address
holds; Base has a public instance (Robinhood Chain's sits behind a Cloudflare
challenge, see ``chains.py``).

The indexer is trusted for *identity only*. Nothing it returns is written to
the ledger: the addresses join the sync's scan set and every balance is then
read from the RPC, so a stale or wrong indexer row costs one ``balanceOf``
and nothing else. It is fail-soft in every direction: unreachable, slow,
malformed or rate-limited all mean "no discovery this round", which leaves
the sweep exactly as informed as it was before.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import httpx
import structlog

from agentos.trading.chains import ChainSpec, checksum_address
from agentos.trading.evm import USER_AGENT

log = structlog.get_logger(__name__)

# The indexer is asked once per wallet/chain per this many seconds; the RPC
# re-reads the balances every sync anyway, so discovery has no reason to be
# fast — it only has to notice a token the sweep never will.
DEFAULT_TTL_S = 300.0
# A wallet with more distinct tokens than this is airdrop spam; the scan set
# is capped so one such wallet does not turn every sync into a 2,000-call batch.
MAX_TOKENS = 200
# ...and its answer is not downloaded either. Base Blockscout returns ~500
# bytes per token, unpaginated: an exchange hot wallet came back as 2.6 MB
# (5,346 tokens, measured 2026-09-15). Past this many bytes the answer is
# treated as "none", which costs that wallet discovery and nothing else.
MAX_BODY_BYTES = 1_000_000
TIMEOUT_S = 8.0


class BlockscoutDiscovery:
    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        ttl_s: float = DEFAULT_TTL_S,
        now: Callable[[], float] = time.time,
        max_tokens: int = MAX_TOKENS,
        max_body_bytes: int = MAX_BODY_BYTES,
    ) -> None:
        self._http = http
        self.ttl_s = ttl_s
        self._now = now
        self.max_tokens = max_tokens
        self.max_body_bytes = max_body_bytes
        self._cache: dict[tuple[int, str], tuple[float, list[str], bool]] = {}

    async def holdings(self, chain: ChainSpec, address: str) -> list[str]:
        """Lower-cased ERC-20 addresses the indexer says ``address`` holds.

        Empty when the chain has no indexer, the indexer could not answer, or
        the wallet genuinely holds nothing — the caller cannot tell these
        apart and must not need to.
        """
        base = chain.blockscout_url
        if not base:
            return []
        key = (chain.chain_id, address.lower())
        cached = self._cache.get(key)
        if cached is not None and self._now() - cached[0] < self.ttl_s:
            return list(cached[1])
        answer = await self._fetch(chain, base, address)
        if answer is None:
            # Keep serving the last good answer while the indexer is down;
            # an empty answer would silently shrink the scan set.
            return list(cached[1]) if cached is not None else []
        tokens, complete = answer
        if not complete:
            # The body cap already says so when it trips; this one used to
            # drop the rest of the wallet in silence (#3506).
            log.info(
                "trading.discovery_truncated",
                chain=chain.key,
                wallet=address,
                kept=len(tokens),
                limit=self.max_tokens,
            )
        self._cache[key] = (self._now(), tokens, complete)
        return list(tokens)

    def truncated(self, chain: ChainSpec, address: str) -> bool:
        """Whether the last answer for this wallet hit :attr:`max_tokens`.

        The sweep reads it to mark the chain's balances partial, so a
        portfolio missing an airdropped token says so instead of reading as
        "this is everything you hold".
        """
        cached = self._cache.get((chain.chain_id, address.lower()))
        return bool(cached and not cached[2])

    async def _fetch(
        self, chain: ChainSpec, base: str, address: str
    ) -> tuple[list[str], bool] | None:
        url = f"{base.rstrip('/')}/api/v2/addresses/{checksum_address(address)}/token-balances"
        try:
            async with self._http.stream(
                "GET",
                url,
                headers={"accept": "application/json", "user-agent": USER_AGENT},
                timeout=TIMEOUT_S,
            ) as response:
                if response.status_code == 404:
                    # Blockscout answers 404 for an address it has never seen:
                    # that is a real "holds nothing", not an outage.
                    return [], True
                if response.status_code != 200:
                    log.debug(
                        "trading.discovery_failed", chain=chain.key, status=response.status_code
                    )
                    return None
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self.max_body_bytes:
                        log.info(
                            "trading.discovery_too_large",
                            chain=chain.key,
                            wallet=address,
                            limit=self.max_body_bytes,
                        )
                        return None
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            log.debug("trading.discovery_unreachable", chain=chain.key, error=str(exc))
            return None
        try:
            payload = json.loads(b"".join(chunks))
        except ValueError:
            return None
        return _parse_token_balances(payload, self.max_tokens)

    async def nft_ids(
        self,
        chain: ChainSpec,
        holder: str,
        contract: str,
        *,
        max_pages: int = 10,
        on_page: Callable[[list[int]], None] | None = None,
    ) -> list[int] | None:
        """Token ids of ERC-721 ``contract`` the indexer says ``holder`` owns.

        Same trust model as :meth:`holdings`: identity only, every id is
        re-verified on chain by the caller. ``None`` when the chain has no
        indexer or it did not answer; an indexer lags a few blocks, so the
        caller must not read a short list as "that is all of them".

        ``on_page`` gets each page's new ids as it arrives (it must not block:
        it runs on the event loop), so a caller can verify page one while page
        twenty is still on its way -- and keep what it has when it gives up.
        """
        base = chain.blockscout_url
        if not base:
            return None
        url = f"{base.rstrip('/')}/api/v2/tokens/{checksum_address(contract)}/instances"
        params: dict[str, Any] = {"holder_address_hash": checksum_address(holder)}
        ids: list[int] = []
        seen: set[int] = set()
        for _ in range(max(1, max_pages)):
            try:
                response = await self._http.get(
                    url,
                    params=params,
                    headers={"accept": "application/json", "user-agent": USER_AGENT},
                    timeout=TIMEOUT_S,
                )
            except httpx.HTTPError as exc:
                log.debug("trading.nft_discovery_unreachable", chain=chain.key, error=str(exc))
                return ids or None
            if response.status_code == 404:
                return ids
            if response.status_code != 200:
                return ids or None
            try:
                payload = response.json()
            except ValueError:
                return ids or None
            items = payload.get("items") if isinstance(payload, dict) else None
            page: list[int] = []
            for item in items if isinstance(items, list) else []:
                raw = str((item or {}).get("id") or "") if isinstance(item, dict) else ""
                if raw.isdigit() and int(raw) not in seen:
                    seen.add(int(raw))
                    page.append(int(raw))
            ids.extend(page)
            if on_page is not None and page:
                on_page(page)
            nxt = payload.get("next_page_params") if isinstance(payload, dict) else None
            if not isinstance(nxt, dict) or not nxt:
                break
            params = {"holder_address_hash": checksum_address(holder), **nxt}
        return ids


def _parse_token_balances(payload: Any, max_tokens: int) -> tuple[list[str], bool] | None:
    """The ERC-20 contract addresses in a Blockscout ``token-balances`` body.

    The second element is False when the wallet holds more than *max_tokens*
    of them, so the caller can say the scan set is a subset rather than the
    wallet (#3506).
    """
    items = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return None
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        token = item.get("token")
        if not isinstance(token, dict):
            continue
        kind = str(token.get("type") or "").upper().replace("_", "-")
        if kind and kind != "ERC-20":
            continue
        address = str(token.get("address") or token.get("address_hash") or "").strip().lower()
        if not address.startswith("0x") or len(address) != 42 or address in seen:
            continue
        try:
            int(address, 16)
        except ValueError:
            continue
        value = str(item.get("value") or "0")
        if not value.isdigit() or int(value) <= 0:
            continue
        seen.add(address)
        out.append(address)
        if len(out) >= max_tokens:
            return out, False
    return out, True
