"""Tests for onboarding mutations."""

from __future__ import annotations

import pytest

from agentos.gateway.config import GatewayConfig
from agentos.gateway.llm_runtime import resolve_llm_runtime_config
from agentos.onboarding.mutations import (
    MutationResult,
    list_channel_entries,
    remove_channel,
    set_channel_enabled,
    upsert_channel,
    upsert_image_generation_provider,
    upsert_llm_provider,
    upsert_memory_embedding,
    upsert_router,
    upsert_search_provider,
    validate_channel_entry,
)
from agentos.onboarding.redaction import REDACTED_PLACEHOLDER


def test_upsert_provider_persists_fields():
    cfg = GatewayConfig()
    res = upsert_llm_provider(
        cfg,
        provider_id="openrouter",
        model="deepseek/deepseek-v4-flash",
        api_key="sk-test",
        base_url="https://openrouter.ai/api/v1",
    )
    assert isinstance(res, MutationResult)
    assert res.config.llm.provider == "openrouter"
    assert res.config.llm.model == "deepseek/deepseek-v4-flash"
    assert res.config.llm.api_key == "sk-test"
    assert res.changed is True


def test_upsert_provider_strips_trailing_paste_punctuation_from_api_key():
    cfg = GatewayConfig()
    res = upsert_llm_provider(
        cfg,
        provider_id="openrouter",
        model="deepseek/deepseek-v4-flash",
        api_key="sk-test、",
    )

    assert res.config.llm.api_key == "sk-test"


def test_provider_payload_redacts_api_key():
    cfg = GatewayConfig()
    res = upsert_llm_provider(cfg, provider_id="openrouter", model="x", api_key="sk-test")
    assert res.public_payload["api_key"] == REDACTED_PLACEHOLDER


def test_upsert_memory_embedding_local_requires_restart():
    cfg = GatewayConfig()
    res = upsert_memory_embedding(
        cfg,
        provider="local",
        model="BAAI/bge-small-zh-v1.5",
        onnx_dir="models/bge",
    )
    assert res.restart_required is True
    assert res.config.memory.embedding.requested_provider == "local"
    assert res.config.memory.embedding.local.onnx_dir == "models/bge"


def test_upsert_memory_embedding_remote_redacts_key():
    cfg = GatewayConfig()
    res = upsert_memory_embedding(
        cfg,
        provider="openai",
        model="text-embedding-3-small",
        api_key="mem-secret",
        base_url="https://api.openai.com/v1",
    )
    assert res.config.memory.embedding.remote.api_key == "mem-secret"
    assert res.public_payload["remote"]["api_key"] == REDACTED_PLACEHOLDER


def test_upsert_memory_embedding_remote_can_use_env_key_reference():
    cfg = GatewayConfig()
    res = upsert_memory_embedding(
        cfg,
        provider="openai",
        model="text-embedding-3-small",
        api_key_env="OPENAI_EMBEDDINGS_API_KEY",
        base_url="https://api.openai.com/v1",
    )

    remote = res.config.memory.embedding.remote
    assert remote.api_key in {"", None}
    assert remote.api_key_env == "OPENAI_EMBEDDINGS_API_KEY"
    assert res.public_payload["remote"]["api_key_env"] == "OPENAI_EMBEDDINGS_API_KEY"


def test_upsert_memory_embedding_auto_can_store_remote_fallback():
    cfg = GatewayConfig()
    res = upsert_memory_embedding(
        cfg,
        provider="auto",
        model="text-embedding-3-small",
        api_key="mem-secret",
        base_url="https://embeddings.example/v1",
    )
    assert res.config.memory.embedding.requested_provider == "auto"
    assert res.config.memory.embedding.remote.api_key == "mem-secret"
    assert res.config.memory.embedding.remote.base_url == "https://embeddings.example/v1"
    assert res.config.memory.embedding.remote.model == "text-embedding-3-small"
    assert res.public_payload["remote"]["api_key"] == REDACTED_PLACEHOLDER


def test_upsert_memory_embedding_explicit_remote_reuses_auto_remote_key():
    cfg = GatewayConfig(
        memory={
            "embedding": {
                "provider": "auto",
                "remote": {
                    "api_key": "mem-secret",
                    "base_url": "https://embeddings.example/v1",
                    "model": "embed-model",
                },
            }
        }
    )
    res = upsert_memory_embedding(cfg, provider="openai", api_key="")
    assert res.config.memory.embedding.requested_provider == "openai"
    assert res.config.memory.embedding.remote.api_key == "mem-secret"
    assert res.config.memory.embedding.remote.base_url == "https://embeddings.example/v1"
    assert res.config.memory.embedding.remote.model == "embed-model"


