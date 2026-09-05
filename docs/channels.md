# Channels

Channels let AgentOS run from messaging platforms while sharing the same
agent runtime as the CLI and Web UI. Use channels when you want the same agent
to answer from Slack, Telegram, Discord, or plain email.

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
| `email` | Email (IMAP/SMTP) | polling | no |
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

## Email (IMAP/SMTP)

The email channel needs no platform app registration — only IMAP and SMTP
credentials for a mailbox the agent owns. Inbound is IMAP polling; outbound is
SMTP, with `In-Reply-To`/`References` set so replies land in the originating
thread.

```sh
agentos channels add email --name inbox \
  --field imap_host=imap.example.com \
  --field imap_username=agent@example.com \
  --field imap_password=<app-password> \
  --field smtp_host=smtp.example.com \
  --field from_address=agent@example.com \
  --field allowed_senders=you@example.com,*@yourteam.example
```

Providers with two-factor authentication normally require an app password
rather than the account password. Set `smtp_ssl=true` (and `smtp_port=465`)
for implicit TLS; the default is STARTTLS on port 587.

`imap_folder` picks the mailbox to poll and defaults to `INBOX`. Names with
spaces (`Sent Items`, `Archive 2026`) are quoted per RFC 3501 before they go on
the wire, so they need no quoting of your own; a name carrying a control
character is rejected at channel start rather than at poll time.

### Access Control

`allowed_senders` is a fail-closed From-address allowlist and is **required** —
a channel with an empty list is rejected at config load, because an open inbox
would let anyone who can send mail drive the agent. Entries are exact addresses
(`you@example.com`) or domain patterns (`*@example.com`, equivalently
`@example.com`). Mail from any other sender is logged and dropped without ever
being queued.

The allowlist also governs where a reply goes. `Reply-To` is set by the same
sender the allowlist is meant to constrain, so it is honoured only when the
address it names is itself on the list; otherwise the reply goes back to the
`From` address and the off-list header is logged and ignored. The message is
still processed — an off-list `Reply-To` redirects the answer, it does not make
the mail untrusted.

The adapter also refuses to answer itself and drops machine-generated mail —
anything carrying `Auto-Submitted`, `X-Autoreply`, `List-Id`,
`List-Unsubscribe`, or `Precedence: bulk`. Without that guard an autoresponder
on the far end and the agent would reply to each other indefinitely.

### Threads and Sessions

One mail thread is one session. The thread identity is the first id in
`References`, falling back to `In-Reply-To` and then the message's own
`Message-ID`, so a long back-and-forth keeps its context while a fresh email
from the same person starts a fresh session.

The thread routing table (which address and subject a reply goes back to) is
rebuilt from each inbound message and kept in memory only, so a gateway
restart does not affect replying to live conversations.

Quoted history below a reply is stripped before the text reaches the model, and
HTML-only mail is flattened to text. Replies are sent as plain text: mail
clients do not render Markdown, so the agent is told to answer without it.

### Attachments

Inbound attachments are decoded and passed through the shared attachment
pipeline under the same per-type size limits as every other channel; anything
over the limit is dropped with a warning rather than truncated. Generated
artifacts are mailed back into the same thread as attachments. Whole messages
larger than `max_message_bytes` (25 MB by default) are skipped without being
downloaded.

### Polling

`poll_interval_s` (default 30) sets how often the mailbox is checked, and
`max_messages_per_poll` (default 10) bounds one cycle. Handled mail is flagged
`\Seen`; set `mark_seen=false` to leave the mailbox untouched, at the cost of
re-reading the same messages on the next poll. There is no IMAP IDLE support —
a failed poll is logged, surfaces in `agentos channels status`, and the loop
retries on the next interval.

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
plus `signing_secret`, and the gateway must be reachable by Slack. The secret
is mandatory, not advisory: without it the endpoint answers Slack's
`url_verification` handshake and rejects everything else with `401`, because an
unsigned POST cannot be attributed to Slack.

Leave `webhook_path` empty or omit it to select the automatic path. One enabled
Slack webhook account keeps `/slack/events`. With multiple enabled Slack webhook
accounts, each automatic path is `/slack/events/<account_name>`, for example
`/slack/events/team-a` and `/slack/events/team-b`. Names, not configuration order,
determine these paths, so they stay stable across restarts and config reordering.
Disabled entries and Socket Mode accounts do not affect this choice.

A non-empty `webhook_path` always overrides the automatic path. You can set it
with `agentos channels add slack --name team-a --field webhook_path=/slack/team-a/events`
alongside the token and signing-secret fields above. When adding a second webhook
account, set `webhook_path=/slack/events` explicitly on the existing account if
you want to keep its Request URL. Automatic account names must use letters,
digits, `.`, `_`, `~`, or `-` and cannot be `.` or `..`; otherwise set an explicit
path. Configure each Slack app's Events API and Interactivity Request URLs to
use its matching public URL; use the same URL for slash commands
(`command_request_url` or the exported manifest).

Restart the gateway after changing paths. Duplicate channel webhook paths with
overlapping HTTP methods cause a startup error naming the conflicting entries,
instead of silently routing every request to the first account. Socket Mode
does not register a webhook route and is unaffected.

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
require one depending on mode. Discord and email need none.

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
