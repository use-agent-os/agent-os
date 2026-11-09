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

Gateway boot and `agentos doctor` also warn when a checkout's bundle is older
than its frontend sources (`gateway.control_ui.dist_stale`). The warning is
advisory: it never blocks serving the existing bundle and never degrades
overall health status, because source mtimes are a hint rather than an oracle —
`git checkout` and `git pull` rewrite them, so a freshly built bundle can be
flagged. Rebuild with `python scripts/build_control_ui.py build`; `agentos
doctor` clears on the next run, and a gateway restart clears the boot-time log
line. Wheel installs ship no frontend sources and are never flagged, and the
check is skipped entirely when the Control UI is disabled.

For hot reload during frontend work, run the gateway and Vite together:

```sh
agentos gateway run
npm --prefix frontend run dev
```

Vite serves `/control/` and proxies the gateway API, the WebSocket, the root
approvals REST routes and the `/ready` probe the truth badge reads (point it at
another gateway with `AGENTOS_GATEWAY=http://127.0.0.1:<port>`). Production
builds use relative assets plus a server-provided runtime base, so custom
non-root mounts such as `/console/` work without rebuilding. Root, `/api`, and
`/ws` mounts are rejected because they overlap the gateway's public endpoints.

### Conventions for console controls

- **Disabled with a reason, never hidden or silent.** A control that cannot
  be used right now stays where it is, disabled, with the exact reason next
  to it ("The gateway is offline. Reconnect to use this."). The checks are
  headless and shared with the desktop app (`frontend/src/lib/disabled-reason.ts`:
  `gate(requireConnection(state), requireScheduler(down), …)` returns
  `{disabled, code, reason}`); the console renders the reason with its
  `DisabledReason` wrapper. A transient busy state is not a reason; a
  spinner already says that.
- **Every RPC method the console calls is listed** in
  `frontend/src/lib/required-methods.ts` (the desktop's extra ones in
  `desktop/src/renderer/src/lib/required-methods.ts`). A vitest fails when a
  literal `rpc.call('…')` is missing from the list, and
  `tests/test_console_required_methods.py` fails when a listed method is not
  in the gateway's advertised `features.methods`. Names built at runtime are
  listed by hand.

## Layout and routes

The console is chat-first. On screens wider than 768 px it has three columns:

- **Rail** (left edge): the agent switcher, **New session**, the **Sessions**
  and **Pages** view buttons and **Search**; at the bottom **Home**, **Inbox**
  (with a badge counting approvals, agent questions and runtime issues) and the
  status menu (connection state, theme, keyboard shortcuts, Settings).
- **List pane**: the Sessions list or the Pages catalog. Activate the selected
  view's button again, or press Ctrl+B (⌘B on macOS), to collapse it. Drag its
  edge (or focus the edge and use the arrow keys) to resize it; the width is
  remembered by the browser. A fresh load always starts with the pane open.
- **Main area**: the current route.

The Sessions list shows every session from `sessions.list`, grouped **By date**
(Pinned, Today, Yesterday, Previous 7 days, Older) or **By project** (Pinned,
one section per project, No project). Each row is a link, so a middle-click or
Ctrl/⌘-click opens the session in a new tab. A row shows the session's title
(its name, else its derived title, else a label from its key), its channel and
when it was last updated, plus a running indicator while a task is queued or
running. Child sessions (subagents) are folded under their parent, which shows
how many it has. The filter box matches titles and keys; the filter menu narrows
by agent, channel or project; **Clear filters** resets them all. A row's ⋯ menu
(also right-click, or Shift+F10 on a focused row) holds Open in new tab, Rename,
Pin/Unpin (and Move up/down for pinned rows, which can also be dragged), Move to
project, Reset, Export as Markdown, Copy session key and Delete (confirmed
first). The list refreshes on `sessions.changed` and every 5 seconds while the
tab is visible. Pins and the grouping choice are stored in the browser.

Picking an agent in the rail's switcher makes it the default for New session
and filters the Sessions list to it until the filter is cleared.

At 768 px and below the rail and list pane become a drawer, opened from the
header's menu button or a rightward swipe from the left edge. The header keeps
a New session button.

