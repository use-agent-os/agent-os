import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, useLocation, useNavigate } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { SettingsPage } from './SettingsPage'

const mockRpc = {
  waitForConnection: vi.fn().mockResolvedValue(undefined),
  call: vi.fn(),
}

const { setupPropsSpy, configPropsSpy } = vi.hoisted(() => ({
  setupPropsSpy: vi.fn(),
  configPropsSpy: vi.fn(),
}))

vi.mock('@/app/providers', () => ({ useRpc: () => mockRpc }))
vi.mock('@/views/setup/SetupPage', () => ({
  SetupPage: (props: unknown) => {
    setupPropsSpy(props)
    return (
      <div>
        Guided agent controls
        <input aria-label="Guided draft" defaultValue="" />
      </div>
    )
  },
}))
vi.mock('@/views/config/ConfigPage', () => ({
  ConfigPage: (props: unknown) => {
    configPropsSpy(props)
    return (
      <div>
        Advanced configuration controls
        <input aria-label="Advanced draft" defaultValue="" />
      </div>
    )
  },
}))

const SNAPSHOT = {
  revision: 'sha256:1234567890abcdef',
  configPath: '/tmp/agentos.toml',
  config: { llm: { provider: 'openai', model: 'gpt-5' } },
  status: {
    sectionDetails: {
      llm: { status: 'ok', required: true },
      router: { status: 'missing', required: true },
    },
  },
}

