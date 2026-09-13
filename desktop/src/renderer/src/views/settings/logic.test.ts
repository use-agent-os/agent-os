import { describe, expect, it } from 'vitest'
import { DEFAULT_SETTINGS, isValidHost, normalizeSettings } from '@shared/settings'
import {
  diagnosticsReport,
  draftFromGateway,
  formatUptime,
  gatewayDirty,
  gatewayDraftErrors,
  gatewayFromDraft,
  gatewayNeedsRestart,
  modelOptions,
  providerConfigurePayload,
  providerDirty,
  providerDraft,
  providerNeedsKey,
  orderProviders,
  providerState,
  RECOMMENDED_PROVIDER,
  customEndpointErrors,
  customEndpointPatch,
  customProviderSpec,
  routerDirty,
  routerDraft,
  safetyNetValid,
  textTiers,
} from './logic'
import { isSettingsSection, SETTINGS_GROUPS, SETTINGS_SECTIONS } from './sections'
import type { Catalog, SetupConfig } from '@/views/setup/logic'

const SPEC = {
  providerId: 'opencap',
  label: 'OpenCAP',
  runtimeSupported: true,
  routerSupported: true,
  requiresApiKey: true,
  envKey: 'OPENCAP_API_KEY',
  defaultBaseUrl: 'https://gw.example/v1',
  defaultDirectModel: 'gpt-5.6-luna',
  fields: [],
}
const CONFIG: SetupConfig = {
  llm: {
    provider: 'opencap',
    model: 'gpt-5.6-luna',
    api_key: '[redacted]',
    api_key_env: 'OPENCAP_API_KEY',
    base_url: 'https://gw.example/v1',
    proxy: '',
    thinking: null,
  },
  agentos_router: {
    enabled: true,
    strategy: 'pilot-v1',
    default_tier: 'c2',
    judge_model: undefined,
    judge_base_url: undefined,
    translate_ceiling_enabled: false,
    translate_ceiling_tier: 'c0',
    pilot: { safety_net_threshold: 0.7 },
    tiers: {
      c1: { provider: 'opencap', model: 'custom-c1', thinking_level: 'low' },
      image_model: { provider: 'opencap', model: 'gpt-5.6-luna', supports_image: true },
    },
  },
}
const CATALOG: Catalog = {
  providers: [SPEC],
  routerProfiles: {
    defaultTier: 'c1',
    profiles: [
      {
        providerId: 'opencap',
        tiers: {
          c0: { model: 'deepseek-v4-flash', thinkingLevel: 'high', description: 'fast' },
          c1: { model: 'gpt-5.6-luna', thinkingLevel: 'high', description: 'balanced' },
          c2: { model: 'glm-5.3', thinkingLevel: 'high', description: 'strong' },
          c3: { model: 'claude-opus-5', thinkingLevel: 'high', description: 'frontier' },
          image_model: { model: 'minimax-m3', thinkingLevel: 'medium', description: 'vision' },
        },
      },
    ],
  },
}

const GW = DEFAULT_SETTINGS.gateway

describe('gateway draft', () => {
  it('round-trips the saved settings', () => {
    expect(gatewayFromDraft(draftFromGateway(GW))).toEqual(GW)
    expect(gatewayDirty(GW, draftFromGateway(GW))).toBe(false)
  })

  it('treats an empty token or cli path as null', () => {
    const next = gatewayFromDraft({ ...draftFromGateway(GW), token: '  ', cliPath: '' })
    expect(next.token).toBeNull()
    expect(next.cliPath).toBeNull()
  })

  it('flags a bad host and a bad port', () => {
    expect(gatewayDraftErrors({ ...draftFromGateway(GW), host: 'http://x' })).toEqual({
      host: 'invalid',
    })
    expect(gatewayDraftErrors({ ...draftFromGateway(GW), port: '70000' })).toEqual({
      port: 'invalid',
    })
    expect(gatewayDraftErrors({ ...draftFromGateway(GW), port: '80a' })).toEqual({
      port: 'invalid',
    })
    expect(gatewayDraftErrors(draftFromGateway(GW))).toEqual({})
  })

  it('is dirty when anything meaningful changes', () => {
    expect(gatewayDirty(GW, { ...draftFromGateway(GW), port: '18792' })).toBe(true)
    expect(gatewayDirty(GW, { ...draftFromGateway(GW), mode: 'external' })).toBe(true)
    expect(gatewayDirty(GW, { ...draftFromGateway(GW), host: ' 127.0.0.1 ' })).toBe(false)
  })

  it('asks for a restart only when the running endpoint differs', () => {
    const running = {
      state: 'running' as const,
      pid: 1,
      url: 'http://127.0.0.1:18791',
      error: null,
    }
    expect(gatewayNeedsRestart(GW, running)).toBe(false)
    expect(gatewayNeedsRestart({ ...GW, port: 19000 }, running)).toBe(true)
    expect(gatewayNeedsRestart({ ...GW, port: 19000 }, { ...running, state: 'stopped' })).toBe(
      false,
    )
  })
})

