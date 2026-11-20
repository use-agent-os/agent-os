/**
 * Settings › Memory: the pure part. What each embedding engine needs, the
 * drafts the Setup cards edit and the dot-path patches they write, the
 * prompt budget check, and the small formatters the tabs share. No React,
 * no RPC: the components and the tests both read from here.
 */
import type { ProviderSpec, SetupConfig } from '@/views/setup/logic'
import type { SettingsSnapshot } from '@/views/settings/snapshot'

/** Every memory RPC acts on the main agent, as the web console's section does. */
export const AGENT_ID = 'main'

function str(value: unknown): string {
  return value === undefined || value === null ? '' : String(value)
}

function rec(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' ? (value as Record<string, unknown>) : {}
}

export function memoryConfig(config: SetupConfig | undefined): Record<string, unknown> {
  return rec(config?.memory)
}

// ── Embedding engine ────────────────────────────────────────────────────────

export const EMBEDDING_PROVIDERS = [
  'auto',
  'local',
  'ollama',
  'openai',
  'openai-compatible',
  'none',
] as const
export type EmbeddingProvider = (typeof EMBEDDING_PROVIDERS)[number]

export function isEmbeddingProvider(value: unknown): value is EmbeddingProvider {
  return typeof value === 'string' && (EMBEDDING_PROVIDERS as readonly string[]).includes(value)
}

export type EmbeddingField = 'model' | 'baseUrl' | 'apiKey' | 'apiKeyEnv' | 'onnxDir'

/**
 * The fields an engine needs, split into what shows under the choice and what
 * folds under Advanced. Mirrors the gateway's rules (`memoryControlFlags`):
 * remote engines take a model, a key and an address; Ollama a model and an
 * address; the local model an optional folder; Automatic only an optional
 * cloud fallback.
 */
export function embeddingFields(provider: EmbeddingProvider): {
  main: EmbeddingField[]
  advanced: EmbeddingField[]
} {
  switch (provider) {
    case 'auto':
      return { main: [], advanced: ['apiKey', 'apiKeyEnv', 'model', 'baseUrl'] }
    case 'local':
      return { main: [], advanced: ['onnxDir'] }
    case 'ollama':
      return { main: ['model', 'baseUrl'], advanced: [] }
    case 'openai':
      return { main: ['apiKey', 'model'], advanced: ['apiKeyEnv', 'baseUrl'] }
    case 'openai-compatible':
      return { main: ['baseUrl', 'apiKey', 'model'], advanced: ['apiKeyEnv'] }
    case 'none':
      return { main: [], advanced: [] }
  }
}

/** What the engine uses when a field is left empty (shown as placeholders). */
export const EMBEDDING_DEFAULTS: Record<
  EmbeddingProvider,
  Partial<Record<EmbeddingField, string>>
> = {
  auto: { model: 'text-embedding-3-small', baseUrl: 'https://api.openai.com/v1' },
  local: {},
  ollama: { model: 'embeddinggemma', baseUrl: 'http://localhost:11434' },
  openai: { model: 'text-embedding-3-small', baseUrl: 'https://api.openai.com/v1' },
  'openai-compatible': { model: 'text-embedding-3-small' },
  none: {},
}

export interface EmbeddingDraft {
  provider: EmbeddingProvider
  model: string
  baseUrl: string
  /** Typed only: a saved key is never sent back, the gateway keeps it. */
  apiKey: string
  apiKeyEnv: string
  onnxDir: string
  /**
   * Automatic only: use a cloud service when the model on this Mac cannot
   * run. Off means search falls back to keywords instead.
   */
  fallback: boolean
}

/** The engines the gateway accepts, in the order the choices show them. */
export function embeddingSpecs(snapshot: SettingsSnapshot | undefined): ProviderSpec[] {
  const specs = (snapshot?.catalog?.memoryEmbeddingProviders ?? []).filter(
    (s) => s.runtimeSupported !== false && isEmbeddingProvider(s.providerId),
  )
  return EMBEDDING_PROVIDERS.flatMap((id) => specs.filter((s) => s.providerId === id))
}

