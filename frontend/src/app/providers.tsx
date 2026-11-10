import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import { fallbackBootstrap, fetchBootstrap, resolveWsUrl, type Bootstrap } from '@/lib/bootstrap'
import { WsRpcClient } from '@/lib/ws-rpc'
import { useConnection } from '@/stores/connection'
import { initTheme } from '@/stores/theme'
import { initLocale, t } from '@/i18n'
import { approvalMonitor } from '@/services/approval-monitor'
import { bindPendingQuestions } from '@/lib/pending-questions'
import { bindRuntimeSignals } from '@/lib/runtime-signals'
import type { RpcState } from '@/lib/ws-rpc'
import { KeyboardShortcutProvider, type ShortcutHelpRenderer } from '@/components/KeyboardShortcuts'
import { readStoredToken, readStoredWsUrl } from '@/lib/credentials'
import { bindGatewaySocket } from '@/lib/api-origin'

// Exported so an off-gateway host (the desktop shell) can supply its own
// connected client to the same hooks and views.
export const RpcContext = createContext<WsRpcClient | null>(null)
export const BootstrapContext = createContext<Bootstrap | null>(null)

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

export function AppProviders({
  children,
  shortcutHelp,
}: {
  children: ReactNode
  /** Replaces the built-in shortcut sheet (the rebuilt web console's own dialog). */
  shortcutHelp?: ShortcutHelpRenderer
}) {
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null)
  const [rpc] = useState(() => new WsRpcClient())

  useEffect(() => {
    initTheme()
    // Constant today. When the gateway starts advertising a locale this becomes
    // setLocale(b.locale) inside the bootstrap.then below, before setBootstrap.
    initLocale()
    let cancelled = false
    const unsubscribe = rpc.on('_state', (s) => useConnection.getState().setState(s as RpcState))
    // REST calls (approvals, uploads, artifacts) follow the gateway this
    // socket is connected to, which a switch can make another origin.
    const unbindHttp = bindGatewaySocket(rpc)
    // The truth badge and the attention inbox read what this connection sees:
    // frame age, refused handshakes, doctor/ready, presented ask_user questions.
    const unbindSignals = bindRuntimeSignals(rpc)
    const unbindQuestions = bindPendingQuestions(rpc)
    fetchBootstrap()
      // Legacy served bootstrap inline (it could not fail): the shell always
      // rendered and _autoConnect used the location-derived default WS URL with
      // infinite WS reconnect backoff. A
      // transient /api/bootstrap failure must never wedge the app on the
      // "Connecting…" placeholder.
      .catch(() => fallbackBootstrap())
      .then((b) => {
        if (cancelled) return
        setBootstrap(b)
        // URL override from localStorage; the auth token
        // lives in sessionStorage (per-tab session tier, NOT localStorage).
        // Both reads tolerate storage access errors like legacy.
        const storedUrl = readStoredWsUrl()
        const token = readStoredToken()
        // Stored override wins verbatim (legacy loadConnectionSettings returned
        // it unchanged). Otherwise use the server-computed ws_url, but restore
        // the legacy location-derived scheme when a proxy drops x-forwarded-proto
        // and downgrades a same-host https page to ws:// (resolveWsUrl).
        rpc.connect(storedUrl || resolveWsUrl(b.ws_url), token || undefined)
      })
    return () => {
      cancelled = true
      unsubscribe()
      unbindHttp()
      unbindSignals()
      unbindQuestions()
      rpc.disconnect()
    }
  }, [rpc])

  // approval_monitor.js — the global approval monitor follows the gateway's
  // pushed approval.requested / approval.resolved events on this client and
  // backfills over REST each time it (re)connects. Start it once on mount and
  // tear it down on unmount so it never outlives the app tree.
  useEffect(() => {
    approvalMonitor.start(rpc)
    return () => approvalMonitor.stop()
  }, [rpc])

  if (!bootstrap) return <div className="p-8 text-sm">{t('shell.connecting')}</div>

  return (
    <BootstrapContext.Provider value={bootstrap}>
      <RpcContext.Provider value={rpc}>
        <QueryClientProvider client={queryClient}>
          <KeyboardShortcutProvider renderHelp={shortcutHelp}>{children}</KeyboardShortcutProvider>
        </QueryClientProvider>
      </RpcContext.Provider>
    </BootstrapContext.Provider>
  )
}
