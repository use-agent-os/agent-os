import { describe, expect, it } from 'vitest'
import type { ProviderSpec } from '@/views/setup/logic'
import type { SettingsSnapshot } from '@/views/settings/snapshot'
import {
  buildParams,
  capabilityStatus,
  collectParams,
  draftDirty,
  enableDraft,
  fieldProblem,
  imageDef,
  keyReady,
  keySource,
  keyUrl,
  NUMBER_BOUNDS,
  saveProblem,
  searchDef,
  searchFailure,
  seedDraft,
  shortLabel,
  switchProvider,
  voiceDef,
  xSearchDef,
  type KeyContext,
} from './logic'

const SEARCH_FIELDS = [
  { name: 'api_key', label: 'API key', type: 'password', secret: true, required: true },
  { name: 'max_results', label: 'Max results', type: 'int', default: 5 },
  { name: 'proxy', label: 'HTTP proxy', type: 'text', default: '' },
  { name: 'use_env_proxy', label: 'Use environment proxy', type: 'bool', default: false },
  {
    name: 'fallback_policy',
    label: 'Fallback policy',
    type: 'select',
    default: 'off',
    choices: ['off', 'network'],
  },
  { name: 'diagnostics', label: 'Diagnostics', type: 'bool', default: false },
]

const BRAVE: ProviderSpec = {
  providerId: 'brave',
  label: 'Brave Search',
  runtimeSupported: true,
  requiresApiKey: true,
  envKey: 'BRAVE_SEARCH_API_KEY',
  fields: SEARCH_FIELDS,
}
const DDG: ProviderSpec = {
  providerId: 'duckduckgo',
  label: 'DuckDuckGo',
  runtimeSupported: true,
  requiresApiKey: false,
  envKey: '',
  fields: SEARCH_FIELDS.map((f) => (f.name === 'api_key' ? { ...f, required: false } : f)),
}
const EXA: ProviderSpec = { ...BRAVE, providerId: 'exa', label: 'Exa', runtimeSupported: false }

const IMAGE_FIELDS = [
  { name: 'enabled', type: 'bool', default: true },
  { name: 'primary', label: 'Primary model', type: 'text', required: true },
  { name: 'api_key', label: 'API key', type: 'password', secret: true },
  { name: 'base_url', label: 'Base URL', type: 'text' },
]
const OPENAI_IMG: ProviderSpec = {
  providerId: 'openai',
  label: 'OpenAI Images',
  runtimeSupported: true,
  requiresApiKey: true,
  envKey: 'OPENAI_API_KEY',
  defaultModel: 'openai/gpt-image-1',
  defaultBaseUrl: 'https://api.openai.com/v1',
  fields: IMAGE_FIELDS,
}
const OPENROUTER_IMG: ProviderSpec = {
  providerId: 'openrouter',
  label: 'OpenRouter Images',
  runtimeSupported: true,
  requiresApiKey: true,
  envKey: 'OPENROUTER_API_KEY',
  defaultModel: 'openrouter/google/gemini-image',
  defaultBaseUrl: 'https://openrouter.ai/api/v1',
  fields: IMAGE_FIELDS,
}
const ELEVEN: ProviderSpec = {
  providerId: 'elevenlabs',
  label: 'ElevenLabs Audio',
  runtimeSupported: true,
  requiresApiKey: true,
  envKey: 'ELEVENLABS_API_KEY',
  defaultTtsVoice: 'voice-1',
  defaultTtsModel: 'eleven_v2',
  fields: [
    { name: 'enabled', type: 'bool', default: true },
    { name: 'api_key', type: 'password', secret: true },
    { name: 'api_key_env', type: 'text', default: 'ELEVENLABS_API_KEY' },
    { name: 'tts_voice', type: 'text' },
  ],
}
const XSEARCH: ProviderSpec = {
  providerId: 'x_search',
  label: 'X (Twitter) Search',
  requiresApiKey: true,
  envKey: 'XAI_API_KEY',
  fields: [
    { name: 'api_key', type: 'password', secret: true, required: true },
    { name: 'model', type: 'text', default: 'grok-4.5' },
    { name: 'timeout_seconds', type: 'int', default: 180 },
  ],
}

