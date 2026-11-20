import { describe, expect, it } from 'vitest'
import type { SettingsSnapshot } from '@/views/settings/snapshot'
import {
  hostOf,
  setupItems,
  summarizeSetup,
  type SetupInput,
  type SetupItem,
  type SetupItemId,
} from './setup-items'

const CATALOG: SettingsSnapshot['catalog'] = {
  providers: [{ providerId: 'surplus', label: 'Surplus Intelligence' }],
  searchProviders: [
    { providerId: 'duckduckgo', label: 'DuckDuckGo' },
    { providerId: 'brave', label: 'Brave Search' },
  ],
  imageGenerationProviders: [{ providerId: 'openrouter', label: 'OpenRouter Images' }],
  audioProviders: [{ providerId: 'elevenlabs', label: 'ElevenLabs Audio' }],
  memoryEmbeddingProviders: [{ providerId: 'openai', label: 'OpenAI' }],
  channels: [
    { type: 'telegram', label: 'Telegram' },
    { type: 'slack', label: 'Slack' },
  ],
}

const ok = (detail = '') => ({ status: 'ok', actionRequired: false, blocking: false, detail })
const optional = () => ({ status: 'optional', actionRequired: false, blocking: false })

/** A gateway that has only just been installed: no provider, nothing on. */
function fresh(): SettingsSnapshot {
  return {
    catalog: CATALOG,
    revision: 'r1',
    config: {
      llm: { provider: '', model: '' },
      agentos_router: { enabled: true, strategy: 'pilot-v1', tiers: {} },
      memory: { embedding: { provider: 'none' } },
      search_provider: '',
      image_generation: { primary: 'openrouter/google/gemini' },
      audio: { enabled: false },
      channels: { channels: [] },
    },
    status: {
      hasConfig: true,
      llmConfigured: false,
      llmSource: 'none',
      needsOnboarding: true,
      searchConfigured: false,
      imageGenerationEnabled: false,
      audioEnabled: false,
      memoryEmbeddingProvider: 'none',
      channelCount: 0,
      sectionDetails: {
        llm: { status: 'missing', required: true, actionRequired: true, blocking: true },
        router: ok('uses Pilot Router after provider setup'),
        search: optional(),
        channels: optional(),
        image_generation: optional(),
        audio: optional(),
        memory_embedding: optional(),
      },
    },
  }
}

/** Only a provider: the state right after the first-run wizard. */
function providerOnly(): SettingsSnapshot {
  const s = fresh()
  s.config!.llm = { provider: 'surplus', model: 'gpt-6-luna', api_key_env: 'SURPLUS_API_KEY' }
  s.config!.agentos_router = {
    enabled: true,
    strategy: 'pilot-v1',
    tiers: { c0: {}, c1: {}, c2: {}, c3: {}, image_model: {} },
  }
  s.status = {
    ...s.status,
    llmConfigured: true,
    llmSource: 'env',
    needsOnboarding: false,
    sectionDetails: {
      ...s.status!.sectionDetails,
      llm: ok('env key visible: SURPLUS_API_KEY'),
      router: ok('Pilot Router profile: surplus'),
    },
  }
  return s
}

/** Everything a person can turn on, on and working. */
function everything(): SettingsSnapshot {
  const s = providerOnly()
  s.config = {
    ...s.config,
    memory: { embedding: { provider: 'auto' } },
    search_provider: 'brave',
    image_generation: { enabled: true, primary: 'openrouter/google/gemini-image' },
    audio: { enabled: true },
    channels: { channels: [{ type: 'telegram' }, { type: 'slack' }, { type: 'telegram' }] },
    trading: { enabled: true },
  }
  s.status = {
    ...s.status,
    searchConfigured: true,
    searchSource: 'env',
    imageGenerationEnabled: true,
    imageGenerationConfigured: true,
    imageGenerationProvider: 'openrouter',
    imageGenerationPrimary: 'openrouter/google/gemini-image',
    imageGenerationSource: 'env',
    audioEnabled: true,
    audioConfigured: true,
    audioProvider: 'elevenlabs',
    memoryEmbeddingProvider: 'auto',
    memoryEmbeddingConfigured: true,
    channelCount: 3,
    sectionDetails: {
      llm: ok(),
      router: ok('Pilot Router profile: surplus'),
      search: ok(),
      channels: ok(),
      image_generation: ok(),
      audio: ok(),
      memory_embedding: ok(),
    },
  }
  return s
}

