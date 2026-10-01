"""Issue #3555: the price caches grew for the life of the process.

``PriceService`` kept ``_prices``, ``_held_until`` and ``_history`` in plain
dicts whose only removal was a ``pop`` on the hold map. The TTLs decide
whether an entry is *stale*, never whether it is *kept*, so a gateway that
stays up accumulates one entry per token ever priced -- discovery adds up to
200 addresses per wallet per chain, airdrop spam included -- and, because the
history key carries a day, one per (token, day) for as long as it runs.

They are LRU-bounded now. A dropped entry costs one refetch, which is what a
stale entry costs anyway.
"""

from __future__ import annotations

import httpx
import pytest

from agentos.trading.chains import BASE
from agentos.trading.prices import (
    MAX_HISTORY_ENTRIES,
    MAX_PRICE_ENTRIES,
    PriceService,
    _LruCache,
)
from tests.test_trading.fakes import FakePrices


def _token(index: int) -> str:
    return f"0x{index:040x}"


@pytest.fixture
async def svc() -> PriceService:
    fake = FakePrices()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as http:
        yield PriceService(http=http, ttl_s=300)


# ── the bounded mapping ────────────────────────────────────────────────────


def test_it_forgets_the_least_recently_used_entry() -> None:
    cache: _LruCache[str, int] = _LruCache(3)
    for index, key in enumerate("abc"):
        cache.set(key, index)

    cache.get("a")  # a is now the most recent, b the least
    cache.set("d", 3)

    assert "b" not in cache
    assert ["a", "c", "d"] == [key for key in "abcd" if key in cache]
    assert len(cache) == 3


def test_a_rewrite_is_not_a_new_entry() -> None:
    cache: _LruCache[str, int] = _LruCache(2)
    cache.set("a", 1)
    cache.set("b", 2)

    cache.set("a", 10)

    assert len(cache) == 2
    assert cache.get("a") == 10


def test_reading_by_index_does_not_change_what_is_evicted_next() -> None:
    """``__getitem__`` is for a caller inspecting the cache; it must not
    quietly promote an entry the way a real read does."""
    cache: _LruCache[str, int] = _LruCache(2)
    cache.set("a", 1)
    cache.set("b", 2)

    assert cache["a"] == 1
    cache.set("c", 3)

    assert "a" not in cache, "indexing must not have promoted a"


def test_a_missing_key_behaves_like_a_dict() -> None:
    cache: _LruCache[str, int] = _LruCache(2)

    assert cache.get("nope") is None
    with pytest.raises(KeyError):
        cache["nope"]
    cache.pop("nope")  # no error


def test_the_bound_is_never_below_one() -> None:
    cache: _LruCache[str, int] = _LruCache(0)
    cache.set("a", 1)
    cache.set("b", 2)

    assert len(cache) == 1


# ── the service keeps its caches bounded ───────────────────────────────────


@pytest.fixture
def many_tokens() -> FakePrices:
    """A feed that prices more tokens than the cache may hold."""
    fake = FakePrices()
    for index in range(MAX_PRICE_ENTRIES + 200):
        fake.spot[("base", _token(index))] = 1.0 + index
    return fake


async def test_pricing_more_tokens_than_the_bound_evicts_rather_than_grows(
    many_tokens: FakePrices,
) -> None:
    """The shape the issue describes, through the real path: a desk prices
    far more tokens than it holds, and the cache must stop somewhere."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(many_tokens.handle)) as http:
        svc = PriceService(http=http, ttl_s=300)
        wanted = [_token(index) for index in range(MAX_PRICE_ENTRIES + 200)]

        for start in range(0, len(wanted), 25):
            await svc.prices(BASE, wanted[start : start + 25])

    assert len(svc._prices) == MAX_PRICE_ENTRIES
    assert len(svc._held_until) <= MAX_PRICE_ENTRIES
    # The most recent token is still cached; the first one was let go.
    assert (BASE.chain_id, _token(MAX_PRICE_ENTRIES + 199)) in svc._prices
    assert (BASE.chain_id, _token(0)) not in svc._prices


async def test_a_cached_price_is_still_served_without_a_request(
    many_tokens: FakePrices,
) -> None:
    """The bound must not break the cache it bounds."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(many_tokens.handle)) as http:
        svc = PriceService(http=http, ttl_s=300)
        token = _token(5)

        assert await svc.price(BASE, token) == 6.0
        calls = len(many_tokens.requests)

        assert await svc.price(BASE, token) == 6.0

    assert len(many_tokens.requests) == calls, "the second read came from the cache"


async def test_history_is_bounded_along_its_day_axis(svc: PriceService) -> None:
    """The history key carries a day, so even a single token grows this cache
    for as long as the process lives."""
    for day in range(MAX_HISTORY_ENTRIES + 100):
        svc._history.set((BASE.chain_id, _token(1), day), (0.0, 1.0))

    assert len(svc._history) == MAX_HISTORY_ENTRIES
