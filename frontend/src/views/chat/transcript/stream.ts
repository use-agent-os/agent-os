// Chat transcript — imperative streaming renderer.
//
// This module is the OWNER-APPROVED imperative boundary of the chat-view
// migration (design §2.1): the transcript/streaming region is ported as
// near-verbatim imperative DOM code (innerHTML/appendChild/manual scroll)
// inside a ref container, NOT reactified. The legacy source is
// static/js/views/chat.js; each method below carries the cited legacy line
// range it was ported from. Module-level `let _foo` globals in the legacy IIFE
// become fields on the controller instance; functions that live OUTSIDE the
// ported ranges (Markdown, the strip helpers, _addMessage, router-fx, run-state
// sync, diagnostics, …) are injected as `deps` so later tasks can wire the real
// implementations — each has a safe default here so the controller stands
// alone.
//
// The one pure, timing-independent piece — the 800-event seq de-dup window —
// is extracted as `createSeqGate()` and unit-tested (stream.test.ts). The DOM
// mutation is verified by a live-browser sweep (parity matrix), not RTL.

import type { StreamEventPayload } from '../types'
import { createToolRenderer, type ToolRenderer } from './tools'
import { createArtifactRenderer, type Artifact, type ArtifactRenderer } from './artifacts'
import {
  createRouterFxRenderer,
  createRouterFxRegistry,
  routerFxLoadPref,
  routerFxSavePref,
  type RouterFxRenderer,
  type RouterFxRegistry,
  type RouterFxPref,
} from './routerFx'
import {
  createCompactionRenderer,
  type CompactionRenderer,
  type CompactionSummary,
} from './compaction'

/* ── Constants (ported verbatim from chat.js) ───────────────────────────── */

// chat.js:51 — server should emit a terminal event first; this is the backstop.
export const DEFAULT_STREAM_IDLE_TIMEOUT_MS = 210000
// chat.js:56 — bounded de-dup memory of recently-seen stream_seq values.
export const STREAM_SEQ_SEEN_WINDOW = 800
// chat.js:378-379 — thinking indicator show delay + auto-hide TTL.
export const THINKING_DELAY_MS = 400
export const THINKING_TTL_MS = 60000
// chat.js:381-382 — "Watching · N.Ns" verb cycle for the thinking indicator.
export const CAP_VERBS = [
  'Watching',
  'Tracking',
  'Sensing',
  'Pulsing',
  'Thinking',
  'Drafting',
  'Polishing',
] as const
export const CAP_DWELL_MS = 2500
// chat.js:45-47 — hint/reveal classes toggled on the streaming bubble.
export const AWAITING_MODEL_CLASS = 'awaiting-model'
export const STREAM_ACTIVE_MARK_CLASS = 'streaming-active-mark'
export const STREAM_ACTIVE_MARK_DELAY_MS = 3500
// chat.js:2577-2578 — remain pinned while the viewport is within 60px of the end.
export const AUTO_SCROLL_BOTTOM_GAP_PX = 60

/* ── Pure seq gate (ported verbatim from chat.js:1645-1682) ─────────────── */

/**
 * The per-session stream_seq de-duplication window. Ported verbatim from
 * legacy chat.js `_sessionStreamSeq` / `_setSessionStreamSeq` /
 * `_sessionStreamSeqSeen` / `_markSessionStreamSeqSeen` (chat.js:1645-1682),
 * which `_acceptStreamSeq` (chat.js:6345-6350) drives. Kept pure (no DOM, no
 * timers) so it is unit-testable in isolation — the sanctioned test surface for
 * this task.
 */
export function createSeqGate() {
  // chat.js:54-55 — one high-water number and one seen-Set per session key.
  const streamSeqBySession = new Map<string, number>()
  const streamSeqSeenBySession = new Map<string, Set<number>>()

  // chat.js:1645-1648
  function sessionStreamSeq(key: string): number {
    const stored = streamSeqBySession.get(key || '')
    return typeof stored === 'number' && Number.isFinite(stored) ? stored : 0
  }

  // chat.js:1650-1655 (the `_lastStreamSeq` mirror there is a live-view concern
  // handled by the controller, not the pure gate).
  function setSessionStreamSeq(key: string, seq: number): void {
    if (!key || typeof seq !== 'number' || !Number.isFinite(seq)) return
    const next = Math.max(sessionStreamSeq(key), seq)
    streamSeqBySession.set(key, next)
  }

  // chat.js:1657-1665
  function sessionStreamSeqSeen(key: string): Set<number> {
    const canonicalKey = key || ''
    let seen = streamSeqSeenBySession.get(canonicalKey)
    if (!seen) {
      seen = new Set<number>()
      streamSeqSeenBySession.set(canonicalKey, seen)
    }
    return seen
  }

  // chat.js:1667-1682 — the accept/de-dup decision + bounded pruning.
  function accept(key: string, seq: number): boolean {
    if (!key || typeof seq !== 'number' || !Number.isFinite(seq)) return true
    const seen = sessionStreamSeqSeen(key)
    if (seen.has(seq)) return false
    seen.add(seq)
    setSessionStreamSeq(key, seq)

    const highWater = sessionStreamSeq(key)
    const pruneBefore = highWater - STREAM_SEQ_SEEN_WINDOW
    if (seen.size > STREAM_SEQ_SEEN_WINDOW) {
      seen.forEach((value) => {
        if (value < pruneBefore) seen.delete(value)
      })
    }
    return true
  }

  return {
    accept,
    /** High-water accepted seq for a session (parity `_sessionStreamSeq`). */
    highWater: sessionStreamSeq,
  }
}

export type SeqGate = ReturnType<typeof createSeqGate>

/* ── Injected dependencies ──────────────────────────────────────────────── */

/** A parked text/tool segment inside the streaming bubble (chat.js:6647). */
interface StreamSegment {
  type: 'text' | string
  raw: string
  el: HTMLElement | null
}

/**
 * Parked live-stream state for a backgrounded session
 * (chat.js:6860-6876). Only the fields this task's methods read/write are
 * modelled; later tasks that own router-fx / user-anchor restore extend it.
 */
interface ParkedStreamState {
  isStreaming: boolean
  streamBubble: HTMLElement | null
  streamSessionKey: string
  streamRaw: string
  liveUserAnchor: HTMLElement | null
  segments: StreamSegment[]
  activeTextSeg: HTMLElement | null
  activeTextRaw: string
  streamArtifacts: Artifact[]
  lastVisibleStreamEvent: string
  routerStrips: HTMLElement[]
  streamGeneration: number
  autoScroll: boolean
  pendingFinalizedAssistantBubble: HTMLElement | null
  pendingFinalizedAssistantFallbackId: string
}

/**
 * The markdown renderer the flush path uses (legacy `Markdown`, markdown.js).
 * Injected so the real renderer can be wired by a later task; the default is a
 * text-only escape so the bubble still shows content standalone.
 */
export interface MarkdownDep {
  render(text: string): string
  bindCopy(el: HTMLElement): void
  bindHighlight?(el: HTMLElement): void
}

/**
 * Everything the ported streaming region references that lives OUTSIDE the
 * cited ranges. All optional: each has a faithful-enough default so the
 * controller is usable before later tasks land their real implementations.
 * Real wiring (router-fx, run-state, history sync, the export `messages`
 * array, diagnostics) is supplied by the transcript controller when available.
 */
