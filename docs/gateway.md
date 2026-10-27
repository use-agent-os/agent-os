# Gateway

The AgentOS gateway is the local server behind the Web UI, channels, RPC
clients, sessions, approvals, diagnostics, and usage views. Most day-to-day
AgentOS surfaces work best when the gateway is running.

Use this page when you want to start, stop, inspect, expose, or troubleshoot
the gateway.

## Foreground Gateway

Run the gateway in the current terminal:

```sh
agentos gateway run
```

Open the control console:

```text
http://127.0.0.1:18791/control/
```

Stop a foreground gateway with `Ctrl+C`. `Ctrl+C` and `SIGTERM` (what
`agentos gateway stop`, the desktop app, launchd and systemd send) run the
same orderly shutdown: new turns are refused, turns in flight get up to 30
seconds to finish, channels stop, connected clients receive a `shutdown`
event, sockets and MCP clients close, and the process exits 0. A second
signal cancels the turns still running instead of waiting for them.

## Managed Background Gateway

Start a managed background process and wait for readiness:

```sh
agentos gateway start --json
```

Inspect it:

```sh
agentos gateway status
agentos gateway status --json
```

Restart or stop it:

```sh
agentos gateway restart
agentos gateway stop
```

Use the managed gateway for the Web UI, channels, scheduled jobs, and local
automation that should survive the current terminal tab.

`agentos gateway status --json` also reports what a supervisor needs from a
live gateway: `drain_budget_ms` (how long a stop may take to drain turns),
`active_turns` (turns running right now) and `runtime` — the gateway's own
readiness `state` (`starting`, `ready`, `draining`, `safe_mode`), what is
`failing`, and the `safe_mode` details. The top-level `state` stays the
process state (`running`, `unmanaged`, `not_started`, ...). Add `--deep` to
also report an installed service.

## Run as a Service

Keep the gateway running across crashes, logouts and reboots by installing it
as a user service:

```sh
agentos gateway install            # port from config (usually 18791)
agentos gateway install --port 18792 --config ~/.agentos/config.toml
agentos gateway status --deep      # adds the service: installed, loaded/active
agentos gateway uninstall
```

| Platform | Unit | Restart policy |
|---|---|---|
| macOS | LaunchAgent `~/Library/LaunchAgents/dev.useagentos.gateway.plist` (label `dev.useagentos.gateway`), logs in `~/Library/Logs/AgentOS/gateway-service.log` and `gateway-service.err.log` | `KeepAlive`, `RunAtLoad`, `ThrottleInterval 10` (at most one respawn per 10 s), `ExitTimeOut 54` |
| Linux | systemd user unit `~/.config/systemd/user/agentos-gateway.service` (`systemctl --user daemon-reload` + `enable --now`) | `Restart=always`, `RestartSec=5`, `RestartPreventExitStatus=78`, `KillMode=mixed`, `TimeoutStopSec=54` |
| Windows | not supported | — |

The unit runs the absolute path of the `agentos` you installed it with
(`~/.local/bin/agentos` keeps working after `uv tool upgrade`) as
`agentos gateway run --port N [--config PATH]`, with a `PATH` that starts with
`~/.local/bin`, `/opt/homebrew/bin` and `/usr/local/bin`, followed by the
installing shell's own `PATH`, so harness CLIs installed with Homebrew, npm,
nvm, volta, bun or pyenv are found. `AGENTOS_STATE_DIR` and
`AGENTOS_GATEWAY_CONFIG_PATH` are carried into the unit when they are set in
the installing shell, and `AGENTOS_SERVICE` (`launchd` or `systemd`) tells
the gateway it runs under a service manager. The stop timeout (54 s) is the
gateway's worst-case stop — 30 s for turns to finish, 2 s to cancel the rest,
2 s for clients' closing handshakes and 10 s for the HTTP server to stop —
plus a 10 s margin, derived from the bounds the shutdown itself uses.

Installing the same unit again is a no-op, also from a shell whose `PATH`
differs (the installed unit is kept; uninstall and install again to refresh
its `PATH`). A different unit under the same name (another port or config, or
a job with that label loaded from elsewhere) is refused: run
`agentos gateway uninstall` first. `--no-start` writes the unit without
loading it. `uninstall` with nothing installed succeeds. On Linux, run
`loginctl enable-linger $USER` once if the gateway should keep running after
you log out.

`agentos gateway stop` does not stop a service-run gateway: the service
manager would start it again. It exits `3` and says how to stop it instead —
`launchctl bootout gui/$(id -u)/dev.useagentos.gateway` until the next login
on macOS, `systemctl --user stop agentos-gateway.service` on Linux — or remove
the service with `agentos gateway uninstall`. `agentos gateway restart`
restarts a service-run gateway in place.

