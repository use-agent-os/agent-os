from __future__ import annotations

from agentos.channels.discord import (
    _MAX_TRACKED_CHANNELS,
    DiscordChannel,
    DiscordChannelConfig,
)


def test_channel_type_cache_is_bounded() -> None:
    """A long-running session that sees many channels (THREAD_LIST_SYNC,
    GUILD_CREATE, CHANNEL_CREATE/UPDATE) must not grow _channel_types
    without bound."""
    channel = DiscordChannel(DiscordChannelConfig(token="token"))

    for index in range(_MAX_TRACKED_CHANNELS + 500):
        channel._cache_channel_context({"id": f"channel-{index}", "type": 0})

    assert len(channel._channel_types) == _MAX_TRACKED_CHANNELS


def test_thread_parent_cache_is_bounded() -> None:
    channel = DiscordChannel(DiscordChannelConfig(token="token"))

    for index in range(_MAX_TRACKED_CHANNELS + 500):
        channel._cache_channel_context({"id": f"thread-{index}", "parent_id": f"parent-{index}"})

    assert len(channel._thread_parent_channels) == _MAX_TRACKED_CHANNELS


def test_channel_type_cache_evicts_oldest_entry_first() -> None:
    channel = DiscordChannel(DiscordChannelConfig(token="token"))

    for index in range(_MAX_TRACKED_CHANNELS):
        channel._cache_channel_context({"id": f"channel-{index}", "type": 0})
    # One more insert should evict channel-0 (the oldest), not a recent one.
    channel._cache_channel_context({"id": "channel-new", "type": 0})

    assert "channel-0" not in channel._channel_types
    assert "channel-1" in channel._channel_types
    assert "channel-new" in channel._channel_types


def test_recaching_a_channel_protects_it_from_eviction() -> None:
    """Re-observing a channel (e.g. another CHANNEL_UPDATE) must refresh its
    recency, the same as _util.EventDedupeCache.check_and_add does for a
    repeat event id -- otherwise a channel that is still active could be
    evicted ahead of ones that have gone quiet."""
    channel = DiscordChannel(DiscordChannelConfig(token="token"))

    for index in range(_MAX_TRACKED_CHANNELS):
        channel._cache_channel_context({"id": f"channel-{index}", "type": 0})
    # Touch the oldest entry again before pushing the cache past its bound.
    channel._cache_channel_context({"id": "channel-0", "type": 0})
    channel._cache_channel_context({"id": "channel-new", "type": 0})

    assert "channel-0" in channel._channel_types
    assert "channel-1" not in channel._channel_types


def test_channel_context_cache_still_enriches_within_bound() -> None:
    """Existing enrichment behavior must not regress: a cached channel type
    and thread parent still show up on _annotate_channel_context."""
    channel = DiscordChannel(DiscordChannelConfig(token="token"))

    channel._cache_channel_context({"id": "thread-1", "type": 11, "parent_id": "chan-1"})
    enriched = channel._annotate_channel_context({"channel_id": "thread-1"})

    assert enriched["channel_type"] == 11
    assert enriched["thread_parent_channel_id"] == "chan-1"
