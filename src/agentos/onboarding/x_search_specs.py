"""Onboarding descriptor for the xAI-backed X (Twitter) search tool.

X Search is not a web-search provider — it answers from X's post index rather
than returning ranked pages — so it does not appear in
``agentos.search.registry``. Setup surfaces still need a field list, a label,
and the env key to explain, which is what this module provides.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, get_args

from agentos.gateway.config import XSearchConfig

FieldType = Literal["text", "password", "select", "bool", "int"]

X_SEARCH_PROVIDER_ID = "x_search"
X_SEARCH_LABEL = "X (Twitter) Search"
X_SEARCH_ENV_KEY = "XAI_API_KEY"


@dataclass(frozen=True)
class XSearchSetupField:
    name: str
    label: str
    field_type: FieldType
    required: bool
    default: str | int | bool | None = None
    choices: tuple[str, ...] = ()
    description: str = ""
    secret: bool = False


@dataclass(frozen=True)
class XSearchSetupSpec:
    provider_id: str
    label: str
    requires_api_key: bool
    env_key: str
    deployment: Literal["cloud", "local"]
    what_you_need: tuple[str, ...]
    capabilities: tuple[str, ...]
    fields: tuple[XSearchSetupField, ...]


def _reasoning_effort_choices() -> tuple[str, ...]:
    """The accepted values, read off the config field that enforces them."""
    annotation = XSearchConfig.model_fields["reasoning_effort"].annotation
    return tuple(str(value) for value in get_args(annotation))


def _fields() -> tuple[XSearchSetupField, ...]:
    # Defaults come from the config model, not from the tool module: onboarding
    # describes configuration, and importing the tool here would both invert the
    # layering and register ``x_search`` on the default registry as a side
    # effect of a descriptor lookup.
    defaults = XSearchConfig()

    return (
        XSearchSetupField(
            name="api_key",
            label="xAI API key",
            field_type="password",
            required=True,
            description=f"Stored under env key {defaults.api_key_env}.",
            secret=True,
        ),
        XSearchSetupField(
            name="model",
            label="Grok model",
            field_type="text",
            required=False,
            default=defaults.model,
            description="Any Grok model with access to xAI's server-side x_search tool.",
        ),
        XSearchSetupField(
            name="reasoning_effort",
            label="Reasoning effort",
            field_type="select",
            required=False,
            default="",
            choices=_reasoning_effort_choices(),
            description="Leave empty to use the model's own default.",
        ),
        XSearchSetupField(
            name="timeout_seconds",
            label="Per-attempt timeout (s)",
            field_type="int",
            required=False,
            default=int(defaults.timeout_seconds),
            description="A complex X Search can take 60-120s.",
        ),
        XSearchSetupField(
            name="total_timeout_seconds",
            label="Total timeout (s)",
            field_type="int",
            required=False,
            default=int(defaults.total_timeout_seconds),
            description="Hard wall for the whole call, retries included.",
        ),
        XSearchSetupField(
            name="retries",
            label="Retries",
            field_type="int",
            required=False,
            default=defaults.retries,
            description="Retried only on 5xx, timeout, and connection errors.",
        ),
        XSearchSetupField(
            name="base_url",
            label="Base URL",
            field_type="text",
            required=False,
            default=defaults.base_url,
            description="Only change this for an HTTPS proxy that speaks xAI's Responses API.",
        ),
    )


def get_x_search_setup_spec() -> XSearchSetupSpec:
    return XSearchSetupSpec(
        provider_id=X_SEARCH_PROVIDER_ID,
        label=X_SEARCH_LABEL,
        requires_api_key=True,
        env_key=X_SEARCH_ENV_KEY,
        deployment="cloud",
        what_you_need=(
            f"An xAI API key via {X_SEARCH_ENV_KEY} or a one-time paste.",
            "Or a SuperGrok / X Premium+ login: sign in below, or run "
            "`agentos auth login xai`.",
            "A login is preferred over a key when both are present.",
        ),
        capabilities=("x_posts", "citations", "image_understanding", "video_understanding"),
        fields=_fields(),
    )


def x_search_catalog_payload() -> list[dict[str, Any]]:
    """Return the section as a one-row list.

    There is only ever one X Search provider, but every other catalog section is
    a list and the CLI renderer walks rows generically. A lone dict would need a
    special case in three places to display at all.
    """
    spec = get_x_search_setup_spec()
    return [_spec_payload(spec)]


def _spec_payload(spec: XSearchSetupSpec) -> dict[str, Any]:
    return {
        "providerId": spec.provider_id,
        "label": spec.label,
        "requiresApiKey": spec.requires_api_key,
        "envKey": spec.env_key,
        "deployment": spec.deployment,
        "whatYouNeed": list(spec.what_you_need),
        "capabilities": list(spec.capabilities),
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
            }
            for f in spec.fields
        ],
    }
