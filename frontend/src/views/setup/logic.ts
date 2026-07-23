// Pure setup-view helpers ported 1:1 from the legacy view
// (src/agentos/gateway/static/js/views/setup.js). Each function carries the
// legacy line range it mirrors so the parity matrix stays auditable. RPC calls,
// event subscriptions, DOM, and rendering live in SetupPage.tsx + the per-section
// components; this module owns the pure derivations: per-section status /
// readiness derivation, the onboarding-reasons list, provider/router/extras
// field-value derivation, the four onboarding.*.configure payload builders,
// scoped-field reading + required validation, and the Finish CLI command
// assembly. No `any`; secrets are never emitted from a builder when blank.

// ── shared catalog / status / config wire shapes ────────────────────────────

/** A field spec from onboarding.catalog (provider/channel/image/audio). */
export interface FieldSpec {
  name: string
  label?: string
  type?: string // '', 'bool', 'select', 'int', 'float', 'password', 'text'
  required?: boolean
  secret?: boolean
  default?: unknown
  placeholder?: string
  description?: string
  choices?: string[]
  group?: string
  advanced?: boolean
  help?: string
  showWhen?: Record<string, unknown>
  [key: string]: unknown
}

/** A provider spec (llm) from onboarding.catalog.providers. */
export interface ProviderSpec {
  providerId: string
  label?: string
  runtimeSupported?: boolean
  routerSupported?: boolean
  requiresApiKey?: boolean
  envKey?: string
  whatYouNeed?: string[]
  fields?: FieldSpec[]
  defaultBaseUrl?: string
  defaultModel?: string
  defaultTtsVoice?: string
  defaultTtsModel?: string
  defaultLanguageCode?: string
  [key: string]: unknown
}

/** A channel spec from onboarding.catalog.channels. */
export interface ChannelSpec {
  type: string
  label?: string
  description?: string
  transport?: string
  requiresPublicUrl?: boolean
  dependencyExtra?: string | null
  restartRequired?: boolean
  docsHint?: string
  help?: string
  blocking?: boolean
  canProbe?: boolean
  whatYouNeed?: string[]
  fields?: FieldSpec[]
  [key: string]: unknown
}

/** onboarding.catalog response. */
export interface Catalog {
  providers?: ProviderSpec[]
  searchProviders?: ProviderSpec[]
  imageGenerationProviders?: ProviderSpec[]
  audioProviders?: ProviderSpec[]
  memoryEmbeddingProviders?: ProviderSpec[]
  channels?: ChannelSpec[]
  routerProfiles?: {
    profiles?: Array<{
      profileId?: string
      providerId: string
      label?: string
      tiers?: Record<string, TierSpec>
    }>
    defaultTier?: string
    judge?: {
      profiles?: Record<string, { autoModel?: string | null; models?: string[] }>
    }
  }
  [key: string]: unknown
}

/** A router-tier spec (catalog default or config override). */
export interface TierSpec {
  provider?: string
  model?: string
  thinkingLevel?: string
  thinking_level?: string
  supportsImage?: boolean
  supports_image?: boolean
  [key: string]: unknown
}

/** A per-section detail block from onboarding.status.sectionDetails. */
export interface SectionDetail {
  label?: string
  status?: string // 'ok' | 'optional' | 'missing' | 'degraded' | 'unknown'
  blocking?: boolean
  actionRequired?: boolean
  required?: boolean
  detail?: string
  [key: string]: unknown
}

/** onboarding.status response (only the fields the view reads). */
export interface OnboardingStatus {
  needsOnboarding?: boolean
  hasConfig?: boolean
  llmConfigured?: boolean
  llmSource?: string
  sectionDetails?: Record<string, SectionDetail>
  envRecoveryCommands?: Array<{ section?: string; label?: string; command?: string }>
  configPath?: string
  channelCount?: number
  searchConfigured?: boolean
  searchSource?: string
  searchEnvKey?: string
  imageGenerationEnabled?: boolean
  imageGenerationConfigured?: boolean
  imageGenerationSource?: string
  imageGenerationEnvKey?: string
  imageGenerationProvider?: string
  imageGenerationPrimary?: string
  audioEnabled?: boolean
  audioConfigured?: boolean
  audioSource?: string
  audioEnvKey?: string
  audioProvider?: string
  memoryEmbeddingProvider?: string
  memoryEmbeddingConfigured?: boolean
  memoryEmbeddingSource?: string
  memoryEmbeddingEnvKey?: string
  [key: string]: unknown
}

