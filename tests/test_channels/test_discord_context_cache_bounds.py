"""Issue #2088: Discord's channel-context caches grew without limit.

``_channel_types`` and ``_thread_parent_channels`` were plain dicts written on
six different gateway events -- CHANNEL_CREATE/UPDATE, THREAD_CREATE/UPDATE,
GUILD_CREATE and THREAD_LIST_SYNC -- with nothing ever removing an entry. On a
long-running connection to an active guild they grow for the life of the
process.

They are now bounded the way ``email.py`` bounds its thread cache, with one
difference that matters here: recency counts *reads* as well as writes. These
caches are consulted on every inbound message, so write-only recency would
evict the busiest channel -- cached once, read constantly, never rewritten --
to make room for a thousand channels that were announced once and never spoken
in.
"""

from __future__ import annotations

from typing import Any

from agentos.channels.discord import (
    _MAX_TRACKED_CHANNELS,
    DiscordChannel,
    DiscordChannelConfig,
)

GUILD_TEXT = 0
PUBLIC_THREAD = 11


def _channel() -> DiscordChannel:
    return DiscordChannel(DiscordChannelConfig(token="t"))


def _cache_text_channel(channel: DiscordChannel, channel_id: str) -> None:
    channel._cache_channel_context({"id": channel_id, "type": GUILD_TEXT})


def _cache_thread(channel: DiscordChannel, thread_id: str, parent_id: str) -> None:
    channel._cache_channel_context({"id": thread_id, "type": PUBLIC_THREAD, "parent_id": parent_id})


def _annotate(channel: DiscordChannel, channel_id: str) -> dict[str, Any]:
    return channel._annotate_channel_context({"channel_id": channel_id, "content": "hi"})


# ── the caches stay bounded ─────────────────────────────────────────────────


def test_the_channel_type_cache_stops_growing_at_the_cap() -> None:
    channel = _channel()

    for index in range(_MAX_TRACKED_CHANNELS * 2):
        _cache_text_channel(channel, f"c{index}")

    assert len(channel._channel_types) == _MAX_TRACKED_CHANNELS


def test_the_thread_parent_cache_stops_growing_at_the_cap() -> None:
    channel = _channel()

    for index in range(_MAX_TRACKED_CHANNELS * 2):
        _cache_thread(channel, f"t{index}", f"parent{index}")

    assert len(channel._thread_parent_channels) == _MAX_TRACKED_CHANNELS


def test_the_oldest_entry_is_the_one_evicted() -> None:
    channel = _channel()

    for index in range(_MAX_TRACKED_CHANNELS + 1):
        _cache_text_channel(channel, f"c{index}")

    assert "c0" not in channel._channel_types
    assert "c1" in channel._channel_types
    assert f"c{_MAX_TRACKED_CHANNELS}" in channel._channel_types


def test_exactly_the_cap_evicts_nothing() -> None:
    """Off-by-one at the boundary would drop a live entry a beat early."""
    channel = _channel()

    for index in range(_MAX_TRACKED_CHANNELS):
        _cache_text_channel(channel, f"c{index}")

    assert len(channel._channel_types) == _MAX_TRACKED_CHANNELS
    assert "c0" in channel._channel_types


def test_recaching_a_channel_moves_it_out_of_the_eviction_line() -> None:
    channel = _channel()
    for index in range(_MAX_TRACKED_CHANNELS):
        _cache_text_channel(channel, f"c{index}")

    _cache_text_channel(channel, "c0")  # a CHANNEL_UPDATE for the oldest entry
    _cache_text_channel(channel, "new")

    assert "c0" in channel._channel_types, "the refreshed entry should have survived"
    assert "c1" not in channel._channel_types


def test_recaching_does_not_grow_the_cache() -> None:
    """An UPDATE for a channel already cached is not a new entry."""
    channel = _channel()
    _cache_text_channel(channel, "c0")

    for _ in range(50):
        _cache_text_channel(channel, "c0")

    assert len(channel._channel_types) == 1


