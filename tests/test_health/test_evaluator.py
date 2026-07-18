from __future__ import annotations

from agentos.health.evaluator import (
    evaluate_channels,
    evaluate_image_generation,
    evaluate_logs,
    evaluate_memory,
    evaluate_memory_embedding,
    evaluate_provider,
    evaluate_router,
    evaluate_sandbox,
    evaluate_search,
)


def _impact(finding) -> str:
    return finding.to_dict()["readinessImpact"]


def test_readiness_impact_matrix_matches_user_visible_strategy() -> None:
    provider = evaluate_provider(
        {
            "activeProvider": "openrouter",
            "providers": [
                {
                    "providerId": "openrouter",
                    "active": True,
                    "configured": False,
                    "buildable": True,
                }
            ],
        }
    )[0]
    assert provider.id == "provider.active.not_configured"
    assert _impact(provider) == "blocks_ready"

    router_missing = evaluate_router(
        {
            "enabled": True,
            "rolloutPhase": "full",
            "runtimeValid": False,
        }
    )[0]
    assert router_missing.id == "router.runtime.missing"
    assert _impact(router_missing) == "degrades"

    memory = evaluate_memory({"backend": "sqlite", "status": "unavailable"})[0]
    assert memory.id == "memory.status.error"
    assert _impact(memory) == "degrades"

    logs = evaluate_logs(
        {
            "gateway_file_log": {
                "enabled": True,
                "path": "/tmp/missing-debug.log",
                "exists": False,
                "active_tail_path_exists": False,
            }
        }
    )[0]
    assert logs.id == "logs.gateway_file_log.missing"
    assert _impact(logs) == "degrades"

    search = evaluate_search(
        {
            "provider": "brave",
            "activeProvider": "brave",
            "configured": False,
            "runtimeSupported": True,
            "requiresApiKey": True,
            "apiKeyConfigured": False,
            "buildable": False,
        }
    )[0]
    assert search.id == "search.provider.not_configured"
    assert _impact(search) == "degrades"

    image = evaluate_image_generation(
        {
            "enabled": True,
            "configured": False,
            "status": "missing",
            "provider": "openai",
            "primary": "openai/gpt-image-1",
        }
    )[0]
    assert image.id == "image_generation.credentials.missing"
    assert _impact(image) == "degrades"

    channel = evaluate_channels(
        {"channels": [{"name": "feishu", "enabled": True, "status": "dead"}]}
    )[0]
    assert channel.id == "channel.feishu.dead"
    assert _impact(channel) == "degrades"

    no_channels = evaluate_channels({"channels": []})[0]
    assert no_channels.id == "channels.none_configured"
    assert _impact(no_channels) == "optional"

    sandbox = evaluate_sandbox({"posture": "bypass"})[0]
    assert sandbox.id == "sandbox.posture.bypass"
    assert _impact(sandbox) == "optional"

    router_optional = evaluate_router({"enabled": False, "runtimeValid": False})[0]
    assert router_optional.id == "router.disabled"
    assert _impact(router_optional) == "optional"


def test_provider_evaluator_flags_active_provider_missing_key() -> None:
    findings = evaluate_provider(
        {
            "activeProvider": "openrouter",
            "providers": [
                {
                    "providerId": "openrouter",
                    "active": True,
                    "configured": False,
                    "buildable": True,
                    "apiKeyConfigured": False,
                    "model": "openai/gpt-5.1",
                }
            ],
        }
    )

    assert findings[0].id == "provider.active.not_configured"
    assert findings[0].severity == "error"
    assert findings[0].restart_required is True
    assert "agentos providers configure openrouter" in findings[0].fix_steps[0].command


def test_provider_evaluator_explains_missing_configured_api_key_env() -> None:
    findings = evaluate_provider(
        {
            "activeProvider": "openrouter",
            "providers": [
                {
                    "providerId": "openrouter",
                    "active": True,
                    "configured": False,
                    "buildable": True,
                    "requiresApiKey": True,
                    "apiKeyConfigured": False,
                    "apiKeyEnv": "AGENTOS_PROVIDER_KEY",
                    "model": "openai/gpt-5.1",
                }
            ],
        }
    )

    finding = findings[0]
    assert finding.id == "provider.active.not_configured"
    assert finding.evidence["apiKeyEnv"] == "AGENTOS_PROVIDER_KEY"
    assert "AGENTOS_PROVIDER_KEY" in finding.detail
    assert any(
        step.detail and "AGENTOS_PROVIDER_KEY" in step.detail
        for step in finding.fix_steps
    )


