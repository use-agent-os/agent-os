"""RPC handlers for the tools domain."""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from agentos.gateway.access import CONTROL_ONLY
from agentos.gateway.rpc import RpcContext, get_dispatcher
from agentos.tools.builtin.web import (
    get_active_provider,
    run_web_search_payload,
    search_runtime_status,
)
from agentos.tools.registry import get_default_registry
from agentos.tools.rpc_payload import (
    tools_catalog_payload,
    tools_effective_payload,
)

_d = get_dispatcher()


@_d.method("tools.catalog")
async def _handle_tools_catalog(params: dict | None, ctx: RpcContext) -> dict:
    tool_registry = getattr(ctx, "tool_registry", None) or get_default_registry()
    return await tools_catalog_payload(
        params,
        tool_registry=tool_registry,
        session_manager=getattr(ctx, "session_manager", None),
        task_runtime=getattr(ctx, "task_runtime", None),
        scheduler=getattr(ctx, "cron_scheduler", None),
        gateway_config=getattr(ctx, "config", None),
        channel_manager=getattr(ctx, "channel_manager", None),
        originating_envelope=getattr(ctx, "originating_envelope", None),
    )


@_d.method("tools.effective")
async def _handle_tools_effective(params: dict | None, ctx: RpcContext) -> dict:
    tool_registry = getattr(ctx, "tool_registry", None) or get_default_registry()
    return await tools_effective_payload(
        params,
        tool_registry=tool_registry,
        session_manager=getattr(ctx, "session_manager", None),
        task_runtime=getattr(ctx, "task_runtime", None),
        scheduler=getattr(ctx, "cron_scheduler", None),
        gateway_config=getattr(ctx, "config", None),
        channel_manager=getattr(ctx, "channel_manager", None),
        originating_envelope=getattr(ctx, "originating_envelope", None),
    )


@_d.method("tools.search_provider")
async def _handle_tools_search_provider(params: dict | None, ctx: RpcContext) -> dict:
    return {"provider": get_active_provider()}


def _active_llm_provider(ctx: RpcContext) -> str | None:
    selector = getattr(ctx, "provider_selector", None)
    current_config = getattr(selector, "current_config", None)
    provider = getattr(current_config, "provider", None)
    if provider:
        return str(provider)
    llm_cfg = getattr(getattr(ctx, "config", None), "llm", None)
    provider = getattr(llm_cfg, "provider", None)
    return str(provider) if provider else None


def _provider_api_key_env(provider_id: str, default_env_key: str, ctx: RpcContext) -> str:
    active = provider_id == _active_llm_provider(ctx)
    llm_cfg = getattr(getattr(ctx, "config", None), "llm", None)
    if active:
        configured_env = str(getattr(llm_cfg, "api_key_env", "") or "")
        if configured_env:
            return configured_env
    return default_env_key


def _provider_key_configured(provider_id: str, env_key: str, ctx: RpcContext) -> bool:
    active = provider_id == _active_llm_provider(ctx)
    llm_cfg = getattr(getattr(ctx, "config", None), "llm", None)
    if active and bool(getattr(llm_cfg, "api_key", "")):
        return True
    return bool(env_key and os.environ.get(env_key))


def _provider_base_url(provider_id: str, default_base_url: str, ctx: RpcContext) -> str:
    active = provider_id == _active_llm_provider(ctx)
    llm_cfg = getattr(getattr(ctx, "config", None), "llm", None)
    configured_base_url = getattr(llm_cfg, "base_url", None)
    if active and configured_base_url:
        return str(configured_base_url)
    return default_base_url


def _circuit_breaker_row(provider_id: str, ctx: RpcContext) -> dict[str, Any] | None:
    """Side-effect-free breaker snapshot for one provider (None when absent)."""
    selector = getattr(ctx, "provider_selector", None)
    status_fn = getattr(selector, "circuit_breaker_status", None)
    if status_fn is None:
        return None
    try:
        return dict(status_fn(provider_id).to_dict())
    except Exception:  # noqa: BLE001 - diagnostic surface
        return None


async def _model_probe(provider_id: str, ctx: RpcContext) -> dict[str, Any]:
    selector = getattr(ctx, "provider_selector", None)
    if selector is None:
        return {
            "attempted": True,
            "status": "unavailable",
            "count": 0,
            "error": "No provider selector configured",
        }
    try:
        rows = await selector.list_models()
        matching = [
            row
            for row in rows
            if isinstance(row, dict) and str(row.get("provider") or "") == provider_id
        ]
        return {"attempted": True, "status": "ok", "count": len(matching), "error": None}
    except Exception as exc:  # noqa: BLE001 - diagnostic surface
        return {"attempted": True, "status": "error", "count": 0, "error": str(exc)}


