"""Email (IMAP/SMTP) channel adapter.

Inbound is IMAP polling: every ``poll_interval_s`` the adapter opens a
short-lived IMAP connection, searches ``UNSEEN`` in ``imap_folder``,
skips anything oversized or auto-generated, and enqueues the rest.
Outbound is SMTP — replies carry ``In-Reply-To``/``References`` so mail
clients keep them in the originating thread.

Threading contract
------------------
One email thread is one session. The thread key is the first id in
``References`` (falling back to ``In-Reply-To``, then the message's own
``Message-ID``), published as ``metadata['native_thread_id']`` so
``ChannelManager._build_session_key`` scopes the DM key per thread.

Access control
--------------
``allowed_senders`` is a fail-closed From-address allowlist. Entries are
exact addresses (``me@example.com``) or domain patterns (``*@example.com``
/ ``@example.com``). It is enforced twice: at poll time, so a stranger's
mail is never queued, and again through ``evaluate_access`` so gateway
dispatch reaches the same verdict.

Loop safety
-----------
Mail from ``from_address`` itself, and anything carrying
``Auto-Submitted``/``X-Autoreply``/``Precedence: bulk``/``List-Id``, is
dropped. Without that an autoresponder on the far end and this adapter
would answer each other forever.
"""

from __future__ import annotations

import asyncio
import contextlib
import email.utils
import html
import imaplib
import re
import smtplib
import ssl
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default as email_policy
from pathlib import Path
from typing import Any, ClassVar

import structlog
from pydantic import BaseModel, Field

from agentos.channels._attachment_io import (
    attachment_limit_for_mime,
    ensure_bytes_within_limit,
)
from agentos.channels._util import (
    AccessDecision,
    ChannelAccessPolicy,
    EventDedupeCache,
    check_channel_file_size,
)
from agentos.channels.contract import (
    ChannelCapabilities,
    ChannelCapabilityProfile,
    ChannelSendResult,
)
from agentos.channels.types import (
    Attachment,
    ChannelHealth,
    IncomingMessage,
    OutgoingMessage,
    UnsupportedChannelOperation,
)

log = structlog.get_logger(__name__)

# Channel-contract constants pinned by the adapter audit.
CAPABILITY_TIER = "GREEN-shipping"

DM_SAFETY_TIERS: tuple[str, ...] = ("safe", "confirm")

RETRYABLE_ERROR_CLASSES: tuple[str, ...] = (
    "transport_transient",
    "rate_limited",
    "channel_degraded",
)
FATAL_ERROR_CLASSES: tuple[str, ...] = (
    "auth_invalid",
    "payload_rejected",
    "target_missing",
    "contract_violation",
)

#: Bound on the in-memory thread routing table.
_MAX_TRACKED_THREADS = 1000

#: Headers that mark a message as machine-generated. Answering one risks a
#: mail loop, so the adapter drops them before they reach the queue.
_AUTOMATED_HEADERS: tuple[str, ...] = (
    "auto-submitted",
    "x-autoreply",
    "x-autorespond",
    "list-id",
    "list-unsubscribe",
)

# Deliberately narrow: a marker that can appear in ordinary prose (a bare
# ``From:`` line) would silently truncate the user's own text. Outlook's
# quote block is caught by the underscore rule that precedes its header.
_QUOTE_MARKERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.I),
    re.compile(r"^\s*_{5,}\s*$"),
    re.compile(r"^\s*On .+ wrote:\s*$", re.I),
)

_HTML_BREAK_RE = re.compile(r"(?i)<\s*(?:br\s*/?|/p|/div|/tr|/li)\s*>")
_HTML_DROP_RE = re.compile(r"(?is)<\s*(script|style)\b.*?<\s*/\s*\1\s*>")
_HTML_TAG_RE = re.compile(r"(?s)<[^>]+>")
_HEADER_COMMENT_RE = re.compile(r"\([^()]*\)")
_DEFAULT_OUTBOUND_SUBJECT = "Message from AgentOS"


