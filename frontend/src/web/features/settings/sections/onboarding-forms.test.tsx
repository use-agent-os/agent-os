import '@/test/web'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import { renderShell, resetShellState, shellRpc } from '@/test/web-shell'
import { useRestartNotice } from '../config-editor'
import { needsSentence } from './onboarding-forms'

// The catalog-driven forms (Models & routing, Search, Media, guided setup).
// Every onboarding.*.configure call rewrites its whole section, so these
// assert the exact params: a field the form does not send is a field the
// gateway resets.

beforeEach(() => {
  resetShellState()
  useRestartNotice.getState().clear()
})

const field = (name: string, type: string, extra: Record<string, unknown> = {}) => ({
  name,
  label: name,
  type,
  required: false,
  secret: type === 'password',
  choices: [],
  ...extra,
})

const SEARCH_FIELDS = [
  field('api_key', 'password', { required: true }),
  field('max_results', 'int', { default: 5 }),
  field('proxy', 'text', { default: '' }),
  field('use_env_proxy', 'bool', { default: false }),
  field('fallback_policy', 'select', { default: 'off', choices: ['off', 'network'] }),
  field('diagnostics', 'bool', { default: false }),
]

const CATALOG = {
  providers: [
    {
      providerId: 'openai',
      label: 'OpenAI',
      runtimeSupported: true,
      routerSupported: true,
      envKey: 'OPENAI_API_KEY',
      fields: [
        field('model', 'text', { default: 'gpt-5.6-luna' }),
        field('api_key', 'password', { required: true }),
        field('api_key_env', 'text', { default: 'OPENAI_API_KEY' }),
        field('base_url', 'text', { default: 'https://api.openai.com/v1' }),
        field('proxy', 'text', { default: '' }),
      ],
    },
    {
      providerId: 'openrouter',
      label: 'OpenRouter',
      runtimeSupported: true,
      routerSupported: true,
      envKey: 'OPENROUTER_API_KEY',
      fields: [
        field('model', 'text', { default: 'openai/gpt-6-luna' }),
        field('api_key', 'password', { required: true }),
        field('api_key_env', 'text', { default: 'OPENROUTER_API_KEY' }),
        field('base_url', 'text', { default: 'https://openrouter.ai/api/v1' }),
        field('proxy', 'text', { default: '' }),
      ],
    },
  ],
  searchProviders: [
    {
      providerId: 'brave',
      label: 'Brave',
      runtimeSupported: true,
      requiresApiKey: true,
      envKey: 'BRAVE_SEARCH_API_KEY',
      fields: SEARCH_FIELDS,
    },
    {
      providerId: 'duckduckgo',
      label: 'DuckDuckGo',
      runtimeSupported: true,
      requiresApiKey: false,
      fields: SEARCH_FIELDS,
    },
    {
      providerId: 'exa',
      label: 'Exa',
      runtimeSupported: false,
      requiresApiKey: true,
      fields: SEARCH_FIELDS,
    },
  ],
  xSearch: [
    {
      providerId: 'x_search',
      label: 'X search',
      requiresApiKey: true,
      envKey: 'XAI_API_KEY',
      fields: [
        field('api_key', 'password', { required: true }),
        field('model', 'text', { default: 'grok-4.5' }),
        field('reasoning_effort', 'select', {
          default: '',
          choices: ['', 'low', 'medium', 'high', 'xhigh'],
        }),
        field('timeout_seconds', 'int', { default: 180 }),
        field('total_timeout_seconds', 'int', { default: 300 }),
        field('retries', 'int', { default: 2 }),
        field('base_url', 'text', { default: 'https://api.x.ai/v1' }),
      ],
    },
  ],
  imageGenerationProviders: ['openai', 'openrouter'].map((id) => ({
    providerId: id,
    label: id,
    runtimeSupported: true,
    requiresApiKey: true,
    envKey: id === 'openai' ? 'OPENAI_API_KEY' : 'OPENROUTER_API_KEY',
    defaultModel: id === 'openai' ? 'openai/gpt-image-1' : 'openrouter/google/flash-image',
    fields: [
      field('enabled', 'bool', { default: true }),
      field('primary', 'text', { required: true }),
      field('api_key', 'password'),
      field('base_url', 'text'),
    ],
  })),
  audioProviders: [
    {
      providerId: 'elevenlabs',
      label: 'ElevenLabs',
      runtimeSupported: true,
      requiresApiKey: true,
      envKey: 'ELEVENLABS_API_KEY',
      fields: [
        field('enabled', 'bool', { default: true }),
        field('api_key', 'password'),
        field('api_key_env', 'text', { default: 'ELEVENLABS_API_KEY' }),
        field('base_url', 'text', { default: 'https://api.elevenlabs.io' }),
        field('tts_voice', 'text', { default: 'voice-default' }),
        field('tts_model', 'text', { default: 'model-default' }),
        field('language_code', 'text', { default: '' }),
      ],
    },
  ],
  memoryEmbeddingProviders: [
    { providerId: 'auto', label: 'Auto', runtimeSupported: true, fields: [] },
    {
      providerId: 'openai',
      label: 'OpenAI',
      runtimeSupported: true,
      requiresApiKey: true,
      envKey: 'OPENAI_API_KEY',
      fields: [],
    },
  ],
  routerProfiles: {
    defaultTier: 'c1',
    profiles: [
      {
        providerId: 'openai',
        tiers: {
          c0: { provider: 'openai', model: 'oa-c0' },
          c1: { provider: 'openai', model: 'oa-c1' },
          c2: { provider: 'openai', model: 'oa-c2' },
          c3: { provider: 'openai', model: 'oa-c3' },
          image_model: { provider: 'openai', model: 'oa-image', supports_image: true },
        },
      },
      {
        providerId: 'openrouter',
        tiers: {
          c0: { provider: 'openrouter', model: 'or-c0' },
          c1: { provider: 'openrouter', model: 'or-c1' },
          c2: { provider: 'openrouter', model: 'or-c2' },
          c3: { provider: 'openrouter', model: 'or-c3' },
          image_model: { provider: 'openrouter', model: 'or-image', supports_image: true },
        },
      },
    ],
    judge: { profiles: { openai: { autoModel: 'oa-c0', models: ['oa-c0', 'oa-c1'] } } },
  },
}

