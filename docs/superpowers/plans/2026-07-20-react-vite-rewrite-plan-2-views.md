# React+Vite Console Rewrite — Plan 2: 11 Standard Views Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate the 11 remaining standard views (approvals, logs, overview, channels, agents, sessions, usage, config, skills, cron, setup) plus the approval-monitor background service to the React console, each behavior-faithful to its legacy file and styled with the established terminal design system.

**Architecture:** Every view follows the proven Health-view pattern (Plan 1 Task 8): read the legacy file as the behavioral spec → fill parity-matrix rows → `logic.ts` pure helpers (TDD) → `<Page>` component (RTL-tested with mocked rpc) → swap the route stub → matrix evidence. The shared design system (tokens, `.panel`, `.tone-*` gutters, `.t-display`/`.t-label`, `AsciiField`, `CommandLine`, `.view-container`) is mandatory — views never invent their own containers, status colors, or copy logic.

**Tech Stack:** unchanged from Plan 1 (React 19, Vite 6, TS strict, TanStack Query v5, Zustand v5, shadcn/ui, Vitest 3 + RTL, lucide icons).

**Spec:** `docs/superpowers/specs/2026-07-19-react-vite-console-rewrite-design.md` (§6 protocol). Parity matrix: `docs/superpowers/specs/2026-07-19-console-rewrite-parity-matrix.md`.

## Global Constraints

- Everything from Plan 1's Global Constraints still binds (TS strict/no-any, dist gitignored, legacy untouched, no RPC/backend changes, conventional commits, NO AI attribution trailers, matrix updated in the same commit).
- **Design-system law (from the Plan-1 polish rounds):**
  - Views never set their own outer max-width/padding — `.view-container` in AppShell owns it.
  - Status colors ONLY via the `--tone` primitive (`.tone-*` classes or view state-class → `--tone` mapping). One gutter per nesting level — never two colored bars side by side.
  - Every view header: `.t-label` eyebrow (`Control · <Title>`) + `.t-display` title + `<AsciiField />` backdrop, exactly like Health/StubView.
  - Sections are `.panel` boxes (head = mono kicker + semantic dot) or view-specific equivalents built from the same tokens.
  - CLI commands ALWAYS render via the common `<CommandLine command=... toastIdPrefix="<view>-copy" />`.
  - Key-value readouts follow the Health `.health-evidence` posture (aligned KV table, hairline rows); if a second view needs it, extract it into a common `.kv-readout` class in `globals.css` in that task and refactor Health to use it.
  - Lime `--primary` is SIGNAL only (active nav, focus, primary CTA, `$` prompt, title period) — never a status color.
- View titles stay Title-case in the DOM (accessible name / tests); lowercase + period comes from `.t-display` CSS only.
- FE gate `npm run check` clean after every task; Python suite untouched (no Python edits expected in this plan).
- Legacy per-view CSS (`static/css/views/*.css`) is reference for information structure, NOT ported wholesale — views are rebuilt on the token system.

## The View Migration Protocol (applies to every view task below)

Each view task executes these steps — this is the complete procedure, proven on Health:

1. **Inventory (pre-step, mandatory):** read the legacy view file END-TO-END. Fill the matrix's `### <view>` section: one row per behavior (RPC calls with params, event subscriptions, states loading/empty/error, mutations + optimistic updates, confirmation dialogs, polling cadences, localStorage keys, keyboard/clipboard interactions, badges/chips) with exact `file:line` refs, all `pending`.
2. **logic.ts (TDD):** extract every pure helper from the legacy file 1:1 (formatting, mapping, validation, sorting, derivation). Write the failing unit tests FIRST from the legacy semantics, then port. Signatures typed, no `any`.
3. **Page component (TDD):** RTL test file first (mock `useRpc`/`useBootstrap` exactly like `HealthPage.test.tsx`), covering: happy render with representative payload, error state, user actions (clicks → RPC calls asserted), event-driven invalidation where applicable. Then implement: `useQuery` per read (queryKey = method+params, `waitForConnection()` in queryFn), `useMutation` + `queryClient.invalidateQueries` for writes, rpc `.on(event)` subscriptions → invalidate (in `useEffect` with unsubscribe cleanup). Compose UI from the design system.
4. **Route swap:** replace the view's StubView entry in `routes.tsx`.
5. **Gate + live check:** `npm run check` fully clean. If the dev gateway (port 18999) is up, smoke the view against it (`npm run dev` is already running; report what you saw). If not feasible, mark the live row `RTL + manual pending`.
6. **Matrix + commit:** every row → `ported` + test evidence (or `waived` + reason — never silent). One commit: `feat(frontend): <view> view migration`.