def test_upsert_memory_embedding_auto_without_changes_does_not_require_restart():
    cfg = GatewayConfig()
    res = upsert_memory_embedding(cfg, provider="auto")
    assert res.changed is False
    assert res.restart_required is False


def test_unsupported_provider_rejected():
    cfg = GatewayConfig()
    with pytest.raises(ValueError, match="not runtime-supported"):
        upsert_llm_provider(cfg, provider_id="openai_codex", model="x")


def test_unverified_base_url_provider_rejected_before_configuration():
    cfg = GatewayConfig()
    with pytest.raises(ValueError, match="not runtime-supported"):
        upsert_llm_provider(cfg, provider_id="azure", model="x", api_key="k")


def test_ollama_does_not_require_api_key():
    cfg = GatewayConfig()
    res = upsert_llm_provider(cfg, provider_id="ollama", model="llama3.1")
    assert res.changed is True
    assert res.config.llm.provider == "ollama"


def test_upsert_channel_appends_new():
    cfg = GatewayConfig()
    res = upsert_channel(
        cfg,
        entry_payload={
            "type": "slack",
            "name": "work",
            "token": "xoxb-secret",
            "signing_secret": "ss-secret",
        },
    )
    assert res.restart_required is True
    entries = list_channel_entries(res.config)
    assert len(entries) == 1
    assert entries[0]["name"] == "work"
    assert entries[0]["type"] == "slack"


def test_upsert_channel_updates_same_name():
    cfg = GatewayConfig()
    res1 = upsert_channel(
        cfg,
        entry_payload={
            "type": "slack",
            "name": "work",
            "token": "old",
            "signing_secret": "ss-old",
        },
    )
    res2 = upsert_channel(
        res1.config,
        entry_payload={"type": "slack", "name": "work", "token": "new", "slack_channel_id": "C123"},
    )
    entries = list_channel_entries(res2.config)
    assert len(entries) == 1
    assert entries[0]["slack_channel_id"] == "C123"


def test_upsert_channel_redacts_secrets_in_payload():
    cfg = GatewayConfig()
    res = upsert_channel(
        cfg,
        entry_payload={"type": "telegram", "name": "tg", "token": "abc"},
    )
    assert res.public_payload["token"] == REDACTED_PLACEHOLDER


def test_telegram_edit_preserves_channel_approved_sender_ids() -> None:
    cfg = GatewayConfig()
    created = upsert_channel(
        cfg,
        entry_payload={
            "type": "telegram",
            "name": "tg",
            "token": "abc",
            "access_mode": "approval",
            "approved_sender_ids": ["42"],
        },
    )

    edited = upsert_channel(
        created.config,
        entry_payload={
            "type": "telegram",
            "name": "tg",
            "token": "",
            "access_mode": "approval",
            "poll_timeout_s": 15,
        },
    )

    entry = edited.config.channels.channels[0]
    assert entry.approved_sender_ids == ["42"]
    assert entry.access_mode == "pairing"
    assert entry.poll_timeout_s == 15


def test_slack_webhook_channel_requires_signing_secret():
    cfg = GatewayConfig()
    with pytest.raises(ValueError, match="signing_secret"):
        upsert_channel(
            cfg,
            entry_payload={"type": "slack", "name": "w", "token": "xoxb-test"},
        )


def test_slack_socket_channel_does_not_require_signing_secret():
    cfg = GatewayConfig()
    res = upsert_channel(
        cfg,
        entry_payload={
            "type": "slack",
            "name": "w",
            "token": "xoxb-test",
            "connection_mode": "socket",
            "app_token": "xapp-test",
        },
    )

    entry = list_channel_entries(res.config)[0]
    assert entry["connection_mode"] == "socket"
    assert "signing_secret" not in entry or entry["signing_secret"] in (None, "")


def test_slack_socket_channel_requires_app_token():
    cfg = GatewayConfig()
    with pytest.raises(ValueError, match="app_token"):
        upsert_channel(
            cfg,
            entry_payload={
                "type": "slack",
                "name": "w",
                "token": "xoxb-test",
                "connection_mode": "socket",
            },
        )


def test_remove_channel():
    cfg = GatewayConfig()
    res1 = upsert_channel(
        cfg,
        entry_payload={"type": "slack", "name": "w", "token": "x", "signing_secret": "ss"},
    )
    res2 = remove_channel(res1.config, name="w")
    assert list_channel_entries(res2.config) == []
    assert res2.restart_required is True


def test_remove_missing_channel_raises():
    cfg = GatewayConfig()
    with pytest.raises(KeyError, match="w"):
        remove_channel(cfg, name="w")


def test_set_channel_enabled_toggles():
    cfg = GatewayConfig()
    res1 = upsert_channel(
        cfg,
        entry_payload={"type": "slack", "name": "w", "token": "x", "signing_secret": "ss"},
    )
    res2 = set_channel_enabled(res1.config, name="w", enabled=False)
    assert list_channel_entries(res2.config)[0]["enabled"] is False