const TIERS = {
  c0: { provider: 'openai', model: 'oa-c0', thinking_level: 'low', supports_image: false },
  c1: { provider: 'openai', model: 'oa-c1', thinking_level: 'high', supports_image: false },
  c2: { provider: 'openai', model: 'oa-c2', thinking_level: 'high', supports_image: false },
  c3: { provider: 'openai', model: 'oa-c3', thinking_level: 'high', supports_image: true },
  image_model: {
    provider: 'openai',
    model: 'my/custom-image',
    description: 'My own image route',
    thinking_level: 'medium',
    supports_image: true,
    image_only: true,
  },
}

function config(overrides: Record<string, unknown> = {}) {
  return {
    llm: {
      provider: 'openai',
      model: 'gpt-saved',
      api_key: '',
      api_key_env: 'MY_OPENAI_KEY',
      base_url: 'https://proxy.example/v1',
      proxy: 'http://corp:3128',
    },
    provider_profiles: {},
    agentos_router: {
      enabled: true,
      strategy: 'pilot-v1',
      default_tier: 'c1',
      judge_model: null,
      tiers: TIERS,
      pilot: { safety_net_threshold: 0.4 },
      jev: { api_key: null, api_key_env: 'TYPESAFE_API_KEY', high_risk_threshold: 0.7 },
      translate_ceiling_enabled: true,
      translate_ceiling_tier: 'c1',
    },
    search_provider: 'brave',
    search_api_key: '[redacted]',
    search_api_key_env: '',
    search_max_results: 10,
    search_proxy: 'http://search-proxy:8080',
    search_use_env_proxy: false,
    search_fallback_policy: 'network',
    search_diagnostics: false,
    x_search: {
      enabled: false,
      model: 'grok-custom',
      base_url: 'https://api.x.ai/v1',
      api_key: '',
      api_key_env: 'XAI_API_KEY',
      reasoning_effort: 'high',
      timeout_seconds: 120.0,
      total_timeout_seconds: 240.0,
      retries: 3,
    },
    image_generation: {
      enabled: true,
      primary: 'openrouter/my/image-model',
      providers: {
        openai: {
          base_url: 'https://api.openai.com/v1',
          api_key: '',
          api_key_env: 'OPENAI_API_KEY',
        },
        openrouter: {
          base_url: 'https://openrouter.ai/api/v1',
          api_key: '',
          api_key_env: 'MY_OR_KEY',
        },
      },
    },
    audio: {
      enabled: true,
      tts: { model: 'tts-saved', voice: 'voice-saved', language_code: 'vi' },
      providers: {
        elevenlabs: { base_url: 'https://api.elevenlabs.io', api_key: '', api_key_env: 'EL_KEY' },
      },
    },
    memory: {
      embedding: {
        provider: 'openai',
        remote: { model: 'text-embedding-3-small', api_key_env: 'EMB_KEY', base_url: null },
        ollama: {},
        local: {},
      },
    },
    ...overrides,
  }
}

