"""Contract and behavior tests for the IMAP/SMTP email channel adapter."""

from __future__ import annotations

import asyncio
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default as email_policy
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.channels import email as email_module
from agentos.channels.contract import ChannelSendStatus, run_channel_contract
from agentos.channels.email import (
    _DEFAULT_OUTBOUND_SUBJECT,
    EmailChannel,
    EmailChannelConfig,
    _merge_references,
    _quote_imap_mailbox,
    html_to_text,
    is_automated,
    is_email_address,
    normalize_address,
    reply_subject,
    sender_allowed,
    strip_quoted_reply,
    thread_key_for,
)
from agentos.channels.manager import ChannelManager
from agentos.channels.registry import discover_all, markdown_render_hint_for, parse_channel_entry
from agentos.channels.types import IncomingMessage, OutgoingMessage, UnsupportedChannelOperation


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
        "allowed_senders": ["owner@example.com", "*@team.example"],
    }
    base.update(overrides)
    return EmailChannelConfig(**base)


def _raw(
    *,
    sender: str = "owner@example.com",
    subject: str = "Status?",
    body: str = "hello there",
    message_id: str = "m1@example.com",
    extra_headers: dict[str, str] | None = None,
    subtype: str = "plain",
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "agent@example.com"
    message["Subject"] = subject
    message["Message-ID"] = f"<{message_id}>"
    for key, value in (extra_headers or {}).items():
        message[key] = value
    message.set_content(body, subtype=subtype)
    parsed = BytesParser(policy=email_policy).parsebytes(message.as_bytes())
    assert isinstance(parsed, EmailMessage)
    return parsed


# ---------------------------------------------------------------------------
# Shared channel contract
# ---------------------------------------------------------------------------


def test_email_adapter_keeps_shared_channel_contract() -> None:
    run_channel_contract(email_module)


def test_email_is_discoverable_and_flagged_as_plain_text() -> None:
    assert "email" in discover_all()
    assert markdown_render_hint_for("email")


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sender", "expected"),
    [
        ("owner@example.com", True),
        ("Owner@Example.com", True),
        ("Owner <owner@example.com>", True),
        ("anyone@team.example", True),
        ("stranger@elsewhere.com", False),
        ("owner@example.com.evil.com", False),
        ("", False),
    ],
)
def test_sender_allowlist_matches_addresses_and_domain_patterns(
    sender: str, expected: bool
) -> None:
    assert sender_allowed(sender, ["owner@example.com", "*@team.example"]) is expected


def test_empty_allowlist_admits_nobody() -> None:
    assert sender_allowed("owner@example.com", []) is False


def test_evaluate_access_denies_unknown_sender_and_any_group() -> None:
    channel = EmailChannel(config=_config())
    inbound = IncomingMessage(sender_id="stranger@elsewhere.com", channel_id="t1", content="hi")

    denied = channel.evaluate_access(inbound, is_group=False, mentioned=True)
    assert denied.admit is False
    assert denied.reason == "not_in_allowlist"

    admitted = channel.evaluate_access(
        IncomingMessage(sender_id="owner@example.com", channel_id="t1", content="hi"),
        is_group=False,
        mentioned=True,
    )
    assert admitted.admit is True

    grouped = channel.evaluate_access(inbound, is_group=True, mentioned=True)
    assert grouped.admit is False
    assert grouped.reason == "group_denied"


async def test_start_refuses_an_inbox_with_no_allowlist() -> None:
    channel = EmailChannel(config=_config(allowed_senders=[]))

    with pytest.raises(ValueError, match="allowed_senders"):
        await channel.start()


def test_config_entry_requires_allowlist_and_full_from_address() -> None:
    payload: dict[str, Any] = {
        "type": "email",
        "name": "inbox",
        "imap_host": "imap.example.com",
        "imap_username": "agent@example.com",
        "smtp_host": "smtp.example.com",
        "from_address": "agent@example.com",
    }

    with pytest.raises(ValueError, match="allowed_senders"):
        parse_channel_entry(payload)

    with pytest.raises(ValueError, match="from_address"):
        parse_channel_entry(
            {**payload, "allowed_senders": "owner@example.com", "from_address": "agent"}
        )

    entry = parse_channel_entry(
        {**payload, "allowed_senders": "Owner@Example.com, owner@example.com"}
    )
    assert entry.allowed_senders == ["owner@example.com"]


