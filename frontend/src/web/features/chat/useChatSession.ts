import { useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useLayoutEffect, useReducer, useRef } from 'react'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { t } from '@/i18n'
import '@/i18n/en/webChat'
import { newIdempotencyKey } from '@/lib/ws-rpc'
import { useApprovals } from '@/services/approval-monitor'
import { CHAT_HISTORY_PAGE_SIZE } from '@/views/chat/transcript/history'
import { createSeqGate, DEFAULT_STREAM_IDLE_TIMEOUT_MS } from '@/views/chat/transcript/stream'
import { SESSIONS_QUERY_KEY } from '@/web/shell/session-data'
import { initialTranscript, isRunning, reduce } from './model/reducer'
import type {
  HistoryPage,
  InFlightRun,
  TranscriptInput,
  TranscriptState,
  Turn,
} from './model/types'
import { sendOnce } from './send'

// The chat subscription (overhaul-control-ui D7, task 6.5): chat.history
// pages and sessions.messages.subscribe frames feed the transcript reducer.
// Frames for another session, from an older epoch or already seen (the kept
// createSeqGate) are dropped; a gap, an epoch change or a reconnect reloads
// the history window, which the reducer reconciles by entry id.
//
// The first subscribe asks for no replay: its answer and the history page
// carry the running turn as a snapshot (`in_flight_run`), and frames after it
// arrive live. A reconnect replays from the last frame seen. Streamed text is
// folded once per animation frame; anything else flushes at once.

interface SubscribeResponse {
  current_stream_seq?: number
  replay_complete?: boolean
  replay_gap_reason?: string | null
  in_flight_run?: InFlightRun | null
}

const RESYNC_GAP_REASONS = new Set(['stream_buffer_reset', 'cursor_ahead_of_stream'])

/** Frames worth batching: they arrive many per second while a turn streams. */
const COALESCED = new Set([
  'session.event.text_delta',
  'session.event.thinking',
  'session.event.run_heartbeat',
])

/** Without any frame for this long, a running turn is checked against the gateway. */
export const STREAM_IDLE_TIMEOUT_MS = DEFAULT_STREAM_IDLE_TIMEOUT_MS

type FrameInput = { name: string; payload: Record<string, unknown>; at: number }

function nextFrame(callback: () => void): () => void {
  if (typeof requestAnimationFrame === 'function') {
    const id = requestAnimationFrame(callback)
    return () => cancelAnimationFrame(id)
  }
  const id = setTimeout(callback, 16)
  return () => clearTimeout(id)
}

function warnMissedEvents(): void {
  toast.warning(t('webChat.missedEvents'), { id: 'chat-missed-events' })
}

export interface SendInput {
  text: string
  /** Shown in the transcript when it differs from what the model gets. */
  displayText?: string
  attachments?: unknown[]
  intent?: string | null
  extra?: Record<string, unknown>
}

export interface ChatSession {
  state: TranscriptState
  loading: boolean
  error: string | null
  send(input: SendInput): Promise<boolean>
  /** Resend a failed turn's message with a fresh idempotency key. */
  retry(turn: Turn): Promise<boolean>
  /**
   * Ask the gateway to stop the running turn; throws when it refused. Asked
   * again while the first stop is pending, it stops the turn at once.
   */
  abort(): Promise<void>
  loadOlder(): Promise<void>
  reload(): Promise<void>
  /** The composer's queue length, for Details. */
  setQueued(count: number): void
  /** Show local output (a slash command's) in the transcript. */
  addNote(text: string): void
}

