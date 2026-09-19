"""Verify EmailChannel conversion failure in _to_incoming
increments retry count and quarantines poison messages.
"""

from __future__ import annotations

import imaplib
from email.message import EmailMessage
from email.policy import default as email_policy
from typing import Any
from unittest.mock import MagicMock, patch

from agentos.channels.email import EmailChannel, EmailChannelConfig


def _config(**overrides: Any) -> EmailChannelConfig:
    base: dict[str, Any] = {
        "name": "inbox",
        "imap_host": "imap.example.com",
        "imap_username": "agent@example.com",
        "imap_password": "secret",
        "smtp_host": "smtp.example.com",
        "smtp_username": "agent@example.com",
        "smtp_password": "secret",
        "from_address": "agent@example.com",
        "from_name": "Agent",
        "allowed_senders": ["owner@example.com"],
    }
    base.update(overrides)
    return EmailChannelConfig(**base)


def _build_rfc822_message() -> bytes:
    msg = EmailMessage()
    msg["From"] = "owner@example.com"
    msg["To"] = "agent@example.com"
    msg["Subject"] = "Hello"
    msg["Message-ID"] = "<abc123@example.com>"
    msg.set_content("test body")
    return msg.as_bytes(policy=email_policy)


def _make_mock_client(uids: list[bytes], message_bytes: bytes) -> MagicMock:
    client = MagicMock(spec=imaplib.IMAP4_SSL)
    client.select.return_value = ("OK", [b"1"])
    uid_line = b" ".join(uids)

    def _uid_dispatch(command: str, *args: Any) -> tuple[str, list[Any]]:
        cmd = command.upper()
        if cmd == "SEARCH":
            return ("OK", [uid_line])
        if cmd == "FETCH":
            data_part = args[1] if len(args) > 1 else ""
            if "RFC822.SIZE" in str(data_part):
                return ("OK", [(b"100 (RFC822.SIZE " + str(len(message_bytes)).encode() + b")",)])
            if "BODY.PEEK" in str(data_part):
                return ("OK", [(b"100 (BODY[]", message_bytes), b")"])
            return ("OK", [])
        if cmd == "STORE":
            return ("OK", [])
        return ("OK", [])

    client.uid.side_effect = _uid_dispatch
    client.close.return_value = ("OK", [])
    client.logout.return_value = ("BYE", [])
    return client


def test_to_incoming_exception_increments_attempts_and_quarantines() -> None:
    channel = EmailChannel(config=_config())
    msg_bytes = _build_rfc822_message()
    client = _make_mock_client([b"100"], msg_bytes)

    with patch.object(channel, "_imap_connect", return_value=client):
        with patch.object(channel, "_to_incoming", side_effect=ValueError("corrupt attachment")):
            # Poll 1: Attempt 1, should register retry, not yet seen
            res1 = channel._fetch_unseen()
            assert res1 == []
            assert channel._fetch_attempts.get("100") == 1
            client.uid.assert_any_call("FETCH", "100", "(BODY.PEEK[])")
            seen_calls_poll1 = [
                c for c in client.uid.call_args_list if c[0][0] == "STORE" and "\\Seen" in c[0][3]
            ]
            assert len(seen_calls_poll1) == 0

            # Poll 2: Attempt 2, should increment attempt count to 2
            res2 = channel._fetch_unseen()
            assert res2 == []
            assert channel._fetch_attempts.get("100") == 2
            seen_calls_poll2 = [
                c for c in client.uid.call_args_list if c[0][0] == "STORE" and "\\Seen" in c[0][3]
            ]
            assert len(seen_calls_poll2) == 0

            # Poll 3: Attempt 3 (MAX_FETCH_ATTEMPTS), should quarantine and mark seen
            res3 = channel._fetch_unseen()
            assert res3 == []
            assert "100" not in channel._fetch_attempts
            seen_calls_poll3 = [
                c for c in client.uid.call_args_list if c[0][0] == "STORE" and "\\Seen" in c[0][3]
            ]
            assert len(seen_calls_poll3) == 1
