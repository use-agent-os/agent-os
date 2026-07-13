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
