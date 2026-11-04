"""Environment references for credentials scoped to one RPC client."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from typing import Any

_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_RESERVED = frozenset(
    {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "proxy-connection",
        "keep-alive",
        "te",
        "trailer",
        "upgrade",
        "content-type",
        "accept",
        "user-agent",
    }
)


def validate_header_env(references: Mapping[str, str]) -> dict[str, str]:
    """Normalize header names; configuration contains env names, never values."""
    normalized: dict[str, str] = {}
    for name, env_name in references.items():
        lower = name.lower()
        if not _HEADER_NAME.fullmatch(name) or lower in _RESERVED:
            raise ValueError("RPC authentication header name is invalid or reserved")
        if lower in normalized:
            raise ValueError("RPC authentication header names must be case-insensitively unique")
        if not _ENV_NAME.fullmatch(env_name):
            raise ValueError("RPC authentication headers must reference environment variable names")
        normalized[lower] = env_name
    return normalized


def resolve_header_env(references: Mapping[str, str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for name, env_name in references.items():
        value = os.environ.get(env_name, "")
        if (
            not value
            or value != value.strip()
            or any(ord(char) < 32 or ord(char) > 126 for char in value)
        ):
            raise ValueError(f"RPC header environment variable {env_name} is missing or invalid")
        headers[name] = value
    return headers


def redact_text(text: str, credentials: Sequence[str]) -> str:
    # Also cover the token alone when a provider echoes a Bearer credential.
    secrets = set(credentials)
    for credential in credentials:
        if credential.lower().startswith("bearer "):
            secrets.add(credential[7:])
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def _redact_value(value: Any, credentials: Sequence[str]) -> Any:
    if isinstance(value, str):
        return redact_text(value, credentials)
    if isinstance(value, dict):
        return {
            redact_text(key, credentials): _redact_value(item, credentials)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item, credentials) for item in value]
    return value


def redact_rpc_errors(value: Any, credentials: Sequence[str]) -> Any:
    """Preserve successful RPC results, sanitizing only diagnostic error payloads."""
    if not credentials:
        return value
    if isinstance(value, list):
        return [redact_rpc_errors(item, credentials) for item in value]
    if isinstance(value, dict) and "error" in value:
        return {**value, "error": _redact_value(value["error"], credentials)}
    return value
