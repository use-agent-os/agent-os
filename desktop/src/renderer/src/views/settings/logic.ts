import {
  DEFAULT_GATEWAY_SETTINGS,
  gatewayUrl,
  isValidHost,
  isValidPort,
  type DesktopSettings,
  type GatewaySettings,
} from '@shared/settings'
import type { GatewayStatus } from '@shared/gateway'
import type { AppInfo } from '@shared/app'
import type { Catalog, ProviderSpec, RouterMode, SetupConfig, TierSpec } from '@/views/setup/logic'
import { mergeTiers, routerMode, TEXT_TIERS } from '@/views/setup/logic'

/* ── Gateway form ───────────────────────────────────────────────────────── */

/** What the Gateway pane edits: strings, so a half-typed port is representable. */
export interface GatewayDraft {
  mode: GatewaySettings['mode']
  scheme: GatewaySettings['scheme']
  host: string
  port: string
  token: string
  cliPath: string
}

export function draftFromGateway(gw: GatewaySettings): GatewayDraft {
  return {
    mode: gw.mode,
    scheme: gw.scheme,
    host: gw.host,
    port: String(gw.port),
    token: gw.token ?? '',
    cliPath: gw.cliPath ?? '',
  }
}

export function gatewayFromDraft(draft: GatewayDraft): GatewaySettings {
  return {
    mode: draft.mode,
    scheme: draft.scheme,
    host: draft.host.trim(),
    port: Number(draft.port.trim()),
    token: draft.token.trim() || null,
    cliPath: draft.cliPath.trim() || null,
  }
}

/**
 * Hosts a managed gateway can bind on this machine: loopback, `localhost`,
 * or the wildcard addresses. Anything else in the Host field was typed for
 * an external gateway somewhere else.
 */
export function isLocalGatewayHost(host: string): boolean {
  const h = host
    .trim()
    .toLowerCase()
    .replace(/^\[(.*)\]$/, '$1')
  return (
    h === 'localhost' ||
    h.endsWith('.localhost') ||
    /^127(\.\d{1,3}){3}$/.test(h) ||
    h === '0.0.0.0' ||
    h === '::1' ||
    h === '::'
  )
}

/**
 * The draft after the Run mode control changes. A managed gateway is spawned
 * on this machine with `--bind <host> --port <port>`, so an endpoint left
 * over from an external gateway (a remote host behind TLS on 443) can neither
 * be bound nor reached: going managed puts the endpoint back on the local
 * default. A local endpoint (a custom port, a LAN-wide bind) is kept, and so
 * is the token. Going external keeps the fields as they are for the user to
 * point elsewhere.
 */
export function switchGatewayMode(
  draft: GatewayDraft,
  mode: GatewaySettings['mode'],
): GatewayDraft {
  if (mode === draft.mode) return draft
  if (mode === 'external' || isLocalGatewayHost(draft.host)) return { ...draft, mode }
  return {
    ...draft,
    mode,
    scheme: DEFAULT_GATEWAY_SETTINGS.scheme,
    host: DEFAULT_GATEWAY_SETTINGS.host,
    port: String(DEFAULT_GATEWAY_SETTINGS.port),
  }
}

export interface GatewayDraftErrors {
  host?: 'invalid'
  port?: 'invalid'
}

export function gatewayDraftErrors(draft: GatewayDraft): GatewayDraftErrors {
  const errors: GatewayDraftErrors = {}
  if (!isValidHost(draft.host)) errors.host = 'invalid'
  const port = Number(draft.port.trim())
  if (!/^\d+$/.test(draft.port.trim()) || !isValidPort(port)) errors.port = 'invalid'
  return errors
}

/** True when saving the draft would change what is on disk. */
export function gatewayDirty(saved: GatewaySettings, draft: GatewayDraft): boolean {
  const next = gatewayFromDraft(draft)
  return (
    next.mode !== saved.mode ||
    next.scheme !== saved.scheme ||
    next.host !== saved.host ||
    next.port !== saved.port ||
    next.token !== saved.token ||
    next.cliPath !== saved.cliPath
  )
}

/**
 * The running gateway was started from a different endpoint than the saved
 * settings describe. Only the endpoint is checked: token and CLI path
 * changes matter too, but only a managed gateway is ours to restart.
 */
