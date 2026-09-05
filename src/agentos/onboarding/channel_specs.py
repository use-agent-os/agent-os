"""Onboarding-friendly channel catalog aligned with gateway config models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

FieldType = Literal["text", "password", "select", "bool", "int", "float"]
Transport = Literal["polling", "webhook", "websocket", "mixed", "unknown"]


@dataclass(frozen=True)
class ChannelSetupField:
    name: str
    label: str
    field_type: FieldType
    required: bool
    default: str | int | float | bool | None = None
    choices: tuple[str, ...] = ()
    description: str = ""
    secret: bool = False
    group: str = "basic"
    advanced: bool = False
    show_when: dict[str, str] | None = None
    help: str = ""
    placeholder: str = ""


@dataclass(frozen=True)
class ChannelSetupSpec:
    type: str
    label: str
    description: str
    transport: Transport
    requires_public_url: bool
    dependency_extra: str | None
    restart_required: bool
    docs_hint: str
    fields: tuple[ChannelSetupField, ...]
    help: str = ""
    blocking: bool = False
    can_probe: bool = True
    readme_scenarios: tuple[str, ...] = ("chat channels", "first-run setup")


def _common_fields() -> tuple[ChannelSetupField, ...]:
    return (
        ChannelSetupField(
            "name", "Channel name", "text", required=True,
            description="Unique identifier for this channel entry.",
        ),
        ChannelSetupField(
            "agent_id", "Agent id", "text", required=False, default="main",
        ),
        ChannelSetupField(
            "enabled", "Enabled", "bool", required=False, default=True,
        ),
    )


def _slack_spec() -> ChannelSetupSpec:
    return ChannelSetupSpec(
        type="slack",
        label="Slack",
        description="Slack workspace bot - Socket Mode (websocket) or Events API webhook.",
        transport="mixed",
        requires_public_url=False,
        dependency_extra=None,
        restart_required=True,
        docs_hint="https://api.slack.com/apps",
        help=(
            "connection_mode=socket uses Slack Socket Mode (an outbound websocket) and "
            "needs no public URL - set app_token (xapp-...). connection_mode=webhook uses "
            "the Events API and needs a public Request URL reachable by Slack."
        ),
        fields=(
            *_common_fields(),
            ChannelSetupField("token", "Bot token (xoxb-...)", "password",
                              required=True, secret=True, group="credentials",
                              placeholder="xoxb-..."),
            ChannelSetupField("app_token", "App-level token (xapp-...)", "password",
                              required=False, secret=True, group="credentials",
                              placeholder="xapp-...",
                              show_when={"connection_mode": "socket"}),
            ChannelSetupField("app_id", "Slack app id", "text",
                              required=False, default="", advanced=True,
                              description="Required only for automatic command sync."),
            ChannelSetupField("manifest_token", "App configuration access token", "password",
                              required=False, secret=True, group="credentials",
                              placeholder="xoxe.xoxp-...", advanced=True,
                              description="Short-lived token used by apps.manifest.* APIs."),
            ChannelSetupField("command_request_url", "Slash-command request URL", "text",
                              required=False, default="", advanced=True,
                              description="Public Slack command endpoint used in the manifest."),
            ChannelSetupField("slack_channel_id", "Default channel id", "text",
                              required=False, default="",
                              description="Optional; replies auto-target the incoming "
                              "conversation when unset."),
            ChannelSetupField("signing_secret", "Signing secret", "password",
                              required=True, secret=True, group="credentials",
                              advanced=True,
                              show_when={"connection_mode": "webhook"}),
            ChannelSetupField("webhook_path", "Webhook path", "text",
                              required=False, default="", advanced=True,
                              description="Blank uses /slack/events for one webhook account, "
                              "or /slack/events/<account_name> for multiple accounts.",
                              show_when={"connection_mode": "webhook"}),
            ChannelSetupField("reply_in_thread", "Reply in thread", "bool",
                              required=False, default=False),
            ChannelSetupField("connection_mode", "Connection mode", "select",
                              required=False, default="webhook",
                              choices=("webhook", "socket")),
        ),
    )


def _discord_spec() -> ChannelSetupSpec:
    return ChannelSetupSpec(
        type="discord",
        label="Discord",
        description="Discord bot using gateway websocket.",
        transport="websocket",
        requires_public_url=False,
        dependency_extra=None,
        restart_required=True,
        docs_hint="https://discord.com/developers/applications",
        fields=(
            *_common_fields(),
            ChannelSetupField("token", "Bot token", "password",
                              required=True, secret=True),
            ChannelSetupField("application_id", "Application id", "text",
                              required=False, default=""),
            ChannelSetupField("default_channel_id", "Default channel id", "text",
                              required=False, default=""),
            ChannelSetupField("gateway_url", "Gateway URL", "text",
                              required=False,
                              default="wss://gateway.discord.gg/?v=10&encoding=json"),
            ChannelSetupField("intents", "Intents bitmask", "int",
                              required=False, default=33281),
        ),
    )


def _msteams_spec() -> ChannelSetupSpec:
    return ChannelSetupSpec(
        type="msteams",
        label="Microsoft Teams",
        description="Microsoft Teams via Bot Framework webhook.",
        transport="webhook",
        requires_public_url=True,
        dependency_extra=None,
        restart_required=True,
        docs_hint="https://learn.microsoft.com/microsoftteams/platform/",
        help="Microsoft Teams Bot Framework webhooks require a public HTTPS URL.",
        fields=(
            *_common_fields(),
            ChannelSetupField("app_id", "App id", "text", required=True),
            ChannelSetupField("app_password", "App password", "password",
                              required=True, secret=True),
            ChannelSetupField("webhook_path", "Webhook path", "text",
                              required=False, default="/msteams/messages"),
        ),
    )


def _email_spec() -> ChannelSetupSpec:
    return ChannelSetupSpec(
        type="email",
        label="Email",
        description="Email via IMAP polling (inbound) and SMTP (outbound).",
        transport="polling",
        requires_public_url=False,
        dependency_extra=None,
        restart_required=True,
        docs_hint="https://support.google.com/mail/answer/7126229",
        help=(
            "Needs no platform app registration - only IMAP and SMTP credentials. "
            "allowed_senders is a fail-closed From-address allowlist: exact "
            "addresses or domain patterns (*@example.com). One mail thread is one "
            "session. Providers with 2FA usually require an app password."
        ),
        fields=(
            *_common_fields(),
            ChannelSetupField("imap_host", "IMAP host", "text", required=True,
                              group="inbound", placeholder="imap.gmail.com"),
            ChannelSetupField("imap_port", "IMAP port", "int", required=False,
                              default=993, group="inbound"),
            ChannelSetupField("imap_ssl", "IMAP SSL", "bool", required=False,
                              default=True, group="inbound"),
            ChannelSetupField("imap_username", "IMAP username", "text",
                              required=True, group="inbound"),
            ChannelSetupField("imap_password", "IMAP password", "password",
                              required=True, secret=True, group="credentials"),
            ChannelSetupField("imap_folder", "IMAP folder", "text", required=False,
                              default="INBOX", group="inbound"),
            ChannelSetupField("smtp_host", "SMTP host", "text", required=True,
                              group="outbound", placeholder="smtp.gmail.com"),
            ChannelSetupField("smtp_port", "SMTP port", "int", required=False,
                              default=587, group="outbound"),
            ChannelSetupField("smtp_ssl", "SMTP implicit TLS", "bool", required=False,
                              default=False, group="outbound",
                              description="Use SMTPS (usually port 465) instead of STARTTLS."),
            ChannelSetupField("smtp_starttls", "SMTP STARTTLS", "bool", required=False,
                              default=True, group="outbound"),
            ChannelSetupField("smtp_username", "SMTP username", "text", required=False,
                              default="", group="outbound",
                              description="Defaults to unauthenticated relay when empty."),
            ChannelSetupField("smtp_password", "SMTP password", "password",
                              required=False, secret=True, default="",
                              group="credentials"),
            ChannelSetupField("from_address", "From address", "text", required=True,
                              placeholder="agent@example.com"),
            ChannelSetupField("from_name", "From display name", "text",
                              required=False, default=""),
            ChannelSetupField("allowed_senders", "Allowed senders", "text",
                              required=True,
                              description="Comma-separated From addresses or domain "
                              "patterns (*@example.com). Mail from anyone else is "
                              "dropped."),
            ChannelSetupField("poll_interval_s", "Poll interval (s)", "float",
                              required=False, default=30.0, group="polling"),
            ChannelSetupField("max_messages_per_poll", "Max messages per poll", "int",
                              required=False, default=10, group="polling"),
            ChannelSetupField("max_message_bytes", "Max message bytes", "int",
                              required=False, default=25 * 1024 * 1024,
                              advanced=True, group="polling"),
            ChannelSetupField("mark_seen", "Mark handled mail as seen", "bool",
                              required=False, default=True, group="polling"),
            ChannelSetupField("connect_timeout_s", "Connect timeout (s)", "float",
                              required=False, default=30.0, advanced=True),
        ),
    )


def _telegram_spec() -> ChannelSetupSpec:
    return ChannelSetupSpec(
        type="telegram",
        label="Telegram",
        description="Telegram Bot API — polling or webhook transport.",
        transport="mixed",
        requires_public_url=False,
        dependency_extra=None,
        restart_required=True,
        docs_hint="https://core.telegram.org/bots/api",
        fields=(
            *_common_fields(),
            ChannelSetupField("token", "Bot token", "password",
                              required=True, secret=True, group="credentials",
                              placeholder="123456:ABC..."),
            ChannelSetupField("default_chat_id", "Default chat id", "text",
                              required=False, default=""),
            ChannelSetupField("groups_enabled", "Enable configured groups", "bool",
                              required=False, default=False,
                              description="Direct messages always require pairing. "
                              "Groups are disabled unless explicitly enabled."),
            ChannelSetupField("group_chat_ids", "Allowed group chat ids", "text",
                              required=False, default="",
                              description="Comma-separated Telegram group chat IDs. "
                              "Every sender in these groups must still be paired."),
            ChannelSetupField("group_mention_required", "Require mention in groups", "bool",
                              required=False, default=True),
            ChannelSetupField("api_base", "API base", "text",
                              required=False, default="https://api.telegram.org"),
            ChannelSetupField("transport_name", "Transport", "select",
                              required=False, default="polling",
                              choices=("polling", "webhook")),
            ChannelSetupField("webhook_path", "Webhook path", "text",
                              required=False, default="/telegram/events",
                              group="webhook",
                              show_when={"transport_name": "webhook"}),
            ChannelSetupField("webhook_url", "Webhook URL (webhook only)", "text",
                              required=False, default="", group="webhook",
                              show_when={"transport_name": "webhook"},
                              placeholder="https://example.com/telegram/events"),
            ChannelSetupField("webhook_secret_token", "Webhook secret token",
                              "password", required=False, secret=True, default="",
                              group="webhook",
                              show_when={"transport_name": "webhook"}),
            ChannelSetupField("drop_pending_updates", "Drop pending updates",
                              "bool", required=False, default=False),
            ChannelSetupField("poll_timeout_s", "Polling timeout (s)", "int",
                              required=False, default=30, group="polling",
                              show_when={"transport_name": "polling"}),
            ChannelSetupField("poll_limit", "Poll limit", "int",
                              required=False, default=100, group="polling",
                              show_when={"transport_name": "polling"}),
            ChannelSetupField("poll_idle_sleep_s", "Poll idle sleep (s)", "float",
                              required=False, default=0.1, group="polling",
                              show_when={"transport_name": "polling"}),
        ),
    )


_BUILDERS = {
    "discord": _discord_spec,
    "email": _email_spec,
    # msteams is intentionally absent: the adapter is text-only and hidden
    # from runtime catalog surfaces until first-class support lands. The
    # _msteams_spec helper is retained for future restoration.
    "slack": _slack_spec,
    "telegram": _telegram_spec,
}


def list_channel_setup_specs() -> list[ChannelSetupSpec]:
    return [_BUILDERS[t]() for t in sorted(_BUILDERS)]


def get_channel_setup_spec(type_name: str) -> ChannelSetupSpec:
    if type_name not in _BUILDERS:
        raise KeyError(f"unknown channel type: {type_name!r}")
    return _BUILDERS[type_name]()


def channel_catalog_payload() -> list[dict[str, Any]]:
    return [
        {
            "type": s.type,
            "label": s.label,
            "description": s.description,
            "transport": s.transport,
            "requiresPublicUrl": s.requires_public_url,
            "dependencyExtra": s.dependency_extra,
            "restartRequired": s.restart_required,
            "docsHint": s.docs_hint,
            "help": s.help,
            "blocking": s.blocking,
            "canProbe": s.can_probe,
            "readmeScenarios": list(s.readme_scenarios),
            "whatYouNeed": _what_you_need(s),
            "fields": [
                {
                    "name": f.name,
                    "label": f.label,
                    "type": f.field_type,
                    "required": f.required,
                    "default": f.default,
                    "choices": list(f.choices),
                    "description": f.description,
                    "secret": f.secret,
                    "group": f.group,
                    "advanced": f.advanced,
                    "showWhen": dict(f.show_when or {}),
                    "help": f.help,
                    "placeholder": f.placeholder,
                }
                for f in s.fields
            ],
        }
        for s in list_channel_setup_specs()
    ]


def _what_you_need(spec: ChannelSetupSpec) -> list[str]:
    needs = [
        f"{field.label}."
        for field in spec.fields
        if field.required and field.name not in {"name", "enabled", "agent_id"}
    ]
    if spec.requires_public_url:
        needs.append("A public URL reachable by the channel provider.")
    if spec.dependency_extra:
        needs.append(f"Install the `{spec.dependency_extra}` optional extra.")
    if not needs:
        needs.append("A channel entry name and provider-side bot/app setup.")
    return needs