/** config.get response (only the fields the view reads). */
export interface SetupConfig {
  llm?: {
    provider?: string
    model?: string
    base_url?: string
    proxy?: string
    api_key?: string
    api_key_env?: string
    [key: string]: unknown
  }
  agentos_router?: {
    enabled?: boolean
    strategy?: string
    default_tier?: string
    judge_model?: string
    judge_base_url?: string
    tiers?: Record<string, TierSpec>
    pilot?: { safety_net_threshold?: number | null }
    [key: string]: unknown
  }
  memory?: {
    embedding?: {
      provider?: string
      mode?: string
      remote?: Record<string, unknown>
      local?: Record<string, unknown>
      ollama?: Record<string, unknown>
      [key: string]: unknown
    }
    provider?: { name?: string }
    curated_memory_char_limit?: number
    curated_user_char_limit?: number
    inject_limit?: number
    [key: string]: unknown
  }
  search_provider?: string
  search_api_key_env?: string
  search_max_results?: number
  search_proxy?: string
  search_use_env_proxy?: boolean
  search_fallback_policy?: string
  search_diagnostics?: boolean
  image_generation?: { providers?: Record<string, Record<string, unknown>>; [key: string]: unknown }
  audio?: {
    enabled?: boolean
    providers?: Record<string, Record<string, unknown>>
    tts?: Record<string, unknown>
    [key: string]: unknown
  }
  channels?: { channels?: Array<Record<string, unknown>> }
  updates?: { notify?: boolean }
  [key: string]: unknown
}

// ── constants (setup.js:4-36) ───────────────────────────────────────────────

export const TEXT_TIERS = ['c0', 'c1', 'c2', 'c3'] as const

export const TIER_LABELS: Record<string, string> = {
  c0: 'Route c0',
  c1: 'Route c1',
  c2: 'Route c2',
  c3: 'Route c3',
}

export const READINESS_LABELS: Record<string, string> = {
  ok: 'Ready',
  optional: 'Optional',
  missing: 'Missing',
  degraded: 'Needs action',
  unknown: 'Check',
}

/** setup.js:27-36 — section id → setup step id (shared by initial-step + reasons). */
export const SECTION_STEPS: Array<[string, string]> = [
  ['llm', 'provider'],
  ['provider', 'provider'],
  ['router', 'router'],
  ['channels', 'channels'],
  ['search', 'extras'],
  ['image_generation', 'extras'],
  ['audio', 'extras'],
  ['memory_embedding', 'extras'],
]

export type StepId = 'provider' | 'router' | 'channels' | 'extras' | 'finish'

/** setup.js:4-10 — the ordered stepper steps. */
export const STEPS: Array<{ id: StepId; label: string }> = [
  { id: 'provider', label: 'Provider' },
  { id: 'router', label: 'Router Tiers' },
  { id: 'extras', label: 'Capabilities' },
  { id: 'finish', label: 'Finish' },
]

// ── small utilities ─────────────────────────────────────────────────────────

/** setup.js:2010-2012 — snake_case → camelCase for RPC param keys. */
export function camel(name: string): string {
  return String(name || '').replace(/_([a-z])/g, (_m, c: string) => c.toUpperCase())
}

/** setup.js:660-662 — tier label with a c1 fallback. */
export function tierLabel(tier: string | undefined): string {
  return TIER_LABELS[tier ?? ''] || tier || 'Route c1'
}

// ── status / readiness derivation (setup.js:125-305) ────────────────────────

export interface StepStatus {
  label: string
  tone: 'is-ok' | 'is-warn' | 'is-muted'
}

/** setup.js:485-491 — provider env missing + selected env key. */
export function providerEnvMissing(status: OnboardingStatus): boolean {
  return status.llmSource === 'missing_env'
}

export function providerEnvKey(config: SetupConfig): string {
  return (config.llm || {}).api_key_env || 'the selected API key environment variable'
}

/** setup.js:165-173 — a section detail needs action. */
export function stepDetailNeedsAction(detail: SectionDetail | undefined): boolean {
  return Boolean(
    detail &&
    (detail.blocking ||
      detail.actionRequired ||
      detail.status === 'missing' ||
      detail.status === 'degraded'),
  )
}

/** setup.js:158-163 — status of a single detail-backed step. */
export function detailStepStatus(detail: SectionDetail | undefined): StepStatus {
  if (!detail) return { label: 'Review', tone: 'is-muted' }
  if (stepDetailNeedsAction(detail)) return { label: 'Needs action', tone: 'is-warn' }
  if (detail.status === 'ok') return { label: 'Ready', tone: 'is-ok' }
  return { label: READINESS_LABELS[detail.status ?? ''] || 'Optional', tone: 'is-muted' }
}

/** setup.js:146-156 — aggregate several sections into one step status. */
export function aggregateStepStatus(status: OnboardingStatus, sectionNames: string[]): StepStatus {
  const details = status.sectionDetails || {}
  const entries = sectionNames.map((name) => details[name]).filter(Boolean) as SectionDetail[]
  if (entries.some((detail) => stepDetailNeedsAction(detail))) {
    return { label: 'Needs action', tone: 'is-warn' }
  }
  if (entries.length && entries.every((detail) => detail.status === 'ok')) {
    return { label: 'Ready', tone: 'is-ok' }
  }
  return { label: 'Optional', tone: 'is-muted' }
}

/** setup.js:261-270 — any pending setup action anywhere. */
export function hasSetupAction(status: OnboardingStatus): boolean {
  if (status.needsOnboarding) return true
  const details = status.sectionDetails || {}
  return Object.values(details).some((detail) => stepDetailNeedsAction(detail))
}