function snapshot(overrides: Record<string, unknown> = {}, cfg = config()) {
  return {
    config: cfg,
    catalog: CATALOG,
    status: {
      llmSource: 'env',
      imageGenerationProvider: 'openrouter',
      imageGenerationPrimary: 'openrouter/my/image-model',
      audioProvider: 'elevenlabs',
      memoryEmbeddingProvider: 'openai',
    },
    revision: 'rev-1',
    pendingRestart: false,
    restartReasons: [],
    diskDiverged: false,
    writeBlocked: false,
    ...overrides,
  }
}

const OK = { changed: true, restartRequired: false, warnings: [] }

function formsRpc(snap: unknown = snapshot()) {
  return shellRpc()
    .handle('config.snapshot', snap)
    .handle('auth.status', { xai: { loggedIn: false } })
    .handle('onboarding.provider.configure', OK)
    .handle('onboarding.router.configure', OK)
    .handle('onboarding.search.configure', OK)
    .handle('onboarding.x_search.configure', OK)
    .handle('onboarding.imageGeneration.configure', OK)
    .handle('onboarding.audio.configure', OK)
    .handle('onboarding.memory_embedding.configure', OK)
}

function form(name: string): HTMLElement {
  return screen.getByRole('region', { name })
}

async function apply(section: HTMLElement) {
  await userEvent.setup().click(within(section).getByRole('button', { name: 'Apply' }))
}