/** The engine the gateway is configured for (legacy `mode` included). */
export function savedEmbeddingProvider(snapshot: SettingsSnapshot | undefined): EmbeddingProvider {
  const embedding = rec(memoryConfig(snapshot?.config).embedding)
  const provider = str(embedding.provider)
  const mode = str(embedding.mode)
  const requested =
    (provider === '' || provider === 'auto') && mode
      ? mode
      : provider || str(snapshot?.status?.memoryEmbeddingProvider)
  return isEmbeddingProvider(requested) ? requested : 'auto'
}

/** Keyword-only for every engine: `memory.retrieval_mode = "fts_only"`. */
export function retrievalIsKeywordOnly(snapshot: SettingsSnapshot | undefined): boolean {
  return memoryConfig(snapshot?.config).retrieval_mode === 'fts_only'
}

/** A stored literal key is published as a non-empty (redacted) marker. */
export function hasStoredEmbeddingKey(snapshot: SettingsSnapshot | undefined): boolean {
  const embedding = rec(memoryConfig(snapshot?.config).embedding)
  const remote = rec(embedding.remote)
  return [remote.api_key, embedding.api_key].some((v) => typeof v === 'string' && v.trim() !== '')
}

/** The env name the gateway reads a cloud key from, kept when the field is blank. */
export function savedEmbeddingKeyEnv(snapshot: SettingsSnapshot | undefined): string {
  return str(rec(rec(memoryConfig(snapshot?.config).embedding).remote).api_key_env).trim()
}

/** Automatic is saved with a cloud service to fall back on (a key or a key's variable). */
export function hasCloudFallback(snapshot: SettingsSnapshot | undefined): boolean {
  if (savedEmbeddingProvider(snapshot) !== 'auto') return false
  return hasStoredEmbeddingKey(snapshot) || savedEmbeddingKeyEnv(snapshot) !== ''
}

/**
 * What a field starts at for an engine: what is saved, else blank. Automatic
 * shows a cloud fallback only when Automatic itself was saved with one: the
 * settings of a cloud engine it replaces are not carried over as a fallback
 * nobody chose.
 */
export function seedEmbedding(
  snapshot: SettingsSnapshot | undefined,
  provider: EmbeddingProvider,
  spec?: ProviderSpec,
): EmbeddingDraft {
  const embedding = rec(memoryConfig(snapshot?.config).embedding)
  const remote = rec(embedding.remote)
  const local = rec(embedding.local)
  const blank: EmbeddingDraft = {
    provider,
    model: '',
    baseUrl: '',
    apiKey: '',
    apiKeyEnv: '',
    onnxDir: '',
    fallback: false,
  }
  switch (provider) {
    case 'auto': {
      if (!hasCloudFallback(snapshot)) return blank
      return {
        ...blank,
        model: str(remote.model),
        baseUrl: str(remote.base_url),
        apiKeyEnv: str(remote.api_key_env),
        fallback: true,
      }
    }
    case 'local':
      return { ...blank, onnxDir: str(local.onnx_dir) }
    case 'none':
      return blank
    case 'ollama': {
      const ollama = rec(embedding.ollama)
      return { ...blank, model: str(ollama.model), baseUrl: str(ollama.base_url) }
    }
    default: {
      // A stored literal key is used while no variable is named: offering the
      // catalog's variable here would swap a working key for an unset name.
      const env =
        str(remote.api_key_env) ||
        (hasStoredEmbeddingKey(snapshot) || spec?.requiresApiKey !== true ? '' : str(spec.envKey))
      return { ...blank, model: str(remote.model), baseUrl: str(remote.base_url), apiKeyEnv: env }
    }
  }
}

/** The fields a draft actually sends: Automatic without a fallback sends none. */
function liveFields(draft: EmbeddingDraft): EmbeddingField[] {
  const { main, advanced } = embeddingFields(draft.provider)
  if (draft.provider === 'auto' && !draft.fallback) return []
  return [...main, ...advanced]
}

export function embeddingDirty(saved: EmbeddingDraft, draft: EmbeddingDraft): boolean {
  if (saved.provider !== draft.provider) return true
  if (draft.provider === 'auto' && saved.fallback !== draft.fallback) return true
  if (draft.apiKey.trim() && liveFields(draft).includes('apiKey')) return true
  return liveFields(draft).some((f) => f !== 'apiKey' && saved[f].trim() !== draft[f].trim())
}