/** The shared gateway's state on 2026-11-20: DuckDuckGo, images and voice off. */
function snapshot(): SettingsSnapshot {
  return {
    revision: 'r1',
    catalog: {
      searchProviders: [BRAVE, DDG, EXA],
      imageGenerationProviders: [OPENAI_IMG, OPENROUTER_IMG],
      audioProviders: [ELEVEN],
      xSearch: [XSEARCH],
    },
    config: {
      llm: { provider: 'surplus', api_key: '[redacted]' },
      search_provider: 'duckduckgo',
      search_api_key: '',
      search_api_key_env: '',
      search_max_results: 5,
      search_fallback_policy: 'off',
      x_search: { enabled: true, model: 'grok-4.5', api_key: '', api_key_env: 'XAI_API_KEY' },
      image_generation: {
        enabled: false,
        primary: 'openai/gpt-image-1',
        providers: {
          openai: { api_key: '', api_key_env: 'OPENAI_API_KEY', base_url: '' },
          openrouter: { api_key: '', api_key_env: 'OPENROUTER_API_KEY' },
        },
      },
      audio: {
        enabled: false,
        tts: { voice: 'voice-1', model: 'eleven_v2', language_code: '' },
        providers: { elevenlabs: { api_key: '', api_key_env: 'ELEVENLABS_API_KEY' } },
      },
    },
    status: {
      searchConfigured: true,
      searchSource: 'not_required',
      imageGenerationEnabled: false,
      imageGenerationProvider: '',
      imageGenerationPrimary: 'openai/gpt-image-1',
      audioEnabled: false,
      audioProvider: '',
    },
  }
}

const SET = new Set(['OPENROUTER_API_KEY', 'SURPLUS_API_KEY'])
const ctx: KeyContext = { envSet: (name) => SET.has(name), xaiLogin: false }
const unknownEnv: KeyContext = { envSet: () => undefined }

describe('search', () => {
  it('offers only runtime-supported services and starts on the saved one', () => {
    const s = snapshot()
    expect(searchDef.specs(s).map((p) => p.providerId)).toEqual(['brave', 'duckduckgo'])
    const draft = seedDraft(searchDef, s)
    expect(draft.providerId).toBe('duckduckgo')
    // A keyless service shows no key field at all.
    expect(searchDef.fields(DDG).some((f) => f.name === 'api_key')).toBe(false)
    expect(draft.values.max_results).toBe('5')
  })

  it('switching to a keyed service keeps the shared values and reads its env variable', () => {
    const s = snapshot()
    const draft = seedDraft(searchDef, s)
    draft.values.max_results = '8'
    const brave = switchProvider(searchDef, s, draft, 'brave')
    expect(brave.values.max_results).toBe('8')
    expect(brave.values.api_key_env).toBe('BRAVE_SEARCH_API_KEY')
    expect(draftDirty(seedDraft(searchDef, s), brave)).toBe(true)
  })

  it('a keyed service with nothing behind it cannot be saved; a pasted key can', () => {
    const s = snapshot()
    const brave = switchProvider(searchDef, s, seedDraft(searchDef, s), 'brave')
    expect(keySource(searchDef, BRAVE, brave, s, ctx)).toBe('missing')
    expect(saveProblem(searchDef, BRAVE, brave, s, ctx)).toEqual({
      kind: 'key',
      envKey: 'BRAVE_SEARCH_API_KEY',
    })
    // The environment not read yet: never block on a guess.
    expect(saveProblem(searchDef, BRAVE, brave, s, unknownEnv)).toBeNull()

    const pasted = { ...brave, values: { ...brave.values, api_key: ' sk-test ' } }
    expect(keySource(searchDef, BRAVE, pasted, s, ctx)).toBe('typed')
    expect(saveProblem(searchDef, BRAVE, pasted, s, ctx)).toBeNull()
    const params = buildParams(searchDef, BRAVE, pasted)
    expect(params).toMatchObject({ providerId: 'brave', apiKey: 'sk-test', maxResults: 5 })
    // A pasted key replaces the env reference: the gateway refuses both.
    expect(params).not.toHaveProperty('apiKeyEnv')
  })

  it('a stored key for the saved service counts and is kept by a blank paste', () => {
    const s = snapshot()
    s.config!.search_provider = 'brave'
    s.config!.search_api_key = '[redacted]'
    const draft = seedDraft(searchDef, s)
    expect(draft.values.api_key_env).toBe('')
    expect(keySource(searchDef, BRAVE, draft, s, ctx)).toBe('stored')
    expect(buildParams(searchDef, BRAVE, draft)).not.toHaveProperty('apiKey')
  })

  it('results per search stay within what the engine and the services take (1 to 20)', () => {
    const s = snapshot()
    const draft = seedDraft(searchDef, s)
    const problem = (max_results: string) =>
      saveProblem(searchDef, DDG, { ...draft, values: { ...draft.values, max_results } }, s, ctx)
    const range = { kind: 'range', field: 'max_results', min: 1, max: 20 }
    expect(problem('0')).toEqual(range)
    expect(problem('500')).toEqual(range)
    expect(problem('21')).toEqual(range)
    expect(problem('-3')).toEqual(range)
    expect(problem('1')).toBeNull()
    expect(problem('20')).toBeNull()
    expect(problem('x')).toEqual({ kind: 'number', field: 'max_results' })
    expect(problem('2.5')).toEqual({ kind: 'number', field: 'max_results' })
    // Cleared: the gateway would put its own default back, not the saved value.
    expect(problem('')).toEqual(range)
    expect(problem('  ')).toEqual(range)
  })

  it('status: on with no key needed', () => {
    expect(capabilityStatus(searchDef, snapshot(), ctx)).toMatchObject({
      state: 'on',
      providerLabel: 'DuckDuckGo',
      source: 'none',
    })
  })
})

