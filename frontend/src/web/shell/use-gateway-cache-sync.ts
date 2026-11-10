import { useQueryClient } from '@tanstack/react-query'
import { useEffect } from 'react'
import { useRpc } from '@/app/providers'

// The URL each client last completed a handshake with, kept per client so a
// remounted shell still knows whether the gateway changed.
const lastHelloUrl = new WeakMap<object, string>()

/**
 * Keep cached data honest across connections. Every handshake refreshes what
 * is cached (a crash and restart, a dropped network, new credentials); a
 * handshake with a different gateway URL resets the cache first, so nothing
 * the previous gateway said is shown as this one's.
 */
export function useGatewayCacheSync(): void {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  useEffect(
    () =>
      rpc.on('_hello', () => {
        const url = (rpc as { currentUrl?: string }).currentUrl ?? ''
        const previous = lastHelloUrl.get(rpc)
        lastHelloUrl.set(rpc, url)
        if (previous !== undefined && previous !== url) void queryClient.resetQueries()
        else void queryClient.invalidateQueries()
      }),
    [rpc, queryClient],
  )
}
