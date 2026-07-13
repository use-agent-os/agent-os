"""First-run configuration wizard."""

from __future__ import annotations

import questionary
import tomli_w
import typer

from agentos.cli.ui import console
from agentos.onboarding import get_provider_setup_spec
from agentos.paths import default_agentos_home


def _default_model_for_provider(provider: str) -> str:
    normalized = provider.strip().lower()
    if normalized == "bankr":
        return "minimax-m3"
    if normalized == "openrouter":
        return "deepseek/deepseek-v4-pro"
    if normalized == "deepseek":
        return "deepseek-v4-flash"
    return "openai/gpt-4o-mini"


def _env_key_for_provider(provider: str) -> str:
    """Return the API-key env var the gateway actually reads for ``provider``.

    Deriving it from the provider id (``provider.upper() + "_API_KEY"``) breaks
    for ids containing a hyphen (e.g. a hyphenated gateway id would yield an
    invalid ``FOO-BAR_API_KEY`` the runtime never reads), so the env key is read
    from the provider setup spec instead.
    """
    if provider == "custom":
        return "AGENTOS_LLM_API_KEY"
    try:
        env_key = get_provider_setup_spec(provider).env_key
    except KeyError:
        env_key = ""
    return env_key or "AGENTOS_LLM_API_KEY"


def run_init() -> None:
    """Create a basic AgentOS home with env and config files."""
    home = default_agentos_home()
    env_path = home / ".env"
    config_path = home / "config.toml"
    home.mkdir(parents=True, exist_ok=True)
    (home / "state").mkdir(parents=True, exist_ok=True)

    provider = questionary.select(
        "Choose provider:",
        choices=["openrouter", "bankr", "openai", "anthropic", "deepseek", "custom"],
        default="openrouter",
    ).ask()
    if not provider:
        raise typer.Exit(1)

    api_key = questionary.password("API key:").ask()
    if api_key is None:
        raise typer.Exit(1)

    default_model = questionary.text(
        "Default model:",
        default=_default_model_for_provider(provider),
    ).ask()
    if not default_model:
        raise typer.Exit(1)

    key_name = _env_key_for_provider(provider)
    existing_env = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    lines = [line for line in existing_env.splitlines() if not line.startswith(f"{key_name}=")]
    lines.append(f"{key_name}={api_key}")
    env_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    config = {
        "llm": {
            "provider": provider,
            "model": default_model,
        },
        "state_dir": str(home / "state"),
    }
    config_path.write_text(tomli_w.dumps(config), encoding="utf-8")

    console.print(f"[green]Wrote[/green] {env_path}")
    console.print(f"[green]Wrote[/green] {config_path}")
    console.print("[dim]Tip: enable shell completion with `agentos --install-completion`[/dim]")


def init_command() -> None:
    """Initialize a workspace.

    Deprecated: prefer ``agentos onboard`` for full provider/channel setup.
    Kept for compatibility with older scripts.
    """
    run_init()
