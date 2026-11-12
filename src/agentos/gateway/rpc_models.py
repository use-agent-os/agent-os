"""RPC handlers for the models domain."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import structlog

from agentos.gateway.access import CONTROL_AND_CHANNEL
from agentos.gateway.rpc import RpcContext, get_dispatcher
from agentos.provider.model_catalog import ModelCatalog
from agentos.provider.types import ModelInfo

_d = get_dispatcher()
log = structlog.get_logger(__name__)

# Providers whose model catalog is a public, unauthenticated document. The
# runtime catalog holds only the ACTIVE provider's copy (fetched at boot), so
# these are fetched on demand for every other case, and kept for a while:
# Surplus' catalog is ~470 KB / 500+ models and the Settings pages ask for it
# several times per visit (Providers form, Router text + vision menus).
PUBLIC_CATALOG_PROVIDERS = frozenset({"opencap", "surplus"})
PUBLIC_CATALOG_TTL_S = 300.0
PUBLIC_CATALOG_TIMEOUT_S = 20.0

_monotonic = time.monotonic


@dataclass(frozen=True)
class _CatalogSnapshot:
    models: tuple[ModelInfo, ...]
    fetched_at: float


_public_snapshots: dict[str, _CatalogSnapshot] = {}
_public_inflight: dict[str, asyncio.Task[_CatalogSnapshot | None]] = {}


def reset_public_catalog_cache() -> None:
    """Forget every cached public catalog (tests)."""

    _public_snapshots.clear()
    _public_inflight.clear()


async def _fetch_public_catalog(provider_id: str, proxy: str) -> _CatalogSnapshot | None:
    fresh = ModelCatalog()
    fetch = fresh.fetch_surplus if provider_id == "surplus" else fresh.fetch_opencap
    try:
        await asyncio.wait_for(fetch(proxy), timeout=PUBLIC_CATALOG_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - stale snapshot / empty list is the fallback
        log.warning("models.public_catalog_failed", provider=provider_id, error=str(exc))
        return None
    models = tuple(fresh.list_models(provider_id))
    log.info("models.public_catalog_fetched", provider=provider_id, count=len(models))
    return _CatalogSnapshot(models=models, fetched_at=_monotonic())


async def public_catalog_models(provider_id: str, *, proxy: str = "") -> list[ModelInfo]:
    """The provider's public catalog, cached for ``PUBLIC_CATALOG_TTL_S``.

    Concurrent callers share one upstream request. A failed refresh keeps
    serving the previous snapshot rather than emptying the menus.
    """

    cached = _public_snapshots.get(provider_id)
    if cached is not None and _monotonic() - cached.fetched_at < PUBLIC_CATALOG_TTL_S:
        return list(cached.models)

    task = _public_inflight.get(provider_id)
    if task is None or task.get_loop() is not asyncio.get_running_loop():
        task = asyncio.ensure_future(_fetch_public_catalog(provider_id, proxy))
        _public_inflight[provider_id] = task
    try:
        snapshot = await asyncio.shield(task)
    finally:
        if task.done() and _public_inflight.get(provider_id) is task:
            del _public_inflight[provider_id]

    if snapshot is not None and snapshot.models:
        _public_snapshots[provider_id] = snapshot
        return list(snapshot.models)
    return list(cached.models) if cached is not None else []


def _model_info_to_wire(m: dict[str, Any]) -> dict[str, Any]:
    """Convert a ModelInfo.model_dump() dict to the RPC wire format."""
    capabilities: list[str] = ["chat"]
    if m.get("supports_tools"):
        capabilities.append("tools")
    if m.get("supports_vision"):
        capabilities.append("vision")
    if m.get("supports_reasoning"):
        capabilities.append("reasoning")
    return {
        "id": m.get("model_id", ""),
        "name": m.get("display_name") or m.get("model_id", ""),
        "provider": m.get("provider", ""),
        "contextWindow": m.get("context_window", 0),
        "capabilities": capabilities,
        "pricing": {
            "inputPer1k": m.get("input_cost_per_1k", 0.0),
            "outputPer1k": m.get("output_cost_per_1k", 0.0),
        },
    }


def active_provider_id(ctx: RpcContext) -> str:
    """The single provider every turn runs through (``llm.provider``).

    The runtime never builds a per-tier or per-model provider client, so this is
    the only provider whose models can actually be reached — see
    ``_degrade_model_for_local_provider`` in the router step.
    """

    return str(getattr(getattr(ctx.config, "llm", None), "provider", "") or "").strip()


def _llm_proxy(ctx: RpcContext) -> str:
    return str(getattr(getattr(ctx.config, "llm", None), "proxy", "") or "").strip()


def _runtime_catalog(ctx: RpcContext) -> Any:
    # ``is not None``, not ``or``: an empty catalog (boot fetch failed) has
    # ``len() == 0`` and is falsy, and it is exactly the one that needs healing.
    if ctx.model_catalog is not None:
        return ctx.model_catalog
    return getattr(ctx.turn_runner, "_model_catalog", None)


def _catalog_wire(catalog: Any) -> list[dict[str, Any]]:
    if catalog is None:
        return []
    try:
        return [_model_info_to_wire(m.model_dump()) for m in catalog.list_models()]
    except Exception:
        return []


async def _public_provider_models(
    ctx: RpcContext, provider_id: str, catalog: Any
) -> list[dict[str, Any]]:
    """Models of a public-catalog provider (OpenCAP, Surplus), never empty by accident.

    The runtime catalog is used when it already holds this provider's models.
    Otherwise — the provider is not the active one, it was switched to at
    runtime (the selector hot-swaps, the boot catalog does not), or the boot
    fetch failed — the public catalog is fetched (cached). When the provider
    IS the active one, the runtime catalog adopts that copy, so turns resolve
    windows and capabilities against the provider they actually run on.
    """

    held = [m for m in _catalog_wire(catalog) if m["provider"] == provider_id]
    if held:
        return held

    infos = await public_catalog_models(provider_id, proxy=_llm_proxy(ctx))
    if infos and provider_id == active_provider_id(ctx).lower() and hasattr(catalog, "replace"):
        catalog.replace(infos)
        log.info("models.runtime_catalog_replaced", provider=provider_id, count=len(infos))
    return [_model_info_to_wire(m.model_dump()) for m in infos]


async def _selector_models(ctx: RpcContext, models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge the active provider chain's authenticated ``/models`` listing over ``models``."""

    if ctx.provider_selector is None:
        return models
    try:
        raw = await ctx.provider_selector.list_models()
    except Exception:
        return models
    if not raw:
        return models
    by_provider_model = {(m["provider"], m["id"]): m for m in models}
    for model in (_model_info_to_wire(m) for m in raw):
        by_provider_model[(model["provider"], model["id"])] = model
    return list(by_provider_model.values())