async def _probe_chat(provider: Any, model: str, timeout: float) -> str | None:
    """One 1-token turn: the only check every provider answers honestly.

    ``list_models`` is static for some providers and swallows HTTP errors for
    others, so a wrong key can still produce a plausible list. A completion
    cannot: the provider either answers or reports why not. Returns the error
    text, or None when the turn went through.
    """
    from agentos.provider.types import ChatConfig, Message

    async def _run() -> str | None:
        stream = provider.chat(
            messages=[Message(role="user", content="ping")],
            config=ChatConfig(max_tokens=1, temperature=0.0),
        )
        async for event in stream:
            kind = getattr(event, "kind", "")
            if kind == "error":
                code = str(getattr(event, "code", "") or "provider_error")
                message = str(getattr(event, "message", "") or "provider stream failed")
                return f"{code}: {message}"
            if kind == "done":
                return None
        return None

    try:
        return await asyncio.wait_for(_run(), timeout=timeout)
    except TimeoutError:
        return f"no answer within {timeout:.0f}s"
    except Exception as exc:  # noqa: BLE001 - diagnostic surface
        return str(exc)


@_d.method("providers.probe", CONTROL_ONLY)
async def _handle_providers_probe(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Try a provider with a key BEFORE it is saved: list its models, send one token.

    Params:
        providerId: catalog id (required)
        apiKey: the key to try; omitted → the configured key for that provider
        apiKeyEnv: env var holding the key (used when apiKey is omitted)
        baseUrl: override; omitted → configured / default
        model: model for the 1-token turn; omitted → the provider's default,
          else the first listed model

    Returns ``ok`` (the turn went through), ``models`` (id, name, contextWindow),
    ``model`` (the one tried), ``latencyMs`` and ``error``. The key is never
    echoed back.
    """
    from agentos.onboarding.provider_specs import list_provider_setup_specs
    from agentos.provider.selector import ProviderBuildError, build_provider

    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    provider_id = str(params.get("providerId") or "").strip()
    if not provider_id:
        raise ValueError("params.providerId is required")
    by_id = {spec.provider_id: spec for spec in list_provider_setup_specs()}
    spec = by_id.get(provider_id)
    if spec is None:
        raise ValueError(f"Unknown provider: {provider_id}")

    llm_cfg = getattr(getattr(ctx, "config", None), "llm", None)
    is_active = provider_id == _active_llm_provider(ctx)

    api_key = str(params.get("apiKey") or "").strip()
    if not api_key:
        env_name = str(params.get("apiKeyEnv") or "").strip() or _provider_api_key_env(
            provider_id, spec.env_key, ctx
        )
        if is_active:
            api_key = str(getattr(llm_cfg, "api_key", "") or "")
        if not api_key and env_name:
            api_key = os.environ.get(env_name, "")
    base_url = str(params.get("baseUrl") or "").strip() or _provider_base_url(
        provider_id, spec.default_base_url, ctx
    )
    requested_model = str(params.get("model") or "").strip()

    if spec.requires_api_key and not api_key:
        return {
            "providerId": provider_id,
            "ok": False,
            "models": [],
            "model": requested_model,
            "latencyMs": 0,
            "error": "No API key to try.",
        }

    started = time.monotonic()
    model = requested_model or spec.default_direct_model or "diagnostic-model"
    try:
        provider = build_provider(provider_id, model, api_key=api_key, base_url=base_url)
    except ProviderBuildError as exc:
        return {
            "providerId": provider_id,
            "ok": False,
            "models": [],
            "model": model,
            "latencyMs": 0,
            "error": str(exc),
        }

    models: list[dict[str, Any]] = []
    try:
        listed = await asyncio.wait_for(provider.list_models(), timeout=12.0)
        models = [
            {
                "id": m.model_id,
                "name": m.display_name or m.model_id,
                "contextWindow": m.context_window,
            }
            for m in listed
            if getattr(m, "model_id", "")
        ]
    except Exception:  # noqa: BLE001 - the chat probe below is the verdict
        models = []

    if not requested_model and not spec.default_direct_model and models:
        model = models[0]["id"]
        try:
            provider = build_provider(provider_id, model, api_key=api_key, base_url=base_url)
        except ProviderBuildError as exc:
            return {
                "providerId": provider_id,
                "ok": False,
                "models": models,
                "model": model,
                "latencyMs": int((time.monotonic() - started) * 1000),
                "error": str(exc),
            }

    error = await _probe_chat(provider, model, timeout=15.0)
    return {
        "providerId": provider_id,
        "ok": error is None,
        "models": models,
        "model": model,
        "latencyMs": int((time.monotonic() - started) * 1000),
        "error": error,
    }


@_d.method("providers.status")
async def _handle_providers_status(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    from agentos.onboarding.provider_specs import list_provider_setup_specs
    from agentos.provider.selector import ProviderBuildError, build_provider

    if params is not None and not isinstance(params, dict):
        raise ValueError("params must be an object")
    provider_filter = (params or {}).get("provider")
    probe_models = bool((params or {}).get("probeModels", False))

    specs = list_provider_setup_specs()
    by_id = {spec.provider_id: spec for spec in specs}
    if provider_filter:
        provider_filter = str(provider_filter)
        if provider_filter not in by_id:
            raise ValueError(f"Unknown provider: {provider_filter}")
        specs = [by_id[provider_filter]]

    active = _active_llm_provider(ctx)
    llm_cfg = getattr(getattr(ctx, "config", None), "llm", None)
    rows: list[dict[str, Any]] = []
    for spec in specs:
        is_active = spec.provider_id == active
        api_key_env = _provider_api_key_env(spec.provider_id, spec.env_key, ctx)
        api_key_configured = _provider_key_configured(spec.provider_id, api_key_env, ctx)
        base_url = _provider_base_url(spec.provider_id, spec.default_base_url, ctx)
        base_url_configured = bool(base_url)
        configured = (
            spec.runtime_supported
            and (not spec.requires_api_key or api_key_configured)
            and (not spec.requires_base_url or base_url_configured)
        )
        model = str(getattr(llm_cfg, "model", "") or "") if is_active else ""
        api_key = str(getattr(llm_cfg, "api_key", "") or "") if is_active else ""
        if is_active and not api_key and api_key_env:
            api_key = os.environ.get(api_key_env, "")
        error: str | None = None
        buildable = False
        try:
            build_provider(
                spec.provider_id,
                model or "diagnostic-model",
                api_key=api_key,
                base_url=base_url,
            )
            buildable = True
        except ProviderBuildError as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 - diagnostic surface
            error = str(exc)
        probe = (
            await _model_probe(spec.provider_id, ctx)
            if probe_models and is_active
            else {"attempted": False, "status": "skipped", "count": 0, "error": None}
        )
        rows.append(
            {
                "providerId": spec.provider_id,
                "active": is_active,
                "configured": configured,
                "buildable": buildable,
                "model": model,
                "requiresApiKey": spec.requires_api_key,
                "apiKeyEnv": api_key_env,
                "apiKeyConfigured": api_key_configured,
                "baseUrlConfigured": base_url_configured,
                "error": error,
                "modelProbe": probe,
                "circuitBreaker": _circuit_breaker_row(spec.provider_id, ctx)
                if is_active
                else None,
            }
        )
    return {"activeProvider": active, "providers": rows, "count": len(rows)}


@_d.method("search.status")
async def _handle_search_status(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if params is not None and not isinstance(params, dict):
        raise ValueError("params must be an object")
    provider = (params or {}).get("provider")
    return search_runtime_status(str(provider) if provider else None)


def _query_limit(params: dict[str, Any]) -> int | None:
    if "limit" not in params or params.get("limit") is None:
        return None
    try:
        limit = int(params["limit"])
    except (TypeError, ValueError) as exc:
        raise ValueError("params.limit must be an integer") from exc
    if limit < 1 or limit > 20:
        raise ValueError("params.limit must be between 1 and 20")
    return limit


@_d.method("search.query")
async def _handle_search_query(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    query = str(params.get("query") or "").strip()
    if not query:
        raise ValueError("params.query is required")
    provider = params.get("provider")
    provider_name = str(provider) if provider else None
    if provider_name:
        search_runtime_status(provider_name)
    payload = await run_web_search_payload(
        query,
        _query_limit(params),
        provider_name=provider_name,
    )
    error = payload.get("error")
    if payload.get("ok", False):
        result = {
            "ok": True,
            "query": payload.get("query", query),
            "provider": payload.get("provider", provider_name or get_active_provider()),
            "results": payload.get("results", []),
        }
        if payload.get("fallbackFrom"):
            result["fallbackFrom"] = payload.get("fallbackFrom")
        if payload.get("attempts") is not None:
            result["attempts"] = payload.get("attempts")
        return result
    if not isinstance(error, dict):
        error = {
            "kind": payload.get("error_kind", "unknown"),
            "class": payload.get("error_class", ""),
            "message": str(payload.get("error") or ""),
            "retryable": False,
        }
    result = {
        "ok": False,
        "query": payload.get("query", query),
        "provider": payload.get("provider", provider_name or get_active_provider()),
        "results": payload.get("results", []),
        "error": error,
    }
    if payload.get("attempts") is not None:
        result["attempts"] = payload.get("attempts")
    return result
