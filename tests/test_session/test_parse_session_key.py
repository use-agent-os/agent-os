from __future__ import annotations

from agentos.session.keys import (
    DmScope,
    SessionKeyComponents,
    build_channel_key,
    build_cron_key,
    build_direct_key,
    build_group_key,
    build_main_key,
    build_subagent_key,
    build_subagent_session_key,
    build_thread_key,
    build_webchat_key,
    derive_chat_type,
    parse_agent_id,
    parse_session_key,
)
from agentos.session.models import ChatType


def test_parse_empty_or_none() -> None:
    assert parse_session_key(None) == SessionKeyComponents()
    assert parse_session_key("") == SessionKeyComponents()
    assert parse_session_key("   ") == SessionKeyComponents()


def test_parse_main_key() -> None:
    key = build_main_key("ops")
    assert key == "agent:ops:main"
    parsed = parse_session_key(key)
    assert parsed.agent_id == "ops"
    assert parsed.channel is None
    assert parsed.chat_type == "main"
    assert parsed.peer_id == "main"
    assert not parsed.is_subagent


def test_parse_webchat_key() -> None:
    key = build_webchat_key("ops")
    assert key == "agent:ops:webchat:default"
    parsed = parse_session_key(key)
    assert parsed.agent_id == "ops"
    assert parsed.channel == "webchat"
    assert parsed.chat_type == "direct"
    assert parsed.peer_id == "default"


def test_parse_direct_keys() -> None:
    # PER_PEER
    k1 = build_direct_key("ops", "user-1", dm_scope=DmScope.PER_PEER)
    assert k1 == "agent:ops:direct:user-1"
    p1 = parse_session_key(k1)
    assert p1.agent_id == "ops"
    assert p1.channel is None
    assert p1.chat_type == "direct"
    assert p1.peer_id == "user-1"

    # PER_CHANNEL_PEER
    k2 = build_direct_key("ops", "user-1", channel="slack", dm_scope=DmScope.PER_CHANNEL_PEER)
    assert k2 == "agent:ops:slack:direct:user-1"
    p2 = parse_session_key(k2)
    assert p2.agent_id == "ops"
    assert p2.channel == "slack"
    assert p2.chat_type == "direct"
    assert p2.peer_id == "user-1"

    # PER_ACCOUNT_CHANNEL_PEER
    k3 = build_direct_key(
        "ops",
        "user-1",
        channel="telegram",
        account_id="bot-account-1",
        dm_scope=DmScope.PER_ACCOUNT_CHANNEL_PEER,
    )
    assert k3 == "agent:ops:telegram:bot-account-1:direct:user-1"
    p3 = parse_session_key(k3)
    assert p3.agent_id == "ops"
    assert p3.channel == "telegram"
    assert p3.account_id == "bot-account-1"
    assert p3.chat_type == "direct"
    assert p3.peer_id == "user-1"


def test_parse_group_and_channel_keys() -> None:
    gk = build_group_key("ops", "slack", "C12345")
    assert gk == "agent:ops:slack:group:C12345"
    pg = parse_session_key(gk)
    assert pg.agent_id == "ops"
    assert pg.channel == "slack"
    assert pg.chat_type == "group"
    assert pg.peer_id == "C12345"

    ck = build_channel_key("ops", "discord", "chan-999")
    assert ck == "agent:ops:discord:channel:chan-999"
    pc = parse_session_key(ck)
    assert pc.agent_id == "ops"
    assert pc.channel == "discord"
    assert pc.chat_type == "channel"
    assert pc.peer_id == "chan-999"


def test_parse_threaded_keys() -> None:
    base = "agent:ops:telegram:acc1:direct:user-1"
    topic_key = build_thread_key(base, "42", channel_hint="telegram")
    assert topic_key == "agent:ops:telegram:acc1:direct:user-1:topic:42"
    pt = parse_session_key(topic_key)
    assert pt.agent_id == "ops"
    assert pt.channel == "telegram"
    assert pt.account_id == "acc1"
    assert pt.chat_type == "direct"
    assert pt.peer_id == "user-1"
    assert pt.thread_id == "42"

    slack_base = "agent:ops:slack:group:C12345"
    thread_key = build_thread_key(slack_base, "171234.567", channel_hint="slack")
    assert thread_key == "agent:ops:slack:group:C12345:thread:171234.567"
    ps = parse_session_key(thread_key)
    assert ps.agent_id == "ops"
    assert ps.channel == "slack"
    assert ps.chat_type == "group"
    assert ps.peer_id == "C12345"
    assert ps.thread_id == "171234.567"


def test_parse_subagent_keys() -> None:
    # subagent:agent:ops:telegram:direct:user-1
    base = "agent:ops:telegram:direct:user-1"
    sub_key = build_subagent_key(base)
    ps = parse_session_key(sub_key)
    assert ps.agent_id == "ops"
    assert ps.channel == "telegram"
    assert ps.chat_type == "direct"
    assert ps.peer_id == "user-1"
    assert ps.is_subagent is True

    # agent:ops:subagent:run-123
    canon_sub = build_subagent_session_key("ops", "run-123")
    pcs = parse_session_key(canon_sub)
    assert pcs.agent_id == "ops"
    assert pcs.chat_type == "subagent"
    assert pcs.peer_id == "run-123"
    assert pcs.is_subagent is True


def test_parse_cron_keys() -> None:
    ck = build_cron_key("heartbeat", "run-456")
    assert ck == "cron:heartbeat:run:run-456"
    pc = parse_session_key(ck)
    assert pc.agent_id == "main"
    assert pc.channel == "cron"
    assert pc.chat_type == "cron"
    assert pc.peer_id == "heartbeat"


def test_parse_discord_legacy_keys() -> None:
    key = "agent:ops:discord:guild-12345:channel-67890"
    pd = parse_session_key(key)
    assert pd.agent_id == "ops"
    assert pd.channel == "discord"
    assert pd.chat_type == "channel"
    assert pd.peer_id == "67890"


def test_parse_agent_id_and_derive_chat_type() -> None:
    assert parse_agent_id("agent:analytics:telegram:direct:user-1") == "analytics"
    assert parse_agent_id("subagent:agent:analytics:telegram:direct:user-1") == "analytics"
    assert parse_agent_id("cron:heartbeat:run:123") == "main"

    assert derive_chat_type("agent:main:slack:group:C123") == ChatType.GROUP
    assert derive_chat_type("agent:main:slack:channel:C123") == ChatType.CHANNEL
    assert derive_chat_type("agent:main:telegram:acc1:direct:user1") == ChatType.DIRECT
