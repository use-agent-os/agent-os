import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'sonner'
import { OverviewPage } from './OverviewPage'

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    warning: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
  },
}))

const navigateSpy = vi.fn()
vi.mock('react-router', async () => {
  const actual = await vi.importActual<typeof import('react-router')>('react-router')
  return { ...actual, useNavigate: () => navigateSpy }
})

// A minimal event-bus stub matching the WsRpcClient surface OverviewPage uses.
type Handler = (...args: unknown[]) => void
function makeRpc() {
  const listeners = new Map<string, Set<Handler>>()
  return {
    waitForConnection: vi.fn().mockResolvedValue(undefined),
    call: vi.fn(),
    on: vi.fn((event: string, handler: Handler) => {
      if (!listeners.has(event)) listeners.set(event, new Set())
      listeners.get(event)!.add(handler)
      return () => listeners.get(event)?.delete(handler)
    }),
    connect: vi.fn(),
    disconnect: vi.fn(),
    // test helper: fan an event out exactly like WsRpcClient.emit('*', name, payload)
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

const STATUS = { uptime_ms: 3723000, version: '2026.7.19', provider: 'openai' }
const DOCTOR = { status: 'degraded', summary: 'Two capabilities need attention' }
const USAGE = { totalSessions: 12, totalTokens: 1234567, totalCostUsd: 4.2 }
const SESSIONS = {
  sessions: [
    {
      key: 'sess-b',
      status: 'running',
      model: 'gpt-4',
      message_count: 8,
      updated_at: '2026-01-03T00:00:00Z',
    },
    {
      key: 'sess-a',
      status: 'done',
      message_count: 3,
      updated_at: '2026-01-01T00:00:00Z',
    },
  ],
}

// Route the four overview RPC methods off a config object.
function wireRpc(
  opts: {
    status?: unknown
    statusReject?: boolean
    doctor?: unknown
    doctorReject?: boolean
    usage?: unknown
    sessions?: unknown
    sessionsReject?: boolean
  } = {},
) {
  mockRpc.call.mockImplementation((method: string) => {
    switch (method) {
      case 'status':
        return opts.statusReject
          ? Promise.reject(new Error('boom'))
          : Promise.resolve(opts.status ?? STATUS)
      case 'doctor.status':
        return opts.doctorReject
          ? Promise.reject(new Error('down'))
          : Promise.resolve(opts.doctor ?? DOCTOR)
      case 'usage.status':
        return Promise.resolve(opts.usage ?? USAGE)
      case 'sessions.list':
        return opts.sessionsReject
          ? Promise.reject(new Error('down'))
          : Promise.resolve(opts.sessions ?? SESSIONS)
      default:
        return Promise.resolve({})
    }
  })
}

function renderPage() {
  return render(
    <MemoryRouter>
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <OverviewPage />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

describe('OverviewPage', () => {
  beforeEach(() => {
    mockRpc = makeRpc()
    navigateSpy.mockReset()
    vi.mocked(toast.error).mockClear()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('calls status, doctor.status, usage.status and sessions.list after waitForConnection', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(mockRpc.call).toHaveBeenCalledWith('status', {}))
    expect(mockRpc.call).toHaveBeenCalledWith('doctor.status', { agentId: 'main', deep: false })
    expect(mockRpc.call).toHaveBeenCalledWith('usage.status', {})
    expect(mockRpc.call).toHaveBeenCalledWith('sessions.list', { limit: 5 })
    expect(mockRpc.waitForConnection).toHaveBeenCalled()
  })

  it('renders the stat tiles from the four payloads (uptime, tokens, sessions, provider, health)', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByText('1h 2m 3s')).toBeInTheDocument())
    expect(screen.getByText((1234567).toLocaleString())).toBeInTheDocument()
    expect(screen.getByText('$4.2000 spent')).toBeInTheDocument()
    expect(screen.getByText('12')).toBeInTheDocument()
    expect(screen.getByText('openai')).toBeInTheDocument()
    expect(screen.getByText('v2026.7.19')).toBeInTheDocument()
    // doctor.status → readiness label + summary
    expect(screen.getByText('Degraded')).toBeInTheDocument()
    expect(screen.getByText('Two capabilities need attention')).toBeInTheDocument()
  })

  it('presents health as the primary signal inside one gateway summary', async () => {
    wireRpc()
    renderPage()
    const summary = await screen.findByRole('region', { name: 'Gateway summary' })
    expect(within(summary).getByText('System pulse')).toBeInTheDocument()
    expect(within(summary).getByText('Refreshes every 30s')).toBeInTheDocument()
    expect(within(summary).getByRole('button', { name: /^health$/i })).toHaveClass('ov-stat--hero')
  })

  // overview.js:262 — the Total sessions tile printed the raw integer, unlike
  // the token tile which localizes. A value with a thousands boundary proves
  // no toLocaleString grouping is applied.
  it('renders the total-sessions count raw (no thousands grouping)', async () => {
    wireRpc({ usage: { totalSessions: 12345, totalTokens: 1234567, totalCostUsd: 4.2 } })
    renderPage()
    const tile = await screen.findByRole('button', { name: /total sessions/i })
    await waitFor(() => expect(within(tile).getByText('12345')).toBeInTheDocument())
    expect(within(tile).queryByText((12345).toLocaleString())).not.toBeInTheDocument()
  })

  it('renders recent sessions sorted newest-first with status dot, model and relative time', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByText('sess-b')).toBeInTheDocument())
    const rows = screen.getAllByRole('button', { name: /open session/i })
    // sess-b (newer) is sorted before sess-a.
    expect(within(rows[0]!).getByText('sess-b')).toBeInTheDocument()
    expect(within(rows[1]!).getByText('sess-a')).toBeInTheDocument()
    expect(screen.getByText('gpt-4')).toBeInTheDocument()
  })

  it('shows the empty state when sessions.list returns none', async () => {
    wireRpc({ sessions: { sessions: [] } })
    renderPage()
    await waitFor(() => expect(screen.getByText(/No sessions yet/i)).toBeInTheDocument())
  })

  // overview.js:272-310 — a failed sessions.list left the skeleton in place;
  // only a *successful* empty response showed the "No sessions yet" CTA.
  it('keeps a neutral state (not the empty CTA) when sessions.list rejects', async () => {
    wireRpc({ sessionsReject: true })
    renderPage()
    await waitFor(() =>
      expect(screen.getByText(/Recent sessions unavailable/i)).toBeInTheDocument(),
    )
    // The "No sessions yet" empty CTA must NOT render on a failed read.
    expect(screen.queryByText(/No sessions yet/i)).not.toBeInTheDocument()
  })

  // overview.js:245-248 — status is the one read that toasts on failure.
  it('toasts once when the status read rejects', async () => {
    wireRpc({ statusReject: true })
    renderPage()
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        expect.stringContaining('Failed to load status:'),
        expect.objectContaining({ id: 'overview-status-err' }),
      ),
    )
    // A stable toast id keeps it to a single visible notification.
    expect(toast.error).toHaveBeenCalledTimes(1)
  })

  // The sibling reads stay silent on failure — only status surfaces a toast.
  it('does not toast when sessions.list rejects', async () => {
    wireRpc({ sessionsReject: true })
    renderPage()
    await waitFor(() =>
      expect(screen.getByText(/Recent sessions unavailable/i)).toBeInTheDocument(),
    )
    expect(toast.error).not.toHaveBeenCalled()
  })

  it('shows the health tile as unavailable when doctor.status rejects', async () => {
    wireRpc({ doctorReject: true })
    renderPage()
    await waitFor(() => expect(screen.getByText(/unavailable/i)).toBeInTheDocument())
    expect(screen.getByText(/open health/i)).toBeInTheDocument()
  })

  it('navigates from stat tiles and the recent row', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByText('sess-b')).toBeInTheDocument())

    fireEvent.click(screen.getByRole('button', { name: /total sessions/i }))
    expect(navigateSpy).toHaveBeenCalledWith('/sessions')

    fireEvent.click(screen.getByRole('button', { name: /provider/i }))
    expect(navigateSpy).toHaveBeenCalledWith('/agents')

    fireEvent.click(screen.getByRole('button', { name: /^open chat$/i }))
    expect(navigateSpy).toHaveBeenCalledWith('/chat')

    // Recent row → /chat?session=<key>
    fireEvent.click(screen.getAllByRole('button', { name: /open session/i })[0]!)
    expect(navigateSpy).toHaveBeenCalledWith('/chat?session=sess-b')
  })

  it('appends wildcard events to the live event stream (newest first, capped counter)', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByText(/Listening for events/i)).toBeInTheDocument())
    mockRpc.emit('*', 'session.started', { key: 'x' })
    await waitFor(() => expect(screen.getByText('session.started')).toBeInTheDocument())
    expect(screen.getByText('1 event')).toBeInTheDocument()
    mockRpc.emit('*', 'chat.delta', { n: 2 })
    await waitFor(() => expect(screen.getByText('chat.delta')).toBeInTheDocument())
    expect(screen.getByText('2 events')).toBeInTheDocument()
  })

  it('does NOT refetch card RPCs when events arrive (events feed only the log)', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(mockRpc.call).toHaveBeenCalledWith('sessions.list', { limit: 5 }))
    const before = mockRpc.call.mock.calls.length
    mockRpc.emit('*', 'session.updated', { key: 'z' })
    mockRpc.emit('rpc.state', 'connected')
    // No new RPC calls: the event stream and pill are the only reactions.
    await new Promise((r) => setTimeout(r, 0))
    expect(mockRpc.call.mock.calls.length).toBe(before)
  })

  it('refetches all four card RPCs when Refresh is clicked', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(mockRpc.call).toHaveBeenCalledWith('status', {}))
    const statusCalls = () => mockRpc.call.mock.calls.filter(([m]) => m === 'status').length
    expect(statusCalls()).toBe(1)
    fireEvent.click(screen.getByRole('button', { name: /^refresh$/i }))
    await waitFor(() => expect(statusCalls()).toBe(2))
  })

  it('renders the connection panel prefilled and reconnects on Connect', async () => {
    localStorage.setItem('agentos.wsUrl', 'ws://127.0.0.1:19000/ws')
    wireRpc()
    renderPage()
    const urlInput = screen.getByLabelText(/WebSocket URL/i) as HTMLInputElement
    expect(urlInput.value).toBe('ws://127.0.0.1:19000/ws')
    fireEvent.change(urlInput, { target: { value: 'ws://127.0.0.1:19999/ws' } })
    fireEvent.click(screen.getByRole('button', { name: /^connect$/i }))
    expect(mockRpc.disconnect).toHaveBeenCalled()
    expect(mockRpc.connect).toHaveBeenCalledWith('ws://127.0.0.1:19999/ws', undefined)
    expect(localStorage.getItem('agentos.wsUrl')).toBe('ws://127.0.0.1:19999/ws')
    localStorage.clear()
  })

  it('does not duplicate the global connection state in the overview header', async () => {
    wireRpc()
    renderPage()
    expect(screen.queryByRole('status', { name: /gateway connection/i })).not.toBeInTheDocument()
    expect(screen.getByText('Gateway connection')).toBeInTheDocument()
  })

  it('sets the document title', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(document.title).toBe('Overview - AgentOS Control'))
  })
})
