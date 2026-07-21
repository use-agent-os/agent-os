// Chat transcript — compaction separators + controls (imperative).
//
// This module is part of the OWNER-APPROVED imperative boundary of the
// chat-view migration (design §2.1): the compaction context-separator region is
// ported as near-verbatim imperative DOM + timers from static/js/views/chat.js
// (the compaction range, chat.js:2916-3397 + the in-flight controls,
// chat.js:8654-8710), NOT reactified. Each function carries the cited legacy
// line range it was ported from. It composes into `createStreamController`
// exactly how `createToolRenderer` / `createArtifactRenderer` /
// `createRouterFxRenderer` do.
//
// Split into two surfaces (mirroring tools.ts / artifacts.ts / routerFx.ts):
//   1. Pure helpers (top-level exports) — no DOM, no timers, no module globals:
//      the terminal-status / tone / label / persistence / visibility /
//      skip-message / detail / failure-classification / semantic-notice /
//      safe-message-redaction helpers, plus the toast-dedup window factory
//      (made pure by injecting `now`/`getSessionKey`). These are the sanctioned
//      unit-test surface for this task (compaction.test.ts).
//   2. `createCompactionRenderer(deps)` — a factory the streaming controller
//      composes. The separator DOM builders + timers need view state (the
//      thread, the streaming bubble, the current session key, auto-scroll, the
//      history compaction summaries) and side-effect deps (in-flight send-button
//      sync, thinking-indicator hide, router-fx compaction suppression, history
//      resync, pending-queue recovery, toast) injected as `deps`. The legacy
//      module-globals (`_compactionSeparatorEl`, `_compactionSeparatorTimer`,
//      `_compactInFlight`, `_compactInFlightKey`, `_lastCompactionToastSig`,
//      `_lastCompactionToastAt`) rebind to instance fields here. DOM/timer
//      behavior is verified by a live-browser sweep (parity matrix), not RTL.

/* ── Constants (ported verbatim from chat.js) ───────────────────────────── */

// chat.js:2991-2998 — the six terminal compaction statuses.
const COMPACTION_TERMINAL_STATUSES = new Set([
  'completed',
  'skipped',
  'failed',
  'error',
  'cancelled',
  'emergency_ephemeral',
])

// chat.js:3200-3208 — skip reasons that are internal book-keeping (never shown
// to the user as a distinct notice; the separator collapses to "no compaction
// needed" and the detail is suppressed).
export const INTERNAL_COMPACTION_SKIP_REASONS = new Set([
  'already_attempted_this_turn',
  'already_compacted_this_turn',
  'no_entries',
  'stale_preimage',
  'structured_content_noop',
  'within_budget',
  'within_compaction_budget',
])

// chat.js:3210-3216 — manual-compact user-facing skip messages by reason.
const COMPACTION_SKIP_MESSAGES: Record<string, string> = {
  coverage_blocked: 'Context was left unchanged because required details could not be preserved.',
  empty_ephemeral_webchat_session: 'No compactable chat history yet.',
  empty_summary: 'Context was left unchanged because no usable summary was produced.',
  no_entries: 'No compactable chat history yet.',
  no_safe_turn_boundary: 'Context cannot be compacted safely during the current tool turn.',
}

// chat.js:3218-3225 — short separator-detail strings by reason.
const COMPACTION_SKIP_DETAILS: Record<string, string> = {
  coverage_blocked: 'Required details could not be preserved',
  empty_ephemeral_webchat_session: 'No compactable history',
  empty_summary: 'No usable summary was produced',
  no_entries: 'No compactable history',
  no_safe_turn_boundary: 'Current tool turn boundary is not safe to compact',
  unsafe_flush_receipt: 'Memory safety check did not complete',
}

// chat.js:2973 — default separator auto-removal delay.
export const COMPACTION_SEPARATOR_REMOVAL_MS = 4500

/* ── Shapes ─────────────────────────────────────────────────────────────── */

/** The raw compaction event payload (chat.js `session.event.compaction`). */
export interface CompactionPayload {
  key?: string
  status?: string
  source?: string
  event?: string
  reason?: string
  skip_reason?: string
  user_visible?: boolean
  compacted?: boolean
  refused?: boolean
  safe_to_send?: boolean
  safeToSend?: boolean
  error_reason?: string
  errorClass?: string
  error_class?: string
  error?: { reason?: string; code?: string } | null
  message?: string
  semanticMemory?: { status?: string } | null
  semantic_memory?: { status?: string } | null
  memorySafety?: { status?: string } | null
  memory_safety?: { status?: string } | null
  [k: string]: unknown
}

