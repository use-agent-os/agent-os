import { camel, type FieldSpec, type ProviderSpec, type SetupConfig } from '@/views/setup/logic'
import type { SettingsSnapshot } from '@/views/settings/snapshot'
import { providerKeyUrl } from '../provider-links'

/**
 * Web search, X search, image generation and voice as data: which catalog
 * specs each offers, what its form starts from, and what its
 * `onboarding.*.configure` call carries. The configure calls rewrite their
 * whole section, so a form sends every field it shows, seeded from what is
 * saved. Re-implemented from the console's adapters (the desktop never
 * imports `@/web/**`) and kept pure for the tests.
 */

export type CapabilityId = 'search' | 'x' | 'image' | 'voice'
export type FormValues = Record<string, unknown>

export interface CapabilityDef {
  id: CapabilityId
  /** The onboarding RPC the card saves through. */
  method: string
  /** One fixed spec (X search): no provider choice, no `providerId` param. */
  single?: boolean
  /** Has an on/off flag that rides along with the write. */
  toggles: boolean
  specs(snapshot: SettingsSnapshot): ProviderSpec[]
  /** The provider in use now, or '' when none is. */
  current(snapshot: SettingsSnapshot): string
  /** The saved on/off state (true when the capability has no flag). */
  enabled(snapshot: SettingsSnapshot): boolean
  /** The fields the card shows for a spec, catalog fields plus synthetic ones. */
  fields(spec: ProviderSpec): FieldSpec[]
  seed(snapshot: SettingsSnapshot, spec: ProviderSpec): FormValues
  /** Fields about the section, not the provider: kept across a provider switch. */
  shared?: readonly string[]
  /** Folded under Advanced. */
  advanced(field: FieldSpec): boolean
  /** A literal key for this provider is saved in the config (published redacted). */
  storedKey(snapshot: SettingsSnapshot, providerId: string): boolean
}

export function str(value: unknown): string {
  return value === undefined || value === null ? '' : String(value)
}

/** A stored literal key is published as a non-empty (redacted) marker. */
export function hasStoredKey(value: unknown): boolean {
  return typeof value === 'string' && value.trim() !== ''
}

export function isSecretField(field: FieldSpec): boolean {
  return Boolean(field.secret) || field.type === 'password'
}

/** Runtime-supported specs only: the gateway rejects the others on save. */
export function supportedSpecs(specs: ProviderSpec[] | undefined): ProviderSpec[] {
  return (specs ?? []).filter((s) => s.runtimeSupported !== false)
}

/** The fields shown for the current values (`showWhen`). */
export function visibleFields(fields: FieldSpec[], values: FormValues): FieldSpec[] {
  return fields.filter((f) =>
    Object.entries(f.showWhen ?? {}).every(([k, v]) => (values[k] ?? '') === v),
  )
}

function apiKeyEnvField(spec: ProviderSpec): FieldSpec {
  return {
    name: 'api_key_env',
    type: 'text',
    label: 'API key env',
    placeholder: spec.envKey || undefined,
    default: spec.envKey ?? '',
  }
}

/** Insert `extra` right after `api_key` (or at the end); never twice. */
function afterApiKey(fields: FieldSpec[], extra: FieldSpec): FieldSpec[] {
  if (fields.some((f) => f.name === extra.name)) return fields
  const at = fields.findIndex((f) => f.name === 'api_key')
  if (at < 0) return [...fields, extra]
  return [...fields.slice(0, at + 1), extra, ...fields.slice(at + 1)]
}

/** The on/off flag is the card's switch, not a field. */
function withoutEnabled(fields: FieldSpec[]): FieldSpec[] {
  return fields.filter((f) => f.name !== 'enabled')
}

function defaults(fields: FieldSpec[]): FormValues {
  return Object.fromEntries(
    fields.map((f) => [
      f.name,
      isSecretField(f) ? '' : f.type === 'bool' ? Boolean(f.default) : str(f.default),
    ]),
  )
}