def test_provider_evaluator_uses_configured_active_provider_when_missing_row() -> None:
    findings = evaluate_provider(
        {
            "activeProvider": "zhipu",
            "providers": [
                {
                    "providerId": "zhipu",
                    "active": False,
                    "configured": False,
                    "buildable": False,
                }
            ],
        }
    )

    assert findings[0].id == "provider.active.missing"
    assert findings[0].evidence["activeProvider"] == "zhipu"
    assert findings[0].fix_steps[0].command == (
        "agentos providers configure zhipu --api-key YOUR_API_KEY"
    )


def test_provider_evaluator_reports_missing_diagnostic_shape_as_incomplete() -> None:
    findings = evaluate_provider({})

    assert findings[0].id == "provider.diagnostic.incomplete"
    assert findings[0].severity == "error"
    assert _impact(findings[0]) == "blocks_ready"
    assert "providers" in findings[0].detail
    assert [step.command for step in findings[0].fix_steps] == [
        "agentos providers status --json",
        "agentos gateway restart",
    ]


def test_provider_evaluator_flags_unknown_active_provider() -> None:
    findings = evaluate_provider(
        {
            "activeProvider": "zai",
            "providers": [
                {"providerId": "openrouter", "active": False},
                {"providerId": "zhipu", "active": False},
            ],
        }
    )

    assert findings[0].id == "provider.active.unknown"
    assert findings[0].severity == "error"
    assert findings[0].evidence["activeProvider"] == "zai"
    commands = [step.command for step in findings[0].fix_steps]
    assert "agentos providers list --json" in commands
    assert "agentos providers configure zhipu --api-key YOUR_API_KEY" in commands
    assert "agentos providers configure zai --api-key YOUR_API_KEY" not in commands


def test_provider_evaluator_reports_ready_active_provider() -> None:
    findings = evaluate_provider(
        {
            "activeProvider": "openrouter",
            "providers": [
                {
                    "providerId": "openrouter",
                    "active": True,
                    "configured": True,
                    "buildable": True,
                    "apiKeyConfigured": True,
                    "baseUrlConfigured": True,
                    "model": "openai/gpt-5.1",
                }
            ],
        }
    )

    assert findings[0].id == "provider.active.ready"
    assert findings[0].severity == "ok"


def test_provider_evaluator_does_not_report_unidentified_active_row_as_ready() -> None:
    findings = evaluate_provider(
        {
            "activeProvider": "",
            "providers": [
                {
                    "active": True,
                    "configured": True,
                    "buildable": True,
                    "apiKeyConfigured": True,
                    "model": "openai/gpt-5.1",
                }
            ],
        }
    )

    assert findings[0].id == "provider.active.unidentified"
    assert findings[0].severity == "error"
    assert _impact(findings[0]) == "blocks_ready"
    assert "did not include a provider id" in findings[0].detail
    assert [step.command for step in findings[0].fix_steps] == [
        "agentos providers status --json",
        "agentos providers configure openrouter --api-key YOUR_API_KEY",
        "agentos gateway restart",
    ]


def test_memory_evaluator_flags_degraded_backend() -> None:
    findings = evaluate_memory(
        {
            "backend": "sqlite",
            "status": "degraded",
            "vecAvailable": False,
            "ftsAvailable": True,
            "pendingRepairCount": 2,
        }
    )

    ids = [finding.id for finding in findings]
    assert "memory.status.degraded" in ids
    assert "memory.repair.pending" in ids


def test_memory_evaluator_does_not_report_unknown_status_as_ready() -> None:
    findings = evaluate_memory({"backend": "sqlite", "status": "unknown"})

    assert findings[0].id == "memory.status.unknown"
    assert findings[0].severity == "warn"
    assert findings[0].fix_steps[0].command == "agentos memory status --deep --json"


