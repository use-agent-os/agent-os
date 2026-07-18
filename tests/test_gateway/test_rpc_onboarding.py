"""RPC tests for onboarding handlers."""

from __future__ import annotations

import platform
import tomllib

import pytest

import agentos.gateway.rpc_onboarding  # noqa: F401  ensures registration
from agentos.gateway.auth import Principal
from agentos.gateway.rpc import RpcContext, get_dispatcher


def _env_hint(env_key: str) -> str:
    if platform.system().lower().startswith("win"):
        return f'PowerShell: $env:{env_key} = "<your-key>"'
    return f'export {env_key}="<your-key>"'


def _admin_ctx() -> RpcContext:
    return RpcContext(
        conn_id="t",
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.admin"}),
            is_owner=True,
            authenticated=True,
        ),
    )


def _read_ctx() -> RpcContext:
    return RpcContext(
        conn_id="t",
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.read"}),
            is_owner=False,
            authenticated=True,
        ),
    )


@pytest.mark.asyncio
async def test_onboarding_status_works_with_read_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    res = await get_dispatcher().dispatch("r1", "onboarding.status", {}, _read_ctx())
    assert res.error is None, res.error
    assert "needsOnboarding" in res.payload
    assert "configPath" in res.payload
    assert "sections" in res.payload
    assert "sectionDetails" in res.payload
    assert "memory_embedding" in res.payload["sections"]


@pytest.mark.asyncio
async def test_onboarding_catalog_returns_providers_and_channels(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    res = await get_dispatcher().dispatch("r1", "onboarding.catalog", {}, _read_ctx())
    assert res.error is None, res.error
    payload = res.payload
    assert "providers" in payload
    assert "channels" in payload
    assert "searchProviders" in payload
    assert "routerProfiles" in payload
    assert "imageGenerationProviders" in payload
    assert "audioProviders" in payload
    assert "memoryEmbeddingProviders" in payload
    types = {c["type"] for c in payload["channels"]}
    assert {"slack", "telegram", "matrix", "discord"} <= types
    search_provider_ids = {p["providerId"] for p in payload["searchProviders"]}
    assert {"brave", "duckduckgo"} <= search_provider_ids
    image_provider_ids = {p["providerId"] for p in payload["imageGenerationProviders"]}
    assert {"openai", "openrouter"} <= image_provider_ids
    audio_provider_ids = {p["providerId"] for p in payload["audioProviders"]}
    assert {"elevenlabs"} <= audio_provider_ids
    assert all("whatYouNeed" in p for p in payload["audioProviders"])
    memory_provider_ids = {p["providerId"] for p in payload["memoryEmbeddingProviders"]}
    assert {
        "auto",
        "local",
        "openai",
        "openai-compatible",
        "ollama",
        "none",
    } <= memory_provider_ids
    assert all("whatYouNeed" in p for p in payload["memoryEmbeddingProviders"])
    router_profile_ids = {p["profileId"] for p in payload["routerProfiles"]["profiles"]}
    assert {"openrouter", "deepseek", "openai"} <= router_profile_ids


@pytest.mark.asyncio
async def test_provider_configure_redacts_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.provider.configure",
        {"providerId": "openrouter", "model": "x", "apiKey": "sk-test"},
        _admin_ctx(),
    )
    assert res.error is None, res.error
    assert res.payload["changed"] is True
    assert res.payload["entry"]["api_key"] == "***"
    assert res.payload["restartRequired"] is False


@pytest.mark.asyncio
async def test_provider_configure_can_omit_model_for_router_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.provider.configure",
        {"providerId": "deepseek", "apiKeyEnv": "DEEPSEEK_API_KEY"},
        _admin_ctx(),
    )
    assert res.error is None, res.error
    assert res.payload["entry"]["model"] == "deepseek-v4-flash"
    data = tomllib.loads((tmp_path / "c.toml").read_text())
    assert data["llm"]["model"] == "deepseek-v4-flash"
    assert data["agentos_router"]["tier_profile"] == "deepseek"


@pytest.mark.asyncio
async def test_router_configure_recommended_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    from agentos.gateway.config import GatewayConfig

    ctx = _admin_ctx()
    ctx.config = GatewayConfig(llm={"provider": "deepseek", "model": "deepseek-chat"})
    ctx.config.config_path = str(tmp_path / "c.toml")

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.router.configure",
        {"mode": "recommended"},
        ctx,
    )

    assert res.error is None, res.error
    assert ctx.config.agentos_router.enabled is True
    assert ctx.config.agentos_router.tier_profile == "deepseek"
    persisted = tomllib.loads((tmp_path / "c.toml").read_text())
    assert persisted["agentos_router"]["tier_profile"] == "deepseek"
    assert "tiers" not in persisted["agentos_router"]


