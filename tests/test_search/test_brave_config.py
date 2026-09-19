from __future__ import annotations

import pytest

from agentos.gateway.config import GatewayConfig
from agentos.search.providers.brave import BraveSearchProvider
from agentos.tools.builtin import web


@pytest.fixture(autouse=True)
def clean_search_runtime() -> None:
    web.reset_search_runtime()
    yield
    web.reset_search_runtime()


def test_gateway_config_accepts_search_api_key() -> None:
    config = GatewayConfig(search_api_key="brave-test-key")

    assert config.search_api_key == "brave-test-key"


def test_brave_provider_prefers_explicit_api_key(monkeypatch) -> None:
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)

    provider = BraveSearchProvider(api_key="brave-test-key")

    assert provider._api_key == "brave-test-key"


def test_brave_provider_strips_trailing_paste_punctuation(monkeypatch) -> None:
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)

    provider = BraveSearchProvider(api_key="brave-test-key、")

    assert provider._api_key == "brave-test-key"


def test_web_search_kwargs_pass_brave_api_key() -> None:
    web.configure_search("brave", api_key="brave-test-key")

    assert web._search_provider_kwargs("brave")["api_key"] == "brave-test-key"


def test_brave_provider_falls_back_to_brave_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    monkeypatch.setenv("BRAVE_API_KEY", "legacy-brave-key")

    provider = BraveSearchProvider()

    assert provider._api_key == "legacy-brave-key"


def test_is_search_api_key_configured_detects_brave_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    monkeypatch.setenv("BRAVE_API_KEY", "legacy-brave-key")

    assert web.is_search_api_key_configured("brave") is True