export function gatewayNeedsRestart(saved: GatewaySettings, status: GatewayStatus): boolean {
  if (status.state !== 'running' && status.state !== 'starting') return false
  if (!status.url) return false
  return status.url !== gatewayUrl(saved)
}

/* ── Provider (Models pane) ─────────────────────────────────────────────── */

export const THINKING_LEVELS = [
  'off',
  'minimal',
  'low',
  'medium',
  'high',
  'xhigh',
  'adaptive',
] as const
export type ThinkingLevel = (typeof THINKING_LEVELS)[number]

export function isThinkingLevel(value: unknown): value is ThinkingLevel {
  return typeof value === 'string' && (THINKING_LEVELS as readonly string[]).includes(value)
}

export interface CatalogModel {
  id: string
  name: string
  provider: string
}

export interface ModelOption {
  id: string
  /** The id, which is what config.toml holds and what the router tiers show. */
  label: string
  /** The vendor's display name, for a tooltip; empty when it adds nothing. */
  title: string
  /** The current model is not in the catalog; keep it selectable. */
  custom?: boolean
}

/**
 * A model picker's options: the provider's catalog, in catalog order, plus
 * the configured model when the catalog does not know it (typed into
 * config.toml, or a provider whose catalog is offline).
 */
export function modelOptions(
  models: readonly CatalogModel[],
  provider: string,
  current: string,
): ModelOption[] {
  const seen = new Set<string>()
  const out: ModelOption[] = []
  for (const m of models) {
    if (provider && m.provider !== provider) continue
    if (!m.id || seen.has(m.id)) continue
    seen.add(m.id)
    out.push({ id: m.id, label: m.id, title: m.name && m.name !== m.id ? m.name : '' })
  }
  if (current && !seen.has(current)) {
    out.unshift({ id: current, label: current, title: '', custom: true })
  }
  return out
}

/** The provider form. Secrets are never seeded: a blank key means "keep". */
export interface ProviderDraft {
  providerId: string
  model: string
  apiKey: string
  apiKeyEnv: string
  baseUrl: string
  proxy: string
}

/** A provider used before: the gateway keeps its model/env/base URL (never a key). */
export interface ProviderProfile {
  model?: string
  api_key_env?: string
  base_url?: string
  proxy?: string
}

export function providerProfile(config: SetupConfig, providerId: string): ProviderProfile | null {
  const profiles = (config as { provider_profiles?: Record<string, ProviderProfile> })
    .provider_profiles
  const profile = profiles?.[providerId]
  return profile && typeof profile === 'object' ? profile : null
}

/**
 * Seed the form: the live config for the active provider, the saved profile
 * for one used before, the catalog defaults otherwise. The key is never
 * seeded; blank means "keep what is stored".
 */
export function providerDraft(config: SetupConfig, spec: ProviderSpec | undefined): ProviderDraft {
  const llm = config.llm || {}
  const providerId = spec?.providerId ?? ''
  const own = llm.provider === providerId
  const profile = own ? null : providerProfile(config, providerId)
  const field = (name: string) => spec?.fields?.find((f) => f.name === name)
  const fallbackModel = String(spec?.defaultDirectModel || field('model')?.default || '')
  const fallbackEnv = String(spec?.envKey || field('api_key_env')?.default || '')
  const fallbackUrl = String(spec?.defaultBaseUrl || field('base_url')?.default || '')
  return {
    providerId,
    model: own ? String(llm.model || '') : String(profile?.model || fallbackModel),
    apiKey: '',
    apiKeyEnv: own ? String(llm.api_key_env || '') : String(profile?.api_key_env || fallbackEnv),
    baseUrl: own ? String(llm.base_url || '') : String(profile?.base_url || fallbackUrl),
    proxy: own ? String(llm.proxy || '') : String(profile?.proxy || ''),
  }
}

/**
 * Any OpenAI-compatible server. The gateway's runtime registry knows this
 * provider as `vllm` (openai_compat backend, no default URL, key optional)
 * but its onboarding catalog does not list it, so the desktop adds the tile
 * itself and writes it through `config.patch` instead of the guided RPC.
 */
export const CUSTOM_PROVIDER_ID = 'vllm'