# ---------------------------------------------------------------------------
# Inbound parsing
# ---------------------------------------------------------------------------


def test_inbound_message_maps_thread_sender_and_body() -> None:
    channel = EmailChannel(config=_config())

    message = channel._to_incoming(_raw())

    assert message is not None
    assert message.sender_id == "owner@example.com"
    assert message.channel_id == "m1@example.com"
    assert message.content == "hello there"
    assert message.metadata["is_group"] is False
    assert message.metadata["native_thread_id"] == "m1@example.com"
    assert message.metadata["subject"] == "Status?"


def test_thread_key_follows_the_references_root() -> None:
    parsed = _raw(
        message_id="m3@example.com",
        extra_headers={
            "References": "<root@example.com> <m2@example.com>",
            "In-Reply-To": "<m2@example.com>",
        },
    )

    assert thread_key_for(parsed) == "root@example.com"


def test_reply_without_references_falls_back_to_in_reply_to() -> None:
    parsed = _raw(message_id="m2@example.com", extra_headers={"In-Reply-To": "<root@example.com>"})

    assert thread_key_for(parsed) == "root@example.com"


def test_messages_from_strangers_self_and_autoresponders_are_dropped() -> None:
    channel = EmailChannel(config=_config())

    assert channel._to_incoming(_raw(sender="stranger@elsewhere.com")) is None
    assert channel._to_incoming(_raw(sender="agent@example.com")) is None
    assert channel._to_incoming(_raw(extra_headers={"Auto-Submitted": "auto-replied"})) is None
    assert channel._to_incoming(_raw(extra_headers={"List-Id": "<news.example.com>"})) is None
    assert channel._to_incoming(_raw(extra_headers={"Precedence": "bulk"})) is None


def test_auto_submitted_no_is_not_treated_as_automated() -> None:
    assert is_automated(_raw(extra_headers={"Auto-Submitted": "no"})) is False


def test_html_bodies_are_flattened_to_text() -> None:
    channel = EmailChannel(config=_config())

    message = channel._to_incoming(
        _raw(body="<div>Hi <b>there</b></div><script>bad()</script>", subtype="html")
    )

    assert message is not None
    assert message.content == "Hi there"


def test_html_to_text_drops_styles_and_unescapes_entities() -> None:
    assert html_to_text("<style>a{}</style><p>a &amp; b</p><p>c</p>") == "a & b\nc"


def test_quoted_history_is_stripped_from_replies() -> None:
    body = "My answer\n\nOn Mon, someone wrote:\n> earlier question"

    assert strip_quoted_reply(body) == "My answer"


def test_a_body_that_is_only_quoted_history_is_kept() -> None:
    body = "On Mon, someone wrote:\n> earlier question"

    assert strip_quoted_reply(body) == body


def test_attachments_are_carried_through_and_oversized_ones_dropped() -> None:
    channel = EmailChannel(config=_config())
    message = EmailMessage()
    message["From"] = "owner@example.com"
    message["Subject"] = "files"
    message["Message-ID"] = "<m9@example.com>"
    message.set_content("see attached")
    message.add_attachment(b"small", maintype="text", subtype="plain", filename="ok.txt")
    message.add_attachment(
        b"x" * (60 * 1024 * 1024), maintype="application", subtype="pdf", filename="huge.pdf"
    )
    parsed = BytesParser(policy=email_policy).parsebytes(message.as_bytes())

    inbound = channel._to_incoming(parsed)

    assert inbound is not None
    assert [a.name for a in inbound.attachments] == ["ok.txt"]
    assert inbound.attachments[0].data == b"small"


def test_enqueue_dedupes_on_message_id() -> None:
    channel = EmailChannel(config=_config())
    first = channel._to_incoming(_raw())
    duplicate = channel._to_incoming(_raw())
    assert first is not None and duplicate is not None

    channel.enqueue(first)
    channel.enqueue(duplicate)

    assert channel._queue.qsize() == 1


# ---------------------------------------------------------------------------
# Outbound
# ---------------------------------------------------------------------------


def test_reply_targets_the_thread_and_quotes_the_subject() -> None:
    channel = EmailChannel(config=_config())
    inbound = channel._to_incoming(_raw())
    assert inbound is not None

    reply = channel.build_reply_message("done", inbound)

    assert reply.reply_to == "m1@example.com"
    assert reply.metadata["to"] == "owner@example.com"
    assert reply.metadata["subject"] == "Re: Status?"
    assert reply.metadata["in_reply_to"] == "m1@example.com"


