from __future__ import annotations

import shlex
from pathlib import Path

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
    "agentos memory repair list",
    "agentos memory repair run",
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
    return f"{command} --config {shlex.quote(str(config_path))}"
