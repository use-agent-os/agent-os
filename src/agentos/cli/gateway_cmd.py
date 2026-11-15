"""Gateway run command — start ASGI gateway with uvicorn."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import typer
from rich.markup import escape

from agentos.cli.gateway_auth_prompt import (
    AuthProvisionOutcome,
    provision_public_bind_auth,
)
from agentos.cli.gateway_lifecycle import (
    UNMANAGED_GATEWAY_RUNNING,
    GatewayLifecycleManager,
    GatewayLifecycleResult,
    remote_gateway_status,
)
from agentos.cli.ui import ACCENT_MARKUP, console
from agentos.cli_quoting import config_cli_arg
from agentos.gateway.bind_modes import (
    BindResolution,
    BindResolutionError,
    CommandRunner,
    resolve_bind,
)
from agentos.gateway.boot import start_gateway_server
from agentos.gateway.config import (
    GatewayConfig,
    GatewayConfigError,
    is_public_bind,
    resolve_listen_address,
)
from agentos.gateway.config_persist import read_raw_bind_overrides, set_runtime_overrides
from agentos.gateway.readiness import EX_CONFIG
from agentos.paths import default_agentos_home


def _stdin_isatty() -> bool:
    """Seam for tests — CliRunner replaces sys.stdin, so patch this instead."""
    return sys.stdin.isatty()


def _load_config_or_exit(config_path: str | None, *, exit_code: int = 2) -> GatewayConfig:
    """Load the gateway config, or exit ``exit_code`` with one line naming the bad setting.

    ``gateway run`` passes ``EX_CONFIG`` (78), the supervisor contract's
    "repair the config, then retry"; every other command keeps ``2``.

    A value that fails validation -- from the config file or from an
    ``AGENTOS_GATEWAY_*`` override, which is what ``agentos config set``
    prints for the operator to export -- used to surface as a pydantic
    traceback through typer (#3100). Each error is reported as ``key: reason``
    with the environment variable named when one is set for that key, since
    that is the setting the operator has to fix or unset.
    """
    import tomllib

    from pydantic import ValidationError

    try:
        return GatewayConfig.load(config_path or os.environ.get("AGENTOS_GATEWAY_CONFIG_PATH"))
    except tomllib.TOMLDecodeError as exc:
        console.print(
            "[red]AgentOS config error:[/red] the config file is not valid TOML: "
            + escape(str(exc)),
            soft_wrap=True,
        )
        raise typer.Exit(exit_code) from exc
    except ValidationError as exc:
        console.print("[red]AgentOS config error:[/red] the gateway configuration is invalid")
        for error in exc.errors():
            loc = ".".join(str(part) for part in error.get("loc", ()))
            reason = str(error.get("msg") or "invalid value")
            env_key = "AGENTOS_GATEWAY_" + loc.upper().replace(".", "__")
            source = f" (set by {env_key})" if env_key in os.environ else ""
            # soft_wrap keeps the variable name on one line so it stays copy-pasteable.
            console.print(f"  {escape(loc)}: {escape(reason)}{escape(source)}", soft_wrap=True)
        raise typer.Exit(exit_code) from exc


def _enforce_config_approval_or_exit(config: GatewayConfig) -> None:
    from agentos.gateway.config import GatewayConfigError
    from agentos.gateway.config_approval import enforce_approval

    try:
        held = enforce_approval(config)
    except GatewayConfigError as exc:
        console.print(f"[red]Gateway could not start:[/red] {escape(str(exc))}", soft_wrap=True)
        _service_config_backoff()
        raise typer.Exit(code=EX_CONFIG) from exc
    if held:
        console.print(
            "[yellow]Held operator-only settings:[/yellow] "
            + escape(", ".join(held))
            + " differ in the config file from what you last approved, so the gateway runs "
            "on the approved values. Run `agentos config reload` from your own terminal "
            "to apply the file.",
            soft_wrap=True,
        )


def resolve_cli_bind(
    config: GatewayConfig,
    *,
    explicit_host: str | None,
    bind_mode: str | None,
    env: dict[str, str] | None = None,
    tailscale_runner: CommandRunner | None = None,
) -> BindResolution:
    """Resolve the listener for ``gateway run`` / ``gateway start``.

    An explicit ``--listen``/``--bind`` host wins and is ``custom``; then
    ``--bind-mode``; then ``AGENTOS_LISTEN`` / ``AGENTOS_GATEWAY_HOST``
    (``custom``); then ``[gateway] bind``; then the ``host`` field.
    """
    if explicit_host:
        return resolve_bind("custom", host=explicit_host)
    if bind_mode:
        return resolve_bind(bind_mode, host=config.host, tailscale_runner=tailscale_runner)
    environ = env if env is not None else dict(os.environ)
    env_host = environ.get("AGENTOS_LISTEN") or environ.get("AGENTOS_GATEWAY_HOST")
    if env_host:
        return resolve_bind("custom", host=resolve_listen_address(None, env=environ))
    return resolve_bind(
        config.gateway.bind,
        host=config.host or "127.0.0.1",
        tailscale_runner=tailscale_runner,
    )


def gateway_startup_guidance(host: str, port: int, scheme: str = "http") -> tuple[str, ...]:
    """Return operator-facing guidance shown after the gateway starts."""

    base_url = f"{scheme}://{host}:{port}"
    return (
        f"[bold]Web UI:[/bold] {base_url}/control/",
        f"[bold]API base:[/bold] {base_url}",
        f"[bold]Debug log:[/bold] {default_agentos_home() / 'logs' / 'debug.log'}",
        "[dim]Keep this terminal open. Press Ctrl+C to stop.[/dim]",
    )


def run_gateway(
    port: int | None = typer.Option(18791, "--port", "-p", help="Port to bind"),
    bind: str | None = typer.Option("127.0.0.1", "--bind", "-b", help="Host to bind"),
    listen: str = typer.Option("", "--listen", help="Host to bind (wins over --bind)"),
    debug: bool = typer.Option(False, "--debug", help="Enable debug mode"),
    config_path: str | None = typer.Option(None, "--config", help="Override config path"),
    bind_mode: str | None = typer.Option(
        None,
        "--bind-mode",
        help="loopback | lan | tailnet | custom (overrides [gateway] bind)",
    ),
) -> None:
    """Start the ASGI gateway server.

    Precedence: ``--listen`` > ``--bind`` > ``--bind-mode`` > ``AGENTOS_LISTEN``
    > ``AGENTOS_GATEWAY_HOST`` > toml ``[gateway] bind`` > toml ``host`` field >
    default ``127.0.0.1``. An explicit host counts as bind mode ``custom``.

    The toml ``host`` field was previously silently ignored — operators
    setting ``host = "0.0.0.0"`` in agentos.toml then ran the gateway
    expecting public binding and got loopback instead. The toml is now
    honoured as the fallback when no CLI flag or env var is supplied,
    matching what the field name promises.
    """
    # Load config FIRST so its ``host`` field can act as the final
    # fallback below ``AGENTOS_GATEWAY_HOST``. A config that does not load is
    # EX_CONFIG (78): a supervisor repairs it before trying again.
    try:
        config = _load_config_or_exit(config_path, exit_code=EX_CONFIG)
    except typer.Exit as exc:
        if exc.exit_code == EX_CONFIG:
            _service_config_backoff()
        raise
    if config_path and not config.config_path:
        config.config_path = str(config_path)
    # Operator-only settings the file holds unapproved stay at the approved
    # values (gateway.config_approval) before the bind, auth and anything else
    # below reads them; start_gateway_server checks again for other launchers.
    _enforce_config_approval_or_exit(config)
    # Treat the CLI ``--bind`` default as "not explicitly supplied" so the
    # env vars + toml get a chance to participate when the operator only
    # sets one of them.
    explicit_flag: str | None = listen or (bind if bind and bind != "127.0.0.1" else None)
    # Called as a plain function (tests, the main.py command) the typer default
    # is an OptionInfo, not None.
    requested_mode = bind_mode if isinstance(bind_mode, str) else None
    try:
        resolution = resolve_cli_bind(config, explicit_host=explicit_flag, bind_mode=requested_mode)
    except BindResolutionError as exc:
        console.print(f"[red]Gateway could not start:[/red] {escape(str(exc))}")
        raise typer.Exit(2) from exc
    host = resolution.host
    resolved_port = port if port is not None else config.port
    # Record the RAW on-disk values BEFORE overriding them in memory, in the
    # process-global runtime-override map that every config writer consults. A
    # one-off --listen/--port/--debug values must never be frozen into
    # config.toml by ANY config write; each key maps to its
    # pre-override on-disk value so persist restores it unless a writer marks
    # that exact field explicitly changed.
    #
    # Read the literal TOML — NOT config.host/port/debug, which GatewayConfig.load
    # has already env-merged (AGENTOS_GATEWAY_HOST etc.). An env-supplied posture
    # is itself transient (env vars are per-invocation), so recording the effective
    # value as the "on-disk original" would let a later config.patch freeze it. A
    # key absent from the TOML records as None -> persist drops it, so the
    # load-time default/env re-applies at the next boot.
    set_runtime_overrides(read_raw_bind_overrides(config.config_path))
    config = config.model_copy(update={"host": host, "port": resolved_port, "debug": debug})

    # Public-bind auth provisioning: the helper owns all public-bind warning
    # messaging and, on an interactive TTY, prompts to secure an unprotected
    # public bind (generate+persist a token or cancel). Non-TTY
    # runs never prompt — enforce_public_bind_auth_guard still refuses the
    # unsafe combination downstream, exactly as before.
    outcome, config = provision_public_bind_auth(
        config,
        interactive=_stdin_isatty(),
        emit=console.print,
    )
    if outcome is AuthProvisionOutcome.CANCEL:
        console.print("[yellow]Gateway start cancelled.[/yellow]")
        raise typer.Exit(0)

    banner_host = f"[red]{host}[/red]" if is_public_bind(host) else f"[{ACCENT_MARKUP}]{host}[/]"
    also = "".join(f" + {extra}:{resolved_port}" for extra in resolution.extra_hosts)
    console.print(
        f"[bold green]Starting AgentOS gateway[/bold green] on {banner_host}:{resolved_port}"
        f"{also} [dim](bind mode {resolution.mode})[/dim]"
    )
    scheme = "https" if (config.tls.keyfile and config.tls.certfile) else "http"
    for line in gateway_startup_guidance(host, resolved_port, scheme=scheme):
        console.print(line)

    async def _run() -> None:
        # Subscription manager is gateway-specific (WS event routing)
        from agentos.gateway.websocket import SubscriptionManager

        subscription_mgr = SubscriptionManager()

        # build_services() inside start_gateway_server handles:
        # session_manager, provider_selector, tool_registry, usage_tracker,
        # memory, skills, scheduler, search, MCP discovery.
        extra: dict[str, Any] = {}
        if resolution.extra_hosts:
            extra["extra_listen_hosts"] = resolution.extra_hosts
        try:
            server = await start_gateway_server(
                config=config,
                subscription_manager=subscription_mgr,
                run=True,
                **extra,
            )
        except BaseException as exc:
            _record_startup_failure(exc)
            raise
        _clear_service_config_failures()
        assert server._task is not None
        try:
            await server._task
        except (KeyboardInterrupt, asyncio.CancelledError):
            await server.close("keyboard_interrupt")
        # SIGTERM / SIGINT run ``close()`` in a task of their own (the server
        # installs the handlers): uvicorn returns as soon as close() stops it,
        # but the services are closed after that, so wait for the rest.
        shutdown_task = getattr(server, "shutdown_task", None)
        if shutdown_task is not None:
            try:
                await shutdown_task
            except asyncio.CancelledError:
                pass
        # An in-place restart ends the server task from inside its own
        # ``close()``. Let it finish closing the services here, inside the
        # loop, before ``asyncio.run`` tears the loop down under it.
        restarter = getattr(server, "_restarter", None)
        restart_task = getattr(restarter, "task", None)
        if restart_task is not None:
            try:
                await restart_task
            except asyncio.CancelledError:
                pass

    try:
        asyncio.run(_run())
        _exec_pending_restart()
        console.print("[yellow]Gateway stopped.[/yellow]")
    except GatewayConfigError as exc:
        # Refused public bind, failed schema migration: only a config change
        # fixes these, so a supervisor must not just restart (systemd's
        # RestartPreventExitStatus=78; the desktop runs doctor, then retries).
        # Escaped: the messages name config sections ([gateway] public_origin).
        console.print(f"[red]Gateway could not start:[/red] {escape(str(exc))}")
        console.print("Inspect: agentos doctor")
        _service_config_backoff()
        raise typer.Exit(code=EX_CONFIG) from exc
    except ValueError as exc:
        from agentos.onboarding.next_steps import env_recovery_commands
        from agentos.onboarding.status import get_onboarding_status

        console.print(f"[red]Gateway could not start:[/red] {escape(str(exc))}")
        status = get_onboarding_status(config)
        recovery_entries = env_recovery_commands(status)
        if not recovery_entries:
            embedding = getattr(getattr(config, "memory", None), "embedding", None)
            remote = getattr(embedding, "remote", None)
            env_key = str(getattr(remote, "api_key_env", "") or "").strip()
            if not env_key and config.config_path:
                try:
                    import tomllib

                    with open(config.config_path, "rb") as f:
                        raw_config = tomllib.load(f)
                    env_key = str(
                        raw_config.get("memory", {})
                        .get("embedding", {})
                        .get("remote", {})
                        .get("api_key_env", "")
                        or ""
                    ).strip()
                except (OSError, tomllib.TOMLDecodeError):
                    env_key = ""
            if env_key and not os.environ.get(env_key):
                from agentos.onboarding.next_steps import set_env_hint

                recovery_entries.append(
                    {"label": "Set memory key", "command": set_env_hint(env_key)}
                )
        for entry in recovery_entries:
            console.print(f"{entry['label']}: {entry['command']}")
        if config.config_path:
            console.print(
                f"Inspect onboarding: agentos onboard status{config_cli_arg(config.config_path)}"
            )
        raise typer.Exit(code=1) from exc
    except KeyboardInterrupt:
        console.print("\n[yellow]Gateway stopped.[/yellow]")


def _service_config_failures_path() -> Path:
    from agentos.paths import state_dir

    return state_dir("gateway", "service-config-failures.json")


def _service_config_backoff() -> None:
    """Under launchd, slow an exit-78 respawn loop down (see gateway_service)."""
    from agentos.cli.gateway_service import config_failure_backoff

    config_failure_backoff(
        counter_path=_service_config_failures_path(),
        emit=lambda message: console.print(f"[yellow]{escape(message)}[/yellow]", soft_wrap=True),
    )


def _clear_service_config_failures() -> None:
    from agentos.cli.gateway_service import clear_config_failures

    clear_config_failures(_service_config_failures_path())


def _with_service_hint(result: GatewayLifecycleResult) -> GatewayLifecycleResult:
    """Say so when the gateway that answers belongs to the installed service."""
    if result.code != UNMANAGED_GATEWAY_RUNNING:
        return result
    try:
        from agentos.cli.gateway_service import service_stop_hint

        service = _deep_service_status()
        port = service.get("port")
        if port is not None and port != result.port:
            return result
        hint = service_stop_hint(service)
    except Exception:  # noqa: BLE001 - the hint is a courtesy
        return result
    if hint:
        result.message = f"{result.message} {hint}"
        result.details = {**result.details, "service": service}
    return result


def _record_startup_failure(exc: BaseException) -> None:
    """A boot that raised before serving is not a crash loop safe mode can stop."""
    from agentos.gateway.boot_lifecycle import get_boot_tracker

    tracker = get_boot_tracker()
    if tracker is not None:
        tracker.mark_startup_failed(f"{type(exc).__name__}: {exc}")


def _exec_pending_restart() -> None:
    """Replace the process with the re-exec image a ``/restart`` planned.

    Runs outside any event loop; a no-op when no restart is pending.
    """
    from agentos.gateway.restart import get_restarter

    restarter = get_restarter()
    if restarter is not None:
        restarter.exec_if_pending()


def _resolve_lifecycle_host(*, bind: str, listen: str) -> str:
    explicit_flag: str | None = listen or (bind if bind and bind != "127.0.0.1" else None)
    return resolve_listen_address(explicit_flag)


def _lifecycle_manager(
    *,
    port: int | None,
    bind: str | None,
    listen: str,
    config_path: str | None = None,
    health_timeout: float = 60.0,
    shutdown_timeout: float = 10.0,
    bind_mode: str | None = None,
) -> GatewayLifecycleManager:
    config = _load_config_or_exit(config_path)
    host = _resolve_lifecycle_host(bind=bind or "127.0.0.1", listen=listen)
    run_bind_mode: str | None = None
    if not listen and (bind is None or bind == "127.0.0.1"):
        host = resolve_listen_address(None, default=config.host or "127.0.0.1")
        env_host = os.environ.get("AGENTOS_LISTEN") or os.environ.get("AGENTOS_GATEWAY_HOST")
        if bind_mode or (config.gateway.bind and not env_host):
            # A bind mode travels to the spawned ``gateway run`` as itself, so
            # ``tailnet`` keeps its loopback listener; ``host`` is still what
            # the lifecycle manager records and probes.
            try:
                resolution = resolve_cli_bind(config, explicit_host=None, bind_mode=bind_mode)
            except BindResolutionError as exc:
                typer.echo(f"Error: {exc}", err=True)
                raise typer.Exit(2) from exc
            host = resolution.host
            run_bind_mode = resolution.mode
    resolved_port = port if port is not None else config.port
    explicit_target = bool(listen) or (bind is not None and bind != "127.0.0.1") or bool(bind_mode)
    return GatewayLifecycleManager(
        host=host,
        port=resolved_port,
        config_path=config_path or os.environ.get("AGENTOS_GATEWAY_CONFIG_PATH") or None,
        health_timeout=health_timeout,
        shutdown_timeout=shutdown_timeout,
        bind_mode=run_bind_mode,
        follow_record=not explicit_target,
    )


def _emit_lifecycle_result(result: GatewayLifecycleResult, *, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps(result.to_payload(), ensure_ascii=False, default=str))
    elif result.ok:
        typer.echo(f"{result.state}: {result.url}")
    else:
        typer.echo(f"Error: {result.message or result.code or result.state}", err=True)

    if result.exit_code != 0:
        raise typer.Exit(code=result.exit_code)


def start_gateway(
    port: int | None = typer.Option(18791, "--port", "-p", help="Port to bind"),
    bind: str | None = typer.Option("127.0.0.1", "--bind", "-b", help="Host to bind"),
    listen: str = typer.Option("", "--listen", help="Host to bind (wins over --bind)"),
    config_path: str | None = typer.Option(None, "--config", help="Override config path"),
    health_timeout: float = typer.Option(60.0, "--timeout", help="Readiness wait timeout"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    bind_mode: str | None = typer.Option(
        None,
        "--bind-mode",
        help="loopback | lan | tailnet | custom (overrides [gateway] bind)",
    ),
) -> None:
    """Start the gateway in the background and wait for readiness."""

    manager = _lifecycle_manager(
        port=port,
        bind=bind,
        listen=listen,
        config_path=config_path,
        health_timeout=health_timeout,
        bind_mode=bind_mode if isinstance(bind_mode, str) else None,
    )
    _emit_lifecycle_result(_with_service_hint(manager.start()), json_output=json_output)


def gateway_handshake_version(
    *,
    gateway_url: str | None = None,
    config_path: str | None = None,
) -> str | None:
    """Return the running gateway's version from the connect handshake.

    Lightweight: the version is carried in the hello-ok ServerInfo, so no RPC
    method call is needed. Returns ``None`` when the gateway is unreachable.
    """

    import asyncio

    from agentos.cli import gateway_client as gcm
    from agentos.cli.gateway_rpc import _target_gateway_url, default_gateway_token

    async def _go() -> str | None:
        client = gcm.GatewayClient()
        try:
            url = _target_gateway_url(gateway_url=gateway_url, config_path=config_path)
            await client.connect(url, token=default_gateway_token(config_path))
            return client.server_version
        except SystemExit:
            return None
        except (ConnectionError, OSError):
            return None
        finally:
            await client.close()

    try:
        return asyncio.run(_go())
    except Exception:  # noqa: BLE001 - status must never crash on a version probe
        return None


def gateway_runtime_probe(
    *,
    http_base: str,
    gateway_url: str | None = None,
    config_path: str | None = None,
) -> dict[str, Any] | None:
    """The running gateway's supervisor-contract numbers, or ``None``.

    ``gateway.status`` over one handshake (no startup wait) for the
    authenticated fields (``active_turns``); the public ``/ready`` when the
    RPC is unavailable (a booting or older gateway). Never raises.
    """
    from agentos.cli import gateway_client as gcm
    from agentos.cli.gateway_rpc import _target_gateway_url, default_gateway_token

    async def _go() -> dict[str, Any] | None:
        client = gcm.GatewayClient()
        try:
            url = _target_gateway_url(gateway_url=gateway_url, config_path=config_path)
            await client.connect(url, token=default_gateway_token(config_path), startup_wait_s=0)
            payload = await client.call("gateway.status", {})
            return payload if isinstance(payload, dict) else None
        except (gcm.GatewayRPCError, SystemExit, ConnectionError, OSError):
            return None
        finally:
            await client.close()

    try:
        runtime = asyncio.run(_go())
    except Exception:  # noqa: BLE001 - status must never crash on a probe
        runtime = None
    return runtime if runtime is not None else _ready_probe(http_base)


def _ready_probe(http_base: str) -> dict[str, Any] | None:
    """``GET /ready`` (public): state/failing/safe_mode, no active turn count."""
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    request = Request(f"{http_base.rstrip('/')}/ready", method="GET")
    try:
        with urlopen(request, timeout=1.0) as response:  # noqa: S310 - operator's own gateway
            body = response.read()
    except HTTPError as exc:
        body = exc.read() if exc.code == 503 else b""
    except (OSError, URLError, TimeoutError):
        return None
    try:
        payload = json.loads(body or b"null")
    except ValueError:
        return None
    if not isinstance(payload, dict) or "ready" not in payload:
        return None
    payload.setdefault("active_turns", None)
    return payload


def _runtime_fields(runtime: dict[str, Any] | None) -> dict[str, Any]:
    """``drain_budget_ms`` / ``active_turns`` / ``runtime`` for ``status --json``.

    The lifecycle ``state`` (running, unmanaged, not_started, ...) keeps the
    top-level ``state`` key; the gateway's own readiness state is
    ``runtime.state`` (starting, ready, draining, safe_mode).
    """
    from agentos.gateway.readiness import drain_budget_ms

    if runtime is None:
        return {"drain_budget_ms": drain_budget_ms(), "active_turns": None, "runtime": None}
    budget = runtime.get("drain_budget_ms")
    return {
        "drain_budget_ms": int(budget) if isinstance(budget, int) else drain_budget_ms(),
        "active_turns": runtime.get("active_turns"),
        "runtime": {
            "state": runtime.get("state"),
            "ready": runtime.get("ready"),
            "failing": runtime.get("failing") or [],
            "safe_mode": runtime.get("safe_mode") or {"active": False},
            "startup": runtime.get("startup"),
        },
    }


def _emit_status_with_versions(
    result: GatewayLifecycleResult,
    *,
    gateway_version: str | None,
    json_output: bool,
    runtime: dict[str, Any] | None = None,
    service: dict[str, Any] | None = None,
) -> None:
    """Emit a status result annotated with BOTH versions + a skew diagnostic."""

    from agentos import __version__

    cli_version = __version__
    mismatch = bool(gateway_version and gateway_version != cli_version)
    runtime_fields = _runtime_fields(runtime)

    if json_output:
        payload = result.to_payload()
        payload["cliVersion"] = cli_version
        payload["gatewayVersion"] = gateway_version
        payload["versionMismatch"] = mismatch
        payload.update(runtime_fields)
        if service is not None:
            payload["service"] = service
        typer.echo(json.dumps(payload, ensure_ascii=False, default=str))
    else:
        if result.ok:
            typer.echo(f"{result.state}: {result.url}")
        else:
            typer.echo(f"Error: {result.message or result.code or result.state}", err=True)
        typer.echo(f"CLI version:     {cli_version}")
        typer.echo(f"Gateway version: {gateway_version or 'unknown (not running / unreachable)'}")
        live = runtime_fields["runtime"]
        if live is not None:
            turns = runtime_fields["active_turns"]
            typer.echo(
                f"Gateway state:   {live.get('state')}"
                + (f" (active turns: {turns})" if turns is not None else "")
                + f", drain budget {runtime_fields['drain_budget_ms'] // 1000}s"
            )
            safe_mode = live.get("safe_mode") or {}
            if safe_mode.get("active"):
                typer.echo(
                    f"Safe mode:       {safe_mode.get('reason')}; clears at "
                    f"{safe_mode.get('clears_at')} (override: agentos channels start --yes)",
                    err=True,
                )
        if service is not None:
            _echo_service_status(service)
        if mismatch:
            typer.echo(
                "⚠ Version mismatch — the running gateway does not match the installed "
                "CLI. Run 'agentos gateway restart' (or 'agentos upgrade') to apply it.",
                err=True,
            )

    if result.exit_code != 0:
        raise typer.Exit(code=result.exit_code)


def _echo_service_status(service: dict[str, Any]) -> None:
    if not service.get("supported"):
        typer.echo(f"Service:         {service.get('message')}")
        return
    loaded = service.get("loaded", service.get("active"))
    state = "installed" if service.get("installed") else "not installed"
    if service.get("installed") or loaded:
        state += ", loaded" if loaded else ", not loaded"
    typer.echo(f"Service:         {service.get('label')} ({state}) {service.get('path')}")


def _deep_service_status() -> dict[str, Any]:
    from agentos.cli.gateway_service import detect_platform, service_status

    return service_status(detect_platform(), home=Path.home())


def status_gateway(
    port: int | None = typer.Option(18791, "--port", "-p", help="Port to inspect"),
    bind: str | None = typer.Option("127.0.0.1", "--bind", "-b", help="Host to inspect"),
    listen: str = typer.Option("", "--listen", help="Host to inspect (wins over --bind)"),
    config_path: str | None = typer.Option(None, "--config", help="Override config path"),
    gateway_url: str | None = typer.Option(None, "--gateway", help="Remote gateway URL"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    deep: bool = False,
    bind_mode: str | None = typer.Option(
        None,
        "--bind-mode",
        help="loopback | lan | tailnet | custom (default: what gateway start recorded)",
    ),
) -> None:
    """Inspect the managed gateway process without mutating state.

    Reports BOTH the installed CLI version and the running gateway's
    RPC-reported version; a mismatch appends a restart diagnostic. A live
    gateway also reports its readiness state, active turns and drain budget;
    ``deep`` adds the installed launchd/systemd service, if any.
    """

    service = _deep_service_status() if deep else None
    if gateway_url:
        result = remote_gateway_status(gateway_url)
        version = gateway_handshake_version(gateway_url=gateway_url) if result.ok else None
        runtime = (
            gateway_runtime_probe(http_base=result.url, gateway_url=gateway_url)
            if result.ok
            else None
        )
        _emit_status_with_versions(
            result,
            gateway_version=version,
            json_output=json_output,
            runtime=runtime,
            service=service,
        )
        return

    manager = _lifecycle_manager(
        port=port,
        bind=bind,
        listen=listen,
        config_path=config_path,
        bind_mode=bind_mode if isinstance(bind_mode, str) else None,
    )
    result = manager.status()
    version = None
    runtime = None
    if result.ok and result.state in ("running", "unmanaged"):
        # Probe the gateway this command inspects (``--port``/``--listen``),
        # not the one the config file points at.
        http_base = result.health_url.rsplit("/health", 1)[0]
        ws_url = "ws" + http_base[len("http") :] + "/ws"
        version = gateway_handshake_version(gateway_url=ws_url, config_path=config_path)
        runtime = gateway_runtime_probe(
            http_base=http_base, gateway_url=ws_url, config_path=config_path
        )
    _emit_status_with_versions(
        result,
        gateway_version=version,
        json_output=json_output,
        runtime=runtime,
        service=service,
    )


def stop_gateway(
    port: int | None = typer.Option(18791, "--port", "-p", help="Port to stop"),
    bind: str | None = typer.Option("127.0.0.1", "--bind", "-b", help="Host to stop"),
    listen: str = typer.Option("", "--listen", help="Host to stop (wins over --bind)"),
    config_path: str | None = typer.Option(None, "--config", help="Override config path"),
    shutdown_timeout: float = typer.Option(10.0, "--timeout", help="Shutdown wait timeout"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    bind_mode: str | None = typer.Option(
        None,
        "--bind-mode",
        help="loopback | lan | tailnet | custom (default: what gateway start recorded)",
    ),
) -> None:
    """Stop the recorded gateway process."""

    manager = _lifecycle_manager(
        port=port,
        bind=bind,
        listen=listen,
        config_path=config_path,
        shutdown_timeout=shutdown_timeout,
        bind_mode=bind_mode if isinstance(bind_mode, str) else None,
    )
    _emit_lifecycle_result(_with_service_hint(manager.stop()), json_output=json_output)


class _RestartRpcRefusedError(Exception):
    """``gateway.restart`` could not be made, or the gateway said no."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _gateway_pid_record(config: GatewayConfig) -> dict[str, Any] | None:
    """The ``gateway.pid`` record every ``gateway run`` writes: ``pid`` + ``start_ts``.

    ``start_ts`` is what tells a re-executed gateway (same pid) apart from
    the one that accepted the restart.
    """
    path = Path(config.state_dir or default_agentos_home() / "state") / "gateway.pid"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _call_gateway_restart(
    *, gateway_url: str, config_path: str | None, mode: str
) -> dict[str, Any]:
    """Ask the gateway at ``gateway_url`` to restart itself in place (``gateway.restart``)."""

    from agentos.cli import gateway_client as gcm
    from agentos.cli.gateway_rpc import _target_gateway_url, default_gateway_token

    async def _go() -> dict[str, Any]:
        client = gcm.GatewayClient()
        try:
            url = _target_gateway_url(gateway_url=gateway_url, config_path=config_path)
            await client.connect(url, token=default_gateway_token(config_path))
            payload = await client.call("gateway.restart", {"mode": mode, "reason": "cli"})
            return payload if isinstance(payload, dict) else {}
        except gcm.GatewayRPCError as exc:
            raise _RestartRpcRefusedError(str(exc.code or "RPC_ERROR"), str(exc.message)) from exc
        except SystemExit as exc:
            raise _RestartRpcRefusedError("GATEWAY_UNAVAILABLE", str(exc)) from exc
        except (ConnectionError, OSError) as exc:
            raise _RestartRpcRefusedError("GATEWAY_UNAVAILABLE", str(exc)) from exc
        finally:
            await client.close()

    return asyncio.run(_go())


def restart_gateway_in_place(
    manager: GatewayLifecycleManager,
    *,
    config_path: str | None,
    mode: str = "safe",
) -> GatewayLifecycleResult:
    """Restart a healthy gateway this CLI does not manage, through the gateway itself.

    The pidfile path only works for a gateway ``agentos gateway start``
    spawned. One started by the desktop app, launchd, systemd or a terminal
    shows up as ``unmanaged``; it can still restart itself in place over
    ``gateway.restart`` (same pid, so every launcher keeps tracking it). The
    restart is confirmed by a changed ``start_ts`` in ``gateway.pid`` plus a
    passing readiness probe. A refusal (Windows, no gateway) keeps the
    ``unmanaged`` result and its exit code 3 with the RPC error in details.
    """
    from agentos.cli.gateway_rpc import _format_url_host

    config = _load_config_or_exit(config_path)
    before = _gateway_pid_record(config) or {}
    before_ts = before.get("start_ts")
    # The gateway this command was pointed at (``--port``/``--listen``), not
    # the CLI's default endpoint: ``probe_host`` already maps a wildcard bind
    # to loopback the way the health probe does.
    gateway_url = f"ws://{_format_url_host(manager.probe_host)}:{manager.port}/ws"
    try:
        payload = _call_gateway_restart(gateway_url=gateway_url, config_path=config_path, mode=mode)
    except _RestartRpcRefusedError as exc:
        result = manager._unmanaged_result("restart", ok=False)
        result.details = {"rpc": {"code": exc.code, "message": exc.message}}
        if exc.code == "RESTART_UNSUPPORTED":
            result.message = exc.message
        return result

    details: dict[str, Any] = {
        "restartId": payload.get("restartId"),
        "mode": payload.get("mode") or mode,
        "alreadyPending": bool(payload.get("alreadyPending")),
        "inPlace": True,
    }
    deadline = (
        time.monotonic() + max(manager.shutdown_timeout, 0.0) + max(manager.health_timeout, 0.0)
    )
    while True:
        record = _gateway_pid_record(config) or {}
        start_ts = record.get("start_ts")
        if start_ts and start_ts != before_ts and manager._probe_ready():
            return manager._result(
                "restart",
                "running",
                pid=record.get("pid"),
                managed=False,
                message="Gateway restarted in place.",
                details=details,
            )
        if time.monotonic() > deadline:
            break
        time.sleep(manager.poll_interval)
    return manager._result(
        "restart",
        "restart_timeout",
        ok=False,
        pid=before.get("pid"),
        managed=False,
        code="RESTART_TIMEOUT",
        message=(
            "The gateway accepted the restart but has not come back yet; "
            "check 'agentos gateway status'."
        ),
        details=details,
        exit_code_value=1,
    )


def restart_gateway(
    port: int | None = typer.Option(18791, "--port", "-p", help="Port to restart"),
    bind: str | None = typer.Option("127.0.0.1", "--bind", "-b", help="Host to restart"),
    listen: str = typer.Option("", "--listen", help="Host to restart (wins over --bind)"),
    config_path: str | None = typer.Option(None, "--config", help="Override config path"),
    health_timeout: float = typer.Option(60.0, "--timeout", help="Readiness wait timeout"),
    shutdown_timeout: float = typer.Option(
        10.0, "--shutdown-timeout", help="Shutdown wait timeout"
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="In-place restart only: cancel active turns instead of draining them",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    bind_mode: str | None = typer.Option(
        None,
        "--bind-mode",
        help="loopback | lan | tailnet | custom (default: what gateway start recorded)",
    ),
) -> None:
    """Restart the gateway: the recorded process, or a healthy unmanaged one in place."""

    manager = _lifecycle_manager(
        port=port,
        bind=bind,
        listen=listen,
        config_path=config_path,
        health_timeout=health_timeout,
        shutdown_timeout=shutdown_timeout,
        bind_mode=bind_mode if isinstance(bind_mode, str) else None,
    )
    status = manager.status()
    if status.state == "unmanaged":
        result = restart_gateway_in_place(
            manager, config_path=config_path, mode="force" if force else "safe"
        )
    else:
        result = manager.restart()
    _emit_lifecycle_result(result, json_output=json_output)