function providerSection(
  section: { providers?: Record<string, Record<string, unknown>> } | undefined,
  providerId: string,
): Record<string, unknown> {
  return section?.providers?.[providerId] ?? {}
}

function savedEnvKey(saved: Record<string, unknown>, spec: ProviderSpec): string {
  if (str(saved.api_key_env)) return str(saved.api_key_env)
  if (hasStoredKey(saved.api_key)) return ''
  return spec.requiresApiKey === false ? '' : str(spec.envKey)
}

// ── Web search ─────────────────────────────────────────────────────────────

const SEARCH_KEYS: Record<string, string> = {
  max_results: 'search_max_results',
  proxy: 'search_proxy',
  use_env_proxy: 'search_use_env_proxy',
  fallback_policy: 'search_fallback_policy',
  diagnostics: 'search_diagnostics',
}

export const searchDef: CapabilityDef = {
  id: 'search',
  method: 'onboarding.search.configure',
  toggles: false,
  specs: (s) => supportedSpecs(s.catalog?.searchProviders),
  current: (s) => str(s.config?.search_provider),
  enabled: () => true,
  fields(spec) {
    const fields = spec.fields ?? []
    // A keyless provider never reads a key: offering one invites a paste the
    // gateway silently drops.
    if (spec.requiresApiKey !== true) return fields.filter((f) => f.name !== 'api_key')
    return afterApiKey(fields, apiKeyEnvField(spec))
  },
  seed(snapshot, spec) {
    const config = (snapshot.config ?? {}) as Record<string, unknown>
    const fields = this.fields(spec)
    const values = defaults(fields)
    for (const field of fields) {
      const key = SEARCH_KEYS[field.name]
      if (!key) continue
      const saved = config[key]
      if (saved === undefined || saved === null) continue
      values[field.name] = field.type === 'bool' ? Boolean(saved) : str(saved)
    }
    if (fields.some((f) => f.name === 'api_key_env')) {
      const active = config.search_provider === spec.providerId
      values.api_key_env = active
        ? str(config.search_api_key_env) ||
          (hasStoredKey(config.search_api_key) ? '' : str(spec.envKey))
        : str(spec.envKey)
    }
    return values
  },
  shared: ['max_results', 'proxy', 'use_env_proxy', 'fallback_policy', 'diagnostics'],
  advanced: (field) =>
    ['api_key_env', 'proxy', 'use_env_proxy', 'fallback_policy', 'diagnostics'].includes(
      field.name,
    ),
  storedKey: (s, providerId) =>
    s.config?.search_provider === providerId && hasStoredKey(s.config?.search_api_key),
}

// ── X search ───────────────────────────────────────────────────────────────

export const xSearchDef: CapabilityDef = {
  id: 'x',
  method: 'onboarding.x_search.configure',
  single: true,
  toggles: true,
  specs: (s) => supportedSpecs(s.catalog?.xSearch),
  current: (s) => supportedSpecs(s.catalog?.xSearch)[0]?.providerId ?? '',
  // Only an explicit false turns it off; a missing key is the gateway default (on).
  enabled: (s) => s.config?.x_search?.enabled !== false,
  fields: (spec) => withoutEnabled(afterApiKey(spec.fields ?? [], apiKeyEnvField(spec))),
  seed(snapshot, spec) {
    const saved = (snapshot.config?.x_search ?? {}) as Record<string, unknown>
    const fields = this.fields(spec)
    const values = defaults(fields)
    for (const field of fields) {
      if (isSecretField(field)) continue
      const value = saved[field.name]
      if (value === undefined || value === null) continue
      if (field.type === 'bool') values[field.name] = Boolean(value)
      else if (field.name === 'reasoning_effort' || str(value) !== '') {
        values[field.name] = str(value)
      }
    }
    return values
  },
  advanced: (field) =>
    [
      'api_key_env',
      'reasoning_effort',
      'timeout_seconds',
      'total_timeout_seconds',
      'retries',
      'base_url',
    ].includes(field.name),
  storedKey: (s) => hasStoredKey(s.config?.x_search?.api_key),
}

// ── Image generation ───────────────────────────────────────────────────────

