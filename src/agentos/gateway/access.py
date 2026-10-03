"""Connection-surface authorization for gateway RPC calls.

AgentOS is a personal-agent runtime. Human callers do not form a privilege
hierarchy: a Control client is either connected or disconnected, and a channel
identity is either paired or unpaired. RPC authorization therefore depends on
the surface that admitted the request, not on user roles or implied scopes.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from enum import StrEnum


class ConnectionSurface(StrEnum):
    """Protocol surface that admitted a request."""

    CONTROL = "control"
    CHANNEL = "channel"
    NODE = "node"
    SYSTEM = "system"


CONTROL_ONLY: frozenset[ConnectionSurface] = frozenset({ConnectionSurface.CONTROL})
CONTROL_AND_CHANNEL: frozenset[ConnectionSurface] = frozenset(
    {ConnectionSurface.CONTROL, ConnectionSurface.CHANNEL}
)
CONTROL_AND_NODE: frozenset[ConnectionSurface] = frozenset(
    {ConnectionSurface.CONTROL, ConnectionSurface.NODE}
)

# Channel slash commands are projected from ``engine.commands`` and are the
# only channel traffic allowed to enter the RPC registry.
CHANNEL_RPC_METHODS: frozenset[str] = frozenset(
    {
        "chat.history",
        "commands.list_for_surface",
        "doctor.memory.status",
        "models.list",
        "plan.mode.set",
        "router.hold.clear",
        "router.hold.set",
        "sessions.abort",
        "sessions.contextCompact",
        "sessions.rename",
        "sessions.reset",
        "skills.list",
        "status",
        "usage.status",
    }
)

NODE_RPC_METHODS: frozenset[str] = frozenset({"skills.bins"})


def normalize_audiences(
    audiences: ConnectionSurface | Iterable[ConnectionSurface],
) -> frozenset[ConnectionSurface]:
    """Return an immutable, non-empty audience set."""

    if isinstance(audiences, ConnectionSurface):
        return frozenset({audiences})
    normalized = frozenset(ConnectionSurface(item) for item in audiences)
    if not normalized:
        raise ValueError("RPC audiences must not be empty")
    return normalized


def is_loopback_address(addr: str | None) -> bool:
    """Return whether ``addr`` is a literal loopback IPv4/IPv6 address.

    Compared as an address, not as a spelling. One IPv6 address has many
    valid textual forms and the string comparison this used to do recognised
    only the shortest: ``0:0:0:0:0:0:0:1``, ``::0001`` and the fully expanded
    form are all ``::1`` and all answered False, as did ``::ffff:7f00:1``,
    the hexadecimal spelling of the IPv4-mapped loopback whose dotted form
    was handled. :func:`peer_is_trusted_proxy` below was already changed to
    compare addresses for exactly this reason; this is the same fix for the
    predicate that gates the no-auth listener, the ``Host`` header guard and
    the control-UI ``Origin`` guard, each of which refused a local client
    that spelled the loopback any other way (#3572).

    ``localhost`` is answered before parsing because it is a name, not a
    literal, and anything else unparseable stays False -- a hostname is not
    an address and must not be read as one here.
    """

    if not addr:
        return False
    host = addr.strip().split("%", 1)[0]
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    # ``::ffff:127.0.0.1`` is the loopback reached over a v6 socket; the
    # mapped v4 address is what carries the verdict, and ``is_loopback`` on
    # the v6 object does not look through the mapping.
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped is not None and bool(mapped.is_loopback)


def is_loopback_bind(host: str | None) -> bool:
    """Return whether the gateway is bound to a loopback-only address."""

    if not host:
        return False
    return host == "localhost" or is_loopback_address(host)


def parse_trusted_proxy_set(trusted_proxy: str | None) -> frozenset[str]:
    """Parse the ``auth.trusted_proxy`` CSV into a normalized IP set.

    Shared by the HTTP middleware (peer admission) and the RPC auth layer
    (per-connection admission) so the two gates cannot drift in how they
    normalize a configured proxy address (case, whitespace, IPv6 brackets).
    """
    if not trusted_proxy:
        return frozenset()
    return frozenset(p.strip().lower().strip("[]") for p in trusted_proxy.split(",") if p.strip())


def normalize_peer_ip(peer_ip: str | None) -> str:
    """Normalize a transport peer address for trusted-proxy comparison.

    Lowercase, bracket-stripped for IPv6 literals; empty string when missing
    so callers can compare against a normalized trusted set directly.
    """
    return (peer_ip or "").strip().lower().strip("[]")


def _parsed_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address *value* denotes, or ``None`` when it is not an IP literal."""
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def peer_is_trusted_proxy(trusted_proxy: str | None, peer_ip: str | None) -> bool:
    """True when the transport ``peer_ip`` is in the trusted-proxy set.

    This is the single shared gate: admission (HTTP middleware and RPC auth)
    requires it, and X-Forwarded-For consumption requires it — a header from
    any other peer is never honored.

    Comparison is on the address, not its spelling. One IPv6 address has many
    valid textual forms, and the operator writing the config and the ASGI
    server reporting the peer need not choose the same one, so ``::1`` was not
    matching a peer reported as ``0:0:0:0:0:0:0:1``. An entry that is not an IP
    literal — a hostname, or anything unparseable — keeps the exact string
    comparison it had, so this only ever matches an address already configured.
    """
    peer = normalize_peer_ip(peer_ip)
    if not peer:
        return False
    configured = parse_trusted_proxy_set(trusted_proxy)
    if peer in configured:
        return True
    peer_address = _parsed_ip(peer)
    if peer_address is None:
        return False
    return any(peer_address == entry for entry in map(_parsed_ip, configured) if entry is not None)


__all__ = [
    "CHANNEL_RPC_METHODS",
    "CONTROL_AND_CHANNEL",
    "CONTROL_AND_NODE",
    "CONTROL_ONLY",
    "NODE_RPC_METHODS",
    "ConnectionSurface",
    "is_loopback_address",
    "is_loopback_bind",
    "normalize_audiences",
    "normalize_peer_ip",
    "parse_trusted_proxy_set",
    "peer_is_trusted_proxy",
]