export function customProviderSpec(label: string, need: string): ProviderSpec {
  return {
    providerId: CUSTOM_PROVIDER_ID,
    label,
    runtimeSupported: true,
    routerSupported: false,
    requiresApiKey: false,
    requiresBaseUrl: true,
    envKey: '',
    defaultBaseUrl: '',
    defaultDirectModel: '',
    deployment: 'custom',
    whatYouNeed: [need],
    fields: [],
  }
}

export function isCustomProvider(id: string): boolean {
  return id === CUSTOM_PROVIDER_ID
}

export interface CustomEndpointErrors {
  baseUrl?: 'invalid'
  model?: 'missing'
}

export function customEndpointErrors(draft: ProviderDraft): CustomEndpointErrors {
  const errors: CustomEndpointErrors = {}
  const url = draft.baseUrl.trim()
  let ok = false
  try {
    const parsed = new URL(url)
    ok = parsed.protocol === 'http:' || parsed.protocol === 'https:'
  } catch {
    ok = false
  }
  if (!ok) errors.baseUrl = 'invalid'
  if (!draft.model.trim()) errors.model = 'missing'
  return errors
}

/**
 * The `config.patch` payload for the custom endpoint. A typed key replaces
 * the stored one; none typed keeps it while this provider is already
 * active and clears it when switching in from another provider, so a key
 * for one vendor is never sent to a stranger's server.
 */
export function customEndpointPatch(
  draft: ProviderDraft,
  alreadyActive: boolean,
): Record<string, unknown> {
  const llm: Record<string, unknown> = {
    provider: CUSTOM_PROVIDER_ID,
    base_url: draft.baseUrl.trim(),
    model: draft.model.trim(),
    proxy: draft.proxy.trim(),
    api_key_env: '',
  }
  if (draft.apiKey.trim()) llm.api_key = draft.apiKey.trim()
  else if (!alreadyActive) llm.api_key = ''
  // The gateway refuses a tier profile that names another provider; there is
  // none for a custom server, and its tiers are degraded to llm.model at boot.
  return { patch: { llm, agentos_router: { tier_profile: null } } }
}

/** Shown first with a "Recommended" tag: one key, many models, router profile. */
export const RECOMMENDED_PROVIDER = 'opencap'

/**
 * Grid order for the provider tiles: the recommended provider first, then the
 * catalog's own order. Stable otherwise, so a user's mental map does not
 * shuffle when a key is added.
 */
export function orderProviders<T extends { providerId: string }>(providers: readonly T[]): T[] {
  const first = providers.filter((p) => p.providerId === RECOMMENDED_PROVIDER)
  const rest = providers.filter((p) => p.providerId !== RECOMMENDED_PROVIDER)
  return [...first, ...rest]
}

/**
 * The "Switch provider" filter: every word of the query must appear in the
 * provider's name or id, in any case. Order is kept, so the recommended
 * provider stays first among the matches.
 */
export function filterProviders<T extends { providerId: string; label?: string }>(
  providers: readonly T[],
  query: string,
): T[] {
  const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean)
  if (words.length === 0) return [...providers]
  return providers.filter((p) => {
    const hay = `${p.label ?? ''} ${p.providerId}`.toLowerCase()
    return words.every((w) => hay.includes(w))
  })
}

/**
 * What a failed key test most likely means, read from the provider's own
 * message. Drives the one human sentence above the raw text; the raw text is
 * always shown too, since it is what a support request needs.
 */
export type ProbeProblem = 'network' | 'auth' | 'scope' | 'quota' | 'model' | 'other'

export function probeProblem(raw: string): ProbeProblem {
  const text = raw.toLowerCase()
  if (
    /econn|enotfound|eai_again|getaddrinfo|fetch failed|timed? ?out|unreachable|network|connection (refused|reset)|certificate/.test(
      text,
    )
  )
    return 'network'
  if (
    /\b401\b|unauthori[sz]ed|invalid[ _-]?api[ _-]?key|incorrect api key|invalid key|authentication/.test(
      text,
    )
  )
    return 'auth'
  if (/\b403\b|forbidden|not scoped|not allowed|permission/.test(text)) return 'scope'
  if (/\b402\b|\b429\b|quota|insufficient|credit|billing|payment|rate.?limit/.test(text))
    return 'quota'
  if (/\b404\b|model.*(not found|does not exist|unknown)|unknown model|no such model/.test(text))
    return 'model'
  return 'other'
}

