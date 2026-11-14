"""SSRF-guarded HTTP client: validate the address the socket actually uses.

The URL-level guards in :mod:`agentos.tools.ssrf` resolve a hostname once and
check what came back. The fetch that follows goes through ``httpx``, which
resolves the same hostname *again* when it opens the connection — so a
short-TTL (DNS-rebinding) domain can answer with a public address for the
guard's lookup and with ``169.254.169.254`` for the socket's. That is a
time-of-check/time-of-use window, and the cloud metadata endpoint sits on the
other side of it.

The fix is to remove the second resolution rather than narrow the window: a
network backend resolves the hostname itself at connect time, applies the same
policy to every address it got, and then connects to a validated **IP literal**.
The address that was checked is the address the socket uses, because nothing
resolves the name a second time.

TLS is unaffected. ``httpcore`` performs the handshake separately from the TCP
connect and passes ``server_hostname`` from the request origin (or the
``sni_hostname`` extension), never from the host it connected to — so SNI and
certificate verification still run against the origin hostname. Only the TCP
endpoint is pinned.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable
from typing import Any

import anyio
import httpcore
import httpx

from agentos.tools.ssrf import (
    IPAddress,
    assert_address_allowed_for_fetch,
    assert_address_not_gateway,
    assert_address_not_metadata,
    is_metadata_hostname,
    resolve_trusted_fake_ip_networks,
)
from agentos.tools.types import SSRFBlockedError

#: The real client class, captured at import. Callers construct through
#: ``httpx.AsyncClient`` so a test that monkeypatches that attribute still gets
#: its double; this reference is only used to tell a real client (which must be
#: guarded, or the call fails closed) from such a double (which never opens a
#: socket).
_REAL_ASYNC_CLIENT = httpx.AsyncClient

#: Applied to every address a hostname resolves to, immediately before connect,
#: as ``validator(hostname, addr, port)``.
AddressValidator = Callable[[str, IPAddress, int], None]

__all__ = [
    "AddressValidator",
    "ValidatingNetworkBackend",
    "ssrf_guarded_client",
    "validate_agent_http_address",
    "validate_fetch_address",
    "validate_metadata_only_address",
]


def validate_fetch_address(hostname: str, addr: IPAddress, port: int | None = None) -> None:
    """Full fetch policy: no private, loopback, link-local or reserved targets.

    Used by ``web_fetch``, the media image fetch and skill-dependency downloads
    — tools that only ever have business on the public internet.
    """
    assert_address_allowed_for_fetch(hostname, addr, resolve_trusted_fake_ip_networks())


def validate_metadata_only_address(hostname: str, addr: IPAddress, port: int | None = None) -> None:
    """Security floor: block cloud metadata endpoints, allow private addresses.

    Used by MCP HTTP servers, cron webhooks and ``x_search``, which are pointed
    at ``localhost`` and LAN services on purpose and so cannot take the full
    fetch policy.
    """
    assert_address_not_metadata(hostname, addr)


def validate_agent_http_address(hostname: str, addr: IPAddress, port: int) -> None:
    """Metadata floor plus the gateway's own listener.

    Used by ``http_request``, whose URL, method and body are model-chosen:
    private addresses stay reachable (local dev servers), but the gateway's
    Control API on this host never is.
    """
    assert_address_not_metadata(hostname, addr)
    assert_address_not_gateway(hostname, addr, port)


def _as_ip_literal(host: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(host.strip().strip("[]"))
    except ValueError:
        return None


def _getaddrinfo_addresses(host: str, port: int) -> list[IPAddress]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses: list[IPAddress] = []
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:  # pragma: no cover - getaddrinfo returned a non-address
            continue
        if addr not in addresses:
            addresses.append(addr)
    return addresses


class ValidatingNetworkBackend(httpcore.AsyncNetworkBackend):
    """Network backend that validates the destination address it connects to.

    Wraps another backend, resolving and checking the hostname itself and then
    delegating the connect to a validated IP literal. Everything else — TLS,
    HTTP/1.1 and HTTP/2, timeouts, socket options — is the wrapped backend's
    behaviour, unchanged.
    """

    def __init__(
        self,
        validator: AddressValidator,
        backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._validator = validator
        self._backend = backend if backend is not None else httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        addresses = await self._validated_addresses(host, port)

        last_error: Exception | None = None
        for addr in addresses:
            try:
                return await self._backend.connect_tcp(
                    str(addr),
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as exc:
                # Every candidate here has already passed the guard, so trying
                # the next one cannot reach a blocked address — it is the same
                # fallback a resolver-driven connect would do internally.
                last_error = exc
        assert last_error is not None  # addresses is never empty past the guard
        raise last_error

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        # No name resolution, no rebinding window: a UDS path is the endpoint.
        return await self._backend.connect_unix_socket(
            path, timeout=timeout, socket_options=socket_options
        )

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)

    async def _validated_addresses(self, host: str, port: int) -> list[IPAddress]:
        """Resolve *host* and return the addresses, or raise if any is blocked."""
        hostname = host.strip().lower().rstrip(".")
        if is_metadata_hostname(hostname):
            raise SSRFBlockedError(
                f"Blocked request to {hostname}: cloud metadata endpoints serve instance "
                "credentials and are never a valid agent target."
            )

        literal = _as_ip_literal(host)
        if literal is not None:
            addresses = [literal]
        else:
            try:
                addresses = await anyio.to_thread.run_sync(_getaddrinfo_addresses, host, port)
            except (socket.gaierror, UnicodeError, ValueError) as exc:
                raise httpcore.ConnectError(f"Cannot resolve hostname: {host}") from exc
            if not addresses:
                raise httpcore.ConnectError(f"Cannot resolve hostname: {host}")

        for addr in addresses:
            self._validator(hostname, addr, port)
        return addresses


def _install_backend(client: httpx.AsyncClient, validator: AddressValidator) -> None:
    """Swap the validating backend onto *client*'s default connection pool.

    Only the default transport is touched. Proxy mounts keep their own
    transports: a forwarding proxy connects to the *proxy*, which is often a
    private address on purpose, and the destination name is resolved by the
    proxy rather than by this process — there is no local rebinding window to
    close there.

    A caller that supplied its own transport (a mock, a recorder) owns its
    connect path and is left alone. A stock transport whose pool cannot be
    reached is a different story — that means httpx changed shape underneath
    us, and returning an unguarded client there would silently reopen the
    rebinding window, so it raises instead.
    """
    transport = client._transport
    if not isinstance(transport, httpx.AsyncHTTPTransport):
        return
    pool = getattr(transport, "_pool", None)
    backend = getattr(pool, "_network_backend", None)
    if pool is None or backend is None:  # pragma: no cover - httpx internals changed
        raise RuntimeError(
            "Cannot install the SSRF-validating network backend on httpx "
            f"{httpx.__version__} (transport {type(transport).__name__}); refusing to "
            "fetch without connect-time SSRF validation."
        )
    pool._network_backend = ValidatingNetworkBackend(validator, backend)


def ssrf_guarded_client(
    *,
    validator: AddressValidator = validate_fetch_address,
    **kwargs: Any,
) -> httpx.AsyncClient:
    """Return an ``httpx.AsyncClient`` that validates addresses at connect time.

    A plain client built from *kwargs* — every httpx default, including
    environment proxy mounts, is preserved — with the default pool's network
    backend wrapped so the address it dials is the address the guard approved.
    """
    client = httpx.AsyncClient(**kwargs)
    if isinstance(client, _REAL_ASYNC_CLIENT):
        # Installation failing raises rather than returning an unguarded
        # client; no sockets exist yet, so the half-built client is dropped.
        _install_backend(client, validator)
    return client