@pytest.mark.asyncio
async def test_router_configure_persists_pilot_strategy_round_trip(tmp_path, monkeypatch):
    """T10: the web setup UI persists ``strategy="pilot-v1"`` through the same
    ``onboarding.router.configure`` → ``upsert_router`` path, and reloading the
    config keeps the Pilot strategy (never silently re-derived to v4/judge)."""
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    from agentos.gateway.config import GatewayConfig

    ctx = _admin_ctx()
    ctx.config = GatewayConfig(llm={"provider": "deepseek", "model": "deepseek-chat"})
    ctx.config.config_path = str(tmp_path / "c.toml")

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.router.configure",
        {"mode": "recommended", "strategy": "pilot-v1"},
        ctx,
    )

    assert res.error is None, res.error
    assert res.payload["entry"]["strategy"] == "pilot-v1"
    assert ctx.config.agentos_router.enabled is True
    assert ctx.config.agentos_router.strategy == "pilot-v1"
    persisted = tomllib.loads((tmp_path / "c.toml").read_text())
    assert persisted["agentos_router"]["strategy"] == "pilot-v1"

    # Reload from the persisted TOML — the strategy must survive a fresh load so
    # the setup UI shows Pilot selected, not a judge-else-v4 re-derivation.
    reloaded = GatewayConfig.model_validate(persisted)
    assert reloaded.agentos_router.strategy == "pilot-v1"


@pytest.mark.asyncio
async def test_router_configure_forwards_explicit_judge_model(tmp_path, monkeypatch):
    """Finding #11: the web onboarding RPC must forward judgeModel/judgeProvider
    to upsert_router (like the CLI path) so a picked judge is actually
    persisted, not silently dropped to AUTO."""
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    from agentos.gateway.config import GatewayConfig

    ctx = _admin_ctx()
    ctx.config = GatewayConfig(llm={"provider": "deepseek", "model": "deepseek-chat"})
    ctx.config.config_path = str(tmp_path / "c.toml")

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.router.configure",
        {"mode": "recommended", "judgeModel": "deepseek-v4-pro"},
        ctx,
    )

    assert res.error is None, res.error
    assert ctx.config.agentos_router.judge_model == "deepseek-v4-pro"
    persisted = tomllib.loads((tmp_path / "c.toml").read_text())
    assert persisted["agentos_router"]["judge_model"] == "deepseek-v4-pro"


@pytest.mark.asyncio
async def test_router_configure_empty_judge_model_stays_auto(tmp_path, monkeypatch):
    """An empty judgeModel (the web UI's 'Auto' option) clears to AUTO
    (judge_model=None), persisting nothing so profile switches auto-update."""
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    from agentos.gateway.config import GatewayConfig

    ctx = _admin_ctx()
    ctx.config = GatewayConfig(
        llm={"provider": "deepseek", "model": "deepseek-chat"},
        agentos_router={"judge_model": "stale-pinned-model"},
    )
    ctx.config.config_path = str(tmp_path / "c.toml")

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.router.configure",
        {"mode": "recommended", "judgeModel": ""},
        ctx,
    )

    assert res.error is None, res.error
    assert ctx.config.agentos_router.judge_model is None


@pytest.mark.asyncio
async def test_router_configure_omitted_judge_model_preserves_local_endpoint(
    tmp_path, monkeypatch
):
    """WebUI round-trip: an untouched judge dropdown sends judgeModel=null (not
    ''), which must PRESERVE a CLI-configured local judge (base_url/api_key)
    rather than clearing it to AUTO. Clearing it would leave an explicit cloud
    judge on a model the provider doesn't serve → judge_unavailable every turn.
    """
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    from agentos.gateway.config import GatewayConfig

    ctx = _admin_ctx()
    ctx.config = GatewayConfig(
        llm={"provider": "deepseek", "model": "deepseek-chat"},
        agentos_router={
            "judge_model": "llama3",
            "judge_base_url": "http://localhost:11434/v1",
            "judge_api_key": "sk-local",
        },
    )
    ctx.config.config_path = str(tmp_path / "c.toml")

    # judgeModel omitted entirely -> params.get("judgeModel") is None -> preserve.
    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.router.configure",
        {"mode": "recommended"},
        ctx,
    )

    assert res.error is None, res.error
    router = ctx.config.agentos_router
    assert router.judge_model == "llama3"
    assert router.judge_base_url == "http://localhost:11434/v1"
    assert router.judge_api_key == "sk-local"


@pytest.mark.asyncio
async def test_router_configure_runs_local_endpoint_verify_off_event_loop(
    tmp_path, monkeypatch
):
    """Findings #1/#3: when a local judge endpoint is supplied, upsert_router runs
    the blocking connectivity probe. The RPC handler must dispatch it off the
    gateway event loop (via asyncio.to_thread) so a slow/unreachable endpoint
    can't freeze the loop for the whole ~13.5s probe. Assert the synchronous
    upsert_router executed on a worker thread with NO running event loop."""
    import asyncio
    import threading

    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    from agentos.gateway.config import GatewayConfig
    from agentos.onboarding import mutations as mutations_mod

    ctx = _admin_ctx()
    ctx.config = GatewayConfig(llm={"provider": "deepseek", "model": "deepseek-chat"})
    ctx.config.config_path = str(tmp_path / "c.toml")

    loop_thread = threading.get_ident()
    observed: dict[str, object] = {}
    real_upsert = mutations_mod.upsert_router

    def _spy_upsert(config, **kwargs):
        observed["thread"] = threading.get_ident()
        try:
            observed["running_loop"] = asyncio.get_running_loop()
        except RuntimeError:
            observed["running_loop"] = None
        observed["verify_local_endpoint"] = kwargs.get("verify_local_endpoint")
        # Skip the real network probe: verify only WHERE it runs, not the probe.
        return real_upsert(config, **{**kwargs, "verify_local_endpoint": False})

    # The handler imports upsert_router locally from the mutations module at call
    # time, so patching it at its source is sufficient.
    monkeypatch.setattr(mutations_mod, "upsert_router", _spy_upsert)

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.router.configure",
        {
            "mode": "recommended",
            "judgeModel": "qwen2.5",
            "judgeBaseUrl": "http://127.0.0.1:11434/v1",
        },
        ctx,
    )

    assert res.error is None, res.error
    assert observed["verify_local_endpoint"] is True
    # Ran on a different thread than the event loop, with no loop running there.
    assert observed["thread"] != loop_thread
    assert observed["running_loop"] is None