/** setup.js:125-144 — per-step status chip. `provider` is the effective provider. */
export function stepStatus(
  stepId: StepId,
  status: OnboardingStatus,
  effectiveProviderId: string,
): StepStatus {
  const details = status.sectionDetails || {}
  if (stepId === 'provider') {
    if (providerEnvMissing(status)) return { label: 'Needs action', tone: 'is-warn' }
    return detailStepStatus(details.llm || details.provider)
  }
  if (stepId === 'router' && !effectiveProviderId) {
    return { label: 'Provider first', tone: 'is-muted' }
  }
  if (stepId === 'router') return detailStepStatus(details.router)
  if (stepId === 'channels') return detailStepStatus(details.channels)
  if (stepId === 'extras') {
    return aggregateStepStatus(status, ['search', 'image_generation', 'audio', 'memory_embedding'])
  }
  if (stepId === 'finish') {
    return hasSetupAction(status)
      ? { label: 'Review', tone: 'is-warn' }
      : { label: 'Ready', tone: 'is-ok' }
  }
  return { label: 'Review', tone: 'is-muted' }
}

/** setup.js:302-305 — the step that fixes a given section. */
export function stepForSection(name: string): StepId {
  const entry = SECTION_STEPS.find(([section]) => section === name)
  return (entry ? entry[1] : 'provider') as StepId
}

/** setup.js:286-300 — auto-select the initial step from status. */
export function initialStepFromStatus(status: OnboardingStatus): StepId {
  const details = status.sectionDetails || {}
  const entry = SECTION_STEPS.find(
    ([section, destination]) =>
      destination !== 'channels' && stepDetailNeedsAction(details[section]),
  )
  if (entry) return entry[1] as StepId
  if (stepDetailNeedsAction(details.channels)) return 'finish'
  if (status.needsOnboarding === false) return 'finish'
  return 'provider'
}

// ── header headline + onboarding reasons (setup.js:177-259) ─────────────────

export interface SetupHeadline {
  title: string
  chip: string
  tone: 'is-warn' | 'is-optional' | 'is-ok'
}

export interface Reason {
  text: string
  tier: 'blocking' | 'optional'
  step: StepId
}

/** setup.js:251-259 — reason text for a section (env-key aware). */
export function setupActionReason(name: string, detail: SectionDetail): string {
  const missingEnvPrefix = 'env key not visible: '
  const detailText = String(detail.detail || '')
  if (detailText.startsWith(missingEnvPrefix)) {
    const envKey = detailText.slice(missingEnvPrefix.length).trim()
    if (envKey) return `${envKey} is not visible`
  }
  return `${detail.label || name} setup needed`
}

/**
 * setup.js:219-249 — the tiered clickable reasons list. Blocking = detail.blocking
 * || status === 'missing'; optional otherwise. providerEnvKey + connect-provider
 * are special-cased; empty list unless there is a pending action.
 */
export function onboardingReasons(status: OnboardingStatus, config: SetupConfig): Reason[] {
  if (!hasSetupAction(status)) return []
  const reasons: Reason[] = []
  const seen = new Set<string>()
  const push = (text: string, tier: Reason['tier'], step: StepId): void => {
    if (seen.has(text)) return
    seen.add(text)
    reasons.push({ text, tier, step })
  }
  const llm = config.llm || {}
  if (providerEnvMissing(status)) {
    push(`${providerEnvKey(config)} is not visible`, 'blocking', 'provider')
  } else if (!llm.provider || !llm.model) {
    push('Connect a model provider', 'blocking', 'provider')
  }
  const details = status.sectionDetails || {}
  Object.entries(details).forEach(([name, detail]) => {
    if (
      !detail.blocking &&
      !detail.actionRequired &&
      detail.status !== 'missing' &&
      detail.status !== 'degraded'
    ) {
      return
    }
    const step = stepForSection(name)
    const tier: Reason['tier'] =
      detail.blocking || detail.status === 'missing' ? 'blocking' : 'optional'
    if ((name === 'llm' || name === 'provider') && detail.status === 'missing') {
      push('Connect a model provider', 'blocking', step)
      return
    }
    if ((name === 'llm' || name === 'provider') && reasons.length) return
    push(setupActionReason(name, detail), tier, step)
  })
  if (!reasons.length) push('Review setup sections for pending actions', 'blocking', 'provider')
  return reasons
}

/** setup.js:177-192 — header headline + status chip tiered by reasons. */
export function setupHeadline(reasons: Reason[]): SetupHeadline {
  const blocking = reasons.filter((reason) => reason.tier === 'blocking').length
  const optional = reasons.length - blocking
  if (blocking) {
    return { title: 'Action needed', chip: 'Action needed', tone: 'is-warn' }
  }
  if (optional) {
    return {
      title: 'Optional improvements',
      chip: `Optional · ${optional} ${optional === 1 ? 'item' : 'items'}`,
      tone: 'is-optional',
    }
  }
  return { title: 'Ready to run', chip: 'Ready', tone: 'is-ok' }
}

// ── env recovery command lookup (setup.js:501-507) ──────────────────────────

/** setup.js:501-507 — the env recovery command for a section, or ''. */
export function envRecoveryCommand(status: OnboardingStatus, section: string): string {
  const commands = Array.isArray(status.envRecoveryCommands) ? status.envRecoveryCommands : []
  const entry = commands.find((e) => e && e.section === section && e.command)
  return entry ? entry.command! : ''
}

