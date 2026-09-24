r"""A connection string with an empty username went through unmasked.

`_DB_CONNSTR_RE` lists `postgres`, `mysql`, `mongodb`, `redis` and `amqp`
deliberately, and `_URL_USERINFO_RE`'s comment says "userinfo in a web URL is a
credential the same way a DSN password is". Both required a **non-empty**
username before the colon:

    r"(?:...|redis|amqp)://[^:\s/]+:"
                          ^^^^^^^^^^

`redis://:password@host` is the canonical Redis URL -- Redis had no usernames
before ACLs, so the empty field is what a `REDIS_URL` holds in practice -- and
`postgres`, `amqp`, `mongodb` and `mysql` all accept the same shape. The one
spelling these schemes are usually written in was the one that reached the model
unmasked, while `redis://user:password@host` was masked correctly.

`redact_file_output` is "one policy for every file-read surface -- `read_file`,
`read_spreadsheet`, `grep_search` and `edit_file`'s closest-match hint alike",
and `AGENTOS_REDACT_SECRETS` defaults to on, so this is the default path for an
agent reading a `.env`, a `docker-compose.yml` or a `settings.py`.
"""

from __future__ import annotations

import pytest

from agentos.redact import redact_file_output, redact_sensitive_text

SECRET = "s3cr3tp4ssw0rd"


# ── the report ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "connection_string",
    [
        f"redis://:{SECRET}@cache:6379/0",
        f"postgres://:{SECRET}@db:5432/app",
        f"postgresql://:{SECRET}@db:5432/app",
        f"mysql://:{SECRET}@mysql:3306/app",
        f"mongodb://:{SECRET}@mongo:27017/app",
        f"mongodb+srv://:{SECRET}@mongo/app",
        f"amqp://:{SECRET}@rabbit:5672/",
    ],
)
def test_an_empty_username_still_masks_the_password(connection_string):
    assert SECRET not in redact_sensitive_text(connection_string, force=True)


def test_a_web_url_with_empty_userinfo_is_masked_too():
    """`_URL_USERINFO_RE` had the identical `+` and the same consequence."""
    assert SECRET not in redact_sensitive_text(f"https://:{SECRET}@example.com/x", force=True)


def test_the_named_form_was_already_masked():
    """Positive control: the gap was the empty field, not the scheme."""
    masked = redact_sensitive_text(f"redis://user:{SECRET}@cache:6379/0", force=True)
    assert SECRET not in masked
    assert masked.startswith("redis://user:")


def test_it_masks_through_the_file_read_surface():
    """The path an agent actually takes: file content handed back to the model."""
    env_file = "\n".join(
        [
            "APP_ENV=production",
            f"REDIS_URL=redis://:{SECRET}@cache:6379/0",
            "LOG_LEVEL=info",
        ]
    )
    out = redact_file_output(env_file, path=".env")

    assert SECRET not in out
    assert "APP_ENV=production" in out
    assert "LOG_LEVEL=info" in out


# ── nothing benign starts being masked ─────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "see https://example.com/docs for details",
        "visit https://example.com:8080/health",
        "clone https://github.com/use-agent-os/agent-os.git",
        "redis://cache:6379/0",
        "postgres://db:5432/app",
        "contact alice@example.com about it",
        "the ratio is 3:1@scale",
        "docker run -p 8080:80 nginx",
        "![img](https://cdn.example.com/a.png)",
    ],
)
def test_a_string_with_no_credential_is_returned_unchanged(text):
    assert redact_sensitive_text(text, force=True) == text


def test_a_host_and_port_are_kept_so_the_dsn_stays_recognisable():
    """The mask replaces the password only — the rest stays readable."""
    masked = redact_sensitive_text(f"redis://:{SECRET}@cache:6379/0", force=True)
    assert masked.startswith("redis://:")
    assert masked.endswith("@cache:6379/0")