def test_invalid_channel_type_rejected():
    cfg = GatewayConfig()
    with pytest.raises(ValueError, match="unknown channel type"):
        upsert_channel(cfg, entry_payload={"type": "nope", "name": "x"})


def test_telegram_webhook_requires_webhook_url():
    cfg = GatewayConfig()
    with pytest.raises(ValueError, match="webhook_url"):
        upsert_channel(
            cfg,
            entry_payload={
                "type": "telegram",
                "name": "t",
                "token": "x",
                "transport_name": "webhook",
            },
        )


def test_upsert_llm_provider_preserves_existing_api_key_on_same_provider():
    cfg = GatewayConfig()
    res1 = upsert_llm_provider(
        cfg,
        provider_id="openrouter",
        model="m1",
        api_key="sk-existing",
        base_url="https://openrouter.ai/api/v1",
    )
    # Reconfigure model only, leaving api_key blank — should reuse existing.
    res2 = upsert_llm_provider(
        res1.config,
        provider_id="openrouter",
        model="m2",
        api_key="",
    )
    assert res2.config.llm.api_key == "sk-existing"
    assert res2.config.llm.model == "m2"


def test_upsert_llm_provider_can_use_env_key_without_secret():
    cfg = GatewayConfig()
    res = upsert_llm_provider(
        cfg,
        provider_id="openrouter",
        model="deepseek/deepseek-v4-flash",
        api_key="",
        api_key_env="OPENROUTER_API_KEY",
    )

    assert res.config.llm.api_key == ""
    assert res.config.llm.api_key_env == "OPENROUTER_API_KEY"
    assert res.public_payload["api_key_source"] == "env"


def test_upsert_llm_provider_recomputes_openrouter_mix_on_provider_switch():
    cfg = GatewayConfig(llm={"provider": "openrouter", "model": "deepseek/x"})
    assert cfg.agentos_router.enabled is True
    # openrouter is now the baked-in default tier set, so a direct openrouter
    # provider does NOT auto-select a tier_profile (it is the skip case); the
    # profile stays unset and the default openrouter tiers apply.
    assert cfg.agentos_router.tier_profile is None

    res = upsert_llm_provider(
        cfg,
        provider_id="deepseek",
        model="deepseek-chat",
        api_key_env="DEEPSEEK_API_KEY",
    )

    assert res.config.llm.provider == "deepseek"
    assert res.config.agentos_router.enabled is True
    assert res.config.agentos_router.tier_profile == "deepseek"
    assert res.config.agentos_router.tiers["c0"]["provider"] == "deepseek"
    assert "tiers" not in res.config.to_toml_dict()["agentos_router"]


def test_upsert_router_recommended_writes_profile_without_expanded_tiers():
    cfg = GatewayConfig(llm={"provider": "deepseek", "model": "deepseek-chat"})

    res = upsert_router(cfg, mode="recommended")

    assert res.config.agentos_router.enabled is True
    assert res.config.agentos_router.tier_profile == "deepseek"
    assert "tiers" not in res.config.to_toml_dict()["agentos_router"]
    assert res.public_payload["mode"] == "recommended"


def test_upsert_router_forces_image_model_role_invariants():
    cfg = GatewayConfig(llm={"provider": "openrouter", "model": "z-ai/glm-5.1"})

    res = upsert_router(
        cfg,
        mode="openrouter-mix",
        tiers={
            "image_model": {
                "provider": "openrouter",
                "model": "anthropic/claude-opus-4.7",
                "supportsImage": False,
                "image_only": False,
            }
        },
    )

    image_tier = res.config.agentos_router.tiers["image_model"]
    assert image_tier["model"] == "anthropic/claude-opus-4.7"
    assert image_tier["supports_image"] is True
    assert image_tier["image_only"] is True


def test_upsert_router_can_disable():
    cfg = GatewayConfig(llm={"provider": "openrouter", "model": "deepseek/x"})

    res = upsert_router(cfg, mode="disabled")

    assert res.config.agentos_router.enabled is False
    assert res.config.agentos_router.tier_profile is None
    assert res.public_payload["mode"] == "disabled"


def test_upsert_router_rejects_openrouter_mix_for_direct_provider():
    cfg = GatewayConfig(llm={"provider": "deepseek", "model": "deepseek-chat"})

    with pytest.raises(ValueError, match="openrouter-mix"):
        upsert_router(cfg, mode="openrouter-mix")


