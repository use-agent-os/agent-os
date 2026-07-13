from agentos.provider.openrouter_attribution import (
    OPENROUTER_APP_CATEGORIES,
    OPENROUTER_APP_REFERER,
    OPENROUTER_APP_TITLE,
    is_openrouter_url,
    openrouter_app_headers,
)


def test_openrouter_app_headers_match_app_attribution_contract() -> None:
    assert openrouter_app_headers("https://openrouter.ai/api/v1") == {
        "HTTP-Referer": "https://useagentos.dev",
        "X-OpenRouter-Title": "AgentOS",
        "X-OpenRouter-Categories": "cli-agent,personal-agent",
    }
    assert OPENROUTER_APP_REFERER == "https://useagentos.dev"
    assert OPENROUTER_APP_TITLE == "AgentOS"
    assert OPENROUTER_APP_CATEGORIES == "cli-agent,personal-agent"


def test_openrouter_url_detection_accepts_openrouter_hosts_only() -> None:
    assert is_openrouter_url("https://openrouter.ai/api/v1")
    assert is_openrouter_url("https://api.openrouter.ai/v1")
    assert not is_openrouter_url("https://openrouter.example/api/v1")
    assert not is_openrouter_url("http://localhost:4000/v1")


def test_openrouter_app_headers_skip_non_openrouter_urls() -> None:
    assert openrouter_app_headers("https://api.openai.com/v1") == {}
    assert openrouter_app_headers("http://localhost:4000/v1") == {}
