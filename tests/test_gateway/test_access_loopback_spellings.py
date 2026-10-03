"""Issue #3572: is_loopback_address compared IPv6 spellings as strings.

One IPv6 address has many valid textual forms. ``peer_is_trusted_proxy`` in
this same module was already changed to compare addresses rather than
spellings, and its docstring names the reason -- "``::1`` was not matching a
peer reported as ``0:0:0:0:0:0:0:1``". ``is_loopback_address``, forty lines
above it, still matched ``"::1"`` literally.

It gates more than the proxy set does: the no-auth listener
(``auth.mode = "none"`` admits only a loopback bind *and* a loopback peer),
the ``Host`` header guard in the middleware, and the control-UI ``Origin``
guard. Each fails closed, so the symptom is a local client refused with a
message about auth or about the Origin rather than about how the address was
written.
"""

from __future__ import annotations

import ipaddress

import pytest

from agentos.gateway.access import (
    is_loopback_address,
    is_loopback_bind,
    peer_is_trusted_proxy,
)

LOOPBACK_SPELLINGS = [
    "::1",
    "0:0:0:0:0:0:0:1",
    "::0001",
    "0000:0000:0000:0000:0000:0000:0000:0001",
    "[::1]",
    "[0:0:0:0:0:0:0:1]",
    "::1%lo0",
    "::ffff:127.0.0.1",
    "::ffff:7f00:1",
    "127.0.0.1",
    "127.1.2.3",
    "127.255.255.254",
]

NOT_LOOPBACK = [
    "192.168.1.5",
    "10.0.0.1",
    "::2",
    "fe80::1",
    "fe80::1%eth0",
    "2001:db8::1",
    "example.com",
    "evil-127.0.0.1.attacker.test",
    "",
]


# ── the issue's reproduction ───────────────────────────────────────────────


@pytest.mark.parametrize("spelling", LOOPBACK_SPELLINGS)
def test_every_spelling_of_the_loopback_is_recognised(spelling: str) -> None:
    assert is_loopback_address(spelling) is True


@pytest.mark.parametrize("spelling", LOOPBACK_SPELLINGS)
def test_the_verdict_matches_the_stdlib(spelling: str) -> None:
    """Pins the rule rather than the list: whatever ``ipaddress`` calls a
    loopback is one, including through an IPv4 mapping."""
    host = spelling.strip("[]").split("%", 1)[0]
    address = ipaddress.ip_address(host)
    mapped = getattr(address, "ipv4_mapped", None)

    assert address.is_loopback or (mapped is not None and mapped.is_loopback)


def test_the_two_predicates_in_this_module_now_agree() -> None:
    """``peer_is_trusted_proxy`` has handled this since it was fixed; the
    loopback predicate beside it had not."""
    assert peer_is_trusted_proxy("::1", "0:0:0:0:0:0:0:1") is True
    assert is_loopback_address("0:0:0:0:0:0:0:1") is True


def test_a_gateway_bound_to_an_expanded_loopback_counts_as_loopback() -> None:
    """``config.host`` is typed by a person, so any spelling can land here."""
    assert is_loopback_bind("0:0:0:0:0:0:0:1") is True
    assert is_loopback_bind("::0001") is True


# ── what must not change ───────────────────────────────────────────────────


@pytest.mark.parametrize("spelling", NOT_LOOPBACK)
def test_a_non_loopback_address_is_still_refused(spelling: str) -> None:
    assert is_loopback_address(spelling) is False


def test_none_is_not_a_loopback() -> None:
    assert is_loopback_address(None) is False


def test_localhost_is_still_accepted_by_name() -> None:
    """A name, not a literal, so it is answered before parsing."""
    assert is_loopback_address("localhost") is True
    assert is_loopback_bind("localhost") is True


def test_a_hostname_is_never_read_as_an_address() -> None:
    """The widening is to other spellings of an address, not to names: an
    unparseable value stays False so DNS cannot decide this gate."""
    for name in ["localhost.attacker.test", "loopback", "127.0.0.1.nip.io"]:
        assert is_loopback_address(name) is False


@pytest.mark.parametrize(
    "malformed",
    ["127.999.0.1", "127.1.2.3.4", "127.", "::ffff:", "1.2.3", "127.0.0.1/8"],
)
def test_a_malformed_literal_is_refused(malformed: str) -> None:
    assert is_loopback_address(malformed) is False


def test_an_octet_with_a_leading_zero_is_refused() -> None:
    """``127.00000.0.1`` used to pass through ``int()`` and count as
    loopback. Leading zeros are ambiguous (octal in some resolvers) and
    ``ipaddress`` rejects them, so it is no longer read as an address at
    all -- the narrower reading, and this gate fails closed."""
    assert is_loopback_address("127.00000.0.1") is False
    assert is_loopback_address("0177.0.0.1") is False


def test_surrounding_whitespace_does_not_change_the_verdict() -> None:
    assert is_loopback_address(" 127.0.0.1 ") is True
    assert is_loopback_address(" ::1 ") is True
