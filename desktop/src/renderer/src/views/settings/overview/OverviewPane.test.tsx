import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useConnection } from '@/stores/connection'
import type { SettingsSnapshot } from '@/views/settings/snapshot'
import { DEFAULT_SETTINGS } from '@shared/settings'
import { useGateway } from '~/stores/gateway'
import { useSettings } from '~/stores/settings'
import { useUi } from '~/stores/ui'
import { OverviewPane } from '../panes/OverviewPane'

const rpcCall = vi.fn()
vi.mock('@/app/providers', () => ({
  useRpc: () => ({ call: rpcCall, waitForConnection: async () => {}, on: () => () => {} }),
}))

const gatewayStart = vi.fn(async () => ({
  state: 'running',
  pid: 1,
  url: 'http://127.0.0.1:18791',
  error: null,
}))
vi.mock('~/lib/desktop-api', () => ({
  desktopApi: () => ({ gateway: { start: gatewayStart }, app: { openExternal: vi.fn() } }),
  isDesktop: () => true,
}))

const ok = { status: 'ok', actionRequired: false, blocking: false }
const optional = { status: 'optional', actionRequired: false, blocking: false }

function ready(): SettingsSnapshot {
  return {
    revision: 'r1',
    catalog: {
      providers: [{ providerId: 'surplus', label: 'Surplus Intelligence' }],
      searchProviders: [{ providerId: 'duckduckgo', label: 'DuckDuckGo' }],
    },
    config: {
      llm: { provider: 'surplus', model: 'gpt-6-luna' },
      agentos_router: { enabled: true, strategy: 'pilot-v1', tiers: { c0: {}, c1: {} } },
      memory: { embedding: { provider: 'auto' } },
      search_provider: 'duckduckgo',
      channels: { channels: [] },
    },
    status: {
      llmConfigured: true,
      llmSource: 'env',
      searchConfigured: true,
      imageGenerationEnabled: false,
      audioEnabled: false,
      channelCount: 0,
      sectionDetails: {
        llm: ok,
        router: ok,
        search: ok,
        memory_embedding: ok,
        channels: optional,
        image_generation: optional,
        audio: optional,
      },
    },
  }
}

function fresh(): SettingsSnapshot {
  const s = ready()
  s.config!.llm = { provider: '', model: '' }
  s.status = {
    ...s.status,
    llmConfigured: false,
    llmSource: 'none',
    sectionDetails: {
      ...s.status!.sectionDetails,
      llm: { status: 'missing', actionRequired: true, blocking: true, required: true },
    },
  }
  return s
}

let snapshot: SettingsSnapshot | Error = ready()

function mount() {
  return render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <OverviewPane />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  rpcCall.mockReset()
  gatewayStart.mockClear()
  snapshot = ready()
  useConnection.setState({ state: 'connected' })
  useGateway.setState({
    status: { state: 'running', pid: 1, url: 'http://127.0.0.1:18791', error: null },
    busy: false,
  })
  useSettings.setState({ settings: structuredClone(DEFAULT_SETTINGS), loaded: true })
  useUi.setState({ settingsSection: 'overview' })
  rpcCall.mockImplementation(async (method: string) => {
    if (method === 'config.snapshot') {
      if (snapshot instanceof Error) throw snapshot
      return snapshot
    }
    if (method === 'skills.list') return { skills: [] }
    if (method === 'env.list') return { vars: [], setCount: 0, totalCount: 0, shadowedCount: 0 }
    return {}
  })
})

describe('OverviewPane', () => {
  it('ready: says so, suggests the next optional step, and every row jumps', async () => {
    mount()
    expect(await screen.findByText('Your agent is ready')).toBeInTheDocument()
    expect(screen.getByText('All set')).toBeInTheDocument()
    const provider = await screen.findByTestId('setup-item-provider')
    expect(within(provider).getByText('Surplus Intelligence · gpt-6-luna')).toBeInTheDocument()
    expect(
      within(screen.getByTestId('setup-item-search')).getByText('DuckDuckGo'),
    ).toBeInTheDocument()

    // Image generation is off: its button lands on the Capabilities section.
    fireEvent.click(
      within(screen.getByTestId('setup-item-image')).getByRole('button', { name: /Set up/ }),
    )
    expect(useUi.getState().settingsSection).toBe('capabilities')
  })

  it('nothing configured: the hero leads to Providers and the pill counts it', async () => {
    snapshot = fresh()
    mount()
    expect(await screen.findByText('Your agent cannot answer yet')).toBeInTheDocument()
    expect(screen.getByText('1 thing to finish')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Add a provider key/ }))
    expect(useUi.getState().settingsSection).toBe('providers')
  })

  it('offline with a managed gateway: the hero starts it', async () => {
    useConnection.setState({ state: 'disconnected' })
    useGateway.setState({
      status: { state: 'stopped', pid: null, url: null, error: null },
      busy: false,
    })
    mount()
    expect(screen.getByText('The gateway is not running')).toBeInTheDocument()
    expect(screen.getByText('Gateway offline')).toBeInTheDocument()
    expect(
      screen.getByText('The rest of the checklist appears once the gateway is running.'),
    ).toBeInTheDocument()
    expect(screen.queryByTestId('setup-item-provider')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Start gateway' }))
    await waitFor(() => expect(gatewayStart).toHaveBeenCalled())
    expect(rpcCall).not.toHaveBeenCalledWith('config.snapshot', expect.anything())
  })

  it('a failed read offers Retry', async () => {
    snapshot = new Error('boom')
    mount()
    expect(
      await screen.findByText('Could not read the setup state from the gateway.'),
    ).toBeInTheDocument()
    snapshot = ready()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('Your agent is ready')).toBeInTheDocument()
  })

  it('a pending restart is said at the top', async () => {
    snapshot = { ...ready(), pendingRestart: true }
    mount()
    expect(
      await screen.findByText('The gateway is running an older configuration. Restart to apply.'),
    ).toBeInTheDocument()
  })
})
