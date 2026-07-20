import { render } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, expect, it, vi } from 'vitest'
import { ChatPage } from './ChatPage'

// Same provider-wrapping pattern SkillsPage.test.tsx uses: there is no shared
// `@/test/utils` wrapper in this repo, so the RPC provider is stubbed via a
// module mock and the tree is wrapped in MemoryRouter + QueryClientProvider.
type Handler = (...args: unknown[]) => void
function makeRpc() {
  const listeners = new Map<string, Set<Handler>>()
  return {
    waitForConnection: vi.fn().mockResolvedValue(undefined),
    call: vi.fn().mockResolvedValue({}),
    on: vi.fn((event: string, handler: Handler) => {
      if (!listeners.has(event)) listeners.set(event, new Set())
      listeners.get(event)!.add(handler)
      return () => listeners.get(event)?.delete(handler)
    }),
    emit(event: string, ...args: unknown[]) {
      listeners.get(event)?.forEach((h) => h(...args))
    },
  }
}
let mockRpc = makeRpc()

vi.mock('@/app/providers', () => ({
  useRpc: () => mockRpc,
  useBootstrap: () => ({
    version: '1',
    ws_url: 'ws://127.0.0.1:18791/ws',
    auth_mode: 'none',
    base_path: '/control',
    config_path: '/tmp/agentos.toml',
    features: {},
  }),
}))

function renderPage() {
  return render(
    <MemoryRouter>
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <ChatPage />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

describe('ChatPage', () => {
  it('renders the full-bleed chat shell with a thread region', () => {
    mockRpc = makeRpc()
    renderPage()
    expect(document.querySelector('.chat-thread')).not.toBeNull()
    expect(document.title).toBe('Chat - AgentOS Control')
  })

  it('mounts the thread region above a composer row', () => {
    mockRpc = makeRpc()
    renderPage()
    expect(document.querySelector('.chat-stage')).not.toBeNull()
    expect(document.querySelector('.chat-composer')).not.toBeNull()
  })
})