describe('host validation', () => {
  it('accepts hostnames and IPs, rejects urls and ports', () => {
    expect(isValidHost('localhost')).toBe(true)
    expect(isValidHost('127.0.0.1')).toBe(true)
    expect(isValidHost('[::1]')).toBe(true)
    expect(isValidHost('gateway.local')).toBe(true)
    expect(isValidHost('http://localhost')).toBe(false)
    expect(isValidHost('localhost:18791')).toBe(false)
    expect(isValidHost('')).toBe(false)
  })

  it('normalizes new sections with defaults and drops junk', () => {
    const s = normalizeSettings({
      general: { openAtLogin: 'yes', launchView: 'last' },
      appearance: { uiScale: 133, reduceTransparency: true },
      notifications: { sound: false },
    })
    expect(s.general).toEqual({ ...DEFAULT_SETTINGS.general, launchView: 'last' })
    expect(s.appearance).toEqual({ uiScale: 100, reduceTransparency: true })
    expect(s.notifications).toEqual({ ...DEFAULT_SETTINGS.notifications, sound: false })
  })
})

describe('models', () => {
  const catalog = [
    { id: 'a/one', name: 'One', provider: 'p' },
    { id: 'a/two', name: 'a/two', provider: 'p' },
    { id: 'b/three', name: 'Three', provider: 'other' },
    { id: 'a/one', name: 'One again', provider: 'p' },
  ]

  it('lists only the active provider, once each, and keeps the current model', () => {
    const opts = modelOptions(catalog, 'p', 'a/two')
    expect(opts.map((o) => o.id)).toEqual(['a/one', 'a/two'])
    expect(opts[0]).toMatchObject({ label: 'a/one', title: 'One' })
    expect(opts[1]).toMatchObject({ label: 'a/two', title: '' })
  })

  it('prepends an unknown current model as custom', () => {
    const opts = modelOptions(catalog, 'p', 'typed/by-hand')
    expect(opts[0]).toEqual({
      id: 'typed/by-hand',
      label: 'typed/by-hand',
      title: '',
      custom: true,
    })
  })
})

describe('provider form', () => {
  it('seeds from the saved provider without the secret', () => {
    const d = providerDraft(CONFIG, SPEC)
    expect(d).toEqual({
      providerId: 'opencap',
      model: 'gpt-5.6-luna',
      apiKey: '',
      apiKeyEnv: 'OPENCAP_API_KEY',
      baseUrl: 'https://gw.example/v1',
      proxy: '',
    })
    expect(providerDirty(d, d)).toBe(false)
  })

  it('seeds a different provider from the catalog defaults', () => {
    const d = providerDraft(CONFIG, { ...SPEC, providerId: 'openai', envKey: 'OPENAI_API_KEY' })
    expect(d.apiKeyEnv).toBe('OPENAI_API_KEY')
    expect(d.baseUrl).toBe('https://gw.example/v1')
    expect(d.model).toBe('gpt-5.6-luna')
    expect(providerNeedsKey(d, { ...SPEC, providerId: 'openai' }, CONFIG)).toBe(false)
    expect(
      providerNeedsKey({ ...d, apiKeyEnv: '' }, { ...SPEC, providerId: 'openai' }, CONFIG),
    ).toBe(true)
  })

  it('seeds a provider used before from its saved profile', () => {
    const openaiProfile = {
      model: 'gpt-5.6-terra',
      api_key_env: 'MY_OPENAI_KEY',
      base_url: 'https://x/v1',
    }
    const config: SetupConfig = { ...CONFIG, provider_profiles: { openai: openaiProfile } }
    const spec = { ...SPEC, providerId: 'openai', envKey: 'OPENAI_API_KEY' }
    const d = providerDraft(config, spec)
    expect(d).toMatchObject({
      model: 'gpt-5.6-terra',
      apiKeyEnv: 'MY_OPENAI_KEY',
      baseUrl: 'https://x/v1',
    })
    expect(providerState(spec, config, 'opencap')).toEqual({
      active: false,
      profile: openaiProfile,
    })
    expect(providerState(SPEC, config, 'opencap')).toEqual({ active: true, profile: null })
  })

  it('never sends both a pasted key and an env reference', () => {
    const d = providerDraft(CONFIG, SPEC)
    expect(providerConfigurePayload(d)).toEqual({
      providerId: 'opencap',
      model: 'gpt-5.6-luna',
      baseUrl: 'https://gw.example/v1',
      proxy: '',
      apiKeyEnv: 'OPENCAP_API_KEY',
    })
    const pasted = providerConfigurePayload({ ...d, apiKey: ' sk-1 ' })
    expect(pasted.apiKey).toBe('sk-1')
    expect(pasted).not.toHaveProperty('apiKeyEnv')
    expect(providerDirty(d, { ...d, apiKey: 'x' })).toBe(true)
  })
})