export type EmbeddingProblem =
  | { kind: 'needsUrl' }
  | { kind: 'needsKey' }
  /** The key would come from a variable that is not set on the gateway. */
  | { kind: 'envUnset'; name: string }
  | null

export interface EmbeddingKeyContext {
  /** A literal key is saved (published redacted). */
  storedKey: boolean
  /** The variable the gateway keeps reading when the field is left blank. */
  savedEnv: string
  /** Whether a variable is set on the gateway, per `env.list`; undefined while unknown. */
  envSet: (name: string) => boolean | undefined
}

/** Does this draft send memory text to a cloud service, so need a key? */
export function needsCloudKey(draft: EmbeddingDraft): boolean {
  return (
    draft.provider === 'openai' ||
    draft.provider === 'openai-compatible' ||
    (draft.provider === 'auto' && draft.fallback)
  )
}

/**
 * What stops a save before it would leave search broken. The gateway accepts
 * any variable name for the key and only fails at the first search, so a
 * named variable must be set on the gateway; while the variables are not
 * known yet, nothing is blocked on a guess.
 */
export function embeddingProblem(
  draft: EmbeddingDraft,
  ctx: EmbeddingKeyContext,
): EmbeddingProblem {
  if (draft.provider === 'openai-compatible' && !draft.baseUrl.trim()) return { kind: 'needsUrl' }
  if (!needsCloudKey(draft) || draft.apiKey.trim()) return null
  // A blank field keeps the variable already saved, else a stored key.
  const env = draft.apiKeyEnv.trim() || ctx.savedEnv.trim()
  if (!env) return ctx.storedKey ? null : { kind: 'needsKey' }
  return ctx.envSet(env) === false ? { kind: 'envUnset', name: env } : null
}

/**
 * The `onboarding.memory_embedding.configure` params: every field the engine
 * shows, trimmed. A blank field keeps what is saved (the gateway falls back
 * to it), a blank key is left out, and a pasted key drops the env reference
 * because the gateway refuses both at once. Automatic's fallback says what a
 * blank model or address means, so nothing of an engine it replaced is kept.
 */
export function embeddingParams(draft: EmbeddingDraft): Record<string, unknown> {
  const params: Record<string, unknown> = { providerId: draft.provider }
  for (const field of liveFields(draft)) {
    const value = draft[field].trim()
    if (field === 'apiKey') {
      if (value) params.apiKey = value
      continue
    }
    params[field] =
      value || (draft.provider === 'auto' ? (EMBEDDING_DEFAULTS.auto[field] ?? '') : '')
  }
  if (params.apiKey) delete params.apiKeyEnv
  return params
}

/**
 * How a draft is written. The guided write keeps every blank fallback field
 * at its saved value, so it cannot take a fallback away: Automatic without
 * one is written whole, which leaves no cloud key, variable or address.
 */
export type EmbeddingWrite =
  | { method: 'onboarding.memory_embedding.configure'; params: Record<string, unknown> }
  | { method: 'config.patch'; params: { patches: Record<string, unknown> } }

export function embeddingWrite(draft: EmbeddingDraft): EmbeddingWrite {
  if (draft.provider === 'auto' && !draft.fallback) {
    return {
      method: 'config.patch',
      params: { patches: { 'memory.embedding': { provider: 'auto' } } },
    }
  }
  return { method: 'onboarding.memory_embedding.configure', params: embeddingParams(draft) }
}

/** An address on this Mac, where the app itself can look for Ollama. */
export function isLoopbackHost(host: string): boolean {
  const h = host
    .trim()
    .toLowerCase()
    .replace(/^\[|\]$/g, '')
  return h === 'localhost' || h === '::1' || h === '0.0.0.0' || /^127\./.test(h)
}

export function urlHost(url: string): string {
  try {
    return new URL(url).hostname
  } catch {
    return ''
  }
}

