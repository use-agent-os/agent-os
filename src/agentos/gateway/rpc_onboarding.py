"""RPC handlers for onboarding (catalog, status, provider/channel mutations).

Mutations are applied against the gateway's *active* in-memory config when the
RPC context provides one (``ctx.config``). The same context exposes the
running ``provider_selector``; provider mutations are mirrored into it so a
``configure`` from the WebUI takes effect on the next chat without a restart.

Channel mutations always require a restart because ``ChannelManager`` is built
once at boot.

The onboarding mutation/store modules import ``agentos.gateway.config`` at
module top level, which transitively re-enters ``agentos.gateway`` during
boot. To avoid the circular import, we import those bindings lazily inside the
handler bodies.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agentos.gateway.config_commit import (
    ConfigCommitResult,
    commit_config,
    expected_revision,
)
from agentos.gateway.rpc import RpcContext, get_dispatcher

_d = get_dispatcher()


def _active_config(ctx: RpcContext) -> Any:
    """Return the gateway's running config when available, else load from disk."""
    if ctx.config is not None:
        return ctx.config
    from agentos.onboarding.config_store import load_config

    return load_config()


def _config_path_for(ctx: RpcContext, source: Any) -> str | None:
    """Resolve the persistence path that matches ``source``.

    Prefers the path stored on the running ``GatewayConfig`` so RPCs save back
    to wherever the gateway booted from (e.g. ``./agentos.toml``) rather
    than the env-default user config.
    """
    path = getattr(source, "config_path", None)
    if path:
        return str(path)
    return None


def _onboarding_explicit_paths(new_cfg: Any) -> set[str]:
    """Every dotted path the onboarding write carries EXCEPT the fields the CLI
    runtime-override map owns (host/port/debug/auth bind posture).

    Onboarding mutations persist the whole config, like ``config.apply``. To
    avoid freezing a one-off ``--listen``/``--debug`` (or break-glass
    ``auth.mode=none``) that ``run_gateway`` injected into ``ctx.config``, we
    hand ``persist_config`` an explicit-paths set that includes everything the
    onboarding change touches but deliberately omits the runtime-override keys,
    so those restore to their on-disk originals.
    """
    from agentos.gateway.config_persist import get_runtime_overrides

    override_keys = set(get_runtime_overrides())
    toml = new_cfg.to_toml_dict() if hasattr(new_cfg, "to_toml_dict") else {}
    return _all_dotted_paths(toml) - override_keys


def _all_dotted_paths(payload: Any, prefix: str = "") -> set[str]:
    paths: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            current = f"{prefix}.{key}" if prefix else key
            paths.add(current)
            paths.update(_all_dotted_paths(value, current))
    return paths


def _persist(ctx: RpcContext, new_cfg: Any, *, restart_required: bool) -> str:
    result = commit_config(
        ctx,
        new_cfg,
        source_config=ctx.config,
        explicit_paths=_onboarding_explicit_paths(new_cfg),
        restart_reasons=("channels",) if restart_required else (),
    )
    return str(result.path)


def _commit_mutation(
    ctx: RpcContext,
    source_cfg: Any,
    new_cfg: Any,
    params: Any,
    *,
    changed_paths: set[str],
    restart_required: bool,
    restart_reason: str,
) -> ConfigCommitResult:
    reasons = (restart_reason,) if restart_required else ()
    return commit_config(
        ctx,
        new_cfg,
        source_config=source_cfg,
        explicit_paths=_onboarding_explicit_paths(new_cfg),
        changed_paths=changed_paths,
        expected=expected_revision(params),
        restart_reasons=reasons,
    )


def _status_payload(ctx: RpcContext, config: Any | None = None) -> dict[str, Any]:
    from agentos.onboarding.next_steps import env_recovery_commands
    from agentos.onboarding.status import get_onboarding_status

    cfg = config if config is not None else _active_config(ctx)
    s = get_onboarding_status(cfg)
    return {
        "configPath": _config_path_for(ctx, cfg) or s.config_path,
        "hasConfig": s.has_config,
        "llmConfigured": s.llm_configured,
        "llmSource": s.llm_source,
        "llmEnvKey": s.llm_env_key,
        "imageGenerationConfigured": s.image_generation_configured,
        "imageGenerationEnabled": s.image_generation_enabled,
        "imageGenerationSource": s.image_generation_source,
        "imageGenerationProvider": s.image_generation_provider,
        "imageGenerationPrimary": s.image_generation_primary,
        "imageGenerationEnvKey": s.image_generation_env_key,
        "audioConfigured": s.audio_configured,
        "audioEnabled": s.audio_enabled,
        "audioSource": s.audio_source,
        "audioProvider": s.audio_provider,
        "audioEnvKey": s.audio_env_key,
        "searchConfigured": s.search_configured,
        "searchProvider": s.search_provider,
        "searchSource": s.search_source,
        "searchEnvKey": s.search_env_key,
        "memoryEmbeddingConfigured": s.memory_embedding_configured,
        "memoryEmbeddingProvider": s.memory_embedding_provider,
        "memoryEmbeddingSource": s.memory_embedding_source,
        "memoryEmbeddingEnvKey": s.memory_embedding_env_key,
        "channelCount": s.channel_count,
        "channelsConfigured": s.channels_configured,
        "needsOnboarding": s.needs_onboarding,
        "sections": {name: state.value for name, state in s.sections.items()},
        "sectionDetails": s.section_details,
        "envRecoveryCommands": env_recovery_commands(s),
        "warnings": list(s.warnings),
    }


