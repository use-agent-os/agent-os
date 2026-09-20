from __future__ import annotations

from agentos.tools.builtin import web


def test_search_runtime_can_reset_global_configuration() -> None:
    web.configure_search(
        "brave",
        max_results=9,
        api_key="brave-test-key",
        proxy="http://proxy.test",
        use_env_proxy=True,
        fallback_policy="network",
        diagnostics=True,
    )

    assert web.get_active_provider() == "brave"
    assert web.get_search_proxy() == "http://proxy.test"
    assert web.get_search_use_env_proxy() is True
    assert web.get_search_fallback_policy() == "network"
    assert web.get_search_diagnostics() is True
    assert web._search_provider_kwargs("brave")["api_key"] == "brave-test-key"

    web.reset_search_runtime()

    assert web.get_active_provider() == "duckduckgo"
    assert web.get_search_proxy() == ""
    assert web.get_search_use_env_proxy() is False
    assert web.get_search_fallback_policy() == "off"
    assert web.get_search_diagnostics() is False
    assert "api_key" not in web._search_provider_kwargs("brave")


def test_search_provider_name_normalization() -> None:
    from agentos.search.registry import get_provider, get_provider_spec

    spec1 = get_provider_spec("  Brave  ")
    spec2 = get_provider_spec("brave")
    assert spec1.provider_id == "brave"
    assert spec1 is spec2

    ddg_spec = get_provider_spec("DuckDuckGo")
    assert ddg_spec.provider_id == "duckduckgo"

    ddg_provider = get_provider("  DUCKDUCKGO ")
    assert ddg_provider is not None