/** Does Ollama's tag list hold `model` (a bare name matches any tag of it)? */
export function ollamaHasModel(tags: readonly string[], model: string): boolean {
  const want = model.trim().toLowerCase()
  if (!want) return false
  const bare = want.includes(':') ? want : `${want}:latest`
  return tags.some((tag) => {
    const name = tag.toLowerCase()
    return name === want || name === bare || (!want.includes(':') && name.split(':')[0] === want)
  })
}

// ── Recall, review, limits: dot-path drafts ─────────────────────────────────

export type RecallMode = 'escalate' | 'always' | 'off'
export const RECALL_MODES: readonly RecallMode[] = ['escalate', 'always', 'off']

export interface RecallDraft {
  enabled: boolean
  mode: RecallMode
  maxInjectedChars: string
  deepTimeoutSeconds: string
  provider: string
  model: string
}

export function recallDraft(config: SetupConfig | undefined): RecallDraft {
  const recall = rec(memoryConfig(config).recall)
  const mode = str(recall.mode)
  return {
    enabled: recall.enabled !== false,
    mode: (RECALL_MODES as readonly string[]).includes(mode) ? (mode as RecallMode) : 'escalate',
    maxInjectedChars: str(recall.max_injected_chars ?? 1800),
    deepTimeoutSeconds: str(recall.deep_timeout_seconds ?? 8),
    provider: str(recall.provider),
    model: str(recall.model),
  }
}

export interface ReviewDraft {
  enabled: boolean
  interval: string
  beforeCompaction: boolean
  timeoutSeconds: string
}

/** `interval = 0` turns the review off too: show it as off, offer 10 to turn it on. */
export function reviewDraft(config: SetupConfig | undefined): ReviewDraft {
  const nudge = rec(memoryConfig(config).nudge)
  const interval = Number(nudge.interval ?? 10)
  return {
    enabled: nudge.enabled !== false && interval !== 0,
    interval: String(interval > 0 ? interval : 10),
    beforeCompaction: nudge.before_compaction !== false,
    timeoutSeconds: str(nudge.timeout_seconds ?? 90),
  }
}

export const REVIEW_INTERVALS = ['5', '10', '20'] as const

export interface LimitsDraft {
  workChars: string
  youChars: string
  injectLimit: string
}

export function limitsDraft(config: SetupConfig | undefined): LimitsDraft {
  const memory = memoryConfig(config)
  return {
    workChars: str(memory.curated_memory_char_limit ?? 4000),
    youChars: str(memory.curated_user_char_limit ?? 2000),
    injectLimit: str(memory.inject_limit ?? 6400),
  }
}

/** A whole number within bounds, or null. */
export function parseWhole(raw: string, min: number, max = Number.MAX_SAFE_INTEGER): number | null {
  const text = raw.trim()
  if (!/^\d+$/.test(text)) return null
  const n = Number(text)
  return n >= min && n <= max ? n : null
}

/** A positive number up to `max` (seconds), or null. */
export function parseSeconds(raw: string, max: number): number | null {
  const text = raw.trim()
  if (!/^\d+(\.\d+)?$/.test(text)) return null
  const n = Number(text)
  return n > 0 && n <= max ? n : null
}

export type Patches = Record<string, string | number | boolean>

/** Dot-path patches for what changed in the recall card; null while invalid. */
export function recallPatches(config: SetupConfig | undefined, draft: RecallDraft): Patches | null {
  const saved = recallDraft(config)
  const maxChars = parseWhole(draft.maxInjectedChars, 200, 8000)
  const timeout = parseSeconds(draft.deepTimeoutSeconds, 120)
  if (maxChars === null || timeout === null) return null
  const out: Patches = {}
  if (draft.enabled !== saved.enabled) out['memory.recall.enabled'] = draft.enabled
  if (draft.mode !== saved.mode) out['memory.recall.mode'] = draft.mode
  if (maxChars !== Number(saved.maxInjectedChars))
    out['memory.recall.max_injected_chars'] = maxChars
  if (timeout !== Number(saved.deepTimeoutSeconds))
    out['memory.recall.deep_timeout_seconds'] = timeout
  if (draft.provider.trim() !== saved.provider)
    out['memory.recall.provider'] = draft.provider.trim()
  if (draft.model.trim() !== saved.model) out['memory.recall.model'] = draft.model.trim()
  return out
}

