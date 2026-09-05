"""Verify the email adapter polls IMAP via UID commands, not sequence numbers.

IMAP sequence numbers are ephemeral — they renumber when any message in the
mailbox is expunged by another client (RFC 3501 §2.3.1.2).  Using bare
``search``/``fetch``/``store`` operates on sequence numbers; the adapter must
use ``client.uid("SEARCH", ...)`` etc. to reference messages by their stable,
monotonically-increasing UIDs.
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


def _build_rfc822_message(
    *,
    sender: str = "owner@example.com",
    subject: str = "Hello",
    body: str = "test body",
    message_id: str = "abc123@example.com",
) -> bytes:
    """Return a minimal RFC 822 message as raw bytes."""
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "agent@example.com"
    msg["Subject"] = subject
    msg["Message-ID"] = f"<{message_id}>"
    msg.set_content(body)
    return msg.as_bytes(policy=email_policy)


def _make_mock_client(
    uids: list[bytes],
    message_bytes: bytes,
    message_size: int | None = None,
) -> MagicMock:
    """Return a mock IMAP4_SSL client that records uid() calls.

    The mock responds to uid("SEARCH", ...), uid("FETCH", ...), and
    uid("STORE", ...) so that ``_fetch_unseen`` can run end-to-end.
    """
    client = MagicMock(spec=imaplib.IMAP4_SSL)

    # select returns OK
    client.select.return_value = ("OK", [b"1"])

    # uid("SEARCH", "UNSEEN") → list of UIDs
    uid_line = b" ".join(uids) if uids else b""

    size = message_size or len(message_bytes)

    def _uid_dispatch(command: str, *args: Any) -> tuple[str, list[Any]]:
        cmd = command.upper()
        if cmd == "SEARCH":
            return ("OK", [uid_line])
        if cmd == "FETCH":
            uid_val = args[0] if args else b""
            data_part = args[1] if len(args) > 1 else ""
            if "RFC822.SIZE" in str(data_part):
                return (
                    "OK",
                    [(f"{uid_val} (RFC822.SIZE {size})".encode(),)],
                )
            if "BODY.PEEK" in str(data_part) or "BODY" in str(data_part):
                return ("OK", [(b"1 (BODY[]", message_bytes), b")"])
            return ("OK", [])
        if cmd == "STORE":
            return ("OK", [])
        return ("OK", [])

    client.uid.side_effect = _uid_dispatch

    # close and logout should not raise
    client.close.return_value = ("OK", [])
    client.logout.return_value = ("BYE", [])

    return client


class TestEmailImapUidCommands:
    """Assert that _fetch_unseen uses client.uid() instead of bare methods."""

    def test_fetch_unseen_uses_uid_search(self) -> None:
        """client.uid('SEARCH', ...) must be called, not client.search()."""
        raw = _build_rfc822_message()
        mock_client = _make_mock_client([b"42"], raw)

        channel = EmailChannel(config=_config())
        with patch.object(channel, "_imap_connect", return_value=mock_client):
            channel._fetch_unseen()

        # uid("SEARCH", ...) must have been called
        uid_calls = [
            call for call in mock_client.uid.call_args_list
            if call[0][0].upper() == "SEARCH"
        ]
        assert uid_calls, "Expected client.uid('SEARCH', ...) to be called"

        # bare search() must NOT have been called
        mock_client.search.assert_not_called()

    def test_fetch_unseen_uses_uid_fetch(self) -> None:
        """client.uid('FETCH', ...) must be called, not client.fetch()."""
        raw = _build_rfc822_message()
        mock_client = _make_mock_client([b"42"], raw)

        channel = EmailChannel(config=_config())
        with patch.object(channel, "_imap_connect", return_value=mock_client):
            channel._fetch_unseen()

        # uid("FETCH", ...) must have been called
        fetch_calls = [
            call for call in mock_client.uid.call_args_list
            if call[0][0].upper() == "FETCH"
        ]
        assert fetch_calls, "Expected client.uid('FETCH', ...) to be called"

        # bare fetch() must NOT have been called
        mock_client.fetch.assert_not_called()

    def test_mark_seen_uses_uid_store(self) -> None:
        """client.uid('STORE', ...) must be called, not client.store()."""
        raw = _build_rfc822_message()
        mock_client = _make_mock_client([b"42"], raw)

        channel = EmailChannel(config=_config(mark_seen=True))
        with patch.object(channel, "_imap_connect", return_value=mock_client):
            channel._fetch_unseen()

        # uid("STORE", ...) must have been called
        store_calls = [
            call for call in mock_client.uid.call_args_list
            if call[0][0].upper() == "STORE"
        ]
        assert store_calls, "Expected client.uid('STORE', ...) to be called"

        # bare store() must NOT have been called
        mock_client.store.assert_not_called()

    def test_fetch_unseen_returns_parsed_message(self) -> None:
        """The full pipeline should return an IncomingMessage from a UID fetch."""
        raw = _build_rfc822_message(
            sender="owner@example.com",
            subject="Check status",
            body="What is the status?",
            message_id="msg-uid-test@example.com",
        )
        mock_client = _make_mock_client([b"100"], raw)

        channel = EmailChannel(config=_config())
        with patch.object(channel, "_imap_connect", return_value=mock_client):
            messages = channel._fetch_unseen()

        assert len(messages) == 1
        msg = messages[0]
        assert "What is the status?" in msg.content
        assert msg.sender_id == "owner@example.com"

    def test_empty_unseen_returns_empty_list(self) -> None:
        """No UNSEEN messages → empty list, no FETCH or STORE calls."""
        mock_client = _make_mock_client([], b"")

        channel = EmailChannel(config=_config())
        with patch.object(channel, "_imap_connect", return_value=mock_client):
            messages = channel._fetch_unseen()

        assert messages == []
        fetch_calls = [
            call for call in mock_client.uid.call_args_list
            if call[0][0].upper() == "FETCH"
        ]
        assert not fetch_calls

    def test_oversized_message_skipped_via_uid(self) -> None:
        """Oversized messages are skipped, and _mark_seen uses uid('STORE')."""
        raw = _build_rfc822_message()
        # Declare a size larger than the limit
        mock_client = _make_mock_client(
            [b"99"],
            raw,
            message_size=100 * 1024 * 1024,  # 100 MB — exceeds default 25 MB
        )

        channel = EmailChannel(config=_config())
        with patch.object(channel, "_imap_connect", return_value=mock_client):
            messages = channel._fetch_unseen()

        # The oversized message is skipped
        assert messages == []

        # The oversized message should still be marked seen via uid("STORE")
        store_calls = [
            call for call in mock_client.uid.call_args_list
            if call[0][0].upper() == "STORE"
        ]
        assert store_calls, "Oversized message should be marked seen via uid('STORE')"
        mock_client.store.assert_not_called()

    def test_mark_seen_disabled_skips_uid_store(self) -> None:
        """When mark_seen=False, no STORE calls should be made."""
        raw = _build_rfc822_message()
        mock_client = _make_mock_client([b"42"], raw)

        channel = EmailChannel(config=_config(mark_seen=False))
        with patch.object(channel, "_imap_connect", return_value=mock_client):
            channel._fetch_unseen()

        store_calls = [
            call for call in mock_client.uid.call_args_list
            if call[0][0].upper() == "STORE"
        ]
        assert not store_calls, "mark_seen=False should skip uid('STORE') calls"