/** Overrides threaded through `syncCompactionSeparator` (chat.js:3043). */
export interface CompactionSeparatorOverrides {
  tone?: string
  label?: string | null
  animated?: boolean
  persist?: boolean
}

/** A history compaction summary row (chat.js `_historyCompactionSummaries`). */
export interface CompactionSummary {
  covered_through_id?: number | string
  [k: string]: unknown
}

/* ── Pure helpers (unit-tested) ─────────────────────────────────────────── */

// chat.js:3227-3229 — the payload's reason (reason → skip_reason → "").
export function compactionReason(payload: CompactionPayload | null | undefined): string {
  return String((payload && (payload.reason || payload.skip_reason)) || '')
}

// chat.js:3000-3002
export function compactionTerminalStatus(status: string | null | undefined): boolean {
  return COMPACTION_TERMINAL_STATUSES.has(String(status || '').toLowerCase())
}

// chat.js:3004-3009
export function compactionSeparatorAnimated(
  status: string,
  overrides: CompactionSeparatorOverrides = {},
): boolean {
  if (overrides && Object.prototype.hasOwnProperty.call(overrides, 'animated')) {
    return !!overrides.animated
  }
  return status === 'started' || status === 'observed'
}

// chat.js:3011-3017
export function shouldPersistCompactionSeparator(
  status: string,
  // chat.js:3011 signature carries `source`; the body never reads it (parity).
  _source: string,
  overrides: CompactionSeparatorOverrides = {},
): boolean {
  if (overrides && Object.prototype.hasOwnProperty.call(overrides, 'persist')) {
    return !!overrides.persist
  }
  if (!compactionTerminalStatus(status)) return false
  return status === 'completed'
}

// chat.js:3019-3033
export function compactionStatusLabel(
  payload: CompactionPayload | null | undefined,
  source: string,
  status: string,
): string {
  if (status === 'started') return 'context compacting'
  if (status === 'observed') return 'context compacting'
  if (status === 'emergency_ephemeral') return 'temporary compaction'
  if (status === 'skipped') {
    const reason = compactionReason(payload)
    return !reason || INTERNAL_COMPACTION_SKIP_REASONS.has(reason)
      ? 'no compaction needed'
      : 'compaction skipped'
  }
  if (status === 'failed' || status === 'error') return 'compaction failed'
  if (status === 'cancelled') return 'compaction cancelled'
  if (status === 'completed') return 'context compacted'
  return source === 'manual' ? 'manual compact' : 'context maintenance'
}

// chat.js:3035-3041
export function compactionSeparatorTone(status: string, payload: CompactionPayload = {}): string {
  if (status === 'completed') return 'ok'
  if (status === 'failed' || status === 'error') return 'err'
  if (status === 'cancelled' || status === 'emergency_ephemeral') return 'warn'
  if (status === 'skipped' && compactionReason(payload)) return 'warn'
  return 'info'
}

// chat.js:3231-3241
export function compactionUserVisible(
  payload: CompactionPayload | null | undefined,
  source: string,
  status: string,
): boolean {
  if (payload && Object.prototype.hasOwnProperty.call(payload, 'user_visible')) {
    return payload.user_visible !== false
  }
  if (source === 'manual') return true
  if (status === 'skipped') {
    const reason = compactionReason(payload)
    return !INTERNAL_COMPACTION_SKIP_REASONS.has(reason)
  }
  return true
}

// chat.js:3243-3251
export function compactionSkipMessage(
  payload: CompactionPayload | null | undefined,
  source: string,
): string {
  const reason = compactionReason(payload)
  if (source === 'manual') {
    return (
      COMPACTION_SKIP_MESSAGES[reason] || 'Already within context budget; no compact was applied.'
    )
  }
  if (COMPACTION_SKIP_MESSAGES[reason]) return 'Context compaction could not be applied'
  if (reason) return 'Context compaction skipped'
  return 'Already within context budget; no compact was applied.'
}

