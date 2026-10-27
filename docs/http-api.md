# HTTP API

The AgentOS gateway is a [Starlette](https://www.starlette.io/) ASGI
application that exposes a **REST HTTP API** alongside its streaming WebSocket.
Every AgentOS surface — CLI, Web UI, channels, and external clients — talks to
this same gateway, so the HTTP endpoints below are a stable integration point
for your own applications and automation.

Use this page when you want to call AgentOS from an external app, script, or
service instead of the CLI.

## Base URL and Auth

The gateway binds to loopback by default:

```text
http://127.0.0.1:18791
```

**On the default loopback bind, no token is required.** The gateway ships with
`auth.mode = "none"`, and a loopback peer is admitted as a Control connection,
so `curl http://127.0.0.1:18791/api/...` works with no credentials.

A token is only enforced when `auth.mode = "token"`. A public address
(`0.0.0.0` / LAN) needs that mode or `trusted-proxy`: the startup guard
refuses to serve an unauthenticated public bind, and a token is
auto-generated when unset. See [`gateway.md`](gateway.md) for bind safety and
[`remote-access.md`](remote-access.md) for tunnels and reverse proxies.

When auth is enabled, send the token via HTTP headers (checked in order):

```text
Authorization: Bearer <token>
X-Agentos-Token: <token>
```

> **Note:** Query-string tokens (`?token=<token>`) are rejected (`401 Unauthorized`). Passing tokens in URL query parameters is not supported to prevent token leaks in access logs, browser history, and HTTP referrers.

The gateway implements exactly three auth modes — `none`, `token`, and
`trusted-proxy`. Any other value (including the never-implemented `"password"`,
or a typo like `"tokenn"`) is refused when the config loads rather than silently
admitting every request; the startup error names the supported modes.

`trusted-proxy` admits a request only when the transport peer is listed in
`auth.trusted_proxy` (IP literals or CIDR blocks such as `10.0.0.0/8`,
comma-separated). `X-Forwarded-For` is read only from such a peer and walked
right to left, skipping trusted hops; an address naming the gateway host
itself is ignored as spoofed.

### Failed-auth rate limiting

A *wrong* token counts against a budget per client address, shared by HTTP
and the WebSocket: ten failures within 60 seconds lock that address out for
five minutes, whatever `Origin` header its attempts carry. A locked-out
client gets `429` with `Retry-After` (seconds) and

```json
{"error": "Too many failed authentication attempts", "code": "RATE_LIMITED",
 "details": {"code": "AUTH_RATE_LIMITED", "retry_after_ms": 299000}}
```

even when it presents the right token. A request with no token is a plain
`401` and never counts. A loopback address is never locked out (each failure
is answered after a delay growing from 250 ms to 5 s), but browser requests
(those carrying `Origin`) are also counted in a second budget per address,
shared by every `Origin` value and never exempt, so a page in the operator's
browser cannot guess the token from loopback, and rotating `Origin` gains
nothing. Behind a proxy on the same host every client looks like loopback:
list the proxy in `auth.trusted_proxy` (its `X-Forwarded-For` then names the
client) or set `exempt_loopback = false`. Settings:

```toml
[auth.rate_limit]
enabled = true
max_failures = 10
window_seconds = 60
lockout_seconds = 300
exempt_loopback = true
```

`/health`, `/ready` and `/startupz` never require a token. Browser requests
that carry an `Origin` header follow the gateway's origin policy on every bind
— declared origins, a private address opened as itself, or a loopback origin
on this machine — and CORS reflects only those origins, never `*`; see
[Gateway → Browser origins](gateway.md#browser-origins).

Two route families authenticate themselves instead of with the gateway token:
ticketed downloads (`/api/v1/artifacts/download/...`, the ticket is the
credential) and inbound webhooks (`/api/v1/hooks/...`, their own `[hooks]
token`).

## Liveness and Readiness

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health`, `/healthz` | Liveness probe — `{"ok": true, "status": "live"}`. |
| `GET` | `/ready`, `/readyz` | Readiness probe — `{ready, state, failing, startup, safe_mode, drain_budget_ms, uptime_ms}`; `state` is `starting`, `ready`, `draining` or `safe_mode`. `503` while starting or draining, `200` when ready and in safe mode. `failing` also lists down channels as `channel:<name>` without making the gateway unready. The older `status` field mirrors `state`. |
| `GET` | `/startupz` | Startup probe — `200` once every boot sidecar settled (channels started, router preloaded), `503` with the `pending` list before. |

These require no auth and are safe for load balancers and container probes.
The full supervisor contract (WebSocket refusal during boot, the `shutdown`
event, exit code `78`) is in [`gateway.md`](gateway.md#supervisor-contract).

## Core Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/config` | Effective gateway configuration. |
| `GET` | `/api/system/status` | Version, uptime, active provider, auth mode, plus `circuitBreaker` (the active provider's breaker) and `circuitBreakers` (every tracked provider). |
| `GET` | `/api/sessions` | List sessions. |
| `POST` | `/api/chat` | Send a chat turn. Body: `message` (required), `sessionKey` (optional). |
| `GET` | `/api/chat/history?sessionKey=<key>` | Fetch a session transcript. |
| `GET` | `/api/agents` | List durable agents. |
| `GET` | `/api/cron` | List scheduled jobs. |
| `GET` | `/api/usage` | Token usage and cost breakdown. |
| `GET` | `/metrics` | Prometheus metrics exposition (`observability.metrics_path`). |
| `POST` | `/api/v1/rpc` | Call an allowlisted RPC method: `{id?, method, params?}` → `{id, ok, payload \| error}`. See [RPC over HTTP](#rpc-over-http). |

## Channels

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/channels/status` | Channel connection status. |
| `POST` | `/api/channels/logout` | Log a channel out. |

## Approvals and Permissions

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/approvals` | Pending approvals + current mode/patterns. |
| `POST` | `/api/approvals/settings` | Set mode (`prompt` / `auto-approve` / `auto-deny`). |
| `POST` | `/api/approvals/resolve` | Approve or deny a pending item (optional `payload` object carries ACP form answers). |
| `POST` | `/api/elevated-mode` | Set per-session elevated mode (admitted Control connection only). |

`GET /api/approvals` serializes every pending command and its arguments, so it
is rate limited per client IP like the rest of `/api/*` — just in its own
bucket, because consoles read it on every reconnect, window focus and resolve
(older consoles still poll it every 1.5s). The cap is
`AGENTOS_RATE_APPROVALS_MAX_REQUESTS` (default 300 per
`AGENTOS_RATE_WINDOW_SECONDS`, i.e. 300/min), which clears several open tabs.
Raise it only if you run more consoles than that against one gateway.

Queue changes are also pushed over the WebSocket to Control connections (never
to channel or node connections, nor to a connection holding an agent token),
for every namespace (`exec`, `plugin`, `acp`, `acp_elicitation`):

- `approval.requested` — `{id, namespace, session_key, audience_session_keys,
  created_at, expires_at, summary, tool, command, approval}` (fields that do
  not apply are omitted; `approval` is the item exactly as `GET /api/approvals`
  lists it, with the same audience as `audienceSessionKeys`).
  `audience_session_keys` is the requesting session followed by the sessions
  that spawned it, so a parent session's view can match a child's prompt.
- `approval.resolved` — `{id, namespace, decision, resolved_by, resolved_at}`,
  `decision` being `approved`, `denied`, `expired` or `cancelled` (its run was
  aborted); `resolved_by` is `operator`, `policy`, `session_elevation`,
  `timeout`, `abort` or `system`. The gateway sweeps the queue every 30 s, so
  an expiry is announced even when nothing waits on the prompt.

A row an `auto-approve` / `auto-deny` policy settles in the same call that
queued it is not announced at all. Use the REST list once per (re)connect to
catch up on what happened while the socket was down.

## Files and Media

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/v1/files/upload` | Upload a file; returns an opaque id for `chat.send`. |
| `GET`, `HEAD` | `/api/v1/artifacts/download/{conn_id}/{ticket}` | Fetch an artifact or attachment through a ticket minted with the `artifacts.download` RPC. No `Authorization` header. |
| `GET` | `/api/v1/attachments/{sha256}?sessionKey=` | Deprecated: fetch an attachment by content hash. |
| `GET` | `/api/v1/artifacts/{artifact_id}?sessionKey=` | Deprecated: fetch a generated artifact. |
| `POST` | `/api/audio/transcribe` | Transcribe audio to text. |

### Ticketed downloads

Over the WebSocket, ask for a download URL:

```json
{"type": "req", "id": "1", "method": "artifacts.download",
 "params": {"session_key": "agent:main:webchat:default",
            "artifact_id": "art-…", "transport": "http"}}
```

Use `attachment_sha256` (plus the display `name` and `mime`) instead of
`artifact_id` for a file a user attached. The answer carries `url`
(`/api/v1/artifacts/download/<conn_id>/<ticket>`), `expiresAt` (epoch ms),
`ttlSeconds`, `name`, `mime` and `size`. The ticket is 32 random bytes, lives
five minutes, is bound to the WebSocket connection that minted it and dies
when that connection closes; a connection holds at most 128 live tickets (the
oldest goes first). The URL works for `<img>`, `<audio>`, `fetch()` and plain
links without a header.

The route answers `HEAD`, one byte range (`Range: bytes=0-1023` → `206`;
several ranges get the whole file), and `?variant=thumbnail` for a PNG, JPEG or
WebP still image: a PNG no larger than 1200 px on its longest side. Every response carries `Content-Security-Policy: default-src
'none'; sandbox`, `Content-Disposition: attachment`, `Cache-Control: no-store`
and `X-Content-Type-Options: nosniff`, so an HTML or SVG file opened directly
cannot run script. An unknown, expired, revoked or foreign ticket is `404`; a
file that no longer matches its recorded hash is `409` with
`details.code = "INTEGRITY_ERROR"` (the RPC answers `INTERNAL_ERROR` with the
same reason), and a thumbnail of anything else `415` with
`details.code = "UNSUPPORTED_MEDIA_TYPE"`.

The `?sessionKey=` routes keep working for this release, with a deprecation
line in the log the first time each is used, and go away in the next. Turn
them off now with `[artifacts] legacy_session_key_urls = false`.

## RPC over HTTP

`POST /api/v1/rpc` calls one gateway RPC method without a WebSocket, behind the
same bearer auth, origin guard and rate limits as the other `/api/*` routes:

```sh
curl -X POST -H "Authorization: Bearer $AGENTOS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"id": "1", "method": "sessions.list", "params": {"limit": 5}}' \
  http://127.0.0.1:18791/api/v1/rpc
```

The body is `{id?, method, params?}` (`id` a string or integer, echoed back;
`params` an object). The answer is the WebSocket `res` frame without its
`type`: `{"id", "ok": true, "payload"}` with `200`, or `{"id", "ok": false,
"error": {code, message, details, retryable, retry_after_ms}}` with a status
from the error's category:

| Category | Status |
| --- | --- |
| `INVALID_REQUEST` | `400` |
| `UNAUTHORIZED`, `FORBIDDEN` | `403` |
| `NOT_FOUND`, `METHOD_NOT_FOUND` | `404` |
| `RATE_LIMITED` | `429` |
| `UNAVAILABLE` | `503` |
| `INTERNAL_ERROR` and anything else | `500` |

Only an allowlist is reachable: reads (`health`, `status`, `config.get`,
`sessions.list`/`get`/`preview`/`resolve`, `chat.history`, `models.list`,
`usage.*`, `agents.list`, `projects.list`/`get`, `skills.list`, `cron.list`,
`channels.status`, `doctor.status`, `acp.*` status, ...) and mutations that
create, rename or send without deleting anything (`chat.send`,
`sessions.send`, `sessions.create`, `sessions.rename`,
`projects.create`/`update`, `*.abort`, `plan.mode.set`). `chat.send` and
`sessions.send` start an agent turn with the same options as over the
WebSocket, including an elevated execution mode the request asks for, so the
token must be treated as full operator access either way. Configuration
writes, secrets and environment variables, trading and wallets, restarts,
deletes and approval decisions stay on the WebSocket. Any other method answers `403` with `error.code = "FORBIDDEN"` and
`details.code = "METHOD_NOT_ALLOWED_OVER_HTTP"`. The full list is
`HTTP_RPC_METHODS` in
[`src/agentos/gateway/http_rpc.py`](../src/agentos/gateway/http_rpc.py).

## Direct tool calls

`POST /api/v1/tools/invoke` runs one tool without an LLM turn (bearer auth,
Control surface):

```sh
curl -X POST -H "Authorization: Bearer $AGENTOS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"tool": "web_search", "args": {"query": "agentos release notes"}}' \
  http://127.0.0.1:18791/api/v1/tools/invoke
```

Body: `tool` (required), `args` (object), `action` (merged into `args` for
tools that take one), `session_key` (runs the tool in that session's context
and agent), `idempotency_key`, `dry_run`.

Errors have the typed shape `{"ok": false, "error", "code", "details"}`: `code`
is a category (`INVALID_REQUEST`, `FORBIDDEN`, `NOT_FOUND`, …) and
`details.code` the specific reason.

| Status | Meaning |
| --- | --- |
| `200` `{"ok": true, "result": …}` | The tool ran. A tool that ran and failed answers `200` with `ok: false` and the tool's error class. |
| `200` `{"dryRun": true, "allowed": …, "policy": {…}}` | `dry_run`: the resolved policy (profile, caller, sandboxed, read-only, may-require-approval); nothing ran. |
| `403` `FORBIDDEN` / `POLICY_DENIED` | The configured tool policy (`[tools]` profile/allow/deny, the agent's own entry) refuses it; `details.error_class` names the refusal. |
| `403` `FORBIDDEN` / `APPROVAL_REQUIRED` | The tool needs a human approval. HTTP calls run unattended, so the tool refuses as on any unattended run; `details.approval_id` is included when it queued one. |
| `400` `INVALID_REQUEST` | Bad body, an argument that writes a file (`output_path`, `save_path`, `output_file`, `destination`, …), refused for every tool, or arguments that do not match the tool's schema (`details.error_class = "ToolArgumentsRejected"`, the message names each failing argument). |
| `404` `NOT_FOUND` | Unknown tool **or** one the endpoint does not run — the two look the same. |
| `409` `INVALID_REQUEST` / `REQUEST_CONFLICT` | The `idempotency_key` was used for a different request in the last ten minutes (a repeat of the same request replays its first answer with `idempotentReplay: true`). |
| `504` `UNAVAILABLE` / `TOOL_TIMEOUT` | The tool did not finish within the tool timeout. |

The endpoint denies by default: it runs only tools whose registration
declares them read-only (`read_file`, `list_dir`, `grep_search`, `web_search`,
`web_fetch`, `memory_search`, `sessions_list`, …). Every other tool — anything
that may write, send, spend or change state, every MCP tool, and anything
registered later without the read-only flag — answers `404`. On top of that, a
name floor holds even if a flag changes: shell, terminal and code execution,
file writes, deletes and moves (including `memory`, memory and skill writes,
document creation, `publish_artifact`, `env_set`, `http_request`), project and
session renames, `sessions_spawn`, `sessions_send`, `subagents`, `acp`,
`message`, `cron` and scheduler tools, `gateway`, `browser`,
`router_control`, the paid media generators (`image_generate`, `tts`,
`music_generate`, `song_generate`, `voice_clone`, `voice_convert`,
`dubbing_*`), and any `trade*`/`trading*` tool. Lift a single tool with
`[tools.invoke] allow = ["create_pdf_report"]` — in `agentos.toml` only: config
RPC writes refuse to add an entry, and a connection the gateway recognizes as an
agent's cannot touch the setting at all. Tools run sandboxed in an unattended
context; an agent's connection (an agent token or an open exec window) is
refused with `403`.

## Inbound webhooks

External automation can reach the agent without a chat channel. These routes
take their own bearer, `[hooks] token`, sent as `Authorization: Bearer <token>`
or `X-AgentOS-Hook-Token`; the gateway token is refused there. Without a hooks
token they answer `404`.

| Method | Path | Body | Effect |
| --- | --- | --- | --- |
| `POST` | `/api/v1/hooks/agent` | `{message, agent?, session_key?}` | Enqueues `message` as a user message in `agent:<agent>:hook:agent`, or in `session_key` when it is under `agent:<agent>:hook:`. |
| `POST` | `/api/v1/hooks/wake` | `{reason?}` | Asks the heartbeat to run now (`503` when there is none). |
| `POST` | `/api/v1/hooks/<name>` | any JSON | Delivers to the agent and session of `[hooks.routes.<name>]`, message rendered from its `template`. |

Delivery answers `202` with `sessionKey`. Per hook name: `429` past
`rate_limit_per_minute` (default 60, with `Retry-After`); an `idempotency_key`
in the body or an `Idempotency-Key` header replays the first answer for five
minutes (`duplicate: true`), and the same key with a different body is `409`
with `details.code = "REQUEST_CONFLICT"`; and `409` with `details.code =
"FANOUT_LIMIT"` once the hook has delivered into `max_sessions_per_hook`
(default 1) distinct sessions within the last hour. A malformed or wrong token
is `401`, never an error page. Wrong tokens (the gateway token presented here
included) count against `[auth.rate_limit]` like wrong gateway tokens, in a
budget of their own: past it the address gets `429` with
`details.code = "AUTH_RATE_LIMITED"` on the hooks, while the rest of the API
is unaffected. Errors have the same typed shape as above. See
[Configuration → Inbound webhooks](configuration.md#inbound-webhooks) and
[Scheduling → Automation](scheduling.md#automation-from-outside).

## Streaming (WebSocket)

For streaming turns and live events, connect to the WebSocket route:

```text
ws://127.0.0.1:18791/ws
```

The WebSocket carries the same JSON-RPC methods the HTTP endpoints dispatch to,
plus server-pushed events. It is one route on the same app — the REST surface
above sits alongside it. See [`mcp-server.md`](mcp-server.md) for a bridge that
uses this transport.

### Handshake

1. The server sends `connect.challenge` with `{nonce, ts}` (`ts` in ms since
   the epoch).
2. The client answers with a `req` whose `method` is `connect` and whose
   `params` are `{minProtocol, maxProtocol, clientKind, client, auth?}`. The
   current protocol is `3`; send `minProtocol = maxProtocol = 3`. `client` is
   informational, `{id, version, platform, mode}`, every field optional.
   `auth.token` carries the gateway token.
   While the gateway is still booting, `connect` is answered with a retryable
   `UNAVAILABLE` (`details.code: "STARTUP_SIDECARS"`, `retry_after_ms`) and the
   socket closes `1013`; retry after `retry_after_ms`.
3. The server answers with a bare `hello-ok` frame (not a `res`) carrying
   `features.methods`, `features.events`, `features.capabilities` (additive
   feature names such as `typed-errors`, `challenge-ts`, `auth-rate-limit`,
   `rpc-http`, `supervisor-contract`; treat an unknown one as absent), `policy` and
   `auth.nonce` (the challenge nonce, so a client can check it answered its
   own challenge).

Messages before `hello-ok` are capped at 64 KiB: a larger one (plain,
fragmented or compressed) closes the socket with `1009` before it is read.
After it the cap is `policy.max_payload` (25 MiB). One address may hold at
most 128 sockets in the handshake; the next upgrade is refused with HTTP 403. Malformed
frames answer `INVALID_REQUEST` with `details.code` `INVALID_FRAME` or
`INVALID_CONNECT_PARAMS` and an `errors` list. A protocol range the server
cannot meet answers `INVALID_REQUEST` with `details {code:
"PROTOCOL_MISMATCH", client_min, client_max, server_min, server_max,
reload_required}`; `reload_required` is true when the client is the older
side (a stale web console bundle).

### Errors

`error.code` is one of `INVALID_REQUEST`, `UNAUTHORIZED`, `FORBIDDEN`,
`NOT_FOUND`, `METHOD_NOT_FOUND`, `UNAVAILABLE`, `RATE_LIMITED`,
`INTERNAL_ERROR`, a long-standing code kept for existing clients
(`QUEUE_FULL`, `GATEWAY_RESTARTING`, ...), or a namespaced domain code
(`project.conflict`, `trading.operator_required`). The specific reason of a
new error is in `details.code` (`PROTOCOL_MISMATCH`, `AUTH_RATE_LIMITED`,
`REQUEST_START_TIMEOUT`, `QUEUE_FULL`, ...). `retryable: true` says trying
again later can succeed.

### Concurrency and backpressure

Up to four requests per connection run at once. Requests naming the same
session (`sessionKey`, or `key` on `sessions.*`) run in order, and a request
naming no session waits for every earlier one, so a client can still rely on
`chat.send` before `sessions.abort` or `subscribe` before a history read.
Session-less reads (`sessions.list`, `status`, `config.get`, `models.list`,
`usage.*`, ...) wait only for earlier session-less writes, so a poll is not
held back by another session's slow history. A request that cannot start
within 30 seconds answers `UNAVAILABLE` with
`details.code = "REQUEST_START_TIMEOUT"` (retryable).

Event frames carry a per-connection `seq`. When more than
`policy.max_buffered_bytes` (50 MiB) is waiting for a client that is not
reading, periodic refreshes (`tick`, `presence`, `health`) are dropped and
their `seq` still consumed, so the client sees a gap and resynchronizes;
anything else closes the socket with `1008` `slow consumer`.

Projects (session groups with shared knowledge) have no REST route of their
own: `projects.create` / `projects.list` / `projects.get` / `projects.update`
are RPC methods (WebSocket, or `POST /api/v1/rpc`), `projects.delete` is
WebSocket-only (params use `projectId`, `agentId`, `name`, `knowledge`;
delete responds with `sessionsCleared` — sessions are detached, not deleted).
`projects.update` writes only the fields you pass and accepts an optional
`expectedUpdatedAt` (the `updatedAt` you last read): when the row changed in
between, the call fails with code `project.conflict` instead of silently
overwriting the other writer's edit.
`sessions.create` accepts an optional `projectId`, `sessions.patch` moves a
session with `projectId` (explicit `null` detaches), and `sessions.list`
accepts a `projectId` filter and emits `project_id`/`projectId` on every row.
`sessions.patch` and `sessions.rename` on an ephemeral WebChat key
(`agent:<id>:webchat:<suffix>`) that has no row yet create it first — the WebUI
mints that key client-side and only sends it on the first message; every other
key shape still fails with `NOT_FOUND`.
Project CRUD broadcasts a `projects.changed` event to connected clients.

### Run lifecycle

`hello-ok.features.capabilities` names the behaviours below; check for a name
before relying on it.

- **`terminal-replay`** — every run ends with exactly one
  `session.event.done` or `session.event.error`, whichever path ends it (a
  normal end, a cancel, a crash, `sessions.abort`). The frame carries `state`
  (`done`, `error` or `aborted`) and `task_id`; an aborted one also carries
  `partial_text`. It is published after the task's terminal status is stored,
  before `task.<status>`. `task.*` and `sessions.changed` frames are kept in
  the session's replay buffer with a `stream_seq`, so
  `sessions.messages.subscribe {key, since_stream_seq}` replays them (with
  `meta.replayed: true`) to a client that reconnects late. Coding-agent and
  subagent child sessions get `sessions.changed` with their `run_status` too.
- **`inflight-snapshot`** — `sessions.messages.subscribe` and `chat.history`
  (unless paging with `before`) return `in_flight_run`: `null` when nothing
  runs (drop any live view), or `{run_id, task_id, status, started_at,
  through_stream_seq, text, text_truncated?, plan?, events?}` for the running
  turn. It is capped at 256 KiB, filled in that order of priority; text is cut
  from the front. `through_stream_seq` is the stream position it covers.
- **`mid-turn-steering`** — a `chat.send` / `sessions.send` with an explicit
  `queueMode: "steer"` while the session's turn runs joins that turn instead
  of cancelling it (without `queueMode` the session's default still replaces
  the running turn, as before). The turn takes one
  waiting message per checkpoint: before starting the next call of an ordered
  tool sequence (calls not started get a "skipped" tool result), after a tool
  batch (the message follows the tool results), and when the model would end
  the turn (it continues instead). The user row is written when the turn
  takes the message; until then it is not in the transcript. A message no
  checkpoint takes runs as the next turn. When the running turn does not check
  (a coding-harness turn, a turn still starting) `steer` still replaces it.
  A `noMemoryCapture` message steers too; the turn it joins then skips
  memory capture. The send answer carries `task_id` and the effective
  `queue_mode` (`steer`, `interrupt`, `followup`, `collect`); an explicit
  `steer` that fell back to interrupt also carries `steer_fallback`:
  `attachments` (a message with attachments always starts its own turn) or
  `no_steerable_turn` (nothing running takes steering messages). `chat.history` (unless paging with
  `before`) returns `pending_inputs`: the session's queued messages, oldest
  first, as `{task_id, queue_mode, text, text_truncated?, queued_at,
  attachments, steer, message_id?}`; `message_id` is set when the user row is
  already in the transcript (a follow-up), so a client does not show it
  twice. `chat.abort {sessionKey, taskId}` (and `sessions.abort {key,
  taskId}`) withdraws a queued message — `{withdrawn: true, aborted: [],
  removed_message_id?}`, the task ends `cancelled` / `withdrawn` — stops just
  that run when it is the running one, and answers `delivered: true` when a
  running turn already took it. When a turn takes the message it emits
  `session.event.steering {text, message_id, task_id}` and adds a
  `{"type": "steer", "message_id"}` segment to its row's `tool_calls`.
  `chat.history` cuts such an assistant message at each `steer` segment and
  moves the steered user message between the parts (work before, message,
  work after); the last part keeps the row's ids, usage and artifacts, earlier
  parts get `<message_id>#partN` ids and `split_from`. While the turn runs,
  its part up to the newest steer is listed and `in_flight_run.text` holds
  only the text after it. A `delta_cursor` read whose turn would move a
  steered message the client already has answers `kind: "reset"`.
- **`idempotency-keys`** — `chat.send`, `sessions.send`, `sessions.create`,
  `cron.add`, `exec.approval.resolve` and `plugin.approval.resolve` accept an
  optional `idempotency_key`. The same key within five minutes returns the first
  answer; the same key with a different request fails with `INVALID_REQUEST`
  and `details.code: "REQUEST_CONFLICT"`. A `chat.send` / `sessions.send` key
  is also stored on the turn's task, so a retry after a gateway restart finds
  the turn instead of starting another.

`sessions.abort` stops the session's run and every child run it spawned
(coding-agent and subagent sessions, any depth), withdraws the approval
prompts they were waiting on, waits up to 10 s for them to end, and answers
`{aborted: [<run ids>], children_aborted: [<session keys>], key}`. If some are
still running after that, it fails with `UNAVAILABLE` and
`details: {code: "ABORT_INCOMPLETE", still_running: [...]}`. It waits in its
session's lane, so other sessions' requests on the same socket go ahead; the
runs it stops are the ones active when it started.

A session's `stream_seq` keeps counting for as long as the session exists (a
follow-up turn on a finished child continues it); it restarts only with the
gateway, which a subscribe answer shows as a `current_stream_seq` below the
client's cursor or `replay_gap_reason: "cursor_ahead_of_stream"`.

`chat.history` answers stay under 6 MiB. A message over 128 KiB arrives as a
placeholder (`placeholder: true`, `byte_size`, `entry_id`, the tool calls'
identity, a short text preview); `chat.entry {sessionKey, entryId}` returns the
full row (over 20 MiB it answers `INVALID_REQUEST` with `PAYLOAD_TOO_LARGE`). `delta_cursor` (a `newest_cursor` from an earlier answer) returns only
the rows after it (`kind: "delta"`, at most 200 rows / 1 MiB, `has_more` when
there are more), or `kind: "reset"` with the latest page when the cursor no
longer names a row. Assistant text, in history and in `text_delta` frames, is
display-ready: reply directives, generated-artifact markers and leaked
tool-protocol text are removed by the gateway.

### Environment variables (`env.*`)

Control-surface only. Values never appear in a listing.

| Method | Purpose |
| --- | --- |
| `env.list` | Every known variable: name, set/unset, source, description, owner, and a **masked** value. |
| `env.set` | Write one variable. Returns its new state without echoing the value. |
| `env.unset` | Remove one variable from `~/.agentos/.env`. |
| `env.reveal` | Return one real value. Rate limited to 5 per 30s and written to the audit log. |

Writes are refused for names that steer subprocess execution or AgentOS runtime
posture; see
[configuration.md](configuration.md#what-cannot-be-written-through-agentos).
`env.set` and `env.unset` report `restartRequired` per variable — provider
clients are built at boot with the key they had then, while other variables are
picked up by the next process AgentOS spawns.

## Example

On the default loopback bind these work as-is, no token needed:

```sh
# Liveness
curl http://127.0.0.1:18791/health

# System status
curl http://127.0.0.1:18791/api/system/status

# Send a chat turn (message is required; sessionKey is optional)
curl -X POST \
  -H "Content-Type: application/json" \
  -d '{"sessionKey": "agent:main:webchat:default", "message": "hello"}' \
  http://127.0.0.1:18791/api/chat
```

When the gateway runs with `auth.mode = "token"` (any public bind), add the
token to each `/api/*` call:

```sh
curl -H "Authorization: Bearer $AGENTOS_TOKEN" \
  https://gateway.example.com/api/system/status
```

## Source

The full route table is defined in `create_gateway_app`:
[`src/agentos/gateway/app.py`](../src/agentos/gateway/app.py) (the `routes`
list). File-and-media routes are registered just below it via
`register_upload_routes`, `register_attachment_routes`,
`register_artifact_routes`, `register_download_routes` and
`register_audio_transcription_routes`; then `register_inbound_hook_routes`
and `register_tools_invoke_route`.

Read next:

- [`gateway.md`](gateway.md)
- [`mcp-server.md`](mcp-server.md)
- [`web-ui.md`](web-ui.md)

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
