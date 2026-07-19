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
`auth.mode = "none"`, and a request from a loopback peer is treated as the local
owner — so `curl http://127.0.0.1:18791/api/...` works with no credentials.

A token is only enforced when `auth.mode = "token"`. That mode is required
before the gateway will bind to a public address (`0.0.0.0` / LAN): the startup
guard refuses to serve an unauthenticated public bind, and a token is
auto-generated when unset. See [`gateway.md`](gateway.md) for bind safety.

When auth is enabled, send the token any one of these ways (checked in order):

```text
Authorization: Bearer <token>
X-Agentos-Token: <token>
?token=<token>          # query parameter
```

`/health` and `/ready` never require a token. Cross-origin browser requests are
governed by CORS configuration regardless of auth mode.

## Liveness and Readiness

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health`, `/healthz` | Liveness probe — `{"ok": true, "status": "live"}`. |
| `GET` | `/ready`, `/readyz` | Readiness probe — `503` until the gateway is ready. |

These require no auth and are safe for load balancers and container probes.

## Core Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/config` | Effective gateway configuration. |
| `GET` | `/api/system/status` | Version, uptime, active provider, auth mode. |
| `GET` | `/api/sessions` | List sessions. |
| `POST` | `/api/chat` | Send a chat turn. Body: `message` (required), `sessionKey` (optional). |
| `GET` | `/api/chat/history?sessionKey=<key>` | Fetch a session transcript. |
| `GET` | `/api/agents` | List durable agents. |
| `GET` | `/api/cron` | List scheduled jobs. |
| `GET` | `/api/usage` | Token usage and cost breakdown. |

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
| `POST` | `/api/approvals/resolve` | Approve or deny a pending item. |
| `POST` | `/api/elevated-mode` | Set per-session elevated mode (owner only). |

## Files and Media

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/v1/files/upload` | Upload a file; returns an opaque id for `chat.send`. |
| `GET` | `/api/v1/attachments/{sha256}` | Fetch an attachment by content hash. |
| `GET` | `/api/v1/artifacts/{artifact_id}` | Fetch a generated artifact. |
| `POST` | `/api/audio/transcribe` | Transcribe audio to text. |

## Streaming (WebSocket)

For streaming turns and live events, connect to the WebSocket route:

```text
ws://127.0.0.1:18791/ws
```

The WebSocket carries the same JSON-RPC methods the HTTP endpoints dispatch to,
plus server-pushed events. It is one route on the same app — the REST surface
above sits alongside it. See [`mcp-server.md`](mcp-server.md) for a bridge that
uses this transport.

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
`register_artifact_routes`, and `register_audio_transcription_routes`.

Read next:

- [`gateway.md`](gateway.md)
- [`mcp-server.md`](mcp-server.md)
- [`web-ui.md`](web-ui.md)

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
