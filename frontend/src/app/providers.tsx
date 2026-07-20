import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import { fallbackBootstrap, fetchBootstrap, resolveWsUrl, type Bootstrap } from '@/lib/bootstrap'
import { WsRpcClient } from '@/lib/ws-rpc'
import { useConnection } from '@/stores/connection'
import { initTheme } from '@/stores/theme'
import type { RpcState } from '@/lib/ws-rpc'

const WS_URL_KEY = 'agentos.wsUrl'
const WS_TOKEN_KEY = 'agentos.wsToken'

const RpcContext = createContext<WsRpcClient | null>(null)
const BootstrapContext = createContext<Bootstrap | null>(null)

export function useRpc(): WsRpcClient {
  const rpc = useContext(RpcContext)
  if (!rpc) throw new Error('useRpc outside AppProviders')
  return rpc
}

export function useBootstrap(): Bootstrap {
  const b = useContext(BootstrapContext)
  if (!b) throw new Error('useBootstrap outside AppProviders')
  return b
}

const queryClient = new QueryClient({
  defaultOptions: { queries: { staleTime: 5_000, retry: 1 } },
})

export function AppProviders({ children }: { children: ReactNode }) {
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null)
  const [rpc] = useState(() => new WsRpcClient())

  useEffect(() => {
    initTheme()
    let cancelled = false
    const unsubscribe = rpc.on('_state', (s) => useConnection.getState().setState(s as RpcState))
    fetchBootstrap()
      // Legacy served bootstrap inline (it could not fail): the shell always
      // rendered and _autoConnect used the location-derived default WS URL with
      // infinite WS reconnect backoff (app.js:186-203, rpc.js:226-231). A
      // transient /api/bootstrap failure must never wedge the app on the
      // "Connecting…" placeholder.
      .catch(() => fallbackBootstrap())
      .then((b) => {
        if (cancelled) return
        setBootstrap(b)
        // app.js:197-203 — URL override from localStorage; the auth token
        // lives in sessionStorage (per-tab session tier, NOT localStorage).
        // Both reads tolerate storage access errors like legacy.
        let storedUrl = ''
        let token = ''
        try {
          storedUrl = localStorage.getItem(WS_URL_KEY) || ''
        } catch {
          /* storage unavailable */
        }
        try {
          token = sessionStorage.getItem(WS_TOKEN_KEY) || ''
        } catch {
          /* storage unavailable */
        }
        // Stored override wins verbatim (legacy loadConnectionSettings returned
        // it unchanged). Otherwise use the server-computed ws_url, but restore
        // the legacy location-derived scheme when a proxy drops x-forwarded-proto
        // and downgrades a same-host https page to ws:// (resolveWsUrl).
        rpc.connect(storedUrl || resolveWsUrl(b.ws_url), token || undefined)
      })
    return () => {
      cancelled = true
      unsubscribe()
      rpc.disconnect()
    }
  }, [rpc])

  if (!bootstrap) return <div className="p-8 text-sm">Connecting…</div>

  return (
    <BootstrapContext.Provider value={bootstrap}>
      <RpcContext.Provider value={rpc}>
        <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
      </RpcContext.Provider>
    </BootstrapContext.Provider>
  )
}
