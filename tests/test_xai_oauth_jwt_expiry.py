"""Issue #3554: a non-finite ``exp`` decided the refresh question wrongly.

``_jwt_expiry`` returned ``float(exp)`` for any number in the token payload,
and ``json.loads`` accepts the non-standard literals ``NaN``, ``Infinity``
and ``-Infinity``. A NaN or an infinity read as "not expiring" for ever, so
the proactive refresh never ran; ``-Infinity`` read as "expiring" on every
call, and this module serializes refresh precisely because xAI's refresh
tokens are single-use -- every credential resolution would have spent one.

An expiry nobody can read is no expiry: ``None`` is what both callers
already handle (no proactive refresh, maximum skew, refresh on a 401).
"""

from __future__ import annotations

import base64
import json
import time

import pytest

from agentos.xai_oauth import (
    MAX_REFRESH_SKEW_SECONDS,
    _jwt_expiry,
    access_token_is_expiring,
    proactive_skew_seconds,
)


def _token(payload: str) -> str:
    """A JWT whose payload segment is *payload* verbatim (never verified)."""
    head = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    body = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{head}.{body}.signature"


def _with_exp(literal: str) -> str:
    return _token(f'{{"exp": {literal}}}')


#: Literals ``json.loads`` accepts that are not an expiry.
UNUSABLE = [
    pytest.param("NaN", id="nan"),
    pytest.param("Infinity", id="infinity"),
    pytest.param("-Infinity", id="minus_infinity"),
    pytest.param("true", id="true"),
    pytest.param("false", id="false"),
]


# ── the report ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("literal", UNUSABLE)
def test_an_unusable_exp_reads_as_no_expiry(literal: str) -> None:
    assert _jwt_expiry(_with_exp(literal)) is None


@pytest.mark.parametrize("literal", UNUSABLE)
def test_an_unusable_exp_does_not_claim_the_token_is_expiring(literal: str) -> None:
    """``-Infinity`` used to answer True here, on every single call, and each
    answer costs one single-use refresh token."""
    assert access_token_is_expiring(_with_exp(literal), 60) is False


@pytest.mark.parametrize("literal", UNUSABLE)
def test_an_unusable_exp_falls_back_to_the_maximum_skew(literal: str) -> None:
    assert proactive_skew_seconds(_with_exp(literal)) == MAX_REFRESH_SKEW_SECONDS


def test_json_really_does_parse_those_literals() -> None:
    """Why the guard is needed at all: this is not a hypothetical shape."""
    assert json.loads('{"exp": NaN}')["exp"] != json.loads('{"exp": NaN}')["exp"]  # NaN != NaN
    assert json.loads('{"exp": Infinity}')["exp"] == float("inf")


# ── what must not change ────────────────────────────────────────────────────


def test_a_token_expiring_soon_still_says_so() -> None:
    token = _with_exp(str(int(time.time()) + 30))

    assert access_token_is_expiring(token, 60) is True


def test_a_token_with_room_left_is_not_expiring() -> None:
    token = _with_exp(str(int(time.time()) + 10_000))

    assert access_token_is_expiring(token, 60) is False


def test_an_already_expired_token_says_so() -> None:
    token = _with_exp(str(int(time.time()) - 10))

    assert access_token_is_expiring(token) is True


def test_a_fractional_exp_is_still_an_expiry() -> None:
    expiry = time.time() + 120.5

    assert _jwt_expiry(_with_exp(repr(expiry))) == pytest.approx(expiry)


def test_an_opaque_token_is_still_no_expiry() -> None:
    assert _jwt_expiry("not-a-jwt") is None
    assert _jwt_expiry(_token("not json")) is None
    assert _jwt_expiry(_token('{"sub": "me"}')) is None


def test_a_short_lived_token_still_gets_the_short_skew() -> None:
    """The one branch that reads the number rather than its presence."""
    soon = _with_exp(str(int(time.time()) + 60))

    assert proactive_skew_seconds(soon) < MAX_REFRESH_SKEW_SECONDS
