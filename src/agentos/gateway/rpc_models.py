"""RPC handlers for the models domain."""

from __future__ import annotations

from typing import Any

from agentos.gateway.rpc import RpcContext, get_dispatcher

_d = get_dispatcher()


def _model_info_to_wire(m: dict[str, Any]) -> dict[str, Any]:
    """Convert a ModelInfo.model_dump() dict to the RPC wire format."""
    capabilities: list[str] = ["chat"]
    if m.get("supports_tools"):
        capabilities.append("tools")
    # Providers can signal vision support via extra fields; keep extensible
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


@_d.method("models.list", scope="operator.read")
async def _handle_models_list(params: dict | None, ctx: RpcContext) -> list[dict[str, Any]]:
    provider_filter = (params or {}).get("provider")
    capabilities_filter: list[str] | None = (params or {}).get("capabilities")

    models: list[dict[str, Any]] = []
    catalog = ctx.model_catalog or getattr(ctx.turn_runner, "_model_catalog", None)
    if catalog is not None:
        try:
            models = [_model_info_to_wire(m.model_dump()) for m in catalog.list_models()]
        except Exception:
            pass

    active_provider = str(getattr(getattr(ctx.config, "llm", None), "provider", "") or "")
    catalog_is_canonical = bool(models) and active_provider.strip().lower() == "opencap"
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
