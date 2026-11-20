import { describe, expect, it } from 'vitest'
import type { SettingsSnapshot } from '@/views/settings/snapshot'
import {
  embeddingDirty,
  embeddingFields,
  embeddingParams,
  embeddingProblem,
  embeddingSpecs,
  factsTotalAfter,
  forgetCounts,
  hasStoredEmbeddingKey,
  isLoopbackHost,
  isOtherSource,
  limitsCheck,
  limitsDraft,
  limitsPatches,
  ollamaHasModel,
  promptBudgetNeeded,
  recallDraft,
  recallPatches,
  relativeTime,
  retrievalIsKeywordOnly,
  reviewDraft,
  reviewPatches,
  savedEmbeddingProvider,
  searchKind,
  seedEmbedding,
  uploadVerdict,
  type EmbeddingDraft,
} from './logic'

function snap(memory: Record<string, unknown>, extra: Partial<SettingsSnapshot> = {}) {
  return { config: { memory }, revision: 'r1', ...extra } as SettingsSnapshot
}

const draft = (patch: Partial<EmbeddingDraft>): EmbeddingDraft => ({
  provider: 'auto',
  model: '',
  baseUrl: '',
  apiKey: '',
  apiKeyEnv: '',
  onnxDir: '',
  ...patch,
})