**View CSS:** a `<view>.css` next to the page (imported by it) in the Health style — only for structures Tailwind utilities can't express cleanly; prefer utilities + design-system classes first.

---

### Task 1: Approval-monitor service port

**Files:** Create `frontend/src/services/approval-monitor.ts`, `frontend/src/services/approval-monitor.test.ts`, `frontend/src/components/ApprovalPrompt.tsx`; modify `frontend/src/app/AppShell.tsx` (mount point + pending badge on the approvals nav item), `frontend/src/app/providers.tsx` (start/stop wiring).

**Legacy spec:** `static/js/approval_monitor.js` (271 lines). Key behaviors: REST polling of `GET /api/approvals` (NOT WS-RPC) with adaptive backoff 1500ms → 30000ms, immediate re-poll on window focus/visibilitychange, `POST /api/approvals/resolve`, modal prompt for pending approvals (approve/deny + elevated mode), localStorage keys `agentos.elevatedMode` + `agentos.elevatedMode.version` (storage version '2'), toast on new pending count, `pollNow()` re-poll hook consumed by the approvals view.

**Interfaces produced:** `approvalMonitor.start() / stop() / pollNow()`, Zustand store `useApprovals` (`{pending: Approval[], count: number}`), `<ApprovalPrompt />` modal mounted in AppShell. REST paths derive from `bootstrapUrl()`-style base derivation (same `/control`-aware logic; `/api/approvals` lives at the gateway root per legacy fetch — verify against control_ui/app.py routes and record the exact path in the matrix).

Follow protocol steps 1-2-3 adapted (service instead of view): inventory rows into a `### approval-monitor` matrix section; TDD the polling/backoff/focus logic with fake timers + mocked fetch; RTL the modal.

### Task 2: Approvals view (legacy 351 lines)

**Files:** `frontend/src/views/approvals/{logic.ts,logic.test.ts,ApprovalsPage.tsx,ApprovalsPage.test.tsx}` (+ `approvals.css` if needed); `routes.tsx` swap.
**RPC:** `config.get`. **Consumes Task 1:** renders pending list from `useApprovals`, resolve actions via the monitor, calls `pollNow()` after mutations (legacy approvals.js:299).
**Notable:** approval-mode config surface (read from `config.get`), durable-approvals list, empty state. Protocol applies.

### Task 3: Logs view (367 lines)

**Files:** `frontend/src/views/logs/...` per pattern.
**RPC:** `logs.status`, `logs.tail`. **Events:** none — legacy refreshes via manual controls/polling; port its exact cadence.
**Notable:** tail rendering (mono, `.t-data`, autoscroll behavior), level filtering, follow/pause control. Log lines are a natural fit for the tone-gutter primitive (error/warn lines).

### Task 4: Overview view (378 lines)

**Files:** `frontend/src/views/overview/...`.
**RPC:** `status`, `doctor.status`, `sessions.list`, `usage.status`. **Events:** wildcard `*` + `rpc.state` — legacy refreshes cards on events; map to targeted `invalidateQueries`.
**Notable:** the landing dashboard — stat tiles (reuse the Health count-tile posture), quick links to other views, readiness summary. This is the default desktop route: keep it fast (parallel queries).

### Task 5: Channels view (419 lines)

**Files:** `frontend/src/views/channels/...`.
**RPC:** `channels.status`, `channels.access.list/resolve/revoke/setMode`. **Events:** `channel.status` → invalidate.
**Notable:** channel cards with status variants (tone system), telegram access panel (mode select + approve/deny/revoke mutations), guided-setup links into `/setup`.

### Task 6: Agents view (533 lines)