def test_memory_evaluator_treats_missing_status_as_incomplete() -> None:
    findings = evaluate_memory({"backend": "sqlite"})

    assert findings[0].id == "memory.diagnostic.incomplete"
    assert findings[0].severity == "warn"
    assert _impact(findings[0]) == "degrades"
    assert "status" in findings[0].detail
    assert [step.command for step in findings[0].fix_steps] == [
        "agentos memory status --deep --json",
        "agentos gateway restart",
    ]


def test_memory_repair_guidance_uses_existing_cli_options() -> None:
    findings = evaluate_memory(
        {
            "backend": "sqlite",
            "status": "ok",
            "pendingRepairCount": 2,
        }
    )

    repair = next(finding for finding in findings if finding.id == "memory.repair.pending")
    commands = [step.command for step in repair.fix_steps]
    assert "agentos memory repair run --json" in commands
    assert "agentos memory repair run --yes" not in commands


def test_memory_repair_guidance_accepts_pending_repairs_alias() -> None:
    findings = evaluate_memory(
        {
            "backend": "sqlite",
            "status": "ok",
            "pendingRepairs": 2,
        }
    )

    assert any(finding.id == "memory.repair.pending" for finding in findings)


def test_logs_evaluator_flags_missing_debug_log_path() -> None:
    findings = evaluate_logs(
        {
            "gateway_file_log": {
                "enabled": True,
                "path": "/tmp/missing-debug.log",
                "exists": False,
                "active_tail_path_exists": False,
            },
            "raw_turn_call_log": {
                "enabled": False,
                "directory": {"path": "/tmp/raw", "exists": False},
            },
            "diagnostics_enabled": {"effective": False, "detail": "off"},
        }
    )

    assert findings[0].id == "logs.gateway_file_log.missing"
    assert findings[0].severity == "warn"
    commands = [step.command for step in findings[0].fix_steps]
    assert "agentos diagnostics status" in commands
    assert "agentos gateway restart" in commands
    assert "agentos logs tail" not in commands


def test_logs_evaluator_does_not_treat_missing_diagnostic_shape_as_optional() -> None:
    findings = evaluate_logs({})

    assert findings[0].id == "logs.diagnostic.incomplete"
    assert findings[0].severity == "warn"
    assert _impact(findings[0]) == "degrades"
    assert "Log diagnostics did not include gateway_file_log" in findings[0].detail
    assert [step.command for step in findings[0].fix_steps] == [
        "agentos diagnostics status",
        "agentos gateway restart",
    ]


def test_optional_surface_evaluators_do_not_treat_missing_diagnostic_shape_as_optional() -> None:
    cases = [
        ("search", evaluate_search, "search.diagnostic.incomplete"),
        ("image_generation", evaluate_image_generation, "image_generation.diagnostic.incomplete"),
        ("router", evaluate_router, "router.diagnostic.incomplete"),
        (
            "memory_embedding",
            evaluate_memory_embedding,
            "memory_embedding.diagnostic.incomplete",
        ),
        ("channels", evaluate_channels, "channels.diagnostic.incomplete"),
    ]

    for surface, evaluator, finding_id in cases:
        findings = evaluator({})

        assert findings[0].id == finding_id, surface
        assert findings[0].severity == "warn", surface
        assert _impact(findings[0]) == "degrades", surface
        assert "diagnostics did not include" in findings[0].detail


def test_search_evaluator_treats_partial_provider_diagnostic_as_incomplete() -> None:
    findings = evaluate_search({"provider": "duckduckgo", "activeProvider": "duckduckgo"})

    assert findings[0].id == "search.diagnostic.incomplete"
    assert findings[0].severity == "warn"
    assert _impact(findings[0]) == "degrades"
    assert "runtimeSupported" in findings[0].detail
    assert [step.command for step in findings[0].fix_steps] == [
        "agentos search status --json",
        "agentos gateway restart",
    ]


