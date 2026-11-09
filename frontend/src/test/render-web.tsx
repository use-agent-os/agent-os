// Render web console trees with the kept providers' contexts, a fresh query
// client and a memory router.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render } from '@testing-library/react'
import type { ReactNode } from 'react'
import { createMemoryRouter, RouterProvider, type RouteObject } from 'react-router'
import { BootstrapContext, RpcContext } from '@/app/providers'
import { KeyboardShortcutProvider } from '@/components/KeyboardShortcuts'
import type { Bootstrap } from '@/lib/bootstrap'
import { TooltipProvider } from '@/web/components/ui/tooltip'
import { ShortcutOverviewHost } from '@/web/features/palette/overview-host'
import { FakeRpc } from './fake-rpc'

export const TEST_BOOTSTRAP: Bootstrap = {
  version: '2026.11.7',
  ws_url: 'ws://127.0.0.1:18791/ws',
  auth_mode: 'none',
  base_path: '/control',
  features: { diagnostics: false },
}

export interface WebRenderOptions {
  rpc?: FakeRpc
  bootstrap?: Partial<Bootstrap>
  initialEntries?: string[]
  queryClient?: QueryClient
}

export function testQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0 }, mutations: { retry: false } },
  })
}

export function WebTestProviders({
  rpc,
  bootstrap,
  queryClient,
  children,
}: {
  rpc: FakeRpc
  bootstrap?: Partial<Bootstrap>
  queryClient: QueryClient
  children: ReactNode
}) {
  return (
    <BootstrapContext.Provider value={{ ...TEST_BOOTSTRAP, ...bootstrap }}>
      <RpcContext.Provider value={rpc.asClient()}>
        <QueryClientProvider client={queryClient}>
          <KeyboardShortcutProvider renderHelp={ShortcutOverviewHost}>
            <TooltipProvider delayDuration={0}>{children}</TooltipProvider>
          </KeyboardShortcutProvider>
        </QueryClientProvider>
      </RpcContext.Provider>
    </BootstrapContext.Provider>
  )
}

export function renderRoutes(routes: RouteObject[], options: WebRenderOptions = {}) {
  const rpc = options.rpc ?? new FakeRpc()
  const queryClient = options.queryClient ?? testQueryClient()
  const router = createMemoryRouter(routes, { initialEntries: options.initialEntries ?? ['/'] })
  const result = render(
    <WebTestProviders rpc={rpc} bootstrap={options.bootstrap} queryClient={queryClient}>
      <RouterProvider router={router} />
    </WebTestProviders>,
  )
  return { ...result, rpc, router, queryClient }
}

export function renderWeb(ui: ReactNode, options: WebRenderOptions = {}) {
  return renderRoutes([{ path: '*', element: ui }], options)
}