@pytest.mark.asyncio
async def test_router_configure_without_local_endpoint_stays_on_loop(
    tmp_path, monkeypatch
):
    """Without a local endpoint there is no blocking probe, so the non-verify
    path stays inline on the event loop (no needless thread hop)."""
    import threading

    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    from agentos.gateway.config import GatewayConfig
    from agentos.onboarding import mutations as mutations_mod

    ctx = _admin_ctx()
    ctx.config = GatewayConfig(llm={"provider": "deepseek", "model": "deepseek-chat"})
    ctx.config.config_path = str(tmp_path / "c.toml")

    loop_thread = threading.get_ident()
    observed: dict[str, object] = {}
    real_upsert = mutations_mod.upsert_router

    def _spy_upsert(config, **kwargs):
        observed["thread"] = threading.get_ident()
        observed["verify_local_endpoint"] = kwargs.get("verify_local_endpoint")
        return real_upsert(config, **kwargs)

    monkeypatch.setattr(mutations_mod, "upsert_router", _spy_upsert)

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.router.configure",
        {"mode": "recommended"},
        ctx,
    )

    assert res.error is None, res.error
    assert observed["verify_local_endpoint"] is False
    assert observed["thread"] == loop_thread


@pytest.mark.asyncio
async def test_router_configure_accepts_tier_overrides_and_syncs_llm_model(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    from agentos.gateway.config import GatewayConfig

    ctx = _admin_ctx()
    ctx.config = GatewayConfig(llm={"provider": "openai", "model": "gpt-5.4-mini"})
    ctx.config.config_path = str(tmp_path / "c.toml")

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.router.configure",
        {
            "mode": "recommended",
            "defaultTier": "c2",
            "tiers": {
                "c2": {"provider": "openai", "model": "gpt-5.5-custom"},
                "image_model": {
                    "provider": "openai",
                    "model": "gpt-5.4-mini",
                    "supportsImage": True,
                },
            },
        },
        ctx,
    )

    assert res.error is None, res.error
    assert ctx.config.llm.model == "gpt-5.5-custom"
    assert ctx.config.agentos_router.default_tier == "c2"
    persisted = tomllib.loads((tmp_path / "c.toml").read_text())
    assert persisted["llm"]["model"] == "gpt-5.5-custom"
    assert persisted["agentos_router"]["tiers"]["c2"]["model"] == "gpt-5.5-custom"
    assert persisted["agentos_router"]["tiers"]["image_model"]["supports_image"] is True


@pytest.mark.asyncio
async def test_router_configure_persists_image_model_as_image_capable(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    from agentos.gateway.config import GatewayConfig

    ctx = _admin_ctx()
    ctx.config = GatewayConfig(llm={"provider": "openrouter", "model": "z-ai/glm-5.1"})
    ctx.config.config_path = str(tmp_path / "c.toml")

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.router.configure",
        {
            "mode": "openrouter-mix",
            "defaultTier": "t1",
            "tiers": {
                "image_model": {
                    "provider": "openrouter",
                    "model": "anthropic/claude-opus-4.7",
                    "supportsImage": False,
                },
            },
        },
        ctx,
    )

    assert res.error is None, res.error
    persisted = tomllib.loads((tmp_path / "c.toml").read_text())
    image_tier = persisted["agentos_router"]["tiers"]["image_model"]
    assert image_tier["model"] == "anthropic/claude-opus-4.7"
    assert image_tier["supports_image"] is True
    assert image_tier["image_only"] is True
    assert ctx.config.agentos_router.tiers["image_model"]["supports_image"] is True
    assert ctx.config.agentos_router.tiers["image_model"]["image_only"] is True


@pytest.mark.asyncio
async def test_router_configure_rejects_image_model_as_default_tier(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    from agentos.gateway.config import GatewayConfig

    ctx = _admin_ctx()
    ctx.config = GatewayConfig(llm={"provider": "openrouter", "model": "m"})
    ctx.config.config_path = str(tmp_path / "c.toml")

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.router.configure",
        {"mode": "recommended", "defaultTier": "image_model"},
        ctx,
    )

    assert res.error is not None
    assert "defaultTier must reference a text tier" in res.error.message


