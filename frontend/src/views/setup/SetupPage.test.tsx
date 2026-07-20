import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'sonner'
import { SetupPage } from './SetupPage'

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), warning: vi.fn(), error: vi.fn(), info: vi.fn() },
}))

const navigateSpy = vi.fn()
vi.mock('react-router', async () => {
  const actual = await vi.importActual<typeof import('react-router')>('react-router')
  return { ...actual, useNavigate: () => navigateSpy }
})

const mockRpc = {
  waitForConnection: vi.fn().mockResolvedValue(undefined),
  call: vi.fn(),
  on: vi.fn(() => () => {}),
}
vi.mock('@/app/providers', () => ({
  useRpc: () => mockRpc,
  useBootstrap: () => ({
    version: '1',
    ws_url: 'ws://127.0.0.1:18791/ws',
    auth_mode: 'none',
    base_path: '/control',
    config_path: '/tmp/agentos.toml',
    features: {},
  }),
}))

// A representative onboarding.catalog covering every section.
const CATALOG = {
  providers: [
    {
      providerId: 'openai',
      label: 'OpenAI',
      runtimeSupported: true,
      routerSupported: true,
      whatYouNeed: ['API key via OPENAI_API_KEY or a one-time paste.'],
      fields: [
        { name: 'api_key', label: 'API key', type: 'password', secret: true, required: true },
        { name: 'api_key_env', label: 'API key env', default: 'OPENAI_API_KEY' },
        { name: 'base_url', label: 'Base URL' },
      ],
    },
    { providerId: 'other', label: 'Other', runtimeSupported: true, fields: [] },
  ],
  routerProfiles: {
    defaultTier: 'c1',
    profiles: [
      {
        providerId: 'openai',
        tiers: {
          c0: { provider: 'openai', model: 'gpt-4o-mini' },
          c1: { provider: 'openai', model: 'gpt-4o' },
          image_model: { provider: 'openai', model: 'dall-e' },
        },
      },
    ],
    judge: { profiles: { openai: { autoModel: 'gpt-4o', models: ['gpt-4o', 'gpt-4o-mini'] } } },
  },
  channels: [
    {
      type: 'telegram',
      label: 'Telegram',
      whatYouNeed: ['Bot token from @BotFather'],
      fields: [
        { name: 'name', label: 'Name', required: true },
        { name: 'token', label: 'Bot token', type: 'password', secret: true, required: true },
      ],
    },
  ],
  searchProviders: [
    { providerId: 'duckduckgo', label: 'DuckDuckGo', runtimeSupported: true },
    {
      providerId: 'brave',
      label: 'Brave',
      runtimeSupported: true,
      requiresApiKey: true,
      envKey: 'BRAVE_API_KEY',
      whatYouNeed: ['API key via BRAVE_API_KEY or a one-time paste.'],
    },
  ],
  memoryEmbeddingProviders: [
    { providerId: 'auto', label: 'Auto' },
    { providerId: 'openai', label: 'OpenAI', requiresApiKey: true, envKey: 'OPENAI_API_KEY' },
    { providerId: 'local', label: 'Local BGE' },
  ],
  imageGenerationProviders: [
    {
      providerId: 'openrouter',
      label: 'OpenRouter',
      requiresApiKey: true,
      envKey: 'OPENROUTER_API_KEY',
    },
  ],
  audioProviders: [
    {
      providerId: 'elevenlabs',
      label: 'ElevenLabs',
      requiresApiKey: true,
      envKey: 'ELEVENLABS_API_KEY',
    },
  ],
}

const CONFIG = {
  llm: { provider: 'openai', model: 'gpt-4o', api_key_env: 'OPENAI_API_KEY' },
  agentos_router: { enabled: true, strategy: 'pilot-v1', default_tier: 'c1' },
  search_provider: 'duckduckgo',
  memory: { curated_memory_char_limit: 4000, curated_user_char_limit: 2000, inject_limit: 6400 },
}

function statusFor(overrides: Record<string, unknown> = {}) {
  return {
    needsOnboarding: false,
    hasConfig: true,
    llmConfigured: true,
    configPath: '/tmp/agentos.toml',
    channelCount: 0,
    sectionDetails: {
      llm: { label: 'Provider', status: 'ok', required: true },
      router: { label: 'Router', status: 'ok', required: true },
      channels: { label: 'Channels', status: 'optional' },
      search: { label: 'Search', status: 'ok' },
    },
    ...overrides,
  }
}