def test_upsert_router_auto_judge_persists_nothing_and_echoes_resolution():
    cfg = GatewayConfig(llm={"provider": "deepseek", "model": "deepseek-chat"})

    res = upsert_router(cfg, mode="recommended", judge_model="auto")

    router = res.config.agentos_router
    assert router.judge_model is None
    assert router.judge_provider is None
    judge = res.public_payload["judge"]
    assert judge["judge_model"] is None
    assert judge["source"] == "auto"
    assert judge["resolved_provider"] == "deepseek"
    assert judge["resolved_model"] == router.tiers["c0"]["model"]


def test_upsert_router_manual_judge_model_is_persisted_and_echoed():
    cfg = GatewayConfig(llm={"provider": "deepseek", "model": "deepseek-chat"})

    res = upsert_router(cfg, mode="recommended", judge_model="deepseek-v4-pro")

    router = res.config.agentos_router
    assert router.judge_model == "deepseek-v4-pro"
    assert router.judge_provider is None
    judge = res.public_payload["judge"]
    assert judge["judge_model"] == "deepseek-v4-pro"
    assert judge["source"] == "explicit"
    assert judge["resolved_model"] == "deepseek-v4-pro"
    assert judge["resolved_provider"] == "deepseek"


def test_upsert_router_auto_judge_clears_previous_manual_pick():
    cfg = GatewayConfig(llm={"provider": "deepseek", "model": "deepseek-chat"})
    cfg = upsert_router(cfg, mode="recommended", judge_model="deepseek-v4-pro").config
    assert cfg.agentos_router.judge_model == "deepseek-v4-pro"

    res = upsert_router(cfg, mode="recommended", judge_model="auto")

    assert res.config.agentos_router.judge_model is None
    assert res.config.agentos_router.judge_provider is None


def test_upsert_router_omitted_judge_params_preserve_existing_values():
    cfg = GatewayConfig(llm={"provider": "deepseek", "model": "deepseek-chat"})
    cfg = upsert_router(cfg, mode="recommended", judge_model="deepseek-v4-pro").config

    res = upsert_router(cfg, mode="recommended")

    assert res.config.agentos_router.judge_model == "deepseek-v4-pro"


def test_upsert_router_omitted_judge_params_preserve_existing_local_endpoint():
    # WebUI Router-step reproduction: a local judge is configured via the CLI
    # (base_url + model + api_key persisted). The operator later opens the WebUI
    # setup and clicks Save on the router step WITHOUT touching the judge
    # dropdown. The dropdown lists only the profile's cloud text-tier models, so
    # the local model has no option and the WebUI must send judge_model=None
    # (preserve) — NOT '' (which would clear to AUTO and wipe the endpoint,
    # degrading every judged turn to judge_unavailable). judge_model=None here
    # must leave the whole local endpoint intact.
    cfg = GatewayConfig(llm={"provider": "deepseek", "model": "deepseek-chat"})
    cfg = upsert_router(
        cfg,
        mode="recommended",
        judge_model="llama3",
        judge_base_url="http://localhost:11434/v1",
        judge_api_key="sk-local",
    ).config
    assert cfg.agentos_router.judge_base_url == "http://localhost:11434/v1"

    res = upsert_router(cfg, mode="recommended")  # judge params omitted -> None

    router = res.config.agentos_router
    assert router.judge_model == "llama3"
    assert router.judge_base_url == "http://localhost:11434/v1"
    assert router.judge_api_key == "sk-local"
    assert router.judge_provider is None


def test_upsert_router_rejects_cross_provider_judge_without_credentials():
    cfg = GatewayConfig(llm={"provider": "deepseek", "model": "deepseek-chat"})

    with pytest.raises(ValueError, match="judge_provider"):
        upsert_router(
            cfg,
            mode="recommended",
            judge_model="gpt-5.4-nano",
            judge_provider="openai",
        )


def test_upsert_router_local_judge_persists_base_url_and_bypasses_provider_match():
    cfg = GatewayConfig(llm={"provider": "deepseek", "model": "deepseek-chat"})

    res = upsert_router(
        cfg,
        mode="recommended",
        judge_model="llama3",
        judge_base_url="http://localhost:11434/v1",
        judge_api_key="sk-local",
    )

    router = res.config.agentos_router
    assert router.judge_model == "llama3"
    assert router.judge_base_url == "http://localhost:11434/v1"
    assert router.judge_api_key == "sk-local"
    # A local endpoint never persists a cloud judge_provider.
    assert router.judge_provider is None
    judge = res.public_payload["judge"]
    assert judge["judge_base_url"] == "http://localhost:11434/v1"
    assert judge["source"] == "local"


def test_upsert_router_local_judge_rejects_malformed_base_url():
    cfg = GatewayConfig(llm={"provider": "deepseek", "model": "deepseek-chat"})

    with pytest.raises(ValueError, match="judge_base_url"):
        upsert_router(
            cfg,
            mode="recommended",
            judge_model="llama3",
            judge_base_url="localhost:11434",  # missing scheme
        )


