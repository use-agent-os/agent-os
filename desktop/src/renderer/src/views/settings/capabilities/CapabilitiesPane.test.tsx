import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useConnection } from '@/stores/connection'
import type { SettingsSnapshot } from '@/views/settings/snapshot'
import { CapabilitiesPane } from '../panes/CapabilitiesPane'

const rpcCall = vi.fn()
vi.mock('@/app/providers', () => ({
  useRpc: () => ({ call: rpcCall, waitForConnection: async () => {}, on: () => () => {} }),
}))
const openExternal = vi.fn(async () => {})
vi.mock('~/lib/desktop-api', () => ({
  desktopApi: () => ({ app: { openExternal } }),
  isDesktop: () => true,
}))

const SEARCH_FIELDS = [
  { name: 'api_key', label: 'API key', type: 'password', secret: true, required: true },
  { name: 'max_results', label: 'Max results', type: 'int', default: 5 },
  { name: 'proxy', label: 'HTTP proxy', type: 'text', default: '' },
]
const IMAGE_FIELDS = [
  { name: 'enabled', type: 'bool', default: true },
  { name: 'primary', label: 'Primary model', type: 'text', required: true },
  { name: 'api_key', label: 'API key', type: 'password', secret: true },
  { name: 'base_url', label: 'Base URL', type: 'text' },
]

function snapshot(): SettingsSnapshot {
  return {
    revision: 'rev-7',
    catalog: {
      searchProviders: [
        {
          providerId: 'brave',
          label: 'Brave Search',
          requiresApiKey: true,
          envKey: 'BRAVE_SEARCH_API_KEY',
          fields: SEARCH_FIELDS,
        },
        {
          providerId: 'duckduckgo',
          label: 'DuckDuckGo',
          requiresApiKey: false,
          fields: SEARCH_FIELDS,
        },
      ],
      imageGenerationProviders: [
        {
          providerId: 'openai',
          label: 'OpenAI Images',
          requiresApiKey: true,
          envKey: 'OPENAI_API_KEY',
          defaultModel: 'openai/gpt-image-1',
          fields: IMAGE_FIELDS,
        },
        {
          providerId: 'openrouter',
          label: 'OpenRouter Images',
          requiresApiKey: true,
          envKey: 'OPENROUTER_API_KEY',
          defaultModel: 'openrouter/google/gemini-image',
          fields: IMAGE_FIELDS,
        },
      ],
      audioProviders: [],
    },
    config: {
      search_provider: 'duckduckgo',
      search_max_results: 5,
      image_generation: {
        enabled: false,
        primary: 'openai/gpt-image-1',
        providers: {
          openai: { api_key: '', api_key_env: 'OPENAI_API_KEY' },
          openrouter: { api_key: '', api_key_env: 'OPENROUTER_API_KEY' },
        },
      },
      audio: { enabled: false },
    },
    status: {
      searchConfigured: true,
      searchSource: 'not_required',
      imageGenerationEnabled: false,
      audioEnabled: false,
    },
  }
}

function mount() {
  return render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <CapabilitiesPane />
    </QueryClientProvider>,
  )
}

function card(name: string) {
  return screen.getByRole('region', { name })
}

/** The env listing is what the key notes are judged against: wait for it. */
async function envLoaded() {
  await waitFor(() => expect(rpcCall).toHaveBeenCalledWith('env.list', {}))
  await act(async () => {
    await new Promise((r) => setTimeout(r, 0))
  })
}

beforeEach(() => {
  rpcCall.mockReset()
  openExternal.mockClear()
  useConnection.setState({ state: 'connected' })
  rpcCall.mockImplementation(async (method: string) => {
    if (method === 'config.snapshot') return snapshot()
    if (method === 'env.list') {
      return {
        vars: [
          { name: 'OPENROUTER_API_KEY', isSet: true, url: '' },
          { name: 'BRAVE_SEARCH_API_KEY', isSet: false, url: 'https://brave.com/search/api/' },
        ],
      }
    }
    if (method === 'auth.status') return { xai: { logged_in: false } }
    return { restartRequired: false }
  })
})

describe('CapabilitiesPane', () => {
  it('shows what is on at a glance', async () => {
    mount()
    const stats = await screen.findByRole('group', { name: 'Capabilities at a glance' })
    expect(within(stats).getByText('DuckDuckGo')).toBeInTheDocument()
    expect(within(stats).getByText('No key needed')).toBeInTheDocument()
    expect(within(stats).getAllByText('Off')).toHaveLength(2)
  })

  it('a keyed search service waits for a key, then saves with the revision', async () => {
    mount()
    await screen.findByRole('region', { name: 'Web search' })
    await envLoaded()
    const search = card('Web search')
    fireEvent.click(within(search).getByRole('radio', { name: /Brave Search/ }))
    expect(
      within(search).getByText('Paste a key, or set BRAVE_SEARCH_API_KEY in Environment.'),
    ).toBeInTheDocument()
    expect(within(search).getByRole('button', { name: 'Save' })).toBeDisabled()

    // The gateway's own link for the variable.
    fireEvent.click(within(search).getByRole('button', { name: /Get a key/ }))
    expect(openExternal).toHaveBeenCalledWith('https://brave.com/search/api/')

    fireEvent.change(within(search).getByLabelText('API key'), {
      target: { value: 'sk-brave' },
    })
    fireEvent.click(within(search).getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith(
        'onboarding.search.configure',
        expect.objectContaining({
          providerId: 'brave',
          apiKey: 'sk-brave',
          maxResults: 5,
          expectedRevision: 'rev-7',
        }),
      ),
    )
  })

  it('turning images on picks the service whose key is already set', async () => {
    mount()
    await screen.findByRole('region', { name: 'Image generation' })
    await envLoaded()
    const image = card('Image generation')
    expect(within(image).queryByRole('radiogroup')).toBeNull()
    fireEvent.click(within(image).getByRole('switch'))
    await waitFor(() =>
      expect(within(image).getByRole('radio', { name: /OpenRouter Images/ })).toHaveAttribute(
        'aria-checked',
        'true',
      ),
    )
    expect(
      within(image).getByText('OPENROUTER_API_KEY is already set, so you can leave this empty.'),
    ).toBeInTheDocument()
    fireEvent.click(within(image).getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith(
        'onboarding.imageGeneration.configure',
        expect.objectContaining({
          providerId: 'openrouter',
          enabled: true,
          primary: 'openrouter/google/gemini-image',
          apiKeyEnv: 'OPENROUTER_API_KEY',
          expectedRevision: 'rev-7',
        }),
      ),
    )
  })

  it('a gateway refusal is shown on the card', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'config.snapshot') return snapshot()
      if (method === 'env.list') return { vars: [] }
      if (method === 'auth.status') return { xai: {} }
      if (method === 'onboarding.search.configure') throw new Error('max_results must be >= 1')
      return {}
    })
    mount()
    await screen.findByRole('region', { name: 'Web search' })
    const search = card('Web search')
    fireEvent.change(within(search).getByLabelText('Results per search'), {
      target: { value: '7' },
    })
    fireEvent.click(within(search).getByRole('button', { name: 'Save' }))
    expect(await within(search).findByText('max_results must be >= 1')).toBeInTheDocument()
  })

  it('offline: says so and reads nothing', () => {
    useConnection.setState({ state: 'disconnected' })
    mount()
    expect(screen.getByText('Start the gateway to configure this.')).toBeInTheDocument()
    expect(rpcCall).not.toHaveBeenCalled()
  })
})
