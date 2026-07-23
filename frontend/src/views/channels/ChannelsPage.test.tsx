import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router'
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

const CHANNEL_CATALOG = {
  channels: [
    {
      type: 'telegram',
      label: 'Telegram',
      description: 'Telegram Bot API.',
      transport: 'mixed',
      whatYouNeed: ['Bot token from @BotFather'],
      fields: [
        { name: 'name', label: 'Channel name', required: true },
        { name: 'agent_id', label: 'Agent id', default: 'main' },
        { name: 'enabled', label: 'Enabled', type: 'bool', default: true },
        {
          name: 'token',
          label: 'Bot token',
          type: 'password',
          secret: true,
          required: true,
        },
        {
          name: 'transport_name',
          label: 'Transport',
          type: 'select',
          choices: ['polling', 'webhook'],
          default: 'polling',
        },
        {
          name: 'webhook_url',
          label: 'Webhook URL',
          showWhen: { transport_name: 'webhook' },
        },
      ],
    },
  ],
}

const SETTINGS_SNAPSHOT = {
  revision: 'revision-a',
  catalog: CHANNEL_CATALOG,
  config: {
    channels: {
      channels: [
        {
          type: 'telegram',
          name: 'tg-main',
          agent_id: 'main',
          enabled: true,
          token: '[redacted]',
          transport_name: 'polling',
          debounce_window_s: 2.5,
          status_reactions_enabled: true,
        },
      ],
    },
  },
  status: { needsOnboarding: false },
}

// Route the two channel RPC reads (and mutations) off a config object.
function wireRpc(
  opts: {
    channels?: unknown[]
    access?: unknown[]
    statusReject?: boolean
    accessReject?: boolean
    mutationReject?: boolean
    snapshot?: Record<string, unknown>
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
      case 'config.snapshot':
        return Promise.resolve(opts.snapshot ?? SETTINGS_SNAPSHOT)
      case 'onboarding.channel.probe':
        return opts.mutationReject
          ? Promise.reject(new Error('invalid channel'))
          : Promise.resolve({ status: 'ready', restartRequired: true })
      case 'onboarding.channel.upsert':
        return opts.mutationReject
          ? Promise.reject(new Error('save failed'))
          : Promise.resolve({ changed: true, restartRequired: true })
      default:
        return Promise.resolve({})
    }
  })
}

function renderPage(initialEntries: string[] = ['/channels']) {
  const router = createMemoryRouter(
    [
      { path: '/channels', element: <ChannelsPage /> },
      { path: '/config', element: <div>Advanced configuration route</div> },
    ],
    { initialEntries },
  )
  const view = render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return { ...view, router }
}

