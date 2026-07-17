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
)
from agentos.gateway.config_persist import persist_config
from agentos.gateway.scopes import is_loopback_bind


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
    # Use the SAME predicate as enforce_public_bind_auth_guard: any non-loopback
    # bind (a wildcard 0.0.0.0/:: OR a specific LAN IP like 192.168.1.50) needs
    # auth. is_public_bind only catches wildcards, so a LAN bind would otherwise
    # skip the prompt and hit the raw startup ValueError (PR #25 review, P1 #2).
    if is_loopback_bind(config.host):
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
        # Break-glass means "serve openly", so force auth.mode="none" too — not
        # just the opt-in flag. Leaving an unsupported mode (password /
        # trusted-proxy / typo) in place would let the guard start the gateway
        # while resolve_auth has no resolver for it, so the WS/chat surface
        # still rejects everyone — the operator's "serve without auth" intent
        # would silently not hold (PR #25 review, P2). None is session-only;
        # nothing is persisted (host/mode/opt-in are all runtime-only here).
        new_config = config.model_copy(
            update={
                "auth": config.auth.model_copy(
                    update={"mode": "none", "allow_unauthenticated_public": True}
                )
            }
        )
        emit(_LAN_OPEN_WARNING)
        emit("[yellow]  Break-glass is session-only; nothing was written to the config.[/yellow]")
        return (AuthProvisionOutcome.PROCEED, new_config)

    # [1] and everything else (empty / invalid input) — the safe default.
    token = secrets.token_urlsafe(32)
    new_config = config.model_copy(
        update={"auth": config.auth.model_copy(update={"mode": "token", "token": token})}
    )
    # persist_config writes only the auth change: host/port/debug are runtime-
    # only and are never frozen from the in-memory config (see config_persist).
    try:
        persist_config(new_config)
    except OSError as exc:
        emit(
            f"[yellow]WARNING: could not persist the token to the config file ({exc}). "
            "It stays active for this session only.[/yellow]"
        )
    else:
        emit(f"[green]auth.mode=token enabled; token saved to {new_config.config_path}[/green]")
    emit(f"[bold]Gateway token:[/bold] {token}")
    emit("[dim]Clients authenticate with: Authorization: Bearer <token>[/dim]")
    return (AuthProvisionOutcome.PROCEED, new_config)
