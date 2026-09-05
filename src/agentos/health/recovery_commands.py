from __future__ import annotations

from pathlib import Path

from agentos.onboarding.next_steps import _config_cli_arg

CONFIG_AWARE_COMMAND_PREFIXES = (
    "agentos gateway restart",
    "agentos gateway start",
    "agentos gateway status",
    "agentos providers configure",
    "agentos providers status",
    "agentos config ",
    "agentos search status",
    "agentos search configure",
    "agentos diagnostics status",
    "agentos memory status",
    "agentos memory raw-fallbacks list",
    "agentos configure ",
    "agentos onboard",
    "agentos sandbox ",
    "agentos channels add",
    "agentos channels edit",
    "agentos channels enable",
    "agentos channels disable",
    "agentos channels remove",
    "agentos channels list",
    "agentos channels restart",
    "agentos channels status",
)


def supports_config_option(command: str) -> bool:
    return any(command.startswith(prefix) for prefix in CONFIG_AWARE_COMMAND_PREFIXES)


def command_with_config(command: str, config_path: str | Path | None) -> str:
    if not config_path or " --config " in command or not supports_config_option(command):
        return command
    return f"{command}{_config_cli_arg(config_path)}"