/** A provider message longer than this is folded to three lines behind "Show more". */
export const ERROR_CLAMP_CHARS = 180

export function isLongError(text: string): boolean {
  return text.length > ERROR_CLAMP_CHARS || text.split('\n').length > 3
}

/**
 * What a provider tile in Switch provider says about its key, and only that:
 * whether picking it would work today. A saved profile (the provider was
 * active once) says nothing about a key, since a profile never keeps one.
 */
export type ProviderTileState = 'custom' | 'local' | 'noKey' | 'keyReady' | 'needsKey'

export function providerTileState(
  spec: Pick<ProviderSpec, 'providerId' | 'requiresApiKey'> & { deployment?: unknown },
  /** The gateway's environment holds this provider's key (`providers.status`). */
  envKeyed: boolean,
): ProviderTileState {
  if (isCustomProvider(spec.providerId)) return 'custom'
  if (spec.deployment === 'local') return 'local'
  if (!spec.requiresApiKey) return 'noKey'
  return envKeyed ? 'keyReady' : 'needsKey'
}

export interface ProviderState {
  /** The provider every turn goes through right now. */
  active: boolean
  /** Used before: switching back restores model/env/base URL. */
  profile: ProviderProfile | null
}

export function providerState(
  spec: ProviderSpec,
  config: SetupConfig,
  configured: string,
): ProviderState {
  const active = spec.providerId === configured
  return { active, profile: active ? null : providerProfile(config, spec.providerId) }
}

/**
 * The `onboarding.provider.configure` payload. A pasted key is the explicit
 * credential source, so the env reference is dropped alongside it (the
 * gateway rejects both at once). Blank strings are sent for base URL and
 * proxy so a cleared field clears the config.
 */
export function providerConfigurePayload(draft: ProviderDraft): Record<string, unknown> {
  const params: Record<string, unknown> = {
    providerId: draft.providerId,
    model: draft.model.trim(),
    baseUrl: draft.baseUrl.trim(),
    proxy: draft.proxy.trim(),
  }
  if (draft.apiKey.trim()) params.apiKey = draft.apiKey.trim()
  else if (draft.apiKeyEnv.trim()) params.apiKeyEnv = draft.apiKeyEnv.trim()
  return params
}

export function providerDirty(saved: ProviderDraft, draft: ProviderDraft): boolean {
  return (
    saved.providerId !== draft.providerId ||
    saved.model.trim() !== draft.model.trim() ||
    draft.apiKey.trim() !== '' ||
    saved.apiKeyEnv.trim() !== draft.apiKeyEnv.trim() ||
    saved.baseUrl.trim() !== draft.baseUrl.trim() ||
    saved.proxy.trim() !== draft.proxy.trim()
  )
}

/** A provider needs a key it does not have yet: switching to it, or none saved. */
export function providerNeedsKey(
  draft: ProviderDraft,
  spec: ProviderSpec | undefined,
  config: SetupConfig,
): boolean {
  if (!spec?.requiresApiKey) return false
  if (draft.apiKey.trim()) return false
  const llm = config.llm || {}
  const own = llm.provider === draft.providerId
  const hasSaved = own && (Boolean(llm.api_key) || Boolean(llm.api_key_env))
  return !hasSaved && !draft.apiKeyEnv.trim()
}

/* ── Router pane ────────────────────────────────────────────────────────── */

export const TIER_ORDER = [...TEXT_TIERS, 'image_model'] as const
export type TierName = (typeof TIER_ORDER)[number]

export const ROUTER_THINKING = ['', 'off', 'minimal', 'low', 'medium', 'high', 'xhigh'] as const

export interface TierRow {
  tier: TierName
  model: string
  thinkingLevel: string
  supportsImage: boolean
  /** From the catalog profile: what this rung is for. */
  description: string
}

export interface RouterDraft {
  mode: RouterMode
  defaultTier: string
  judgeModel: string
  safetyNet: string
  // jev only. The public config never carries the key, so the field starts
  // blank: blank on save = keep the stored key or TYPESAFE_API_KEY.
  jevApiKey: string
  jevHighRisk: string
  translateCeiling: string // 'off' | tier
  tiers: TierRow[]
}

