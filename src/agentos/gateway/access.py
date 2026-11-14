"""Connection-surface authorization for gateway RPC calls.

AgentOS is a personal-agent runtime. Human callers do not form a privilege
hierarchy: a Control client is either connected or disconnected, and a channel
identity is either paired or unpaired. RPC authorization therefore depends on
the surface that admitted the request, not on user roles or implied scopes.
"""

from __future__ import annotations

import ipaddress
import socket
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
        # The one lifecycle RPC a channel may reach; its handler refuses
        # channel callers unless ``[gateway_control] chat_restart`` is on.
        "gateway.restart",
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
    """Return whether ``addr`` is a literal loopback IPv4/IPv6 address."""

    if not addr:
        return False
    host = addr.split("%", 1)[0]
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if host.startswith("::ffff:"):
        host = host[7:]
    if host in ("::1", "localhost"):
        return True
    if host.startswith("127."):
        parts = host.split(".")
        if len(parts) != 4:
            return False
        try:
            return all(0 <= int(part) <= 255 for part in parts)
        except ValueError:
            return False
    return False


def is_loopback_bind(host: str | None) -> bool:
    """Return whether the gateway is bound to a loopback-only address."""

    if not host:
        return False
    return host == "localhost" or is_loopback_address(host)


def parse_trusted_proxy_set(trusted_proxy: str | None) -> frozenset[str]:
    """Parse the ``auth.trusted_proxy`` CSV into a normalized entry set.

    Shared by the HTTP middleware (peer admission) and the RPC auth layer
    (per-connection admission) so the two gates cannot drift in how they
    normalize a configured proxy address (case, whitespace, IPv6 brackets).
    An entry is an IP literal, a CIDR block (``10.0.0.0/8``, ``fd7a::/48``)
    or, as before, any other string compared exactly.
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


def _parsed_network(value: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    """The block a CIDR entry denotes (host bits ignored), else ``None``."""
    if "/" not in value:
        return None
    try:
        return ipaddress.ip_network(value, strict=False)
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
    matching a peer reported as ``0:0:0:0:0:0:0:1``. A CIDR entry admits every
    address in its block, an IPv4 block also the IPv4-mapped form a
    dual-stack listener reports (``::ffff:10.0.0.7``); an exact IPv4 entry
    stays exact. An entry that is neither — a hostname,
    or anything unparseable — keeps the exact string comparison it had, so
    this only ever matches an address already configured.
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
    mapped = getattr(peer_address, "ipv4_mapped", None)
    for entry in configured:
        network = _parsed_network(entry)
        if network is not None:
            if peer_address.version == network.version and peer_address in network:
                return True
            # A dual-stack listener reports IPv4 peers as ``::ffff:a.b.c.d``.
            if mapped is not None and network.version == 4 and mapped in network:
                return True
            continue
        address = _parsed_ip(entry)
        if address is not None and address == peer_address:
            return True
    return False


# How long a discovered set of this host's own addresses is reused.
_LOCAL_ADDRESS_TTL_S = 60.0
_local_address_cache: tuple[float, frozenset[str]] | None = None


def _discover_local_addresses() -> frozenset[str]:
    """This host's primary outbound IPv4/IPv6 addresses, without DNS.

    ``connect`` on a UDP socket sends nothing; it only asks the kernel which
    local address would route to a public destination. No route (offline,
    no IPv6) simply contributes nothing.
    """
    found: set[str] = set()
    for family, target in (
        (socket.AF_INET, ("192.0.2.1", 9)),
        (socket.AF_INET6, ("2001:db8::1", 9)),
    ):
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as sock:
                sock.connect(target)
                address = sock.getsockname()[0]
        except OSError:
            continue
        parsed = _parsed_ip(normalize_peer_ip(str(address).split("%", 1)[0]))
        if parsed is not None and not parsed.is_unspecified:
            found.add(str(parsed))
    return frozenset(found)


def local_interface_addresses() -> frozenset[str]:
    """This host's own non-loopback addresses (cached for a minute)."""
    import time

    global _local_address_cache
    now = time.monotonic()
    cached = _local_address_cache
    if cached is not None and now - cached[0] < _LOCAL_ADDRESS_TTL_S:
        return cached[1]
    addresses = _discover_local_addresses()
    _local_address_cache = (now, addresses)
    return addresses