def test_logs_evaluator_treats_disabled_file_logging_as_optional_setup() -> None:
    findings = evaluate_logs(
        {
            "gateway_file_log": {
                "enabled": False,
                "path": "/tmp/agentos-debug.log",
                "exists": False,
            }
        }
    )

    assert len(findings) == 1
    assert findings[0].id == "logs.gateway_file_log.disabled"
    assert findings[0].severity == "info"
    assert _impact(findings[0]) == "optional"
    assert "Persistent gateway file logging is optional" in findings[0].detail
    assert [step.command for step in findings[0].fix_steps] == [
        "agentos config set log_file_enabled true",
        "agentos gateway restart",
    ]
    assert findings[0].restart_required is True


def test_channels_evaluator_flags_dead_enabled_channel() -> None:
    findings = evaluate_channels(
        {
            "channels": [
                {
                    "name": "slack-main",
                    "type": "slack",
                    "enabled": True,
                    "configured": True,
                    "status": "dead",
                    "connected": False,
                }
            ]
        }
    )

    assert findings[0].id == "channel.slack-main.dead"
    assert findings[0].severity == "error"
    assert findings[0].fix_steps[0].command == "agentos channels restart slack-main --yes"


def test_channels_evaluator_flags_stopped_enabled_channel() -> None:
    findings = evaluate_channels(
        {
            "channels": [
                {
                    "name": "slack-main",
                    "type": "slack",
                    "enabled": True,
                    "configured": True,
                    "status": "stopped",
                    "connected": False,
                }
            ]
        }
    )

    assert findings[0].id == "channel.slack-main.stopped"
    assert findings[0].severity == "warn"
    commands = [step.command for step in findings[0].fix_steps]
    assert "agentos channels status slack-main --json" in commands
    assert "agentos channels restart slack-main --yes" in commands


def test_channels_evaluator_quotes_channel_names_in_recovery_commands() -> None:
    findings = evaluate_channels(
        {
            "channels": [
                {
                    "name": "feishu work",
                    "type": "feishu",
                    "enabled": True,
                    "configured": True,
                    "status": "dead",
                    "connected": False,
                }
            ]
        }
    )

    commands = [step.command for step in findings[0].fix_steps]
    assert "agentos channels restart 'feishu work' --yes" in commands
    assert "agentos channels status 'feishu work' --json" in commands


def test_channels_evaluator_treats_disabled_channel_as_optional_info() -> None:
    findings = evaluate_channels(
        {
            "channels": [
                {
                    "name": "slack-main",
                    "type": "slack",
                    "enabled": False,
                    "configured": True,
                    "status": "disabled",
                    "connected": False,
                }
            ]
        }
    )

    assert findings[0].id == "channel.slack-main.disabled"
    assert findings[0].severity == "info"
    assert findings[0].restart_required is True
    commands = [step.command for step in findings[0].fix_steps]
    assert "agentos channels enable slack-main" in commands
    assert "agentos configure --section channels" not in commands


def test_channels_evaluator_treats_no_channels_as_optional_setup() -> None:
    findings = evaluate_channels({"channels": []})

    assert len(findings) == 1
    assert findings[0].id == "channels.none_configured"
    assert findings[0].severity == "info"
    assert _impact(findings[0]) == "optional"
    assert "No channel entrypoints are configured" in findings[0].detail
    assert [step.command for step in findings[0].fix_steps] == [
        "agentos configure --section channels"
    ]


def test_channels_evaluator_treats_malformed_channels_shape_as_incomplete() -> None:
    findings = evaluate_channels({"channels": {"name": "slack-main"}})

    assert findings[0].id == "channels.diagnostic.incomplete"
    assert findings[0].severity == "warn"
    assert _impact(findings[0]) == "degrades"
    assert "channels" in findings[0].detail
    assert [step.command for step in findings[0].fix_steps] == [
        "agentos channels status --json",
        "agentos gateway restart",
    ]


def test_channels_evaluator_treats_malformed_channel_rows_as_incomplete() -> None:
    findings = evaluate_channels({"channels": ["slack-main"]})

    assert findings[0].id == "channels.diagnostic.incomplete"
    assert findings[0].severity == "warn"
    assert _impact(findings[0]) == "degrades"
    assert "channel rows" in findings[0].detail
    assert [step.command for step in findings[0].fix_steps] == [
        "agentos channels status --json",
        "agentos gateway restart",
    ]