describe('Settings › Search', () => {
  it('sends every saved search setting with a newly typed key, then clears the key', async () => {
    const user = userEvent.setup()
    const rpc = formsRpc()
    renderShell('/settings/search', rpc)
    const web = await screen.findByRole('region', { name: 'Web search' })
    expect(within(web).getByRole('combobox', { name: 'Provider' })).toHaveValue('brave')
    expect(within(web).getByRole('spinbutton', { name: 'max_results' })).toHaveValue(10)
    const key = within(web).getByLabelText(/^api_key/)
    await user.type(key, 'brave-new-key')
    await apply(web)
    await waitFor(() =>
      expect(rpc.callsTo('onboarding.search.configure')).toEqual([
        {
          providerId: 'brave',
          apiKey: 'brave-new-key',
          maxResults: 10,
          proxy: 'http://search-proxy:8080',
          useEnvProxy: false,
          fallbackPolicy: 'network',
          diagnostics: false,
          expectedRevision: 'rev-1',
        },
      ]),
    )
    // A one-time secret does not stay in the form after it was saved.
    await waitFor(() => expect(key).toHaveValue(''))
  })

  it('keeps a stored literal key: no env reference is sent in its place', async () => {
    const rpc = formsRpc()
    renderShell('/settings/search', rpc)
    await apply(await screen.findByRole('region', { name: 'Web search' }))
    await waitFor(() => expect(rpc.callsTo('onboarding.search.configure')).toHaveLength(1))
    expect(rpc.callsTo('onboarding.search.configure')[0]).toMatchObject({
      providerId: 'brave',
      apiKeyEnv: '',
      maxResults: 10,
    })
    expect(rpc.callsTo('onboarding.search.configure')[0]).not.toHaveProperty('apiKey')
  })

  it('offers only runtime-supported providers and the env name for keyed ones', async () => {
    const user = userEvent.setup()
    const rpc = formsRpc()
    renderShell('/settings/search', rpc)
    const web = await screen.findByRole('region', { name: 'Web search' })
    const select = within(web).getByRole('combobox', { name: 'Provider' })
    expect(
      within(select)
        .getAllByRole('option')
        .map((o) => o.textContent),
    ).toEqual(['Brave', 'DuckDuckGo'])
    await user.selectOptions(select, 'duckduckgo')
    // DuckDuckGo needs no key: neither key field is offered or sent.
    expect(within(web).queryByLabelText(/^api_key/)).not.toBeInTheDocument()
    await apply(web)
    await waitFor(() =>
      expect(rpc.callsTo('onboarding.search.configure')).toEqual([
        {
          providerId: 'duckduckgo',
          maxResults: 10,
          proxy: 'http://search-proxy:8080',
          useEnvProxy: false,
          fallbackPolicy: 'network',
          diagnostics: false,
          expectedRevision: 'rev-1',
        },
      ]),
    )
  })

  it('X search: seeds the saved values, keeps it off, and sends the whole section', async () => {
    const user = userEvent.setup()
    const rpc = formsRpc()
    renderShell('/settings/search', rpc)
    const x = await screen.findByRole('region', { name: 'X search' })
    const enabled = within(x).getByRole('switch', { name: 'Enable X search' })
    expect(enabled).not.toBeChecked()
    const model = within(x).getByRole('textbox', { name: 'model' })
    expect(model).toHaveValue('grok-custom')
    await user.clear(model)
    await user.type(model, 'grok-next')
    await apply(x)
    await waitFor(() =>
      expect(rpc.callsTo('onboarding.x_search.configure')).toEqual([
        {
          enabled: false,
          apiKeyEnv: 'XAI_API_KEY',
          model: 'grok-next',
          reasoningEffort: 'high',
          timeoutSeconds: 120,
          totalTimeoutSeconds: 240,
          retries: 3,
          baseUrl: 'https://api.x.ai/v1',
          expectedRevision: 'rev-1',
        },
      ]),
    )
  })

  it('X search: Sign out appears when signed in and refreshes the login state', async () => {
    const user = userEvent.setup()
    let signedIn = true
    const rpc = formsRpc()
      .handle('auth.status', () => ({ xai: { loggedIn: signedIn, has_refresh_token: true } }))
      .handle('auth.xai.logout', () => {
        signedIn = false
        return { cleared: true }
      })
    renderShell('/settings/search', rpc)
    const x = await screen.findByRole('region', { name: 'X search' })
    await user.click(await within(x).findByRole('button', { name: 'Sign out of xAI' }))
    expect(await within(x).findByRole('button', { name: 'Sign in with xAI' })).toBeInTheDocument()
    expect(rpc.callsTo('auth.xai.logout')).toHaveLength(1)
  })
})

