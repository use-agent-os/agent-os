"""Sender admission for chat channels: a static allowlist plus optional pairing.

Discord, Slack and MS Teams reach the agent with the operator's tools, so an
inbound sender has to be admitted by the operator first. Two mechanisms do
that, and either one (or both) must be configured:

- ``allowed_user_ids``: platform user ids the operator lists in config.
- ``pairing_enabled``: the Telegram pairing flow. An unknown DM sender gets a
  one-time code, and the operator approves it with
  ``agentos channels pairing approve <channel> <code>``. Grants live in the
  shared :class:`~agentos.channel_pairing.ChannelPairingStore`.

With neither configured the channel fails closed: every inbound sender is
refused, and ``start()`` logs ``channel.admission_closed`` telling the operator
how to admit someone. The same decision gates chat turns, slash commands and
approval buttons, since dispatch and the adapters' interaction handlers all
call ``evaluate_access``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import structlog

from agentos.channel_pairing import (
    SENDER_ADMISSION_CHANNEL_TYPES,
    AdmissionMode,
    ChannelPairingStore,
    PairingStoreError,
    admission_mode,
    admission_payload,
    normalize_user_ids,
)
from agentos.channels._util import AccessDecision, ChannelAccessPolicy
from agentos.channels.types import IncomingMessage

log = structlog.get_logger(__name__)

#: Sender ids adapters substitute when the platform payload had none. They are
#: never a real user, so they are never admitted, even if listed.
_PLACEHOLDER_SENDER_IDS = frozenset({"", "unknown"})


@dataclass
class SenderAdmission:
    """Admission state for one configured channel entry."""

    channel_name: str
    platform_label: str
    allowed_user_ids: frozenset[str] = frozenset()
    pairing_enabled: bool = False
    pairing_store: ChannelPairingStore = field(default_factory=ChannelPairingStore)
    #: Adapter-specific stand-ins for "no sender" (Slack's ``sender_id``).
    placeholder_sender_ids: frozenset[str] = frozenset()

    def _is_placeholder(self, sender_id: str) -> bool:
        return sender_id in _PLACEHOLDER_SENDER_IDS or sender_id in self.placeholder_sender_ids

    @property
    def mode(self) -> AdmissionMode:
        return admission_mode(
            allowed_user_ids=self.allowed_user_ids,
            pairing_enabled=self.pairing_enabled,
        )

    @property
    def closed(self) -> bool:
        return self.mode == "closed"

    def strict_policy(self) -> ChannelAccessPolicy:
        """The ``policy`` declaration for code that still reads it directly.

        ``allowlist_enabled`` makes an empty allowlist deny everyone, so a
        caller that bypasses ``evaluate_access`` cannot fall back to open.
        """
        return ChannelAccessPolicy(
            dm_allowed=True,
            group_allowed=True,
            mention_required_in_group=True,
            allowlist=self.allowed_user_ids,
            allowlist_enabled=True,
        )

    def log_if_closed(self) -> None:
        if not self.closed:
            return
        log.warning(
            "channel.admission_closed",
            channel=self.channel_name,
            platform=self.platform_label,
            detail=(
                "No allowed_user_ids and pairing_enabled=false: every inbound "
                "message, slash command and approval button is refused. Set "
                f"channels.{self.channel_name}.allowed_user_ids or enable pairing; "
                "see docs/channels.md#sender-admission."
            ),
        )

    def evaluate(
        self,
        sender_id: str,
        *,
        is_group: bool,
        mentioned: bool,
        mention_required_in_group: bool = True,
    ) -> AccessDecision:
        sender_id = str(sender_id or "").strip()
        if is_group and mention_required_in_group and not mentioned:
            return AccessDecision(admit=False, reason="not_mentioned_in_group")
        admitted_reason: Literal["group_admitted", "dm_admitted"] = (
            "group_admitted" if is_group else "dm_admitted"
        )
        if self._is_placeholder(sender_id):
            return AccessDecision(admit=False, reason="not_in_allowlist")
        if sender_id in self.allowed_user_ids:
            return AccessDecision(admit=True, reason=admitted_reason)
        if not self.pairing_enabled:
            return AccessDecision(admit=False, reason="not_in_allowlist")
        try:
            admission = self.pairing_store.admission(self.channel_name, sender_id)
        except PairingStoreError as exc:
            log.error(
                "channel.pairing_store_error",
                channel=self.channel_name,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            admission = None
        if admission is None:
            return AccessDecision(admit=False, reason="not_paired")
        return AccessDecision(
            admit=True,
            reason=admitted_reason,
            admission=admission,
            admission_validator=self.pairing_store.validate_admission,
        )

    def record_denial(self, message: IncomingMessage, reason: str) -> None:
        """Log the refusal and, for an unpaired DM, open a pairing request."""
        metadata = message.metadata
        metadata["access_denial_reason"] = reason
        sender_id = str(message.sender_id or "").strip()
        log.info(
            "channel.sender_not_admitted",
            channel=self.channel_name,
            sender_id=sender_id,
            reason=reason,
            mode=self.mode,
        )
        if reason != "not_paired" or not self.pairing_enabled:
            return
        if bool(metadata.get("is_group")) or self._is_placeholder(sender_id):
            return
        profile = {
            "sender_id": sender_id,
            "username": str(metadata.get("sender_username") or ""),
            "display_name": str(metadata.get("sender_display_name") or ""),
            "chat_id": str(message.channel_id or ""),
        }
        try:
            result = self.pairing_store.request(self.channel_name, sender_id, profile=profile)
        except PairingStoreError as exc:
            log.error(
                "channel.pairing_store_error",
                channel=self.channel_name,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            metadata["pairing_request_status"] = "store_error"
            return
        metadata["pairing_request_status"] = result.status
        metadata["pairing_request_created"] = result.created
        if result.code:
            metadata["pairing_code"] = result.code
        if result.retry_after_s:
            metadata["pairing_retry_after_s"] = result.retry_after_s

    def denial_notice(self, message: IncomingMessage) -> str | None:
        """Text to send back to a refused sender, or ``None`` to stay silent.

        Only a freshly created pairing code is answered: anything else would
        let a stranger make the bot talk on every message.
        """
        if not message.metadata.get("pairing_request_created"):
            return None
        code = str(message.metadata.get("pairing_code") or "")
        if not code:
            return None
        return (
            f"Pairing code: {code}\n\nIt expires in 1 hour. Ask the operator of this "
            "AgentOS bot to approve it with: "
            f"agentos channels pairing approve {self.channel_name} {code}"
        )

    def snapshot(self) -> dict[str, Any]:
        pairing = self.pairing_store.snapshot(self.channel_name)
        return {
            "mode": self.mode,
            "allowed_user_ids": sorted(self.allowed_user_ids),
            "pairing_enabled": self.pairing_enabled,
            "pending": pairing["pending"],
            "paired": pairing["approved"],
            "locked_until": pairing["locked_until"],
        }

    def resolve_request(self, sender_id: str, *, approved: bool) -> dict[str, Any]:
        sender_id = str(sender_id).strip()
        pending = next(
            (
                item
                for item in self.pairing_store.snapshot(self.channel_name)["pending"]
                if str(item.get("sender_id") or "") == sender_id
            ),
            None,
        )
        if pending is None:
            raise KeyError(f"{self.platform_label} pairing request not found: {sender_id}")
        if approved:
            return self.pairing_store.approve(self.channel_name, str(pending["code"]))
        return self.pairing_store.deny(self.channel_name, sender_id)

    def revoke(self, sender_id: str) -> str:
        sender_id = str(sender_id).strip()
        self.pairing_store.revoke(self.channel_name, sender_id)
        return "pairing"


__all__ = [
    "SENDER_ADMISSION_CHANNEL_TYPES",
    "AdmissionMode",
    "SenderAdmission",
    "admission_mode",
    "admission_payload",
    "normalize_user_ids",
]
