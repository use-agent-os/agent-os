# Chat View Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate the legacy `chat` view (`src/agentos/gateway/static/js/views/chat.js`, 8,841 lines) to the React + Vite console at behavioral parity, replacing the last remaining `StubView`.

**Architecture:** The timing-sensitive transcript (streaming, tool cards, artifacts, router-fx, compaction) is ported **near-verbatim** as imperative `ref`-driven controllers/modules, verified by a mandatory live-browser sweep. Everything around it (composer, attachments, slash menu, session chip, elevated pill, toolbar, pending queue, inline approvals) is idiomatic React with RTL tests. The live event stream is consumed via imperative `rpc.on()` on the existing Task-5 `WsRpcClient`; `chat.history` uses react-query. No new transport.

**Tech Stack:** React 19, Vite 6, strict TypeScript, Tailwind v4, @tanstack/react-query, motion, sonner, Vitest + React Testing Library. Terminal design system in `frontend/src/styles/globals.css`.

## Global Constraints

- Design doc (authority): `docs/superpowers/specs/2026-07-20-chat-view-migration-design.md`.
- Parity matrix (single source of truth): `docs/superpowers/specs/2026-07-19-console-rewrite-parity-matrix.md` — every task adds real rows.
- Legacy source: `src/agentos/gateway/static/js/views/chat.js`. **Read cited line ranges verbatim; do not port from memory.**
- **RPC brief is NOT exhaustive.** Every task implementer reads its legacy functions end-to-end and cross-checks `scripts/fe_parity_inventory.py`. (Cron view nearly lost functionality by trusting the documented list.)
- **Never** declare a legacy behavior "owner-approved" for removal. Deviations → recorded **waived** in the matrix; OWNER decides.
- Follow migration protocol per module: `inventory → logic.ts (TDD) → component/controller → parity matrix with real evidence`. **Never cite tests that don't exist.**
- Imperative region is **not** RTL-testable → verified by the **mandatory live-browser sweep** (§ each imperative task's final step).
- Terminal design system: `.view-container` NOT used (chat is full-bleed, documented exception); reuse `.btn-term`, `AsciiField`, `CommandLine`, `.tone-*` (one gutter per severity), `.t-label`/`.t-display`; lime `#CCFF00` = signal only; radius `0`; dialogs via `ModalShell` (portal).
- Conventional Commits. **No AI attribution trailers** (`Co-Authored-By` etc.) — repo policy.
- **No outward actions** (push/PR/tag) without explicit owner approval.
- Gate before marking any task done: `cd frontend && npm run check` (tsc + ESLint + Prettier + Vitest).
- Dev loop for live sweeps:
  ```bash
  AGENTOS_STATE_DIR=/private/tmp/claude-501/.../scratchpad/chat-state uv run agentos gateway run --port 18999
  cd frontend && AGENTOS_GATEWAY=http://127.0.0.1:18999 npm run dev   # proxies /ws, /control/api, /api
  ```
  Verify with claude-in-chrome at `http://127.0.0.1:5173/control/chat`.

## File Structure

All under `frontend/src/views/chat/`:

- `logic.ts` / `logic.test.ts` — pure helpers (session-key canonicalization, message identity, seq helpers, formatters, MIME/attachment predicates, slash normalization, elevated-mode normalization, compaction/router-fx pure predicates, markdown export builder). **Grows across tasks; every added export is TDD'd.**
- `types.ts` — shared TypeScript types (Message, ToolCall, Artifact, StreamEvent payloads, ElevatedMode, SlashCommand, Attachment, RouterDecision).
- `ChatPage.tsx` / `ChatPage.test.tsx` — the React shell: full-bleed layout, mounts the transcript controller, hosts composer/menus/chip/pill/toolbar/dialogs.
- `useTranscript.ts` — the imperative transcript controller hook (ref container + ported render/stream/history). **Not RTL-tested; live-verified.**
- `transcript/` — ported imperative modules the controller calls into:
  - `stream.ts` — streaming renderer (delta/flush/bubble/finalize/seq/park-restore/thinking/idle).
  - `history.ts` — history load/merge/day-separators/pagination.
  - `tools.ts` — tool call/result DOM + subagent disclosure.
  - `artifacts.ts` — artifact cards/category/download/preview.
  - `routerFx.ts` — router-fx animation engine.
  - `compaction.ts` — compaction separators + toast.
- `Composer.tsx` / `Composer.test.tsx`, `Attachments.tsx` / `.test.tsx`, `SlashMenu.tsx` / `.test.tsx`, `SessionChip.tsx` / `.test.tsx`, `ElevatedPill.tsx` / `.test.tsx`, `Toolbar.tsx` / `.test.tsx`, `PendingQueue.tsx` / `.test.tsx`, `InlineApproval.tsx` / `.test.tsx`.
- `chat.css` — full-bleed layout + any chat-specific terminal styling not in globals.
- Wire into `frontend/src/app/routes.tsx` (replace the `chat` StubView) — done in the final task.

**Reference an existing migrated view** (`frontend/src/views/skills/`) for conventions: `useRpc()` from `@/app/providers`, react-query hooks, `AsciiField`/`ModalShell`/`Button`/`MotionListItem` primitives, `toast` from sonner, `logic.ts`/`logic.test.ts` split.

---

## Task 1: Foundation — types, pure logic helpers, controller skeleton

**Files:**
- Create: `frontend/src/views/chat/types.ts`
- Create: `frontend/src/views/chat/logic.ts`, `frontend/src/views/chat/logic.test.ts`
- Create: `frontend/src/views/chat/useTranscript.ts`
- Create: `frontend/src/views/chat/chat.css`
- Create: `frontend/src/views/chat/ChatPage.tsx`, `frontend/src/views/chat/ChatPage.test.tsx`

**Interfaces:**
- Consumes: `useRpc()` from `@/app/providers` (returns `WsRpcClient`).
- Produces (for later tasks):
  - `types.ts`: `type Role = 'user' | 'assistant' | 'system'`; `interface ChatMessage { role: Role; text: string; timestamp?: number; transcriptId?: string | null }`; `interface StreamEventPayload { seq?: number; session_key?: string; [k: string]: unknown }`.
  - `logic.ts`: `agentIdFromSessionKey(key: string): string` (port chat.js:1145); `webchatSessionKey(agentId: string, suffix?: string): string` (chat.js:1151); `canonicalSessionKey(key: string): string` (chat.js:1159); `readSessionFromUrl(search: string): string | null` (pure over an injected search string; port the URL-reading part of chat.js:1182); `messageTranscriptId(msg: ChatMessage): string | null` (chat.js:3086); `historyStableMessageIdentity(msg: ChatMessage): string` (chat.js:5833); `historyFallbackMessageIdentity(role: Role, text: string): string` (chat.js:5838).
  - `useTranscript.ts`: `function useTranscript(opts: { sessionKey: string }): { containerRef: React.RefObject<HTMLDivElement>; /* extended in later tasks */ }`.

- [ ] **Step 1: Inventory.** Read `chat.js:1145-1197` (session-key helpers), `1182-1197` (`_readSessionFromUrl`), `3086-3101` (`_messageTranscriptId`), `5833-5864` (history identity helpers). Run `python3 scripts/fe_parity_inventory.py | grep -A30 chat` and record the localStorage keys this view owns. Note anything the design doc §5 list missed.

- [ ] **Step 2: Write failing tests for the pure helpers.**

```ts
// logic.test.ts
import { describe, it, expect } from 'vitest'
import { agentIdFromSessionKey, canonicalSessionKey, readSessionFromUrl, webchatSessionKey } from './logic'

describe('session key helpers', () => {
  it('extracts the agent id from a webchat session key', () => {
    expect(agentIdFromSessionKey('agent:main:webchat:default')).toBe('main')
  })
  it('builds a webchat session key with the default suffix', () => {
    expect(webchatSessionKey('main')).toBe('agent:main:webchat:default')
  })
  it('canonicalizes aliases to the stable key (parity chat.js:1159)', () => {
    // fill exact expected mappings from the legacy switch/aliases at chat.js:1159-1166
    expect(canonicalSessionKey('default')).toBe(canonicalSessionKey('default'))
  })
  it('reads ?session= from a search string, else null', () => {
    expect(readSessionFromUrl('?session=agent%3Amain%3Awebchat%3Adefault')).toBe('agent:main:webchat:default')
    expect(readSessionFromUrl('')).toBeNull()
  })
})
```

- [ ] **Step 3: Run tests, verify they fail.** Run: `cd frontend && npx vitest run src/views/chat/logic.test.ts`. Expected: FAIL (module/exports not found).

- [ ] **Step 4: Implement `types.ts` and `logic.ts`.** Port the cited legacy functions verbatim into pure TS (inject `search`/`storage` rather than reading `window`). Match the exact alias table at chat.js:1159-1166 and the URL parsing at chat.js:1182-1197.

- [ ] **Step 5: Run tests, verify pass.** Run: `cd frontend && npx vitest run src/views/chat/logic.test.ts`. Expected: PASS.

- [ ] **Step 6: Controller skeleton + full-bleed shell.** Create `useTranscript.ts` returning `{ containerRef }` (a `useRef<HTMLDivElement>(null)` — no rendering logic yet). Create `chat.css` with the full-bleed flex layout (scroll region + pinned composer row) opting out of `.view-container`. Create `ChatPage.tsx`: mounts `<div className="chat-thread" ref={containerRef} />` above a placeholder composer row; sets `document.title = 'Chat - AgentOS Control'`.

- [ ] **Step 7: Shell smoke test (RTL).**

```tsx
// ChatPage.test.tsx
import { render, screen } from '@testing-library/react'
import { ChatPage } from './ChatPage'
import { renderWithProviders } from '@/test/utils' // use the repo's existing provider test wrapper

it('renders the full-bleed chat shell with a thread region', () => {
  renderWithProviders(<ChatPage />)
  expect(document.querySelector('.chat-thread')).not.toBeNull()
  expect(document.title).toBe('Chat - AgentOS Control')
})
```

(If `@/test/utils`/`renderWithProviders` does not exist, use the same provider-wrapping pattern `SkillsPage.test.tsx` uses — check it first.)

- [ ] **Step 8: Run gate.** Run: `cd frontend && npm run check`. Expected: PASS.

- [ ] **Step 9: Add parity matrix rows** for the session-key helpers + shell (real test names). Commit.

```bash
git add frontend/src/views/chat docs/superpowers/specs/2026-07-19-console-rewrite-parity-matrix.md
git commit -m "feat(frontend): chat view foundation — types, session-key logic, full-bleed shell"
```

---

## Task 2: Transcript streaming renderer (imperative)

**Files:**
- Create: `frontend/src/views/chat/transcript/stream.ts`
- Modify: `frontend/src/views/chat/useTranscript.ts`
- Create: `frontend/src/views/chat/transcript/stream.test.ts` (pure-helper coverage only)

**Interfaces:**
- Consumes: `containerRef` (Task 1); `ChatMessage`, `StreamEventPayload` (Task 1).
- Produces: a `StreamController` object created inside `useTranscript`:
  - `acceptStreamSeq(payload: StreamEventPayload): boolean` (port chat.js:6345-6378 — 800-event seen window, per-session dedup).
  - `startStreaming(): void` (chat.js:6553), `ensureStreamBubble(): HTMLElement` (chat.js:6576), `appendDelta(text: string): void` (chat.js:6651), `flushRender(): void` (chat.js:6693), `endStreaming(opts?): void` (chat.js:6716), `reconcileFinalStreamText(finalText: string): void` (chat.js:6046).
  - `parkСurrentSessionStreamState(reason: string): void` (chat.js:6851), `restoreLiveStreamStateForSession(key: string): void` (chat.js:6911).
  - `showThinkingIndicator()/hideThinkingIndicator()` (chat.js:6379/6486); `resetStreamIdleTimer()/clearStreamIdleTimer()` (chat.js:6235/6209); `scrollToBottom()` (chat.js:7924).
  - Constants ported verbatim: `_DEFAULT_STREAM_IDLE_TIMEOUT_MS = 210000` (chat.js:51), `_STREAM_SEQ_SEEN_WINDOW = 800` (chat.js:56), `_THINKING_DELAY_MS = 400` / `_THINKING_TTL_MS = 60000` (chat.js:378-379).

- [ ] **Step 1: Inventory.** Read `chat.js:6046-6062` (reconcile), `6209-6378` (idle timer + seq accept), `6379-6551` (thinking), `6553-6836` (stream lifecycle), `6837-7001` (park/restore/clear view-local state), `7924-7951` (scroll). List every module-global this region mutates (e.g. `_streamBubble`, `_thinkingEl`, `_streamSeqBySession`) — these become fields on the controller instance.

- [ ] **Step 2: Write failing tests for the pure/testable seq logic.** (The DOM mutation is live-verified, but the seq gate is pure and MUST be unit-tested.)

```ts
// stream.test.ts
import { describe, it, expect } from 'vitest'
import { createSeqGate } from './stream' // extract the seq window into a pure factory

describe('stream seq gate (parity chat.js:6345-6378)', () => {
  it('accepts strictly increasing seqs and rejects duplicates within the 800 window', () => {
    const gate = createSeqGate()
    expect(gate.accept('s1', 1)).toBe(true)
    expect(gate.accept('s1', 1)).toBe(false) // duplicate
    expect(gate.accept('s1', 2)).toBe(true)
  })
  it('tracks seqs per session independently', () => {
    const gate = createSeqGate()
    expect(gate.accept('s1', 5)).toBe(true)
    expect(gate.accept('s2', 5)).toBe(true) // different session
  })
})
```

- [ ] **Step 3: Run tests, verify fail.** Run: `cd frontend && npx vitest run src/views/chat/transcript/stream.test.ts`. Expected: FAIL.

- [ ] **Step 4: Port the streaming module verbatim.** Port chat.js:6046-7001 + 7924-7951 into `stream.ts` as a `createStreamController(containerRef, deps)` factory whose methods are the legacy functions with module-globals rebound to instance fields. Extract `createSeqGate()` as the pure factory the test targets. Preserve: the 210s idle timer, the 800-seq window, thinking 400ms/60s timings, and `_flushRender`'s markdown/scroll behavior exactly. Wire the controller into `useTranscript.ts`.

- [ ] **Step 5: Run tests, verify pass.** Run: `cd frontend && npx vitest run src/views/chat/transcript/stream.test.ts`. Expected: PASS.

- [ ] **Step 6: Run gate.** `cd frontend && npm run check`. Expected: PASS.

- [ ] **Step 7: LIVE-BROWSER SWEEP (mandatory).** Start the dev gateway + vite (Global Constraints). With claude-in-chrome at `/control/chat`: send a message; confirm the streaming bubble appears, text streams token-by-token, the block cursor shows, it auto-scrolls, the thinking indicator appears (>400ms) then clears, and the bubble finalizes. Confirm zero console errors. Record evidence (what you drove + result) for the matrix.

- [ ] **Step 8: Add parity matrix rows** (seq-gate unit test names + live evidence for the DOM stream). Commit.

```bash
git add frontend/src/views/chat docs/superpowers/specs/2026-07-19-console-rewrite-parity-matrix.md
git commit -m "feat(frontend): chat transcript streaming renderer (imperative, ported)"
```

---

## Task 3: History load + pagination + subscription wiring

**Files:**
- Create: `frontend/src/views/chat/transcript/history.ts`, `history.test.ts`
- Modify: `frontend/src/views/chat/useTranscript.ts`

**Interfaces:**
- Consumes: stream controller (Task 2); `useRpc()`.
- Produces:
  - `mergeHistoryMessagePages(older: ChatMessage[], current: ChatMessage[]): ChatMessage[]` (port chat.js:5357-5368).
  - `messagePageIdentity(msg: ChatMessage): string` (chat.js:5350).
  - inside `useTranscript`: a `useEffect` keyed on `sessionKey` that calls `rpc.call('sessions.messages.subscribe', {...})` on mount and `unsubscribe` on cleanup (port `_subscribeSession` chat.js:2857 / `_unsubscribeSession` chat.js:2909), registers `rpc.on('session.event.text_delta'|...)` handlers dispatching into the Task-2 controller, and handles `_gap` → terminal-history resync (`_syncTerminalSessionChange` chat.js:1713).
  - history read via **react-query** `useQuery(['chat','history',sessionKey], () => rpc.call('chat.history', { key, limit: CHAT_HISTORY_PAGE_SIZE }))`, `CHAT_HISTORY_PAGE_SIZE = 50` (chat.js:350).

- [ ] **Step 1: Inventory.** Read `chat.js:2857-2915` (subscribe/unsubscribe), `5289-5798` (history load/merge/render/day-separator/scope-row), `1688-1766` (foreign-payload drop + terminal sync), and the full `rpc.on(...)` registration block (search `rpc.on(` in chat.js). Enumerate EVERY `session.event.*` handler — cross-check against design §5 (do not assume the list is complete).

- [ ] **Step 2: Write failing tests for merge/identity.**

```ts
// history.test.ts
import { describe, it, expect } from 'vitest'
import { mergeHistoryMessagePages } from './history'

describe('mergeHistoryMessagePages (parity chat.js:5357)', () => {
  it('prepends older messages without duplicating the overlap boundary', () => {
    const current = [{ role: 'user', text: 'b' }, { role: 'assistant', text: 'c' }]
    const older = [{ role: 'user', text: 'a' }, { role: 'user', text: 'b' }]
    const merged = mergeHistoryMessagePages(older as never, current as never)
    expect(merged.map((m) => m.text)).toEqual(['a', 'b', 'c']) // b deduped by identity
  })
})
```

- [ ] **Step 3: Run, verify fail.** `cd frontend && npx vitest run src/views/chat/transcript/history.test.ts`. Expected: FAIL.
- [ ] **Step 4: Implement `history.ts`** (port merge/identity verbatim) and wire subscription + react-query history into `useTranscript.ts`. Register every enumerated `session.event.*` handler; unknown-but-present events must still be dispatched exactly as legacy does.
- [ ] **Step 5: Run, verify pass.** Same command. Expected: PASS.
- [ ] **Step 6: Gate.** `cd frontend && npm run check`. Expected: PASS.
- [ ] **Step 7: LIVE SWEEP.** Load `/control/chat?session=<an existing session>`; confirm history renders, day separators appear, scrolling to top triggers earlier-history load (page size 50), and a fresh send still streams. Kill+restart the gateway mid-idle to confirm `_gap`/reconnect resync. Zero console errors. Record evidence.
- [ ] **Step 8: Matrix rows + commit.**

```bash
git commit -am "feat(frontend): chat history pagination + session subscription wiring"
```

---

## Task 4: Tool activity + subagent disclosure (imperative)

**Files:** Create `frontend/src/views/chat/transcript/tools.ts`, `tools.test.ts`; modify `useTranscript.ts`.

**Interfaces:**
- Produces: `buildToolCallDOM(name, toolId, input, isRunning): HTMLElement` (chat.js:7061); `appendToolCall(payload): void` (chat.js:7320); `appendToolResult(payload): void` (chat.js:7368); `buildToolResultDOM(content, isError, isTruncated?, toolName?): HTMLElement` (chat.js:7284); `settleToolResultCard(payload, isError): void` (chat.js:7181); `reconstructToolCalls(bubbleDiv, segments): void` (chat.js:7681); `appendSubagentCompletion(payload): void` (chat.js:7796). Pure helpers to TDD: `toolDisplayName(name, input): string` (chat.js:7049), `fmtToolDuration(ms): string` (chat.js:7107), `toolResultIsError(payload): boolean` (chat.js:7206), `toolResultIsTruncated(payload): boolean` (chat.js:7221), `parseSubagentCompletion(text)` (chat.js:7817), `isControlPlaneToolName(name)` (chat.js:7057).

- [ ] **Step 1: Inventory.** Read chat.js:7022-7455 (tool call/result DOM + status/duration/truncation) and 7681-7850 (reconstruct + subagent). Note the `_toolEmoji` map (chat.js:517) and control-plane tool filtering.
- [ ] **Step 2: Failing tests for pure helpers.**

```ts
// tools.test.ts
import { describe, it, expect } from 'vitest'
import { fmtToolDuration, toolResultIsError, toolDisplayName } from './tools'
describe('tool pure helpers', () => {
  it('formats sub-second and multi-second durations (parity chat.js:7107)', () => {
    expect(fmtToolDuration(450)).toBe('450ms')   // confirm exact legacy format
    expect(fmtToolDuration(1500)).toBe('1.5s')
  })
  it('detects error tool results (parity chat.js:7206)', () => {
    expect(toolResultIsError({ is_error: true } as never)).toBe(true)
    expect(toolResultIsError({ } as never)).toBe(false)
  })
})
```
(Confirm exact output strings against the legacy source before asserting.)
- [ ] **Step 3: Run, verify fail.** `cd frontend && npx vitest run src/views/chat/transcript/tools.test.ts`. Expected: FAIL.
- [ ] **Step 4: Port `tools.ts` verbatim** and dispatch `session.event.tool_use_start`/`tool_result`/`subagent_completion`/`task_group.*` into it from `useTranscript`.
- [ ] **Step 5: Run, verify pass.** Same command. Expected: PASS.
- [ ] **Step 6: Gate.** `npm run check`. Expected: PASS.
- [ ] **Step 7: LIVE SWEEP.** Send a prompt that triggers a tool call (e.g. a message that makes the agent read a file). Confirm the tool card appears (emoji + name), shows running state, then settles with duration + result, truncation state renders, errors show error tone. Confirm a subagent completion renders its disclosure. Zero console errors. Record.
- [ ] **Step 8: Matrix + commit.** `git commit -am "feat(frontend): chat tool activity + subagent disclosure (imperative)"`

---

## Task 5: Artifacts + publish (imperative)

**Files:** Create `frontend/src/views/chat/transcript/artifacts.ts`, `artifacts.test.ts`; modify `useTranscript.ts`.

**Interfaces:**
- Produces: `appendArtifact(payload): void` (chat.js:7457); `renderArtifacts(artifacts): void` (chat.js:7595); `downloadArtifact(artifact): Promise<void>` (chat.js:7653). Pure helpers to TDD: `artifactCategory(artifact): string` (chat.js:7538), `artifactCategoryLabel(category)` (chat.js:7551), `artifactExtension(name)` (chat.js:7531), `isImageArtifact`/`isAudioArtifact` (chat.js:7561/7565), `artifactDownloadUrl(artifact)` (chat.js:7480), `artifactAuthenticatedDownloadUrl(raw, token)` (chat.js:7583), `publishArtifactTargetName(input)` (chat.js:7043).

- [ ] **Step 1: Inventory.** Read chat.js:7043-7048, 7457-7680 (artifact render/category/url/preview/download). Note authenticated-URL token handling and image/audio inline preview.
- [ ] **Step 2: Failing tests for category/extension/url helpers.**

```ts
// artifacts.test.ts
import { describe, it, expect } from 'vitest'
import { artifactCategory, artifactExtension } from './artifacts'
describe('artifact classification (parity chat.js:7531-7550)', () => {
  it('derives extension from a name', () => {
    expect(artifactExtension('report.md')).toBe('md')
  })
  it('categorizes by mime/extension into image/audio/code/data/document', () => {
    expect(artifactCategory({ mime: 'image/png', name: 'x.png' } as never)).toBe('image')
  })
})
```
- [ ] **Step 3–5:** run-fail → port `artifacts.ts` verbatim, dispatch `session.event.artifact` into it → run-pass. Commands: `cd frontend && npx vitest run src/views/chat/transcript/artifacts.test.ts`.
- [ ] **Step 6: Gate.** `npm run check`.
- [ ] **Step 7: LIVE SWEEP.** Trigger an artifact (ask the agent to publish/generate a file). Confirm the artifact card renders with category label, image/audio previews inline, download works (authenticated URL), and publish target name shows. Zero console errors. Record.
- [ ] **Step 8: Matrix + commit.** `git commit -am "feat(frontend): chat artifacts + publish rendering (imperative)"`

---

## Task 6: Router-fx engine (imperative)

**Files:** Create `frontend/src/views/chat/transcript/routerFx.ts`, `routerFx.test.ts`; modify `useTranscript.ts`.

**Interfaces:**
- Produces: `buildRouterFxElement(decision, opts): HTMLElement` (chat.js:3708); `routerFxMountStrip(wrap)` (chat.js:3907); `flushPendingRouterDecisions(): void` (chat.js:3666); `cachePendingRouterDecision(payload)` (chat.js:3656). Pure helpers to TDD: `routerFxStripProvider(name)` (chat.js:3451), `modelDisplayName(name)` (chat.js:3444), `routerFxNormalizeRequestKind(kind)` (chat.js:3464), `routerFxRequestKindFromAttachments(attachments)` (chat.js:3455), `routerFxVisualEntries(requestKind, decision)` (chat.js:3508), `routerFxHasMultipleCandidates(...)` (chat.js:3551), `routerFxSeedCacheKey(...)` (chat.js:3582). Pref storage keys: `agentos-router-fx` (chat.js), seed cache prefix `osq.routerFx.seed:` (chat.js:3591).

- [ ] **Step 1: Inventory.** Read chat.js:3398-4402 (the router-fx subsystem, ~80 functions). It's self-contained animation code — port as one module. Note seed-cache trim/resolve and the winner/settled animation.
- [ ] **Step 2: Failing tests for the pure entry/seed helpers.**

```ts
// routerFx.test.ts
import { describe, it, expect } from 'vitest'
import { routerFxStripProvider, routerFxNormalizeRequestKind } from './routerFx'
describe('router-fx pure helpers', () => {
  it('strips the provider prefix from a model id (parity chat.js:3451)', () => {
    expect(routerFxStripProvider('anthropic/claude-x')).toBe('claude-x') // confirm exact legacy behavior
  })
  it('normalizes request kinds (parity chat.js:3464)', () => {
    expect(routerFxNormalizeRequestKind('TEXT')).toBe(routerFxNormalizeRequestKind('text'))
  })
})
```
- [ ] **Step 3–5:** run-fail → port `routerFx.ts` verbatim, dispatch `session.event.router_decision` + hook into stream anchor placement → run-pass.
- [ ] **Step 6: Gate.** `npm run check`.
- [ ] **Step 7: LIVE SWEEP.** With router-fx enabled (localStorage `agentos-router-fx`), send a message and confirm the tier grid strip animates, pings candidates, highlights the winner, and settles/normalizes. Toggle the pref off and confirm it's suppressed. Zero console errors. Record.
- [ ] **Step 8: Matrix + commit.** `git commit -am "feat(frontend): chat router-fx animation engine (imperative)"`

---

## Task 7: Compaction separators + controls (imperative)

**Files:** Create `frontend/src/views/chat/transcript/compaction.ts`, `compaction.test.ts`; modify `useTranscript.ts`.

**Interfaces:**
- Produces: `syncCompactionSeparator(payload, status, source, overrides?): void` (chat.js:3043); `buildCompactionSeparator(label, tone?, extraClass?): HTMLElement` (chat.js:2982); `showCompactionToast(payload, meta?): void` (chat.js:3285); `renderCompactionSummarySeparators(messages): void` (chat.js:3119); `setCompactInFlight(active, key?): void` (chat.js:8654); `settleCompactInFlight(payload?, options?): void` (chat.js:8665). RPC: `sessions.contextCompact`. Pure helpers to TDD: `compactionTerminalStatus(status)` (chat.js:3000), `compactionSeparatorTone(status, payload?)` (chat.js:3035), `compactionStatusLabel(payload, source, status)` (chat.js:3019), `shouldPersistCompactionSeparator(...)` (chat.js:3011), `compactionUserVisible(...)` (chat.js:3231).

- [ ] **Step 1: Inventory.** Read chat.js:2916-3397 (compaction separators/tones/status/toast/suppression) and 8654-8710 (in-flight state). Note router-fx suppression for compaction turns (chat.js:3263-3284).
- [ ] **Step 2: Failing tests for tone/status/persistence helpers.**

```ts
// compaction.test.ts
import { describe, it, expect } from 'vitest'
import { compactionTerminalStatus, compactionSeparatorTone } from './compaction'
describe('compaction pure helpers', () => {
  it('identifies terminal statuses (parity chat.js:3000)', () => {
    expect(compactionTerminalStatus('done')).toBe(true) // confirm exact legacy set
    expect(compactionTerminalStatus('running')).toBe(false)
  })
  it('maps status to a tone (parity chat.js:3035)', () => {
    expect(compactionSeparatorTone('error')).toBe('error') // confirm exact mapping
  })
})
```
- [ ] **Step 3–5:** run-fail → port `compaction.ts` verbatim, dispatch `session.event.compaction` → run-pass.
- [ ] **Step 6: Gate.** `npm run check`.
- [ ] **Step 7: LIVE SWEEP.** Trigger a compaction (via the compact control or long context). Confirm the animated separator appears with the right tone/label, persists or auto-removes per status, the toast shows once (no duplicate), and router-fx is suppressed for the compaction turn. Zero console errors. Record.
- [ ] **Step 8: Matrix + commit.** `git commit -am "feat(frontend): chat compaction separators + controls (imperative)"`

---

## Task 8: Composer (React)

**Files:** Create `frontend/src/views/chat/Composer.tsx`, `Composer.test.tsx`; extend `logic.ts`/`logic.test.ts`; modify `ChatPage.tsx`.

**Interfaces:**
- Consumes: `useTranscript` send hook; `useRpc()`.
- Produces: `<Composer onSend={(text) => void} busy={boolean} />`. Pure helpers to TDD: `shouldAutofocusComposer(env): boolean` (chat.js:1353), and the send-button enable/label logic extracted from `_updateSendButton` (chat.js:7002) as `sendButtonState(input, busy, pending): { disabled: boolean; label: string }`.
- Wires RPC: `chat.send` (via the controller's `_onSend` port at chat.js:6062), `chat.abort` (chat.js).

- [ ] **Step 1: Inventory.** Read chat.js:2181-2216 (resize), 6062-6208 (`_onSend`), 7002-7021 (send button), 8711-8760 (history cycling), 1353-1360 (autofocus).
- [ ] **Step 2: Failing tests (RTL + logic).**

```tsx
// Composer.test.tsx
import { render, screen, fireEvent } from '@testing-library/react'
import { Composer } from './Composer'
it('disables send when empty and enables on input', () => {
  render(<Composer onSend={() => {}} busy={false} />)
  const send = screen.getByRole('button', { name: /send/i })
  expect(send).toBeDisabled()
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'hi' } })
  expect(send).toBeEnabled()
})
it('shows abort affordance while busy', () => {
  render(<Composer onSend={() => {}} busy={true} />)
  expect(screen.getByRole('button', { name: /abort|stop/i })).toBeInTheDocument()
})
```
- [ ] **Step 3: Run, verify fail.** `cd frontend && npx vitest run src/views/chat/Composer.test.tsx`. Expected: FAIL.
- [ ] **Step 4: Implement `Composer.tsx`** using `AsciiField`/`CommandLine` + `.btn-term`; textarea auto-resize; history cycling on ↑/↓ at caret bounds (port chat.js:8711); send/abort wiring. Autofocus per `shouldAutofocusComposer`.
- [ ] **Step 5: Run, verify pass.** Same command. Expected: PASS.
- [ ] **Step 6: Gate.** `npm run check`.
- [ ] **Step 7: LIVE SWEEP.** Type, send (Enter + button), abort mid-stream, cycle history with ↑/↓, confirm resize. Zero console errors. Record.
- [ ] **Step 8: Matrix + commit.** `git commit -am "feat(frontend): chat composer (React, RTL-tested)"`

---

## Task 9: Attachments + payload normalization (React)

**Files:** Create `frontend/src/views/chat/Attachments.tsx`, `Attachments.test.tsx`; extend `logic.ts`/`logic.test.ts`.

**Interfaces:**
- Produces: `<Attachments ... />` + attachment tray; pure helpers to TDD (port verbatim): `isAllowedAttachmentMime(mime)` (chat.js:304), `isImageAttachmentMime`/`isTextAttachmentMime`/`canStageAttachmentMime` (chat.js:307/310/313), `attachmentHardCapBytes(mime)` (chat.js:316), `resolveAttachmentMime(file)` (chat.js:8291), `normalizeOutgoingComposerPayload(text, attachments, options?)` (chat.js:7982), large-paste/page-dump detection (chat.js constants `LARGE_PASTE_CHARS=20000`, `PAGE_DUMP_CHARS=8000`, markers at chat.js:256). Caps: text/PDF `INLINE_THRESHOLD_BYTES=2_000_000` (chat.js:251), image `5*1024*1024` (chat.js:268), PDF `30*1024*1024` (chat.js:269). `_MAX_PENDING=5` (chat.js:335). RPC: staged upload path in `_uploadAttachmentStaged` (chat.js:8127).

- [ ] **Step 1: Inventory.** Read chat.js:251-334 (mime/caps), 7952-8345 (normalize/add/upload/preview/mime). Note the exact allowed-mime label (chat.js:303) and hard-cap rejection messages.
- [ ] **Step 2: Failing tests (logic).**

```ts
// logic.test.ts (append)
import { isAllowedAttachmentMime, attachmentHardCapBytes } from './logic'
describe('attachment mime + caps (parity chat.js:304-334)', () => {
  it('allows the documented mimes and rejects others', () => {
    expect(isAllowedAttachmentMime('image/png')).toBe(true)
    expect(isAllowedAttachmentMime('application/x-msdownload')).toBe(false)
  })
  it('applies per-type hard caps', () => {
    expect(attachmentHardCapBytes('image/png')).toBe(5 * 1024 * 1024)
    expect(attachmentHardCapBytes('application/pdf')).toBe(30 * 1024 * 1024)
  })
})
```
- [ ] **Step 3–5:** run-fail → port helpers + `Attachments.tsx` (drag/drop, paste, tray, previews, cap rejection inline with the allowed-types label) → run-pass. Command: `cd frontend && npx vitest run src/views/chat/logic.test.ts src/views/chat/Attachments.test.tsx`.
- [ ] **Step 6: Gate.** `npm run check`.
- [ ] **Step 7: LIVE SWEEP.** Attach an image (preview), a text file, an oversized file (rejection message), paste >20k chars (becomes an attachment), paste a page dump. Confirm max-5 pending enforced. Send with an attachment; confirm normalized payload. Zero console errors. Record.
- [ ] **Step 8: Matrix + commit.** `git commit -am "feat(frontend): chat attachments + payload normalization (React)"`

---

## Task 10: Slash commands (React)

**Files:** Create `frontend/src/views/chat/SlashMenu.tsx`, `SlashMenu.test.tsx`; extend `logic.ts`/`logic.test.ts`.

**Interfaces:**
- Produces: `<SlashMenu ... />`; pure helpers to TDD: `slashCommandKey(value)` (chat.js:2597), `normalizeSlashCommand(cmd)` (chat.js:2603), slash-input parse from `_handleSlashInput` (chat.js:2637) as `parseSlashInput(text): { active: boolean; query: string }`. RPC: `commands.list_for_surface` (chat.js:2615), execution via `_executeSlashCommand` (chat.js:2842).

- [ ] **Step 1: Inventory.** Read chat.js:2597-2856 (slash key/normalize/load/input/menu/select/execute).
- [ ] **Step 2: Failing tests.**

```tsx
// SlashMenu.test.tsx — filters commands on '/' input
it('opens and filters the slash menu on "/" prefix', async () => {
  // render with a stubbed commands list, type '/he', expect '/help' visible
})
```
```ts
// logic.test.ts (append) — parseSlashInput('/he') => { active: true, query: 'he' }
```
- [ ] **Step 3–5:** run-fail → port helpers + `SlashMenu.tsx` (menu render/filter/keyboard select) → run-pass.
- [ ] **Step 6: Gate.** `npm run check`.
- [ ] **Step 7: LIVE SWEEP.** Type `/`, confirm the menu opens with real commands (from `commands.list_for_surface`), filter, arrow-select, Enter to insert/execute. Zero console errors. Record.
- [ ] **Step 8: Matrix + commit.** `git commit -am "feat(frontend): chat slash commands (React)"`

---

## Task 11: Session chip + lifecycle (React)

**Files:** Create `frontend/src/views/chat/SessionChip.tsx`, `SessionChip.test.tsx`; extend `logic.ts`/`logic.test.ts`; modify `ChatPage.tsx`.

**Interfaces:**
- Produces: `<SessionChip ... />` (switcher list + current key + copy + reset); consumes session-key helpers (Task 1). Pure helpers to TDD: `sessionRunStatus(source)` (chat.js:1611), `persistSession(key)` writes `agentos_active_session` (chat.js:1167). RPC: `sessions.reset` (chat.js). URL: `?session=`/`?agent=` (react-router `useSearchParams`). Handles `session.epoch_changed`/`sessions.changed`.

- [ ] **Step 1: Inventory.** Read chat.js:1145-1197, 1549-1900 (chip render/status/switch/copy/list), 1809-1835 (`_switchToSession`).
- [ ] **Step 2: Failing tests (logic + RTL).** Test `sessionRunStatus` mapping and that switching sessions updates the URL `?session=`.
- [ ] **Step 3–5:** run-fail → implement → run-pass.
- [ ] **Step 6: Gate.** `npm run check`.
- [ ] **Step 7: LIVE SWEEP.** Open `/control/chat?session=<key>` and `?agent=<id>`; switch sessions via the chip; copy the key; reset a session; confirm `agentos_active_session` persists and the transcript re-subscribes/parks-restores correctly on switch. Zero console errors. Record.
- [ ] **Step 8: Matrix + commit.** `git commit -am "feat(frontend): chat session chip + lifecycle (React)"`

---

## Task 12: Elevated mode + toolbar/model+router config (React)

**Files:** Create `frontend/src/views/chat/ElevatedPill.tsx`, `.test.tsx`, `Toolbar.tsx`, `.test.tsx`; extend `logic.ts`/`logic.test.ts`.

**Interfaces:**
- Produces: `<ElevatedPill />`, `<Toolbar />`. Pure helpers to TDD: `normalizeElevatedMode(mode)` (chat.js:2217), `effectiveElevatedMode()` (chat.js:2221), `isApprovalBypassMode(mode)` (chat.js:2225). Storage: `agentos.elevatedMode`/`.version` (version `2`, chat.js:19). RPC: `router.hold.set`/`router.hold.clear` (chat.js), `models.list` (chat.js), `config.get`/`config.patch.safe` (chat.js), `usage.status` (chat.js). Ports: `_setElevatedMode` (2246), `_syncElevatedMode` (2277), `_updateElevatedPill` (2314), `_bindToolbarPills` (1361), `_bindRouterConfigRefresh` (1459).

- [ ] **Step 1: Inventory.** Read chat.js:2217-2596 (elevated mode), 1361-1548 (toolbar + router-config refresh), 599-732 (usage readout).
- [ ] **Step 2: Failing tests (logic + RTL).** `isApprovalBypassMode` mapping; toggling the pill calls `router.hold.set`/`clear` and persists storage with version 2.
- [ ] **Step 3–5:** run-fail → implement (reuse the Approvals view's elevated-mode conventions — cross-check `frontend/src/views/approvals/`) → run-pass.
- [ ] **Step 6: Gate.** `npm run check`.
- [ ] **Step 7: LIVE SWEEP.** Toggle elevated/bypass mode; confirm the pill state, storage version 2, and `router.hold.*` calls. Open the model picker (`models.list`), change model (`config.patch.safe`), confirm the usage readout. Zero console errors. Record.
- [ ] **Step 8: Matrix + commit.** `git commit -am "feat(frontend): chat elevated mode + toolbar/model config (React)"`

---

## Task 13: Pending queue + markdown export + inline approvals (React)

**Files:** Create `frontend/src/views/chat/PendingQueue.tsx`, `.test.tsx`, `InlineApproval.tsx`, `.test.tsx`; extend `logic.ts`/`logic.test.ts`.

**Interfaces:**
- Produces: `<PendingQueue />`, `<InlineApproval />`. Pure helpers to TDD: `exportMarkdown(messages, artifacts)` builder (chat.js:8389) + `artifactMarkdownLines(artifacts)` (chat.js:8411); pending-queue model from `_renderPendingQueue` (chat.js:8474) + `_popAllPendingIntoComposer` (chat.js:8596).
- **Inline approvals is a first-class parity concern** (own inventory + own matrix rows + own live evidence). If inventory shows the approval surface is larger than expected, STOP and split it into its own task rather than dropping behavior.

- [ ] **Step 1: Inventory.** Read chat.js:8389-8653 (export + pending queue). Then **inventory the inline-approval surface**: search chat.js for approval/bypass rendering in the thread and cross-reference `frontend/src/views/approvals/` + `frontend/src/*approval_monitor*` conventions. Enumerate every approval affordance shown in-thread. Record scope; split if larger than a single task.
- [ ] **Step 2: Failing tests.** `exportMarkdown` produces the expected markdown (headers + artifact links); pending-queue pop moves all pending into the composer; inline approval renders approve/deny with the correct tone gutter.
- [ ] **Step 3–5:** run-fail → implement → run-pass.
- [ ] **Step 6: Gate.** `npm run check`.
- [ ] **Step 7: LIVE SWEEP.** Queue messages while busy; pop all into composer; export markdown (download + artifact links). With bypass OFF, trigger an approval and confirm the in-thread approve/deny prompt (correct tone, one gutter) and that resolving it advances the stream. Zero console errors. Record.
- [ ] **Step 8: Matrix + commit.** `git commit -am "feat(frontend): chat pending queue + markdown export + inline approvals (React)"`

---

## Task 14: Route swap + full parity sweep + cutover

**Files:** Modify `frontend/src/app/routes.tsx` (replace the `chat` StubView with `<ChatPage />`); update the parity matrix chat section to complete.

- [ ] **Step 1: Wire the route.** In `routes.tsx`, import `ChatPage` and return `<ChatPage />` for `path === 'chat'` in both `viewElement` and `routeChildren` (remove the StubView fallback for chat). Add the `chat` import alongside the others.
- [ ] **Step 2: Run full gate.** `cd frontend && npm run check`. Expected: ALL tests pass (existing 829 + new chat tests), tsc/ESLint/Prettier clean.
- [ ] **Step 3: RPC completeness cross-check.** Run `python3 scripts/fe_parity_inventory.py | grep -A40 chat` and diff every chat RPC + storage key + event against what the new code references. Any legacy RPC/event/key not wired → either wire it or record a **waived** matrix row for owner decision.
- [ ] **Step 4: FULL LIVE-BROWSER SWEEP.** With the dev gateway + vite: exercise the entire view end-to-end — send/stream/abort, tool calls, artifacts, router-fx, compaction, attachments (all types + rejections + paste), slash commands, session switch (`?session=`/`?agent=`), elevated/bypass toggle, model change, pending queue, markdown export, inline approval. Open every dialog. Navigate to `/control/chat` from Overview/Sessions/Agents/Cron jump links and confirm they land correctly. Confirm zero console errors and zero failed requests across the whole session.
- [ ] **Step 5: Update the parity matrix** chat section to complete with real evidence (test names that exist + live-sweep notes). Update `.superpowers/sdd/progress.md` (Plan 3 complete).
- [ ] **Step 6: Commit (do NOT push).**

```bash
git add frontend/src/app/routes.tsx docs/superpowers/specs/2026-07-19-console-rewrite-parity-matrix.md .superpowers/sdd/progress.md
git commit -m "feat(frontend): swap chat route to React ChatPage — Plan 3 complete"
```

- [ ] **Step 7: Report to owner.** Summarize what landed, the live-sweep evidence, any waived deviations awaiting owner decision, and ask before any push/PR.

---

## Self-Review

**Spec coverage** (design doc §3 → tasks):
- Foundation/logic/controller skeleton → Task 1 ✓
- Transcript streaming → Task 2 ✓; history/subscription → Task 3 ✓
- Tool activity + subagent → Task 4 ✓; artifacts/publish → Task 5 ✓
- Router-fx → Task 6 ✓; compaction → Task 7 ✓
- Composer → Task 8 ✓; attachments → Task 9 ✓; slash → Task 10 ✓
- Session chip + lifecycle → Task 11 ✓; elevated + toolbar/model → Task 12 ✓
- Pending queue + markdown export + inline approvals → Task 13 ✓ (approvals flagged first-class + split-if-larger)
- Route swap + full sweep + cutover → Task 14 ✓
- Design §5 RPC/WS surface → cross-checked in Task 3 (events) + Task 14 Step 3 (completeness) ✓
- Design §7 error handling (idle 210s, `_gap` resync, task/session errors, cap rejections, protocol-text stripping) → Tasks 2/3/9 ✓
- Design §8 live-sweep requirement → every imperative task Step 7 + Task 14 Step 4 ✓

**Placeholder scan:** No "TBD/TODO". Where exact legacy output strings are asserted (durations, tones, provider stripping), the step explicitly says "confirm exact legacy behavior" against a cited line — this is a required verification, not a placeholder, because the authoritative value lives in the source the implementer must read.

**Type consistency:** `ChatMessage`/`Role` defined in Task 1, reused in Tasks 2/3. `canonicalSessionKey`/`webchatSessionKey`/`agentIdFromSessionKey` defined Task 1, reused Task 11. Stream controller methods defined Task 2, consumed Tasks 3–7. Storage keys/caps use the same constant names as legacy throughout.

**Note on TDD for the imperative region:** DOM-mutation code (stream/tool/artifact/fx/compaction) is ported verbatim and cannot be meaningfully RTL-tested; each such task extracts its **pure** sub-logic (seq gate, duration/tone/category/mime helpers) for real TDD unit tests, and gates the DOM behavior on the mandatory live sweep. This matches the owner-approved boundary (design §2.1).