describe('Settings › Media', () => {
  it('preselects the image provider in use and sends its saved model and env', async () => {
    const rpc = formsRpc()
    renderShell('/settings/media', rpc)
    const image = await screen.findByRole('region', { name: 'Image generation' })
    expect(within(image).getByRole('combobox', { name: 'Provider' })).toHaveValue('openrouter')
    await apply(image)
    await waitFor(() =>
      expect(rpc.callsTo('onboarding.imageGeneration.configure')).toEqual([
        {
          providerId: 'openrouter',
          enabled: true,
          primary: 'openrouter/my/image-model',
          apiKeyEnv: 'MY_OR_KEY',
          baseUrl: 'https://openrouter.ai/api/v1',
          expectedRevision: 'rev-1',
        },
      ]),
    )
  })

  it('sends the saved audio voice, model and language, not the catalog defaults', async () => {
    const rpc = formsRpc()
    renderShell('/settings/media', rpc)
    await apply(await screen.findByRole('region', { name: 'Audio' }))
    await waitFor(() =>
      expect(rpc.callsTo('onboarding.audio.configure')).toEqual([
        {
          providerId: 'elevenlabs',
          enabled: true,
          apiKeyEnv: 'EL_KEY',
          baseUrl: 'https://api.elevenlabs.io',
          ttsVoice: 'voice-saved',
          ttsModel: 'tts-saved',
          languageCode: 'vi',
          expectedRevision: 'rev-1',
        },
      ]),
    )
  })

  it('a pasted key replaces the env reference (the gateway rejects both)', async () => {
    const user = userEvent.setup()
    const rpc = formsRpc()
    renderShell('/settings/media', rpc)
    const audio = await screen.findByRole('region', { name: 'Audio' })
    await user.type(within(audio).getByLabelText(/^api_key$/), 'el-secret')
    await apply(audio)
    await waitFor(() => expect(rpc.callsTo('onboarding.audio.configure')).toHaveLength(1))
    const params = rpc.callsTo('onboarding.audio.configure')[0]!
    expect(params.apiKey).toBe('el-secret')
    expect(params).not.toHaveProperty('apiKeyEnv')
  })
})

