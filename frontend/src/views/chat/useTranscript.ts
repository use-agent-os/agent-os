import { useCallback, useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useRpc } from '@/app/providers'
import { createStreamController, type StreamController } from './transcript/stream'
import type { CompactionSummary } from './transcript/compaction'
import {
  CHAT_HISTORY_PAGE_SIZE,
  createHistoryRenderer,
  historyResponseMetadata,
  mergeHistoryMessagePages,
  type HistoryPagingState,
  type HistoryResponse,
} from './transcript/history'
import {
  dayKey,
  dayLabel,
  esc,
  historyFallbackMessageIdentity,
  inputNormalizationProvenanceFromAttachments,
  outgoingAttachment,
  stripTimePrefix,
  type PendingAttachment,
} from './logic'
import type { ChatMessage, Role, StreamEventPayload } from './types'

// app.js:200-207 `getAuthToken` reads the connection token from sessionStorage
// (providers.tsx uses the same key). The artifact renderer appends it to
// preview/download URLs + the download Authorization header (chat.js:7575/7657).
const WS_TOKEN_KEY = 'agentos.wsToken'
function getAuthToken(): string {
  try {
    return sessionStorage.getItem(WS_TOKEN_KEY) || ''
  } catch {
    return ''
  }
}

/**
 * Imperative transcript controller + live wiring.
 *
 * Task 1 built the foundation (the imperative `StreamController` bound to the
 * scroll container). Task 2 built the streaming renderer. THIS hook (Task 3)
 * adds the two pieces that make streaming end-to-end drivable in a browser:
 *
 *  1. **History read + pagination** via react-query (`chat.history`), matching
 *     legacy `_loadHistory` (chat.js:5440) / `_loadEarlierHistory` (chat.js:5492).
 *     The result is rendered by the imperative, controller-driven history
 *     renderer (design §2.1 — history DOM rendering is NOT reactified).
 *  2. **The live WS subscription** — `sessions.messages.subscribe` on mount /
 *     `unsubscribe` on cleanup (chat.js:2857 / 2909) — plus EVERY `rpc.on(...)`
 *     handler the legacy view registers (chat.js:4699-5181), each dispatching
 *     into the Task-2 controller or a clearly-marked later-task seam.
 *
 * StrictMode: the subscription effect is idempotent + fully tears down (every
 * `rpc.on` unsubscribe is collected and called on cleanup, and the effect
 * re-subscribes on re-mount) so a double-invoke never leaks a listener.
 */

/**
 * The later-task event seams. EVERY `session.event.*` handler the legacy view
 * registers is wired below; the ones whose full handling belongs to a later
 * task dispatch into one of these (default no-op) so the SUBSCRIPTION is never
 * silently omitted — later tasks replace the seam with the real handler.
 */
export interface TranscriptEventSeams {
  /** Task 4 — tool_use_start / tool_result rendering (chat.js:4730/4750). */
  appendToolCall?: (payload: StreamEventPayload) => void
  appendToolResult?: (payload: StreamEventPayload) => void
  /** Task 5 — artifact rendering (chat.js:4769). */
  appendArtifact?: (payload: StreamEventPayload) => void
  /** Task 6 — router decision strip (chat.js:4699). */
  handleRouterDecision?: (payload: StreamEventPayload) => void
  /** Task 7 — compaction toast (chat.js:4881). */
  showCompactionToast?: (payload: StreamEventPayload, meta: Record<string, unknown>) => void
  /** Later task — subagent completion row (chat.js:4788). */
  appendSubagentCompletion?: (payload: StreamEventPayload) => void
  /** Later task — cron result row (chat.js:4860). */
  appendCronResult?: (payload: StreamEventPayload) => void
  /** Later task — non-persistent turn warning toast (chat.js:4891). */
  showWarningToast?: (message: string) => void
  /** Later task — run-status chip + Send/Stop affordance (chat.js:1767). */
  applySessionRunState?: (state: Record<string, unknown>) => void
  /** Later task — task-group activity tracking (chat.js:4936-4962). */
  noteTaskGroupActive?: (payload: StreamEventPayload) => void
  noteTaskGroupTerminal?: (payload: StreamEventPayload, status: 'succeeded' | 'failed') => void
  /** Later task — the `*` wildcard terminal/done/error handling (chat.js:4965). */
  handleGenericEvent?: (
    event: string,
    payload: StreamEventPayload,
    meta: Record<string, unknown>,
  ) => void
  /** Later task — session epoch bump (chat.js:4899). */
  onEpochChanged?: (payload: StreamEventPayload) => void
  /** chat.js:* — the diagnostics ring (legacy `_chatDiag`). Default: no-op. */
  diag?: (event: string, detail: Record<string, unknown>) => void
}

/** The current-session predicate: legacy `_isCurrentSessionPayload` (chat.js:1636). */
function isCurrentSessionPayload(
  payload: StreamEventPayload | undefined,
  sessionKey: string,
): boolean {
  const p = payload as { key?: string; session_key?: string; sessionKey?: string } | undefined
  const key = p?.key || p?.session_key || p?.sessionKey || ''
  return !key || !sessionKey || key === sessionKey
}