export interface StreamControllerDeps {
  markdown?: MarkdownDep
  /** chat.js:419/387/391 — display-text sanitizers. Default: identity. */
  stripProtocolTextLeak?: (text: string) => string
  stripDirectiveTags?: (text: string) => string
  stripGeneratedArtifactMarkers?: (text: string) => string
  /** chat.js:661 — HTML-escape a string. Default: minimal entity escape. */
  esc?: (s: string) => string
  /** chat.js:673 — role → display label. Default: capitalized role. */
  displayRoleLabel?: (role: string) => string
  /** chat.js:7833/7840 — day separator key + human label. */
  dayKey?: (iso: string) => string
  dayLabel?: (dayKey: string) => string
  /** chat.js:7851 — append a plain (non-streaming) message row. */
  addMessage?: (role: string, text: string, timestamp?: string) => HTMLElement | null
  /** chat.js:5971 — stamp history identity onto a finalized bubble. */
  stampHistoryElement?: (
    el: HTMLElement,
    stableIdentity: string,
    role: string,
    text: string,
    transcriptId?: string | null,
    ts?: string | number | null,
  ) => void
  /** chat.js:6819/748 — attach the hover Copy/Regenerate toolbar. */
  attachHoverActions?: (el: HTMLElement, role: string) => void
  /** chat.js:6777 — remember the just-finalized assistant bubble for reconcile. */
  markPendingFinalizedAssistantBubble?: (el: HTMLElement, cleanedText: string) => void
  clearPendingFinalizedAssistantBubble?: () => void
  /** chat.js:6563/6230/6951 — session run-status sync (running / approval). */
  applySessionRunState?: (state: Record<string, unknown>) => void
  /** chat.js:6571 — refresh the Send/Stop button affordance. */
  updateSendButton?: () => void
  /** chat.js:6807 — record a finished assistant message for export. */
  pushMessage?: (message: Record<string, unknown>) => void
  /* ── router-fx (Task 6 — routerFx.ts, composed below) ─────────────────────
   * The controller composes `createRouterFxRenderer` and wires the stream
   * lifecycle's router-fx hooks (settle-for-output, cancel-scan, staticize,
   * pause/resume, live-strip/anchor lookups, dock predicate) to it. The
   * renderer's inputs are injected here so the chat config loader can feed the
   * SAME tier registry + visualisation pref the config-load path populates.
   * Sensible faithful defaults let the controller stand alone:
   *   - `routerFxRegistry`  — a fresh empty registry (config unknown → strips
   *      stay suppressed until config lands, matching legacy `configTiers===null`).
   *   - `routerFxPref`      — enabled by default, hydrated from localStorage
   *      (`agentos-router-fx`) via `routerFxLoadPref`.
   *   - `routerFeatureEnabled` — false until the operator routing flag is known.
   *   - `routerFxDock`      — legacy `_routerFxDock`: a DOM ELEMENT, not a flag.
   *      Default null (no mounted dock) → all strips suppressed,
   *      exactly as legacy `if (!_routerFxDock)` short-circuits. */
  routerFxRegistry?: RouterFxRegistry
  routerFxPref?: RouterFxPref
  routerFeatureEnabled?: () => boolean
  routerFxDock?: () => HTMLElement | null
  /** chat.js:3569 — await the router-fx config-ready gate. Default: resolve now. */
  routerFxAwaitConfig?: () => Promise<void>
  /** chat.js `_historyHasRendered` — has the history render completed. Default: true. */
  historyHasRendered?: () => boolean
  /** chat.js `_historyHydrating` — is the history render in progress. Default: false. */
  historyHydrating?: () => boolean
  // Stream-lifecycle router-fx hook overrides. Default to the composed
  // `routerFxRenderer` methods; kept overridable so a test / later task can
  // stub them without re-composing the renderer.
  routerFxSettleForOutput?: () => void
  cancelPendingRouterFxScan?: (reason: string) => void
  routerFxStaticizeCompletedStrips?: (key: string) => void
  currentSessionLiveRouterStrips?: (key: string) => HTMLElement[]
  currentSessionLiveUserAnchor?: (key: string) => HTMLElement | null
  routerFxPauseScanTimers?: (el: HTMLElement) => void
  routerFxResumeLiveStrip?: (el: HTMLElement) => void
  insertLiveRouterStripForAnchor?: (
    el: HTMLElement,
    anchor: HTMLElement | null,
    bubble: HTMLElement | null,
  ) => void
  /* ── compaction (Task 7 — compaction.ts, composed below) ──────────────────
   * The controller composes `createCompactionRenderer` and OWNS the
   * compaction-in-flight state (chat.js:8654-8663) — so `isCompactInFlight-
   * ForCurrentSession` is now sourced from the compaction renderer, not this
   * dep — and wires the compaction toast to router-fx suppression. The renderer
   * reads the history summary array + pending-queue helpers that live outside
   * the streaming range; those are injected here. */
  /** chat.js `_historyCompactionSummaries` — history summary rows, read live.
   *  Default: none (the history renderer feeds the real array). */
  getHistoryCompactionSummaries?: () => CompactionSummary[]
  /** chat.js:5305 `_scheduleHistorySync` — debounced history refresh. */
  scheduleHistorySync?: () => void
  /** chat.js:8644 `_schedulePendingDrainAfterTerminal`. Default: no-op. */
  schedulePendingDrainAfterTerminal?: () => void
  /** chat.js:8596 `_popAllPendingIntoComposer`. Default: false. */
  popAllPendingIntoComposer?: () => boolean
  /** chat.js `_pendingQueue.length`. Default: 0. */
  pendingQueueLength?: () => number
  /** chat.js:6412 — true while any router strip is still scanning. */
  routerScanActive?: () => boolean
  /** chat.js:6729/6879 — the debounced history-sync timer handle to cancel. */
  clearHistorySyncTimer?: () => void
  /** chat.js:* — the shared per-session parked-state map (chat.js:57). */
  liveStreamStateBySession?: Map<string, ParkedStreamState>
  /** The active session key (legacy `_sessionKey`), read live. */
  getSessionKey?: () => string
  /**
   * chat.js `App.getAuthToken()` — the connection auth token, appended to
   * artifact preview/download URLs (chat.js:7575/7599/7657). Default: "".
   */
  getAuthToken?: () => string
  /**
   * chat.js `UI.toast` — a transient toast surface (used by the artifact
   * download-failure path, chat.js:7667). Owned by the UI/toast surface (a later
   * task); default no-op so a failed download degrades silently.
   */
  toast?: (message: string, kind?: string, durationMs?: number) => void
  /**
   * chat.js:6652 — the abort flag (`_aborted`, set in `_onSend`/ESC). When true,
   * `appendDelta` drops incoming deltas. Owned by the send/abort flow (a later
   * task); default `false` so streaming is never spuriously suppressed here.
   */
  isAborted?: () => boolean
  /** chat.js:* — the diagnostics ring (legacy `_chatDiag`). Default: no-op. */
  diag?: (event: string, detail: Record<string, unknown>) => void
  /**
   * chat.js:7311 `UI.modal` — open the "View full" tool-result modal. Owned by
   * the UI/toast surface (a later task); default no-op so the tool card renders
   * standalone (the "View full" button is inert until wired).
   */
  openModal?: (title: string, html: string, buttons: Array<Record<string, unknown>>) => void
  /**
   * chat.js:7814 `_addMessage` with provenance options — append the subagent
   * completion system row. Distinct from `addMessage` above (which is the
   * streaming timeout/error 3-arg form); this is the 4-arg provenance form the
   * subagent path needs. Default: no-op (the real builder is a later task).
   */
  addMessageWithOptions?: (
    role: string,
    text: string,
    timestamp: number,
    options: Record<string, unknown>,
  ) => HTMLElement | null
}

/* ── Default dep implementations ────────────────────────────────────────── */

const defaultEsc = (s: string): string =>
  String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')

const defaultMarkdown: MarkdownDep = {
  // Text-only fallback for standalone controller consumers. The chat view
  // injects its sanitized marked/DOMPurify renderer through this seam.
  render: (text: string): string => defaultEsc(text).replace(/\n/g, '<br>'),
  bindCopy: (): void => {},
  bindHighlight: (): void => {},
}

