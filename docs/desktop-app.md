# Desktop App (macOS)

AgentOS App is the native desktop app for macOS 13 or newer, on Apple silicon
and Intel. It installs the engine for you, starts and supervises the local
gateway, and gives you chat and the Trading Desk in one window. It talks to
the same gateway as the [Web UI](web-ui.md) and the [CLI](cli.md), so
sessions, projects, approvals and scheduled jobs are shared between all
three.

## Install

1. Download the dmg for your Mac from the
   [latest release](https://github.com/use-agent-os/agent-os/releases/latest):
   `AgentOS-<version>-arm64.dmg` for Apple silicon, `AgentOS-<version>.dmg`
   for Intel. The app is signed with a Developer ID and notarized, so it
   opens without any Gatekeeper workaround.
2. Drag AgentOS into Applications and open it.
3. On first launch the app looks for an installed `agentos` CLI. If none is
   found (or it is older than the app), the setup screen offers **Install**:
   it installs the `use-agent-os` engine — the same package the terminal
   install uses — with no terminal, Python or `uv` needed. If a gateway is
   already running on the configured endpoint, the app connects to it
   instead; **Connect to an existing gateway instead** does the same for a
   gateway on another host (see [Using a gateway on another
   machine](#using-a-gateway-on-another-machine)).
4. Pick a provider. OpenCAP is listed first as **Recommended**; any provider
   in the catalog works. **Test key** checks the key against the provider
   before you save, and the last step tries it for real. **Skip for now**
   leaves a "Choose a provider" card on Home until one is configured.

The managed gateway's output goes to `~/Library/Logs/AgentOS/gateway.log`
(Settings › Advanced › App logs). Settings › Advanced also has **Reinstall
engine** and **Remove engine**; removing the engine keeps `~/.agentos`.

## Using a gateway on another machine

The app does not have to run the gateway itself. Settings › Gateway ›
Connection has a **Run mode**: **Managed** (the default) starts and
supervises `agentos gateway run` on this Mac; **External** only connects to
a gateway that is already running, on this machine or on another one — a
server, a VPS, a home box. For an external gateway set:

- **Protocol**: **HTTP** for a plain bind (loopback, an SSH tunnel, a LAN
  bind with a token) or **HTTPS** for a gateway behind TLS (a reverse proxy,
  Tailscale Serve, `[tls]` in its config). The WebSocket follows: `ws://`
  with HTTP, `wss://` with HTTPS.
- **Host** and **Port**: where the gateway answers, as the app will reach
  it. Behind a proxy on `https://agent.example.com` that is host
  `agent.example.com`, port `443`.
- **Auth token**: the gateway's `auth.token`. A gateway reached from another
  machine must enforce one (`auth.mode = "token"`); the app sends it as a
  bearer token on every request and on the WebSocket connect.

Then press **Save**. If the app was running a gateway of its own at that
moment, it stops it — the app no longer manages one — and connects to the
endpoint you entered; a gateway it had merely adopted (one started from a
terminal) is left running. A gateway on another host must also know the origin
the app will present, which is the gateway's own: declare it there with
`agentos config set gateway.public_origin https://agent.example.com --config
~/.agentos/config.toml` and restart it. The recipes, and the `agentos
doctor` checks to run before exposing anything, are in
[Remote access](remote-access.md). For a full walkthrough from the published
image, see [Run the gateway in a container](container-gateway.md).

In external mode the app only reports the gateway's state: the light in the
sidebar footer, the Process row in Settings › Gateway and the menu bar
item show whether it is reachable, and none of them starts, stops or
restarts it — the gateway belongs to whoever runs it. The one action left
is **Reconnect**, offered when the saved endpoint differs from the one the
app is connected to. Configuration changes that need a restart (providers,
channels, the router) still ask the gateway to restart itself in place, as
they do from the Web UI.

To go back, set **Run mode** to **Managed**. A host that is not on this Mac
(the external gateway's) is replaced with the local default, HTTP on
`127.0.0.1:18791`, since a managed gateway binds here; a local host and port
you chose before are kept. Switching back to **External** before saving
restores the endpoint you had. On **Save** the app starts its own gateway, or
adopts one already answering on that endpoint, and connects to it.

## Chat and Trade

The mode pill at the top of the window reads **Chat | Trade** (⌘⇧T switches).

- **Chat** is the conversation surface: streaming replies, tool activity,
  approvals, plan cards, `ask_user` question cards and artifacts — the same
  transcript the Web UI shows, in the desktop's own styling.
- **Trade** opens the Trading Desk: wallets, swaps, holdings, orders,
  history and allowances. See [Trading](features/trading.md).

The pill enters Trade at your newest desk chat. Any chat with the trading
agent, started from Trade or with the **+** on the Trading desk project folder,
opens in Trade when you pick it in the sidebar; every other chat opens in Chat.

The strip next to the pill shows **AWAITING** while a decision is waiting
for you and **RUNNING** while a mission is in flight.

### Run modes

The sliders button left of the paperclip in the composer opens **Run modes**
for the open session:

| Control | What it does |
| --- | --- |
| Execution | Approval prompts or bypass for this session. The global default is shown next to it. |
| Pilot Router | Turns [model routing](features/agentos-router.md) on or off for this session. |
| Plan mode | Research-only until you approve a plan (same as typing `/plan`; see [Plan mode](web-ui.md#plan-mode)). |
| Usage | The session's token usage so far. |

### Files and cards in the chat

A file the agent generates shows as a **file card**: a tile drawn by kind
(spreadsheet, document, PDF, presentation, archive, data, code, web page),
the file name, a subtitle such as "Spreadsheet · XLSX · 5 KB", and a
**Download** button. Images and audio play inline.

Trading commands run with `--json` publish cards the chat renders live:

- **DCA mandates** show a countdown to the next buy, spent-of-cap progress,
  average buy price vs now and a buys chart, with **Approve & start**,
  **Pause**, **Buy now** and **Stop** on the card. See [DCA](dca.md).
- **Uniswap V4 liquidity** cards show a pool, its range distribution or a
  position. See [LP cards](lp-cards.md) and [LP writes](lp-write.md).

The trading desk's **Liquidity** tab lists every liquidity position the
desk's wallets hold on Uniswap (V2, V3, V4), Aero and Aerodrome, on Base and
Robinhood Chain: the pair, the range with the price marked on it, the market
caps (USD) that range spans, what is deposited, unclaimed fees and gauge rewards, and the value. Out-of-range
positions come first. The engine reads the chain only while the tab is open
and serves its cache in between. A Uniswap V4 row has **Collect** and
**Remove** buttons: each prepares an order that waits for your approval on the
Approvals tab, and nothing is signed before that. Positions on the other
protocols are listed only. See [the liquidity book](lp-book.md).

## Sidebar

The sidebar lists your sessions, grouped by Today, Yesterday and This week,
with project folders above them.

- **Projects** are folders. Drag a session onto a folder to file it there
  (onto the "Sessions" header to unfile it). The `+` just left of a folder's
  chat count starts a new chat in that project with its agent; it appears on
  hover or keyboard focus. Click a folder to open its page, where the title
  renames on click and the shared brief saves itself (⌘S saves now). See
  [Projects](sessions.md#projects-group-sessions-and-share-knowledge).
- **Session names** come from the first message, whether the chat started
  here, in the Web UI or in a channel such as Telegram. A name you set is
  never overwritten. See [Session titles](configuration.md#session-titles).
- **Right-click a session** to rename, pin, mark as unread, export as
  Markdown, move it to a project, archive or delete it.
- **Pinned** sessions sit in their own section at the top of the sidebar,
  above Projects. Right-click a session and choose **Pin** (or **Unpin**), or
  ask the agent in chat ("pin this session"). Pins are stored on the gateway,
  so the Web UI shows the same ones; a pinned session filed in a project
  appears both on top and in its folder. See
  [Pin a Session](sessions.md#pin-a-session).

### Selecting several sessions

| Action | Effect |
| --- | --- |
| ⌘-click | Add or remove one row from the selection. |
| ⇧-click | Select the range from the last row clicked, in the order shown (chats inside open project folders included). |
| ⌘A | Select every row shown. |
| Escape | Clear the selection. |

Right-click inside the selection for one menu that acts on all of it:
**Pin**, **Mark as unread**, **Archive** and **Delete N sessions…** (asked
once). Rows the gateway could not delete stay selected and a toast gives
the count. A plain click still opens a chat and clears the selection.

## Scheduled jobs and skills

**Scheduled jobs** (⌘⇧J) opens as a panel over the current view, so checking
a schedule never leaves the conversation. Jobs use the same `cron.*` model
as the Web UI and `agentos cron`, with a natural-language schedule builder.
See [Scheduling](scheduling.md). **Skills** (⌘⇧K) browses and installs
skills; see [Skills](features/skills.md).

## Files

**Files** (⌘⇧E) opens the agent's workspace as a panel over the current view:
the folder tree on the left, the selected file on the right. It is read-only;
nothing in it can create, change or delete a file.

- **Previews.** Markdown renders (switch to **Source** for the raw text),
  text and code show with syntax colouring, images display to fit, and PDFs
  draw page by page. Text previews stop at 1 MB, images at 20 MB and PDFs at
  30 MB. Anything else says it has no preview.
- **What is left out.** Hidden files and folders (names starting with a dot,
  such as `.env` and `.git`), keys and certificates, credential files,
  databases and lock files are neither listed nor openable. Links are listed
  but never followed, so nothing outside the workspace can be reached through
  one.
- **Nothing in a file runs.** An HTML or SVG file is shown as source or as a
  still image. A Markdown file's remote images are not loaded; a link to each
  is shown instead, since loading one would tell its host the file was opened.
- **Folders load as you open them**, 500 items at a time, so a large project
  folder does not slow the panel down. **Refresh** re-reads what is open.

The files come from the gateway, so against a
[gateway on another machine](#using-a-gateway-on-another-machine) you browse
that machine's workspace. A gateway older than this feature shows a note
asking for an update.

## Quick Ask

Press **⌥ Space** in any app to open a small prompt over it, the way
Spotlight works. Type a question and press **Return** to ask it in a new
chat, or **⌥ Return** to add it to the chat the window was on (or the last
one you opened). AgentOS comes forward on that chat with the reply
streaming. **Escape** or a click elsewhere closes the prompt without
sending; what you typed is still there next time.

Settings › Behaviour › Quick Ask turns the shortcut off or moves it to
⌃ Space or ⌘⇧ Space. If another app already uses the key, the pane says
"Unavailable"; choose another one. The prompt works while the app is
running, even with its window closed. A question asked while the gateway is
stopped waits in the chat and is sent once the gateway is running again.

## Runtime status and what needs you

The toolbar's status capsule shows the same runtime truth as the Web UI's
status button at the bottom of the rail: **Ready**, **Degraded**, **Connecting**, **Blocked**,
**Offline** or **Unknown**, with how fresh the view is (`Live · 12 s`,
`Snapshot · 3 min`, `Unavailable`). Click it for the reason and one way
forward. Because the app runs the gateway, a stopped gateway reads Offline
and offers **Start gateway** (an external gateway offers **Retry
connection** instead: the app never starts or stops a gateway it does not
manage).

The tray icon beside it opens **Needs you**: approvals of every kind, agent
questions (`ask_user`) and runtime issues in one list, with a count on the
icon. Approve, deny, pick an answer or type a reply right there; the same
command waiting in several sessions is grouped ("Seen in N sessions") and
each occurrence links to its session. Runtime issues come from the doctor
and the connection: open the fix steps in place, jump to the right
Settings section, dismiss for this session, or **Restart gateway**, which
asks before restarting. Questions are collected from the sessions this
window has open; the gateway does not list unanswered questions.

## Notifications

The app notifies you when a reply finishes or fails, when an approval is
needed, when a scheduled job runs, and when the gateway stops on its own.
While you are watching the session in question it only plays the sound. The
toolbar bell keeps recent notifications, a Do not disturb switch (30 min,
1 h, 3 h, until tomorrow 9:00) and a sound toggle. Settings › Notifications
holds the per-event switches, the minimum reply length, the sound, and the
Dock badge and bounce. If nothing shows, check that AgentOS is allowed in
System Settings › Notifications and that no Focus mode is on.

## Settings

Open Settings with ⌘, or the toolbar gear. Escape or **Done** closes it. The
first time after the app starts, Settings opens on **Overview**; after that it
reopens on the section you last used. The rail on the left lists every
section with its icon. Type in its search field to narrow it: it matches
section names and the words for what is on each page, such as "telegram",
"dark mode", "text size", "uniswap", "slippage", "grok" or "api key", every
word in any order ("brave key" works); the best match comes first and Return
opens it. A dot marks a section that needs you: no provider yet, or something
switched on that cannot work, such as a missing key.

| Group | Sections |
| --- | --- |
| (top) | Overview |
| Agent | Providers, Pilot Router, Memory, Capabilities, Skills, Channels, Environment, Trading |
| App | Gateway, Appearance, Notifications, Security, Behaviour, Shortcuts |
| More | Advanced, About |

Each page starts with its state and its one main action; settings most
people never change sit under a fold (**Advanced**, **Fine-tuning**).
Providers, Pilot Router, Memory, Capabilities, Channels and Trading edit the
gateway's own configuration through the same guided writes the Web UI uses.
An unsaved edit in Memory or Capabilities stays in its form when you go to
another section and come back; a key pasted into Capabilities is forgotten
when Settings closes.

- **Overview** is the setup checklist. Required: **Gateway running** and
  **Provider and model**. Recommended: model routing, memory, web search,
  image generation, voice, search on X, channels, skills, environment
  variables, a trading wallet (while trading is on) and updates (once the app
  has checked for them). Each row says what is in place now, and its button
  (Set up, Fix, Review) lands on the place that changes it, with the keyboard
  there too. The head says where setup stands in one line: how many required
  steps are left, else "Required setup done" with how many things need fixing
  or how many optional ones remain, and "All set" only when nothing is left.
  The hero says the same thing in words: the required step first, else what
  is broken (one is named with its fix; several are counted, the first one
  leading), else a suggestion for the next optional step.
- **Providers** leads with the active provider: its key, the **Test key**
  result, the default model, the thinking level and the connection. A refused
  key is explained in one sentence, with the provider's own message under it
  to copy. Below, **Switch provider** lists the others with a filter; picking
  one opens its setup in place, and nothing changes until you save. With no
  provider yet, the list comes first with the recommended one marked.
- **Pilot Router**: the routing mode as cards that each say what they do
  (Pilot, LLM judge, Jev, Off) with the judge model or TypeSafe key a mode
  needs, the tiers (model and thinking per tier, one of them the default),
  and the safety net, high-risk floor and translation cap under
  **Fine-tuning**. Changes wait in one save bar for the page.
- **Memory** is where the agent's memory is set up, in four tabs: Remembered,
  Knowledge, Try it and Setup.
- **Capabilities** sets up web search, search on X (Grok, with an xAI key or a
  SuperGrok login), image generation and voice, under a strip that says which
  service answers each one and whether its key is in place. **Test search**
  runs one real search with the saved web search settings; a failure is said
  in one sentence (key rejected, request refused, too many searches, no
  answer in time, not reachable), with the service's own message behind
  **Show details**. Numbers are checked against what the engine accepts
  (results per search 1 to 20; X search timeouts 30 to 300 and 30 to 600
  seconds, 0 to 5 retries), and an empty one is refused rather than saved as
  a default. Switching a capability off says so ("Search on X is off.").
- **Skills** opens on a summary strip (installed, ready, needs setup, offered
  to the agent); **Needs setup** opens the Skills panel on those skills, and a card lists
  where skills come from.
- **Trading** carries its on/off switch in the page head and opens on a
  summary strip: the wallets, the amount above which the agent asks you, and
  the daily cap. Below: the swap route (the AgentOS DEX, with **Test
  connection**), the networks and their RPC endpoints, the vault (where it
  lives and whether it unlocks automatically or asks each time; changing that
  asks for the vault password), and the agent's limits (ask-me-above amount,
  daily cap, approval timeout, default slippage). When the gateway does not
  take trading changes from this window, the page says so and is read-only.
- **Gateway** shows whether the gateway runs and where, with Start, Stop and
  Restart when the app manages it and a link to the web console. Run mode is
  Managed (recommended) or External. The address (protocol, host, port) and
  the auth token fold away while a managed gateway is on its defaults; the
  command-line tool a managed gateway launches from is below them.
- **Appearance** holds the theme (Auto, Light, Dark), the colour palette,
  the UI scale (⌘ + and ⌘ − step it, ⌘ 0 resets), reduce transparency and
  the optional pet mascot.
- **Notifications** carries its master switch in the page head, with
  **Send a test**. It holds what happens while the window is in front
  (nothing, a banner in the app, or a system notification), Do not disturb,
  whether notifications show details, which events notify (a reply finishes,
  a reply fails, the agent needs approval, a scheduled job runs, a swap
  settles, the gateway stops on its own), the sound, the Dock badge and
  bounce, and a link to the macOS permission.
- **Security** holds **Confirm with Touch ID**: Off, high-risk approvals, or
  every approval (and every click that trades or may spend). With it on, the
  desk asks for a fingerprint before it approves, trades or fires anything,
  and cancelling the prompt sends nothing. It shows whether this Mac's sensor
  is ready and has **Try the prompt**.
- **Behaviour** holds open at login, what to open at launch (Home or the last
  session), stopping the gateway on quit, Show in menu bar, Quick Ask (its
  global shortcut on or off, and which keys), Return vs ⌘Return to send, and
  the sidebar width (with a reset).
- **Shortcuts** lists every key the app answers to.
- **Advanced** has the app settings file, the app logs and the gateway
  configuration file (open or show in Finder), **Copy diagnostics**, and
  under **Start over** reset all app settings, reinstall the engine and remove
  the engine, each of which asks first.
- **About** has an **App** card and an **Engine** card (a **Gateway** card for
  an external gateway, updated on its own host), and the runtime versions and
  links.

Channels is the Web UI's Channels screen inside the app: every messaging
adapter the gateway is configured with (Telegram, Discord, Slack, Email), its
live status, when it connected and how often it has been restarted, under a
summary strip whose **Needs attention** and **Pairing requests** figures jump
to the channel they count. With no channel yet the page is one empty state
with **Add channel**, which opens a sheet built from the gateway's adapter
catalog: pick the adapter, paste what it needs, save; **Configure** reopens
it for an existing channel with the adapter and name locked, and a secret
left blank keeps the one already stored. Each card has an Enabled switch,
Restart and Remove, and a Telegram card lists who is waiting to pair and who
is in, with Pair, Deny and Disconnect. Changes to the configuration restart a
gateway the app manages so they apply at once; with an external gateway the
pane only says to restart it on its host, with no **Restart gateway** button
(the app does not run that process). Providers and Router do the same.

Environment is the Web UI's Environment screen inside the app: every variable
the gateway, its providers and your skills read, grouped by what uses it,
under a summary strip (set, missing, shadowed by the shell; **Set** and
**Missing** filter the list to those variables), with a search field and an
All / Missing / Set / Custom filter. Set, replace, import from a source that
already holds the credential, or remove a variable in `~/.agentos/.env`;
**Add variable** stores one of your own. Listings show a secret masked (its
first and last four characters, or only dots when it is short) and a plain
setting as it is. Showing a secret in full asks first, is rate limited by the
gateway, and hides again after 30 seconds. **Open Environment** next to a
missing key in Capabilities or Memory opens this page searched to that
variable.

## Updates

The app and the engine update together from one release.

- New releases show up as an **Update** pill in the toolbar and a single
  notice. Nothing downloads or installs until you click: **Update** updates
  the engine in place, then downloads the app; **Restart** installs it. The
  pill stays until the release is fully applied.
- Settings › About has an **Engine** card and an **App** card. Each shows
  the running version and a **Latest** row with the newest version ("Not
  checked yet" before the first check) and, while the app downloads, the
  download percent beside it.
- An engine update restarts the gateway, so it goes through a preflight
  in Settings › About first: running sessions, scheduled jobs due in the
  next 10 minutes, and pending approvals. A pending approval blocks the
  update (with a way to **Needs you**); running work only warns, and the
  update starts once you confirm (**Update anyway**). The notice's
  **Update** opens that preflight too.
- Scheduled jobs the gateway reports as degraded carry a **Degraded** chip
  with the failure streak, and the last error, in the job's detail pane.
- After `agentos upgrade` in a terminal, the app offers **Restart gateway**
  so the running gateway picks up the new engine.
- With an external gateway (Settings › Gateway › Run mode External) the
  engine on this Mac is not the one running, so it is left out: no
  **Update** for it, no **Restart gateway**. Instead, when the connected
  gateway runs an older release than the latest, the toolbar shows
  **Gateway outdated** and About has a **Gateway** card with only its
  running version and the latest. There is no button: update the gateway on
  its own host (`agentos upgrade`, or pull the new image) and restart it
  there. The App card updates as usual.

## Keyboard Shortcuts

The full list is in Settings › Shortcuts.

| Keys | Action |
| --- | --- |
| ⌥ Space (from any app) | Quick Ask (Settings › Behaviour) |
| ⌘N | New session |
| ⌘⇧O | New chat in this agent |
| ⌘, | Open settings |
| ⌘⇧K | Open skills |
| ⌘⇧J | Open scheduled jobs |
| ⌘⇧E | Open files |
| ⌘⇧T | Switch Chat / Trade |
| ⌘⇧S | Show or hide the sidebar |
| ⌘+ / ⌘− / ⌘0 | Larger UI / smaller UI / actual size |
| Escape | Stop the current reply, or close a sheet |
| Return (or ⌘Return) | Send; the other key adds a new line (Settings › Behaviour) |
| ↑ / ↓ | Previous or next message you sent |
| / | Slash commands |
| ⌘S | Save a project brief now |
| ⌘Return (Trade) | Press the focused ticket's, sheet's or card's action: review, swap, bridge, send, approve (never a destructive button) |

## Troubleshooting

- **"The gateway is not running."** Start it from the sidebar, or open
  Settings › Gateway for Start, Stop and Restart and the endpoint. See
  [Gateway](gateway.md).
- **"Gateway exited with a configuration error (exit 78)."** The gateway the
  app started refused its configuration (a config file that does not parse
  or validate, a migration that cannot apply). The app does not change your
  configuration: `agentos doctor` has no automatic fix, so the app runs
  `agentos doctor --json` once to name the problem (the first error finding
  and its fix command appear in Settings › Gateway and the sidebar tooltip),
  retries the start once in case it was already fixed, and then shows the
  error. Fix what doctor names, then press Start.
- **Gateway shows "starting" for a few seconds.** The app reports the
  gateway running once its `/ready` probe answers, after the channels have
  started; until then the chat keeps connecting on its own.
- **Install failed.** The failure screen offers Retry, Copy output and Show
  log in Finder; the log is in `~/.agentos/logs/bootstrap-<timestamp>.log`.
- **Copy diagnostics** in Settings › Advanced copies a report with the
  gateway token redacted, for a bug report.

For building the app from source, see
[`desktop/README.md`](https://github.com/use-agent-os/agent-os/blob/main/desktop/README.md).

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