describe('Settings › Models & routing', () => {
  it('prefills the provider form from llm.* and sends it back whole', async () => {
    const rpc = formsRpc()
    renderShell('/settings/models', rpc)
    const provider = await screen.findByRole('region', { name: 'Provider' })
    expect(within(provider).getByRole('textbox', { name: 'model' })).toHaveValue('gpt-saved')
    expect(within(provider).getByText('Model tiers:')).toBeInTheDocument()
    await apply(provider)
    await waitFor(() =>
      expect(rpc.callsTo('onboarding.provider.configure')).toEqual([
        {
          providerId: 'openai',
          model: 'gpt-saved',
          apiKeyEnv: 'MY_OPENAI_KEY',
          baseUrl: 'https://proxy.example/v1',
          proxy: 'http://corp:3128',
          expectedRevision: 'rev-1',
        },
      ]),
    )
  })

  it('switching provider sends the env reference the form shows', async () => {
    const user = userEvent.setup()
    const rpc = formsRpc()
    renderShell('/settings/models', rpc)
    const provider = await screen.findByRole('region', { name: 'Provider' })
    await user.selectOptions(
      within(provider).getByRole('combobox', { name: 'Provider' }),
      'openrouter',
    )
    await apply(provider)
    await waitFor(() =>
      expect(rpc.callsTo('onboarding.provider.configure')).toEqual([
        {
          providerId: 'openrouter',
          model: 'openai/gpt-6-luna',
          apiKeyEnv: 'OPENROUTER_API_KEY',
          baseUrl: 'https://openrouter.ai/api/v1',
          proxy: '',
          expectedRevision: 'rev-1',
        },
      ]),
    )
  })

  it('warns when the provider key variable is not visible to the gateway', async () => {
    const snap = snapshot()
    snap.status = { ...snap.status, llmSource: 'missing_env' }
    renderShell('/settings/models', formsRpc(snap))
    expect(await screen.findByText(/The gateway cannot see MY_OPENAI_KEY/)).toBeInTheDocument()
  })

  it('the router form sends every tier, image_model included, and its per-mode settings', async () => {
    const user = userEvent.setup()
    const rpc = formsRpc()
    renderShell('/settings/models', rpc)
    const router = await screen.findByRole('region', { name: 'Pilot Router' })
    expect(within(router).getByRole('textbox', { name: 'image_model model' })).toHaveValue(
      'my/custom-image',
    )
    expect(within(router).getByRole('spinbutton', { name: 'Safety-net threshold' })).toHaveValue(
      0.4,
    )
    await user.selectOptions(within(router).getByRole('combobox', { name: 'Default tier' }), 'c2')
    await apply(router)
    await waitFor(() => expect(rpc.callsTo('onboarding.router.configure')).toHaveLength(1))
    expect(rpc.callsTo('onboarding.router.configure')[0]).toEqual({
      mode: 'recommended',
      strategy: 'pilot-v1',
      defaultTier: 'c2',
      judgeModel: null,
      safetyNetThreshold: 0.4,
      translateCeilingEnabled: true,
      translateCeilingTier: 'c1',
      tiers: {
        c0: { provider: 'openai', model: 'oa-c0', thinkingLevel: 'low', supportsImage: false },
        c1: { provider: 'openai', model: 'oa-c1', thinkingLevel: 'high', supportsImage: false },
        c2: { provider: 'openai', model: 'oa-c2', thinkingLevel: 'high', supportsImage: false },
        c3: { provider: 'openai', model: 'oa-c3', thinkingLevel: 'high', supportsImage: true },
        image_model: {
          provider: 'openai',
          model: 'my/custom-image',
          // Not shown in the form; sent back so the gateway keeps it.
          description: 'My own image route',
          thinkingLevel: 'medium',
          supportsImage: true,
          image_only: true,
        },
      },
      expectedRevision: 'rev-1',
    })
  })

  it("openrouter, Apply, back, Apply, then Pilot: sends the current provider's tiers", async () => {
    const user = userEvent.setup()
    // Router off, no tier_profile, and the shipped OpenRouter tiers saved.
    const orTiers = Object.fromEntries(
      ['c0', 'c1', 'c2', 'c3'].map((tier) => [
        tier,
        { provider: 'openrouter', model: `or-${tier}`, description: 'shipped' },
      ]),
    ) as unknown as typeof TIERS
    const withProvider = (provider: string, revision: string) => {
      const cfg = config()
      cfg.llm = { ...cfg.llm, provider, model: provider === 'openai' ? 'oa-c1' : 'or-c1' }
      cfg.agentos_router = {
        ...cfg.agentos_router,
        enabled: false,
        strategy: 'pilot-v1',
        tiers: orTiers,
      }
      return snapshot({ revision }, cfg)
    }
    let current = withProvider('openai', 'rev-1')
    let n = 1
    const rpc = formsRpc()
      .handle('config.snapshot', () => current)
      .handle('onboarding.provider.configure', (p: Record<string, unknown>) => {
        // Provider switches leave the tiers of a disabled router alone.
        current = withProvider(String(p.providerId), `rev-${++n}`)
        return OK
      })
    renderShell('/settings/models', rpc)
    const provider = await screen.findByRole('region', { name: 'Provider' })
    const router = form('Pilot Router')
    const c1 = () => within(router).getByRole('textbox', { name: 'c1 model' })
    expect(c1()).toHaveValue('oa-c1')
    const select = within(provider).getByRole('combobox', { name: 'Provider' })
    await user.selectOptions(select, 'openrouter')
    await apply(provider)
    await waitFor(() => expect(c1()).toHaveValue('or-c1'))
    await user.selectOptions(select, 'openai')
    await apply(provider)
    await waitFor(() => expect(c1()).toHaveValue('oa-c1'))
    await user.selectOptions(
      within(router).getByRole('combobox', { name: 'Routing mode' }),
      'pilot-v1',
    )
    await apply(router)
    await waitFor(() => expect(rpc.callsTo('onboarding.router.configure')).toHaveLength(1))
    const sent = rpc.callsTo('onboarding.router.configure')[0]!.tiers as Record<
      string,
      { provider: string; model: string }
    >
    expect(Object.values(sent).map((tier) => [tier.provider, tier.model])).toEqual([
      ['openai', 'oa-c0'],
      ['openai', 'oa-c1'],
      ['openai', 'oa-c2'],
      ['openai', 'oa-c3'],
      ['openai', 'oa-image'],
    ])
  })

  it("an edited router draft takes the new provider's tiers after a switch", async () => {
    const user = userEvent.setup()
    let current = snapshot()
    const rpc = formsRpc()
      .handle('config.snapshot', () => current)
      .handle('onboarding.provider.configure', () => {
        const switched = config()
        switched.llm = { ...switched.llm, provider: 'openrouter', model: 'or-c1' }
        current = snapshot({ revision: 'rev-2' }, switched)
        return OK
      })
    renderShell('/settings/models', rpc)
    const provider = await screen.findByRole('region', { name: 'Provider' })
    const router = form('Pilot Router')
    await user.selectOptions(within(router).getByRole('combobox', { name: 'Default tier' }), 'c2')
    const c3 = within(router).getByRole('textbox', { name: 'c3 model' })
    await user.clear(c3)
    await user.type(c3, 'typed-before')
    await user.selectOptions(
      within(provider).getByRole('combobox', { name: 'Provider' }),
      'openrouter',
    )
    await apply(provider)
    await waitFor(() =>
      expect(within(router).getByRole('textbox', { name: 'c3 model' })).toHaveValue('or-c3'),
    )
    // Typed after the switch: that is the user's choice for this provider.
    const c0 = within(router).getByRole('textbox', { name: 'c0 model' })
    await user.clear(c0)
    await user.type(c0, 'typed-after')
    await apply(router)
    await waitFor(() => expect(rpc.callsTo('onboarding.router.configure')).toHaveLength(1))
    expect(rpc.callsTo('onboarding.router.configure')[0]).toMatchObject({
      defaultTier: 'c2',
      tiers: {
        c0: { provider: 'openrouter', model: 'typed-after' },
        c1: { provider: 'openrouter', model: 'or-c1' },
        c3: { provider: 'openrouter', model: 'or-c3' },
      },
    })
  })

  it('lets a first-time Jev user enter a key and threshold, and clears the key after Apply', async () => {
    const user = userEvent.setup()
    const rpc = formsRpc()
    renderShell('/settings/models', rpc)
    const router = await screen.findByRole('region', { name: 'Pilot Router' })
    await user.selectOptions(within(router).getByRole('combobox', { name: 'Routing mode' }), 'jev')
    const key = within(router).getByLabelText('Jev API key')
    await user.type(key, 'ts-key')
    const risk = within(router).getByRole('spinbutton', { name: 'High-risk threshold' })
    await user.clear(risk)
    await user.type(risk, '0.8')
    await user.selectOptions(
      within(router).getByRole('combobox', { name: 'Translation cap' }),
      'off',
    )
    await apply(router)
    await waitFor(() => expect(rpc.callsTo('onboarding.router.configure')).toHaveLength(1))
    expect(rpc.callsTo('onboarding.router.configure')[0]).toMatchObject({
      mode: 'recommended',
      strategy: 'jev',
      jevApiKey: 'ts-key',
      jevHighRiskThreshold: 0.8,
      translateCeilingEnabled: false,
    })
    await waitFor(() => expect(key).toHaveValue(''))
  })

  it('reseeds the tier models after a provider switch on the same page', async () => {
    const user = userEvent.setup()
    let current = snapshot()
    const rpc = formsRpc().handle('config.snapshot', () => current)
    rpc.handle('onboarding.provider.configure', () => {
      const switched = config()
      switched.llm = { ...switched.llm, provider: 'openrouter', model: 'or-c1' }
      switched.agentos_router = { ...switched.agentos_router, tiers: {} as typeof TIERS }
      current = snapshot({ revision: 'rev-2' }, switched)
      return OK
    })
    renderShell('/settings/models', rpc)
    const provider = await screen.findByRole('region', { name: 'Provider' })
    const router = form('Pilot Router')
    expect(within(router).getByRole('textbox', { name: 'c1 model' })).toHaveValue('oa-c1')
    await user.selectOptions(
      within(provider).getByRole('combobox', { name: 'Provider' }),
      'openrouter',
    )
    await apply(provider)
    await waitFor(() =>
      expect(within(router).getByRole('textbox', { name: 'c1 model' })).toHaveValue('or-c1'),
    )
    await apply(router)
    await waitFor(() => expect(rpc.callsTo('onboarding.router.configure')).toHaveLength(1))
    expect(rpc.callsTo('onboarding.router.configure')[0]).toMatchObject({
      tiers: { c1: { provider: 'openrouter', model: 'or-c1' } },
      expectedRevision: 'rev-2',
    })
  })

  it('keeps an edited router draft when the saved values move, and says so', async () => {
    const user = userEvent.setup()
    let current = snapshot()
    const rpc = formsRpc().handle('config.snapshot', () => current)
    const { queryClient } = renderShell('/settings/models', rpc)
    const router = await screen.findByRole('region', { name: 'Pilot Router' })
    const c1 = within(router).getByRole('textbox', { name: 'c1 model' })
    await user.clear(c1)
    await user.type(c1, 'mine')
    const moved = config()
    moved.agentos_router = { ...moved.agentos_router, default_tier: 'c3' }
    current = snapshot({ revision: 'rev-9' }, moved)
    await queryClient.invalidateQueries()
    expect(
      await within(router).findByText(/The saved settings changed since you started editing/),
    ).toBeInTheDocument()
    expect(c1).toHaveValue('mine')
    await user.click(within(router).getByRole('button', { name: 'Reload' }))
    expect(within(router).getByRole('textbox', { name: 'c1 model' })).toHaveValue('oa-c1')
    expect(within(router).getByRole('combobox', { name: 'Default tier' })).toHaveValue('c3')
  })
})

