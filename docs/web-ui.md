# Web UI

The AgentOS Web UI is the local control console for setup, chat sessions,
approvals, channels, logs, agents, usage, and operational status. It is the
best surface when you want browser-based chat, visible tool activity, durable
approvals, and a quick view of runtime health.

## Start the Web UI

Run the gateway in the foreground:

```sh
agentos gateway run
```

Open:

```text
http://127.0.0.1:18791/control/
```

Or start a managed background gateway:

```sh
agentos gateway start --json
agentos gateway status
```

The default gateway binds to `127.0.0.1` for safety.

For gateway lifecycle, host/port, and exposure details, see
[`gateway.md`](gateway.md).

## React bundle and development

Published AgentOS wheels contain a prebuilt React/Vite Control UI. The gateway
serves that single application for the base route and every deep link; there is
no legacy-console fallback. The SPA shell is never cached, while
content-hashed assets are cached immutably.

A source checkout must build the browser bundle before starting the production
gateway:

```sh
python scripts/build_control_ui.py build
npm --prefix frontend run check
```

The shared build command requires Node.js 22 or newer, performs the clean npm
install, builds and verifies the bundle, and generates its exact third-party
license ledger. The source-install scripts perform it automatically. If a
checkout is started without a bundle, the Control UI returns an actionable
`503` instead of a blank page or a different interface.

For hot reload during frontend work, run the gateway and Vite together:

```sh
agentos gateway run
npm --prefix frontend run dev
```

Vite serves `/control/` and proxies the gateway API and WebSocket. Production
builds use relative assets plus a server-provided runtime base, so custom
non-root mounts such as `/console/` work without rebuilding. Root, `/api`, and
`/ws` mounts are rejected because they overlap the gateway's public endpoints.

## Main Areas

| Area | Use it for |
| --- | --- |
| Chat | Run and resume chat sessions, inspect tool activity, publish artifacts, and use manual compact controls. |
| Overview / Health | See readiness, provider state, memory state, sandbox posture, and recovery hints. |
| Channels | Inspect configured channel adapter status and jump to Agent setup for configuration changes. |
| Skills | Browse available skills. |
| Sessions | Inspect durable conversations and operational state. |
| Agents | Manage durable agent entries. |
| Usage | Inspect token and estimated-cost rollups. |
| Cron | View and manage scheduled runs. |
| MCP Servers | Add local or remote MCP servers, connect tools live, and complete OAuth authorization. |
| Agent setup | Configure the agent through Guided capability setup or the complete Advanced Form/YAML editor. |
| Logs | Inspect runtime logs and diagnostics. |
| Approvals | Respond to sensitive tool-call approval requests. |

## Agent Setup

Open **Settings > Agent setup**, or go directly to:

```text
http://127.0.0.1:18791/control/settings
```

The workspace has two modes:

- **Guided** configures the provider, Pilot Router, channels, search, memory,
  image generation, audio, and readiness through the specialized onboarding
  operations for each capability.
- **Advanced** exposes Form and YAML editors for the complete current gateway
  configuration. Form mode groups every current `GatewayConfig` key, keeps an
  **Other** section for future keys, searches across all sections, supports
  keyboard tab navigation, and provides accessible show/hide controls for
  sensitive inputs.

The workspace uses one redacted `config.snapshot` composite for a coherent view
of the catalog, readiness, active configuration, revision, persistence target,
restart state, and runtime/disk coherence. Writes still use the narrowly scoped
onboarding operations or `config.patch` / `config.apply`; the snapshot itself is
read-only. A legacy gateway fallback is used only when `config.snapshot` is not
implemented, not when the snapshot call fails or returns an invalid payload.

Existing bookmarks remain valid: `/control/setup` opens Guided mode and
`/control/config` opens Advanced mode. Both are compatibility paths into the
same Agent setup workspace, not separate sidebar destinations. Guided and
Advanced stay mounted while you switch between them so in-progress drafts are
not discarded merely by changing modes.

### Configuration write safety

Guided and Advanced are two editors for the same persisted configuration. Each
write carries the snapshot `revision` as `expectedRevision` when available. If
another editor advances that revision while a local draft is open, Save is
disabled until the stale draft is explicitly discarded and the latest snapshot
is loaded. A one-time secret is cleared from the saved form immediately after a
successful write, even when the follow-up refresh cannot complete.

