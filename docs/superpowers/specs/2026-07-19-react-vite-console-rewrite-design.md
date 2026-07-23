# AgentOS Control UI — React 19 + Vite 6 Rewrite (Design)

Date: 2026-07-19
Status: Approved design, pending implementation plan
Branch: `worktree-fe-core-swap` (to be rebranded `feat/...` at PR time)

> **Current implementation note (2026-07-23).** The original migration kept
> the 13 legacy route contracts described below. The current console also has a
> canonical `/settings` **Agent setup** workspace: `/setup` opens its Guided
> mode and `/config` opens Advanced mode for compatibility. The sidebar exposes
> one Agent setup item. A read-only `config.snapshot` RPC supplies a coherent,
> redacted setup/config snapshot; existing specialized onboarding mutations and
> `config.patch` / `config.apply` remain the write contracts. Those writes now
> share a persist-first transaction boundary, optional revision/CAS, fail-closed
> disk-divergence checks, readonly runtime-owned paths, and conservative restart
> metadata.

## 1. Goal

Replace the current vanilla-JS control console (~21,200 lines JS, ~13,500
lines CSS, 13 views, no build step) with a full rewrite on:

- **React 19 + Vite 6**, **TypeScript strict**
- **TanStack Query** (server state over WS-RPC) + **Zustand** (client state)
- **shadcn/ui + Tailwind v4** as the UI foundation (code-owned components)
- **ESLint + Prettier + tsc + Vitest/RTL** quality gate, wired into CI and
  AGENTS.md

Strategy: **single-branch full rewrite with one cutover**. No period of the
two frontends coexisting on `main`. Playwright E2E is an optional follow-up,
not part of this rewrite.

This is a technical rewrite that also adopts a component library: each view
keeps the *behavior and information structure* of its legacy counterpart
(legacy `static/js/views/*.js` files are the behavioral spec) but is rebuilt
on shadcn components — not a pixel-faithful port.

## 2. Current state (what is being replaced)

- `src/agentos/gateway/static/` — `js/` (app.js, router.js, rpc.js,
  components.js, theme.js, markdown.js, icons.js, approval_monitor.js +
  13 `views/*.js`; `chat.js` alone is 8,841 lines), `css/` (base,
  components, mobile, prism + per-view css), `vendor/` (marked, purify,
  prism), `fonts/`, `img/`.
- Served by `src/agentos/gateway/control_ui.py`: Starlette mount at
  `{base_path}/static` (`_CachedStaticFiles`, 30-day Cache-Control,
  `?v=` cache-busting), Jinja2 `templates/index.html` with SPA fallback,
  bootstrap data injected as data-attributes on `#agentos-data`.
- Backend interface: WebSocket-RPC at `/ws` (plus `/api/*` REST). The baseline
  rewrite was a different consumer of the same protocol. The later Agent setup
  consolidation adds the read-only `config.snapshot` composite and hardens the
  existing mutation contracts with shared transactional/CAS semantics; it does
  not introduce a broader generic write surface.

## 3. Repository layout & packaging

```
frontend/                          # React source — NOT packaged in the wheel
  index.html                       # Vite entry (carries theme flash-prevention inline script)
  package.json, vite.config.ts, tsconfig.json,
  eslint/prettier/vitest configs, tailwind config (v4, CSS-first)
  src/
    main.tsx                       # entry; mounts app
    app/                           # AppShell, React Router routes, providers
    lib/                           # WsRpcClient, bootstrap, markdown, icons, utils
    components/                    # shared UI incl. shadcn/ui components (code-owned)
    views/                         # legacy behavior modules plus additive surfaces:
                                   # overview, health, chat, sessions, agents, cron,
                                   # usage, config, setup, settings, channels, MCP,
                                   # approvals, skills, logs
    styles/                        # Tailwind entry + design tokens (CSS variables)

src/agentos/gateway/static/dist/   # Vite build output — packaged & served
                                   # (legacy static/fonts/ and static/img/ move into
                                   # frontend/ as source assets and are emitted into dist/)
```

Packaging rules:

- `pyproject.toml` uses `packages = ["src/agentos"]`, so everything under
  `static/dist/` enters the wheel automatically — no force-include needed.
- `frontend/` sits outside `src/agentos/`, so React source never enters the
  wheel.
- **`dist/` is gitignored.** The release pipeline runs
  `npm ci && npm run build` (in `frontend/`) *before* `hatch build`, so
  published wheels always contain a fresh `dist/`. Contributors running from
  source either run `npm run build` once or use the Vite dev server.
  A guard in the release workflow fails the build if `dist/` is missing or
  empty. This is the Jupyter/Streamlit/Gradio pattern.

## 4. Build/serve integration

- **Dev:** `vite dev` with HMR; proxies `/ws` and `/api` (and
  `{base_path}/api`) to a running gateway. Python-only contributors never
  need Node.