def test_off_allowlist_reply_to_falls_back_to_the_sender() -> None:
    """An allowlisted sender must not be able to redirect the reply off-list."""

    channel = EmailChannel(config=_config())

    inbound = channel._to_incoming(_raw(extra_headers={"Reply-To": "attacker@elsewhere.com"}))

    assert inbound is not None
    assert inbound.metadata["email_reply_to"] == "owner@example.com"
    assert channel._threads["m1@example.com"].to_address == "owner@example.com"
    assert channel.build_reply_message("done", inbound).metadata["to"] == "owner@example.com"


def test_allowlisted_reply_to_is_still_honoured() -> None:
    channel = EmailChannel(config=_config())

    inbound = channel._to_incoming(_raw(extra_headers={"Reply-To": "Mate <mate@team.example>"}))

    assert inbound is not None
    assert inbound.metadata["email_reply_to"] == "mate@team.example"
    assert channel._threads["m1@example.com"].to_address == "mate@team.example"
    assert channel.build_reply_message("done", inbound).metadata["to"] == "mate@team.example"


def test_build_reply_message_re_checks_a_tampered_reply_target() -> None:
    """Metadata can outlive the parse, so the reply target is validated again."""

    channel = EmailChannel(config=_config())
    inbound = channel._to_incoming(_raw())
    assert inbound is not None
    inbound.metadata["email_reply_to"] = "attacker@elsewhere.com"

    assert channel.build_reply_message("done", inbound).metadata["to"] == "owner@example.com"


def test_reply_subject_is_not_double_prefixed() -> None:
    assert reply_subject("Re: Status?") == "Re: Status?"
    assert reply_subject("") == "Re: (no subject)"


async def test_send_composes_threading_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = EmailChannel(config=_config())
    inbound = channel._to_incoming(_raw())
    assert inbound is not None
    sent: list[EmailMessage] = []
    monkeypatch.setattr(channel, "_smtp_send", sent.append)

    await channel.send(channel.build_reply_message("the answer", inbound))

    assert len(sent) == 1
    outbound = sent[0]
    assert outbound["To"] == "owner@example.com"
    assert outbound["Subject"] == "Re: Status?"
    assert outbound["In-Reply-To"] == "<m1@example.com>"
    assert outbound["References"] == "<m1@example.com>"
    assert normalize_address(outbound["From"]) == "agent@example.com"
    assert outbound.get_content().strip() == "the answer"


def test_merge_references_falls_back_to_in_reply_to() -> None:
    """RFC 5322 3.6.4: a parent without ``References`` still names the thread root."""

    parsed = _raw(
        message_id="reply-102@example.com",
        extra_headers={"In-Reply-To": "<root-001@example.com>"},
    )

    merged = _merge_references(parsed, "reply-102@example.com")

    assert merged == "<root-001@example.com> <reply-102@example.com>"


def test_merge_references_prefers_the_existing_chain() -> None:
    parsed = _raw(
        message_id="reply-103@example.com",
        extra_headers={
            "References": "<root-001@example.com> <reply-102@example.com>",
            "In-Reply-To": "<reply-102@example.com>",
        },
    )

    merged = _merge_references(parsed, "reply-103@example.com")

    assert merged == ("<root-001@example.com> <reply-102@example.com> <reply-103@example.com>")


def test_merge_references_keeps_a_root_message_alone() -> None:
    parsed = _raw(message_id="root-001@example.com")

    assert _merge_references(parsed, "root-001@example.com") == "<root-001@example.com>"


def test_merge_references_drops_header_comments() -> None:
    """Some clients decorate the header with comments; only ids belong in the chain."""

    parsed = _raw(
        message_id="reply-102@example.com",
        extra_headers={"In-Reply-To": "<root-001@example.com> (from Outlook)"},
    )

    merged = _merge_references(parsed, "reply-102@example.com")

    assert merged == "<root-001@example.com> <reply-102@example.com>"


def test_merge_references_drops_comments_around_bare_ids() -> None:
    parsed = _raw(
        message_id="reply-102@example.com",
        extra_headers={"In-Reply-To": "root-001@example.com (from Outlook)"},
    )

    merged = _merge_references(parsed, "reply-102@example.com")

    assert merged == "<root-001@example.com> <reply-102@example.com>"