@_d.method("onboarding.status", scope="operator.read")
async def _onboarding_status(params: Any, ctx: RpcContext) -> dict[str, Any]:
    return _status_payload(ctx)


@_d.method("onboarding.catalog", scope="operator.read")
async def _onboarding_catalog(params: Any, ctx: RpcContext) -> dict[str, Any]:
    from agentos.onboarding.setup_engine import setup_catalog_payload

    return setup_catalog_payload()


def _require(params: Any, key: str) -> Any:
    if not isinstance(params, dict) or key not in params:
        raise ValueError(f"params.{key} is required")
    return params[key]


@_d.method("onboarding.provider.configure", scope="operator.admin")
async def _provider_configure(params: Any, ctx: RpcContext) -> dict[str, Any]:
    from agentos.onboarding.mutations import upsert_llm_provider

    provider_id = _require(params, "providerId")
    model = params.get("model", "") if isinstance(params, dict) else ""
    cfg = _active_config(ctx)
    res = upsert_llm_provider(
        cfg,
        provider_id=provider_id,
        model=model,
        api_key=params.get("apiKey", "") if isinstance(params, dict) else "",
        api_key_env=params.get("apiKeyEnv", "") if isinstance(params, dict) else "",
        base_url=params.get("baseUrl", "") if isinstance(params, dict) else "",
        proxy=params.get("proxy", "") if isinstance(params, dict) else "",
    )
    commit = _commit_mutation(
        ctx,
        cfg,
        res.config,
        params,
        changed_paths={"llm", "agentos_router"},
        restart_required=res.restart_required,
        restart_reason="provider",
    )
    return {
        "changed": res.changed,
        "restartRequired": commit.restart_required,
        "configPath": str(commit.path),
        "entry": res.public_payload,
        "warnings": res.warnings,
    }


@_d.method("onboarding.router.catalog", scope="operator.read")
async def _router_catalog(params: Any, ctx: RpcContext) -> dict[str, Any]:
    from agentos.onboarding.router_specs import router_catalog_payload

    return router_catalog_payload()


@_d.method("onboarding.router.configure", scope="operator.admin")
async def _router_configure(params: Any, ctx: RpcContext) -> dict[str, Any]:
    from agentos.onboarding.mutations import upsert_router

    cfg = _active_config(ctx)
    mode = params.get("mode", "recommended") if isinstance(params, dict) else "recommended"
    strategy = params.get("strategy") if isinstance(params, dict) else None
    default_tier = params.get("defaultTier") if isinstance(params, dict) else None
    tiers = params.get("tiers") if isinstance(params, dict) else None
    judge_model = params.get("judgeModel") if isinstance(params, dict) else None
    judge_provider = params.get("judgeProvider") if isinstance(params, dict) else None
    judge_base_url = params.get("judgeBaseUrl") if isinstance(params, dict) else None
    judge_api_key = params.get("judgeApiKey") if isinstance(params, dict) else None
    safety_net_threshold = (
        params.get("safetyNetThreshold") if isinstance(params, dict) else None
    )
    verify_local_endpoint = bool(judge_base_url)
    # ``upsert_router`` is synchronous, and with ``verify_local_endpoint=True`` it
    # runs a full test classification against the local judge endpoint (up to the
    # probe's ~13.5s inner timeout). Awaiting it inline would block the gateway
    # event loop — freezing every other in-flight RPC, WebSocket stream, and
    # heartbeat — for the whole probe duration when the endpoint is slow or
    # unreachable-but-accepting-TCP. Run the probing call off the event loop on a
    # worker thread so the blocking connectivity check never stalls the loop.
    if verify_local_endpoint:
        res = await asyncio.to_thread(
            upsert_router,
            cfg,
            mode=mode,
            strategy=strategy,
            default_tier=default_tier,
            tiers=tiers,
            judge_model=judge_model,
            judge_provider=judge_provider,
            judge_base_url=judge_base_url,
            judge_api_key=judge_api_key,
            safety_net_threshold=safety_net_threshold,
            verify_local_endpoint=True,
        )
    else:
        res = upsert_router(
            cfg,
            mode=mode,
            strategy=strategy,
            default_tier=default_tier,
            tiers=tiers,
            judge_model=judge_model,
            judge_provider=judge_provider,
            judge_base_url=judge_base_url,
            judge_api_key=judge_api_key,
            safety_net_threshold=safety_net_threshold,
            verify_local_endpoint=False,
        )
    commit = _commit_mutation(
        ctx,
        cfg,
        res.config,
        params,
        changed_paths={"agentos_router", "llm.model"},
        restart_required=res.restart_required,
        restart_reason="router",
    )
    return {
        "changed": res.changed,
        "restartRequired": commit.restart_required,
        "configPath": str(commit.path),
        "entry": res.public_payload,
        "warnings": res.warnings,
    }


