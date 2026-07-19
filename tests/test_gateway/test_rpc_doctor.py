from __future__ import annotations

from typing import Any

import pytest

from agentos.gateway.config import GatewayConfig
from agentos.gateway.rpc import RpcContext, get_dispatcher
from agentos.gateway.scopes import METHOD_SCOPES, READ_SCOPE


async def _ready_memory(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
    return {"backend": "sqlite", "status": "ok", "pendingRepairCount": 0}


async def _ready_channels(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
    return {
        "channels": [
            {
                "name": "slack-main",
                "type": "slack",
                "enabled": True,
                "status": "connected",
            }
        ]
    }


async def _ready_search(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
    return {
        "provider": "duckduckgo",
        "activeProvider": "duckduckgo",
        "configured": True,
        "runtimeSupported": True,
        "requiresApiKey": False,
        "apiKeyConfigured": False,
        "buildable": True,
    }


def _ready_logs(ctx: RpcContext) -> dict[str, Any]:
    return {
        "gateway_file_log": {
            "enabled": True,
            "path": "/tmp/agentos-debug.log",
            "exists": True,
            "active_tail_path_exists": True,
        },
        "raw_turn_call_log": {"enabled": False},
        "diagnostics_enabled": {"effective": False},
    }


def _disabled_file_logs(ctx: RpcContext) -> dict[str, Any]:
    return {
        "gateway_file_log": {
            "enabled": False,
            "path": "/tmp/agentos-debug.log",
            "exists": False,
        }
    }


def _optional_image_generation(ctx: RpcContext) -> dict[str, Any]:
    return {
        "enabled": False,
        "configured": False,
        "status": "optional",
        "provider": "",
        "primary": "openai/gpt-image-1",
        "source": "none",
    }


def _patch_ready_support_surfaces(monkeypatch: pytest.MonkeyPatch, rpc_doctor: Any) -> None:
    monkeypatch.setattr(rpc_doctor, "_handle_doctor_memory_status", _ready_memory)
    monkeypatch.setattr(rpc_doctor, "_handle_channels_status", _ready_channels)
    monkeypatch.setattr(rpc_doctor, "_handle_search_status", _ready_search)
    monkeypatch.setattr(rpc_doctor, "_build_logs_status", _ready_logs)
    monkeypatch.setattr(
        rpc_doctor,
        "_image_generation_payload",
        _optional_image_generation,
    )


@pytest.mark.asyncio
async def test_doctor_status_is_read_scoped() -> None:
    assert METHOD_SCOPES["doctor.status"] == READ_SCOPE


@pytest.mark.asyncio
async def test_doctor_status_combines_runtime_findings(monkeypatch) -> None:
    import agentos.gateway.rpc_doctor as rpc_doctor

    async def provider_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        return {
            "activeProvider": "openrouter",
            "providers": [
                {
                    "providerId": "openrouter",
                    "active": True,
                    "configured": False,
                    "buildable": True,
                    "apiKeyConfigured": False,
                }
            ],
        }

    monkeypatch.setattr(rpc_doctor, "_handle_providers_status", provider_status)
    _patch_ready_support_surfaces(monkeypatch, rpc_doctor)

    cfg = GatewayConfig()
    cfg.config_path = "/tmp/custom-agentos.toml"
    ctx = RpcContext(conn_id="test", config=cfg)
    response = await get_dispatcher().dispatch("req-1", "doctor.status", {}, ctx)

    assert response.ok is True
    assert response.payload["configPath"] == "/tmp/custom-agentos.toml"
    assert response.payload["status"] == "action_required"
    assert response.payload["ready"] is False
    ids = [finding["id"] for finding in response.payload["findings"]]
    assert "gateway.rpc.ready" in ids
    assert "provider.active.not_configured" in ids
    provider_finding = next(
        finding
        for finding in response.payload["findings"]
        if finding["id"] == "provider.active.not_configured"
    )
    commands = [step["command"] for step in provider_finding["fixSteps"] if "command" in step]
    assert (
        "agentos providers configure openrouter --api-key YOUR_API_KEY "
        "--config /tmp/custom-agentos.toml"
    ) in commands
    assert "agentos gateway restart --config /tmp/custom-agentos.toml" in commands
    assert response.payload["agentId"] == "main"


@pytest.mark.asyncio
async def test_doctor_status_scopes_config_set_recovery_commands(monkeypatch) -> None:
    import agentos.gateway.rpc_doctor as rpc_doctor

    async def provider_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        return {
            "activeProvider": "openrouter",
            "providers": [
                {
                    "providerId": "openrouter",
                    "active": True,
                    "configured": True,
                    "buildable": True,
                }
            ],
        }

    monkeypatch.setattr(rpc_doctor, "_handle_providers_status", provider_status)
    _patch_ready_support_surfaces(monkeypatch, rpc_doctor)
    monkeypatch.setattr(rpc_doctor, "_build_logs_status", _disabled_file_logs)

    cfg = GatewayConfig()
    cfg.config_path = "/tmp/custom-agentos.toml"
    response = await get_dispatcher().dispatch(
        "req-1",
        "doctor.status",
        {},
        RpcContext(conn_id="test", config=cfg),
    )

    assert response.ok is True
    finding = next(
        finding
        for finding in response.payload["findings"]
        if finding["id"] == "logs.gateway_file_log.disabled"
    )
    commands = [step["command"] for step in finding["fixSteps"] if "command" in step]
    assert (
        "agentos config set log_file_enabled true "
        "--config /tmp/custom-agentos.toml"
    ) in commands
    assert "agentos gateway restart --config /tmp/custom-agentos.toml" in commands


@pytest.mark.asyncio
async def test_doctor_status_includes_search_and_image_generation_findings(
    monkeypatch,
) -> None:
    import agentos.gateway.rpc_doctor as rpc_doctor

    async def provider_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        return {
            "activeProvider": "openrouter",
            "providers": [
                {
                    "providerId": "openrouter",
                    "active": True,
                    "configured": True,
                    "buildable": True,
                }
            ],
        }

    async def search_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        return {
            "provider": "brave",
            "activeProvider": "brave",
            "configured": False,
            "runtimeSupported": True,
            "requiresApiKey": True,
            "apiKeyConfigured": False,
            "buildable": False,
            "fallbackPolicy": "off",
        }

    def image_generation_payload(ctx: RpcContext) -> dict[str, Any]:
        return {
            "enabled": True,
            "configured": False,
            "status": "missing",
            "provider": "openai",
            "primary": "openai/gpt-image-1",
            "source": "none",
        }

    monkeypatch.setattr(rpc_doctor, "_handle_providers_status", provider_status)
    monkeypatch.setattr(rpc_doctor, "_handle_doctor_memory_status", _ready_memory)
    monkeypatch.setattr(rpc_doctor, "_handle_channels_status", _ready_channels)
    monkeypatch.setattr(rpc_doctor, "_handle_search_status", search_status)
    monkeypatch.setattr(rpc_doctor, "_build_logs_status", _ready_logs)
    monkeypatch.setattr(rpc_doctor, "_image_generation_payload", image_generation_payload)

    response = await get_dispatcher().dispatch(
        "req-1",
        "doctor.status",
        {},
        RpcContext(
            conn_id="test",
            config=GatewayConfig(
                search_provider="brave",
                search_api_key_env="CUSTOM_SEARCH_KEY",
            ),
        ),
    )

    assert response.ok is True
    assert response.payload["status"] == "degraded"
    findings = response.payload["findings"]
    ids = [finding["id"] for finding in findings]
    assert "search.provider.not_configured" in ids
    assert "image_generation.credentials.missing" in ids
    search_finding = next(
        finding for finding in findings if finding["id"] == "search.provider.not_configured"
    )
    assert search_finding["evidence"]["apiKeyEnv"] == "CUSTOM_SEARCH_KEY"
    assert "CUSTOM_SEARCH_KEY" in search_finding["detail"]


@pytest.mark.asyncio
async def test_doctor_status_explains_missing_image_generation_env_key(
    monkeypatch,
) -> None:
    import agentos.gateway.rpc_doctor as rpc_doctor

    async def provider_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        return {
            "activeProvider": "openrouter",
            "providers": [
                {
                    "providerId": "openrouter",
                    "active": True,
                    "configured": True,
                    "buildable": True,
                }
            ],
        }

    monkeypatch.delenv("CUSTOM_IMAGE_KEY", raising=False)
    monkeypatch.setattr(rpc_doctor, "_handle_providers_status", provider_status)
    monkeypatch.setattr(rpc_doctor, "_handle_doctor_memory_status", _ready_memory)
    monkeypatch.setattr(rpc_doctor, "_handle_channels_status", _ready_channels)
    monkeypatch.setattr(rpc_doctor, "_handle_search_status", _ready_search)
    monkeypatch.setattr(rpc_doctor, "_build_logs_status", _ready_logs)

    response = await get_dispatcher().dispatch(
        "req-1",
        "doctor.status",
        {},
        RpcContext(
            conn_id="test",
            config=GatewayConfig(
                image_generation={
                    "enabled": True,
                    "primary": "openrouter/google/gemini-3.1-flash-image-preview",
                    "providers": {
                        "openrouter": {
                            "api_key": "",
                            "api_key_env": "CUSTOM_IMAGE_KEY",
                        }
                    },
                }
            ),
        ),
    )

    assert response.ok is True
    finding = next(
        finding
        for finding in response.payload["findings"]
        if finding["id"] == "image_generation.credentials.missing"
    )
    assert finding["evidence"]["apiKeyEnv"] == "CUSTOM_IMAGE_KEY"
    assert "CUSTOM_IMAGE_KEY" in finding["detail"]


@pytest.mark.asyncio
async def test_doctor_status_reports_unknown_search_provider_as_reconfigurable(
    monkeypatch,
) -> None:
    import agentos.gateway.rpc_doctor as rpc_doctor

    async def provider_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        return {
            "activeProvider": "openrouter",
            "providers": [
                {
                    "providerId": "openrouter",
                    "active": True,
                    "configured": True,
                    "buildable": True,
                }
            ],
        }

    async def search_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        raise ValueError("Unknown search provider 'serpapi'. Available: brave, duckduckgo")

    monkeypatch.setattr(rpc_doctor, "_handle_providers_status", provider_status)
    monkeypatch.setattr(rpc_doctor, "_handle_doctor_memory_status", _ready_memory)
    monkeypatch.setattr(rpc_doctor, "_handle_channels_status", _ready_channels)
    monkeypatch.setattr(rpc_doctor, "_handle_search_status", search_status)
    monkeypatch.setattr(rpc_doctor, "_build_logs_status", _ready_logs)
    monkeypatch.setattr(
        rpc_doctor,
        "_image_generation_payload",
        _optional_image_generation,
    )

    cfg = GatewayConfig()
    cfg.config_path = "/tmp/custom-agentos.toml"

    response = await get_dispatcher().dispatch(
        "req-1",
        "doctor.status",
        {},
        RpcContext(conn_id="test", config=cfg),
    )

    assert response.ok is True
    search_finding = next(
        finding
        for finding in response.payload["findings"]
        if finding["surface"] == "search"
    )
    assert search_finding["id"] == "search.provider.unknown"
    commands = [step["command"] for step in search_finding["fixSteps"]]
    assert "agentos search list --json" in commands
    assert (
        "agentos configure search --search-provider duckduckgo "
        "--config /tmp/custom-agentos.toml"
    ) in commands


@pytest.mark.asyncio
async def test_doctor_status_includes_router_and_memory_embedding_findings(
    monkeypatch,
) -> None:
    import agentos.gateway.rpc_doctor as rpc_doctor

    async def provider_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        return {
            "activeProvider": "openrouter",
            "providers": [
                {
                    "providerId": "openrouter",
                    "active": True,
                    "configured": True,
                    "buildable": True,
                }
            ],
        }

    def router_payload(ctx: RpcContext) -> dict[str, Any]:
        return {
            "enabled": True,
            "rolloutPhase": "full",
            "strategy": "llm_judge",
            "tierProfile": "openrouter",
            "runtimeValid": False,
            "error": "judge target could not be resolved",
        }

    def memory_embedding_payload(ctx: RpcContext) -> dict[str, Any]:
        return {
            "status": "error",
            "requestedProvider": "openai",
            "effectiveProvider": "none",
            "model": "text-embedding-3-small",
            "retrievalMode": "hybrid",
            "error": "memory.embedding.remote.api_key is required",
        }

    monkeypatch.setattr(rpc_doctor, "_handle_providers_status", provider_status)
    _patch_ready_support_surfaces(monkeypatch, rpc_doctor)
    monkeypatch.setattr(rpc_doctor, "_router_payload", router_payload)
    monkeypatch.setattr(rpc_doctor, "_memory_embedding_payload", memory_embedding_payload)

    response = await get_dispatcher().dispatch(
        "req-1",
        "doctor.status",
        {},
        RpcContext(conn_id="test", config=GatewayConfig()),
    )

    assert response.ok is True
    assert response.payload["status"] == "degraded"
    ids = [finding["id"] for finding in response.payload["findings"]]
    assert "router.runtime.missing" in ids
    assert "memory_embedding.config.error" in ids


@pytest.mark.asyncio
async def test_doctor_status_accepts_deep_memory_flag(monkeypatch) -> None:
    import agentos.gateway.rpc_doctor as rpc_doctor

    seen_memory_params: dict[str, Any] = {}

    async def provider_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        return {
            "activeProvider": "openrouter",
            "providers": [
                {
                    "providerId": "openrouter",
                    "active": True,
                    "configured": True,
                    "buildable": True,
                }
            ],
        }

    async def memory_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        seen_memory_params.update(params)
        return {"backend": "sqlite", "status": "ok", "pendingRepairCount": 0}

    monkeypatch.setattr(rpc_doctor, "_handle_providers_status", provider_status)
    _patch_ready_support_surfaces(monkeypatch, rpc_doctor)
    monkeypatch.setattr(rpc_doctor, "_handle_doctor_memory_status", memory_status)

    response = await get_dispatcher().dispatch(
        "req-1",
        "doctor.status",
        {"agentId": "main", "deep": True},
        RpcContext(conn_id="test", config=GatewayConfig()),
    )

    assert response.ok is True
    assert response.payload["agentId"] == "main"
    assert seen_memory_params == {"agentId": "main", "deep": True}


@pytest.mark.asyncio
async def test_doctor_status_defaults_to_deep_memory_diagnostics(monkeypatch) -> None:
    import agentos.gateway.rpc_doctor as rpc_doctor

    seen_memory_params: dict[str, Any] = {}

    async def memory_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        seen_memory_params.update(params)
        return {"backend": "sqlite", "status": "ok", "pendingRepairCount": 0}

    _patch_ready_support_surfaces(monkeypatch, rpc_doctor)
    monkeypatch.setattr(rpc_doctor, "_handle_doctor_memory_status", memory_status)

    response = await get_dispatcher().dispatch(
        "req-1",
        "doctor.status",
        {},
        RpcContext(conn_id="test", config=GatewayConfig()),
    )

    assert response.ok is True
    assert seen_memory_params == {"agentId": "main", "deep": True}


@pytest.mark.asyncio
async def test_doctor_status_can_skip_deep_memory_diagnostics(monkeypatch) -> None:
    import agentos.gateway.rpc_doctor as rpc_doctor

    seen_memory_params: dict[str, Any] = {}

    async def memory_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        seen_memory_params.update(params)
        return {"backend": "sqlite", "status": "ok", "pendingRepairCount": 0}

    _patch_ready_support_surfaces(monkeypatch, rpc_doctor)
    monkeypatch.setattr(rpc_doctor, "_handle_doctor_memory_status", memory_status)

    response = await get_dispatcher().dispatch(
        "req-1",
        "doctor.status",
        {"deep": False},
        RpcContext(conn_id="test", config=GatewayConfig()),
    )

    assert response.ok is True
    assert seen_memory_params == {"agentId": "main", "deep": False}


@pytest.mark.asyncio
async def test_doctor_status_explains_recovery_when_collection_fails(monkeypatch) -> None:
    import agentos.gateway.rpc_doctor as rpc_doctor

    async def provider_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        raise RuntimeError("provider status crashed")

    monkeypatch.setattr(rpc_doctor, "_handle_providers_status", provider_status)
    _patch_ready_support_surfaces(monkeypatch, rpc_doctor)

    response = await get_dispatcher().dispatch(
        "req-1",
        "doctor.status",
        {},
        RpcContext(conn_id="test", config=GatewayConfig()),
    )

    assert response.ok is True
    assert response.payload["ready"] is False
    assert response.payload["status"] == "action_required"
    provider_finding = next(
        finding
        for finding in response.payload["findings"]
        if finding["id"] == "provider.diagnostic.unavailable"
    )
    assert provider_finding["severity"] == "error"
    commands = [step["command"] for step in provider_finding["fixSteps"]]
    assert commands == [
        "agentos providers status --json",
        "agentos diagnostics status",
        "agentos gateway restart",
    ]


@pytest.mark.asyncio
async def test_doctor_status_degrades_when_noncritical_collection_fails(monkeypatch) -> None:
    import agentos.gateway.rpc_doctor as rpc_doctor

    async def provider_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        return {
            "activeProvider": "openrouter",
            "providers": [
                {
                    "providerId": "openrouter",
                    "active": True,
                    "configured": True,
                    "buildable": True,
                }
            ],
        }

    async def memory_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        raise RuntimeError("memory diagnostics crashed")

    _patch_ready_support_surfaces(monkeypatch, rpc_doctor)
    monkeypatch.setattr(rpc_doctor, "_handle_providers_status", provider_status)
    monkeypatch.setattr(rpc_doctor, "_handle_doctor_memory_status", memory_status)

    response = await get_dispatcher().dispatch(
        "req-1",
        "doctor.status",
        {},
        RpcContext(conn_id="test", config=GatewayConfig()),
    )

    assert response.ok is True
    assert response.payload["ready"] is True
    assert response.payload["status"] == "degraded"
    assert response.payload["impactCounts"]["degrades"] == 1
    memory_finding = next(
        finding
        for finding in response.payload["findings"]
        if finding["id"] == "memory.diagnostic.unavailable"
    )
    assert memory_finding["severity"] == "warn"
    assert memory_finding["readinessImpact"] == "degrades"


@pytest.mark.asyncio
async def test_doctor_status_treats_dead_channel_as_surface_degradation(monkeypatch) -> None:
    import agentos.gateway.rpc_doctor as rpc_doctor

    async def provider_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        return {
            "activeProvider": "openrouter",
            "providers": [
                {
                    "providerId": "openrouter",
                    "active": True,
                    "configured": True,
                    "buildable": True,
                }
            ],
        }

    async def channels_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        return {
            "channels": [
                {
                    "name": "feishu",
                    "type": "feishu",
                    "enabled": True,
                    "status": "dead",
                }
            ]
        }

    _patch_ready_support_surfaces(monkeypatch, rpc_doctor)
    monkeypatch.setattr(rpc_doctor, "_handle_providers_status", provider_status)
    monkeypatch.setattr(rpc_doctor, "_handle_channels_status", channels_status)

    cfg = GatewayConfig()
    cfg.config_path = "/tmp/custom-agentos.toml"

    response = await get_dispatcher().dispatch(
        "req-1",
        "doctor.status",
        {},
        RpcContext(conn_id="test", config=cfg),
    )

    assert response.ok is True
    assert response.payload["ready"] is True
    assert response.payload["status"] == "degraded"
    channel_finding = next(
        finding
        for finding in response.payload["findings"]
        if finding["id"] == "channel.feishu.dead"
    )
    assert channel_finding["severity"] == "error"
    assert channel_finding["readinessImpact"] == "degrades"
    commands = [step["command"] for step in channel_finding["fixSteps"] if "command" in step]
    assert (
        "agentos channels restart feishu --yes "
        "--config /tmp/custom-agentos.toml"
    ) in commands
    assert (
        "agentos channels status feishu --json "
        "--config /tmp/custom-agentos.toml"
    ) in commands


@pytest.mark.asyncio
async def test_doctor_status_treats_no_channels_as_optional_setup(monkeypatch) -> None:
    import agentos.gateway.rpc_doctor as rpc_doctor

    async def provider_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        return {
            "activeProvider": "openrouter",
            "providers": [
                {
                    "providerId": "openrouter",
                    "active": True,
                    "configured": True,
                    "buildable": True,
                }
            ],
        }

    async def channels_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        return {"channels": []}

    _patch_ready_support_surfaces(monkeypatch, rpc_doctor)
    monkeypatch.setattr(rpc_doctor, "_handle_providers_status", provider_status)
    monkeypatch.setattr(rpc_doctor, "_handle_channels_status", channels_status)

    response = await get_dispatcher().dispatch(
        "req-1",
        "doctor.status",
        {},
        RpcContext(conn_id="test", config=GatewayConfig()),
    )

    assert response.ok is True
    assert response.payload["ready"] is True
    assert response.payload["status"] == "ready"
    channel_finding = next(
        finding
        for finding in response.payload["findings"]
        if finding["id"] == "channels.none_configured"
    )
    assert channel_finding["severity"] == "info"
    assert channel_finding["readinessImpact"] == "optional"
    assert channel_finding["fixSteps"][0]["command"] == (
        "agentos configure --section channels"
    )


def test_router_payload_reports_resolved_llm_judge() -> None:
    import agentos.gateway.rpc_doctor as rpc_doctor

    # Default strategy is now v4_phase3 (which short-circuits judge resolution);
    # select llm_judge explicitly to exercise the judge doctor path.
    config = GatewayConfig(
        llm={"provider": "deepseek", "model": "deepseek-chat"},
        agentos_router={"strategy": "llm_judge"},
    )
    ctx = RpcContext(conn_id="test", config=config)

    payload = rpc_doctor._router_payload(ctx)

    assert payload["strategy"] == "llm_judge"
    assert payload["judgeProvider"] == "deepseek"
    assert payload["judgeModel"] == config.agentos_router.tiers["c0"]["model"]
    assert payload["judgeSource"] == "auto"
    assert payload["runtimeValid"] is True


def test_router_payload_reports_explicit_judge_model() -> None:
    import agentos.gateway.rpc_doctor as rpc_doctor

    config = GatewayConfig(
        llm={"provider": "deepseek", "model": "deepseek-chat"},
        agentos_router={"strategy": "llm_judge", "judge_model": "deepseek-v4-pro"},
    )
    ctx = RpcContext(conn_id="test", config=config)

    payload = rpc_doctor._router_payload(ctx)

    assert payload["judgeModel"] == "deepseek-v4-pro"
    assert payload["judgeSource"] == "explicit"


def test_router_payload_reports_local_judge_endpoint() -> None:
    """A local-endpoint judge (judge_base_url set) reports source="local" and
    its base_url, carries its own credentials (no no-credentials finding), and
    the router stays runtimeValid."""
    import agentos.gateway.rpc_doctor as rpc_doctor

    config = GatewayConfig(
        llm={"provider": "deepseek", "model": "deepseek-chat"},
        agentos_router={
            "strategy": "llm_judge",
            "judge_model": "llama3",
            "judge_base_url": "http://localhost:11434/v1",
            "judge_api_key": "sk-local",
        },
    )
    ctx = RpcContext(conn_id="test", config=config)

    payload = rpc_doctor._router_payload(ctx)

    assert payload["judgeModel"] == "llama3"
    assert payload["judgeSource"] == "local"
    assert payload["judgeBaseUrl"] == "http://localhost:11434/v1"
    assert payload["runtimeValid"] is True
    # The api key is never surfaced by the doctor payload.
    assert "sk-local" not in str(payload)


def test_router_payload_flags_local_endpoint_ignored_in_auto_mode() -> None:
    """A configured local endpoint (judge_base_url) is only honored with an
    EXPLICIT judge_model. In AUTO mode (judge_model unset) control falls through
    to the tier scan and resolves a CLOUD tier target with source="auto",
    silently discarding the operator's local endpoint. Doctor must report the
    router unhealthy (reason="judge_base_url_requires_model") so the
    misconfiguration is not hidden behind a working cloud judge."""
    import agentos.gateway.rpc_doctor as rpc_doctor

    config = GatewayConfig(
        llm={"provider": "deepseek", "model": "deepseek-chat"},
        agentos_router={
            "strategy": "llm_judge",
            "tier_profile": "deepseek",
            # judge_model left unset (AUTO) while judge_base_url is set.
            "judge_base_url": "http://localhost:11434/v1",
        },
    )
    assert config.agentos_router.judge_model is None
    ctx = RpcContext(conn_id="test", config=config)

    payload = rpc_doctor._router_payload(ctx)

    assert payload["strategy"] == "llm_judge"
    # AUTO resolution discards the local endpoint and picks a cloud tier target.
    assert payload["judgeSource"] == "auto"
    assert payload["runtimeValid"] is False
    assert payload["runtimeInvalidReason"] == "judge_base_url_requires_model"
    assert payload["error"]


def test_router_payload_marks_runtime_invalid_when_judge_unresolvable() -> None:
    """Finding #9: an enabled llm_judge router whose judge can never resolve
    (no judge_model and no usable tier model) must surface via
    runtimeValid=False so doctor/health emit a finding instead of router.ready.
    """
    import agentos.gateway.rpc_doctor as rpc_doctor

    config = GatewayConfig(
        llm={"provider": "deepseek", "model": "deepseek-chat"},
        agentos_router={"strategy": "llm_judge"},
    )
    # Simulate an unresolvable judge: no explicit judge_model and no text-tier
    # model to fall back to (resolve_judge_target -> None).
    config.agentos_router.tiers = {}
    ctx = RpcContext(conn_id="test", config=config)

    payload = rpc_doctor._router_payload(ctx)

    assert payload["strategy"] == "llm_judge"
    assert payload["judgeModel"] is None
    assert payload["runtimeValid"] is False
    assert payload["error"]


def test_router_payload_v4_phase3_skips_judge_resolution() -> None:
    """v4_phase3 (the reintegrated default local ML router) needs no judge and no
    cloud credentials: its doctor health is purely bundle presence. The payload
    must early-return before any judge resolution, so all judge fields are None
    and the strategy is reported verbatim as "v4_phase3". Bundle presence is
    env-dependent (git-ignored), so this asserts only the shape that holds
    regardless of whether the local bundle is present.
    """
    import agentos.gateway.rpc_doctor as rpc_doctor

    config = GatewayConfig(
        llm={"provider": "deepseek", "model": "deepseek-chat"},
        agentos_router={"strategy": "v4_phase3"},
    )
    assert config.agentos_router.strategy == "v4_phase3"
    ctx = RpcContext(conn_id="test", config=config)

    payload = rpc_doctor._router_payload(ctx)

    assert payload["strategy"] == "v4_phase3"
    assert payload["judgeProvider"] is None
    assert payload["judgeModel"] is None
    assert payload["judgeSource"] is None
    assert payload["judgeBaseUrl"] is None


def test_router_payload_reports_tier_providers_for_v4_phase3() -> None:
    """The payload carries llm.provider plus each tier's declared provider so
    evaluate_router can flag tiers pointing at a provider the runtime never
    builds a client for (routing is single-provider; tiers only pick the model).
    """
    import agentos.gateway.rpc_doctor as rpc_doctor

    config = GatewayConfig(
        llm={"provider": "ollama", "model": "llama3"},
        agentos_router={"strategy": "v4_phase3"},
    )
    ctx = RpcContext(conn_id="test", config=config)

    payload = rpc_doctor._router_payload(ctx)

    assert payload["llmProvider"] == "ollama"
    # Default tier profile is OpenRouter: every tier declares provider=openrouter.
    assert payload["tierProviders"]["c0"] == "openrouter"
    assert set(payload["tierProviders"]) == set(config.agentos_router.tiers)


def test_router_payload_reports_tier_providers_for_llm_judge() -> None:
    import agentos.gateway.rpc_doctor as rpc_doctor

    config = GatewayConfig(
        llm={"provider": "deepseek", "model": "deepseek-chat"},
        agentos_router={"strategy": "llm_judge"},
    )
    ctx = RpcContext(conn_id="test", config=config)

    payload = rpc_doctor._router_payload(ctx)

    assert payload["llmProvider"] == "deepseek"
    # tierProviders mirrors the resolved tier mapping's provider fields verbatim
    # (the config layer may rewrite the tier profile for the active provider).
    assert payload["tierProviders"] == {
        name: tier["provider"]
        for name, tier in config.agentos_router.tiers.items()
        if str(tier.get("provider") or "").strip()
    }
    assert payload["tierProviders"]


def test_router_payload_marks_runtime_invalid_for_cross_provider_auto_judge() -> None:
    """Findings #2/#4: in AUTO mode (no judge_model) resolve_judge_target takes a
    tier entry's own provider field. When that resolved provider differs from
    llm.provider — e.g. an llm.provider that is not a router tier profile id, so
    the default openrouter tiers are left in place — the judge has NO
    credential source (tier entries carry no credentials) and every turn
    degrades to judge_unavailable. Doctor previously reported runtimeValid=True
    (router.ready) because resolve_judge_target returned a non-None target; it
    must now report the router as unhealthy instead."""
    import agentos.gateway.rpc_doctor as rpc_doctor

    # "anthropic" is not a router tier profile id, so the router is NOT
    # reconciled to it and keeps the default openrouter tiers.
    config = GatewayConfig(
        llm={"provider": "anthropic", "model": "claude-x", "api_key": "sk"},
        agentos_router={"strategy": "llm_judge"},
    )
    assert config.agentos_router.tiers["c0"]["provider"] == "openrouter"
    ctx = RpcContext(conn_id="test", config=config)

    payload = rpc_doctor._router_payload(ctx)

    assert payload["strategy"] == "llm_judge"
    assert payload["judgeSource"] == "auto"
    assert payload["judgeProvider"] == "openrouter"
    assert payload["runtimeValid"] is False
    assert payload["error"]


def test_router_payload_marks_runtime_invalid_for_hand_edited_cross_provider_tier() -> None:
    """Finding #4: a hand-edited tiers table whose c0 provider differs from
    llm.provider resolves (AUTO) to that cross-provider tier, which has no
    credential source. Doctor must report the router as unhealthy rather than
    router.ready."""
    import agentos.gateway.rpc_doctor as rpc_doctor

    config = GatewayConfig(
        llm={"provider": "deepseek", "model": "deepseek-chat"},
        agentos_router={"strategy": "llm_judge"},
    )
    # Hand-edit the cheapest text tier's provider to a mismatched backend.
    config.agentos_router.tiers = {
        "c0": {"provider": "dashscope", "model": "qwen-x", "description": "cheap"},
        "c1": {"provider": "deepseek", "model": "deepseek-chat", "description": "mid"},
    }
    ctx = RpcContext(conn_id="test", config=config)

    payload = rpc_doctor._router_payload(ctx)

    assert payload["judgeSource"] == "auto"
    assert payload["judgeProvider"] == "dashscope"
    assert payload["runtimeValid"] is False
    assert payload["error"]


def test_router_payload_sets_reason_judge_no_credentials() -> None:
    """A cross-provider AUTO judge with no credential source must carry
    runtimeInvalidReason="judge_no_credentials" (finding #1)."""
    import agentos.gateway.rpc_doctor as rpc_doctor

    config = GatewayConfig(
        llm={"provider": "anthropic", "model": "claude-x", "api_key": "sk"},
        agentos_router={"strategy": "llm_judge"},
    )
    ctx = RpcContext(conn_id="test", config=config)

    payload = rpc_doctor._router_payload(ctx)

    assert payload["runtimeValid"] is False
    assert payload["runtimeInvalidReason"] == "judge_no_credentials"


def test_router_payload_sets_reason_judge_unresolvable() -> None:
    """An unresolvable judge must carry runtimeInvalidReason="judge_unresolvable"
    (finding #1)."""
    import agentos.gateway.rpc_doctor as rpc_doctor

    config = GatewayConfig(
        llm={"provider": "deepseek", "model": "deepseek-chat"},
        agentos_router={"strategy": "llm_judge"},
    )
    config.agentos_router.tiers = {}
    ctx = RpcContext(conn_id="test", config=config)

    payload = rpc_doctor._router_payload(ctx)

    assert payload["runtimeValid"] is False
    assert payload["runtimeInvalidReason"] == "judge_unresolvable"


def test_evaluate_router_flags_v4_phase3_missing_bundle() -> None:
    """v4_phase3 is the reintegrated local ML router. When its git-ignored bundle
    is absent, rpc_doctor reports runtimeValid=False with runtimeInvalidReason
    "assets" (a genuine missing local runtime asset), which maps to the
    asset-centric router.runtime.missing finding — the correct taxonomy for a
    missing v4 bundle now that the strategy_removed reason is gone."""
    from agentos.health.evaluator import evaluate_router

    findings = evaluate_router(
        {
            "enabled": True,
            "rolloutPhase": "full",
            "strategy": "v4_phase3",
            "tierProfile": "deepseek",
            "defaultTier": "c1",
            "runtimeValid": False,
            "runtimeInvalidReason": "assets",
            "error": "missing V4 bundle files",
        }
    )
    assert findings
    finding = findings[0]
    assert finding.id == "router.runtime.missing"
    assert finding.title == "Router runtime assets are missing"
    assert finding.severity == "warn"


def test_evaluate_router_flags_judge_no_credentials() -> None:
    """Finding #1: a cross-provider judge with no credentials gets a
    credentials-specific finding, not the missing-assets title."""
    from agentos.health.evaluator import evaluate_router

    findings = evaluate_router(
        {
            "enabled": True,
            "rolloutPhase": "full",
            "strategy": "llm_judge",
            "tierProfile": "deepseek",
            "defaultTier": "c1",
            "runtimeValid": False,
            "runtimeInvalidReason": "judge_no_credentials",
            "error": "judge resolved to a provider with no credential source",
        }
    )
    assert findings
    finding = findings[0]
    assert finding.id == "router.judge.no_credentials"
    assert finding.title != "Router runtime assets are missing"
    assert finding.severity == "warn"


def test_evaluate_router_flags_unresolved_judge_as_judge_unresolvable() -> None:
    """Finding #1: an unresolvable judge gets a resolution-specific finding, not
    the missing-assets title."""
    from agentos.health.evaluator import evaluate_router

    findings = evaluate_router(
        {
            "enabled": True,
            "rolloutPhase": "full",
            "strategy": "llm_judge",
            "tierProfile": "deepseek",
            "defaultTier": "c1",
            "runtimeValid": False,
            "runtimeInvalidReason": "judge_unresolvable",
            "error": "judge unresolvable",
        }
    )
    assert findings
    finding = findings[0]
    assert finding.id == "router.judge.unresolvable"
    assert finding.title != "Router runtime assets are missing"
    assert finding.severity == "warn"


def test_evaluate_router_still_flags_missing_assets_for_asset_reason() -> None:
    """A genuine missing-asset runtime failure (reason="assets" or absent) keeps
    the original router.runtime.missing taxonomy."""
    from agentos.health.evaluator import evaluate_router

    findings = evaluate_router(
        {
            "enabled": True,
            "rolloutPhase": "full",
            "strategy": "llm_judge",
            "tierProfile": "deepseek",
            "defaultTier": "c1",
            "runtimeValid": False,
            "runtimeInvalidReason": "assets",
            "error": "runtime asset missing",
        }
    )
    assert findings
    assert findings[0].id == "router.runtime.missing"
    assert findings[0].title == "Router runtime assets are missing"