def test_merge_references_keeps_ids_a_mixed_header_brackets_unevenly() -> None:
    """A chain that brackets only some ids must still keep the root."""

    parsed = _raw(
        message_id="reply-103@example.com",
        extra_headers={"References": "root-001@example.com <reply-102@example.com>"},
    )

    merged = _merge_references(parsed, "reply-103@example.com")

    assert merged == ("<root-001@example.com> <reply-102@example.com> <reply-103@example.com>")


def test_merge_references_brackets_bare_ids() -> None:
    parsed = _raw(
        message_id="reply-102@example.com",
        extra_headers={"In-Reply-To": "root-001@example.com"},
    )

    merged = _merge_references(parsed, "reply-102@example.com")

    assert merged == "<root-001@example.com> <reply-102@example.com>"


def test_merge_references_deduplicates_repeated_ids() -> None:
    parsed = _raw(
        message_id="reply-102@example.com",
        extra_headers={"References": "<root-001@example.com> <root-001@example.com>"},
    )

    merged = _merge_references(parsed, "reply-102@example.com")

    assert merged == "<root-001@example.com> <reply-102@example.com>"


def test_thread_key_ignores_header_comments() -> None:
    """The thread cache key and the reference chain must name the same root."""

    parsed = _raw(
        message_id="m2@example.com",
        extra_headers={"References": "(from Outlook) <root-001@example.com> <m1@example.com>"},
    )

    assert thread_key_for(parsed) == "root-001@example.com"
    assert _merge_references(parsed, "m2@example.com").startswith("<root-001@example.com>")


def test_thread_key_reads_the_first_id_of_a_multi_id_in_reply_to() -> None:
    parsed = _raw(
        message_id="m2@example.com",
        extra_headers={"In-Reply-To": "<root-001@example.com> <m1@example.com>"},
    )

    assert thread_key_for(parsed) == "root-001@example.com"


def test_merge_references_without_a_message_id() -> None:
    parsed = _raw(
        message_id="reply-102@example.com",
        extra_headers={"In-Reply-To": "<root-001@example.com>"},
    )

    assert _merge_references(parsed, "") == "<root-001@example.com>"


async def test_send_keeps_the_thread_root_when_inbound_has_no_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reply must stay in the thread mail clients built from ``In-Reply-To``."""

    channel = EmailChannel(config=_config())
    inbound = channel._to_incoming(
        _raw(
            message_id="m2@example.com",
            extra_headers={"In-Reply-To": "<m1@example.com>"},
        )
    )
    assert inbound is not None
    sent: list[EmailMessage] = []
    monkeypatch.setattr(channel, "_smtp_send", sent.append)

    await channel.send(channel.build_reply_message("the answer", inbound))

    outbound = sent[0]
    assert outbound["In-Reply-To"] == "<m2@example.com>"
    assert outbound["References"] == "<m1@example.com> <m2@example.com>"


async def test_send_resolves_the_recipient_from_the_thread_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The message tool and artifact delivery send without reply metadata."""

    channel = EmailChannel(config=_config())
    assert channel._to_incoming(_raw()) is not None
    sent: list[EmailMessage] = []
    monkeypatch.setattr(channel, "_smtp_send", sent.append)

    await channel.send(OutgoingMessage(content="ping", reply_to="m1@example.com"))

    assert sent[0]["To"] == "owner@example.com"
    assert sent[0]["Subject"] == "Re: Status?"


async def test_send_resolves_the_recipient_from_metadata_recipient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The built-in message tool writes the target as ``metadata["recipient"]``."""

    channel = EmailChannel(config=_config())
    sent: list[EmailMessage] = []
    monkeypatch.setattr(channel, "_smtp_send", sent.append)

    await channel.send(
        OutgoingMessage(
            content="Daily status update",
            reply_to="colleague@example.com",
            metadata={"recipient": "colleague@example.com"},
        )
    )

    assert sent[0]["To"] == "colleague@example.com"
    assert sent[0]["Subject"] == _DEFAULT_OUTBOUND_SUBJECT
    assert sent[0].get("In-Reply-To") is None


async def test_send_resolves_the_recipient_from_reply_to_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scheduler and heartbeat delivery pass the address as ``reply_to`` alone."""

    channel = EmailChannel(config=_config())
    sent: list[EmailMessage] = []
    monkeypatch.setattr(channel, "_smtp_send", sent.append)

    await channel.send(OutgoingMessage(content="alert", reply_to="alerts@example.com"))

    assert sent[0]["To"] == "alerts@example.com"
    assert sent[0].get_content().strip() == "alert"