def test_upsert_router_auto_judge_clears_previous_local_endpoint():
    cfg = GatewayConfig(llm={"provider": "deepseek", "model": "deepseek-chat"})
    cfg = upsert_router(
        cfg,
        mode="recommended",
        judge_model="llama3",
        judge_base_url="http://localhost:11434/v1",
        judge_api_key="sk-local",
    ).config
    assert cfg.agentos_router.judge_base_url == "http://localhost:11434/v1"

    res = upsert_router(cfg, mode="recommended", judge_model="auto")

    assert res.config.agentos_router.judge_model is None
    assert res.config.agentos_router.judge_base_url is None
    assert res.config.agentos_router.judge_api_key is None


def test_upsert_router_cloud_judge_clears_stale_local_endpoint_when_base_url_omitted():
    # WebUI Router step reproduction: a local judge was first configured via the
    # CLI (persisting judge_base_url/judge_api_key), then the operator picks a
    # cloud tier model from the judge dropdown. The WebUI RPC never sends the
    # local-endpoint fields, so judge_base_url arrives as None. The stale
    # base_url must still be cleared, otherwise resolve_judge_target classifies
    # the cloud model as a local endpoint and every judged turn degrades to
    # judge_unavailable.
    cfg = GatewayConfig(llm={"provider": "deepseek", "model": "deepseek-chat"})
    cfg = upsert_router(
        cfg,
        mode="recommended",
        judge_model="qwen2.5:7b",
        judge_base_url="http://localhost:11434/v1",
        judge_api_key="sk-local",
    ).config
    assert cfg.agentos_router.judge_base_url == "http://localhost:11434/v1"

    # Cloud pick with judge_base_url/judge_api_key omitted (WebUI behavior).
    res = upsert_router(cfg, mode="recommended", judge_model="deepseek-v4-flash")

    router = res.config.agentos_router
    assert router.judge_model == "deepseek-v4-flash"
    assert router.judge_base_url is None
    assert router.judge_api_key is None
    judge = res.public_payload["judge"]
    # No stale base_url means the judge resolves as a cloud target, not local.
    assert judge["source"] == "explicit"
    assert judge["resolved_provider"] == "deepseek"


def test_upsert_router_matching_judge_provider_is_accepted():
    cfg = GatewayConfig(llm={"provider": "deepseek", "model": "deepseek-chat"})

    res = upsert_router(
        cfg,
        mode="recommended",
        judge_model="deepseek-v4-flash",
        judge_provider="deepseek",
    )

    assert res.config.agentos_router.judge_model == "deepseek-v4-flash"
    assert res.config.agentos_router.judge_provider == "deepseek"


def test_upsert_router_stale_cross_provider_judge_resets_to_auto_with_warning():
    cfg = GatewayConfig(llm={"provider": "deepseek", "model": "deepseek-chat"})
    cfg = upsert_router(
        cfg,
        mode="recommended",
        judge_model="deepseek-v4-flash",
        judge_provider="deepseek",
    ).config

    # The provider switch is where the stale judge is introduced, so it is where
    # the reset + warning now fire (the reconcile guards it directly).
    switch_res = upsert_llm_provider(
        cfg,
        provider_id="openai",
        model="gpt-5.5",
        api_key_env="OPENAI_API_KEY",
    )
    assert switch_res.config.agentos_router.judge_model is None
    assert switch_res.config.agentos_router.judge_provider is None
    assert any("judge" in warning for warning in switch_res.warnings)

    # By the time a subsequent router upsert runs, the judge is already AUTO.
    res = upsert_router(switch_res.config, mode="recommended")
    assert res.config.agentos_router.judge_model is None
    assert res.config.agentos_router.judge_provider is None


def test_upsert_router_disabled_ignores_judge_params():
    cfg = GatewayConfig(llm={"provider": "deepseek", "model": "deepseek-chat"})

    res = upsert_router(cfg, mode="disabled", judge_model="deepseek-v4-pro")

    assert res.config.agentos_router.judge_model is None
    assert "judge" not in res.public_payload


