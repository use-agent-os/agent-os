import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { toast } from 'sonner'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useConnection } from '@/stores/connection'
import { useGateway } from '~/stores/gateway'
import { useSettings } from '~/stores/settings'
import { ChannelsPane } from './ChannelsPane'

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), warning: vi.fn(), error: vi.fn(), info: vi.fn() },
}))

type Handler = (...args: unknown[]) => void
const listeners = new Map<string, Set<Handler>>()
const rpcCall = vi.fn()
const rpcOn = vi.fn((event: string, handler: Handler) => {
  if (!listeners.has(event)) listeners.set(event, new Set())
  listeners.get(event)!.add(handler)
  return () => listeners.get(event)?.delete(handler)
})
vi.mock('@/app/providers', () => ({
  useRpc: () => ({ call: rpcCall, on: rpcOn, waitForConnection: async () => {} }),
}))

const openExternal = vi.fn(async () => {})
const openPath = vi.fn(async () => '')
const gatewayRestart = vi.fn(async () => ({ state: 'running', pid: 4242, url: null, error: null }))
vi.mock('~/lib/desktop-api', () => ({
  desktopApi: () => ({
    app: { openExternal, openPath, showItemInFolder: vi.fn() },
    gateway: { restart: gatewayRestart },
  }),
  isDesktop: () => true,
}))

const TELEGRAM_SPEC = {
  type: 'telegram',
  label: 'Telegram',
  description: 'Telegram Bot API.',
  transport: 'mixed',
  docsHint: 'https://example.test/telegram',
  whatYouNeed: ['Bot token from @BotFather.'],
  fields: [
    { name: 'name', label: 'Channel name', required: true },
    { name: 'agent_id', label: 'Agent id', default: 'main' },
    { name: 'enabled', label: 'Enabled', type: 'bool', default: true },
    { name: 'token', label: 'Bot token', type: 'password', secret: true, required: true },
    {
      name: 'transport_name',
      label: 'Transport',
      type: 'select',
      choices: ['polling', 'webhook'],
      default: 'polling',
    },
    { name: 'webhook_url', label: 'Webhook URL', showWhen: { transport_name: 'webhook' } },
    { name: 'debounce_window_s', label: 'Debounce', type: 'float', default: 2.5, advanced: true },
  ],
}

const STORED = {
  type: 'telegram',
  name: 'tg-main',
  agent_id: 'main',
  enabled: true,
  token: '[redacted]',
  transport_name: 'polling',
  debounce_window_s: 2.5,
}

const SNAPSHOT = {
  revision: 'rev-a',
  configPath: '/tmp/agentos-home/.agentos/config.toml',
  catalog: { channels: [TELEGRAM_SPEC] },
  config: { channels: { channels: [STORED] } },
  status: {},
}

const TG = {
  name: 'tg-main',
  type: 'telegram',
  status: 'running',
  connected: true,
  connected_since: Math.floor(Date.now() / 1000) - 120,
  restart_attempts: 0,
  enabled: true,
}
const DISCORD = { name: 'disc', type: 'discord', status: 'stopped', restart_attempts: 2 }
const IRC = { name: 'irc-old', type: 'irc', status: 'dead', restart_attempts: 5 }
const ACCESS = {
  name: 'tg-main',
  pending: [{ sender_id: 42, username: 'ada', code: 'A1B2' }],
  paired: [{ sender_id: 7, display_name: 'Bob' }],
  groups_enabled: true,
  group_chat_ids: ['-100'],
  group_mention_required: true,
}