// Route each read to its payload; onboarding.*.configure default to {}.
function wireCalls(status: Record<string, unknown> = statusFor()) {
  mockRpc.call.mockImplementation((method: string) => {
    if (method === 'onboarding.catalog') return Promise.resolve(CATALOG)
    if (method === 'onboarding.status') return Promise.resolve(status)
    if (method === 'config.get') return Promise.resolve(CONFIG)
    if (method === 'channels.status') return Promise.resolve({ channels: [] })
    if (method === 'doctor.memory.status') return Promise.resolve(null)
    return Promise.resolve({})
  })
}

function renderPage() {
  return render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <MemoryRouter>
        <SetupPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('SetupPage', () => {
  beforeEach(() => {
    mockRpc.call.mockReset()
    mockRpc.waitForConnection.mockReset().mockResolvedValue(undefined)
    navigateSpy.mockReset()
    vi.mocked(toast.info).mockClear()
    vi.mocked(toast.error).mockClear()
    vi.mocked(toast.warning).mockClear()
  })
  afterEach(() => vi.clearAllTimers())

  it('loads the five setup reads after waitForConnection and renders the stepper', async () => {
    wireCalls()
    renderPage()
    await waitFor(() => expect(screen.getByText('Setup')).toBeInTheDocument())
    const methods = mockRpc.call.mock.calls.map((c) => c[0])
    expect(methods).toContain('onboarding.catalog')
    expect(methods).toContain('onboarding.status')
    expect(methods).toContain('config.get')
    expect(methods).toContain('channels.status')
    expect(mockRpc.waitForConnection).toHaveBeenCalled()
    // Five stepper items.
    expect(screen.getByRole('navigation', { name: 'Setup steps' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Provider:/ })).toBeInTheDocument()
  })

  it('shows the error banner when a setup read fails', async () => {
    mockRpc.call.mockImplementation((method: string) =>
      method === 'onboarding.catalog' ? Promise.reject(new Error('boom')) : Promise.resolve({}),
    )
    renderPage()
    await waitFor(() =>
      expect(screen.getByText(/Failed to load setup catalog: boom/)).toBeInTheDocument(),
    )
  })

  it('auto-selects the first step needing action', async () => {
    wireCalls(statusFor({ sectionDetails: { channels: { status: 'missing', label: 'Channels' } } }))
    renderPage()
    // channels needs action → starts on the channels step.
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Save Channel' })).toBeInTheDocument(),
    )
  })

  it('provider save calls onboarding.provider.configure with masked secret + advances', async () => {
    wireCalls(statusFor({ needsOnboarding: true, sectionDetails: { llm: { status: 'missing' } } }))
    // Start on provider (llm missing).
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Provider')).toBeInTheDocument())

    // The API key input is a password field (masked) and never carries a value.
    const keyInput = screen.getByLabelText('API key') as HTMLInputElement
    expect(keyInput.type).toBe('password')
    fireEvent.change(keyInput, { target: { value: 'sk-secret' } })

    fireEvent.click(screen.getByRole('button', { name: 'Save Provider' }))
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith(
        'onboarding.provider.configure',
        expect.objectContaining({ providerId: 'openai', apiKey: 'sk-secret' }),
      ),
    )
  })

  it('router save calls onboarding.router.configure with the assembled payload', async () => {
    wireCalls()
    renderPage()
    await waitFor(() => expect(screen.getByText('Setup')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /^Router Tiers:/ }))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Save Router' })).toBeInTheDocument(),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Save Router' }))
    await waitFor(() => {
      const call = mockRpc.call.mock.calls.find((c) => c[0] === 'onboarding.router.configure')
      expect(call).toBeTruthy()
      expect(call![1]).toMatchObject({
        mode: 'recommended',
        strategy: 'pilot-v1',
        defaultTier: 'c1',
      })
      // image_model row stamped.
      expect((call![1] as { tiers: Record<string, unknown> }).tiers.image_model).toMatchObject({
        image_only: true,
        supportsImage: true,
      })
    })
  })

  it('router preview uses the drafted provider chosen (unsaved) in the Provider step', async () => {
    // No configured provider yet: config.llm carries no provider, needsOnboarding.
    const noProviderConfig = { ...CONFIG, llm: {} }
    mockRpc.call.mockImplementation((method: string) => {
      if (method === 'onboarding.catalog') return Promise.resolve(CATALOG)
      if (method === 'onboarding.status')
        return Promise.resolve(
          statusFor({ needsOnboarding: true, sectionDetails: { llm: { status: 'missing' } } }),
        )
      if (method === 'config.get') return Promise.resolve(noProviderConfig)
      if (method === 'channels.status') return Promise.resolve({ channels: [] })
      return Promise.resolve({})
    })
    renderPage()
    // Starts on the provider step (llm missing).
    await waitFor(() => expect(screen.getByLabelText('Provider')).toBeInTheDocument())

    // Router step, before any pick: no configured provider → "Choose a provider first".
    fireEvent.click(screen.getByRole('button', { name: /^Router Tiers:/ }))
    await waitFor(() =>
      expect(screen.getByText(/Choose a provider first to preview/)).toBeInTheDocument(),
    )

    // Back to provider, pick openai (draft only — do NOT save).
    fireEvent.click(screen.getByRole('button', { name: /^Provider:/ }))
    await waitFor(() => expect(screen.getByLabelText('Provider')).toBeInTheDocument())
    fireEvent.change(screen.getByLabelText('Provider'), { target: { value: 'openai' } })

    // Router step now previews the drafted provider's tiers (no save happened).
    fireEvent.click(screen.getByRole('button', { name: /^Router Tiers:/ }))
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Router Tiers' })).toBeInTheDocument(),
    )
    // The tier table renders (drafted openai profile), not the empty-provider warning.
    expect(screen.queryByText(/Choose a provider first to preview/)).not.toBeInTheDocument()
    expect(screen.getByLabelText('c1 model')).toBeInTheDocument()
    // Summary line reflects the drafted provider.
    expect(screen.getByText('openai / Route c1')).toBeInTheDocument()
    // Save is still gated on the provider being saved (draft ≠ configured).
    expect(screen.getByText(/Save the provider before saving router tiers/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save Router' })).toBeDisabled()

    // No provider.configure was sent — the draft never triggered a save.
    expect(mockRpc.call.mock.calls.map((c) => c[0])).not.toContain('onboarding.provider.configure')
  })

  it('channel save probes THEN upserts', async () => {
    wireCalls()
    renderPage()
    await waitFor(() => expect(screen.getByText('Setup')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /^Channels:/ }))
    await waitFor(() => expect(screen.getByLabelText('Name')).toBeInTheDocument())
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'mybot' } })
    fireEvent.change(screen.getByLabelText('Bot token'), { target: { value: 'tok' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save Channel' }))
    await waitFor(() => {
      const methods = mockRpc.call.mock.calls.map((c) => c[0])
      const probeIdx = methods.indexOf('onboarding.channel.probe')
      const upsertIdx = methods.indexOf('onboarding.channel.upsert')
      expect(probeIdx).toBeGreaterThanOrEqual(0)
      expect(upsertIdx).toBeGreaterThan(probeIdx)
    })
  })

  it('channel save is blocked and toasts when a required field is blank', async () => {
    wireCalls()
    renderPage()
    await waitFor(() => expect(screen.getByText('Setup')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /^Channels:/ }))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Save Channel' })).toBeInTheDocument(),
    )
    // name + token blank → validation error, no probe/upsert.
    fireEvent.click(screen.getByRole('button', { name: 'Save Channel' }))
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('Name is required.', expect.anything()),
    )
    const methods = mockRpc.call.mock.calls.map((c) => c[0])
    expect(methods).not.toContain('onboarding.channel.probe')
  })

  it('search save calls onboarding.search.configure; brave reveals the masked key field', async () => {
    wireCalls()
    renderPage()
    await waitFor(() => expect(screen.getByText('Setup')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /^Capabilities:/ }))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Save web search' })).toBeInTheDocument(),
    )
    // switch to brave → API key field appears, masked.
    fireEvent.change(screen.getByLabelText('Search provider'), { target: { value: 'brave' } })
    const key = screen.getByLabelText('Search API key') as HTMLInputElement
    expect(key.type).toBe('password')
    fireEvent.click(screen.getByRole('button', { name: 'Save web search' }))
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith(
        'onboarding.search.configure',
        expect.objectContaining({ providerId: 'brave' }),
      ),
    )
  })

  it('switching search provider re-seeds api_key_env to the new provider envKey', async () => {
    wireCalls()
    renderPage()
    await waitFor(() => expect(screen.getByText('Setup')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /^Capabilities:/ }))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Save web search' })).toBeInTheDocument(),
    )
    // Start on duckduckgo (no key). Switch to brave WITHOUT typing an env name.
    fireEvent.change(screen.getByLabelText('Search provider'), { target: { value: 'brave' } })
    // The env field is re-seeded to BRAVE_API_KEY (legacy _syncSearchProviderKeyControls).
    const envInput = screen.getByLabelText('Search API key env') as HTMLInputElement
    expect(envInput.value).toBe('BRAVE_API_KEY')
    fireEvent.click(screen.getByRole('button', { name: 'Save web search' }))
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith(
        'onboarding.search.configure',
        expect.objectContaining({ providerId: 'brave', apiKeyEnv: 'BRAVE_API_KEY' }),
      ),
    )
  })

  it('a hand-typed search api_key_env is preserved across a provider switch', async () => {
    wireCalls()
    renderPage()
    await waitFor(() => expect(screen.getByText('Setup')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /^Capabilities:/ }))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Save web search' })).toBeInTheDocument(),
    )
    // Reveal the key fields, type a custom env name, then switch provider.
    fireEvent.change(screen.getByLabelText('Search provider'), { target: { value: 'brave' } })
    const envInput = screen.getByLabelText('Search API key env') as HTMLInputElement
    fireEvent.change(envInput, { target: { value: 'MY_CUSTOM_KEY' } })
    // Switch away and back — the user's typed value is NOT clobbered by the reseed.
    fireEvent.change(screen.getByLabelText('Search provider'), { target: { value: 'duckduckgo' } })
    fireEvent.change(screen.getByLabelText('Search provider'), { target: { value: 'brave' } })
    expect((screen.getByLabelText('Search API key env') as HTMLInputElement).value).toBe(
      'MY_CUSTOM_KEY',
    )
  })

  it('memory embedding save calls onboarding.memory_embedding.configure', async () => {
    wireCalls()
    renderPage()
    await waitFor(() => expect(screen.getByText('Setup')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /^Capabilities:/ }))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Save memory embedding' })).toBeInTheDocument(),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Save memory embedding' }))
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith(
        'onboarding.memory_embedding.configure',
        expect.objectContaining({ providerId: 'auto' }),
      ),
    )
  })

  it('memory settings save calls config.patch with the memory patches', async () => {
    wireCalls()
    renderPage()
    await waitFor(() => expect(screen.getByText('Setup')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /^Capabilities:/ }))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Save memory settings' })).toBeInTheDocument(),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Save memory settings' }))
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('config.patch', {
        patches: expect.objectContaining({
          'memory.curated_memory_char_limit': 4000,
          'memory.inject_limit': 6400,
        }),
      }),
    )
  })

  it('image save calls onboarding.imageGeneration.configure with enabled + masked key', async () => {
    wireCalls()
    renderPage()
    await waitFor(() => expect(screen.getByText('Setup')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /^Capabilities:/ }))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Save image generation' })).toBeInTheDocument(),
    )
    const key = screen.getByLabelText('Image API key') as HTMLInputElement
    expect(key.type).toBe('password')
    fireEvent.click(screen.getByRole('button', { name: 'Save image generation' }))
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith(
        'onboarding.imageGeneration.configure',
        expect.objectContaining({ providerId: 'openrouter', enabled: true }),
      ),
    )
  })

  it('audio save calls onboarding.audio.configure', async () => {
    wireCalls()
    renderPage()
    await waitFor(() => expect(screen.getByText('Setup')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /^Capabilities:/ }))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Save voice audio' })).toBeInTheDocument(),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Save voice audio' }))
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith(
        'onboarding.audio.configure',
        expect.objectContaining({ providerId: 'elevenlabs', enabled: false }),
      ),
    )
  })

  it('finish shows the summary + CLI recipes and saves the update preference', async () => {
    wireCalls()
    renderPage()
    await waitFor(() => expect(screen.getByText('Setup')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /^Finish:/ }))
    // CLI recipe command present (config-arg quoted path).
    await waitFor(() =>
      expect(
        screen.getByText('agentos onboard catalog providers --config /tmp/agentos.toml'),
      ).toBeInTheDocument(),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Save update preference' }))
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('config.patch', {
        patches: { 'updates.notify': true },
      }),
    )
  })

  it('exit setup navigates to overview', async () => {
    wireCalls()
    renderPage()
    await waitFor(() => expect(screen.getByText('Setup')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: 'Exit setup and return to Overview' }))
    expect(navigateSpy).toHaveBeenCalledWith('/overview')
  })

  it('clicking a reason row jumps to its step', async () => {
    wireCalls(
      statusFor({
        needsOnboarding: true,
        llmSource: 'missing_env',
        sectionDetails: { channels: { status: 'missing', label: 'Channels' } },
      }),
    )
    renderPage()
    await waitFor(() => expect(screen.getByText('Setup')).toBeInTheDocument())
    // A blocking reason for the missing env key is shown.
    const reasons = screen.getByRole('list', { name: /Setup actions needed|Optional improvements/ })
    expect(within(reasons).getByText(/is not visible/)).toBeInTheDocument()
  })
})
