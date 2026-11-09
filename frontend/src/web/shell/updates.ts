import { useQuery } from '@tanstack/react-query'
import { useCallback, useState } from 'react'
import { useRpc } from '@/app/providers'
import { useConnection } from '@/stores/connection'

export interface UpdateCheck {
  current: string
  latest: string | null
  status: 'up-to-date' | 'outdated' | 'offline'
}

/** Shared with the previous console, so a dismissal carries over. */
export const DISMISSED_VERSION_KEY = 'agentos.dismissedVersion'
export const UPDATE_CHECK_KEY = ['updates', 'check'] as const

export function useUpdateCheck() {
  const rpc = useRpc()
  const connected = useConnection((s) => s.state === 'connected')
  const query = useQuery({
    queryKey: UPDATE_CHECK_KEY,
    queryFn: () => rpc.call<UpdateCheck>('updates.check'),
    enabled: connected,
    staleTime: 60 * 60_000,
    retry: false,
  })
  const check = query.data ?? null
  const available = check?.status === 'outdated' && !!check.latest
  return { check, available }
}

function readDismissed(): string | null {
  try {
    return localStorage.getItem(DISMISSED_VERSION_KEY)
  } catch {
    return null
  }
}

/** Per-version dismissal of the update notice (the menu badge stays). */
export function useDismissedVersion(): [string | null, (version: string) => void] {
  const [dismissed, setDismissed] = useState(readDismissed)
  const dismiss = useCallback((version: string) => {
    try {
      localStorage.setItem(DISMISSED_VERSION_KEY, version)
    } catch {
      /* storage unavailable: dismissed for this page only */
    }
    setDismissed(version)
  }, [])
  return [dismissed, dismiss]
}