describe('embedding engine', () => {
  it('shows each engine only the fields it needs', () => {
    expect(embeddingFields('none')).toEqual({ main: [], advanced: [] })
    expect(embeddingFields('ollama')).toEqual({ main: ['model', 'baseUrl'], advanced: [] })
    expect(embeddingFields('local')).toEqual({ main: [], advanced: ['onnxDir'] })
    expect(embeddingFields('openai-compatible').main).toEqual(['baseUrl', 'apiKey', 'model'])
    expect(embeddingFields('openai').advanced).toContain('apiKeyEnv')
    // Automatic needs nothing; its cloud fallback is optional and folded away.
    expect(embeddingFields('auto').main).toEqual([])
  })

  it('reads the saved engine, honouring a legacy mode under provider auto', () => {
    expect(savedEmbeddingProvider(snap({ embedding: { provider: 'ollama' } }))).toBe('ollama')
    expect(savedEmbeddingProvider(snap({ embedding: { provider: 'auto', mode: 'local' } }))).toBe(
      'local',
    )
    expect(savedEmbeddingProvider(snap({ embedding: { provider: 'bogus' } }))).toBe('auto')
    expect(savedEmbeddingProvider(undefined)).toBe('auto')
  })

  it('lists the catalog engines the gateway supports, in choice order', () => {
    const s = snap(
      {},
      {
        catalog: {
          memoryEmbeddingProviders: [
            { providerId: 'none' },
            { providerId: 'ollama' },
            { providerId: 'auto' },
            { providerId: 'mystery' },
            { providerId: 'openai', runtimeSupported: false },
          ],
        },
      },
    )
    expect(embeddingSpecs(s).map((x) => x.providerId)).toEqual(['auto', 'ollama', 'none'])
  })

  it('seeds a form from what is saved for that engine', () => {
    const s = snap({
      embedding: {
        provider: 'ollama',
        ollama: { base_url: 'http://10.0.0.2:11434', model: 'nomic-embed-text' },
        remote: { api_key_env: 'EMBED_KEY', model: 'text-embedding-3-large' },
      },
    })
    expect(seedEmbedding(s, 'ollama')).toMatchObject({
      provider: 'ollama',
      model: 'nomic-embed-text',
      baseUrl: 'http://10.0.0.2:11434',
    })
    expect(seedEmbedding(s, 'openai')).toMatchObject({
      model: 'text-embedding-3-large',
      apiKeyEnv: 'EMBED_KEY',
      apiKey: '',
    })
    // A key-needing engine with no saved env reference starts at the catalog's.
    const bare = snap({ embedding: { provider: 'auto' } })
    expect(
      seedEmbedding(bare, 'openai', {
        providerId: 'openai',
        requiresApiKey: true,
        envKey: 'OPENAI_API_KEY',
      }).apiKeyEnv,
    ).toBe('OPENAI_API_KEY')
  })

  it('sends every shown field, leaves a blank key out, and drops the env name for a pasted key', () => {
    expect(embeddingParams(draft({ provider: 'ollama' }))).toEqual({
      providerId: 'ollama',
      model: '',
      baseUrl: '',
    })
    expect(embeddingParams(draft({ provider: 'none' }))).toEqual({ providerId: 'none' })
    expect(
      embeddingParams(draft({ provider: 'openai', apiKeyEnv: 'OPENAI_API_KEY', model: ' m ' })),
    ).toEqual({ providerId: 'openai', model: 'm', apiKeyEnv: 'OPENAI_API_KEY', baseUrl: '' })
    const pasted = embeddingParams(
      draft({ provider: 'openai', apiKey: ' sk-1 ', apiKeyEnv: 'OPENAI_API_KEY' }),
    )
    expect(pasted.apiKey).toBe('sk-1')
    expect(pasted).not.toHaveProperty('apiKeyEnv')
  })

  it('stops a save the gateway would refuse', () => {
    expect(embeddingProblem(draft({ provider: 'openai' }), false)).toBe('needsKey')
    expect(embeddingProblem(draft({ provider: 'openai' }), true)).toBeNull()
    expect(embeddingProblem(draft({ provider: 'openai', apiKeyEnv: 'K' }), false)).toBeNull()
    expect(embeddingProblem(draft({ provider: 'openai-compatible', apiKey: 'k' }), false)).toBe(
      'needsUrl',
    )
    expect(embeddingProblem(draft({ provider: 'ollama' }), false)).toBeNull()
  })

  it('counts a switch of engine or a pasted key as a change', () => {
    const saved = draft({ provider: 'auto' })
    expect(embeddingDirty(saved, saved)).toBe(false)
    expect(embeddingDirty(saved, draft({ provider: 'ollama' }))).toBe(true)
    expect(embeddingDirty(saved, draft({ apiKey: 'sk' }))).toBe(true)
    // A field the engine does not show is not a change.
    expect(embeddingDirty(saved, draft({ onnxDir: '/x' }))).toBe(false)
  })

  it('knows a stored key, keyword-only retrieval, and the search kind', () => {
    expect(hasStoredEmbeddingKey(snap({ embedding: { remote: { api_key: '***' } } }))).toBe(true)
    expect(hasStoredEmbeddingKey(snap({ embedding: { remote: { api_key: null } } }))).toBe(false)
    expect(retrievalIsKeywordOnly(snap({ retrieval_mode: 'fts_only' }))).toBe(true)
    expect(searchKind({ vecAvailable: true, ftsAvailable: true })).toBe('meaning')
    expect(searchKind({ vecAvailable: false, ftsAvailable: true })).toBe('keywords')
    expect(searchKind({})).toBe('none')
  })

  it('matches an Ollama model with or without its tag', () => {
    const tags = ['embeddinggemma:latest', 'qwen3:4b']
    expect(ollamaHasModel(tags, 'embeddinggemma')).toBe(true)
    expect(ollamaHasModel(tags, 'embeddinggemma:latest')).toBe(true)
    expect(ollamaHasModel(tags, 'qwen3')).toBe(true)
    expect(ollamaHasModel(tags, 'nomic-embed-text')).toBe(false)
    expect(ollamaHasModel(tags, 'qwen3:8b')).toBe(false)
  })

  it('tells a host on this Mac from a remote one', () => {
    expect(isLoopbackHost('127.0.0.1')).toBe(true)
    expect(isLoopbackHost('localhost')).toBe(true)
    expect(isLoopbackHost('[::1]')).toBe(true)
    expect(isLoopbackHost('gw.example.com')).toBe(false)
  })
})