class EmailChannelConfig(BaseModel):
    """Adapter-level config for the IMAP/SMTP email channel.

    Every field defaults so ``EmailChannel(config=EmailChannelConfig())``
    stays valid for contract construction; ``start()`` is what refuses an
    under-configured entry.
    """

    name: str = "email"
    imap_host: str = ""
    imap_port: int = 993
    imap_ssl: bool = True
    imap_username: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_ssl: bool = False
    smtp_starttls: bool = True
    smtp_username: str = ""
    smtp_password: str = ""
    from_address: str = ""
    from_name: str = ""
    allowed_senders: list[str] = Field(default_factory=list)
    poll_interval_s: float = 30.0
    max_messages_per_poll: int = 10
    max_message_bytes: int = 25 * 1024 * 1024
    mark_seen: bool = True
    connect_timeout_s: float = 30.0


@dataclass(frozen=True, slots=True)
class _EmailThread:
    """Outbound routing state for one inbound mail thread."""

    to_address: str
    subject: str
    last_message_id: str
    references: str


def normalize_address(value: str) -> str:
    """Return the bare, lowercased address out of a From/To header value."""

    _, address = email.utils.parseaddr(value or "")
    return address.strip().lower()


def is_email_address(value: str) -> bool:
    """Return True when ``value`` carries a usable mailbox address.

    Outbound callers hand us ``reply_to`` values that are sometimes an address
    and sometimes an opaque routing token (``cron``, a thread key), so the
    address fallback has to be able to tell the two apart.
    """

    address = normalize_address(value)
    local, at, domain = address.partition("@")
    return bool(local and at and domain)


def sender_allowed(sender: str, allowlist: list[str] | tuple[str, ...]) -> bool:
    """Return True when ``sender`` matches the fail-closed allowlist.

    Entries are exact addresses or domain patterns (``*@example.com`` and
    ``@example.com`` are equivalent). An empty allowlist admits nobody.
    """

    address = normalize_address(sender) or (sender or "").strip().lower()
    if not address:
        return False
    domain = address.rpartition("@")[2]
    for raw in allowlist:
        pattern = (raw or "").strip().lower()
        if not pattern:
            continue
        if pattern.startswith("*@"):
            pattern = pattern[1:]
        if pattern.startswith("@"):
            if domain and domain == pattern[1:]:
                return True
            continue
        if address == pattern:
            return True
    return False


def _quote_imap_mailbox(folder: str) -> str:
    """Return ``folder`` as an RFC 3501 quoted-string for ``SELECT``.

    ``imaplib`` splices command arguments onto the wire verbatim, so a name
    with a space in it — ``Sent Items`` and friends are ordinary on
    Exchange/Outlook — arrives as two tokens and the server answers ``BAD``.
    RFC 3501 4.3 escapes only ``\\`` and ``"`` inside a quoted-string;
    control characters cannot appear at all, and a bare CR/LF would end the
    command line and let the tail of the name run as a second IMAP command, so
    those are refused rather than escaped.
    """

    name = folder or ""
    if not name.strip():
        raise ValueError("email channel imap_folder must not be empty")
    if any(char < " " or char == "\x7f" for char in name):
        raise ValueError(f"email channel imap_folder must not contain control characters: {name!r}")
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def decode_header_value(value: str | None) -> str:
    """Decode an RFC 2047 header into text, degrading to the raw value."""

    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (UnicodeDecodeError, ValueError, LookupError):
        return str(value)


def html_to_text(payload: str) -> str:
    """Flatten an HTML mail part into readable plain text."""

    without_blocks = _HTML_DROP_RE.sub(" ", payload)
    with_breaks = _HTML_BREAK_RE.sub("\n", without_blocks)
    stripped = _HTML_TAG_RE.sub("", with_breaks)
    text = html.unescape(stripped)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def strip_quoted_reply(body: str) -> str:
    """Drop the quoted history below a reply so only the new text remains."""

    lines = body.replace("\r\n", "\n").split("\n")
    kept: list[str] = []
    for line in lines:
        if any(marker.match(line) for marker in _QUOTE_MARKERS):
            break
        kept.append(line)
    while kept and kept[-1].startswith(">"):
        kept.pop()
    text = "\n".join(kept).strip()
    # A reply that is nothing but quoted history keeps its original body:
    # an empty prompt is worse than a noisy one.
    return text or body.strip()


def thread_key_for(parsed: EmailMessage) -> str:
    """Return the stable thread id for an inbound message."""

    references = _message_ids(parsed.get("References"))
    if references:
        return references[0]
    in_reply_to = _message_ids(parsed.get("In-Reply-To"))
    if in_reply_to:
        return in_reply_to[0]
    return (parsed.get("Message-ID") or "").strip().strip("<>")


