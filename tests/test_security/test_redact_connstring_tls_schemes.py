"""The TLS spellings of the connection-string schemes went through unmasked.

`_DB_CONNSTR_RE` lists `postgres(ql)?`, `mysql`, `mongodb(+srv)?`, `redis` and
`amqp`; `mongodb+srv` shows a variant of a covered scheme is carried on
purpose. `rediss://` and `amqps://` -- the TLS spellings Heroku/Upstash issue
as `REDIS_TLS_URL` and TLS AMQP endpoints are written in -- were not in the
list, so the password reached the model while the plain `redis://` / `amqp://`
spelling was masked. The same regex backs the payload guard's
`connection_string` case, so both layers missed it (#3373).
"""

from __future__ import annotations

import pytest

from agentos.redact import redact_file_output, redact_sensitive_text, secret_literal_marker

SECRET = "s3cr3tp4ssw0rd"


# ── the report ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "connection_string",
    [
        f"rediss://user:{SECRET}@cache:6379/0",
        f"rediss://default:{SECRET}@cache.example.com:6379",
        f"amqps://user:{SECRET}@rabbit:5671/",
        f"amqps://billing:{SECRET}@rabbit.example.com:5671/vhost",
    ],
)
def test_the_tls_spellings_mask_their_password(connection_string):
    assert SECRET not in redact_sensitive_text(connection_string, force=True)


def test_the_plain_spellings_still_mask_unchanged():
    """Positive control: the gap was the scheme, not the shape."""
    masked = redact_sensitive_text(f"redis://user:{SECRET}@cache:6379/0", force=True)
    assert SECRET not in masked
    assert masked.startswith("redis://user:")
    masked = redact_sensitive_text(f"amqp://user:{SECRET}@rabbit:5672/", force=True)
    assert SECRET not in masked


def test_it_masks_through_the_file_read_surface():
    """The path an agent actually takes: file content handed back to the model."""
    env_file = "\n".join(
        [
            "APP_ENV=production",
            f"REDIS_TLS_URL=rediss://default:{SECRET}@cache.example.com:6379",
            f"AMQP_URL=amqps://billing:{SECRET}@rabbit.example.com:5671/vhost",
        ]
    )
    assert SECRET not in redact_file_output(env_file, path=".env")


def test_the_payload_guard_sees_the_tls_spelling_too():
    """The guard shares `_DB_CONNSTR_RE`; the miss was both layers'."""
    assert secret_literal_marker(f"psql amqps://user:{SECRET}@rabbit:5671/") == "connection_string"
    assert secret_literal_marker(f"rediss://user:{SECRET}@cache:6379/0") == "connection_string"


def test_a_tls_url_without_a_password_is_not_connection_material():
    """Anti-drift: the scheme alone must not start matching passwordless URLs."""
    assert secret_literal_marker("rediss://cache.example.com:6379") is None
    assert secret_literal_marker("amqps://rabbit.example.com:5671/vhost") is None
