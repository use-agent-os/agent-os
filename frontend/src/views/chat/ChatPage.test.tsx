import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, expect, it, vi } from 'vitest'
import { ChatPage } from './ChatPage'

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), warning: vi.fn(), error: vi.fn(), info: vi.fn() },
}))

function makeImageFile(name: string, size = 100): File {
  const file = new File([new Blob(['img'], { type: 'image/png' })], name, { type: 'image/png' })
  Object.defineProperty(file, 'size', { value: size, configurable: true })
  return file
}

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

  it('sends the composed text via chat.send with the legacy payload (chat.js:6150/6193)', async () => {
    mockRpc = makeRpc()
    renderPage()
    const ta = screen.getByRole('textbox') as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: 'hello world' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))
    await waitFor(() => {
      const sends = mockRpc.call.mock.calls.filter(([m]) => m === 'chat.send')
      expect(sends.length).toBe(1)
      const params = sends[0]![1] as Record<string, unknown>
      expect(params.message).toBe('hello world')
      expect(params.sessionKey).toBe('agent:main:webchat:default')
    })
  })

  it('enables an attachments-only send and threads attachments into chat.send (chat.js:6064/6157)', async () => {
    mockRpc = makeRpc()
    renderPage()
    // Attach an image via the composer file picker (fire change on the hidden input).
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement
    await act(async () => {
      fireEvent.change(fileInput, { target: { files: [makeImageFile('shot.png')] } })
    })
    // The inline FileReader resolves → the send button enables even with empty text.
    const send = await screen.findByRole('button', { name: /send/i })
    await waitFor(() => expect(send).toBeEnabled())
    fireEvent.click(send)
    await waitFor(() => {
      const sends = mockRpc.call.mock.calls.filter(([m]) => m === 'chat.send')
      expect(sends.length).toBe(1)
      const params = sends[0]![1] as Record<string, unknown>
      // Empty-text attachments-only send → the fallback provider prompt.
      expect(params.message).toBe('Describe these attachments')
      const atts = params.attachments as Array<Record<string, unknown>>
      expect(atts).toHaveLength(1)
      expect(atts[0]?.name).toBe('shot.png')
      expect(atts[0]?.mime).toBe('image/png')
    })
  })

  it('aborts the in-flight turn via chat.abort while streaming (chat.js:8444)', async () => {
    mockRpc = makeRpc()
    renderPage()
    // Drive the controller into a streaming state via a live text_delta frame.
    await act(async () => {
      mockRpc.emit('session.event.text_delta', { seq: 1, text: 'hi' }, {})
    })
    const abort = await screen.findByRole('button', { name: /abort|stop/i })
    fireEvent.click(abort)
    await waitFor(() => {
      const aborts = mockRpc.call.mock.calls.filter(([m]) => m === 'chat.abort')
      expect(aborts.length).toBe(1)
      const params = aborts[0]![1] as Record<string, unknown>
      expect(params.sessionKey).toBe('agent:main:webchat:default')
    })
  })
})