def is_automated(parsed: EmailMessage) -> bool:
    """Return True for machine-generated mail that must not be answered."""

    for header in _AUTOMATED_HEADERS:
        raw = parsed.get(header)
        if not raw:
            continue
        if header == "auto-submitted" and str(raw).strip().lower() == "no":
            continue
        return True
    precedence = str(parsed.get("Precedence") or "").strip().lower()
    return precedence in {"bulk", "list", "junk", "auto_reply"}


def reply_subject(subject: str) -> str:
    """Return ``subject`` prefixed with ``Re:`` unless it already is one."""

    text = (subject or "").strip() or "(no subject)"
    return text if text.lower().startswith("re:") else f"Re: {text}"


@dataclass
class EmailChannel:
    """IMAP/SMTP adapter implementing ``ManagedChannel``.

    Inbound polls IMAP on a background task; outbound sends over SMTP. Both
    transports are blocking stdlib clients, so every network call is handed
    to ``asyncio.to_thread`` — the event loop never blocks on mail I/O.
    """

    config: EmailChannelConfig
    MAX_ATTACHMENT_BYTES: ClassVar[int] = 25 * 1024 * 1024

    _queue: asyncio.Queue[IncomingMessage] = field(
        default_factory=asyncio.Queue, init=False, repr=False
    )
    _task: asyncio.Task | None = field(default=None, init=False, repr=False)
    _threads: OrderedDict[str, _EmailThread] = field(
        default_factory=OrderedDict, init=False, repr=False
    )
    _dedupe: EventDedupeCache = field(
        default_factory=lambda: EventDedupeCache(max_size=10_000),
        init=False,
        repr=False,
    )
    _connected: bool = field(default=False, init=False, repr=False)
    _last_message_at: datetime | None = field(default=None, init=False, repr=False)
    _last_error: str = field(default="", init=False, repr=False)

    # ------------------------------------------------------------------
    # Capability declaration
    # ------------------------------------------------------------------

    @property
    def policy(self) -> ChannelAccessPolicy:
        """Fail-closed DM-only policy backed by the From-address allowlist."""

        return ChannelAccessPolicy(
            dm_allowed=True,
            group_allowed=False,
            mention_required_in_group=False,
            allowlist=frozenset(
                address
                for address in (a.strip().lower() for a in self.config.allowed_senders)
                if address
            ),
            allowlist_enabled=True,
        )

    @property
    def capability_profile(self) -> ChannelCapabilityProfile:
        return ChannelCapabilityProfile(
            channel_type="email",
            threads=True,
            thread_messages=True,
            reply=True,
            thread_reply=True,
            native_file_upload=True,
            media=True,
            artifact_delivery=True,
            transports=("polling",),
            notes=(
                "IMAP polling inbound, SMTP outbound. Mail has no edit, delete, "
                "reactions, or typing indicator, so those stay unsupported.",
                "Replies are plain text; Markdown markers would render literally.",
            ),
        )

    @property
    def capabilities(self) -> frozenset[str]:
        return self.capability_profile.capability_tags()

    def evaluate_access(
        self,
        message: IncomingMessage,
        *,
        is_group: bool,
        mentioned: bool,
    ) -> AccessDecision:
        """Apply the From-address allowlist, including domain patterns."""

        if is_group:
            return AccessDecision(admit=False, reason="group_denied")
        if sender_allowed(message.sender_id, self.config.allowed_senders):
            return AccessDecision(admit=True, reason="dm_admitted")
        return AccessDecision(admit=False, reason="not_in_allowlist")

    def access_snapshot(self) -> dict[str, Any]:
        return {
            "allowed_senders": list(self.config.allowed_senders),
            "from_address": self.config.from_address,
            "imap_folder": self.config.imap_folder,
            "poll_interval_s": self.config.poll_interval_s,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _validate_config(self) -> None:
        missing = [
            name
            for name in ("imap_host", "imap_username", "smtp_host", "from_address")
            if not str(getattr(self.config, name, "") or "").strip()
        ]
        if missing:
            raise ValueError(f"email channel requires {', '.join(missing)}")
        if not self.config.allowed_senders:
            raise ValueError(
                "email channel requires a non-empty allowed_senders allowlist; "
                "an open inbox would let any stranger drive the agent"
            )
        # Surface an unusable folder name here instead of as an opaque server
        # ``BAD`` once every poll interval.
        _quote_imap_mailbox(self.config.imap_folder)
        if not self.config.imap_ssl:
            log.warning("email.imap_plaintext", name=self.config.name)
        if not (self.config.smtp_ssl or self.config.smtp_starttls):
            log.warning("email.smtp_plaintext", name=self.config.name)

    async def start(self) -> None:
        self._validate_config()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._poll_loop(), name=f"email-poll:{self.config.name}"
            )
        log.info(
            "email.started",
            name=self.config.name,
            imap_host=self.config.imap_host,
            folder=self.config.imap_folder,
            allowed_senders=len(self.config.allowed_senders),
        )

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._connected = False
        log.info("email.stopped", name=self.config.name)

    async def health_check(self) -> ChannelHealth:
        return ChannelHealth(
            connected=self._connected,
            bot_user_id=self.config.from_address or None,
            last_message_at=self._last_message_at,
            extra={"last_error": self._last_error} if self._last_error else {},
        )

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        while True:
            try:
                messages = await asyncio.to_thread(self._fetch_unseen)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — a bad poll must not kill the loop
                self._connected = False
                self._last_error = str(exc)
                log.warning("email.poll_failed", name=self.config.name, error=str(exc))
            else:
                self._connected = True
                self._last_error = ""
                for message in messages:
                    self.enqueue(message)
            await asyncio.sleep(max(1.0, self.config.poll_interval_s))

    def _imap_connect(self) -> imaplib.IMAP4:
        timeout = self.config.connect_timeout_s
        if self.config.imap_ssl:
            client: imaplib.IMAP4 = imaplib.IMAP4_SSL(
                self.config.imap_host,
                self.config.imap_port,
                ssl_context=ssl.create_default_context(),
                timeout=timeout,
            )
        else:
            client = imaplib.IMAP4(self.config.imap_host, self.config.imap_port, timeout=timeout)
        client.login(self.config.imap_username, self.config.imap_password)
        return client

    def _fetch_unseen(self) -> list[IncomingMessage]:
        """Blocking IMAP poll — always called through ``asyncio.to_thread``."""

        client = self._imap_connect()
        try:
            client.select(_quote_imap_mailbox(self.config.imap_folder))
            status, data = client.search(None, "UNSEEN")
            if status != "OK":
                raise RuntimeError(f"IMAP search failed: {status}")
            raw_uids = (data[0] or b"").split()[: max(1, self.config.max_messages_per_poll)]
            uids = [uid.decode("ascii", "ignore") for uid in raw_uids]
            messages: list[IncomingMessage] = []
            for uid in uids:
                try:
                    parsed = self._fetch_one(client, uid)
                    if parsed is None:
                        continue
                    message = self._to_incoming(parsed)
                    # Acknowledge only after parsing and conversion return normally.
                    self._mark_seen(client, uid)
                except Exception as exc:  # noqa: BLE001 — one bad mail, not the batch
                    log.warning("email.message_read_failed", name=self.config.name, error=str(exc))
                    continue
                if message is not None:
                    messages.append(message)
            return messages
        finally:
            with contextlib.suppress(Exception):
                client.close()
            with contextlib.suppress(Exception):
                client.logout()

    def _fetch_one(self, client: imaplib.IMAP4, uid: str) -> EmailMessage | None:
        """Fetch one message, refusing oversized bodies before downloading them."""

        status, size_data = client.fetch(uid, "(RFC822.SIZE)")
        if status == "OK" and size_data:
            declared = _parse_rfc822_size(size_data)
            if declared is not None and declared > self.config.max_message_bytes:
                log.warning(
                    "email.message_too_large",
                    name=self.config.name,
                    size=declared,
                    limit=self.config.max_message_bytes,
                )
                self._mark_seen(client, uid)
                return None

        status, body_data = client.fetch(uid, "(BODY.PEEK[])")
        if status != "OK":
            return None
        raw = _first_literal(body_data)
        if raw is None:
            log.warning(
                "email.message_unparseable",
                name=self.config.name,
                uid=uid,
                reason="empty_or_missing_literal",
            )
            self._mark_seen(client, uid)
            return None
        if len(raw) > self.config.max_message_bytes:
            log.warning(
                "email.message_too_large",
                name=self.config.name,
                size=len(raw),
                limit=self.config.max_message_bytes,
            )
            self._mark_seen(client, uid)
            return None

        parsed = BytesParser(policy=email_policy).parsebytes(raw)
        if not isinstance(parsed, EmailMessage):
            log.warning(
                "email.message_unparseable",
                name=self.config.name,
                uid=uid,
                reason="not_an_email_message",
            )
            self._mark_seen(client, uid)
            return None
        return parsed

    def _mark_seen(self, client: imaplib.IMAP4, uid: str) -> None:
        if not self.config.mark_seen:
            return
        with contextlib.suppress(Exception):
            client.store(uid, "+FLAGS", "\\Seen")

    def _reply_target(self, sender: str, reply_to_header: str) -> str:
        """Return the address replies should go to, honouring the allowlist.

        ``Reply-To`` is attacker-controlled even on an admitted message: an
        allowlisted sender can point it at any mailbox and redirect the agent's
        answer — tool output included. Honour it only when it clears the same
        fail-closed allowlist as ``From``, otherwise reply to the sender.
        """

        reply_to = normalize_address(reply_to_header)
        if not reply_to or reply_to == sender:
            return sender
        if sender_allowed(reply_to, self.config.allowed_senders):
            return reply_to
        log.warning(
            "email.reply_to_not_allowed",
            name=self.config.name,
            sender=sender,
            reply_to=reply_to,
        )
        return sender

    def _to_incoming(self, parsed: EmailMessage) -> IncomingMessage | None:
        sender = normalize_address(parsed.get("From", ""))
        if not sender:
            return None
        if sender == normalize_address(self.config.from_address):
            log.debug("email.skipped_self", name=self.config.name)
            return None
        if is_automated(parsed):
            log.info("email.skipped_automated", name=self.config.name, sender=sender)
            return None
        if not sender_allowed(sender, self.config.allowed_senders):
            log.warning("email.sender_not_allowed", name=self.config.name, sender=sender)
            return None

        thread_id = thread_key_for(parsed)
        if not thread_id:
            return None
        message_id = (parsed.get("Message-ID") or "").strip().strip("<>")
        subject = decode_header_value(parsed.get("Subject"))
        body = strip_quoted_reply(_body_text(parsed))
        reply_to = self._reply_target(sender, parsed.get("Reply-To", ""))

        self._remember_thread(
            thread_id,
            _EmailThread(
                to_address=reply_to,
                subject=subject,
                last_message_id=message_id,
                references=_merge_references(parsed, message_id),
            ),
        )

        return IncomingMessage(
            sender_id=sender,
            channel_id=thread_id,
            content=body,
            attachments=self._extract_attachments(parsed),
            metadata={
                "is_group": False,
                "dm_thread_scoped": True,
                "conversation_kind": "thread",
                "native_message_id": message_id,
                "native_chat_id": thread_id,
                "native_thread_id": thread_id,
                "reply_target_id": message_id,
                "subject": subject,
                "email_from": sender,
                "email_reply_to": reply_to,
                "email_to": decode_header_value(parsed.get("To")),
                "email_date": decode_header_value(parsed.get("Date")),
            },
        )

    def _extract_attachments(self, parsed: EmailMessage) -> list[Attachment]:
        attachments: list[Attachment] = []
        for part in parsed.iter_attachments():
            if not isinstance(part, EmailMessage):
                continue
            name = part.get_filename() or "attachment"
            mime = part.get_content_type()
            payload = part.get_payload(decode=True)
            if not isinstance(payload, bytes):
                continue
            try:
                data = ensure_bytes_within_limit(
                    payload,
                    name=name,
                    limit=attachment_limit_for_mime(mime),
                )
            except ValueError as exc:
                log.warning(
                    "email.attachment_rejected",
                    name=self.config.name,
                    attachment=name,
                    error=str(exc),
                )
                continue
            attachments.append(Attachment(name=name, mime_type=mime, data=data, size=len(data)))
        return attachments

    def _remember_thread(self, thread_id: str, state: _EmailThread) -> None:
        self._threads[thread_id] = state
        self._threads.move_to_end(thread_id)
        while len(self._threads) > _MAX_TRACKED_THREADS:
            self._threads.popitem(last=False)

    def enqueue(self, message: IncomingMessage) -> None:
        message_id = str(message.metadata.get("native_message_id") or "")
        if message_id and not self._dedupe.check_and_add(message_id):
            return
        self._queue.put_nowait(message)

    async def receive(self) -> IncomingMessage:
        message = await self._queue.get()
        self._last_message_at = datetime.now(UTC)
        return message

    def is_group_mentioned(self, msg: IncomingMessage) -> bool:
        # Email has no group surface; every admitted message is addressed to us.
        return True

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    def build_reply_message(self, content: str, inbound: IncomingMessage) -> OutgoingMessage:
        sender = normalize_address(inbound.sender_id) or inbound.sender_id
        metadata: dict[str, Any] = {
            "to": self._reply_target(sender, str(inbound.metadata.get("email_reply_to") or "")),
            "subject": reply_subject(str(inbound.metadata.get("subject") or "")),
            "in_reply_to": inbound.metadata.get("native_message_id") or "",
        }
        return OutgoingMessage(content=content, reply_to=inbound.channel_id, metadata=metadata)

    def _resolve_target(self, message: OutgoingMessage) -> tuple[str, str, str, str]:
        """Return ``(to, subject, in_reply_to, references)`` for an outbound send."""

        reply_to = (message.reply_to or "").strip()
        thread = self._threads.get(reply_to)
        metadata = message.metadata or {}
        # The message tool writes "recipient", channel replies write "to", and
        # scheduler/heartbeat delivery sends the bare address as reply_to, so
        # every producer has to be able to name the mailbox.
        to_address = str(
            metadata.get("to") or metadata.get("recipient") or (thread.to_address if thread else "")
        ).strip()
        if not to_address and is_email_address(reply_to):
            to_address = normalize_address(reply_to)
        if not to_address:
            raise ValueError("email.send has no recipient for reply_to")
        subject = str(metadata.get("subject") or "").strip()
        if not subject:
            subject = reply_subject(thread.subject) if thread else _DEFAULT_OUTBOUND_SUBJECT
        in_reply_to = str(metadata.get("in_reply_to") or (thread.last_message_id if thread else ""))
        references = str(metadata.get("references") or (thread.references if thread else ""))
        return to_address, subject, in_reply_to, references

    def _compose(
        self,
        *,
        to_address: str,
        subject: str,
        body: str,
        in_reply_to: str,
        references: str,
    ) -> EmailMessage:
        message = EmailMessage()
        message["From"] = email.utils.formataddr(
            (self.config.from_name or "", self.config.from_address)
        )
        message["To"] = to_address
        message["Subject"] = subject
        message["Date"] = email.utils.formatdate(localtime=True)
        message["Message-ID"] = email.utils.make_msgid()
        if in_reply_to:
            message["In-Reply-To"] = f"<{in_reply_to.strip('<>')}>"
        if references:
            message["References"] = references
        message.set_content(body or "")
        return message

    def _smtp_send(self, message: EmailMessage) -> None:
        """Blocking SMTP send — always called through ``asyncio.to_thread``."""

        context = ssl.create_default_context()
        timeout = self.config.connect_timeout_s
        if self.config.smtp_ssl:
            server: smtplib.SMTP = smtplib.SMTP_SSL(
                self.config.smtp_host,
                self.config.smtp_port,
                context=context,
                timeout=timeout,
            )
        else:
            server = smtplib.SMTP(self.config.smtp_host, self.config.smtp_port, timeout=timeout)
        with server:
            if not self.config.smtp_ssl and self.config.smtp_starttls:
                server.starttls(context=context)
            if self.config.smtp_username:
                server.login(self.config.smtp_username, self.config.smtp_password)
            server.send_message(message)

    async def send(self, message: OutgoingMessage) -> None:
        to_address, subject, in_reply_to, references = self._resolve_target(message)
        outbound = self._compose(
            to_address=to_address,
            subject=subject,
            body=message.content,
            in_reply_to=in_reply_to,
            references=references,
        )
        for attachment in message.attachments:
            if attachment.data:
                _attach(outbound, attachment.name, attachment.mime_type, attachment.data)
        await asyncio.to_thread(self._smtp_send, outbound)
        log.info("email.outbound_sent", name=self.config.name, thread_id=message.reply_to)

    async def send_file(
        self,
        thread_id: str,
        file_path: str,
        content: str = "",
    ) -> ChannelSendResult:
        """Mail one file back into ``thread_id`` as an attachment."""

        path = Path(file_path)
        try:
            check_channel_file_size(path, self.MAX_ATTACHMENT_BYTES, "Email")
            payload = path.read_bytes()
            to_address, subject, in_reply_to, references = self._resolve_target(
                OutgoingMessage(content="", reply_to=thread_id)
            )
            outbound = self._compose(
                to_address=to_address,
                subject=subject,
                body=content or f"Attached: {path.name}",
                in_reply_to=in_reply_to,
                references=references,
            )
            _attach(outbound, path.name, None, payload)
            await asyncio.to_thread(self._smtp_send, outbound)
        except (OSError, ValueError, smtplib.SMTPException) as exc:
            return ChannelSendResult.failed(
                capability=ChannelCapabilities.NATIVE_FILE_UPLOAD,
                target_id=thread_id,
                reason=str(exc),
                retryable=isinstance(exc, smtplib.SMTPException),
            )
        return ChannelSendResult.sent(
            capability=ChannelCapabilities.NATIVE_FILE_UPLOAD,
            target_id=thread_id,
        )

    async def edit(self, message_id: str, content: str) -> None:
        raise UnsupportedChannelOperation(
            channel="email",
            operation="edit",
            reason="a delivered email cannot be edited",
        )

    async def delete(self, message_id: str) -> None:
        raise UnsupportedChannelOperation(
            channel="email",
            operation="delete",
            reason="a delivered email cannot be recalled",
        )


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


