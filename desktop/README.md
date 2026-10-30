# AgentOS Desktop

macOS desktop shell for AgentOS, built with Electron + React + TypeScript.
**macOS is the only supported platform**: the window chrome (`hiddenInset`
title bar, traffic-light inset), menu bar, quit semantics and CLI lookup paths
all assume Darwin, and `package.json` declares `"os": ["darwin"]`.
It is a thin client: the app locates the installed `agentos` CLI, supervises
`agentos gateway run`, and renders its own desktop UI on top of the gateway's
WebSocket/REST API.

## Shares logic with the web console, never UI

The renderer imports the console's non-visual layer straight from
`frontend/src` through the `@/` alias — the WebSocket RPC client, the chat
hooks (`useTranscript`, `useAttachments`, `useSlashCommands`, `useRoutePin`,
`usePendingQueue`), the approvals monitor, the imperative transcript
renderer, the session/chat pure logic, and the runtime honesty layer
(`useRuntimeTruth`, `useAttentionInbox`, runtime issues, the
disabled-with-reason checks and `useUpdatePreflight`). One protocol implementation, one
place to fix a gateway change.

What it does **not** take is the console's appearance. `frontend`'s
stylesheets are never imported here; `views/chat/chat.css` restyles the
shared transcript class names (`.msg`, `.chat-tools-collapse`,
`.msg-artifact-*`, …) in the desktop's own vocabulary, and the composer is a
desktop component that merely matches the console's prop contract so the
shared hooks can drive it. Desktop code lives under the `~/` alias; `@/` is
always the console.

## Layout

```
desktop/
├── electron.vite.config.ts   # main / preload / renderer targets + the @ and ~ aliases
├── vitest.config.ts          # unit tests (jsdom by default, node per-file)
├── electron-builder.yml      # .app / .dmg packaging
├── tsconfig.node.json        # main + preload + shared
├── tsconfig.web.json         # renderer + shared + the console's sources
├── resources/                # icons and other packaging assets
└── src/
    ├── shared/               # contracts used by all three processes
    │   ├── ipc.ts            #   channel names + DesktopApi shape
    │   ├── theme.ts          #   ThemePreference / PaletteId / resolveTheme
    │   ├── settings.ts       #   DesktopSettings + normalizer
    │   ├── quick-ask.ts      #   Quick Ask shortcuts, submission payload + validation
    │   └── gateway.ts        #   GatewayStatus
    ├── main/                 # Electron main process (Node)
    │   ├── index.ts          #   lifecycle, single instance, quit hook
    │   ├── loopback-cors.ts  #   Origin presented to the gateway + CORS answer translated back
    │   ├── window.ts         #   BrowserWindow (vibrancy, hiddenInset, sandbox)
    │   ├── menu.ts           #   macOS application menu
    │   ├── tray/             #   menu bar status item: pure menu builder, summary validation, Tray
    │   ├── ipc/              #   one file per IPC domain, registered in index.ts
    │   ├── settings/store.ts #   atomic JSON settings in userData/
    │   ├── gateway/          #   cli-locator + process supervisor (spawn, adopt, health)
    │   ├── bootstrap/        #   first-run engine install: discovery, install.sh runner, controller
    │   ├── quick-ask/        #   global hotkey, the floating panel window, hand-off inbox
    │   └── updates/          #   engine updater (agentos upgrade) + electron-updater controller
    ├── preload/index.ts      # contextBridge -> window.agentos (typed DesktopApi)
    └── renderer/             # React app (browser, no Node access)
        ├── index.html        #   CSP locked to self + loopback
        └── src/
            ├── app/          #   App, AppShell, GatewayProviders (rpc + approvals), router
            ├── views/chat/   #   ChatView + chat.css — the desktop skin for the
            │                 #   console's transcript DOM
            ├── views/jobs/   #   Scheduled jobs panel (a layer over the window, not a
            │                 #   route): job list + blueprints, detail pane, create/edit
            │                 #   sheet with the natural schedule builder
            ├── views/flows/  #   Image Flows: home (`/flows`, list + templates) and the
            │                 #   node editor (`/flows/:id`, @xyflow/react) over `flows.*`
            ├── views/projects/ # Project page (`/projects/:id`): renamable title,
            │                 #   self-saving brief, the chats filed there
            ├── views/quick-ask/ # The Quick Ask panel (its own window, `#/quick-ask`)
            ├── views/settings/ # Settings sheet: SettingsPanel (rail + section), one pane
            │                 #   per section (providers, router, skills, channels, environment,
            │                 #   trading, gateway, appearance, notifications, security, behaviour,
            │                 #   shortcuts, advanced, about), parts.tsx, logic.ts, channels-logic.ts
            ├── components/   #   Sidebar (+ resizer, project folders, session list with
            │                 #   its row menu and view menu), Toolbar, menu/ (PopMenu,
            │                 #   Menu, items, submenus), pet/, composer/, TruthBadge (the
            │                 #   toolbar's runtime status) and AttentionPanel ("Needs you")
            ├── theme/        #   theme system (see below)
            ├── stores/       #   zustand: gateway, sessions, projects, live, settings, ui,
            │                 #   session-marks (pin/archive/unread), session-view
            ├── lib/          #   desktop-api bridge, motion curves, relative time,
            │                 #   use-runtime-host (process facts for the shared truth hook),
            │                 #   required-methods (RPCs the desktop adds; contract-tested)
            ├── i18n/         #   t() catalog for desktop-only copy
            └── assets/fonts/ #   Bricolage Grotesque (wordmark) + JetBrains Mono