// chat.js:3253-3261
export function compactionStatusDetail(
  payload: CompactionPayload | null | undefined,
  source = '',
  status = '',
): string {
  if (!compactionUserVisible(payload, source, status)) return ''
  if (status === 'emergency_ephemeral') return 'Request-scoped; session history was not rewritten'
  const reason = compactionReason(payload)
  if (INTERNAL_COMPACTION_SKIP_REASONS.has(reason)) return ''
  if (COMPACTION_SKIP_DETAILS[reason]) return COMPACTION_SKIP_DETAILS[reason]
  if (reason) return reason.replace(/_/g, ' ')
  return ''
}

// chat.js:3158-3178
export function compactFailureBlocksPending(
  payload: CompactionPayload | null | undefined,
): boolean {
  if (!payload) return false
  if (payload.refused === true || payload.safe_to_send === false || payload.safeToSend === false) {
    return true
  }
  const reason = String(
    payload.reason ||
      payload.error_reason ||
      payload.errorClass ||
      payload.error_class ||
      (payload.error && payload.error.reason) ||
      (payload.error && payload.error.code) ||
      '',
  ).toLowerCase()
  return [
    'compaction_insufficient',
    'compaction_flush_failed',
    'context_overflow',
    'unsafe_flush_receipt',
  ].includes(reason)
}

// chat.js:3180-3189
export function compactSemanticMemoryNotice(payload: CompactionPayload | null | undefined): string {
  const semantic = (payload && (payload.semanticMemory || payload.semantic_memory)) || null
  const safety = (payload && (payload.memorySafety || payload.memory_safety)) || null
  const semanticStatus = String((semantic && semantic.status) || '').toLowerCase()
  const safetyStatus = String((safety && safety.status) || '').toLowerCase()
  if (semanticStatus === 'degraded' && safetyStatus !== 'error') {
    return 'Memory saved; organizing'
  }
  return ''
}