@_d.method("onboarding.channel.probe", scope="operator.admin")
async def _channel_probe(params: Any, ctx: RpcContext) -> dict[str, Any]:
    from agentos.onboarding.mutations import upsert_channel

    entry = _require(params, "entry")
    if not isinstance(entry, dict):
        raise ValueError("params.entry must be an object")
    # Validate against the same merge semantics used by upsert. This lets an
    # existing channel keep a write-only credential when the edit form leaves
    # that secret blank, without persisting or mutating the active config.
    result = upsert_channel(_active_config(ctx), entry_payload=entry)
    return {
        "status": "ready",
        "connected": False,
        "restartRequired": True,
        "entry": result.public_payload,
        "warnings": result.warnings,
    }


@_d.method("onboarding.search.configure", scope="operator.admin")
async def _search_configure(params: Any, ctx: RpcContext) -> dict[str, Any]:
    from agentos.onboarding.mutations import upsert_search_provider

    provider_id = _require(params, "providerId")
    cfg = _active_config(ctx)
    res = upsert_search_provider(
        cfg,
        provider_id=provider_id,
        api_key=params.get("apiKey", "") if isinstance(params, dict) else "",
        api_key_env=params.get("apiKeyEnv", "") if isinstance(params, dict) else "",
        max_results=params.get("maxResults", 5) if isinstance(params, dict) else 5,
        proxy=params.get("proxy", "") if isinstance(params, dict) else "",
        use_env_proxy=(params.get("useEnvProxy", False) if isinstance(params, dict) else False),
        fallback_policy=(
            params.get("fallbackPolicy", "off") if isinstance(params, dict) else "off"
        ),
        diagnostics=params.get("diagnostics", False) if isinstance(params, dict) else False,
    )
    commit = _commit_mutation(
        ctx,
        cfg,
        res.config,
        params,
        changed_paths={
            "search_provider",
            "search_api_key",
            "search_api_key_env",
            "search_max_results",
            "search_proxy",
            "search_use_env_proxy",
            "search_fallback_policy",
            "search_diagnostics",
        },
        restart_required=res.restart_required,
        restart_reason="search",
    )
    return {
        "changed": res.changed,
        "restartRequired": commit.restart_required,
        "configPath": str(commit.path),
        "entry": res.public_payload,
        "warnings": res.warnings,
    }


@_d.method("onboarding.imageGeneration.configure", scope="operator.admin")
async def _image_generation_configure(params: Any, ctx: RpcContext) -> dict[str, Any]:
    from agentos.onboarding.mutations import upsert_image_generation_provider

    provider_id = _require(params, "providerId")
    cfg = _active_config(ctx)
    res = upsert_image_generation_provider(
        cfg,
        provider_id=provider_id,
        primary=params.get("primary", "") if isinstance(params, dict) else "",
        api_key=params.get("apiKey", "") if isinstance(params, dict) else "",
        api_key_env=params.get("apiKeyEnv", "") if isinstance(params, dict) else "",
        base_url=params.get("baseUrl", "") if isinstance(params, dict) else "",
        enabled=params.get("enabled", True) if isinstance(params, dict) else True,
    )
    commit = _commit_mutation(
        ctx,
        cfg,
        res.config,
        params,
        changed_paths={"image_generation"},
        restart_required=res.restart_required,
        restart_reason="image_generation",
    )
    return {
        "changed": res.changed,
        "restartRequired": commit.restart_required,
        "configPath": str(commit.path),
        "entry": res.public_payload,
        "warnings": res.warnings,
    }


