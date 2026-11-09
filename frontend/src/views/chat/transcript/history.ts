// Chat transcript — history load, pagination + render helpers.
// This module carries the two PURE, unit-tested helpers the brief calls out —
// `messagePageIdentity` and `mergeHistoryMessagePages`
// plus the imperative, controller-driven history-render
// seam (design §2.1: the transcript region is the reviewed imperative
// boundary; history DOM rendering is ported near-verbatim, NOT reactified).
// The react-query wiring that DRIVES these (fetch + backward pagination) lives
// in useTranscript.ts (idiomatic React). Legacy source: static/js/views/chat.js.

import { t } from '@/i18n'
import '@/i18n/en/chat'

import type { ChatMessage } from '../types'
import { lockAnsweredAskCards } from './ask'
import type { Artifact } from './artifacts'
import type { TurnMeta, TurnUsage } from './message'
import type { TranscriptHeaderStateRef } from './stream'
import { historyFallbackMessageIdentity, historyStableMessageIdentity } from '../logic'
import { routerFxRequestKindFromAttachments, type RouterFxHistoryOptions } from './routerFx'
import { createThinkingBlock, wireLazyThinkingBlock } from './thinking'

/** — history page size for `chat.history` reads. */
export const CHAT_HISTORY_PAGE_SIZE = 50

/**
 * The identity a merged history page dedups on. Prefers the
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
 * boundary by identity. First occurrence wins, so when the
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

/**
 * Read one persisted list of object payloads without trusting the RPC boundary.
 * `chat.history` currently emits dictionaries for all three fields consumed by
 * the history renderer (`tool_calls`, `attachments`, and `artifacts`).
 */
function historyMessageRecords(
  msg: ChatMessage | null | undefined,
  field: 'tool_calls' | 'attachments' | 'artifacts',
): Array<Record<string, unknown>> {
  const value = (msg as unknown as Record<string, unknown> | null | undefined)?.[field]
  if (!Array.isArray(value)) return []
  return value.filter(
    (item): item is Record<string, unknown> =>
      item !== null && typeof item === 'object' && !Array.isArray(item),
  )
}

/** `chat.history` persists generated files on the message's `artifacts` field. */
export function historyMessageArtifacts(msg: ChatMessage | null | undefined): Artifact[] {
  return historyMessageRecords(msg, 'artifacts') as Artifact[]
}

/* ── History response metadata ──────────────────────── */

/** The paging metadata a `chat.history` response carries. */
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
  /** `delta` / `reset` on an answer to `delta_cursor`. */
  kind?: string
}

/**
 * Fold a `chat.history` `delta_cursor` answer (only the rows after the
 * client's `newest_cursor`) into what is loaded, as a full response the
 * renderer can take. Returns null when the delta cannot stand in for a full
 * read: the cursor was unusable (`kind: "reset"`) or more rows are waiting.
 */
export function applyHistoryDelta(
  paging: HistoryPagingState,
  delta: HistoryResponse | null | undefined,
): HistoryResponse | null {
  if (!delta || delta.kind !== 'delta' || delta.has_more) return null
  return {
    messages: mergeHistoryMessagePages(paging.loadedMessages, delta.messages || []),
    has_more: paging.hasMore,
    oldest_cursor: paging.oldestCursor,
    newest_cursor: delta.newest_cursor ?? null,
    history_scope: paging.scope,
    compaction_summaries: paging.compactionSummaries,
  }
}