def test_channels_evaluator_ready_finding_summarizes_checked_channels() -> None:
    findings = evaluate_channels(
        {
            "channels": [
                {
                    "name": "slack-main",
                    "type": "slack",
                    "enabled": True,
                    "configured": True,
                    "status": "connected",
                    "connected": True,
                },
                {
                    "name": "feishu-main",
                    "type": "feishu",
                    "enabled": True,
                    "configured": True,
                    "status": "connected",
                    "connected": True,
                },
            ]
        }
    )

    assert len(findings) == 1
    assert findings[0].id == "channels.ready"
    assert findings[0].severity == "ok"
    assert "2 configured channel entrypoints" in findings[0].detail
    assert findings[0].evidence == {
        "channelCount": 2,
        "enabledCount": 2,
        "statuses": {"connected": 2},
        "types": ["feishu", "slack"],
    }


def test_channels_evaluator_does_not_report_unknown_enabled_status_as_ready() -> None:
    findings = evaluate_channels(
        {
            "channels": [
                {
                    "name": "slack-main",
                    "type": "slack",
                    "enabled": True,
                    "configured": True,
                    "status": "warming_up",
                    "connected": False,
                }
            ]
        }
    )

    assert len(findings) == 1
    assert findings[0].id == "channel.slack-main.unknown_status"
    assert findings[0].severity == "warn"
    assert _impact(findings[0]) == "degrades"
    assert "not recognized" in findings[0].detail
    assert findings[0].evidence == {
        "name": "slack-main",
        "status": "warming_up",
        "type": "slack",
    }
    assert [step.command for step in findings[0].fix_steps] == [
        "agentos channels status slack-main --json",
        "agentos channels restart slack-main --yes",
    ]


def test_sandbox_evaluator_surfaces_bypass_posture_as_info() -> None:
    findings = evaluate_sandbox(
        {
            "posture": "bypass",
            "sandbox": {"sandbox": False, "security_grading": False},
            "permissions": {"default_mode": "bypass"},
            "restart_required": False,
        }
    )

    assert findings[0].id == "sandbox.posture.bypass"
    assert findings[0].severity == "info"
    assert findings[0].restart_required is True
    assert "restart_required" not in findings[0].evidence
    assert "agentos sandbox on" in findings[0].fix_steps[0].command


def test_sandbox_evaluator_does_not_report_unknown_posture_as_ready() -> None:
    findings = evaluate_sandbox(
        {
            "posture": "unknown",
            "sandbox": {"sandbox": False, "security_grading": False},
            "permissions": {"default_mode": "unknown"},
        }
    )

    assert findings[0].id == "sandbox.posture.unknown"
    assert findings[0].severity == "warn"
    assert _impact(findings[0]) == "degrades"
    assert findings[0].fix_steps[0].command == "agentos sandbox status --json"


def test_sandbox_evaluator_does_not_report_custom_posture_as_ready() -> None:
    findings = evaluate_sandbox(
        {
            "posture": "custom",
            "sandbox": {"sandbox": True, "security_grading": False},
            "permissions": {"default_mode": "full"},
        }
    )

    assert findings[0].id == "sandbox.posture.custom"
    assert findings[0].severity == "warn"
    assert _impact(findings[0]) == "degrades"
    assert findings[0].restart_required is True
    assert [step.command for step in findings[0].fix_steps] == [
        "agentos sandbox status --json",
        "agentos sandbox on",
        "agentos gateway restart",
    ]


def test_search_evaluator_flags_missing_key_without_blocking_core_runtime() -> None:
    findings = evaluate_search(
        {
            "provider": "brave",
            "activeProvider": "brave",
            "configured": False,
            "runtimeSupported": True,
            "requiresApiKey": True,
            "apiKeyConfigured": False,
            "buildable": False,
            "fallbackPolicy": "off",
        }
    )

    assert findings[0].id == "search.provider.not_configured"
    assert findings[0].severity == "warn"
    assert "agentos configure search" in findings[0].fix_steps[0].command


