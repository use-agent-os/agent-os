"""Image model registry: capabilities, availability and price per model.

One entry per model the ``image`` node can use. The registry drives the model
dropdown (``flows.models``), validation (what a model can do), the widget set
(``options_from`` / ``show_when``), price estimates and adapter selection.

Prices are hand-maintained and approximate; every entry says where its numbers
came from. Model ids were checked against the OpenRouter catalog on 2026-10-29;
no image has been generated against a live key yet. Availability is decided
by an injected auth probe (the gateway passes one that reads the configured
image-generation providers).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agentos.flows.types import price_range, price_unknown, price_usd

GEMINI_RATIOS = (
    "1:1",
    "2:3",
    "3:2",
    "3:4",
    "4:3",
    "4:5",
    "5:4",
    "9:16",
    "16:9",
    "21:9",
    "1:4",
    "4:1",
    "1:8",
    "8:1",
)
GEMINI_PRO_RATIOS = GEMINI_RATIOS[:10]
GPT_IMAGE_SIZES = ("auto", "1024x1024", "1536x1024", "1024x1536")
GPT_IMAGE_QUALITIES = ("auto", "low", "medium", "high")

#: Inputs whose value comes from the model's output caps.
CAP_FIELDS = {
    "model.ratios": "ratios",
    "model.tiers": "tiers",
    "model.sizes": "sizes",
    "model.qualities": "qualities",
}


@dataclass(frozen=True)
class ModelEntry:
    id: str
    display: str
    provider: str
    api_model: str
    family: str
    #: Other API ids that mean this entry (``[image_generation].primary`` matching).
    aliases: tuple[str, ...] = ()
    status: str = "active"
    replaced_by: str | None = None
    t2i: bool = True
    edit: bool = True
    mask: bool = False
    max_images: int = 1
    ratios: tuple[str, ...] = ()
    tiers: tuple[str, ...] = ()
    sizes: tuple[str, ...] = ()
    qualities: tuple[str, ...] = ()
    transparent_bg: bool = False
    n_max: int = 1
    seed_honored: bool = False
    timeout_s: float = 180.0
    per_image: dict[str, float] = field(default_factory=dict)
    per_ref: float = 0.0
    price_approximate: bool = True
    price_source: str = ""
    #: Upper bounds per ``per_image`` key when the provider meters by token.
    per_image_max: dict[str, float] | None = None
    #: A separate provider model for edit / multi-reference (Surplus pairs a
    #: text-to-image id with an ``-edit`` id); ``None``: ``api_model`` does both.
    edit_api_model: str | None = None
    #: Price table of the edit model when it differs from ``per_image``.
    edit_per_image: dict[str, float] | None = None
    #: What the edit model accepts, when it differs (``None``: same as above).
    edit_ratios: tuple[str, ...] | None = None
    edit_tiers: tuple[str, ...] | None = None

    def api_model_for(self, *, edit: bool) -> str:
        """The provider model id for a text-to-image or an edit call."""
        return self.edit_api_model if edit and self.edit_api_model else self.api_model

    def caps(self, name: str) -> Any:
        """``ratios | tiers | sizes | qualities | transparent_bg``."""
        return getattr(self, name, None)

    def has_cap(self, name: str) -> bool:
        return bool(self.caps(name))

    def options_for(self, options_from: str) -> tuple[str, ...]:
        field_name = CAP_FIELDS.get(options_from)
        return tuple(getattr(self, field_name, ()) or ()) if field_name else ()

    # ── pricing ─────────────────────────────────────────────────────────

    def unit_price(
        self, params: dict[str, Any], *, edit: bool = False
    ) -> tuple[float, float] | None:
        """``(low, high)`` USD for one image with *params*, ``None`` if unknown.

        Lookup order: ``per_image[tier]``, ``per_image["<quality>:<size>"]``,
        ``per_image["default"]``. An ``auto`` quality or size spans every
        matching key, which is why the result is a range; so does a
        token-metered price with a ``per_image_max`` bound. ``edit`` prices the
        edit model when it has its own table.
        """
        if edit and self.edit_per_image:
            table, ceiling = self.edit_per_image, None
        else:
            table, ceiling = self.per_image, self.per_image_max
        if not table:
            return None
        tier = params.get("resolution")
        if self.tiers and isinstance(tier, str) and tier in table:
            value = table[tier]
            high = (ceiling or {}).get(tier, (ceiling or {}).get("default", value))
            return value, max(value, high)
        if self.qualities or self.sizes:
            quality = str(params.get("quality") or "auto")
            size = str(params.get("size") or "auto")
            qualities = (
                [q for q in self.qualities if q != "auto"] if quality == "auto" else [quality]
            )
            sizes = [s for s in self.sizes if s != "auto"] if size == "auto" else [size]
            found = [table[f"{q}:{s}"] for q in qualities for s in sizes if f"{q}:{s}" in table]
            if found:
                return min(found), max(found)
        if "default" in table:
            value = table["default"]
            high = (ceiling or {}).get("default", value)
            return value, max(value, high)
        return None

    def estimate(self, params: dict[str, Any], *, count: int, refs: int) -> dict[str, Any]:
        """Price of ``count`` images; connected images (``refs``) mean an edit call."""
        unit = self.unit_price(params, edit=refs > 0)
        if unit is None:
            return price_unknown()
        ref_cost = self.per_ref * max(0, refs)
        low = unit[0] * count + ref_cost
        high = unit[1] * count + ref_cost
        if unit[0] == unit[1]:
            return price_usd(low, approximate=self.price_approximate)
        return price_range(low, high, approximate=self.price_approximate)

    def price_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "per_image": dict(self.per_image),
            "per_ref": self.per_ref,
            "approximate": self.price_approximate,
            "source": self.price_source,
        }
        if self.per_image_max:
            wire["per_image_max"] = dict(self.per_image_max)
        if self.edit_per_image:
            wire["edit_per_image"] = dict(self.edit_per_image)
        return wire


#: Output tokens one Gemini 3.x image costs, per tier (approximate; Google's
#: image-token tables for the Gemini 3 image family).
GEMINI_FLASH_TOKENS = {"1K": 1120, "2K": 1680, "4K": 2520}
GEMINI_PRO_TOKENS = {"1K": 1120, "2K": 1120, "4K": 2000}
#: gpt-image style output tokens of a 1024x1024 high-quality image (upper bound).
GPT_IMAGE_HIGH_1K_TOKENS = 4160

_OPENROUTER_SOURCE = (
    "OpenRouter catalog 2026-10-29, token-based estimate: image_output price per token "
    "x approximate output tokens per image ({detail}); input image tokens not included"
)


def _per_tier(tokens: dict[str, int], usd_per_token: float) -> dict[str, float]:
    return {tier: round(count * usd_per_token, 4) for tier, count in tokens.items()}


REGISTRY: tuple[ModelEntry, ...] = (
    ModelEntry(
        id="openrouter/nano-banana-2",
        display="Nano Banana 2",
        provider="openrouter",
        api_model="google/gemini-3.1-flash-image",
        aliases=("google/gemini-3.1-flash-image-preview",),
        family="gemini-image",
        max_images=14,
        ratios=GEMINI_RATIOS,
        tiers=("1K", "2K", "4K"),
        n_max=1,
        per_image=_per_tier(GEMINI_FLASH_TOKENS, 0.00006),
        per_ref=0.0,
        price_source=_OPENROUTER_SOURCE.format(
            detail="$0.00006/token x 1120 / 1680 / 2520 tokens at 1K / 2K / 4K"
        ),
    ),
    ModelEntry(
        id="openrouter/nano-banana-2.1",
        display="Nano Banana 2.1",
        provider="openrouter",
        api_model="google/gemini-nano-banana-2.1",
        family="gemini-image",
        max_images=14,
        ratios=GEMINI_RATIOS,
        tiers=("1K", "2K", "4K"),
        n_max=1,
        per_image=_per_tier(GEMINI_FLASH_TOKENS, 0.00003),
        per_ref=0.0,
        price_source=_OPENROUTER_SOURCE.format(
            detail="$0.00003/token x 1120 / 1680 / 2520 tokens at 1K / 2K / 4K, token "
            "counts assumed equal to Nano Banana 2"
        ),
    ),
    ModelEntry(
        id="openrouter/nano-banana-pro",
        display="Nano Banana Pro",
        provider="openrouter",
        api_model="google/gemini-3-pro-image",
        aliases=("google/gemini-3-pro-image-preview",),
        family="gemini-image",
        max_images=14,
        ratios=GEMINI_PRO_RATIOS,
        tiers=("1K", "2K", "4K"),
        n_max=1,
        per_image=_per_tier(GEMINI_PRO_TOKENS, 0.00012),
        per_ref=0.0,
        price_source=_OPENROUTER_SOURCE.format(
            detail="$0.00012/token x 1120 / 1120 / 2000 tokens at 1K / 2K / 4K"
        ),
    ),
    ModelEntry(
        id="openrouter/gpt-5-image-mini",
        display="GPT-5 Image Mini",
        provider="openrouter",
        api_model="openai/gpt-5-image-mini",
        family="gpt-image",
        # Whether OpenRouter honours image_config for this model is unverified;
        # only the three ratios OpenAI image models render natively are offered.
        max_images=16,
        ratios=("1:1", "3:2", "2:3"),
        n_max=1,
        per_image={"default": round(GPT_IMAGE_HIGH_1K_TOKENS * 0.000008, 4)},
        per_ref=0.0,
        price_source=_OPENROUTER_SOURCE.format(
            detail="$0.000008/token x about 4160 tokens, a high-quality 1024x1024 image; "
            "an upper bound, lower qualities cost less; max_images unverified"
        ),
    ),
    ModelEntry(
        id="openai/gpt-image-1",
        display="GPT Image",
        provider="openai",
        api_model="gpt-image-1",
        family="gpt-image",
        mask=False,
        max_images=16,
        sizes=GPT_IMAGE_SIZES,
        qualities=GPT_IMAGE_QUALITIES,
        transparent_bg=True,
        n_max=8,
        per_image={
            "low:1024x1024": 0.011,
            "low:1536x1024": 0.016,
            "low:1024x1536": 0.016,
            "medium:1024x1024": 0.042,
            "medium:1536x1024": 0.063,
            "medium:1024x1536": 0.063,
            "high:1024x1024": 0.167,
            "high:1536x1024": 0.25,
            "high:1024x1536": 0.25,
        },
        # Input images bill as image input tokens; about a cent each, rounded up.
        per_ref=0.01,
        price_source=(
            "OpenAI gpt-image-1 published per-image output prices (2025), direct OpenAI "
            "API (needs OPENAI_API_KEY); per_ref is a rounded-up guess for image input "
            "tokens; unverified"
        ),
    ),
)


AuthProbe = Callable[[str], tuple[bool, str | None]]


def no_provider_auth(provider_id: str) -> tuple[bool, str | None]:
    """Default probe when nothing is wired: no provider has a key."""
    return False, f"No {provider_id} image provider is configured"


class ModelRegistry:
    """The registry plus live availability and the ``auto`` default."""

    def __init__(
        self,
        entries: tuple[ModelEntry, ...] | Callable[[], tuple[ModelEntry, ...]] = REGISTRY,
        *,
        auth: AuthProbe = no_provider_auth,
        primary: Callable[[], str | None] | None = None,
    ) -> None:
        # A callable is re-read on every access: the Surplus part of the
        # registry follows its catalog cache without rebuilding the registry.
        self._source = entries
        self._auth = auth
        self._primary = primary or (lambda: None)

    @property
    def entries(self) -> tuple[ModelEntry, ...]:
        source = self._source
        return tuple(source()) if callable(source) else source

    @property
    def _entries(self) -> tuple[ModelEntry, ...]:
        return self.entries

    @property
    def _by_id(self) -> dict[str, ModelEntry]:
        return {entry.id: entry for entry in self.entries}

    def get(self, model_id: str) -> ModelEntry | None:
        return self._by_id.get(model_id)

    def availability(self, entry: ModelEntry) -> tuple[bool, str | None]:
        return self._auth(entry.provider)

    def _match_primary(self, raw: str | None) -> ModelEntry | None:
        if not raw:
            return None
        if raw in self._by_id:
            return self._by_id[raw]
        for entry in self._entries:
            names = (entry.api_model, *entry.aliases)
            if any(raw == f"{entry.provider}/{name}" for name in names):
                return entry
        return None

    def default_id(self) -> str | None:
        """``[image_generation].primary`` when it is an available entry, else
        the first available entry, else ``None``."""
        primary = self._match_primary(self._primary())
        if primary is not None and self.availability(primary)[0]:
            return primary.id
        for entry in self._entries:
            if entry.status != "deprecated" and self.availability(entry)[0]:
                return entry.id
        return None

    def resolve(self, value: Any) -> ModelEntry | None:
        """The entry a node's ``model`` value names (``auto`` → the default)."""
        if value in (None, "", "auto"):
            default = self.default_id()
            return self._by_id.get(default) if default else None
        return self._by_id.get(str(value))

    def to_wire(self) -> dict[str, Any]:
        models = []
        for entry in self._entries:
            available, reason = self.availability(entry)
            models.append(
                {
                    "id": entry.id,
                    "display": entry.display,
                    "provider": entry.provider,
                    "family": entry.family,
                    "status": entry.status,
                    "available": available,
                    "unavailable_reason": None if available else reason,
                    "ops": {"t2i": entry.t2i, "edit": entry.edit, "mask": entry.mask},
                    "max_images": entry.max_images,
                    "output": {
                        "ratios": list(entry.ratios),
                        "tiers": list(entry.tiers),
                        "sizes": list(entry.sizes),
                        "qualities": list(entry.qualities),
                        "transparent_bg": entry.transparent_bg,
                        "n_max": entry.n_max,
                    },
                    "seed_honored": entry.seed_honored,
                    "price": entry.price_wire(),
                }
            )
        return {"default": self.default_id(), "models": models}
