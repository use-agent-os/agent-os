// Chat transcript — history load, pagination + render helpers.
//
// This module carries the two PURE, unit-tested helpers the brief calls out —
// `messagePageIdentity` (chat.js:5350) and `mergeHistoryMessagePages`
// (chat.js:5357-5368) — plus the imperative, controller-driven history-render
// seam (design §2.1: the transcript region is the owner-approved imperative
// boundary; history DOM rendering is ported near-verbatim, NOT reactified).
//
// The react-query wiring that DRIVES these (fetch + backward pagination) lives
// in useTranscript.ts (idiomatic React). Legacy source: static/js/views/chat.js.

import type { ChatMessage } from '../types'
import type { TurnMeta, TurnUsage } from './message'
import { historyFallbackMessageIdentity, historyStableMessageIdentity } from '../logic'

/** chat.js:350 — history page size for `chat.history` reads. */
export const CHAT_HISTORY_PAGE_SIZE = 50

/**
 * The identity a merged history page dedups on (chat.js:5350-5355). Prefers the
 * stable `message_id`/`id` (as `stable:<id>`); otherwise the role|text fallback
 * (as `fallback:<role>|<text>`). Returns `''` only for a nullish message — so a
 * present message always has a truthy identity (legacy `_messagePageIdentity`).
 */
export function messagePageIdentity(msg: ChatMessage): string {
  if (!msg) return ''
  const stable = historyStableMessageIdentity(msg)
  if (stable) return `stable:${stable}`
  return `fallback:${historyFallbackMessageIdentity(msg.role, msg.text || '')}`
}

/**
 * Merge an older page in FRONT of the current messages, deduping the overlap
 * boundary by identity (chat.js:5357-5368). First occurrence wins, so when the
 * older page's tail overlaps the current page's head the older instance is kept.
 * Nullish page arguments are tolerated (legacy `(olderMessages || [])`).
 */
export function mergeHistoryMessagePages(
  olderMessages: ChatMessage[] | null | undefined,
  currentMessages: ChatMessage[] | null | undefined,
): ChatMessage[] {
  const seen = new Set<string>()
  const merged: ChatMessage[] = []
  ;(olderMessages || []).concat(currentMessages || []).forEach((msg) => {
    const identity = messagePageIdentity(msg)
    if (identity && seen.has(identity)) return
    if (identity) seen.add(identity)
    merged.push(msg)
  })
  return merged
}

/* ── History response metadata (chat.js:5329-5348) ──────────────────────── */

/** The paging metadata a `chat.history` response carries (chat.js:5329-5338). */
export interface HistoryMetadata {
  hasMore: boolean
  oldestCursor: string | null
  newestCursor: string | null
  scope: string
  summaries: unknown[]
}

/** Raw `chat.history` response shape (only the fields the client reads). */
export interface HistoryResponse {
  messages?: ChatMessage[]
  has_more?: boolean
  oldest_cursor?: string | null
  newest_cursor?: string | null
  history_scope?: string
  compaction_summaries?: unknown[]
}

/** chat.js:5329-5338 — normalize a `chat.history` response's paging metadata. */
export function historyResponseMetadata(data: HistoryResponse | null | undefined): HistoryMetadata {
  return {
    hasMore: !!(data && data.has_more),
    oldestCursor: data ? data.oldest_cursor || null : null,
    newestCursor: data ? data.newest_cursor || null : null,
    scope: data ? data.history_scope || 'complete' : 'complete',
    summaries: Array.isArray(data && data.compaction_summaries) ? data!.compaction_summaries! : [],
  }
}

/* ── Imperative history renderer (controller-driven seam) ───────────────── */

/**
 * Injected dependencies for the imperative history renderer. These are the
 * cross-module functions the legacy `_renderHistoryMessages` / scope-row /
 * day-separator paths reference that live OUTSIDE this module (message rows,
 * turn metadata, sanitizers and history actions). `useTranscript` supplies the
 * production implementations while tests can inject focused fakes.
 */