def test_search_evaluator_explains_missing_configured_api_key_env() -> None:
    findings = evaluate_search(
        {
            "activeProvider": "brave",
            "provider": "brave",
            "configured": False,
            "runtimeSupported": True,
            "requiresApiKey": True,
            "apiKeyConfigured": False,
            "apiKeyEnv": "CUSTOM_SEARCH_KEY",
            "buildable": False,
        }
    )

    finding = findings[0]
    assert finding.id == "search.provider.not_configured"
    assert finding.evidence["apiKeyEnv"] == "CUSTOM_SEARCH_KEY"
    assert "CUSTOM_SEARCH_KEY" in finding.detail
    assert any(
        step.detail and "CUSTOM_SEARCH_KEY" in step.detail
        for step in finding.fix_steps
    )


def test_search_evaluator_treats_empty_provider_as_optional_setup() -> None:
    findings = evaluate_search(
        {
            "provider": "",
            "activeProvider": "",
            "configured": False,
            "runtimeSupported": False,
            "requiresApiKey": False,
            "apiKeyConfigured": False,
            "buildable": False,
            "fallbackPolicy": "off",
        }
    )

    assert findings[0].id == "search.provider.disabled"
    assert findings[0].severity == "info"
    assert _impact(findings[0]) == "optional"
    assert "not configured" in findings[0].detail
    assert findings[0].restart_required is True
    assert [step.command for step in findings[0].fix_steps] == [
        "agentos configure search --search-provider duckduckgo",
        "agentos gateway restart",
    ]


def test_search_evaluator_flags_unknown_provider_with_supported_fallback() -> None:
    findings = evaluate_search(
        {
            "provider": "serpapi",
            "activeProvider": "serpapi",
            "unknownProvider": True,
            "configured": False,
            "runtimeSupported": False,
            "requiresApiKey": False,
            "apiKeyConfigured": False,
            "buildable": False,
            "error": "Unknown search provider 'serpapi'",
        }
    )

    assert findings[0].id == "search.provider.unknown"
    assert findings[0].severity == "warn"
    commands = [step.command for step in findings[0].fix_steps]
    assert "agentos search list --json" in commands
    assert "agentos configure search --search-provider duckduckgo" in commands
    assert all("serpapi" not in command for command in commands)


def test_search_evaluator_reports_ready_provider() -> None:
    findings = evaluate_search(
        {
            "provider": "duckduckgo",
            "activeProvider": "duckduckgo",
            "configured": True,
            "runtimeSupported": True,
            "requiresApiKey": False,
            "apiKeyConfigured": False,
            "buildable": True,
            "fallbackPolicy": "off",
            "maxResults": 8,
            "proxyConfigured": True,
            "useEnvProxy": False,
            "diagnostics": True,
        }
    )

    assert findings[0].id == "search.provider.ready"
    assert findings[0].severity == "ok"
    assert findings[0].evidence["maxResults"] == 8
    assert findings[0].evidence["proxyConfigured"] is True
    assert findings[0].evidence["diagnostics"] is True


def test_image_generation_evaluator_treats_disabled_as_optional_info() -> None:
    findings = evaluate_image_generation(
        {
            "enabled": False,
            "configured": False,
            "status": "optional",
            "provider": "",
            "primary": "openai/gpt-image-1",
            "source": "none",
        }
    )

    assert findings[0].id == "image_generation.disabled"
    assert findings[0].severity == "info"
    assert findings[0].restart_required is True
    assert [step.command for step in findings[0].fix_steps] == [
        "agentos configure image-generation --image-provider openai --api-key YOUR_API_KEY",
        "agentos gateway restart",
    ]


def test_image_generation_evaluator_treats_partial_enabled_diagnostic_as_incomplete() -> None:
    findings = evaluate_image_generation({"enabled": True, "provider": "openai"})

    assert findings[0].id == "image_generation.diagnostic.incomplete"
    assert findings[0].severity == "warn"
    assert _impact(findings[0]) == "degrades"
    assert "configured" in findings[0].detail
    assert [step.command for step in findings[0].fix_steps] == [
        "agentos onboard status --json",
        "agentos gateway restart",
    ]


def test_image_generation_evaluator_flags_enabled_missing_credentials() -> None:
    findings = evaluate_image_generation(
        {
            "enabled": True,
            "configured": False,
            "status": "missing",
            "provider": "openai",
            "primary": "openai/gpt-image-1",
            "source": "none",
        }
    )

    assert findings[0].id == "image_generation.credentials.missing"
    assert findings[0].severity == "warn"
    assert "agentos configure image-generation" in findings[0].fix_steps[0].command


