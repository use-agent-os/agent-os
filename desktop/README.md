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
renderer, and the session/chat pure logic. One protocol implementation, one
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
    │   └── gateway.ts        #   GatewayStatus
    ├── main/                 # Electron main process (Node)
    │   ├── index.ts          #   lifecycle, single instance, loopback Origin, quit hook
    │   ├── window.ts         #   BrowserWindow (vibrancy, hiddenInset, sandbox)
    │   ├── menu.ts           #   macOS menu bar
    │   ├── ipc/              #   one file per IPC domain, registered in index.ts
    │   ├── settings/store.ts #   atomic JSON settings in userData/
    │   ├── gateway/          #   cli-locator + process supervisor (spawn, adopt, health)
    │   ├── bootstrap/        #   first-run engine install: discovery, install.sh runner, controller
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
            ├── views/projects/ # Project page (`/projects/:id`): renamable title,
            │                 #   self-saving brief, the chats filed there
            ├── views/settings/ # Settings sheet: SettingsPanel (rail + section), one pane
            │                 #   per section (providers, router, gateway, appearance,
            │                 #   behaviour, shortcuts, advanced, about), parts.tsx, logic.ts
            ├── components/   #   Sidebar (+ resizer, project folders, session list with
            │                 #   its row menu and view menu), Toolbar, menu/ (PopMenu,
            │                 #   Menu, items, submenus), pet/, composer/
            ├── theme/        #   theme system (see below)
            ├── stores/       #   zustand: gateway, sessions, projects, live, settings, ui,
            │                 #   session-marks (pin/archive/unread), session-view
            ├── lib/          #   desktop-api bridge, motion curves, relative time
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
`settings:update`; `shared/settings.ts` owns that schema. The two agent
sections edit the **gateway's** configuration instead, through the same
guided RPCs the web console's setup uses, with the `config.snapshot`
revision on every write so a stale form cannot overwrite a newer file.

| Section       | What it holds                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| ------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Models        | Provider (catalog from `onboarding.catalog`), API key / env key / base URL / proxy, default model from `models.list`; saved via `onboarding.provider.configure`. Thinking level via `config.set`.                                                                                                                                                                                                                                                                                                                                  |
| Pilot Router  | Mode (Pilot / LLM judge / Off), default tier, safety net, judge model, translation cap, and the tier ladder c0–c3 + vision with a model and thinking level per rung; saved via `onboarding.router.configure` using the console's `buildRouterConfigureParams`.                                                                                                                                                                                                                                                                     |
| Gateway       | Live status with Start/Stop/Restart, endpoint copy + open console; an editable draft of mode/host/port/token/CLI path with validation, Save/Revert, and a "restart to apply" notice when the running endpoint differs.                                                                                                                                                                                                                                                                                                             |
| Appearance    | Theme + palette (`ThemeRows`), text size (`data-text-size` on `<html>`), reduce transparency (`data-transparency` + `win.setVibrancy`).                                                                                                                                                                                                                                                                                                                                                                                            |
| Notifications | Master switch; what happens while the window is in front (nothing / in-app banner / system notification); Do not disturb (30 min, 1 h, 3 h, until tomorrow 9:00); show details; per-event switches (reply finished with a minimum length, reply failed, approval needed, scheduled job runs off/failures/all, gateway stopped on its own); sound on/off and which (the app chime or a macOS alert sound); Dock badge and bounce; a test button and a door to System Settings › Notifications. See [Notifications](#notifications). |
| Behaviour     | Open at login (mirrored to `app.setLoginItemSettings`), open at launch (home / last session), stop the gateway on quit, Return vs ⌘Return to send, sidebar width reset.                                                                                                                                                                                                                                                                                                                                                            |
| Shortcuts     | The keys the app binds (⌘, ⌘N ⌘⇧S ⌘⇧O …); static, nothing is rebindable.                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| Advanced      | Paths (settings file, logs, gateway `config.toml`) with Finder/open actions, copy diagnostics (token redacted), reset all app settings behind an alertdialog.                                                                                                                                                                                                                                                                                                                                                                      |
| About         | App/Electron/Chromium versions, gateway version + uptime, `updates.check`, links.                                                                                                                                                                                                                                                                                                                                                                                                                                                  |

Main mirrors three settings onto the window/OS on every write
(`mirrorSettingsToOs` in `main/index.ts`): the login item, window vibrancy and
the zoom factor. `nativeTheme` follows the theme section the same way, so a
reset repaints correctly.

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

- `scripts/sign-dev-electron.mjs` (`postinstall` and `predev`) ad-hoc signs
  `node_modules/electron/dist/Electron.app` when its signature does not
  verify and registers it with LaunchServices. In `npm run dev` the
  notifications appear as "Electron", and macOS asks for permission once.
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
  slider 10–300%, remove.

## First run: the app installs the engine

The DMG is the whole install. On launch, main runs *discovery*
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
  (streaming its output into the pane), then restarts the gateway *it*
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
  (`main/updates/app-updater.ts`). Downloads are explicit; the swap happens
  on relaunch (`quitAndInstall`), after the managed gateway is stopped.
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
`.github/workflows/desktop-release.yml` on every `v*` tag. It needs these
repository secrets: `MAC_CSC_LINK` (base64 `.p12` of the "Developer ID
Application" certificate), `MAC_CSC_KEY_PASSWORD`, `APPLE_ID`,
`APPLE_APP_SPECIFIC_PASSWORD`, `APPLE_TEAM_ID`. Locally, `npm run
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
  operator, the same trust the browser console gets.
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
