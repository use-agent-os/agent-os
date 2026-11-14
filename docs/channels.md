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

## Sender Admission

Every channel that can start an agent turn admits only senders the operator
chose. A channel turn runs with the operator's tools (including the shell), so
"anyone who can message the bot" must never be the default.

| Channel | How senders are admitted |
| --- | --- |
| Telegram | Pairing, always (see [Telegram Account Pairing](#telegram-account-pairing)). |
| Email | `allowed_senders` From-address allowlist (required). |
| Discord, Slack | `allowed_user_ids` and/or `pairing_enabled`. With neither, nobody is admitted. |

For Discord and Slack, set at least one of:

- `allowed_user_ids`: comma-separated platform user ids that are always
  admitted. Discord ids are numeric snowflakes (Developer Mode › right-click
  the user › Copy User ID); Slack ids are member ids such as `U012ABCDEF`
  (profile › ⋯ › Copy member ID).
- `pairing_enabled = true`: an unknown user who DMs the bot gets a one-time
  code, exactly like Telegram, and is admitted once you approve it with
  `agentos channels pairing approve <name> <code>`. Codes are only issued in
  DMs; group messages from unpaired users are ignored.

```sh
agentos channels edit team --field allowed_user_ids=U012ABCDEF,U034GHIJKL
agentos channels edit my-discord --field pairing_enabled=true
agentos gateway restart
```

```toml
[[channels.channels]]
type = "discord"
name = "my-discord"
token = "..."
allowed_user_ids = ["123456789012345678"]
pairing_enabled = false
```

The same admission decision gates every inbound path: DMs, group messages that
@mention the bot (the mention is still required), slash commands, and
interactive approval buttons (Discord message components, Slack block
actions). A refused sender gets no reply unless pairing just issued them a
code; a refused Discord slash command is answered with "not authorized" so the
deferred interaction does not hang.

A Discord or Slack channel with neither setting **fails closed**: it still
connects and can deliver outbound messages (cron results, notifications), but
refuses every inbound sender. The gateway logs `channel.admission_closed` at
start, each refused message is logged as `channel.sender_not_admitted` with the
sender id (copy it into `allowed_user_ids`), and `agentos doctor` reports
`channel.<name>.admission_closed` with the fix commands.

The MS Teams adapter (not yet exposed in the channel catalog) follows the same
rules: `allowed_user_ids` holds Teams user ids (`29:…`, the activity
`from.id`).

Admitting a sender is your decision, never an agent's: a connection the
gateway recognizes as an agent's (an agent token, a process it spawned, or one
opened while an agent shell runs) is refused `channels.pairing.approve`, and
any config write (`config.set` / `patch` / `apply`, `onboarding.channel.upsert`)
that adds an id to a channel's `allowed_user_ids`. It may still deny, revoke,
or remove ids.

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
restart does not affect replying to live conversations. A reply into a thread
the table no longer knows -- a scheduled job or artifact delivery that fires
after a restart, before the correspondent has written again -- is refused with
`email.send has no recipient for unknown thread` rather than guessed: the thread
key is the inbound `Message-ID`, which looks like a mailbox but whose domain the
original sender chose. Cron and heartbeat deliveries that are configured with an
address (cron `channel_id: "alerts@example.com"`, heartbeat `to:`) do not depend
on the table.

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

Discord and Slack channels with `pairing_enabled = true` use this same flow
and the same `agentos channels pairing` commands; everything below applies to
them except the Telegram group settings.

Telegram direct messages always require pairing. There is no Telegram
admin/owner role and no open or allowlist mode. An unknown sender is stopped
before commands or agent execution, then receives a one-time 8-character code.
Any connected Control client can pair the sender from Settings › Channels or
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
unpaired Telegram sender, or a Discord/Slack sender who is neither in
`allowed_user_ids` nor paired, cannot execute commands or start an agent turn. Once
paired, the sender gets the channel command surface, including session and
router operations such as `/new`, `/reset`, `/compact`, `/abort`, `/c0`
through `/c3`, and `/auto`. Direct messages and configured groups use the same
rules; there is no command privilege tier.

`/restart [force]` is the one exception that is gated by configuration rather
than by pairing: it asks the gateway to restart itself in place and is refused
on every channel until the operator sets `[gateway_control] chat_restart =
true` (see [Configuration](configuration.md#gateway-control)). With the toggle
on, any paired sender may use it; without it, the sender gets a reply naming
the setting. The native command menus (Telegram, Discord, Slack) always list
`/restart`, since they are registered once at channel start. A restart
acknowledges first, drains the active turns (`force` cancels them), and posts
"Gateway restarted" back into the same chat or thread once the new process is
up. Messages that arrive while the gateway is draining get "The gateway is
restarting; please send that again in a moment." and are not queued.

Tool visibility is not derived from a sender role. It is resolved from the
agent/global tool configuration, runtime capabilities, sandbox policy, and
approval gates. Channel RPC exposure is a separate protocol boundary: channel
clients receive only the explicitly registered channel-safe RPC methods, while
Control-only configuration RPCs remain unavailable. `gateway.restart` is the one
lifecycle RPC exposed to channels, behind the toggle above.

## Slack Modes

Slack Socket Mode uses an outbound websocket and does not require a public
Request URL. It requires the bot token (`xoxb-...`) plus an app-level token
(`xapp-...`) saved as `app_token`.

Slack webhook mode uses the Events API Request URL. It requires the bot token
plus `signing_secret`, and the gateway must be reachable by Slack. The secret
is mandatory, not advisory: without it the endpoint answers Slack's
`url_verification` handshake and rejects everything else with `401`, because an
unsigned POST cannot be attributed to Slack.

Leave `webhook_path` empty or omit it to select the automatic path. The first
enabled Slack webhook account in your config (by configuration order, not
name) keeps `/slack/events` regardless of how many other webhook accounts are
enabled — adding a second account never changes an already-configured
account's Request URL. Every other enabled Slack webhook account with no
explicit `webhook_path` gets `/slack/events/<account_name>`, for example
`/slack/events/team-b`. Disabled entries and Socket Mode accounts do not
affect this choice or count toward "first".

A non-empty `webhook_path` always overrides the automatic path. You can set it
with `agentos channels add slack --name team-a --field webhook_path=/slack/team-a/events`
alongside the token and signing-secret fields above. Automatic account names
(for every account but the first) must use letters, digits, `.`, `_`, `~`, or
`-` and cannot be `.` or `..`; otherwise set an explicit path. Configure each
Slack app's Events API and Interactivity Request URLs to use its matching
public URL; use the same URL for slash commands (`command_request_url` or the
exported manifest).

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

Slack, Discord and Microsoft Teams stream the reply itself: AgentOS posts one
message as soon as text is available and edits it as the answer grows.

Telegram does not stream by default. The chat shows Telegram's typing status
for the whole turn, and the finished reply arrives in one go (split into
several messages only past the 4096-character ceiling). Set `stream_replies =
true` on a Telegram channel to live-edit instead:

```sh
agentos channels add telegram --name personal --token <bot-token> \
  --field stream_replies=true
```

A streaming Telegram channel edits at most once every 1.2 seconds — its rate
limit is far stricter than Slack's — and rolls over into a second message when
an answer passes the 4096-character ceiling. If Telegram starts answering
`429`, the edit loop stops and the remainder is delivered as one final message
instead of fighting the limiter; no text is lost either way. Forum topic
replies go to the originating topic in both modes.

Streaming adapters that also have a typing indicator — Discord, and Telegram
with `stream_replies` — show it while the model is still thinking, and drop it
the moment the first chunk reaches the chat. Nothing can be streamed until the
first token exists, and with tool calls in the loop that wait is often tens of
seconds, so the indicator covers exactly that gap and then hands the chat over
to the live-edited message.

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

5. For Discord and Slack, confirm the sender is admitted: look for
   `channel.sender_not_admitted` in the gateway log and run `agentos doctor`.
   See [Sender Admission](#sender-admission).

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
