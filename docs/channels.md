# Channels

Channels let AgentOS run from messaging platforms while sharing the same
agent runtime as the CLI and Web UI. Use channels when you want the same agent
to answer from Slack, Telegram, DingTalk, QQ, WeCom, Discord, Matrix,
or another supported adapter.

## Supported Channel Types

Inspect your local install:

```sh
agentos channels types
agentos channels types --json
agentos channels describe slack
```

This build exposes the following channel families:

| Type | Label | Transport | Public URL needed |
| --- | --- | --- | :---: |
| `dingtalk` | DingTalk | websocket | no |
| `discord` | Discord | websocket | no |
| `matrix` | Matrix | websocket | no |
| `qq` | QQ Bot | websocket | no |
| `slack` | Slack | mixed | depends on mode |
| `telegram` | Telegram | mixed | depends on mode |
| `wecom` | WeCom | webhook | yes |

The local `channels describe <type>` output is the source of truth for required
fields, secrets, extras, and restart behavior.

## Setup Flow

Interactive setup:

```sh
agentos configure channels
```

Add a channel explicitly:

```sh
agentos channels add telegram --name personal
```

Add provider-specific fields as needed. Slack supports two modes:

```sh
# Slack Socket Mode: outbound websocket, no public URL.
agentos channels add slack --name team \
  --field connection_mode=socket \
  --field app_token=xapp-... \
  --token xoxb-...

# Slack Events API webhook: requires a public Request URL and signing secret.
agentos channels add slack --name team-webhook \
  --field connection_mode=webhook \
  --field signing_secret=... \
  --token xoxb-...
```

Restart the gateway process after config edits:

```sh
agentos gateway restart
```

Verify runtime connection:

```sh
agentos channels status
agentos channels status personal --json
```

Saving a channel proves the config was written. `channels status` proves whether
the running gateway loaded and connected it.

## Manage Channels

```sh
agentos channels list
agentos channels enable <name>
agentos channels disable <name>
agentos channels edit <name>
agentos channels restart <name>
agentos channels logout <name>
agentos channels remove <name>
```

Use `gateway restart` after config changes. Use `channels restart <name>` only
for an already-loaded live adapter.

## Telegram Account Pairing

Telegram direct messages use `pairing` mode by default. An unknown account is
stopped before commands or agent execution, then receives a one-time
8-character code. Give that code to an operator, who can approve it from the
Channels page or with:

```sh
agentos channels pairing list personal
agentos channels pairing approve personal ABCD2345
```

Approvals survive gateway restarts. To review or remove access:

```sh
agentos channels pairing list personal --json
agentos channels pairing revoke personal <telegram-user-id>
agentos channels pairing clear-pending personal
```

Pairing codes expire after one hour. Requests are limited to one per account
every 10 minutes and three pending accounts per configured Telegram channel.
Five invalid approval attempts lock approval for one hour. Pairing state is
stored outside the main config under `$AGENTOS_STATE_DIR/pairing` (by default
`~/.agentos/pairing`) with owner-only permissions.

DM access and group access are separate. Pairing a user does not grant that
user access in Telegram groups. Configure them independently during setup or
with channel fields:

```sh
agentos channels add telegram --name personal --token <bot-token> \
  --field access_mode=pairing \
  --field group_access_mode=allowlist \
  --field group_allowed_sender_ids=123456789,987654321
```

DM modes are `pairing`, `allowlist`, `open`, and `disabled`; group modes are
`allowlist`, `open`, and `disabled`. Group messages still require a bot mention
where the adapter's mention policy applies. Use `open` only when unrestricted
access is intentional.

## Slack Modes

Slack Socket Mode uses an outbound websocket and does not require a public
Request URL. It requires the bot token (`xoxb-...`) plus an app-level token
(`xapp-...`) saved as `app_token`.

Slack webhook mode uses the Events API Request URL. It requires the bot token
plus `signing_secret`, and the gateway must be reachable by Slack.

Leave `slack_channel_id` empty when the adapter should reply to the incoming
conversation. Set it only when you want a default fallback channel. Enable
`reply_in_thread` when replies should stay in Slack threads.

## Native Slash-Command Menus

Telegram and Discord synchronize their native command menus when the adapter
starts. The entries are derived from AgentOS's unified channel command registry,
so they stay aligned with text `/command` dispatch.

Slack requires slash commands to be declared in the Slack app manifest. To
synchronize them automatically at adapter startup, configure `app_id`,
`manifest_token`, and `command_request_url` on the Slack channel entry. The
manifest token must be a short-lived Slack app configuration access token
(`xoxe.xoxp-...`), not the Socket Mode app-level token (`xapp-...`). AgentOS
exports the existing manifest first and replaces only `features.slash_commands`
so unrelated app configuration is preserved.

When those optional credentials are absent, print the manifest fragment with
the public Request URL for the Slack webhook route and import it in the Slack
app settings:

```sh
agentos channels native-commands slack \
  --request-url https://agent.example/slack/events
```

The command endpoint acknowledges Slack's form submission and routes the
resulting `/command` through the same channel dispatcher. Keep text command
interception as the fallback for platforms without native command menus.

## Webhook Channels

Slack webhook mode and WeCom require a public, provider-reachable URL.
Telegram may require one depending on mode.

For public channels:

- bind the gateway to a reachable interface;
- place it behind a trusted reverse proxy or tunnel;
- configure auth;
- check provider callback URLs and secrets carefully.

Example bind for a controlled network:

```sh
agentos gateway run --listen 0.0.0.0 --port 18791
```

Do not expose an unauthenticated gateway to the public internet.

## Attachments and Artifacts

Channel adapters can differ in attachment and artifact delivery behavior.
AgentOS normalizes agent execution through the same runtime path, but the
platform transport still controls file size limits, message threading, and
download/upload capabilities.

When a channel cannot deliver a large artifact directly, use the Web UI artifact
card or session export as the recovery path.

## Troubleshooting

If a channel does not respond:

1. Check config entries:

   ```sh
   agentos channels list
   ```

2. Check runtime status:

   ```sh
   agentos channels status <name> --json
   ```

3. Restart the gateway process after config changes:

   ```sh
   agentos gateway restart
   ```

4. For webhook channels, confirm the public URL, provider callback secret, and
   gateway auth/network boundary.

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
