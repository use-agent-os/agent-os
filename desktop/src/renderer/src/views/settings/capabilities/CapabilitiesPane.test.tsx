import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useConnection } from '@/stores/connection'
import type { SettingsSnapshot } from '@/views/settings/snapshot'
import { useUi } from '~/stores/ui'
import { sectionAttention } from '../attention'
import { takeFocus } from '../overview/jumps'
import { CapabilitiesPane, scrollBehavior } from '../panes/CapabilitiesPane'

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

/** X search as the gateway ships it: on, reading XAI_API_KEY. */
function withX(s: SettingsSnapshot): SettingsSnapshot {
  s.catalog!.xSearch = [
    {
      providerId: 'x_search',
      label: 'X (Twitter) Search',
      requiresApiKey: true,
      envKey: 'XAI_API_KEY',
      fields: [
        { name: 'api_key', label: 'API key', type: 'password', secret: true, required: true },
        { name: 'model', label: 'Model', type: 'text', default: 'grok-4.5' },
        { name: 'timeout_seconds', label: 'Timeout', type: 'int', default: 180 },
      ],
    },
  ]
  s.config!.x_search = { enabled: true, model: 'grok-4.5', api_key: '', api_key_env: 'XAI_API_KEY' }
  return s
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

  it('search on X on without a key: the strip, the card and the rail tell one story', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'config.snapshot') return withX(snapshot())
      if (method === 'env.list') return { vars: [{ name: 'XAI_API_KEY', isSet: false }] }
      if (method === 'auth.status') return { xai: { logged_in: false } }
      return {}
    })
    mount()
    const stats = await screen.findByRole('group', { name: 'Capabilities at a glance' })
    await envLoaded()
    const x = within(stats).getByRole('button', { name: /Search on X/ })
    await waitFor(() => expect(within(x).getByText('Key missing')).toBeInTheDocument())
    expect(within(x).getByText('Grok')).toBeInTheDocument()
    expect(within(card('Search on X')).getByText('Needs a key')).toBeInTheDocument()
    expect(sectionAttention(withX(snapshot()), { envSet: () => false, xaiLogin: false })).toEqual({
      capabilities: 'warn',
      overview: 'warn',
    })
  })

  it('results per search outside 1 to 20 is refused at the field', async () => {
    mount()
    await screen.findByRole('region', { name: 'Web search' })
    const search = card('Web search')
    const input = within(search).getByLabelText('Results per search')
    fireEvent.change(input, { target: { value: '500' } })
    expect(
      within(search).getByText('Results per search must be between 1 and 20.'),
    ).toBeInTheDocument()
    expect(input).toHaveAttribute('aria-invalid', 'true')
    expect(within(search).getByRole('button', { name: 'Save' })).toBeDisabled()
    fireEvent.change(input, { target: { value: '20' } })
    expect(within(search).queryByText(/must be between/)).toBeNull()
    expect(within(search).getByRole('button', { name: 'Save' })).toBeEnabled()
  })

  it('X timeouts outside the engine bounds are said at the field, in seconds', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'config.snapshot') return withX(snapshot())
      if (method === 'env.list') return { vars: [{ name: 'XAI_API_KEY', isSet: true }] }
      if (method === 'auth.status') return { xai: {} }
      return {}
    })
    mount()
    const x = await screen.findByRole('region', { name: 'Search on X' })
    await envLoaded()
    fireEvent.click(within(x).getByRole('button', { name: /Advanced/ }))
    fireEvent.change(within(x).getByLabelText('Time per attempt (seconds)'), {
      target: { value: '10' },
    })
    // Under Advanced, so the foot says it too.
    expect(
      within(x).getAllByText('Time per attempt must be between 30 and 300 seconds.'),
    ).toHaveLength(2)
    expect(within(x).getByRole('button', { name: 'Save' })).toBeDisabled()
  })

  it('Open Environment lands on the variable the key is read from', async () => {
    useUi.setState({ settingsSection: 'capabilities' })
    mount()
    await screen.findByRole('region', { name: 'Web search' })
    await envLoaded()
    const search = card('Web search')
    fireEvent.click(within(search).getByRole('radio', { name: /Brave Search/ }))
    fireEvent.click(within(search).getByRole('button', { name: 'Open Environment' }))
    expect(useUi.getState().settingsSection).toBe('environment')
    expect(takeFocus('environment')).toBe('BRAVE_SEARCH_API_KEY')
  })

  it('a jump scrolls smoothly, or at once for reduced motion or a hidden window', () => {
    const original = window.matchMedia
    const visibility = vi.spyOn(document, 'visibilityState', 'get')
    const media = (matches: boolean) =>
      vi.fn(() => ({ matches }) as unknown as MediaQueryList) as unknown as typeof window.matchMedia
    try {
      window.matchMedia = media(false)
      expect(scrollBehavior()).toBe('smooth')
      window.matchMedia = media(true)
      expect(scrollBehavior()).toBe('instant')
      // A hidden window gets no animation frames: a smooth scroll would never land.
      window.matchMedia = media(false)
      visibility.mockReturnValue('hidden')
      expect(scrollBehavior()).toBe('instant')
    } finally {
      window.matchMedia = original
      visibility.mockRestore()
    }
  })

  it('offline: says so and reads nothing', () => {
    useConnection.setState({ state: 'disconnected' })
    mount()
    expect(screen.getByText('Start the gateway to configure this.')).toBeInTheDocument()
    expect(rpcCall).not.toHaveBeenCalled()
  })
})
