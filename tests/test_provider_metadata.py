from __future__ import annotations

from agentos.provider.protocol import (
    ProviderConnectionConfig,
    ProviderMetadata,
    provider_connection_config,
    provider_metadata,
)
from agentos.session.compaction import build_compaction_config_from_provider


class _MetadataProvider:
    provider_name = "openai"

    def provider_metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            provider_name="openai",
            provider_kind="openrouter",
            model="meta/model",
            base_url="https://metadata.example/v1",
        )

    def provider_connection_config(self) -> ProviderConnectionConfig:
        return ProviderConnectionConfig(
            provider_kind="openrouter",
            model="meta/model",
            api_key="meta-key",
            base_url="https://metadata.example/v1",
        )


def test_provider_metadata_prefers_read_only_protocol() -> None:
    provider = _MetadataProvider()

    metadata = provider_metadata(provider)

    assert metadata.provider_name == "openai"
    assert metadata.provider_kind == "openrouter"
    assert metadata.model == "meta/model"
    assert "meta-key" not in repr(metadata)


def test_provider_connection_config_keeps_secret_out_of_repr() -> None:
    config = provider_connection_config(_MetadataProvider())

    assert config.api_key == "meta-key"
    assert "meta-key" not in repr(config)


def test_compaction_config_uses_provider_connection_config_protocol() -> None:
    cfg = build_compaction_config_from_provider(_MetadataProvider())

    assert cfg.api_key == "meta-key"
    assert cfg.model == "meta/model"
    assert cfg.base_url == "https://metadata.example/v1"


def test_model_info_name_property_and_calculate_cost() -> None:
    import pytest

    from agentos.provider.types import ModelInfo

    info_with_display = ModelInfo(
        provider="openrouter",
        model_id="anthropic/claude-3.5-sonnet",
        display_name="Claude 3.5 Sonnet",
        input_cost_per_1k=0.003,
        output_cost_per_1k=0.015,
    )
    assert info_with_display.name == "Claude 3.5 Sonnet"
    assert info_with_display.calculate_cost(input_tokens=1000, output_tokens=1000) == pytest.approx(
        0.018
    )
    assert info_with_display.calculate_cost(input_tokens=2000, output_tokens=500) == pytest.approx(
        0.0135
    )

    info_bare = ModelInfo(
        provider="ollama",
        model_id="llama3:latest",
    )
    assert info_bare.name == "llama3:latest"
    assert info_bare.calculate_cost(100, 100) == 0.0