// ── provider derivation (setup.js:406-483) ──────────────────────────────────

export function providerRouterSupportText(spec: ProviderSpec | null | undefined): string {
  if (!spec || !spec.providerId) return 'choose provider'
  return spec.routerSupported === true ? 'Pilot Router ready' : 'Direct only'
}

export function providerRouterSupportTone(
  spec: ProviderSpec | null | undefined,
): 'is-ready' | 'is-direct' | 'is-neutral' {
  if (!spec || !spec.providerId) return 'is-neutral'
  return spec.routerSupported === true ? 'is-ready' : 'is-direct'
}

/** setup.js:416-419 — the saved config for a provider (only if it is the current one). */
export function providerConfigFor(config: SetupConfig, providerId: string): SetupConfig['llm'] {
  const current = config.llm || {}
  return current.provider === providerId ? current : {}
}

/** setup.js:421-428 — the configured (persisted + trusted) provider id, or ''. */
export function configuredProvider(status: OnboardingStatus, config: SetupConfig): string {
  const provider = String((config.llm || {}).provider || '').trim()
  if (!provider) return ''
  if (status.hasConfig !== false) return provider
  if (status.llmConfigured === true) return provider
  if (['explicit', 'env', 'not_required'].includes(status.llmSource ?? '')) return provider
  return ''
}

/**
 * setup.js:437-439 — the effective provider: a draft selection (if provided)
 * else the configured provider. `draftProviderId` is the live `<select>` value
 * or a restored draft (read at the edge in the component).
 */
export function effectiveProvider(
  status: OnboardingStatus,
  config: SetupConfig,
  draftProviderId = '',
): string {
  return draftProviderId || configuredProvider(status, config)
}

/** setup.js:441-447 — is a provider field "advanced" (base_url/proxy/optional-model)? */
export function isProviderAdvancedField(field: FieldSpec, spec: ProviderSpec): boolean {
  if (['base_url', 'proxy'].includes(field.name)) return true
  if (field.name === 'model') {
    return spec.routerSupported === true && field.required !== true
  }
  return false
}

/** setup.js:449-456 — a provider field's seed value from the saved config. */
export function providerFieldValue(
  field: FieldSpec,
  current: NonNullable<SetupConfig['llm']>,
): string {
  const name = field.name
  const def = String(field.default ?? '')
  if (name === 'model') return String(current.model || def || '')
  if (name === 'base_url') return String(current.base_url || def || '')
  if (name === 'proxy') return String(current.proxy || '')
  if (name === 'api_key_env')
    return String(current.api_key_env || (current.api_key ? '' : def) || '')
  return ''
}

/** setup.js:458-466 — should the advanced provider section open by default? */
export function providerAdvancedOpen(
  fields: FieldSpec[],
  current: NonNullable<SetupConfig['llm']>,
): boolean {
  return fields.some((field) => {
    if (field.required) return true
    const value = String(providerFieldValue(field, current) || '').trim()
    const defaultValue = String(field.default ?? '').trim()
    if (defaultValue) return value !== defaultValue
    return value.length > 0
  })
}

// ── credential / needs-list helpers (setup.js:333-353) ──────────────────────

/** setup.js:333-345 — rewrite generic credential needs to name the env key. */
export function credentialNeedList(
  items: string[] | undefined,
  envKey: string | undefined,
): string[] {
  const key = String(envKey || '').trim()
  const list = items || []
  if (!key) return list
  return list.map((item) => {
    if (/API key via [A-Z0-9_]+ or a one-time paste\./.test(item)) {
      return `API key via ${key} or a one-time paste.`
    }
    if (/Remote embedding API key or [A-Z0-9_]+ reference\./.test(item)) {
      return `Remote embedding API key or ${key} reference.`
    }
    return item
  })
}

/** setup.js:347-353 — the memory-embedding needs list (auto drops remote-fallback cred). */
export function memoryNeedList(
  spec: ProviderSpec | undefined,
  providerId: string,
  envKey: string | undefined,
): string[] {
  const items = (spec?.whatYouNeed || []).filter(Boolean)
  if (providerId === 'auto' && !String(envKey || '').trim()) {
    return items.filter((item) => !/remote fallback credentials/i.test(item))
  }
  return spec?.requiresApiKey ? credentialNeedList(items, envKey || spec.envKey) : items
}

// ── capability status text (setup.js:971-1058) ──────────────────────────────

/** setup.js:971-975 — the "$KEY not visible" status text (falls back when no key). */
export function missingEnvStatusText(
  capability: string,
  envKey: string | undefined,
  fallback: string,
): string {
  const key = String(envKey || '').trim()
  if (!key) return fallback
  return `${capability} is selected, but $${key} is not visible to the gateway.`
}

/** setup.js:977-992 — web search status text. */
export function searchStatusText(status: OnboardingStatus, config: SetupConfig): string {
  if (!config.search_provider) {
    return 'Web search is off until a provider is selected.'
  }
  if (status.searchConfigured === true) {
    return 'Web search is ready for new turns.'
  }
  if (status.searchSource === 'missing_env') {
    return missingEnvStatusText(
      'Web search',
      status.searchEnvKey,
      'Web search is selected but still needs a visible provider key.',
    )
  }
  return 'Web search is selected but still needs a visible provider key.'
}