@pytest.mark.asyncio
async def test_provider_configure_recomputes_existing_router_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    from agentos.gateway.config import GatewayConfig

    ctx = _admin_ctx()
    ctx.config = GatewayConfig(
        llm={"provider": "deepseek", "model": "deepseek-chat"},
        agentos_router={"tier_profile": "deepseek"},
    )
    ctx.config.config_path = str(tmp_path / "c.toml")

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.provider.configure",
        {
            "providerId": "openai",
            "model": "gpt-5.4-mini",
            "apiKeyEnv": "OPENAI_API_KEY",
        },
        ctx,
    )

    assert res.error is None, res.error
    assert ctx.config.llm.provider == "openai"
    assert ctx.config.agentos_router.tier_profile == "openai"
    persisted = tomllib.loads((tmp_path / "c.toml").read_text())
    assert persisted["agentos_router"]["tier_profile"] == "openai"
    assert "tiers" not in persisted["agentos_router"]


@pytest.mark.asyncio
async def test_provider_configure_recomputes_openrouter_mix_router(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    from agentos.gateway.config import GatewayConfig

    ctx = _admin_ctx()
    ctx.config = GatewayConfig(llm={"provider": "openrouter", "model": "deepseek/x"})
    ctx.config.config_path = str(tmp_path / "c.toml")

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.provider.configure",
        {
            "providerId": "deepseek",
            "model": "deepseek-chat",
            "apiKeyEnv": "DEEPSEEK_API_KEY",
        },
        ctx,
    )

    assert res.error is None, res.error
    assert ctx.config.llm.provider == "deepseek"
    assert ctx.config.agentos_router.enabled is True
    assert ctx.config.agentos_router.tier_profile == "deepseek"
    persisted = tomllib.loads((tmp_path / "c.toml").read_text())
    assert persisted["agentos_router"]["tier_profile"] == "deepseek"
    assert "tiers" not in persisted["agentos_router"]


@pytest.mark.asyncio
async def test_router_catalog_rpc(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.router.catalog",
        {},
        _read_ctx(),
    )

    assert res.error is None, res.error
    profile_ids = {p["profileId"] for p in res.payload["profiles"]}
    assert {"openrouter", "deepseek"} <= profile_ids


@pytest.mark.asyncio
async def test_channel_upsert_redacts_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.channel.upsert",
        {
            "entry": {
                "type": "slack",
                "name": "w",
                "token": "supersecret",
                "signing_secret": "signing-secret",
            }
        },
        _admin_ctx(),
    )
    assert res.error is None, res.error
    assert res.payload["changed"] is True
    assert res.payload["restartRequired"] is True
    assert res.payload["entry"]["token"] == "***"


@pytest.mark.asyncio
async def test_channel_upsert_rejects_slack_webhook_without_signing_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.channel.upsert",
        {"entry": {"type": "slack", "name": "w", "token": "supersecret"}},
        _admin_ctx(),
    )

    assert res.error is not None
    assert "signing_secret" in res.error.message


@pytest.mark.asyncio
async def test_channel_upsert_rejects_slack_socket_without_app_token(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.channel.upsert",
        {
            "entry": {
                "type": "slack",
                "name": "w",
                "token": "supersecret",
                "connection_mode": "socket",
            }
        },
        _admin_ctx(),
    )

    assert res.error is not None
    assert "app_token" in res.error.message


@pytest.mark.asyncio
async def test_channel_probe_validates_and_redacts_without_persisting(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(target))
    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.channel.probe",
        {
            "entry": {
                "type": "telegram",
                "name": "tg",
                "token": "123:secret",
                "transport_name": "polling",
            }
        },
        _admin_ctx(),
    )
    assert res.error is None, res.error
    assert res.payload["status"] in {"ready", "action_needed"}
    assert res.payload["entry"]["token"] == "***"
    assert "123:secret" not in str(res.payload)
    assert not target.exists()


@pytest.mark.asyncio
async def test_search_configure_redacts_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.search.configure",
        {"providerId": "brave", "apiKey": "brave-secret", "maxResults": 3},
        _admin_ctx(),
    )
    assert res.error is None, res.error
    assert res.payload["changed"] is True
    assert res.payload["entry"]["api_key"] == "***"


@pytest.mark.asyncio
async def test_search_configure_accepts_webui_string_max_results(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.search.configure",
        {"providerId": "duckduckgo", "maxResults": "5"},
        _admin_ctx(),
    )

    assert res.error is None, res.error
    assert res.payload["entry"]["max_results"] == 5


@pytest.mark.asyncio
async def test_image_generation_configure_redacts_api_key(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(target))
    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.imageGeneration.configure",
        {
            "providerId": "openrouter",
            "primary": "openrouter/google/gemini-3.1-flash-image-preview",
            "apiKey": "sk-or",
        },
        _admin_ctx(),
    )
    assert res.error is None, res.error
    assert res.payload["changed"] is True
    assert res.payload["restartRequired"] is False
    assert res.payload["entry"]["api_key"] == "***"

    data = tomllib.loads(target.read_text())
    assert data["image_generation"]["enabled"] is True
    assert (
        data["image_generation"]["primary"]
        == "openrouter/google/gemini-3.1-flash-image-preview"
    )
    assert data["image_generation"]["providers"]["openrouter"]["api_key"] == "sk-or"