function wire(
  opts: {
    channels?: unknown[]
    access?: unknown[]
    statusReject?: boolean
    snapshot?: Record<string, unknown>
  } = {},
) {
  rpcCall.mockImplementation(async (method: string) => {
    switch (method) {
      case 'channels.status':
        if (opts.statusReject) throw new Error('status down')
        return { channels: opts.channels ?? [TG, DISCORD] }
      case 'channels.pairing.list':
        return { channels: opts.access ?? [ACCESS] }
      case 'config.snapshot':
        return { ...SNAPSHOT, ...opts.snapshot }
      case 'onboarding.channel.probe':
        return { status: 'ready', restartRequired: true }
      case 'onboarding.channel.upsert':
      case 'onboarding.channel.remove':
      case 'onboarding.channel.enable':
      case 'onboarding.channel.disable':
        return { changed: true, restartRequired: true }
      default:
        return {}
    }
  })
}

function renderPane() {
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <ChannelsPane />
    </QueryClientProvider>,
  )
}

function card(name: string) {
  return screen.getByRole('region', { name })
}

async function loaded() {
  await screen.findByRole('region', { name: 'tg-main' })
}

beforeEach(() => {
  rpcCall.mockReset()
  rpcOn.mockClear()
  listeners.clear()
  openExternal.mockClear()
  openPath.mockClear()
  gatewayRestart.mockClear()
  vi.mocked(toast.success).mockClear()
  vi.mocked(toast.error).mockClear()
  useConnection.setState({ state: 'connected' })
  useGateway.setState({ status: { state: 'running', pid: null, url: null, error: null } })
  useSettings.setState((s) => ({
    settings: { ...s.settings, gateway: { ...s.settings.gateway, mode: 'managed' } },
  }))
  wire()
})