**Files:** `frontend/src/views/agents/...`.
**RPC:** `agents.list/create/update/delete`.
**Notable:** CRUD table + create/edit dialog (shadcn Dialog), delete confirmation, validation in logic.ts. Destructive actions get `variant="destructive"` + confirm.

### Task 7: Sessions view (847 lines)

**Files:** `frontend/src/views/sessions/...`.
**RPC:** `sessions.list/create/delete`, `agents.list/create`.
**Notable:** session table (agent, activity, status), create-session flow (agent picker), delete confirm, jump-to-chat links (`/chat?session=...` — link only; chat itself is Plan 3).

### Task 8: Usage view (889 lines)

**Files:** `frontend/src/views/usage/...`.
**RPC:** `usage.status`.
**Notable:** usage aggregates + per-model/per-day breakdowns. Numbers are `.t-data` mono; charts in legacy are DOM-built bars — rebuild as simple CSS bars on tokens (no chart lib; YAGNI).

### Task 9: Config view (917 lines)

**Files:** `frontend/src/views/config/...`.
**RPC:** `config.get/patch/apply`.
**Notable:** config tree/editor with dirty-state tracking, patch vs apply semantics, validation errors inline, dangerous-change confirmation. logic.ts owns diff/dirty derivation — test it hard.

### Task 10: Skills view (1081 lines)

**Files:** `frontend/src/views/skills/...`.
**RPC:** `skills.list/search/install/uninstall/update/deps.install`.
**Notable:** installed-vs-catalog lists, search with debounce, install/update/uninstall mutations with busy states, dependency-install prompt. `CommandLine` for any CLI hints legacy shows.

### Task 11: Cron view (1529 lines)

**Files:** `frontend/src/views/cron/...`.
**RPC:** `cron.list/update/remove/run/runs/subscribe/unsubscribe`. **Events:** `cron.run.finished` → invalidate runs.
**Notable:** job list + enable/disable toggles, run-now, run history drawer, subscribe/unsubscribe lifecycle tied to view mount/unmount (legacy subscribes on render, unsubscribes on destroy — mirror in useEffect cleanup; get this exactly right).

### Task 12: Setup view (2046 lines — the big one)

**Files:** `frontend/src/views/setup/...` (multiple components allowed: one orchestrator + per-section panels; keep files focused).
**RPC:** `onboarding.status/catalog/provider.configure/router.configure/memory_embedding.configure/search.configure/audio.configure/imageGeneration.configure/channel.probe/channel.upsert`, `channels.status`, `config.get/patch`, `doctor.memory.status`.
**Notable:** the guided onboarding surface — sectioned wizard (provider, router, memory, search, audio, image-gen, channels), probe flows with busy/result states, secrets input (masked; never logged), per-section status from `onboarding.status`. Decompose: `SetupPage` orchestrator + one component per section + shared `logic.ts`. This task may take multiple commits (one per section group) — all under the protocol.

### Task 13: Whole-batch parity audit + fix round

Run the two saved workflows over the Plan-2 surface, in order:
1. `Workflow` with the **migration-parity-review** script (session scripts dir) scoped to the 11 views + approval monitor: auditors per view-pair, critic, 3-lens verify. (Controller runs this — the implementer of this task prepares nothing; this task exists for the ledger.)
2. `Workflow({scriptPath: '.claude/workflows/parity-fix-round.js'})` with the confirmed findings as batches (controller assembles args as in the Plan-1 round).

### Task 14: Plan-2 close-out

- `npm run check` + `npm run build` clean; count tests (expect 200+).
- `uv run pytest tests/test_gateway tests/test_fe_parity_inventory.py -q` still green (no Python changes).
- Legacy-untouched check (same git diff command as Plan 1 Task 10).
- Matrix audit: all 11 view sections + approval-monitor section zero `pending` (waivers owner-reviewed); dated "Plan 2 complete" note.
- Do NOT push or open a PR without explicit user approval.

## Execution notes for the controller

- Dispatch order = task order (size-ascending keeps feedback loops short early).
- One implementer at a time (shared branch). Opus for all (user's standing instruction).
- Per-task review (task-reviewer template) after each view; carry Minors in the ledger; the Task-13 workflows are the whole-batch net.
- The dev gateway (18999) + vite (5173) may still be running from the design session — implementers should reuse, not restart.
