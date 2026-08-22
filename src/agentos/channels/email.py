"""Email channel adapter backed by standard IMAP and SMTP protocols."""

from __future__ import annotations

import asyncio
import contextlib
import email
import email.utils
import imaplib
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.header import decode_header
from email.message import Message
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Literal

import structlog
from pydantic import BaseModel, Field

from agentos.channels._util import AccessDecision, ChannelAccessPolicy
from agentos.channels.contract import (
    ChannelCapabilities,
    ChannelCapabilityProfile,
    ChannelPlatformCapability,
    ChannelPlatformCapabilityStatus,
    ChannelPlatformCategories,
    ChannelPlatformManifest,
    ChannelSendResult,
)
from agentos.channels.types import Attachment, ChannelHealth, IncomingMessage, OutgoingMessage

log = structlog.get_logger(__name__)

CAPABILITY_TIER = "YELLOW-experimental"

DM_SAFETY_TIERS: tuple[str, ...] = ("safe",)

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


class EmailChannelConfig(BaseModel):
    """Adapter-level config for Email (IMAP/SMTP) channel."""

    name: str = "email"
    imap_server: str = ""
    imap_port: int = 993
    imap_use_ssl: bool = True
    imap_username: str = ""
    imap_password: str = ""
    smtp_server: str = ""
    smtp_port: int = 587
    smtp_use_tls: bool = True
    smtp_username: str = ""
    smtp_password: str = ""
    allowed_from_addresses: list[str] = Field(default_factory=list)
    poll_interval_s: float = 30.0