def test_image_generation_evaluator_explains_missing_configured_api_key_env() -> None:
    findings = evaluate_image_generation(
        {
            "enabled": True,
            "configured": False,
            "status": "degraded",
            "provider": "openrouter",
            "primary": "openrouter/google/gemini-3.1-flash-image-preview",
            "source": "none",
            "apiKeyEnv": "CUSTOM_IMAGE_KEY",
        }
    )

    finding = findings[0]
    assert finding.id == "image_generation.credentials.missing"
    assert finding.evidence["apiKeyEnv"] == "CUSTOM_IMAGE_KEY"
    assert "CUSTOM_IMAGE_KEY" in finding.detail
    assert any(
        step.detail and "CUSTOM_IMAGE_KEY" in step.detail
        for step in finding.fix_steps
    )


def test_image_generation_unknown_provider_uses_supported_default_recovery() -> None:
    findings = evaluate_image_generation(
        {
            "enabled": True,
            "configured": False,
            "status": "unknown",
            "provider": "",
            "primary": "foo/image-model",
            "source": "none",
        }
    )

    assert findings[0].id == "image_generation.provider.unknown"
    commands = [step.command for step in findings[0].fix_steps]
    assert (
        "agentos configure image-generation --image-provider openai --api-key YOUR_API_KEY"
        in commands
    )
    assert all("--image-provider foo" not in command for command in commands)


def test_router_evaluator_treats_disabled_router_as_optional_info() -> None:
    findings = evaluate_router(
        {
            "enabled": False,
            "rolloutPhase": "observe",
            "strategy": "llm_judge",
            "tierProfile": "openrouter",
            "runtimeValid": True,
        }
    )

    assert findings[0].id == "router.disabled"
    assert findings[0].severity == "info"
    assert findings[0].restart_required is True


def test_router_evaluator_treats_partial_enabled_diagnostic_as_incomplete() -> None:
    findings = evaluate_router({"enabled": True})

    assert findings[0].id == "router.diagnostic.incomplete"
    assert findings[0].severity == "warn"
    assert _impact(findings[0]) == "degrades"
    assert "runtimeValid" in findings[0].detail
    assert [step.command for step in findings[0].fix_steps] == [
        "agentos diagnostics status",
        "agentos gateway restart",
    ]


def test_router_evaluator_marks_observe_only_recovery_as_restart_required() -> None:
    findings = evaluate_router(
        {
            "enabled": True,
            "rolloutPhase": "observe",
            "strategy": "llm_judge",
            "tierProfile": "openrouter",
            "runtimeValid": True,
        }
    )

    assert findings[0].id == "router.observe_only"
    assert findings[0].severity == "info"
    assert findings[0].restart_required is True


def test_router_evaluator_does_not_treat_unknown_rollout_phase_as_optional() -> None:
    findings = evaluate_router(
        {
            "enabled": True,
            "rolloutPhase": "mystery",
            "strategy": "llm_judge",
            "tierProfile": "openrouter",
            "runtimeValid": True,
        }
    )

    assert findings[0].id == "router.rollout_phase.unknown"
    assert findings[0].severity == "warn"
    assert _impact(findings[0]) == "degrades"
    assert "mystery" in findings[0].detail
    assert [step.command for step in findings[0].fix_steps] == [
        "agentos configure router --router recommended",
        "agentos gateway restart",
    ]


def test_router_evaluator_flags_missing_runtime_as_warn() -> None:
    findings = evaluate_router(
        {
            "enabled": True,
            "rolloutPhase": "full",
            "strategy": "llm_judge",
            "tierProfile": "openrouter",
            "runtimeValid": False,
            "error": "judge target could not be resolved",
        }
    )

    assert findings[0].id == "router.runtime.missing"
    assert findings[0].severity == "warn"
    assert "agentos configure router --router disabled" in findings[0].fix_steps[0].command