async def collect_models(
    ctx: RpcContext,
    *,
    provider_filter: str | None = None,
    capabilities_filter: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Gather the model catalog in wire format.

    Shared with ``router.hold.set``, which validates a user-named model against
    it before pinning: a typo'd id would otherwise install a hold that fails
    every subsequent turn at the provider.

    Note on OpenCAP and Surplus: their inference endpoints also serve namespaced
    ``<upstream>/<model>`` aliases, but the PUBLIC catalogs this reads publish
    only the bare canonical ids, so those aliases never appear here and are not
    pinnable. Their public catalog is canonical, so the authenticated
    ``/models`` listing (no vision flags) is only a last resort for them.
    """

    catalog = _runtime_catalog(ctx)
    active = active_provider_id(ctx).lower()
    target = str(provider_filter or active).strip().lower()

    if target in PUBLIC_CATALOG_PROVIDERS:
        models = await _public_provider_models(ctx, target, catalog)
        if not models and target == active:
            models = await _selector_models(ctx, [])
    else:
        models = await _selector_models(ctx, _catalog_wire(catalog))

    if provider_filter:
        models = [m for m in models if m["provider"] == provider_filter]

    if capabilities_filter:
        required = set(capabilities_filter)
        models = [m for m in models if required.issubset(set(m["capabilities"]))]

    return models


@_d.method("models.list", CONTROL_AND_CHANNEL)
async def _handle_models_list(params: dict | None, ctx: RpcContext) -> list[dict[str, Any]]:
    return await collect_models(
        ctx,
        provider_filter=(params or {}).get("provider"),
        capabilities_filter=(params or {}).get("capabilities"),
    )