/** setup.js:994-1012 — image generation status text. */
export function imageGenerationStatusText(status: OnboardingStatus): string {
  if (status.imageGenerationEnabled === false) {
    return 'Image generation is hidden from agents until this capability is enabled.'
  }
  if (status.imageGenerationConfigured === true) {
    if (status.imageGenerationSource === 'llm_fallback') {
      return 'Image generation will be available in new turns using the same provider key.'
    }
    return 'Image generation will be available in new turns once the gateway has the visible key.'
  }
  if (status.imageGenerationSource === 'missing_env') {
    return missingEnvStatusText(
      'Image generation',
      status.imageGenerationEnvKey,
      'Image generation is enabled but still needs a visible provider key before agents can use it.',
    )
  }
  return 'Image generation is enabled but still needs a visible provider key before agents can use it.'
}

/** setup.js:1014-1029 — voice audio status text. */
export function audioStatusText(status: OnboardingStatus): string {
  if (status.audioEnabled === false) {
    return 'Voice audio tools stay hidden until this capability is enabled.'
  }
  if (status.audioConfigured === true) {
    return 'Voice audio tools are ready for TTS, transcription, dubbing, cloning, conversion, and music.'
  }
  if (status.audioSource === 'missing_env') {
    return missingEnvStatusText(
      'Voice audio',
      status.audioEnvKey,
      'Voice audio is enabled but still needs a visible provider key.',
    )
  }
  return 'Voice audio is enabled but still needs a visible provider key.'
}

/** setup.js:1031-1058 — memory-embedding status text for the selected provider. */
export function memoryEmbeddingStatusText(
  status: OnboardingStatus,
  config: SetupConfig,
  providerId = '',
): string {
  const current = (config.memory || {}).embedding || {}
  const savedProvider = current.provider || current.mode || status.memoryEmbeddingProvider || 'auto'
  const provider = providerId || savedProvider
  if (provider === 'none') {
    return 'Keyword search stays available; embeddings are disabled.'
  }
  if (provider === 'local') {
    return 'Uses local BGE embeddings; no remote key is needed.'
  }
  if (provider === 'ollama') {
    return 'Uses your Ollama server; no API key is needed.'
  }
  if (provider === 'auto') {
    return 'Local-first memory search; optional remote fallback can be configured.'
  }
  if (provider === savedProvider && status.memoryEmbeddingConfigured === true) {
    return 'Remote memory embeddings are configured for new turns.'
  }
  if (provider === savedProvider && status.memoryEmbeddingSource === 'missing_env') {
    return missingEnvStatusText(
      'Remote memory embeddings',
      status.memoryEmbeddingEnvKey,
      'Remote memory embeddings need a visible provider key before they can run.',
    )
  }
  return 'Remote memory embeddings need a visible provider key before they can run.'
}

// ── memory-embedding provider control enablement (setup.js:741-749,1560-1575) ──

export interface MemoryControlFlags {
  remoteControlEnabled: boolean
  apiKeyEnabled: boolean
  localControlEnabled: boolean
  hasRemoteOptions: boolean
}

/** setup.js:741-749 — which memory-embedding controls are live for a provider. */
export function memoryControlFlags(
  providerId: string,
  spec: ProviderSpec | undefined,
): MemoryControlFlags {
  const remoteControlEnabled = ['auto', 'openai', 'openai-compatible', 'ollama'].includes(
    providerId,
  )
  const apiKeyEnabled = providerId === 'auto' || spec?.requiresApiKey === true
  const localControlEnabled = providerId === 'local'
  return {
    remoteControlEnabled,
    apiKeyEnabled,
    localControlEnabled,
    hasRemoteOptions: remoteControlEnabled || apiKeyEnabled,
  }
}

// ── router derivation (setup.js:550-635,1767-1855) ──────────────────────────

/** setup.js:566 — the two human-selectable router strategies. */
export const ROUTER_STRATEGIES = ['pilot-v1', 'llm_judge'] as const
export type RouterMode = 'pilot-v1' | 'llm_judge' | 'disabled'

/**
 * setup.js:567-569 — the Mode value: 'disabled' when router.enabled === false,
 * else the persisted strategy if it is one of the selectable ones, else the
 * pilot-v1 fallback (v4_phase3 / unknown force-migrate to pilot-v1).
 */
export function routerMode(router: NonNullable<SetupConfig['agentos_router']>): RouterMode {
  if (router.enabled === false) return 'disabled'
  const strategy = router.strategy ?? ''
  return (ROUTER_STRATEGIES as readonly string[]).includes(strategy)
    ? (strategy as RouterMode)
    : 'pilot-v1'
}

/** setup.js:557 — merge catalog profile tiers under the config's saved tiers. */
export function mergeTiers(
  profileTiers: Record<string, TierSpec> | undefined,
  configTiers: Record<string, TierSpec> | undefined,
): Record<string, TierSpec> {
  return Object.assign({}, profileTiers || {}, configTiers || {})
}