```

## Theme system

Two axes, both persisted in settings and mirrored to macOS:

| Axis         | Values                           | Where it lives                   |
| ------------ | -------------------------------- | -------------------------------- |
| `preference` | `system` \| `light` \| `dark`    | `shared/theme.ts`                |
| `palette`    | `tactical` (brand) \| `graphite` | `renderer/src/theme/palettes.ts` |

Flow:

1. `main/ipc/theme.ts` sets `nativeTheme.themeSource` from the saved preference
   so window chrome and `prefers-color-scheme` agree with the app.
2. `renderer/src/theme/theme-store.ts` (`initTheme`) loads settings, resolves
   the mode, and paints via `apply.ts`: `data-theme`, `data-palette`,
   `color-scheme`, and every colour token as a `--<name>` custom property on
   `<html>`.
3. `theme/tokens.css` maps those custom properties into Tailwind (`bg-primary`,
   `text-muted-foreground`, …) and owns the palette-independent parts: fonts,
   radius scale, z-index, base element styles.
4. OS appearance changes reach the store through `matchMedia` and the
   `theme:changed` IPC push; they only repaint while preference is `system`.

Adding a palette: add an id to `PALETTE_IDS` in `shared/theme.ts` and a full
`PaletteDefinition` in `palettes.ts`. The type forces every token for both
modes; `palettes.test.ts` fails otherwise.

UI: `ThemeToggle` (title bar, cycles preference) and `ThemeControls`
(Settings > Appearance: the mode row and the searchable palette gallery).
Tactical and Graphite are hand-tuned; the other palettes are derived from
three seeds per mode (ground, ink, signal) by `derive()` in `palettes.ts`, so
adding one is a `palette(id, label, description, darkSeed, lightSeed)` line
plus its id in `PALETTE_IDS`.

## Settings

Settings is a sheet over the window, the Scheduled jobs posture: a quiet
rail of sections on the left, the chosen one on the right as soft cards,
Escape or "Done" to leave, the section remembered between opens (the
`settingsOpen` / `settingsSection` flags in `stores/ui.ts`). Reached from the
toolbar gear, ⌘, or the app menu's "Settings…" (main pushes `settings:open`). App preferences persist to `settings.json` through
`settings:update`; `shared/settings.ts` owns that schema. The agent
sections edit the **gateway's** configuration instead, through the same
guided RPCs the web console's setup uses, with the `config.snapshot`
revision on every write so a stale form cannot overwrite a newer file.

| Section       | What it holds                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| ------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Models        | Provider (catalog from `onboarding.catalog`), API key / env key / base URL / proxy, default model from `models.list`; saved via `onboarding.provider.configure`. Thinking level via `config.set`.                                                                                                                                                                                                                                                                                                                                  |
| Pilot Router  | Mode (Pilot / LLM judge / Off), default tier, safety net, judge model, translation cap, and the tier ladder c0–c3 + vision with a model and thinking level per rung; saved via `onboarding.router.configure` using the console's `buildRouterConfigureParams`.                                                                                                                                                                                                                                                                     |
| Channels      | The console's Channels screen: every configured adapter from `channels.status` with status, connected time, restart count and the Telegram pairing list (`channels.pairing.*`); Add/Configure through a sheet built from the catalog's channel specs (`onboarding.channel.probe` + `upsert`), an Enabled switch (`onboarding.channel.enable`/`disable`), Restart (`channels.restart`) and Remove (`onboarding.channel.remove`), all with the snapshot revision; a managed gateway is restarted to apply, as Providers does. |
| Gateway       | Live status with Start/Stop/Restart, endpoint copy + open console; an editable draft of mode/host/port/token/CLI path with validation, Save/Revert, and a "restart to apply" notice when the running endpoint differs.                                                                                                                                                                                                                                                                                                             |
| Appearance    | Theme + palette (`ThemeRows`), text size (`data-text-size` on `<html>`), reduce transparency (`data-transparency` + `win.setVibrancy`).                                                                                                                                                                                                                                                                                                                                                                                            |
| Notifications | Master switch; what happens while the window is in front (nothing / in-app banner / system notification); Do not disturb (30 min, 1 h, 3 h, until tomorrow 9:00); show details; per-event switches (reply finished with a minimum length, reply failed, approval needed, scheduled job runs off/failures/all, gateway stopped on its own); sound on/off and which (the app chime or a macOS alert sound); Dock badge and bounce; a test button and a door to System Settings › Notifications. See [Notifications](#notifications). |
| Security      | Confirm with Touch ID: Off (default) / High-risk approvals / Every approval. Disabled with an explanation when the Mac cannot prompt (no sensor, none enrolled, lid closed) unless it is already on, so it can always be turned off; a Test button runs the prompt and reports the outcome. See [Touch ID](#touch-id).                                                                                                                                                                                                             |
| Behaviour     | Open at login (mirrored to `app.setLoginItemSettings`), open at launch (home / last session), stop the gateway on quit, show in menu bar (see [Menu bar](#menu-bar)), Quick Ask (on/off, which global shortcut, a note when macOS refused the key; see [Quick Ask](#quick-ask)), Return vs ⌘Return to send, sidebar width reset.                                                                                                                                                                                                   |
| Shortcuts     | The keys the app binds (⌘, ⌘N ⌘⇧S ⌘⇧O …, and the Quick Ask key chosen under Behaviour); static, nothing is rebindable.                                                                                                                                                                                                                                                                                                                                                                                                             |
| Advanced      | Paths (settings file, logs, gateway `config.toml`) with Finder/open actions, copy diagnostics (token redacted), reset all app settings behind an alertdialog.                                                                                                                                                                                                                                                                                                                                                                      |
| About         | App/Electron/Chromium versions, gateway version + uptime, `updates.check`, links.                                                                                                                                                                                                                                                                                                                                                                                                                                                  |

Main mirrors a few settings onto the window/OS on every write
(`mirrorSettingsToOs` in `main/index.ts`): the login item, window vibrancy,
the zoom factor, the menu bar item and the Quick Ask global shortcut. `nativeTheme` follows the theme section the same way, so a
reset repaints correctly.

## Menu bar

The app keeps the gateway (and with it DCA mandates and scheduled jobs)
running after the last window closes, so it also keeps a status item in the
macOS menu bar, the way Docker or Tailscale do. Clicking it drops a native
menu:

```
●  Gateway running · 127.0.0.1:18791
───────────────────────────────
2 approvals waiting                 → opens the desk (or the window, for a tool approval)
Next DCA buy · ETH → USDC in 14 min → opens the desk
1 reply in progress                 → opens the window
───────────────────────────────
Open AgentOS
New Chat
Quick Ask…                          (while Quick Ask is on)
───────────────────────────────
Stop Gateway                        (Start / Stop / Restart by state; managed mode only)
Settings…
Quit AgentOS
```

While approvals wait, their count sits beside the icon (`tray.setTitle`), so
a trade waiting on you shows from any app. Settings › Behaviour › "Show in
menu bar" (`general.showInMenuBar`, on by default) removes and restores it.

- `main/tray/menu.ts`: `buildTrayMenu(summary, status, settings, actions)`,
  a pure function (unit-tested in the node environment) with the clicks
  injected as a small `actions` object. Rows appear only when there is
  something to say; an external gateway gets no lifecycle row, but the status
  line still names its endpoint; an error carries its first line, cut short,
  and offers Restart.
- `main/tray/tray.ts` (`MenuBar`): owns the `Tray` and redraws on every
  input — a summary push, `gateway:changed` (the supervisor's subscription),
  a settings write (`mirrorSettingsToOs` calls `sync`), and every 30 s while a
  DCA countdown is shown. The icon is a template pair,
  `resources/trayTemplate.png` + `@2x` (`scripts/make-tray-icon.py`), so
  macOS tints it for a light or dark bar; it ships through `extraResources`.
- Main never talks to the gateway: the renderer pushes `tray:summary`
  (`{ liveTurns, approvalsPending, tradeApprovals, nextMandate }`) from
  `lib/use-tray-summary.ts`, bound once from `AppShell`, at most once a
  second. `main/tray/summary.ts` validates it (counts clamped to whole
  numbers, the label cut to one short line, the time a real instant). Its
  sources: `useLive` (live turns), the console's `useApprovals` (tool
  approvals), and the desk's orders awaiting approval plus live DCA mandates
  (`trading.orders.list` / `trading.dca.list`, the desk's own query keys).
  Asking for those starts the engine's trading loop, which an ordinary chat
  must not do, so they only run once trading is in use on this Mac (a desk
  session exists, or a trading event arrived) and are event-driven with a
  60 s backstop rather than polled.
- With no window there is no renderer to ask: closing the last window clears
  the summary, and the menu says only what main knows (the gateway).
- Clicks: Open focuses, restores or creates the main window (as `activate`
  does). New Chat, the approvals row and the DCA row push `tray:navigate`
  with a `NotifyTarget` (`newChat`, `trading`, `approvals`) that the
  notifications' activation handler (`use-notifications.ts`) routes, so
  there is one navigation router. A window that was just created, or is
  reloading, gets the target once its first summary arrives. Settings… goes
  through `requestOpenSettings`; Quit is `app.quit()`, so `before-quit`
  stops a managed gateway exactly as ⌘Q does.

## Notifications

Every notification goes through one door, `notify()` in
`renderer/src/lib/notifications/dispatch.ts`, which reads the settings at fire
time and asks `decideDelivery()` (`logic.ts`, pure and unit-tested) where the
event goes: a native notification, an in-app banner (sonner), a sound, a Dock
bounce, and whether it is recorded in the bell. The rules:

- Off, or the event's switch off, or shorter than "only replies longer than":
  nothing.
- Do not disturb: recorded in the bell, nothing shown or played.
- The event is about the session on screen and the window is in front: only
  the sound; the user is watching it happen.
- Window in front, other session: per "while the window is in front".
- Window behind: native notification, sound, Dock bounce.

The sources (`renderer/src/lib/use-notifications.ts`, bound once from
`AppShell`):

| Event                 | Where it comes from                                                                                                                                                                                                                                                                                 |
| --------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Reply finished/failed | The sessions list. The gateway broadcasts `sessions.changed` on every task transition to connections that called `sessions.subscribe` (the sessions store does, on every connect); the list refetches and a row that stops being live is a settled reply. Cancelled and interrupted runs are quiet. |
| Approval needed       | The console's approval poller (`useApprovals`): the pending count grew.                                                                                                                                                                                                                             |
| Scheduled job         | `cron.run.finished` on the wildcard topic; the shell calls `cron.subscribe` for the app's lifetime (the Jobs panel only listens).                                                                                                                                                                   |
| Gateway stopped       | The gateway store went from `running` to `error` without a stop being asked for.                                                                                                                                                                                                                    |

Native notifications are posted by **main** (`main/notify/notifier.ts`,
Electron's `Notification`), not the renderer's web API, so a click can focus
the window and push `notify:activated` with a target (session, jobs, settings)
that the renderer navigates to. They are always `silent`; the sound is the
renderer's job (`lib/notify.ts`): the synthesised chime, or a macOS alert
sound that main plays with `afplay` so it works with or without a
notification. Main also owns the Dock badge (unseen + approvals waiting) and
bounce, and opens System Settings › Notifications on request. Every IPC
payload is validated in `main/ipc/notify.ts`.

macOS only posts notifications for a bundle it can validate. Electron's npm
`Electron.app` carries a linker-only signature with no resource seal, so on
macOS 15+ `usernotificationsd` drops every request ("addRequest not allowed:
com.github.Electron") while `Notification.show()` reports success. Two
scripts keep that from happening:

- `scripts/sign-dev-electron.mjs` (`postinstall` and `predev`) gives
  `node_modules/electron/dist/Electron.app` its own identity
  (`dev.agentos.desktop.dev`, named "AgentOS Dev"), ad-hoc signs it and
  registers it with LaunchServices. The identity matters: macOS routes a
  notification click by bundle identifier, and every stock Electron.app is
  `com.github.Electron`, so with the default one a click could launch some
  other copy (an `npx electron` cache, another project) and show Electron's
  welcome window instead of this app. In `npm run dev` the notifications
  appear as "AgentOS Dev", and macOS asks for permission once.
- `scripts/adhoc-sign.mjs` (electron-builder `afterSign`) ad-hoc signs the
  packaged `AgentOS.app` when no Developer ID identity signed it; a release
  build already carries a Developer ID signature, which the hook only
  verifies and leaves alone (see [Updates](#updates)).

If a notification still does not show, check `/usr/bin/log stream
--predicate 'process == "usernotificationsd"'` for the refusal, then System
Settings › Notifications (the app must be allowed) and Focus: an active
Focus mode delays banners and Notification Center logs "muted by DND
suppression".

The toolbar bell (`components/NotificationBell.tsx`) shows the unseen count, a
slash when muted or off, and a popover with the recent notifications (in
memory, `stores/notify-center.ts`), Do not disturb, the sound toggle and a
link to the settings section. Opening the popover or the session a
notification points at marks it seen. In a plain browser tab the fallback
uses the web Notification API and the chime; Dock and system sounds are inert.

### Pet

The same petdex mascots Hermes and Codex use (https://petdex.dev, a public
gallery of ~4,800 community pets). A pet is `pet.json` + `spritesheet.webp`,
a grid of 192×208 frames, one row per animation state, six frames stepped
over 1.1 s. `shared/pet.ts` owns the format facts (grid inference, row
taxonomy for 8/9/11-row sheets, Hermes' state priority: failed → jump →
wave → waiting → run → review → idle).

- `main/pets/store.ts` keeps pets in `userData/pets/<slug>/`, gallery
  previews in `userData/pets/.cache/`, and the manifest cached in memory
  (5 min) and on disk (offline). Downloads only from petdex hosts.
- `main/pets/protocol.ts` serves sheets as `agentos-pet://sheet/<slug>`
  (listed under `img-src` in the renderer CSP) so a 2 MB sheet never crosses
  IPC and the image cache does its job.