/** Dot-path patches for the review card; null while invalid. */
export function reviewPatches(config: SetupConfig | undefined, draft: ReviewDraft): Patches | null {
  const nudge = rec(memoryConfig(config).nudge)
  const savedEnabled = nudge.enabled !== false
  const savedInterval = Number(nudge.interval ?? 10)
  const interval = parseWhole(draft.interval, 1, 1000)
  const timeout = parseSeconds(draft.timeoutSeconds, 3600)
  if (timeout === null || (draft.enabled && interval === null)) return null
  const out: Patches = {}
  if (draft.enabled) {
    if (!savedEnabled) out['memory.nudge.enabled'] = true
    if (interval !== savedInterval) out['memory.nudge.interval'] = interval as number
  } else if (savedEnabled && savedInterval !== 0) {
    out['memory.nudge.enabled'] = false
  }
  if (draft.beforeCompaction !== (nudge.before_compaction !== false))
    out['memory.nudge.before_compaction'] = draft.beforeCompaction
  if (timeout !== Number(nudge.timeout_seconds ?? 90)) out['memory.nudge.timeout_seconds'] = timeout
  return out
}

/** How many characters each list holds now, when known. */
export interface ListUse {
  work?: number
  you?: number
}

/**
 * Lists whose limit the draft lowers below what they already hold. The
 * gateway would take it, but the list would then refuse every new fact, and
 * an entry longer than the new limit reads as damaged after a restart.
 */
export function limitsBelowUse(
  config: SetupConfig | undefined,
  draft: LimitsDraft,
  used: ListUse,
): { list: 'work' | 'you'; used: number; limit: number }[] {
  const saved = limitsDraft(config)
  const out: { list: 'work' | 'you'; used: number; limit: number }[] = []
  const check = (list: 'work' | 'you', raw: string, savedRaw: string, n: number | undefined) => {
    const limit = parseWhole(raw, 100, 1_000_000)
    if (limit === null || n === undefined || limit >= n) return
    // A limit already saved below the use is not this edit's doing: do not block other fields.
    if (limit === Number(savedRaw)) return
    out.push({ list, used: n, limit })
  }
  check('work', draft.workChars, saved.workChars, used.work)
  check('you', draft.youChars, saved.youChars, used.you)
  return out
}

/** Dot-path patches for the limits card; null while invalid, too small, or below use. */
export function limitsPatches(
  config: SetupConfig | undefined,
  draft: LimitsDraft,
  used: ListUse = {},
): Patches | null {
  const { ok, work, you, inject } = limitsCheck(draft)
  if (!ok || work === null || you === null || inject === null) return null
  if (limitsBelowUse(config, draft, used).length > 0) return null
  const saved = limitsDraft(config)
  const out: Patches = {}
  if (work !== Number(saved.workChars)) out['memory.curated_memory_char_limit'] = work
  if (you !== Number(saved.youChars)) out['memory.curated_user_char_limit'] = you
  if (inject !== Number(saved.injectLimit)) out['memory.inject_limit'] = inject
  return out
}

/** Characters one curated block takes in the prompt besides its entries, at full use. */
function blockOverhead(target: 'memory' | 'user', limit: number): number {
  const n = limit.toLocaleString('en-US')
  const header =
    target === 'user'
      ? `USER PROFILE (who the user is) [100% — ${n}/${n} chars]`
      : `MEMORY (your personal notes) [100% — ${n}/${n} chars]`
  // Two 46-character rules and three line breaks around the header.
  return 46 * 2 + 3 + header.length
}

/**
 * The smallest `inject_limit` that still fits both lists when they are full:
 * both budgets, each block's heading, and the blank line between them (the
 * gateway drops a whole block that does not fit). 6,304 at the defaults.
 */
export function promptBudgetNeeded(workChars: number, youChars: number): number {
  return (
    workChars + youChars + blockOverhead('memory', workChars) + blockOverhead('user', youChars) + 2
  )
}

export interface LimitsCheck {
  ok: boolean
  work: number | null
  you: number | null
  inject: number | null
  /** The smallest prompt budget the two lists need (null while one is invalid). */
  needed: number | null
}