describe('image generation', () => {
  it('starts off, on the saved provider, with the switch out of the field list', () => {
    const s = snapshot()
    const draft = seedDraft(imageDef, s)
    expect(draft).toMatchObject({ providerId: 'openai', enabled: false })
    expect(imageDef.fields(OPENAI_IMG).map((f) => f.name)).toEqual([
      'primary',
      'api_key',
      'api_key_env',
      'base_url',
    ])
    expect(capabilityStatus(imageDef, s, ctx).state).toBe('off')
  })

  it('turning on moves to the service whose key is already in place', () => {
    const s = snapshot()
    const on = enableDraft(imageDef, s, seedDraft(imageDef, s), ctx)
    expect(on).toMatchObject({ providerId: 'openrouter', enabled: true })
    expect(on.values.primary).toBe('openrouter/google/gemini-image')
    expect(keySource(imageDef, OPENROUTER_IMG, on, s, ctx)).toBe('env')
    expect(saveProblem(imageDef, OPENROUTER_IMG, on, s, ctx)).toBeNull()
    expect(buildParams(imageDef, OPENROUTER_IMG, on)).toMatchObject({
      providerId: 'openrouter',
      enabled: true,
      primary: 'openrouter/google/gemini-image',
      apiKeyEnv: 'OPENROUTER_API_KEY',
    })
    expect(keyReady(imageDef, OPENAI_IMG, s, ctx)).toBe(false)
    expect(keyReady(imageDef, OPENROUTER_IMG, s, ctx)).toBe(true)
  })

  it('a model of another service is refused; switching off needs no key', () => {
    const s = snapshot()
    const draft = { ...seedDraft(imageDef, s), enabled: true }
    draft.values.api_key = 'sk-x'
    draft.values.primary = 'openrouter/google/gemini-image'
    expect(saveProblem(imageDef, OPENAI_IMG, draft, s, ctx)).toEqual({
      kind: 'model-prefix',
      prefix: 'openai/',
    })
    const off = { ...seedDraft(imageDef, s), enabled: false }
    expect(saveProblem(imageDef, OPENAI_IMG, off, s, ctx)).toBeNull()
    expect(buildParams(imageDef, OPENAI_IMG, off)).toMatchObject({
      providerId: 'openai',
      enabled: false,
      primary: 'openai/gpt-image-1',
    })
  })

  it('reuses the chat provider key of the same name', () => {
    const s = snapshot()
    s.config!.llm = { provider: 'openai', api_key: '[redacted]' }
    const draft = { ...seedDraft(imageDef, s), enabled: true }
    expect(keySource(imageDef, OPENAI_IMG, draft, s, ctx)).toBe('provider')
  })

  it('status: on when the gateway says configured, key when it does not', () => {
    const s = snapshot()
    s.config!.image_generation = { ...s.config!.image_generation, enabled: true }
    s.status = {
      ...s.status,
      imageGenerationEnabled: true,
      imageGenerationConfigured: false,
      imageGenerationProvider: 'openai',
    }
    expect(capabilityStatus(imageDef, s, ctx).state).toBe('key')
    s.status.imageGenerationConfigured = true
    expect(capabilityStatus(imageDef, s, ctx)).toMatchObject({
      state: 'on',
      providerLabel: 'OpenAI Images',
    })
  })
})

describe('voice', () => {
  it('seeds the speech settings and blocks a save with no key anywhere', () => {
    const s = snapshot()
    const draft = enableDraft(voiceDef, s, seedDraft(voiceDef, s), ctx)
    // Nothing else to move to: stays on ElevenLabs.
    expect(draft).toMatchObject({ providerId: 'elevenlabs', enabled: true })
    expect(draft.values.tts_voice).toBe('voice-1')
    expect(saveProblem(voiceDef, ELEVEN, draft, s, ctx)).toEqual({
      kind: 'key',
      envKey: 'ELEVENLABS_API_KEY',
    })
  })
})

