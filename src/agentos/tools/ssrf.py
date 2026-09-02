"""Shared SSRF protection for URL-fetching tools."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterable
from urllib.parse import urlparse

from agentos.tools.types import SSRFBlockedError, UnsupportedURLSchemeError

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

RFC2544_FAKE_IP_NETWORK = ipaddress.IPv4Network("198.18.0.0/15")

_HARD_BLOCKED_NETWORKS: tuple[IPNetwork, ...] = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)

#: Hostnames that resolve to a cloud metadata service. Blocked by name as well
#: as by address, because a resolver that answers them at all is answering for
#: the credential endpoint.
_METADATA_HOSTNAMES: frozenset[str] = frozenset(
    {
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
        "metadata.azure.com",
        "metadata.azure.net",
    }
)

#: Addresses that serve instance credentials to anything that can reach them.
#: These are the non-negotiable floor: unlike ordinary private ranges they have
#: no legitimate agent use, on any tool, under any configuration.
_METADATA_ADDRESSES: frozenset[IPAddress] = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),  # AWS / GCP / Azure / DO / Oracle
        ipaddress.ip_address("169.254.169.253"),  # Azure IMDS wire server
        ipaddress.ip_address("169.254.170.2"),  # AWS ECS task role credentials
        ipaddress.ip_address("100.100.100.200"),  # Alibaba Cloud
        ipaddress.ip_address("192.0.0.192"),  # Azure IMDS (legacy wire server)
        ipaddress.ip_address("fd00:ec2::254"),  # AWS metadata over IPv6
    }
)

#: The whole link-local range. Nothing an agent should be talking to lives
#: here, and enumerating individual metadata addresses has repeatedly missed a
#: cloud vendor's variant.
_METADATA_NETWORKS: tuple[IPNetwork, ...] = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
)

_trusted_fake_ip_cidrs: tuple[IPNetwork, ...] = ()


def validate_trusted_fake_ip_cidrs(values: Iterable[str]) -> list[str]:
    """Return normalized fake-IP CIDRs or raise for unsafe entries."""
    networks: list[str] = []
    for raw in values:
        try:
            network = ipaddress.ip_network(str(raw).strip(), strict=False)
        except ValueError as exc:
            raise ValueError(f"trusted_fake_ip_cidrs entry {raw!r} is not a valid CIDR") from exc

        if not isinstance(network, ipaddress.IPv4Network) or not network.subnet_of(
            RFC2544_FAKE_IP_NETWORK
        ):
            raise ValueError(
                "trusted_fake_ip_cidrs may only contain subnets of "
                f"{RFC2544_FAKE_IP_NETWORK}; got {network}"
            )
        networks.append(str(network))
    return networks


def configure_trusted_fake_ip_cidrs(values: Iterable[str]) -> None:
    """Configure process-wide fake-IP CIDRs trusted by URL fetch guards."""
    global _trusted_fake_ip_cidrs
    normalized = validate_trusted_fake_ip_cidrs(values)
    _trusted_fake_ip_cidrs = tuple(ipaddress.ip_network(value) for value in normalized)


def get_trusted_fake_ip_cidrs() -> list[str]:
    """Return the process-wide trusted fake-IP CIDRs as normalized strings."""
    return [str(network) for network in _trusted_fake_ip_cidrs]


def _is_metadata_address(addr: IPAddress) -> bool:
    """Return whether *addr* is a cloud metadata endpoint."""
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        # ``::ffff:169.254.169.254`` is the same endpoint wearing a different
        # hat, and compares equal to neither the IPv4 address nor the IPv4
        # networks. Unwrap before deciding.
        addr = addr.ipv4_mapped
    if addr in _METADATA_ADDRESSES:
        return True
    return any(
        addr.version == network.version and addr in network for network in _METADATA_NETWORKS
    )


def is_metadata_hostname(hostname: str) -> bool:
    """Return whether *hostname* names a cloud metadata service."""
    return hostname.strip().lower().rstrip(".") in _METADATA_HOSTNAMES


def assert_address_not_metadata(hostname: str, addr: IPAddress) -> None:
    """Raise :class:`SSRFBlockedError` if *addr* is a cloud metadata endpoint.

    The address-level half of :func:`assert_not_metadata_endpoint`, split out so
    the connect-time guard in :mod:`agentos.tools.ssrf_client` applies the exact
    same policy to the address it is about to open a socket to.
    """
    if _is_metadata_address(addr):
        raise SSRFBlockedError(
            f"Blocked request to {hostname}: it resolves to {addr}, a cloud "
            "metadata endpoint that serves instance credentials."
        )


def resolve_trusted_fake_ip_networks(
    trusted_fake_ip_cidrs: Iterable[str] | None = None,
) -> tuple[IPNetwork, ...]:
    """Return the fake-IP networks to trust, defaulting to the process-wide set."""
    if trusted_fake_ip_cidrs is None:
        return _trusted_fake_ip_cidrs
    return tuple(
        ipaddress.ip_network(value)
        for value in validate_trusted_fake_ip_cidrs(trusted_fake_ip_cidrs)
    )


def assert_address_allowed_for_fetch(
    hostname: str,
    addr: IPAddress,
    trusted_networks: tuple[IPNetwork, ...] = (),
) -> None:
    """Raise :class:`SSRFBlockedError` if *addr* is not a valid fetch target.

    The address-level half of :func:`validate_http_url_for_fetch`, split out so
    the connect-time guard in :mod:`agentos.tools.ssrf_client` applies the exact
    same policy to the address it is about to open a socket to.
    """
    block_reason = _hard_block_reason(addr)
    if block_reason is not None:
        raise SSRFBlockedError(_blocked_message(hostname, addr, block_reason))
    if _is_trusted_fake_ip(addr, trusted_networks):
        return
    if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
        reason = (
            f"reserved/private range; configure [tools].trusted_fake_ip_cidrs "
            f"with {RFC2544_FAKE_IP_NETWORK} only if this is fake-IP DNS"
            if addr in RFC2544_FAKE_IP_NETWORK
            else "private/internal range"
        )
        raise SSRFBlockedError(_blocked_message(hostname, addr, reason))


def assert_not_metadata_endpoint(url: str) -> None:
    """Raise :class:`SSRFBlockedError` if *url* targets a cloud metadata service.

    The security floor for tools that must keep reaching private addresses.
    ``http_request`` is routinely pointed at ``localhost`` and LAN services on
    purpose, so it cannot take the full :func:`validate_http_url_for_fetch`
    treatment — but no configuration makes the instance-credential endpoint a
    legitimate destination.

    A hostname that cannot be resolved is not blocked: the request that follows
    will fail on its own, and failing closed here would take the tool offline
    whenever DNS is unavailable.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return
    hostname = (parsed.hostname or "").strip().lower().rstrip(".")
    if not hostname:
        return
    if is_metadata_hostname(hostname):
        raise SSRFBlockedError(
            f"Blocked request to {hostname}: cloud metadata endpoints serve instance "
            "credentials and are never a valid agent target."
        )

    try:
        literal = ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        if _is_metadata_address(literal):
            raise SSRFBlockedError(
                f"Blocked request to {hostname}: link-local/metadata addresses serve "
                "instance credentials and are never a valid agent target."
            )
        return

    try:
        infos = socket.getaddrinfo(hostname, None)
    except (socket.gaierror, UnicodeError, ValueError):
        return
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:  # pragma: no cover - getaddrinfo returned a non-address
            continue
        assert_address_not_metadata(hostname, addr)


def validate_http_url_for_fetch(
    url: str,
    *,
    trusted_fake_ip_cidrs: Iterable[str] | None = None,
) -> None:
    """Validate that an HTTP(S) URL does not resolve to a blocked address."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsupportedURLSchemeError("Only HTTP/HTTPS URLs are supported")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Invalid URL: no hostname")

    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise ValueError(f"Cannot resolve hostname: {hostname}") from exc

    trusted_networks = resolve_trusted_fake_ip_networks(trusted_fake_ip_cidrs)

    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        assert_address_allowed_for_fetch(hostname, addr, trusted_networks)


def _hard_block_reason(addr: IPAddress) -> str | None:
    for network in _HARD_BLOCKED_NETWORKS:
        if addr.version == network.version and addr in network:
            return f"hard-blocked network {network}"
    return None


def _is_trusted_fake_ip(addr: IPAddress, trusted_networks: tuple[IPNetwork, ...]) -> bool:
    return any(addr.version == network.version and addr in network for network in trusted_networks)


def _blocked_message(hostname: str, addr: IPAddress, reason: str) -> str:
    return f"Blocked: {hostname} resolves to {addr} ({reason})"