def _attach(message: EmailMessage, name: str, mime_type: str | None, payload: bytes) -> None:
    maintype, _, subtype = (mime_type or "application/octet-stream").partition("/")
    message.add_attachment(
        payload,
        maintype=maintype or "application",
        subtype=subtype or "octet-stream",
        filename=name,
    )


def _body_text(parsed: EmailMessage) -> str:
    body = parsed.get_body(preferencelist=("plain", "html"))
    if body is None:
        return ""
    try:
        content = body.get_content()
    except (LookupError, UnicodeDecodeError):
        raw = body.get_payload(decode=True)
        content = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else ""
    if not isinstance(content, str):
        return ""
    if body.get_content_type() == "text/html":
        return html_to_text(content)
    return content.strip()


def _merge_references(parsed: EmailMessage, message_id: str) -> str:
    """Build the ``References`` chain a reply to ``parsed`` must carry.

    RFC 5322 3.6.4: the reply repeats the parent's ``References`` and appends the
    parent's ``Message-ID``. A parent that carries no ``References`` -- the second
    message of a thread in most mail clients -- keeps the thread root in
    ``In-Reply-To``, so fall back to it or the root is lost for good.
    """

    chain = _message_ids(parsed.get("References")) or _message_ids(parsed.get("In-Reply-To"))
    own = message_id.strip().strip("<>")
    if own:
        chain.append(own)
    return " ".join(f"<{ref}>" for ref in dict.fromkeys(chain))