/**
 * Seed the router form from the saved config laid over the catalog profile
 * for the active provider, the same merge the console's setup does. Only the
 * text tiers and the image row are editable; the image row only exists when
 * the profile defines one.
 */
export function routerDraft(config: SetupConfig, catalog: Catalog, provider: string): RouterDraft {
  const router = config.agentos_router || {}
  const profile = (catalog.routerProfiles?.profiles || []).find((p) => p.providerId === provider)
  const merged = mergeTiers(profile?.tiers, router.tiers)
  const tiers: TierRow[] = []
  for (const tier of TIER_ORDER) {
    const spec: TierSpec | undefined = merged[tier]
    if (!spec) continue
    const fromProfile = profile?.tiers?.[tier]
    tiers.push({
      tier,
      model: String(spec.model || ''),
      thinkingLevel: String(spec.thinkingLevel || spec.thinking_level || ''),
      supportsImage: tier === 'image_model' || Boolean(spec.supportsImage ?? spec.supports_image),
      description: String(spec.description || fromProfile?.description || ''),
    })
  }
  return {
    mode: routerMode(router),
    defaultTier: router.default_tier || catalog.routerProfiles?.defaultTier || 'c1',
    judgeModel: router.judge_model || '',
    safetyNet:
      router.pilot?.safety_net_threshold != null
        ? String(router.pilot.safety_net_threshold)
        : '0.5',
    jevApiKey: '',
    jevHighRisk:
      router.jev?.high_risk_threshold != null ? String(router.jev.high_risk_threshold) : '0.7',
    translateCeiling:
      router.translate_ceiling_enabled === false ? 'off' : router.translate_ceiling_tier || 'c0',
    tiers,
  }
}

export function routerDirty(saved: RouterDraft, draft: RouterDraft): boolean {
  return JSON.stringify(saved) !== JSON.stringify(draft)
}

/** The Router page's unsaved edit, parked across section switches and sheet close. */
export function routerDraftId(provider: string): string {
  return `router:${provider}`
}

/**
 * The `config.patch` for a Router page save: the `agentos_router` keys the
 * edit changed, and nothing else.
 *
 * Not `onboarding.router.configure`: that guided write rewrites more than it
 * is asked to. Every save with the router on copies the default tier's model
 * into `llm.model` (the Default model the Providers page owns), and turning
 * the router off drops `tier_profile`, which persists the OpenRouter default
 * tiers under any provider (`onboarding.mutations.upsert_router`). The web
 * console sends every tier on every save as well, which pins the profile's
 * tiers into the file. A mode change here is the mode keys only; a tier edit
 * is that tier's changed fields only; `llm.model` is never written.
 *
 * Returns null when nothing changed.
 */
export function routerPatch(
  saved: RouterDraft,
  draft: RouterDraft,
  opts: { judgeIsLocal?: boolean } = {},
): { agentos_router: Record<string, unknown> } | null {
  const router: Record<string, unknown> = {}
  if (draft.mode !== saved.mode) {
    if (draft.mode === 'disabled') router.enabled = false
    else {
      router.enabled = true
      router.strategy = draft.mode
    }
  }
  if (draft.judgeModel !== saved.judgeModel) {
    // Auto is no pin at all (the engine resolves the cheapest tier); a cloud
    // pick replaces a local endpoint, as the guided write does. null deletes.
    router.judge_model = draft.judgeModel || null
    if (!draft.judgeModel) router.judge_provider = null
    if (opts.judgeIsLocal) {
      router.judge_base_url = null
      router.judge_api_key = null
    }
  }
  if (draft.safetyNet !== saved.safetyNet && safetyNetValid(draft.safetyNet)) {
    router.pilot = { safety_net_threshold: Number.parseFloat(draft.safetyNet) }
  }
  const jev: Record<string, unknown> = {}
  if (draft.jevApiKey.trim()) jev.api_key = draft.jevApiKey.trim()
  if (draft.jevHighRisk !== saved.jevHighRisk && safetyNetValid(draft.jevHighRisk)) {
    jev.high_risk_threshold = Number.parseFloat(draft.jevHighRisk)
  }
  if (Object.keys(jev).length > 0) router.jev = jev
  if (draft.translateCeiling !== saved.translateCeiling) {
    if (draft.translateCeiling === 'off') router.translate_ceiling_enabled = false
    else {
      router.translate_ceiling_enabled = true
      router.translate_ceiling_tier = draft.translateCeiling
    }
  }
  if (draft.defaultTier !== saved.defaultTier) router.default_tier = draft.defaultTier
  const tiers: Record<string, Record<string, unknown>> = {}
  for (const row of draft.tiers) {
    const before = saved.tiers.find((r) => r.tier === row.tier)
    const change: Record<string, unknown> = {}
    if (!before || before.model !== row.model) change.model = row.model
    // Blank is "the model's default": no level at all (null deletes the key).
    if (!before || before.thinkingLevel !== row.thinkingLevel)
      change.thinking_level = row.thinkingLevel || null
    if (Object.keys(change).length > 0) tiers[row.tier] = change
  }
  if (Object.keys(tiers).length > 0) router.tiers = tiers
  return Object.keys(router).length > 0 ? { agentos_router: router } : null
}