@pytest.mark.asyncio
async def test_image_generation_configure_can_use_custom_env_reference(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(target))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("AGENTOS_TEST_IMAGE_KEY", "sk-image-env")

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.imageGeneration.configure",
        {
            "providerId": "openrouter",
            "primary": "openrouter/google/gemini-3.1-flash-image-preview",
            "apiKeyEnv": "AGENTOS_TEST_IMAGE_KEY",
        },
        _admin_ctx(),
    )

    assert res.error is None, res.error
    assert res.payload["entry"]["api_key_source"] == "env"
    assert res.payload["entry"]["api_key_env"] == "AGENTOS_TEST_IMAGE_KEY"
    data = tomllib.loads(target.read_text())
    provider = data["image_generation"]["providers"]["openrouter"]
    assert provider["api_key"] == ""
    assert provider["api_key_env"] == "AGENTOS_TEST_IMAGE_KEY"


@pytest.mark.asyncio
async def test_image_generation_configure_can_save_missing_custom_env_reference(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(target))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("AGENTOS_TEST_IMAGE_KEY", raising=False)

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.imageGeneration.configure",
        {
            "providerId": "openrouter",
            "primary": "openrouter/google/gemini-3.1-flash-image-preview",
            "apiKeyEnv": "AGENTOS_TEST_IMAGE_KEY",
        },
        _admin_ctx(),
    )

    assert res.error is None, res.error
    assert res.payload["entry"]["api_key_source"] == "missing_env"
    assert res.payload["entry"]["api_key_env"] == "AGENTOS_TEST_IMAGE_KEY"
    data = tomllib.loads(target.read_text())
    provider = data["image_generation"]["providers"]["openrouter"]
    assert provider["api_key"] == ""
    assert provider["api_key_env"] == "AGENTOS_TEST_IMAGE_KEY"


@pytest.mark.asyncio
async def test_image_generation_configure_can_disable_without_visible_key(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(target))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.imageGeneration.configure",
        {
            "providerId": "openrouter",
            "primary": "openrouter/google/gemini-3.1-flash-image-preview",
            "enabled": False,
        },
        _admin_ctx(),
    )

    assert res.error is None, res.error
    assert res.payload["entry"]["enabled"] is False
    assert res.payload["entry"]["api_key_source"] == "none"

    data = tomllib.loads(target.read_text())
    assert data["image_generation"]["enabled"] is False


@pytest.mark.asyncio
async def test_onboarding_status_requires_image_generation_enable_for_llm_fallback(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    from agentos.gateway.config import GatewayConfig

    ctx = _read_ctx()
    ctx.config = GatewayConfig()
    ctx.config.llm.provider = "openrouter"
    ctx.config.llm.api_key = "sk-or"

    res = await get_dispatcher().dispatch("r1", "onboarding.status", {}, ctx)

    assert res.error is None, res.error
    assert res.payload["imageGenerationConfigured"] is False
    assert res.payload["imageGenerationEnabled"] is False
    assert res.payload["imageGenerationSource"] == "none"
    assert res.payload["imageGenerationProvider"] == ""


@pytest.mark.asyncio
async def test_onboarding_status_exposes_missing_env_keys_for_optional_capabilities(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    from agentos.gateway.config import GatewayConfig

    ctx = _read_ctx()
    ctx.config = GatewayConfig()
    ctx.config.llm.provider = "openrouter"
    ctx.config.llm.model = "deepseek/deepseek-v4-flash"
    ctx.config.llm.api_key = "sk-or"
    ctx.config.search_provider = "brave"
    ctx.config.search_api_key_env = "BRAVE_SEARCH_API_KEY"
    ctx.config.image_generation.enabled = True
    ctx.config.image_generation.primary = "openai/gpt-image-1"
    ctx.config.image_generation.providers.openai.api_key_env = "OPENAI_IMAGE_KEY"
    ctx.config.memory.embedding.provider = "openai"
    ctx.config.memory.embedding.remote.api_key_env = "OPENAI_EMBEDDINGS_API_KEY"
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_IMAGE_KEY", raising=False)
    monkeypatch.delenv("OPENAI_EMBEDDINGS_API_KEY", raising=False)

    res = await get_dispatcher().dispatch("r1", "onboarding.status", {}, ctx)

    assert res.error is None, res.error
    assert res.payload["searchProvider"] == "brave"
    assert res.payload["searchSource"] == "missing_env"
    assert res.payload["searchEnvKey"] == "BRAVE_SEARCH_API_KEY"
    assert res.payload["sections"]["image_generation"] == "degraded"
    assert res.payload["sectionDetails"]["image_generation"]["actionRequired"] is True
    assert res.payload["imageGenerationSource"] == "missing_env"
    assert res.payload["imageGenerationProvider"] == "openai"
    assert res.payload["imageGenerationEnvKey"] == "OPENAI_IMAGE_KEY"
    assert res.payload["memoryEmbeddingSource"] == "missing_env"
    assert res.payload["memoryEmbeddingEnvKey"] == "OPENAI_EMBEDDINGS_API_KEY"
    assert res.payload["envRecoveryCommands"] == [
        {
            "section": "memory_embedding",
            "label": "Set memory key",
            "command": _env_hint("OPENAI_EMBEDDINGS_API_KEY"),
        },
        {
            "section": "search",
            "label": "Set search key",
            "command": _env_hint("BRAVE_SEARCH_API_KEY"),
        },
        {
            "section": "image_generation",
            "label": "Set image key",
            "command": _env_hint("OPENAI_IMAGE_KEY"),
        },
    ]


@pytest.mark.asyncio
async def test_image_generation_configure_can_enable_llm_fallback(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(target))
    from agentos.gateway.config import GatewayConfig

    ctx = _admin_ctx()
    ctx.config = GatewayConfig()
    ctx.config.llm.provider = "openrouter"
    ctx.config.llm.api_key = "sk-or"

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.imageGeneration.configure",
        {"providerId": "openrouter"},
        ctx,
    )

    assert res.error is None, res.error
    assert res.payload["entry"]["enabled"] is True
    assert res.payload["entry"]["api_key_source"] == "llm_fallback"

    data = tomllib.loads(target.read_text())
    assert data["image_generation"]["enabled"] is True
    assert data["image_generation"]["providers"]["openrouter"]["api_key"] == ""


@pytest.mark.asyncio
async def test_audio_configure_redacts_api_key_and_persists_tts_defaults(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(target))

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.audio.configure",
        {
            "providerId": "elevenlabs",
            "apiKey": "el-secret",
            "baseUrl": "https://audio.example",
            "ttsVoice": "voice_custom",
            "ttsModel": "eleven_turbo_v2_5",
            "languageCode": "zh-CN",
        },
        _admin_ctx(),
    )

    assert res.error is None, res.error
    assert res.payload["changed"] is True
    assert res.payload["restartRequired"] is False
    assert res.payload["entry"]["api_key"] == "***"
    assert res.payload["entry"]["enabled"] is True

    data = tomllib.loads(target.read_text())
    assert data["audio"]["enabled"] is True
    assert data["audio"]["providers"]["elevenlabs"]["api_key"] == "el-secret"
    assert data["audio"]["providers"]["elevenlabs"]["base_url"] == "https://audio.example"
    assert data["audio"]["tts"]["voice"] == "voice_custom"
    assert data["audio"]["tts"]["model"] == "eleven_turbo_v2_5"
    assert data["audio"]["tts"]["language_code"] == "zh-CN"