describe('what you need', () => {
  it('reads as sentences, without doubled punctuation', () => {
    const text = needsSentence([
      'An xAI API key via XAI_API_KEY or a one-time paste.',
      'Or a SuperGrok / X Premium+ login: sign in below, or run `agentos auth login xai`.',
      'A login is preferred',
    ])
    expect(text).toBe(
      'An xAI API key via XAI_API_KEY or a one-time paste. Or a SuperGrok / X Premium+ login: sign in below, or run `agentos auth login xai`. A login is preferred.',
    )
    expect(text).not.toMatch(/,,|\.,|\)\./)
  })
})

describe('write-blocked configuration', () => {
  it('explains the block, disables Apply and reloads on request', async () => {
    const user = userEvent.setup()
    const rpc = formsRpc(snapshot({ writeBlocked: true, diskDiverged: true, revision: null }))
    renderShell('/settings/search', rpc)
    const web = await screen.findByRole('region', { name: 'Web search' })
    expect(within(web).getByRole('button', { name: 'Apply' })).toBeDisabled()
    const notice = within(web).getByRole('alert')
    expect(notice).toHaveTextContent('The config file changed on disk')
    const before = rpc.callsTo('config.snapshot').length
    await user.click(within(notice).getByRole('button', { name: 'Reload' }))
    await waitFor(() => expect(rpc.callsTo('config.snapshot').length).toBeGreaterThan(before))
    expect(rpc.callsTo('onboarding.search.configure')).toEqual([])
  })
})

