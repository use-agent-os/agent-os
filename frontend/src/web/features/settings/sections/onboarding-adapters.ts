// What each catalog-driven settings form (provider, search, X search, image,
// audio, memory embedding) seeds from the current configuration and what it
// sends to its onboarding.*.configure call. Pure: the forms in
// onboarding-forms.tsx render these, the tests assert them directly.
//
// The configure calls are not patches. Each rewrites its whole section and
// fills anything missing with its own defaults, so a form has to send every
// field it shows, seeded from what is saved — never only what was touched.
import { t } from '@/i18n'
import '@/i18n/en/webSetConfig'
import type { SettingsSnapshot } from '@/views/settings/snapshot'
import {
  camel,
  isProviderAdvancedField,
  memoryControlFlags,
  providerFieldValue,
  type FieldSpec,
  type ProviderSpec,
  type SetupConfig,
} from '@/views/setup/logic'

export type FormValues = Record<string, unknown>

export interface CapabilityAdapter {
  /** The onboarding RPC this form submits to. */
  method: string
  /** The config path of the provider choice (settings-search target). */
  providerPath: string
  /** The config path a field edits (its `data-field-id`). */
  fieldPath(name: string, providerId: string): string
  /** The specs the gateway will accept (runtime-supported ones). */
  specs(snapshot: SettingsSnapshot): ProviderSpec[]
  /** The provider in use now, or '' when none is. */
  current(snapshot: SettingsSnapshot): string
  /** Every field the form shows for a spec, catalog fields plus synthetic ones. */
  fields(spec: ProviderSpec): FieldSpec[]
  /** The values a field starts at: what is saved, else the spec's default. */
  seed(snapshot: SettingsSnapshot, spec: ProviderSpec): FormValues
  /** Fields that describe the section, not the provider: kept across a provider switch. */
  shared?: readonly string[]
  /** One fixed spec (X search): no provider choice to show. */
  single?: boolean
  /** Folded under "Advanced connection settings". */
  advanced?(field: FieldSpec, spec: ProviderSpec): boolean
}

export function isSecretField(field: FieldSpec): boolean {
  return Boolean(field.secret) || field.type === 'password'
}

/** The fields of a spec a form shows for the current values (`showWhen`). */
export function visibleFields(fields: FieldSpec[], values: FormValues): FieldSpec[] {
  return fields.filter((f) =>
    Object.entries(f.showWhen ?? {}).every(([k, v]) => (values[k] ?? '') === v),
  )
}

/** Runtime-supported specs only: the gateway rejects the others on Apply. */
export function supportedSpecs(specs: ProviderSpec[] | undefined): ProviderSpec[] {
  return (specs ?? []).filter((s) => s.runtimeSupported !== false)
}

/**
 * The configure params for a form: every shown field, camelCased. A blank
 * secret is left out (the gateway keeps the stored one), a pasted key drops
 * the env reference (the gateway rejects both at once), and a cleared number
 * is left out so the gateway keeps its own value.
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
    params[key] = value === undefined || value === null ? '' : String(value)
  }
  if (params.apiKey) delete params.apiKeyEnv
  return params
}

function str(value: unknown): string {
  return value === undefined || value === null ? '' : String(value)
}

/** A stored literal key is published as a non-empty (redacted) marker. */
function hasStoredKey(value: unknown): boolean {
  return typeof value === 'string' && value.trim() !== ''
}

function apiKeyEnvField(spec: ProviderSpec): FieldSpec {
  return {
    name: 'api_key_env',
    type: 'text',
    label: t('webSetConfig.fieldApiKeyEnv'),
    help: t('webSetConfig.fieldApiKeyEnvHelp'),
    placeholder: spec.envKey || undefined,
    default: spec.envKey ?? '',
  }
}

/** Insert `extra` right after the `api_key` field (or at the end). */
function afterApiKey(fields: FieldSpec[], extra: FieldSpec): FieldSpec[] {
  if (fields.some((f) => f.name === extra.name)) return fields
  const at = fields.findIndex((f) => f.name === 'api_key')
  if (at < 0) return [...fields, extra]
  return [...fields.slice(0, at + 1), extra, ...fields.slice(at + 1)]
}

function defaults(fields: FieldSpec[]): FormValues {
  return Object.fromEntries(
    fields.map((f) => [
      f.name,
      isSecretField(f) ? '' : f.type === 'bool' ? Boolean(f.default) : str(f.default),
    ]),
  )
}

// ── LLM provider ───────────────────────────────────────────────────────────

type LlmSource = NonNullable<SetupConfig['llm']>

