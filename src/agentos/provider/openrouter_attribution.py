"""OpenRouter application attribution headers."""

from __future__ import annotations

from urllib.parse import urlparse

OPENROUTER_APP_REFERER = "https://useagentos.dev"
OPENROUTER_APP_TITLE = "AgentOS"
OPENROUTER_APP_CATEGORIES = "cli-agent,personal-agent"


def is_openrouter_url(url: str | None) -> bool:
    """Return whether a URL points at OpenRouter's hosted API."""
    if not url:
        return False
    raw = url.strip()
    if not raw:
        return False
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").lower()
    return host == "openrouter.ai" or host.endswith(".openrouter.ai")


def openrouter_app_headers(url: str | None) -> dict[str, str]:
    """Return attribution headers only for real OpenRouter API URLs."""
    if not is_openrouter_url(url):
        return {}
    return {
        "HTTP-Referer": OPENROUTER_APP_REFERER,
        "X-OpenRouter-Title": OPENROUTER_APP_TITLE,
        "X-OpenRouter-Categories": OPENROUTER_APP_CATEGORIES,
    }