function frameSessionKey(payload: Record<string, unknown>): string {
  const value = payload.session_key ?? payload.key ?? payload.sessionKey
  return typeof value === 'string' ? value : ''
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

export function useChatSession(sessionKey: string | null): ChatSession {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const [state, dispatchNow] = useReducer(reduce, undefined, initialTranscript)
  const [status, setStatus] = useReducer(
    (
      _: { loading: boolean; error: string | null },
      next: { loading: boolean; error: string | null },
    ) => next,
    { loading: Boolean(sessionKey), error: null },
  )
  const gate = useRef(createSeqGate())
  const keyRef = useRef(sessionKey)
  const stateRef = useRef(state)
  const loadSeq = useRef(0)
  const buffered = useRef<FrameInput[]>([])
  const cancelFlush = useRef<(() => void) | null>(null)

  // Layout timing: the transcript's own effects (paging) read it right after.
  useLayoutEffect(() => {
    stateRef.current = state
  }, [state])

  // Frames held for the next animation frame go first, so order is kept.
  const flush = useCallback(() => {
    cancelFlush.current?.()
    cancelFlush.current = null
    const events = buffered.current
    if (events.length === 0) return
    buffered.current = []
    dispatchNow(events.length === 1 ? { type: 'event', ...events[0]! } : { type: 'events', events })
  }, [])
  const dispatch = useCallback(
    (input: TranscriptInput) => {
      flush()
      dispatchNow(input)
    },
    [flush],
  )
  const enqueueFrame = useCallback(
    (frame: FrameInput) => {
      if (!COALESCED.has(frame.name)) {
        flush()
        dispatchNow({ type: 'event', ...frame })
        return
      }
      buffered.current.push(frame)
      if (!cancelFlush.current) cancelFlush.current = nextFrame(flush)
    },
    [flush],
  )
  useEffect(() => () => cancelFlush.current?.(), [])

  const loadHistory = useCallback(
    async function load(key: string, mode: 'replace' | 'older' | 'delta'): Promise<void> {
      const id = ++loadSeq.current
      const params: Record<string, unknown> = {
        sessionKey: key,
        limit: CHAT_HISTORY_PAGE_SIZE,
        includeCanonical: false,
        includeSummaries: mode !== 'delta',
      }
      if (mode === 'older') params.before = stateRef.current.oldestCursor
      if (mode === 'delta') params.delta_cursor = stateRef.current.newestCursor
      try {
        await rpc.waitForConnection()
        const issuedAt = Date.now()
        const page = await rpc.call<HistoryPage>('chat.history', params)
        if (keyRef.current !== key || (mode !== 'older' && id !== loadSeq.current)) return
        dispatch({ type: 'historyPage', page: page ?? {}, mode, issuedAt })
        if (mode === 'replace') setStatus({ loading: false, error: null })
      } catch (e) {
        if (keyRef.current !== key) return
        if (mode === 'replace') setStatus({ loading: false, error: errorMessage(e) })
        // A failed delta resyncs with a full window instead.
        else if (mode === 'delta') void load(key, 'replace')
        else toast.error(t('webChat.loadOlderFailed'))
      }
    },
    [rpc, dispatch],
  )

  // Open / switch / close a session.
  useEffect(() => {
    keyRef.current = sessionKey
    dispatch({ type: 'reset' })
    if (!sessionKey) {
      setStatus({ loading: false, error: null })
      return
    }
    setStatus({ loading: true, error: null })
    const key = sessionKey
    let active = true

    const resetStream = () => {
      gate.current.reset(key)
      dispatch({ type: 'streamReset' })
    }

    const subscribe = async () => {
      try {
        await rpc.waitForConnection()
        const since = gate.current.highWater(key)
        const params: Record<string, unknown> = { key }
        // Replay only what this view missed; a first open takes the snapshot.
        if (since > 0) params.since_stream_seq = since
        const issuedAt = Date.now()
        const res = await rpc.call<SubscribeResponse>('sessions.messages.subscribe', params)
        if (!active) return
        const current = Number(res?.current_stream_seq)
        if (
          (Number.isFinite(current) && current < since) ||
          RESYNC_GAP_REASONS.has(String(res?.replay_gap_reason ?? ''))
        ) {
          resetStream()
        }
        if (Number.isFinite(current)) gate.current.sync(key, current)
        if (res && Object.prototype.hasOwnProperty.call(res, 'in_flight_run')) {
          dispatch({ type: 'runState', inFlight: res.in_flight_run ?? null, issuedAt })
        }
        if (res?.replay_complete === false) {
          if (since > 0) warnMissedEvents()
          void loadHistory(key, 'replace')
        }
      } catch {
        /* the reconnect handler subscribes again */
      }
    }

    const onFrame = (...args: unknown[]) => {
      const [name, rawPayload] = args as [string, Record<string, unknown> | undefined]
      if (
        !name.startsWith('session.event.') &&
        name !== 'session.epoch_changed' &&
        !name.startsWith('task.')
      ) {
        return
      }
      const payload = rawPayload ?? {}
      const frameKey = frameSessionKey(payload)
      if (frameKey && frameKey !== key) return
      if (!frameKey && name.startsWith('task.')) return
      const seq = Number(payload.stream_seq)
      if (Number.isFinite(seq) && !gate.current.accept(key, seq)) return
      enqueueFrame({ name, payload, at: Date.now() })
      // A finished run: reload the window so entries carry their history ids.
      if (name === 'session.event.done' || name === 'session.event.error') {
        void loadHistory(key, 'replace')
        void queryClient.invalidateQueries({ queryKey: SESSIONS_QUERY_KEY })
      }
    }

    const offFrame = rpc.on('*', onFrame)
    const offHello = rpc.on('_hello', () => {
      void subscribe()
      void loadHistory(key, stateRef.current.newestCursor ? 'delta' : 'replace')
    })
    const offGap = rpc.on('_gap', () => {
      resetStream()
      warnMissedEvents()
      void loadHistory(key, 'replace')
    })

    void loadHistory(key, 'replace')
    void subscribe()

    return () => {
      active = false
      offFrame()
      offHello()
      offGap()
      void rpc.call('sessions.messages.unsubscribe', { key }).catch(() => {})
    }
  }, [sessionKey, rpc, loadHistory, queryClient, dispatch, enqueueFrame])

  // The reducer asked for a fresh window (epoch change, unusable delta).
  useEffect(() => {
    if (state.needsResync && sessionKey) void loadHistory(sessionKey, 'replace')
  }, [state.needsResync, sessionKey, loadHistory])

  // Backstop: a running turn that has been silent this long is checked
  // against the gateway (its history page says whether anything still runs).
  const running = isRunning(state)
  useEffect(() => {
    if (!running || !sessionKey) return
    const timer = setTimeout(() => void loadHistory(sessionKey, 'replace'), STREAM_IDLE_TIMEOUT_MS)
    return () => clearTimeout(timer)
  }, [running, state.turns, sessionKey, loadHistory])

  const deliver = useCallback(
    async (input: SendInput, shown: string): Promise<boolean> => {
      const key = keyRef.current
      if (!key) return false
      const idempotencyKey = newIdempotencyKey()
      dispatch({
        type: 'optimisticSend',
        text: shown,
        attachments: input.attachments,
        idempotencyKey,
        at: Date.now(),
      })
      const params: Record<string, unknown> = {
        message: input.text,
        sessionKey: key,
        idempotency_key: idempotencyKey,
        ...input.extra,
      }
      const elevated = useApprovals.getState().elevatedMode
      if (elevated) params._source = { elevated }
      if (input.intent) params.intent = input.intent
      if (input.attachments?.length) {
        params.attachments = input.attachments
        params.displayText = shown
      }
      const outcome = await sendOnce(rpc, 'chat.send', params)
      if (outcome.status === 'sent') {
        const result = outcome.result as { task_id?: unknown } | null | undefined
        const runId = typeof result?.task_id === 'string' ? result.task_id : undefined
        dispatch({ type: 'sendAccepted', idempotencyKey, at: Date.now(), runId })
        return true
      }
      dispatch({
        type: 'sendFailed',
        idempotencyKey,
        error: errorMessage(outcome.error),
        notSent: outcome.status === 'not_sent',
      })
      return false
    },
    [rpc, dispatch],
  )

  const send = useCallback(
    (input: SendInput) => deliver(input, input.displayText ?? input.text),
    [deliver],
  )

  const retry = useCallback(
    (turn: Turn) => {
      const message = turn.userMessage
      if (!message) return Promise.resolve(false)
      return deliver(
        {
          text: message.text,
          attachments: message.attachments.length ? message.attachments : undefined,
        },
        message.text,
      )
    },
    [deliver],
  )

  const abort = useCallback(async () => {
    const key = keyRef.current
    if (!key) return
    const live = stateRef.current.liveTurns.at(-1)
    // Asked twice already: the gateway is cancelling the turn at once.
    if (live?.forceStop) return
    // Asked while the first stop is pending: the gateway joins the two and
    // ends the turn without its grace period. Both answers arrive together.
    const force = Boolean(live?.abortRequested)
    dispatch({ type: 'abortRequested', force })
    try {
      await rpc.call('chat.abort', { sessionKey: key, source: 'web' })
    } catch (e) {
      if (keyRef.current === key) dispatch({ type: 'abortFailed', force })
      throw e
    }
  }, [rpc, dispatch])

  const loadOlder = useCallback(async () => {
    const key = keyRef.current
    if (!key || !stateRef.current.hasMore || !stateRef.current.oldestCursor) return
    await loadHistory(key, 'older')
  }, [loadHistory])

  const reload = useCallback(async () => {
    if (keyRef.current) await loadHistory(keyRef.current, 'replace')
  }, [loadHistory])

  const setQueued = useCallback(
    (count: number) => {
      if (stateRef.current.queued !== count) dispatch({ type: 'queue', count })
    },
    [dispatch],
  )
  const addNote = useCallback(
    (text: string) => dispatch({ type: 'systemNote', text, at: Date.now() }),
    [dispatch],
  )

  return {
    state,
    loading: status.loading,
    error: status.error,
    send,
    retry,
    abort,
    loadOlder,
    reload,
    setQueued,
    addNote,
  }
}
