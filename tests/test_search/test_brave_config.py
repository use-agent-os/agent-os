from __future__ import annotations

from agentos.gateway.config import GatewayConfig
from agentos.search.providers.brave import BraveSearchProvider
from agentos.tools.builtin import web


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