function renderPage(path: string) {
  return render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <MemoryRouter initialEntries={[path]}>
        <SettingsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

function RouteDriver() {
  const navigate = useNavigate()
  return (
    <>
      <button type="button" onClick={() => navigate('/setup')}>
        Route setup
      </button>
      <button type="button" onClick={() => navigate('/config')}>
        Route config
      </button>
    </>
  )
}

function LocationProbe() {
  const location = useLocation()
  return <output aria-label="Current route">{location.pathname + location.search}</output>
}

describe('SettingsPage', () => {
  beforeEach(() => {
    mockRpc.call.mockReset().mockResolvedValue(SNAPSHOT)
    mockRpc.waitForConnection.mockReset().mockResolvedValue(undefined)
    setupPropsSpy.mockClear()
    configPropsSpy.mockClear()
  })

  it('opens /setup in Guided and loads one atomic agent snapshot', async () => {
    renderPage('/setup')

    expect(screen.getByText('Control · Settings')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Agent settings' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Refresh agent state' })).toHaveTextContent('Refresh')
    expect(screen.getByRole('tab', { name: /Guided/ })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tabpanel', { name: /Guided/ })).toBeVisible()

    await waitFor(() => expect(mockRpc.call).toHaveBeenCalledWith('config.snapshot'))
    expect(await screen.findByText('gpt-5')).toBeInTheDocument()
    expect(screen.getByText('1 setup item left')).toBeInTheDocument()
    expect(screen.getByText('1 of 2 ready')).toBeInTheDocument()
  })

  it('redirects legacy channel-step links to the unified Channels setup', async () => {
    render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <MemoryRouter initialEntries={['/setup?step=channels']}>
          <LocationProbe />
          <SettingsPage />
        </MemoryRouter>
      </QueryClientProvider>,
    )

    await waitFor(() =>
      expect(screen.getByLabelText('Current route')).toHaveTextContent('/channels?view=setup'),
    )
  })

  it('opens /config in Advanced and switches modes without remounting the workspace', () => {
    renderPage('/config')

    expect(screen.getByRole('tab', { name: /Advanced/ })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tabpanel', { name: /Advanced/ })).toBeVisible()

    fireEvent.click(screen.getByRole('tab', { name: /Guided/ }))
    expect(screen.getByRole('tabpanel', { name: /Guided/ })).toBeVisible()
    expect(screen.getByText('Advanced configuration controls')).toBeInTheDocument()
  })

  it('keeps both workspace drafts mounted while switching tabs locally', async () => {
    renderPage('/setup')
    const guidedDraft = (await screen.findByLabelText('Guided draft')) as HTMLInputElement
    fireEvent.change(guidedDraft, { target: { value: 'unsaved provider key' } })

    fireEvent.click(screen.getByRole('tab', { name: /Advanced/ }))
    fireEvent.change(screen.getByLabelText('Advanced draft'), { target: { value: 'yaml draft' } })
    fireEvent.click(screen.getByRole('tab', { name: /Guided/ }))

    expect(screen.getByLabelText('Guided draft')).toHaveValue('unsaved provider key')
    expect(screen.getByLabelText('Advanced draft')).toHaveValue('yaml draft')
  })

  it('passes the same authoritative snapshot to Guided and Advanced', async () => {
    renderPage('/setup')
    await screen.findByText('gpt-5')

    expect(setupPropsSpy.mock.calls.at(-1)?.[0].externalSnapshot).toBe(SNAPSHOT)
    expect(configPropsSpy.mock.calls.at(-1)?.[0].externalSnapshot).toBe(SNAPSHOT)
  })

  it('owns the single divergence warning and pauses changes', async () => {
    mockRpc.call.mockResolvedValue({
      ...SNAPSHOT,
      revision: undefined,
      diskDiverged: true,
      writeBlocked: true,
    })
    renderPage('/setup')

    expect(await screen.findByText('Changes paused')).toBeVisible()
    expect(screen.getAllByText(/config file changed outside AgentOS/i)).toHaveLength(1)
  })

  it('keeps legacy snapshot details out of the friendly workspace summary', async () => {
    mockRpc.call.mockResolvedValue({
      config: SNAPSHOT.config,
      status: SNAPSHOT.status,
    })
    renderPage('/setup')

    expect(await screen.findByText('gpt-5')).toBeVisible()
    expect(screen.getByText('openai')).toBeVisible()
    expect(screen.queryByText('Unversioned')).not.toBeInTheDocument()
    expect(screen.queryByText('Runtime configuration')).not.toBeInTheDocument()
  })

  it('falls back to legacy reads only for METHOD_NOT_FOUND', async () => {
    mockRpc.call.mockImplementation((method: string) => {
      if (method === 'config.snapshot') {
        return Promise.reject(Object.assign(new Error('missing'), { code: 'METHOD_NOT_FOUND' }))
      }
      if (method === 'onboarding.catalog') return Promise.resolve({ providers: [] })
      if (method === 'onboarding.status') return Promise.resolve(SNAPSHOT.status)
      if (method === 'config.get') return Promise.resolve(SNAPSHOT.config)
      return Promise.resolve({})
    })
    renderPage('/setup')

    await screen.findByText('gpt-5')
    expect(mockRpc.call).toHaveBeenCalledWith('onboarding.catalog')
    expect(mockRpc.call).toHaveBeenCalledWith('onboarding.status')
    expect(mockRpc.call).toHaveBeenCalledWith('config.get')
  })

  it('surfaces non-compatibility errors without legacy reads or an infinite setup loader', async () => {
    mockRpc.call.mockImplementation((method: string) => {
      if (method === 'config.snapshot') {
        return Promise.reject(Object.assign(new Error('forbidden'), { code: 'FORBIDDEN' }))
      }
      return Promise.resolve({})
    })
    renderPage('/setup')

    expect(await screen.findByText(/Agent state could not be loaded/i)).toBeVisible()
    expect(mockRpc.call).not.toHaveBeenCalledWith('onboarding.catalog')
    expect(mockRpc.call).not.toHaveBeenCalledWith('onboarding.status')
    expect(mockRpc.call).not.toHaveBeenCalledWith('config.get')
    expect(screen.queryByText('Loading setup…')).not.toBeInTheDocument()
  })

  it('synchronizes the selected tab when route aliases change externally', async () => {
    render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <MemoryRouter initialEntries={['/setup']}>
          <RouteDriver />
          <SettingsPage />
        </MemoryRouter>
      </QueryClientProvider>,
    )
    expect(screen.getByRole('tab', { name: /Guided/ })).toHaveAttribute('aria-selected', 'true')

    fireEvent.click(screen.getByRole('button', { name: 'Route config' }))
    await waitFor(() =>
      expect(screen.getByRole('tab', { name: /Advanced/ })).toHaveAttribute(
        'aria-selected',
        'true',
      ),
    )
  })

  it('supports arrow-key navigation on the Settings tablist', () => {
    renderPage('/setup')
    const guided = screen.getByRole('tab', { name: /Guided/ })
    fireEvent.keyDown(guided, { key: 'ArrowRight' })
    expect(screen.getByRole('tab', { name: /Advanced/ })).toHaveAttribute('aria-selected', 'true')
  })
})