@pytest.mark.asyncio
async def test_audio_configure_can_save_missing_env_reference(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(target))
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.audio.configure",
        {
            "providerId": "elevenlabs",
            "apiKeyEnv": "ELEVENLABS_API_KEY",
            "enabled": True,
        },
        _admin_ctx(),
    )

    assert res.error is None, res.error
    assert res.payload["entry"]["api_key_source"] == "missing_env"
    assert res.payload["entry"]["api_key_env"] == "ELEVENLABS_API_KEY"

    status = await get_dispatcher().dispatch("r2", "onboarding.status", {}, _read_ctx())
    assert status.error is None, status.error
    assert status.payload["sections"]["audio"] == "degraded"
    assert status.payload["audioSource"] == "missing_env"
    assert status.payload["audioEnvKey"] == "ELEVENLABS_API_KEY"


@pytest.mark.asyncio
async def test_memory_embedding_configure_redacts_remote_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.memory_embedding.configure",
        {
            "providerId": "openai",
            "model": "text-embedding-3-small",
            "apiKey": "mem-secret",
            "baseUrl": "https://api.openai.com/v1",
        },
        _admin_ctx(),
    )
    assert res.error is None, res.error
    assert res.payload["changed"] is True
    assert res.payload["restartRequired"] is True
    assert res.payload["entry"]["remote"]["api_key"] == "***"


@pytest.mark.asyncio
async def test_memory_embedding_configure_can_use_env_key_reference(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(target))

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.memory_embedding.configure",
        {
            "providerId": "openai",
            "model": "text-embedding-3-small",
            "apiKeyEnv": "OPENAI_EMBEDDINGS_API_KEY",
        },
        _admin_ctx(),
    )

    assert res.error is None, res.error
    assert res.payload["entry"]["remote"]["api_key_env"] == "OPENAI_EMBEDDINGS_API_KEY"
    data = tomllib.loads(target.read_text())
    remote = data["memory"]["embedding"]["remote"]
    assert remote["api_key_env"] == "OPENAI_EMBEDDINGS_API_KEY"
    assert "api_key" not in remote


@pytest.mark.asyncio
async def test_memory_embedding_configure_updates_ctx_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    from agentos.gateway.config import GatewayConfig

    ctx = _admin_ctx()
    ctx.config = GatewayConfig()
    ctx.config.config_path = str(tmp_path / "c.toml")

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.memory_embedding.configure",
        {"providerId": "local", "onnxDir": "models/bge"},
        ctx,
    )
    assert res.error is None, res.error
    assert ctx.config.memory.embedding.requested_provider == "local"
    assert ctx.config.memory.embedding.local.onnx_dir == "models/bge"


