"""Interactive auth provisioning when the gateway resolves a public bind.

The CLI is the single control point for bind posture: when ``gateway run``
resolves a wildcard bind whose auth does not protect it, this prompt offers
to generate + persist a token (recommended), serve break-glass for this
session only, or cancel. Non-interactive invocations never prompt — the
startup guard (``enforce_public_bind_auth_guard``) raises exactly as before.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from enum import Enum

from agentos.gateway.config import (
    GatewayConfig,
    _mode_protects_public_bind,
    is_public_bind,
)
from agentos.gateway.config_persist import persist_config


class AuthProvisionOutcome(Enum):
    PROCEED = "proceed"  # config is ready to serve (token set, or opt-in set)
    CANCEL = "cancel"  # operator chose to abort; CLI exits 0
    UNCHANGED = "unchanged"  # nothing to do (not public, or already decided)


# ASCII-only glyphs so the warnings still print on Windows consoles configured
# for legacy GBK code pages (where U+26A0 / em-dash crash Rich's legacy
# renderer with UnicodeEncodeError).
_WILDCARD_WARNING = (
    "[yellow]WARNING: gateway is bound to a wildcard address - "
    "reachable from every interface.[/yellow]"
)
_LAN_OPEN_WARNING = (
    "[yellow]  auth.mode=none + wildcard bind + "
    "allow_unauthenticated_public = LAN-open. "
    "Anyone reachable on this network can use the chat, sessions, "
    "and config surfaces with your provider credentials.[/yellow]"
)
_BYPASS_NOTE = (
    "[yellow]  Bypass / elevated mode remains owner-only and "
    "is unreachable from non-loopback peers; the chat UI will "
    "self-disable that pill.[/yellow]"
)


def provision_public_bind_auth(
    config: GatewayConfig,
    *,
    interactive: bool,
    prompt: Callable[[str], str] = input,
    emit: Callable[[str], None] = print,
) -> tuple[AuthProvisionOutcome, GatewayConfig]:
    """Ensure a public bind is authenticated before the gateway starts.

    Returns the outcome plus the (possibly updated) config. Loopback binds
    and already-protected/opted-in public binds return ``UNCHANGED``; so do
    non-interactive runs, which fall through to the startup guard unchanged.
    """
    if not is_public_bind(config.host):
        return (AuthProvisionOutcome.UNCHANGED, config)

    emit(_WILDCARD_WARNING)
    if config.auth.mode == "none" and config.auth.allow_unauthenticated_public:
        # Without the opt-in, start_gateway_server refuses the unauthenticated
        # combination outright (enforce_public_bind_auth_guard) — no point
        # warning that the network is open right before the refusal explains
        # itself.
        emit(_LAN_OPEN_WARNING)
    emit(_BYPASS_NOTE)

    if _mode_protects_public_bind(config.auth) or config.auth.allow_unauthenticated_public:
        return (AuthProvisionOutcome.UNCHANGED, config)

    if not interactive:
        # Non-TTY (CI, pipes): never prompt; the startup guard raises
        # ValueError downstream exactly as today.
        return (AuthProvisionOutcome.UNCHANGED, config)

    emit(
        "[yellow]This public bind has no authentication configured. "
        "Choose how to proceed:[/yellow]"
    )
    emit("  [1] Generate a token and enable auth (recommended)")
    emit("  [2] Serve without authentication (break-glass, this run only)")
    emit("  [3] Cancel")
    try:
        choice = prompt("Select [1/2/3] (default 1): ").strip()
    except (EOFError, KeyboardInterrupt):
        return (AuthProvisionOutcome.CANCEL, config)

    if choice == "3":
        return (AuthProvisionOutcome.CANCEL, config)

    if choice == "2":
        new_config = config.model_copy(
            update={"auth": config.auth.model_copy(update={"allow_unauthenticated_public": True})}
        )
        emit(_LAN_OPEN_WARNING)
        emit("[yellow]  Break-glass is session-only; nothing was written to the config.[/yellow]")
        return (AuthProvisionOutcome.PROCEED, new_config)

    # [1] and everything else (empty / invalid input) — the safe default.
    token = secrets.token_urlsafe(32)
    new_config = config.model_copy(
        update={"auth": config.auth.model_copy(update={"mode": "token", "token": token})}
    )
    _persist_auth_only(new_config, emit)
    emit(f"[bold]Gateway token:[/bold] {token}")
    emit("[dim]Clients authenticate with: Authorization: Bearer <token>[/dim]")
    return (AuthProvisionOutcome.PROCEED, new_config)


def _persist_auth_only(new_config: GatewayConfig, emit: Callable[[str], None]) -> None:
    """Persist ONLY the auth change, not the one-off CLI overrides.

    ``run_gateway`` injects ``host``/``port``/``debug`` from CLI flags into the
    in-memory config before this prompt runs, so writing ``new_config`` wholesale
    would freeze a one-off ``--listen 0.0.0.0 --debug`` into the config file. We
    instead reload the on-disk config and apply only the new auth, so a plain
    ``agentos gateway run`` next time is unaffected.
    """
    config_path = new_config.config_path
    try:
        to_write = (
            GatewayConfig.load(config_path) if config_path else new_config.model_copy(deep=True)
        )
        to_write = to_write.model_copy(update={"auth": new_config.auth})
        if config_path and not to_write.config_path:
            to_write.config_path = config_path
        persist_config(to_write)
    except OSError as exc:
        emit(
            f"[yellow]WARNING: could not persist the token to the config file ({exc}). "
            "It stays active for this session only.[/yellow]"
        )
    else:
        emit(f"[green]auth.mode=token enabled; token saved to {config_path}[/green]")