/** setup.js:626 — only text tiers + the image_model row are shown/saved. */
export function isVisibleTier(name: string): boolean {
  return (TEXT_TIERS as readonly string[]).includes(name) || name === 'image_model'
}

/**
 * setup.js:1767-1788 — resolve the judge-model RPC param from the dropdown state.
 * null preserves the persisted judge (incl. a CLI-configured local endpoint that
 * the cloud-only dropdown can't represent); '' clears to AUTO; a model id pins.
 * @param value   the current `<select>` value
 * @param loaded  the value the dropdown was rendered with (data-judge-loaded)
 * @param isLocal whether a local judge endpoint is persisted (data-judge-local)
 */
export function resolveJudgeModelParam(
  value: string,
  loaded: string,
  isLocal: boolean,
): string | null {
  if (isLocal) {
    return value ? value : null
  }
  return value === loaded ? null : value
}

export interface RouterTierInput {
  tier: string
  provider: string
  model: string
  thinkingLevel: string
  supportsImage: boolean
}

export interface RouterConfigureParams {
  mode: 'recommended' | 'disabled'
  strategy?: string
  defaultTier: string
  judgeModel: string | null
  safetyNetThreshold?: number
  tiers: Record<string, Record<string, unknown>>
}

/**
 * setup.js:1801-1846 — assemble the onboarding.router.configure payload from the
 * collected tier rows + mode/default/judge/threshold. `sel` is the Mode value.
 * The pilot threshold is forwarded ONLY for pilot-v1 with a finite value; the
 * image_model row is stamped supportsImage+image_only.
 */
export function buildRouterConfigureParams(input: {
  sel: RouterMode
  defaultTier: string
  judgeModel: string | null
  pilotThresholdRaw: string | undefined
  tiers: RouterTierInput[]
}): RouterConfigureParams {
  const tiers: Record<string, Record<string, unknown>> = {}
  input.tiers.forEach((row) => {
    const tier: Record<string, unknown> = {
      provider: row.provider,
      model: row.model,
      thinkingLevel: row.thinkingLevel,
      supportsImage: row.supportsImage,
    }
    if (row.tier === 'image_model') {
      tier.supportsImage = true
      tier.image_only = true
    }
    tiers[row.tier] = tier
  })
  const mode: 'recommended' | 'disabled' = input.sel === 'disabled' ? 'disabled' : 'recommended'
  const strategy = input.sel === 'disabled' ? undefined : input.sel
  const pilotThresholdNum = Number.parseFloat(input.pilotThresholdRaw ?? '')
  const safetyNetThreshold =
    input.sel === 'pilot-v1' && Number.isFinite(pilotThresholdNum) ? pilotThresholdNum : undefined
  return {
    mode,
    strategy,
    defaultTier: input.defaultTier,
    judgeModel: input.judgeModel,
    safetyNetThreshold,
    tiers,
  }
}

// ── scoped-field read + required validation (setup.js:1705-1741) ────────────

/** A minimal editable field the scoped reader/validator understands. */
export interface ScopedField {
  name: string // raw field name (snake_case)
  value: string
  checked: boolean
  type: string // 'checkbox' | 'password' | 'text' | ...
  secret: boolean
  required: boolean
  hidden: boolean
  label?: string // human label text (for the validation message)
}

/**
 * setup.js:1705-1716 — read visible scoped fields into an RPC params object.
 * `scope` 'channel' keeps snake_case names; every other scope camelCases them.
 * Checkboxes → bool; blank secrets are omitted (never send an empty secret).
 */
export function readScopedFields(fields: ScopedField[], scope: string): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  fields.forEach((f) => {
    if (f.hidden) return
    const name = scope === 'channel' ? f.name : camel(f.name)
    if (f.type === 'checkbox') out[name] = f.checked
    else if (f.value !== '' || !f.secret) out[name] = f.value
  })
  return out
}

/**
 * setup.js:1718-1730 — the first missing required field's label, or '' when all
 * required fields are satisfied. A blank secret is allowed only when the caller
 * says an existing secret can be kept (`canKeepSecret`, channel-only in legacy).
 */
export function validateScopedRequiredFields(
  fields: ScopedField[],
  canKeepSecret: boolean,
): string {
  for (const f of fields) {
    if (f.hidden || !f.required) continue
    if (f.type === 'checkbox') continue
    if (String(f.value || '').trim()) continue
    if (f.secret && canKeepSecret) continue
    return String(f.label || f.name || 'required field')
      .replace(/\s*\*\s*$/, '')
      .trim()
  }
  return ''
}

// ── capability payload builders (setup.js:1877-1994) ────────────────────────

/** A capability field read at the edge (already knows its disabled/secret state). */
export interface CapabilityField {
  name: string // raw field name (snake_case)
  value: string
  checked: boolean
  type: string // 'checkbox' | 'number' | 'password' | ...
  secret: boolean
  disabled: boolean
}

/**
 * setup.js:1877-1882 — onboarding.memory_embedding.configure params. Disabled
 * fields are skipped; a blank secret is omitted; keys are camelCased.
 */