type ImageSection = Record<string, unknown> & {
  providers?: Record<string, Record<string, unknown>>
}

export const imageDef: CapabilityDef = {
  id: 'image',
  method: 'onboarding.imageGeneration.configure',
  toggles: true,
  specs: (s) => supportedSpecs(s.catalog?.imageGenerationProviders),
  current(s) {
    const primary =
      str(s.status?.imageGenerationPrimary) || str(s.config?.image_generation?.primary)
    return str(s.status?.imageGenerationProvider) || primary.split('/')[0] || ''
  },
  enabled(s) {
    const image = (s.config?.image_generation ?? {}) as ImageSection
    return typeof image.enabled === 'boolean'
      ? image.enabled
      : s.status?.imageGenerationEnabled !== false
  },
  fields: (spec) => withoutEnabled(afterApiKey(spec.fields ?? [], apiKeyEnvField(spec))),
  seed(snapshot, spec) {
    const image = (snapshot.config?.image_generation ?? {}) as ImageSection
    const saved = providerSection(image, spec.providerId)
    const values = defaults(this.fields(spec))
    const primary = str(image.primary)
    // The primary model names its provider; another provider's model is not
    // a value this provider can take.
    values.primary = primary.startsWith(`${spec.providerId}/`)
      ? primary
      : str(spec.defaultModel) || str(values.primary)
    values.api_key_env = savedEnvKey(saved, spec)
    values.base_url = str(saved.base_url) || str(spec.defaultBaseUrl) || str(values.base_url)
    return values
  },
  advanced: (field) => ['base_url', 'api_key_env'].includes(field.name),
  storedKey: (s, providerId) =>
    hasStoredKey(
      providerSection(s.config?.image_generation as ImageSection | undefined, providerId).api_key,
    ),
}

// ── Voice ──────────────────────────────────────────────────────────────────

export const voiceDef: CapabilityDef = {
  id: 'voice',
  method: 'onboarding.audio.configure',
  toggles: true,
  specs: (s) => supportedSpecs(s.catalog?.audioProviders),
  current: (s) => str(s.status?.audioProvider),
  enabled(s) {
    const audio = s.config?.audio ?? {}
    return typeof audio.enabled === 'boolean' ? audio.enabled : s.status?.audioEnabled === true
  },
  fields: (spec) => withoutEnabled(afterApiKey(spec.fields ?? [], apiKeyEnvField(spec))),
  seed(snapshot, spec) {
    const audio = (snapshot.config?.audio ?? {}) as NonNullable<SetupConfig['audio']>
    const saved = providerSection(audio, spec.providerId)
    const tts = (audio.tts ?? {}) as Record<string, unknown>
    const values = defaults(this.fields(spec))
    values.api_key_env = savedEnvKey(saved, spec)
    values.base_url = str(saved.base_url) || str(spec.defaultBaseUrl) || str(values.base_url)
    values.tts_voice = str(tts.voice) || str(spec.defaultTtsVoice) || str(values.tts_voice)
    values.tts_model = str(tts.model) || str(spec.defaultTtsModel) || str(values.tts_model)
    values.language_code = str(tts.language_code)
    return values
  },
  advanced: (field) =>
    ['base_url', 'api_key_env', 'tts_voice', 'tts_model', 'language_code'].includes(field.name),
  storedKey: (s, providerId) => hasStoredKey(providerSection(s.config?.audio, providerId).api_key),
}

export const CAPABILITIES: readonly CapabilityDef[] = [searchDef, xSearchDef, imageDef, voiceDef]

// ── the card's draft ───────────────────────────────────────────────────────

export interface CapDraft {
  providerId: string
  enabled: boolean
  values: FormValues
}

/** The provider the card opens on: the saved one when offered, else the first. */
export function initialProvider(def: CapabilityDef, snapshot: SettingsSnapshot): string {
  const specs = def.specs(snapshot)
  const current = def.current(snapshot)
  return specs.some((s) => s.providerId === current) ? current : (specs[0]?.providerId ?? '')
}

