// Pure setup helpers, first ported 1:1 from the retired console's setup.js
// view (the line ranges cited below point into that source). RPC calls, event
// subscriptions and rendering live in the callers (the web console's
// Settings › Setup forms, the desktop's settings panes and setup steps); this
// module owns the pure derivations: per-section status /
// readiness derivation, the onboarding-reasons list, provider/router/extras
// field-value derivation, the four onboarding.*.configure payload builders,
// scoped-field reading + required validation, and the Finish CLI command
// assembly. No `any`; secrets are never emitted from a builder when blank.

// ── shared catalog / status / config wire shapes ────────────────────────────

/** A field spec from onboarding.catalog (provider/channel/image/audio). */
import { t, tPlural } from '@/i18n'
import '@/i18n/en/setup'

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

/** auth.xai.login.start response. Neither field is a secret on its own. */
export interface XaiPendingLogin {
  loginId: string
  verificationUri: string
  userCode: string
  interval: number
}

/** auth.xai.login.poll response. */
export interface XaiLoginPoll {
  status: 'pending' | 'complete' | 'expired'
  interval?: number
}

/** auth.status response. Token values are never included. */
export interface AuthStatus {
  xai?: {
    loggedIn?: boolean
    logged_in?: boolean
    expires_at?: string | null
    expiring_soon?: boolean
    has_refresh_token?: boolean
    [key: string]: unknown
  }
  [key: string]: unknown
}

