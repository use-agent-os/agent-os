from agentos.channels.email import (
    EmailChannel,
    EmailChannelConfig,
    _EmailThread,
    decode_header_value,
    reply_subject,
)
from agentos.channels.types import OutgoingMessage


def test_decode_header_value_unfolds_multiline_headers() -> None:
    raw = "Quarterly Roadmap\r\n & Project Status Update"
    decoded = decode_header_value(raw)
    assert decoded == "Quarterly Roadmap & Project Status Update"
    assert "\r" not in decoded
    assert "\n" not in decoded


def test_reply_subject_unfolds_multiline_subject() -> None:
    raw = "Quarterly Roadmap\n & Project Status Update"
    res = reply_subject(raw)
    assert res == "Re: Quarterly Roadmap & Project Status Update"
    assert "\n" not in res


def test_email_channel_compose_handles_folded_subject() -> None:
    channel = EmailChannel(
        config=EmailChannelConfig(
            name="test",
            imap_host="imap.example.com",
            imap_username="u",
            imap_password="p",
            smtp_host="smtp.example.com",
            smtp_username="u",
            smtp_password="p",
            from_address="bot@example.com",
        )
    )

    folded_subj = "Quarterly Roadmap\r\n & Project Status Update"
    channel._threads["<thread-1>"] = _EmailThread(
        to_address="user@example.com",
        subject=folded_subj,
        last_message_id="<msg-1>",
        references="<msg-1>",
    )

    to, subj, in_reply_to, refs = channel._resolve_target(
        OutgoingMessage(content="Acknowledged", reply_to="<thread-1>")
    )

    composed = channel._compose(
        to_address=to,
        subject=subj,
        body="Acknowledged",
        in_reply_to=in_reply_to,
        references=refs,
    )

    assert composed["Subject"] == "Re: Quarterly Roadmap & Project Status Update"