/** The parts of the Router page an unsaved edit touches, in page order. */
export type RouterChange = 'mode' | 'tuning' | 'tiers'

export function routerChanges(saved: RouterDraft, draft: RouterDraft): RouterChange[] {
  const changes: RouterChange[] = []
  if (
    saved.mode !== draft.mode ||
    saved.judgeModel !== draft.judgeModel ||
    draft.jevApiKey.trim() !== ''
  )
    changes.push('mode')
  if (
    saved.safetyNet !== draft.safetyNet ||
    saved.jevHighRisk !== draft.jevHighRisk ||
    saved.translateCeiling !== draft.translateCeiling
  )
    changes.push('tuning')
  if (
    saved.defaultTier !== draft.defaultTier ||
    JSON.stringify(saved.tiers) !== JSON.stringify(draft.tiers)
  )
    changes.push('tiers')
  return changes
}

/** The tiers a turn can be routed to, in cost order, for the default picker. */
export function textTiers(draft: RouterDraft): TierRow[] {
  return draft.tiers.filter((row) => row.tier !== 'image_model')
}

export function safetyNetValid(raw: string): boolean {
  const n = Number.parseFloat(raw)
  return Number.isFinite(n) && n >= 0 && n <= 1
}

/* ── About ──────────────────────────────────────────────────────────────── */

/** "3d 4h", "2h 05m", "12m", "40s". */
export function formatUptime(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000))
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  if (d > 0) return `${d}d ${h}h`
  if (h > 0) return `${h}h ${String(m).padStart(2, '0')}m`
  if (m > 0) return `${m}m`
  return `${s}s`
}

/* ── Advanced ───────────────────────────────────────────────────────────── */

/** Plain-text report for a bug report. The auth token never leaves the app. */
export function diagnosticsReport(input: {
  info: AppInfo | null
  gateway: GatewayStatus
  settings: DesktopSettings
  gatewayVersion?: string | null
  configPath?: string | null
  now?: Date
}): string {
  const { info, gateway, settings } = input
  const redacted = {
    ...settings,
    gateway: { ...settings.gateway, token: settings.gateway.token ? '<redacted>' : null },
  }
  const lines = [
    `AgentOS desktop diagnostics · ${(input.now ?? new Date()).toISOString()}`,
    '',
    `app: ${info?.version ?? '?'}${info && !info.packaged ? ' (dev)' : ''}`,
    `electron: ${info?.electron ?? '?'}  chromium: ${info?.chrome ?? '?'}  node: ${info?.node ?? '?'}`,
    `platform: ${info?.platform ?? '?'} ${info?.arch ?? ''}`.trimEnd(),
    '',
    `gateway: ${gateway.state}${gateway.url ? ` ${gateway.url}` : ''}${gateway.pid ? ` pid ${gateway.pid}` : ''}`,
    `gateway version: ${input.gatewayVersion ?? 'unknown'}`,
    `gateway config: ${input.configPath ?? 'unknown'}`,
  ]
  if (gateway.error) lines.push(`gateway error: ${gateway.error}`)
  lines.push('', 'settings:', JSON.stringify(redacted, null, 2))
  return lines.join('\n')
}
