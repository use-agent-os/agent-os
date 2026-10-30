"""Surplus Intelligence image models: catalog → Image-node registry entries.

Surplus serves text-to-image and edit as separate model ids (``venice-nano-
banana-2`` / ``nano-banana-2-edit``). Each :data:`FAMILIES` row pairs them into
one registry entry; the executor sends the edit id when images are connected.

Capabilities and per-image prices come from the public catalog
(``GET /v1/models``). :class:`SurplusCatalog` keeps a copy under
``<state_dir>/flows/surplus_catalog.json`` refreshed every
:data:`CATALOG_TTL_S`; with no usable copy it falls back to the bundled
snapshot in :mod:`agentos.flows.surplus_snapshot`. The catalog does not say
how many images an edit accepts, so ``max_images`` is a per-family figure.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import structlog

from agentos.flows.models import GEMINI_FLASH_TOKENS, ModelEntry
from agentos.flows.surplus_snapshot import MODELS as SNAPSHOT_MODELS
from agentos.flows.surplus_snapshot import SNAPSHOT_DATE

log = structlog.get_logger(__name__)

SURPLUS_BASE_URL = "https://api.surplusintelligence.ai/v1"
CATALOG_TTL_S = 6 * 3600
CATALOG_FILENAME = "surplus_catalog.json"
FETCH_TIMEOUT_S = 15.0
QUALITY_ORDER = ("high", "medium", "low")


@dataclass(frozen=True)
class Family:
    id: str
    display: str
    t2i: str
    edit: str | None
    family: str
    #: Images one edit call accepts (not in the catalog; family limit, unverified).
    max_images: int
    #: Output tokens per tier, for a token-metered text-to-image price.
    tokens: dict[str, int] | None = None
    #: Live-verified corrections to what the catalog says a model honours:
    #: ``(param, honoured)`` for ``aspect_ratio | resolution | quality | size``.
    overrides: tuple[tuple[str, bool], ...] = ()

    def honours(self, model: dict[str, Any], param: str) -> bool:
        """Whether *model* honours request field *param*.

        An override wins; otherwise the catalog decides: the field is named in
        ``supported_parameters`` or (for the image extras, which the catalog
        lists only there) in ``supported_features``.
        """
        for name, honoured in self.overrides:
            if name == param:
                return honoured
        params = {str(p) for p in model.get("supported_parameters") or []}
        return param in params or param in _features(model)


FAMILIES: tuple[Family, ...] = (
    Family(
        "surplus/nano-banana-2",
        "Nano Banana 2",
        "venice-nano-banana-2",
        "nano-banana-2-edit",
        "gemini-image",
        14,
        GEMINI_FLASH_TOKENS,
    ),
    Family(
        "surplus/nano-banana-2.1",
        "Nano Banana 2.1",
        "venice-nano-banana-2-1",
        "nano-banana-2-1-edit",
        "gemini-image",
        14,
        GEMINI_FLASH_TOKENS,
    ),
    Family(
        "surplus/nano-banana-pro",
        "Nano Banana Pro",
        "venice-nano-banana-pro",
        "nano-banana-pro-edit",
        "gemini-image",
        14,
    ),
    Family(
        "surplus/gpt-image-2",
        "GPT Image 2",
        "venice-gpt-image-2",
        "gpt-image-2-edit",
        "gpt-image",
        16,
        # Live 2026-10-29: "venice-gpt-image-2 cannot honor an explicit resolution
        # tier" although the catalog lists resolutions.
        overrides=(("resolution", False),),
    ),
    Family(
        "surplus/seedream-v5-lite",
        "Seedream 5 Lite",
        "venice-seedream-v5-lite",
        "seedream-v5-lite-edit",
        "seedream",
        10,
    ),
    # No ``flux-2-pro`` edit model in the catalog: text-to-image only.
    Family(
        "surplus/flux-2-pro",
        "FLUX.2 Pro",
        "venice-flux-2-pro",
        None,
        "flux",
        0,
        # Live 2026-10-29: aspect_ratio 1:1 and size 1536x1024 both came back
        # 1024x768. The provider fixes the output, so neither is offered.
        overrides=(("aspect_ratio", False), ("size", False)),
    ),
    Family(
        "surplus/qwen-image-2",
        "Qwen Image 2",
        "venice-qwen-image-2",
        "qwen-image-2-edit",
        "qwen-image",
        3,
    ),
)

#: Every catalog id the families read (what a cached copy keeps).
FAMILY_MODEL_IDS = frozenset(
    model_id for fam in FAMILIES for model_id in (fam.t2i, fam.edit) if model_id
)


def _features(model: dict[str, Any]) -> set[str]:
    return {str(f) for f in model.get("supported_features") or []}


def _strings(raw: Any) -> tuple[str, ...]:
    return tuple(str(item) for item in raw or [] if isinstance(item, str) and item)


def _per_image_price(model: dict[str, Any]) -> float | None:
    pricing = model.get("pricing") or {}
    if model.get("metering") == "per_token" or pricing.get("media_unit") not in (None, "image"):
        return None
    try:
        value = float(str(pricing.get("image")))
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _completion_price(model: dict[str, Any]) -> float | None:
    try:
        value = float(str((model.get("pricing") or {}).get("completion")))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def entry_for(family: Family, by_id: dict[str, dict[str, Any]], *, as_of: str) -> ModelEntry | None:
    """The registry entry for *family*, or ``None`` when its t2i model is not listed."""
    t2i = by_id.get(family.t2i)
    if t2i is None:
        return None
    edit = by_id.get(family.edit) if family.edit else None
    ratios = tuple(r for r in _strings(t2i.get("aspect_ratios")) if r != "auto")
    ratios = ratios if family.honours(t2i, "aspect_ratio") else ()
    tiers = _strings(t2i.get("resolutions")) if family.honours(t2i, "resolution") else ()
    qualities_raw = _strings(t2i.get("qualities")) if family.honours(t2i, "quality") else ()
    qualities = tuple(q for q in QUALITY_ORDER if q in qualities_raw) + tuple(
        q for q in qualities_raw if q not in QUALITY_ORDER
    )
    # A model that honours aspect_ratio gets ratios; one that does not gets ``size``.
    sizes = _strings(t2i.get("sizes")) if not ratios and family.honours(t2i, "size") else ()

    edit_price = _per_image_price(edit) if edit is not None else None
    t2i_price = _per_image_price(t2i)
    notes: list[str] = []
    per_image: dict[str, float] = {}
    per_image_max: dict[str, float] | None = None
    if t2i_price is not None:
        per_image = {"default": t2i_price}
        notes.append(f"{family.t2i} ${t2i_price:g}/image")
    else:
        completion = _completion_price(t2i)
        if completion is not None and family.tokens:
            per_image = {
                tier: round(count * completion, 4)
                for tier, count in family.tokens.items()
                if not tiers or tier in tiers
            }
            notes.append(
                f"{family.t2i} is token-metered (${completion:g}/output token x approximate "
                "tokens per tier)"
            )
            if edit_price is not None:
                per_image_max = {"default": edit_price}
                notes.append(f"upper bound: the edit model's ${edit_price:g}/image")
    edit_per_image = None
    if edit_price is not None:
        if edit_price != t2i_price:
            edit_per_image = {"default": edit_price}
        notes.append(f"{family.edit} ${edit_price:g}/image")
    has_edit = edit is not None and edit_price is not None
    multi = edit is not None and "multi-image" in _features(edit)
    max_images = (family.max_images if multi else 1) if has_edit else 0
    if has_edit:
        notes.append(f"max_images {max_images} is a family limit, not in the catalog; unverified")
    edit_ratios = (
        _strings(edit.get("aspect_ratios"))
        if edit is not None and family.honours(edit, "aspect_ratio")
        else ()
    )
    edit_tiers = (
        _strings(edit.get("resolutions"))
        if edit is not None and family.honours(edit, "resolution")
        else ()
    )
    return ModelEntry(
        id=family.id,
        display=family.display,
        provider="surplus",
        api_model=family.t2i,
        family=family.family,
        t2i=True,
        edit=has_edit,
        max_images=max_images,
        ratios=ratios,
        tiers=tiers,
        sizes=sizes,
        qualities=qualities,
        # ``n`` is listed for every model but the edit route rejects it: fan out.
        n_max=1,
        # A live edit took 46 s and returned an 8 MB PNG.
        timeout_s=300.0,
        per_image=per_image,
        per_ref=0.0,
        per_image_max=per_image_max,
        edit_api_model=family.edit if has_edit else None,
        edit_per_image=edit_per_image,
        edit_ratios=edit_ratios if has_edit else None,
        edit_tiers=edit_tiers if has_edit else None,
        price_source=f"Surplus catalog {as_of}: " + "; ".join(notes),
    )


def entries_from_models(models: Iterable[dict[str, Any]], *, as_of: str) -> tuple[ModelEntry, ...]:
    by_id = {str(m.get("id")): m for m in models if isinstance(m, dict) and m.get("id")}
    entries = (entry_for(family, by_id, as_of=as_of) for family in FAMILIES)
    return tuple(entry for entry in entries if entry is not None)


SNAPSHOT_ENTRIES = entries_from_models(SNAPSHOT_MODELS, as_of=f"{SNAPSHOT_DATE} (bundled snapshot)")


LIVE_CATALOG_ENV = "AGENTOS_SURPLUS_LIVE_PRICING"


def live_catalog_enabled() -> bool:
    raw = os.environ.get(LIVE_CATALOG_ENV, "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


class SurplusCatalog:
    """The Surplus image entries, from a cached live catalog or the bundled snapshot."""

    def __init__(
        self,
        cache_path: Path,
        *,
        base_url: Callable[[], str] | None = None,
        clock: Callable[[], float] = time.time,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self.cache_path = Path(cache_path)
        self.base_url: Callable[[], str] = base_url or (lambda: SURPLUS_BASE_URL)
        self._clock = clock
        self._client_factory = client_factory
        self._memo: tuple[float, tuple[ModelEntry, ...]] | None = None
        self._task: asyncio.Task[bool] | None = None

    def _read_cache(self) -> dict[str, Any] | None:
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict) or not isinstance(raw.get("data"), list):
            return None
        return raw

    def fetched_at(self) -> float | None:
        cached = self._read_cache()
        value = cached.get("fetched_at") if cached else None
        return float(value) if isinstance(value, int | float) else None

    def stale(self) -> bool:
        fetched = self.fetched_at()
        return fetched is None or self._clock() - fetched > CATALOG_TTL_S

    def entries(self) -> tuple[ModelEntry, ...]:
        """Cached catalog entries (any age), else the bundled snapshot."""
        try:
            mtime = self.cache_path.stat().st_mtime
        except OSError:
            return SNAPSHOT_ENTRIES
        if self._memo is not None and self._memo[0] == mtime:
            return self._memo[1]
        cached = self._read_cache()
        entries: tuple[ModelEntry, ...] = ()
        if cached is not None:
            stamp = time.strftime("%Y-%m-%d", time.gmtime(float(cached.get("fetched_at") or 0)))
            entries = entries_from_models(cached["data"], as_of=stamp)
        if not entries:
            entries = SNAPSHOT_ENTRIES
        self._memo = (mtime, entries)
        return entries

    async def refresh(self) -> bool:
        """Fetch the live catalog and cache the family models; ``False`` on any failure."""
        url = self.base_url().rstrip("/") + "/models"
        try:
            if self._client_factory is not None:
                client = self._client_factory()
            else:
                from agentos.env import trust_env

                client = httpx.AsyncClient(timeout=FETCH_TIMEOUT_S, trust_env=trust_env())
            async with client:
                response = await client.get(url)
                response.raise_for_status()
                body = response.json()
        except Exception as exc:  # noqa: BLE001 - offline: keep whatever we had
            log.info("flows.surplus_catalog_unavailable", error=str(exc))
            return False
        models = body.get("data") if isinstance(body, dict) else None
        if not isinstance(models, list):
            log.info("flows.surplus_catalog_malformed")
            return False
        kept = [m for m in models if isinstance(m, dict) and m.get("id") in FAMILY_MODEL_IDS]
        if not entries_from_models(kept, as_of="check"):
            log.info("flows.surplus_catalog_without_families")
            return False
        payload = {"fetched_at": self._clock(), "source": url, "data": kept}
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(f".json.tmp{os.getpid()}")
        tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        os.replace(tmp, self.cache_path)
        self._memo = None
        return True

    def ensure_fresh(self) -> None:
        """Start a background refresh when the copy is stale (needs a running loop).

        ``AGENTOS_SURPLUS_LIVE_PRICING=0`` (set for the test suite) turns the
        automatic refresh off, as it does for Surplus LLM prices.
        """
        if not live_catalog_enabled():
            return
        if self._task is not None and not self._task.done():
            return
        if not self.stale():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = loop.create_task(self.refresh(), name="flows.surplus_catalog")