// chat.js:3191-3198 — redact any checkpoint path to "[memory checkpoint]".
export function compactSafeMessageDetail(payload: CompactionPayload | null | undefined): string {
  const message = payload && payload.message ? String(payload.message) : ''
  if (!message) return ''
  return message.replace(
    /(?:[A-Za-z]:[\\/][^\s'"<>]*checkpoint[^\s'"<>]*|\/[^\s'"<>]*checkpoint[^\s'"<>]*|memory\/\.raw_fallbacks\/[^\s'"<>]+|[^\s'"<>]*checkpoint[^\s'"<>]*)/gi,
    '[memory checkpoint]',
  )
}

/**
 * chat.js:2916-2928 — the duplicate-toast suppression window. Factored out as a
 * small stateful object so it is unit-testable (injected `now`/`getSessionKey`).
 * The legacy module-globals `_lastCompactionToastSig` / `_lastCompactionToastAt`
 * become closure fields here. Returns `true` to SUPPRESS a duplicate.
 */
export function createCompactionToastDedup(opts: {
  now?: () => number
  getSessionKey: () => string
}) {
  const now = opts.now ?? (() => Date.now())
  const getSessionKey = opts.getSessionKey
  let lastSig = ''
  let lastAt = 0
  return {
    suppress(
      payload: CompactionPayload | null | undefined,
      status: string | undefined,
      source: string | undefined,
    ): boolean {
      const key = String((payload && payload.key) || getSessionKey() || '')
      const event = String((payload && payload.event) || '')
      const reason = String((payload && (payload.reason || payload.skip_reason)) || '')
      const sig = `${key}|${source || ''}|${status || ''}|${event}|${reason}`
      const t = now()
      if (sig === lastSig && t - lastAt < 1500) {
        return true
      }
      lastSig = sig
      lastAt = t
      return false
    },
  }
}

/* ── Renderer factory ───────────────────────────────────────────────────── */

export interface CompactionRendererDeps {
  /** chat.js `_thread` — the transcript scroll container. */
  thread: () => HTMLElement | null
  /** chat.js `_sessionKey` — the active session key, read live. */
  getSessionKey: () => string
  /** chat.js:661 — HTML-escape a string. */
  esc: (s: string) => string
  /** chat.js `_streamBubble` — the live streaming bubble (placement anchor). */
  getStreamBubble: () => HTMLElement | null
  /** chat.js `_isStreaming` — is a stream in progress (placement branch). */
  isStreaming: () => boolean
  /** chat.js:5918 `_isCurrentSessionStreamBubble` — is `el` the current stream bubble. */
  isCurrentSessionStreamBubble: (el: HTMLElement | null) => boolean
  /** chat.js `_autoScroll` — should the view stick to the bottom. */
  getAutoScroll: () => boolean
  /** chat.js:7924 `_scrollToBottom`. */
  scrollToBottom: () => void
  /**
   * chat.js `_historyCompactionSummaries` — the summary rows the history load
   * populates, read live (the history renderer owns the array).
   */
  getHistoryCompactionSummaries: () => CompactionSummary[]
  /** chat.js:7002 `_updateSendButton` — refresh the Send/Stop affordance. */
  updateSendButton?: () => void
  /** chat.js:6486 `_hideThinkingIndicator`. */
  hideThinkingIndicator?: () => void
  /** chat.js:6379 `_showThinkingIndicator`. */
  showThinkingIndicator?: () => void
  /** chat.js:3269 `_suppressRouterFxForCompaction` — the router-fx renderer's
   *  `suppressForCompaction`, composed in the controller (Task 6). */
  suppressRouterFxForCompaction?: (payload: { key?: string }) => void
  /** chat.js:5305 `_scheduleHistorySync` — debounced history refresh. */
  scheduleHistorySync?: () => void
  /** chat.js:8644 `_schedulePendingDrainAfterTerminal` — drain the pending queue. */
  schedulePendingDrainAfterTerminal?: () => void
  /** chat.js:8596 `_popAllPendingIntoComposer` — recover pending → composer input. */
  popAllPendingIntoComposer?: () => boolean
  /** chat.js `_pendingQueue.length` — count of queued pending messages. */
  pendingQueueLength?: () => number
  /** chat.js `UI.toast` — a transient corner toast. Default: no-op. */
  toast?: (message: string, kind?: string, durationMs?: number) => void
  /** chat.js `_chatDiag` — diagnostics ring. Default: no-op. */
  diag?: (event: string, detail: Record<string, unknown>) => void
}

export function createCompactionRenderer(deps: CompactionRendererDeps) {
  const {
    thread,
    getSessionKey,
    esc,
    getStreamBubble,
    isStreaming,
    isCurrentSessionStreamBubble,
    getAutoScroll,
    scrollToBottom,
    getHistoryCompactionSummaries,
  } = deps
  const updateSendButton = deps.updateSendButton ?? (() => {})
  const hideThinkingIndicator = deps.hideThinkingIndicator ?? (() => {})
  const showThinkingIndicator = deps.showThinkingIndicator ?? (() => {})
  const suppressRouterFxForCompaction = deps.suppressRouterFxForCompaction ?? (() => {})
  const scheduleHistorySync = deps.scheduleHistorySync ?? (() => {})
  const schedulePendingDrainAfterTerminal = deps.schedulePendingDrainAfterTerminal ?? (() => {})
  const popAllPendingIntoComposer = deps.popAllPendingIntoComposer ?? (() => false)
  const pendingQueueLength = deps.pendingQueueLength ?? (() => 0)
  const toast = deps.toast ?? (() => {})

  /* ── instance fields (legacy module-globals) ──────────────────────────── */
  // chat.js:344-345 — the live session separator element + its removal timer.
  let _compactionSeparatorEl: HTMLElement | null = null
  let _compactionSeparatorTimer: ReturnType<typeof setTimeout> | null = null
  // chat.js:338-339 — compaction-in-flight tracking (drives the Send button).
  let _compactInFlight = false
  let _compactInFlightKey = ''

  // chat.js:2916-2928 — duplicate-toast suppression window.
  const toastDedup = createCompactionToastDedup({ getSessionKey })

  const sessionKey = (): string => getSessionKey() || ''

  /* ── separator timer + placement (chat.js:2930-2980) ──────────────────── */

  // chat.js:2930-2935
  function clearCompactionSeparatorTimer(): void {
    if (_compactionSeparatorTimer) {
      clearTimeout(_compactionSeparatorTimer)
      _compactionSeparatorTimer = null
    }
  }

  // chat.js:2937-2943
  function hideCompactionSeparator(): void {
    clearCompactionSeparatorTimer()
    if (_compactionSeparatorEl && _compactionSeparatorEl.parentNode) {
      _compactionSeparatorEl.remove()
    }
    _compactionSeparatorEl = null
  }

  // chat.js:2945-2959
  function placeCompactionSeparator(): void {
    const th = thread()
    if (!th || !_compactionSeparatorEl) return
    const empty = th.querySelector('.chat-empty')
    if (empty) empty.remove()
    const streamBubble = getStreamBubble()
    if (isStreaming() && isCurrentSessionStreamBubble(streamBubble)) {
      if (_compactionSeparatorEl.nextSibling !== streamBubble) {
        th.insertBefore(_compactionSeparatorEl, streamBubble)
      }
      return
    }
    if (
      _compactionSeparatorEl.parentNode !== th ||
      th.lastElementChild !== _compactionSeparatorEl
    ) {
      th.appendChild(_compactionSeparatorEl)
    }
  }

  // chat.js:2961-2971
  function ensureCompactionSeparator(): HTMLElement | null {
    const th = thread()
    if (!th) return null
    if (!_compactionSeparatorEl || !_compactionSeparatorEl.isConnected) {
      _compactionSeparatorEl = document.createElement('div')
      _compactionSeparatorEl.className =
        'chat-context-separator chat-context-separator--session chat-context-separator--info'
      _compactionSeparatorEl.setAttribute('role', 'status')
      _compactionSeparatorEl.setAttribute('aria-live', 'polite')
    }
    placeCompactionSeparator()
    return _compactionSeparatorEl
  }

  // chat.js:2973-2980
  function scheduleCompactionSeparatorRemoval(delayMs = COMPACTION_SEPARATOR_REMOVAL_MS): void {
    clearCompactionSeparatorTimer()
    const separator = _compactionSeparatorEl
    if (!separator) return
    _compactionSeparatorTimer = setTimeout(() => {
      if (_compactionSeparatorEl === separator) hideCompactionSeparator()
    }, delayMs)
  }

  // chat.js:2982-2989
  function buildCompactionSeparator(label: string, tone = 'info', extraClass = ''): HTMLElement {
    const el = document.createElement('div')
    el.className = ['chat-context-separator', extraClass, `chat-context-separator--${tone}`]
      .filter(Boolean)
      .join(' ')
    el.innerHTML = `<span>${esc(label)}</span>`
    return el
  }

  /* ── live session separator sync (chat.js:3043-3079) ──────────────────── */

  function syncCompactionSeparator(
    payload: CompactionPayload | null | undefined,
    status: string,
    source: string,
    overrides: CompactionSeparatorOverrides = {},
  ): void {
    if (
      payload &&
      Object.prototype.hasOwnProperty.call(payload, 'user_visible') &&
      payload.user_visible === false
    ) {
      hideCompactionSeparator()
      return
    }
    if (status === 'skipped' && !compactionUserVisible(payload || {}, source, status)) {
      hideCompactionSeparator()
      return
    }
    const separator = ensureCompactionSeparator()
    if (!separator) return
    clearCompactionSeparatorTimer()
    const tone = overrides.tone || compactionSeparatorTone(status, payload || {})
    const label =
      overrides.label != null
        ? overrides.label
        : compactionStatusLabel(payload || {}, source, status)
    const liveClass = compactionSeparatorAnimated(status, overrides)
      ? 'chat-context-separator--live'
      : ''
    separator.className = [
      'chat-context-separator',
      'chat-context-separator--session',
      liveClass,
      `chat-context-separator--${tone}`,
      `chat-context-separator--${status || 'unknown'}`,
    ]
      .filter(Boolean)
      .join(' ')
    separator.dataset.status = status || ''
    separator.dataset.source = source || ''
    separator.innerHTML = `<span>${esc(label)}</span>`
    placeCompactionSeparator()
    if (getAutoScroll()) scrollToBottom()
    if (compactionTerminalStatus(status)) {
      if (shouldPersistCompactionSeparator(status, source, overrides)) return
      scheduleCompactionSeparatorRemoval()
    }
  }

  /* ── history summary separators (chat.js:3081-3156) ───────────────────── */

  // chat.js:3081-3084
  function clearCompactionSummarySeparators(): void {
    const th = thread()
    if (!th) return
    th.querySelectorAll('.chat-compaction-separator').forEach((el) => el.remove())
  }

  // chat.js:3092-3096
  function summaryCoveredThroughId(summary: CompactionSummary): number | null {
    const raw = summary && summary.covered_through_id
    const value = Number(raw)
    return Number.isFinite(value) ? value : null
  }

  // chat.js:3098-3101
  function messageElementTranscriptId(el: HTMLElement): number | null {
    const value = Number(el && el.dataset ? el.dataset.transcriptId : NaN)
    return Number.isFinite(value) ? value : null
  }

  // chat.js:3103-3117
  function insertCompactionSummarySeparator(
    marker: HTMLElement,
    target: HTMLElement,
    mode: 'after' | 'before',
  ): boolean {
    const th = thread()
    if (!target || target.parentNode !== th || !th) return false
    if (mode === 'after') {
      let anchor: HTMLElement = target
      while (
        anchor.nextElementSibling &&
        anchor.nextElementSibling.classList &&
        anchor.nextElementSibling.classList.contains('router-fx')
      ) {
        anchor = anchor.nextElementSibling as HTMLElement
      }
      th.insertBefore(marker, anchor.nextSibling)
      return true
    }
    th.insertBefore(marker, target)
    return true
  }

  // chat.js:3119-3156
  function renderCompactionSummarySeparators(
    messages: unknown[] | null | undefined,
  ): HTMLElement | null {
    clearCompactionSummarySeparators()
    const th = thread()
    const summaries = getHistoryCompactionSummaries()
    if (!th || !summaries.length || !Array.isArray(messages)) return null
    const visibleMessages = Array.from(th.querySelectorAll<HTMLElement>('.msg'))
    if (!visibleMessages.length) return null
    const visibleIds = visibleMessages
      .map((el) => ({ el, id: messageElementTranscriptId(el) }))
      .filter((item): item is { el: HTMLElement; id: number } => item.id != null)
    if (!visibleIds.length) return null

    const seen = new Set<number>()
    let inserted = 0
    let firstMarker: HTMLElement | null = null
    summaries.forEach((summary) => {
      const coveredId = summaryCoveredThroughId(summary)
      if (coveredId == null || seen.has(coveredId)) return
      seen.add(coveredId)
      let target = visibleIds.find((item) => item.id === coveredId)
      let mode: 'after' | 'before' = 'after'
      if (!target) {
        target = visibleIds.find((item) => item.id > coveredId)
        mode = 'before'
      }
      if (!target) return
      const marker = buildCompactionSeparator(
        'context compacted',
        'info',
        'chat-compaction-separator chat-context-separator--history',
      )
      marker.dataset.coveredThroughId = String(coveredId)
      if (insertCompactionSummarySeparator(marker, target.el, mode)) {
        inserted++
        if (!firstMarker) firstMarker = marker
      }
    })
    if (inserted > 0) hideCompactionSeparator()
    return firstMarker
  }

  /* ── in-flight controls (chat.js:8654-8689) ───────────────────────────── */

  // chat.js:8654-8658
  function setCompactInFlight(active: boolean, key?: string): void {
    _compactInFlight = !!active
    _compactInFlightKey = active ? String(key || sessionKey() || '') : ''
    updateSendButton()
  }

  // chat.js:8660-8663
  function isCompactInFlightForCurrentSession(): boolean {
    if (!_compactInFlight) return false
    return !_compactInFlightKey || _compactInFlightKey === sessionKey()
  }

  // chat.js:8665-8689
  function settleCompactInFlight(
    payload: CompactionPayload = {},
    options: { recoverPending?: boolean; preservePending?: boolean } = {},
  ): boolean {
    const key = String((payload && payload.key) || _compactInFlightKey || sessionKey() || '')
    if (!_compactInFlight || (_compactInFlightKey && key && key !== _compactInFlightKey)) {
      return false
    }
    setCompactInFlight(false)
    const status = String((payload && payload.status) || '').toLowerCase()
    const compactedFlag =
      payload && Object.prototype.hasOwnProperty.call(payload, 'compacted')
        ? !!payload.compacted
        : null
    let recovered = false
    if (
      status === 'completed' ||
      status === 'skipped' ||
      (status === '' && compactedFlag !== null)
    ) {
      schedulePendingDrainAfterTerminal()
    } else if (options && options.preservePending) {
      recovered = pendingQueueLength() > 0
    } else if (options && options.recoverPending) {
      recovered = popAllPendingIntoComposer()
    }
    // chat.js:8687 — re-show the thinking indicator if a stream is live w/o a bubble.
    if (isStreaming() && !getStreamBubble()) showThinkingIndicator()
    return recovered
  }

  /* ── toast entry point (chat.js:3285-3362) ────────────────────────────── */

  function showCompactionToast(
    payload: CompactionPayload,
    meta: { replayed?: boolean } = {},
  ): void {
    let status = String((payload && payload.status) || '').toLowerCase()
    if (!status && payload && Object.prototype.hasOwnProperty.call(payload, 'compacted')) {
      status = payload.compacted ? 'completed' : 'skipped'
    }
    const source = String((payload && payload.source) || '').toLowerCase()
    const isReplay = !!(meta && meta.replayed)
    if (isReplay && !compactionTerminalStatus(status)) return
    if (toastDedup.suppress(payload || {}, status, source)) return
    // Single surface: the in-thread context separator renders every lifecycle
    // state (and hides itself for not-user-visible skips). The branches below
    // only drive non-UI side effects — in-flight tracking, router-fx
    // suppression, pending recovery — plus corner toasts when warranted.
    syncCompactionSeparator(payload || {}, status, source)
    if (status === 'started') {
      setCompactInFlight(true, (payload && payload.key) || sessionKey())
      hideThinkingIndicator()
      suppressRouterFxForCompaction(payload || {})
      return
    }
    if (status === 'observed') {
      hideThinkingIndicator()
      suppressRouterFxForCompaction(payload || {})
      return
    }
    if (status === 'emergency_ephemeral') {
      settleCompactInFlight(payload || {})
      if (!isReplay) {
        toast('Continuing with temporary context compaction for this turn', 'info', 4500)
      }
      return
    }
    if (status === 'skipped') {
      settleCompactInFlight(payload || {})
      scheduleCompactionSeparatorRemoval()
      return
    }
    const semanticNotice = compactSemanticMemoryNotice(payload || {})
    if (semanticNotice) {
      settleCompactInFlight(payload || {})
      syncCompactionSeparator(payload || {}, 'completed', source, {
        tone: 'ok',
        label: 'context compacted',
      })
      scheduleHistorySync()
      return
    }
    if (status === 'failed' || status === 'error') {
      const preservePending = compactFailureBlocksPending(payload || {})
      const keepPendingQueued = preservePending || (source !== 'manual' && isStreaming())
      const recovered = settleCompactInFlight(payload || {}, {
        recoverPending: !keepPendingQueued,
        preservePending: keepPendingQueued,
      })
      const safe = compactSafeMessageDetail(payload || {})
      const msg = safe ? ': ' + safe : ''
      const pendingSuffix = keepPendingQueued
        ? '; pending message preserved'
        : recovered
          ? '; pending message recovered to input'
          : ''
      syncCompactionSeparator(payload || {}, status, source, { label: 'compaction failed' })
      if (!isReplay) toast('Compact failed' + msg + pendingSuffix, 'err', 5000)
      return
    }
    if (status === 'cancelled') {
      const recovered = settleCompactInFlight(payload || {}, { recoverPending: true })
      if (!isReplay) {
        toast(
          'Compact cancelled' + (recovered ? '; pending message recovered to input' : ''),
          'info',
          4500,
        )
      }
      return
    }
    if (status !== 'completed') return
    settleCompactInFlight(payload || {})
    scheduleHistorySync()
  }

  return {
    // toast entry point (routed by `session.event.compaction`)
    showCompactionToast,
    // live session separator surface
    syncCompactionSeparator,
    buildCompactionSeparator,
    hideCompactionSeparator,
    scheduleCompactionSeparatorRemoval,
    // history summary separators (called by the history renderer post-render)
    renderCompactionSummarySeparators,
    clearCompactionSummarySeparators,
    // in-flight controls (send-button gate + terminal recovery)
    setCompactInFlight,
    settleCompactInFlight,
    isCompactInFlightForCurrentSession,
  }
}

export type CompactionRenderer = ReturnType<typeof createCompactionRenderer>
