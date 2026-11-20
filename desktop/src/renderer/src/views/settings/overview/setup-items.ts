import type { GatewayState } from '@shared/gateway'
import {
  routerMode,
  routerNeedsProvider,
  TEXT_TIERS,
  type SectionDetail,
  type SetupConfig,
} from '@/views/setup/logic'
import type { SettingsSnapshot } from '@/views/settings/snapshot'
import { t, type MessageKey } from '~/i18n'
import type { SettingsSection } from '../sections'

/**
 * The Overview checklist as data: one item per thing a person sets up, each
 * with its state, a line of current fact, and where its button goes. Pure,
 * so the page and its tests read the same model.
 */

export type SetupItemId =
  | 'gateway'
  | 'provider'
  | 'router'
  | 'memory'
  | 'search'
  | 'image'
  | 'voice'
  | 'channels'
  | 'skills'
  | 'env'
  | 'wallet'
  | 'updates'

/**
 * done: in place and working. todo: not set up yet (fine to skip unless
 * required). attention: set up but broken (a key is missing). checking: the
 * fact is still loading; not counted.
 */
export type SetupState = 'done' | 'todo' | 'attention' | 'checking'
export type SetupGroup = 'required' | 'recommended'

/** Where an item's button goes: a Settings section (and a card in it), or start the gateway. */
export type SetupJump =
  { kind: 'section'; section: SettingsSection; focus?: string } | { kind: 'start-gateway' }

export interface SetupItem {
  id: SetupItemId
  group: SetupGroup
  state: SetupState
  title: string
  detail: string
  /** The row button's word: Set up, Fix, Review, Start. */
  action: string
  jump: SetupJump
}

export interface SetupInput {
  /** The RPC socket is up: the snapshot and the lists below can be trusted. */
  connected: boolean
  gateway: {
    state: GatewayState
    /** The app connects to a gateway it does not run. */
    external: boolean
    url?: string | null
    error?: string | null
  }
  /** undefined while loading. */
  snapshot?: SettingsSnapshot
  /** skills.list counts; undefined while loading, null when it could not be read. */
  skills?: { total: number; needs: number } | null
  /** Names of variables a skill or service requires and that are unset. */
  env?: { missing: string[] } | null
  /** wallet.status; undefined while loading, null when unreadable. */
  wallet?: { initialized: boolean; walletCount: number } | null
  /** The updates store; omitted until it has checked once. */
  updates?: {
    checked: boolean
    engineOutdated: boolean
    engineLatest: string | null
    appAvailable: boolean
    appReady: boolean
    appLatest: string | null
  }
}

function fill(key: MessageKey, vars: Record<string, string | number>): string {
  return Object.entries(vars).reduce(
    (text, [name, value]) => text.replace(`{${name}}`, String(value)),
    t(key),
  )
}

