"""RPC handlers for the models domain."""

from __future__ import annotations

from typing import Any

from agentos.gateway.access import CONTROL_AND_CHANNEL
from agentos.gateway.rpc import RpcContext, get_dispatcher

_d = get_dispatcher()


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
    pinnable.
    """

    models: list[dict[str, Any]] = []
    catalog = ctx.model_catalog or getattr(ctx.turn_runner, "_model_catalog", None)
    if catalog is not None:
        try:
            models = [_model_info_to_wire(m.model_dump()) for m in catalog.list_models()]
        except Exception:
            pass

    catalog_is_canonical = bool(models) and active_provider_id(ctx).lower() in {
        "opencap",
        "surplus",
    }
    if ctx.provider_selector is not None and not catalog_is_canonical:
        try:
            raw = await ctx.provider_selector.list_models()
            if raw:
                by_provider_model = {(m["provider"], m["id"]): m for m in models}
                for model in (_model_info_to_wire(m) for m in raw):
                    by_provider_model[(model["provider"], model["id"])] = model
                models = list(by_provider_model.values())
        except Exception:
            pass

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