def test_router_evaluator_flags_degraded_local_bundle_as_warn() -> None:
    findings = evaluate_router(
        {
            "enabled": True,
            "rolloutPhase": "full",
            "strategy": "pilot-v1",
            "tierProfile": "custom",
            "runtimeValid": False,
            "runtimeInvalidReason": "assets_degraded",
            "error": (
                "The pilot-v1 router is selected but its local model bundle is "
                "missing (model.onnx), so every request routes to the balanced tier."
            ),
        }
    )

    # A degraded-but-serving bundle is a distinct, non-blocking warning — NOT the
    # hard "router.runtime.missing" raise state, and NOT a silent "Router ready".
    assert findings[0].id == "router.runtime.degraded"
    assert findings[0].title == "Router degraded"
    assert findings[0].severity == "warn"
    assert _impact(findings[0]) == "degrades"
    # The recovery steers the operator to require_router_runtime / reinstall.
    assert "require_router_runtime" in findings[0].fix_steps[0].detail


def test_router_evaluator_flags_ignored_local_endpoint() -> None:
    findings = evaluate_router(
        {
            "enabled": True,
            "rolloutPhase": "full",
            "strategy": "llm_judge",
            "tierProfile": "deepseek",
            "runtimeValid": False,
            "runtimeInvalidReason": "judge_base_url_requires_model",
            "error": "judge_base_url is set but judge_model is unset (auto)",
        }
    )

    assert findings[0].id == "router.judge.base_url_requires_model"
    assert findings[0].severity == "warn"
    assert _impact(findings[0]) == "degrades"
    # The finding steers the operator to set a judge_model, NOT to "missing assets".
    assert "judge_model" in findings[0].fix_steps[0].detail


def test_memory_embedding_evaluator_flags_explicit_remote_without_key() -> None:
    findings = evaluate_memory_embedding(
        {
            "status": "error",
            "requestedProvider": "openai",
            "effectiveProvider": "none",
            "model": "text-embedding-3-small",
            "retrievalMode": "hybrid",
            "error": "memory.embedding.remote.api_key is required",
        }
    )

    assert findings[0].id == "memory_embedding.config.error"
    assert findings[0].severity == "warn"
    assert "agentos configure memory-embedding" in findings[0].fix_steps[0].command


def test_memory_embedding_evaluator_treats_config_error_status_as_repairable() -> None:
    findings = evaluate_memory_embedding(
        {
            "status": "config_error",
            "requestedProvider": "openai",
            "effectiveProvider": None,
            "model": "text-embedding-3-small",
            "retrievalMode": "hybrid",
            "error": "missing",
        }
    )

    assert findings[0].id == "memory_embedding.config.error"
    assert findings[0].severity == "warn"


def test_memory_embedding_evaluator_treats_fts_fallback_as_optional_info() -> None:
    findings = evaluate_memory_embedding(
        {
            "status": "fts_only",
            "requestedProvider": "auto",
            "effectiveProvider": "none",
            "model": "fts-only",
            "retrievalMode": "hybrid",
            "reason": "local_unavailable",
        }
    )

    assert findings[0].id == "memory_embedding.fts_only"
    assert findings[0].severity == "info"


def test_memory_embedding_evaluator_treats_partial_ready_diagnostic_as_incomplete() -> None:
    findings = evaluate_memory_embedding({"status": "ok"})

    assert findings[0].id == "memory_embedding.diagnostic.incomplete"
    assert findings[0].severity == "warn"
    assert _impact(findings[0]) == "degrades"
    assert "effectiveProvider" in findings[0].detail
    assert [step.command for step in findings[0].fix_steps] == [
        "agentos memory status --deep --json",
        "agentos gateway restart",
    ]


def test_memory_embedding_evaluator_does_not_report_unknown_status_as_ready() -> None:
    findings = evaluate_memory_embedding(
        {
            "status": "warming_up",
            "requestedProvider": "auto",
            "effectiveProvider": "local",
            "model": "bge-small",
            "retrievalMode": "hybrid",
            "reason": "local_available",
        }
    )

    assert findings[0].id == "memory_embedding.status.unknown"
    assert findings[0].severity == "warn"
    assert _impact(findings[0]) == "degrades"
    assert "warming_up" in findings[0].detail
    assert [step.command for step in findings[0].fix_steps] == [
        "agentos memory status --deep --json",
        "agentos gateway restart",
    ]
