"""What AgentOS knows about the environment variables it depends on.

An environment surface that only reports "OPENAI_API_KEY: not set" leaves the
operator to work out what that variable is for and where to obtain a value.
This module is the answer to both questions: a name-keyed description of every
variable AgentOS or an installed skill actually reads, assembled from the
places that already declare them rather than a hand-kept list that drifts.

Sources, most authoritative first:

1. **Provider setup specs** (``onboarding/*_specs.py``) — the five families of
   provider the setup flow already knows how to configure. Their ``env_key``
   is the same string the runtime reads, so the catalog stays correct by
   construction when a provider is added.
2. **Skill manifests** — ``requires.env`` in a skill's frontmatter, which may
   carry a description and a link to where the credential comes from.
3. **The user's own ``.env``** — anything present but undeclared is surfaced as
   ``custom`` rather than hidden, so a variable an operator added by hand is
   still manageable from the UI.

Nothing here holds a value. The catalog describes variables; reading one is a
separate, audited operation.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import structlog

from agentos import env_policy
from agentos.env_policy import ENV_NAME_RE

if TYPE_CHECKING:  # pragma: no cover - typing only
    from agentos.skills.loader import SkillLoader

log = structlog.get_logger(__name__)

#: Grouping used by listings. ``custom`` is the catch-all for undeclared names.
Category = str

CATEGORY_PROVIDER = "provider"
CATEGORY_SEARCH = "search"
CATEGORY_IMAGE = "image"
CATEGORY_AUDIO = "audio"
CATEGORY_MEMORY = "memory"
CATEGORY_SKILL = "skill"
CATEGORY_CUSTOM = "custom"

# Categories whose values are consumed once while the gateway boots — a
# provider client is constructed with the key it had at start. Changing one of
# these applies to the file and to newly spawned tools immediately, but the
# already-built client keeps the old value until a restart. Surfaces use this
# to decide whether to show a restart notice.
_BOOT_CONSUMED_CATEGORIES = frozenset(
    {CATEGORY_PROVIDER, CATEGORY_SEARCH, CATEGORY_IMAGE, CATEGORY_AUDIO, CATEGORY_MEMORY}
)


@dataclass(frozen=True)
class EnvVarSpec:
    """What is known about one environment variable — never its value."""

    name: str
    description: str = ""
    url: str = ""
    secret: bool = True
    category: Category = CATEGORY_CUSTOM
    owner: str = ""
    required: bool = False

    @property
    def restart_required(self) -> bool:
        """Whether changing this variable needs a gateway restart to fully apply."""
        return self.category in _BOOT_CONSUMED_CATEGORIES


def _is_env_var_name(value: str) -> bool:
    """Return whether *value* is a real variable name rather than a sentinel.

    ``env_key`` is not always one. Providers that authenticate by OAuth carry
    the literal string ``"OAuth"`` there, meaning "no API key involved" — and
    taking that at face value put a variable called ``OAuth`` in the catalog,
    and on the Environment screen, that no one could ever set.

    Requiring upper case is what separates the two: environment variables are
    conventionally shouted, sentinels are prose. It also holds for any future
    sentinel without this needing to know its name.
    """
    return bool(value) and value.isupper() and ENV_NAME_RE.match(value) is not None


def _provider_specs() -> list[EnvVarSpec]:
    """Return catalog entries derived from the onboarding provider families."""
    from agentos.onboarding.audio_specs import list_audio_provider_setup_specs
    from agentos.onboarding.image_generation_specs import (
        list_image_generation_provider_setup_specs,
    )
    from agentos.onboarding.memory_embedding_specs import (
        list_memory_embedding_provider_setup_specs,
    )
    from agentos.onboarding.provider_specs import list_provider_setup_specs
    from agentos.onboarding.search_specs import list_search_provider_setup_specs

    families: list[tuple[Category, str, list[Any]]] = [
        (CATEGORY_PROVIDER, "LLM provider", list(list_provider_setup_specs())),
        (CATEGORY_SEARCH, "Search provider", list(list_search_provider_setup_specs())),
        (
            CATEGORY_IMAGE,
            "Image generation provider",
            list(list_image_generation_provider_setup_specs()),
        ),
        (CATEGORY_AUDIO, "Audio provider", list(list_audio_provider_setup_specs())),
        (
            CATEGORY_MEMORY,
            "Memory embedding provider",
            list(list_memory_embedding_provider_setup_specs()),
        ),
    ]

    entries: list[EnvVarSpec] = []
    for category, kind, specs in families:
        for spec in specs:
            env_key = str(getattr(spec, "env_key", "") or "").strip()
            if not _is_env_var_name(env_key):
                continue
            label = str(getattr(spec, "label", "") or getattr(spec, "provider_id", "") or env_key)
            entries.append(
                EnvVarSpec(
                    name=env_key,
                    description=f"API key for {label} ({kind}).",
                    secret=True,
                    category=category,
                    owner=str(getattr(spec, "provider_id", "") or ""),
                    # Never required at the catalog level. A provider's
                    # ``requires_api_key`` means "this provider needs a key if
                    # you use it", and AgentOS talks to one provider at a time —
                    # flagging all forty as missing would drown the one that
                    # actually matters. Whether the *configured* provider has
                    # its key is what onboarding status already reports.
                    required=False,
                )
            )
    return entries


def _skill_specs(loader: SkillLoader | None) -> list[EnvVarSpec]:
    """Return catalog entries declared by loaded skills."""
    if loader is None:
        return []
    try:
        skills = loader.load_all()
    except Exception:  # pragma: no cover - a broken loader must not break listings
        # An unreadable skill directory or a malformed manifest must not take
        # down the env listing; the catalog degrades to provider entries only.
        log.debug("env_catalog.skill_scan_failed", exc_info=True)
        return []

    entries: list[EnvVarSpec] = []
    for skill in skills:
        meta = getattr(skill, "metadata", None)
        requires = getattr(meta, "requires", None) if meta else None
        for declared in getattr(requires, "env", []) or []:
            name = getattr(declared, "name", "") or str(declared)
            if not name:
                continue
            described = getattr(declared, "description", "")
            entries.append(
                EnvVarSpec(
                    name=name,
                    description=described or f"Required by the {skill.name} skill.",
                    url=getattr(declared, "url", "") or "",
                    secret=_declared_secret(declared, name),
                    category=CATEGORY_SKILL,
                    owner=skill.name,
                    required=bool(getattr(declared, "required", True)),
                )
            )
    return entries


def _declared_secret(declared: object, name: str) -> bool:
    """Return whether *declared* holds a credential, falling back to the name."""
    explicit = getattr(declared, "secret", None)
    if isinstance(explicit, bool):
        return explicit
    return env_policy.is_secret_name(name)


def build_catalog(
    loader: SkillLoader | None = None,
    *,
    present_names: set[str] | None = None,
) -> dict[str, EnvVarSpec]:
    """Return the known-variable catalog, keyed by name.

    The first source to declare a name wins, so a hand-written provider entry
    is not overwritten by a skill that happens to want the same credential —
    but the skill still contributes its owner, which is what a listing shows to
    explain *why* the variable matters.

    *present_names* are names found in the operator's ``.env``; any that no
    source declares are added as ``custom``. They default to secret because an
    unrecognised name could hold anything, and masking something harmless is a
    smaller mistake than printing something sensitive.
    """
    catalog: dict[str, EnvVarSpec] = {}
    for entry in [*_provider_specs(), *_skill_specs(loader)]:
        existing = catalog.get(entry.name)
        if existing is None:
            catalog[entry.name] = entry
            continue
        # Same name from a second source: keep the first description, but let a
        # later source fill blanks and record co-ownership.
        catalog[entry.name] = replace(
            existing,
            description=existing.description or entry.description,
            url=existing.url or entry.url,
            owner=existing.owner or entry.owner,
            required=existing.required or entry.required,
        )

    for name in sorted(present_names or set()):
        if name in catalog:
            continue
        catalog[name] = EnvVarSpec(
            name=name,
            description="Set in your .env but not declared by AgentOS or any installed skill.",
            secret=True,
            category=CATEGORY_CUSTOM,
        )
    return catalog


def describe(name: str, catalog: dict[str, EnvVarSpec] | None = None) -> EnvVarSpec:
    """Return the catalog entry for *name*, synthesizing one when unknown."""
    if catalog is not None and name in catalog:
        return catalog[name]
    return EnvVarSpec(
        name=name,
        secret=env_policy.is_secret_name(name),
        category=CATEGORY_CUSTOM,
    )
