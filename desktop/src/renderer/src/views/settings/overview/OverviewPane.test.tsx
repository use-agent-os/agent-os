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
import { takeFocus } from './jumps'

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

/** X search on, as the gateway ships it, reading XAI_API_KEY. */
function withX(s: SettingsSnapshot): SettingsSnapshot {
  s.catalog!.xSearch = [
    {
      providerId: 'x_search',
      label: 'X (Twitter) Search',
      requiresApiKey: true,
      envKey: 'XAI_API_KEY',
    },
  ]
  s.config!.x_search = { enabled: true, model: 'grok-4.5', api_key: '', api_key_env: 'XAI_API_KEY' }
  return s
}

let snapshot: SettingsSnapshot | Error = ready()
let login = false

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
  login = false
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
    if (method === 'env.list') {
      return {
        vars: [{ name: 'XAI_API_KEY', isSet: false, missing: false }],
        setCount: 0,
        totalCount: 1,
        shadowedCount: 0,
      }
    }
    if (method === 'auth.status') return { xai: { logged_in: login } }
    return {}
  })
})

describe('OverviewPane', () => {
  it('ready: says so, suggests the next optional step, and every row jumps', async () => {
    mount()
    expect(await screen.findByText('Your agent is ready')).toBeInTheDocument()
    // Not "All set": image, voice and channels are still open, and skippable.
    expect(screen.getByText('Required setup done · 3 optional')).toBeInTheDocument()
    expect(screen.queryByText('All set')).toBeNull()
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
    expect(screen.getByText('1 required step left')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Add a provider key/ }))
    expect(useUi.getState().settingsSection).toBe('providers')
  })

  it('search on X switched on without a key is one thing to fix, not "All set"', async () => {
    snapshot = withX(ready())
    mount()
    expect(await screen.findByText('Search on X needs attention')).toBeInTheDocument()
    expect(screen.getByText('Required setup done · 1 to fix')).toBeInTheDocument()
    const row = screen.getByTestId('setup-item-x')
    expect(
      within(row).getByText('On, but XAI_API_KEY is not set and there is no SuperGrok login'),
    ).toBeInTheDocument()
    // Recommended, not required.
    expect(
      within(screen.getByRole('region', { name: 'Recommended' })).getByTestId('setup-item-x'),
    ).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Fix search on X/ }))
    expect(useUi.getState().settingsSection).toBe('capabilities')
    expect(takeFocus('capabilities')).toBe('x')
  })

  it('search on X with a SuperGrok login is done', async () => {
    snapshot = withX(ready())
    login = true
    mount()
    const row = await screen.findByTestId('setup-item-x')
    await waitFor(() =>
      expect(within(row).getByText('grok-4.5 · SuperGrok login')).toBeInTheDocument(),
    )
    expect(screen.getByText('Your agent is ready')).toBeInTheDocument()
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