def test_provider_switch_resets_stale_cross_provider_judge_to_auto():
    # An explicitly-pinned judge on the previous provider is carried through
    # verbatim by the provider-switch reconcile. It no longer matches the new
    # llm.provider and has no credential source, so every judged turn would
    # silently degrade to judge_unavailable. The reconcile must reset it to AUTO.
    cfg = GatewayConfig(llm={"provider": "deepseek", "model": "deepseek-chat"})
    cfg = upsert_router(
        cfg,
        mode="recommended",
        judge_model="deepseek-v4-flash",
        judge_provider="deepseek",
    ).config
    assert cfg.agentos_router.judge_provider == "deepseek"

    # Switch the LLM provider directly (the CLI/RPC provider step) — no
    # subsequent upsert_router runs, so the reconcile is the only guard.
    switch_res = upsert_llm_provider(
        cfg,
        provider_id="openai",
        model="gpt-5.5",
        api_key_env="OPENAI_API_KEY",
    )

    router = switch_res.config.agentos_router
    assert router.tier_profile == "openai"
    assert router.judge_model is None
    assert router.judge_provider is None
    # The silent degradation is now surfaced at the step that introduced it.
    assert any("judge" in warning for warning in switch_res.warnings)


def test_provider_switch_preserves_local_endpoint_judge():
    # A local-endpoint judge carries its own credentials and does not depend on
    # llm.provider, so a provider switch must NOT reset it.
    cfg = GatewayConfig(llm={"provider": "deepseek", "model": "deepseek-chat"})
    cfg = upsert_router(
        cfg,
        mode="recommended",
        judge_model="llama3",
        judge_base_url="http://localhost:11434/v1",
        judge_api_key="sk-local",
    ).config
    assert cfg.agentos_router.judge_base_url == "http://localhost:11434/v1"

    switched = upsert_llm_provider(
        cfg,
        provider_id="openai",
        model="gpt-5.5",
        api_key_env="OPENAI_API_KEY",
    ).config

    router = switched.agentos_router
    assert router.judge_model == "llama3"
    assert router.judge_base_url == "http://localhost:11434/v1"
    assert router.judge_api_key == "sk-local"
    assert router.judge_provider is None


def test_provider_switch_preserves_matching_explicit_judge():
    # A pinned judge that still matches the NEW provider must survive the switch
    # (the reset is only for genuinely stale cross-provider judges).
    cfg = GatewayConfig(llm={"provider": "deepseek", "model": "deepseek-chat"})
    cfg = upsert_router(
        cfg,
        mode="recommended",
        judge_model="deepseek-v4-flash",
        judge_provider="deepseek",
    ).config

    # Re-configure the SAME provider (e.g. rotate the api key). tier_profile
    # already matches, so reconcile returns early — the judge is untouched.
    same = upsert_llm_provider(
        cfg,
        provider_id="deepseek",
        model="deepseek-chat",
        api_key="sk-new",
    ).config

    assert same.agentos_router.judge_model == "deepseek-v4-flash"
    assert same.agentos_router.judge_provider == "deepseek"


def test_upsert_router_local_judge_verifies_connectivity_and_rejects_unreachable(
    monkeypatch,
):
    # Finding D2: the non-CLI (WebUI/RPC) path must not silently persist an
    # unreachable/wrong-model local endpoint. With verify_local_endpoint=True a
    # failing probe raises rather than persisting a judge that would degrade to
    # judge_unavailable on every turn.
    import agentos.agentos_router.llm_judge as judge_mod

    monkeypatch.setattr(
        judge_mod,
        "probe_local_judge",
        lambda base_url, model, api_key: "connection refused",
    )
    cfg = GatewayConfig(llm={"provider": "deepseek", "model": "deepseek-chat"})

    with pytest.raises(ValueError, match="not usable"):
        upsert_router(
            cfg,
            mode="recommended",
            judge_model="llama3",
            judge_base_url="http://localhost:11434/v1",
            judge_api_key="sk-local",
            verify_local_endpoint=True,
        )


def test_upsert_router_local_judge_persists_when_probe_succeeds(monkeypatch):
    import agentos.agentos_router.llm_judge as judge_mod

    calls: list[tuple[str, str, str | None]] = []

    def _fake_probe(base_url, model, api_key):
        calls.append((base_url, model, api_key))
        return None

    monkeypatch.setattr(judge_mod, "probe_local_judge", _fake_probe)
    cfg = GatewayConfig(llm={"provider": "deepseek", "model": "deepseek-chat"})

    res = upsert_router(
        cfg,
        mode="recommended",
        judge_model="llama3",
        judge_base_url="http://localhost:11434/v1",
        judge_api_key="sk-local",
        verify_local_endpoint=True,
    )

    assert calls == [("http://localhost:11434/v1", "llama3", "sk-local")]
    assert res.config.agentos_router.judge_base_url == "http://localhost:11434/v1"


def test_upsert_router_local_judge_does_not_probe_by_default(monkeypatch):
    # Pure-config callers (the default) must not trigger a network probe.
    import agentos.agentos_router.llm_judge as judge_mod

    def _boom(*_args, **_kwargs):
        raise AssertionError("probe_local_judge must not run without opt-in")

    monkeypatch.setattr(judge_mod, "probe_local_judge", _boom)
    cfg = GatewayConfig(llm={"provider": "deepseek", "model": "deepseek-chat"})

    res = upsert_router(
        cfg,
        mode="recommended",
        judge_model="llama3",
        judge_base_url="http://localhost:11434/v1",
        judge_api_key="sk-local",
    )

    assert res.config.agentos_router.judge_base_url == "http://localhost:11434/v1"


