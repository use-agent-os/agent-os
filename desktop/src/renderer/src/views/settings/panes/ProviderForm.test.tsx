import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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
    expect(screen.getByText('3 models, fetched from the provider just now.')).toBeInTheDocument()
  })

  it('keeps the model menu usable when the probe answers before the catalog does', async () => {
    // A large catalog (Surplus: 500+ models) can still be in flight when the
    // key probe returns its own list; the menu must not stay locked on it.
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'models.list') return new Promise(() => {})
      if (method === 'providers.probe') {
        return { ok: true, models: [{ id: 'glm-5.3' }], model: 'glm-5.3', error: null }
      }
      return {}
    })
    renderForm()
    const select = screen.getByLabelText('Default model') as HTMLSelectElement
    expect(select).toBeDisabled()

    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'sk-test-123' } })
    fireEvent.click(screen.getByTestId('key-probe-button'))

    await waitFor(() => expect(select).not.toBeDisabled())
    expect([...select.options].map((o) => o.value)).toEqual(['', 'glm-5.3'])
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
    const failure = screen.getByTestId('key-probe')
    // One human sentence first, then the provider's own words, verbatim.
    expect(failure).toHaveTextContent('The provider did not accept this key.')
    expect(within(failure).getByText('unauthorized: Invalid API key')).toBeInTheDocument()
    // Short enough to show whole: nothing to unfold.
    expect(within(failure).queryByRole('button', { name: 'Show more' })).toBeNull()
  })

  it('folds a long rejection to three lines behind Show more', async () => {
    const raw =
      "403: Surplus Intelligence chat request failed (HTTP 403): This key is not scoped to use model 'gpt-6-luna'. Unusual bug? Email support@example.test with request id 01M7VDEVSZ4JK1H4JJ7QCBN4WZ and a description of what happened."
    rpcCall.mockImplementation(async (method: string) =>
      method === 'providers.probe' ? { ok: false, models: [], error: raw } : [],
    )
    renderForm()
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'sk-scoped' } })
    fireEvent.click(screen.getByTestId('key-probe-button'))
    const failure = await screen.findByTestId('key-probe')
    expect(failure).toHaveTextContent('The key is valid but not allowed to use this model.')
    const message = within(failure).getByText(raw)
    expect(message).toHaveAttribute('data-clamped', 'true')
    fireEvent.click(within(failure).getByRole('button', { name: 'Show more' }))
    expect(message).not.toHaveAttribute('data-clamped')
    expect(within(failure).getByRole('button', { name: 'Show less' })).toHaveAttribute(
      'aria-expanded',
      'true',
    )
  })

  it('tries the key the gateway reads from its environment the moment the form opens', async () => {
    rpcCall.mockImplementation(async (method: string, params?: { provider?: string }) => {
      if (method === 'providers.status') {
        return {
          providers: [
            { providerId: params?.provider, apiKeyEnv: 'OPENCAP_API_KEY', apiKeyConfigured: true },
          ],
        }
      }
      if (method === 'providers.probe') {
        return { ok: true, models: [{ id: 'gpt-5.6-terra' }], error: null, latencyMs: 90 }
      }
      return []
    })
    renderForm(openai)

    await waitFor(() =>
      expect(screen.getByTestId('key-probe')).toHaveAttribute('data-verdict', 'ok'),
    )
    const probeCall = rpcCall.mock.calls.find((c) => c[0] === 'providers.probe')
    expect(probeCall?.[1]).toMatchObject({ providerId: 'openai', apiKeyEnv: 'OPENCAP_API_KEY' })
    expect(probeCall?.[1].apiKey).toBeUndefined()
    expect(screen.getByTestId('key-probe-button')).not.toBeDisabled()
  })

  it('does not call out when the gateway has no key for the provider', async () => {
    rpcCall.mockImplementation(async (method: string, params?: { provider?: string }) =>
      method === 'providers.status'
        ? {
            providers: [
              {
                providerId: params?.provider,
                apiKeyEnv: 'OPENCAP_API_KEY',
                apiKeyConfigured: false,
              },
            ],
          }
        : [],
    )
    renderForm(openai)
    await waitFor(() =>
      expect(rpcCall.mock.calls.some((c) => c[0] === 'providers.status')).toBe(true),
    )
    expect(rpcCall.mock.calls.some((c) => c[0] === 'providers.probe')).toBe(false)
    expect(screen.getByTestId('key-probe-button')).toBeDisabled()
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