Gateway configuration writes are persist-first transactions:

1. validate a cloned candidate configuration;
2. verify `expectedRevision` and the live/disk coherence state;
3. atomically persist the candidate;
4. update the running configuration;
5. hot-apply the adapters that support it.

If persistence fails, the running configuration and runtime adapters remain
unchanged. If persistence succeeds but a hot-apply adapter fails, the persisted
configuration remains authoritative and the gateway records a pending restart
reason instead of reporting the change as fully live.

An external edit to the active config file is fail-closed. In that state,
`config.snapshot` returns `revision: null`, `diskDiverged: true`, and
`writeBlocked: true`; Agent setup shows **Out of sync** and disables both Guided
and Advanced writes. Reload or restart the gateway with that file before
editing again. Refreshing only the browser cannot reconcile a stale running
configuration.

`host`, `port`, `config_path`, `auth.token`, and `auth.password` are display-only
in Advanced. The WebSocket config mutation RPCs reject attempts to retarget the
active config file or replace runtime-owned authentication credentials. Other
authentication settings, such as `auth.mode`, remain editable and may require a
gateway restart.

Restart reporting is deliberately conservative. Mutation responses expose
`restartRequired`; subsequent snapshots expose cumulative `pendingRestart` and
`restartReasons` for boot-captured settings and failed hot applies. This covers
areas such as memory, channels, sandbox/bind posture, task runtime, server
middleware, MCP discovery, tools/skills loading, state paths, heartbeat, and
diagnostics. Treat a restart advisory as part of completing the change rather
than as a save failure.

## Chat Sessions

The chat UI supports:

- streaming assistant output;
- tool-call cards;
- artifact cards;
- pending message queue behavior while compaction or runtime work is in flight;
- manual `/compact`;
- per-turn usage and savings metadata when available;
- copyable session keys.

Use the session selector to switch between existing sessions. Copy the session
key when reporting a bug or asking another AgentOS surface to inspect the
same session.

## Manual Compaction

Long sessions can be compacted from chat. If no compaction is needed, the UI
reports:

```text
Already within context budget; no compact was applied
```

If compaction is running, wait for its terminal state before assuming the next
message has the compacted context. See
[`features/compaction-and-cache.md`](features/compaction-and-cache.md).

## Artifacts

When the agent publishes a file, the Web UI shows an artifact card. Use artifact
cards for:

- generated HTML prototypes;
- reports and briefings;
- exported data files;
- PDFs, slide decks, images, and other generated outputs.

For channel delivery limits and artifact recovery, see
[`artifacts-and-media.md`](artifacts-and-media.md).

## Approvals

Some tools require confirmation. The approvals area gives operators a durable
place to approve or deny sensitive actions instead of burying the decision in
chat text.

Use the approvals area when:

- the agent wants to write files;
- a command requires elevated permissions;
- a channel or external action needs human confirmation;
- unattended automation should pause before a risky operation.

## MCP Servers

Open **Settings > MCP Servers** to add and manage external MCP connections. The
screen supports local `stdio`, legacy SSE, and Streamable HTTP servers. Remote
servers can use custom headers or OAuth. OAuth tokens are stored separately
from `config.toml` in the AgentOS state directory. AgentOS applies mode `0600`
inside a `0700` directory on POSIX systems; Windows uses the current user's
state-directory ACL.

The featured Robinhood Trading connection uses:

```text
https://agent.robinhood.com/mcp/trading
```

It is configured as Streamable HTTP with OAuth. Saving the connection opens the
provider authorization flow and loads its tools without requiring a gateway
restart. Agentic trading involves significant risk. Review the server's access
and action permissions before authorizing it.

## Logs and Diagnostics

For local diagnosis:

```sh
agentos diagnostics on
agentos gateway status
agentos doctor
```

Use the Web UI logs and health views to correlate provider readiness, channel
state, session state, and user-visible errors.

## Safety

The Web UI is local by default. If you bind the gateway to a public interface,
configure token auth and network controls first:

```sh
agentos gateway run --listen 0.0.0.0 --port 18791
```

Do not expose an unauthenticated gateway to the public internet.

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