def test_upsert_llm_provider_keeps_runtime_secret_marker_when_reusing_key():
    cfg = GatewayConfig()
    cfg.llm.provider = "openrouter"
    cfg.llm.model = "m1"
    cfg.llm.api_key = "from-env"
    cfg.mark_runtime_secret("llm.api_key")

    res = upsert_llm_provider(
        cfg,
        provider_id="openrouter",
        model="m2",
        api_key="",
    )

    assert res.config.llm.api_key == "from-env"
    assert "llm.api_key" in res.config._runtime_secret_paths


def test_upsert_llm_provider_clears_runtime_secret_marker_for_explicit_key():
    cfg = GatewayConfig()
    cfg.llm.provider = "openrouter"
    cfg.llm.model = "m1"
    cfg.llm.api_key = "from-env"
    cfg.mark_runtime_secret("llm.api_key")

    res = upsert_llm_provider(
        cfg,
        provider_id="openrouter",
        model="m2",
        api_key="sk-written",
    )

    assert res.config.llm.api_key == "sk-written"
    assert "llm.api_key" not in res.config._runtime_secret_paths


def test_upsert_llm_provider_explicit_key_clears_existing_env_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-env")
    cfg = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "m1",
            "api_key": "",
            "api_key_env": "OPENROUTER_API_KEY",
        }
    )

    res = upsert_llm_provider(
        cfg,
        provider_id="openrouter",
        model="m2",
        api_key="sk-written",
    )
    runtime = resolve_llm_runtime_config(res.config)

    assert res.config.llm.api_key_env == ""
    assert runtime.api_key == "sk-written"
    assert runtime.api_key_from_env is False


def test_upsert_llm_provider_rejects_ambiguous_key_sources():
    cfg = GatewayConfig()

    with pytest.raises(ValueError, match="either api_key or api_key_env"):
        upsert_llm_provider(
            cfg,
            provider_id="openrouter",
            model="m",
            api_key="sk-written",
            api_key_env="OPENROUTER_API_KEY",
        )


def test_upsert_llm_provider_does_not_carry_key_across_providers():
    cfg = GatewayConfig()
    res1 = upsert_llm_provider(
        cfg,
        provider_id="openrouter",
        model="m",
        api_key="sk-or",
    )
    # Switching to a different key-required provider must require a new key.
    with pytest.raises(ValueError, match="api_key"):
        upsert_llm_provider(
            res1.config,
            provider_id="openai",
            model="gpt-4o",
            api_key="",
        )


def test_validate_channel_entry_returns_normalized_payload():
    out = validate_channel_entry(
        {"type": "slack", "name": "w", "token": "x", "signing_secret": "ss"}
    )
    assert out["type"] == "slack"
    assert out["enabled"] is True
    assert out["agent_id"] == "main"


def test_upsert_search_provider_configures_brave():
    cfg = GatewayConfig()
    res = upsert_search_provider(
        cfg,
        provider_id="brave",
        api_key="brave-key",
        max_results=7,
        proxy="http://127.0.0.1:7890",
        use_env_proxy=True,
        fallback_policy="network",
        diagnostics=True,
    )
    assert res.config.search_provider == "brave"
    assert res.config.search_api_key == "brave-key"
    assert res.config.search_max_results == 7
    assert res.config.search_proxy == "http://127.0.0.1:7890"
    assert res.config.search_use_env_proxy is True
    assert res.config.search_fallback_policy == "network"
    assert res.config.search_diagnostics is True
    assert res.public_payload["api_key"] == REDACTED_PLACEHOLDER


def test_upsert_search_provider_strips_trailing_paste_punctuation_from_api_key():
    cfg = GatewayConfig()
    res = upsert_search_provider(cfg, provider_id="brave", api_key="brave-key、")

    assert res.config.search_api_key == "brave-key"


def test_upsert_search_provider_can_use_env_key_reference():
    cfg = GatewayConfig()
    res = upsert_search_provider(
        cfg,
        provider_id="brave",
        api_key="",
        api_key_env="BRAVE_SEARCH_API_KEY",
    )
    assert res.config.search_provider == "brave"
    assert res.config.search_api_key == ""
    assert res.config.search_api_key_env == "BRAVE_SEARCH_API_KEY"
    assert res.public_payload["api_key_source"] == "env"