- `stores/pet.ts` derives the state from what the app already tracks: a turn
  streaming (`useLive`), approvals pending, the gateway in error, and
  `task.succeeded` / `task.failed` / `task.timeout` beats that hold ~3 s.
- `components/pet/PetOverlay.tsx` paints it: one `<button>` whose
  background-position steps across the row (petdex's own `steps()` CSS),
  draggable (the spot is remembered as a fraction of the window's free
  space, so a resized window carries the pet along and never strands it
  off screen; see `components/pet/logic.ts`), click to wave. Sheets are left-packed
  (a wave may have 4 frames, a jump 5), so the sheet is decoded once on a
  canvas to count each row's real frames (`rowFrameCounts`) and only those
  are stepped; the scheme is CORS-open for that pixel read. Frame sizes are
  rounded to whole pixels so the sprite does not shimmer.
- Settings > Appearance > Pet: toggle, searchable gallery (installed first,
  then the manifest a page at a time, thumbnails fetched on sight), size
  slider 10–300%, remove, and **Import folder** for a pet someone handed you
  (`pet.json` + its sheet, checked before a byte is copied).
- Built-in pets live in `resources/pets/<slug>/` and ship with the app
  (`extraResources` → `Contents/Resources/pets`). `PetStore.seedBundled`
  adopts each one once on launch, so the gallery has AgentOS' own mascot
  before anyone reaches petdex.dev — and with no network at all. The slugs
  it seeded are recorded in `userData/pets/.bundled.json`, so a built-in pet
  you remove stays removed.

## Touch ID

Settings › Security › **Confirm with Touch ID** puts a fingerprint in front
of the operator's money-moving clicks. `Off` (the default: a Mac without a
sensor must never be locked out) asks for nothing. `High-risk approvals` asks
before an approval the card stamps high-risk (the same `askRisk` the card
uses; a DCA mandate whose cap reaches the same line), and before **Export
private key** and **Remove wallet**. `Every approval` asks before every
approval as well, DCA mandates included.