export function buildMemoryConfigureParams(
  providerId: string,
  fields: CapabilityField[],
): Record<string, unknown> {
  const params: Record<string, unknown> = { providerId: providerId || 'auto' }
  fields.forEach((f) => {
    if (f.disabled) return
    if (f.value !== '' || !f.secret) params[camel(f.name)] = f.value
  })
  return params
}

/**
 * setup.js:1929-1935 — onboarding.search.configure params. A blank secret is
 * skipped; checkboxes → bool; number fields → parseInt; keys camelCased.
 */
export function buildSearchConfigureParams(
  providerId: string,
  fields: CapabilityField[],
): Record<string, unknown> {
  const params: Record<string, unknown> = { providerId: providerId || 'duckduckgo' }
  fields.forEach((f) => {
    if (f.value === '' && f.secret) return
    const key = camel(f.name)
    if (f.type === 'checkbox') params[key] = f.checked
    else params[key] = f.type === 'number' ? Number.parseInt(f.value || '0', 10) : f.value
  })
  return params
}

/**
 * setup.js:1947-1951 — onboarding.imageGeneration.configure params. `enabled`
 * from the toggle; blank secrets skipped; keys camelCased.
 */
export function buildImageConfigureParams(
  providerId: string,
  enabled: boolean,
  fields: CapabilityField[],
): Record<string, unknown> {
  const params: Record<string, unknown> = { providerId: providerId || 'openrouter', enabled }
  fields.forEach((f) => {
    if (f.value !== '' || !f.secret) params[camel(f.name)] = f.value
  })
  return params
}

/**
 * setup.js:1972-1976 — onboarding.audio.configure params. `enabled` from the
 * toggle; blank secrets skipped; keys camelCased.
 */
export function buildAudioConfigureParams(
  providerId: string,
  enabled: boolean,
  fields: CapabilityField[],
): Record<string, unknown> {
  const params: Record<string, unknown> = { providerId: providerId || 'elevenlabs', enabled }
  fields.forEach((f) => {
    if (f.value !== '' || !f.secret) params[camel(f.name)] = f.value
  })
  return params
}

/** setup.js:1907-1912 — the memory-settings config.patch patches object. */
export function buildMemorySettingsPatches(input: {
  providerName: string
  memoryLimit: string
  userLimit: string
  injectLimit: string
}): Record<string, unknown> {
  return {
    'memory.provider.name': input.providerName || null,
    'memory.curated_memory_char_limit': Number.parseInt(input.memoryLimit || '0', 10),
    'memory.curated_user_char_limit': Number.parseInt(input.userLimit || '0', 10),
    'memory.inject_limit': Number.parseInt(input.injectLimit || '0', 10),
  }
}

/**
 * setup.js:776-778 — the memory-settings over-budget heuristic. ~310 chars of
 * header/separator overhead per curated block; over budget when the two curated
 * limits + overhead exceed the injection limit.
 */
export const MEMORY_SETTINGS_OVERHEAD_CHARS = 310

export function memorySettingsOverBudget(
  memoryLimit: number,
  userLimit: number,
  injectLimit: number,
): boolean {
  return memoryLimit + userLimit + MEMORY_SETTINGS_OVERHEAD_CHARS > injectLimit
}

// ── env-reference save advisory (setup.js:1060-1075) ────────────────────────

export type EnvSaveAdvisory =
  { kind: 'none' } | { kind: 'warn'; message: string } | { kind: 'info'; message: string }

/**
 * setup.js:1060-1075 — the env-reference save advisory. Suppressed entirely when
 * there is no env key or an inline key was pasted; warns to restart when the key
 * is missing / a restart is required; else an info "keep it set" note.
 */
export function envReferenceSaveAdvisory(input: {
  surface: string
  envKey: string | undefined
  keySource?: string
  hasInlineKey?: unknown
  restartRequired?: boolean
}): EnvSaveAdvisory {
  const key = String(input.envKey || '').trim()
  if (!key || input.hasInlineKey) return { kind: 'none' }
  if (input.keySource === 'missing_env' || input.restartRequired) {
    return {
      kind: 'warn',
      message: `${input.surface} saved $${key}. Start or restart the gateway with that variable set.`,
    }
  }
  return {
    kind: 'info',
    message: `${input.surface} saved $${key} reference. Keep it set for gateway restarts.`,
  }
}

// ── Finish CLI command assembly (setup.js:1077-1208,2014-2022) ──────────────