/** onboarding.catalog response. */
export interface Catalog {
  providers?: ProviderSpec[]
  searchProviders?: ProviderSpec[]
  /** One-row list: the catalog is uniform, and the CLI renderer walks rows. */
  xSearch?: ProviderSpec[]
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
    // The public config never carries jev.api_key; only its env name and the
    // tunables are exposed (config.py to_public).
    jev?: { api_key_env?: string; high_risk_threshold?: number | null; model?: string }
    translate_ceiling_enabled?: boolean | null
    translate_ceiling_tier?: string | null
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
  x_search?: {
    enabled?: boolean
    model?: string
    base_url?: string
    api_key_env?: string
    reasoning_effort?: string
    timeout_seconds?: number
    total_timeout_seconds?: number
    retries?: number
    [key: string]: unknown
  }
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

// ── constants ───────────────────────────────────────────────

export const TEXT_TIERS = ['c0', 'c1', 'c2', 'c3'] as const

function tierLabels(): Record<string, string> {
  return {
    c0: t('setup.tierC0'),
    c1: t('setup.tierC1'),
    c2: t('setup.tierC2'),
    c3: t('setup.tierC3'),
  }
}

function readinessLabels(): Record<string, string> {
  return {
    ok: t('setup.readyOk'),
    optional: t('setup.readyOptional'),
    missing: t('setup.readyMissing'),
    degraded: t('setup.readyDegraded'),
    unknown: t('setup.readyUnknown'),
  }
}

/** — section id → setup step id (shared by initial-step + reasons). */
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

/**
 * the ordered stepper steps. `label` is a getter: the array is a
 * module constant, so a resolved label would freeze at module-evaluation time
 * and keep the boot locale (#258).
 */
export const STEPS: Array<{ id: StepId; label: string }> = [
  {
    id: 'provider',
    get label() {
      return t('setup.stepProvider')
    },
  },
  {
    id: 'router',
    get label() {
      return t('setup.stepRouter')
    },
  },
  {
    id: 'extras',
    get label() {
      return t('setup.stepExtras')
    },
  },
  {
    id: 'finish',
    get label() {
      return t('setup.stepFinish')
    },
  },
]

// ── small utilities ─────────────────────────────────────────────────────────

/** — snake_case → camelCase for RPC param keys. */
export function camel(name: string): string {
  return String(name || '').replace(/_([a-z])/g, (_m, c: string) => c.toUpperCase())
}

/** — tier label with a c1 fallback. */
export function tierLabel(tier: string | undefined): string {
  return tierLabels()[tier ?? ''] || tier || t('setup.tierC1')
}

// ── status / readiness derivation ────────────────────────

export interface StepStatus {
  label: string
  tone: 'is-ok' | 'is-warn' | 'is-muted'
}

/** — provider env missing + selected env key. */
export function providerEnvMissing(status: OnboardingStatus): boolean {
  return status.llmSource === 'missing_env'
}

export function providerEnvKey(config: SetupConfig): string {
  return (config.llm || {}).api_key_env || 'the selected API key environment variable'
}

/** — a section detail needs action. */
export function stepDetailNeedsAction(detail: SectionDetail | undefined): boolean {
  return Boolean(
    detail &&
    (detail.blocking ||
      detail.actionRequired ||
      detail.status === 'missing' ||
      detail.status === 'degraded'),
  )
}

/** — status of a single detail-backed step. */
export function detailStepStatus(detail: SectionDetail | undefined): StepStatus {
  if (!detail) return { label: t('setup.chipReview'), tone: 'is-muted' }
  if (stepDetailNeedsAction(detail)) return { label: t('setup.chipNeedsAction'), tone: 'is-warn' }
  if (detail.status === 'ok') return { label: t('setup.chipReady'), tone: 'is-ok' }
  return {
    label: readinessLabels()[detail.status ?? ''] || t('setup.chipOptional'),
    tone: 'is-muted',
  }
}

/** — aggregate several sections into one step status. */
export function aggregateStepStatus(status: OnboardingStatus, sectionNames: string[]): StepStatus {
  const details = status.sectionDetails || {}
  const entries = sectionNames.map((name) => details[name]).filter(Boolean) as SectionDetail[]
  if (entries.some((detail) => stepDetailNeedsAction(detail))) {
    return { label: t('setup.chipNeedsAction'), tone: 'is-warn' }
  }
  if (entries.length && entries.every((detail) => detail.status === 'ok')) {
    return { label: t('setup.chipReady'), tone: 'is-ok' }
  }
  return { label: t('setup.chipOptional'), tone: 'is-muted' }
}

/** — any pending setup action anywhere. */
export function hasSetupAction(status: OnboardingStatus): boolean {
  if (status.needsOnboarding) return true
  const details = status.sectionDetails || {}
  return Object.values(details).some((detail) => stepDetailNeedsAction(detail))
}

/** — per-step status chip. `provider` is the effective provider. */
export function stepStatus(
  stepId: StepId,
  status: OnboardingStatus,
  effectiveProviderId: string,
): StepStatus {
  const details = status.sectionDetails || {}
  if (stepId === 'provider') {
    if (providerEnvMissing(status)) return { label: t('setup.chipNeedsAction'), tone: 'is-warn' }
    return detailStepStatus(details.llm || details.provider)
  }
  if (stepId === 'router' && !effectiveProviderId) {
    return { label: t('setup.chipProviderFirst'), tone: 'is-muted' }
  }
  if (stepId === 'router') return detailStepStatus(details.router)
  if (stepId === 'channels') return detailStepStatus(details.channels)
  if (stepId === 'extras') {
    return aggregateStepStatus(status, ['search', 'image_generation', 'audio', 'memory_embedding'])
  }
  if (stepId === 'finish') {
    return hasSetupAction(status)
      ? { label: t('setup.chipReview'), tone: 'is-warn' }
      : { label: t('setup.chipReady'), tone: 'is-ok' }
  }
  return { label: t('setup.chipReview'), tone: 'is-muted' }
}

/** — the step that fixes a given section. */
export function stepForSection(name: string): StepId {
  const entry = SECTION_STEPS.find(([section]) => section === name)
  return (entry ? entry[1] : 'provider') as StepId
}

/** — auto-select the initial step from status. */
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

// ── header headline + onboarding reasons ─────────────────

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

/** — reason text for a section (env-key aware). */
export function setupActionReason(name: string, detail: SectionDetail): string {
  const missingEnvPrefix = 'env key not visible: '
  const detailText = String(detail.detail || '')
  if (detailText.startsWith(missingEnvPrefix)) {
    const envKey = detailText.slice(missingEnvPrefix.length).trim()
    if (envKey) return t('setup.reasonEnvNotVisible', { key: envKey })
  }
  return t('setup.reasonSectionSetup', { label: detail.label || name })
}

/**
 * the tiered clickable reasons list. Blocking = detail.blocking
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
    push(t('setup.reasonEnvNotVisible', { key: providerEnvKey(config) }), 'blocking', 'provider')
  } else if (!llm.provider || !llm.model) {
    push(t('setup.reasonConnectProvider'), 'blocking', 'provider')
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
      push(t('setup.reasonConnectProvider'), 'blocking', step)
      return
    }
    if ((name === 'llm' || name === 'provider') && reasons.length) return
    push(setupActionReason(name, detail), tier, step)
  })
  if (!reasons.length) push(t('setup.reasonReviewSections'), 'blocking', 'provider')
  return reasons
}

/** — header headline + status chip tiered by reasons. */
export function setupHeadline(reasons: Reason[]): SetupHeadline {
  const blocking = reasons.filter((reason) => reason.tier === 'blocking').length
  const optional = reasons.length - blocking
  if (blocking) {
    return {
      title: t('setup.headlineActionNeeded'),
      chip: t('setup.headlineActionNeeded'),
      tone: 'is-warn',
    }
  }
  if (optional) {
    return {
      title: t('setup.headlineOptional'),
      chip: tPlural('setup.headlineOptionalChip', optional),
      tone: 'is-optional',
    }
  }
  return { title: t('setup.headlineReady'), chip: t('setup.chipReady'), tone: 'is-ok' }
}

// ── env recovery command lookup ──────────────────────────

/** — the env recovery command for a section, or ''. */
export function envRecoveryCommand(status: OnboardingStatus, section: string): string {
  const commands = Array.isArray(status.envRecoveryCommands) ? status.envRecoveryCommands : []
  const entry = commands.find((e) => e && e.section === section && e.command)
  return entry ? entry.command! : ''
}

// ── provider derivation ──────────────────────────────────

export function providerRouterSupportText(spec: ProviderSpec | null | undefined): string {
  if (!spec || !spec.providerId) return 'choose provider'
  return spec.routerSupported === true ? t('setup.routerSupported') : t('setup.routerDirectOnly')
}

export function providerRouterSupportTone(
  spec: ProviderSpec | null | undefined,
): 'is-ready' | 'is-direct' | 'is-neutral' {
  if (!spec || !spec.providerId) return 'is-neutral'
  return spec.routerSupported === true ? 'is-ready' : 'is-direct'
}

/** — the saved config for a provider (only if it is the current one). */
export function providerConfigFor(config: SetupConfig, providerId: string): SetupConfig['llm'] {
  const current = config.llm || {}
  return current.provider === providerId ? current : {}
}

/** — the configured (persisted + trusted) provider id, or ''. */
export function configuredProvider(status: OnboardingStatus, config: SetupConfig): string {
  const provider = String((config.llm || {}).provider || '').trim()
  if (!provider) return ''
  if (status.hasConfig !== false) return provider
  if (status.llmConfigured === true) return provider
  if (['explicit', 'env', 'not_required'].includes(status.llmSource ?? '')) return provider
  return ''
}

/**
 * the effective provider: a draft selection (if provided)
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

/** — is a provider field "advanced" (base_url/proxy/optional-model)? */
export function isProviderAdvancedField(field: FieldSpec, spec: ProviderSpec): boolean {
  if (['base_url', 'proxy'].includes(field.name)) return true
  if (field.name === 'model') {
    return spec.routerSupported === true && field.required !== true
  }
  return false
}

/** — a provider field's seed value from the saved config. */
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

/** — should the advanced provider section open by default? */
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

// ── credential / needs-list helpers ──────────────────────

/** — rewrite generic credential needs to name the env key. */
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

/** — the memory-embedding needs list (auto drops remote-fallback cred). */
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

// ── capability status text ──────────────────────────────

/** — the "$KEY not visible" status text (falls back when no key). */
export function missingEnvStatusText(
  capability: string,
  envKey: string | undefined,
  fallback: string,
): string {
  const key = String(envKey || '').trim()
  if (!key) return fallback
  return t('setup.missingEnvStatus', { capability, key })
}

/** — web search status text. */
export function searchStatusText(status: OnboardingStatus, config: SetupConfig): string {
  if (!config.search_provider) {
    return t('setup.searchOff')
  }
  if (status.searchConfigured === true) {
    return t('setup.searchReady')
  }
  if (status.searchSource === 'missing_env') {
    return missingEnvStatusText(
      t('setup.capSearch'),
      status.searchEnvKey,
      t('setup.searchNeedsKey'),
    )
  }
  return t('setup.searchNeedsKey')
}

/**
 * Which xAI credential x_search will actually use.
 *
 * OAuth wins over an API key at call time, so the card has to say which one is
 * in play — "you have a key configured" is misleading when a SuperGrok login is
 * quietly taking precedence.
 */
/** Whether a SuperGrok / X Premium+ login is stored. */
export function xSearchSignedIn(auth: AuthStatus): boolean {
  const xai = auth.xai || {}
  return xai.loggedIn === true || xai.logged_in === true
}

export function xSearchCredentialText(auth: AuthStatus, config: SetupConfig): string {
  const xai = auth.xai || {}
  const loggedIn = xSearchSignedIn(auth)
  if (loggedIn) {
    return xai.has_refresh_token === false
      ? t('setup.xSearchOauthIncomplete')
      : t('setup.xSearchOauthActive')
  }
  const envKey = config.x_search?.api_key_env || 'XAI_API_KEY'
  return t('setup.xSearchOauthAbsent', { envKey })
}

/** — image generation status text. */
export function imageGenerationStatusText(status: OnboardingStatus): string {
  if (status.imageGenerationEnabled === false) {
    return t('setup.imageDisabled')
  }
  if (status.imageGenerationConfigured === true) {
    if (status.imageGenerationSource === 'llm_fallback') {
      return t('setup.imageReadySameKey')
    }
    return t('setup.imageReadyVisibleKey')
  }
  if (status.imageGenerationSource === 'missing_env') {
    return missingEnvStatusText(
      t('setup.capImage'),
      status.imageGenerationEnvKey,
      t('setup.imageNeedsKey'),
    )
  }
  return t('setup.imageNeedsKey')
}

/** — voice audio status text. */
export function audioStatusText(status: OnboardingStatus): string {
  if (status.audioEnabled === false) {
    return t('setup.audioDisabled')
  }
  if (status.audioConfigured === true) {
    return t('setup.audioReady')
  }
  if (status.audioSource === 'missing_env') {
    return missingEnvStatusText(t('setup.capAudio'), status.audioEnvKey, t('setup.audioNeedsKey'))
  }
  return t('setup.audioNeedsKey')
}

/** — memory-embedding status text for the selected provider. */
export function memoryEmbeddingStatusText(
  status: OnboardingStatus,
  config: SetupConfig,
  providerId = '',
): string {
  const current = (config.memory || {}).embedding || {}
  const savedProvider = current.provider || current.mode || status.memoryEmbeddingProvider || 'auto'
  const provider = providerId || savedProvider
  if (provider === 'none') {
    return t('setup.memoryKeywordOnly')
  }
  if (provider === 'local') {
    return t('setup.memoryLocalBge')
  }
  if (provider === 'ollama') {
    return t('setup.memoryOllama')
  }
  if (provider === 'auto') {
    return t('setup.memoryLocalFirst')
  }
  if (provider === savedProvider && status.memoryEmbeddingConfigured === true) {
    return t('setup.memoryRemoteReady')
  }
  if (provider === savedProvider && status.memoryEmbeddingSource === 'missing_env') {
    return missingEnvStatusText(
      t('setup.capMemory'),
      status.memoryEmbeddingEnvKey,
      t('setup.memoryNeedsKey'),
    )
  }
  return t('setup.memoryNeedsKey')
}

// ── memory-embedding provider control enablement ──

export interface MemoryControlFlags {
  remoteControlEnabled: boolean
  apiKeyEnabled: boolean
  localControlEnabled: boolean
  hasRemoteOptions: boolean
}

/** — which memory-embedding controls are live for a provider. */
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

// ── router derivation ──────────────────────────

/** — the human-selectable router strategies (jev is experimental, opt-in). */
export const ROUTER_STRATEGIES = ['pilot-v1', 'llm_judge', 'jev'] as const
export type RouterMode = 'pilot-v1' | 'llm_judge' | 'jev' | 'disabled'

/**
 * the Mode value: 'disabled' when router.enabled === false,
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

/** — merge catalog profile tiers under the config's saved tiers. */
export function mergeTiers(
  profileTiers: Record<string, TierSpec> | undefined,
  configTiers: Record<string, TierSpec> | undefined,
): Record<string, TierSpec> {
  return Object.assign({}, profileTiers || {}, configTiers || {})
}

/** — only text tiers + the image_model row are shown/saved. */
export function isVisibleTier(name: string): boolean {
  return (TEXT_TIERS as readonly string[]).includes(name) || name === 'image_model'
}

/**
 * resolve the judge-model RPC param from the dropdown state.
 * null preserves the persisted judge (incl. a CLI-configured local endpoint that
 * the cloud-only dropdown can't represent); '' clears to AUTO; a model id pins.
 * @param value the current `<select>` value
 * @param loaded the value the dropdown was rendered with (data-judge-loaded)
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
  /**
   * The tier's other saved fields (`description`, …), sent back unchanged:
   * the gateway rebuilds a tier from its profile default plus what is sent,
   * so a field left out reverts to the default.
   */
  extra?: Record<string, unknown>
}