A pending approval from a session other than the one on screen appears in the
approval tray at the bottom right of every page. It can collapse to a count but
stays until every request is resolved.

### Routes

All routes live under the console base path (`/control` by default) and work as
deep links, through reload and browser history.

| Route | Shows |
| --- | --- |
| `/` | Redirects to `/chat` on every screen size. |
| `/chat` | The last active session, or the default webchat session. |
| `/chat/<agent>/<rest of key>` | One session. |
| `/new` | New session. |
| `/home` | Home: readiness, usage and live gateway activity. |
| `/sessions` | Sessions: search, filter and manage every session. |
| `/projects` | Projects. |
| `/projects/<project>` | One project. |
| `/usage` | Usage. |
| `/automations` | Automations (scheduled jobs). |
| `/inbox` | Inbox. |
| `/settings` | Settings. |
| `/settings/<section>` | One Settings section. |
| `/mcp/oauth/callback` | Finishes MCP OAuth, then shows Settings › MCP. |

A session key `agent:<agent>:<part>:<part>…` maps to
`/chat/<agent>/<part>/<part>…`, with each part percent-encoded. Any other key,
or one whose agent part is not a normalized agent id, maps to
`/chat/~/<whole key, encoded>`.

An unknown path shows a not-found page that names the path as plain text and
links to Home and Chat.

### Legacy URLs

Links from the previous console redirect (replacing the history entry, so Back
skips them) and keep the query parameters that still apply:

| Old URL | New URL |
| --- | --- |
| `/overview` | `/home` |
| `/cron` | `/automations` |
| `/projects?project=<id>` | `/projects/<id>` |
| `/chat?session=<key>` | `/chat/<agent>/<rest of key>` |
| `/chat?agent=<agent>` | `/chat/<agent>/webchat/default` |
| `/health` | `/settings/health` |
| `/channels` | `/settings/channels` |
| `/channels?view=setup` | `/settings/channels?setup=1` |
| `/mcp` | `/settings/mcp` |
| `/skills` | `/settings/skills` |
| `/memory` | `/settings/memory` |
| `/agents` | `/settings/agents` |
| `/env` | `/settings/environment` |
| `/logs` | `/settings/logs` |
| `/approvals` | `/settings/approvals` |
| `/config` | `/settings/advanced` |
| `/setup` | `/settings/setup` |
| `/settings?step=channels` | `/settings/channels?setup=1` |
| `/settings?step=<step>` | `/settings/setup?step=<step>` |

## Main Areas

| Area | Use it for |
| --- | --- |
| Chat | Run and resume chat sessions, inspect tool activity, publish artifacts, and use manual compact controls. |
| Inbox | Everything waiting on you in one list: approvals, agent questions and runtime issues, answered in place. |
| Projects | Group chat sessions, edit each project's shared knowledge (injected into every member session), and start new chats inside a project. |
| Overview / Health | See readiness, provider state, memory state, sandbox posture, and recovery hints. |
| Channels | Inspect configured channel adapter status and jump to Agent Setup for configuration changes. |
| Skills | Browse installed skills grouped by where they came from, see whether the agent is actually being offered each one, and install more from a hub. |
| Sessions | Inspect durable conversations and operational state. |
| Agents | Manage durable agent entries. |
| Usage | Inspect token and estimated-cost rollups. |
| Cron | View and manage scheduled runs. |
| MCP Servers | Add local or remote MCP servers, connect tools live, and complete OAuth authorization. |
| Agent Setup | Configure the agent through Guided capability setup or the complete Advanced Form/YAML editor. |
| Environment | Set, replace, and remove the environment variables skills and providers read. |
| Logs | Inspect runtime logs and diagnostics. |
| Approvals | Respond to sensitive tool-call approval requests. |

## Gateway connection

### Connect screen

When the gateway requires a token (`auth.mode = "token"`) and this browser tab
has none, or the gateway refuses the tab's token, the console shows a
full-page **Connect** screen instead of the workspace. Nothing behind it is
rendered or requested. It takes the gateway token and, when the console should
reach a different gateway, its WebSocket URL; it shows the gateway's address
and where the token comes from (`auth.token`, or `AGENTOS_GATEWAY_TOKEN`).

- A rejected token keeps the screen up with "The gateway rejected this token."
  and keeps the URL you entered.
