import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useConnection } from '@/stores/connection'
import { useBootstrap } from '~/stores/bootstrap'
import { useGateway } from '~/stores/gateway'
import { ProviderStep } from './ProviderStep'

const rpcCall = vi.fn()
vi.mock('@/app/providers', () => ({
  useRpc: () => ({ call: rpcCall, waitForConnection: async () => {} }),
}))
vi.mock('~/views/settings/panes/ProvidersPane', () => ({
  ProviderForm: ({
    spec,
    onSave,
  }: {
    spec: { providerId: string }
    onSave: (draft: {
      providerId: string
      model: string
      apiKey: string
      apiKeyEnv: string
      baseUrl: string
      proxy: string
    }) => void
  }) => (
    <button
      type="button"
      data-testid="provider-form"
      onClick={() =>
        onSave({
          providerId: spec.providerId,
          model: 'm',
          apiKey: 'sk-1',
          apiKeyEnv: '',
          baseUrl: '',
          proxy: '',
        })
      }
    >
      form for {spec.providerId}
    </button>
  ),
}))

const snapshot = {
  revision: 'r1',
  config: { llm: {} },
  status: { hasConfig: false },
  catalog: {
    providers: [
      {
        providerId: 'openai',
        label: 'OpenAI',
        runtimeSupported: true,
        routerSupported: true,
        deployment: 'cloud',
        fields: [],
      },
      {
        providerId: 'opencap',
        label: 'OpenCAP',
        runtimeSupported: true,
        routerSupported: true,
        deployment: 'cloud',
        fields: [],
      },
      {
        providerId: 'ollama',
        label: 'Ollama (local)',
        runtimeSupported: true,
        routerSupported: false,
        deployment: 'local',
        fields: [],
      },
    ],
  },
}

let probeResult: unknown = {
  ok: true,
  models: Array.from({ length: 12 }, (_, i) => ({ id: `m${i}`, name: `m${i}` })),
  model: 'm0',
  latencyMs: 500,
  error: null,
}

function renderStep(onDone = vi.fn()) {
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <ProviderStep onDone={onDone} />
    </QueryClientProvider>,
  )
  return onDone
}

beforeEach(() => {
  rpcCall.mockReset()
  rpcCall.mockImplementation(async (method: string) => {
    if (method === 'config.snapshot') return snapshot
    if (method === 'onboarding.provider.configure') return { restartRequired: true }
    if (method === 'providers.probe') return probeResult
    return {}
  })
  useConnection.getState().setState('connected')
  useBootstrap.setState({ provider: { selected: null, saved: null } })
  useGateway.setState({
    status: { state: 'running', pid: 42, url: 'http://127.0.0.1:18791', error: null },
    restart: vi.fn(async () => {}),
  })
})

describe('ProviderStep', () => {
  it('lists providers with the recommended one first, and Skip hands over', async () => {
    const onDone = renderStep()
    const grid = await screen.findByTestId('setup-provider-grid')
    const names = [...grid.querySelectorAll('.setup__tile-name')].map((n) => n.textContent)
    expect(names.slice(0, 3)).toEqual(['OpenCAP', 'OpenAI', 'Ollama (local)'])
    expect(screen.getByText('Recommended')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Skip for now' }))
    expect(onDone).toHaveBeenCalledTimes(1)
  })

  it('opens one provider on its own screen, saves, restarts the managed gateway, then says all set', async () => {
    const onDone = renderStep()
    await screen.findByTestId('setup-provider-grid')
    fireEvent.click(screen.getByTestId('setup-tile-opencap'))
    expect(screen.getByTestId('setup-provider-form')).toHaveTextContent('form for opencap')
    expect(screen.queryByTestId('setup-provider-grid')).toBeNull()

    fireEvent.click(screen.getByTestId('provider-form'))
    await waitFor(() => expect(screen.getByTestId('setup-ready')).toBeInTheDocument())
    expect(rpcCall).toHaveBeenCalledWith(
      'onboarding.provider.configure',
      expect.objectContaining({ providerId: 'opencap', apiKey: 'sk-1', expectedRevision: 'r1' }),
    )
    expect(useGateway.getState().restart).toHaveBeenCalledTimes(1)
    expect(screen.getByText('OpenCAP')).toBeInTheDocument()
    // The key is tried for real: providers.status probes the model list.
    await waitFor(() =>
      expect(screen.getByTestId('setup-key-verdict')).toHaveAttribute('data-verdict', 'ok'),
    )
    expect(rpcCall).toHaveBeenCalledWith('providers.probe', { providerId: 'opencap' })
    expect(screen.getByTestId('setup-key-verdict')).toHaveTextContent('12 models available')
    fireEvent.click(screen.getByRole('button', { name: 'Start chatting' }))
    expect(onDone).toHaveBeenCalledTimes(1)
  })

  it('goes back to the grid from the form', async () => {
    renderStep()
    await screen.findByTestId('setup-provider-grid')
    fireEvent.click(screen.getByTestId('setup-tile-openai'))
    fireEvent.click(screen.getByRole('button', { name: 'All providers' }))
    expect(screen.getByTestId('setup-provider-grid')).toBeInTheDocument()
  })
})

describe('ProviderStep across a gateway restart', () => {
  it('comes back on "all set" after the step remounts mid-save', async () => {
    // Saving restarts the gateway; the connection drops and the step
    // unmounts. The saved provider lives in the store, so a fresh mount must
    // land on the ready screen, not the grid.
    useBootstrap.setState({ provider: { selected: null, saved: 'opencap' } })
    const onDone = renderStep()
    expect(await screen.findByTestId('setup-ready')).toBeInTheDocument()
    expect(screen.queryByTestId('setup-provider-grid')).toBeNull()
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Start chatting' })).not.toBeDisabled(),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Start chatting' }))
    expect(onDone).toHaveBeenCalledTimes(1)
  })
})

describe('ProviderStep key verification', () => {
  it('flags a rejected key and offers to edit it', async () => {
    probeResult = { ok: false, models: [], model: 'm0', latencyMs: 300, error: '401 Unauthorized' }
    useBootstrap.setState({ provider: { selected: null, saved: 'opencap' } })
    renderStep()
    await waitFor(() =>
      expect(screen.getByTestId('setup-key-verdict')).toHaveAttribute('data-verdict', 'bad'),
    )
    expect(screen.getByTestId('setup-key-verdict')).toHaveTextContent('401 Unauthorized')
    expect(screen.getByRole('button', { name: 'Continue anyway' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Edit key' }))
    expect(await screen.findByTestId('setup-provider-form')).toHaveTextContent('form for opencap')
  })
})