@_d.method("onboarding.memory_embedding.configure", scope="operator.admin")
async def _memory_embedding_configure(params: Any, ctx: RpcContext) -> dict[str, Any]:
    from agentos.onboarding.mutations import upsert_memory_embedding

    provider = _require(params, "providerId")
    cfg = _active_config(ctx)
    res = upsert_memory_embedding(
        cfg,
        provider=provider,
        model=params.get("model", "") if isinstance(params, dict) else "",
        api_key=params.get("apiKey", "") if isinstance(params, dict) else "",
        api_key_env=params.get("apiKeyEnv", "") if isinstance(params, dict) else "",
        base_url=params.get("baseUrl", "") if isinstance(params, dict) else "",
        onnx_dir=params.get("onnxDir", "") if isinstance(params, dict) else "",
    )
    commit = _commit_mutation(
        ctx,
        cfg,
        res.config,
        params,
        changed_paths={"memory.embedding"},
        restart_required=res.restart_required,
        restart_reason="memory",
    )
    return {
        "changed": res.changed,
        "restartRequired": commit.restart_required,
        "configPath": str(commit.path),
        "entry": res.public_payload,
        "warnings": res.warnings,
    }


@_d.method("onboarding.audio.configure", scope="operator.admin")
async def _audio_configure(params: Any, ctx: RpcContext) -> dict[str, Any]:
    from agentos.onboarding.mutations import upsert_audio_provider

    provider_id = _require(params, "providerId")
    cfg = _active_config(ctx)
    res = upsert_audio_provider(
        cfg,
        provider_id=provider_id,
        api_key=params.get("apiKey", "") if isinstance(params, dict) else "",
        api_key_env=params.get("apiKeyEnv", "") if isinstance(params, dict) else "",
        base_url=params.get("baseUrl", "") if isinstance(params, dict) else "",
        enabled=params.get("enabled", True) if isinstance(params, dict) else True,
        tts_voice=params.get("ttsVoice", "") if isinstance(params, dict) else "",
        tts_model=params.get("ttsModel", "") if isinstance(params, dict) else "",
        language_code=params.get("languageCode", "") if isinstance(params, dict) else "",
    )
    commit = _commit_mutation(
        ctx,
        cfg,
        res.config,
        params,
        changed_paths={"audio"},
        restart_required=res.restart_required,
        restart_reason="audio",
    )
    return {
        "changed": res.changed,
        "restartRequired": commit.restart_required,
        "configPath": str(commit.path),
        "entry": res.public_payload,
        "warnings": res.warnings,
    }


@_d.method("onboarding.channel.upsert", scope="operator.admin")
async def _channel_upsert(params: Any, ctx: RpcContext) -> dict[str, Any]:
    from agentos.onboarding.mutations import upsert_channel

    entry = _require(params, "entry")
    if not isinstance(entry, dict):
        raise ValueError("params.entry must be an object")
    cfg = _active_config(ctx)
    res = upsert_channel(cfg, entry_payload=entry)
    commit = _commit_mutation(
        ctx,
        cfg,
        res.config,
        params,
        changed_paths={"channels"},
        restart_required=True,
        restart_reason="channels",
    )
    return {
        "changed": res.changed,
        "restartRequired": commit.restart_required,
        "configPath": str(commit.path),
        "entry": res.public_payload,
        "warnings": res.warnings,
    }


@_d.method("onboarding.channel.remove", scope="operator.admin")
async def _channel_remove(params: Any, ctx: RpcContext) -> dict[str, Any]:
    from agentos.onboarding.mutations import remove_channel

    name = _require(params, "name")
    cfg = _active_config(ctx)
    res = remove_channel(cfg, name=name)
    commit = _commit_mutation(
        ctx,
        cfg,
        res.config,
        params,
        changed_paths={"channels"},
        restart_required=True,
        restart_reason="channels",
    )
    return {
        "changed": res.changed,
        "restartRequired": commit.restart_required,
        "configPath": str(commit.path),
        "removed": name,
    }


async def _toggle(ctx: RpcContext, params: Any, enabled: bool) -> dict[str, Any]:
    from agentos.onboarding.mutations import set_channel_enabled

    name = _require(params, "name")
    cfg = _active_config(ctx)
    res = set_channel_enabled(cfg, name=name, enabled=enabled)
    commit = _commit_mutation(
        ctx,
        cfg,
        res.config,
        params,
        changed_paths={"channels"},
        restart_required=True,
        restart_reason="channels",
    )
    return {
        "changed": res.changed,
        "restartRequired": commit.restart_required,
        "configPath": str(commit.path),
        "name": name,
        "enabled": enabled,
    }


@_d.method("onboarding.channel.enable", scope="operator.admin")
async def _channel_enable(params: Any, ctx: RpcContext) -> dict[str, Any]:
    return await _toggle(ctx, params, True)


@_d.method("onboarding.channel.disable", scope="operator.admin")
async def _channel_disable(params: Any, ctx: RpcContext) -> dict[str, Any]:
    return await _toggle(ctx, params, False)