- Too many failed sign-ins (`AUTH_RATE_LIMITED`) disable the form and say that
  sign-in is rate limited. The console does not retry by itself.

### Credentials in a link

A link may carry the token once, as `?token=…` or `#token=…`, for example
`http://127.0.0.1:18791/control/chat#token=abc`. Before anything else runs,
the console stores it for this tab only (`sessionStorage`, key
`agentos.wsToken`) and replaces the URL, so the address bar and Back never show
it. A new tab starts without a token. A `gatewayUrl` parameter is never applied
silently: a dialog names the target host first, and the URL is only used after
you confirm, and only in a top-level window (never inside a frame).

### Short drops and restarts

A connection loss shorter than 2 seconds changes nothing on screen: the status
stays as it was and no control is disabled. After that, the status changes and
disabled controls say why. A message sent while the socket is reconnecting is
delivered once when it is back, or shown as not sent with **Retry**; it is
never lost silently.

When the gateway announces an in-place restart (`shutdown` with
`restart_expected_ms`, for example after **Restart gateway** in Settings), the
console shows **Restarting gateway…** instead of an error, reconnects on the
gateway's timing, and refreshes the open view once the hello succeeds.

If an open tab's console is older than an upgraded gateway
(`PROTOCOL_MISMATCH`), the console says a newer console is available and offers
**Reload**. It may reload by itself, but at most once per console version
within 60 seconds, so it can never loop.

## Runtime status (truth badge)

The status button at the bottom of the rail answers "is what I am seeing true
and fresh". It shows one of six states and how fresh the view is:

| State | Meaning |
| --- | --- |
| Ready | Connected, frames are arriving, and the doctor reports no error-level finding. |
| Degraded | Connected, but a doctor finding is at error level, `/ready` reports `safe_mode` or `draining`, or no frame has arrived for 2.5 tick intervals (the view is a snapshot). |
| Connecting | The WebSocket is opening, `/ready` reports `starting`, or the gateway announced an in-place restart (`shutdown` with `restart_expected_ms`) and is on its way back. |
| Blocked | The gateway refused the handshake, read from its typed error: bad credentials (`UNAUTHORIZED`), too many failed sign-ins (`AUTH_RATE_LIMITED`), or a different protocol version (`PROTOCOL_MISMATCH`; when the console is the older side, reloading the page fixes it). The console does not retry these by itself. |
| Offline | No connection. What is on screen is the last snapshot. |
| Unknown | Connected, but the doctor report has not arrived or could not be loaded, so "Ready" cannot be claimed. |

Next to the state is the source and its age: `Live · 12 s` (age of the last
frame the gateway sent), `Snapshot · 3 min` (the connection dropped; data is
from before), or `Unavailable` (never connected). Open the status menu for the
reason and exactly one recovery action: **Retry connection** (reconnects at
once, skipping the backoff, and opens a new socket when the current one still
looks open but has gone silent), **Open attention inbox**, or **Open
Health**. A connection with no frame for 2.5 tick intervals is dropped and
reopened by itself.

The badge refreshes from the connection itself: the hello, every event frame,
the gateway's `tick` (every 30 s), and a shallow `doctor.status` plus
`GET /ready` (its `state`: starting, ready, draining, safe_mode, and the
`failing` checks) read on connect and every fourth tick (a `health` event or
a `state_version` revision triggers an extra read, once the gateway sends
them). It runs no polling timer of its own; a one-second display clock only
ages the "Live · 12 s" readout. Screen readers hear the state when it
changes, not the ageing freshness.

## Attention inbox

**Inbox** (`g i`) lists everything that waits on you, with a count on its
nav item:

- **Approvals** of every namespace (the same queue as the Approvals page).
  The same command waiting in several sessions is one item, "Seen in N
  sessions", and each occurrence keeps its own **Approve once** / **Deny**
  and a link into its session. "Always allow" and "Bypass" stay on the
  Approvals page. The global approval prompt does not pop over the inbox.
- **Agent questions** (`ask_user`): the option chips and a free-text reply,
  sent as your next message in that session (to every session, when the
  same question was asked in several). The gateway has no listing of
  unanswered questions, so the inbox collects them from the sessions this
  console has open; a question asked in a session you have not opened shows
  in its chat when you open it.