#: Documentation addresses (RFC 5737 / RFC 3849): never assigned to a host, so
#: a successful bind to one means the kernel accepts non-local binds.
_NEVER_LOCAL = {4: "192.0.2.1", 6: "2001:db8::1"}


def _bindable(family: socket.AddressFamily, host: str) -> bool:
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.bind((host, 0))
    except OSError:
        return False
    return True


def is_own_address(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether *addr* is assigned to one of this host's interfaces.

    A socket can only bind to an address the host owns, so a throwaway UDP
    bind answers that on every platform without enumerating interfaces. A
    kernel that accepts non-local binds (``net.ipv4.ip_nonlocal_bind=1``)
    cannot answer it this way; then every address counts as this host's,
    which fails closed for both users: the agent surface's remote-operator
    rule is never earned by a same-host process, and an agent HTTP tool is
    refused the gateway's port on every address (``tools.ssrf``).

    Unlike :func:`local_interface_addresses` (the primary outbound address
    per family, for forwarded-header trust), this answers for *any* address
    the host holds: secondary interfaces, container bridges, aliases.
    """
    family = socket.AF_INET6 if addr.version == 6 else socket.AF_INET
    if _bindable(family, _NEVER_LOCAL[addr.version]):
        return True
    return _bindable(family, str(addr))


def _forwarded_hop(value: str) -> str | None:
    """One ``X-Forwarded-For`` entry as a normalized IP literal, or ``None``.

    Tolerates the ``host:port`` and ``[v6]:port`` spellings some proxies use.
    """
    hop = value.strip()
    if hop.startswith("[") and "]" in hop:
        hop = hop[1 : hop.index("]")]
    elif hop.count(":") == 1:
        hop = hop.split(":", 1)[0]
    hop = normalize_peer_ip(hop)
    parsed = _parsed_ip(hop)
    return str(parsed) if parsed is not None else None


def _names_this_host(address: str, own: frozenset[str]) -> bool:
    parsed = _parsed_ip(address)
    if parsed is None:
        return False
    mapped = getattr(parsed, "ipv4_mapped", None)
    if parsed.is_loopback or (mapped is not None and mapped.is_loopback):
        return True
    return any(parsed == other for other in map(_parsed_ip, own) if other is not None)


def resolve_client_ip(
    peer_ip: str | None,
    forwarded_for: str | None,
    trusted_proxy: str | None,
    *,
    local_addresses: frozenset[str] | None = None,
) -> str:
    """The client address a request is attributed to (rate limits, lockouts).

    ``X-Forwarded-For`` is read only when the transport peer is a trusted
    proxy. A proxy *appends* the address it received the request from, so
    the header is walked right to left: trusted hops are skipped and the
    first untrusted one is the client. Taking the left-most entry instead
    would trust the one value the client wrote itself. An entry that is not
    an IP literal stops the walk at the last trusted hop.

    A forwarded address that names this host — loopback, or one of its own
    interface addresses — cannot be a remote client and is treated as a
    spoof: the header is ignored and the proxy's own address is used, so no
    one obtains a loopback client's exemptions through a proxy.
    """
    peer = normalize_peer_ip(peer_ip)
    if not peer:
        return "unknown"
    if not forwarded_for or not peer_is_trusted_proxy(trusted_proxy, peer):
        return peer
    candidate = peer
    for raw_hop in reversed(forwarded_for.split(",")):
        if not raw_hop.strip():
            continue
        hop = _forwarded_hop(raw_hop)
        if hop is None:
            break
        candidate = hop
        if not peer_is_trusted_proxy(trusted_proxy, hop):
            break
    if candidate == peer:
        return peer
    own = local_addresses if local_addresses is not None else local_interface_addresses()
    if _names_this_host(candidate, own):
        import structlog

        structlog.get_logger(__name__).warning(
            "gateway.forwarded_for_spoof_rejected", peer=peer, forwarded_client=candidate
        )
        return peer
    return candidate


__all__ = [
    "CHANNEL_RPC_METHODS",
    "CONTROL_AND_CHANNEL",
    "CONTROL_AND_NODE",
    "CONTROL_ONLY",
    "NODE_RPC_METHODS",
    "ConnectionSurface",
    "is_loopback_address",
    "is_own_address",
    "is_loopback_bind",
    "normalize_audiences",
    "local_interface_addresses",
    "normalize_peer_ip",
    "parse_trusted_proxy_set",
    "peer_is_trusted_proxy",
    "resolve_client_ip",
]
