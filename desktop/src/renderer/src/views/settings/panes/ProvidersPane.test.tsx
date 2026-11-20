import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useConnection } from '@/stores/connection'
import { ProvidersPane } from './ProvidersPane'

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), warning: vi.fn(), error: vi.fn(), info: vi.fn() },
}))

const rpcCall = vi.fn()
vi.mock('@/app/providers', () => ({
  useRpc: () => ({ call: rpcCall, waitForConnection: async () => {}, on: () => () => {} }),
}))
vi.mock('~/lib/desktop-api', () => ({
  desktopApi: () => ({ app: { openExternal: vi.fn() } }),
  isDesktop: () => true,
}))

function spec(providerId: string, label: string, extra: Record<string, unknown> = {}) {
  return {
    providerId,
    label,
    runtimeSupported: true,
    routerSupported: true,
    requiresApiKey: true,
    deployment: 'cloud',
    fields: [],
    ...extra,
  }
}

const CATALOG = {
  providers: [
    spec('openai', 'OpenAI'),
    spec('opencap', 'OpenCAP'),
    spec('surplus', 'Surplus Intelligence'),
    spec('ollama', 'Ollama (local)', { requiresApiKey: false, deployment: 'local' }),
  ],
}

function wire(config: Record<string, unknown>) {
  rpcCall.mockImplementation(async (method: string) => {
    if (method === 'config.snapshot') {
      return { revision: 'r1', catalog: CATALOG, config, status: { hasConfig: true } }
    }
    if (method === 'models.list') return []
    if (method === 'providers.status') return { providers: [] }
    if (method === 'providers.probe') return { ok: true, models: [], error: null }
    return {}
  })
}

function renderPane() {
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <ProvidersPane />
    </QueryClientProvider>,
  )
}

/** The names of the provider tiles in the catalog, in order. */
function tiles(): string[] {
  const list = screen.getByRole('list', { name: 'Providers' })
  return within(list)
    .getAllByRole('button')
    .map((b) => b.querySelector('.prov-tile__name')?.textContent ?? '')
}

beforeEach(() => {
  rpcCall.mockReset()
  useConnection.setState({ state: 'connected' })
})

describe('ProvidersPane', () => {
  it('leads with the active provider, then offers the rest to switch to', async () => {
    wire({ llm: { provider: 'surplus', model: 'gpt-6-luna', api_key: '[redacted]' } })
    renderPane()
    const active = await screen.findByRole('region', { name: 'Surplus Intelligence' })
    const switcher = screen.getByRole('region', { name: 'Switch provider' })
    // The active card comes before the catalog in the page.
    expect(active.compareDocumentPosition(switcher) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(within(active).getByText('Active')).toBeInTheDocument()
    // The active provider is not offered again; the recommended one leads.
    expect(tiles()).toEqual(['OpenCAP', 'OpenAI', 'Ollama (local)', 'Custom endpoint'])
  })

  it('filters the catalog and says when nothing matches', async () => {
    wire({ llm: { provider: 'surplus', model: 'gpt-6-luna' } })
    renderPane()
    const filter = await screen.findByRole('searchbox', { name: 'Filter providers' })
    fireEvent.change(filter, { target: { value: 'open' } })
    expect(tiles()).toEqual(['OpenCAP', 'OpenAI'])
    fireEvent.change(filter, { target: { value: 'nothing like it' } })
    expect(screen.getByText('No provider matches')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Clear filter' }))
    expect(tiles()).toHaveLength(4)
  })

  it('opens the picked provider’s setup in place of the grid, with a way back', async () => {
    wire({ llm: { provider: 'surplus', model: 'gpt-6-luna' } })
    renderPane()
    fireEvent.click(await screen.findByTestId('provider-tile-openai'))
    const form = screen.getByRole('region', { name: 'OpenAI' })
    expect(within(form).getByRole('button', { name: 'Switch to OpenAI' })).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Switch provider' })).toBeNull()
    // The active provider's card stays on top while another is being set up.
    expect(screen.getByRole('region', { name: 'Surplus Intelligence' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'All providers' }))
    expect(screen.getByRole('region', { name: 'Switch provider' })).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'OpenAI' })).toBeNull()
  })

  it('starts with the catalog on a first run, the recommended provider marked', async () => {
    wire({ llm: {} })
    renderPane()
    const chooser = await screen.findByRole('region', { name: 'Choose a provider' })
    expect(within(chooser).getByTestId('provider-tile-opencap')).toHaveAttribute(
      'data-recommended',
      'true',
    )
    expect(within(chooser).getByTestId('provider-tile-openai')).not.toHaveAttribute(
      'data-recommended',
    )
    expect(tiles()[0]).toBe('OpenCAP')
    // Nothing is set up yet, so there is no thinking level to tune.
    expect(screen.queryByLabelText('Thinking')).toBeNull()
  })
})
