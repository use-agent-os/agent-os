"""Shared gateway RPC helpers for CLI commands."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import typer

from agentos.cli.output import emit_error
from agentos.cli.url_utils import normalize_gateway_url


def default_gateway_url() -> str:
    """Return the configured gateway WebSocket URL."""

    if gateway_url := os.environ.get("AGENTOS_GATEWAY_URL"):
        return normalize_gateway_url(gateway_url)
    if config_path := os.environ.get("AGENTOS_GATEWAY_CONFIG_PATH"):
        return gateway_url_from_config(config_path)
    return normalize_gateway_url("ws://localhost:18791/ws")


def _client_host(host: str) -> str:
    if host == "0.0.0.0":
        return "127.0.0.1"
    if host == "::":
        return "::1"
    return host


def _format_url_host(host: str) -> str:
    if ":" in host and not (host.startswith("[") and host.endswith("]")):
        return f"[{host}]"
    return host


def gateway_url_from_config(config_path: str | Path) -> str:
    """Return the WebSocket URL implied by an AgentOS config file."""

    from agentos.onboarding.config_store import load_config

    config = load_config(config_path)
    host = _format_url_host(_client_host(str(config.host or "127.0.0.1")))
    return normalize_gateway_url(f"ws://{host}:{int(config.port)}/ws")


def _target_gateway_url(
    *,
    gateway_url: str | None,
    config_path: str | Path | None,
) -> str:
    if gateway_url is not None:
        return normalize_gateway_url(gateway_url)
    if config_path is not None:
        return gateway_url_from_config(config_path)
    return default_gateway_url()


def default_gateway_token(config_path: str | Path | None = None) -> str | None:
    """Resolve the auth token used to connect to the gateway.

    Resolution order (matches the gateway's own config-loading
    precedence, so a single ``agentos.toml`` works for both ends):

      1. ``AGENTOS_GATEWAY_TOKEN`` env var (explicit override)
      2. ``GatewayConfig.auth.token`` (from the explicit CLI config path,
         ``AGENTOS_GATEWAY_CONFIG_PATH`` env var,
         ``./agentos.toml``, or ``~/.agentos/config.toml``)
      3. ``None`` — the connect handshake omits ``auth`` and only
         works against ``[auth] mode = "none"`` deployments.

    Returns ``None`` instead of raising on any load failure so the
    CLI still tries to connect (UNAUTHORIZED is more informative than
    a config-loader crash).
    """
    env = os.environ.get("AGENTOS_GATEWAY_TOKEN", "").strip()
    if env:
        return env
    try:
        from agentos.gateway.config import GatewayConfig

        effective_config_path = (
            str(config_path)
            if config_path is not None
            else os.environ.get("AGENTOS_GATEWAY_CONFIG_PATH", "").strip()
        )
        cfg = GatewayConfig.load(effective_config_path or None)
        token = getattr(getattr(cfg, "auth", None), "token", None)
        if isinstance(token, str) and token.strip():
            return token.strip()
    except Exception:  # noqa: BLE001 — config-loader robustness
        pass
    return None


def rpc_error_exit_code(code: str | None) -> int:
    """Map gateway error codes to the CLI exit-code convention."""

    normalized = (code or "").upper()
    if normalized in {"INVALID_REQUEST", "NOT_FOUND", "METHOD_NOT_FOUND"}:
        return 2
    if normalized in {"CONFLICT", "STATE_CONFLICT", "LIFECYCLE_CONFLICT"}:
        return 3
    return 1


def _apply_version_skew_policy(client: Any, *, json_output: bool) -> None:
    """Warn (gateway older) or refuse (gateway newer) on CLI/gateway skew.

    Runs once per invocation off the version reported in the connect
    handshake. A gateway-newer refusal exits non-zero with remediation; the
    ``AGENTOS_ALLOW_VERSION_SKEW=1`` escape hatch is honoured inside the policy.
    """

    from agentos import __version__
    from agentos.cli.version_skew import SkewReporter, VersionSkewError

    gateway_version = getattr(client, "server_version", None)
    try:
        SkewReporter().check(cli_version=__version__, gateway_version=gateway_version)
    except VersionSkewError as exc:
        emit_error(str(exc), json_output=json_output, code="VERSION_SKEW")
        raise typer.Exit(3) from exc


def _maybe_emit_update_notice(*, config_path: str | Path | None) -> None:
    """Best-effort passive update notice; never affects command outcome."""

    try:
        from agentos import __version__
        from agentos.cli.update_notice import maybe_emit_update_notice
        from agentos.gateway.config import GatewayConfig

        try:
            config = GatewayConfig.load(
                str(config_path)
                if config_path is not None
                else os.environ.get("AGENTOS_GATEWAY_CONFIG_PATH") or None
            )
        except Exception:  # noqa: BLE001 - config-loader robustness; notice is optional
            config = None
        maybe_emit_update_notice(current_version=__version__, config=config)
    except Exception:  # noqa: BLE001 - a courtesy line must never break a command
        pass


async def run_gateway_call(
    action: Callable[[Any], Awaitable[Any]],
    *,
    gateway_url: str | None = None,
    config_path: str | Path | None = None,
    json_output: bool = False,
) -> Any:
    """Connect to the gateway, run ``action(client)``, and close cleanly."""

    from agentos.cli import gateway_client as gateway_client_module

    client = gateway_client_module.GatewayClient()
    try:
        target_url = _target_gateway_url(gateway_url=gateway_url, config_path=config_path)
        await client.connect(target_url, token=default_gateway_token(config_path))
        _apply_version_skew_policy(client, json_output=json_output)
        result = await action(client)
        _maybe_emit_update_notice(config_path=config_path)
        return result
    except SystemExit as exc:
        message = str(exc)
        emit_error(message, json_output=json_output, code="GATEWAY_UNAVAILABLE")
        raise typer.Exit(1) from exc
    except gateway_client_module.GatewayRPCError as exc:
        emit_error(
            exc.message,
            json_output=json_output,
            code=exc.code,
            details=exc.data,
        )
        raise typer.Exit(rpc_error_exit_code(exc.code)) from exc
    except (ConnectionError, OSError) as exc:
        emit_error(str(exc), json_output=json_output, code="GATEWAY_UNAVAILABLE")
        raise typer.Exit(1) from exc
    finally:
        await client.close()


def run_gateway_sync(
    action: Callable[[Any], Awaitable[Any]],
    *,
    gateway_url: str | None = None,
    config_path: str | Path | None = None,
    json_output: bool = False,
) -> Any:
    """Synchronous Typer-friendly wrapper around :func:`run_gateway_call`."""

    return asyncio.run(
        run_gateway_call(
            action,
            gateway_url=gateway_url,
            config_path=config_path,
            json_output=json_output,
        )
    )


def confirm_or_exit(prompt: str, *, yes: bool, json_output: bool = False) -> None:
    """Require confirmation unless ``--yes`` was passed."""

    if yes:
        return
    if json_output:
        emit_error(
            "confirmation required; rerun with --yes to execute",
            json_output=True,
            code="CONFIRMATION_REQUIRED",
        )
        raise typer.Exit(2)
    typer.confirm(prompt, abort=True)