export const providerAdapter: CapabilityAdapter = {
  method: 'onboarding.provider.configure',
  providerPath: 'llm.provider',
  fieldPath: (name) => `llm.${name}`,
  specs: (s) => supportedSpecs(s.catalog?.providers),
  current: (s) => str(s.config?.llm?.provider),
  fields: (spec) => spec.fields ?? [],
  seed(snapshot, spec) {
    const config = snapshot.config ?? {}
    const llm = config.llm ?? {}
    // The active provider seeds from llm.*; another one from the profile the
    // gateway saved when it was last switched away from (never a literal key).
    const profiles = (config.provider_profiles ?? {}) as Record<string, LlmSource>
    const source: LlmSource =
      llm.provider === spec.providerId ? llm : (profiles[spec.providerId] ?? {})
    const values = defaults(this.fields(spec))
    for (const field of this.fields(spec)) {
      if (isSecretField(field)) continue
      if (['model', 'base_url', 'proxy', 'api_key_env'].includes(field.name)) {
        values[field.name] = providerFieldValue(field, source)
      } else if (field.name in source) {
        values[field.name] =
          field.type === 'bool' ? Boolean(source[field.name]) : str(source[field.name])
      }
    }
    return values
  },
  advanced: (field, spec) => isProviderAdvancedField(field, spec),
}

// ── Web search ─────────────────────────────────────────────────────────────

const SEARCH_KEYS: Record<string, string> = {
  max_results: 'search_max_results',
  proxy: 'search_proxy',
  use_env_proxy: 'search_use_env_proxy',
  fallback_policy: 'search_fallback_policy',
  diagnostics: 'search_diagnostics',
  api_key: 'search_api_key',
  api_key_env: 'search_api_key_env',
}

