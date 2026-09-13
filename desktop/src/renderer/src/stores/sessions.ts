import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo } from 'react'
import { useRpc } from '@/app/providers'
import { sessionName, sessionRunStatus, type RawSession } from '@/views/sessions/logic'
import { useConnection } from '@/stores/connection'
import { t } from '~/i18n'
import { shownSessionName } from '~/lib/session-name'

interface SessionsList {
  sessions?: RawSession[]
  count?: number
}

/** One sidebar row, derived from a gateway session record. */
export interface SessionRow {
  key: string
  title: string
  /** Last activity, epoch ms (0 when unknown). */
  updatedAt: number
  /** A turn is queued or running in this session right now. */
  live: boolean
  raw: RawSession
}

/** Events after which the list is stale: rows appear, rename, or change run state. */
const INVALIDATING_EVENTS = [
  'sessions.changed',
  'task.queued',
  'task.running',
  'task.succeeded',
  'task.failed',
  'task.timeout',
  'task.abandoned',
  'task.cancelled',
  '_hello',
]

function toEpochMs(value: unknown): number {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value < 1e12 ? value * 1000 : value
  }
  if (typeof value === 'string' && value.trim()) {
    const numeric = Number(value)
    if (Number.isFinite(numeric)) return numeric < 1e12 ? numeric * 1000 : numeric
    const parsed = Date.parse(value)
    return Number.isFinite(parsed) ? parsed : 0
  }
  return 0
}

export function toSessionRow(raw: RawSession): SessionRow {
  const key = String(raw.key || '')
  const status = sessionRunStatus(raw)
  return {
    key,
    title:
      shownSessionName(sessionName(raw)) ||
      String(raw.derived_title || raw.derivedTitle || raw.subject || '') ||
      t('chat.untitled'),
    updatedAt: toEpochMs(raw.updated_at ?? raw.updatedAt),
    live: status === 'running' || status === 'queued',
    raw,
  }
}

/**
 * The gateway's session list as sidebar rows. Refetched on every session or
 * task event and on reconnect, plus a slow poll as a backstop for events the
 * gateway does not broadcast (e.g. a session renamed by another client).
 */
export function useSessions(): {
  rows: SessionRow[]
  loading: boolean
  error: string | null
} {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const connected = useConnection((s) => s.state === 'connected')

  const query = useQuery<SessionsList>({
    queryKey: ['sessions'],
    enabled: connected,
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<SessionsList>('sessions.list', { limit: 200 })
    },
    refetchInterval: 30_000,
    refetchOnWindowFocus: true,
  })

  // `sessions.changed` for sessions other than the one on screen only reaches
  // connections that asked for the list topic. Ask on every (re)connect.
  useEffect(() => {
    if (!connected) return
    let cancelled = false
    rpc
      .waitForConnection()
      .then(() => (cancelled ? undefined : rpc.call('sessions.subscribe', {})))
      .catch(() => undefined)
    return () => {
      cancelled = true
    }
  }, [rpc, connected])

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null
    const invalidate = () => {
      // Task events arrive in bursts; one refetch per burst is plenty.
      if (timer) return
      timer = setTimeout(() => {
        timer = null
        void queryClient.invalidateQueries({ queryKey: ['sessions'] })
      }, 150)
    }
    const offs = INVALIDATING_EVENTS.map((event) => rpc.on(event, invalidate))
    return () => {
      offs.forEach((off) => off())
      if (timer) clearTimeout(timer)
    }
  }, [rpc, queryClient])

  const rows = useMemo(
    () =>
      (query.data?.sessions ?? [])
        .map(toSessionRow)
        .filter((row) => row.key)
        .sort((a, b) => b.updatedAt - a.updatedAt),
    [query.data],
  )

  return {
    rows,
    loading: connected && query.isPending,
    error: query.error
      ? query.error instanceof Error
        ? query.error.message
        : String(query.error)
      : null,
  }
}
