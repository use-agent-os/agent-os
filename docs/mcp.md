# MCP Client Guide

AgentOS connects to external [Model Context Protocol](https://modelcontextprotocol.io)
servers and offers their tools to the agent. This page is about that *client*
side. To expose AgentOS itself to other MCP clients, see
[`mcp-server.md`](mcp-server.md).

## Quick start

```sh
# A local stdio server, only its read tools
agentos mcp add fs --command npx \
  --arg -y --arg @modelcontextprotocol/server-filesystem --arg /srv/data \
  --include 'read_*,list_*'

# Check it before the gateway uses it
agentos mcp doctor fs --probe

# Apply
agentos gateway restart
```

Or use **Settings › MCP** in the Web UI, which also handles OAuth sign-in for
Streamable HTTP servers.

## Configuration

Servers live in `[[mcp.servers]]` in the active config file. The client as a
whole is switched by `mcp.enabled`; `agentos mcp add` turns it on when it adds
the first server (an explicit `mcp.enabled = false` next to existing servers is
left alone).

```toml
[mcp]
enabled = true
connect_timeout_seconds = 5.0   # default for every server

[[mcp.servers]]
name = "fs"
transport = "stdio"             # stdio | sse | streamable_http
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "/srv/data"]
cwd = "/srv"                    # stdio only
env = { LOG_LEVEL = "warn" }    # stdio only; merged over the gateway's env
enabled = true
connect_timeout_seconds = 20.0  # overrides [mcp].connect_timeout_seconds
tool_timeout_seconds = 30.0     # per tool call
tool_filter.include = ["read_*", "list_*"]
tool_filter.exclude = ["read_secret"]

[[mcp.servers]]
name = "docs"
transport = "streamable_http"
url = "https://mcp.example.com/mcp"
headers = { "X-Team" = "platform" }
oauth = false
```

| Key | Transports | Default | Notes |
|---|---|---|---|
| `name` | all | — | Unique; used in `mcp.status`, the CLI, and long tool names. |
| `enabled` | all | `true` | `false` keeps the definition but never connects it. |
| `transport` | — | `stdio` | `stdio`, `sse` (legacy), or `streamable_http`. |
| `command`, `args` | stdio | — | The server process. Resolved on the gateway's `PATH` (or the `PATH` in `env`). |
| `cwd` | stdio | gateway's | Working directory; `~` expands; must exist. |
| `env` | stdio | `{}` | Extra environment. Skill signing keys are never passed on. |
| `url`, `headers` | sse, streamable_http | — | `http(s)://` only, through the SSRF guard. |
| `oauth` | streamable_http | `false` | Sign in from Settings › MCP. |
| `connect_timeout_seconds` | all | global | Time allowed to connect and list tools. |
| `tool_timeout_seconds` | all | `30` | Per tool call. |
| `tool_filter.include` / `.exclude` | all | `[]` | See below. |

All of these keys are optional, and configs written before they existed keep
working unchanged. `agentos migrate openclaw` carries OpenClaw's `enabled`,
`cwd`, `connectionTimeoutMs` and `toolFilter` over.

### Tool filters

`tool_filter.include` and `tool_filter.exclude` are lists of `fnmatch` globs
(`*`, `?`, `[abc]`) matched case-sensitively against the tool names the
*server* uses (`read_file`, not `mcp_read_file`). An empty `include` admits
every tool; a tool matching any `exclude` pattern is dropped even if `include`
matched it. Filtering happens before registration, so a filtered tool never
reaches the model or the tool registry.

`agentos mcp probe <name>` shows every tool the server offers and which the
filter registers; `agentos mcp doctor <name> --probe` also warns about a
pattern that matches no tool, or a filter that leaves nothing.

### Tool names

Tools register as `mcp_<tool>`. When two servers offer the same tool name, the
first configured server keeps the short name and later ones get
`mcp_<server>_<tool>`. `mcp.status` returns a `toolMap` of registered name to
server tool name.

## The `agentos mcp` command

| Command | What it does |
|---|---|
| `list [--json]` | Configured servers, from the config file. |
| `status [--json]` | Same, plus counts and the `mcp.enabled` switch. |
| `add <name> …` | `--command C [--arg A]… [--cwd D] [--env K=V]…`, or `--url U [--transport sse\|streamable_http] [--header K=V]… [--oauth]`; `--include` / `--exclude` (repeatable or comma-separated), `--tool-timeout`, `--connect-timeout`, `--disabled`, `--replace`. |
| `remove <name>` | Delete the definition. |
| `enable <name>` / `disable <name>` | Toggle `enabled`. |
| `probe <name> [--timeout S]` | Connect, list tools, close. Exit 1 if unreachable. |
| `doctor <name> [--probe]` | Validate (command on `PATH`, `cwd` exists, URL shape, fields that do not apply to the transport), then optionally probe. Exit 1 on any error. |

Every command takes `--config PATH` (default: the active config) and `--json`.
None needs a running gateway. `probe` runs from the CLI process: a stdio server
is spawned as a child of your shell, with your environment, not the gateway's.

Writes validate the whole config, keep a backup, and rewrite the file — comments
in it are not preserved. `mcp` is an operator-only setting, so with a gateway
running a write is held until you run `agentos config reload` from your own
terminal; MCP servers are connected at gateway start, so restart it to pick the
change up.

## Reconnects

- **At boot.** A server that fails or times out is logged once
  (`build_services.mcp_server_failed` / `…_timeout`) and retried in the
  background after 30 s, then 60 s, 120 s, … capped at 10 minutes. Each failed
  retry logs one `mcp.reconnect_failed` line with the next delay; success logs
  `mcp.reconnected` and registers the server's tools.
- **When a stdio server dies.** If the process exits on its own, AgentOS logs
  `mcp.server_exited`, unregisters that server's tools (so the model is not
  offered tools that can only fail), and reconnects on the same schedule,
  registering the tools again when it is back.
- **What is left alone.** OAuth servers that are not signed in wait for the
  user. Disabled servers are never retried. `mcp.connect`, `mcp.disconnect` and
  `mcp.oauth.clear` take a server over from the background retry, and a
  deliberate disconnect is never revived.

While a retry is pending, `mcp.status` reports it on the server's row:

```json
"retry": {"reason": "boot", "attempts": 2, "lastError": "timed out", "nextRetryAt": 1763300000.0}
```

`reason` is `boot` or `exited`; `nextRetryAt` is a Unix timestamp.

## Troubleshooting

- **Expected tools are missing.** `agentos mcp probe <name>` — look at the
  *Registered* column, then adjust `tool_filter`.
- **A stdio server will not start.** `agentos mcp doctor <name>` checks that
  `command` resolves and `cwd` exists. Arguments belong in `args`, not in
  `command`.
- **Nothing connects.** `mcp.enabled` must be `true`, and the server's own
  `enabled` too; `agentos mcp status` shows both.
- **An edit has no effect.** It is probably held: run `agentos config reload`,
  then restart the gateway.

See [`configuration.md`](configuration.md#mcp-configuration) for the transport
details (stdio framing, SSE endpoint handling, SSRF guard, result envelopes).