describe('ChannelsPage', () => {
  beforeEach(() => {
    mockRpc = makeRpc()
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

  it('groups live posture and adapter inventory into the redesigned workspace', async () => {
    wireRpc()
    renderPage()
    const operations = await screen.findByLabelText('Channel operations')
    expect(within(operations).getByText('Integration mesh')).toBeInTheDocument()
    expect(within(operations).getByLabelText('Channels summary')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Configured channels' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^add channel$/i })).toBeInTheDocument()
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
    expect(card.querySelector('[data-adapter-logo="telegram"]')).toBeInTheDocument()
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

  it('opens the unified setup flow from the empty state', async () => {
    wireRpc({ channels: [], access: [] })
    renderPage()
    await waitFor(() => expect(screen.getByText(/No configured channels/i)).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /add your first channel/i }))
    expect(await screen.findByRole('dialog', { name: 'Add a channel' })).toBeInTheDocument()
    expect(mockRpc.call).toHaveBeenCalledWith('config.snapshot')
  })

  it('supports a canonical deep link into channel setup', async () => {
    wireRpc()
    renderPage(['/channels?view=setup'])
    expect(await screen.findByRole('dialog', { name: 'Add a channel' })).toBeInTheDocument()
    expect(screen.getByRole('list', { name: 'Channel setup progress' })).toBeInTheDocument()
    expect((await screen.findByLabelText('Agent id')).closest('.ch-setup__field')).toHaveAttribute(
      'data-field',
      'agent_id',
    )
    expect(screen.getByLabelText('Enabled').closest('.ch-setup__field')).toHaveClass('is-wide')
    const telegram = screen.getByRole('radio', { name: /telegram.*mixed/i })
    expect(telegram.querySelector('[data-adapter-logo="telegram"]')).toBeInTheDocument()
  })

  it('reveals catalog fields only when their showWhen condition matches', async () => {
    wireRpc()
    renderPage(['/channels?view=setup'])
    await screen.findByLabelText('Channel name')
    expect(screen.queryByLabelText('Webhook URL')).not.toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Transport'), { target: { value: 'webhook' } })
    expect(screen.getByLabelText('Webhook URL')).toBeInTheDocument()
  })

  it('validates required channel fields before calling the probe', async () => {
    wireRpc()
    renderPage(['/channels?view=setup'])
    await screen.findByRole('dialog', { name: 'Add a channel' })
    await screen.findByLabelText('Channel name')
    fireEvent.click(screen.getByRole('button', { name: /validate & add/i }))
    expect(await screen.findByText('Channel name is required.')).toBeInTheDocument()
    expect(mockRpc.call.mock.calls.map(([method]) => method)).not.toContain(
      'onboarding.channel.probe',
    )
  })

  it('probes before upsert and carries the snapshot revision', async () => {
    wireRpc({ channels: [], access: [] })
    renderPage(['/channels?view=setup'])
    await screen.findByRole('dialog', { name: 'Add a channel' })
    fireEvent.change(await screen.findByLabelText('Channel name'), {
      target: { value: 'my-bot' },
    })
    fireEvent.change(screen.getByLabelText('Bot token'), { target: { value: '123:secret' } })
    fireEvent.click(screen.getByRole('button', { name: /validate & add/i }))

    await waitFor(() => {
      const methods = mockRpc.call.mock.calls.map(([method]) => method)
      expect(methods.indexOf('onboarding.channel.probe')).toBeGreaterThanOrEqual(0)
      expect(methods.indexOf('onboarding.channel.upsert')).toBeGreaterThan(
        methods.indexOf('onboarding.channel.probe'),
      )
    })
    expect(mockRpc.call).toHaveBeenCalledWith(
      'onboarding.channel.upsert',
      expect.objectContaining({
        expectedRevision: 'revision-a',
        entry: expect.objectContaining({
          type: 'telegram',
          name: 'my-bot',
          token: '123:secret',
        }),
      }),
    )
    expect(toast.success).toHaveBeenCalledWith(
      expect.stringContaining('Restart AgentOS'),
      expect.anything(),
    )
  })

  it('opens Configure on the existing channel and keeps its secret write-only', async () => {
    wireRpc()
    renderPage()
    await screen.findByLabelText('Channel tg-main')
    const card = screen.getByLabelText('Channel tg-main')
    fireEvent.click(within(card).getByRole('button', { name: /configure/i }))
    expect(await screen.findByRole('dialog', { name: 'Configure tg-main' })).toBeInTheDocument()
    expect(await screen.findByLabelText('Channel name')).toHaveValue('tg-main')
    expect(screen.getByLabelText('Channel name')).toBeDisabled()
    expect(screen.getByLabelText('Bot token')).toHaveValue('')

    fireEvent.change(screen.getByLabelText('Agent id'), { target: { value: 'support' } })
    fireEvent.click(screen.getByRole('button', { name: /validate & update/i }))
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith(
        'onboarding.channel.upsert',
        expect.objectContaining({
          expectedRevision: 'revision-a',
          entry: expect.objectContaining({
            name: 'tg-main',
            agent_id: 'support',
            debounce_window_s: 2.5,
            status_reactions_enabled: true,
          }),
        }),
      ),
    )
    const upsert = mockRpc.call.mock.calls.find(
      ([method]) => method === 'onboarding.channel.upsert',
    )
    expect((upsert?.[1] as { entry: Record<string, unknown> }).entry).not.toHaveProperty('token')
  })

  it('routes adapters outside the guided catalog to Advanced config', async () => {
    wireRpc({
      channels: [{ name: 'teams', type: 'msteams', status: 'running', restart_attempts: 0 }],
      access: [],
      snapshot: {
        ...SETTINGS_SNAPSHOT,
        config: {
          channels: {
            channels: [{ type: 'msteams', name: 'teams', app_id: 'app' }],
          },
        },
      },
    })
    renderPage()
    const card = await screen.findByLabelText('Channel teams')
    expect(card.querySelector('[data-adapter-logo="teams"]')).toBeInTheDocument()
    const advanced = await within(card).findByRole('button', { name: /advanced config/i })
    expect(within(card).queryByRole('button', { name: /^configure$/i })).not.toBeInTheDocument()
    fireEvent.click(advanced)
    expect(await screen.findByText('Advanced configuration route')).toBeInTheDocument()
  })

  it('keeps the dialog open while validation and save are pending', async () => {
    wireRpc({ channels: [], access: [] })
    const baseCall = mockRpc.call.getMockImplementation()
    let finishProbe: (() => void) | undefined
    mockRpc.call.mockImplementation((method: string, ...args: unknown[]) => {
      if (method === 'onboarding.channel.probe') {
        return new Promise((resolve) => {
          finishProbe = () => resolve({ status: 'ready' })
        })
      }
      return baseCall?.(method, ...args)
    })

    const { router } = renderPage(['/channels', '/channels?view=setup'])
    fireEvent.change(await screen.findByLabelText('Channel name'), {
      target: { value: 'pending-bot' },
    })
    fireEvent.change(screen.getByLabelText('Bot token'), { target: { value: '123:secret' } })
    fireEvent.click(screen.getByRole('button', { name: /validate & add/i }))
    expect(await screen.findByRole('button', { name: /validating/i })).toBeDisabled()

    fireEvent.keyDown(screen.getByRole('dialog', { name: 'Add a channel' }), { key: 'Escape' })
    expect(screen.getByRole('dialog', { name: 'Add a channel' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /close channel setup/i })).toBeDisabled()

    await router.navigate(-1)
    const blocked = await screen.findByRole('alertdialog')
    expect(within(blocked).getByRole('button', { name: /keep editing/i })).toBeDisabled()
    expect(within(blocked).getByRole('button', { name: /discard draft/i })).toBeDisabled()
    expect(router.state.location.search).toBe('?view=setup')

    finishProbe?.()
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Add a channel' })).not.toBeInTheDocument(),
    )
    expect(router.state.location.search).toBe('')
  })

  it('preserves a dirty draft when SPA navigation is attempted', async () => {
    wireRpc()
    const { router } = renderPage(['/channels', '/channels?view=setup'])
    const name = await screen.findByLabelText('Channel name')
    fireEvent.change(name, { target: { value: 'draft-channel' } })

    await router.navigate(-1)
    const confirm = await screen.findByRole('alertdialog', {
      name: /discard this channel draft/i,
    })
    const keepEditing = within(confirm).getByRole('button', { name: /keep editing/i })
    expect(keepEditing).toHaveFocus()
    expect(router.state.location.search).toBe('?view=setup')

    fireEvent.click(keepEditing)
    expect(screen.getByLabelText('Channel name')).toHaveValue('draft-channel')
    expect(router.state.location.search).toBe('?view=setup')

    await router.navigate(-1)
    fireEvent.click(
      within(await screen.findByRole('alertdialog')).getByRole('button', {
        name: /discard draft/i,
      }),
    )
    await waitFor(() => expect(router.state.location.search).toBe(''))
    expect(screen.queryByRole('dialog', { name: 'Add a channel' })).not.toBeInTheDocument()
  })

  it('rebases a preserved draft after a config revision conflict', async () => {
    wireRpc({ channels: [], access: [] })
    const baseCall = mockRpc.call.getMockImplementation()
    let snapshotCalls = 0
    let upsertCalls = 0
    mockRpc.call.mockImplementation((method: string, ...args: unknown[]) => {
      if (method === 'config.snapshot') {
        snapshotCalls += 1
        return Promise.resolve({
          ...SETTINGS_SNAPSHOT,
          revision: snapshotCalls === 1 ? 'revision-a' : 'revision-b',
        })
      }
      if (method === 'onboarding.channel.upsert') {
        upsertCalls += 1
        return upsertCalls === 1
          ? Promise.reject(new Error('config revision mismatch'))
          : Promise.resolve({ changed: true, restartRequired: true })
      }
      return baseCall?.(method, ...args)
    })

    renderPage(['/channels?view=setup'])
    fireEvent.change(await screen.findByLabelText('Channel name'), {
      target: { value: 'conflicted-bot' },
    })
    fireEvent.change(screen.getByLabelText('Bot token'), { target: { value: '123:secret' } })
    fireEvent.click(screen.getByRole('button', { name: /validate & add/i }))

    const useLatest = await screen.findByRole('button', { name: /use latest version/i })
    expect(screen.getByLabelText('Channel name')).toHaveValue('conflicted-bot')
    expect(screen.getByLabelText('Bot token')).toHaveValue('123:secret')
    fireEvent.click(useLatest)
    fireEvent.click(screen.getByRole('button', { name: /validate & add/i }))

    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith(
        'onboarding.channel.upsert',
        expect.objectContaining({ expectedRevision: 'revision-b' }),
      ),
    )
  })

  it('blocks channel writes when the coherent snapshot is disk-diverged', async () => {
    wireRpc({
      snapshot: {
        ...SETTINGS_SNAPSHOT,
        revision: null,
        diskDiverged: true,
        writeBlocked: true,
      },
    })
    renderPage(['/channels?view=setup'])
    await screen.findByLabelText('Channel name')
    expect(screen.getByText(/Configuration changed on disk/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /validate & add/i })).toBeDisabled()
  })

  it('asks before discarding a dirty channel draft', async () => {
    wireRpc()
    renderPage(['/channels?view=setup'])
    const name = await screen.findByLabelText('Channel name')
    fireEvent.change(name, { target: { value: 'draft-channel' } })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    const confirm = screen.getByRole('alertdialog', { name: /discard this channel draft/i })
    const keepEditing = within(confirm).getByRole('button', { name: /keep editing/i })
    expect(keepEditing).toHaveFocus()
    fireEvent.keyDown(confirm, { key: 'Escape' })
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Channel name')).toHaveValue('draft-channel')

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    fireEvent.click(
      within(screen.getByRole('alertdialog')).getByRole('button', { name: /discard draft/i }),
    )
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Add a channel' })).not.toBeInTheDocument(),
    )
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