export function seedDraft(
  def: CapabilityDef,
  snapshot: SettingsSnapshot,
  providerId = initialProvider(def, snapshot),
): CapDraft {
  const spec = def.specs(snapshot).find((s) => s.providerId === providerId)
  return {
    providerId,
    enabled: def.enabled(snapshot),
    values: spec ? def.seed(snapshot, spec) : {},
  }
}

/** A provider switch reseeds from what is saved, keeping section-wide values. */
export function switchProvider(
  def: CapabilityDef,
  snapshot: SettingsSnapshot,
  draft: CapDraft,
  next: string,
): CapDraft {
  const fresh = seedDraft(def, snapshot, next)
  for (const name of def.shared ?? []) {
    if (name in draft.values && name in fresh.values) fresh.values[name] = draft.values[name]
  }
  return { ...fresh, enabled: draft.enabled }
}

/**
 * Turning a capability on: when the chosen service would have no key but
 * another one already has its key in place, start on that one instead.
 */
export function enableDraft(
  def: CapabilityDef,
  snapshot: SettingsSnapshot,
  draft: CapDraft,
  ctx: KeyContext,
): CapDraft {
  const on = { ...draft, enabled: true }
  const specs = def.specs(snapshot)
  const spec = specs.find((s) => s.providerId === draft.providerId)
  if (!spec || keySource(def, spec, on, snapshot, ctx) !== 'missing') return on
  const ready = specs.find((s) => s.requiresApiKey !== true || keyReady(def, s, snapshot, ctx))
  return ready ? { ...switchProvider(def, snapshot, on, ready.providerId), enabled: true } : on
}

export function draftDirty(saved: CapDraft, draft: CapDraft): boolean {
  if (saved.providerId !== draft.providerId || saved.enabled !== draft.enabled) return true
  const names = new Set([...Object.keys(saved.values), ...Object.keys(draft.values)])
  for (const name of names) {
    if (str(saved.values[name]) !== str(draft.values[name])) return true
  }
  return false
}

/**
 * The configure params: every shown field, camelCased. A blank secret is
 * left out (the gateway keeps the stored one), a pasted key drops the env
 * reference (the gateway rejects both at once), a cleared number is left out
 * so the gateway keeps its own value.
 */
export function collectParams(
  providerId: string,
  fields: FieldSpec[],
  values: FormValues,
): Record<string, unknown> {
  const params: Record<string, unknown> = { providerId }
  for (const field of fields) {
    const key = camel(field.name)
    const value = values[field.name]
    if (isSecretField(field)) {
      const secret = String(value ?? '').trim()
      if (secret) params[key] = secret
      continue
    }
    if (field.type === 'bool') {
      params[key] = Boolean(value)
      continue
    }
    if (field.type === 'int' || field.type === 'float') {
      const raw = String(value ?? '').trim()
      if (raw === '') continue
      const num = field.type === 'int' ? Number.parseInt(raw, 10) : Number.parseFloat(raw)
      params[key] = Number.isFinite(num) ? num : raw
      continue
    }
    params[key] = value === undefined || value === null ? '' : String(value).trim()
  }
  if (params.apiKey) delete params.apiKeyEnv
  return params
}

export function buildParams(
  def: CapabilityDef,
  spec: ProviderSpec,
  draft: CapDraft,
): Record<string, unknown> {
  const fields = visibleFields(def.fields(spec), draft.values)
  const params = collectParams(draft.providerId, fields, draft.values)
  if (def.toggles) params.enabled = draft.enabled
  if (def.single) delete params.providerId
  return params
}

// ── keys ───────────────────────────────────────────────────────────────────

/**
 * Where the key this draft would use comes from. `typed`: pasted now.
 * `stored`: saved in the config. `env`: the variable it reads is set.
 * `login`: an xAI login covers it. `provider`: images reuse the key of the
 * chat provider of the same name. `none`: the provider takes no key.
 * `missing`: nothing would answer. `unknown`: the environment could not be read.
 */
export type KeySource =
  'typed' | 'stored' | 'env' | 'login' | 'provider' | 'none' | 'missing' | 'unknown'