@pytest.mark.asyncio
async def test_memory_embedding_configure_auto_can_store_remote_fallback(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    from agentos.gateway.config import GatewayConfig

    ctx = _admin_ctx()
    ctx.config = GatewayConfig()
    ctx.config.config_path = str(tmp_path / "c.toml")

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.memory_embedding.configure",
        {
            "providerId": "auto",
            "model": "text-embedding-3-small",
            "apiKey": "mem-secret",
            "baseUrl": "https://embeddings.example/v1",
        },
        ctx,
    )

    assert res.error is None, res.error
    assert ctx.config.memory.embedding.requested_provider == "auto"
    assert ctx.config.memory.embedding.remote.api_key == "mem-secret"
    assert ctx.config.memory.embedding.remote.base_url == "https://embeddings.example/v1"
    assert res.payload["entry"]["remote"]["api_key"] == "***"


@pytest.mark.asyncio
async def test_admin_required_for_mutations(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.provider.configure",
        {"providerId": "openrouter", "model": "x", "apiKey": "k"},
        _read_ctx(),
    )
    assert res.error is not None
    assert res.error.code == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_provider_configure_writes_to_active_config_path(tmp_path, monkeypatch):
    # Gateway booted from ./agentos.toml — RPC must respect ctx.config.config_path.
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(tmp_path / "wrong.toml"))
    project_config = tmp_path / "project.toml"

    from agentos.gateway.config import GatewayConfig

    ctx = _admin_ctx()
    ctx.config = GatewayConfig()
    ctx.config.config_path = str(project_config)

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.provider.configure",
        {"providerId": "openrouter", "model": "x", "apiKey": "sk-test"},
        ctx,
    )
    assert res.error is None, res.error
    assert project_config.exists()
    assert not (tmp_path / "wrong.toml").exists()
    assert res.payload["configPath"] == str(project_config)


@pytest.mark.asyncio
async def test_provider_configure_updates_ctx_config_in_place(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    from agentos.gateway.config import GatewayConfig

    ctx = _admin_ctx()
    ctx.config = GatewayConfig()
    ctx.config.config_path = str(tmp_path / "c.toml")

    await get_dispatcher().dispatch(
        "r1",
        "onboarding.provider.configure",
        {"providerId": "openrouter", "model": "deepseek/x", "apiKey": "sk-new"},
        ctx,
    )
    # The running gateway's config should now reflect the change.
    assert ctx.config.llm.provider == "openrouter"
    assert ctx.config.llm.model == "deepseek/x"
    assert ctx.config.llm.api_key == "sk-new"


@pytest.mark.asyncio
async def test_provider_configure_does_not_persist_runtime_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    from agentos.gateway.config import GatewayConfig

    target = tmp_path / "c.toml"
    ctx = _admin_ctx()
    ctx.config = GatewayConfig()
    ctx.config.config_path = str(target)
    ctx.config.llm.provider = "openrouter"
    ctx.config.llm.model = "m1"
    ctx.config.llm.api_key = "from-env"
    ctx.config.mark_runtime_secret("llm.api_key")

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.provider.configure",
        {"providerId": "openrouter", "model": "m2"},
        ctx,
    )

    assert res.error is None, res.error
    data = tomllib.loads(target.read_text())
    assert "api_key" not in data["llm"]
    assert ctx.config.llm.api_key == "from-env"