describe('guided setup extras', () => {
  it('memory embeddings: seeds the saved remote model and env, and sends them', async () => {
    const rpc = formsRpc()
    renderShell('/settings/setup?step=extras', rpc)
    const memory = await screen.findByRole('region', { name: 'Memory embeddings' })
    expect(within(memory).getByRole('combobox', { name: 'Provider' })).toHaveValue('openai')
    await apply(memory)
    await waitFor(() =>
      expect(rpc.callsTo('onboarding.memory_embedding.configure')).toEqual([
        {
          providerId: 'openai',
          model: 'text-embedding-3-small',
          apiKeyEnv: 'EMB_KEY',
          baseUrl: '',
          expectedRevision: 'rev-1',
        },
      ]),
    )
  })
})

describe('settings search targets', () => {
  it.each([
    ['models', 'agentos_router.tiers.image_model.model'],
    ['models', 'llm.model'],
    ['search', 'x_search.enabled'],
    ['search', 'search_max_results'],
    ['media', 'image_generation.primary'],
    ['media', 'audio.enabled'],
  ])('/settings/%s has a field for %s', async (section, id) => {
    renderShell(`/settings/${section}`, formsRpc())
    await screen.findAllByRole('button', { name: 'Apply' })
    expect(document.querySelector(`[data-field-id="${id}"]`)).not.toBeNull()
  })
})