async def test_send_recipient_resolution_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    """``to`` beats ``recipient`` beats the thread cache beats ``reply_to``."""

    channel = EmailChannel(config=_config())
    assert channel._to_incoming(_raw()) is not None
    sent: list[EmailMessage] = []
    monkeypatch.setattr(channel, "_smtp_send", sent.append)

    await channel.send(
        OutgoingMessage(
            content="x",
            reply_to="m1@example.com",
            metadata={"to": "first@example.com", "recipient": "second@example.com"},
        )
    )
    await channel.send(
        OutgoingMessage(
            content="x",
            reply_to="m1@example.com",
            metadata={"recipient": "second@example.com"},
        )
    )
    await channel.send(OutgoingMessage(content="x", reply_to="m1@example.com"))
    await channel.send(OutgoingMessage(content="x", reply_to="fourth@example.com"))

    assert [m["To"] for m in sent] == [
        "first@example.com",
        "second@example.com",
        "owner@example.com",
        "fourth@example.com",
    ]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("owner@example.com", True),
        ("Owner Name <owner@example.com>", True),
        ("unknown", False),
        ("cron", False),
        ("", False),
        ("m1@example.com", True),
    ],
)
def test_is_email_address_accepts_only_addressable_values(value: str, expected: bool) -> None:
    assert is_email_address(value) is expected


async def test_send_without_a_known_thread_is_refused() -> None:
    channel = EmailChannel(config=_config())

    with pytest.raises(ValueError, match="recipient"):
        await channel.send(OutgoingMessage(content="ping", reply_to="unknown"))