@pytest.mark.asyncio
async def test_provider_configure_calls_provider_selector_sync(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    from agentos.gateway.config import GatewayConfig

    sync_calls: list[object] = []

    class FakeSelector:
        def sync_primary(self, provider_config):
            sync_calls.append(provider_config)

    ctx = _admin_ctx()
    ctx.config = GatewayConfig()
    ctx.config.config_path = str(tmp_path / "c.toml")
    ctx.provider_selector = FakeSelector()

    await get_dispatcher().dispatch(
        "r1",
        "onboarding.provider.configure",
        {"providerId": "openrouter", "model": "m", "apiKey": "k"},
        ctx,
    )
    assert len(sync_calls) == 1
    assert sync_calls[0].provider == "openrouter"
    assert sync_calls[0].model == "m"
    assert sync_calls[0].api_key == "k"


@pytest.mark.asyncio
async def test_provider_configure_syncs_env_key_to_provider_selector(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-env")
    from agentos.gateway.config import GatewayConfig

    sync_calls: list[object] = []

    class FakeSelector:
        def sync_primary(self, provider_config):
            sync_calls.append(provider_config)

    ctx = _admin_ctx()
    ctx.config = GatewayConfig()
    ctx.config.config_path = str(tmp_path / "c.toml")
    ctx.provider_selector = FakeSelector()

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.provider.configure",
        {
            "providerId": "openrouter",
            "model": "deepseek/deepseek-v4-flash",
            "apiKeyEnv": "OPENROUTER_API_KEY",
        },
        ctx,
    )

    assert res.error is None, res.error
    assert len(sync_calls) == 1
    assert sync_calls[0].api_key == "from-env"
    assert "llm.api_key" in ctx.config._runtime_secret_paths
    persisted = tomllib.loads((tmp_path / "c.toml").read_text())
    assert "api_key" not in persisted["llm"]


@pytest.mark.asyncio
async def test_channel_disable_then_remove(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    d = get_dispatcher()
    await d.dispatch(
        "r1",
        "onboarding.channel.upsert",
        {"entry": {"type": "slack", "name": "w", "token": "t", "signing_secret": "ss"}},
        _admin_ctx(),
    )
    res = await d.dispatch("r2", "onboarding.channel.disable", {"name": "w"}, _admin_ctx())
    assert res.error is None
    assert res.payload["enabled"] is False
    res2 = await d.dispatch("r3", "onboarding.channel.remove", {"name": "w"}, _admin_ctx())
    assert res2.error is None
    assert res2.payload["changed"] is True


# ---------------------------------------------------------------------------
# Local-endpoint judge verification over the running-loop RPC surface (spec D2).
# These drive the real probe_local_judge — only build_provider is faked — so the
# asyncio.run-inside-a-running-loop regression (findings #1-#5) is actually
# exercised through the async dispatcher, not masked by a mocked probe.
# ---------------------------------------------------------------------------


def _script_local_probe_provider(monkeypatch, events_scripts):
    """Point probe_local_judge's self-built strategy at a scripted provider.

    probe_local_judge constructs its own LLMJudgeStrategy (no factory injection),
    so it resolves the client via build_provider. Patch that symbol in the
    llm_judge module to hand back a scripted streaming provider."""
    from agentos.provider.types import DoneEvent, TextDeltaEvent, ToolUseEndEvent

    def _tool_events(route_class):
        return [
            ToolUseEndEvent(
                tool_use_id="tu_1",
                tool_name="emit_route",
                arguments={
                    "route_class": route_class,
                    "confidence": 0.9,
                    "reason": "probe",
                },
            ),
            DoneEvent(),
        ]

    def _text_events(text):
        return [TextDeltaEvent(text=text), DoneEvent()]

    scripts: list[list] = []
    for kind, payload in events_scripts:
        scripts.append(_tool_events(payload) if kind == "tool" else _text_events(payload))

    class _ScriptedProvider:
        provider_name = "fake-local"

        def __init__(self) -> None:
            self._scripts = list(scripts)

        def chat(self, messages, tools=None, config=None):
            events = self._scripts.pop(0)

            async def _gen():
                for event in events:
                    yield event

            return _gen()

        async def list_models(self):
            return []

    import agentos.agentos_router.llm_judge as llm_judge_module

    monkeypatch.setattr(
        llm_judge_module, "build_provider", lambda **_kwargs: _ScriptedProvider()
    )


@pytest.mark.asyncio
async def test_router_configure_local_endpoint_persists_when_reachable(
    tmp_path, monkeypatch
):
    """Findings #1-#5 (blocker): configuring a reachable local judge endpoint via
    onboarding.router.configure (judgeBaseUrl set -> verify_local_endpoint=True)
    used to ALWAYS fail — probe_local_judge called asyncio.run() inside the
    running gateway event loop, raising RuntimeError. With the loop-safe probe a
    reachable endpoint (scripted to return a usable verdict) must persist."""
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    from agentos.gateway.config import GatewayConfig

    _script_local_probe_provider(monkeypatch, [("tool", "R1")])

    ctx = _admin_ctx()
    ctx.config = GatewayConfig(llm={"provider": "deepseek", "model": "deepseek-chat"})
    ctx.config.config_path = str(tmp_path / "c.toml")

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.router.configure",
        {
            "mode": "recommended",
            "judgeModel": "llama3",
            "judgeBaseUrl": "http://localhost:11434/v1",
            "judgeApiKey": "sk-local",
        },
        ctx,
    )

    assert res.error is None, res.error
    assert ctx.config.agentos_router.judge_base_url == "http://localhost:11434/v1"
    assert ctx.config.agentos_router.judge_model == "llama3"
    persisted = tomllib.loads((tmp_path / "c.toml").read_text())
    assert persisted["agentos_router"]["judge_base_url"] == "http://localhost:11434/v1"


@pytest.mark.asyncio
async def test_router_configure_local_endpoint_rejects_unusable_with_invalid_request(
    tmp_path, monkeypatch
):
    """A reachable-but-wrong-model local endpoint (never returns a usable routing
    decision) must be rejected with a clean INVALID_REQUEST 'not usable' error —
    NOT an opaque INTERNAL_ERROR from an asyncio-loop RuntimeError, and NOT
    silently persisted."""
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    from agentos.gateway.config import GatewayConfig

    # Garbage text + garbage repair -> judge_unavailable -> "not usable".
    _script_local_probe_provider(
        monkeypatch, [("text", "not json"), ("text", "still not json")]
    )

    ctx = _admin_ctx()
    ctx.config = GatewayConfig(llm={"provider": "deepseek", "model": "deepseek-chat"})
    ctx.config.config_path = str(tmp_path / "c.toml")

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.router.configure",
        {
            "mode": "recommended",
            "judgeModel": "llama3",
            "judgeBaseUrl": "http://localhost:11434/v1",
        },
        ctx,
    )

    assert res.error is not None
    assert res.error.code == "INVALID_REQUEST"
    assert "not usable" in res.error.message
    assert ctx.config.agentos_router.judge_base_url is None
    assert not (tmp_path / "c.toml").exists()