/** `http://127.0.0.1:18799` → `127.0.0.1:18799`. */
export function hostOf(url: string | null | undefined): string {
  return String(url ?? '')
    .replace(/^[a-z]+:\/\//i, '')
    .replace(/\/+$/, '')
}

function section(section: SettingsSection, focus?: string): SetupJump {
  return focus ? { kind: 'section', section, focus } : { kind: 'section', section }
}

function actionFor(state: SetupState): string {
  if (state === 'attention') return t('settings.overview.action.fix')
  if (state === 'todo') return t('settings.overview.action.setUp')
  return t('settings.overview.action.review')
}

function item(
  id: SetupItemId,
  group: SetupGroup,
  state: SetupState,
  detail: string,
  jump: SetupJump,
  action?: string,
): SetupItem {
  return {
    id,
    group,
    state,
    title: t(`settings.overview.item.${id}`),
    detail,
    action: action ?? actionFor(state),
    jump,
  }
}

function needsAction(detail: SectionDetail | undefined): boolean {
  return Boolean(
    detail &&
    (detail.blocking ||
      detail.actionRequired ||
      detail.status === 'missing' ||
      detail.status === 'degraded'),
  )
}

function labelIn(
  list: readonly { providerId: string; label?: string }[] | undefined,
  id: string,
): string {
  return list?.find((p) => p.providerId === id)?.label || id
}

/** "Brave Search · BRAVE_SEARCH_API_KEY is not set", or "… · needs a key". */
function keyProblem(label: string, envKey: string | undefined): string {
  const key = String(envKey ?? '').trim()
  return key
    ? fill('settings.overview.detail.keyMissing', { label, key })
    : fill('settings.overview.detail.needsKey', { label })
}

// ── one function per item ───────────────────────────────────────────────────

function gatewayItem(input: SetupInput): SetupItem {
  const { gateway } = input
  const host = hostOf(gateway.url)
  if (input.connected) {
    const detail = host
      ? fill(
          gateway.external
            ? 'settings.overview.detail.gateway.connected'
            : 'settings.overview.detail.gateway.running',
          { url: host },
        )
      : t('settings.overview.detail.gateway.runningBare')
    return item('gateway', 'required', 'done', detail, section('gateway'))
  }
  if (gateway.state === 'starting' || gateway.state === 'stopping') {
    return item(
      'gateway',
      'required',
      'checking',
      t(`settings.overview.detail.gateway.${gateway.state}`),
      section('gateway'),
      t('settings.overview.action.open'),
    )
  }
  // An external gateway is not the app's to start: "Start" only re-checks it.
  const startable = !gateway.external
  const detail =
    gateway.state === 'error'
      ? gateway.external && host
        ? fill('settings.overview.detail.gateway.unreachable', { url: host })
        : t('settings.overview.detail.gateway.failed')
      : gateway.external && host
        ? fill('settings.overview.detail.gateway.unreachable', { url: host })
        : t('settings.overview.detail.gateway.stopped')
  return item(
    'gateway',
    'required',
    'attention',
    detail,
    startable ? { kind: 'start-gateway' } : section('gateway'),
    startable ? t('settings.overview.action.start') : t('settings.overview.action.open'),
  )
}

function providerItem(snapshot: SettingsSnapshot): SetupItem {
  const status = snapshot.status ?? {}
  const config: SetupConfig = snapshot.config ?? {}
  const llm = config.llm ?? {}
  const providerId = String(llm.provider ?? '').trim()
  const detail = status.sectionDetails?.llm
  const jump = section('providers')
  if (!providerId) {
    return item('provider', 'required', 'todo', t('settings.overview.detail.provider.none'), jump)
  }
  const label = labelIn(snapshot.catalog?.providers, providerId)
  if (status.llmSource === 'missing_env' || detail?.status === 'degraded') {
    const envKey = (status as { llmEnvKey?: string }).llmEnvKey || llm.api_key_env
    return item('provider', 'required', 'attention', keyProblem(label, envKey), jump)
  }
  if (status.llmConfigured === false || needsAction(detail)) {
    const state: SetupState = detail?.status === 'missing' ? 'todo' : 'attention'
    return item(
      'provider',
      'required',
      state,
      llm.model
        ? keyProblem(label, llm.api_key_env)
        : fill('settings.overview.detail.provider.noModel', { label }),
      jump,
    )
  }
  const model = String(llm.model ?? '').trim()
  return item(
    'provider',
    'required',
    'done',
    `${label} · ${model || t('settings.overview.detail.provider.defaultModel')}`,
    jump,
  )
}

function routerItem(snapshot: SettingsSnapshot): SetupItem {
  const router = snapshot.config?.agentos_router ?? {}
  const detail = snapshot.status?.sectionDetails?.router
  const jump = section('router')
  const provider = String(snapshot.config?.llm?.provider ?? '').trim()
  if (!provider || (detail && routerNeedsProvider(detail, 'router'))) {
    return item(
      'router',
      'recommended',
      'todo',
      t('settings.overview.detail.router.needsProvider'),
      jump,
    )
  }
  if (needsAction(detail)) {
    return item(
      'router',
      'recommended',
      'attention',
      String(detail?.detail || t('settings.overview.state.attention')),
      jump,
    )
  }
  const mode = routerMode(router)
  if (mode === 'disabled') {
    return item('router', 'recommended', 'todo', t('settings.overview.detail.router.off'), jump)
  }
  const head = t(
    mode === 'llm_judge'
      ? 'settings.overview.detail.router.judge'
      : mode === 'jev'
        ? 'settings.overview.detail.router.jev'
        : 'settings.overview.detail.router.pilot',
  )
  const tiers = Object.keys(router.tiers ?? {}).filter((name) =>
    (TEXT_TIERS as readonly string[]).includes(name),
  ).length
  const text =
    tiers > 0 ? `${head} · ${fill('settings.overview.detail.router.tiers', { n: tiers })}` : head
  return item('router', 'recommended', 'done', text, jump)
}

const MEMORY_REMOTE = new Set(['openai', 'openai-compatible'])

function memoryItem(snapshot: SettingsSnapshot): SetupItem {
  const status = snapshot.status ?? {}
  const embedding = snapshot.config?.memory?.embedding ?? {}
  const provider =
    String(embedding.provider || embedding.mode || status.memoryEmbeddingProvider || '').trim() ||
    'auto'
  const detail = status.sectionDetails?.memory_embedding
  const jump = section('memory')
  const remoteLabel =
    provider === 'openai'
      ? labelIn(snapshot.catalog?.memoryEmbeddingProviders, 'openai')
      : t('settings.overview.detail.memory.remoteLabel')
  if (status.memoryEmbeddingSource === 'missing_env' || needsAction(detail)) {
    return item(
      'memory',
      'recommended',
      'attention',
      MEMORY_REMOTE.has(provider)
        ? keyProblem(remoteLabel, status.memoryEmbeddingEnvKey)
        : String(detail?.detail || t('settings.overview.state.attention')),
      jump,
    )
  }
  if (provider === 'none') {
    return item('memory', 'recommended', 'todo', t('settings.overview.detail.memory.none'), jump)
  }
  const text =
    provider === 'auto'
      ? t('settings.overview.detail.memory.auto')
      : provider === 'local'
        ? t('settings.overview.detail.memory.local')
        : provider === 'ollama'
          ? t('settings.overview.detail.memory.ollama')
          : fill('settings.overview.detail.memory.remote', { label: remoteLabel })
  return item('memory', 'recommended', 'done', text, jump)
}

function searchItem(snapshot: SettingsSnapshot): SetupItem {
  const status = snapshot.status ?? {}
  const providerId = String(snapshot.config?.search_provider ?? '').trim()
  const jump = section('capabilities', 'search')
  if (!providerId) {
    return item('search', 'recommended', 'todo', t('settings.overview.detail.off'), jump)
  }
  const label = labelIn(snapshot.catalog?.searchProviders, providerId)
  if (status.searchConfigured === true && !needsAction(status.sectionDetails?.search)) {
    return item('search', 'recommended', 'done', label, jump)
  }
  return item('search', 'recommended', 'attention', keyProblem(label, status.searchEnvKey), jump)
}

function imageItem(snapshot: SettingsSnapshot): SetupItem {
  const status = snapshot.status ?? {}
  const jump = section('capabilities', 'image')
  if (status.imageGenerationEnabled === false) {
    return item('image', 'recommended', 'todo', t('settings.overview.detail.off'), jump)
  }
  const primary = String(
    status.imageGenerationPrimary || snapshot.config?.image_generation?.primary || '',
  )
  const providerId = String(status.imageGenerationProvider || primary.split('/')[0] || '')
  const label = labelIn(snapshot.catalog?.imageGenerationProviders, providerId)
  if (status.imageGenerationConfigured === true) {
    if (status.imageGenerationSource === 'llm_fallback') {
      return item(
        'image',
        'recommended',
        'done',
        fill('settings.overview.detail.image.sameKey', { label }),
        jump,
      )
    }
    const model = primary.startsWith(`${providerId}/`)
      ? primary.slice(providerId.length + 1)
      : primary
    return item('image', 'recommended', 'done', model ? `${label} · ${model}` : label, jump)
  }
  return item(
    'image',
    'recommended',
    'attention',
    keyProblem(label, status.imageGenerationEnvKey),
    jump,
  )
}

function voiceItem(snapshot: SettingsSnapshot): SetupItem {
  const status = snapshot.status ?? {}
  const jump = section('capabilities', 'voice')
  if (status.audioEnabled === false) {
    return item('voice', 'recommended', 'todo', t('settings.overview.detail.off'), jump)
  }
  const providerId = String(status.audioProvider || '')
  const label = labelIn(snapshot.catalog?.audioProviders, providerId)
  if (status.audioConfigured === true) {
    return item('voice', 'recommended', 'done', label, jump)
  }
  return item('voice', 'recommended', 'attention', keyProblem(label, status.audioEnvKey), jump)
}

function channelsItem(snapshot: SettingsSnapshot): SetupItem {
  const status = snapshot.status ?? {}
  const jump = section('channels')
  const entries = snapshot.config?.channels?.channels ?? []
  const count = Number(status.channelCount ?? entries.length) || 0
  if (needsAction(status.sectionDetails?.channels)) {
    return item(
      'channels',
      'recommended',
      'attention',
      String(status.sectionDetails?.channels?.detail || t('settings.overview.state.attention')),
      jump,
    )
  }
  if (count === 0) {
    return item(
      'channels',
      'recommended',
      'todo',
      t('settings.overview.detail.channels.none'),
      jump,
    )
  }
  const catalog = snapshot.catalog?.channels ?? []
  const names = [
    ...new Set(
      entries
        .map((e) => String(e.type ?? '').trim())
        .filter(Boolean)
        .map((type) => catalog.find((c) => c.type === type)?.label || type),
    ),
  ]
  return item(
    'channels',
    'recommended',
    'done',
    names.length ? names.join(', ') : String(count),
    jump,
  )
}

function skillsItem(skills: SetupInput['skills']): SetupItem | null {
  const jump = section('skills')
  if (skills === null) return null
  if (skills === undefined) {
    return item('skills', 'recommended', 'checking', t('settings.overview.detail.checking'), jump)
  }
  if (skills.needs > 0) {
    return item(
      'skills',
      'recommended',
      'todo',
      skills.needs === 1
        ? t('settings.overview.detail.skills.needsOne')
        : fill('settings.overview.detail.skills.needsMany', { n: skills.needs }),
      jump,
    )
  }
  return item(
    'skills',
    'recommended',
    'done',
    skills.total === 0
      ? t('settings.overview.detail.skills.none')
      : fill('settings.overview.detail.skills.ready', { n: skills.total }),
    jump,
  )
}

function envItem(env: SetupInput['env']): SetupItem | null {
  const jump = section('environment')
  if (env === null) return null
  if (env === undefined) {
    return item('env', 'recommended', 'checking', t('settings.overview.detail.checking'), jump)
  }
  const n = env.missing.length
  if (n === 0) return item('env', 'recommended', 'done', t('settings.overview.detail.env.ok'), jump)
  return item(
    'env',
    'recommended',
    'todo',
    n === 1
      ? t('settings.overview.detail.env.missingOne')
      : fill('settings.overview.detail.env.missingMany', { n }),
    jump,
  )
}

function walletItem(snapshot: SettingsSnapshot, wallet: SetupInput['wallet']): SetupItem | null {
  const trading = (snapshot.config as Record<string, unknown> | undefined)?.trading as
    { enabled?: boolean } | undefined
  // Trading switched off: a wallet is not part of this person's setup.
  if (!trading || trading.enabled === false) return null
  const jump = section('trading')
  if (wallet === null) return null
  if (wallet === undefined) {
    return item('wallet', 'recommended', 'checking', t('settings.overview.detail.checking'), jump)
  }
  const n = wallet.initialized ? wallet.walletCount : 0
  if (n === 0) {
    return item('wallet', 'recommended', 'todo', t('settings.overview.detail.wallet.none'), jump)
  }
  return item(
    'wallet',
    'recommended',
    'done',
    n === 1
      ? t('settings.overview.detail.wallet.one')
      : fill('settings.overview.detail.wallet.many', { n }),
    jump,
  )
}

function updatesItem(updates: SetupInput['updates']): SetupItem | null {
  if (!updates) return null
  const jump = section('about')
  const app = updates.appAvailable || updates.appReady
  if (updates.engineOutdated && app) {
    return item(
      'updates',
      'recommended',
      'todo',
      t('settings.overview.detail.updates.both'),
      jump,
      t('settings.overview.action.update'),
    )
  }
  if (updates.engineOutdated) {
    return item(
      'updates',
      'recommended',
      'todo',
      fill('settings.overview.detail.updates.engine', { version: updates.engineLatest ?? '' }),
      jump,
      t('settings.overview.action.update'),
    )
  }
  if (app) {
    return item(
      'updates',
      'recommended',
      'todo',
      fill(
        updates.appReady
          ? 'settings.overview.detail.updates.appReady'
          : 'settings.overview.detail.updates.app',
        { version: updates.appLatest ?? '' },
      ),
      jump,
      t('settings.overview.action.update'),
    )
  }
  // Never checked: say nothing rather than claim "up to date".
  if (!updates.checked) return null
  return item('updates', 'recommended', 'done', t('settings.overview.detail.updates.current'), jump)
}

/**
 * The checklist. Offline, only the gateway row is known: everything else
 * lives in the gateway, and a stale snapshot would be a guess.
 */
export function setupItems(input: SetupInput): SetupItem[] {
  const gateway = gatewayItem(input)
  if (!input.connected) return [gateway]
  const snapshot = input.snapshot
  if (!snapshot) return [gateway]
  const items: (SetupItem | null)[] = [
    gateway,
    providerItem(snapshot),
    routerItem(snapshot),
    memoryItem(snapshot),
    searchItem(snapshot),
    imageItem(snapshot),
    voiceItem(snapshot),
    channelsItem(snapshot),
    skillsItem(input.skills),
    envItem(input.env),
    walletItem(snapshot, input.wallet),
    updatesItem(input.updates),
  ]
  return items.filter((i): i is SetupItem => i !== null)
}

// ── the summary the head and hero read ─────────────────────────────────────

/**
 * offline: the gateway is not reachable. blocked: a required item is not
 * done. attention: something set up is broken. optional: only skippable
 * items remain. complete: nothing left.
 */
export type SetupPhase = 'offline' | 'blocked' | 'attention' | 'optional' | 'complete'

export interface SetupSummary {
  phase: SetupPhase
  /** Items that count (not still checking). */
  total: number
  done: number
  /** Required items not done plus broken ones: what the head pill counts. */
  toFinish: number
  /** The one step the hero offers, or null when nothing is left. */
  next: SetupItem | null
}

/** Which skippable item the hero suggests first: the most useful to most people. */
export const SUGGEST_ORDER: readonly SetupItemId[] = [
  'search',
  'memory',
  'router',
  'channels',
  'skills',
  'env',
  'updates',
  'image',
  'voice',
  'wallet',
]

export function summarizeSetup(items: readonly SetupItem[]): SetupSummary {
  const counted = items.filter((i) => i.state !== 'checking')
  const done = counted.filter((i) => i.state === 'done').length
  const gateway = items.find((i) => i.id === 'gateway')
  const requiredOpen = counted.filter((i) => i.group === 'required' && i.state !== 'done')
  const broken = counted.filter((i) => i.group === 'recommended' && i.state === 'attention')
  const toFinish = requiredOpen.length + broken.length
  const base = { total: counted.length, done, toFinish }
  if (gateway && gateway.state !== 'done') {
    return { ...base, phase: 'offline', next: gateway }
  }
  if (requiredOpen.length > 0) return { ...base, phase: 'blocked', next: requiredOpen[0] ?? null }
  if (broken.length > 0) return { ...base, phase: 'attention', next: broken[0] ?? null }
  const todo = counted.filter((i) => i.state === 'todo')
  if (todo.length === 0) return { ...base, phase: 'complete', next: null }
  const rank = (i: SetupItem) => {
    const at = SUGGEST_ORDER.indexOf(i.id)
    return at < 0 ? SUGGEST_ORDER.length : at
  }
  const next = [...todo].sort((a, b) => rank(a) - rank(b))[0] ?? null
  return { ...base, phase: 'optional', next }
}
