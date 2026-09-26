"""Issue #3432: redaction of URL userinfo was gated on a scheme allowlist.

``_URL_USERINFO_RE`` matched ``https?://`` only, and ``_DB_CONNSTR_RE`` a list
of five database schemes. Every other scheme handed its password to the model
verbatim -- ``ws``/``wss`` (a gateway URL with basic auth), ``ftp``, ``sftp``,
``ssh``, ``smtp``, ``ldap``, and any database scheme outside that list.

A password in a URL's userinfo is a credential whatever the scheme in front of
it is, so the redaction-only pattern now matches the *structure* instead of a
list of names. An allowlist only ever covers the schemes someone thought of.

Scope: this touches ``_URL_USERINFO_RE`` only. ``_DB_CONNSTR_RE`` is shared
with the payload guard (``secret_literal_marker`` -> ``connection_string``)
and is left exactly as it is -- #3373 is widening that one separately.
"""

from __future__ import annotations

import pytest

from agentos.redact import redact_sensitive_text

_SECRET = "s3cr3t-pw"


@pytest.mark.parametrize(
    "url",
    [
        "wss://user:{pw}@gateway.host/ws",
        "ws://user:{pw}@gateway.host/ws",
        "ftp://user:{pw}@files.host/x",
        "sftp://user:{pw}@host/x",
        "ssh://user:{pw}@host",
        "smtp://user:{pw}@mail.host:587",
        "ldap://user:{pw}@directory.host",
        "clickhouse://user:{pw}@db:9000",
        "mariadb://user:{pw}@db",
        "cassandra://user:{pw}@db",
    ],
)
def test_a_password_is_masked_whatever_the_scheme(url: str) -> None:
    """The issue's repro, across the schemes the allowlist never named."""
    redacted = redact_sensitive_text(url.format(pw=_SECRET))

    assert _SECRET not in redacted
    assert "***" in redacted


@pytest.mark.parametrize(
    "url",
    [
        "redis://:{pw}@cache:6379/0",
        "amqp://:{pw}@broker:5672",
        "postgres://:{pw}@db:5432/app",
        "rediss://:{pw}@cache:6379",
        "wss://:{pw}@gateway.host/ws",
    ],
)
def test_an_empty_username_no_longer_defeats_the_match(url: str) -> None:
    """``redis://:pw@host`` is the canonical Redis spelling and carries no
    username, which the old ``[^:\\s/]+`` could not match. Structural matching
    closes that too -- the subject of #3367, which this does not claim to own."""
    redacted = redact_sensitive_text(url.format(pw=_SECRET))

    assert _SECRET not in redacted


@pytest.mark.parametrize(
    "url",
    ["https://user:{pw}@host/x", "http://user:{pw}@host", "postgres://u:{pw}@db/app"],
)
def test_the_schemes_that_already_worked_still_do(url: str) -> None:
    redacted = redact_sensitive_text(url.format(pw=_SECRET))

    assert _SECRET not in redacted
    assert "***" in redacted


def test_the_host_and_scheme_survive_so_the_line_stays_readable() -> None:
    """Masking the secret must not cost the operator the diagnostic."""
    redacted = redact_sensitive_text(f"wss://user:{_SECRET}@gateway.host:443/ws")

    assert redacted == "wss://user:***@gateway.host:443/ws"


@pytest.mark.parametrize(
    "text",
    [
        "https://example.com:8080/path",  # a port, not a credential
        "http://host/a@b",  # an @ in the path
        "see http://plain.host/x for docs",
        "mailto:user@host",  # no ://
        "git+ssh://git@github.com/owner/repo.git",  # user, no password
        "key: value",
        "ratio 3:1 @ noon",
    ],
)
def test_ordinary_text_is_not_touched(text: str) -> None:
    """A structural match is wider than a scheme list, so the boundary matters
    more: a port, an ``@`` in a path, or a userinfo with no password must all
    pass through unchanged."""
    assert redact_sensitive_text(text) == text


def test_a_scheme_with_a_plus_or_dot_is_still_matched() -> None:
    """RFC 3986 scheme characters, so ``mongodb+srv`` and the like keep
    working through the structural pattern."""
    redacted = redact_sensitive_text(f"mongodb+srv://user:{_SECRET}@cluster.net/db")

    assert _SECRET not in redacted