describe('X search', () => {
  it('has no provider id in its params and an explicit on/off', () => {
    const s = snapshot()
    const draft = seedDraft(xSearchDef, s)
    expect(draft.enabled).toBe(true)
    const params = buildParams(xSearchDef, XSEARCH, { ...draft, enabled: false })
    expect(params).not.toHaveProperty('providerId')
    expect(params).toMatchObject({ enabled: false, model: 'grok-4.5', timeoutSeconds: 180 })
  })

  it("timeouts and retries stay within the engine's XSearchConfig bounds", () => {
    const field = (name: string) => ({ name, type: 'int' as const })
    expect(fieldProblem(field('timeout_seconds'), '29')).toEqual({
      kind: 'range',
      field: 'timeout_seconds',
      min: 30,
      max: 300,
    })
    expect(fieldProblem(field('timeout_seconds'), '300')).toBeNull()
    expect(fieldProblem(field('total_timeout_seconds'), '601')).toMatchObject({ max: 600 })
    expect(fieldProblem(field('retries'), '0')).toBeNull()
    expect(fieldProblem(field('retries'), '6')).toMatchObject({ kind: 'range', min: 0, max: 5 })
    expect(Object.keys(NUMBER_BOUNDS).sort()).toEqual([
      'max_results',
      'retries',
      'timeout_seconds',
      'total_timeout_seconds',
    ])
  })

  it('a SuperGrok login stands in for the key', () => {
    const s = snapshot()
    expect(capabilityStatus(xSearchDef, s, ctx).state).toBe('key')
    expect(capabilityStatus(xSearchDef, s, { ...ctx, xaiLogin: true })).toMatchObject({
      state: 'on',
      source: 'login',
    })
    s.config!.x_search = { ...s.config!.x_search, enabled: false }
    expect(capabilityStatus(xSearchDef, s, ctx).state).toBe('off')
  })
})

describe('helpers', () => {
  it('collectParams leaves out a blank secret and a cleared number', () => {
    expect(
      collectParams(
        'brave',
        [
          { name: 'api_key', type: 'password', secret: true },
          { name: 'max_results', type: 'int' },
          { name: 'diagnostics', type: 'bool' },
        ],
        { api_key: '  ', max_results: '', diagnostics: true },
      ),
    ).toEqual({ providerId: 'brave', diagnostics: true })
  })

  it('keyUrl prefers the gateway link, then known pages', () => {
    expect(
      keyUrl(BRAVE, (n) => (n === 'BRAVE_SEARCH_API_KEY' ? 'https://brave.example' : '')),
    ).toBe('https://brave.example')
    expect(keyUrl(OPENROUTER_IMG, () => undefined)).toBe('https://openrouter.ai/keys')
    expect(keyUrl(ELEVEN, () => undefined)).toContain('elevenlabs.io')
    expect(keyUrl({ providerId: 'nobody' }, () => undefined)).toBeNull()
  })

  it('shortLabel drops the capability suffix', () => {
    expect(shortLabel('OpenRouter Images')).toBe('OpenRouter')
    expect(shortLabel('ElevenLabs Audio')).toBe('ElevenLabs')
    expect(shortLabel('DuckDuckGo')).toBe('DuckDuckGo')
  })
})

describe('search test failures', () => {
  it.each([
    [{ kind: 'auth', message: 'Brave search API key not set' }, 'noKey'],
    [{ kind: 'auth', message: "Client error '401 Unauthorized' for url 'https://x'" }, 'rejected'],
    [{ kind: 'auth', message: "Client error '403 Forbidden' for url 'https://x'" }, 'rejected'],
    [
      {
        kind: 'http',
        message:
          "Client error '422 Unprocessable Entity' for url 'https://api.search.brave.com/res/v1/web/search?q=AgentOS&count=3'\nFor more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/422",
      },
      'refused',
    ],
    [{ kind: 'http', message: "Client error '400 Bad Request' for url 'https://x'" }, 'refused'],
    [{ kind: 'rate_limit', message: "Client error '429 Too Many Requests'" }, 'rateLimit'],
    [{ kind: 'timeout', message: 'ReadTimeout' }, 'timeout'],
    [{ kind: 'network', message: 'All connection attempts failed' }, 'network'],
    [{ kind: 'http', message: "Server error '502 Bad Gateway' for url 'https://x'" }, 'server'],
    [{ kind: '', message: 'something odd' }, 'other'],
    [{}, 'other'],
  ] as const)('%j → %s', (error, expected) => {
    expect(searchFailure(error)).toBe(expected)
  })
})
