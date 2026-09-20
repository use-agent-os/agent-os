import ipaddress

import pytest

from agentos.tools.ssrf import (
    assert_address_allowed_for_fetch,
    validate_http_url_for_fetch,
)
from agentos.tools.types import SSRFBlockedError


def test_assert_address_allowed_for_fetch_blocks_multicast() -> None:
    multicast_addrs = [
        ipaddress.ip_address("224.0.0.1"),
        ipaddress.ip_address("239.255.255.250"),
        ipaddress.ip_address("ff02::1"),
        ipaddress.ip_address("ff05::2"),
    ]
    for addr in multicast_addrs:
        with pytest.raises(SSRFBlockedError):
            assert_address_allowed_for_fetch("multicast.local", addr)


def test_validate_http_url_for_fetch_blocks_multicast_urls() -> None:
    multicast_urls = [
        "http://224.0.0.1/status",
        "http://239.255.255.250:1900/description.xml",
        "http://[ff02::1]/status",
    ]
    for url in multicast_urls:
        with pytest.raises(SSRFBlockedError):
            validate_http_url_for_fetch(url)


def test_assert_address_allowed_for_fetch_allows_public_ips() -> None:
    assert_address_allowed_for_fetch("dns.google", ipaddress.ip_address("8.8.8.8"))
    assert_address_allowed_for_fetch("dns.google", ipaddress.ip_address("2001:4860:4860::8888"))
