import { useQuery } from '@tanstack/react-query'
import { useMemo } from 'react'
import { useRpc } from '@/app/providers'
import { ENV_QUERY_KEY, type EnvListResponse } from '@/views/env/logic'
import { xSearchSignedIn, type AuthStatus } from '@/views/setup/logic'
import type { KeyContext } from './logic'

export const AUTH_STATUS_KEY = ['settings', 'auth', 'status'] as const

export interface KeyFacts {
  /** What the key checks read; `envSet` answers undefined until env.list lands. */
  ctx: KeyContext
  /** env.list has answered (false while loading or when it failed). */
  ready: boolean
  /** env.list failed: whether a variable is set cannot be known. */
  failed: boolean
  /** The gateway's own "get a key" link for a variable. */
  envUrl: (name: string) => string | undefined
}

/**
 * Whether the keys the capabilities read are there: the environment listing
 * and the xAI login. One cache for Capabilities, Overview and the rail dots
 * (same query keys as the Environment section), so all three tell the same
 * story about a missing key.
 */
export function useKeyFacts(connected: boolean): KeyFacts {
  const rpc = useRpc()
  const env = useQuery({
    queryKey: ENV_QUERY_KEY,
    enabled: connected,
    refetchOnWindowFocus: false,
    queryFn: () => rpc.call<EnvListResponse>('env.list', {}),
  })
  const auth = useQuery({
    queryKey: AUTH_STATUS_KEY,
    enabled: connected,
    refetchOnWindowFocus: false,
    retry: false,
    queryFn: () => rpc.call<AuthStatus>('auth.status', {}),
  })
  const rows = env.data?.vars
  const login = auth.data ? xSearchSignedIn(auth.data) : false
  // A missing variable is only news once the login is known too: an xAI
  // login covers X search without one.
  const settled = Boolean(rows) && !auth.isPending
  return useMemo(() => {
    const byName = new Map((rows ?? []).map((r) => [r.name, r]))
    return {
      ctx: {
        // Unknown until both land (or when the listing failed): never judge on a guess.
        envSet: (name: string) => (settled ? Boolean(byName.get(name)?.isSet) : undefined),
        xaiLogin: login,
      },
      ready: settled,
      failed: !rows && env.isError,
      envUrl: (name: string) => byName.get(name)?.url || undefined,
    }
  }, [rows, settled, login, env.isError])
}
