import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ProviderSpec } from '@/views/setup/logic'
import { ProviderForm } from './ProvidersPane'

const rpcCall = vi.fn()
vi.mock('@/app/providers', () => ({
  useRpc: () => ({ call: rpcCall, waitForConnection: async () => {} }),
}))

const openExternal = vi.fn(async () => {})
vi.mock('~/lib/desktop-api', () => ({
  desktopApi: () => ({ app: { openExternal } }),
  isDesktop: () => true,
}))

const opencap: ProviderSpec = {
  providerId: 'opencap',
  label: 'OpenCAP',
  runtimeSupported: true,
  routerSupported: true,
  requiresApiKey: true,
  envKey: 'OPENCAP_API_KEY',
  deployment: 'cloud',
  fields: [],
}

const openai: ProviderSpec = { ...opencap, providerId: 'openai', label: 'OpenAI' }

function renderForm(spec: ProviderSpec = opencap, onSave = vi.fn(), submitLabel?: string) {
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <ProviderForm
        config={{ llm: {} }}
        spec={spec}
        configured=""
        saving={false}
        disabled={false}
        onSave={onSave}
        submitLabel={submitLabel}
      />
    </QueryClientProvider>,
  )
  return onSave
}

beforeEach(() => {
  rpcCall.mockReset()
  openExternal.mockClear()
  rpcCall.mockImplementation(async (method: string) => {
    if (method === 'models.list') return []
    if (method === 'providers.probe') {
      return {
        ok: true,
        models: [
          { id: 'gpt-5.6-luna', name: 'GPT-5.6 Luna' },
          { id: 'deepseek-v4-flash', name: 'DeepSeek V4 Flash' },
          { id: 'claude-fable-5.1', name: 'Claude Fable 5.1' },
        ],
        model: 'gpt-5.6-luna',
        latencyMs: 812,
        error: null,
      }
    }
    return {}
  })
})

describe('ProviderForm · Test key', () => {
  it('tries the typed key against the provider and fills the model menu from its list', async () => {
    renderForm()
    const button = screen.getByTestId('key-probe-button')
    // Nothing typed yet: nothing to try.
    expect(button).toBeDisabled()

    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'sk-test-123' } })
    expect(button).not.toBeDisabled()
    fireEvent.click(button)

    await waitFor(() =>
      expect(screen.getByTestId('key-probe')).toHaveAttribute('data-verdict', 'ok'),
    )
    const probeCall = rpcCall.mock.calls.find((c) => c[0] === 'providers.probe')
    expect(probeCall?.[1]).toMatchObject({ providerId: 'opencap', apiKey: 'sk-test-123' })
    expect(screen.getByTestId('key-probe')).toHaveTextContent('Key works. 3 models available.')

    const select = screen.getByLabelText('Default model') as HTMLSelectElement
    const ids = [...select.options].map((o) => o.value)
    expect(ids).toEqual(['', 'gpt-5.6-luna', 'deepseek-v4-flash', 'claude-fable-5.1'])
    expect(screen.getByText(/Fetched from the provider just now/)).toBeInTheDocument()
  })

  it('shows the provider’s rejection verbatim', async () => {
    rpcCall.mockImplementation(async (method: string) =>
      method === 'providers.probe'
        ? { ok: false, models: [], error: 'unauthorized: Invalid API key', latencyMs: 300 }
        : [],
    )
    renderForm()
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'sk-bad' } })
    fireEvent.click(screen.getByTestId('key-probe-button'))
    await waitFor(() =>
      expect(screen.getByTestId('key-probe')).toHaveAttribute('data-verdict', 'bad'),
    )
    expect(screen.getByTestId('key-probe')).toHaveTextContent('Invalid API key')
  })

  it('links to the page where the key is issued when one is known', () => {
    renderForm(openai)
    fireEvent.click(screen.getByRole('button', { name: /Get an API key/ }))
    expect(openExternal).toHaveBeenCalledWith('https://platform.openai.com/api-keys')
  })

  it('uses the caller’s primary label', () => {
    renderForm(opencap, vi.fn(), 'Save and continue')
    expect(screen.getByRole('button', { name: 'Save and continue' })).toBeInTheDocument()
  })
})
