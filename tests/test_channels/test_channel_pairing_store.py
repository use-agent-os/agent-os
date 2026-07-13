from __future__ import annotations

import json
import os

import pytest

from agentos.channel_pairing import (
    PAIRING_CODE_ALPHABET,
    PAIRING_CODE_LENGTH,
    PAIRING_CODE_TTL_S,
    PAIRING_LOCKOUT_S,
    PAIRING_MAX_FAILED_ATTEMPTS,
    PAIRING_MAX_PENDING,
    PAIRING_REQUEST_RATE_LIMIT_S,
    ChannelPairingStore,
    InvalidPairingCodeError,
    PairingApprovalLockedError,
    PairingStoreError,
)


class Clock:
    def __init__(self, value: float = 1_700_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _profile(sender_id: str) -> dict[str, str]:
    return {
        "username": f"user{sender_id}",
        "display_name": f"User {sender_id}",
        "chat_id": sender_id,
    }


def test_pairing_request_and_approval_survive_new_store_instance(tmp_path) -> None:
    clock = Clock()
    root = tmp_path / "pairing"
    first = ChannelPairingStore(root, now=clock)

    request = first.request("telegram-main", "42", profile=_profile("42"))

    assert request.status == "created"
    assert request.created is True
    assert len(request.code) == PAIRING_CODE_LENGTH
    assert set(request.code) <= set(PAIRING_CODE_ALPHABET)

    second = ChannelPairingStore(root, now=clock)
    pending = second.snapshot("telegram-main")["pending"]
    assert pending[0]["code"] == request.code
    approved = second.approve("telegram-main", request.code.lower())

    assert approved["sender_id"] == "42"
    assert ChannelPairingStore(root, now=clock).is_approved("telegram-main", "42") is True
    assert second.snapshot("telegram-main")["pending"] == []
    if os.name != "nt":
        state_files = [path for path in root.iterdir() if path.suffix == ".json"]
        assert state_files
        assert all(path.stat().st_mode & 0o777 == 0o600 for path in state_files)
        assert root.stat().st_mode & 0o777 == 0o700


def test_pairing_request_is_rate_limited_after_denial(tmp_path) -> None:
    clock = Clock()
    store = ChannelPairingStore(tmp_path / "pairing", now=clock)
    request = store.request("tg", "42", profile=_profile("42"))
    store.deny("tg", "42")

    limited = store.request("tg", "42", profile=_profile("42"))

    assert request.status == "created"
    assert limited.status == "rate_limited"
    assert 0 < limited.retry_after_s <= PAIRING_REQUEST_RATE_LIMIT_S
    clock.advance(PAIRING_REQUEST_RATE_LIMIT_S + 1)
    assert store.request("tg", "42", profile=_profile("42")).status == "created"


def test_pairing_codes_expire_and_can_be_reissued(tmp_path) -> None:
    clock = Clock()
    store = ChannelPairingStore(tmp_path / "pairing", now=clock)
    first = store.request("tg", "42", profile=_profile("42"))

    clock.advance(PAIRING_CODE_TTL_S + 1)
    second = store.request("tg", "42", profile=_profile("42"))

    assert first.code != second.code
    assert store.snapshot("tg")["pending"][0]["code"] == second.code


def test_pairing_pending_queue_is_capped_per_channel(tmp_path) -> None:
    store = ChannelPairingStore(tmp_path / "pairing")
    for index in range(PAIRING_MAX_PENDING):
        result = store.request("tg", str(index), profile=_profile(str(index)))
        assert result.status == "created"

    capped = store.request("tg", "overflow", profile=_profile("overflow"))

    assert capped.status == "pending_limit"
    assert len(store.snapshot("tg")["pending"]) == PAIRING_MAX_PENDING


def test_invalid_codes_lock_approvals_for_one_hour(tmp_path) -> None:
    clock = Clock()
    store = ChannelPairingStore(tmp_path / "pairing", now=clock)
    request = store.request("tg", "42", profile=_profile("42"))

    for attempt in range(PAIRING_MAX_FAILED_ATTEMPTS):
        with pytest.raises(InvalidPairingCodeError) as raised:
            store.approve("tg", "ZZZZZZZZ")
        assert raised.value.attempts_remaining == max(
            0,
            PAIRING_MAX_FAILED_ATTEMPTS - attempt - 1,
        )

    with pytest.raises(PairingApprovalLockedError):
        store.approve("tg", request.code)

    clock.advance(PAIRING_LOCKOUT_S + 1)
    with pytest.raises(InvalidPairingCodeError):
        store.approve("tg", request.code)
    renewed = store.request("tg", "42", profile=_profile("42"))
    assert store.approve("tg", renewed.code)["sender_id"] == "42"


def test_corrupt_pairing_state_fails_closed_without_overwrite(tmp_path) -> None:
    root = tmp_path / "pairing"
    store = ChannelPairingStore(root)
    store.request("tg", "42", profile=_profile("42"))
    pending_path = next(root.glob("*-pending.json"))
    pending_path.write_text("not-json", encoding="utf-8")

    with pytest.raises(PairingStoreError):
        store.snapshot("tg")

    assert pending_path.read_text(encoding="utf-8") == "not-json"


def test_pairing_files_do_not_log_codes_in_control_state(tmp_path) -> None:
    root = tmp_path / "pairing"
    store = ChannelPairingStore(root)
    request = store.request("tg", "42", profile=_profile("42"))
    control = json.loads((root / "_rate_limits.json").read_text(encoding="utf-8"))

    assert request.code not in json.dumps(control)