/** setup.js:2018-2022 — POSIX shell-quote a value if it needs quoting. */
export function shellArg(value: string | undefined): string {
  const text = String(value || '')
  if (/^[A-Za-z0-9_@%+=:,./~-]+$/.test(text)) return text
  return `'${text.replace(/'/g, `'\\''`)}'`
}

/** setup.js:2014-2016 — the `--config <path>` CLI arg suffix (empty when no path). */
export function configCliArg(configPath: string | undefined): string {
  return configPath ? ` --config ${shellArg(configPath)}` : ''
}

export interface CliCommand {
  label: string
  command: string
}

/** setup.js:1089-1097 — the env-recovery commands surfaced on Finish. */
export function finishEnvRecoveryCommands(status: OnboardingStatus): CliCommand[] {
  return (Array.isArray(status.envRecoveryCommands) ? status.envRecoveryCommands : [])
    .map((entry) => ({
      label: entry.label || 'Set environment key',
      command: entry.command || '',
    }))
    .filter((entry) => entry.command)
}

/** setup.js:1187-1196 — the "Fix now" command group (env fixes + gateway restart). */
export function envFixCommands(envRecoveryCommands: CliCommand[], configArg: string): CliCommand[] {
  if (!envRecoveryCommands.length) return []
  return [
    ...envRecoveryCommands,
    { label: 'Restart gateway after env fix', command: `agentos gateway restart${configArg}` },
  ]
}

/** setup.js:1098-1107 — the CLI-handoff command group. */
export function handoffCommands(configArg: string): CliCommand[] {
  return [
    { label: 'Guided CLI', command: `agentos onboard --if-needed${configArg}` },
    { label: 'Check status', command: `agentos onboard status${configArg}` },
  ]
}

/** setup.js:1108-1133 — the CLI-recipes command group. */
export function recipeCommands(configArg: string): CliCommand[] {
  return [
    { label: 'Provider options', command: `agentos onboard catalog providers${configArg}` },
    { label: 'Router tiers', command: `agentos onboard catalog router${configArg}` },
    { label: 'Search options', command: `agentos onboard catalog search${configArg}` },
    { label: 'Channel options', command: `agentos onboard catalog channels${configArg}` },
    { label: 'Image options', command: `agentos onboard catalog image${configArg}` },
    { label: 'Memory options', command: `agentos onboard catalog memory${configArg}` },
  ]
}

// ── Finish summary + readiness (setup.js:1077-1288) ─────────────────────────

export interface FinishSummary {
  provider: string
  model: string
  proxy: string
  router: string
  channels: string
}

/** setup.js:1078-1150 — the Finish summary rows. */
export function finishSummary(status: OnboardingStatus, config: SetupConfig): FinishSummary {
  const router = config.agentos_router || {}
  const configured = configuredProvider(status, config)
  return {
    provider: configured || 'not configured',
    model: configured ? (config.llm || {}).model || 'Pilot Router defaults' : 'not configured',
    proxy: configured ? String((config.llm || {}).proxy || '').trim() : '',
    router: configured
      ? router.enabled === false
        ? 'disabled'
        : 'Pilot Router'
      : 'choose a provider first',
    channels: String(status.channelCount || 0),
  }
}

/** setup.js:1250-1254 — the router readiness row needs a provider first. */
export function routerNeedsProvider(detail: SectionDetail, name: string): boolean {
  return (
    name === 'router' &&
    detail.status === 'ok' &&
    detail.detail === 'uses Pilot Router after provider setup'
  )
}

/** setup.js:1277-1282 — readiness-row tone. */
export function readinessTone(
  detail: SectionDetail,
  name: string,
): 'is-ok' | 'is-warn' | 'is-muted' {
  if (routerNeedsProvider(detail, name)) return 'is-warn'
  if (detail.blocking || detail.actionRequired) return 'is-warn'
  if (detail.status === 'ok') return 'is-ok'
  return 'is-muted'
}

/** setup.js:1284-1288 — readiness-row status label. */
export function readinessStatusLabel(detail: SectionDetail, name: string): string {
  if (routerNeedsProvider(detail, name)) return 'Provider first'
  if (detail.blocking || detail.actionRequired) return 'Needs action'
  return READINESS_LABELS[detail.status ?? ''] || 'Optional'
}

/** setup.js:1256-1261 — readiness action button label. */
export function readinessActionLabel(detail: SectionDetail, name: string): string {
  if (routerNeedsProvider(detail, name)) return 'Choose provider'
  if (detail.blocking || detail.actionRequired) return 'Fix'
  if (detail.status === 'ok') return 'Review'
  return 'Configure'
}

/** setup.js:1241-1248 — the setup step a readiness row jumps to (''=no jump). */
export function setupStepForSection(name: string, detail: SectionDetail): StepId | '' {
  if (routerNeedsProvider(detail, name)) return 'provider'
  if (name === 'llm' || name === 'provider') return 'provider'
  if (name === 'router') return 'router'
  if (name === 'channels') return 'channels'
  if (name === 'search' || name === 'image_generation' || name === 'memory_embedding') {
    return 'extras'
  }
  return ''
}

// ── capability badge / save-button tone (setup.js:959-969) ──────────────────

/** setup.js:959-964 — a capability save button is primary when it needs action. */
export function capabilityIsPrimary(status: OnboardingStatus, name: string): boolean {
  const detail = (status.sectionDetails || {})[name] || {}
  return Boolean(detail.blocking || detail.actionRequired)
}

/** setup.js:966-969 — a capability readiness badge (tone + label). */
export function capabilityBadge(
  status: OnboardingStatus,
  name: string,
): { tone: 'is-ok' | 'is-warn' | 'is-muted'; label: string } {
  const detail = (status.sectionDetails || {})[name] || {}
  return { tone: readinessTone(detail, name), label: readinessStatusLabel(detail, name) }
}