The prompt is `systemPreferences.promptTouchID()` in main (`main/security.ts`,
behind `app.biometrics` / `app.authenticate`): biometrics only, never a
password fallback, and never attempted when `canPromptTouchID()` says no
(with the lid closed a raw prompt hangs). LocalAuthentication's error message
is mapped to `cancelled`, `unavailable` or `failed`. In the renderer one gate,
`lib/biometric-gate.ts`, reads the setting at call time and sits in front of
the RPC rather than in the buttons: inside `useOrderDecision` (every
`trading.orders.approve`: the desk's cards, the BOOK, the Trading tab),
`useMandateActions().approve` and the chat DCA card's `trading.dca.approve`,
and `useWalletMutation` for `wallet.export` / `wallet.remove` (before the
password leaves the renderer). A cancelled, unavailable or failed prompt
toasts and sends nothing; the card stays live. While the sheet is up the
button that asked reads "Touch ID…" and a second click is ignored. The setting
itself never needs Touch ID to change. Tool approvals are not gated: they are
file and command gates, not money.

## Quick Ask

A system-wide shortcut, **⌥ Space** by default, opens a small floating prompt
over whatever app is in front, the Spotlight posture. **Return** sends the text
to a **new chat**; **⌥ Return** sends it to the **current** one (the session the
main window is on, or the last one opened). Either way AgentOS comes forward on
that session with the reply streaming. **Escape** or clicking away closes the
prompt without sending, and keeps the text for next time; a send clears it.
Shift-Return adds a line. Empty text does nothing, and more than 20 kB is
refused in place.

Settings › Behaviour › Quick Ask turns it off (the key is released at once) or
moves it to ⌃ Space or ⌘⇧ Space: a fixed list, not free rebinding. When macOS
refuses the key because another app holds it, the pane says "Unavailable"
instead of failing silently, and the key is tried again on the next settings
write. Settings › Shortcuts lists the chosen key. One limit of
`globalShortcut`: a key macOS itself reserves (⌃ Space is the input-source
switch on many Macs) can register and still never arrive; the menu bar's
"Quick Ask…" row opens the same panel when a key does not.

How it is built:

- **Main** (`main/quick-ask/`). `hotkey.ts` keeps `globalShortcut` in step with
  the settings (`mirrorSettingsToOs` calls it at boot and on change; the key is
  released on `will-quit`). `panel.ts` owns the prompt's window: frameless,
  `type: 'panel'` so it floats over full-screen apps and takes the keyboard
  without making AgentOS the active app, `hud` vibrancy (opaque with Reduce
  transparency), centred on the display under the cursor, sized to its
  content, hidden on blur. It is created once while Quick Ask is on and only
  shown and hidden after that, so the key is instant; it is never shown before
  its view has mounted (`quickAsk:ready`). `controller.ts` ties them together.
- **Renderer** (`views/quick-ask/`). The panel loads the same bundle at
  `#/quick-ask`. `App` renders it without the router, `AppShell` or
  `GatewayProviders`: the panel never connects to the gateway. It reads the
  theme and appearance over `window.agentos` again each time it is shown, so
  it matches the main window.
- **Hand-off.** The text crosses IPC only. The panel calls `quickAsk:submit`
  (validated in main: a string, not blank, at most 20 kB, target `new` or
  `current`); main hides the panel, restores and focuses the main window (or
  creates one when it was closed), puts the submission in an inbox and pings
  the window with `quickAsk:deliver`. The window collects with `quickAsk:take`,
  on the ping and once when it mounts, so a window that was just created or is
  reloading cannot miss one and none is delivered twice. In the renderer,
  `stores/quick-ask.ts` holds the queue; the shell (`useQuickAskRouting`)
  closes any sheet and navigates to the destination, and the chat there
  (`useQuickAskSend` in `ChatView`) **sends** it, unlike Skills' "Use in chat",
  which only fills the composer. It waits for the transcript to settle
  (`data-history-ready`) so the first draw of the session's history cannot wipe
  the new message, with a 4-second fallback. While a reply streams it queues
  behind it; the user's draft and attachments in the composer are left alone.
  With the gateway down the chat says the text is waiting and sends it once
  the gateway is back.

## First run: the app installs the engine

The DMG is the whole install. On launch, main runs _discovery_
(`main/bootstrap/discovery.ts`): `gateway.cliPath` from settings wins as-is;
otherwise the CLI found on PATH or in the usual dirs is smoke-tested with
`agentos --version` and compared with the app's version. Same or newer → the
gateway starts. Missing, older, or not starting → if a gateway already
answers on the configured endpoint it is adopted (updating stays a
Settings › About action); otherwise the setup overlay
(`views/setup/SetupOverlay.tsx`) offers **Install** (or **Update**), or
"Connect to an existing gateway instead" (external mode).

Installing means driving the repo's `install.sh`, bundled at
`Contents/Resources/install.sh` (`extraResources`), over its stage protocol
(`main/bootstrap/runner.ts`, a port of the Hermes Agent bootstrap runner):
`--manifest` for the stage list, then one process per
`--stage NAME --json --non-interactive`, the last JSON line of stdout being
the result frame. Stages: prerequisites, uv, python, package (the
version-pinned wheel, `--force`), path, complete. Output streams to the
overlay (stderr muted, not red: uv writes progress there) and to
`~/.agentos/logs/bootstrap-<timestamp>.log`; Cancel kills the running
stage's process group. The failure screen opens the output, and offers
Retry, Copy output, Show log in Finder and the terminal one-liner. After
success the gateway starts and step 2 (`views/setup/ProviderStep.tsx`)
takes over: a grid of the full catalog, OpenCAP first with a **Recommended**
tag (`RECOMMENDED_PROVIDER` in `views/settings/logic.ts`); picking one opens
its own screen with the Settings pane's `ProviderForm`; saving restarts the
managed gateway itself (the step's state lives in `stores/bootstrap.ts` so the
reconnect cannot bounce it back to the grid) and ends on "You're all set",
where `providers.probe` tries the saved key against the provider for real (its
model list, then a 1-token turn): a rejected key shows the error with
**Edit key** / **Continue anyway**. The form itself has **Test key** (same
RPC with the key as typed), fills Default model from the provider's list, and
links to the provider's key page (`views/settings/provider-links.ts`). The stage carries a
three-step rail (Install → Provider → Ready) across the top. `Skip for now`
hands over to Home, which shows a "Choose a provider" card until one is
configured. Settings › Advanced has **Reinstall engine** and
**Remove engine** (`uv tool uninstall use-agent-os`; `~/.agentos` stays).

Design in a browser tab with nothing installed: `npm run dev`, then open the
renderer URL with `?fake=install`, `?fake=update` or `?fake=failure`
(`stores/bootstrap.ts`, development builds only).

The managed gateway's stdout/stderr are appended to
`~/Library/Logs/AgentOS/gateway.log` (a header per spawn), the file Settings ›
Advanced › App logs opens. Session names the gateway seeds before the titler
runs (`WebChat`, `Chat`, …; `lib/session-name.ts` mirrors the gateway's
placeholder list) render as "New session".

## Updates

Two things go out of date, and Settings › About updates both:

- **Engine** — the `use-agent-os` package the app runs as `agentos gateway
run`. `main/updates/engine-updater.ts` runs the installed CLI's own
  `agentos upgrade --check --json` and `agentos upgrade --json --no-restart`
  (streaming its output into the pane), then restarts the gateway _it_
  spawned through the supervisor; `--no-restart` keeps the CLI's restart out
  of the supervisor's way. The CLI snapshots config and the state databases
  first. The renderer then confirms the gateway reports the new version
  (`status` RPC) and runs `updates.verifyData`. A gateway the app merely
  adopted is left running with a note; external mode refuses. While the
  installer runs a marker (`~/.agentos/state/desktop/engine-update.json`,
  pid + start time) exists so a relaunch after a crash reports the
  interrupted update instead of trusting the last "done". Exit 3 from the CLI
  (pip / editable install) surfaces the manual command verbatim.
- **App** — this shell, through `electron-updater`
  (`main/updates/app-updater.ts`). Two explicit steps: nothing downloads
  without a click and nothing installs without a second one; the swap
  happens on the relaunch that click triggers (`quitAndInstall`), after the
  managed gateway is stopped. `autoInstallOnAppQuit` is off, so a plain ⌘Q
  keeps the running build. Before the relaunch an install gate (`index.ts`)
  refuses while the engine updater or the first-run installer is mid-write;
  About and the toast say why (`AppUpdateState.blocked`).

One release tag covers both, so the shell shows **one notice** for the
release rather than one per part. `main/updates/auto-check.ts` runs a
_silent_ check of both updaters — 15 s after launch, when a window regains
focus (at most once a minute) and every 5 minutes: the app through
electron-updater, the engine through `agentos upgrade --check --json`
(`EngineUpdater.check({ silent: true })` shows no phase and reports no
failure). `releaseUpdate()` in `shared/updates.ts` folds the two states into
the single next step, and two surfaces read it:

- `components/UpdateNotices.tsx` fires one sonner toast per step under one
  id: "AgentOS X is available" with **Update** (engine install, then app
  download, i.e. `updateAll`), "ready to install" with **Restart**, and
  "Engine X is installed / the gateway is still running Y" with **Restart
  gateway** after a terminal ran `agentos upgrade`. An Update or gateway
  restart that would cut a live session opens Settings › About instead,
  where the warning and "Update anyway" live. A finished engine-only update
  says so once and goes away.
- `components/UpdatePill.tsx` is the standing toolbar pill: "Update",
  "Updating engine…", "Downloading 37%", "Restart", "Restart gateway",
  "Update failed". It stays until the release is fully applied and opens
  Settings › About. A failed download or restart keeps the toast up with
  **Try again** (check, then download; the cached zip makes it quick) and,
  when Squirrel refused the relaunch ("The command is disabled"), tells the
  person to quit and reopen the app first: Squirrel does not accept a second
  relaunch in the same process.

A miss or a failure shows nothing, and a late result never rewinds a
download or install the user started meanwhile.
`electron-builder.yml` publishes to the GitHub release of the same
`v<CalVer>` tag as the Python wheel, so `package.json`'s version must equal
`pyproject.toml`'s: `tests/test_release_consistency.py` asserts it and the
`pump-version` skill bumps both. A dev build or an unpublished local
package reports `unsupported`.

`shared/updates.ts` also carries `MIN_GATEWAY_VERSION`: the oldest engine
this renderer speaks to. Bump it whenever the desktop starts depending on a
gateway RPC or field the previous release lacks; About warns when the
connected gateway is older.

Release builds are signed and notarized by
`.github/workflows/desktop-release.yml` on every `v*` tag pushed to the
repository that holds this directory, or by hand from the Actions tab ("Run
workflow": the tag to publish to, optionally the git ref to build from and
the `owner/name` whose release receives the assets; the default is
`use-agent-os/agent-os`, where the Python releases live). It needs these
repository secrets: `MAC_CSC_LINK` (base64 `.p12` of the "Developer ID
Application" certificate), `MAC_CSC_KEY_PASSWORD`, `APPLE_ID`,
`APPLE_APP_SPECIFIC_PASSWORD`, `APPLE_TEAM_ID`, plus `DESKTOP_RELEASE_TOKEN`
(a fine-grained PAT with _Contents: write_ on the target) whenever the target
is another repository. The publish target is also written into the bundled
`app-update.yml`, so a build looks for its updates exactly where it was
uploaded. The run refuses to publish until the bundle passes `codesign
--verify`, `stapler validate` and `spctl --assess`, and `latest-mac.yml`
lists both the arm64 and x64 zips; without the signing secrets it builds an
ad-hoc signed app and keeps it as a workflow artifact only.

The packaged app is versioned by a **semver twin** of the CalVer, because
electron-builder and electron-updater only speak semver and `2026.9.22.post1`
is not one (left alone it becomes `2026.9.2-2.post1`, which sorts _before_
2026.9.2, so a `.post` release would never be offered). `calverToSemver` in
`shared/updates.ts` folds month and day into the minor number and keeps the
post number as the patch: `2026.9.22` → `2026.922.0`, `2026.9.22.post1` →
`2026.922.1`, `2026.10.1` → `2026.1001.0`. `scripts/release-version.mjs`
prints it for electron-builder's `extraMetadata.version`, with the CalVer
riding along as `extraMetadata.calver`; artifacts, `latest-mac.yml` and
`CFBundleShortVersionString` carry the twin, while `main/app-version.ts`
(`appCalver()`) reads the CalVer back for About, the menu, the engine
installer and the updater's own display. `package.json` keeps the plain
CalVer, which the release-consistency test pins to `pyproject.toml`. To
export the certificate for `MAC_CSC_LINK`:

```sh
security export -t identities -f pkcs12 -k ~/Library/Keychains/login.keychain-db \
  -P "<p12 password>" -o /tmp/developer-id.p12   # prompts for keychain access
base64 -i /tmp/developer-id.p12 | gh secret set MAC_CSC_LINK -R <owner/name>
gh secret set MAC_CSC_KEY_PASSWORD -R <owner/name> --body "<p12 password>"
rm /tmp/developer-id.p12
```

Locally, `npm run
package:mac` signs with the Developer ID identity in the keychain when there
is one (set the same `APPLE_*` variables to notarize) and falls back to an
ad-hoc signature otherwise. Hardened Runtime is on with
`resources/entitlements.mac.plist` (V8 JIT + unsigned executable memory,
which Electron needs; nothing wider).

## Commands

The console's dependencies are separate; `frontend/` has its own
`npm ci`. This app only needs its own.

```sh
npm ci                      # Node >= 22
npm run dev                 # electron-vite dev with HMR
npm run check               # tsc (node + web), eslint, prettier, vitest
npm run build               # out/{main,preload,renderer}
npm run package:dir         # unpacked .app in release/ (Developer ID if present, else ad-hoc)
npm run package:mac         # dmg + zip + latest-mac.yml (signed + notarized when credentials are set)
```

If `npm ci` did not download the Electron binary (sandboxed installs skip
postinstall), run `node node_modules/electron/install.js` once; `npm run dev`
then signs it on its `predev` step (see [Notifications](#notifications)).

## Conventions

- Renderer never imports `electron` or Node modules (eslint enforces it).
  Everything crosses `window.agentos`, typed by `shared/ipc.ts`.
- `lib/desktop-api.ts` provides a localStorage-backed fallback so the renderer
  also runs in a plain browser tab and in vitest.
- Settings live in `~/Library/Application Support/AgentOS/settings.json`
  (Electron `userData`), validated by `shared/settings.ts` on every read.
- Desktop-only copy goes through `~/i18n`'s `t()`; copy that belongs to the
  shared chat surface stays in the console's catalog (`@/i18n`).
- Preload is emitted as CommonJS (`out/preload/index.cjs`) because the window
  runs sandboxed.
- The main process rewrites the `Origin` header on loopback requests: the
  gateway's WebSocket guard rejects `file://`, and the renderer is the local
  operator, the same trust the browser console gets. The gateway reflects
  that Origin in `Access-Control-Allow-Origin`, which Chromium then compares
  with the renderer's real origin (`null` from disk), so the same hook
  translates the answer back on the way in (`main/loopback-cors.ts`); without
  it every `fetch` to the gateway — bootstrap, approvals, uploads, artifact
  downloads — fails as a CORS error while the WebSocket works.
- The gateway is started (or adopted, if one is already running) when the app
  launches, and stopped on quit; the chat and jobs views wait for `running`
  before they connect.
- Scheduled jobs open as a panel over whatever is on screen (the `jobsOpen`
  flag in `stores/ui.ts`, toggled from the sidebar), so checking a schedule
  never leaves the conversation. The panel reuses the console's cron model end to end (`@/views/cron/logic`:
  `seedForm`, `buildSavePayload`, the cron parser and humanizer) and the same
  `cron.*` RPCs, so a job created here reads identically in the web console.
  Only the presentation is the desktop's: health buckets, the natural-language
  schedule builder (`views/jobs/logic.ts`), native time/date pickers, and the
  session picker fed by the sidebar's session list.
- Projects are folders in the sidebar (Notes posture), not a page of their
  own: each project is a disclosure row with its chats inside, "+" opens an
  inline name row (Return creates, Escape discards), and a session dragged
  onto a folder is filed there (onto the "Sessions" header, unfiled). A
  folder's page (`views/projects/ProjectView.tsx`) has a title you click to
  rename and a brief that saves itself after a pause, on blur, on ⌘S and on
  leaving the page, with the gateway's compare-and-swap (`expectedUpdatedAt`)
  behind every write. The chat header shows a project chip whose menu moves
  the session between folders. All of it is the console's project model and
  `projects.*` / `sessions.patch` RPCs (`@/views/projects/logic`); the desktop
  owns only filing, disclosure state, and the autosave (`views/projects/logic.ts`).
