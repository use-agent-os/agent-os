"""Tests for the channel catalog."""

from __future__ import annotations

import pytest

from agentos.gateway.config import (
    DiscordChannelEntry,
    SlackChannelEntry,
    TelegramChannelEntry,
)
from agentos.onboarding.channel_specs import (
    ChannelSetupSpec,
    channel_catalog_payload,
    get_channel_setup_spec,
    list_channel_setup_specs,
)

# msteams is intentionally absent: the adapter is text-only and hidden
# from runtime catalog surfaces until first-class support lands.
ALL_TYPES = {"discord", "slack", "telegram"}

ENTRY_MODELS = {
    "slack": SlackChannelEntry,
    "discord": DiscordChannelEntry,
    "telegram": TelegramChannelEntry,
}

CONDITIONAL_PUBLIC_URL = {"slack", "telegram"}
RETIRED_TYPES = {"dingtalk", "matrix", "qq", "qqbot", "wecom"}


def test_catalog_includes_all_channels():
    types = {s.type for s in list_channel_setup_specs()}
    assert types == ALL_TYPES


@pytest.mark.parametrize("type_name", sorted(ALL_TYPES))
def test_each_channel_has_common_fields(type_name: str):
    spec = get_channel_setup_spec(type_name)
    names = {f.name for f in spec.fields}
    assert {"name", "enabled", "agent_id"} <= names


@pytest.mark.parametrize("type_name", sorted(ALL_TYPES))
def test_spec_fields_align_with_pydantic_model(type_name: str):
    spec = get_channel_setup_spec(type_name)
    model = ENTRY_MODELS[type_name]
    pydantic_fields = set(model.model_fields.keys())
    spec_fields = {f.name for f in spec.fields}
    assert "type" not in spec_fields
    extra = spec_fields - pydantic_fields - {"type"}
    assert not extra, f"setup spec exposes unknown field(s): {extra}"


@pytest.mark.parametrize("type_name", sorted(ALL_TYPES))
def test_required_pydantic_fields_are_required_in_spec(type_name: str):
    spec = get_channel_setup_spec(type_name)
    model = ENTRY_MODELS[type_name]
    spec_required = {f.name for f in spec.fields if f.required}
    for fname, finfo in model.model_fields.items():
        if fname == "type":
            continue
        if finfo.is_required():
            assert fname in spec_required, (
                f"{type_name}.{fname} is required in pydantic but not in setup spec"
            )


def test_slack_secrets_are_marked_secret():
    spec = get_channel_setup_spec("slack")
    secrets = {f.name for f in spec.fields if f.secret}
    assert {"token", "app_token", "manifest_token", "signing_secret"} <= secrets


def test_telegram_secrets_are_marked_secret():
    spec = get_channel_setup_spec("telegram")
    secrets = {f.name for f in spec.fields if f.secret}
    assert {"token", "webhook_secret_token"} <= secrets


def test_slack_connection_mode_choices():
    spec = get_channel_setup_spec("slack")
    field = next(f for f in spec.fields if f.name == "connection_mode")
    assert field.field_type == "select"
    assert field.default == "webhook"
    assert field.choices == ("webhook", "socket")
    assert field.advanced is False


def test_slack_mode_specific_fields_are_conditional():
    spec = get_channel_setup_spec("slack")
    fields = {f.name: f for f in spec.fields}
    assert fields["app_token"].show_when == {"connection_mode": "socket"}
    assert fields["signing_secret"].show_when == {"connection_mode": "webhook"}
    assert fields["signing_secret"].required is True
    assert fields["slack_channel_id"].required is False


def test_telegram_webhook_fields_are_conditional():
    spec = get_channel_setup_spec("telegram")
    fields = {f.name: f for f in spec.fields}
    assert fields["transport_name"].default == "polling"
    assert fields["webhook_path"].show_when == {"transport_name": "webhook"}
    assert fields["webhook_url"].show_when == {"transport_name": "webhook"}
    assert fields["webhook_secret_token"].show_when == {"transport_name": "webhook"}
    assert fields["poll_timeout_s"].show_when == {"transport_name": "polling"}


def test_telegram_new_setup_defaults_to_account_pairing() -> None:
    spec = get_channel_setup_spec("telegram")
    field = next(item for item in spec.fields if item.name == "access_mode")

    assert field.field_type == "select"
    assert field.default == "pairing"
    assert field.choices == ("pairing", "allowlist", "open", "disabled")


def test_channel_catalog_payload_exposes_ui_metadata():
    payload = channel_catalog_payload()
    slack = next(c for c in payload if c["type"] == "slack")
    fields = {f["name"]: f for f in slack["fields"]}
    assert fields["token"]["group"] == "credentials"
    assert fields["signing_secret"]["showWhen"] == {"connection_mode": "webhook"}
    assert fields["app_token"]["showWhen"] == {"connection_mode": "socket"}
    assert slack["blocking"] is False
    assert slack["whatYouNeed"]
    assert "public URL" in slack["help"]
    assert slack["transport"] == "mixed"
    assert slack["requiresPublicUrl"] is False


@pytest.mark.parametrize("type_name", sorted(CONDITIONAL_PUBLIC_URL))
def test_conditional_webhook_channels_flagged(type_name: str):
    spec = get_channel_setup_spec(type_name)
    assert spec.transport in {"mixed", "webhook"}


def test_base_channel_specs_do_not_advertise_legacy_extras():
    assert get_channel_setup_spec("telegram").dependency_extra is None


def test_channel_catalog_payload_only_advertises_real_install_extras():
    payload = {entry["type"]: entry for entry in channel_catalog_payload()}
    assert payload["telegram"]["dependencyExtra"] is None


def test_unknown_channel_raises():
    with pytest.raises(KeyError):
        get_channel_setup_spec("not-a-channel")


@pytest.mark.parametrize("type_name", sorted(RETIRED_TYPES))
def test_retired_channels_are_absent_from_catalog(type_name: str) -> None:
    with pytest.raises(KeyError):
        get_channel_setup_spec(type_name)


def test_msteams_is_hidden_from_catalog():
    """msteams must not be advertised via the onboarding catalog."""
    types = {s.type for s in list_channel_setup_specs()}
    assert "msteams" not in types
    with pytest.raises(KeyError):
        get_channel_setup_spec("msteams")


def test_payload_redacts_secret_defaults():
    payload = channel_catalog_payload()
    for entry in payload:
        for f in entry["fields"]:
            if f.get("secret"):
                assert f["default"] in (None, "", False)


def test_catalog_is_sorted():
    types = [s.type for s in list_channel_setup_specs()]
    assert types == sorted(types)


def test_returns_setup_spec_instance():
    assert isinstance(get_channel_setup_spec("slack"), ChannelSetupSpec)