/* ── Controller ─────────────────────────────────────────────────────────── */

/**
 * Create the imperative streaming controller bound to a scroll-container ref.
 * The methods are the legacy streaming functions with module-globals rebound to
 * fields on the returned closure state. `containerRef.current` is legacy
 * `_thread`.
 */
export function createStreamController(
  containerRef: React.RefObject<HTMLElement | null>,
  deps: StreamControllerDeps = {},
) {
  /* ── injected deps with defaults ──────────────────────────────────────── */
  const Markdown = deps.markdown ?? defaultMarkdown
  const stripProtocolTextLeak = deps.stripProtocolTextLeak ?? ((t: string) => t)
  const stripDirectiveTags = deps.stripDirectiveTags ?? ((t: string) => t)
  const stripGeneratedArtifactMarkers = deps.stripGeneratedArtifactMarkers ?? ((t: string) => t)
  const esc = deps.esc ?? defaultEsc
  const displayRoleLabel =
    deps.displayRoleLabel ??
    ((role: string) => (role ? role.charAt(0).toUpperCase() + role.slice(1) : ''))
  const dayKey = deps.dayKey ?? (() => '')
  const dayLabel = deps.dayLabel ?? ((k: string) => k)
  const addMessage = deps.addMessage ?? (() => null)
  const stampHistoryElement = deps.stampHistoryElement ?? (() => {})
  const attachHoverActions = deps.attachHoverActions ?? (() => {})
  const markPendingFinalizedAssistantBubble = deps.markPendingFinalizedAssistantBubble ?? (() => {})
  const clearPendingFinalizedAssistantBubble =
    deps.clearPendingFinalizedAssistantBubble ?? (() => {})
  const applySessionRunState = deps.applySessionRunState ?? (() => {})
  const updateSendButton = deps.updateSendButton ?? (() => {})
  const pushMessage = deps.pushMessage ?? (() => {})
  // Compaction-in-flight ownership moved to the composed compaction renderer
  // (Task 7, created below). Router-fx + the thinking indicator gate on it, but
  // only ever CALL it (never at composition time), so a late-bound delegate is
  // safe: it resolves to `compactionRenderer.isCompactInFlightForCurrentSession`
  // once that is assigned, and is a no-op (false) before then.
  let compactionRenderer: CompactionRenderer | null = null
  const isCompactInFlightForCurrentSession = (): boolean =>
    compactionRenderer ? compactionRenderer.isCompactInFlightForCurrentSession() : false
  const clearHistorySyncTimer = deps.clearHistorySyncTimer ?? (() => {})
  const liveStreamStateBySession =
    deps.liveStreamStateBySession ?? new Map<string, ParkedStreamState>()
  const getSessionKey = deps.getSessionKey ?? (() => '')
  const getAuthToken = deps.getAuthToken ?? (() => '')
  const toast = deps.toast ?? (() => {})
  const isAborted = deps.isAborted ?? (() => false)
  const diag = deps.diag ?? (() => {})
  const openModal = deps.openModal ?? (() => {})
  const addMessageWithOptions = deps.addMessageWithOptions ?? (() => null)

  const thread = (): HTMLElement | null => containerRef.current

  /* ── router-fx renderer (Task 6 — routerFx.ts) ────────────────────────────
   * Composed exactly how the tool/artifact renderers are: the controller owns
   * the tier registry + visualisation pref (so the config loader can feed the
   * same instances later) and wires the stream lifecycle's router-fx hooks to
   * the renderer's methods. `scrollToBottom` below is a hoisted `function`, safe
   * to reference from here. */
  const routerFxRegistry = deps.routerFxRegistry ?? createRouterFxRegistry()
  const routerFxPref = deps.routerFxPref ?? { enabled: true, variant: 'default' }
  if (!deps.routerFxPref) routerFxLoadPref(routerFxPref)
  const routerFxRenderer: RouterFxRenderer = createRouterFxRenderer({
    thread,
    dock: deps.routerFxDock ?? (() => null),
    getSessionKey: () => getSessionKey() || '',
    registry: routerFxRegistry,
    pref: routerFxPref,
    routerFeatureEnabled: deps.routerFeatureEnabled ?? (() => false),
    esc,
    scrollToBottom: () => scrollToBottom(),
    isCompactInFlightForCurrentSession,
    historyHasRendered: deps.historyHasRendered ?? (() => true),
    historyHydrating: deps.historyHydrating ?? (() => false),
    awaitConfig: deps.routerFxAwaitConfig ?? (() => Promise.resolve()),
    diag,
  })

  // Stream-lifecycle router-fx hooks now route to the composed renderer
  // (chat.js:6585/6716/6907/…). Overridable, but the renderer is the default.
  const routerFxSettleForOutput =
    deps.routerFxSettleForOutput ?? (() => routerFxRenderer.settleForOutput())
  const cancelPendingRouterFxScan =
    deps.cancelPendingRouterFxScan ??
    ((reason: string) => routerFxRenderer.cancelPendingRouterFxScan(reason))
  const routerFxStaticizeCompletedStrips =
    deps.routerFxStaticizeCompletedStrips ??
    ((key: string) => routerFxRenderer.staticizeCompletedStrips(key))
  const currentSessionLiveRouterStrips =
    deps.currentSessionLiveRouterStrips ??
    ((key: string) => routerFxRenderer.currentSessionLiveRouterStrips(key))
  // The parked live-user anchor is a send-flow concept (a later task); the
  // router-fx engine does not track it, so this stays null until wired.
  const currentSessionLiveUserAnchor = deps.currentSessionLiveUserAnchor ?? (() => null)
  const routerFxPauseScanTimers =
    deps.routerFxPauseScanTimers ?? ((el: HTMLElement) => routerFxRenderer.pauseScanTimers(el))
  const routerFxResumeLiveStrip =
    deps.routerFxResumeLiveStrip ?? ((el: HTMLElement) => routerFxRenderer.resumeLiveStrip(el))
  const insertLiveRouterStripForAnchor =
    deps.insertLiveRouterStripForAnchor ??
    ((el: HTMLElement) => routerFxRenderer.insertLiveRouterStripForAnchor(el))
  // Legacy `if (_routerFxDock)` tests the dock ELEMENT's presence (chat.js:6940).
  const routerFxDock = () => routerFxRenderer.hasDock()
  const routerScanActive =
    deps.routerScanActive ??
    (() => routerFxRenderer.strips('.router-fx[data-scanning="true"]').length > 0)

  /* ── instance fields (legacy module-globals) ──────────────────────────── */
  const seqGate = createSeqGate()

  // chat.js:40-41 idle timer, chat.js:42 approval-pause flags
  let _streamIdleTimer: ReturnType<typeof setTimeout> | null = null
  let _streamIdlePausedForApproval = false
  let _approvalPendingForCurrentSession = false
  let _streamIdleTimeoutMs = DEFAULT_STREAM_IDLE_TIMEOUT_MS

  // chat.js stream lifecycle state
  let _isStreaming = false
  let _streamSessionKey = ''
  let _streamGeneration = 0
  let _streamRaw = ''
  let _segments: StreamSegment[] = []
  let _activeTextSeg: HTMLElement | null = null
  let _activeTextRaw = ''
  let _streamArtifacts: Artifact[] = []
  let _lastVisibleStreamEvent = ''
  let _streamBubble: HTMLElement | null = null
  let _autoScroll = true

  // chat.js:443 — the sticky web_search provider label for the running/settled
  // tool-card badge. Owned here so the tool renderer and the streaming path
  // share one value across a turn.
  let _searchProvider = ''

  // chat.js:436-437 render debouncing
  let _renderDirty = false
  let _renderRafId: number | null = null

  // chat.js:48-49 stream-active reveal window
  let _streamActiveMarkTimer: ReturnType<typeof setTimeout> | null = null
  let _streamActiveMarkVisibleStartedAt = 0

  // thinking indicator (chat.js:6379-6501)
  let _thinkingEl: HTMLElement | null = null
  let _thinkingDelayTimer: ReturnType<typeof setTimeout> | null = null
  let _thinkingTimerInterval: ReturnType<typeof setInterval> | null = null
  let _thinkingStartTime = 0

  // header dedup (chat.js:6601/6620) — private to this controller.
  let _lastHeaderRole = ''
  let _lastHeaderDay = ''

  // finalize/reconcile bookkeeping (chat.js:59-60)
  let _pendingFinalizedAssistantBubble: HTMLElement | null = null
  let _pendingFinalizedAssistantFallbackId = ''

  /* ── seq accept (chat.js:6345-6350) ───────────────────────────────────── */

  function sessionKeyFromPayload(payload: StreamEventPayload | undefined): string {
    // chat.js:1641-1643
    const p = payload as { key?: string; session_key?: string; sessionKey?: string } | undefined
    return p?.key || p?.session_key || p?.sessionKey || ''
  }

  function acceptStreamSeq(payload: StreamEventPayload): boolean {
    // chat.js:6345-6350
    const seq = (payload as { stream_seq?: unknown })?.stream_seq
    if (typeof seq !== 'number' || !Number.isFinite(seq)) return true
    const key = sessionKeyFromPayload(payload) || getSessionKey() || ''
    return seqGate.accept(key, seq)
  }

  /* ── idle timer (chat.js:6209-6245) ───────────────────────────────────── */

  function clearStreamIdleTimer(): void {
    // chat.js:6209-6214
    if (_streamIdleTimer) {
      clearTimeout(_streamIdleTimer)
      _streamIdleTimer = null
    }
  }

  function resetStreamIdleTimer(): void {
    // chat.js:6235-6245
    clearStreamIdleTimer()
    if (!_isStreaming || _streamIdlePausedForApproval) return
    _streamIdleTimer = setTimeout(() => {
      if (_isStreaming && !_streamIdlePausedForApproval) {
        endStreaming()
        const seconds = Math.round(_streamIdleTimeoutMs / 1000)
        addMessage('error', `Response timed out — no events received for ${seconds}s`)
      }
    }, _streamIdleTimeoutMs)
  }

  function setStreamIdlePausedForApproval(paused: boolean): void {
    // chat.js:6216-6233
    const nextPaused = !!paused
    const changed = _approvalPendingForCurrentSession !== nextPaused
    _streamIdlePausedForApproval = nextPaused
    _approvalPendingForCurrentSession = nextPaused
    if (_streamIdlePausedForApproval) {
      clearStreamIdleTimer()
      if (changed || _isStreaming) {
        applySessionRunState({
          run_status: 'approval_pending',
          active_task: { status: 'approval_pending', terminal_reason: 'tool_approval' },
        })
      }
    } else if (_isStreaming) {
      applySessionRunState({ run_status: 'running', active_task: { status: 'running' } })
      resetStreamIdleTimer()
    }
  }

  function applyRpcPolicy(
    policy: { webui_stream_idle_grace_ms?: unknown } | null | undefined,
  ): void {
    // chat.js:6247-6254
    const raw = policy && policy.webui_stream_idle_grace_ms
    if (typeof raw === 'number' && Number.isFinite(raw) && raw > 0) {
      _streamIdleTimeoutMs = raw
    } else {
      _streamIdleTimeoutMs = DEFAULT_STREAM_IDLE_TIMEOUT_MS
    }
  }

  /* ── thinking indicator (chat.js:6379-6501) ───────────────────────────── */

  function showThinkingIndicator(): void {
    // chat.js:6379-6395
    if (_thinkingEl || _thinkingDelayTimer) return
    if (isCompactInFlightForCurrentSession()) {
      diag('thinking.skip.compaction_in_flight', {})
      return
    }
    _thinkingStartTime = Date.now()
    _thinkingDelayTimer = setTimeout(showThinkingIndicatorNow, THINKING_DELAY_MS)
  }

  function showThinkingIndicatorNow(): void {
    // chat.js:6397-6484
    _thinkingDelayTimer = null
    if (_streamBubble) {
      diag('thinking.skip.stream_bubble', {})
      return // content already arrived, skip
    }
    if (isCompactInFlightForCurrentSession()) {
      diag('thinking.defer.compaction_in_flight', {})
      _thinkingDelayTimer = setTimeout(showThinkingIndicatorNow, 150)
      return
    }
    if (routerScanActive()) {
      diag('thinking.defer.router_scan', {})
      _thinkingDelayTimer = setTimeout(showThinkingIndicatorNow, 150)
      return
    }

    const th = thread()
    if (!th) return
    const empty = th.querySelector('.chat-empty')
    if (empty) empty.remove()

    _thinkingEl = document.createElement('div')
    _thinkingEl.className = 'msg assistant thinking'
    _thinkingEl.setAttribute('role', 'status')
    _thinkingEl.setAttribute('aria-live', 'polite')
    _thinkingEl.dataset.sessionKey = _streamSessionKey || getSessionKey() || ''

    if (_lastHeaderRole !== 'assistant') {
      const header = document.createElement('div')
      header.className = 'msg-header'
      const roleLabel = document.createElement('span')
      roleLabel.className = 'role-label'
      roleLabel.textContent = displayRoleLabel('assistant')
      header.appendChild(roleLabel)
      _thinkingEl.appendChild(header)
    }

    const body = document.createElement('div')
    body.className = 'msg-body thinking-body'
    const status = document.createElement('div')
    status.className = 'thinking-status'

    const glyph = document.createElement('span')
    glyph.className = 'thinking-glyph'
    glyph.setAttribute('aria-hidden', 'true')
    glyph.textContent = '▸'

    const dots = document.createElement('div')
    dots.className = 'typing-indicator'
    for (let i = 0; i < 3; i++) {
      const dot = document.createElement('span')
      dot.className = 'dot'
      dots.appendChild(dot)
    }

    const elapsed = document.createElement('span')
    elapsed.className = 'thinking-elapsed'
    elapsed.setAttribute('aria-live', 'off')
    const elapsedMs = Date.now() - _thinkingStartTime
    const seconds = Math.floor(elapsedMs / 1000)
    const verb = CAP_VERBS[Math.floor(elapsedMs / CAP_DWELL_MS) % CAP_VERBS.length]
    elapsed.textContent = `${verb} (${seconds}s)`

    const agent = document.createElement('span')
    agent.className = 'thinking-agent'
    agent.textContent = `${displayRoleLabel('assistant')} is thinking`

    status.appendChild(glyph)
    status.appendChild(dots)
    status.appendChild(agent)
    status.appendChild(elapsed)
    body.appendChild(status)
    _thinkingEl.appendChild(body)
    th.appendChild(_thinkingEl)
    diag('thinking.show', {})
    if (_autoScroll) scrollToBottom()

    _thinkingTimerInterval = setInterval(() => {
      if (!_thinkingEl) {
        if (_thinkingTimerInterval) clearInterval(_thinkingTimerInterval)
        return
      }
      const eMs = Date.now() - _thinkingStartTime
      const s = Math.floor(eMs / 1000)
      const v = CAP_VERBS[Math.floor(eMs / CAP_DWELL_MS) % CAP_VERBS.length]
      const label = _thinkingEl.querySelector('.thinking-elapsed')
      if (label) label.textContent = `${v} (${s}s)`

      if (s >= THINKING_TTL_MS / 1000) {
        hideThinkingIndicator()
        addMessage('system', 'Still waiting for agent response…')
      }
    }, 1000)
  }

  function hideThinkingIndicator(): void {
    // chat.js:6486-6501
    const hadThinking = !!_thinkingEl || !!_thinkingDelayTimer || !!_thinkingTimerInterval
    if (_thinkingDelayTimer) {
      clearTimeout(_thinkingDelayTimer)
      _thinkingDelayTimer = null
    }
    if (_thinkingTimerInterval) {
      clearInterval(_thinkingTimerInterval)
      _thinkingTimerInterval = null
    }
    if (_thinkingEl) {
      _thinkingEl.remove()
      _thinkingEl = null
    }
    if (hadThinking) diag('thinking.hide', {})
  }

  /* ── awaiting-model hint + stream-active reveal (chat.js:6503-6551) ────── */

  function clearAwaitingModelHint(): void {
    // chat.js:6503-6505
    if (_streamBubble) _streamBubble.classList.remove(AWAITING_MODEL_CLASS)
  }

  function markVisibleStreamEvent(kind: string): void {
    // chat.js:6507-6510
    _lastVisibleStreamEvent = kind || ''
    if (_lastVisibleStreamEvent !== 'tool_result') clearAwaitingModelHint()
  }

  function showAwaitingModelHintAfterToolResult(): boolean {
    // chat.js:6512-6519
    if (!_streamBubble || _lastVisibleStreamEvent !== 'tool_result') return false
    if (!_streamBubble.classList.contains(AWAITING_MODEL_CLASS)) {
      _streamBubble.classList.add(AWAITING_MODEL_CLASS)
      if (_autoScroll) scrollToBottom()
    }
    return true
  }

  function clearStreamActiveMarkReveal(): void {
    // chat.js:6521-6528
    if (_streamActiveMarkTimer) {
      clearTimeout(_streamActiveMarkTimer)
      _streamActiveMarkTimer = null
    }
    _streamActiveMarkVisibleStartedAt = 0
    if (_streamBubble) _streamBubble.classList.remove(STREAM_ACTIVE_MARK_CLASS)
  }

  function beginStreamActiveMarkRevealWindow(): void {
    // chat.js:6530-6533
    _streamActiveMarkVisibleStartedAt = Date.now()
    scheduleStreamActiveMarkReveal()
  }

  function maybeRevealStreamActiveMark(): boolean {
    // chat.js:6535-6541
    if (!_isStreaming || !_streamBubble) return false
    const elapsedMs = _streamActiveMarkVisibleStartedAt
      ? Date.now() - _streamActiveMarkVisibleStartedAt
      : 0
    if (elapsedMs < STREAM_ACTIVE_MARK_DELAY_MS) return false
    _streamBubble.classList.add(STREAM_ACTIVE_MARK_CLASS)
    return true
  }

  function scheduleStreamActiveMarkReveal(): void {
    // chat.js:6543-6551
    if (_streamActiveMarkTimer) clearTimeout(_streamActiveMarkTimer)
    const generation = _streamGeneration
    _streamActiveMarkTimer = setTimeout(() => {
      _streamActiveMarkTimer = null
      if (_streamGeneration !== generation) return
      maybeRevealStreamActiveMark()
    }, STREAM_ACTIVE_MARK_DELAY_MS)
  }

  /* ── stream lifecycle (chat.js:6553-6835) ─────────────────────────────── */

  function startStreaming(): void {
    // chat.js:6553-6574
    diag('stream.start.before', {
      wasStreaming: _isStreaming,
      hadStreamBubble: !!_streamBubble,
      streamRawLen: _streamRaw.length,
    })
    _isStreaming = true
    _streamSessionKey = getSessionKey() || ''
    if (_streamSessionKey) liveStreamStateBySession.delete(_streamSessionKey)
    _streamGeneration += 1
    applySessionRunState({ run_status: 'running', active_task: { status: 'running' } })
    _streamRaw = ''
    _segments = []
    _activeTextSeg = null
    _activeTextRaw = ''
    _streamArtifacts = []
    _lastVisibleStreamEvent = ''
    _streamBubble = null
    _autoScroll = true
    const th = thread()
    if (th) th.setAttribute('aria-busy', 'true')
    updateSendButton()
    resetStreamIdleTimer()
    diag('stream.start.after', {})
  }

  function ensureStreamBubble(): HTMLElement {
    // chat.js:6576-6636
    diag('stream.ensure.start', {
      hadStreamBubble: !!_streamBubble,
      streamRawLen: _streamRaw.length,
      activeTextRawLen: _activeTextRaw.length,
    })
    hideThinkingIndicator()
    routerFxSettleForOutput()
    if (!_streamBubble) {
      const th = thread()
      const empty = th?.querySelector('.chat-empty')
      if (empty) empty.remove()

      _streamBubble = document.createElement('div')
      _streamBubble.className = 'msg assistant streaming'
      _streamBubble.setAttribute('data-history-role', 'assistant')
      _streamBubble.setAttribute('aria-live', 'polite')
      _streamBubble.dataset.sessionKey = _streamSessionKey || getSessionKey() || ''
      _streamBubble.dataset.streamSessionKey = _streamSessionKey || getSessionKey() || ''

      // Day separator for streaming bubbles (use current time as timestamp)
      const now = new Date().toISOString()
      const day = dayKey(now)
      if (day && day !== _lastHeaderDay && th) {
        const sep = document.createElement('div')
        sep.className = 'chat-day-sep'
        sep.innerHTML = `<span>${dayLabel(day)}</span>`
        th.insertBefore(sep, null)
        _lastHeaderDay = day
        _lastHeaderRole = ''
      }

      const sameGroup = _lastHeaderRole === 'assistant'
      if (!sameGroup) {
        _streamBubble.innerHTML = `
          <div class="msg-header">
            <span class="role-label">${esc(displayRoleLabel('assistant'))}</span>
            <span class="savings-indicator"></span>
            <span class="msg-time"></span>
          </div>
          <div class="msg-body"></div>`
        _lastHeaderRole = 'assistant'
      } else {
        _streamBubble.innerHTML = `<div class="msg-body"></div>`
      }

      if (th) th.appendChild(_streamBubble)
      beginStreamActiveMarkRevealWindow()

      newTextSegment()
      diag('stream.bubble.created', {})
    }
    maybeRevealStreamActiveMark()
    return _streamBubble
  }

  function newTextSegment(): HTMLElement {
    // chat.js:6639-6649
    const bubble = _streamBubble as HTMLElement
    const body = bubble.querySelector('.msg-body') as HTMLElement
    const seg = document.createElement('div')
    seg.className = 'msg-text-seg'
    seg.setAttribute('data-seg', String(_segments.length))
    body.appendChild(seg)
    _activeTextSeg = seg
    _activeTextRaw = ''
    _segments.push({ type: 'text', raw: '', el: seg })
    return seg
  }

  function appendDelta(text: string): void {
    // chat.js:6651-6682
    if (isAborted()) return // chat.js:6652
    diag('stream.delta.start', {
      len: text ? text.length : 0,
      wasStreaming: _isStreaming,
      hasStreamBubble: !!_streamBubble,
    })
    if (!_isStreaming) startStreaming()
    ensureStreamBubble()
    markVisibleStreamEvent('text_delta')
    _streamRaw += text
    _activeTextRaw += text
    const lastSeg = _segments[_segments.length - 1]
    if (lastSeg && lastSeg.type === 'text') lastSeg.raw = _activeTextRaw

    // First delta: render immediately; subsequent deltas batch via rAF.
    if (!_renderRafId && _activeTextRaw.length === text.length) {
      _renderDirty = true
      flushRender()
    } else {
      _renderDirty = true
      if (!_renderRafId) {
        _renderRafId = requestAnimationFrame(flushRender)
      }
    }
    diag('stream.delta.queued', {
      streamRawLen: _streamRaw.length,
      activeTextRawLen: _activeTextRaw.length,
    })
  }

  function flushPendingTextSegment(): void {
    // chat.js:6684-6691
    if (!_renderDirty) return
    if (_renderRafId) {
      cancelAnimationFrame(_renderRafId)
      _renderRafId = null
    }
    flushRender()
  }

  function flushRender(): void {
    // chat.js:6693-6714
    _renderRafId = null
    if (!_renderDirty || !_streamBubble) {
      diag('stream.flush.skip', {
        renderDirty: !!_renderDirty,
        hasStreamBubble: !!_streamBubble,
      })
      _renderDirty = false
      return
    }
    if (_activeTextSeg && _activeTextRaw) {
      _activeTextSeg.innerHTML = Markdown.render(
        stripProtocolTextLeak(stripDirectiveTags(stripGeneratedArtifactMarkers(_activeTextRaw))),
      )
      Markdown.bindCopy(_activeTextSeg)
      Markdown.bindHighlight?.(_activeTextSeg)
    }
    _renderDirty = false
    if (_autoScroll) scrollToBottom()
    diag('stream.flush.done', {
      streamRawLen: _streamRaw.length,
      activeTextRawLen: _activeTextRaw.length,
    })
  }

  function endStreaming(opts?: { reason?: string }): void {
    // chat.js:6716-6835
    const reason = opts && opts.reason
    const wasAborted = reason === 'aborted'
    diag('stream.end.start', {
      reason: reason || '',
      wasAborted,
      hasStreamBubble: !!_streamBubble,
      streamRawLen: _streamRaw.length,
    })
    hideThinkingIndicator()
    cancelPendingRouterFxScan('stream_end')
    clearAwaitingModelHint()
    _lastVisibleStreamEvent = ''
    clearHistorySyncTimer()
    if (_renderRafId) {
      cancelAnimationFrame(_renderRafId)
      _renderRafId = null
    }
    _renderDirty = false
    clearStreamIdleTimer()
    clearStreamActiveMarkReveal()
    _streamIdlePausedForApproval = false
    _approvalPendingForCurrentSession = false
    const th = thread()
    if (_streamBubble) {
      _streamBubble.classList.remove('streaming')
      const cleanedText = stripProtocolTextLeak(
        stripDirectiveTags(stripGeneratedArtifactMarkers(_streamRaw)),
      ).trim()

      const SENTINELS = ['NO_REPLY', 'HEARTBEAT_OK']
      if (!wasAborted && SENTINELS.includes(cleanedText)) {
        diag('stream.end.remove.sentinel', { cleanedText })
        _streamBubble.remove()
        _streamBubble = null
        _isStreaming = false
        if (_streamSessionKey) liveStreamStateBySession.delete(_streamSessionKey)
        _streamSessionKey = ''
        _streamRaw = ''
        _segments = []
        _activeTextSeg = null
        _activeTextRaw = ''
        _streamArtifacts = []
        updateSendButton()
        return
      }

      if (wasAborted && !cleanedText) {
        diag('stream.end.remove.aborted_empty', {})
        _streamBubble.remove()
        _streamBubble = null
        _isStreaming = false
        if (_streamSessionKey) liveStreamStateBySession.delete(_streamSessionKey)
        _streamSessionKey = ''
        _streamRaw = ''
        _segments = []
        _activeTextSeg = null
        _activeTextRaw = ''
        _streamArtifacts = []
        if (th) th.setAttribute('aria-busy', 'false')
        updateSendButton()
        return
      }
      stampHistoryElement(_streamBubble, '', 'assistant', cleanedText)
      markPendingFinalizedAssistantBubble(_streamBubble, cleanedText)

      // Final render: render each text segment with its own content.
      for (const seg of _segments) {
        if (seg.type !== 'text' || !seg.el) continue
        const segText = stripProtocolTextLeak(
          stripDirectiveTags(stripGeneratedArtifactMarkers(seg.raw)),
        ).trim()
        if (segText) {
          seg.el.innerHTML = Markdown.render(segText)
          Markdown.bindCopy(seg.el)
          Markdown.bindHighlight?.(seg.el)
        } else {
          seg.el.remove()
        }
      }

      const body = _streamBubble.querySelector('.msg-body') as HTMLElement | null
      if (wasAborted && body && !body.querySelector('.msg-interrupt-mark')) {
        const mark = document.createElement('span')
        mark.className = 'msg-interrupt-mark'
        mark.textContent = 'interrupted'
        body.appendChild(mark)
      }

      pushMessage({
        role: 'assistant',
        text: cleanedText,
        ts: new Date().toISOString(),
        artifacts: _streamArtifacts.slice(),
        ...(wasAborted ? { interrupted: true } : {}),
      })

      if (body)
        body
          .querySelectorAll('.chat-tools-collapse--running')
          .forEach((el) => el.classList.remove('chat-tools-collapse--running'))

      attachHoverActions(_streamBubble, 'assistant')
    }
    _isStreaming = false
    routerFxStaticizeCompletedStrips(_streamSessionKey || getSessionKey() || '')
    if (_streamSessionKey) liveStreamStateBySession.delete(_streamSessionKey)
    _streamBubble = null
    _streamSessionKey = ''
    _streamRaw = ''
    _segments = []
    _activeTextSeg = null
    _activeTextRaw = ''
    _streamArtifacts = []
    if (th) th.setAttribute('aria-busy', 'false')
    updateSendButton()
    diag('stream.end.done', { reason: reason || '', wasAborted })
  }

  /* ── reconcile final stream text (chat.js:6021-6058) ──────────────────── */

  function replaceStreamText(finalText: string): void {
    // chat.js:6023-6044
    if (!_isStreaming) startStreaming()
    ensureStreamBubble()
    markVisibleStreamEvent('text_delta')
    if (!_streamBubble) {
      _streamRaw = finalText
      return
    }
    const body = _streamBubble.querySelector('.msg-body') as HTMLElement | null
    if (body) body.innerHTML = ''
    _streamRaw = finalText
    _segments = []
    _activeTextSeg = null
    _activeTextRaw = ''
    newTextSegment()
    _activeTextRaw = finalText
    const lastSeg = _segments[_segments.length - 1]
    if (lastSeg && lastSeg.type === 'text') lastSeg.raw = finalText
    _renderDirty = true
    flushRender()
    artifactRenderer.renderStreamArtifacts()
  }

  function reconcileFinalStreamText(finalText: string): void {
    // chat.js:6046-6058
    if (!finalText || finalText === _streamRaw) return
    if (_streamRaw && finalText.startsWith(_streamRaw)) {
      appendDelta(finalText.slice(_streamRaw.length))
      return
    }
    const textOnly = _segments.every((seg) => seg.type === 'text')
    if (!_streamRaw || textOnly) {
      replaceStreamText(finalText)
      return
    }
    _streamRaw = finalText
  }

  /* ── park / restore / clear view-local state (chat.js:6837-7001) ───────── */

  function hasViewLocalStreamState(): boolean {
    // chat.js:6837-6849
    return !!(
      _isStreaming ||
      _streamBubble ||
      _streamRaw ||
      _segments.length ||
      _activeTextRaw ||
      _streamArtifacts.length ||
      currentSessionLiveRouterStrips(_streamSessionKey || getSessionKey() || '').length ||
      _thinkingEl ||
      _thinkingDelayTimer
    )
  }

  function parkCurrentSessionStreamState(reason: string): boolean {
    // chat.js:6851-6909
    const key = _streamSessionKey || getSessionKey() || ''
    const routerStrips = currentSessionLiveRouterStrips(key)
    const liveUserAnchor = currentSessionLiveUserAnchor(key)
    if (!key || !hasViewLocalStreamState()) {
      clearViewLocalStreamState(reason)
      return false
    }
    flushPendingTextSegment()
    const state: ParkedStreamState = {
      isStreaming: _isStreaming,
      streamBubble: _streamBubble,
      streamSessionKey: key,
      streamRaw: _streamRaw,
      liveUserAnchor,
      segments: _segments,
      activeTextSeg: _activeTextSeg,
      activeTextRaw: _activeTextRaw,
      streamArtifacts: _streamArtifacts.slice(),
      lastVisibleStreamEvent: _lastVisibleStreamEvent,
      routerStrips,
      streamGeneration: _streamGeneration,
      autoScroll: _autoScroll,
      pendingFinalizedAssistantBubble: _pendingFinalizedAssistantBubble,
      pendingFinalizedAssistantFallbackId: _pendingFinalizedAssistantFallbackId,
    }
    liveStreamStateBySession.set(key, state)
    hideThinkingIndicator()
    clearHistorySyncTimer()
    if (_renderRafId) {
      cancelAnimationFrame(_renderRafId)
      _renderRafId = null
    }
    _renderDirty = false
    clearStreamIdleTimer()
    _streamIdlePausedForApproval = false
    _approvalPendingForCurrentSession = false
    if (_streamBubble) _streamBubble.remove()
    routerStrips.forEach((el) => {
      routerFxPauseScanTimers(el)
      if (el.parentNode) el.remove()
    })
    if (liveUserAnchor && liveUserAnchor.parentNode) liveUserAnchor.remove()
    _isStreaming = false
    _streamBubble = null
    _streamSessionKey = ''
    _streamRaw = ''
    _segments = []
    _activeTextSeg = null
    _activeTextRaw = ''
    _streamArtifacts = []
    _lastVisibleStreamEvent = ''
    const th = thread()
    if (th) th.setAttribute('aria-busy', 'false')
    updateSendButton()
    diag('stream.view_state_parked', {
      reason: reason || '',
      sessionKey: key,
      streamRawLen: state.streamRaw.length,
      hasStreamBubble: !!state.streamBubble,
      hasLiveUserAnchor: !!state.liveUserAnchor,
      routerStripCount: routerStrips.length,
    })
    return true
  }

  function restoreLiveStreamStateForSession(key: string): boolean {
    // chat.js:6911-6963
    const sessionKey = key || getSessionKey() || ''
    const state = liveStreamStateBySession.get(sessionKey)
    if (!state || state.streamSessionKey !== sessionKey) return false
    liveStreamStateBySession.delete(sessionKey)
    _isStreaming = !!state.isStreaming
    _streamBubble = state.streamBubble || null
    _streamSessionKey = state.streamSessionKey || sessionKey
    _streamRaw = state.streamRaw || ''
    _segments = Array.isArray(state.segments) ? state.segments : []
    _activeTextSeg = state.activeTextSeg || null
    _activeTextRaw = state.activeTextRaw || ''
    _streamArtifacts = Array.isArray(state.streamArtifacts) ? state.streamArtifacts.slice() : []
    _lastVisibleStreamEvent = state.lastVisibleStreamEvent || ''
    _streamGeneration = Math.max(_streamGeneration, state.streamGeneration || 0)
    _autoScroll = state.autoScroll !== false
    _pendingFinalizedAssistantBubble = state.pendingFinalizedAssistantBubble || null
    _pendingFinalizedAssistantFallbackId = state.pendingFinalizedAssistantFallbackId || ''
    const liveUserAnchor = state.liveUserAnchor || null
    const routerStrips = Array.isArray(state.routerStrips) ? state.routerStrips : []
    const th = thread()
    if (th && liveUserAnchor && !liveUserAnchor.isConnected) {
      th.appendChild(liveUserAnchor)
    }
    if (_streamBubble) {
      _streamBubble.dataset.sessionKey = sessionKey
      _streamBubble.dataset.streamSessionKey = sessionKey
      if (th && !_streamBubble.isConnected) th.appendChild(_streamBubble)
    }
    if (_lastVisibleStreamEvent !== 'tool_result') clearAwaitingModelHint()
    if (routerFxDock()) {
      routerStrips.forEach((el) => {
        el.dataset.sessionKey = sessionKey
        if (!el.isConnected) {
          insertLiveRouterStripForAnchor(el, liveUserAnchor, _streamBubble)
        }
        routerFxResumeLiveStrip(el)
      })
    }
    if (th) th.setAttribute('aria-busy', _isStreaming ? 'true' : 'false')
    if (_isStreaming) {
      applySessionRunState({ run_status: 'running', active_task: { status: 'running' } })
      resetStreamIdleTimer()
    }
    updateSendButton()
    diag('stream.view_state_restored', {
      sessionKey,
      streamRawLen: _streamRaw.length,
      hasStreamBubble: !!_streamBubble,
      hasLiveUserAnchor: !!liveUserAnchor,
      routerStripCount: routerStrips.length,
    })
    return true
  }

  function clearViewLocalStreamState(reason: string): void {
    // chat.js:6965-7000
    const hadStreamBubble = !!_streamBubble
    const hadPendingFinalized = !!_pendingFinalizedAssistantBubble
    const routerStrips = currentSessionLiveRouterStrips(_streamSessionKey || getSessionKey() || '')
    hideThinkingIndicator()
    cancelPendingRouterFxScan(reason || 'clear_view_state')
    clearHistorySyncTimer()
    if (_renderRafId) {
      cancelAnimationFrame(_renderRafId)
      _renderRafId = null
    }
    _renderDirty = false
    clearStreamIdleTimer()
    _streamIdlePausedForApproval = false
    _approvalPendingForCurrentSession = false
    if (_streamBubble) _streamBubble.remove()
    routerStrips.forEach((el) => {
      routerFxPauseScanTimers(el)
      if (el.parentNode) el.remove()
    })
    clearPendingFinalizedAssistantBubble()
    _pendingFinalizedAssistantBubble = null
    _pendingFinalizedAssistantFallbackId = ''
    if (_streamSessionKey) liveStreamStateBySession.delete(_streamSessionKey)
    _isStreaming = false
    _streamBubble = null
    _streamSessionKey = ''
    _streamRaw = ''
    _segments = []
    _activeTextSeg = null
    _activeTextRaw = ''
    _streamArtifacts = []
    _lastVisibleStreamEvent = ''
    _streamGeneration += 1
    const th = thread()
    if (th) th.setAttribute('aria-busy', 'false')
    updateSendButton()
    diag('stream.view_state_cleared', {
      reason: reason || '',
      hadStreamBubble,
      hadPendingFinalized,
      routerStripCount: routerStrips.length,
    })
  }

  /* ── scroll (chat.js:7924-7928) ───────────────────────────────────────── */

  function scrollToBottom(): void {
    // chat.js:7924-7928
    const th = thread()
    if (th) {
      th.scrollTop = th.scrollHeight
    }
  }

  function updateAutoScrollFromThread(): void {
    // chat.js:2575-2579 — a manual scroll away from the tail pauses following;
    // returning within 60px resumes it. This prevents streaming deltas from
    // fighting a user who has moved up to read an earlier part of the answer.
    const th = thread()
    if (!th) return
    const gap = th.scrollHeight - th.scrollTop - th.clientHeight
    _autoScroll = gap < AUTO_SCROLL_BOTTOM_GAP_PX
  }

  /* ── web_search provider badge (chat.js:463-478) ──────────────────────── */

  function refreshRunningSearchProviderBadges(provider: string): void {
    // chat.js:463-469 — re-inject the badge onto every still-running web_search
    // card so a provider learned late still labels the in-flight card.
    const p = String(provider || '').trim()
    const th = thread()
    if (!th || !p) return
    th.querySelectorAll(
      '.chat-tools-collapse--running[data-tool-name="web_search"] .chat-tools-summary',
    ).forEach((summary) => toolRenderer.injectProviderBadge(summary, p))
  }

  function setSearchProvider(provider: string, options: { refreshRunning?: boolean } = {}): void {
    // chat.js:471-478
    const p = String(provider || '').trim()
    if (!p) return
    _searchProvider = p
    if (options.refreshRunning !== false) refreshRunningSearchProviderBadges(p)
  }

  /* ── tool-activity renderer (chat.js:7020-7455 / 7681-7815, tools.ts) ──── */

  const toolRenderer: ToolRenderer = createToolRenderer({
    ensureStreamBubble,
    markVisibleStreamEvent,
    flushPendingTextSegment,
    newTextSegment,
    scrollToBottom,
    getAutoScroll: () => _autoScroll,
    pushSegment: (seg) => _segments.push({ type: seg.type, raw: '', el: seg.el }),
    getSearchProvider: () => _searchProvider,
    setSearchProvider,
    getSessionKey: () => _streamSessionKey || getSessionKey() || '',
    addMessage: addMessageWithOptions,
    pushMessage,
    openModal,
    diag,
  })

  /* ── artifact renderer (chat.js:7457-7679, artifacts.ts) ──────────────── */

  const artifactRenderer: ArtifactRenderer = createArtifactRenderer({
    ensureStreamBubble,
    markVisibleStreamEvent,
    scrollToBottom,
    getAutoScroll: () => _autoScroll,
    getStreamBubble: () => _streamBubble,
    pushStreamArtifact: (artifact) => _streamArtifacts.push(artifact),
    getStreamArtifacts: () => _streamArtifacts,
    getSessionKey: () => _streamSessionKey || getSessionKey() || '',
    getAuthToken,
    esc,
    toast,
    diag,
  })

  /* ── compaction renderer (chat.js:2916-3397 + 8654-8710, compaction.ts) ── */

  // chat.js:5918-5925 — is `el` the current session's live stream bubble
  // (the compaction separator's placement anchor).
  function isCurrentSessionStreamBubble(el: HTMLElement | null): boolean {
    if (!el || el !== _streamBubble) return false
    const currentKey = getSessionKey() || ''
    const streamKey = _streamSessionKey || el.dataset.streamSessionKey || ''
    return (
      !!currentKey &&
      streamKey === currentKey &&
      (!el.dataset.streamSessionKey || el.dataset.streamSessionKey === currentKey)
    )
  }

  compactionRenderer = createCompactionRenderer({
    thread,
    getSessionKey: () => getSessionKey() || '',
    esc,
    getStreamBubble: () => _streamBubble,
    isStreaming: () => _isStreaming,
    isCurrentSessionStreamBubble,
    getAutoScroll: () => _autoScroll,
    scrollToBottom,
    getHistoryCompactionSummaries: deps.getHistoryCompactionSummaries ?? (() => []),
    updateSendButton,
    hideThinkingIndicator,
    showThinkingIndicator,
    // Wire the compaction toast into the composed router-fx renderer's
    // compaction-turn suppression (chat.js:3269-3282 / 3302/3307).
    suppressRouterFxForCompaction: (payload) => routerFxRenderer.suppressForCompaction(payload),
    scheduleHistorySync: deps.scheduleHistorySync ?? (() => {}),
    schedulePendingDrainAfterTerminal: deps.schedulePendingDrainAfterTerminal ?? (() => {}),
    popAllPendingIntoComposer: deps.popAllPendingIntoComposer ?? (() => false),
    pendingQueueLength: deps.pendingQueueLength ?? (() => 0),
    toast,
    diag,
  })

  return {
    // seq
    acceptStreamSeq,
    // idle timer
    resetStreamIdleTimer,
    clearStreamIdleTimer,
    setStreamIdlePausedForApproval,
    applyRpcPolicy,
    // thinking
    showThinkingIndicator,
    hideThinkingIndicator,
    // hints
    markVisibleStreamEvent,
    showAwaitingModelHintAfterToolResult,
    clearStreamActiveMarkReveal,
    // lifecycle
    startStreaming,
    ensureStreamBubble,
    appendDelta,
    flushRender,
    flushPendingTextSegment,
    endStreaming,
    reconcileFinalStreamText,
    // park/restore/clear
    parkCurrentSessionStreamState,
    restoreLiveStreamStateForSession,
    clearViewLocalStreamState,
    hasViewLocalStreamState,
    // scroll
    scrollToBottom,
    updateAutoScrollFromThread,
    // tool activity + subagent disclosure (Task 4 — tools.ts)
    appendToolCall: toolRenderer.appendToolCall,
    appendToolResult: toolRenderer.appendToolResult,
    settleToolResultCard: toolRenderer.settleToolResultCard,
    reconstructToolCalls: toolRenderer.reconstructToolCalls,
    appendSubagentCompletion: toolRenderer.appendSubagentCompletion,
    setSearchProvider,
    // artifacts (Task 5 — artifacts.ts)
    appendArtifact: artifactRenderer.appendArtifact,
    renderArtifacts: artifactRenderer.renderArtifacts,
    renderStreamArtifacts: artifactRenderer.renderStreamArtifacts,
    downloadArtifact: artifactRenderer.downloadArtifact,
    // router-fx (Task 6 — routerFx.ts). The live entry point routes the
    // `session.event.router_decision` event; the rest is the send/history/
    // compaction surface later tasks drive.
    handleRouterDecision: routerFxRenderer.handleRouterDecision,
    buildRouterFxFromUsage: routerFxRenderer.buildRouterFxFromUsage,
    flushPendingRouterDecisions: routerFxRenderer.flushPendingRouterDecisions,
    cachePendingRouterDecision: routerFxRenderer.cachePendingRouterDecision,
    scheduleRouterFxBeginScan: routerFxRenderer.scheduleBeginScan,
    suppressRouterFxForCompaction: routerFxRenderer.suppressForCompaction,
    routerFxRegistry,
    routerFxPref,
    // chat.js:1424-1437 — the "Visual effects" toolbar toggle writes the live
    // `_routerFx.enabled` and persists it. Ownership of the pref mutation stays
    // in the controller (this module owns `routerFxPref`), so the toolbar calls
    // this rather than mutating the object across the component boundary. The
    // live engine reads `pref.enabled` off this SAME object → immediate pickup.
    setRouterFxEnabled: (enabled: boolean): void => {
      routerFxPref.enabled = enabled
      routerFxSavePref(routerFxPref)
      if (!enabled) routerFxRenderer.clearRouterFxVisuals('preference_disabled')
    },
    // compaction (Task 7 — compaction.ts). `showCompactionToast` is the live
    // entry point routed by `session.event.compaction`; the rest is the history/
    // separator/in-flight surface the history renderer + send flow drive.
    showCompactionToast: compactionRenderer.showCompactionToast,
    syncCompactionSeparator: compactionRenderer.syncCompactionSeparator,
    renderCompactionSummarySeparators: compactionRenderer.renderCompactionSummarySeparators,
    clearCompactionSummarySeparators: compactionRenderer.clearCompactionSummarySeparators,
    setCompactInFlight: compactionRenderer.setCompactInFlight,
    settleCompactInFlight: compactionRenderer.settleCompactInFlight,
    isCompactInFlightForCurrentSession: compactionRenderer.isCompactInFlightForCurrentSession,
    // introspection (for the transcript controller + tests)
    isStreaming: (): boolean => _isStreaming,
    streamSessionKey: (): string => _streamSessionKey,
  }
}

export type StreamController = ReturnType<typeof createStreamController>