export interface HistoryRenderDeps {
  /** The scroll container (legacy `_thread`). */
  thread: () => HTMLElement | null
  /** chat.js:661 — HTML-escape a string. */
  esc: (s: string) => string
  /** chat.js:673 — role → display label. */
  displayRoleLabel: (role: string) => string
  /** chat.js:7833/7840 — day-separator key + human label. */
  dayKey: (iso: string) => string
  dayLabel: (dayKey: string) => string
  /** chat.js:7851 — append a plain (non-streaming) message row. */
  addMessage: (
    role: string,
    text: string,
    timestamp?: string | null,
    options?: Record<string, unknown>,
  ) => HTMLElement | null
  /** chat.js:6819/748 — attach the hover Copy/Regenerate toolbar. */
  attachHoverActions: (el: HTMLElement, role: string) => void
  /** chat.js:5700-5745 — per-assistant history usage metadata. */
  turnMetaForMessage?: (message: Record<string, unknown>, assistantIndex: number) => TurnMeta | null
  attachTurnMeta?: (
    el: HTMLElement,
    model: string,
    input: number,
    output: number,
    usage: TurnUsage | null,
  ) => void
  resetMessageGrouping?: () => void
  /** chat.js:5971 — stamp history identity onto a finalized bubble. */
  stampHistoryElement: (
    el: HTMLElement,
    stableIdentity: string,
    role: string,
    text: string,
    transcriptId?: string | null,
    ts?: string | number | null,
  ) => void
  /** chat.js:389/391/419 — assistant display-text sanitizers (default identity). */
  stripProtocolTextLeak: (t: string) => string
  stripDirectiveTags: (t: string) => string
  stripGeneratedArtifactMarkers: (t: string) => string
  /** chat.js:* — user display-text: strip the "[HH:MM] " time prefix. */
  stripTimePrefix: (t: string) => string
  /** Load the next-older page (wired to the react-query fetchEarlier callback). */
  loadEarlierHistory: () => void
  /** Retry the whole history load (wired to react-query refetch). */
  reloadHistory: () => void
  /** True while the streaming controller holds a live bubble for this session. */
  isStreaming: () => boolean
  /** chat.js:* — the diagnostics ring (legacy `_chatDiag`). Default: no-op. */
  diag?: (event: string, detail: Record<string, unknown>) => void
}

/** Paging state the scope row + render path read (subset of legacy fields). */
export interface HistoryPagingState {
  loadedMessages: ChatMessage[]
  oldestCursor: string | null
  hasMore: boolean
  scope: string
  loadingEarlier: boolean
  error: string
  compactionSummaries: unknown[]
}

/**
 * Create the imperative history renderer. It owns the `_thread` DOM for history
 * rows exactly like legacy `_renderHistoryMessages` (chat.js:5550) — day
 * separators (chat.js:5805), message rows via `addMessage`, and the scope row
 * (chat.js:5374). The controller-shaped surface is verified by the live-browser
 * sweep (parity matrix), not RTL; only the pure merge/identity helpers above are
 * unit-tested (design §2.1).
 */