A configuration error (exit `78`) is not restarted by systemd. launchd has no
such switch and would respawn it every 10 s, so under launchd the gateway
waits before exiting `78`: 5 s, doubling with each consecutive config failure
up to 5 minutes (a streak older than 15 minutes starts over, a successful
boot resets it), and says so in `~/Library/Logs/AgentOS/gateway-service.log`.

The desktop app adopts a gateway that already answers on its port, so it works
with an installed service; do not let both start their own gateway on the same
state directory.

### Crash-loop breaker (safe mode)

Every boot is recorded in the gateway database, with a heartbeat every 30 s.
When a boot finds **3 unclean boots that ended in the last 5 minutes** — runs
that ended without a clean shutdown (killed, crashed, out of memory), counted
by their last heartbeat, so a gateway that ran for an hour before crashing
counts as crashing now — it starts in **safe mode** instead of restarting the
same crash forever:

- the control plane starts as usual: Web UI, WebSocket, HTTP, `agentos doctor`,
  logs, config;
- channels, the cron scheduler and the heartbeat are held back, so no
  automatic turn runs;
- `/ready` answers `200` with `state: "safe_mode"` and the reason, and
  `agentos doctor` reports a `gateway.safe_mode` finding with the way out;
- safe mode clears by itself once the 5-minute window drains without another
  unclean boot (checked every 30 s), and everything it held back starts then.

To leave it sooner, fix the cause (the log has the crash) and run
`agentos channels start --yes`: it starts every channel, the scheduler and the
heartbeat, and stops counting the recorded crashes. `agentos channels start
NAME --yes` starts a single channel and keeps safe mode on for the rest — the
way to find which channel crashes the gateway.

A clean stop never counts: `agentos gateway stop`, a service manager's or the
desktop app's SIGTERM, `Ctrl+C` and an in-place restart are all clean. Nor
does a boot that failed before it served (a bad config, a failed migration):
safe mode could not help it. If the boot ledger cannot be read the breaker is
skipped for that boot (fail open), never the start.

## Supervisor Contract

What a process supervisor (the desktop app, launchd, systemd, a script) can
rely on:

| Signal | Contract |
|---|---|
| `GET /health` | Liveness: `200` whenever the HTTP server answers. |
| `GET /ready` | `{ready, state, failing, startup, safe_mode, drain_budget_ms, uptime_ms}`. `state` is `starting` (boot sidecars still running; `failing` names them, e.g. `["channels"]`), `ready`, `draining` (stopping or restarting in place; `failing: ["draining"]`) or `safe_mode` (`failing: ["safe_mode"]`). `503` while `starting` or `draining`; `200` for `ready` and `safe_mode`, where the control plane serves so the cause can be fixed. In `ready` and `safe_mode`, `failing` also names each channel that is down (failed to start, or stopped on bad credentials) as `channel:<name>`; that does not make the gateway unready. `startup.failed.channels` records which channels failed at boot and why. `turn_recovery` counts the [interrupted turns](#interrupted-turns) found at boot by state (`found`, `pending`, `running`, `resumed`, `closed`, `tombstoned`, `cancelled`, `reported`, `expired`) plus `auto_resume`; it never makes the gateway unready. |
| `GET /startupz` | `200` once every boot sidecar settled (channels started, router preloaded), `503` before; `pending` lists what is left. |
| WebSocket `connect` during boot | Answered with a retryable error `UNAVAILABLE`, `details: {code: "STARTUP_SIDECARS", retry_after_ms}`, then close `1013` (`gateway starting`). Clients retry after `retry_after_ms`; the CLI retries for up to 30 s, the web console and desktop keep "connecting". |
| `shutdown` event | `{reason, restart_expected_ms}`, flushed to every connection before its socket closes with `1012`. A number is an in-place restart with an estimate of when the gateway serves again (this boot's own duration plus teardown, at least 3 s); `null` is a terminal stop (a signal, `Ctrl+C`). A client that stopped reading may still miss it (each flush is bounded to 1 s); on close `1012` without the frame, reconnect with the normal backoff. |
| Exit code `78` | `EX_CONFIG`: a startup failure only a configuration change fixes — a config file that is not valid TOML or does not validate, a schema migration that cannot apply, a public bind without auth. Repair, then retry; systemd's `RestartPreventExitStatus=78` stops a restart loop. Other startup failures exit `1`. |

The desktop app follows it: a spawned gateway is "running" once `/ready`
answers `200`; on exit `78` it runs `agentos doctor --json` once, retries the
start once, and otherwise shows the exit code with doctor's top finding.

### Sleep and wake

A laptop gateway checks itself every 30 s. A gap of more than 45 s between two
checks during which the process used almost no CPU means the machine slept:
the gateway logs `gateway.thaw`, restarts the channels that are idle (their
sockets are stale after a sleep), wakes the scheduler and tells connected
clients to refresh (`health` event). Every 5 minutes (after a 60 s start-up
grace) it also sweeps channel health and restarts a channel that is
`disconnected`, `stuck` (its retry loop gave up), `start_failed` (its last
start raised, e.g. no network at login) or `stale-socket` (no inbound event
for 30 minutes on a socket or polling transport), at most once per two sweeps
each.

A restart that fails — the network is not back yet right after a wake — never
leaves the channel stopped: the gateway retries the start with backoff
(1 s, 2 s, 4 s, ... six times; the channel reads `retrying`), and if that
runs out the channel reads `stuck` and the next sweep starts over. Only a
channel that failed on bad credentials (`terminal`) or that the platform
`blocked` is never restarted automatically: doctor reports it with the fix.
A channel you stopped yourself (`agentos channels logout`) stays `stopped`.

`agentos channels status` shows each channel's `state` and the reason it is
down, and whether safe mode is on; `--json` adds `last_event_at`,
`terminal_reason`, `start_error`, and the policy plus the last sweep and thaw
(`restarted`, `failed`, `skipped` per channel) under `health_policy`.

## Host and Port

Use a different port:

```sh
agentos gateway run --port 18792
agentos gateway status --port 18792
```

Pick where the gateway listens with a bind mode:

| Mode | Listens on | Notes |
| --- | --- | --- |
| `loopback` | `127.0.0.1` | The default. Only this machine can connect. |
| `lan` | `0.0.0.0` (every interface) | Token auth is required; the gateway refuses to start without it. |
| `tailnet` | the Tailscale IPv4 **and** `127.0.0.1` | Reads the address from `tailscale ip -4`. Fails to start if Tailscale is missing, stopped or logged out — it never falls back to `0.0.0.0`. Token auth is required. |
| `custom` | the `host` you configured | What `--listen` / `--bind` select. |

```sh
agentos gateway run --bind-mode tailnet
agentos gateway start --bind-mode lan
```

Or persist it:

```toml
[gateway]
bind = "tailnet"
```

Bind to a specific host:

```sh
agentos gateway run --listen 127.0.0.1 --port 18791
```

`--listen` is an alias for the bind host and wins over `--bind` when both are
provided; either one means bind mode `custom`. `gateway start` records the
bind mode it used, so a plain `gateway status`, `gateway stop` or `gateway
restart` finds that gateway again (`--bind-mode` is accepted there too). On
macOS the Tailscale app's bundled CLI is used when `tailscale` is not on PATH. Precedence, highest first:
`--listen`, `--bind`, `--bind-mode`, `AGENTOS_LISTEN`, `AGENTOS_GATEWAY_HOST`,
`[gateway] bind`, the `host` key, `127.0.0.1`.

## Safety Defaults

The gateway defaults to loopback scope, usually `127.0.0.1`, because the local
gateway controls chat, tools, sessions, channels, approvals, and configuration.

Public binding is opt-in:

```sh
agentos gateway run --bind-mode lan --port 18791
```

Do not expose a gateway to an untrusted network without token auth and a network
boundary you understand. To reach a gateway from another machine, prefer
keeping it on loopback and tunnelling in (SSH, Tailscale Serve) or putting an
authenticating reverse proxy in front: see [`remote-access.md`](remote-access.md).
`agentos doctor` flags risky exposure settings.

### Browser origins

Every bind applies the same origin policy to the WebSocket handshake, to
`/api/*` requests that carry an `Origin` header, and to CORS. A browser page may
drive the gateway when its origin is:

1. listed in `control_ui.allowed_origins`, `cors.allowed_origins` or
   `[gateway] public_origin`, or is the gateway's own `http://localhost:<port>` /
   `http://127.0.0.1:<port>` (always allowed, so a LAN bind never locks out the
   browser on the gateway's machine);
2. the same origin as the address the request was sent to, when that address is
   private: loopback, `10/8`, `172.16/12`, `192.168/16`, link-local,
   `100.64.0.0/10` (tailnet addresses), IPv6 `fc00::/7`, or a `.local` /
   `.ts.net` name. Opening `http://192.168.1.20:18791/control/` on a LAN bind
   works with no configuration;
3. a loopback origin on another port (a dev server) when the connection itself
   comes from this machine and not through a proxy.

Anything else is refused with `403` — for a WebSocket, at the handshake, before
the upgrade — and CORS never answers `*`. On a loopback bind only loopback addresses count for
rule 2. Requests without an `Origin` header — the CLI, `curl`, scripts — are not
affected; token auth still applies.

The `Host` header is checked too (a DNS-rebinding guard): the gateway answers to
loopback, to the hostnames of the declared origins, and off loopback to any IP
address and `.local` / `.ts.net` names. A request for any other name gets `400`
naming the setting to change.

When you serve the console from a public address or a DNS name, declare the
origin you open it at:

```toml
[gateway]
public_origin = "https://agent.example.com"
```

Without it, the gateway refuses to start the Control UI on a specific public
address (it would load and then fail every request). The fix is in the error
message; `control_ui.enabled = false` keeps the API running without the console.

## Configuration Path

Use a specific config file:

```sh
agentos gateway run --config /path/to/agentos.toml
agentos gateway status --config /path/to/agentos.toml
```

AgentOS also reads standard configuration locations described in
[`configuration.md`](configuration.md).

## Remote Status Check

Inspect a gateway URL directly:

```sh
agentos gateway status --gateway ws://localhost:18791/ws
```

This is useful when a client or MCP bridge is configured with an explicit
gateway URL.

## When to Restart

Restart the gateway after changing:

- provider or router configuration;
- channel configuration;
- durable agent entries;
- global sandbox posture;
- search or image-generation setup;
- environment variables used by configured providers.

```sh
agentos gateway restart
```

That works for a gateway started with `agentos gateway start` (stop, then
start) and for one started any other way — the desktop app, launchd, systemd
or a terminal `agentos gateway run` — which restarts itself **in place**: the
gateway drains the turns in flight, re-executes itself under the same process
id so every launcher keeps tracking it, and comes back on upgraded code. The
CLI confirms the restart from the gateway's readiness probe. Not available on
Windows, where the process image cannot be replaced.

The same in-place restart is reachable from chat:

- `/restart` in the web console or the desktop app (operators only, no
  setting needed). The socket drops and reconnects; a toast reports when the
  gateway is back.
- `/restart` from a paired Telegram, Discord or Slack sender, and the agent's
  own `gateway` tool, once `[gateway_control] chat_restart = true` is set and
  the gateway has been restarted once. The reply acknowledges, then "Gateway
  restarted (v…, Ns)" lands in the same chat or thread after boot.

`/restart` waits up to 30 seconds for active turns to finish; `/restart force`
(or `agentos gateway restart --force`) cancels them instead. A second
request while one is pending is reported, not queued.

### Interrupted turns

A turn the gateway stops under -- a restart that cancels it, or a crash --
is marked interrupted (task status `abandoned`, reason
`shutdown_interrupted` or `process_restart`). On the next boot, once channels
are up, the gateway looks at every session whose newest turn is such a turn
and decides from the transcript what to do:

- The reply (or an error) was already written, or there is nothing to
  answer: nothing to resume.
- The user's message was never answered, or the last recorded tool round
  finished: the turn is resumed with the session's usual tools.
- Tool calls were issued with no recorded result (their outcome is unknown):
  the turn is resumed with **read and status tools only** (file, git, memory
  and session reads, `web_search`), so it checks what happened instead of
  acting again. Shell, file writes, trading, outbound messages, approvals and
  arbitrary HTTP are not available to that turn; the user's next message
  gets every tool back.

The resume is a short internal continuation message, not a replay of the
user's message, and its reply goes where a normal reply would: the web
console and the desktop app show it in the session, a channel turn's reply is
sent to the same chat or thread. A turn from a paired channel is resumed only
while that sender's pairing still stands. Each interrupted turn gets at most
three attempts (counted across restarts, with backoff); after that, or for a
turn older than 24 hours, the session gets a note asking the user to send a
message to continue. A new message on the session cancels a resume that has
not started yet. Cron, heartbeat, subagent and ACP turns are never resumed,
and neither is anything while the gateway is in safe mode. A turn that asked
for the restart itself (the `gateway` tool's `restart`, or a shell command
that restarts, stops or updates the gateway), or any turn of the session the
restart request came from, is not resumed either, so it cannot restart the
gateway again: its session gets a note instead.

`[task_runtime] resume_interrupted_turns = false` turns the automatic resume
off: interrupted turns are only reported (the note in the session, `/ready`
`turn_recovery`, an `agentos doctor` finding).

```toml
[task_runtime]
resume_interrupted_turns = true   # default
```

## Troubleshooting

Check status and readiness:

```sh
agentos gateway status
agentos doctor
curl -s http://127.0.0.1:18791/ready
```

If `/ready` reports `state: "safe_mode"`, the gateway restarted after crashing
three times in five minutes and is holding channels, cron and heartbeat back;
see [Crash-loop breaker](#crash-loop-breaker-safe-mode).

If `agentos gateway run` exits `78`, the configuration is the problem: run
`agentos doctor` and fix what it reports before starting again.

If the port is busy:

```sh
agentos gateway run --port 18792
```

If the Web UI cannot connect, confirm that the URL matches the gateway bind
host and port.

Read next:

- [`remote-access.md`](remote-access.md)
- [`http-api.md`](http-api.md)
- [`web-ui.md`](web-ui.md)
- [`configuration.md`](configuration.md)
- [`channels.md`](channels.md)
- [`troubleshooting.md`](troubleshooting.md)

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
