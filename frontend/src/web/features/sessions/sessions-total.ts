import { useQuery } from '@tanstack/react-query'
import { useRpc } from '@/app/providers'
import { SESSIONS_LIST_LIMIT } from '@/views/sessions/logic'
import { useDocumentVisible } from '@/web/shell/session-data'

// The shared session list holds the newest SESSIONS_LIST_LIMIT sessions only.
// Anything that counts or filters it must say so once the list is full, and
// name the real total when the gateway reports one (`sessions.list` `total`).

export const SESSIONS_TOTAL_QUERY_KEY = ['sessions', 'total'] as const
const TOTAL_REFRESH_MS = 30_000

export interface SessionsTotal {
  /** The loaded list is the newest SESSIONS_LIST_LIMIT, not every session. */
  truncated: boolean
  /** Every stored session; null when the list is full and the gateway did not say. */
  total: number | null
  /** How many rows the list holds. */
  listed: number
}

/** Read `sessions.list`'s `total`; null from a gateway that does not send one. */
export function readSessionsTotal(data: unknown): number | null {
  const raw = (data as { total?: unknown } | null)?.total
  return typeof raw === 'number' && Number.isFinite(raw) && raw >= 0 ? raw : null
}

/** Combine the list size with a reported total (null when unknown). */
export function sessionsTotal(listed: number, reported: number | null): SessionsTotal {
  if (listed < SESSIONS_LIST_LIMIT) return { truncated: false, total: listed, listed }
  if (reported !== null && reported <= listed) return { truncated: false, total: listed, listed }
  return { truncated: true, total: reported, listed }
}

/**
 * The total behind the shared session list. Below the limit the list is
 * complete and nothing is fetched; a full list asks the gateway for the count
 * with a one-row `sessions.list`.
 */
export function useSessionsTotal(listed: number): SessionsTotal {
  const rpc = useRpc()
  const visible = useDocumentVisible()
  const full = listed >= SESSIONS_LIST_LIMIT
  const query = useQuery({
    queryKey: SESSIONS_TOTAL_QUERY_KEY,
    enabled: full,
    queryFn: async () => {
      await rpc.waitForConnection()
      return readSessionsTotal(await rpc.call('sessions.list', { limit: 1 }))
    },
    refetchInterval: full && visible ? TOTAL_REFRESH_MS : false,
  })
  return sessionsTotal(listed, full ? (query.data ?? null) : null)
}
