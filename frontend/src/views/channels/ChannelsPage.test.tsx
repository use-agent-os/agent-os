import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'sonner'
import { ChannelsPage } from './ChannelsPage'

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

// A minimal event-bus stub matching the WsRpcClient surface ChannelsPage uses.
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
    // test helper: fan a real-time event out to its subscribers.
    emit(event: string, ...args: unknown[]) {
      listeners.get(event)?.forEach((h) => h(...args))
    },
    listenerCount(event: string) {
      return listeners.get(event)?.size ?? 0
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

const TELEGRAM_CHANNEL = {
  name: 'tg-main',
  type: 'telegram',
  status: 'running',
  connected_since: 1_700_000_000,
  restart_attempts: 0,
}
const DISCORD_CHANNEL = {
  name: 'disc',
  type: 'discord',
  status: 'stopped',
  restart_attempts: 2,
}
const ACCESS_ENTRY = {
  name: 'tg-main',
  mode: 'pairing',
  pending: [{ sender_id: 42, username: 'ada', code: 'A1B2' }],
  approved: [{ sender_id: 7, display_name: 'Bob' }],
}

// Route the two channel RPC reads (and mutations) off a config object.
function wireRpc(
  opts: {
    channels?: unknown[]
    access?: unknown[]
    statusReject?: boolean
    accessReject?: boolean
    mutationReject?: boolean
  } = {},
) {
  mockRpc.call.mockImplementation((method: string) => {
    switch (method) {
      case 'channels.status':
        return opts.statusReject
          ? Promise.reject(new Error('status down'))
          : Promise.resolve({ channels: opts.channels ?? [TELEGRAM_CHANNEL, DISCORD_CHANNEL] })
      case 'channels.access.list':
        return opts.accessReject
          ? Promise.reject(new Error('access down'))
          : Promise.resolve({ channels: opts.access ?? [ACCESS_ENTRY] })
      case 'channels.access.setMode':
      case 'channels.access.resolve':
      case 'channels.access.revoke':
        return opts.mutationReject
          ? Promise.reject(new Error('mutation failed'))
          : Promise.resolve({})
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
        <ChannelsPage />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

describe('ChannelsPage', () => {
  beforeEach(() => {
    mockRpc = makeRpc()
    navigateSpy.mockReset()
    vi.mocked(toast.success).mockClear()
    vi.mocked(toast.warning).mockClear()
    vi.mocked(toast.error).mockClear()
    vi.mocked(toast.info).mockClear()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('calls channels.status and channels.access.list after waitForConnection', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(mockRpc.call).toHaveBeenCalledWith('channels.status', {}))
    expect(mockRpc.call).toHaveBeenCalledWith('channels.access.list', {})
    expect(mockRpc.waitForConnection).toHaveBeenCalled()
  })

  it('still renders channels when channels.access.list rejects', async () => {
    wireRpc({ accessReject: true })
    renderPage()
    await waitFor(() => expect(screen.getByText('tg-main')).toBeInTheDocument())
    expect(screen.getByText('disc')).toBeInTheDocument()
  })

  it('renders the stat row from the channel payload', async () => {
    wireRpc()
    renderPage()
    // Total channels tile shows 2; restart attempts sum to 2; 1 chat approval.
    await waitFor(() => expect(screen.getByLabelText('Total channels')).toHaveTextContent('2'))
    expect(screen.getByLabelText('Chat approvals')).toHaveTextContent('1')
    expect(screen.getByLabelText('Restart attempts')).toHaveTextContent('2')
  })

  it('tones the Chat approvals attention tile as warn, not danger (channels.js:146,384-389)', async () => {
    // Default fixture has one pending Telegram request → the tile is in
    // attention. Legacy colors the attention stat with --warn, never --danger:
    // the tile must not carry a tone-danger class.
    wireRpc()
    renderPage()
    await waitFor(() =>
      expect(screen.getByLabelText('Chat approvals')).toHaveClass('ch-stat--attention'),
    )
    expect(screen.getByLabelText('Chat approvals')).not.toHaveClass('tone-danger')
  })

  it('gives the inactive "N need attention" hint the danger ch-neg class (channels.js:139)', async () => {
    // A dead channel is counted as attention; the Inactive tile's hint then
    // reads "N need attention" and legacy emphasizes it with .ch-neg (--danger).
    wireRpc({
      channels: [{ name: 'dead-tg', type: 'telegram', status: 'dead', restart_attempts: 1 }],
      access: [],
    })
    renderPage()
    await waitFor(() =>
      expect(screen.getByLabelText('Inactive').querySelector('.ch-neg')).not.toBeNull(),
    )
    expect(screen.getByLabelText('Inactive').querySelector('.ch-neg')).toHaveTextContent(
      '1 need attention',
    )
  })

  it('renders a channel card with status chip, type and config details', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByText('tg-main')).toBeInTheDocument())
    const card = screen.getByLabelText('Channel tg-main')
    expect(within(card).getByText('running')).toBeInTheDocument()
    expect(within(card).getByText('telegram')).toBeInTheDocument()
    // adapter config <details>
    expect(within(card).getByText('Adapter config')).toBeInTheDocument()
  })

  it('renders the telegram access panel only for telegram channels and shows pending/approved groups', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByText('tg-main')).toBeInTheDocument())
    const tgCard = screen.getByLabelText('Channel tg-main')
    const discCard = screen.getByLabelText('Channel disc')
    // Telegram card has the access panel; discord card does not.
    expect(within(tgCard).getByText('Chat access')).toBeInTheDocument()
    expect(within(discCard).queryByText('Chat access')).not.toBeInTheDocument()
    // Pending account (@ada) + approved account (Bob).
    expect(within(tgCard).getByText('@ada')).toBeInTheDocument()
    expect(within(tgCard).getByText('Bob')).toBeInTheDocument()
  })

  it('changing the access mode calls channels.access.setMode and invalidates (open warns)', async () => {
    wireRpc()
    renderPage()
    await waitFor(() =>
      expect(screen.getByLabelText('Telegram chat access mode')).toBeInTheDocument(),
    )
    const select = screen.getByLabelText('Telegram chat access mode')
    fireEvent.change(select, { target: { value: 'open' } })
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('channels.access.setMode', {
        channel: 'tg-main',
        mode: 'open',
      }),
    )
    // open → warn toast; a refetch re-issues channels.status.
    await waitFor(() => expect(toast.warning).toHaveBeenCalled())
    const statusCalls = () =>
      mockRpc.call.mock.calls.filter(([m]) => m === 'channels.status').length
    await waitFor(() => expect(statusCalls()).toBeGreaterThanOrEqual(2))
  })

  it('approving a pending account calls channels.access.resolve with approved:true and invalidates', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByText('@ada')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /approve/i }))
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('channels.access.resolve', {
        channel: 'tg-main',
        senderId: '42',
        approved: true,
      }),
    )
    const statusCalls = () =>
      mockRpc.call.mock.calls.filter(([m]) => m === 'channels.status').length
    await waitFor(() => expect(statusCalls()).toBeGreaterThanOrEqual(2))
  })

  it('denying a pending account calls channels.access.resolve with approved:false', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByText('@ada')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /deny/i }))
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('channels.access.resolve', {
        channel: 'tg-main',
        senderId: '42',
        approved: false,
      }),
    )
  })

  it('revoking an approved account calls channels.access.revoke and invalidates', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByText('Bob')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /revoke/i }))
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('channels.access.revoke', {
        channel: 'tg-main',
        senderId: '7',
      }),
    )
    const statusCalls = () =>
      mockRpc.call.mock.calls.filter(([m]) => m === 'channels.status').length
    await waitFor(() => expect(statusCalls()).toBeGreaterThanOrEqual(2))
  })

  it('refetches when a channel.status event arrives (targeted invalidate)', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(mockRpc.call).toHaveBeenCalledWith('channels.status', {}))
    const statusCalls = () =>
      mockRpc.call.mock.calls.filter(([m]) => m === 'channels.status').length
    expect(statusCalls()).toBe(1)
    mockRpc.emit('channel.status', {})
    await waitFor(() => expect(statusCalls()).toBe(2))
  })

  it('unsubscribes from channel.status on unmount', async () => {
    wireRpc()
    const view = renderPage()
    await waitFor(() => expect(mockRpc.listenerCount('channel.status')).toBe(1))
    view.unmount()
    expect(mockRpc.listenerCount('channel.status')).toBe(0)
  })

  it('refreshes on the Refresh button', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(mockRpc.call).toHaveBeenCalledWith('channels.status', {}))
    const statusCalls = () =>
      mockRpc.call.mock.calls.filter(([m]) => m === 'channels.status').length
    expect(statusCalls()).toBe(1)
    fireEvent.click(screen.getByRole('button', { name: /^refresh$/i }))
    await waitFor(() => expect(statusCalls()).toBe(2))
  })

  it('shows the empty state with a guided-setup link into /setup when no channels are configured', async () => {
    wireRpc({ channels: [], access: [] })
    renderPage()
    await waitFor(() => expect(screen.getByText(/No configured channels/i)).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /guided setup/i }))
    expect(navigateSpy).toHaveBeenCalledWith('/setup')
  })

  it('toasts when channels.status fails', async () => {
    wireRpc({ statusReject: true })
    renderPage()
    await waitFor(() => expect(toast.error).toHaveBeenCalled())
  })

  it('sets the document title', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(document.title).toBe('Channels - AgentOS Control'))
  })
})