def test_upsert_search_provider_accepts_webui_string_max_results():
    cfg = GatewayConfig()
    res = upsert_search_provider(
        cfg,
        provider_id="duckduckgo",
        max_results="5",
    )

    assert res.config.search_provider == "duckduckgo"
    assert res.config.search_max_results == 5


def test_upsert_search_provider_clears_env_key_for_no_key_provider():
    cfg = GatewayConfig(search_provider="brave", search_api_key_env="BRAVE_SEARCH_API_KEY")

    res = upsert_search_provider(
        cfg,
        provider_id="duckduckgo",
        api_key_env="BRAVE_SEARCH_API_KEY",
    )

    assert res.config.search_provider == "duckduckgo"
    assert res.config.search_api_key_env == ""
    assert res.public_payload["api_key_source"] == "none"


def test_upsert_image_generation_provider_configures_openrouter(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = GatewayConfig()
    res = upsert_image_generation_provider(
        cfg,
        provider_id="openrouter",
        primary="openrouter/google/gemini-3.1-flash-image-preview",
        api_key="sk-or",
    )
    assert res.config.image_generation.enabled is True
    assert res.config.image_generation.primary == "openrouter/google/gemini-3.1-flash-image-preview"
    assert res.config.image_generation.providers.openrouter.api_key == "sk-or"
    assert res.public_payload["api_key"] == REDACTED_PLACEHOLDER
    assert res.public_payload["api_key_source"] == "explicit"


def test_upsert_image_generation_provider_can_use_matching_llm_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = GatewayConfig()
    cfg.llm.provider = "openrouter"
    cfg.llm.api_key = "sk-llm"
    res = upsert_image_generation_provider(cfg, provider_id="openrouter")
    assert res.config.image_generation.enabled is True
    assert res.config.image_generation.providers.openrouter.api_key == ""
    assert res.public_payload["api_key_source"] == "llm_fallback"


def test_upsert_image_generation_provider_can_disable_without_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = GatewayConfig()

    res = upsert_image_generation_provider(
        cfg,
        provider_id="openrouter",
        primary="openrouter/google/gemini-3.1-flash-image-preview",
        enabled=False,
    )

    assert res.config.image_generation.enabled is False
    assert res.config.image_generation.primary == "openrouter/google/gemini-3.1-flash-image-preview"
    assert res.public_payload["api_key_source"] == "none"


def test_upsert_image_generation_provider_rejects_wrong_primary_provider():
    cfg = GatewayConfig()
    cfg.llm.provider = "openrouter"
    cfg.llm.api_key = "sk-llm"
    with pytest.raises(ValueError, match="provider/model"):
        upsert_image_generation_provider(
            cfg,
            provider_id="openrouter",
            primary="openai/gpt-image-1",
        )


def test_search_provider_requiring_key_can_reuse_existing_key():
    cfg = GatewayConfig(search_provider="brave", search_api_key="old")
    res = upsert_search_provider(cfg, provider_id="brave", api_key="")
    assert res.config.search_api_key == "old"


def test_search_provider_requiring_key_rejects_missing_key():
    cfg = GatewayConfig()
    with pytest.raises(ValueError, match="api_key"):
        upsert_search_provider(cfg, provider_id="brave", api_key="", api_key_env="")


def test_unsupported_search_provider_rejected():
    cfg = GatewayConfig()
    with pytest.raises(ValueError, match="not runtime-supported"):
        upsert_search_provider(cfg, provider_id="tavily", api_key="k")


def test_upsert_channel_preserves_secret_when_blank():
    cfg = GatewayConfig()
    first = upsert_channel(
        cfg,
        entry_payload={
            "type": "slack",
            "name": "w",
            "token": "xoxb-original",
            "signing_secret": "ss-original",
        },
    )
    second = upsert_channel(
        first.config,
        entry_payload={
            "type": "slack",
            "name": "w",
            "token": "",  # blank = keep current
            "signing_secret": "",
            "slack_channel_id": "C999",
        },
    )
    raw = [e.model_dump(mode="python") for e in second.config.channels.channels]
    entry = next(e for e in raw if e["name"] == "w")
    assert entry["token"] == "xoxb-original"
    assert entry["signing_secret"] == "ss-original"
    assert entry["slack_channel_id"] == "C999"


def test_upsert_channel_replaces_secret_when_provided():
    cfg = GatewayConfig()
    first = upsert_channel(
        cfg,
        entry_payload={
            "type": "slack",
            "name": "w",
            "token": "xoxb-old",
            "signing_secret": "ss-old",
        },
    )
    second = upsert_channel(
        first.config,
        entry_payload={"type": "slack", "name": "w", "token": "xoxb-new"},
    )
    raw = [e.model_dump(mode="python") for e in second.config.channels.channels]
    entry = next(e for e in raw if e["name"] == "w")
    assert entry["token"] == "xoxb-new"
