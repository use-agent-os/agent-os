import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useEffect, useState, type ReactNode } from 'react'
import { BootstrapContext, RpcContext } from '@/app/providers'
import { KeyboardShortcutProvider } from '@/components/KeyboardShortcuts'
import { Toaster } from '@/components/ui/sonner'
import { initLocale } from '@/i18n'
import { fallbackBootstrap, fetchBootstrap, resolveWsUrl, type Bootstrap } from '@/lib/bootstrap'
import { WsRpcClient, type RpcState } from '@/lib/ws-rpc'
import { approvalMonitor } from '@/services/approval-monitor'
import { useConnection } from '@/stores/connection'
import { useTheme as useWebTheme } from '@/stores/theme'
import { ApprovalDialog } from '~/components/approval/ApprovalDialog'
import { desktopApi } from '~/lib/desktop-api'
import { useGateway } from '~/stores/gateway'
import { useSettings } from '~/stores/settings'
import { useTheme } from '~/theme/theme-store'

/** Same key the web console's http-auth reads for REST bearer tokens. */
const WS_TOKEN_KEY = 'agentos.wsToken'
/** Gateway default; the desktop never serves the console itself. */
const CONTROL_BASE = '/control'

const queryClient = new QueryClient({
  defaultOptions: { queries: { staleTime: 5_000, retry: 1 } },
})

function emptyBootstrap(): Bootstrap {
  return {
    version: '',
    ws_url: '',
    auth_mode: '',
    base_path: CONTROL_BASE,
    features: { diagnostics: false },
  }
}

/**
 * The desktop counterpart of the web console's AppProviders. Same client,
 * same contexts, same hooks downstream; the difference is where the gateway
 * comes from. The console is served BY the gateway, so it connects to its own
 * origin. Here the main process owns the gateway and reports its URL through
 * the gateway store; every time that URL becomes reachable we point the
 * shared URL helpers at it (`window.__AGENTOS_ENV__`), fetch bootstrap and
 * connect. Losing the gateway disconnects; a new URL reconnects.
 */
export function GatewayProviders({ children }: { children: ReactNode }) {
  const [rpc] = useState(() => new WsRpcClient())
  const [bootstrap, setBootstrap] = useState<Bootstrap>(emptyBootstrap)
  const gatewayState = useGateway((s) => s.status.state)
  const gatewayUrl = useGateway((s) => s.status.url)
  const token = useSettings((s) => s.settings.gateway.token)
  const loadSettings = useSettings((s) => s.load)
  const resolvedTheme = useTheme((s) => s.resolved)

  useEffect(() => {
    void loadSettings()
  }, [loadSettings])

  // One-time wiring shared with the console: locale + connection-state mirror.
  useEffect(() => {
    initLocale()
    return rpc.on('_state', (s) => useConnection.getState().setState(s as RpcState))
  }, [rpc])

  // The transcript's chart theme reads the console's theme store; keep it in
  // step with the desktop's resolved appearance without running its initTheme
  // (which would fight the desktop's own <html data-theme> writer).
  useEffect(() => {
    useWebTheme.setState({ mode: resolvedTheme })
  }, [resolvedTheme])

  useEffect(() => {
    if (gatewayState !== 'running' || !gatewayUrl) {
      rpc.disconnect()
      return
    }
    window.__AGENTOS_ENV__ = { apiOrigin: gatewayUrl, controlBase: CONTROL_BASE }
    try {
      if (token) sessionStorage.setItem(WS_TOKEN_KEY, token)
      else sessionStorage.removeItem(WS_TOKEN_KEY)
    } catch {
      /* storage unavailable */
    }

    let cancelled = false
    // The operator secret is what tells the gateway this connection is the
    // user's app, not an agent's shell; without it (adopted or external
    // gateway) the gateway falls back to its own rules.
    // A renderer can be newer than the preload it runs under (dev reload,
    // mid-update): an older bridge has no operatorSecret, and that is a
    // connection without proof, not a crash.
    const bridge = desktopApi().gateway as { operatorSecret?: () => Promise<string | null> }
    const secretPromise =
      typeof bridge.operatorSecret === 'function'
        ? bridge.operatorSecret().catch(() => null)
        : Promise.resolve<string | null>(null)
    Promise.all([fetchBootstrap().catch(() => fallbackBootstrap()), secretPromise]).then(
      ([b, secret]) => {
        if (cancelled) return
        setBootstrap(b)
        rpc.connect(
          resolveWsUrl(b.ws_url),
          token || undefined,
          secret ? { operatorSecret: secret } : null,
        )
      },
    )
    // Approvals are a REST poller against the same origin; only worth running
    // while there is a gateway to ask.
    approvalMonitor.start()
    return () => {
      cancelled = true
      approvalMonitor.stop()
      rpc.disconnect()
    }
  }, [rpc, gatewayState, gatewayUrl, token])

  return (
    <BootstrapContext.Provider value={bootstrap}>
      <RpcContext.Provider value={rpc}>
        <QueryClientProvider client={queryClient}>
          <KeyboardShortcutProvider>
            {children}
            <ApprovalDialog />
            <Toaster />
          </KeyboardShortcutProvider>
        </QueryClientProvider>
      </RpcContext.Provider>
    </BootstrapContext.Provider>
  )
}