export const searchAdapter: CapabilityAdapter = {
  method: 'onboarding.search.configure',
  providerPath: 'search_provider',
  fieldPath: (name) => SEARCH_KEYS[name] ?? `search_${name}`,
  specs: (s) => supportedSpecs(s.catalog?.searchProviders),
  current: (s) => str(s.config?.search_provider),
  fields(spec) {
    const fields = spec.fields ?? []
    // A keyless provider never reads a key: offering one invites a paste that
    // the gateway silently drops.
    if (spec.requiresApiKey !== true) return fields.filter((f) => f.name !== 'api_key')
    return afterApiKey(fields, apiKeyEnvField(spec))
  },
  seed(snapshot, spec) {
    const config = snapshot.config ?? {}
    const fields = this.fields(spec)
    const values = defaults(fields)
    for (const field of fields) {
      const key = SEARCH_KEYS[field.name]
      if (!key || isSecretField(field) || field.name === 'api_key_env') continue
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
    ['proxy', 'use_env_proxy', 'fallback_policy', 'diagnostics'].includes(field.name),
}

// ── X search ───────────────────────────────────────────────────────────────

export const xSearchAdapter: CapabilityAdapter = {
  method: 'onboarding.x_search.configure',
  providerPath: 'x_search.provider',
  single: true,
  fieldPath: (name) => `x_search.${name}`,
  specs: (s) => supportedSpecs(s.catalog?.xSearch),
  current: (s) => supportedSpecs(s.catalog?.xSearch)[0]?.providerId ?? '',
  fields(spec) {
    const enabled: FieldSpec = {
      name: 'enabled',
      type: 'bool',
      label: t('webSetConfig.xSearchEnabled'),
      default: true,
    }
    return [enabled, ...afterApiKey(spec.fields ?? [], apiKeyEnvField(spec))]
  },
  seed(snapshot, spec) {
    const saved = (snapshot.config?.x_search ?? {}) as Record<string, unknown>
    const fields = this.fields(spec)
    const values = defaults(fields)
    for (const field of fields) {
      if (isSecretField(field)) continue
      const value = saved[field.name]
      if (value === undefined || value === null) continue
      if (field.type === 'bool') values[field.name] = Boolean(value)
      else if (field.name === 'reasoning_effort' || str(value) !== '')
        values[field.name] = str(value)
    }
    // Only an explicit false turns it off; a missing key is the gateway default (on).
    values.enabled = saved.enabled !== false
    return values
  },
  advanced: (field) =>
    [
      'reasoning_effort',
      'timeout_seconds',
      'total_timeout_seconds',
      'retries',
      'base_url',
    ].includes(field.name),
}

/** X search params: the shared rule, minus the provider id the RPC does not take. */
export function xSearchParams(values: FormValues, fields: FieldSpec[]): Record<string, unknown> {
  const { providerId: _drop, ...params } = collectParams('', fields, values)
  void _drop
  return params
}

// ── Image generation ───────────────────────────────────────────────────────

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

export const imageAdapter: CapabilityAdapter = {
  method: 'onboarding.imageGeneration.configure',
  providerPath: 'image_generation.provider',
  fieldPath: (name, providerId) =>
    name === 'enabled' || name === 'primary'
      ? `image_generation.${name}`
      : `image_generation.providers.${providerId}.${name}`,
  specs: (s) => supportedSpecs(s.catalog?.imageGenerationProviders),
  current(s) {
    const primary =
      str(s.status?.imageGenerationPrimary) || str(s.config?.image_generation?.primary)
    return str(s.status?.imageGenerationProvider) || primary.split('/')[0] || ''
  },
  fields: (spec) => afterApiKey(spec.fields ?? [], apiKeyEnvField(spec)),
  seed(snapshot, spec) {
    const config = snapshot.config ?? {}
    const image = (config.image_generation ?? {}) as Record<string, unknown> & {
      providers?: Record<string, Record<string, unknown>>
    }
    const saved = providerSection(image, spec.providerId)
    const values = defaults(this.fields(spec))
    values.enabled =
      typeof image.enabled === 'boolean'
        ? image.enabled
        : snapshot.status?.imageGenerationEnabled !== false
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
}

// ── Audio ──────────────────────────────────────────────────────────────────

export const audioAdapter: CapabilityAdapter = {
  method: 'onboarding.audio.configure',
  providerPath: 'audio.provider',
  fieldPath: (name, providerId) =>
    name === 'enabled'
      ? 'audio.enabled'
      : ['tts_voice', 'tts_model'].includes(name)
        ? `audio.tts.${name.slice(4)}`
        : name === 'language_code'
          ? 'audio.tts.language_code'
          : `audio.providers.${providerId}.${name}`,
  specs: (s) => supportedSpecs(s.catalog?.audioProviders),
  current: (s) => str(s.status?.audioProvider),
  fields: (spec) => afterApiKey(spec.fields ?? [], apiKeyEnvField(spec)),
  seed(snapshot, spec) {
    const audio = (snapshot.config?.audio ?? {}) as NonNullable<SetupConfig['audio']>
    const saved = providerSection(audio, spec.providerId)
    const tts = (audio.tts ?? {}) as Record<string, unknown>
    const values = defaults(this.fields(spec))
    values.enabled =
      typeof audio.enabled === 'boolean' ? audio.enabled : snapshot.status?.audioEnabled === true
    values.api_key_env = savedEnvKey(saved, spec)
    values.base_url = str(saved.base_url) || str(spec.defaultBaseUrl) || str(values.base_url)
    values.tts_voice = str(tts.voice) || str(spec.defaultTtsVoice) || str(values.tts_voice)
    values.tts_model = str(tts.model) || str(spec.defaultTtsModel) || str(values.tts_model)
    values.language_code = str(tts.language_code)
    return values
  },
  advanced: (field) =>
    ['base_url', 'api_key_env', 'tts_voice', 'tts_model', 'language_code'].includes(field.name),
}

// ── Memory embedding (guided setup) ────────────────────────────────────────

export const memoryEmbeddingAdapter: CapabilityAdapter = {
  method: 'onboarding.memory_embedding.configure',
  providerPath: 'memory.embedding.provider',
  fieldPath: (name, providerId) =>
    name === 'onnx_dir'
      ? 'memory.embedding.local.onnx_dir'
      : providerId === 'ollama'
        ? `memory.embedding.ollama.${name}`
        : `memory.embedding.remote.${name}`,
  specs: (s) => supportedSpecs(s.catalog?.memoryEmbeddingProviders),
  current(s) {
    const embedding = s.config?.memory?.embedding ?? {}
    return str(embedding.provider) || str(embedding.mode) || str(s.status?.memoryEmbeddingProvider)
  },
  // The catalog carries no fields for these providers; which ones apply is
  // decided by the provider kind, as the gateway does.
  fields(spec) {
    const flags = memoryControlFlags(spec.providerId, spec)
    const fields: FieldSpec[] = []
    if (flags.localControlEnabled) {
      fields.push({ name: 'onnx_dir', type: 'text', label: t('webSetConfig.fieldOnnxDir') })
    }
    if (flags.remoteControlEnabled) {
      fields.push({ name: 'model', type: 'text', label: t('webSetConfig.fieldModel') })
    }
    if (flags.apiKeyEnabled) {
      fields.push({
        name: 'api_key',
        type: 'password',
        secret: true,
        label: t('webSetConfig.fieldApiKey'),
      })
      fields.push(apiKeyEnvField(spec))
    }
    if (flags.remoteControlEnabled) {
      fields.push({ name: 'base_url', type: 'text', label: t('webSetConfig.fieldBaseUrl') })
    }
    return fields
  },
  seed(snapshot, spec) {
    const embedding = snapshot.config?.memory?.embedding ?? {}
    const remote = (embedding.remote ?? {}) as Record<string, unknown>
    const ollama = (embedding.ollama ?? {}) as Record<string, unknown>
    const local = (embedding.local ?? {}) as Record<string, unknown>
    const conn = spec.providerId === 'ollama' ? ollama : remote
    const values = defaults(this.fields(spec))
    values.model = str(conn.model)
    values.base_url = str(conn.base_url)
    values.onnx_dir = str(local.onnx_dir)
    values.api_key_env =
      str(remote.api_key_env) || (spec.requiresApiKey === true ? str(spec.envKey) : '')
    return Object.fromEntries(
      Object.entries(values).filter(([name]) => this.fields(spec).some((f) => f.name === name)),
    )
  },
  advanced: (field, spec) => spec.providerId === 'auto' && field.name !== 'onnx_dir',
}
