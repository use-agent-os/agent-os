"""Session key construction — deterministic from agent/channel/account/peer info."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache

# Prototype-poisoning keys blocked for account_id normalization
_POISONING_KEYS = frozenset({"__proto__", "constructor", "prototype", "hasownproperty"})

_INVALID_CHARS = re.compile(r"[^a-z0-9_-]")
_LEADING_TRAILING_DASHES = re.compile(r"^-+|-+$")


class DmScope(StrEnum):
    MAIN = "main"
    PER_PEER = "per_peer"
    PER_CHANNEL_PEER = "per_channel_peer"
    PER_ACCOUNT_CHANNEL_PEER = "per_account_channel_peer"


class PeerKind(StrEnum):
    DIRECT = "direct"
    GROUP = "group"
    CHANNEL = "channel"


def _normalize_id(value: str, max_len: int = 64) -> str:
    """Normalize an id segment: lowercase, replace invalid chars, strip dashes, cap length."""
    v = value.strip().lower()
    v = _INVALID_CHARS.sub("-", v)
    v = _LEADING_TRAILING_DASHES.sub("", v)
    return v[:max_len] if v else "default"


@lru_cache(maxsize=512)
def normalize_agent_id(agent_id: str | None) -> str:
    """Return the canonical runtime agent id.

    ``default`` was historically used by Web/RPC/CLI entrypoints as a
    no-agent sentinel. Treat it as an alias for the real default agent,
    ``main``, so sessions, workspaces, and memory stores do not split.
    """
    raw = str(agent_id or "").strip()
    if not raw or raw.lower() == "default":
        return "main"
    normalized = _normalize_id(raw)
    return "main" if normalized == "default" else normalized


@lru_cache(maxsize=512)
def normalize_account_id(account_id: str) -> str:
    lower = account_id.strip().lower()
    if lower in _POISONING_KEYS:
        return "default"
    return _normalize_id(account_id) or "default"


def build_main_key(agent_id: str = "main") -> str:
    """Return the main session key for an agent."""
    return f"agent:{normalize_agent_id(agent_id)}:main"


def build_webchat_key(agent_id: str = "main") -> str:
    """Return the canonical WebChat default session key for an agent."""
    return f"agent:{normalize_agent_id(agent_id)}:webchat:default"


def canonicalize_session_key(session_key: str | None) -> str:
    """Normalize legacy session-key aliases without changing conversation scope."""
    key = str(session_key or "").strip()
    if not key:
        return ""
    if key == "webchat:default":
        return build_webchat_key()
    if key.startswith("subagent:agent:"):
        return f"subagent:{canonicalize_session_key(key[len('subagent:') :])}"
    if key.startswith("agent:"):
        parts = key.split(":")
        if len(parts) >= 2:
            parts[1] = normalize_agent_id(parts[1])
            return ":".join(parts)
    return key


def build_direct_key(
    agent_id: str,
    peer_id: str,
    channel: str | None = None,
    account_id: str | None = None,
    dm_scope: DmScope = DmScope.MAIN,
) -> str:
    """Build a DM session key depending on dm_scope."""
    aid = normalize_agent_id(agent_id)
    match dm_scope:
        case DmScope.MAIN:
            return f"agent:{aid}:main"
        case DmScope.PER_PEER:
            return f"agent:{aid}:direct:{peer_id}"
        case DmScope.PER_CHANNEL_PEER:
            ch = channel or "unknown"
            return f"agent:{aid}:{ch}:direct:{peer_id}"
        case DmScope.PER_ACCOUNT_CHANNEL_PEER:
            ch = channel or "unknown"
            acc = normalize_account_id(account_id or "default")
            return f"agent:{aid}:{ch}:{acc}:direct:{peer_id}"


def build_group_key(agent_id: str, channel: str, peer_id: str) -> str:
    aid = normalize_agent_id(agent_id)
    return f"agent:{aid}:{channel}:group:{peer_id}"


def build_channel_key(agent_id: str, channel: str, peer_id: str) -> str:
    aid = normalize_agent_id(agent_id)
    return f"agent:{aid}:{channel}:channel:{peer_id}"


def build_thread_key(base_key: str, thread_id: str, channel_hint: str | None = None) -> str:
    """Append thread suffix; uses :topic: for telegram."""
    marker = "topic" if channel_hint == "telegram" else "thread"
    return f"{base_key}:{marker}:{thread_id}"


def parse_thread_suffix(key: str) -> tuple[str, str | None]:
    """Split key into (base_key, thread_id). Returns (key, None) if no suffix."""
    for marker in (":thread:", ":topic:"):
        idx = key.rfind(marker)
        if idx != -1:
            return key[:idx], key[idx + len(marker) :]
    return key, None


def build_subagent_key(base_key: str) -> str:
    return f"subagent:{base_key}"


def build_subagent_session_key(agent_id: str, run_id: str) -> str:
    """Build the canonical agent-scoped subagent session key."""
    aid = normalize_agent_id(agent_id)
    rid = _normalize_id(run_id)
    return f"agent:{aid}:subagent:{rid}"


def is_subagent_key(session_key: str) -> bool:
    """Return True for canonical and legacy subagent session keys."""
    key = session_key.strip().lower()
    return key.startswith("subagent:") or bool(re.match(r"^agent:[^:]+:subagent:[^:]+$", key))


def allows_private_memory_prompt_injection(session_key: str | None) -> bool:
    """Return whether automatic private memory may be injected into a prompt."""
    key = canonicalize_session_key(session_key)
    if not key:
        return True

    key_lower = key.lower()
    if is_subagent_key(key_lower) or key_lower.startswith("cron:"):
        return False

    chat_type = derive_chat_type(key_lower)
    if chat_type in {"group", "channel"}:
        return False
    if chat_type in {"direct", "dm"}:
        return True

    # Preserve main/private CLI and webchat behavior, while denying unknown
    # shared-looking legacy keys that contain channel/guild/group markers.
    shared_markers = (":group", "group:", ":channel", "channel:", ":guild", "guild:")
    if any(marker in key_lower for marker in shared_markers):
        return False
    return True


def build_cron_key(name: str, run_id: str) -> str:
    return f"cron:{name}:run:{run_id}"


@dataclass(frozen=True, slots=True)
class SessionKeyComponents:
    """Structured representation of a parsed session key."""

    agent_id: str = "main"
    channel: str | None = None
    account_id: str | None = None
    chat_type: str = "unknown"
    peer_id: str | None = None
    thread_id: str | None = None
    is_subagent: bool = False


def parse_session_key(session_key: str | None) -> SessionKeyComponents:
    """Parse a canonical or legacy session key into structured components.

    Handles:
    - Standard keys: ``agent:<aid>:<channel>:<kind>:<peer_id>``
    - Account-scoped keys: ``agent:<aid>:<channel>:<account_id>:<kind>:<peer_id>``
    - Direct per-peer keys: ``agent:<aid>:direct:<peer_id>``
    - Main/WebChat keys: ``agent:<aid>:main``, ``agent:<aid>:webchat:<peer_id>``
    - Subagent keys: ``subagent:...`` or ``agent:<aid>:subagent:<run_id>``
    - Threaded/topic keys: ``...:thread:<id>``, ``...:topic:<id>``
    - Cron keys: ``cron:<name>:run:<run_id>``
    - Discord guild keys: ``agent:<aid>:discord:(<acc>:)?guild-<gid>:channel-<cid>``
    """
    raw = str(session_key or "").strip()
    if not raw:
        return SessionKeyComponents()

    is_sub = is_subagent_key(raw)
    key = raw
    if key.lower().startswith("subagent:"):
        key = key[len("subagent:") :]

    base_key, thread_id = parse_thread_suffix(key)

    if base_key.lower().startswith("cron:"):
        parts = base_key.split(":")
        peer = parts[1] if len(parts) >= 2 else None
        return SessionKeyComponents(
            agent_id="main",
            channel="cron",
            chat_type="cron",
            peer_id=peer,
            thread_id=thread_id,
            is_subagent=is_sub,
        )

    if not base_key.lower().startswith("agent:"):
        if ":" in base_key:
            parts = base_key.split(":")
            return SessionKeyComponents(
                agent_id="main",
                channel=parts[0],
                peer_id=parts[-1],
                thread_id=thread_id,
                is_subagent=is_sub,
            )
        return SessionKeyComponents(
            agent_id="main",
            peer_id=base_key,
            thread_id=thread_id,
            is_subagent=is_sub,
        )

    parts = base_key.split(":")
    agent_id = normalize_agent_id(parts[1]) if len(parts) >= 2 else "main"
    rem = parts[2:] if len(parts) >= 3 else []

    if not rem:
        return SessionKeyComponents(agent_id=agent_id, is_subagent=is_sub, thread_id=thread_id)

    if rem == ["main"]:
        return SessionKeyComponents(
            agent_id=agent_id,
            chat_type="main",
            peer_id="main",
            thread_id=thread_id,
            is_subagent=is_sub,
        )

    if rem[0] == "webchat":
        peer = rem[1] if len(rem) >= 2 else "default"
        return SessionKeyComponents(
            agent_id=agent_id,
            channel="webchat",
            chat_type="direct",
            peer_id=peer,
            thread_id=thread_id,
            is_subagent=is_sub,
        )

    if rem[0] == "direct":
        peer = ":".join(rem[1:]) if len(rem) >= 2 else None
        return SessionKeyComponents(
            agent_id=agent_id,
            channel=None,
            chat_type="direct",
            peer_id=peer,
            thread_id=thread_id,
            is_subagent=is_sub,
        )

    if rem[0] == "subagent":
        peer = ":".join(rem[1:]) if len(rem) >= 2 else None
        return SessionKeyComponents(
            agent_id=agent_id,
            chat_type="subagent",
            peer_id=peer,
            thread_id=thread_id,
            is_subagent=True,
        )

    channel = rem[0]
    sub_rem = rem[1:]
    if not sub_rem:
        return SessionKeyComponents(
            agent_id=agent_id,
            channel=channel,
            thread_id=thread_id,
            is_subagent=is_sub,
        )

    if sub_rem[0] in ("direct", "group", "channel", "dm"):
        kind = "direct" if sub_rem[0] == "dm" else sub_rem[0]
        peer = ":".join(sub_rem[1:]) if len(sub_rem) >= 2 else None
        return SessionKeyComponents(
            agent_id=agent_id,
            channel=channel,
            chat_type=kind,
            peer_id=peer,
            thread_id=thread_id,
            is_subagent=is_sub,
        )

    if len(sub_rem) >= 2 and sub_rem[1] in ("direct", "group", "channel", "dm"):
        acc = sub_rem[0]
        kind = "direct" if sub_rem[1] == "dm" else sub_rem[1]
        peer = ":".join(sub_rem[2:]) if len(sub_rem) >= 3 else None
        return SessionKeyComponents(
            agent_id=agent_id,
            channel=channel,
            account_id=acc,
            chat_type=kind,
            peer_id=peer,
            thread_id=thread_id,
            is_subagent=is_sub,
        )

    if channel == "discord" and any(
        seg.startswith("guild-") or seg.startswith("channel-") for seg in sub_rem
    ):
        chan_seg = next((s for s in sub_rem if s.startswith("channel-")), "")
        guild_seg = next((s for s in sub_rem if s.startswith("guild-")), "")
        peer = chan_seg.removeprefix("channel-") if chan_seg else guild_seg.removeprefix("guild-")
        return SessionKeyComponents(
            agent_id=agent_id,
            channel="discord",
            chat_type="channel",
            peer_id=peer or ":".join(sub_rem),
            thread_id=thread_id,
            is_subagent=is_sub,
        )

    return SessionKeyComponents(
        agent_id=agent_id,
        channel=channel,
        peer_id=":".join(sub_rem),
        thread_id=thread_id,
        is_subagent=is_sub,
    )


def parse_agent_id(session_key: str) -> str:
    """Extract agent_id from a session key string. Fallback 'main'.

    'agent:ops:discord:group:123'       -> 'ops'
    'subagent:agent:ops:...'            -> 'ops'
    'cron:foo:run:abc'                  -> 'main'
    """
    return parse_session_key(session_key).agent_id


def derive_chat_type(session_key: str) -> str:
    """Derive chat type from session key tokens."""
    from agentos.session.models import ChatType

    parsed = parse_session_key(session_key)
    if parsed.chat_type in ("direct", "group", "channel"):
        return parsed.chat_type
    key = (session_key or "").lower()
    for token in ("group", "channel", "direct", "dm"):
        if f":{token}:" in key or key.endswith(f":{token}"):
            if token in ("direct", "dm"):
                return ChatType.DIRECT
            return token
    return ChatType.UNKNOWN