/** — normalize a `chat.history` response's paging metadata. */
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
  /** — HTML-escape a string. */
  esc: (s: string) => string
  /** — role → display label. */
  displayRoleLabel: (role: string) => string
  /** — day-separator key + human label. */
  dayKey: (iso: string) => string
  dayLabel: (dayKey: string) => string
  /** Shared legacy `_lastHeaderDay` / `_lastHeaderRole` cursor. */
  headerState?: TranscriptHeaderStateRef
  /** — append a plain (non-streaming) message row. */
  addMessage: (
    role: string,
    text: string,
    timestamp?: string | null,
    options?: Record<string, unknown>,
  ) => HTMLElement | null
  /** — refresh a reused history row without replacing its DOM identity. */
  replaceMessage: (
    el: HTMLElement,
    role: string,
    text: string,
    timestamp?: string | null,
    options?: Record<string, unknown>,
  ) => void
  /** — create/remove/refresh a row header after identity reuse. */
  syncMessageHeader: (
    el: HTMLElement,
    displayRole: string,
    timestamp: string | number | null | undefined,
    options: Record<string, unknown>,
    sameGroup: boolean,
  ) => void
  /** — attach the hover Copy/Regenerate toolbar. */
  attachHoverActions: (el: HTMLElement, role: string) => void
  /** — rebuild persisted tool segments inside the message body. */
  reconstructToolCalls: (el: HTMLElement, segments: Array<Record<string, unknown>>) => void
  /**
   * render one persisted attachment as escaped,
   * insertion-safe HTML. The injected attachment renderer owns URL validation.
   */
  renderMessageAttachmentHtml: (attachment: Record<string, unknown>) => string
  /** — render persisted artifact cards as escaped HTML. */
  renderArtifacts: (artifacts: Artifact[]) => string
  /**
   * chart.ts `mountCharts` — draw chart placeholders in a replayed row.
   * AgentOS-native (no legacy counterpart); optional so focused history tests
   * need not supply it.
   */
  mountCharts?: (container: HTMLElement) => void
  /** — remove foreign/unstamped dock residue before a rebuild. */
  prepareHistoryRouterFx: () => void
  /** — rebuild a settled dock receipt from persisted usage. */
  reconcileHistoryRouterFx: (
    usage: TurnUsage | null | undefined,
    opts: RouterFxHistoryOptions,
  ) => HTMLElement | null
  /** — flush pending decisions and perform the final dock sweep. */
  finishHistoryRouterFx: () => void
  /** Must set `_historyHasRendered` before pending router decisions flush. */
  markHistoryRendered: () => void
  /** — per-assistant history usage metadata. */
  turnMetaForMessage?: (message: Record<string, unknown>, assistantIndex: number) => TurnMeta | null
  attachTurnMeta?: (
    el: HTMLElement,
    model: string,
    input: number,
    output: number,
    usage: TurnUsage | null,
  ) => void
  resetMessageGrouping?: () => void
  /** — stamp history identity onto a finalized bubble. */
  stampHistoryElement: (
    el: HTMLElement,
    stableIdentity: string,
    role: string,
    text: string,
    transcriptId?: string | null,
    ts?: string | number | null,
  ) => void
  /** /419 — assistant display-text sanitizers (default identity). */
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
  /** Preserve the reader-controlled auto-follow state during async history refreshes. */
  shouldAutoScroll: () => boolean
  /** Live nodes preserved/reordered during a history refresh . */
  getStreamBubble: () => HTMLElement | null
  getThinkingIndicator: () => HTMLElement | null
  /** Live reasoning block row (AgentOS-native; kept alongside the stream bubble). */
  getLiveThinkingBlock?: () => HTMLElement | null
  /** `chat.thinking` fetch-on-expand for history rows flagged `has_thinking`. */
  fetchThinking?: (messageId: string) => Promise<string | null>
  /**
   * `chat.entry` fetch for a row the gateway sent as a placeholder (too large
   * for a history page): resolves to the full message, or null.
   */
  fetchEntry?: (entryId: string) => Promise<ChatMessage | null>
  getCurrentSessionLiveUserAnchor: () => HTMLElement | null
  getPendingFinalizedAssistantBubble: () => HTMLElement | null
  isPendingFinalizedAssistantBubble: (el: HTMLElement | null) => boolean
  clearPendingFinalizedAssistantBubble: () => void
  /** chat.js:* — the diagnostics ring (legacy `_chatDiag`). Default: no-op. */
  diag?: (event: string, detail: Record<string, unknown>) => void
}