function input(snapshot: SettingsSnapshot | undefined, patch: Partial<SetupInput> = {}) {
  return {
    connected: true,
    gateway: { state: 'running' as const, external: false, url: 'http://127.0.0.1:18791' },
    snapshot,
    skills: { total: 12, needs: 0 },
    env: { missing: [] },
    wallet: { initialized: true, walletCount: 1 },
    updates: {
      checked: true,
      engineOutdated: false,
      engineLatest: null,
      appAvailable: false,
      appReady: false,
      appLatest: null,
    },
    ...patch,
  }
}

function byId(items: SetupItem[]): Record<SetupItemId, SetupItem> {
  return Object.fromEntries(items.map((i) => [i.id, i])) as Record<SetupItemId, SetupItem>
}

describe('setupItems', () => {
  it('nothing configured: the provider is the one required step and leads', () => {
    const items = setupItems(input(fresh(), { skills: { total: 0, needs: 0 }, wallet: null }))
    const it_ = byId(items)
    expect(it_.gateway.state).toBe('done')
    expect(it_.provider).toMatchObject({ state: 'todo', group: 'required' })
    expect(it_.provider.detail).toBe('No provider yet')
    expect(it_.provider.jump).toEqual({ kind: 'section', section: 'providers' })
    expect(it_.router).toMatchObject({ state: 'todo', detail: 'Waits for a provider' })
    expect(it_.memory).toMatchObject({ state: 'todo', detail: 'Keyword search only' })
    expect(it_.search).toMatchObject({ state: 'todo', detail: 'Off' })
    expect(it_.image.state).toBe('todo')
    expect(it_.voice.state).toBe('todo')
    expect(it_.channels).toMatchObject({ state: 'todo', detail: 'No channels yet' })
    expect(it_.skills).toMatchObject({ state: 'done', detail: 'None installed' })
    // Trading is not configured at all here: no wallet row.
    expect(it_.wallet).toBeUndefined()

    const summary = summarizeSetup(items)
    expect(summary.phase).toBe('blocked')
    expect(summary.next?.id).toBe('provider')
    expect(summary.toFinish).toBe(1)
  })

  it('provider only: required is done, the hero suggests web search', () => {
    const items = setupItems(input(providerOnly()))
    const it_ = byId(items)
    expect(it_.provider).toMatchObject({
      state: 'done',
      detail: 'Surplus Intelligence · gpt-6-luna',
    })
    expect(it_.router).toMatchObject({ state: 'done', detail: 'Pilot Router on · 4 tiers' })
    expect(it_.search.jump).toEqual({ kind: 'section', section: 'capabilities', focus: 'search' })

    const summary = summarizeSetup(items)
    expect(summary.phase).toBe('optional')
    expect(summary.toFinish).toBe(0)
    expect(summary.next?.id).toBe('search')
  })

  it('everything ready: every row is done and the summary is complete', () => {
    const items = setupItems(input(everything()))
    expect(items.every((i) => i.state === 'done')).toBe(true)
    const it_ = byId(items)
    expect(it_.search.detail).toBe('Brave Search')
    expect(it_.image.detail).toBe('OpenRouter Images · google/gemini-image')
    expect(it_.voice.detail).toBe('ElevenLabs Audio')
    expect(it_.memory.detail).toBe('Automatic · finds by meaning')
    expect(it_.channels.detail).toBe('Telegram, Slack')
    expect(it_.wallet.detail).toBe('1 wallet')
    expect(it_.updates.detail).toBe('Up to date')

    const summary = summarizeSetup(items)
    expect(summary).toMatchObject({ phase: 'complete', next: null, toFinish: 0 })
    expect(summary.done).toBe(summary.total)
  })

  it('a degraded section is a warning that names the missing key', () => {
    const s = everything()
    s.status = {
      ...s.status,
      searchConfigured: false,
      searchSource: 'missing_env',
      searchEnvKey: 'BRAVE_SEARCH_API_KEY',
      sectionDetails: {
        ...s.status!.sectionDetails,
        search: {
          status: 'degraded',
          actionRequired: true,
          blocking: false,
          detail: 'env key not visible: BRAVE_SEARCH_API_KEY',
        },
      },
    }
    const items = setupItems(input(s))
    expect(byId(items).search).toMatchObject({
      state: 'attention',
      detail: 'Brave Search · BRAVE_SEARCH_API_KEY is not set',
      action: 'Fix',
    })
    const summary = summarizeSetup(items)
    expect(summary).toMatchObject({ phase: 'attention', toFinish: 1 })
    expect(summary.next?.id).toBe('search')
  })

  it('a provider whose key is gone blocks, even when the rest is fine', () => {
    const s = everything()
    s.status = {
      ...s.status,
      llmSource: 'missing_env',
      llmEnvKey: 'SURPLUS_API_KEY',
      sectionDetails: {
        ...s.status!.sectionDetails,
        llm: { status: 'degraded', required: true, actionRequired: true, blocking: true },
      },
    }
    const items = setupItems(input(s))
    expect(byId(items).provider).toMatchObject({
      state: 'attention',
      detail: 'Surplus Intelligence · SURPLUS_API_KEY is not set',
    })
    expect(summarizeSetup(items)).toMatchObject({ phase: 'blocked', toFinish: 1 })
  })

  it('offline: only the gateway row, and a managed gateway offers Start', () => {
    const items = setupItems(
      input(everything(), {
        connected: false,
        gateway: { state: 'stopped', external: false, url: null },
      }),
    )
    expect(items).toHaveLength(1)
    expect(items[0]).toMatchObject({
      id: 'gateway',
      state: 'attention',
      detail: 'Not running',
      action: 'Start',
      jump: { kind: 'start-gateway' },
    })
    const summary = summarizeSetup(items)
    expect(summary.phase).toBe('offline')
    expect(summary.next?.id).toBe('gateway')
  })

  it('offline external: points at Gateway settings instead of starting anything', () => {
    const [gateway] = setupItems(
      input(undefined, {
        connected: false,
        gateway: { state: 'error', external: true, url: 'http://10.0.0.5:18791' },
      }),
    )
    expect(gateway).toMatchObject({
      state: 'attention',
      detail: 'Not answering at 10.0.0.5:18791',
      jump: { kind: 'section', section: 'gateway' },
    })
  })

  it('facts still loading are checking and do not count', () => {
    const items = setupItems(
      input(providerOnly(), { skills: undefined, env: undefined, wallet: undefined }),
    )
    const it_ = byId(items)
    expect(it_.skills.state).toBe('checking')
    expect(it_.env.state).toBe('checking')
    const summary = summarizeSetup(items)
    expect(summary.total).toBe(items.length - 2)
  })

  it('unreadable side facts and unchecked updates are left out, not guessed', () => {
    const items = setupItems(
      input(everything(), {
        skills: null,
        env: null,
        wallet: null,
        updates: {
          checked: false,
          engineOutdated: false,
          engineLatest: null,
          appAvailable: false,
          appReady: false,
          appLatest: null,
        },
      }),
    )
    const ids = items.map((i) => i.id)
    expect(ids).not.toContain('skills')
    expect(ids).not.toContain('env')
    expect(ids).not.toContain('wallet')
    expect(ids).not.toContain('updates')
  })

  it('counts skills and variables that wait for setup, and a pending update', () => {
    const items = setupItems(
      input(everything(), {
        skills: { total: 70, needs: 13 },
        env: { missing: ['GMGN_API_KEY', 'GMGN_PRIVATE_KEY'] },
        wallet: { initialized: false, walletCount: 0 },
        updates: {
          checked: true,
          engineOutdated: true,
          engineLatest: '2026.11.21',
          appAvailable: false,
          appReady: false,
          appLatest: null,
        },
      }),
    )
    const it_ = byId(items)
    expect(it_.skills).toMatchObject({ state: 'todo', detail: '13 skills need a key or a tool' })
    expect(it_.env).toMatchObject({ state: 'todo', detail: '2 variables skills need are not set' })
    expect(it_.wallet).toMatchObject({ state: 'todo', detail: 'No wallet yet' })
    expect(it_.updates).toMatchObject({
      state: 'todo',
      detail: 'Engine 2026.11.21 is available',
      jump: { kind: 'section', section: 'about' },
    })
    // Skippable items never turn the head pill amber.
    expect(summarizeSetup(items).toFinish).toBe(0)
  })

  it('image generation on the provider key reads as such', () => {
    const s = everything()
    s.status = { ...s.status, imageGenerationSource: 'llm_fallback' }
    expect(byId(setupItems(input(s))).image.detail).toBe(
      'OpenRouter Images · uses your provider key',
    )
  })
})

describe('hostOf', () => {
  it('drops the scheme and a trailing slash', () => {
    expect(hostOf('http://127.0.0.1:18799/')).toBe('127.0.0.1:18799')
    expect(hostOf(null)).toBe('')
  })
})