async def test_send_file_attaches_into_the_thread(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    channel = EmailChannel(config=_config())
    assert channel._to_incoming(_raw()) is not None
    artifact = tmp_path / "report.csv"
    artifact.write_bytes(b"a,b\n1,2\n")
    sent: list[EmailMessage] = []
    monkeypatch.setattr(channel, "_smtp_send", sent.append)

    result = await channel.send_file("m1@example.com", str(artifact), content="here you go")

    assert result.status == ChannelSendStatus.SENT
    names = [part.get_filename() for part in sent[0].iter_attachments()]
    assert names == ["report.csv"]


async def test_send_file_reports_failure_instead_of_raising() -> None:
    channel = EmailChannel(config=_config())

    result = await channel.send_file("missing-thread", "/nope/nothing.txt")

    assert result.status == ChannelSendStatus.FAILED
    assert result.retryable is False


async def test_edit_and_delete_are_unsupported() -> None:
    channel = EmailChannel(config=_config())

    with pytest.raises(UnsupportedChannelOperation):
        await channel.edit("m1", "text")
    with pytest.raises(UnsupportedChannelOperation):
        await channel.delete("m1")


# ---------------------------------------------------------------------------
# Polling lifecycle
# ---------------------------------------------------------------------------


class _FakeIMAP:
    """Minimal imaplib stand-in covering the calls the adapter makes."""

    def __init__(self, raw: bytes, *, size: int | None = None) -> None:
        self._raw = raw
        self._size = len(raw) if size is None else size
        self.stored: list[tuple[str, str, str]] = []
        self.selected: list[str] = []
        self.closed = False

    def select(self, folder: str) -> tuple[str, list[bytes]]:
        self.selected.append(folder)
        return "OK", [b"1"]

    def search(self, charset: Any, criteria: str) -> tuple[str, list[bytes]]:
        return "OK", [b"7"]

    def fetch(self, uid: str, spec: str) -> tuple[str, list[Any]]:
        if "RFC822.SIZE" in spec:
            return "OK", [f"7 (RFC822.SIZE {self._size})".encode()]
        return "OK", [(b"7 (BODY[] {%d}" % len(self._raw), self._raw), b")"]

    def store(self, uid: str, command: str, flags: str) -> tuple[str, list[bytes]]:
        self.stored.append((uid, command, flags))
        return "OK", [b""]

    def close(self) -> None:
        self.closed = True

    def logout(self) -> None:
        return None


def test_poll_parses_and_marks_seen(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = EmailChannel(config=_config())
    raw = _raw().as_bytes()
    fake = _FakeIMAP(raw)
    monkeypatch.setattr(channel, "_imap_connect", lambda: fake)

    messages = channel._fetch_unseen()

    assert [m.sender_id for m in messages] == ["owner@example.com"]
    assert fake.stored == [("7", "+FLAGS", "\\Seen")]
    assert fake.closed is True


def test_poll_retries_message_after_transient_conversion_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = EmailChannel(config=_config())
    fake = _FakeIMAP(_raw().as_bytes())
    monkeypatch.setattr(channel, "_imap_connect", lambda: fake)
    real_to_incoming = channel._to_incoming
    attempts = 0

    def _flaky(parsed: EmailMessage) -> IncomingMessage | None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValueError("transient conversion failure")
        return real_to_incoming(parsed)

    monkeypatch.setattr(channel, "_to_incoming", _flaky)

    assert channel._fetch_unseen() == []
    assert fake.stored == []

    messages = channel._fetch_unseen()

    assert [message.sender_id for message in messages] == ["owner@example.com"]
    assert fake.stored == [("7", "+FLAGS", "\\Seen")]


def test_poll_marks_deliberately_filtered_message_seen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = EmailChannel(config=_config())
    fake = _FakeIMAP(_raw(sender="stranger@example.net").as_bytes())
    monkeypatch.setattr(channel, "_imap_connect", lambda: fake)

    assert channel._fetch_unseen() == []
    assert fake.stored == [("7", "+FLAGS", "\\Seen")]


def test_poll_skips_a_message_larger_than_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = EmailChannel(config=_config(max_message_bytes=32))
    fake = _FakeIMAP(_raw().as_bytes(), size=10_000)
    monkeypatch.setattr(channel, "_imap_connect", lambda: fake)

    assert channel._fetch_unseen() == []
    # Still flagged so the same oversized mail is not re-read every poll.
    assert fake.stored == [("7", "+FLAGS", "\\Seen")]


def test_poll_skips_and_marks_seen_message_with_missing_literal_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = EmailChannel(config=_config())
    fake = _FakeIMAP(_raw().as_bytes())
    monkeypatch.setattr(channel, "_imap_connect", lambda: fake)

    def _fetch(uid: str, spec: str) -> tuple[str, list[Any]]:
        if "RFC822.SIZE" in spec:
            return "OK", [b"7 (RFC822.SIZE 100)"]
        return "OK", [b")"]

    monkeypatch.setattr(fake, "fetch", _fetch)

    assert channel._fetch_unseen() == []
    assert fake.stored == [("7", "+FLAGS", "\\Seen")]


def test_poll_skips_and_marks_seen_non_email_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = EmailChannel(config=_config())
    fake = _FakeIMAP(_raw().as_bytes())
    monkeypatch.setattr(channel, "_imap_connect", lambda: fake)

    import email.parser

    monkeypatch.setattr(email.parser.BytesParser, "parsebytes", lambda self, raw: "not_a_message")

    assert channel._fetch_unseen() == []
    assert fake.stored == [("7", "+FLAGS", "\\Seen")]


def test_one_unreadable_message_does_not_sink_the_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = EmailChannel(config=_config())
    fake = _FakeIMAP(_raw().as_bytes())
    monkeypatch.setattr(channel, "_imap_connect", lambda: fake)
    real_fetch_one = channel._fetch_one
    calls: list[str] = []

    def _flaky(client: Any, uid: str) -> Any:
        calls.append(uid)
        if len(calls) == 1:
            raise ValueError("malformed mime")
        return real_fetch_one(client, uid)

    monkeypatch.setattr(fake, "search", lambda charset, criteria: ("OK", [b"7 8"]))
    monkeypatch.setattr(channel, "_fetch_one", _flaky)

    messages = channel._fetch_unseen()

    assert calls == ["7", "8"]
    assert len(messages) == 1
    assert fake.stored == [("8", "+FLAGS", "\\Seen")]


@pytest.mark.parametrize(
    ("folder", "expected"),
    [
        ("INBOX", '"INBOX"'),
        ("Sent Items", '"Sent Items"'),
        ("INBOX/Archive 2026", '"INBOX/Archive 2026"'),
        # RFC 3501 4.3: only backslash and double-quote are escaped in a
        # quoted-string, and the escape is a backslash.
        ('say "hi"', '"say \\"hi\\""'),
        ("back\\slash", '"back\\\\slash"'),
        ('mix "a\\b"', '"mix \\"a\\\\b\\""'),
    ],
)
def test_quote_imap_mailbox_wraps_and_escapes(folder: str, expected: str) -> None:
    assert _quote_imap_mailbox(folder) == expected


@pytest.mark.parametrize("folder", ["", "   ", "In\rbox", "In\nbox", "In\x00box"])
def test_quote_imap_mailbox_refuses_names_it_cannot_encode(folder: str) -> None:
    # A bare CR/LF would end the command line and let the rest of the name run
    # as a second IMAP command, so this has to raise rather than be escaped.
    with pytest.raises(ValueError, match="imap_folder"):
        _quote_imap_mailbox(folder)


def test_poll_selects_a_folder_with_spaces_as_one_quoted_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = EmailChannel(config=_config(imap_folder="Sent Items"))
    fake = _FakeIMAP(_raw().as_bytes())
    monkeypatch.setattr(channel, "_imap_connect", lambda: fake)

    channel._fetch_unseen()

    assert fake.selected == ['"Sent Items"']


def test_poll_selects_the_default_folder_quoted(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = EmailChannel(config=_config())
    fake = _FakeIMAP(_raw().as_bytes())
    monkeypatch.setattr(channel, "_imap_connect", lambda: fake)

    channel._fetch_unseen()

    assert fake.selected == ['"INBOX"']


@pytest.mark.parametrize("folder", ["", "Sent\r\nLOGOUT"])
async def test_start_refuses_an_unusable_imap_folder(folder: str) -> None:
    # Fail at start() with the offending value rather than as an opaque BAD
    # once every poll interval.
    channel = EmailChannel(config=_config(imap_folder=folder))

    with pytest.raises(ValueError, match="imap_folder"):
        await channel.start()


def test_mark_seen_disabled_leaves_the_flag_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = EmailChannel(config=_config(mark_seen=False))
    fake = _FakeIMAP(_raw().as_bytes())
    monkeypatch.setattr(channel, "_imap_connect", lambda: fake)

    channel._fetch_unseen()

    assert fake.stored == []


async def test_poll_loop_survives_a_failing_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = EmailChannel(config=_config(poll_interval_s=1.0))
    calls: list[int] = []

    def _boom() -> list[IncomingMessage]:
        calls.append(1)
        raise OSError("imap down")

    monkeypatch.setattr(channel, "_fetch_unseen", _boom)

    await channel.start()
    await asyncio.sleep(0.05)
    health = await channel.health_check()
    await channel.stop()

    assert calls
    assert health.connected is False
    assert "imap down" in health.extra["last_error"]
    assert channel._task is None


async def test_receive_yields_polled_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = EmailChannel(config=_config(poll_interval_s=1.0))
    inbound = channel._to_incoming(_raw())
    assert inbound is not None
    monkeypatch.setattr(channel, "_fetch_unseen", lambda: [inbound])

    await channel.start()
    received = await asyncio.wait_for(channel.receive(), timeout=2.0)
    await channel.stop()

    assert received.sender_id == "owner@example.com"
    assert (await channel.health_check()).last_message_at is not None


# ---------------------------------------------------------------------------
# Session keying
# ---------------------------------------------------------------------------


def test_each_mail_thread_gets_its_own_session_key() -> None:
    first = SimpleNamespace(
        sender_id="owner@example.com",
        channel_id="root-a",
        metadata={"is_group": False, "dm_thread_scoped": True, "native_thread_id": "root-a"},
    )
    second = SimpleNamespace(
        sender_id="owner@example.com",
        channel_id="root-b",
        metadata={"is_group": False, "dm_thread_scoped": True, "native_thread_id": "root-b"},
    )

    key_a = ChannelManager._build_session_key("email", first, agent_id="main")
    key_b = ChannelManager._build_session_key("email", second, agent_id="main")

    assert key_a == "agent:main:email:direct:owner@example.com:thread:root-a"
    assert key_a != key_b


def test_a_dm_thread_id_without_the_opt_in_still_maps_to_one_session() -> None:
    """Per-thread DM sessions are opt-in; other adapters must not move."""

    message = SimpleNamespace(
        sender_id="user-1",
        channel_id="D-1",
        metadata={"is_group": False, "native_thread_id": "t-1", "thread_ts": "171234.000"},
    )

    assert ChannelManager._build_session_key("slack", message, agent_id="ops") == (
        "agent:ops:slack:direct:user-1"
    )