- **Runtime issues**, typed from `doctor.status` findings and the connection
  state: `gateway_unreachable`, `auth_failed`, `protocol_mismatch`,
  `model_auth_required`, `rate_limited`, `channel_terminal`, `safe_mode`,
  `update_failed` and `doctor_warning`. Each offers the actions that fit it:
  the doctor's fix steps in place, a jump to the right settings screen,
  **Retry connection**, and **Dismiss** (for this browser session only).
  While the console is offline only the connection issue is listed; the
  last doctor report is not repeated as if it were current.

Approvals come from the same store as the Approvals page, which the gateway
keeps current with `approval.requested` / `approval.resolved` events (one
backfill per connect), and the list updates when that store changes; the
inbox adds no timer and no polling of its own (**Re-check** asks for one
backfill), and items show when they started waiting ("since 11:32"). An empty inbox says "Nothing
needs you" only while the console is connected.

## Keyboard Shortcuts

The Web UI features a comprehensive set of keyboard shortcuts to facilitate mouse-free navigation and control. Press `?` (Shift + `/` on US keyboards) from any page to open the keyboard shortcuts cheat sheet.

### Global & Chat Shortcuts

- `?` - Show/hide the keyboard shortcuts cheat sheet.
- `Cmd+Shift+O` (macOS) / `Ctrl+Shift+O` (Windows/Linux) - Start a new chat session.
- `Escape` - Abort the active streaming turn, else recover the queue into the composer.

### Sidebar Navigation Chords

The Web UI supports two-step chords (Gmail/GitHub style) to switch between sidebar views. Press `g` followed by a destination letter/key within **1.5 seconds** to navigate:

- `g` then `c` - Go to Chat
- `g` then `i` - Go to the attention inbox
- `g` then `o` - Go to Overview
- `g` then `h` - Go to Health
- `g` then `n` - Go to Channels
- `g` then `m` - Go to MCP Servers
- `g` then `k` - Go to Skills
- `g` then `p` - Go to Projects
- `g` then `s` - Go to Sessions
- `g` then `a` - Go to Agents
- `g` then `u` - Go to Usage
- `g` then `r` - Go to Cron
- `g` then `,` - Go to Agent Setup
- `g` then `e` - Go to Environment
- `g` then `l` - Go to Logs
- `g` then `y` - Go to Memory
- `g` then `v` - Go to Approvals

> [!NOTE]
> Keyboard shortcuts are automatically suppressed while focus is inside a text input field, textarea, or content-editable region, and while modal dialogs or overlays are open.

## Agent Setup

Open **Settings > Agent Setup**, or go directly to:

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
same Agent Setup workspace, not separate sidebar destinations. Guided and
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
`writeBlocked: true`; Agent Setup shows **Out of sync** and disables both Guided
and Advanced writes. Reload or restart the gateway with that file before
editing again. Refreshing only the browser cannot reconcile a stale running
configuration.

`host`, `port`, `config_path`, `auth.token`, and `auth.password` are display-only
in Advanced. The WebSocket config mutation RPCs reject attempts to retarget the
active config file or replace runtime-owned authentication credentials. Other
authentication settings, such as `auth.mode`, remain editable and may require a
gateway restart — `auth.mode` accepts only `none`, `token`, or `trusted-proxy`,
and any other value is rejected by the mutation RPC rather than saved.

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
- live model reasoning ("thinking") as a collapsible block;
- tool-call cards;
- artifact cards;
- pending message queue behavior while compaction or runtime work is in flight;
- manual `/compact`;
- per-turn usage and savings metadata when available;
- copyable session keys.

Use the session selector to switch between existing sessions. Copy the session
key when reporting a bug or asking another AgentOS surface to inspect the
same session.

### Model reasoning (thinking)

When the routed model streams reasoning (Anthropic thinking blocks, DeepSeek
`reasoning_content`, or local `<think>` models), the WebUI shows it live in a
collapsible block that folds away as soon as the reply text starts. Replies
whose reasoning was persisted show a collapsed **Thinking** block in history;
the reasoning body loads on first expand (it is not shipped with history
pages).