export function limitsCheck(draft: LimitsDraft): LimitsCheck {
  const work = parseWhole(draft.workChars, 100, 1_000_000)
  const you = parseWhole(draft.youChars, 100, 1_000_000)
  const inject = parseWhole(draft.injectLimit, 100, 4_000_000)
  const needed = work !== null && you !== null ? promptBudgetNeeded(work, you) : null
  const ok = work !== null && you !== null && inject !== null && inject >= (needed as number)
  return { ok, work, you, inject, needed }
}

// ── Facts (MEMORY.md / USER.md) ─────────────────────────────────────────────

export const ENTRY_DELIMITER = '\n§\n'

/** The list's size after an add or an edit, counted the way the gateway does. */
export function factsTotalAfter(entries: readonly string[], text: string, replacing?: string) {
  const next =
    replacing === undefined
      ? [...entries, text.trim()]
      : entries.map((e) => (e === replacing ? text.trim() : e))
  return next.join(ENTRY_DELIMITER).length
}

/**
 * How long a new (or the edited) fact may be: the limit, less the other facts
 * and the three characters ("\n§\n") between each two of them.
 */
export function factRoom(entries: readonly string[], limit: number, replacing?: string): number {
  return limit - factsTotalAfter(entries, '', replacing)
}

/**
 * Would the file store `text` as more than one fact? A line that is only "§"
 * splits it, and a fact ending in a "§" line takes the next fact's start.
 * Asked the gateway's way: put it between two facts and read the file back.
 */
export function splitsIntoFacts(text: string): boolean {
  const content = text.replace(/\r\n?/g, '\n').trim()
  const head = '\u0000head'
  const tail = '\u0000tail'
  const parsed = [head, content, tail]
    .join(ENTRY_DELIMITER)
    .split(ENTRY_DELIMITER)
    .map((part) => part.trim())
    .filter(Boolean)
  return parsed.length !== 3 || parsed[1] !== content
}

/** Why the gateway refused a fact write, from its message. */
export type FactFailure =
  'stale' | 'ambiguous' | 'full' | 'damaged' | 'unreadable' | 'split' | 'other'