export interface RouterConfigureParams {
  mode: 'recommended' | 'disabled'
  strategy?: string
  defaultTier: string
  judgeModel: string | null
  safetyNetThreshold?: number
  // jev only: null preserves the persisted key (or the TYPESAFE_API_KEY env
  // fallback), '' clears it. Never present for the other strategies.
  jevApiKey?: string | null
  jevHighRiskThreshold?: number
  translateCeilingEnabled: boolean
  translateCeilingTier: string
  tiers: Record<string, Record<string, unknown>>
}

/**
 * assemble the onboarding.router.configure payload from the
 * collected tier rows + mode/default/judge/threshold. `sel` is the Mode value.
 * The pilot threshold is forwarded ONLY for pilot-v1 with a finite value; the
 * jev key/threshold ONLY for jev (blank key → null = preserve, threshold only
 * when finite and within 0..1); the image_model row is stamped
 * supportsImage+image_only.
 */
export function buildRouterConfigureParams(input: {
  sel: RouterMode
  defaultTier: string
  judgeModel: string | null
  pilotThresholdRaw: string | undefined
  jevApiKeyRaw?: string
  jevHighRiskThresholdRaw?: string
  translateCeilingEnabled: boolean
  translateCeilingTier: string
  tiers: RouterTierInput[]
}): RouterConfigureParams {
  const tiers: Record<string, Record<string, unknown>> = {}
  input.tiers.forEach((row) => {
    const tier: Record<string, unknown> = {
      ...row.extra,
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
  const params: RouterConfigureParams = {
    mode,
    strategy,
    defaultTier: input.defaultTier,
    judgeModel: input.judgeModel,
    safetyNetThreshold,
    // Sent for every mode: the translation cap is an engine guard, not a
    // property of the selected strategy.
    translateCeilingEnabled: input.translateCeilingEnabled,
    translateCeilingTier: input.translateCeilingTier,
    tiers,
  }
  if (input.sel === 'jev') {
    const key = (input.jevApiKeyRaw ?? '').trim()
    params.jevApiKey = key ? key : null
    const threshold = Number.parseFloat(input.jevHighRiskThresholdRaw ?? '')
    if (Number.isFinite(threshold) && threshold >= 0 && threshold <= 1) {
      params.jevHighRiskThreshold = threshold
    }
  }
  return params
}

// ── scoped-field read + required validation ────────────

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
 * read visible scoped fields into an RPC params object.
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
 * the first missing required field's label, or '' when all
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

// ── capability payload builders ────────────────────────

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
 * onboarding.memory_embedding.configure params. Disabled
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
 * onboarding.search.configure params. A blank secret is
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
 * onboarding.x_search.configure params. Same blank-secret rule as search — an
 * empty api_key means "keep the stored one" — plus an explicit `enabled` flag
 * so the card can turn the tool off without clearing the credential.
 */
export function buildXSearchConfigureParams(
  enabled: boolean,
  fields: CapabilityField[],
): Record<string, unknown> {
  const params: Record<string, unknown> = { enabled }
  fields.forEach((f) => {
    if (f.value === '' && f.secret) return
    const key = camel(f.name)
    if (f.type === 'checkbox') params[key] = f.checked
    else params[key] = f.type === 'number' ? Number.parseInt(f.value || '0', 10) : f.value
  })
  return params
}

/**
 * onboarding.imageGeneration.configure params. `enabled`
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
 * onboarding.audio.configure params. `enabled` from the
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

/** — the memory-settings config.patch patches object. */
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
 * the memory-settings over-budget heuristic. ~310 chars of
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

// ── env-reference save advisory ────────────────────────

export type EnvSaveAdvisory =
  { kind: 'none' } | { kind: 'warn'; message: string } | { kind: 'info'; message: string }

/**
 * the env-reference save advisory. Suppressed entirely when
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

// ── Finish CLI command assembly ──────────────

/** — POSIX shell-quote a value if it needs quoting. */
export function shellArg(value: string | undefined): string {
  const text = String(value || '')
  if (/^[A-Za-z0-9_@%+=:,./~-]+$/.test(text)) return text
  return `'${text.replace(/'/g, `'\\''`)}'`
}

/** — the `--config <path>` CLI arg suffix (empty when no path). */
export function configCliArg(configPath: string | undefined): string {
  return configPath ? ` --config ${shellArg(configPath)}` : ''
}

export interface CliCommand {
  label: string
  command: string
}

/** — the env-recovery commands surfaced on Finish. */
export function finishEnvRecoveryCommands(status: OnboardingStatus): CliCommand[] {
  return (Array.isArray(status.envRecoveryCommands) ? status.envRecoveryCommands : [])
    .map((entry) => ({
      label: entry.label || t('setup.cliSetEnvKey'),
      command: entry.command || '',
    }))
    .filter((entry) => entry.command)
}

/** — the "Fix now" command group (env fixes + gateway restart). */
export function envFixCommands(envRecoveryCommands: CliCommand[], configArg: string): CliCommand[] {
  if (!envRecoveryCommands.length) return []
  return [
    ...envRecoveryCommands,
    { label: t('setup.cliRestartGateway'), command: `agentos gateway restart${configArg}` },
  ]
}

/** — the CLI-handoff command group. */
export function handoffCommands(configArg: string): CliCommand[] {
  return [
    { label: t('setup.cliGuided'), command: `agentos onboard --if-needed${configArg}` },
    { label: t('setup.cliCheckStatus'), command: `agentos onboard status${configArg}` },
  ]
}

/** — the CLI-recipes command group. */
export function recipeCommands(configArg: string): CliCommand[] {
  return [
    {
      label: t('setup.cliProviderOptions'),
      command: `agentos onboard catalog providers${configArg}`,
    },
    { label: t('setup.cliRouterTiers'), command: `agentos onboard catalog router${configArg}` },
    { label: t('setup.cliSearchOptions'), command: `agentos onboard catalog search${configArg}` },
    {
      label: t('setup.cliChannelOptions'),
      command: `agentos onboard catalog channels${configArg}`,
    },
    { label: t('setup.cliImageOptions'), command: `agentos onboard catalog image${configArg}` },
    { label: t('setup.cliMemoryOptions'), command: `agentos onboard catalog memory${configArg}` },
  ]
}

// ── Finish summary + readiness ─────────────────────────

export interface FinishSummary {
  provider: string
  model: string
  proxy: string
  router: string
  channels: string
}

/** — the Finish summary rows. */
export function finishSummary(status: OnboardingStatus, config: SetupConfig): FinishSummary {
  const router = config.agentos_router || {}
  const configured = configuredProvider(status, config)
  return {
    provider: configured || 'not configured',
    model: configured
      ? (config.llm || {}).model || t('setup.modelPilotDefaults')
      : t('setup.modelNotConfigured'),
    proxy: configured ? String((config.llm || {}).proxy || '').trim() : '',
    router: configured
      ? router.enabled === false
        ? 'disabled'
        : t('setup.routerPilot')
      : 'choose a provider first',
    channels: String(status.channelCount || 0),
  }
}

/** — the router readiness row needs a provider first. */
export function routerNeedsProvider(detail: SectionDetail, name: string): boolean {
  return (
    name === 'router' &&
    detail.status === 'ok' &&
    detail.detail === 'uses Pilot Router after provider setup'
  )
}

/** — readiness-row tone. */
export function readinessTone(
  detail: SectionDetail,
  name: string,
): 'is-ok' | 'is-warn' | 'is-muted' {
  if (routerNeedsProvider(detail, name)) return 'is-warn'
  if (detail.blocking || detail.actionRequired) return 'is-warn'
  if (detail.status === 'ok') return 'is-ok'
  return 'is-muted'
}

/** — readiness-row status label. */
export function readinessStatusLabel(detail: SectionDetail, name: string): string {
  if (routerNeedsProvider(detail, name)) return t('setup.chipProviderFirst')
  if (detail.blocking || detail.actionRequired) return t('setup.chipNeedsAction')
  return readinessLabels()[detail.status ?? ''] || t('setup.chipOptional')
}

/** — readiness action button label. */
export function readinessActionLabel(detail: SectionDetail, name: string): string {
  if (routerNeedsProvider(detail, name)) return t('setup.chipChooseProvider')
  if (detail.blocking || detail.actionRequired) return 'Fix'
  if (detail.status === 'ok') return t('setup.chipReview')
  return t('setup.chipConfigure')
}

/** — the setup step a readiness row jumps to (''=no jump). */
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

// ── capability badge / save-button tone ──────────────────

/** — a capability save button is primary when it needs action. */
export function capabilityIsPrimary(status: OnboardingStatus, name: string): boolean {
  const detail = (status.sectionDetails || {})[name] || {}
  return Boolean(detail.blocking || detail.actionRequired)
}

/** — a capability readiness badge (tone + label). */
export function capabilityBadge(
  status: OnboardingStatus,
  name: string,
): { tone: 'is-ok' | 'is-warn' | 'is-muted'; label: string } {
  const detail = (status.sectionDetails || {})[name] || {}
  return { tone: readinessTone(detail, name), label: readinessStatusLabel(detail, name) }
}

// ── router tier model catalog (#142) ────────────────────────────────────────
// Two sources, and they have to be unioned rather than chosen between:
// `models.list` (rpc_models.py:33) is live but partial — it aggregates the
// currently configured provider chain, and a full `ModelCatalog` is only
// fetched for some providers. It answers nothing at all before an API key is
// saved, which is exactly when this form is being filled in.
// `onboarding.catalog.routerProfiles` ships with the package, is per
// provider, and carries the recommended model for every tier including
// `image_model`. It is small, and it is the source the tier rows are seeded
// from — so treating it as authoritative on its own would flag ids the
// gateway does know, and treating it as absent would flag the form's own
// defaults.

/** A model the tier picker can offer, from either source. */
export interface ModelOption {
  id: string
  name: string
  contextWindow?: number
  inputPer1k?: number
  outputPer1k?: number
  supportsVision?: boolean
}

/** A `models.list` row (rpc_models.py:13-30). */
export interface ModelListEntry {
  id?: string
  name?: string
  provider?: string
  contextWindow?: number
  capabilities?: string[]
  pricing?: { inputPer1k?: number; outputPer1k?: number }
}

/**
 * The models the offline catalog knows for a provider, from its router profile
 * tiers. `visionOnly` keeps just the tiers that generate images — the image row
 * must not be offered text models, which is what the tier table is for.
 */
export function offlineTierModels(
  profileTiers: Record<string, TierSpec> | undefined,
  opts: { visionOnly?: boolean } = {},
): ModelOption[] {
  const seen = new Set<string>()
  const options: ModelOption[] = []
  for (const [name, tier] of Object.entries(profileTiers || {})) {
    const id = String(tier?.model || '').trim()
    if (!id || seen.has(id)) continue
    const isImageTier = name === 'image_model'
    const supportsVision = isImageTier || Boolean(tier?.supportsImage ?? tier?.supports_image)
    if (opts.visionOnly && !supportsVision) continue
    seen.add(id)
    options.push({ id, name: id, supportsVision })
  }
  return options
}

/**
 * Live rows first (they carry context window and pricing), then offline.
 *
 * `live` is whatever the RPC returned — an older gateway, or a handler that
 * errored into an object, must degrade to the offline list rather than throw
 * inside a render.
 */
export function mergeModelOptions(
  live: ModelListEntry[] | undefined | unknown,
  offline: ModelOption[],
): ModelOption[] {
  const byId = new Map<string, ModelOption>()
  for (const entry of Array.isArray(live) ? (live as ModelListEntry[]) : []) {
    const id = String(entry?.id || '').trim()
    if (!id || byId.has(id)) continue
    byId.set(id, {
      id,
      name: entry.name || id,
      contextWindow: entry.contextWindow,
      inputPer1k: entry.pricing?.inputPer1k,
      outputPer1k: entry.pricing?.outputPer1k,
      supportsVision: (entry.capabilities || []).includes('vision'),
    })
  }
  for (const option of offline) {
    if (!byId.has(option.id)) byId.set(option.id, option)
  }
  return [...byId.values()]
}

/**
 * "128k ctx · $2.50/$10.00 per 1M" — the numbers that decide a tier choice.
 * Empty when the offline catalog is the only source, which carries neither.
 */
export function modelOptionMeta(option: ModelOption): string {
  const parts: string[] = []
  const ctx = Number(option.contextWindow || 0)
  if (ctx > 0) parts.push(`${Math.round(ctx / 1000)}k ctx`)
  const input = Number(option.inputPer1k || 0) * 1000
  const output = Number(option.outputPer1k || 0) * 1000
  if (input > 0 || output > 0) {
    parts.push(`$${input.toFixed(2)}/$${output.toFixed(2)} per 1M`)
  }
  return parts.join(' · ')
}

/** A datalist option's visible label. */
export function modelOptionLabel(option: ModelOption): string {
  const meta = modelOptionMeta(option)
  const base = option.name || option.id
  return meta ? `${base} (${meta})` : base
}

export interface RouterModelWarnings {
  /** Nothing to check against — say so instead of silently accepting. */
  noCatalog: boolean
  /** Entered ids no source knows. */
  unknown: string[]
  /** The image tier pointed at a model the catalog says is not vision-capable. */
  nonVision: string[]
}

/**
 * Warn-only validation for Save (#142: "Warn, don't block — unknown ids are
 * legitimate; silently accepting a typo is not").
 *
 * Membership is checked against every known id, not per-row: typing a text
 * model into the image row is a different mistake from typing a model that
 * does not exist, and conflating them reports the wrong one.
 */
export function classifyRouterModels(
  rows: Array<{ tier: string; model: string }>,
  textOptions: ModelOption[],
  visionOptions: ModelOption[],
): RouterModelWarnings {
  const known = new Set<string>()
  const vision = new Set<string>()
  for (const option of textOptions) {
    known.add(option.id)
    if (option.supportsVision) vision.add(option.id)
  }
  for (const option of visionOptions) {
    known.add(option.id)
    vision.add(option.id)
  }

  const entered = rows.filter((row) => row.model.trim())
  if (known.size === 0) {
    return { noCatalog: entered.length > 0, unknown: [], nonVision: [] }
  }

  const unknown: string[] = []
  const nonVision: string[] = []
  for (const row of entered) {
    const model = row.model.trim()
    if (!known.has(model)) {
      if (!unknown.includes(model)) unknown.push(model)
      continue
    }
    if (row.tier === 'image_model' && !vision.has(model) && !nonVision.includes(model)) {
      nonVision.push(model)
    }
  }
  return { noCatalog: false, unknown, nonVision }
}
