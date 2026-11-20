"""AgentOS token lists: the tokens a chain's registry names.

The lists are AgentOS's own (``github.com/use-agent-os/agent-os-token-list``),
Uniswap token-list JSON, two per chain: the chain's tokens and its tokenized
stocks. They are loaded, re-checked and merged the way the AgentOS DEX
frontend does it (``src/lib/token-lists`` there):

* A list is downloaded on first use and held in memory.
* A copy younger than :data:`REVALIDATE_TTL_S` is used without asking the
  network. An older one is still answered from at once and re-checked in the
  background with its ETag, so an unchanged list costs a 304.
* A downloaded list replaces the held one only when its version (or, failing
  that, its timestamp) is newer — never a rollback, whichever CDN edge
  answered.
* A list that fails is asked again after 2, 5 and 15 seconds, then once a
  minute; a held copy keeps answering meanwhile.
* Where two lists name one address the earlier list wins. A later one may
  only lend it a missing logo or a tag.

Unlike the frontend, nothing is written to disk. A list decides the decimals
every amount is scaled by, and the only decimals this engine keeps across a
restart are the ones the ledger seals (:mod:`agentos.trading.integrity`).

A list is third-party data, so rows are validated here rather than trusted.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, TypeGuard
from urllib.parse import urlsplit

TOKEN_LIST_BASE = "https://raw.githubusercontent.com/use-agent-os/agent-os-token-list/main"
#: The lists of each chain, in order: where two name the same address, the
#: earlier one wins. ``*_stock_tokens.json`` are the tokenized stocks,
#: generated in that repository from the issuers' own APIs (Coinbase on Base,
#: Robinhood on Robinhood Chain).
TOKEN_LIST_URLS: Mapping[int, tuple[str, ...]] = {
    8453: (
        f"{TOKEN_LIST_BASE}/base_tokens.json",
        f"{TOKEN_LIST_BASE}/base_stock_tokens.json",
    ),
    4663: (
        f"{TOKEN_LIST_BASE}/robinhood_tokens.json",
        f"{TOKEN_LIST_BASE}/robinhood_stock_tokens.json",
    ),
}
#: The tag a stock list puts on every row (``"tags": ["stock"]``).
STOCK_TAG = "stock"
#: A held list younger than this is used without asking the network.
REVALIDATE_TTL_S = 15 * 60.0
#: How long a list that failed is left alone: the first three as the frontend
#: retries, then the last one for as long as it keeps failing.
RETRY_DELAYS_S: tuple[float, ...] = (2.0, 5.0, 15.0, 60.0)

MAX_TOKENS_PER_LIST = 5_000
MAX_DECIMALS = 36
MAX_SYMBOL_CHARS = 32
#: Long enough for a Stock Token's full name with its ``• Robinhood Token``
#: suffix (75 characters today), which is what marks it as one.
MAX_NAME_CHARS = 120
MAX_TAGS = 8

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
#: Not tokens: the native-coin sentinel and the zero address.
_RESERVED = frozenset(
    {
        "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        "0x0000000000000000000000000000000000000000",
    }
)

Version = tuple[int, int, int]


@dataclass(frozen=True)
class ListToken:
    """One validated row of a token list."""

    chain_id: int
    address: str  # lowercase
    symbol: str
    name: str
    decimals: int
    logo_uri: str | None = None
    tags: tuple[str, ...] = ()

    @property
    def stock(self) -> bool:
        return STOCK_TAG in self.tags


@dataclass(frozen=True)
class ParsedList:
    tokens: tuple[ListToken, ...] = ()
    name: str | None = None
    version: Version | None = None
    timestamp: str | None = None


@dataclass
class ListRecord:
    """A list as held: what it said, and when it was last checked."""

    url: str
    chain_id: int
    tokens: tuple[ListToken, ...]
    name: str | None
    version: Version | None
    timestamp: str | None
    #: HTTP ETag, for a cheap conditional re-check (304).
    etag: str | None
    #: When the list last answered, changed or not.
    checked_at: float


@dataclass(frozen=True)
class FetchResult:
    """One answer for a list URL: the status, the JSON body of a 200, the ETag."""

    status: int
    body: Any = None
    etag: str | None = None


#: ``fetch(url, etag)``: ``None`` when the list could not be reached at all.
Fetch = Callable[[str, str | None], Awaitable[FetchResult | None]]


def _is_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _version(value: Any) -> Version | None:
    if not isinstance(value, dict):
        return None
    major, minor, patch = value.get("major"), value.get("minor"), value.get("patch")
    if not (_is_int(major) and _is_int(minor) and _is_int(patch)):
        return None
    if min(major, minor, patch) < 0:
        return None
    return (major, minor, patch)


def _http_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return None
    if parts.scheme not in ("https", "http") or not parts.netloc:
        return None
    return value.strip()


def _token(row: Any, chain_id: int) -> ListToken | None:
    if not isinstance(row, dict):
        return None
    if not _is_int(row.get("chainId")) or row["chainId"] != chain_id:
        return None
    address = row.get("address")
    if not isinstance(address, str) or not _ADDRESS_RE.match(address):
        return None
    address = address.lower()
    if address in _RESERVED:
        return None
    decimals = row.get("decimals")
    if not _is_int(decimals) or not 0 <= decimals <= MAX_DECIMALS:
        return None
    symbol = row.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        return None
    symbol = symbol.strip()
    name = row.get("name")
    name = name.strip() if isinstance(name, str) and name.strip() else symbol
    tags = row.get("tags")
    return ListToken(
        chain_id=chain_id,
        address=address,
        symbol=symbol[:MAX_SYMBOL_CHARS],
        name=name[:MAX_NAME_CHARS],
        decimals=decimals,
        logo_uri=_http_url(row.get("logoURI")),
        tags=tuple(
            tag.strip().lower()
            for tag in (tags if isinstance(tags, list) else [])[:MAX_TAGS]
            if isinstance(tag, str) and tag.strip()
        ),
    )


def parse_token_list(data: Any, chain_id: int) -> ParsedList | None:
    """A token-list body → the tokens for ``chain_id`` plus the list's metadata.

    Accepts the Uniswap ``{"tokens": [...]}`` object or a bare array of rows;
    anything else is not a token list (``None``). A row that is not a valid
    token of this chain is skipped, as is a second row for an address.
    """
    name: str | None = None
    version: Version | None = None
    timestamp: str | None = None
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict) and isinstance(data.get("tokens"), list):
        rows = data["tokens"]
        if isinstance(data.get("name"), str):
            name = data["name"][:80]
        version = _version(data.get("version"))
        if isinstance(data.get("timestamp"), str):
            timestamp = data["timestamp"]
    else:
        return None
    tokens: dict[str, ListToken] = {}
    for row in rows[:MAX_TOKENS_PER_LIST]:
        token = _token(row, chain_id)
        if token is not None and token.address not in tokens:
            tokens[token.address] = token
    return ParsedList(
        tokens=tuple(tokens.values()), name=name, version=version, timestamp=timestamp
    )


def merge_token_lists(lists: Iterable[Iterable[ListToken]]) -> dict[str, ListToken]:
    """Each list's tokens in list order, one row per address.

    The first row for an address wins everything except a missing logo and
    its tags, which a later list may add to — so a later list can decorate a
    token (a stock list still marks it a stock) but never change its symbol
    or decimals.
    """
    merged: dict[str, ListToken] = {}
    for tokens in lists:
        for token in tokens:
            held = merged.get(token.address)
            if held is None:
                merged[token.address] = token
                continue
            logo = held.logo_uri or token.logo_uri
            tags = held.tags + tuple(tag for tag in token.tags if tag not in held.tags)
            if logo != held.logo_uri or tags != held.tags:
                merged[token.address] = replace(held, logo_uri=logo, tags=tags)
    return merged


def _instant(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError):
        return None


def is_version_newer(
    new: ParsedList | ListRecord | None, held: ParsedList | ListRecord | None
) -> bool:
    """Whether ``new`` is strictly newer than ``held``: by version, then by timestamp.

    A first copy is always newer.
    """
    if new is None:
        return False
    if held is None:
        return True
    if new.version is not None and held.version is not None:
        if new.version != held.version:
            return new.version > held.version
    elif new.version is not None:
        return True
    new_at, held_at = _instant(new.timestamp), _instant(held.timestamp)
    return new_at is not None and held_at is not None and new_at > held_at


class TokenListStore:
    """Loads, re-checks and merges the token lists of each chain (module docstring).

    The network and the clock are injected, so the whole flow runs in tests
    with a fake of each.
    """

    def __init__(
        self,
        *,
        fetch: Fetch,
        now: Callable[[], float] = time.time,
        urls: Mapping[int, Iterable[str]] = TOKEN_LIST_URLS,
    ) -> None:
        self._fetch = fetch
        self._now = now
        self._urls: dict[int, tuple[str, ...]] = {
            int(chain_id): tuple(chain_urls) for chain_id, chain_urls in urls.items()
        }
        self._records: dict[tuple[int, str], ListRecord] = {}
        #: Merged tokens per chain, rebuilt only when a list changed, so the
        #: mapping is the same object until the tokens are not.
        self._merged: dict[int, dict[str, ListToken]] = {}
        self._failures: dict[tuple[int, str], int] = {}
        self._retry_at: dict[tuple[int, str], float] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._refreshing: dict[int, asyncio.Task[None]] = {}

    def urls(self, chain_id: int) -> tuple[str, ...]:
        return self._urls.get(chain_id, ())

    def record(self, chain_id: int, url: str) -> ListRecord | None:
        return self._records.get((chain_id, url))

    async def tokens(self, chain_id: int) -> Mapping[str, ListToken]:
        """The chain's listed tokens by lowercase address.

        Waits for the network only for a list that has never answered; a held
        list that is due a re-check is answered from now and re-checked behind
        the caller. Never raises: a chain whose lists cannot be reached has no
        listed tokens until one can.
        """
        urls = self.urls(chain_id)
        if not urls:
            return {}
        if self._missing(chain_id, urls):
            async with self._locks.setdefault(chain_id, asyncio.Lock()):
                missing = self._missing(chain_id, urls)
                if missing:
                    await asyncio.gather(*(self._check(chain_id, url) for url in missing))
        stale = self._stale(chain_id, urls)
        if stale:
            self._refresh_behind(chain_id, stale)
        return self._merge(chain_id)

    async def settle(self) -> None:
        """Wait for the re-checks running behind callers (tests, shutdown)."""
        running = [task for task in self._refreshing.values() if not task.done()]
        if running:
            await asyncio.gather(*running, return_exceptions=True)

    async def aclose(self) -> None:
        for task in self._refreshing.values():
            task.cancel()
        await self.settle()
        self._refreshing.clear()

    # ── internals ──────────────────────────────────────────────────────

    def _due(self, key: tuple[int, str]) -> bool:
        return self._now() >= self._retry_at.get(key, 0.0)

    def _missing(self, chain_id: int, urls: Iterable[str]) -> list[str]:
        return [
            url
            for url in urls
            if (chain_id, url) not in self._records and self._due((chain_id, url))
        ]

    def _stale(self, chain_id: int, urls: Iterable[str]) -> list[str]:
        now = self._now()
        out: list[str] = []
        for url in urls:
            record = self._records.get((chain_id, url))
            if record is None or now - record.checked_at < REVALIDATE_TTL_S:
                continue
            if self._due((chain_id, url)):
                out.append(url)
        return out

    def _merge(self, chain_id: int) -> dict[str, ListToken]:
        merged = self._merged.get(chain_id)
        if merged is None:
            lists = [
                record.tokens
                for url in self.urls(chain_id)
                if (record := self._records.get((chain_id, url))) is not None
            ]
            merged = merge_token_lists(lists)
            self._merged[chain_id] = merged
        return merged

    def _refresh_behind(self, chain_id: int, urls: list[str]) -> None:
        running = self._refreshing.get(chain_id)
        if running is not None and not running.done():
            return

        async def refresh() -> None:
            await asyncio.gather(*(self._check(chain_id, url) for url in urls))

        self._refreshing[chain_id] = asyncio.get_running_loop().create_task(refresh())

    def _failed(self, key: tuple[int, str], at: float) -> bool:
        count = self._failures.get(key, 0)
        self._retry_at[key] = at + RETRY_DELAYS_S[min(count, len(RETRY_DELAYS_S) - 1)]
        self._failures[key] = count + 1
        return False

    async def _check(self, chain_id: int, url: str) -> bool:
        """Ask for one list; ``True`` when the chain's tokens changed. Never raises."""
        key = (chain_id, url)
        record = self._records.get(key)
        at = self._now()
        try:
            result = await self._fetch(url, record.etag if record else None)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a list that cannot be read is a failed check
            result = None
        if result is None:
            return self._failed(key, at)
        if result.status == 304 and record is not None:
            self._answered(key)
            record.checked_at = at
            return False
        parsed = parse_token_list(result.body, chain_id) if result.status == 200 else None
        if parsed is None:
            return self._failed(key, at)
        self._answered(key)
        if record is not None and not is_version_newer(parsed, record):
            # The same or an older version changes nothing, but it answered.
            record.checked_at = at
            record.etag = result.etag or record.etag
            return False
        self._records[key] = ListRecord(
            url=url,
            chain_id=chain_id,
            tokens=parsed.tokens,
            name=parsed.name,
            version=parsed.version,
            timestamp=parsed.timestamp,
            etag=result.etag,
            checked_at=at,
        )
        self._merged.pop(chain_id, None)
        return True

    def _answered(self, key: tuple[int, str]) -> None:
        self._failures.pop(key, None)
        self._retry_at.pop(key, None)


__all__ = [
    "REVALIDATE_TTL_S",
    "RETRY_DELAYS_S",
    "STOCK_TAG",
    "TOKEN_LIST_BASE",
    "TOKEN_LIST_URLS",
    "FetchResult",
    "ListRecord",
    "ListToken",
    "ParsedList",
    "TokenListStore",
    "is_version_newer",
    "merge_token_lists",
    "parse_token_list",
]