describe('ChannelsPane', () => {
  it('asks for the gateway when it is not running, and never reads', () => {
    useConnection.setState({ state: 'disconnected' })
    renderPane()
    expect(screen.getByRole('heading', { name: 'Channels' })).toBeInTheDocument()
    expect(screen.getByText('Start the gateway to configure this.')).toBeInTheDocument()
    expect(rpcCall).not.toHaveBeenCalled()
  })

  it('lists every channel with its status, and sums the overview', async () => {
    renderPane()
    await loaded()
    expect(rpcCall).toHaveBeenCalledWith('channels.status', {})
    expect(rpcCall).toHaveBeenCalledWith('channels.pairing.list', {})
    const tg = card('tg-main')
    expect(within(tg).getByText('running')).toBeInTheDocument()
    expect(within(tg).getByText('2m ago')).toBeInTheDocument()
    const disc = card('disc')
    expect(within(disc).getByText('stopped')).toBeInTheDocument()
    expect(within(disc).getByText('2')).toBeInTheDocument()
    // Overview: 2 channels, 1 connected, 0 attention, 1 pairing request.
    const overview = card('Overview')
    const values = within(overview)
      .getAllByText(/^\d+$/)
      .map((el) => el.textContent)
    expect(values).toEqual(['2', '1', '0', '1'])
    // Still subscribed to the gateway's push for the section's lifetime.
    expect(rpcOn).toHaveBeenCalledWith('channel.status', expect.any(Function))
  })

  it('refetches when the gateway pushes a channel.status event', async () => {
    renderPane()
    await loaded()
    const before = rpcCall.mock.calls.filter(([m]) => m === 'channels.status').length
    listeners.get('channel.status')?.forEach((h) => h({}))
    await waitFor(() =>
      expect(rpcCall.mock.calls.filter(([m]) => m === 'channels.status').length).toBe(before + 1),
    )
  })

  it('shows the load error with a retry', async () => {
    wire({ statusReject: true })
    renderPane()
    expect(await screen.findByText('Channels unavailable: status down')).toBeInTheDocument()
    wire()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await loaded()
  })

  it('offers an empty state with the add action', async () => {
    wire({ channels: [], access: [] })
    renderPane()
    const empty = await screen.findByRole('region', { name: 'No channels yet' })
    expect(within(empty).getByRole('button', { name: 'Add channel' })).toBeInTheDocument()
  })

  it('pairs, denies and disconnects Telegram senders', async () => {
    renderPane()
    await loaded()
    const tg = card('tg-main')
    expect(within(tg).getByText('@ada')).toBeInTheDocument()
    expect(within(tg).getByText('Bob')).toBeInTheDocument()
    expect(
      within(tg).getByText('Group chats allowed (1); the bot needs an @mention.'),
    ).toBeInTheDocument()
    fireEvent.click(within(tg).getByRole('button', { name: 'Pair' }))
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('channels.pairing.approve', {
        channel: 'tg-main',
        senderId: '42',
      }),
    )
    fireEvent.click(within(tg).getByRole('button', { name: 'Deny' }))
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('channels.pairing.deny', {
        channel: 'tg-main',
        senderId: '42',
      }),
    )
    fireEvent.click(within(tg).getByRole('button', { name: 'Disconnect' }))
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('channels.pairing.revoke', {
        channel: 'tg-main',
        senderId: '7',
      }),
    )
  })

  it('adds a channel through probe + upsert with the snapshot revision', async () => {
    renderPane()
    await loaded()
    fireEvent.click(screen.getByRole('button', { name: 'Add channel' }))
    const sheet = await screen.findByRole('dialog', { name: 'Add channel' })
    expect(within(sheet).getByRole('radio', { name: /Telegram/ })).toBeChecked()
    expect(within(sheet).getByText('Bot token from @BotFather.')).toBeInTheDocument()
    // A polling bot has no webhook URL to fill in; the advanced field is folded.
    expect(within(sheet).queryByLabelText('Webhook URL')).toBeNull()
    expect(within(sheet).getByText('Advanced options')).toBeInTheDocument()

    // Nothing typed yet: the save is held back.
    expect(within(sheet).getByRole('button', { name: 'Add channel' })).toBeDisabled()
    fireEvent.change(within(sheet).getByLabelText('Channel name *'), {
      target: { value: 'tg-ops' },
    })
    fireEvent.click(within(sheet).getByRole('button', { name: 'Add channel' }))
    expect(within(sheet).getByRole('alert')).toHaveTextContent('Bot token is required.')
    expect(rpcCall).not.toHaveBeenCalledWith('onboarding.channel.probe', expect.anything())

    const token = within(sheet).getByLabelText('Bot token *')
    expect(token).toHaveAttribute('type', 'password')
    fireEvent.change(token, { target: { value: '123:abc' } })
    fireEvent.click(within(sheet).getByRole('switch', { name: 'Enabled' }))
    fireEvent.click(within(sheet).getByRole('button', { name: 'Add channel' }))

    const entry = {
      type: 'telegram',
      name: 'tg-ops',
      agent_id: 'main',
      enabled: false,
      token: '123:abc',
      transport_name: 'polling',
      debounce_window_s: '2.5',
    }
    await waitFor(() => expect(rpcCall).toHaveBeenCalledWith('onboarding.channel.probe', { entry }))
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('onboarding.channel.upsert', {
        entry,
        expectedRevision: 'rev-a',
      }),
    )
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Add channel' })).toBeNull())
    // An external gateway is told to restart; nothing is restarted for it.
    expect(toast.success).toHaveBeenCalledWith('Saved. Restart the gateway to apply.', {
      id: 'stg-channels',
    })
    expect(gatewayRestart).not.toHaveBeenCalled()
  })

  it('refuses a duplicate name before asking the gateway', async () => {
    renderPane()
    await loaded()
    fireEvent.click(screen.getByRole('button', { name: 'Add channel' }))
    const sheet = await screen.findByRole('dialog', { name: 'Add channel' })
    fireEvent.change(within(sheet).getByLabelText('Channel name *'), {
      target: { value: 'disc' },
    })
    fireEvent.change(within(sheet).getByLabelText('Bot token *'), { target: { value: 't' } })
    fireEvent.click(within(sheet).getByRole('button', { name: 'Add channel' }))
    expect(within(sheet).getByRole('alert')).toHaveTextContent(
      'A channel named disc already exists.',
    )
    expect(rpcCall).not.toHaveBeenCalledWith('onboarding.channel.probe', expect.anything())
  })

  it('edits a channel: the adapter and name are locked, a blank secret is kept', async () => {
    renderPane()
    await loaded()
    fireEvent.click(within(card('tg-main')).getByRole('button', { name: 'Configure' }))
    const sheet = await screen.findByRole('dialog', { name: 'Configure tg-main' })
    expect(within(sheet).getByLabelText('Channel name *')).toBeDisabled()
    expect(within(sheet).getByRole('radio', { name: /Telegram/ })).toBeChecked()
    expect(
      within(sheet).getByText('The adapter cannot change once a channel exists.'),
    ).toBeInTheDocument()
    expect(within(sheet).getByLabelText('Bot token *')).toHaveValue('')
    expect(within(sheet).getByRole('button', { name: 'Save changes' })).toBeDisabled()
    fireEvent.change(within(sheet).getByLabelText('Agent id'), { target: { value: 'ops' } })
    fireEvent.click(within(sheet).getByRole('button', { name: 'Save changes' }))
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('onboarding.channel.upsert', {
        entry: {
          type: 'telegram',
          name: 'tg-main',
          agent_id: 'ops',
          enabled: true,
          transport_name: 'polling',
          debounce_window_s: '2.5',
        },
        expectedRevision: 'rev-a',
      }),
    )
  })

  it('keeps a gateway refusal on screen, and asks before discarding edits', async () => {
    renderPane()
    await loaded()
    fireEvent.click(within(card('tg-main')).getByRole('button', { name: 'Configure' }))
    const sheet = await screen.findByRole('dialog', { name: 'Configure tg-main' })
    fireEvent.change(within(sheet).getByLabelText('Agent id'), { target: { value: 'ops' } })
    rpcCall.mockImplementationOnce(async () => {
      throw new Error('token rejected by Telegram')
    })
    fireEvent.click(within(sheet).getByRole('button', { name: 'Save changes' }))
    await waitFor(() =>
      expect(within(sheet).getByRole('alert')).toHaveTextContent('token rejected by Telegram'),
    )
    fireEvent.click(within(sheet).getByRole('button', { name: 'Cancel' }))
    const confirm = screen.getByRole('alertdialog', { name: 'Discard changes?' })
    fireEvent.click(within(confirm).getByRole('button', { name: 'Keep editing' }))
    expect(screen.getByRole('dialog', { name: 'Configure tg-main' })).toBeInTheDocument()
    fireEvent.click(within(sheet).getByRole('button', { name: 'Cancel' }))
    fireEvent.click(screen.getByRole('button', { name: 'Discard' }))
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Configure tg-main' })).toBeNull(),
    )
  })

  it('reports a revision conflict and offers a reload', async () => {
    renderPane()
    await loaded()
    fireEvent.click(within(card('tg-main')).getByRole('button', { name: 'Configure' }))
    const sheet = await screen.findByRole('dialog', { name: 'Configure tg-main' })
    fireEvent.change(within(sheet).getByLabelText('Agent id'), { target: { value: 'ops' } })
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'onboarding.channel.probe') return {}
      if (method === 'onboarding.channel.upsert') throw new Error('config revision mismatch')
      if (method === 'config.snapshot') return { ...SNAPSHOT, revision: 'rev-b' }
      if (method === 'channels.status') return { channels: [TG, DISCORD] }
      return { channels: [ACCESS] }
    })
    fireEvent.click(within(sheet).getByRole('button', { name: 'Save changes' }))
    await waitFor(() =>
      expect(within(sheet).getByRole('alert')).toHaveTextContent(
        'The configuration changed while you were editing.',
      ),
    )
    expect(within(sheet).getByRole('button', { name: 'Save changes' })).toBeDisabled()
    fireEvent.click(within(sheet).getByRole('button', { name: 'Reload' }))
    const fresh = await screen.findByRole('dialog', { name: 'Configure tg-main' })
    expect(within(fresh).getByLabelText('Agent id')).toHaveValue('main')
  })

  it('disables, restarts and removes a channel', async () => {
    renderPane()
    await loaded()
    const tg = card('tg-main')
    fireEvent.click(within(tg).getByRole('switch', { name: 'Enabled tg-main' }))
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('onboarding.channel.disable', {
        name: 'tg-main',
        expectedRevision: 'rev-a',
      }),
    )
    fireEvent.click(within(tg).getByRole('button', { name: 'Restart' }))
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('channels.restart', { channel: 'tg-main' }),
    )
    // A stopped entry has nothing loaded to restart.
    expect(within(card('disc')).queryByRole('button', { name: 'Restart' })).toBeNull()

    fireEvent.click(within(tg).getByRole('button', { name: 'Remove' }))
    const confirm = screen.getByRole('alertdialog', { name: 'Remove tg-main?' })
    fireEvent.click(within(confirm).getByRole('button', { name: 'Cancel' }))
    expect(rpcCall).not.toHaveBeenCalledWith('onboarding.channel.remove', expect.anything())
    fireEvent.click(within(tg).getByRole('button', { name: 'Remove' }))
    fireEvent.click(
      within(screen.getByRole('alertdialog', { name: 'Remove tg-main?' })).getByRole('button', {
        name: 'Remove',
      }),
    )
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('onboarding.channel.remove', {
        name: 'tg-main',
        expectedRevision: 'rev-a',
      }),
    )
  })

  it('restarts a managed gateway after a config write', async () => {
    useGateway.setState({ status: { state: 'running', pid: 4242, url: null, error: null } })
    renderPane()
    await loaded()
    fireEvent.click(within(card('disc')).getByRole('switch', { name: 'Enabled disc' }))
    await waitFor(() => expect(gatewayRestart).toHaveBeenCalled())
    expect(toast.success).toHaveBeenCalledWith('Saved. Restarting the gateway to apply.', {
      id: 'stg-channels',
    })
  })

  it('offers to restart a managed gateway running an older configuration', async () => {
    wire({ snapshot: { pendingRestart: true } })
    renderPane()
    await loaded()
    expect(
      await screen.findByText('The gateway is running an older configuration. Restart to apply.'),
    ).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Restart gateway' }))
    await waitFor(() => expect(gatewayRestart).toHaveBeenCalled())
  })

  it('only tells an external gateway to restart on its host', async () => {
    useSettings.setState((s) => ({
      settings: { ...s.settings, gateway: { ...s.settings.gateway, mode: 'external' } },
    }))
    wire({ snapshot: { pendingRestart: true } })
    renderPane()
    await loaded()
    expect(
      await screen.findByText(
        'The gateway is running an older configuration. Restart it on its host to apply.',
      ),
    ).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Restart gateway' })).not.toBeInTheDocument()
  })

  it('sends an adapter without a guided form to config.toml', async () => {
    wire({ channels: [IRC], access: [] })
    renderPane()
    const irc = await screen.findByRole('region', { name: 'irc-old' })
    expect(within(irc).getByText('dead')).toBeInTheDocument()
    expect(within(irc).queryByRole('button', { name: 'Configure' })).toBeNull()
    fireEvent.click(within(irc).getByRole('button', { name: 'Edit config.toml' }))
    expect(openPath).toHaveBeenCalledWith('/tmp/agentos-home/.agentos/config.toml')
  })

  it('opens the adapter guide through the desktop bridge', async () => {
    renderPane()
    await loaded()
    fireEvent.click(screen.getByRole('button', { name: 'Add channel' }))
    const sheet = await screen.findByRole('dialog', { name: 'Add channel' })
    fireEvent.click(within(sheet).getByRole('button', { name: 'Setup guide' }))
    expect(openExternal).toHaveBeenCalledWith('https://example.test/telegram')
  })
})