export function createHistoryRenderer(deps: HistoryRenderDeps) {
  const diag = deps.diag ?? (() => {})

  // Header-dedup state, private to the renderer (legacy `_lastHeaderDay`).
  let _lastHeaderDay = ''

  function historyFallbackText(role: string, text: string): string {
    // chat.js:5842-5846 — role-specific display-text strip pipeline.
    if (role === 'assistant') {
      return deps
        .stripProtocolTextLeak(
          deps.stripDirectiveTags(deps.stripGeneratedArtifactMarkers(text || '')),
        )
        .trim()
    }
    if (role === 'user') return deps.stripTimePrefix(text || '').trim()
    return (text || '').trim()
  }

  function appendHistoryDaySeparator(timestamp: string | null | undefined): void {
    // chat.js:5805-5819
    const th = deps.thread()
    if (!th) return
    const day = deps.dayKey(timestamp || '')
    if (!day || day === _lastHeaderDay) return
    const sep = document.createElement('div')
    sep.className = 'chat-day-sep'
    sep.innerHTML = `<span>${deps.dayLabel(day)}</span>`
    th.appendChild(sep)
    _lastHeaderDay = day
  }

  function removeHistoryScopeRows(): void {
    // chat.js:5369-5372
    const th = deps.thread()
    if (!th) return
    th.querySelectorAll('.chat-history-scope').forEach((el) => el.remove())
  }

  function renderHistoryScopeRow(state: HistoryPagingState): void {
    // chat.js:5374-5438
    const th = deps.thread()
    if (!th) return
    removeHistoryScopeRows()
    if (state.loadedMessages.length === 0 && !state.error) return

    let tone = ''
    let message = ''
    let detail = ''
    let showLoadEarlier = false
    let showRetry = false

    if (state.loadingEarlier) {
      tone = 'loading'
      message = 'Loading earlier messages...'
    } else if (state.error) {
      tone = 'error'
      message = state.error
      showRetry = true
    } else if (state.hasMore || state.scope === 'latest_window') {
      tone = 'partial'
      message = `Showing latest ${state.loadedMessages.length} messages.`
      detail = 'Older history is available.'
      showLoadEarlier = !!state.oldestCursor
    } else if (state.scope === 'compacted' || state.compactionSummaries.length > 0) {
      tone = 'compacted'
      message = 'Older context was compacted for the model.'
      detail = 'Export the session for exact text.'
    } else {
      return
    }

    const row = document.createElement('div')
    row.className = `chat-history-scope chat-history-scope--${tone}`
    row.setAttribute('role', tone === 'loading' ? 'status' : 'note')
    if (tone === 'loading') row.setAttribute('aria-busy', 'true')
    row.innerHTML =
      `<span class="chat-history-scope__text">${deps.esc(message)}</span>` +
      (detail ? `<span class="chat-history-scope__detail">${deps.esc(detail)}</span>` : '') +
      '<span class="chat-history-scope__actions"></span>'
    const actions = row.querySelector('.chat-history-scope__actions')
    if (actions && showLoadEarlier) {
      const btn = document.createElement('button')
      btn.type = 'button'
      btn.className = 'btn btn--sm btn--ghost'
      btn.textContent = 'Load earlier'
      btn.disabled = state.loadingEarlier
      btn.addEventListener('click', () => deps.loadEarlierHistory())
      actions.appendChild(btn)
    }
    if (actions && showRetry) {
      const btn = document.createElement('button')
      btn.type = 'button'
      btn.className = 'btn btn--sm btn--ghost'
      btn.textContent = state.hasMore && state.oldestCursor ? 'Retry' : 'Retry history'
      btn.addEventListener('click', () => {
        if (state.hasMore && state.oldestCursor) {
          deps.loadEarlierHistory()
        } else {
          deps.reloadHistory()
        }
      })
      actions.appendChild(btn)
    }
    th.insertBefore(row, th.firstChild || null)
  }

  /**
   * Render a full history page into the thread (chat.js:5550-5796, focused
   * port). Rebuilds the message rows + day separators from `messages` and drops
   * the empty state. Cross-module behavior is dispatched through the injected
   * dependencies. `opts.preserveScroll` restores the scroll offset after a prepend
   * (used by the load-earlier path).
   */
  function renderHistoryMessages(
    messages: ChatMessage[],
    state: HistoryPagingState,
    opts: {
      preserveScroll?: boolean
      previousScrollHeight?: number
      previousScrollTop?: number
    } = {},
  ): void {
    const th = deps.thread()
    if (!th) return
    removeHistoryScopeRows()

    if (messages.length === 0) {
      // Preserve a live streaming bubble on an empty history refresh
      // (chat.js:5554-5581, the live-stream-keep branch) — the streaming
      // controller owns those nodes, so leave them in place.
      if (deps.isStreaming()) {
        diag('history.empty.keep_live_stream_view', {})
        return
      }
      th.innerHTML = ''
      _lastHeaderDay = ''
      diag('history.empty.rendered_empty_state', {})
      return
    }

    // Drop stale day separators + any prior message rows; rebuild deterministically.
    th.querySelectorAll('.chat-day-sep, .chat-empty').forEach((el) => el.remove())
    const consumed = new Set<HTMLElement>()
    const existing = Array.from(th.querySelectorAll<HTMLElement>('.msg'))
    existing.forEach((el) => {
      // Never reap a live streaming bubble while streaming (owned by the controller).
      if (deps.isStreaming()) return
      el.remove()
    })
    _lastHeaderDay = ''
    deps.resetMessageGrouping?.()
    let assistantIndex = 0

    messages.forEach((msg) => {
      const rawText = msg.text || ''
      const displayText = msg.role === 'user' ? deps.stripTimePrefix(rawText) : rawText
      const stableIdentity = historyStableMessageIdentity(msg)
      const timestamp = (msg.timestamp as unknown as string) ?? (msg as { ts?: string }).ts ?? null
      appendHistoryDaySeparator(timestamp)
      const div = deps.addMessage(msg.role, displayText, timestamp, {
        provenanceKind: (msg as { provenance_kind?: string }).provenance_kind || '',
        provenanceSourceSessionKey:
          (msg as { provenance_source_session_key?: string }).provenance_source_session_key || '',
        provenanceSourceTool:
          (msg as { provenance_source_tool?: string }).provenance_source_tool || '',
      })
      if (!div) return
      consumed.add(div)
      deps.stampHistoryElement(
        div,
        stableIdentity,
        msg.role,
        displayText,
        (msg as { transcript_id?: string | null }).transcript_id ?? null,
        // chat.js:5648 — export mirrors `ts: msg.timestamp || msg.ts || null`.
        timestamp,
      )
      deps.attachHoverActions(div, msg.role)
      if (msg.role === 'assistant') {
        const meta = deps.turnMetaForMessage?.(
          msg as unknown as Record<string, unknown>,
          assistantIndex,
        )
        assistantIndex += 1
        if (meta) deps.attachTurnMeta?.(div, meta.model, meta.input, meta.output, meta.saved)
      }
    })

    renderHistoryScopeRow(state)

    if (opts.preserveScroll) {
      const oldHeight = Number(opts.previousScrollHeight || 0)
      const oldTop = Number(opts.previousScrollTop || 0)
      th.scrollTop = Math.max(0, th.scrollHeight - oldHeight + oldTop)
    } else {
      th.scrollTop = th.scrollHeight
    }
    diag('history.done', { count: messages.length, consumed: consumed.size })
    // historyFallbackText is retained for identity parity with legacy's
    // role-specific strip pipeline; reference it so the strip seams stay wired.
    void historyFallbackText
  }

  return {
    renderHistoryMessages,
    renderHistoryScopeRow,
    removeHistoryScopeRows,
    appendHistoryDaySeparator,
  }
}

export type HistoryRenderer = ReturnType<typeof createHistoryRenderer>
