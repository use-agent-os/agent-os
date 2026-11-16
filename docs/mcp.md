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
defer_tool_threshold = 8        # defer_loading = "auto": per-server limit
defer_total_tool_threshold = 20 # ... and the limit across all servers
instructions_max_chars = 2000   # server text quoted in the system prompt

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
defer_loading = "auto"          # "auto" | true | false

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
| `defer_loading` | all | `"auto"` | Keep this server's tool schemas behind `tool_search`. See [Deferred tools](#deferred-tools). |

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

## How the agent sees MCP servers

A tool schema says how to call a tool, not when. Three things tell the agent
the rest.

**A "Connected MCP Servers" section in the system prompt.** One entry per
connected server the session can use: its name, tool count, the
`instructions` the server returned from `initialize` (its own account of what
it is for and how to use it), and its tool names — or, for a deferred server,
a pointer to `tool_search`:

```text
## Connected MCP Servers

External tool servers connected over MCP. Their tools are named `mcp_*` ...

- **fetch** (2 tools): <untrusted source='mcp:fetch/instructions'>Fetches web pages. Prefer fetch_url for a single page.</untrusted>
  - Tools: mcp_fetch_feed, mcp_fetch_url
- **github** (41 tools): <untrusted source='mcp:github/instructions'>GitHub issues, pull requests and code search.</untrusted>
  - 41 not loaded yet (e.g. mcp_add_comment, mcp_create_issue, ...): call `tool_search` with keywords or exact names — its list marks them `[MCP server "github"]` — then call the tool.
```

Server instructions are written by the server, so they are quoted in the same
untrusted envelope as tool results, with whitespace collapsed, and capped:
`mcp.instructions_max_chars` (default 2000) is shared evenly between the
servers that sent any; `0` leaves the text out and keeps the list. The section
is part of the cached system prompt. It is sorted by server and tool name and
does not depend on what a session has loaded, so it changes only when a server
connects, disconnects or changes its tools. A session whose tool policy hides
a server's tools is not told about that server.

**A server prefix on every tool description.** `[MCP server "fetch"] Fetch a
URL…`, so a tool can be tied back to its server from the schema alone and in
the `tool_search` catalog. A tool's `title` is added when the description does
not already say it.

**Tool annotations.** The hints a server declares (`readOnlyHint`,
`destructiveHint`, `idempotentHint`, `openWorldHint`) are appended to the
description as `Server hints: read-only, idempotent.` — only the ones the
server actually set. A tool declared read-only may also run alongside other
tool calls of the same model turn; every other MCP tool runs alone, as before.
Annotations are a server's word about itself: they affect scheduling and what
the model reads, never authorization. In particular a read-only hint does not
exempt a tool from `[tools.invoke] allow` or from any tool policy.

### Deferred tools

Every loaded tool costs its full schema on every request. A deferred server's
tools are left out of the request and listed, one line each, in the
`tool_search` tool's catalog; the agent loads the ones it needs by name or
keyword, and they stay loaded for the session (see
[deferred tool loading](configuration.md)).

| `defer_loading` | Effect |
|---|---|
| `"auto"` (default) | Deferred when the server registers more than `mcp.defer_tool_threshold` tools (8), or when all MCP servers together register more than `mcp.defer_total_tool_threshold` (20). |
| `true` | Always deferred. |
| `false` | Never deferred; every schema rides on every request. |

The total is taken across all servers that connected at boot, so they all get
the same answer; a server that connects later (`mcp.connect`, a background
reconnect, a `tools/list_changed`) is judged against what is registered then,
and the others are not re-judged. Nothing is deferred for a session that
cannot use `tool_search` (an agent with a tool allowlist, a cron surface), or
when `tools.defer_loading = false`: those sessions get the schemas up front.
Use `tool_filter` to cut a server down rather than deferral when most of its
tools are never wanted.

### Resources and prompts

When a server has resources or prompts to offer, AgentOS registers generic
access tools for it:

