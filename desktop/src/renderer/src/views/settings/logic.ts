import {
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
  host: string
  port: string
  token: string
  cliPath: string
}

export function draftFromGateway(gw: GatewaySettings): GatewayDraft {
  return {
    mode: gw.mode,
    host: gw.host,
    port: String(gw.port),
    token: gw.token ?? '',
    cliPath: gw.cliPath ?? '',
  }
}

export function gatewayFromDraft(draft: GatewayDraft): GatewaySettings {
  return {
    mode: draft.mode,
    host: draft.host.trim(),
    port: Number(draft.port.trim()),
    token: draft.token.trim() || null,
    cliPath: draft.cliPath.trim() || null,
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
  return status.url !== `http://${saved.host}:${saved.port}`
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
    translateCeiling:
      router.translate_ceiling_enabled === false ? 'off' : router.translate_ceiling_tier || 'c0',
    tiers,
  }
}

export function routerDirty(saved: RouterDraft, draft: RouterDraft): boolean {
  return JSON.stringify(saved) !== JSON.stringify(draft)
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