@dataclass
class EmailChannel:
    """Managed adapter for Email (IMAP/SMTP) polling and sending."""

    config: EmailChannelConfig
    policy: ChannelAccessPolicy = field(
        default_factory=lambda: ChannelAccessPolicy(
            dm_allowed=True,
            group_allowed=False,
            allowlist=frozenset(),
        )
    )
    _queue: asyncio.Queue[IncomingMessage] = field(
        default_factory=asyncio.Queue, init=False, repr=False
    )
    _poll_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _connected: bool = field(default=False, init=False, repr=False)
    _last_message_at: datetime | None = field(default=None, init=False, repr=False)
    _last_headers: dict[str, dict[str, str]] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self.policy = ChannelAccessPolicy(
            dm_allowed=True,
            group_allowed=False,
            allowlist=frozenset(self.config.allowed_from_addresses),
            allowlist_enabled=len(self.config.allowed_from_addresses) > 0,
        )

    def evaluate_access(
        self,
        message: IncomingMessage,
        *,
        is_group: bool,
        mentioned: bool,
    ) -> AccessDecision:
        sender = message.sender_id.lower().strip()
        if not self.config.allowed_from_addresses:
            return AccessDecision(admit=True, reason="dm_admitted")

        for pattern in self.config.allowed_from_addresses:
            pat = pattern.lower().strip()
            if pat.startswith("@"):
                if sender.endswith(pat):
                    return AccessDecision(admit=True, reason="dm_admitted")
            elif pat.startswith("*@"):
                if sender.endswith(pat[1:]):
                    return AccessDecision(admit=True, reason="dm_admitted")
            elif sender == pat:
                return AccessDecision(admit=True, reason="dm_admitted")

        return AccessDecision(admit=False, reason="not_in_allowlist")

    @property
    def capability_profile(self) -> ChannelCapabilityProfile:
        return ChannelCapabilityProfile(
            channel_type="email",
            group_chat=False,
            mentions=False,
            typing_indicator=False,
            streaming=False,
            native_file_upload=True,
            media=True,
            reply=True,
            thread_reply=True,
            edit=False,
            delete=False,
            transports=("polling",),
        )

    @property
    def platform_capability_manifest(self) -> ChannelPlatformManifest:
        return ChannelPlatformManifest.from_channel_profile(
            self.capability_profile,
            has_send_file=True,
            has_inbound_attachment_resolver=True,
        ).with_capabilities(
            ChannelPlatformCapability(
                category=ChannelPlatformCategories.FILES,
                status=ChannelPlatformCapabilityStatus.SUPPORTED,
                notes=("Emails support standard MIME attachments.",),
            ),
            ChannelPlatformCapability(
                category=ChannelPlatformCategories.ATTACHMENTS,
                status=ChannelPlatformCapabilityStatus.SUPPORTED,
                notes=("Inbound files are extracted directly from mail MIME parts.",),
            ),
            ChannelPlatformCapability(
                category=ChannelPlatformCategories.THREADS,
                status=ChannelPlatformCapabilityStatus.SUPPORTED,
                notes=("Threaded conversation matching via In-Reply-To/References.",),
            ),
        )

    @property
    def capabilities(self) -> frozenset[str]:
        return self.capability_profile.capability_tags()

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._validate_credentials)
        except (imaplib.IMAP4.error, smtplib.SMTPAuthenticationError) as exc:
            log.error("email.auth_failed", error=str(exc))
            raise RuntimeError(f"Email credentials validation failed: {exc}") from exc
        except (ssl.SSLError, OSError) as exc:
            log.error("email.connect_failed", error=str(exc))
            raise ConnectionError(f"Email server connection failed: {exc}") from exc

        self._poll_task = asyncio.create_task(
            self._poll_loop(), name=f"email:poll:{self.config.name}"
        )
        self._connected = True
        log.info("email.started", name=self.config.name)

    async def stop(self) -> None:
        task = self._poll_task
        self._poll_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._connected = False
        log.info("email.stopped", name=self.config.name)

    async def health_check(self) -> ChannelHealth:
        return ChannelHealth(
            connected=self._connected,
            bot_user_id=self.config.imap_username or self.config.smtp_username,
            last_message_at=self._last_message_at,
            extra={"poll_interval_s": self.config.poll_interval_s},
        )

    async def receive(self) -> IncomingMessage:
        return await self._queue.get()

    def build_reply_message(self, content: str, msg: IncomingMessage) -> OutgoingMessage:
        metadata = {
            "subject": msg.metadata.get("subject", ""),
            "message_id": msg.metadata.get("native_message_id", ""),
            "references": msg.metadata.get("references", ""),
            "thread_id": msg.metadata.get("native_chat_id", ""),
        }
        return OutgoingMessage(
            content=content,
            reply_to=msg.sender_id,
            metadata=metadata,
        )

    async def send(self, message: OutgoingMessage) -> None:
        if not message.reply_to:
            log.error("email.send_failed", reason="missing reply_to recipient")
            return

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._send_email, message)
        log.info("email.sent_reply", recipient=message.reply_to)

    async def send_file(
        self,
        channel_id: str,
        file_path: str,
        content: str = "",
    ) -> ChannelSendResult:
        loop = asyncio.get_running_loop()
        headers = self._last_headers.get(channel_id, {})
        msg = OutgoingMessage(
            content=content,
            reply_to=channel_id,
            attachments=[
                Attachment(
                    name=Path(file_path).name,
                    data=Path(file_path).read_bytes(),
                )
            ],
            metadata=headers,
        )
        await loop.run_in_executor(None, self._send_email, msg)
        return ChannelSendResult.sent(
            capability=ChannelCapabilities.NATIVE_FILE_UPLOAD,
            target_id=channel_id,
        )

    async def edit(self, message_id: str, content: str) -> None:
        raise NotImplementedError("Email edits are not supported")

    async def delete(self, message_id: str) -> None:
        raise NotImplementedError("Email deletion is not supported")

    def resolve_inbound_attachment(self, attachment: Attachment) -> Attachment:
        return attachment

    # ── Blocking operations run in executors ───────────────────

    def _validate_credentials(self) -> None:
        imap: imaplib.IMAP4
        if self.config.imap_use_ssl:
            imap = imaplib.IMAP4_SSL(self.config.imap_server, self.config.imap_port)
        else:
            imap = imaplib.IMAP4(self.config.imap_server, self.config.imap_port)
        try:
            imap.login(self.config.imap_username, self.config.imap_password)
        finally:
            with contextlib.suppress(Exception):
                imap.logout()

        smtp = smtplib.SMTP(self.config.smtp_server, self.config.smtp_port)
        try:
            if self.config.smtp_use_tls:
                smtp.starttls()
            smtp.login(self.config.smtp_username, self.config.smtp_password)
        finally:
            with contextlib.suppress(Exception):
                smtp.quit()

    def _send_email(self, msg: OutgoingMessage) -> None:
        recipient = msg.reply_to
        if not recipient:
            return

        subject = msg.metadata.get("subject", "")
        if subject:
            subject = subject.strip()
            if not subject.lower().startswith("re:"):
                subject = f"Re: {subject}"
        else:
            subject = "Re: AgentOS Thread"

        mime = MIMEMultipart()
        mime["From"] = self.config.smtp_username
        mime["To"] = recipient
        mime["Subject"] = subject

        parent_msg_id = msg.metadata.get("message_id")
        if parent_msg_id:
            mime["In-Reply-To"] = parent_msg_id
            parent_refs = msg.metadata.get("references", "")
            mime["References"] = f"{parent_refs} {parent_msg_id}".strip()

        mime.attach(MIMEText(msg.content, "plain", "utf-8"))

        for att in msg.attachments:
            if not att.data:
                continue
            part = MIMEApplication(att.data)
            filename = att.name or "attachment"
            part.add_header("Content-Disposition", "attachment", filename=filename)
            mime.attach(part)

        smtp = smtplib.SMTP(self.config.smtp_server, self.config.smtp_port)
        try:
            if self.config.smtp_use_tls:
                smtp.starttls()
            smtp.login(self.config.smtp_username, self.config.smtp_password)
            smtp.sendmail(self.config.smtp_username, [recipient], mime.as_string())
        finally:
            with contextlib.suppress(Exception):
                smtp.quit()

    async def _poll_loop(self) -> None:
        while True:
            try:
                loop = asyncio.get_running_loop()
                messages = await loop.run_in_executor(None, self._poll_inbox)
                for incoming in messages:
                    self._last_message_at = datetime.now(UTC)
                    self._last_headers[incoming.sender_id] = {
                        "subject": incoming.metadata.get("subject", ""),
                        "message_id": incoming.metadata.get("native_message_id", ""),
                        "references": incoming.metadata.get("references", ""),
                        "thread_id": incoming.metadata.get("native_chat_id", ""),
                    }
                    await self._queue.put(incoming)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("email.poll_failed", error=str(exc))

            await asyncio.sleep(self.config.poll_interval_s)

    def _poll_inbox(self) -> list[IncomingMessage]:
        imap: imaplib.IMAP4
        if self.config.imap_use_ssl:
            imap = imaplib.IMAP4_SSL(self.config.imap_server, self.config.imap_port)
        else:
            imap = imaplib.IMAP4(self.config.imap_server, self.config.imap_port)

        incoming_messages: list[IncomingMessage] = []
        try:
            imap.login(self.config.imap_username, self.config.imap_password)
            imap.select("INBOX")
            status, response = imap.search(None, "UNSEEN")
            if status != "OK":
                return []

            msg_ids = response[0].split()
            for msg_id in msg_ids:
                fetch_status, fetch_data = imap.fetch(msg_id, "(RFC822)")
                if fetch_status != "OK" or not fetch_data or not isinstance(fetch_data[0], tuple):
                    continue

                raw_email = fetch_data[0][1]
                if isinstance(raw_email, bytes):
                    msg = email.message_from_bytes(raw_email)
                    incoming = self._parse_email_message(msg)
                    if incoming:
                        incoming_messages.append(incoming)
                        imap.store(msg_id, "+FLAGS", "\\Seen")
        finally:
            with contextlib.suppress(Exception):
                imap.logout()

        return incoming_messages

    def _parse_email_message(self, msg: Message) -> IncomingMessage | None:
        sender_header = msg.get("From", "")
        sender_name, sender_addr = email.utils.parseaddr(sender_header)
        sender_addr = sender_addr.strip().lower()
        if not sender_addr:
            return None

        subject_header = msg.get("Subject", "")
        subject = self._decode_header_str(subject_header)

        message_id = msg.get("Message-ID", "").strip()
        in_reply_to = msg.get("In-Reply-To", "").strip()
        references = msg.get("References", "").strip()

        norm_subj = subject
        for prefix in ("re:", "fwd:", "fw:", "re :", "fwd :"):
            if norm_subj.lower().startswith(prefix):
                norm_subj = norm_subj[len(prefix) :].strip()

        ref_ids = [ref.strip() for ref in references.split() if ref.strip()]
        if ref_ids:
            thread_id = ref_ids[0]
        elif in_reply_to:
            thread_id = in_reply_to
        else:
            thread_id = message_id

        body = ""
        attachments: list[Attachment] = []

        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition", ""))

                if "attachment" in content_disposition:
                    file_data = part.get_payload(decode=True)
                    if isinstance(file_data, bytes):
                        filename = self._decode_header_str(part.get_filename()) or "attachment"
                        attachments.append(
                            Attachment(
                                name=filename,
                                mime_type=content_type,
                                data=file_data,
                                size=len(file_data),
                            )
                        )
                elif content_type == "text/plain" and "attachment" not in content_disposition:
                    payload = part.get_payload(decode=True)
                    if isinstance(payload, bytes):
                        charset = part.get_content_charset() or "utf-8"
                        body += payload.decode(charset, errors="replace")
        else:
            payload = msg.get_payload(decode=True)
            if isinstance(payload, bytes):
                body = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")

        metadata = {
            "conversation_kind": "thread",
            "native_message_id": message_id,
            "native_chat_id": thread_id,
            "native_thread_id": thread_id,
            "subject": subject,
            "references": references,
            "is_group": True,
        }

        return IncomingMessage(
            sender_id=sender_addr,
            channel_id=sender_addr,
            content=body.strip(),
            attachments=attachments,
            metadata=metadata,
        )

    def _decode_header_str(self, header_value: str | None) -> str:
        if not header_value:
            return ""
        decoded_parts = decode_header(header_value)
        value_parts = []
        for text, encoding in decoded_parts:
            if isinstance(text, bytes):
                try:
                    value_parts.append(text.decode(encoding or "utf-8", errors="replace"))
                except LookupError:
                    value_parts.append(text.decode("utf-8", errors="replace"))
            else:
                value_parts.append(str(text))
        return "".join(value_parts)


class ChannelEntry(BaseModel):
    """Pydantic model validating Email configuration in gateway config."""

    type: Literal["email"] = "email"
    imap_server: str
    imap_port: int = 993
    imap_use_ssl: bool = True
    imap_username: str
    imap_password: str
    smtp_server: str
    smtp_port: int = 587
    smtp_use_tls: bool = True
    smtp_username: str
    smtp_password: str
    allowed_from_addresses: list[str] = Field(default_factory=list)
    poll_interval_s: float = 30.0