Turn it off with `control_ui.show_thinking = false` (or
`AGENTOS_CONTROL_UI_SHOW_THINKING=false`) — the gateway then neither streams
thinking events nor serves reasoning bodies. This is WebUI-only either way:
channel adapters (Slack, Telegram, …) never receive thinking.

### Plan mode

Flip **Plan mode** in the run-modes popover (or type `/plan`) to make the
session research-only: the agent keeps its read, search, and analysis tools
but every mutating tool is withheld until you approve a plan. When the agent
finishes planning it calls `exit_plan_mode`, its turn ends, and the chat
shows the plan in a card with an **Approve plan** button. Approving turns
plan mode off and sends the go-ahead; typing feedback instead keeps plan
mode on and refines the plan. Plan mode is per-session and lives in gateway
memory — a gateway restart clears it.

### Agent questions (ask_user)

When the agent needs a decision that is genuinely yours — scope, a
hard-to-reverse action, conflicting instructions — it can call the `ask_user`
tool. The chat renders a question card with clickable options (plus a
free-text field); picking an answer sends it as your next message and the
agent continues from there. Asking ends the agent's turn, so nothing blocks
while you decide, and typing a reply in the composer works exactly the same
as clicking the card. Open questions also appear in the
[attention inbox](#attention-inbox), answerable from there.

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

Images and audio play inline in the card. The console loads them, and every
download, through short-lived ticketed URLs bound to its connection, so no
token ever appears in a page URL. An artifact published as
`application/vnd.agentos.chart+json` renders as an interactive candlestick
chart instead of a download chip — the `gmgn-market` and `gmgn-token` skills use
this for token price charts.

For the chart payload shape, channel delivery limits, and artifact recovery, see
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

Settings → *Push notifications for approvals* sends a notification to this
browser for every new approval, even with the console closed (HTTPS or
`localhost` only); see
[Approvals and Permissions](approvals-and-permissions.md#where-a-prompt-reaches-you).

While the gateway connection is down the cards stay listed, with their
buttons disabled and the reason shown, since an answer could not be
delivered; the global approval prompt does the same. The prompt's **Answer
later** swaps it for a small dock ("1 approval waiting", **Answer now**,
**Open inbox**) so the rest of the console and its shortcuts stay usable; a
new approval brings the prompt back. The [attention inbox](#attention-inbox)
lists the same approvals next to agent questions and runtime issues.

## Cron job health

A job that keeps failing is marked on its card: a **Degraded** badge, the
consecutive failure count and the last error. They come from the `health`
block on `cron.list` (the engine calls a job degraded at 3 failures in a
row); a gateway older than that is read from the job's `consecutive_errors`
and last error with the same threshold. When the job carries an overlap
policy (`concurrency`), an **Overlap** picker sets it (`forbid` skips a run while one is going,
`replace` stops the running one, `allow` runs them side by side) through
`cron.update`; it does not appear when the job has no `concurrency`. **Run**
and **Pause** are disabled, with the reason, while the gateway is offline or
after the scheduler answered that it is not running.

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

## Skills

Open **Skills** to see what is installed and what the agent can actually reach:

```text
http://127.0.0.1:18791/control/skills
```

The **Installed** tab groups cards by where a skill came from, not by which
directory holds it:

| Group | Contains |
| --- | --- |
| Partners | Skills published by an AgentOS partner |
| Shipped with AgentOS | Skills that ship with the release |
| Installed from a hub | Skills you installed with `agentos skills install` or from this screen |
| Your local skills | Skill directories you added yourself |

Partners wins over the other three, so a partner's skills stay under one
heading whether they shipped with AgentOS or you installed them from that
partner's hub. Partner identity comes from an allowlist inside AgentOS: a skill
manifest can only select a recognized publisher by id, so a third-party skill
cannot borrow a partner's name, link, or logo by writing them into its own
frontmatter. Only a skill that ships with the release may select an id at all —
an installed one is branded by the hub catalog row it came from — so dropping a
directory into a skills path cannot put a card in the Partners group.

The storage layer (`bundled`, `managed`, `personal`, …) is still shown, as a
chip on each card, because it decides which skill wins a name collision. It no
longer decides what heading the card sits under.

**Update** and **Remove** follow what the install record actually supports
rather than the layer. A hub-installed skill whose files no longer sit where
the lockfile recorded them keeps Update — an update re-fetches by identifier —
but loses Remove, and the dialog says why instead of offering a button that
would fail.

### Ready is not the same as offered

A card's status dot answers "is this skill set up". A second, separate label
answers "is the agent being offered it right now", and a skill can be fully
ready and still withheld. When it is, the card and its dialog say which of
these applies: the manifest disables model invocation, a requirement is
missing, a tool it needs is not enabled in this session, a native tool
supersedes it, relevance filtering skipped it for that message, or the injected
skills block hit its character budget. Nothing is shown when the skill is
offered — the absence of a label is the normal case.

### Community tab

Browsing and searching a hub also surfaces skills you have already installed
from that source, including ones the catalog does not list — a skill installed
straight from a GitHub URL appears alongside catalog rows rather than vanishing
from the page it was installed on. Installed rows are marked and appear after
catalog results. Installing or removing a skill updates the Installed marker
immediately, without a page reload.

## Environment

Open **Settings > Environment** to manage the variables in
`~/.agentos/.env` without touching a shell:

```text
http://127.0.0.1:18791/control/env
```

Variables are grouped by what needs them — LLM provider, search, memory, each
installed skill, and your own additions — with the description and, where the
skill declared one, a link to where the credential is obtained. A skill listed
as needing setup can be fixed from its own dialog on the Skills screen too: the
Missing block offers **Set &lt;VAR&gt;** next to the existing install action.

A variable that is not set but is already obtainable elsewhere shows a **Use
&lt;source&gt;** button instead of asking you to go find a value — today that is
the GitHub CLI for `GITHUB_TOKEN` and `GH_TOKEN`, when `gh auth login` has been
run. Deciding whether to offer it never reads the credential, and importing
only happens when you click. The imported value is a copy and will not follow
that tool's own rotation.

Values are masked. **Reveal** asks for confirmation, is rate limited, writes an
audit line, and hides the value again after thirty seconds. Setting a variable
applies it to the running gateway, so a skill that was ineligible becomes
eligible immediately — on this screen, on the Skills screen, and for the agent
itself on its next turn. Provider keys additionally need a restart, and the
screen says so when that is the case.

Two things the screen deliberately makes visible:

- **Locked variables.** Names that steer subprocess execution or AgentOS
  runtime posture, or outbound routing (`PATH`, `LD_PRELOAD`,
  `AGENTOS_AGENT_PERMISSIONS`, `HTTP_PROXY`, …) show a lock instead of an edit
  control. See
  [configuration.md](configuration.md#what-cannot-be-written-through-agentos).
- **Shadowed variables.** If the shell that started the gateway exported a
  variable, that value wins over the file and editing here has no effect until
  the export is removed. The screen warns rather than letting you save
  repeatedly and conclude it is broken.

## Logs and Diagnostics

For local diagnosis:

```sh
agentos diagnostics on
agentos gateway status
agentos doctor
```

Use the Web UI logs and health views to correlate provider readiness, channel
state, session state, and user-visible errors.

## Update Banner

If a newer release of `use-agent-os` is available on PyPI, the status menu
shows an **Update available** badge, and a dismissible notice at the top of
the console links to Settings › Updates. The check matches the 24h throttling
behavior of the CLI update checks. Dismissing the notice suppresses it for that
release version (persisted in local storage); the badge in the status menu
stays. Both honor the global `updates.notify` setting and the
`AGENTOS_NO_UPDATE_NOTICE` environment flag.

**Update now** opens a preflight before anything runs. The update restarts
the gateway, so the preflight lists what that would interrupt: running
sessions (`sessions.list`), scheduled jobs due in the next 10 minutes
(`cron.list`), and pending approvals. A pending approval blocks the update,
with the reason and a way to the inbox; running sessions and due jobs only
warn, and the update starts once you confirm (**Update anyway**). A failed
update is also listed in the attention inbox.

## Safety

The Web UI is local by default. If you bind the gateway to a public interface,
configure token auth and network controls first:

```sh
agentos gateway run --listen 0.0.0.0 --port 18791
```

Do not expose an unauthenticated gateway to the public internet. For the
supported ways to open the console from another machine, see
[`remote-access.md`](remote-access.md).

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