| Tool | Does |
|---|---|
| `mcp_<server>_resources_list` | Resources and resource templates, with URIs. |
| `mcp_<server>_resources_read` | Read one resource by `uri`. Binary content is described, not returned. |
| `mcp_<server>_prompts_list` | Prompt templates and their arguments. |
| `mcp_<server>_prompts_get` | Render a prompt (`name`, `arguments`) as a message list. |

They are registered only if the server advertises the capability *and* lists
at least one resource (or template) or prompt when it connects — many servers
advertise both and have neither. They are deferred unless the server sets
`defer_loading = false`, return server text in the untrusted envelope, and
pass through `tool_filter` under their short names (`resources_read`), so
`tool_filter.exclude = ["resources_*", "prompts_*"]` turns them off. A server
that gains its first resource after connecting gets them at its next reconnect.

## Speed

- **Boot.** All enabled servers connect at the same time, each under its own
  connect timeout, so the gateway waits for the slowest server instead of the
  sum of all of them. A server that fails or times out does not hold up or
  affect the others; it goes to the background retry below. Tools are
  registered in config order once the connects finish, so which server keeps a
  short `mcp_<tool>` name does not depend on connect speed.
- **Boot still waits for MCP.** The gateway does not start serving until the
  connects have finished or timed out. Letting tools appear a moment after
  startup would change the system prompt and tool list of sessions already
  running (a prompt-cache miss mid-conversation) and let a cron or channel
  turn that fires at startup run without its tools. The wait is bounded by the
  largest connect timeout.
- **Concurrent calls.** A stdio server's requests are multiplexed on its pipe
  by JSON-RPC id: a slow tool call no longer blocks other calls to the same
  server. The HTTP transports already worked this way.
- **Timeouts and cancellation.** A call that exceeds `tool_timeout_seconds`
  fails with a message naming the server and telling the agent it may retry
  once; a stdio server is also sent `notifications/cancelled` so it can stop
  working. The timeout costs that one call, not the connection.
- **Catalog changes.** On `notifications/tools/list_changed` AgentOS lists the
  server's tools again (after a 250 ms quiet period, so a burst is one
  refresh) and registers the new catalog, with `tool_filter` and the deferral
  rule applied as at connect. Logged as `mcp.tools_refreshed`.
- **A server dying mid-call.** Every call in flight fails at once with a
  "disconnected" error rather than waiting out its timeout, and the reconnect
  below starts.

`scripts/mcp_client_bench.py` measures boot and call overlap against the test
fixture server.

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
- **When an HTTP transport drops.** One failed request (an auth error, a 5xx,
  a dropped connection) can close a Streamable HTTP or SSE session for good.
  The server's tools stay registered, `mcp.status` reports it as
  `connected: false`, and the next tool call reconnects first
  (`mcp.transport_reconnecting`). A call that was in flight when it dropped
  fails and is not resent; one that never left is sent again after the
  reconnect.
- **OAuth tokens.** The saved access token is refreshed with the saved refresh
  token before it expires, so short-lived tokens need no new sign-in. If the
  server refuses the refresh, the saved tokens are dropped and the server waits
  for the user (`build_services.mcp_server_authentication_required` at boot,
  `mcp.reconnect_needs_sign_in` from a background retry, which then stops).
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
- **The agent does not use a server's tools.** Check the server's row in
  `mcp.status`: `deferred: true` means its tools sit behind `tool_search`
  (set `defer_loading = false` for a server the agent should always have at
  hand), and `hasInstructions: false` means the server gave the agent no
  description of itself beyond its tool names.
- **A stdio server will not start.** `agentos mcp doctor <name>` checks that
  `command` resolves and `cwd` exists. Arguments belong in `args`, not in
  `command`.
- **Nothing connects.** `mcp.enabled` must be `true`, and the server's own
  `enabled` too; `agentos mcp status` shows both.
- **An edit has no effect.** It is probably held: run `agentos config reload`,
  then restart the gateway.

See [`configuration.md`](configuration.md#mcp-configuration) for the transport
details (stdio framing, SSE endpoint handling, SSRF guard, result envelopes).