- **Prod:** `control_ui.py` serves `static/dist/` — Vite-generated
  `index.html` as the SPA fallback response, hashed assets under
  `{base_path}/static/`. Keep: base path `/control`, SPA fallback, cache
  headers (Vite content-hashing makes the `?v=` scheme unnecessary for
  hashed assets; keep no-cache on `index.html` itself).
- **Bootstrap data:** replace Jinja data-attr injection with
  `GET {base_path}/api/bootstrap` returning `{version, ws_url, auth_mode,
  base_path, ...}` as JSON; the app fetches it at startup before opening the
  WS. Jinja templating of `index.html` is removed. (`ws_url` derivation
  logic in `_request_ws_url` moves to/behind this endpoint unchanged.)
- Theme flash prevention: the inline `data-theme` script currently in the
  Jinja template is preserved verbatim in Vite's `index.html`.

## 5. Rewrite organization (layers, in order; each green before the next)

- **Layer 0 — Foundation:** scaffold `frontend/` toolchain; Tailwind v4 +
  shadcn/ui base components (Button, Card, Table, Dialog, Form, Select,
  Toast, …); design tokens (colors, spacing, Inter / JetBrains Mono
  self-hosted fonts) extracted from legacy `base.css` into CSS variables;
  typed `WsRpcClient` port of `rpc.js`; markdown pipeline (marked +
  DOMPurify + Prism as npm deps, replacing `vendor/`); AppShell
  (sidebar/nav/topbar/connection state) + React Router with all 13 routes
  stubbed; approval-monitor equivalent.
- **Layer 1 — 12 standard views**, smallest first: health → overview →
  logs → approvals → channels → agents → sessions → usage → config →
  skills → cron → setup. Each = components + TanStack Query hooks + RTL
  tests. Read the legacy view fully before rewriting it.
- **Layer 2 — Chat (8.8k lines), decomposed before writing:** message list
  + streaming rendering, tool-activity display, composer, session
  lifecycle, inline approvals, artifacts/publish, compact controls. Each
  module its own component/hook with tests. Highest-risk layer; the
  implementation plan must split it into many small reviewed tasks.
- **Layer 3 — Cutover & removal:** point `control_ui.py` at `dist/`;
  delete `static/js/`, `static/css/`, `static/vendor/`, Jinja template;
  update docs and notices; wire release pipeline.

Post-rewrite information architecture composes the Layer-1 Config and Setup
modules inside `views/settings/SettingsPage.tsx`. Both surfaces stay mounted so
switching Guided/Advanced does not discard drafts. The legacy URLs remain
registered as compatibility entry points rather than duplicate navigation.

### Agent setup data and write contract

- `SettingsPage` owns one `config.snapshot` query and passes that exact object
  to Guided and Advanced. `/settings` is canonical; `/setup` and `/config` only
  choose the initial mode. Local tab switching does not churn routes or remount
  either editor.
- The snapshot is read-scoped and redacted. It includes catalog, status,
  readiness, active public config, config target, revision, pending-restart
  metadata, and `diskDiverged` / `writeBlocked`. Compatibility reads are used
  only when the server returns `METHOD_NOT_FOUND`; malformed or failed snapshot
  calls remain visible errors.
- Guided tracks a base revision per capability card; Advanced tracks one base
  revision per Form/YAML draft. A changed upstream revision preserves the local
  draft for review but disables Save until explicit discard/reload. Successful
  Guided saves clear only their committed card, including one-time secrets.
- All Config and onboarding writers validate a clone, verify CAS/disk state,
  atomically persist, then update the running object and hot-apply supported
  adapters. Persistence failure cannot mutate the live runtime. A post-persist
  hot-apply failure records a pending restart reason.
- Semantic divergence between the running config and its file is fail-closed:
  the snapshot returns a null revision with `diskDiverged` and `writeBlocked`,
  the workspace shows one global **Out of sync** warning, and both editors
  disable writes until the gateway reloads/restarts from that file.
- `host`, `port`, `config_path`, `auth.token`, and `auth.password` are readonly
  on the WebSocket config mutation surface. Restart metadata errs on the safe
  side for settings captured by boot-created services and failed hot applies.

## 6. Migration protocol — nothing left behind

The rewrite follows a strict inventory → implement → verify-parity loop.
No behavior is dropped silently: everything is either ported, or recorded
as an explicit waiver with a reason.

### 6.1 Global behavior inventory (before Layer 1 starts)

Produce a **parity matrix** — one committed file
(`docs/superpowers/specs/2026-07-19-console-rewrite-parity-matrix.md`)
that is the single source of truth for migration completeness. Built by
reading every legacy file plus mechanical extraction (grep/scripts), it
enumerates per legacy module:

- **RPC surface:** every RPC method called (extracted mechanically from
  `rpc.call(`/method-name usage), with params/response shape notes.
- **Server-push events** subscribed, and what each invalidates/updates.
- **Routes & URL state:** all 13 routes, query params, deep-link behavior.
- **Browser state:** every localStorage/sessionStorage key
  (`agentos.wsUrl`, `agentos.wsToken`, `agentos-theme`, + any found),
  clipboard use, file upload/download, drag-drop, keyboard shortcuts.