describe('recall and review drafts', () => {
  const config = {
    memory: {
      recall: {
        enabled: true,
        mode: 'escalate',
        max_injected_chars: 1800,
        deep_timeout_seconds: 8,
      },
      nudge: { enabled: true, interval: 10, before_compaction: true, timeout_seconds: 90 },
    },
  }

  it('patches only what changed in the recall card, by dot path', () => {
    const d = recallDraft(config)
    expect(recallPatches(config, d)).toEqual({})
    expect(recallPatches(config, { ...d, enabled: false, mode: 'always' })).toEqual({
      'memory.recall.enabled': false,
      'memory.recall.mode': 'always',
    })
    expect(recallPatches(config, { ...d, maxInjectedChars: '2400', model: ' fast-1 ' })).toEqual({
      'memory.recall.max_injected_chars': 2400,
      'memory.recall.model': 'fast-1',
    })
  })

  it('refuses recall values outside what the gateway accepts', () => {
    const d = recallDraft(config)
    expect(recallPatches(config, { ...d, maxInjectedChars: '100' })).toBeNull()
    expect(recallPatches(config, { ...d, maxInjectedChars: '9000' })).toBeNull()
    expect(recallPatches(config, { ...d, deepTimeoutSeconds: '0' })).toBeNull()
    expect(recallPatches(config, { ...d, deepTimeoutSeconds: '121' })).toBeNull()
  })

  it('shows a review with interval 0 as off, and turning it on writes an interval', () => {
    const zero = { memory: { nudge: { enabled: true, interval: 0 } } }
    const d = reviewDraft(zero)
    expect(d.enabled).toBe(false)
    expect(d.interval).toBe('10')
    expect(reviewPatches(zero, d)).toEqual({})
    expect(reviewPatches(zero, { ...d, enabled: true })).toEqual({ 'memory.nudge.interval': 10 })
  })

  it('turns the review off with enabled, and keeps the interval untouched', () => {
    const d = reviewDraft(config)
    expect(reviewPatches(config, { ...d, enabled: false, interval: '20' })).toEqual({
      'memory.nudge.enabled': false,
    })
    expect(reviewPatches(config, { ...d, interval: '20', beforeCompaction: false })).toEqual({
      'memory.nudge.interval': 20,
      'memory.nudge.before_compaction': false,
    })
    expect(reviewPatches(config, { ...d, interval: '0' })).toBeNull()
  })
})

describe('limits', () => {
  it('needs 6,304 characters of prompt for the default lists, as the docs say', () => {
    expect(promptBudgetNeeded(4000, 2000)).toBe(6304)
    expect(promptBudgetNeeded(6000, 2000)).toBe(8304)
  })

  it('blocks a prompt budget too small for both lists', () => {
    const d = limitsDraft({ memory: {} })
    expect(d).toEqual({ workChars: '4000', youChars: '2000', injectLimit: '6400' })
    const raised = { ...d, workChars: '6000' }
    expect(limitsCheck(raised)).toMatchObject({ ok: false, needed: 8304 })
    expect(limitsPatches({ memory: {} }, raised)).toBeNull()
    expect(limitsPatches({ memory: {} }, { ...raised, injectLimit: '8304' })).toEqual({
      'memory.curated_memory_char_limit': 6000,
      'memory.inject_limit': 8304,
    })
    expect(limitsCheck({ ...d, youChars: 'lots' }).ok).toBe(false)
  })
})

describe('facts, documents, recall records, forget', () => {
  it('counts a list the way the gateway does, with the § separator', () => {
    expect(factsTotalAfter([], 'abc')).toBe(3)
    expect(factsTotalAfter(['abc'], 'de')).toBe(3 + 3 + 2)
    expect(factsTotalAfter(['abc', 'xyz'], 'z', 'abc')).toBe(1 + 3 + 3)
  })

  it('accepts text files, sends PDFs to the path route, refuses the rest', () => {
    expect(uploadVerdict('notes.md', 10)).toBe('ok')
    expect(uploadVerdict('README', 10)).toBe('ok')
    expect(uploadVerdict('deck.PPTX', 10)).toBe('binary')
    expect(uploadVerdict('photo.png', 10)).toBe('unsupported')
    expect(uploadVerdict('dump.log', 11 * 1024 * 1024)).toBe('tooBig')
  })

  it('keeps the two lists and the documents out of other sources', () => {
    expect(isOtherSource({ path: 'MEMORY.md', source: 'memory' })).toBe(false)
    expect(isOtherSource({ path: 'knowledge_base/a.md', source: 'knowledge_base' })).toBe(false)
    expect(isOtherSource({ path: 'memory/2026-11-20.md', source: 'memory' })).toBe(true)
  })

  it('ages a record from seconds or milliseconds', () => {
    const now = 1_800_000_000_000
    expect(relativeTime(now / 1000 - 20, now)).toEqual({ unit: 'now', n: 0 })
    expect(relativeTime(now - 5 * 60_000, now)).toEqual({ unit: 'min', n: 5 })
    expect(relativeTime(now / 1000 - 3 * 3600, now)).toEqual({ unit: 'h', n: 3 })
    expect(relativeTime(now - 50 * 3600_000, now)).toEqual({ unit: 'd', n: 2 })
  })

  it('counts what a forget removes', () => {
    expect(
      forgetCounts({
        curated: [{ target: 'memory', entry: 'a' }],
        notes: [{ path: 'memory/x.md', occurrences: 2 }, { path: 'memory/y.md' }],
        sessionDocuments: ['s1'],
      }),
    ).toEqual({ facts: 1, notes: 3, passages: 1, total: 5 })
    expect(forgetCounts({}).total).toBe(0)
  })
})