def _message_ids(raw: Any) -> list[str]:
    """Return the bare message ids in a threading header value, in order.

    Clients decorate these headers with comments and drop the angle brackets, so
    a plain ``split()`` yields tokens that are not ids at all.
    """

    text = _HEADER_COMMENT_RE.sub(" ", str(raw or ""))
    return [token for token in (raw_id.strip().strip("<>") for raw_id in text.split()) if token]


def _first_literal(data: Any) -> bytes | None:
    """Pull the raw RFC822 payload out of an ``imaplib`` FETCH response."""

    for item in data or []:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
            return bytes(item[1])
    return None


def _parse_rfc822_size(data: Any) -> int | None:
    for item in data or []:
        raw = item[0] if isinstance(item, tuple) and item else item
        if isinstance(raw, (bytes, bytearray)):
            raw = bytes(raw).decode("ascii", "replace")
        if not isinstance(raw, str):
            continue
        match = re.search(r"RFC822\.SIZE\s+(\d+)", raw)
        if match:
            return int(match.group(1))
    return None


__all__ = [
    "CAPABILITY_TIER",
    "DM_SAFETY_TIERS",
    "FATAL_ERROR_CLASSES",
    "RETRYABLE_ERROR_CLASSES",
    "EmailChannel",
    "EmailChannelConfig",
    "decode_header_value",
    "html_to_text",
    "is_automated",
    "normalize_address",
    "reply_subject",
    "sender_allowed",
    "strip_quoted_reply",
    "thread_key_for",
]