/** Paging state the scope row + render path read (subset of legacy fields). */
/** "150 KiB" / "2.4 MiB" for a placeholder's `byte_size`. */
export function formatEntrySize(bytes: unknown): string {
  const n = Number(bytes)
  if (!Number.isFinite(n) || n <= 0) return ''
  if (n >= 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MiB`
  return `${Math.max(1, Math.round(n / 1024))} KiB`
}

/** The gateway's oversized-row marker (`chat.history` budgets). */
export function historyPlaceholderEntryId(msg: ChatMessage): string {
  const raw = msg as { placeholder?: unknown; entry_id?: unknown }
  if (raw.placeholder !== true) return ''
  const id = raw.entry_id
  return typeof id === 'string' || typeof id === 'number' ? String(id) : ''
}

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
 * rows exactly like legacy `_renderHistoryMessages` — day
 * separators, message rows via `addMessage`, and the scope row
 *. The controller-shaped surface is verified by the live-browser
 * sweep (parity matrix), not RTL; only the pure merge/identity helpers above are
 * unit-tested (design §2.1).
 */
export function createHistoryRenderer(deps: HistoryRenderDeps) {
  const diag = deps.diag ?? (() => {})
  const headerState = deps.headerState ?? { current: { day: '', role: '' } }
  // Placeholders the reader expanded: a later history refresh (which brings
  // the placeholder again) keeps showing the full message.
  const expandedEntries = new Map<string, ChatMessage>()

  /**
   * The "Message truncated (N KiB) — Show full" line under a placeholder's
   * preview. Show full fetches the row through `chat.entry` and re-renders it.
   */
  function appendTruncatedNotice(
    row: HTMLElement,
    msg: ChatMessage,
    entryId: string,
    rerender: (full: ChatMessage) => void,
  ): void {
    const body = row.querySelector<HTMLElement>('.msg-body')
    if (!body || body.querySelector('.msg-truncated')) return
    const notice = document.createElement('div')
    notice.className = 'msg-truncated'
    const label = document.createElement('span')
    label.className = 'msg-truncated__label'
    const size = formatEntrySize((msg as { byte_size?: unknown }).byte_size)
    label.textContent = size ? t('chat.msgTruncatedSize', { size }) : t('chat.msgTruncated')
    notice.appendChild(label)
    if (deps.fetchEntry) {
      const button = document.createElement('button')
      button.type = 'button'
      button.className = 'msg-truncated__show'
      button.textContent = t('chat.msgShowFull')
      button.addEventListener('click', () => {
        button.disabled = true
        button.textContent = t('chat.msgShowFullLoading')
        deps.fetchEntry!(entryId)
          .then((full) => {
            if (!full) throw new Error(t('chat.msgShowFullMissing'))
            expandedEntries.set(entryId, full)
            rerender(full)
          })
          .catch((err: unknown) => {
            button.disabled = false
            button.textContent = t('chat.msgShowFull')
            const message = err instanceof Error ? err.message : String(err)
            label.textContent = t('chat.msgShowFullFailed', { message })
          })
      })
      notice.appendChild(button)
    }
    body.appendChild(notice)
  }

  function historyFallbackText(role: string, text: string): string {
    // role-specific display-text strip pipeline.
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

  function historyFallbackIdentity(role: string, text: string): string {
    return `${role || ''}|${historyFallbackText(role, text)}`
  }

  function historyElementRole(el: HTMLElement): string {
    const tagged = el.getAttribute('data-history-role') || ''
    if (tagged) return tagged
    if (el.classList.contains('user')) return 'user'
    if (el.classList.contains('assistant')) return 'assistant'
    if (el.classList.contains('subagent') || el.classList.contains('system')) return 'system'
    return ''
  }

  function historyElementText(el: HTMLElement): string {
    const raw = el.getAttribute('data-history-raw-text') || ''
    if (raw) return raw
    const body = el.querySelector('.msg-body')
    return body ? (body.textContent || '').trim() : ''
  }

  function historyElementFallbackIdentity(el: HTMLElement): string {
    const role = historyElementRole(el)
    const text = historyElementText(el)
    return role || text ? historyFallbackIdentity(role, text) : ''
  }

  function pushIdentityElement(
    map: Map<string, HTMLElement[]>,
    identity: string,
    el: HTMLElement,
  ): void {
    const elements = map.get(identity) || []
    elements.push(el)
    map.set(identity, elements)
  }

  function shiftIdentityElement(
    map: Map<string, HTMLElement[]>,
    identity: string,
    consumed: Set<HTMLElement>,
  ): HTMLElement | null {
    if (!identity) return null
    const elements = map.get(identity)
    if (!elements) return null
    while (elements.length > 0) {
      const el = elements.shift()
      if (el && !consumed.has(el)) return el
    }
    return null
  }

  function historyStillWaitingForAssistant(messages: ChatMessage[]): boolean {
    if (messages.length === 0) return true
    return messages[messages.length - 1]?.role !== 'assistant'
  }

  /**
   * The live error row a rebuild must keep: the newest `.msg.error` the live
   * path stamped `data-live-error`, while the history still ends on the
   * user's message. A turn that fails before anything is written (an older
   * gateway, an ACP session refusing the turn) leaves no row behind, and the
   * resync that very error triggers would otherwise erase the only sign the
   * turn failed. Once the gateway persists a reply or an error row after that
   * message, the persisted row takes over and the live one goes.
   */
  function liveErrorToKeep(th: HTMLElement, messages: ChatMessage[]): HTMLElement | null {
    if (messages[messages.length - 1]?.role !== 'user') return null
    const errors = th.querySelectorAll<HTMLElement>('.msg.error[data-live-error]')
    return errors.length ? (errors[errors.length - 1] ?? null) : null
  }

  function historyLiveTailAnchor(): HTMLElement | null {
    if (!deps.isStreaming()) return null
    const bubble = deps.getStreamBubble()
    if (bubble?.isConnected) return bubble
    const thinking = deps.getThinkingIndicator()
    return thinking?.isConnected ? thinking : null
  }

  function appendHistoryElementInOrder(el: HTMLElement): void {
    const th = deps.thread()
    if (!th) return
    const liveTail = historyLiveTailAnchor()
    if (liveTail && el !== liveTail) th.insertBefore(el, liveTail)
    else th.appendChild(el)
  }

  function appendHistoryDaySeparator(timestamp: string | null | undefined): void {
    const th = deps.thread()
    if (!th) return
    const day = deps.dayKey(timestamp || '')
    if (!day || day === headerState.current.day) return
    const sep = document.createElement('div')
    sep.className = 'chat-day-sep'
    sep.innerHTML = `<span>${deps.dayLabel(day)}</span>`
    const liveTail = historyLiveTailAnchor()
    if (liveTail) th.insertBefore(sep, liveTail)
    else th.appendChild(sep)
    headerState.current.day = day
    headerState.current.role = ''
  }

  function removeHistoryScopeRows(): void {
    const th = deps.thread()
    if (!th) return
    th.querySelectorAll('.chat-history-scope').forEach((el) => el.remove())
  }

  function renderHistoryScopeRow(state: HistoryPagingState): void {
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
      message = t('chat.historyLoadingEarlier')
    } else if (state.error) {
      tone = 'error'
      message = state.error
      showRetry = true
    } else if (state.hasMore || state.scope === 'latest_window') {
      tone = 'partial'
      message = `Showing latest ${state.loadedMessages.length} messages.`
      detail = t('chat.historyOlderAvailable')
      showLoadEarlier = !!state.oldestCursor
    } else if (state.scope === 'compacted' || state.compactionSummaries.length > 0) {
      tone = 'compacted'
      message = t('chat.historyCompacted')
      detail = t('chat.historyExportHint')
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
      btn.textContent = t('chat.historyLoadEarlier')
      btn.disabled = state.loadingEarlier
      btn.addEventListener('click', () => deps.loadEarlierHistory())
      actions.appendChild(btn)
    }
    if (actions && showRetry) {
      const btn = document.createElement('button')
      btn.type = 'button'
      btn.className = 'btn btn--sm btn--ghost'
      btn.textContent =
        state.hasMore && state.oldestCursor ? t('chat.historyRetry') : t('chat.historyRetryHistory')
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
   * Render a full history page into the thread (, focused
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
      const streamBubble = deps.getStreamBubble()
      const liveUserAnchor = deps.getCurrentSessionLiveUserAnchor()
      const thinking = deps.getThinkingIndicator()
      const liveThinkingBlock = deps.getLiveThinkingBlock?.() || null
      if (deps.isStreaming() && (streamBubble || liveUserAnchor || thinking)) {
        th.querySelectorAll<HTMLElement>('.msg').forEach((el) => {
          if (
            el !== streamBubble &&
            el !== liveUserAnchor &&
            el !== thinking &&
            el !== liveThinkingBlock
          )
            el.remove()
        })
        th.querySelectorAll('.chat-day-sep, .chat-empty').forEach((el) => el.remove())
        if (liveUserAnchor && !liveUserAnchor.isConnected) th.appendChild(liveUserAnchor)
        if (liveThinkingBlock && !liveThinkingBlock.isConnected) th.appendChild(liveThinkingBlock)
        if (streamBubble && !streamBubble.isConnected) th.appendChild(streamBubble)
        if (thinking && !thinking.isConnected) th.appendChild(thinking)
        if (deps.shouldAutoScroll()) th.scrollTop = th.scrollHeight
        diag('history.empty.keep_live_stream_view', {
          hasStreamBubble: !!streamBubble,
          hasLiveUserAnchor: !!liveUserAnchor,
          hasThinkingIndicator: !!thinking,
        })
        return
      }
      const pendingFinalized = deps.getPendingFinalizedAssistantBubble()
      if (pendingFinalized?.isConnected) {
        if (deps.shouldAutoScroll()) th.scrollTop = th.scrollHeight
        diag('history.empty.keep_pending_finalized_assistant', {})
        return
      }
      th.innerHTML = ''
      headerState.current.day = ''
      headerState.current.role = ''
      const empty = document.createElement('div')
      empty.className = 'chat-empty'
      empty.textContent = t('chat.historyEmpty')
      th.appendChild(empty)
      deps.markHistoryRendered()
      diag('history.empty.rendered_empty_state', {})
      return
    }

    const existingByStableIdentity = new Map<string, HTMLElement>()
    const existingByFallbackIdentity = new Map<string, HTMLElement[]>()
    th.querySelectorAll<HTMLElement>('.msg').forEach((el) => {
      const stable = el.getAttribute('data-message-id') || ''
      if (stable) existingByStableIdentity.set(stable, el)
      const fallback =
        el.getAttribute('data-history-fallback-id') || historyElementFallbackIdentity(el)
      if (fallback) pushIdentityElement(existingByFallbackIdentity, fallback, el)
    })

    th.querySelector('.chat-empty')?.remove()
    th.querySelectorAll('.chat-day-sep, .chat-empty').forEach((el) => el.remove())
    const consumed = new Set<HTMLElement>()
    headerState.current.day = ''
    headerState.current.role = ''
    deps.resetMessageGrouping?.()
    deps.prepareHistoryRouterFx()
    let assistantIndex = 0
    let userIndex = 0
    let lastUserRequestKind: 'image' | 'text' = 'text'

    messages.forEach((sent) => {
      const placeholderId = historyPlaceholderEntryId(sent)
      const msg = (placeholderId && expandedEntries.get(placeholderId)) || sent
      if (msg.role === 'user') {
        userIndex += 1
        lastUserRequestKind = routerFxRequestKindFromAttachments(
          historyMessageRecords(msg, 'attachments'),
        )
      }
      const rawText = msg.text || ''
      const displayText = msg.role === 'user' ? deps.stripTimePrefix(rawText) : rawText
      const stableIdentity = historyStableMessageIdentity(msg)
      const fallbackIdentity = historyFallbackIdentity(msg.role, displayText)
      const timestamp = (msg.timestamp as unknown as string) ?? (msg as { ts?: string }).ts ?? null
      appendHistoryDaySeparator(timestamp)
      const messageOptions = {
        provenanceKind: (msg as { provenance_kind?: string }).provenance_kind || '',
        provenanceSourceSessionKey:
          (msg as { provenance_source_session_key?: string }).provenance_source_session_key || '',
        provenanceSourceTool:
          (msg as { provenance_source_tool?: string }).provenance_source_tool || '',
        // History is a bulk reconstruction. Per-row tail following causes
        // repeated layouts and exposes a top-to-bottom scroll on Chat entry;
        // the renderer positions the completed transcript exactly once below.
        autoScroll: false,
      }
      // Capture the shared cursor before `addMessage` advances it. Reused rows
      // do not go through `addMessage`, while newly-created rows do; this makes
      // the reconciliation result identical for both identity paths.
      const previousHeaderDay = headerState.current.day
      const previousHeaderRole = headerState.current.role
      let div = stableIdentity ? existingByStableIdentity.get(stableIdentity) || null : null
      if (!div) {
        div = shiftIdentityElement(existingByFallbackIdentity, fallbackIdentity, consumed)
      }
      if (div) {
        deps.replaceMessage(div, msg.role, displayText, timestamp, messageOptions)
      } else {
        div = deps.addMessage(msg.role, displayText, timestamp, messageOptions)
      }
      if (!div) return
      consumed.add(div)
      deps.stampHistoryElement(
        div,
        stableIdentity,
        msg.role,
        displayText,
        (msg as { transcript_id?: string | null }).transcript_id ?? null,
        // export mirrors `ts: msg.timestamp || msg.ts || null`.
        timestamp,
      )
      appendHistoryElementInOrder(div)

      const displayRole = div.classList.contains('subagent') ? 'subagent' : msg.role
      const day = deps.dayKey(timestamp || '')
      const collapsible = displayRole === 'user' || displayRole === 'assistant'
      const sameGroup =
        collapsible && displayRole === previousHeaderRole && day === previousHeaderDay && day !== ''
      deps.syncMessageHeader(div, displayRole, timestamp, messageOptions, sameGroup)
      if (collapsible) headerState.current.role = displayRole

      // body additions have a strict order. Tool
      // reconstruction may replace the entire body; attachments follow those
      // segments, then generated artifacts follow the attachments.
      const toolCalls = historyMessageRecords(msg, 'tool_calls')
      if (msg.role === 'assistant' && toolCalls.length > 0) {
        deps.reconstructToolCalls(div, toolCalls)
      }

      // Collapsed reasoning block at the top of the body; the reasoning text
      // itself is fetched from `chat.thinking` on first expand.
      const hasThinking = Boolean((msg as { has_thinking?: boolean }).has_thinking)
      if (msg.role === 'assistant' && hasThinking && deps.fetchThinking) {
        const messageId = String(
          (msg as { message_id?: string | number | null }).message_id ??
            (msg as { id?: string | number | null }).id ??
            '',
        )
        const body = div.querySelector<HTMLElement>('.msg-body')
        if (body && messageId && !body.querySelector('.thinking-block')) {
          const parts = createThinkingBlock({ open: false })
          wireLazyThinkingBlock(parts, () => deps.fetchThinking!(messageId))
          body.insertAdjacentElement('afterbegin', parts.details)
        }
      }

      const attachments = historyMessageRecords(msg, 'attachments')
      if (attachments.length > 0) {
        const body = div.querySelector<HTMLElement>('.msg-body')
        if (body) {
          body.classList.add('msg-body--has-attachments')
          if (msg.role === 'user' && (body.textContent || '').trim()) {
            // The body came from message text; escape it before the deliberate
            // legacy innerHTML rewrite so history payloads cannot inject markup.
            body.innerHTML = `<div class="msg-attachment-text">${deps.esc(body.textContent || '')}</div>`
          }
          const attachmentHtml = attachments
            .map((attachment) => deps.renderMessageAttachmentHtml(attachment))
            .join('')
          body.insertAdjacentHTML(
            'beforeend',
            `<div class="msg-attachments">${attachmentHtml}</div>`,
          )
        }
      }

      const artifacts = historyMessageArtifacts(msg)
      if (artifacts.length > 0) {
        const body = div.querySelector<HTMLElement>('.msg-body')
        if (body) {
          body.insertAdjacentHTML('beforeend', deps.renderArtifacts(artifacts))
          deps.mountCharts?.(body)
        }
      }

      if (msg === sent && placeholderId) {
        const row = div
        appendTruncatedNotice(row, msg, placeholderId, (full) => {
          const fullText = full.role === 'user' ? deps.stripTimePrefix(full.text || '') : full.text
          deps.replaceMessage(row, full.role, fullText || '', timestamp, messageOptions)
          const fullTools = historyMessageRecords(full, 'tool_calls')
          if (full.role === 'assistant' && fullTools.length > 0) {
            deps.reconstructToolCalls(row, fullTools)
          }
          // The re-rendered body dropped the row's attachments and artifacts:
          // put them back, and let the mount pass fill their media (ticketed
          // URLs, charts) as on any replayed row.
          const fullBody = row.querySelector<HTMLElement>('.msg-body')
          if (fullBody) {
            const fullAttachments = historyMessageRecords(full, 'attachments')
            if (fullAttachments.length > 0) {
              fullBody.classList.add('msg-body--has-attachments')
              const chips = fullAttachments
                .map((attachment) => deps.renderMessageAttachmentHtml(attachment))
                .join('')
              fullBody.insertAdjacentHTML(
                'beforeend',
                `<div class="msg-attachments">${chips}</div>`,
              )
            }
            const fullArtifacts = historyMessageArtifacts(full)
            if (fullArtifacts.length > 0) {
              fullBody.insertAdjacentHTML('beforeend', deps.renderArtifacts(fullArtifacts))
              deps.mountCharts?.(fullBody)
            }
          }
          deps.attachHoverActions(row, full.role)
        })
      }

      // Tool reconstruction and the user-attachment body rewrite above remove
      // the toolbar installed by addMessage. Re-attach only after every body
      // mutation so actions are last and remain interactive.
      deps.attachHoverActions(div, msg.role)
      if (msg.role === 'assistant') {
        const meta = deps.turnMetaForMessage?.(
          msg as unknown as Record<string, unknown>,
          assistantIndex,
        )
        assistantIndex += 1
        if (meta) {
          const turnUsage = meta.saved ? { ...meta.saved } : null
          // `_savedUsageFromMeta` — older locally persisted
          // turn-meta rows can keep the model only on the outer record. Carry
          // that real value into the cloned usage payload so router identity
          // reconstruction does not silently lose the turn.
          if (turnUsage && !turnUsage.model && !turnUsage.routed_model && meta.model) {
            turnUsage.model = meta.model
          }
          if (turnUsage) {
            const raw = msg as unknown as Record<string, unknown>
            deps.reconcileHistoryRouterFx(turnUsage, {
              turnIndex: userIndex,
              requestKind: lastUserRequestKind,
              hintTimestamp:
                (raw.timestamp as string | number | null | undefined) ||
                (raw.ts as string | number | null | undefined) ||
                (raw.message_id as string | number | null | undefined) ||
                '',
            })
          }
          deps.attachTurnMeta?.(div, meta.model, meta.input, meta.output, turnUsage)
        }
      }
    })

    // Pending live/replayed decisions may now resolve against the reconstructed
    // user anchors. Set the lifecycle flag before flushing them.
    deps.markHistoryRendered()
    deps.finishHistoryRouterFx()

    const keptError = liveErrorToKeep(th, messages)
    if (keptError) appendHistoryElementInOrder(keptError)

    const streamBubble = deps.getStreamBubble()
    const thinking = deps.getThinkingIndicator()
    const liveUserAnchor = deps.getCurrentSessionLiveUserAnchor()
    const liveThinkingBlock = deps.getLiveThinkingBlock?.() || null
    th.querySelectorAll<HTMLElement>('.msg').forEach((el) => {
      if (el === keptError) return
      if (deps.isStreaming() && el === streamBubble) return
      if (deps.isStreaming() && el === thinking) return
      if (deps.isStreaming() && el === liveThinkingBlock) return
      if (deps.isStreaming() && el === liveUserAnchor) return
      if (deps.isPendingFinalizedAssistantBubble(el) && historyStillWaitingForAssistant(messages))
        return
      if (!consumed.has(el)) el.remove()
    })
    const pendingFinalized = deps.getPendingFinalizedAssistantBubble()
    if (
      pendingFinalized &&
      (consumed.has(pendingFinalized) ||
        !pendingFinalized.isConnected ||
        !historyStillWaitingForAssistant(messages))
    ) {
      deps.clearPendingFinalizedAssistantBubble()
    }

    // Ask cards rebuilt above come back active; lock the ones a later user
    // message already answered so only a genuinely pending question invites
    // a click.
    lockAnsweredAskCards(th)

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
