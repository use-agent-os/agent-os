"""Unit tests for StaleOutputCache and NullStaleOutputCache."""

from __future__ import annotations

import pytest

from agentos.sandbox.stale_output_cache import (
    NullStaleOutputCache,
    StaleOutputCache,
    get_stale_output_cache,
    reset_stale_output_cache,
)


@pytest.mark.asyncio
async def test_stale_output_cache_record_get_purge() -> None:
    cache = StaleOutputCache()

    assert await cache.get("s1", "fp1") is None
    assert await cache.purge("s1", "fp1") is False

    await cache.record_success("s1", "fp1", {"result": "success"})
    assert await cache.get("s1", "fp1") == {"result": "success"}

    # Overwrite same key
    await cache.record_success("s1", "fp1", {"result": "updated"})
    assert await cache.get("s1", "fp1") == {"result": "updated"}

    # Purge existing
    assert await cache.purge("s1", "fp1") is True
    assert await cache.get("s1", "fp1") is None
    assert await cache.purge("s1", "fp1") is False


@pytest.mark.asyncio
async def test_stale_output_cache_clear_session() -> None:
    cache = StaleOutputCache()
    await cache.record_success("s1", "fp1", "out1")
    await cache.record_success("s1", "fp2", "out2")
    await cache.record_success("s2", "fp1", "out3")

    removed = await cache.clear_session("s1")
    assert removed == 2
    assert await cache.get("s1", "fp1") is None
    assert await cache.get("s1", "fp2") is None
    assert await cache.get("s2", "fp1") == "out3"


@pytest.mark.asyncio
async def test_stale_output_cache_clear() -> None:
    cache = StaleOutputCache()
    await cache.record_success("s1", "fp1", "out1")
    await cache.record_success("s2", "fp2", "out2")
    await cache.record_success("s3", "fp3", "out3")

    count = await cache.clear()
    assert count == 3
    assert await cache.get("s1", "fp1") is None
    assert await cache.get("s2", "fp2") is None
    assert await cache.get("s3", "fp3") is None

    # Clearing already empty cache
    assert await cache.clear() == 0


@pytest.mark.asyncio
async def test_stale_output_cache_snapshot() -> None:
    cache = StaleOutputCache()
    await cache.record_success("s1", "fp1", "payload1")
    snap = cache.snapshot()
    assert len(snap) == 1
    assert snap[0]["session_id"] == "s1"
    assert snap[0]["fingerprint"] == "fp1"
    assert "stored_at" in snap[0]


@pytest.mark.asyncio
async def test_null_stale_output_cache() -> None:
    null_cache = NullStaleOutputCache()
    await null_cache.record_success("s1", "fp1", "payload")
    assert null_cache.calls == [("record", "s1", "fp1")]
    assert await null_cache.get("s1", "fp1") is None
    assert await null_cache.purge("s1", "fp1") is False
    assert await null_cache.clear_session("s1") == 0
    assert await null_cache.clear() == 0
    assert null_cache.snapshot() == []


def test_get_and_reset_stale_output_cache() -> None:
    reset_stale_output_cache()
    c1 = get_stale_output_cache()
    c2 = get_stale_output_cache()
    assert c1 is c2

    reset_stale_output_cache()
    c3 = get_stale_output_cache()
    assert c3 is not c1
    reset_stale_output_cache()