describe('custom endpoint', () => {
  const spec = customProviderSpec('Custom endpoint', 'need')
  const base = providerDraft({}, spec)

  it('requires an http(s) base URL and a model id', () => {
    expect(customEndpointErrors(base)).toEqual({ baseUrl: 'invalid', model: 'missing' })
    expect(customEndpointErrors({ ...base, baseUrl: 'localhost:8000', model: 'x' })).toEqual({
      baseUrl: 'invalid',
    })
    expect(
      customEndpointErrors({ ...base, baseUrl: 'http://localhost:8000/v1', model: 'x' }),
    ).toEqual({})
  })

  it('clears a foreign key when switching in, keeps it when already active', () => {
    const d = { ...base, baseUrl: 'http://h/v1', model: 'm' }
    expect(customEndpointPatch(d, false)).toEqual({
      patch: {
        llm: {
          provider: 'vllm',
          base_url: 'http://h/v1',
          model: 'm',
          proxy: '',
          api_key_env: '',
          api_key: '',
        },
        agentos_router: { tier_profile: null },
      },
    })
    expect(customEndpointPatch(d, true)).toEqual({
      patch: {
        llm: { provider: 'vllm', base_url: 'http://h/v1', model: 'm', proxy: '', api_key_env: '' },
        agentos_router: { tier_profile: null },
      },
    })
    expect(customEndpointPatch({ ...d, apiKey: ' k ' }, true).patch).toMatchObject({
      llm: { api_key: 'k' },
    })
  })
})

describe('router form', () => {
  it('lays saved tiers over the catalog profile, in ladder order', () => {
    const d = routerDraft(CONFIG, CATALOG, 'opencap')
    expect(d.mode).toBe('pilot-v1')
    expect(d.defaultTier).toBe('c2')
    expect(d.safetyNet).toBe('0.7')
    expect(d.translateCeiling).toBe('off')
    expect(d.tiers.map((r) => r.tier)).toEqual(['c0', 'c1', 'c2', 'c3', 'image_model'])
    expect(d.tiers[1]).toMatchObject({ model: 'custom-c1', thinkingLevel: 'low' })
    expect(d.tiers[0]).toMatchObject({ model: 'deepseek-v4-flash', description: 'fast' })
    expect(d.tiers[4]).toMatchObject({ model: 'gpt-5.6-luna', supportsImage: true })
    expect(textTiers(d)).toHaveLength(4)
    expect(routerDirty(d, d)).toBe(false)
    expect(routerDirty(d, { ...d, defaultTier: 'c1' })).toBe(true)
  })

  it('falls back to catalog defaults when nothing is saved', () => {
    const d = routerDraft({}, CATALOG, 'opencap')
    expect(d.mode).toBe('pilot-v1')
    expect(d.defaultTier).toBe('c1')
    expect(d.safetyNet).toBe('0.5')
    expect(d.translateCeiling).toBe('c0')
    expect(d.tiers).toHaveLength(5)
    expect(routerDraft({}, CATALOG, 'nope').tiers).toEqual([])
  })

  it('validates the safety net as a 0..1 number', () => {
    expect(safetyNetValid('0.5')).toBe(true)
    expect(safetyNetValid('1')).toBe(true)
    expect(safetyNetValid('1.2')).toBe(false)
    expect(safetyNetValid('abc')).toBe(false)
  })
})

describe('sections', () => {
  it('lists every section exactly once across the rail groups', () => {
    const inGroups = SETTINGS_GROUPS.flatMap((g) => g.sections)
    expect([...inGroups].sort()).toEqual([...SETTINGS_SECTIONS].sort())
    expect(new Set(inGroups).size).toBe(inGroups.length)
    expect(isSettingsSection('router')).toBe(true)
    expect(isSettingsSection('nope')).toBe(false)
  })
})

describe('about + advanced', () => {
  it('formats uptime at the right granularity', () => {
    expect(formatUptime(40_000)).toBe('40s')
    expect(formatUptime(12 * 60_000)).toBe('12m')
    expect(formatUptime((2 * 3600 + 5 * 60) * 1000)).toBe('2h 05m')
    expect(formatUptime((3 * 86400 + 4 * 3600) * 1000)).toBe('3d 4h')
  })

  it('never includes the auth token in diagnostics', () => {
    const report = diagnosticsReport({
      info: null,
      gateway: { state: 'running', pid: 42, url: 'http://127.0.0.1:18791', error: null },
      settings: { ...DEFAULT_SETTINGS, gateway: { ...GW, token: 'sekrit-token' } },
      now: new Date(0),
    })
    expect(report).not.toContain('sekrit-token')
    expect(report).toContain('<redacted>')
    expect(report).toContain('pid 42')
  })
})

describe('orderProviders', () => {
  it('puts the recommended provider first and keeps the rest in catalog order', () => {
    const ordered = orderProviders([
      { providerId: 'openai' },
      { providerId: 'anthropic' },
      { providerId: RECOMMENDED_PROVIDER },
      { providerId: 'ollama' },
    ])
    expect(ordered.map((p) => p.providerId)).toEqual([
      RECOMMENDED_PROVIDER,
      'openai',
      'anthropic',
      'ollama',
    ])
  })

  it('is a no-op when the recommended provider is absent', () => {
    expect(
      orderProviders([{ providerId: 'a' }, { providerId: 'b' }]).map((p) => p.providerId),
    ).toEqual(['a', 'b'])
  })
})