export function useTranscript(opts: { sessionKey: string; seams?: TranscriptEventSeams }): {
  containerRef: React.RefObject<HTMLDivElement | null>
  controller: StreamController
  /**
   * Send composed text + optional attachments (chat.js:6062 `_onSend` →
   * `chat.send`, chat.js:6193). Attachments ride on the RPC params as legacy
   * (`displayText` + `attachments` + `inputProvenance`, chat.js:6157-6167).
   */
  send: (text: string, attachments?: PendingAttachment[]) => void
  /** Abort the in-flight turn (chat.js:8439 `_onStop` → `chat.abort`, chat.js:8444). */
  abort: (source?: string) => void
  /** Reactive streaming flag (legacy `_isStreaming`) — drives the composer's busy prop. */
  busy: boolean
  /** The user's sent-message history, oldest→newest (legacy `_messages`, chat.js:8712). */
  history: string[]
} {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const containerRef = useRef<HTMLDivElement>(null)

  // Reactive mirror of the imperative `_isStreaming` flag. The controller's
  // `updateSendButton` dep fires on every stream lifecycle transition
  // (chat.js:6571) — we re-read `controller.isStreaming()` there to sync React.
  const [busy, setBusy] = useState(false)

  // The user's sent-message history (legacy derives from `_messages` filtered
  // to role 'user', chat.js:8712-8714). Held as React state so ↑/↓ cycling in
  // the composer stays in sync with what was actually sent this session.
  const [history, setHistory] = useState<string[]>([])

  // Live session key holder (legacy `_sessionKey`), read by the controller and
  // by the event handlers. A ref so the once-created controller + the stable
  // handler closures always see the current value. Written only in an effect.
  const sessionKeyRef = useRef(opts.sessionKey)

  // Later-task seams, held in a ref so the (stable) subscription handlers always
  // read the latest without re-registering. Written in an effect.
  const seamsRef = useRef<TranscriptEventSeams>(opts.seams ?? {})

  // Stable syncer the once-created controller calls (via its `updateSendButton`
  // dep) to push the imperative `_isStreaming` flag into the reactive `busy`
  // state. A ref so the controller initializer never closes over a stale setter.
  const setBusyRef = useRef<() => void>(() => {})

  // chat.js `_historyCompactionSummaries` — the history summary rows, exposed to
  // the (once-created) compaction renderer via a late-bound getter ref so the
  // controller's useState initializer never reads `pagingRef` directly (which
  // React's rules-of-hooks flags as reading a hook value during render). The
  // real getter is installed in an effect once `pagingRef` exists.
  const historyCompactionSummariesRef = useRef<() => CompactionSummary[]>(() => [])

  // eslint-disable-next-line react-hooks/refs -- factory stores the refs and reads .current only later, inside methods invoked outside render (never at creation)
  const [controller] = useState<StreamController>(() =>
    createStreamController(containerRef, {
      getSessionKey: () => sessionKeyRef.current,
      // Artifact preview/download URLs + download Authorization header
      // (chat.js:7575/7657 `App.getAuthToken()`).
      getAuthToken,
      applySessionRunState: (state) => seamsRef.current.applySessionRunState?.(state),
      // chat.js:6571 — the Send/Stop affordance refresh fires on every stream
      // lifecycle transition (start/end/park/restore). Re-read the imperative
      // `_isStreaming` flag here to keep the reactive `busy` mirror in sync so
      // the composer swaps between Send and Abort.
      updateSendButton: () => setBusyRef.current(),
      diag: (event, detail) => seamsRef.current.diag?.(event, detail),
      // Compaction (Task 7): the history summary rows the history load populates
      // live (chat.js `_historyCompactionSummaries`), read via a late-bound
      // getter ref (installed in an effect) so this initializer stays ref-free.
      getHistoryCompactionSummaries: () => historyCompactionSummariesRef.current(),
      // chat.js:5305 `_scheduleHistorySync` → invalidate the history query so a
      // completed/skipped compaction refetches the (now-summarized) transcript.
      scheduleHistorySync: () =>
        void queryClient.invalidateQueries({
          queryKey: ['chat', 'history', sessionKeyRef.current],
        }),
      // Pending-queue recovery (chat.js:8596/8644) is owned by the send flow (a
      // later task); leave the faithful no-op defaults until then.
      // Subagent-completion system row (chat.js:7814 `_addMessage`). No real
      // `_addMessage` DOM builder exists in the frontend yet (router-fx/turn-meta
      // entangled — a later task); provide the same faithful minimal row the
      // history renderer uses so a subagent disclosure renders standalone.
      addMessageWithOptions: (role, text) => {
        const th = containerRef.current
        if (!th) return null
        const empty = th.querySelector('.chat-empty')
        if (empty) empty.remove()
        const div = document.createElement('div')
        div.className = `msg ${role}`
        div.setAttribute('data-history-role', role)
        div.innerHTML = `<div class="msg-body">${esc(text || '')}</div>`
        th.appendChild(div)
        return div
      },
    }),
  )

  // Per-session history paging state (legacy `_history*` module-globals). Held
  // in a ref: the imperative renderer reads/writes it directly, no React render
  // depends on it. Reset when the session key changes.
  const pagingRef = useRef<HistoryPagingState>({
    loadedMessages: [],
    oldestCursor: null,
    hasMore: false,
    scope: 'complete',
    loadingEarlier: false,
    error: '',
    compactionSummaries: [],
  })

  // Late-bound callbacks the renderer's scope-row buttons invoke (set in an
  // effect below). Plain refs — the renderer calls through `.current` lazily.
  const loadEarlierHistoryRef = useRef<() => void>(() => {})
  const reloadHistoryRef = useRef<() => void>(() => {})

  // The imperative history renderer, created once. It reads the live thread +
  // seams lazily inside its methods, so creating it here (with refs closed over)
  // is safe.
  // eslint-disable-next-line react-hooks/refs -- renderer reads containerRef.current only inside methods called outside render
  const [historyRenderer] = useState(() =>
    createHistoryRenderer({
      thread: () => containerRef.current,
      esc,
      displayRoleLabel: (role) => (role ? role.charAt(0).toUpperCase() + role.slice(1) : ''),
      dayKey,
      dayLabel,
      // No `_addMessage` DOM builder exists in the frontend yet; provide a
      // faithful minimal row so history renders standalone. The real
      // `_addMessage` (chat.js:7851, with router-fx/turn-meta) is a later task.
      addMessage: (role, text) => {
        const th = containerRef.current
        if (!th) return null
        const div = document.createElement('div')
        div.className = `msg ${role}`
        div.setAttribute('data-history-role', role)
        div.innerHTML = `<div class="msg-body">${esc(text || '')}</div>`
        th.appendChild(div)
        return div
      },
      attachHoverActions: () => {},
      stampHistoryElement: (el, stableIdentity, role, text, transcriptId = null) => {
        if (stableIdentity) el.setAttribute('data-message-id', stableIdentity)
        el.setAttribute('data-history-role', role || '')
        el.setAttribute('data-history-raw-text', text || '')
        el.setAttribute(
          'data-history-fallback-id',
          historyFallbackMessageIdentity(role as Role, text),
        )
        if (transcriptId != null) {
          el.dataset.transcriptId = String(transcriptId)
        } else {
          delete el.dataset.transcriptId
        }
      },
      stripProtocolTextLeak: (t) => t,
      stripDirectiveTags: (t) => t,
      stripGeneratedArtifactMarkers: (t) => t,
      stripTimePrefix,
      loadEarlierHistory: () => void loadEarlierHistoryRef.current(),
      reloadHistory: () => void reloadHistoryRef.current(),
      isStreaming: () => controller.isStreaming(),
      diag: (event, detail) => seamsRef.current.diag?.(event, detail),
    }),
  )

  // Keep the session-key + seams holders current (effect, never during render).
  useEffect(() => {
    sessionKeyRef.current = opts.sessionKey
  }, [opts.sessionKey])
  useEffect(() => {
    seamsRef.current = opts.seams ?? {}
  }, [opts.seams])
  // Install the compaction-summary getter now that `pagingRef` exists (the
  // controller's late-bound getter reads through this ref).
  useEffect(() => {
    historyCompactionSummariesRef.current = () =>
      pagingRef.current.compactionSummaries as CompactionSummary[]
  }, [])
  // Install the busy syncer: re-read the imperative streaming flag and mirror
  // it into React (setState is a no-op when the value is unchanged, so the
  // frequent lifecycle calls don't cause spurious re-renders).
  useEffect(() => {
    setBusyRef.current = () => setBusy(controller.isStreaming())
  }, [controller])

  /* ── Send / Abort (chat.js:6062 `_onSend` / 8439 `_onStop`) ─────────────── */

  // Reset the abort flag for a new turn (legacy `_aborted`, chat.js:6121). The
  // controller drops deltas while aborted (chat.js:6652); a fresh send clears it.
  const abortedRef = useRef(false)

  // chat.js:6062-6205 `_onSend`, plain-text path. The streaming-branch enqueue
  // (chat.js:6091), slash commands (chat.js:6113), and attachments (chat.js:6157)
  // are Task-9 seams. For plain text: add the user bubble, start the streaming
  // UI, and fire `chat.send` with the legacy `{ message, sessionKey }` shape
  // (chat.js:6150). The already-wired subscription renders the resulting stream.
  const send = useCallback(
    (text: string, attachments: PendingAttachment[] = []) => {
      const trimmed = (text ?? '').trim()
      const sessionKey = sessionKeyRef.current
      const atts = attachments ?? []
      // chat.js:6064/6118 — `hasPayload = text || _pendingAttachments.length > 0`;
      // an attachments-only send (empty text) is allowed. No session, no payload,
      // or already streaming (Task-13 enqueue seam) is a no-op.
      const hasPayload = Boolean(trimmed) || atts.length > 0
      if (!hasPayload || !sessionKey || controller.isStreaming()) return

      abortedRef.current = false
      // chat.js:6128 — history/↑↓ tracks the user's display text (not the provider
      // fallback). Only record non-empty text so an attachments-only send doesn't
      // seed a blank history entry.
      if (trimmed) setHistory((prev) => [...prev, trimmed])

      // chat.js:6129 — the model receives a fallback prompt when text is empty but
      // attachments are present ("Describe these attachments").
      const userText = trimmed
      const providerText = trimmed || 'Describe these attachments'

      // Show the user's message row (chat.js:6133 `_addMessage('user', …)`). The
      // controller's minimal row builder (the same one history/subagent use)
      // stands in until the full `_addMessage` lands with router-fx/turn-meta.
      const th = containerRef.current
      if (th) {
        const empty = th.querySelector('.chat-empty')
        if (empty) empty.remove()
        const div = document.createElement('div')
        div.className = 'msg user'
        div.setAttribute('data-history-role', 'user')
        // chat.js:6136-6144 — render the attachments block when present.
        if (atts.length > 0) {
          const bodyText = userText ? `<div class="msg-attachment-text">${esc(userText)}</div>` : ''
          const chips = atts
            .map((a) => {
              const mime = a.mime || ''
              if (mime.startsWith('image/') && (a.dataUrl || a.data)) {
                const src = a.dataUrl || `data:${esc(mime || 'image/png')};base64,${a.data}`
                return `<img class="msg-thumb" src="${src}" alt="${esc(a.name)}">`
              }
              return `<span class="msg-file-chip"><span class="msg-file-chip__name">${esc(
                a.name,
              )}</span><span class="msg-file-chip__meta">${esc(mime || 'attachment')}</span></span>`
            })
            .join('')
          div.innerHTML = `<div class="msg-body msg-body--has-attachments">${bodyText}<div class="msg-attachments">${chips}</div></div>`
        } else {
          div.innerHTML = `<div class="msg-body">${esc(userText)}</div>`
        }
        th.appendChild(div)
      }

      // Start streaming UI (chat.js:6178) + thinking indicator (chat.js:6190).
      controller.startStreaming()
      controller.showThinkingIndicator()
      controller.scrollToBottom()

      // chat.js:6150-6167 — the RPC params. Attachments ride as `displayText` +
      // `attachments` (staged → file_uuid, else inline base64) + `inputProvenance`
      // for a normalization-generated attachment. Intent / _source elevated mode
      // are later-task seams.
      const params: Record<string, unknown> = { message: providerText, sessionKey }
      if (atts.length > 0) {
        params.displayText = userText
        params.attachments = atts.map(outgoingAttachment)
        const provenance = inputNormalizationProvenanceFromAttachments(atts)
        if (provenance) params.inputProvenance = provenance
      }
      seamsRef.current.diag?.('send.start', {
        textLen: providerText.length,
        attachments: atts.length,
      })
      rpc
        .call('chat.send', params)
        .then((res) => {
          const r = res as { sessionKey?: string } | null
          seamsRef.current.diag?.('send.rpc.resolved', {
            responseSessionKey: r?.sessionKey || '',
          })
        })
        .catch((err: unknown) => {
          const message = err instanceof Error ? err.message : String(err)
          seamsRef.current.diag?.('send.rpc.error', { message })
          // chat.js:6202-6203 — end streaming + surface the failure inline.
          if (controller.isStreaming()) controller.endStreaming()
          const th2 = containerRef.current
          if (th2) {
            const div = document.createElement('div')
            div.className = 'msg error'
            div.innerHTML = `<div class="msg-body">${esc('Send failed: ' + message)}</div>`
            th2.appendChild(div)
          }
        })
    },
    [rpc, controller],
  )

  // chat.js:8439-8450 `_onStop`. Abort only while streaming; set the abort flag,
  // fire `chat.abort` with `{ sessionKey, source }` (chat.js:8444), and end the
  // stream locally with `reason:'aborted'`. Pending-queue recovery (chat.js:8448)
  // is a Task-9 seam.
  const abort = useCallback(
    (source = 'webui_stop_button') => {
      if (!controller.isStreaming()) return
      abortedRef.current = true
      const src = typeof source === 'string' && source ? source : 'webui_stop_button'
      rpc.call('chat.abort', { sessionKey: sessionKeyRef.current, source: src }).catch(() => {})
      controller.endStreaming({ reason: 'aborted' })
    },
    [rpc, controller],
  )

  // Reset the sent-message history when the session changes (a switch must not
  // let one session's ↑/↓ history bleed into another's; legacy `_messages` is
  // per-session state rebuilt from history — chat.js destroy/rebind). React's
  // "adjust state during render on a prop change" pattern
  // (https://react.dev/learn/you-might-not-need-an-effect): track the previous
  // key in STATE (not a ref) so the reset runs during render without a
  // setState-in-effect cascade.
  const [historySession, setHistorySession] = useState(opts.sessionKey)
  if (historySession !== opts.sessionKey) {
    setHistorySession(opts.sessionKey)
    setHistory([])
  }

  /* ── History read via react-query (chat.js:5440 `_loadHistory`) ─────────── */

  const historyQuery = useQuery<HistoryResponse>({
    queryKey: ['chat', 'history', opts.sessionKey],
    queryFn: async () => {
      await rpc.waitForConnection()
      // Legacy `_loadHistory` params (chat.js:5457-5462): `sessionKey`, `limit`,
      // `includeCanonical:false`, `includeSummaries:true`.
      return rpc.call<HistoryResponse>('chat.history', {
        sessionKey: opts.sessionKey,
        limit: CHAT_HISTORY_PAGE_SIZE,
        includeCanonical: false,
        includeSummaries: true,
      })
    },
    // Legacy re-issues the initial load on view entry / reconnect / after a turn
    // (via _scheduleHistorySync); react-query owns the initial + refresh, and
    // the subscription's _gap/terminal paths invalidate this key to resync.
    staleTime: 0,
    retry: false,
    refetchOnWindowFocus: false,
  })

  // Render the initial page whenever a fresh history response settles for the
  // CURRENT session (chat.js:5467-5479). The render is imperative; gate on the
  // response identity so we render each settled page exactly once.
  const renderedResponseRef = useRef<HistoryResponse | null>(null)
  useEffect(() => {
    const data = historyQuery.data
    if (!data || renderedResponseRef.current === data) return
    renderedResponseRef.current = data
    const messages = (data.messages || []) as ChatMessage[]
    const meta = historyResponseMetadata(data)
    pagingRef.current = {
      loadedMessages: messages.slice(),
      oldestCursor: meta.oldestCursor,
      hasMore: meta.hasMore,
      scope: meta.scope,
      loadingEarlier: false,
      error: '',
      compactionSummaries: meta.summaries,
    }
    historyRenderer.renderHistoryMessages(messages, pagingRef.current)
    // chat.js:3119 — overlay the history compaction-summary separators once the
    // message rows exist (the history renderer is a focused port; this is folded
    // in here, reading the summaries the controller was given above).
    controller.renderCompactionSummarySeparators(messages)
  }, [historyQuery.data, historyRenderer, controller])

  // Surface a history-load error into the scope row (chat.js:5484-5488).
  useEffect(() => {
    if (!historyQuery.isError) return
    pagingRef.current.error = 'Could not load chat history.'
    historyRenderer.renderHistoryScopeRow(pagingRef.current)
  }, [historyQuery.isError, historyRenderer])

  /* ── Backward pagination (chat.js:5492 `_loadEarlierHistory`) ───────────── */

  const loadingEarlierGuard = useRef(false)
  useEffect(() => {
    loadEarlierHistoryRef.current = async () => {
      const th = containerRef.current
      const state = pagingRef.current
      if (!opts.sessionKey || !th || !state.oldestCursor || loadingEarlierGuard.current) return
      const requestSessionKey = opts.sessionKey
      const previousScrollHeight = th.scrollHeight
      const previousScrollTop = th.scrollTop
      loadingEarlierGuard.current = true
      state.loadingEarlier = true
      state.error = ''
      historyRenderer.renderHistoryScopeRow(state)
      try {
        await rpc.waitForConnection()
        // chat.js:5507-5513 — the load-earlier params add `before: <oldestCursor>`.
        const data = await rpc.call<HistoryResponse>('chat.history', {
          sessionKey: requestSessionKey,
          limit: CHAT_HISTORY_PAGE_SIZE,
          before: state.oldestCursor,
          includeCanonical: false,
          includeSummaries: true,
        })
        if (requestSessionKey !== sessionKeyRef.current) return
        const olderMessages = (data.messages || []) as ChatMessage[]
        const mergedMessages = mergeHistoryMessagePages(olderMessages, state.loadedMessages)
        const meta = historyResponseMetadata(data)
        state.loadedMessages = mergedMessages
        state.oldestCursor = meta.oldestCursor
        state.hasMore = meta.hasMore
        state.scope = meta.scope
        state.compactionSummaries = meta.summaries
        state.loadingEarlier = false
        historyRenderer.renderHistoryMessages(mergedMessages, state, {
          preserveScroll: true,
          previousScrollHeight,
          previousScrollTop,
        })
        // Re-overlay the compaction-summary separators against the new row set.
        controller.renderCompactionSummarySeparators(mergedMessages)
      } catch {
        state.loadingEarlier = false
        state.error = 'Could not load earlier history.'
        historyRenderer.renderHistoryScopeRow(state)
      } finally {
        loadingEarlierGuard.current = false
      }
    }
    reloadHistoryRef.current = () => {
      void queryClient.invalidateQueries({ queryKey: ['chat', 'history', opts.sessionKey] })
    }
  }, [opts.sessionKey, historyRenderer, rpc, queryClient, controller])

  /* ── Live WS subscription + all rpc.on handlers (chat.js:2857/4699-5181) ── */

  useEffect(() => {
    const sessionKey = opts.sessionKey
    const unsubs: Array<() => void> = []
    let cancelled = false
    const seams = () => seamsRef.current
    const diag = (event: string, detail: Record<string, unknown>) =>
      seamsRef.current.diag?.(event, detail)

    // `_isCurrentSessionPayload` reads the LIVE session key (via ref) so a
    // late-arriving frame after a session switch is still correctly dropped.
    const isForeign = (payload: StreamEventPayload | undefined): boolean =>
      !isCurrentSessionPayload(payload, sessionKeyRef.current)

    const resyncHistory = () =>
      void queryClient.invalidateQueries({ queryKey: ['chat', 'history', sessionKeyRef.current] })

    // ── Subscribe (chat.js:2857 `_subscribeSession`) ──────────────────────
    const subscribe = async () => {
      try {
        await rpc.waitForConnection()
        if (cancelled || sessionKey !== sessionKeyRef.current) return
        const res = (await rpc.call('sessions.messages.subscribe', {
          key: sessionKey,
        })) as { subscribed?: boolean; replay_complete?: boolean } | null
        if (cancelled || sessionKey !== sessionKeyRef.current) return
        if (res && res.subscribed === false) throw new Error('No subscription manager available')
        seams().applySessionRunState?.((res as Record<string, unknown>) ?? {})
        // A replay gap means we may have missed live events → resync history
        // (chat.js:2874-2887, the replay_complete === false → _loadHistory path).
        if (res && res.replay_complete === false) resyncHistory()
      } catch {
        // Legacy toasts here (chat.js:2905); the toast surface is a later task.
        diag('session.subscribe.error', { sessionKey })
      }
    }

    // chat.js:2909 `_unsubscribeSession`.
    const unsubscribe = () => {
      rpc.call('sessions.messages.unsubscribe', { key: sessionKey }).catch(() => {})
    }

    // Common pre-dispatch gate for a session.event.* streaming frame: drop
    // foreign-session payloads (chat.js:1688), then the seq gate (Task-2).
    const gateStreamFrame = (event: string, payload: StreamEventPayload): boolean => {
      if (isForeign(payload)) {
        diag(`${event}.drop.foreign_session`, {})
        return false
      }
      if (!controller.acceptStreamSeq(payload)) {
        diag(`${event}.drop.stream_seq`, {})
        return false
      }
      return true
    }

    // Typed rpc.on adapters. `rpc.on`'s Handler is `(...args: unknown[])`; these
    // narrow the payload/meta at the single seam so each handler body stays typed.
    const onEvent = (
      event: string,
      handler: (payload: StreamEventPayload, meta: Record<string, unknown>) => void,
    ) =>
      rpc.on(event, (payload, meta) =>
        handler((payload as StreamEventPayload) ?? {}, (meta as Record<string, unknown>) ?? {}),
      )

    // ── Register EVERY rpc.on(...) the legacy view registers ──────────────

    // chat.js:4699 — router_decision → controller router-fx renderer (Task 6).
    unsubs.push(
      onEvent('session.event.router_decision', (payload: StreamEventPayload) => {
        if (!gateStreamFrame('event.router_decision', payload)) return
        void controller.handleRouterDecision(payload as Record<string, unknown>)
        seams().handleRouterDecision?.(payload)
      }),
    )

    // chat.js:4714 — text_delta → Task-2 controller stream path.
    unsubs.push(
      onEvent('session.event.text_delta', (payload: StreamEventPayload) => {
        if (!gateStreamFrame('event.text_delta', payload)) return
        controller.resetStreamIdleTimer()
        controller.appendDelta((payload as { text?: string }).text || '')
      }),
    )

    // chat.js:4730 — tool_use_start → controller tool renderer (Task 4).
    unsubs.push(
      onEvent('session.event.tool_use_start', (payload: StreamEventPayload) => {
        if (!gateStreamFrame('event.tool_use_start', payload)) return
        controller.resetStreamIdleTimer()
        controller.appendToolCall(payload)
        seams().appendToolCall?.(payload)
      }),
    )

    // chat.js:4750 — tool_result → controller tool renderer (Task 4).
    unsubs.push(
      onEvent('session.event.tool_result', (payload: StreamEventPayload) => {
        if (!gateStreamFrame('event.tool_result', payload)) return
        controller.resetStreamIdleTimer()
        controller.appendToolResult(payload)
        seams().appendToolResult?.(payload)
      }),
    )

    // chat.js:4769 — artifact → controller artifact renderer (Task 5).
    unsubs.push(
      onEvent('session.event.artifact', (payload: StreamEventPayload) => {
        if (!gateStreamFrame('event.artifact', payload)) return
        controller.resetStreamIdleTimer()
        controller.appendArtifact(payload)
        seams().appendArtifact?.(payload)
      }),
    )

    // chat.js:4788 — subagent_completion → controller tool renderer (Task 4).
    unsubs.push(
      onEvent('session.event.subagent_completion', (payload: StreamEventPayload) => {
        if (!gateStreamFrame('event.subagent_completion', payload)) return
        controller.appendSubagentCompletion(payload)
        seams().appendSubagentCompletion?.(payload)
      }),
    )

    // chat.js:4807 — state_change → thinking indicator (Task-2 controller).
    unsubs.push(
      onEvent('session.event.state_change', (payload: StreamEventPayload) => {
        if (!payload) return
        if (!gateStreamFrame('event.state_change', payload)) return
        controller.resetStreamIdleTimer()
        const p = payload as { to_state?: string; toState?: string }
        const to = p.to_state || p.toState || ''
        // Only SHOW thinking on a thinking transition; hiding is owned by the
        // controller's ensureStreamBubble (chat.js:4824-4832).
        if (to === 'thinking' && !controller.isStreaming()) {
          controller.startStreaming()
          controller.showThinkingIndicator()
        } else if (to === 'thinking') {
          controller.showThinkingIndicator()
        }
      }),
    )

    // chat.js:4835 — run_heartbeat → keep-alive + awaiting-model hint / thinking.
    unsubs.push(
      onEvent('session.event.run_heartbeat', (payload: StreamEventPayload) => {
        if (!gateStreamFrame('event.run_heartbeat', payload)) return
        if (!controller.isStreaming()) controller.startStreaming()
        controller.resetStreamIdleTimer()
        if (!controller.showAwaitingModelHintAfterToolResult()) {
          controller.showThinkingIndicator()
        }
      }),
    )

    // chat.js:4860 — cron_result → later-task seam.
    unsubs.push(
      onEvent('session.event.cron_result', (payload: StreamEventPayload) => {
        if (!gateStreamFrame('event.cron_result', payload)) return
        seams().appendCronResult?.(payload)
      }),
    )

    // chat.js:4881 — compaction → controller compaction renderer (Task 7). The
    // renderer drives the in-thread context separator + in-flight controls +
    // router-fx compaction-turn suppression; the seam stays as an optional
    // observer hook for a later task (it never replaces the real handler).
    unsubs.push(
      onEvent(
        'session.event.compaction',
        (payload: StreamEventPayload, meta: Record<string, unknown>) => {
          if (!gateStreamFrame('event.compaction', payload)) return
          controller.showCompactionToast(payload as Record<string, unknown>, meta)
          seams().showCompactionToast?.(payload, meta)
        },
      ),
    )

    // chat.js:4891 — warning → toast only, never written to the transcript.
    unsubs.push(
      onEvent('session.event.warning', (payload: StreamEventPayload) => {
        if (isForeign(payload)) return
        const message = (payload as { message?: string })?.message || 'Cap warning'
        seams().showWarningToast?.(message)
      }),
    )

    // chat.js:4899 — epoch_changed → later-task seam (epoch tracking).
    unsubs.push(
      onEvent('session.epoch_changed', (payload: StreamEventPayload) => {
        if (isForeign(payload)) return
        seams().onEpochChanged?.(payload)
      }),
    )

    // chat.js:4909 — sessions.changed → terminal resync or run-state apply.
    unsubs.push(
      onEvent('sessions.changed', (payload: StreamEventPayload) => {
        if (!isCurrentSessionPayload(payload, sessionKeyRef.current)) return
        // Terminal session change → end streaming + resync history
        // (chat.js:1713 `_syncTerminalSessionChange`). We conservatively resync
        // history + apply run-state; the fine-grained terminal recovery
        // (pending-queue drain / composer restore) is owned by the send task.
        const reason = String((payload as { reason?: string }).reason || '').toLowerCase()
        const status = String((payload as { status?: string }).status || '').toLowerCase()
        const isTerminal =
          reason === 'turn_complete' ||
          reason === 'task_terminal' ||
          ['done', 'failed', 'killed', 'timeout'].includes(status)
        if (isTerminal) {
          if (controller.isStreaming()) controller.endStreaming()
          seams().applySessionRunState?.((payload as Record<string, unknown>) ?? {})
          resyncHistory()
          return
        }
        seams().applySessionRunState?.((payload as Record<string, unknown>) ?? {})
      }),
    )

    // chat.js:4919 — task.queued → run-state seam.
    unsubs.push(
      onEvent('task.queued', (payload: StreamEventPayload) => {
        if (!isCurrentSessionPayload(payload, sessionKeyRef.current)) return
        seams().applySessionRunState?.({
          run_status: 'queued',
          active_task: { ...(payload || {}), status: 'queued' },
        })
      }),
    )

    // chat.js:4928 — task.running → run-state seam.
    unsubs.push(
      onEvent('task.running', (payload: StreamEventPayload) => {
        if (!isCurrentSessionPayload(payload, sessionKeyRef.current)) return
        seams().applySessionRunState?.({
          run_status: 'running',
          active_task: { ...(payload || {}), status: 'running' },
        })
      }),
    )

    // chat.js:4936-4962 — task_group.{waiting,synthesizing,done,failed}.
    unsubs.push(
      onEvent('session.event.task_group.waiting', (payload: StreamEventPayload) => {
        if (!gateStreamFrame('event.task_group.waiting', payload)) return
        seams().noteTaskGroupActive?.(payload)
      }),
    )
    unsubs.push(
      onEvent('session.event.task_group.synthesizing', (payload: StreamEventPayload) => {
        if (!gateStreamFrame('event.task_group.synthesizing', payload)) return
        seams().noteTaskGroupActive?.(payload)
      }),
    )
    unsubs.push(
      onEvent('session.event.task_group.done', (payload: StreamEventPayload) => {
        if (!gateStreamFrame('event.task_group.done', payload)) return
        seams().noteTaskGroupTerminal?.(payload, 'succeeded')
      }),
    )
    unsubs.push(
      onEvent('session.event.task_group.failed', (payload: StreamEventPayload) => {
        if (!gateStreamFrame('event.task_group.failed', payload)) return
        seams().noteTaskGroupTerminal?.(payload, 'failed')
      }),
    )

    // chat.js:4965 — the `*` wildcard: terminal task events + `.done`/`.error`
    // finalization + usage. This is the largest handler and its full body
    // (usage accumulation, savings popup, turn-meta, pending-queue drain) is
    // owned by later tasks (send + router-fx + usage). Register the wildcard
    // NOW and dispatch the whole raw frame into the seam so nothing is dropped;
    // as a faithful backstop, finalize a live stream on a `.done`/`.error` frame
    // for the current session so streaming ends even before the seam is wired.
    unsubs.push(
      rpc.on('*', (rawEventArg: unknown, rawPayloadArg: unknown, rawMetaArg: unknown) => {
        if (typeof rawEventArg !== 'string') return
        const rawEvent = rawEventArg
        const rawPayload = (rawPayloadArg as StreamEventPayload) ?? {}
        const rawMeta = (rawMetaArg as Record<string, unknown>) ?? {}
        seams().handleGenericEvent?.(rawEvent, rawPayload, rawMeta)
        // Faithful backstop until the send/usage task wires handleGenericEvent:
        // finalize streaming on a current-session terminal frame so the bubble
        // is committed (chat.js:5033 `.done` / 5142 `.error` end-streaming path).
        if (seams().handleGenericEvent) return
        if (!rawEvent.startsWith('session.event.')) {
          if (
            (rawEvent.endsWith('.done') || rawEvent === 'chat.done') &&
            isCurrentSessionPayload(rawPayload, sessionKeyRef.current)
          ) {
            const finalText = (rawPayload as { text?: string })?.text
            if (typeof finalText === 'string' && finalText) {
              controller.reconcileFinalStreamText(finalText)
            }
            const wasAborted = (rawPayload as { reason?: string })?.reason === 'aborted'
            if (controller.isStreaming())
              controller.endStreaming(wasAborted ? { reason: 'aborted' } : undefined)
            resyncHistory()
          } else if (
            rawEvent.endsWith('.error') &&
            isCurrentSessionPayload(rawPayload, sessionKeyRef.current)
          ) {
            if (controller.isStreaming()) controller.endStreaming()
            resyncHistory()
          }
        }
      }),
    )

    // chat.js:5159 — `_state`: on (re)connect, apply policy + (re)subscribe +
    // resync history; on disconnect while streaming, keep the thinking indicator.
    unsubs.push(
      rpc.on('_state', (state: unknown) => {
        if (state === 'connected' && sessionKeyRef.current) {
          controller.applyRpcPolicy(rpc.policy)
          controller.hideThinkingIndicator()
          void subscribe()
          resyncHistory()
        }
        if (state === 'disconnected' && controller.isStreaming()) {
          controller.clearStreamIdleTimer()
          controller.showThinkingIndicator()
        }
      }),
    )

    // chat.js:5173 — `_hello`: apply the negotiated RPC policy (idle grace, …).
    unsubs.push(
      rpc.on('_hello', (hello: unknown) => {
        const policy = (hello as { policy?: Record<string, unknown> })?.policy
        controller.applyRpcPolicy(policy ?? {})
      }),
    )

    // chat.js:5177 — `_gap`: a live-stream frame gap → clear the idle timer,
    // warn, and resync terminal history (`_syncTerminalSessionChange` path).
    unsubs.push(
      rpc.on('_gap', () => {
        if (!controller.isStreaming()) return
        controller.clearStreamIdleTimer()
        seams().showWarningToast?.('Stream connection gap detected; reconnecting.')
        // Terminal-history resync: the socket will reconnect and re-subscribe
        // (chat.js:1713 / 5177) — refresh history so the transcript is whole.
        resyncHistory()
      }),
    )

    // Kick off the subscription (mirrors legacy's subscribe-on-view-entry).
    void subscribe()

    return () => {
      cancelled = true
      // StrictMode-safe teardown: drop every registered rpc.on handler and
      // unsubscribe from the session so a re-mount never double-registers.
      unsubs.forEach((off) => off())
      unsubscribe()
    }
  }, [opts.sessionKey, rpc, controller, queryClient])

  // Reset per-session paging state when the session key changes so a switch
  // does not merge one session's pages into another's.
  useEffect(() => {
    pagingRef.current = {
      loadedMessages: [],
      oldestCursor: null,
      hasMore: false,
      scope: 'complete',
      loadingEarlier: false,
      error: '',
      compactionSummaries: [],
    }
    renderedResponseRef.current = null
  }, [opts.sessionKey])

  // On unmount, tear down any live stream timers/rAF (parity legacy destroy —
  // see the Task-2 report). `clearViewLocalStreamState` does not itself cancel
  // the stream-active-mark reveal timer, so call it explicitly to match
  // destroy's unconditional cleanup (chat.js:8788).
  useEffect(() => {
    return () => {
      controller.clearViewLocalStreamState('unmount')
      controller.clearStreamActiveMarkReveal()
    }
  }, [controller])

  return { containerRef, controller, send, abort, busy, history }
}