- **UI behaviors per view:** every user-visible behavior, loading/empty/
  error states, polling/refresh cadence, optimistic updates, confirmation
  dialogs, toasts.
- **Cross-cutting features:** theme + flash prevention, mobile/responsive
  behaviors (`mobile.css`), markdown rendering + sanitization rules
  (DOMPurify config!), Prism highlighting languages, approval-monitor
  background behavior, connection-loss UX, auth modes, feature flags
  (`AGENTOS_FEATURES.tokenViz`), favicon/meta/noscript.

Each matrix row: `behavior | legacy source (file:line) | status
(pending / ported / waived) | evidence (test name or verification note)`.

### 6.2 Per-view protocol (repeated for all 13 views)

1. **Inventory:** read the legacy view fully; fill its matrix rows before
   writing any React code. The legacy file is the behavioral spec.
2. **Implement** against the matrix rows (tests written per TDD where
   logic warrants it).
3. **Parity review:** a reviewer (subagent) compares legacy source vs new
   implementation row by row; every row gets `ported` + evidence, or
   `waived` + reason. A view is **done only when its matrix section has
   zero `pending` rows.**

### 6.3 Mechanical completeness checks (scripted, run at cutover)

- **RPC coverage diff:** script extracts the set of RPC methods used by
  legacy JS and the set used by the new TS client; the diff must be empty
  or every difference waived in the matrix.
- **Route diff:** all legacy `Router.register` routes exist in the new
  router.
- **Storage-key diff:** all legacy storage keys are consumed or retired
  with a waiver.
- **Asset diff:** every file under legacy `fonts/`, `img/` is present in
  the new build output or waived.

### 6.4 Cutover gate (all must hold before deleting legacy)

1. Parity matrix: zero `pending` rows across all views and cross-cutting
   sections; all waivers reviewed by the repo owner.
2. Mechanical diffs (6.3) clean.
3. FE quality gate green; Python gateway tests green (2,358 baseline).
4. Manual smoke pass of all 13 views against a live gateway (checklist in
   the matrix), both themes, desktop + narrow viewport.
5. Docs, AGENTS.md FE lane, THIRD_PARTY_NOTICES updated.

Only then does Layer 3 delete `static/js`, `static/css`, `static/vendor`
and the Jinja template — in the same PR that flips serving to `dist/`.

## 7. Data flow & error handling

- **WsRpcClient** (singleton, typed): auto-reconnect with backoff,
  request/response correlation, server-push event subscription, auth token
  handling per bootstrap `auth_mode` (localStorage keys preserved:
  `agentos.wsUrl`, `agentos.wsToken`, `agentos-theme`).
- **TanStack Query** wraps RPC calls (`queryKey` = method + params).
  Server-push events map to `queryClient.invalidateQueries` — the direct
  analogue of the legacy event-driven invalidation pattern.
- **Zustand** stores: connection state, theme, sidebar, approval badge.
- **Chat streaming** bypasses the Query cache: event stream → local
  reducer state.
- **Errors:** WS disconnect → status banner, queries pause, refetch on
  reconnect (same UX as legacy `_bindConnectionState`). RPC errors → toast
  + per-view inline error states.

## 8. Testing & quality gate

- **Vitest + React Testing Library** per view/component; `WsRpcClient`
  tested against a mock WebSocket.
- **Contract fixtures:** RPC payload fixtures derived from the Python
  gateway tests (`tests/test_gateway` RPC shapes) so FE and BE cannot
  silently diverge.
- **FE gate:** `tsc --noEmit && eslint && prettier --check && vitest run`
  — added to CI and documented as a new FE lane in AGENTS.md (Node
  version pinned; Python-only changes don't require it).
- **Python gate:** the existing gateway suite remains mandatory. The later
  `config.snapshot` composite, persist-first commit boundary, revision-aware
  writes, disk-divergence blocking, readonly paths, and restart metadata add
  focused gateway regression tests; the original baseline count is historical,
  not a target.

## 9. Open-source obligations

- **THIRD_PARTY_NOTICES.md:** all npm dependencies bundled into `dist/`
  (React, TanStack Query, Zustand, Radix/shadcn, Tailwind runtime output,
  marked, DOMPurify, Prism, …) must be listed; generate entries via a
  license-checker script rather than by hand; the existing notices test
  must cover them. Legacy `vendor/` entries are updated/removed
  accordingly.
- **AGENTS.md / docs:** `docs/web-ui.md` updated for the new serve layout
  and dev workflow; per repo rule, any CLI/config surface change also
  updates `src/agentos/skills/bundled/agentos/SKILL.md` + `docs/cli.md` in
  the same change (none anticipated, but the rule binds if one appears).

## 10. Out of scope

- Playwright E2E (optional follow-up).
- Backend feature expansion beyond the additive Agent setup read composite and
  write-hardening contract documented above.
- Visual redesign beyond what adopting shadcn/ui implies; feature additions.
- `tokenViz` widget stays behind its feature flag: the flag and the legacy
  behavior are ported (widget off by default), not redesigned.
