# Channels

Channels let AgentOS run from messaging platforms while sharing the same
agent runtime as the CLI and Web UI. Use channels when you want the same agent
to answer from Slack, Telegram, or Discord.

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
| `discord` | Discord | websocket | no |
| `email` | Email | polling | no |
| `slack` | Slack | mixed | depends on mode |
| `telegram` | Telegram | mixed | depends on mode |

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

Telegram direct messages always require pairing. There is no Telegram
admin/owner role and no open or allowlist mode. An unknown sender is stopped
before commands or agent execution, then receives a one-time 8-character code.
Any connected Control client can pair the sender from the Channels page or
with:

```sh
agentos channels pairing list personal
agentos channels pairing approve personal ABCD2345
```

Pairing survives gateway restarts. To review or disconnect a sender:

```sh
agentos channels pairing list personal --json
agentos channels pairing deny personal <telegram-user-id>
agentos channels pairing revoke personal <telegram-user-id>
agentos channels pairing clear-pending personal
```

Pairing codes expire after one hour. Requests are limited to one per account
every 10 minutes and three pending accounts per configured Telegram channel.
Five invalid approval attempts lock approval for one hour. Pairing state is
stored outside the main config under `$AGENTOS_STATE_DIR/pairing` (by default
`~/.agentos/pairing`) with `0600` filesystem permissions.

Telegram groups are disabled by default. Enabling groups requires all three
admission checks: the chat ID is explicitly configured, the sender is paired,
and the bot is mentioned when `group_mention_required` is true. Configure the
group posture during setup or with channel fields:

```sh
agentos channels add telegram --name personal --token <bot-token> \
  --field groups_enabled=true \
  --field group_chat_ids=-1001234567890,-1009876543210 \
  --field group_mention_required=true
```

### Voice Transcription

Telegram channels can automatically transcribe inbound voice messages, audio files, and video notes before routing them to the model. This requires global audio features to be enabled (with valid ElevenLabs Speech-to-Text credentials) and the following fields set:

```sh
agentos channels add telegram --name personal --token <bot-token> \
  --field transcribe_voice=true \
  --field max_voice_duration_s=120
```

Pairing is sender-level: the same paired sender may use direct messages and
explicitly configured groups. Removing the pairing disconnects that sender
from both surfaces immediately.

## Channel Command Authorization

Channel admission runs before native or text slash-command dispatch. An
unpaired Telegram sender cannot execute commands or start an agent turn. Once
paired, the sender gets the channel command surface, including session and
router operations such as `/new`, `/reset`, `/compact`, `/abort`, `/c0`
through `/c3`, and `/auto`. Direct messages and configured groups use the same
rules; there is no command privilege tier.

Tool visibility is not derived from a sender role. It is resolved from the
agent/global tool configuration, runtime capabilities, sandbox policy, and
approval gates. Channel RPC exposure is a separate protocol boundary: channel
clients receive only the explicitly registered channel-safe RPC methods, while
Control-only configuration and lifecycle RPCs remain unavailable.

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

## In-Flight Reply Feedback

Slack, Discord, Telegram and Microsoft Teams stream the reply itself: AgentOS
posts one message as soon as text is available and edits it as the answer
grows. Telegram edits at most once every 1.2 seconds — its rate limit is far
stricter than Slack's — and rolls over into a second message when an answer
passes the 4096-character ceiling. If Telegram starts answering `429`, the
edit loop stops and the remainder is delivered as one final message instead of
fighting the limiter; no text is lost either way. Forum topic replies stream
into the originating topic.

Streaming adapters that also have a typing indicator — Telegram and Discord —
show it while the model is still thinking, and drop it the moment the first
chunk reaches the chat. Nothing can be streamed until the first token exists,
and with tool calls in the loop that wait is often tens of seconds, so the
indicator covers exactly that gap and then hands the chat over to the
live-edited message.

Adapters without a streaming surface fall back to their native typing
indicator for the whole turn. Telegram refreshes `sendChatAction` every four
seconds in either mode, because Telegram clients expire the status after at
most five seconds. Reply feedback is best-effort and never interrupts the
underlying agent turn.

## Webhook Channels

Slack webhook mode requires a public, provider-reachable URL. Telegram may
require one depending on mode.

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