export interface KeyContext {
  /** Whether a variable is set, per env.list; undefined while unknown. */
  envSet: (name: string) => boolean | undefined
  /** An xAI (SuperGrok) login is stored. */
  xaiLogin?: boolean
}

/** The variable a draft reads its key from. */
export function envKeyOf(spec: ProviderSpec, draft: CapDraft): string {
  return str(draft.values.api_key_env).trim() || str(spec.envKey)
}

export function keySource(
  def: CapabilityDef,
  spec: ProviderSpec,
  draft: CapDraft,
  snapshot: SettingsSnapshot,
  ctx: KeyContext,
): KeySource {
  if (spec.requiresApiKey !== true) return 'none'
  if (str(draft.values.api_key).trim()) return 'typed'
  if (def.id === 'x' && ctx.xaiLogin) return 'login'
  const saved = draft.providerId === initialProvider(def, snapshot)
  // A stored literal key wins only while the env reference is cleared.
  if (saved && def.storedKey(snapshot, spec.providerId) && !str(draft.values.api_key_env).trim()) {
    return 'stored'
  }
  const envKey = envKeyOf(spec, draft)
  if (!envKey) return def.storedKey(snapshot, spec.providerId) ? 'stored' : 'missing'
  const set = ctx.envSet(envKey)
  if (set) return 'env'
  if (def.storedKey(snapshot, spec.providerId)) return 'stored'
  const llm = snapshot.config?.llm ?? {}
  if (def.id === 'image' && llm.provider === spec.providerId && hasStoredKey(llm.api_key)) {
    return 'provider'
  }
  return set === undefined ? 'unknown' : 'missing'
}

/** Whether a provider would have a key without a paste (the "Key ready" tag). */
export function keyReady(
  def: CapabilityDef,
  spec: ProviderSpec,
  snapshot: SettingsSnapshot,
  ctx: KeyContext,
): boolean {
  const draft = seedDraft(def, snapshot, spec.providerId)
  const source = keySource(def, spec, draft, snapshot, ctx)
  return source === 'env' || source === 'stored' || source === 'login' || source === 'provider'
}

/** Where to get a key: the gateway's own link for the variable, else a known page. */
export function keyUrl(
  spec: ProviderSpec,
  envUrl: (name: string) => string | undefined,
): string | null {
  const fromEnv = spec.envKey ? envUrl(spec.envKey) : undefined
  if (fromEnv) return fromEnv
  return providerKeyUrl(spec.providerId) ?? CAPABILITY_KEY_URLS[spec.providerId] ?? null
}

/** Key pages the env catalog has no URL for. Only pages we are sure of. */
const CAPABILITY_KEY_URLS: Readonly<Record<string, string>> = {
  elevenlabs: 'https://elevenlabs.io/app/settings/api-keys',
  x_search: 'https://console.x.ai',
}

// ── what stops a save ──────────────────────────────────────────────────────

/**
 * What the engine and the services accept, checked here so the message is
 * ours and comes before the round trip. Results per search: the gateway's
 * own `search.query` takes 1 to 20, and Brave and Tavily return at most 20
 * whatever is asked. X search: the bounds of `XSearchConfig` in the engine.
 */
export const NUMBER_BOUNDS: Readonly<Record<string, { min: number; max: number }>> = {
  max_results: { min: 1, max: 20 },
  timeout_seconds: { min: 30, max: 300 },
  total_timeout_seconds: { min: 30, max: 600 },
  retries: { min: 0, max: 5 },
}

export type FieldProblem =
  | { kind: 'number'; field: string }
  | { kind: 'range'; field: string; min: number; max: number }
  | { kind: 'required'; field: string }

export type SaveProblem =
  { kind: 'key'; envKey: string } | { kind: 'model-prefix'; prefix: string } | FieldProblem