def test_the_two_caches_are_bounded_independently() -> None:
    """A guild full of plain channels must not evict thread parents, and the
    thread cache filling up must not shrink the type cache."""
    channel = _channel()
    _cache_thread(channel, "t0", "parent0")

    for index in range(_MAX_TRACKED_CHANNELS + 5):
        _cache_text_channel(channel, f"c{index}")

    assert channel._thread_parent_channels["t0"] == "parent0"
    assert len(channel._channel_types) == _MAX_TRACKED_CHANNELS


# ── a read counts as use ────────────────────────────────────────────────────


def test_reading_a_channel_protects_it_from_eviction() -> None:
    """The case write-only recency gets wrong.

    A busy channel is cached once by CHANNEL_CREATE and then only ever *read*,
    on every message that arrives in it. Measuring recency on writes alone
    evicts it in favour of a thousand channels that were merely announced.
    """
    channel = _channel()
    for index in range(_MAX_TRACKED_CHANNELS):
        _cache_text_channel(channel, f"c{index}")

    _annotate(channel, "c0")  # the busy channel, read but never rewritten
    _cache_text_channel(channel, "new")

    assert "c0" in channel._channel_types, "a read should have refreshed its recency"
    assert "c1" not in channel._channel_types


def test_reading_a_thread_parent_protects_it_too() -> None:
    channel = _channel()
    _cache_thread(channel, "t0", "parent0")
    for index in range(_MAX_TRACKED_CHANNELS - 1):
        _cache_thread(channel, f"t{index + 1}", f"parent{index + 1}")

    _annotate(channel, "t0")
    _cache_thread(channel, "new", "parentN")

    assert "t0" in channel._thread_parent_channels
    assert "t1" not in channel._thread_parent_channels


def test_a_read_miss_does_not_create_an_entry() -> None:
    """Annotating a message from an unknown channel must not populate the cache
    with an empty record -- that would be a second unbounded growth path."""
    channel = _channel()

    _annotate(channel, "never-seen")

    assert channel._channel_types == {}
    assert channel._thread_parent_channels == {}


# ── enrichment still works ──────────────────────────────────────────────────


def test_a_cached_channel_type_still_annotates_the_message() -> None:
    channel = _channel()
    _cache_text_channel(channel, "c1")

    assert _annotate(channel, "c1")["channel_type"] == GUILD_TEXT


def test_a_cached_thread_parent_still_annotates_the_message() -> None:
    channel = _channel()
    _cache_thread(channel, "t1", "parent1")

    enriched = _annotate(channel, "t1")

    assert enriched["thread_parent_channel_id"] == "parent1"
    assert enriched["channel_type"] == PUBLIC_THREAD


def test_an_evicted_channel_simply_stops_being_annotated() -> None:
    """Eviction degrades enrichment; it must not raise or corrupt the message."""
    channel = _channel()
    _cache_text_channel(channel, "old")
    for index in range(_MAX_TRACKED_CHANNELS + 1):
        _cache_text_channel(channel, f"c{index}")

    enriched = _annotate(channel, "old")

    assert "channel_type" not in enriched
    assert enriched["content"] == "hi"


def test_an_explicit_field_on_the_event_still_wins_over_the_cache() -> None:
    """Unchanged behaviour, pinned: the cache only fills gaps."""
    channel = _channel()
    _cache_text_channel(channel, "c1")

    enriched = channel._annotate_channel_context(
        {"channel_id": "c1", "channel_type": PUBLIC_THREAD}
    )

    assert enriched["channel_type"] == PUBLIC_THREAD


def test_an_event_with_no_channel_id_is_passed_through_untouched() -> None:
    channel = _channel()
    payload = {"content": "hi"}

    assert channel._annotate_channel_context(payload) == payload


def test_a_malformed_cache_event_is_ignored() -> None:
    """A gateway payload missing an id, or carrying a non-string one, must not
    put junk in a cache that is now size-limited -- an unusable entry would
    evict a real one."""
    channel = _channel()

    channel._cache_channel_context({"type": GUILD_TEXT})
    channel._cache_channel_context({"id": None, "type": GUILD_TEXT})
    channel._cache_channel_context({"id": "", "type": GUILD_TEXT})
    channel._cache_channel_context({"id": 123, "type": GUILD_TEXT})
    channel._cache_channel_context({"id": "c1"})  # no type, no parent

    assert channel._channel_types == {}
    assert channel._thread_parent_channels == {}