export function factFailure(message: string): FactFailure {
  if (/^No entry matched/i.test(message)) return 'stale'
  if (/^Multiple entries matched/i.test(message)) return 'ambiguous'
  if (/would exceed the limit|would put memory at|over the limit/i.test(message)) return 'full'
  if (/wouldn't round-trip|would not round-trip/i.test(message)) return 'damaged'
  if (/could not be\s+read/i.test(message)) return 'unreadable'
  if (/line that is just '§'/.test(message)) return 'split'
  return 'other'
}

// ── Knowledge base ──────────────────────────────────────────────────────────

/** Files the gateway reads as text; PDF, Word and PowerPoint need a path. */
export const TEXT_EXTENSIONS = [
  '.txt',
  '.md',
  '.markdown',
  '.rst',
  '.json',
  '.csv',
  '.tsv',
  '.yaml',
  '.yml',
  '.toml',
  '.xml',
  '.html',
  '.htm',
  '.py',
  '.js',
  '.ts',
  '.jsx',
  '.tsx',
  '.css',
  '.sh',
  '.log',
] as const
export const BINARY_EXTENSIONS = ['.pdf', '.docx', '.pptx'] as const
export const MAX_DOCUMENT_BYTES = 10 * 1024 * 1024

export type UploadVerdict = 'ok' | 'binary' | 'unsupported' | 'tooBig'

export function uploadVerdict(name: string, size: number): UploadVerdict {
  const dot = name.lastIndexOf('.')
  const ext = dot > 0 ? name.slice(dot).toLowerCase() : ''
  if ((BINARY_EXTENSIONS as readonly string[]).includes(ext)) return 'binary'
  if (ext && !(TEXT_EXTENSIONS as readonly string[]).includes(ext)) return 'unsupported'
  if (size > MAX_DOCUMENT_BYTES) return 'tooBig'
  return 'ok'
}

/**
 * Uploaded files land at `knowledge_base/<name>` and overwrite a document of
 * that name: the names (in upload order) that would replace one.
 */
export function replacedByUpload(
  names: readonly string[],
  documents: readonly { path: string }[],
): string[] {
  const have = new Set(documents.map((d) => d.path))
  return names.filter((name) => have.has(`knowledge_base/${name}`))
}

/**
 * What adding a workspace path would overwrite. A file outside the knowledge
 * base is copied to `knowledge_base/<name>`; a folder is merged into
 * `knowledge_base/<name>/`, same-named files replaced. A path already inside
 * the knowledge base is read in place and replaces nothing.
 */
export function replacedByPath(
  path: string,
  documents: readonly { path: string }[],
): { kind: 'file' | 'folder'; name: string } | null {
  const clean = path.trim().replace(/\\/g, '/').replace(/\/+$/, '')
  if (!clean || /^(\.\/)?knowledge_base(\/|$)/.test(clean)) return null
  const name = clean.split('/').pop() ?? ''
  if (!name || name === '.' || name === '..') return null
  if (documents.some((d) => d.path === `knowledge_base/${name}`)) return { kind: 'file', name }
  if (documents.some((d) => d.path.startsWith(`knowledge_base/${name}/`))) {
    return { kind: 'folder', name }
  }
  return null
}

/** `knowledge_base/a/b.md` → `a/b.md`: the folder is implied by the list. */
export function documentName(path: string): string {
  return path.replace(/^knowledge_base\//, '')
}

/** Notes the agent writes besides the two lists (those live under Remembered). */
export function isOtherSource(file: { path: string; source?: string }): boolean {
  if ((file.source || 'memory') === 'knowledge_base') return false
  return file.path !== 'MEMORY.md' && file.path !== 'USER.md'
}

// ── Status ──────────────────────────────────────────────────────────────────

export type SearchKind = 'meaning' | 'keywords' | 'none'

export function searchKind(status: { vecAvailable?: boolean; ftsAvailable?: boolean }): SearchKind {
  if (status.vecAvailable) return 'meaning'
  if (status.ftsAvailable) return 'keywords'
  return 'none'
}

// ── Recall records ──────────────────────────────────────────────────────────

export interface RecallRecordWire {
  sessionKey: string
  status: string
  at: number
  reason?: string
  decision?: string
  triggerHits?: number
  deepHits?: number
  elapsedMs?: number
  injectedChars?: number
  deepModel?: string
}

export interface RecallStatusWire {
  config?: { enabled?: boolean; mode?: string }
  pausedSessions?: string[]
  recent?: (RecallRecordWire | null)[]
}

export type RecallTone = 'ok' | 'warn' | 'danger' | undefined

export function recallTone(status: string): RecallTone {
  if (status === 'injected') return 'ok'
  if (status === 'unavailable') return 'warn'
  if (status === 'error') return 'danger'
  return undefined
}

/** Seconds or ms since the epoch → "just now", "5 min ago", "3 h ago", "2 d ago". */
export function relativeTime(
  at: number,
  now: number,
): { unit: 'now' | 'min' | 'h' | 'd'; n: number } {
  const ms = at < 1e12 ? at * 1000 : at
  const minutes = Math.floor(Math.max(0, now - ms) / 60_000)
  if (minutes < 1) return { unit: 'now', n: 0 }
  if (minutes < 60) return { unit: 'min', n: minutes }
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return { unit: 'h', n: hours }
  return { unit: 'd', n: Math.floor(hours / 24) }
}

// ── Forget ──────────────────────────────────────────────────────────────────

export interface ForgetReport {
  dryRun?: boolean
  sessions?: { selector: string; sessionKey?: string; alreadyForgotten?: boolean }[]
  curated?: { target: 'memory' | 'user'; entry: string; mixed?: boolean }[]
  notes?: { path: string; chars?: number; occurrences?: number }[]
  sessionDocuments?: string[]
  errors?: { code?: string; message?: string }[]
  notCovered?: string
}

export function forgetCounts(report: ForgetReport): {
  facts: number
  notes: number
  passages: number
  total: number
} {
  const facts = report.curated?.length ?? 0
  const notes = (report.notes ?? []).reduce((n, note) => n + (note.occurrences ?? 1), 0)
  const passages = report.sessionDocuments?.length ?? 0
  return { facts, notes, passages, total: facts + notes + passages }
}