/** What is wrong with one field's value as typed, or null. A blank number keeps the saved one. */
export function fieldProblem(field: FieldSpec, value: unknown): FieldProblem | null {
  const raw = str(value).trim()
  if ((field.type === 'int' || field.type === 'float') && raw !== '') {
    const ok = field.type === 'int' ? /^-?\d+$/.test(raw) : Number.isFinite(Number(raw))
    if (!ok) return { kind: 'number', field: field.name }
    const bounds = NUMBER_BOUNDS[field.name]
    const n = Number(raw)
    if (bounds && (n < bounds.min || n > bounds.max)) {
      return { kind: 'range', field: field.name, ...bounds }
    }
  }
  if (field.required && !isSecretField(field) && field.type !== 'bool' && raw === '') {
    return { kind: 'required', field: field.name }
  }
  return null
}

/**
 * The first reason the draft cannot be saved, or null. The gateway would
 * accept a voice or search setup that reads an unset variable and then fail
 * at the first call; this says so before saving instead.
 */
export function saveProblem(
  def: CapabilityDef,
  spec: ProviderSpec,
  draft: CapDraft,
  snapshot: SettingsSnapshot,
  ctx: KeyContext,
): SaveProblem | null {
  const on = !def.toggles || draft.enabled
  if (!on) return null
  if (keySource(def, spec, draft, snapshot, ctx) === 'missing') {
    return { kind: 'key', envKey: envKeyOf(spec, draft) }
  }
  const fields = visibleFields(def.fields(spec), draft.values)
  for (const field of fields) {
    const problem = fieldProblem(field, draft.values[field.name])
    if (problem) return problem
  }
  if (def.id === 'image') {
    const primary = str(draft.values.primary).trim()
    const prefix = `${spec.providerId}/`
    if (primary && !primary.startsWith(prefix)) return { kind: 'model-prefix', prefix }
  }
  return null
}

// ── the saved state, for the pill and the stats strip ──────────────────────

/**
 * on: in use and answering. off: switched off or not chosen. key: on, but
 * the key it reads is not there.
 */
export type CapState = 'on' | 'off' | 'key'

export interface CapStatus {
  state: CapState
  /** The provider in use, '' when off. */
  providerId: string
  providerLabel: string
  /** Where the key comes from when on. */
  source?: KeySource
}

function labelOf(specs: ProviderSpec[], id: string): string {
  return specs.find((s) => s.providerId === id)?.label || id
}

export function capabilityStatus(
  def: CapabilityDef,
  snapshot: SettingsSnapshot,
  ctx: KeyContext,
): CapStatus {
  const status = snapshot.status ?? {}
  const specs = def.specs(snapshot)
  const providerId = def.current(snapshot)
  const label = labelOf(specs, providerId)
  const off: CapStatus = { state: 'off', providerId: '', providerLabel: '' }
  if (def.id === 'search') {
    if (!providerId) return off
    if (status.searchConfigured === true) {
      const source =
        status.searchSource === 'not_required' ? 'none' : (status.searchSource as KeySource)
      return { state: 'on', providerId, providerLabel: label, source }
    }
    return { state: 'key', providerId, providerLabel: label }
  }
  if (def.id === 'image') {
    if (status.imageGenerationEnabled === false || !def.enabled(snapshot)) return off
    return status.imageGenerationConfigured === true
      ? { state: 'on', providerId, providerLabel: label }
      : { state: 'key', providerId, providerLabel: label }
  }
  if (def.id === 'voice') {
    if (status.audioEnabled === false || !def.enabled(snapshot)) return off
    return status.audioConfigured === true
      ? { state: 'on', providerId, providerLabel: label }
      : { state: 'key', providerId, providerLabel: label }
  }
  // X search has no onboarding status of its own: read it off the config,
  // the login and the environment.
  if (!def.enabled(snapshot)) return off
  const spec = specs[0]
  if (!spec) return off
  const source = keySource(def, spec, seedDraft(def, snapshot), snapshot, ctx)
  return {
    state: source === 'missing' ? 'key' : 'on',
    providerId,
    providerLabel: label,
    source,
  }
}

/** "OpenRouter Images" → "OpenRouter": the short name for the stats strip. */
export function shortLabel(label: string): string {
  return label.replace(/\s+(Images|Audio|Search)$/i, '').trim() || label
}
