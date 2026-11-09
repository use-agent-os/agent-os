import { useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useReducer, useRef } from 'react'
import { useRpc } from '@/app/providers'
import { newIdempotencyKey } from '@/lib/ws-rpc'
import { useApprovals } from '@/services/approval-monitor'
import { CHAT_HISTORY_PAGE_SIZE } from '@/views/chat/transcript/history'
import { createSeqGate } from '@/views/chat/transcript/stream'
import { SESSIONS_QUERY_KEY } from '@/web/shell/session-data'
import { initialTranscript, reduce } from './model/reducer'
import type { HistoryPage, TranscriptState, Turn } from './model/types'
import { sendOnce } from './send'

// The chat subscription (overhaul-control-ui D7, task 6.5): chat.history
// pages and sessions.messages.subscribe frames feed the transcript reducer.
// Frames for another session, from an older epoch or already seen (the kept
// createSeqGate) are dropped; a gap, an epoch change or a reconnect reloads
// the history window, which the reducer reconciles by entry id.

interface SubscribeResponse {
  current_stream_seq?: number
  replay_complete?: boolean
  replay_gap_reason?: string | null
}

const RESYNC_GAP_REASONS = new Set(['stream_buffer_reset', 'cursor_ahead_of_stream'])

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
  abort(): Promise<void>
  loadOlder(): Promise<void>
  reload(): Promise<void>
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
  const [state, dispatch] = useReducer(reduce, undefined, initialTranscript)
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

  useEffect(() => {
    stateRef.current = state
  }, [state])

  const loadHistory = useCallback(
    async (key: string, mode: 'replace' | 'older' | 'delta') => {
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
        const page = await rpc.call<HistoryPage>('chat.history', params)
        if (keyRef.current !== key || (mode !== 'older' && id !== loadSeq.current)) return
        dispatch({ type: 'historyPage', page: page ?? {}, mode })
        if (mode === 'replace') setStatus({ loading: false, error: null })
      } catch (e) {
        if (keyRef.current !== key) return
        if (mode === 'replace') setStatus({ loading: false, error: errorMessage(e) })
        else throw e
      }
    },
    [rpc],
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

    const subscribe = async () => {
      try {
        await rpc.waitForConnection()
        const since = gate.current.highWater(key)
        const res = await rpc.call<SubscribeResponse>('sessions.messages.subscribe', {
          key,
          since_stream_seq: since,
        })
        if (!active) return
        const current = Number(res?.current_stream_seq)
        if (
          (Number.isFinite(current) && current < since) ||
          RESYNC_GAP_REASONS.has(String(res?.replay_gap_reason ?? ''))
        ) {
          gate.current.reset(key)
        }
        if (res?.replay_complete === false) void loadHistory(key, 'replace')
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
      dispatch({ type: 'event', name, payload, at: Date.now() })
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
      gate.current.reset(key)
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
  }, [sessionKey, rpc, loadHistory, queryClient])

  // The reducer asked for a fresh window (epoch change, unusable delta).
  useEffect(() => {
    if (state.needsResync && sessionKey) void loadHistory(sessionKey, 'replace')
  }, [state.needsResync, sessionKey, loadHistory])

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
        dispatch({ type: 'sendAccepted', idempotencyKey })
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
    [rpc],
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
    dispatch({ type: 'abortRequested' })
    await rpc.call('chat.abort', { sessionKey: key, source: 'web' })
  }, [rpc])

  const loadOlder = useCallback(async () => {
    const key = keyRef.current
    if (!key || !stateRef.current.hasMore || !stateRef.current.oldestCursor) return
    await loadHistory(key, 'older')
  }, [loadHistory])

  const reload = useCallback(async () => {
    if (keyRef.current) await loadHistory(keyRef.current, 'replace')
  }, [loadHistory])

  return {
    state,
    loading: status.loading,
    error: status.error,
    send,
    retry,
    abort,
    loadOlder,
    reload,
  }
}
