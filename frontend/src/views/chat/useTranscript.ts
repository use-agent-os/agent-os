import { t } from '@/i18n'
import '@/i18n/en/chat'

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { apiOrigin } from '@/lib/api-origin'
import { callIdempotent } from '@/lib/ws-rpc'
import {
  bindTicketResolver,
  createTicketResolver,
  hydrateTicketSources,
  type TicketResolver,
} from '@/lib/artifact-tickets'
import { useApprovals } from '@/services/approval-monitor'
import { useTheme } from '@/stores/theme'
import { chatMarkdown } from './markdown'
import { createAcpMounter, type AcpMounter, type AcpOpenSession } from './transcript/acp'
import { artifactAuthenticatedDownloadUrl } from './transcript/artifacts'
import { createCardsMounter, type CardsMounter } from './transcript/cards'
import { createChartMounter, type ChartMounter } from './transcript/chart'
import { createDcaMounter, type DcaActions, type DcaMounter } from './transcript/dca'
import { createLpMounter, type LpActions, type LpMounter } from './transcript/lp'
import {
  createMarketsMounter,
  type MarketsMounter,
  type MarketsSwapHandler,
} from './transcript/markets'
import {
  createTriggerMounter,
  type TriggerActions,
  type TriggerMounter,
} from './transcript/trigger'
import {
  createStreamController,
  JUMP_TO_TAIL_GAP_PX,
  type StreamController,
  type TranscriptHeaderStateRef,
} from './transcript/stream'
import type { CompactionSummary } from './transcript/compaction'
import {
  CHAT_HISTORY_PAGE_SIZE,
  applyHistoryDelta,
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
  inFlightDecision,
  inputNormalizationProvenanceFromAttachments,
  outgoingAttachment,
  renderMessageAttachmentHtml,
  replayGapShouldWarn,
  sessionChangeIsTerminal,
  sessionRunStatus,
  stripAssistantText,
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
 * Fetch an inline artifact body for the card mounters (chart.ts, cards.ts, …).
 *
 * The URL carries the session (the artifact renderer builds it through
 * `artifactPreviewUrl`). A ticket for it needs no header; without one (an
 * older gateway) the Authorization header mirrors `downloadArtifact`.
 */
async function fetchChartPayload(url: string, tickets: TicketResolver): Promise<unknown> {
  const ticketUrl = await tickets.resolveUrl(url)
  const headers: Record<string, string> = {}
  const token = ticketUrl ? '' : getAuthToken()
  if (token) headers['Authorization'] = `Bearer ${token}`
  const response = await fetch(ticketUrl || url, {
    method: 'GET',
    headers,
    credentials: 'same-origin',
  })
  if (!response.ok) throw new Error(`chart artifact request failed: HTTP ${response.status}`)
  return response.json()
}

/** The legacy source of a ticketed preview: its URL with the token appended. */
function legacyMediaUrl(raw: string): string {
  return artifactAuthenticatedDownloadUrl(raw, { sessionKey: '', token: getAuthToken() })
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
  /**
   * True while the user has pinned a router tier. A pin removes the choice the
   * router-fx strip exists to dramatize, so the animation is suppressed for the
   * duration — see `isRoutePinned` in `createRouterFxRenderer`.
   */
  routePinned?: boolean
  /**
   * Collect/Remove on the user's own LP positions (lp.ts). Only an operator
   * connection can have those orders approved, so only the desktop's desk
   * passes it; without it the cards carry no write buttons. Read live, so it
   * may come and go without remounting the transcript.
   */
  lpActions?: LpActions | null
  /**
   * The DCA mandate controls (dca.ts: approve, pause, resume, buy now, stop).
   * Operator-only like `lpActions`, so only the desktop's desk passes it;
   * without it the cards carry no `.dca-actions`. Read live.
   */
  dcaActions?: DcaActions | null
  /**
   * The price trigger controls (trigger.ts: approve, reject, pause, resume,
   * fire now, stop). Operator-only like `dcaActions`, so only the desktop's
   * desk passes it; without it the cards carry no `.trigger-actions`. Read live.
   */
  triggerActions?: TriggerActions | null
  /**
   * The Swap button on each markets card row (markets.ts, docs/markets.md):
   * called with the row's `{ chainId, tokenIn, tokenOut }`. The desktop passes
   * one that opens its swap panel; the web console passes none, so its cards
   * carry no `.mk-swap`. Read live.
   */
  onMarketsSwap?: MarketsSwapHandler | null
  /**
   * Navigate to another session: the "Open session" action on a coding
   * harness's run card and completion card (transcript/acp.ts). The web
   * console switches its session; the desktop routes to `/sessions/:key`.
   * Absent: the cards carry no such action. Read live.
   */
  onOpenSession?: AcpOpenSession | null
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
  /**
   * chat.js `_addMessage('system', …)` — append a local system row (the
   * `/model` list). It is not part of the session's history, so the next
   * history rebuild drops it, as legacy's did: after a turn, or on the config
   * re-read when the window regains focus.
   */
  addSystemMessage: (text: string) => void
  /** Current session run state rendered by the header chip. */
  runState: RunStatusResult
  /**
   * False once the reader has scrolled more than `JUMP_TO_TAIL_GAP_PX` above the
   * newest row — the view renders its "jump to latest" affordance on this.
   */
  pinnedToTail: boolean
  /** Return to the newest row AND re-arm tail following. */
  scrollToTail: () => void
  /**
   * True while a compaction is in flight for the CURRENT session (chat.js:8660
   * `_isCompactInFlightForCurrentSession`). ChatPage reads it for the
   * enqueue-while-busy branch (chat.js:6091).
   */
  isCompactInFlightForCurrentSession: () => boolean
  /**
   * chat.js:2738-2763 — `/compact` for the CURRENT session: mark the
   * compaction in flight and show its separator before the RPC goes out (a
   * message sent meanwhile queues behind it), then settle both from the
   * result through the same path the `session.event.compaction` frames take.
   */
  compactContext: () => void
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
  // Read live by the router-fx renderer, which is built once; a plain prop would
  // be captured stale in that closure.
  const routePinnedRef = useRef(false)
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
  // ask_user card → composer send path. `send` is defined after the
  // controller exists, so the card reaches it through a ref (same
  // late-binding pattern as openModalRef).
  const sendUserAnswerRef = useRef<((text: string) => boolean) | null>(null)
  const approvePlanRef = useRef<(() => boolean) | null>(null)
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
  // A reconnect asks only for the rows after the newest one rendered
  // (`chat.history` `delta_cursor`); any other refresh reads the full page.
  const historyDeltaRequestedRef = useRef(false)
  const newestHistoryCursorRef = useRef<{ session: string; cursor: string | null }>({
    session: '',
    cursor: null,
  })
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

  // Ticketed file URLs (lib/artifact-tickets.ts). Previews, card payloads and
  // downloads trade the header-authenticated gateway URLs for tickets bound to
  // this socket, which an <img>, an <audio> or a link can load. Every
  // connection change drops them; an older gateway answers with none, and the
  // legacy URLs apply.
  // eslint-disable-next-line react-hooks/refs -- the getter reads .current only when a ticket is minted, never during render
  const [ticketResolver] = useState<TicketResolver>(() =>
    createTicketResolver({
      call: (method, params) => rpc.call(method, params),
      getSessionKey: () => sessionKeyRef.current,
    }),
  )
  useEffect(() => bindTicketResolver(rpc, ticketResolver), [rpc, ticketResolver])
  // Read once, by the mounter factories below.
  const fetchPayload = (url: string): Promise<unknown> => fetchChartPayload(url, ticketResolver)

  // Chart artifacts (chart.ts). The mounter owns every live chart on this
  // transcript so a theme toggle can re-color them in place and a route unmount
  // can dispose the canvases.
  const [chartMounter] = useState<ChartMounter>(() =>
    createChartMounter({
      fetchPayload,
      getTheme: () => useTheme.getState().mode,
    }),
  )
  // Card-grid artifacts (cards.ts). Plain DOM, so unlike charts there is no
  // canvas or library handle to dispose and no theme callback — the mounter only
  // tracks which placeholders it has already filled.
  const [cardsMounter] = useState<CardsMounter>(() => createCardsMounter({ fetchPayload }))
  // Uniswap V4 liquidity cards (lp.ts). Owns a once-a-minute clock for the
  // "2m ago" stamps and the copy-button resets, both cleared on unmount. ↻
  // re-runs a card's read over this connection (the reads are agent-callable);
  // the write buttons exist only while the caller hands over `lpActions`.
  const lpActionsRef = useRef<LpActions | null>(opts.lpActions ?? null)
  useEffect(() => {
    lpActionsRef.current = opts.lpActions ?? null
  }, [opts.lpActions])
  // eslint-disable-next-line react-hooks/refs -- the factory stores the getters and reads .current only later, inside click handlers and renders outside React's render
  const [lpMounter] = useState<LpMounter>(() =>
    createLpMounter({
      fetchPayload,
      call: (method, params) => rpc.call(method, params),
      actions: () => lpActionsRef.current,
    }),
  )
  // Markets cards (markets.ts): every pool a token trades in. Owns the "2m
  // ago" clock, cleared on unmount. ↻, *Show lookalikes* and *Deeper* re-run
  // `trading.markets` over this connection (agent-callable); the per-row Swap
  // button exists only while the caller hands over `onMarketsSwap`.
  const onMarketsSwapRef = useRef<MarketsSwapHandler | null>(opts.onMarketsSwap ?? null)
  useEffect(() => {
    onMarketsSwapRef.current = opts.onMarketsSwap ?? null
  }, [opts.onMarketsSwap])
  // eslint-disable-next-line react-hooks/refs -- the factory stores the getter and reads .current only later, inside click handlers and renders outside React's render
  const [marketsMounter] = useState<MarketsMounter>(() =>
    createMarketsMounter({
      fetchPayload,
      call: (method, params) => rpc.call(method, params),
      getOnSwap: () => onMarketsSwapRef.current,
    }),
  )
  // DCA mandate cards (dca.ts). Owns the countdown clock (1 s under an hour
  // to the next buy, 1 min above) and the copy / Stop-confirm resets, all
  // cleared on unmount. ↻ re-reads over this connection (`trading.dca.get` /
  // `list` are agent-callable); the controls exist only while the caller hands
  // over `dcaActions`.
  const dcaActionsRef = useRef<DcaActions | null>(opts.dcaActions ?? null)
  useEffect(() => {
    dcaActionsRef.current = opts.dcaActions ?? null
  }, [opts.dcaActions])
  // eslint-disable-next-line react-hooks/refs -- the factory stores the getters and reads .current only later, inside click handlers and renders outside React's render
  const [dcaMounter] = useState<DcaMounter>(() =>
    createDcaMounter({
      fetchPayload,
      call: (method, params) => rpc.call(method, params),
      actions: () => dcaActionsRef.current,
    }),
  )
  // Price trigger cards (trigger.ts). Owns the "checked N s ago" clock (1 s
  // while the stamp counts seconds) and the copy / Stop / Fire-now confirm
  // resets, all cleared on unmount. ↻ re-reads over this connection
  // (`trading.trigger.get` / `list` are agent-callable); the controls exist
  // only while the caller hands over `triggerActions`.
  const triggerActionsRef = useRef<TriggerActions | null>(opts.triggerActions ?? null)
  useEffect(() => {
    triggerActionsRef.current = opts.triggerActions ?? null
  }, [opts.triggerActions])
  // eslint-disable-next-line react-hooks/refs -- the factory stores the getters and reads .current only later, inside click handlers and renders outside React's render
  const [triggerMounter] = useState<TriggerMounter>(() =>
    createTriggerMounter({
      fetchPayload,
      call: (method, params) => rpc.call(method, params),
      actions: () => triggerActionsRef.current,
    }),
  )
  // Coding-harness cards (acp.ts): the completion card of a subagent and the
  // run card of a `sessions_spawn(runtime="acp")`. Owns the one `acp.sessions`
  // poller of this transcript, armed only while a connected run card is
  // running and the window is visible, and the run cards' Stop (`acp.cancel`).
  const onOpenSessionRef = useRef<AcpOpenSession | null>(opts.onOpenSession ?? null)
  useEffect(() => {
    onOpenSessionRef.current = opts.onOpenSession ?? null
  }, [opts.onOpenSession])
  // eslint-disable-next-line react-hooks/refs -- the factory stores the getter and reads .current only later, inside click handlers and renders outside React's render
  const [acpMounter] = useState<AcpMounter>(() =>
    createAcpMounter({
      // A card built while the socket is still coming up (an app reload)
      // waits for it instead of failing its one status read.
      call: async (method, params) => {
        await rpc.waitForConnection()
        return rpc.call(method, params)
      },
      markdown: chatMarkdown,
      openSession: () => onOpenSessionRef.current,
    }),
  )
  useEffect(() => () => acpMounter.destroyAll(), [acpMounter])
  useEffect(
    () =>
      rpc.on('_state', (state: unknown) => {
        if (state === 'connected') acpMounter.reconnected()
      }),
    [rpc, acpMounter],
  )

  // A parked LP write's button waits for its order to settle; a DCA or a
  // trigger card re-reads itself when one of its orders settles.
  useEffect(
    () =>
      rpc.on('trading.order.finished', (payload: unknown) => {
        const order = (payload as { order?: { orderId?: unknown; triggerId?: unknown } } | null)
          ?.order
        if (typeof order?.orderId !== 'string') return
        lpMounter.orderFinished(order.orderId)
        dcaMounter.orderFinished(order.orderId)
        triggerMounter.orderFinished(
          order.orderId,
          typeof order.triggerId === 'string' ? order.triggerId : null,
        )
      }),
    [rpc, lpMounter, dcaMounter, triggerMounter],
  )
  // Every mandate state change carries the full mandate: swap it in place.
  useEffect(
    () => rpc.on('trading.dca.changed', (payload: unknown) => dcaMounter.mandateChanged(payload)),
    [rpc, dcaMounter],
  )
  // Likewise every trigger state change carries the full trigger.
  useEffect(
    () =>
      rpc.on('trading.trigger.changed', (payload: unknown) =>
        triggerMounter.triggerChanged(payload),
      ),
    [rpc, triggerMounter],
  )
  // And every bracket change (either leg, docs/brackets.md) the full bracket.
  useEffect(
    () =>
      rpc.on('trading.bracket.changed', (payload: unknown) =>
        triggerMounter.bracketChanged(payload),
      ),
    [rpc, triggerMounter],
  )

  // One seam for both inline-artifact renderers. The downstream deps (stream.ts,
  // history.ts, artifacts.ts) call this whenever new rows land; keeping a single
  // callback means a new inline artifact type is composed here rather than
  // threaded through every one of them.
  const mountCharts = useCallback(
    (container: HTMLElement) => {
      hydrateTicketSources(container, {
        resolveUrl: ticketResolver.resolveUrl,
        fallbackUrl: legacyMediaUrl,
      })
      chartMounter.mountCharts(container)
      cardsMounter.mountCards(container)
      lpMounter.mountLp(container)
      marketsMounter.mountMarkets(container)
      dcaMounter.mountDca(container)
      triggerMounter.mountTrigger(container)
    },
    [
      ticketResolver,
      chartMounter,
      cardsMounter,
      lpMounter,
      marketsMounter,
      dcaMounter,
      triggerMounter,
    ],
  )

  useEffect(() => {
    const unsubscribe = useTheme.subscribe((state) => chartMounter.applyTheme(state.mode))
    return () => {
      unsubscribe()
      chartMounter.destroyAll()
      cardsMounter.destroyAll()
      lpMounter.destroyAll()
      marketsMounter.destroyAll()
      dcaMounter.destroyAll()
      triggerMounter.destroyAll()
    }
  }, [chartMounter, cardsMounter, lpMounter, marketsMounter, dcaMounter, triggerMounter])

  // eslint-disable-next-line react-hooks/refs -- factory stores the refs and reads .current only later, inside methods invoked outside render (never at creation)
  const [controller] = useState<StreamController>(() =>
    createStreamController(containerRef, {
      markdown: chatMarkdown,
      mountCharts,
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
      routePinned: () => routePinnedRef.current,
      routerFxAwaitConfig: () => awaitRouterConfigReady(routerConfigGate.promise),
      historyHasRendered: () => historyHasRenderedRef.current,
      historyHydrating: () => historyHydratingRef.current,
      openModal: (title, html, buttons) => openModalRef.current?.(title, html, buttons),
      sendUserAnswer: (text) => sendUserAnswerRef.current?.(text) ?? false,
      approvePlan: () => approvePlanRef.current?.() ?? false,
      harnessCard: (spec, options) => acpMounter.harnessCard(spec, options),
      // Artifact preview/download URLs + download Authorization header
      // (chat.js:7575/7657 `App.getAuthToken()`), for a gateway without tickets.
      getAuthToken,
      resolveTicket: (url, options) => ticketResolver.resolveUrl(url, options),
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
      subagentCard: (payload, raw) => acpMounter.completionCard(payload, raw),
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

  /* ── tail state (drives the "jump to latest" affordance) ────────────────── */

  // React-facing mirror of "the reader can see the newest row". The controller
  // keeps the authoritative auto-follow flag; this only decides whether the
  // view offers a way back down, so it uses the wider `JUMP_TO_TAIL_GAP_PX`.
  const [pinnedToTail, setPinnedToTail] = useState(true)
  const syncPinnedToTail = useCallback(() => {
    const th = containerRef.current
    if (!th) return
    const gap = th.scrollHeight - th.scrollTop - th.clientHeight
    setPinnedToTail(gap <= JUMP_TO_TAIL_GAP_PX)
  }, [])

  /**
   * Jump back to the newest row and resume following.
   *
   * Re-arming auto-follow is the point: landing at the bottom without it meant
   * the very next streaming delta could scroll past and strand the reader again.
   */
  const scrollToTail = useCallback(() => {
    controller.resetAutoScroll()
    controller.scrollToBottom()
    syncPinnedToTail()
  }, [controller, syncPinnedToTail])

  // chat.js:2575-2579 — streaming follows the tail only while the reader is
  // already near it. Passive scroll tracking lets a manual upward scroll pause
  // auto-follow until the reader returns within the legacy 60px threshold.
  useEffect(() => {
    const thread = containerRef.current
    if (!thread) return
    const onScroll = () => {
      controller.updateAutoScrollFromThread()
      syncPinnedToTail()
    }
    thread.addEventListener('scroll', onScroll, { passive: true })
    onScroll()
    return () => thread.removeEventListener('scroll', onScroll)
  }, [controller, syncPinnedToTail])

  // Tail following used to be driven ONLY by the render seams that append rows
  // (`scrollToBottom` after a delta / a tool card / a history rebuild). Anything
  // that changed the transcript's height WITHOUT going through one of those
  // seams left the reader stranded above the bottom with no way back but a
  // manual drag, because a height change fires no scroll event:
  //
  //   - a <details> tool/thinking row expanding or collapsing (the turn-end
  //     `collapseLiveThinkingBlock` does exactly this, AFTER the last delta),
  //   - images / charts / cards that mount or decode a frame later,
  //   - late syntax highlighting and web-font swaps reflowing a code block,
  //   - a window resize narrowing the column so every row rewraps taller.
  //
  // A mutation + resize watcher closes that gap: while the reader is pinned, any
  // height change re-pins on the next frame. It is a no-op whenever auto-follow
  // is paused, so it never fights a reader who scrolled up.
  useEffect(() => {
    const thread = containerRef.current
    if (!thread) return
    let frame = 0
    const repin = () => {
      frame = 0
      if (controller.isAutoScrollEnabled()) controller.scrollToBottom()
      syncPinnedToTail()
    }
    const schedule = () => {
      if (frame) return
      frame = requestAnimationFrame(repin)
    }
    const mutations = new MutationObserver(schedule)
    mutations.observe(thread, {
      childList: true,
      subtree: true,
      characterData: true,
      // `open` is the <details> toggle; `style`/`class` cover collapse
      // animations and the height-bearing state classes on tool cards.
      attributeFilter: ['open', 'style', 'class'],
    })
    // Images finish decoding without mutating the DOM, so they need the capture
    // -phase load event (which does not bubble) on top of the mutation watch.
    thread.addEventListener('load', schedule, true)
    // The viewport half: narrowing the column rewraps every row taller without
    // touching the DOM. Feature-detected — jsdom has no ResizeObserver, and the
    // mutation watch above already covers everything tests exercise.
    const resize = typeof ResizeObserver === 'function' ? new ResizeObserver(schedule) : null
    resize?.observe(thread)
    return () => {
      if (frame) cancelAnimationFrame(frame)
      mutations.disconnect()
      resize?.disconnect()
      thread.removeEventListener('load', schedule, true)
    }
  }, [controller, syncPinnedToTail])

  // chat.js:916-932 `_bindHoverActions` — every artifact (and gateway-stored
  // attachment) target downloads through `downloadArtifact`: a ticket when the
  // gateway mints them, else the authenticated fetch, so token/session headers
  // and failure toasts work. A chip's own href carries no credential.
  //
  // The one exception is an anchor on the gateway-served console once the
  // gateway is known to have no tickets: there the browser's native download
  // keeps the legacy behavior. Off gateway (the desktop renderer, loaded from
  // disk) the native path cannot work at all: the chip's href points at the
  // gateway, which is another origin, and Chromium ignores a cross-origin
  // `download` attribute and navigates instead — a top-frame navigation the
  // shell refuses.
  useEffect(() => {
    const thread = containerRef.current
    if (!thread) return
    const onArtifactClick = (event: MouseEvent): void => {
      if (!(event.target instanceof Element)) return
      const artifact = event.target.closest<HTMLElement>('[data-artifact-download]')
      if (!artifact) return
      if (artifact.tagName === 'A' && !apiOrigin() && !ticketResolver.available()) return
      event.preventDefault()
      event.stopPropagation()
      void controller.downloadArtifact({
        id: artifact.dataset.artifactId || '',
        name: artifact.dataset.artifactName || artifact.getAttribute('download') || 'artifact',
        download_url: artifact.dataset.artifactDownload || '',
      })
    }
    thread.addEventListener('click', onArtifactClick)
    return () => thread.removeEventListener('click', onArtifactClick)
  }, [controller, ticketResolver])

  // Mirror the pin into the ref the router-fx renderer reads live.
  useEffect(() => {
    routePinnedRef.current = opts.routePinned === true
  }, [opts.routePinned])

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
          stripText: stripAssistantText,
          renderText: (text, into) => {
            into.innerHTML = chatMarkdown.render(text)
            chatMarkdown.bindCopy(into)
            chatMarkdown.bindHighlight?.(into)
          },
        }),
      renderMessageAttachmentHtml,
      renderArtifacts: (artifacts) => controller.renderArtifacts(artifacts),
      mountCharts,
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
      getLiveThinkingBlock: () => controller.getLiveThinkingBlock(),
      fetchThinking: async (messageId: string) => {
        const res = (await rpc.call('chat.thinking', {
          sessionKey: sessionKeyRef.current,
          messageId,
        })) as { reasoning?: string | null } | null
        return res?.reasoning ?? null
      },
      fetchEntry: async (entryId: string) => {
        const res = (await rpc.call('chat.entry', {
          sessionKey: sessionKeyRef.current,
          entryId,
        })) as { message?: ChatMessage | null } | null
        return res?.message ?? null
      },
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
  // Bumped by every send, so a subscribe answer can tell that the user started
  // a turn while it was in flight (replayed frames restart a stream too).
  const sendCountRef = useRef(0)

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
      sendCountRef.current += 1
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
      const providerText = trimmed || t('chat.attachmentsPrompt')

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
      // A per-message idempotency key: a send whose answer was lost with the
      // socket is retried after the reconnect, and the gateway answers the
      // retry with the turn it already started instead of a second one.
      callIdempotent(rpc, 'chat.send', params)
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
          const errorRow = messageRendererRef.current?.addMessage(
            'error',
            t('chat.sendFailed', { message }),
          )
          if (errorRow) errorRow.dataset.liveError = 'true'
        })
    },
    [rpc, controller, routerConfigGate],
  )

  // Late-bind the ask_user card's answer path to the freshest `send` so a
  // card built mid-stream still sends through the current session wiring.
  // Reports success so the card only locks itself once the answer went out
  // (send() is a no-op while the ended turn's stream is still settling).
  useEffect(() => {
    sendUserAnswerRef.current = (text) => {
      if (controller.isStreaming()) return false
      send(text)
      return true
    }
    // Plan approval is out of band by design: turn plan mode off via RPC
    // FIRST, then send the go-ahead so the next turn already has its full
    // tool surface back. The card locks optimistically; an RPC failure
    // surfaces as an error message in the transcript via the send path.
    // The Toolbar's plan toggle reads `plan.mode.get` from the query cache, so
    // it re-reads the flag once the RPC settles, as the toggle and `/plan` do.
    approvePlanRef.current = () => {
      if (controller.isStreaming()) return false
      const key = sessionKeyRef.current
      if (!key) return false
      const refreshPlanMode = () =>
        void queryClient.invalidateQueries({ queryKey: ['plan.mode.get', key] })
      void rpc
        .call('plan.mode.set', { key, mode: 'off' })
        .then(() => {
          refreshPlanMode()
          send(t('chat.planApprovedMessage'))
        })
        .catch((err: unknown) => {
          refreshPlanMode()
          const message = err instanceof Error ? err.message : String(err)
          messageRendererRef.current?.addMessage('error', t('chat.sendFailed', { message }))
        })
      return true
    }
  }, [controller, rpc, send, queryClient])

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
    // Tail following is controller state that outlives the session switch. A
    // stale `false` (the reader had scrolled up in the previous session) would
    // suppress the reveal-time pin below AND make the history renderer take its
    // `preserveScroll` branch, restoring the PREVIOUS session's offset into a
    // freshly built transcript. Every session opens at its tail.
    controller.resetAutoScroll()
    if (th) th.dataset.historyReady = 'false'
  }, [controller, opts.sessionKey])

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
    // chat.js:1819-1820 — a compaction marked in flight belongs to the session
    // it ran in. Its frames stop arriving once that session is unsubscribed, so
    // the flag would otherwise outlive it and queue every send on the way back.
    controller.setCompactInFlight(false)
    controller.hideCompactionSeparator()
    controller.syncLastStreamSeqFromSession(opts.sessionKey)
    // eslint-disable-next-line react-hooks/set-state-in-effect -- a session-key transition must synchronously reset the externally-driven status mirror
    applySessionRunState({ run_status: 'idle' })
  }, [applySessionRunState, controller, opts.sessionKey])

  /* ── History read via react-query (chat.js:5440 `_loadHistory`) ─────────── */

  const historyQuery = useQuery<HistoryResponse>({
    queryKey: ['chat', 'history', opts.sessionKey],
    queryFn: async () => {
      historyHydratingRef.current = true
      // Cleared only once a read completes: a resync that supersedes this read
      // mid-flight (the subscribe's terminal sync right after a reconnect)
      // still reads the delta rather than the full page.
      const wantDelta = historyDeltaRequestedRef.current
      await rpc.waitForConnection()
      // Do not reconstruct router receipts against placeholder tier labels. The
      // timeout is faithful: config failure must never hang history forever.
      await awaitRouterConfigReady(routerConfigGate.promise)
      const known = newestHistoryCursorRef.current
      if (wantDelta && known.session === opts.sessionKey && known.cursor) {
        try {
          const delta = await rpc.call<HistoryResponse>('chat.history', {
            sessionKey: opts.sessionKey,
            delta_cursor: known.cursor,
            includeCanonical: false,
            includeSummaries: false,
          })
          const merged = applyHistoryDelta(pagingRef.current, delta)
          if (merged) {
            historyDeltaRequestedRef.current = false
            return merged
          }
          // `kind: "reset"` (or more rows than one delta carries): read the
          // full page below, as before.
        } catch {
          // An older gateway, or a transient failure: the full read decides.
        }
      }
      // Legacy `_loadHistory` params (chat.js:5457-5462): `sessionKey`, `limit`,
      // `includeCanonical:false`, `includeSummaries:true`.
      const page = await rpc.call<HistoryResponse>('chat.history', {
        sessionKey: opts.sessionKey,
        limit: CHAT_HISTORY_PAGE_SIZE,
        includeCanonical: false,
        includeSummaries: true,
      })
      historyDeltaRequestedRef.current = false
      return page
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
      newestHistoryCursorRef.current = { session: opts.sessionKey, cursor: meta.newestCursor }
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
      // The rebuild above replaced every row, so any chart from the previous
      // session (or the previous page of this one) is now detached.
      chartMounter.pruneDetached()
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
    chartMounter,
    opts.sessionKey,
    revealTranscriptIfSettled,
    subscriptionSettleRevision,
  ])

  // Surface a history-load error into the scope row (chat.js:5484-5488).
  useLayoutEffect(() => {
    if (!historyQuery.isError) return
    historyHydratingRef.current = false
    pagingRef.current.error = t('chat.historyLoadFailed')
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
        // Same rebuild, same sweep: the pre-existing chart rows were replaced.
        chartMounter.pruneDetached()
        // Re-overlay the compaction-summary separators against the new row set.
        controller.renderCompactionSummarySeparators(mergedMessages)
      } catch {
        state.loadingEarlier = false
        state.error = t('chat.historyEarlierFailed')
        historyRenderer.renderHistoryScopeRow(state)
      } finally {
        loadingEarlierGuard.current = false
      }
    }
    reloadHistoryRef.current = () => {
      void queryClient.invalidateQueries({ queryKey: ['chat', 'history', opts.sessionKey] })
    }
  }, [opts.sessionKey, historyRenderer, rpc, queryClient, controller, chartMounter])

  /* ── Reset (`/reset`) clears the visible thread ─────────────────────────── */

  // `sessions.reset` rotates the session's `session_id` but keeps the session
  // KEY, so none of the session-switch teardown above fires and the reset
  // conversation stays on screen. The gateway broadcasts `session.epoch_changed`
  // on (and only on) that path, so an epoch bump is the reset signal — for the
  // typed `/reset`, the slash menu, the SessionChip button, and a reset issued
  // by another connected client alike.
  const clearTranscriptForReset = useCallback(() => {
    // MUST come first: `renderHistoryMessages([])` deliberately preserves a
    // live stream bubble, thinking indicator, or pending-finalized assistant
    // bubble (transcript/history.ts:439-464), so any of those would survive the
    // clear and leave a half-answer floating above an empty thread. This drops
    // all three plus the router strips and lowers the streaming flag.
    controller.clearViewLocalStreamState('session_reset')
    setBusyRef.current()
    activeTaskGroupsRef.current.clear()
    abortedRef.current = false
    stopRequestedByUserRef.current = false
    historyHasRenderedRef.current = false
    headerStateRef.current.day = ''
    headerStateRef.current.role = ''
    pagingRef.current = {
      loadedMessages: [],
      oldestCursor: null,
      hasMore: false,
      scope: 'complete',
      loadingEarlier: false,
      error: '',
      compactionSummaries: [],
    }
    // Immediate visual clear — the empty branch wipes the thread and renders
    // the "No messages yet." state.
    historyRenderer.renderHistoryMessages([], pagingRef.current)
    applySessionRunState({ run_status: 'idle' })
    // Authoritative refetch. The rotated `session_id` makes `chat.history`
    // return empty, so this confirms the clear rather than reverting it.
    void queryClient.invalidateQueries({ queryKey: ['chat', 'history', sessionKeyRef.current] })
  }, [applySessionRunState, controller, historyRenderer, queryClient])

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

    // The subscribe answer names the session's real run state; it wins over
    // whatever the view reconstructed on the way in. A parked live view
    // restored on return, and frames replayed ahead of the answer (a turn's
    // deltas, heartbeats and state changes), both leave the thread streaming.
    // A turn that ended through a task-only terminal — Stop, a kill, a steer,
    // a gateway restart — has no `done` / `error` frame to close it, so the
    // view stayed "Running" with a Stop button and a cursor for good (a
    // harness child then read "Working · Starting Claude Code…"). Once the
    // gateway says no turn is active, drop the stale live view and re-render
    // from history, which already holds the partial answer and its stop row.
    const reconcileStreamWithGateway = (state: Record<string, unknown> | null) => {
      if (!state || typeof state.run_status !== 'string') return
      if (!controller.isStreaming()) return
      const status = sessionRunStatus(state).status
      if (['queued', 'running', 'approval_pending'].includes(status)) return
      diag('session.subscribe.stream_reconciled', { status })
      controller.clearViewLocalStreamState('subscribe_reconcile')
      applySessionRunState(state)
      seams().applySessionRunState?.(state)
      resyncHistory()
    }

    // The gateway's `in_flight_run` is authoritative where the gateway sends
    // it: `null` ends whatever live view the session came back with (a parked
    // bubble, replayed frames), an object is the running turn and its text so
    // far. Returns false for an older gateway, which falls back to the
    // run-status reconcile above.
    const adoptInFlightRun = (
      state: Record<string, unknown> | null,
      sentDuringSubscribe: boolean,
    ): boolean => {
      const decision = inFlightDecision(state, controller.syncLastStreamSeqFromSession(sessionKey))
      if (decision.kind === 'legacy') return false
      // A send started while the subscribe was in flight belongs to a turn
      // this answer predates.
      if (sentDuringSubscribe && controller.isStreaming()) return true
      if (decision.kind === 'clear') {
        controller.discardParkedStreamState(sessionKey)
        if (!controller.hasViewLocalStreamState()) return true
        diag('session.subscribe.inflight_cleared', {})
        controller.clearViewLocalStreamState('inflight_snapshot_cleared')
        if (state) {
          applySessionRunState(state)
          seams().applySessionRunState?.(state)
        }
        resyncHistory()
        return true
      }
      if (decision.kind === 'keep') return true
      diag('session.subscribe.inflight_adopted', {
        rebuild: decision.rebuild,
        textLen: decision.text.length,
      })
      if (decision.rebuild && controller.hasViewLocalStreamState()) {
        controller.clearViewLocalStreamState('inflight_snapshot_rebuilt')
      }
      if (!controller.isStreaming()) {
        controller.startStreaming()
        if (!decision.text) controller.showThinkingIndicator()
      }
      if (decision.text) controller.reconcileFinalStreamText(decision.text)
      controller.resetStreamIdleTimer()
      return true
    }

    // ── Subscribe (chat.js:2857 `_subscribeSession`) ──────────────────────
    const subscribe = async () => {
      let gatewayState: Record<string, unknown> | null = null
      let sentDuringSubscribe = false
      try {
        await rpc.waitForConnection()
        if (cancelled || sessionKey !== sessionKeyRef.current) return
        const sendsAtCall = sendCountRef.current
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
        sentDuringSubscribe = sendCountRef.current !== sendsAtCall
        if (res && res.subscribed === false) throw new Error(t('chat.noSubscriptionManager'))
        const subscribedState = (res as Record<string, unknown>) ?? {}
        gatewayState = subscribedState
        applySessionRunState(subscribedState)
        seams().applySessionRunState?.(subscribedState)
        const currentSeq = Number(res?.current_stream_seq)
        const gapReason = String(res?.replay_gap_reason || res?.replayGapReason || '')
        if (
          Number.isFinite(currentSeq) &&
          (currentSeq < sinceStreamSeq ||
            gapReason === 'stream_buffer_reset' ||
            gapReason === 'cursor_ahead_of_stream')
        ) {
          // The gateway's stream for this session started over (it restarted,
          // or no longer holds it): its next frames reuse `stream_seq` values
          // this view has seen, and they are new, not duplicates.
          diag('session.subscribe.stream_seq_reset', { sinceStreamSeq, currentSeq })
          controller.resetStreamSeq(sessionKey)
        }
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
            toast.warning(t('chat.streamMissedEvents'), { duration: 5000 })
          }
          resyncHistory()
        }
        if (subscribeResultNeedsTerminalHistorySync(res)) resyncHistory()
        if (controller.isStreaming()) controller.resetStreamIdleTimer()
      } catch (error: unknown) {
        toast.error(
          t('chat.streamSubscribeFailed', {
            message: error instanceof Error ? error.message : String(error),
          }),
          { duration: 6000 },
        )
        diag('session.subscribe.error', { sessionKey })
      } finally {
        // The gateway returns from subscribe only after replay frames have been
        // sent. Some replay handlers (notably router decisions) continue across
        // async config work, so include them in the same entry barrier.
        await Promise.allSettled(Array.from(pendingReplayTasks))
        if (!cancelled && sessionKey === sessionKeyRef.current) {
          if (!adoptInFlightRun(gatewayState, sentDuringSubscribe)) {
            reconcileStreamWithGateway(gatewayState)
          }
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

    // session.event.thinking — streamed model reasoning → live collapsible block.
    unsubs.push(
      onEvent('session.event.thinking', (payload: StreamEventPayload) => {
        if (!gateStreamFrame('event.thinking', payload)) return
        controller.resetStreamIdleTimer()
        controller.appendThinkingDelta((payload as { text?: string }).text || '')
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

    // A message steered into the running turn: it shows after the work done
    // so far, and the rest of the turn streams below it.
    unsubs.push(
      onEvent('session.event.steering', (payload: StreamEventPayload) => {
        if (!gateStreamFrame('event.steering', payload)) return
        controller.resetStreamIdleTimer()
        const steer = payload as { text?: string; message_id?: string | null }
        controller.splitStreamAtSteer(steer.text || '', steer.message_id || '')
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
        const message = (payload as { message?: string })?.message || t('chat.capWarning')
        toast.warning(message, { duration: 5000 })
        seams().showWarningToast?.(message)
      }),
    )

    // chat.js:4899-4905 — epoch bump drops stale pre-reset frames, and (since
    // the bump only ever comes from `sessions.reset`) clears the thread the
    // reset just emptied server-side.
    unsubs.push(
      onEvent('session.epoch_changed', (payload: StreamEventPayload) => {
        if (isForeign(payload)) return
        const epoch = Number(payload.epoch)
        if (Number.isFinite(epoch) && epoch > currentEpochRef.current) {
          currentEpochRef.current = epoch
          clearTranscriptForReset()
        }
        seams().onEpochChanged?.(payload)
      }),
    )

    // chat.js:4909 — sessions.changed → terminal resync or run-state apply.
    unsubs.push(
      onEvent('sessions.changed', (payload: StreamEventPayload) => {
        if (isStaleEpoch(payload)) return
        if (!isCurrentSessionPayload(payload, sessionKeyRef.current)) return
        // Replayed to a late subscriber too now: one already applied live is
        // not applied twice.
        if (!controller.acceptStreamSeq(payload)) {
          diag('sessions.changed.drop.stream_seq', {})
          return
        }
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
        if (!controller.acceptStreamSeq(payload)) return
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
        if (!controller.acceptStreamSeq(payload)) return
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
        if (!isCurrentSessionPayload(rawPayload, sessionKeyRef.current)) {
          // A task terminal for a session parked off-screen: its turn is over,
          // so the parked live view must not come back streaming on the next
          // visit (a Stop or a kill leaves no stream frame that would end it).
          if (rawEvent.startsWith('task.')) {
            const p = rawPayload as { key?: string; session_key?: string; sessionKey?: string }
            const key = p.key || p.session_key || p.sessionKey || ''
            if (
              key &&
              ['succeeded', 'failed', 'timeout', 'abandoned', 'cancelled'].includes(
                rawEvent.slice('task.'.length),
              )
            ) {
              controller.discardParkedStreamState(key)
            }
          }
          return
        }

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
        // A task terminal is replayed to a late subscriber as well; the one
        // this view already handled live must not finalize twice. (Queued /
        // running frames were accepted by their own handlers above.)
        if (rawEvent.startsWith('task.') && !controller.acceptStreamSeq(rawPayload)) {
          diag('event.generic.drop.stream_seq', { event: rawEvent })
          return
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
                ? t('chat.taskTimedOut')
                : code.includes('abandoned')
                  ? t('chat.taskStopped')
                  : code.includes('cancelled')
                    ? t('chat.taskCancelled')
                    : typeof payload.message === 'string' && payload.message
                      ? payload.message
                      : t('chat.agentError')
          const errorRow = messageRendererRef.current?.addMessage('error', message)
          if (errorRow) {
            // Survives the history resync below until the gateway persists a
            // row of its own for this failure (history.ts liveErrorToKeep).
            errorRow.dataset.liveError = 'true'
            // A stopped turn is a notice, not a failure.
            if (code.includes('cancel')) errorRow.dataset.tone = 'notice'
          }
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
          // What the view already shows stays; only the rows written while
          // the socket was down are read (full page if the cursor is gone).
          historyDeltaRequestedRef.current = true
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
        toast.warning(t('chat.streamGap'))
        seams().showWarningToast?.(t('chat.streamGap'))
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
  }, [applySessionRunState, opts.sessionKey, rpc, controller, queryClient, clearTranscriptForReset])

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

  const addSystemMessage = useCallback((text: string) => {
    messageRendererRef.current?.addMessage('system', text, Date.now())
  }, [])

  // chat.js:8660 — the enqueue-while-busy branch (chat.js:6091) queries this.
  const isCompactInFlightForCurrentSession = useCallback(
    () => controller.isCompactInFlightForCurrentSession(),
    [controller],
  )

  // chat.js:2738-2763 — the slash command's manual compaction. The gateway
  // also broadcasts the lifecycle as `session.event.compaction`, and those
  // frames go out before the RPC returns: when they have already settled the
  // compaction, the result has nothing left to add (legacy fed it through
  // anyway, which doubled the failure toast). A result that lands after a
  // session switch belongs to the old session.
  const compactContext = useCallback(() => {
    const key = sessionKeyRef.current
    if (!key) return
    controller.setCompactInFlight(true, key)
    controller.syncCompactionSeparator(
      { key, source: 'manual', status: 'started', phase: 'manual' },
      'started',
      'manual',
    )
    rpc
      .call('sessions.contextCompact', { key })
      .then((result: unknown) => {
        if (key !== sessionKeyRef.current) return
        if (!controller.isCompactInFlightForCurrentSession()) return
        controller.showCompactionToast({
          ...((result ?? {}) as Record<string, unknown>),
          key,
          source: 'manual',
        })
      })
      .catch((err: unknown) => {
        if (key !== sessionKeyRef.current) return
        if (!controller.isCompactInFlightForCurrentSession()) return
        controller.showCompactionToast({
          key,
          source: 'manual',
          status: 'failed',
          message: (err instanceof Error && err.message) || 'unknown error',
        })
      })
  }, [controller, rpc])

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
    addSystemMessage,
    runState,
    pinnedToTail,
    scrollToTail,
    isCompactInFlightForCurrentSession,
    compactContext,
    setStreamIdlePausedForApproval,
    setPendingDelegates,
  }
}
