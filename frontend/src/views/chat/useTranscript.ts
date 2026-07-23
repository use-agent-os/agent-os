import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { useApprovals } from '@/services/approval-monitor'
import { chatMarkdown } from './markdown'
import {
  createStreamController,
  type StreamController,
  type TranscriptHeaderStateRef,
} from './transcript/stream'
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
  routerFxNormalizeTier,
  routerFxRequestKindFromAttachments,
  routerFxResolveLayoutSeed,
} from './transcript/routerFx'
import {
  createMessageRenderer,
  historyTurnMeta,
  recallTurnMeta,
  storeTurnMeta,
  type MessageRenderer,
  type TurnUsage,
} from './transcript/message'
import {
  agentIdFromSessionKey,
  dayKey,
  dayLabel,
  esc,
  historyFallbackMessageIdentity,
  inputNormalizationProvenanceFromAttachments,
  outgoingAttachment,
  renderMessageAttachmentHtml,
  replayGapShouldWarn,
  sessionChangeIsTerminal,
  sessionRunStatus,
  stripDirectiveTags,
  stripGeneratedArtifactMarkers,
  stripProtocolTextLeak,
  stripTimePrefix,
  subscribeResultNeedsTerminalHistorySync,
  type RunStatusResult,
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
 *     into the transcript controller or an explicit observer seam.
 *
 * StrictMode: the subscription effect is idempotent + fully tears down (every
 * `rpc.on` unsubscribe is collected and called on cleanup, and the effect
 * re-subscribes on re-mount) so a double-invoke never leaks a listener.
 */

/**
 * Optional observers for transcript events. The real UI behavior is handled in
 * this hook/controller first; these callbacks let diagnostics and integration
 * tests observe the same frames without replacing production behavior.
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
  /** Observe a rendered subagent completion row (chat.js:4788). */
  appendSubagentCompletion?: (payload: StreamEventPayload) => void
  /** Observe a rendered cron result row (chat.js:4860). */
  appendCronResult?: (payload: StreamEventPayload) => void
  /** Observe a non-persistent turn warning toast (chat.js:4891). */
  showWarningToast?: (message: string) => void
  /** Observe a run-status update (chat.js:1767). */
  applySessionRunState?: (state: Record<string, unknown>) => void
  /** Observe task-group activity (chat.js:4936-4962). */
  noteTaskGroupActive?: (payload: StreamEventPayload) => void
  noteTaskGroupTerminal?: (payload: StreamEventPayload, status: 'succeeded' | 'failed') => void
  /** Observe the `*` wildcard terminal/done/error handling (chat.js:4965). */
  handleGenericEvent?: (
    event: string,
    payload: StreamEventPayload,
    meta: Record<string, unknown>,
  ) => void
  /** Observe a session epoch bump (chat.js:4899). */
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

/**
 * Pending-queue drain/recover delegates the controller calls on terminal /
 * compaction-settle events (chat.js:8644/8596/8681). The QUEUE itself lives in
 * ChatPage (composer-adjacent state); ChatPage installs these via
 * `setPendingDelegates` so the controller can drive the real drain without the
 * queue leaking into the transcript layer.
 */
export interface PendingDelegates {
  /** chat.js:8644 — debounced FIFO drain after a natural terminal event. */
  schedulePendingDrainAfterTerminal: () => void
  /** chat.js:8596 — recover the whole queue into the composer (returns recovered). */
  popAllPendingIntoComposer: () => boolean
  /** chat.js:8683 — the current queue length (for preservePending). */
  pendingQueueLength: () => number
}

interface ChatRouterConfigResponse {
  agentos_router?: {
    enabled?: boolean
    tiers?: Record<
      string,
      { model?: string; supports_image?: boolean; image_only?: boolean } | null | undefined
    >
  }
}

interface SearchProviderResponse {
  provider?: string
}

function createRouterConfigGate(): { promise: Promise<void>; resolve: () => void } {
  let release: (() => void) | undefined
  let resolved = false
  const promise = new Promise<void>((resolve) => {
    release = resolve
  })
  return {
    promise,
    resolve: () => {
      if (resolved) return
      resolved = true
      release?.()
    },
  }
}

/** chat.js:3569-3575 — config readiness gate with the legacy 1500ms ceiling. */
export function awaitRouterConfigReady(
  configReady: Promise<void>,
  timeoutMs = 1500,
): Promise<void> {
  return new Promise((resolve) => {
    const timer = window.setTimeout(resolve, timeoutMs)
    configReady.then(
      () => {
        window.clearTimeout(timer)
        resolve()
      },
      () => {
        window.clearTimeout(timer)
        resolve()
      },
    )
  })
}

export function useTranscript(opts: {
  sessionKey: string
  seams?: TranscriptEventSeams
  /** Opens the full tool-result dialog rendered by ChatPage (chat.js:7311). */
  openModal?: (title: string, html: string, buttons: Array<Record<string, unknown>>) => void
  onEditMessage?: (text: string) => void
  onRegenerateMessage?: (text: string) => void
  onSessionKeyResolved?: (key: string) => void
}): {
  containerRef: React.RefObject<HTMLDivElement | null>
  routerFxDockRef: React.RefObject<HTMLDivElement | null>
  controller: StreamController
  /**
   * Send composed text + optional attachments (chat.js:6062 `_onSend` →
   * `chat.send`, chat.js:6193). Attachments ride on the RPC params as legacy
   * (`displayText` + `attachments` + `inputProvenance`, chat.js:6157-6167).
   */
  send: (text: string, attachments?: PendingAttachment[], intent?: string | null) => void
  /** Abort the in-flight turn (chat.js:8439 `_onStop` → `chat.abort`, chat.js:8444). */
  abort: (source?: string) => void
  /** Reactive streaming flag (legacy `_isStreaming`) — drives the composer's busy prop. */
  busy: boolean
  /** Reactive mirror of the browser-local router visual preference. */
  routerFxEnabled: boolean
  /** Persist and apply the router visual preference. */
  setRouterFxEnabled: (enabled: boolean) => void
  /** The user's sent-message history, oldest→newest (legacy `_messages`, chat.js:8712). */
  history: string[]
  /** Current session run state rendered by the header chip. */
  runState: RunStatusResult
  /**
   * True while a compaction is in flight for the CURRENT session (chat.js:8660
   * `_isCompactInFlightForCurrentSession`). ChatPage reads it for the
   * enqueue-while-busy branch (chat.js:6091).
   */
  isCompactInFlightForCurrentSession: () => boolean
  /**
   * chat.js:6216 `_setStreamIdlePausedForApproval` — pause the idle timer + flip
   * run-status to `approval_pending` (or resume). Wired to `useApprovalPending`.
   */
  setStreamIdlePausedForApproval: (paused: boolean) => void
  /**
   * Install the pending-queue drain/recover delegates (ChatPage owns the queue).
   * Called in an effect after ChatPage's `usePendingQueue` primitives exist.
   */
  setPendingDelegates: (delegates: PendingDelegates) => void
} {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const containerRef = useRef<HTMLDivElement>(null)
  const routerFxDockRef = useRef<HTMLDivElement>(null)
  const routerFeatureEnabledRef = useRef(false)
  const [routerConfigGate] = useState(createRouterConfigGate)

  // chat.js:1470-1535 `_loadFeatureToggles` — the animation engine needs the
  // configured tier roster before a decision can become a multi-candidate strip.
  // Keep this global config read independent of the session-keyed history query.
  const routerConfigQuery = useQuery<ChatRouterConfigResponse>({
    queryKey: ['config.get', 'chat-router-fx'],
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<ChatRouterConfigResponse>('config.get')
    },
    retry: false,
    staleTime: 0,
    refetchOnWindowFocus: true,
  })
  // chat.js:1203-1209 — warm the web_search badge on view entry. Tool-result
  // payloads remain the fallback source when this optional lookup fails.
  const searchProviderQuery = useQuery<SearchProviderResponse>({
    queryKey: ['tools.search_provider', 'chat'],
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<SearchProviderResponse>('tools.search_provider', {})
    },
    retry: false,
    staleTime: 0,
    refetchOnWindowFocus: true,
  })

  // Reactive mirror of the imperative `_isStreaming` flag. The controller's
  // `updateSendButton` dep fires on every stream lifecycle transition
  // (chat.js:6571) — we re-read `controller.isStreaming()` there to sync React.
  const [busy, setBusy] = useState(false)

  const idleRunState = sessionRunStatus(undefined)
  const [runState, setRunState] = useState<RunStatusResult>(idleRunState)
  const runStateRef = useRef<RunStatusResult>(idleRunState)
  const activeTaskGroupsRef = useRef(new Set<string>())
  const currentEpochRef = useRef(0)
  const applySessionRunState = useCallback((source: Record<string, unknown>) => {
    const next = sessionRunStatus(source)
    runStateRef.current = next
    setRunState(next)
  }, [])

  // The user's sent-message history (legacy derives from `_messages` filtered
  // to role 'user', chat.js:8712-8714). Held as React state so ↑/↓ cycling in
  // the composer stays in sync with what was actually sent this session.
  const [history, setHistory] = useState<string[]>([])

  // Live session key holder (legacy `_sessionKey`), read by the controller and
  // by the event handlers. A ref so the once-created controller + the stable
  // handler closures always see the current value. Written only in an effect.
  const sessionKeyRef = useRef(opts.sessionKey)

  // Event observers, held in a ref so the (stable) subscription handlers always
  // read the latest without re-registering. Written in an effect.
  const seamsRef = useRef<TranscriptEventSeams>(opts.seams ?? {})

  // The controller is created once, while the React-owned modal callback may
  // change across renders. Route it through a ref so every View-full click sees
  // the latest callback without rebuilding the imperative transcript controller.
  const openModalRef = useRef(opts.openModal)
  const messageActionsRef = useRef({
    onEdit: opts.onEditMessage,
    onRegenerate: opts.onRegenerateMessage,
    onSessionKeyResolved: opts.onSessionKeyResolved,
  })

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

  // Pending-queue drain/recover delegates (chat.js:8596/8644/8681). The QUEUE is
  // owned by ChatPage (composer-adjacent); ChatPage installs the real delegates
  // via `setPendingDelegates` after mount. Held in a ref so the once-created
  // controller reads the latest without re-creating. Faithful no-ops until wired.
  const pendingDelegatesRef = useRef<PendingDelegates>({
    schedulePendingDrainAfterTerminal: () => {},
    popAllPendingIntoComposer: () => false,
    pendingQueueLength: () => 0,
  })

  // Reset the abort flag for a new turn (legacy `_aborted`, chat.js:6121). The
  // controller drops deltas while aborted (chat.js:6652); a fresh send clears it.
  // Declared BEFORE the controller init so the `isAborted` dep can read it (refs
  // are stable, so identifier ordering — not creation timing — is what matters).
  const abortedRef = useRef(false)

  const messageRendererRef = useRef<MessageRenderer | null>(null)
  // Router-decision replay caches frames until history has rebuilt user anchors
  // (chat.js `_historyHasRendered` / `_historyHydrating`). These refs are read
  // lazily by the imperative controller and reset on every session boundary.
  const historyHasRenderedRef = useRef(false)
  const historyHydratingRef = useRef(false)
  const historySettledSessionRef = useRef('')
  const subscriptionSettledSessionRef = useRef('')
  const [subscriptionSettleRevision, setSubscriptionSettleRevision] = useState(0)
  const routerConfigAppliedRef = useRef(false)
  const headerStateRef = useRef<TranscriptHeaderStateRef['current']>({ day: '', role: '' })

  // Shared metadata helpers for the real imperative message builder. Reads
  // `containerRef.current` lazily so history and live-stream rows use the same
  // role labels, timestamps, hover actions and turn-meta footer.
  // Meta caption helpers — the `data-time` (HH:MM) + `data-sender` (NAME/YOU)
  // attributes the CSS renders above each row (matching the reference chat
  // layout). Time is formatted here (CSS can't format an ISO string); the
  // sender is the agent id for assistant/system, "YOU" for user, "ERROR" for
  // errors. Kept next to the builders so every row stamps consistently.
  const stampRowMeta = useCallback(
    (el: HTMLElement, role: string, ts?: string | number | null): void => {
      let date: Date
      if (ts == null || ts === '') {
        date = new Date()
      } else if (typeof ts === 'number') {
        date = new Date(ts)
      } else {
        // History carries epoch-ms as a numeric string (e.g. "1784624965697");
        // the send path carries an ISO string. Parse the numeric form as epoch,
        // else fall back to Date's string parsing (ISO).
        const epoch = Number(ts)
        date = /^\d+$/.test(ts.trim()) && Number.isFinite(epoch) ? new Date(epoch) : new Date(ts)
      }
      el.dataset.time = Number.isNaN(date.getTime())
        ? ''
        : `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`
      if (role === 'user') el.dataset.sender = 'YOU'
      else if (role === 'error') el.dataset.sender = 'ERROR'
      else if (role === 'system') el.dataset.sender = 'SYSTEM'
      // assistant → the agent id from the current session key, uppercased.
      else
        el.dataset.sender = (agentIdFromSessionKey(sessionKeyRef.current) || 'AGENT').toUpperCase()
    },
    [],
  )

  const stampHistoryElement = useCallback(
    (
      el: HTMLElement,
      stableIdentity: string,
      role: string,
      text: string,
      transcriptId: string | null = null,
      ts: string | number | null = null,
    ): void => {
      if (stableIdentity) el.setAttribute('data-message-id', stableIdentity)
      else el.removeAttribute('data-message-id')
      el.setAttribute('data-history-role', role || '')
      el.setAttribute('data-history-raw-text', text || '')
      el.setAttribute(
        'data-history-fallback-id',
        historyFallbackMessageIdentity(role as Role, text),
      )
      if (transcriptId != null) el.dataset.transcriptId = String(transcriptId)
      else delete el.dataset.transcriptId
      if (ts != null && ts !== '') el.dataset.historyTs = String(ts)
      else delete el.dataset.historyTs
      stampRowMeta(el, role || 'assistant', ts)
    },
    [stampRowMeta],
  )

  // eslint-disable-next-line react-hooks/refs -- factory stores the refs and reads .current only later, inside methods invoked outside render (never at creation)
  const [controller] = useState<StreamController>(() =>
    createStreamController(containerRef, {
      markdown: chatMarkdown,
      stripProtocolTextLeak,
      stripDirectiveTags,
      stripGeneratedArtifactMarkers,
      dayKey,
      dayLabel,
      headerState: headerStateRef,
      getSessionKey: () => sessionKeyRef.current,
      displayRoleLabel: (role) =>
        role === 'assistant'
          ? (agentIdFromSessionKey(sessionKeyRef.current) || 'agent').toUpperCase()
          : role
            ? role.charAt(0).toUpperCase() + role.slice(1)
            : '',
      routerFxDock: () => routerFxDockRef.current,
      routerFeatureEnabled: () => routerFeatureEnabledRef.current,
      routerFxAwaitConfig: () => awaitRouterConfigReady(routerConfigGate.promise),
      historyHasRendered: () => historyHasRenderedRef.current,
      historyHydrating: () => historyHydratingRef.current,
      openModal: (title, html, buttons) => openModalRef.current?.(title, html, buttons),
      // Artifact preview/download URLs + download Authorization header
      // (chat.js:7575/7657 `App.getAuthToken()`).
      getAuthToken,
      applySessionRunState: (state) => {
        applySessionRunState(state)
        seamsRef.current.applySessionRunState?.(state)
      },
      // chat.js:6571 — the Send/Stop affordance refresh fires on every stream
      // lifecycle transition (start/end/park/restore). Re-read the imperative
      // `_isStreaming` flag here to keep the reactive `busy` mirror in sync so
      // the composer swaps between Send and Abort.
      updateSendButton: () => setBusyRef.current(),
      stampHistoryElement,
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
      // Pending-queue drain/recovery (chat.js:8596/8644). Task 13 fills these
      // seams: the pending QUEUE lives in ChatPage (composer-adjacent), so the
      // delegates are installed via `setPendingDelegates` after mount and read
      // lazily through `pendingDelegatesRef` — the controller's compaction-settle
      // (chat.js:8681/8685) then drives the real drain/recover. The initial safe
      // defaults cover the short interval before ChatPage installs those delegates.
      schedulePendingDrainAfterTerminal: () =>
        pendingDelegatesRef.current.schedulePendingDrainAfterTerminal(),
      popAllPendingIntoComposer: () => pendingDelegatesRef.current.popAllPendingIntoComposer(),
      pendingQueueLength: () => pendingDelegatesRef.current.pendingQueueLength(),
      // chat.js:6652 — the abort flag `appendDelta` guards on (stream.ts:839): a
      // late `text_delta` buffered on the socket after the user hit Stop must NOT
      // re-open the killed stream bubble. `abort()` sets `abortedRef.current=true`
      // and a fresh send/session-switch clears it, matching legacy `_aborted`.
      isAborted: () => abortedRef.current,
      // chat.js:7851 `_addMessage` (3-arg timeout/error/keep-alive form). The
      // idle-timeout row (stream.ts:522) and keep-alive row (stream.ts:653) call
      // this — without it a stalled stream ends with no user-visible explanation.
      // Shares the real message builder with `addMessageWithOptions` below.
      addMessage: (role, text, timestamp) =>
        messageRendererRef.current?.addMessage(role, text, timestamp) ?? null,
      // Subagent-completion system row (chat.js:7814 `_addMessage`) through the
      // same real builder used by live and history rows.
      addMessageWithOptions: (role, text, timestamp, options) =>
        messageRendererRef.current?.addMessage(role, text, timestamp, options) ?? null,
      attachHoverActions: (row, role) => messageRendererRef.current?.attachHoverActions(row, role),
      toast: (message, kind, durationMs) => {
        const options = durationMs ? { duration: durationMs } : undefined
        if (kind === 'warn' || kind === 'warning') toast.warning(message, options)
        else if (kind === 'err' || kind === 'error') toast.error(message, options)
        else toast.info(message, options)
      },
    }),
  )

  const revealTranscriptIfSettled = useCallback(
    (sessionKey: string, historyFetchStatus: string): void => {
      const th = containerRef.current
      if (!th || th.dataset.historyReady === 'true') return
      if (sessionKeyRef.current !== sessionKey) return
      if (historySettledSessionRef.current !== sessionKey) return
      if (subscriptionSettledSessionRef.current !== sessionKey) return
      if (historyFetchStatus !== 'idle') return

      // Replay deltas may still have one queued rAF render. Drain it while the
      // transcript is hidden, position once, then expose on this layout frame.
      controller.flushPendingTextSegment()
      if (controller.isAutoScrollEnabled()) th.scrollTop = th.scrollHeight
      th.dataset.historyReady = 'true'
    },
    [controller],
  )

  // eslint-disable-next-line react-hooks/refs -- factory stores ref-backed callbacks and reads .current only when an imperative message method runs, never during creation
  const [messageRenderer] = useState(() =>
    createMessageRenderer({
      thread: () => containerRef.current,
      markdown: chatMarkdown,
      displayRoleLabel: (role) =>
        role === 'assistant'
          ? (agentIdFromSessionKey(sessionKeyRef.current) || 'agent').toUpperCase()
          : role === 'user'
            ? 'YOU'
            : role
              ? role.charAt(0).toUpperCase() + role.slice(1)
              : '',
      stampRowMeta,
      getSessionKey: () => sessionKeyRef.current,
      isStreaming: () => controller.isStreaming(),
      scrollToBottom: () => controller.scrollToBottom(),
      dayKey,
      dayLabel,
      headerState: headerStateRef,
      toast: (message, kind, durationMs) => {
        const options = durationMs ? { duration: durationMs } : undefined
        if (kind === 'warn') toast.warning(message, options)
        else if (kind === 'error') toast.error(message, options)
        else toast.info(message, options)
      },
      onEdit: (text) => messageActionsRef.current.onEdit?.(text),
      onRegenerate: (text) => messageActionsRef.current.onRegenerate?.(text),
    }),
  )

  // The imperative controller owns the real preference object; this state is
  // only its React-facing mirror so Toolbar also follows cross-tab/focus reloads.
  const [routerFxEnabled, setRouterFxEnabledState] = useState(() => controller.routerFxPref.enabled)
  const setRouterFxEnabled = useCallback(
    (enabled: boolean) => {
      controller.setRouterFxEnabled(enabled)
      setRouterFxEnabledState(enabled)
    },
    [controller],
  )

  // Feed the controller's existing router-fx registry from config.get. The
  // registry intentionally lives inside the controller; this effect mutates that
  // single instance, then releases decision/scan paths waiting on config.
  useEffect(() => {
    openModalRef.current = opts.openModal
    messageActionsRef.current = {
      onEdit: opts.onEditMessage,
      onRegenerate: opts.onRegenerateMessage,
      onSessionKeyResolved: opts.onSessionKeyResolved,
    }
    messageRendererRef.current = messageRenderer
  }, [
    messageRenderer,
    opts.onEditMessage,
    opts.onRegenerateMessage,
    opts.onSessionKeyResolved,
    opts.openModal,
  ])

  // chat.js:2575-2579 — streaming follows the tail only while the reader is
  // already near it. Passive scroll tracking lets a manual upward scroll pause
  // auto-follow until the reader returns within the legacy 60px threshold.
  useEffect(() => {
    const thread = containerRef.current
    if (!thread) return
    const onScroll = () => controller.updateAutoScrollFromThread()
    thread.addEventListener('scroll', onScroll, { passive: true })
    onScroll()
    return () => thread.removeEventListener('scroll', onScroll)
  }, [controller])

  // chat.js:916-932 `_bindHoverActions` — anchor artifact targets keep native
  // download behavior; any non-anchor target delegates to the authenticated
  // fetch path so token/session headers and failure toasts work.
  useEffect(() => {
    const thread = containerRef.current
    if (!thread) return
    const onArtifactClick = (event: MouseEvent): void => {
      if (!(event.target instanceof Element)) return
      const artifact = event.target.closest<HTMLElement>('[data-artifact-download]')
      if (!artifact || artifact.tagName === 'A') return
      event.preventDefault()
      event.stopPropagation()
      void controller.downloadArtifact({
        id: artifact.dataset.artifactId || '',
        name: artifact.dataset.artifactName || 'artifact',
        download_url: artifact.dataset.artifactDownload || '',
      })
    }
    thread.addEventListener('click', onArtifactClick)
    return () => thread.removeEventListener('click', onArtifactClick)
  }, [controller])

  useEffect(() => {
    if (routerConfigQuery.isPending) return

    const router = routerConfigQuery.data?.agentos_router
    const tiers = router?.tiers
    const configTierKeys: string[] = []
    const configTierSet = new Set<string>()
    if (tiers && typeof tiers === 'object') {
      Object.entries(tiers).forEach(([tier, rawTier]) => {
        const lower = routerFxNormalizeTier(tier)
        if (!lower) return
        configTierKeys.push(lower)
        configTierSet.add(lower)
        controller.routerFxRegistry.setTierConfig(lower, {
          model: typeof rawTier?.model === 'string' ? rawTier.model : '',
          supportsImage: rawTier?.supports_image === true,
          imageOnly: rawTier?.image_only === true,
        })
      })
    }

    Object.keys(controller.routerFxRegistry.models).forEach((tier) => {
      if (!configTierSet.has(tier)) delete controller.routerFxRegistry.models[tier]
    })
    Object.keys(controller.routerFxRegistry.tierConfigs).forEach((tier) => {
      if (!configTierSet.has(tier)) delete controller.routerFxRegistry.tierConfigs[tier]
    })
    controller.routerFxRegistry.setConfigTiers(configTierSet)
    controller.routerFxRegistry.setSlotList(configTierKeys)
    routerFeatureEnabledRef.current = router?.enabled === true
    const refreshedRouterFxEnabled = controller.reloadRouterFxPreference()
    // eslint-disable-next-line react-hooks/set-state-in-effect -- config/focus refresh synchronizes an external localStorage preference into the Toolbar mirror
    setRouterFxEnabledState(refreshedRouterFxEnabled)
    routerConfigGate.resolve()
    const isRefresh = routerConfigAppliedRef.current
    routerConfigAppliedRef.current = true
    // The initial history wait has the legacy 1500ms ceiling. If config arrives
    // after that ceiling, history may already have rendered without an
    // authoritative tier roster; rebuild it once so persisted router receipts
    // are reconstructed instead of remaining absent until an unrelated refresh.
    if (isRefresh || historyHasRenderedRef.current) {
      void queryClient.invalidateQueries({
        queryKey: ['chat', 'history', sessionKeyRef.current],
      })
    }
  }, [
    controller,
    queryClient,
    routerConfigGate,
    routerConfigQuery.data,
    routerConfigQuery.dataUpdatedAt,
    routerConfigQuery.isPending,
  ])

  useEffect(() => {
    const provider = searchProviderQuery.data?.provider
    if (provider) controller.setSearchProvider(provider)
  }, [controller, searchProviderQuery.data?.provider])

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
      headerState: headerStateRef,
      addMessage: (role, text, timestamp, options) =>
        messageRendererRef.current?.addMessage(role, text, timestamp, options) ?? null,
      replaceMessage: (row, role, text, timestamp, options = {}) => {
        let subagent = role === 'system' && row.classList.contains('subagent')
        if (role === 'system' && options.provenanceSourceTool === 'subagent_completion') {
          subagent = true
        }
        const displayRole = subagent ? 'subagent' : role
        row.className = `msg ${displayRole}`
        stampRowMeta(row, displayRole, timestamp)
        let body = row.querySelector<HTMLElement>(':scope > .msg-body')
        if (!body) {
          body = document.createElement('div')
          row.appendChild(body)
        }
        messageRendererRef.current?.renderBody(body, role, text, options)
        row.querySelectorAll(':scope > .msg-meta').forEach((node) => node.remove())
      },
      syncMessageHeader: (row, displayRole, timestamp, options, sameGroup) =>
        messageRendererRef.current?.syncMessageHeader(
          row,
          displayRole,
          timestamp,
          options,
          sameGroup,
        ),
      attachHoverActions: (row, role) => messageRendererRef.current?.attachHoverActions(row, role),
      reconstructToolCalls: (row, segments) =>
        controller.reconstructToolCalls(row, segments, {
          stripText: (text) => stripDirectiveTags(stripProtocolTextLeak(text)),
          renderText: (text, into) => {
            into.innerHTML = chatMarkdown.render(text)
            chatMarkdown.bindCopy(into)
            chatMarkdown.bindHighlight?.(into)
          },
        }),
      renderMessageAttachmentHtml,
      renderArtifacts: (artifacts) => controller.renderArtifacts(artifacts),
      prepareHistoryRouterFx: () => controller.prepareHistoryRouterFx(),
      reconcileHistoryRouterFx: (usage, options) =>
        controller.reconcileHistoryRouterFx(usage, options),
      finishHistoryRouterFx: () => controller.finishHistoryRouterFx(),
      markHistoryRendered: () => {
        historyHasRenderedRef.current = true
      },
      turnMetaForMessage: (message, assistantIndex) =>
        historyTurnMeta(message) || recallTurnMeta(sessionKeyRef.current, assistantIndex),
      attachTurnMeta: (row, model, input, output, usage) =>
        messageRendererRef.current?.attachTurnMeta(row, model, input, output, usage),
      resetMessageGrouping: () => messageRendererRef.current?.resetGrouping(),
      stampHistoryElement,
      stripProtocolTextLeak,
      stripDirectiveTags,
      stripGeneratedArtifactMarkers,
      stripTimePrefix,
      loadEarlierHistory: () => void loadEarlierHistoryRef.current(),
      reloadHistory: () => void reloadHistoryRef.current(),
      isStreaming: () => controller.isStreaming(),
      shouldAutoScroll: () => controller.isAutoScrollEnabled(),
      getStreamBubble: () => controller.getStreamBubble(),
      getThinkingIndicator: () => controller.getThinkingIndicator(),
      getCurrentSessionLiveUserAnchor: () => controller.getCurrentSessionLiveUserAnchor(),
      getPendingFinalizedAssistantBubble: () => controller.getPendingFinalizedAssistantBubble(),
      isPendingFinalizedAssistantBubble: (row) => controller.isPendingFinalizedAssistantBubble(row),
      clearPendingFinalizedAssistantBubble: () => controller.clearPendingFinalizedAssistantBubble(),
      diag: (event, detail) => seamsRef.current.diag?.(event, detail),
    }),
  )

  // Keep the session-key + seams holders current (effect, never during render).
  // Declared BEFORE the subscription effect so on a session-key change this body
  // runs first (React runs effect bodies in declaration order) — the controller's
  // `getSessionKey()` therefore returns the NEW key by the time the subscription
  // effect re-subscribes + restores. On teardown, cleanups run before any body,
  // so the subscription effect's cleanup still sees the OLD key when it parks.
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

  // `abortedRef` (legacy `_aborted`, chat.js:6121) is declared above the
  // controller init so the `isAborted` dep can read it. Set true on abort, and
  // cleared on a fresh send / session switch below.

  // Legacy `_stopRequestedByUser` (chat.js:346). `abort()` (the `_onStop` path,
  // chat.js:8442) sets it true right before `abortAndRecover` recovers pending
  // into the composer; the `.done`-wasAborted drain (chat.js:5126-5128) then
  // SKIPS its own recover when this is already set — otherwise a message enqueued
  // in the abort→done window would be pulled into the composer twice. Reset per
  // turn on a fresh send and after the guarded drain runs (chat.js:5127).
  const stopRequestedByUserRef = useRef(false)

  // chat.js:6062-6205 `_onSend`. ChatPage owns enqueue-while-streaming, slash
  // commands and attachment normalization; this action renders the user turn,
  // starts the stream UI and sends the fully normalized RPC payload.
  const send = useCallback(
    (text: string, attachments: PendingAttachment[] = [], intent: string | null = null) => {
      const trimmed = (text ?? '').trim()
      const sessionKey = sessionKeyRef.current
      const atts = attachments ?? []
      // chat.js:6064/6118 — `hasPayload = text || _pendingAttachments.length > 0`;
      // an attachments-only send (empty text) is allowed. No session, no payload,
      // or already streaming (the caller queues in that case) is a no-op.
      const hasPayload = Boolean(trimmed) || atts.length > 0
      if (!hasPayload || !sessionKey || controller.isStreaming()) return

      abortedRef.current = false
      // A new turn clears the stop-recover guard (legacy resets `_stopRequestedByUser`
      // per turn — chat.js:1722/8799) so the next abort→done cycle re-arms cleanly.
      stopRequestedByUserRef.current = false
      // chat.js:6128 — history/↑↓ tracks the user's display text (not the provider
      // fallback). Only record non-empty text so an attachments-only send doesn't
      // seed a blank history entry.
      if (trimmed) setHistory((prev) => [...prev, trimmed])

      // chat.js:6129 — the model receives a fallback prompt when text is empty but
      // attachments are present ("Describe these attachments").
      const userText = trimmed
      const providerText = trimmed || 'Describe these attachments'

      // Show the user turn through the shared imperative `_addMessage` port.
      const userTs = new Date().toISOString()
      const userDiv = messageRendererRef.current?.addMessage('user', userText, userTs) ?? null
      if (userDiv) {
        userDiv.dataset.historyTs = userTs
        userDiv.dataset.historyRawText = userText
        // chat.js:6136-6147 — attachments replace the body, then the hover row is
        // re-attached because the innerHTML write removes it.
        if (atts.length > 0) {
          const body = userDiv.querySelector('.msg-body') as HTMLElement | null
          if (body) {
            body.classList.add('msg-body--has-attachments')
            const bodyText = userText
              ? `<div class="msg-attachment-text">${esc(userText)}</div>`
              : ''
            const chips = atts.map((attachment) => renderMessageAttachmentHtml(attachment)).join('')
            body.innerHTML = `${bodyText}<div class="msg-attachments">${chips}</div>`
            messageRendererRef.current?.attachHoverActions(userDiv, 'user')
          }
        }
      }

      // Start streaming UI + the delayed router scan (chat.js:6178-6190). Config
      // usually resolves before the first send; the gate also handles a fast send
      // during cold start without letting an empty registry suppress the strip.
      controller.startStreaming()
      void routerConfigGate.promise.then(() => {
        if (sessionKeyRef.current !== sessionKey || !controller.isStreaming()) return
        controller.scheduleRouterFxBeginScan(userDiv, routerFxResolveLayoutSeed(sessionKey), {
          requestKind: routerFxRequestKindFromAttachments(atts),
        })
      })
      controller.showThinkingIndicator()
      controller.scrollToBottom()

      // chat.js:6150-6167 — the RPC params. Attachments ride as `displayText` +
      // `attachments` (staged → file_uuid, else inline base64) + `inputProvenance`
      // for a normalization-generated attachment.
      const params: Record<string, unknown> = { message: providerText, sessionKey }
      const elevatedMode = useApprovals.getState().elevatedMode
      if (elevatedMode) params._source = { elevated: elevatedMode }
      // chat.js:6153-6156 — the per-send session intent rides on the params.
      if (intent) params.intent = intent
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
          if (r?.sessionKey && r.sessionKey !== sessionKeyRef.current) {
            messageActionsRef.current.onSessionKeyResolved?.(r.sessionKey)
          }
        })
        .catch((err: unknown) => {
          const message = err instanceof Error ? err.message : String(err)
          seamsRef.current.diag?.('send.rpc.error', { message })
          // chat.js:6202-6203 — end streaming + surface the failure inline.
          if (controller.isStreaming()) controller.endStreaming()
          messageRendererRef.current?.addMessage('error', 'Send failed: ' + message)
        })
    },
    [rpc, controller, routerConfigGate],
  )

  // chat.js:8439-8450 `_onStop`. Abort only while streaming; set the abort flag,
  // fire `chat.abort` with `{ sessionKey, source }` (chat.js:8444), and end the
  // stream locally with `reason:'aborted'`. ChatPage composes this with the
  // pending-queue recovery path from chat.js:8448.
  const abort = useCallback(
    (source = 'webui_stop_button') => {
      if (!controller.isStreaming()) return
      abortedRef.current = true
      // chat.js:8442 — mark that the user-initiated stop path ran (its caller,
      // `abortAndRecover`, recovers pending immediately after) so the later
      // `.done`-wasAborted drain skips a second recover (chat.js:5126-5128).
      stopRequestedByUserRef.current = true
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

  // Keep a newly mounted/switched transcript out of the paint tree until its
  // persisted rows have been reconstructed and positioned. This is a visual
  // atomicity boundary: the user never sees the scroll container at its default
  // top position while history is still arriving.
  useLayoutEffect(() => {
    const th = containerRef.current
    historySettledSessionRef.current = ''
    subscriptionSettledSessionRef.current = ''
    if (th) th.dataset.historyReady = 'false'
  }, [opts.sessionKey])

  // A session swap clears the abort/stop flags so a prior session's pending stop
  // can't gate the new session's drain (legacy resets these on the session
  // transition / destroy — chat.js:1722/8799). Ref writes live in an effect (not
  // render) per the react-hooks/refs rule.
  useEffect(() => {
    abortedRef.current = false
    stopRequestedByUserRef.current = false
    activeTaskGroupsRef.current.clear()
    historyHasRenderedRef.current = false
    historyHydratingRef.current = true
    headerStateRef.current.day = ''
    headerStateRef.current.role = ''
    controller.clearRouterFxVisuals('session_switch')
    controller.syncLastStreamSeqFromSession(opts.sessionKey)
    // eslint-disable-next-line react-hooks/set-state-in-effect -- a session-key transition must synchronously reset the externally-driven status mirror
    applySessionRunState({ run_status: 'idle' })
  }, [applySessionRunState, controller, opts.sessionKey])

  /* ── History read via react-query (chat.js:5440 `_loadHistory`) ─────────── */

  const historyQuery = useQuery<HistoryResponse>({
    queryKey: ['chat', 'history', opts.sessionKey],
    queryFn: async () => {
      historyHydratingRef.current = true
      await rpc.waitForConnection()
      // Do not reconstruct router receipts against placeholder tier labels. The
      // timeout is faithful: config failure must never hang history forever.
      await awaitRouterConfigReady(routerConfigGate.promise)
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
  const renderedResponseAtRef = useRef(0)
  useLayoutEffect(() => {
    const data = historyQuery.data
    if (!data) return
    // React Query may structurally share an equal refetch payload. A successful
    // refetch must still rerun history lifecycle hooks (especially pending
    // router-decision flush), so gate on the query's success timestamp instead
    // of object identity.
    const responseIsNew = renderedResponseAtRef.current !== historyQuery.dataUpdatedAt
    if (responseIsNew) {
      historyHydratingRef.current = false
      renderedResponseAtRef.current = historyQuery.dataUpdatedAt
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
      const th = containerRef.current
      const followTail = controller.isAutoScrollEnabled()
      const preserveReaderPosition = !followTail && th != null
      const previousScrollHeight = th?.scrollHeight ?? 0
      const previousScrollTop = th?.scrollTop ?? 0
      historyRenderer.renderHistoryMessages(
        messages,
        pagingRef.current,
        preserveReaderPosition
          ? {
              preserveScroll: true,
              previousScrollHeight,
              previousScrollTop,
            }
          : {},
      )
      // chat.js:3119 — overlay the history compaction-summary separators once
      // the message rows exist.
      controller.renderCompactionSummarySeparators(messages)
      historySettledSessionRef.current = opts.sessionKey
    }
    revealTranscriptIfSettled(opts.sessionKey, historyQuery.fetchStatus)
  }, [
    historyQuery.data,
    historyQuery.dataUpdatedAt,
    historyQuery.fetchStatus,
    historyRenderer,
    controller,
    opts.sessionKey,
    revealTranscriptIfSettled,
    subscriptionSettleRevision,
  ])

  // Surface a history-load error into the scope row (chat.js:5484-5488).
  useLayoutEffect(() => {
    if (!historyQuery.isError) return
    historyHydratingRef.current = false
    pagingRef.current.error = 'Could not load chat history.'
    historyRenderer.renderHistoryScopeRow(pagingRef.current)
    historySettledSessionRef.current = opts.sessionKey
    revealTranscriptIfSettled(opts.sessionKey, historyQuery.fetchStatus)
  }, [
    historyQuery.fetchStatus,
    historyQuery.isError,
    historyRenderer,
    opts.sessionKey,
    revealTranscriptIfSettled,
    subscriptionSettleRevision,
  ])

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
    const succeededTerminalTimers = new Set<number>()
    const pendingReplayTasks = new Set<Promise<void>>()
    const seams = () => seamsRef.current
    const diag = (event: string, detail: Record<string, unknown>) =>
      seamsRef.current.diag?.(event, detail)

    // `_isCurrentSessionPayload` reads the LIVE session key (via ref) so a
    // late-arriving frame after a session switch is still correctly dropped.
    const isForeign = (payload: StreamEventPayload | undefined): boolean =>
      !isCurrentSessionPayload(payload, sessionKeyRef.current)

    const isStaleEpoch = (payload: StreamEventPayload | undefined): boolean => {
      const epoch = Number(payload?.epoch)
      return Number.isFinite(epoch) && epoch < currentEpochRef.current
    }

    const resyncHistory = () =>
      void queryClient.invalidateQueries({ queryKey: ['chat', 'history', sessionKeyRef.current] })

    // ── Subscribe (chat.js:2857 `_subscribeSession`) ──────────────────────
    const subscribe = async () => {
      try {
        await rpc.waitForConnection()
        if (cancelled || sessionKey !== sessionKeyRef.current) return
        const sinceStreamSeq = controller.syncLastStreamSeqFromSession(sessionKey)
        const res = (await rpc.call('sessions.messages.subscribe', {
          key: sessionKey,
          since_stream_seq: sinceStreamSeq,
        })) as {
          subscribed?: boolean
          replay_complete?: boolean
          replay_gap_reason?: unknown
          replayGapReason?: unknown
          replayed_count?: unknown
          current_stream_seq?: unknown
        } | null
        if (cancelled || sessionKey !== sessionKeyRef.current) return
        if (res && res.subscribed === false) throw new Error('No subscription manager available')
        const subscribedState = (res as Record<string, unknown>) ?? {}
        applySessionRunState(subscribedState)
        seams().applySessionRunState?.(subscribedState)
        const currentSeq = Number(res?.current_stream_seq)
        if (Number.isFinite(currentSeq)) {
          controller.syncLastStreamSeqFromSession(sessionKey, currentSeq)
        }
        if (
          !controller.isStreaming() &&
          ['queued', 'running', 'approval_pending'].includes(runStateRef.current.status)
        ) {
          controller.startStreaming()
          controller.showThinkingIndicator()
        }
        // A replay gap means we may have missed live events → warn only for an
        // unexpected gap reason, then resync history (chat.js:2874-2887).
        if (res && res.replay_complete === false) {
          const gapReason = res.replay_gap_reason || res.replayGapReason || ''
          if (replayGapShouldWarn(gapReason)) {
            toast.warning('Missed live stream events; transcript refreshed.', { duration: 5000 })
          }
          resyncHistory()
        }
        if (subscribeResultNeedsTerminalHistorySync(res)) resyncHistory()
        if (controller.isStreaming()) controller.resetStreamIdleTimer()
      } catch (error: unknown) {
        toast.error(
          'Session stream subscription failed: ' +
            (error instanceof Error ? error.message : String(error)),
          { duration: 6000 },
        )
        diag('session.subscribe.error', { sessionKey })
      } finally {
        // The gateway returns from subscribe only after replay frames have been
        // sent. Some replay handlers (notably router decisions) continue across
        // async config work, so include them in the same entry barrier.
        await Promise.allSettled(Array.from(pendingReplayTasks))
        if (!cancelled && sessionKey === sessionKeyRef.current) {
          subscriptionSettledSessionRef.current = sessionKey
          setSubscriptionSettleRevision((revision) => revision + 1)
        }
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
      if (isStaleEpoch(payload)) {
        diag(`${event}.drop.stale_epoch`, {})
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
      onEvent(
        'session.event.router_decision',
        (payload: StreamEventPayload, meta: Record<string, unknown>) => {
          if (!gateStreamFrame('event.router_decision', payload)) return
          const task = controller.handleRouterDecision(payload as Record<string, unknown>)
          if (meta.replayed === true) {
            pendingReplayTasks.add(task)
            void task.then(
              () => pendingReplayTasks.delete(task),
              () => pendingReplayTasks.delete(task),
            )
          } else {
            void task
          }
          seams().handleRouterDecision?.(payload)
        },
      ),
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
        applySessionRunState(
          (payload.run_status || payload.runStatus
            ? payload
            : { ...payload, run_status: 'running', active_task: { status: 'running' } }) as Record<
            string,
            unknown
          >,
        )
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
        applySessionRunState(
          (payload.run_status || payload.runStatus
            ? payload
            : { ...payload, run_status: 'running', active_task: { status: 'running' } }) as Record<
            string,
            unknown
          >,
        )
        controller.resetStreamIdleTimer()
        if (!controller.showAwaitingModelHintAfterToolResult()) {
          controller.showThinkingIndicator()
        }
      }),
    )

    // chat.js:4860-4878 — persistent cron result row.
    unsubs.push(
      onEvent('session.event.cron_result', (payload: StreamEventPayload) => {
        if (!gateStreamFrame('event.cron_result', payload)) return
        const message = (payload.message || payload || {}) as Record<string, unknown>
        const targetSession = String(payload.sessionKey || '')
        if (targetSession && targetSession !== sessionKeyRef.current) return
        messageRendererRef.current?.addMessage(
          'assistant',
          String(message.text || ''),
          (message.timestamp as string | number | undefined) || null,
          {
            provenanceKind: String(message.provenanceKind || message.provenance_kind || 'cron'),
          },
        )
        seams().appendCronResult?.(payload)
      }),
    )

    // chat.js:4881 — compaction → controller compaction renderer (Task 7). The
    // renderer drives the in-thread context separator + in-flight controls +
    // router-fx compaction-turn suppression; the seam remains an optional
    // observer hook (it never replaces the real handler).
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
        if (isStaleEpoch(payload)) return
        const message = (payload as { message?: string })?.message || 'Cap warning'
        toast.warning(message, { duration: 5000 })
        seams().showWarningToast?.(message)
      }),
    )

    // chat.js:4899-4905 — epoch bump drops stale pre-reset frames.
    unsubs.push(
      onEvent('session.epoch_changed', (payload: StreamEventPayload) => {
        if (isForeign(payload)) return
        const epoch = Number(payload.epoch)
        if (Number.isFinite(epoch) && epoch > currentEpochRef.current) {
          activeTaskGroupsRef.current.clear()
          currentEpochRef.current = epoch
        }
        seams().onEpochChanged?.(payload)
      }),
    )

    // chat.js:4909 — sessions.changed → terminal resync or run-state apply.
    unsubs.push(
      onEvent('sessions.changed', (payload: StreamEventPayload) => {
        if (isStaleEpoch(payload)) return
        if (!isCurrentSessionPayload(payload, sessionKeyRef.current)) return
        // Terminal session change → end streaming + resync history
        // (chat.js:1713 `_syncTerminalSessionChange`). We conservatively resync
        // history + apply run-state; terminal pending recovery is handled by the
        // wildcard completion/error path below.
        if (sessionChangeIsTerminal(payload)) {
          activeTaskGroupsRef.current.clear()
          if (controller.isStreaming()) controller.endStreaming()
          applySessionRunState(payload)
          seams().applySessionRunState?.((payload as Record<string, unknown>) ?? {})
          resyncHistory()
          return
        }
        applySessionRunState(payload)
        seams().applySessionRunState?.((payload as Record<string, unknown>) ?? {})
      }),
    )

    // chat.js:4919 — task.queued → run-state seam.
    unsubs.push(
      onEvent('task.queued', (payload: StreamEventPayload) => {
        if (!isCurrentSessionPayload(payload, sessionKeyRef.current)) return
        if (
          runStateRef.current.status === 'running' ||
          runStateRef.current.status === 'approval_pending'
        )
          return
        const next = {
          run_status: 'queued',
          active_task: { ...(payload || {}), status: 'queued' },
        }
        applySessionRunState(next)
        seams().applySessionRunState?.(next)
      }),
    )

    // chat.js:4928 — task.running → run-state seam.
    unsubs.push(
      onEvent('task.running', (payload: StreamEventPayload) => {
        if (!isCurrentSessionPayload(payload, sessionKeyRef.current)) return
        const next = {
          run_status: 'running',
          active_task: { ...(payload || {}), status: 'running' },
        }
        applySessionRunState(next)
        seams().applySessionRunState?.(next)
      }),
    )

    // chat.js:4936-4962 — task_group.{waiting,synthesizing,done,failed}.
    unsubs.push(
      onEvent('session.event.task_group.waiting', (payload: StreamEventPayload) => {
        if (!gateStreamFrame('event.task_group.waiting', payload)) return
        const groupId = String(payload.group_id || '')
        if (groupId) activeTaskGroupsRef.current.add(groupId)
        applySessionRunState({
          run_status: 'running',
          active_task: {
            ...payload,
            status: 'running',
            task_group_count: activeTaskGroupsRef.current.size,
          },
        })
        seams().noteTaskGroupActive?.(payload)
      }),
    )
    unsubs.push(
      onEvent('session.event.task_group.synthesizing', (payload: StreamEventPayload) => {
        if (!gateStreamFrame('event.task_group.synthesizing', payload)) return
        const groupId = String(payload.group_id || '')
        if (groupId) activeTaskGroupsRef.current.add(groupId)
        applySessionRunState({
          run_status: 'running',
          active_task: {
            ...payload,
            status: 'running',
            task_group_count: activeTaskGroupsRef.current.size,
          },
        })
        seams().noteTaskGroupActive?.(payload)
      }),
    )
    unsubs.push(
      onEvent('session.event.task_group.done', (payload: StreamEventPayload) => {
        if (!gateStreamFrame('event.task_group.done', payload)) return
        const groupId = String(payload.group_id || '')
        if (groupId) activeTaskGroupsRef.current.delete(groupId)
        applySessionRunState(
          activeTaskGroupsRef.current.size > 0
            ? { run_status: 'running', active_task: { ...payload, status: 'running' } }
            : { run_status: 'idle', last_task: { ...payload, status: 'succeeded' } },
        )
        seams().noteTaskGroupTerminal?.(payload, 'succeeded')
      }),
    )
    unsubs.push(
      onEvent('session.event.task_group.failed', (payload: StreamEventPayload) => {
        if (!gateStreamFrame('event.task_group.failed', payload)) return
        const groupId = String(payload.group_id || '')
        if (groupId) activeTaskGroupsRef.current.delete(groupId)
        applySessionRunState(
          activeTaskGroupsRef.current.size > 0
            ? { run_status: 'running', active_task: { ...payload, status: 'running' } }
            : { run_status: 'failed', last_task: { ...payload, status: 'failed' } },
        )
        seams().noteTaskGroupTerminal?.(payload, 'failed')
      }),
    )

    // chat.js:4965 — the `*` wildcard: terminal task events + `.done`/`.error`
    // finalization, per-turn usage metadata and pending-queue recovery. The
    // optional observer receives the raw frame without replacing production
    // behavior.
    unsubs.push(
      rpc.on('*', (rawEventArg: unknown, rawPayloadArg: unknown, rawMetaArg: unknown) => {
        if (typeof rawEventArg !== 'string') return
        const rawEvent = rawEventArg
        const rawPayload = (rawPayloadArg as StreamEventPayload) ?? {}
        const rawMeta = (rawMetaArg as Record<string, unknown>) ?? {}
        seams().handleGenericEvent?.(rawEvent, rawPayload, rawMeta)
        // The observer never replaces the production finalizer (chat.js:5033
        // `.done` / 5142 `.error`); it only receives the same raw frame.
        if (!isCurrentSessionPayload(rawPayload, sessionKeyRef.current)) return

        if (rawEvent.startsWith('session.event.')) {
          if (isStaleEpoch(rawPayload)) {
            diag('event.generic.drop.stale_epoch', { event: rawEvent })
            return
          }
          if (!controller.acceptStreamSeq(rawPayload)) {
            diag('event.generic.drop.stream_seq', { event: rawEvent })
            return
          }
        }

        const taskTerminal = rawEvent.startsWith('task.') ? rawEvent.slice('task.'.length) : ''
        if (['succeeded', 'failed', 'timeout', 'abandoned', 'cancelled'].includes(taskTerminal)) {
          const terminalStatus =
            taskTerminal === 'succeeded'
              ? 'idle'
              : taskTerminal === 'abandoned'
                ? 'interrupted'
                : taskTerminal
          applySessionRunState(
            activeTaskGroupsRef.current.size > 0
              ? { run_status: 'running', active_task: { status: 'running' } }
              : {
                  run_status: terminalStatus,
                  last_task: { ...rawPayload, status: taskTerminal },
                },
          )
          if (taskTerminal === 'succeeded') {
            // chat.js:6256-6273 — task.succeeded is a terminal backstop for
            // providers whose chat.done frame is missing or delayed. Give the
            // normal done path 75ms to win; otherwise end only the same stream
            // generation, resync history and release the pending queue.
            const streamGeneration = controller.streamGeneration()
            const terminalTimer = window.setTimeout(() => {
              succeededTerminalTimers.delete(terminalTimer)
              if (!isCurrentSessionPayload(rawPayload, sessionKeyRef.current)) return
              if (isStaleEpoch(rawPayload)) return
              controller.scheduleHistorySync()
              if (controller.isStreaming() && controller.streamGeneration() === streamGeneration) {
                controller.endStreaming()
                pendingDelegatesRef.current.schedulePendingDrainAfterTerminal()
              }
            }, 75)
            succeededTerminalTimers.add(terminalTimer)
            if (!controller.isStreaming()) {
              pendingDelegatesRef.current.schedulePendingDrainAfterTerminal()
            }
          } else if (!controller.isStreaming()) {
            pendingDelegatesRef.current.popAllPendingIntoComposer()
          }
        }

        // Legacy normalizes terminal task frames into done/error only while a
        // live stream still needs finalization; a post-Stop acknowledgement is
        // run-state-only and must not duplicate the transcript/recovery path.
        const normalizedDone = rawEvent === 'task.cancelled' && controller.isStreaming()
        const isDone =
          normalizedDone ||
          rawEvent === 'chat.done' ||
          (rawEvent.endsWith('.done') && !rawEvent.includes('.task_group.'))
        const normalizedError =
          controller.isStreaming() &&
          ['task.failed', 'task.timeout', 'task.abandoned'].includes(rawEvent)
        const isError =
          normalizedError || (rawEvent.endsWith('.error') && !rawEvent.includes('.task_group.'))

        if (isDone) {
          const payload = normalizedDone ? { ...rawPayload, reason: 'aborted' } : rawPayload
          const usage = ((payload as { usage?: unknown }).usage || payload) as TurnUsage
          const finalText = (usage as { text?: string })?.text
          if (typeof finalText === 'string' && finalText) {
            controller.reconcileFinalStreamText(finalText)
          }
          const finishedBubble = controller.getStreamBubble()
          const wasAborted =
            abortedRef.current || (payload as { reason?: string })?.reason === 'aborted'
          if (controller.isStreaming())
            controller.endStreaming(wasAborted ? { reason: 'aborted' } : undefined)
          const model = String(usage.model || usage.routed_model || '')
          const input = Number(usage.input_tokens || usage.inputTokens || 0)
          const output = Number(usage.output_tokens || usage.outputTokens || 0)
          messageRendererRef.current?.attachTurnMeta(
            finishedBubble,
            model,
            Number.isFinite(input) ? input : 0,
            Number.isFinite(output) ? output : 0,
            usage,
          )
          if (finishedBubble) {
            const assistants = Array.from(
              containerRef.current?.querySelectorAll<HTMLElement>('.msg.assistant') || [],
            )
            const assistantIndex = assistants.indexOf(finishedBubble)
            if (assistantIndex >= 0) {
              storeTurnMeta(
                sessionKeyRef.current,
                assistantIndex,
                model,
                Number.isFinite(input) ? input : 0,
                Number.isFinite(output) ? output : 0,
                usage,
              )
            }
          }
          // chat.js:5120-5131 — on abort recover pending into the composer (the
          // user explicitly stopped, so auto-firing queued sends is wrong); on
          // a natural completion drain the queue head (FIFO).
          if (wasAborted) {
            abortedRef.current = false
            // chat.js:5126-5128 — the user-stop path (`abortAndRecover`) already
            // recovered pending; this branch is only for the server-initiated
            // cancel (timeout/external abort) where it never ran. Skip a second
            // recover when the stop flag is already set, then clear it (5127) so
            // a message enqueued in the abort→done window isn't pulled in twice.
            if (stopRequestedByUserRef.current) {
              stopRequestedByUserRef.current = false
            } else {
              pendingDelegatesRef.current.popAllPendingIntoComposer()
            }
          } else {
            pendingDelegatesRef.current.schedulePendingDrainAfterTerminal()
          }
          applySessionRunState(
            wasAborted
              ? { run_status: 'cancelled', last_task: { ...payload, status: 'cancelled' } }
              : activeTaskGroupsRef.current.size > 0
                ? { run_status: 'running', active_task: { status: 'running' } }
                : { run_status: 'idle', last_task: { status: 'succeeded' } },
          )
          resyncHistory()
        } else if (isError) {
          if (controller.isStreaming()) controller.endStreaming()
          const payload = rawPayload as {
            terminal_message?: unknown
            message?: unknown
            code?: unknown
          }
          const code = String(payload.code || taskTerminal || '').toLowerCase()
          const message =
            typeof payload.terminal_message === 'string' && payload.terminal_message.trim()
              ? payload.terminal_message.trim()
              : code.includes('timeout')
                ? 'The task timed out before it could finish.'
                : code.includes('abandoned')
                  ? 'The task stopped before it could finish.'
                  : code.includes('cancelled')
                    ? 'The task was cancelled before it finished.'
                    : typeof payload.message === 'string' && payload.message
                      ? payload.message
                      : 'Agent error'
          messageRendererRef.current?.addMessage('error', message)
          // chat.js:5146 — recover pending after a failed turn.
          pendingDelegatesRef.current.popAllPendingIntoComposer()
          applySessionRunState(
            activeTaskGroupsRef.current.size > 0
              ? { run_status: 'running', active_task: { status: 'running' } }
              : {
                  run_status: code.includes('timeout') ? 'timeout' : 'failed',
                  last_task: { ...rawPayload, status: code || 'failed' },
                },
          )
          resyncHistory()
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
        toast.warning('Stream connection gap detected; reconnecting.')
        seams().showWarningToast?.('Stream connection gap detected; reconnecting.')
        // Terminal-history resync: the socket will reconnect and re-subscribe
        // (chat.js:1713 / 5177) — refresh history so the transcript is whole.
        resyncHistory()
      }),
    )

    // Restore any live stream state PARKED for this session on a previous switch
    // (chat.js:1831 `_restoreLiveStreamStateForSession`, called from
    // `_switchToSession` right before re-subscribing). The park map is a
    // controller-lifetime module state (chat.js:57), so a session we switch back
    // to gets its in-flight stream bubble / router strips / thinking indicator
    // re-attached. On a first mount for a key this is a no-op (nothing parked).
    // `sessionKeyRef` is already the NEW key here (its effect runs before this
    // one), so restore keys on the correct session.
    controller.restoreLiveStreamStateForSession(sessionKey)

    // Kick off the subscription (mirrors legacy's subscribe-on-view-entry).
    void subscribe()

    return () => {
      cancelled = true
      succeededTerminalTimers.forEach((timer) => window.clearTimeout(timer))
      succeededTerminalTimers.clear()
      // Park the OUTGOING session's live stream state before we tear the
      // subscription down (chat.js:1813 `_parkCurrentSessionStreamState`, called
      // from `_switchToSession` before `_unsubscribeSession`). Cleanups run
      // before any effect body on a re-render, so `getSessionKey()` still returns
      // the OLD key here — the state is stashed under the session we're leaving
      // and restored if we switch back. On real unmount the final cleanup at the
      // bottom of this hook clears view-local stream state unconditionally.
      controller.parkCurrentSessionStreamState('session_switch')
      // StrictMode-safe teardown: drop every registered rpc.on handler and
      // unsubscribe from the session so a re-mount never double-registers.
      unsubs.forEach((off) => off())
      unsubscribe()
    }
  }, [applySessionRunState, opts.sessionKey, rpc, controller, queryClient])

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
    renderedResponseAtRef.current = 0
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

  // Install the pending-queue drain/recover delegates (ChatPage owns the queue).
  // A stable setter that writes the ref the controller reads lazily.
  const setPendingDelegates = useCallback((delegates: PendingDelegates) => {
    pendingDelegatesRef.current = delegates
  }, [])

  // chat.js:6216 `_setStreamIdlePausedForApproval` — a stable pass-through to the
  // controller so `useApprovalPending` can pause/resume the idle timer + chip.
  const setStreamIdlePausedForApproval = useCallback(
    (paused: boolean) => controller.setStreamIdlePausedForApproval(paused),
    [controller],
  )

  // chat.js:8660 — the enqueue-while-busy branch (chat.js:6091) queries this.
  const isCompactInFlightForCurrentSession = useCallback(
    () => controller.isCompactInFlightForCurrentSession(),
    [controller],
  )

  return {
    containerRef,
    routerFxDockRef,
    controller,
    send,
    abort,
    busy,
    routerFxEnabled,
    setRouterFxEnabled,
    history,
    runState,
    isCompactInFlightForCurrentSession,
    setStreamIdlePausedForApproval,
    setPendingDelegates,
  }
}
