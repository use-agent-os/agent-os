// Chat transcript — router-fx animation engine (imperative).
//
// This module is part of the OWNER-APPROVED imperative boundary of the
// chat-view migration (design §2.1): the router-fx subsystem is a self-contained
// imperative animation engine (DOM + timers + rAF), ported near-verbatim from
// static/js/views/chat.js (the router-fx range, chat.js:3263-4680) — it is NOT
// reactified. Each function carries the cited legacy line range it was ported
// from. It composes into `createStreamController` exactly how `createToolRenderer`
// / `createArtifactRenderer` do.
//
// Split into two surfaces (mirroring tools.ts / artifacts.ts):
//   1. Pure helpers (top-level exports) — no DOM, no timers, no module globals.
//      The provider/label/request-kind/seed-key/identity/tier helpers plus the
//      tier REGISTRY (`createRouterFxRegistry`) and the registry-driven roster
//      builders (`routerFxVisualEntries` / `routerFxHasMultipleCandidates`),
//      made pure by taking the registry explicitly. These are the sanctioned
//      unit-test surface for this task (routerFx.test.ts).
//   2. `createRouterFxRenderer(deps)` — a factory the streaming controller
//      composes. The DOM/animation methods (mount/build/position/ping/scan/
//      settle/lock/normalize) need view state (the dock element, the thread,
//      the session key, per-turn scan bookkeeping, the pending-decision cache)
//      injected as `deps` or held as instance fields — the legacy module-globals
//      rebind to the SAME registry the config loader feeds. DOM/animation
//      behavior is verified by a live-browser sweep (parity matrix), not RTL.
//
// Storage keys ported EXACTLY: the visualisation pref `agentos-router-fx`
// (chat.js:3392) and the seed-cache prefix `osq.routerFx.seed:` (chat.js:3591).

/* ── Constants (ported verbatim from chat.js) ───────────────────────────── */

// chat.js:3375 — default tier ids until config replaces them.
export const ROUTER_FX_DEFAULT_TIERS = ['c0', 'c1', 'c2', 'c3'] as const
// chat.js:3392 — per-browser visualisation preference key.
export const ROUTER_FX_PREF_KEY = 'agentos-router-fx'
// chat.js:3395-3396 — fixed scan window + start-grace delay.
export const ROUTER_FX_SCAN_MS = 600
export const ROUTER_FX_START_DELAY_MS = 280
// chat.js:3589-3590 — localStorage seed-cache soft cap + trim target.
export const ROUTER_FX_SEED_CACHE_MAX = 300
export const ROUTER_FX_SEED_CACHE_TRIM = 250

// chat.js:3591 — seed cache key prefix.
export function routerFxSeedCachePrefix(): string {
  return 'osq.routerFx.seed:'
}

/* ── Shapes ─────────────────────────────────────────────────────────────── */

/** A single router tier's config (chat.js `_routerFxTierConfigs` entry). */
export interface RouterFxTierConfig {
  model: string
  supportsImage: boolean
  imageOnly: boolean
}

/** The routing decision payload (chat.js router_decision / usage-derived). */
export interface RouterFxDecision {
  tier?: string
  routed_tier?: string
  model?: string
  routed_model?: string
  source?: string
  routing_source?: string
  routing_applied?: boolean
  rollout_phase?: string
  [k: string]: unknown
}

/** A deduped roster entry for one visual grid cell (chat.js:3543-3548). */
export interface RouterFxVisualEntry {
  key: string
  tiers: string[]
  model: string
  displayName: string
}

export type RouterFxRequestKind = 'image' | 'text'

/* ── Pure helpers (unit-tested) ─────────────────────────────────────────── */

// chat.js:3444-3449 — Normalize user-facing model labels without changing
// stored/provider ids. "z-ai/glm-5.1" -> "glm-5.1"; "glm-5.1-20260406" -> "glm-5.1".
export function modelDisplayName(name: string): string {
  if (!name || typeof name !== 'string') return name as string
  const idx = name.lastIndexOf('/')
  const stripped = idx >= 0 ? name.slice(idx + 1) : name
  return stripped.replace(/-\d{8}$/, '')
}

// chat.js:3451-3453
export function routerFxStripProvider(name: string): string {
  return modelDisplayName(name)
}

// chat.js:3455-3462
export function routerFxRequestKindFromAttachments(
  attachments: Array<{ mime?: string; type?: string }>,
): RouterFxRequestKind {
  const list = Array.isArray(attachments) ? attachments : []
  for (const item of list) {
    const mime = String(item?.mime || item?.type || '').toLowerCase()
    if (mime.indexOf('image/') === 0) return 'image'
  }
  return 'text'
}

// chat.js:3464-3466
export function routerFxNormalizeRequestKind(
  requestKind: string | undefined | null,
): RouterFxRequestKind {
  return requestKind === 'image' ? 'image' : 'text'
}

// chat.js:3419-3428
export function routerFxSortTiers(list: string[]): string[] {
  return list.slice().sort((a, b) => {
    const am = /^c(\d+)$/.exec(a)
    const bm = /^c(\d+)$/.exec(b)
    if (am && bm) return parseInt(am[1]!, 10) - parseInt(bm[1]!, 10)
    if (am) return -1
    if (bm) return 1
    return a.localeCompare(b)
  })
}

// chat.js:3430-3433
export function routerFxNormalizeTier(tier: string | undefined | null): string {
  if (typeof tier !== 'string' || !tier) return ''
  return tier.toLowerCase().replace(/^t([0-3])$/, 'c$1')
}

// chat.js:3494-3498
export function routerFxTierMatchesRequestKind(
  tierConfig: RouterFxTierConfig,
  requestKind: string | undefined | null,
): boolean {
  const kind = routerFxNormalizeRequestKind(requestKind)
  if (kind === 'image') return !!(tierConfig.supportsImage || tierConfig.imageOnly)
  return !tierConfig.imageOnly
}

// chat.js:3500-3506
export function routerFxRequestKindFromDecision(
  decision: RouterFxDecision | null | undefined,
  fallbackKind: string | undefined | null,
): RouterFxRequestKind {
  if (fallbackKind) return routerFxNormalizeRequestKind(fallbackKind)
  const source = String(decision?.source || decision?.routing_source || '').toLowerCase()
  const tier = String(decision?.tier || decision?.routed_tier || '').toLowerCase()
  if (source === 'image_route' || tier === 'image_model') return 'image'
  return 'text'
}

// chat.js:3582-3584 — seed cache key: prefix + sessionKey + 1-indexed turn + tier.
export function routerFxSeedCacheKey(sessionKey: string, turnIndex: number, tier: string): string {
  return routerFxSeedCachePrefix() + (sessionKey || '') + ':' + (turnIndex | 0) + ':' + tier
}

// chat.js:3631-3636
export function routerFxIdentity(model: string, tier: string): string {
  const modelPart = typeof model === 'string' ? model.trim().toLowerCase() : ''
  const tierPart = routerFxNormalizeTier(tier)
  if (!modelPart && !tierPart) return ''
  return modelPart + '|' + tierPart
}

// chat.js:3637-3640
export function routerFxDecisionIdentity(decision: RouterFxDecision | null | undefined): string {
  if (!decision || typeof decision !== 'object') return ''
  return routerFxIdentity(
    String(decision.model || decision.routed_model || ''),
    String(decision.tier || decision.routed_tier || ''),
  )
}

// chat.js:3641-3644
export function routerFxUsageIdentity(
  usage: { routed_model?: string; model?: string; routed_tier?: string } | null | undefined,
): string {
  if (!usage || typeof usage !== 'object') return ''
  return routerFxIdentity(
    String(usage.routed_model || usage.model || ''),
    String(usage.routed_tier || ''),
  )
}

/* ── Tier registry (chat.js module-globals _routerFxSlotList / _routerFxModels
 *   / _routerFxTierConfigs / _routerFxConfigTiers, chat.js:3376-3384) ─────── */

/**
 * The router tier registry: the mutable tier bookkeeping the legacy view held
 * as IIFE module-globals and fed from `_loadFeatureToggles` (chat.js:1502-1531).
 * Extracted here as a small stateful object so the roster builders below stay
 * pure over it (unit-testable), while the chat config loader populates it via
 * `setTierConfig`/`rememberTierDecision`/`setConfigTiers`.
 */
export interface RouterFxRegistry {
  // chat.js:3376 — ordered slot list; chat.js:3377 tier→model; chat.js:3378
  // tier→config; chat.js:3383 authoritative config-tier set (null = unknown).
  slotList: string[]
  models: Record<string, string>
  tierConfigs: Record<string, RouterFxTierConfig>
  configTiers: Set<string> | null
  /** chat.js:3468-3477 — resolve a tier's config (falls back to a bare model). */
  tierConfig(tier: string): RouterFxTierConfig
  /** chat.js:3435-3440 — register a normalized tier into the sorted slot list. */
  registerTier(tier: string): void
  /** chat.js:3479-3492 — remember a tier→model decision, warming the cache. */
  rememberTierDecision(tier: string, model: string): void
  /** chat.js:1513-1519 — set a tier's full config (config-load path). */
  setTierConfig(tier: string, config: RouterFxTierConfig): void
  /** chat.js:1528 — set the authoritative config-tier set. */
  setConfigTiers(tiers: Set<string> | null): void
  /** chat.js:1530 — replace the slot list from config (sorted). */
  setSlotList(tiers: string[]): void
}

export function createRouterFxRegistry(): RouterFxRegistry {
  const reg: RouterFxRegistry = {
    slotList: ROUTER_FX_DEFAULT_TIERS.slice(),
    models: {},
    tierConfigs: {},
    configTiers: null,
    tierConfig(tier: string): RouterFxTierConfig {
      // chat.js:3468-3477
      const norm = typeof tier === 'string' ? tier.toLowerCase() : ''
      const known = norm ? reg.tierConfigs[norm] : null
      if (known) return known
      return {
        model: norm && reg.models[norm] ? reg.models[norm] : '',
        supportsImage: false,
        imageOnly: false,
      }
    },
    registerTier(tier: string): void {
      // chat.js:3435-3440
      const norm = routerFxNormalizeTier(tier)
      if (!norm) return
      if (reg.slotList.indexOf(norm) >= 0) return
      reg.slotList = routerFxSortTiers(reg.slotList.concat([norm]))
    },
    rememberTierDecision(tier: string, model: string): void {
      // chat.js:3479-3492
      if (typeof tier !== 'string' || !tier) return
      const norm = tier.toLowerCase()
      reg.registerTier(norm)
      if (!model) return
      const modelName = String(model)
      reg.models[norm] = modelName
      const current = reg.tierConfigs[norm] || ({} as Partial<RouterFxTierConfig>)
      reg.tierConfigs[norm] = {
        model: modelName,
        supportsImage: current.supportsImage === true,
        imageOnly: current.imageOnly === true,
      }
    },
    setTierConfig(tier: string, config: RouterFxTierConfig): void {
      const norm = routerFxNormalizeTier(tier)
      if (!norm) return
      reg.tierConfigs[norm] = config
      if (config.model) reg.models[norm] = config.model
      reg.registerTier(norm)
    },
    setConfigTiers(tiers: Set<string> | null): void {
      reg.configTiers = tiers
    },
    setSlotList(tiers: string[]): void {
      reg.slotList = routerFxSortTiers(tiers)
    },
  }
  return reg
}

/* ── Roster builders (pure over the registry, chat.js:3508-3552) ─────────── */

// chat.js:3508-3549
export function routerFxVisualEntries(
  reg: RouterFxRegistry,
  requestKind: string | undefined | null,
  decision: RouterFxDecision | null | undefined,
): RouterFxVisualEntry[] {
  if (reg.configTiers === null) return []
  const kind = routerFxRequestKindFromDecision(decision, requestKind)
  const byDisplay = new Map<string, RouterFxVisualEntry>()
  reg.slotList.forEach((tier) => {
    if (reg.configTiers !== null && !reg.configTiers.has(tier)) return
    const tierConfig = reg.tierConfig(tier)
    if (!routerFxTierMatchesRequestKind(tierConfig, kind)) return
    const displayName = tierConfig.model ? routerFxStripProvider(tierConfig.model) : tier
    const key = displayName ? displayName.toLowerCase() : tier
    let entry = byDisplay.get(key)
    if (!entry) {
      entry = { key, tiers: [], model: tierConfig.model || '', displayName }
      byDisplay.set(key, entry)
    }
    entry.tiers.push(tier)
    if (!entry.model && tierConfig.model) entry.model = tierConfig.model
  })
  const decisionTier =
    decision && typeof decision.tier === 'string' ? decision.tier.toLowerCase() : ''
  const decisionModel = decision && typeof decision.model === 'string' ? decision.model : ''
  if (decisionTier && decisionModel) {
    const displayName = routerFxStripProvider(decisionModel)
    const key = displayName ? displayName.toLowerCase() : decisionTier
    let entry = byDisplay.get(key)
    if (!entry && routerFxTierMatchesRequestKind(reg.tierConfig(decisionTier), kind)) {
      entry = { key, tiers: [], model: decisionModel, displayName }
      byDisplay.set(key, entry)
    }
    if (entry) {
      if (entry.tiers.indexOf(decisionTier) < 0) entry.tiers.push(decisionTier)
      if (!entry.model) entry.model = decisionModel
    }
  }
  return Array.from(byDisplay.values()).map((e) => ({
    key: e.key,
    tiers: routerFxSortTiers(e.tiers),
    model: e.model,
    displayName: e.displayName || (e.model ? routerFxStripProvider(e.model) : e.tiers[0]) || '',
  }))
}

// chat.js:3551-3553
export function routerFxHasMultipleCandidates(
  reg: RouterFxRegistry,
  requestKind: string | undefined | null,
  decision: RouterFxDecision | null | undefined,
): boolean {
  return routerFxVisualEntries(reg, requestKind, decision).length > 1
}

/* ── Preference load/save (chat.js:3398-3417) ───────────────────────────── */

/** The router-fx visualisation pref (chat.js `_routerFx`). */
export interface RouterFxPref {
  enabled: boolean
  variant: string
}

// chat.js:3398-3410 — defaults stand unless a stored pref overrides.
export function routerFxLoadPref(pref: RouterFxPref): void {
  pref.variant = 'default'
  try {
    const raw = localStorage.getItem(ROUTER_FX_PREF_KEY)
    if (!raw) return
    const saved = JSON.parse(raw)
    if (saved && typeof saved === 'object') {
      if (typeof saved.enabled === 'boolean') pref.enabled = saved.enabled
    }
  } catch {
    /* keep defaults */
  }
}

// chat.js:3411-3417
export function routerFxSavePref(pref: RouterFxPref): void {
  try {
    localStorage.setItem(ROUTER_FX_PREF_KEY, JSON.stringify({ enabled: pref.enabled }))
  } catch {
    /* preference is best-effort */
  }
}

/* ── Seed cache (localStorage) — chat.js:3592-3630 ──────────────────────── */

// chat.js:3592-3613 — trim the localStorage seed cache to the soft cap.
export function routerFxSeedCacheTrim(): void {
  try {
    const prefix = routerFxSeedCachePrefix()
    const entries: Array<{ key: string; stamp: number }> = []
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i)
      if (k && k.indexOf(prefix) === 0) {
        const v = localStorage.getItem(k) || ''
        // Stored value starts with the millisecond timestamp; older → smaller.
        const stamp = parseInt(v.split(':', 1)[0]!, 10) || 0
        entries.push({ key: k, stamp })
      }
    }
    if (entries.length <= ROUTER_FX_SEED_CACHE_MAX) return
    entries.sort((a, b) => a.stamp - b.stamp)
    const dropCount = entries.length - ROUTER_FX_SEED_CACHE_TRIM
    for (let i = 0; i < dropCount; i++) {
      try {
        localStorage.removeItem(entries[i]!.key)
      } catch {
        /* ignore */
      }
    }
  } catch {
    /* localStorage unavailable; nothing to trim */
  }
}

// chat.js:3614-3627 — resolve (or generate + cache) the per-turn seed.
export function routerFxResolveSeed(
  sessionKey: string,
  turnIndex: number,
  tier: string,
  hintTimestamp?: number | string,
): string {
  const key = routerFxSeedCacheKey(sessionKey, turnIndex, tier)
  try {
    const cached = localStorage.getItem(key)
    if (cached) return cached
  } catch {
    /* localStorage may be unavailable */
  }
  const stamp = hintTimestamp ? String(hintTimestamp) : String(Date.now())
  const fresh = stamp + ':' + tier + ':i' + (turnIndex | 0)
  try {
    localStorage.setItem(key, fresh)
    routerFxSeedCacheTrim()
  } catch {
    /* ignore */
  }
  return fresh
}

// chat.js:3628-3630
export function routerFxResolveLayoutSeed(
  sessionKey: string,
  hintTimestamp?: number | string,
): string {
  return routerFxResolveSeed(sessionKey, 0, 'layout', hintTimestamp)
}

/* ── Element augmentation (the legacy `wrap._fx*` expandos) ──────────────── */

interface RouterFxStripElement extends HTMLElement {
  _fxGridCells?: RouterFxGridCell[]
  _fxRealEntries?: RouterFxVisualEntry[]
  _fxRequestKind?: RouterFxRequestKind
  _fxDecision?: RouterFxDecision | null
  _fxFinished?: boolean
  _fxAnimFrame?: number | null
  _fxAnimTimers?: Array<ReturnType<typeof setTimeout>>
  _fxScanTimer?: ReturnType<typeof setTimeout> | null
  _fxScanCap?: ReturnType<typeof setTimeout> | null
  _fxFitFrame?: number | null
  _fxFitInstalled?: boolean
  _fxLabelResizeObserver?: ResizeObserver | null
}

interface RouterFxGridCell {
  kind: 'real'
  entry: RouterFxVisualEntry
  displayName: string
}

/** A parked scan awaiting the config gate / decision (chat.js `_routerFxScanPending`). */
interface RouterFxScanPending {
  anchorDiv: HTMLElement | null
  seedKey: string
  requestKind: RouterFxRequestKind
  sessionKey: string
  turnIndex: string
  decision: RouterFxDecision | null
}

/* ── Renderer factory ───────────────────────────────────────────────────── */

export interface RouterFxRendererDeps {
  /** chat.js `_thread` — the transcript scroll container (source of user-msg anchors). */
  thread: () => HTMLElement | null
  /**
   * chat.js `_routerFxDock` — the composer dock element that hosts strips. All
   * strips live here (chat.js:3897-3902); returns null until the dock exists (no
   * dock in the frontend yet → strips are suppressed, matching `if (!_routerFxDock)`).
   */
  dock: () => HTMLElement | null
  /** chat.js `_sessionKey` — the active session key, read live. */
  getSessionKey: () => string
  /** The shared tier registry (fed by the config loader; chat.js module-globals). */
  registry: RouterFxRegistry
  /** The visualisation pref (chat.js `_routerFx`), read live. */
  pref: RouterFxPref
  /** chat.js `_routerFeatureEnabled` — operator routing on/off, read live. */
  routerFeatureEnabled: () => boolean
  /** chat.js:661 — HTML-escape. */
  esc: (s: string) => string
  /** chat.js `_scrollToBottom`. */
  scrollToBottom: () => void
  /** chat.js:3263 — compaction-turn suppression predicate. Default: false. */
  isSuppressedForCompactionTurn?: (turnIndex: string) => boolean
  /** chat.js:8660 — compaction-in-flight for the current session. Default: false. */
  isCompactInFlightForCurrentSession?: () => boolean
  /** chat.js `_historyHasRendered` — has the history render completed. Default: true. */
  historyHasRendered?: () => boolean
  /** chat.js `_historyHydrating` — is the history render in progress. Default: false. */
  historyHydrating?: () => boolean
  /**
   * chat.js:3569-3575 — await the config-ready gate (with its own ceiling).
   * Default: resolve immediately (config already known / no gate).
   */
  awaitConfig?: () => Promise<void>
  /** chat.js `_chatDiag` — diagnostics ring. Default: no-op. */
  diag?: (event: string, detail: Record<string, unknown>) => void
  /** chat.js `_chatDiagSummarizePayload`. Default: identity-ish. */
  summarizePayload?: (payload: unknown) => Record<string, unknown>
  /** chat.js `_chatDiagDescribeElement`. Default: tag/class summary. */
  describeElement?: (el: Element | null) => Record<string, unknown>
}

/**
 * Create the router-fx renderer bound to view state. The streaming controller
 * composes this and re-exports `handleRouterDecision` / `settleForOutput` /
 * `staticizeCompletedStrips` / … so `useTranscript` can route the
 * `session.event.router_decision` event and the stream lifecycle can drive it.
 */
export function createRouterFxRenderer(deps: RouterFxRendererDeps) {
  const {
    thread,
    dock,
    getSessionKey,
    registry: reg,
    pref,
    routerFeatureEnabled,
    esc,
    scrollToBottom,
  } = deps
  const isSuppressedForCompactionTurn = deps.isSuppressedForCompactionTurn ?? (() => false)
  const isCompactInFlightForCurrentSession =
    deps.isCompactInFlightForCurrentSession ?? (() => false)
  const historyHasRendered = deps.historyHasRendered ?? (() => true)
  const historyHydrating = deps.historyHydrating ?? (() => false)
  const awaitConfig = deps.awaitConfig ?? (() => Promise.resolve())
  const diag = deps.diag ?? (() => {})
  const summarizePayload = deps.summarizePayload ?? ((p: unknown) => ({ payload: p }))
  const describeElement = deps.describeElement ?? (() => ({}))

  const sessionKey = (): string => getSessionKey() || ''

  /* ── instance fields (legacy module-globals) ──────────────────────────── */
  // chat.js:61-63 — pending-anchor cache + delayed-scan bookkeeping.
  const _pendingRouterDecisions = new Map<string, RouterFxDecision>()
  let _routerFxScanDelayTimer: ReturnType<typeof setTimeout> | null = null
  let _routerFxScanPending: RouterFxScanPending | null = null
  // chat.js:3269-3282 — compaction suppression sticky state.
  let _compactSuppressedRouterSessionKey = ''
  let _compactSuppressedRouterTurnIndex = ''

  /* ── request-kind + roster (chat.js:3691-3706) ────────────────────────── */

  // chat.js:3691-3693
  function realEntries(
    decision: RouterFxDecision | null | undefined,
    requestKind: string | undefined | null,
  ): RouterFxVisualEntry[] {
    return routerFxVisualEntries(reg, requestKind, decision)
  }

  // chat.js:3697-3706
  function buildGridCells(entries: RouterFxVisualEntry[]): RouterFxGridCell[] {
    const orderedRealEntries = entries
      .slice()
      .sort((a, b) => (a.displayName || a.key || '').localeCompare(b.displayName || b.key || ''))
    return orderedRealEntries.map((entry) => ({
      kind: 'real' as const,
      entry,
      displayName: entry.displayName,
    }))
  }

  // chat.js:4039-4045 — winner label for settled semantics / assistive text.
  function winnerName(decision: RouterFxDecision | null | undefined): string {
    const model = decision && (decision.model || decision.routed_model)
    if (model) return routerFxStripProvider(String(model))
    const tier = routerFxNormalizeTier((decision && decision.tier) || '')
    if (tier && reg.models[tier]) return routerFxStripProvider(reg.models[tier])
    return tier || ''
  }

  /* ── build (chat.js:3708-3783) ────────────────────────────────────────── */

  function buildRouterFxElement(
    decision: RouterFxDecision,
    opts: {
      renderMode?: string
      preSettled?: boolean
      variant?: string
      seedKey?: string
      requestKind?: string
    } = {},
  ): RouterFxStripElement | null {
    const wrap = document.createElement('div') as RouterFxStripElement
    wrap.className = 'router-fx'
    wrap.setAttribute('data-history-role', 'router')
    wrap.dataset.renderMode = opts.renderMode || (opts.preSettled ? 'history' : 'live')
    wrap.dataset.state = 'idle'
    wrap.dataset.tier = routerFxNormalizeTier(decision.tier || '')
    wrap.dataset.source = decision.source || 'none'
    const identity = routerFxDecisionIdentity(decision)
    if (identity) wrap.dataset.routerIdentity = identity
    const observeMode = decision && decision.routing_applied === false
    if (observeMode) {
      wrap.dataset.observe = 'true'
      wrap.dataset.rolloutPhase =
        typeof decision.rollout_phase === 'string' ? decision.rollout_phase : 'observe'
    }
    // Style-variant seam (chat.js:3726-3732): stamp only non-default values.
    const variant = (opts.variant != null ? opts.variant : pref.variant) || 'default'
    if (variant && variant !== 'default') wrap.dataset.variant = variant

    const header = document.createElement('div')
    header.className = 'router-fx-header'
    header.innerHTML =
      '<span class="glyph">←</span>' +
      '<span class="title">AI model router</span>' +
      '<span class="glyph">→</span>'
    wrap.appendChild(header)

    const seedKey = opts && opts.seedKey ? String(opts.seedKey) : ''
    if (seedKey) wrap.dataset.seed = seedKey

    const requestKind = routerFxRequestKindFromDecision(decision, opts.requestKind)
    const entries = realEntries(decision, requestKind)
    if (entries.length <= 1) return null
    const gridCells = buildGridCells(entries)

    const grid = document.createElement('div')
    grid.className = 'router-fx-grid'
    const cols = Math.min(4, Math.max(2, gridCells.length))
    const mobileCols = gridCells.length > 2 ? 2 : gridCells.length
    grid.style.setProperty('--router-fx-cols', String(cols))
    grid.style.setProperty('--router-fx-mobile-cols', String(Math.max(1, mobileCols)))
    gridCells.forEach((cellInfo, i) => {
      const cell = document.createElement('div')
      cell.className = 'router-fx-cell'
      cell.dataset.cellIdx = String(i)
      cell.innerHTML = `<span class="nm" title="${esc(cellInfo.displayName)}">${esc(cellInfo.displayName)}</span>`
      grid.appendChild(cell)
    })
    const selector = document.createElement('div')
    selector.className = 'router-fx-selector'
    grid.appendChild(selector)
    wrap.appendChild(grid)

    wrap._fxGridCells = gridCells
    wrap._fxRealEntries = entries
    wrap._fxRequestKind = requestKind

    if (opts.preSettled) {
      const winnerIdx = winnerCellIndex(wrap, routerFxNormalizeTier(decision.tier || ''))
      if (winnerIdx >= 0) {
        settleRouterFxImmediate(wrap, winnerIdx, { burst: false, decision })
        normalizeSettledStrip(wrap, opts.renderMode || 'history', decision)
      }
    }
    return wrap
  }

  // chat.js:3785-3793
  function winnerCellIndex(wrap: RouterFxStripElement, tier: string): number {
    if (!wrap || !tier) return -1
    const cells = wrap._fxGridCells || []
    const norm = String(tier).toLowerCase()
    for (let i = 0; i < cells.length; i++) {
      const c = cells[i]!
      if (c.kind === 'real' && c.entry.tiers.indexOf(norm) >= 0) return i
    }
    return -1
  }

  /* ── selector position + ping (chat.js:3797-3822) ─────────────────────── */

  function positionSelector(
    selector: HTMLElement,
    cell: HTMLElement,
    opts: { lock?: boolean; hopIdx?: number } = {},
  ): void {
    if (!selector || !cell) return
    const grid = cell.parentElement
    if (!grid || !grid.isConnected) return
    const cellRect = cell.getBoundingClientRect()
    const gridRect = grid.getBoundingClientRect()
    if (!cellRect.width || !cellRect.height || !gridRect.width || !gridRect.height) return
    const padLeft = parseFloat(getComputedStyle(grid).paddingLeft) || 0
    const padTop = parseFloat(getComputedStyle(grid).paddingTop) || 0
    const x = cellRect.left - gridRect.left - padLeft
    const y = cellRect.top - gridRect.top - padTop
    selector.style.width = cellRect.width + 'px'
    selector.style.height = cellRect.height + 'px'
    const rot = opts.lock ? 0 : (opts.hopIdx || 0) % 2 ? -1.4 : 1.4
    selector.style.transform = `translate(${x}px, ${y}px) rotate(${rot}deg)`
  }

  function ping(cell: HTMLElement): void {
    if (!cell) return
    cell.classList.remove('pinging')
    void cell.offsetWidth
    cell.classList.add('pinging')
    setTimeout(() => cell.classList.remove('pinging'), 220)
  }

  /* ── timers + residue + settled semantics (chat.js:3824-3895) ─────────── */

  function clearAnimationTimers(wrap: RouterFxStripElement): void {
    if (!wrap) return
    if (wrap._fxAnimFrame) {
      cancelAnimationFrame(wrap._fxAnimFrame)
      wrap._fxAnimFrame = null
    }
    if (Array.isArray(wrap._fxAnimTimers)) {
      wrap._fxAnimTimers.forEach((timer) => clearTimeout(timer))
    }
    wrap._fxAnimTimers = []
  }

  function applySettledSemantics(
    wrap: RouterFxStripElement,
    decision: RouterFxDecision | null | undefined,
    renderMode?: string,
  ): void {
    if (!wrap) return
    const mode = renderMode || wrap.dataset.renderMode || 'history'
    const effectiveDecision =
      decision ||
      ({
        tier: wrap.dataset.tier || '',
        model: '',
        source: wrap.dataset.source || 'none',
      } as RouterFxDecision)
    wrap.dataset.renderMode = mode
    const name = winnerName(effectiveDecision)
    wrap.setAttribute('role', mode === 'live' ? 'status' : 'group')
    wrap.setAttribute('aria-live', mode === 'live' ? 'polite' : 'off')
    wrap.setAttribute('aria-label', name ? `Router selected ${name}` : 'Router settled')
  }

  function clearVisualResidue(wrap: RouterFxStripElement): void {
    if (!wrap) return
    const selector = wrap.querySelector('.router-fx-selector')
    if (selector) selector.classList.remove('visible', 'lock', 'lock-impact')
    wrap.querySelectorAll('.router-fx-cell.pinging').forEach((cell) => {
      cell.classList.remove('pinging')
    })
    wrap.querySelectorAll('.router-fx-burst').forEach((burst) => burst.remove())
  }

  function normalizeSettledStrip(
    wrap: RouterFxStripElement,
    renderMode?: string,
    decision?: RouterFxDecision | null,
  ): void {
    if (!wrap) return
    stopScan(wrap)
    clearAnimationTimers(wrap)
    clearVisualResidue(wrap)
    wrap.dataset.state = 'settled'
    wrap.dataset.renderMode = renderMode || 'history'
    delete wrap.dataset.live
    delete wrap.dataset.scanning
    wrap._fxFinished = true
    applySettledSemantics(wrap, decision, wrap.dataset.renderMode)
    fitLabels(wrap)
  }

  function disconnectLabelFit(wrap: RouterFxStripElement): void {
    if (!wrap) return
    if (wrap._fxFitFrame) {
      cancelAnimationFrame(wrap._fxFitFrame)
      wrap._fxFitFrame = null
    }
    if (wrap._fxLabelResizeObserver) {
      wrap._fxLabelResizeObserver.disconnect()
      wrap._fxLabelResizeObserver = null
    }
  }

  function removeStrip(wrap: RouterFxStripElement): void {
    if (!wrap) return
    normalizeSettledStrip(wrap, wrap.dataset.renderMode || 'history')
    disconnectLabelFit(wrap)
    wrap.remove()
  }

  /* ── dock mount (chat.js:3900-3927) ───────────────────────────────────── */

  function strips(selector = '.router-fx'): RouterFxStripElement[] {
    const d = dock()
    return d ? Array.from(d.querySelectorAll<RouterFxStripElement>(selector)) : []
  }

  function mountStrip(wrap: RouterFxStripElement): boolean {
    const d = dock()
    if (!d || !wrap) return false
    const wrapIsLive = wrap.dataset.live === 'true' || wrap.dataset.scanning === 'true'
    const existing = strips()
    const liveStrip = existing.find(
      (el) =>
        el !== wrap &&
        (el.dataset.live === 'true' || el.dataset.scanning === 'true') &&
        el.dataset.sessionKey === sessionKey(),
    )
    if (liveStrip && !wrapIsLive) return false
    existing.forEach((el) => {
      if (el !== wrap) removeStrip(el)
    })
    if (wrap.parentNode !== d) d.appendChild(wrap)
    return true
  }

  function staticizeCompletedStrips(key?: string): void {
    const k = key || sessionKey()
    strips().forEach((wrap) => {
      if (k && wrap.dataset.sessionKey && wrap.dataset.sessionKey !== k) return
      if (wrap.dataset.state !== 'settled') return
      normalizeSettledStrip(wrap, 'history', wrap._fxDecision || null)
    })
  }

  /* ── settle + burst (chat.js:3929-3968) ───────────────────────────────── */

  function settleRouterFxImmediate(
    wrap: RouterFxStripElement,
    winnerIdx: number,
    opts: { burst?: boolean; decision?: RouterFxDecision | null } = {},
  ): void {
    const grid = wrap.querySelector('.router-fx-grid')
    const selector = wrap.querySelector('.router-fx-selector')
    if (!grid || !selector) return
    const cells = grid.querySelectorAll('.router-fx-cell')
    if (!cells[winnerIdx]) return

    wrap.dataset.state = 'settled'
    delete wrap.dataset.live
    delete wrap.dataset.scanning
    wrap._fxFinished = true
    cells.forEach((c, i) => c.classList.toggle('win', i === winnerIdx))
    applySettledSemantics(wrap, opts.decision || wrap._fxDecision || null, wrap.dataset.renderMode)

    if (selector) selector.classList.remove('visible', 'lock', 'lock-impact')
    fitLabels(wrap)
    if (opts.burst) {
      requestAnimationFrame(() => fireBurst(grid as HTMLElement, cells[winnerIdx] as HTMLElement))
    }
  }

  function fireBurst(grid: HTMLElement, cell: HTMLElement): void {
    if (!grid || !cell) return
    const cellRect = cell.getBoundingClientRect()
    const gridRect = grid.getBoundingClientRect()
    const cx = cellRect.left - gridRect.left + cellRect.width / 2
    const cy = cellRect.top - gridRect.top + cellRect.height / 2
    const burst = document.createElement('div')
    burst.className = 'router-fx-burst'
    burst.style.left = cx + 'px'
    burst.style.top = cy + 'px'
    burst.innerHTML = '<i></i><i></i><i></i><i></i><i></i><i></i>'
    grid.appendChild(burst)
    setTimeout(() => burst.remove(), 700)
  }

  /* ── one-shot animate (chat.js:3970-4036) — retained for parity, unused by
   *   the delayed-scan path but part of the engine. ──────────────────────── */

  function animateRouterFx(wrap: RouterFxStripElement, winnerIdx: number): void {
    const grid = wrap.querySelector('.router-fx-grid')
    const selector = wrap.querySelector('.router-fx-selector') as HTMLElement | null
    if (!grid || !selector || winnerIdx < 0) return
    const cells = grid.querySelectorAll<HTMLElement>('.router-fx-cell')
    if (!cells.length || !cells[winnerIdx]) return
    clearAnimationTimers(wrap)

    wrap.dataset.state = 'playing'

    const hopCount = 9
    const sequence: number[] = []
    let prev = -1
    const totalCells = cells.length
    for (let i = 0; i < hopCount; i++) {
      let pick: number
      let guard = 0
      do {
        pick = Math.floor(Math.random() * totalCells)
        guard++
      } while ((pick === prev || pick === winnerIdx) && guard < 12)
      sequence.push(pick)
      prev = pick
    }
    sequence.push(winnerIdx)

    const dwellTimes = [50, 55, 65, 75, 90, 110, 140, 180, 240, 330]
    let scheduled = 0

    const placeFirst = (): void => {
      positionSelector(selector, cells[sequence[0]!]!, { hopIdx: 0 })
      selector.classList.add('visible')
      ping(cells[sequence[0]!]!)
    }

    wrap._fxAnimTimers = wrap._fxAnimTimers || []
    sequence.forEach((idx, hopIdx) => {
      if (hopIdx === 0) return
      scheduled += dwellTimes[hopIdx - 1] || 200
      const timer = setTimeout(() => {
        if (!wrap.isConnected || wrap.dataset.renderMode !== 'live') return
        if (hopIdx < sequence.length - 1) {
          positionSelector(selector, cells[idx]!, { hopIdx })
          ping(cells[idx]!)
        } else {
          settleRouterFxImmediate(wrap, idx, { burst: true, decision: wrap._fxDecision })
          ping(cells[idx]!)
        }
      }, scheduled)
      wrap._fxAnimTimers!.push(timer)
    })

    wrap._fxAnimFrame = requestAnimationFrame(() => {
      wrap._fxAnimFrame = null
      if (!wrap.isConnected || wrap.dataset.renderMode !== 'live') return
      placeFirst()
    })
  }

  /* ── scan → lock (chat.js:4047-4379) ──────────────────────────────────── */

  function pendingRouterFxScanMatchesCurrentTurn(): boolean {
    if (!_routerFxScanPending) return false
    return (
      _routerFxScanPending.sessionKey === sessionKey() &&
      _routerFxScanPending.turnIndex === String(countUserMessages())
    )
  }

  function cancelPendingRouterFxScan(reason = ''): void {
    const pending = _routerFxScanPending
    if (_routerFxScanDelayTimer) {
      clearTimeout(_routerFxScanDelayTimer)
      _routerFxScanDelayTimer = null
    }
    _routerFxScanPending = null
    if (pending) {
      diag('router_scan.pending.cancelled', {
        reason: reason || '',
        sessionKey: pending.sessionKey || '',
        turnIndex: pending.turnIndex || '',
      })
    }
  }

  function clearRouterFxVisuals(reason = ''): void {
    cancelPendingRouterFxScan(reason || 'clear_visuals')
    strips().forEach((el) => removeStrip(el))
  }

  async function finishPendingRouterFxScan(): Promise<void> {
    const pending = _routerFxScanPending
    _routerFxScanDelayTimer = null
    _routerFxScanPending = null
    if (!pending) return
    if (pending.sessionKey !== sessionKey()) {
      diag('router_scan.pending.drop.session_changed', {
        pendingSessionKey: pending.sessionKey || '',
        sessionKey: sessionKey(),
      })
      return
    }
    if (
      isCompactInFlightForCurrentSession() ||
      routerFxIsSuppressedForCompactionTurn(pending.turnIndex)
    ) {
      diag('router_scan.pending.drop.compaction_suppressed', {
        sessionKey: pending.sessionKey || '',
        turnIndex: pending.turnIndex || '',
      })
      return
    }
    await awaitConfig()
    if (pending.sessionKey !== sessionKey()) {
      diag('router_scan.pending.drop.session_changed_after_config', {
        pendingSessionKey: pending.sessionKey || '',
        sessionKey: sessionKey(),
      })
      return
    }
    if (
      isCompactInFlightForCurrentSession() ||
      routerFxIsSuppressedForCompactionTurn(pending.turnIndex)
    ) {
      diag('router_scan.pending.drop.compaction_suppressed_after_config', {
        sessionKey: pending.sessionKey || '',
        turnIndex: pending.turnIndex || '',
      })
      return
    }
    const started = beginScan(pending.anchorDiv, pending.seedKey, {
      requestKind: pending.requestKind,
    })
    if (!started || !pending.decision || !thread()) return
    const liveStrip = strips('.router-fx[data-live="true"]')[0] || null
    if (!liveStrip || liveStrip.dataset.turnIndex !== String(pending.turnIndex)) return
    liveStrip._fxDecision = pending.decision
    diag('router_decision.cached_on_delayed_live_strip', {
      payload: summarizePayload(pending.decision),
      liveStrip: describeElement(liveStrip),
    })
    if (liveStrip._fxFinished) {
      lock(liveStrip, pending.decision)
      scrollToBottom()
    }
  }

  function scheduleBeginScan(
    anchorDiv: HTMLElement | null,
    seedKey: string,
    opts: { requestKind?: string } = {},
  ): boolean {
    const requestKind = routerFxNormalizeRequestKind(opts.requestKind)
    cancelPendingRouterFxScan('reschedule')
    if (routerFxIsSuppressedForCompactionTurn(String(countUserMessages()))) {
      diag('router_scan.schedule.skip.compaction_suppressed', {
        turnIndex: String(countUserMessages()),
      })
      return false
    }
    if (!thread() || !pref.enabled || !routerFeatureEnabled()) {
      diag('router_scan.schedule.skip', {
        hasThread: !!thread(),
        routerFxEnabled: !!pref.enabled,
        routerFeatureEnabled: !!routerFeatureEnabled(),
      })
      return false
    }
    if (reg.configTiers !== null && !routerFxHasMultipleCandidates(reg, requestKind, null)) {
      diag('router_scan.schedule.skip.single_candidate', {
        requestKind,
        candidates: routerFxVisualEntries(reg, requestKind, null).length,
      })
      return false
    }
    _routerFxScanPending = {
      anchorDiv,
      seedKey,
      requestKind,
      sessionKey: sessionKey(),
      turnIndex: String(countUserMessages()),
      decision: null,
    }
    _routerFxScanDelayTimer = setTimeout(() => {
      void finishPendingRouterFxScan()
    }, ROUTER_FX_START_DELAY_MS)
    diag('router_scan.scheduled', {
      seedKey,
      requestKind,
      delayMs: ROUTER_FX_START_DELAY_MS,
      turnIndex: _routerFxScanPending.turnIndex,
      anchor: describeElement(anchorDiv),
    })
    return true
  }

  function beginScan(
    anchorDiv: HTMLElement | null,
    seedKey: string,
    opts: { requestKind?: string } = {},
  ): boolean {
    const requestKind = routerFxNormalizeRequestKind(opts.requestKind)
    if (routerFxIsSuppressedForCompactionTurn(String(countUserMessages()))) {
      diag('router_scan.skip.compaction_suppressed', {
        turnIndex: String(countUserMessages()),
      })
      return false
    }
    if (!thread() || !pref.enabled || !routerFeatureEnabled()) {
      diag('router_scan.skip', {
        hasThread: !!thread(),
        routerFxEnabled: !!pref.enabled,
        routerFeatureEnabled: !!routerFeatureEnabled(),
      })
      return false
    }
    if (!routerFxHasMultipleCandidates(reg, requestKind, null)) {
      diag('router_scan.skip.single_candidate', {
        requestKind,
        candidates: routerFxVisualEntries(reg, requestKind, null).length,
      })
      return false
    }
    strips('.router-fx[data-live="true"]').forEach((el) => removeStrip(el))
    const wrap = buildRouterFxElement(
      { source: 'none' },
      { seedKey, renderMode: 'live', requestKind },
    )
    if (!wrap) {
      diag('router_scan.skip.single_candidate', {
        requestKind,
        candidates: routerFxVisualEntries(reg, requestKind, null).length,
      })
      return false
    }
    wrap.dataset.live = 'true'
    wrap.dataset.scanning = 'true'
    wrap.dataset.state = 'scanning'
    wrap.dataset.sessionKey = sessionKey()
    wrap.dataset.turnIndex = String(countUserMessages())
    insertAnchored(wrap)
    scanRoam(wrap)
    diag('router_scan.started', {
      seedKey,
      anchor: describeElement(anchorDiv),
      strip: describeElement(wrap),
    })
    wrap._fxScanCap = setTimeout(() => finishScan(wrap), ROUTER_FX_SCAN_MS)
    scrollToBottom()
    return true
  }

  function finishScan(wrap: RouterFxStripElement): void {
    if (!wrap || wrap._fxFinished) return
    wrap._fxFinished = true
    if (wrap._fxScanCap) {
      clearTimeout(wrap._fxScanCap)
      wrap._fxScanCap = null
    }
    if (wrap._fxDecision) {
      diag('router_scan.finish.with_decision', {
        strip: describeElement(wrap),
        payload: summarizePayload(wrap._fxDecision),
      })
      lock(wrap, wrap._fxDecision)
    } else {
      stopScan(wrap)
      clearVisualResidue(wrap)
      wrap.dataset.state = 'settled'
      applySettledSemantics(wrap, null, 'live')
      diag('router_scan.finish.no_decision', { strip: describeElement(wrap) })
    }
  }

  function scanRoam(wrap: RouterFxStripElement): void {
    const grid = wrap.querySelector('.router-fx-grid')
    if (!grid) return
    const targets = grid.querySelectorAll<HTMLElement>('.router-fx-cell')
    if (!targets.length) return
    const selector = grid.querySelector('.router-fx-selector') as HTMLElement | null
    if (selector) selector.classList.add('visible')
    let prev = -1
    const step = (): void => {
      if (!wrap.isConnected || wrap.dataset.scanning !== 'true') return
      let i: number
      let g = 0
      do {
        i = Math.floor(Math.random() * targets.length)
        g++
      } while (i === prev && g < 8)
      prev = i
      if (selector) {
        positionSelector(selector, targets[i]!, { hopIdx: i })
        ping(targets[i]!)
      }
      wrap._fxScanTimer = setTimeout(step, 190)
    }
    step()
  }

  function stopScan(wrap: RouterFxStripElement): void {
    if (!wrap) return
    if (wrap._fxScanTimer) {
      clearTimeout(wrap._fxScanTimer)
      wrap._fxScanTimer = null
    }
    if (wrap._fxScanCap) {
      clearTimeout(wrap._fxScanCap)
      wrap._fxScanCap = null
    }
    delete wrap.dataset.scanning
  }

  function pauseScanTimers(wrap: RouterFxStripElement): void {
    if (!wrap) return
    if (wrap._fxScanTimer) {
      clearTimeout(wrap._fxScanTimer)
      wrap._fxScanTimer = null
    }
    if (wrap._fxScanCap) {
      clearTimeout(wrap._fxScanCap)
      wrap._fxScanCap = null
    }
  }

  function resumeLiveStrip(wrap: RouterFxStripElement): void {
    if (!wrap || wrap.dataset.live !== 'true') return
    pauseScanTimers(wrap)
    if (wrap.dataset.scanning === 'true' && !wrap._fxFinished) {
      scanRoam(wrap)
      if (wrap._fxDecision) {
        wrap._fxScanCap = setTimeout(() => finishScan(wrap), ROUTER_FX_SCAN_MS)
      } else {
        diag('router_scan.resume_without_decision', { strip: describeElement(wrap) })
      }
      return
    }
    if (wrap._fxFinished && wrap._fxDecision && !wrap.dataset.routerIdentity) {
      lock(wrap, wrap._fxDecision)
    }
  }

  function settleForOutput(): void {
    strips('.router-fx[data-live="true"]').forEach((wrap) => {
      if (wrap._fxDecision) {
        finishScan(wrap)
      } else {
        diag('router_scan.keep_scanning_without_decision_on_output', {
          strip: describeElement(wrap),
        })
      }
    })
  }

  function lock(wrap: RouterFxStripElement, decision: RouterFxDecision | null | undefined): void {
    if (!wrap) return
    decision = decision || {}
    stopScan(wrap)
    wrap.dataset.tier = routerFxNormalizeTier(decision.tier || '')
    wrap.dataset.source = decision.source || 'none'
    wrap.dataset.renderMode = wrap.dataset.renderMode || 'live'
    wrap._fxDecision = decision
    const identity = routerFxDecisionIdentity(decision)
    if (identity) wrap.dataset.routerIdentity = identity
    if (decision.routing_applied === false) {
      wrap.dataset.observe = 'true'
      wrap.dataset.rolloutPhase =
        typeof decision.rollout_phase === 'string' ? decision.rollout_phase : 'observe'
    }
    lockGrid(wrap, decision)
  }

  function lockGrid(wrap: RouterFxStripElement, decision: RouterFxDecision): void {
    const tier = routerFxNormalizeTier(decision.tier || '')
    if (tier) {
      reg.rememberTierDecision(tier, decision.model || '')
    }
    const winnerIdx = winnerCellIndex(wrap, tier)
    if (winnerIdx >= 0) {
      requestAnimationFrame(() => {
        if (wrap.isConnected) settleRouterFxImmediate(wrap, winnerIdx, { burst: true, decision })
      })
    } else {
      wrap.dataset.state = 'settled'
      delete wrap.dataset.live
      delete wrap.dataset.scanning
      wrap._fxFinished = true
      applySettledSemantics(wrap, decision, wrap.dataset.renderMode)
    }
  }

  /* ── anchor lookups (chat.js:4388-4413) ───────────────────────────────── */

  function countUserMessages(): number {
    const th = thread()
    if (!th) return 0
    return th.querySelectorAll('.msg.user, .msg[data-history-role="user"]').length
  }

  function lastUserMessage(): HTMLElement | null {
    const th = thread()
    if (!th) return null
    const userMsgs = th.querySelectorAll<HTMLElement>('.msg.user, .msg[data-history-role="user"]')
    return userMsgs.length ? userMsgs[userMsgs.length - 1]! : null
  }

  function userMessageForAssistant(referenceAssistant: HTMLElement | null): HTMLElement | null {
    if (!referenceAssistant) return null
    let prev = referenceAssistant.previousElementSibling as HTMLElement | null
    while (prev) {
      if (
        prev.classList &&
        (prev.classList.contains('user') || prev.getAttribute('data-history-role') === 'user')
      ) {
        return prev
      }
      prev = prev.previousElementSibling as HTMLElement | null
    }
    return null
  }

  /* ── label fit (chat.js:4419-4472) ────────────────────────────────────── */

  function measureLabels(wrap: RouterFxStripElement): void {
    if (!wrap || !wrap.isConnected) return
    wrap.querySelectorAll<HTMLElement>('.router-fx-cell').forEach((cell) => {
      const nm = cell.querySelector('.nm') as HTMLElement | null
      if (!nm) return
      nm.style.fontSize = ''
      const avail = cell.clientWidth - 12
      if (avail <= 0) return
      const w = nm.scrollWidth
      if (w > avail) {
        const base = parseFloat(getComputedStyle(nm).fontSize) || 10.5
        nm.style.fontSize = Math.max(7, base * (avail / w)).toFixed(1) + 'px'
      }
    })
  }

  function scheduleLabelFit(wrap: RouterFxStripElement): void {
    if (!wrap) return
    if (wrap._fxFitFrame) cancelAnimationFrame(wrap._fxFitFrame)
    wrap._fxFitFrame = requestAnimationFrame(() => {
      wrap._fxFitFrame = null
      measureLabels(wrap)
    })
  }

  function installLabelFit(wrap: RouterFxStripElement): void {
    if (!wrap || wrap._fxFitInstalled) return
    wrap._fxFitInstalled = true
    const grid = wrap.querySelector('.router-fx-grid')
    if (grid && typeof ResizeObserver === 'function') {
      wrap._fxLabelResizeObserver = new ResizeObserver(() => scheduleLabelFit(wrap))
      wrap._fxLabelResizeObserver.observe(grid)
    }
    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(() => scheduleLabelFit(wrap)).catch(() => {})
    }
  }

  function fitLabels(wrap: RouterFxStripElement): void {
    if (!wrap) return
    installLabelFit(wrap)
    scheduleLabelFit(wrap)
  }

  function insertAnchored(wrap: RouterFxStripElement): void {
    // chat.js:4465-4472 — the strip mounts in the composer dock; the legacy
    // reference-assistant param is dropped (the dock mount ignores it).
    fitLabels(wrap)
    mountStrip(wrap)
  }

  /* ── compaction suppression (chat.js:3263-3282) ───────────────────────── */

  function routerFxIsSuppressedForCompactionTurn(turnIndex: string): boolean {
    // Prefer an injected predicate when the compaction owner wires one; else
    // fall back to the ported sticky-state logic (chat.js:3263-3266).
    if (deps.isSuppressedForCompactionTurn) return isSuppressedForCompactionTurn(turnIndex)
    if (!_compactSuppressedRouterTurnIndex) return false
    if (String(turnIndex || '') !== _compactSuppressedRouterTurnIndex) return false
    return (
      !_compactSuppressedRouterSessionKey || _compactSuppressedRouterSessionKey === sessionKey()
    )
  }

  function suppressForCompaction(payload: { key?: string } = {}): void {
    // chat.js:3269-3282
    cancelPendingRouterFxScan('compaction')
    if (!thread()) return
    const turnIndex = String(countUserMessages())
    if (!turnIndex || turnIndex === '0') return
    const key = String((payload && payload.key) || sessionKey() || '')
    _compactSuppressedRouterSessionKey = key
    _compactSuppressedRouterTurnIndex = turnIndex
    strips('.router-fx[data-live="true"]').forEach((el) => {
      const sameSession = !key || !el.dataset.sessionKey || el.dataset.sessionKey === key
      const sameTurn = !el.dataset.turnIndex || el.dataset.turnIndex === turnIndex
      if (sameSession && sameTurn) removeStrip(el)
    })
    diag('router_scan.suppressed_for_compaction', { key, turnIndex })
  }

  /* ── pending-decision cache (chat.js:3652-3685) ───────────────────────── */

  function pendingRouterDecisionKey(turnIndex: number | string): string {
    return `${sessionKey()}:${turnIndex || 'latest'}`
  }

  function cachePendingRouterDecision(payload: RouterFxDecision): void {
    const turnIndex = countUserMessages()
    const key = pendingRouterDecisionKey(turnIndex > 0 ? turnIndex : 'latest')
    _pendingRouterDecisions.set(key, payload)
    diag('router_decision.cached_pending_anchor', {
      key,
      payload: summarizePayload(payload),
    })
  }

  function flushPendingRouterDecisions(): void {
    if (!thread() || !pref.enabled) return
    if (!lastUserMessage()) return
    const turnIndex = countUserMessages()
    const keys = [pendingRouterDecisionKey(turnIndex), pendingRouterDecisionKey('latest')]
    for (const key of keys) {
      if (!_pendingRouterDecisions.has(key)) continue
      const payload = _pendingRouterDecisions.get(key)!
      _pendingRouterDecisions.delete(key)
      diag('router_decision.flush_pending_anchor', {
        key,
        payload: summarizePayload(payload),
      })
      void handleRouterDecision(payload)
      return
    }
  }

  /* ── live entry point (chat.js:4480-4631) ─────────────────────────────── */

  async function handleRouterDecision(payload: RouterFxDecision): Promise<void> {
    diag('router_decision.handle.start', summarizePayload(payload))
    if (!payload || typeof payload !== 'object') {
      diag('router_decision.skip.invalid_payload', {})
      return
    }
    const tier = routerFxNormalizeTier(payload.tier || '')
    if (!tier) {
      diag('router_decision.skip.no_tier', summarizePayload(payload))
      return
    }
    reg.rememberTierDecision(tier, payload.model || '')
    const turnIndex = countUserMessages()
    if (routerFxIsSuppressedForCompactionTurn(String(turnIndex))) {
      if (thread()) {
        strips('.router-fx[data-live="true"]').forEach((el) => {
          if (!el.dataset.turnIndex || el.dataset.turnIndex === String(turnIndex)) {
            removeStrip(el)
          }
        })
      }
      diag('router_decision.skip.compaction_suppressed', {
        payload: summarizePayload(payload),
        turnIndex: String(turnIndex),
      })
      return
    }
    if (!pref.enabled) {
      diag('router_decision.skip.disabled_pre_config', summarizePayload(payload))
      return
    }
    if (!thread()) {
      diag('router_decision.skip.no_thread_pre_config', summarizePayload(payload))
      return
    }
    if (pendingRouterFxScanMatchesCurrentTurn()) {
      _routerFxScanPending!.decision = payload
      diag('router_decision.cached_on_pending_scan', {
        payload: summarizePayload(payload),
        turnIndex: _routerFxScanPending!.turnIndex || '',
        requestKind: _routerFxScanPending!.requestKind || '',
      })
      return
    }
    const liveStrip = strips('.router-fx[data-live="true"]')[0] || null
    if (liveStrip && liveStrip.dataset.turnIndex === String(countUserMessages())) {
      liveStrip.dataset.sessionKey = sessionKey()
      liveStrip._fxDecision = payload
      diag('router_decision.cached_on_live_strip', {
        payload: summarizePayload(payload),
        liveStrip: describeElement(liveStrip),
        finished: !!liveStrip._fxFinished,
      })
      if (liveStrip._fxFinished) {
        lock(liveStrip, payload)
        scrollToBottom()
      }
      return
    }
    await awaitConfig()
    if (!thread()) {
      diag('router_decision.skip.no_thread_post_config', summarizePayload(payload))
      return
    }
    if (!pref.enabled) {
      diag('router_decision.skip.disabled_post_config', summarizePayload(payload))
      return
    }
    const replayRequestKind = routerFxRequestKindFromDecision(payload, null)
    if (!routerFxHasMultipleCandidates(reg, replayRequestKind, payload)) {
      diag('router_decision.skip.single_candidate', summarizePayload(payload))
      return
    }
    if (!historyHasRendered() || historyHydrating()) {
      cachePendingRouterDecision(payload)
      diag('router_decision.cached_during_history_hydration', {
        payload: summarizePayload(payload),
        historyHasRendered: !!historyHasRendered(),
        historyHydrating: !!historyHydrating(),
      })
      return
    }
    const anchorUser = lastUserMessage()
    if (!anchorUser) {
      cachePendingRouterDecision(payload)
      return
    }
    const replaySeed = routerFxResolveLayoutSeed(sessionKey())
    const wrap = buildRouterFxElement(payload, {
      preSettled: true,
      renderMode: 'history',
      seedKey: replaySeed,
      requestKind: replayRequestKind,
    })
    if (!wrap) {
      diag('router_decision.skip.single_candidate', summarizePayload(payload))
      return
    }
    const winnerIdx = winnerCellIndex(wrap, tier)
    if (winnerIdx < 0) {
      diag('router_decision.skip.no_winner', {
        payload: summarizePayload(payload),
        winnerIdx,
      })
      return
    }
    wrap.dataset.sessionKey = sessionKey()
    wrap.dataset.turnIndex = String(turnIndex)
    const observeMode = payload && payload.routing_applied === false
    strips('.router-fx[data-live="true"]').forEach((el) => {
      if (el !== wrap) removeStrip(el)
    })
    insertAnchored(wrap)
    normalizeSettledStrip(wrap, 'history', payload)
    diag('router_decision.inserted_settled_strip', {
      payload: summarizePayload(payload),
      strip: describeElement(wrap),
      observeMode,
      winnerIdx,
    })
    scrollToBottom()
  }

  /* ── history entry point (chat.js:4637-4680) ──────────────────────────── */

  function buildRouterFxFromUsage(
    usage:
      | {
          routed_tier?: string
          routed_model?: string
          model?: string
          routing_source?: string
          routing_confidence?: number
          routing_applied?: boolean
          rollout_phase?: string
        }
      | null
      | undefined,
    seedKey: string | number | null | undefined,
    opts: { requestKind?: string } = {},
  ): RouterFxStripElement | null {
    if (!usage) return null
    if (!pref.enabled) return null
    if (reg.configTiers !== null && !routerFeatureEnabled()) return null
    const tier = routerFxNormalizeTier(usage.routed_tier || '')
    if (!tier) return null
    if (reg.configTiers !== null && !reg.configTiers.has(tier)) {
      return null
    }
    reg.rememberTierDecision(tier, usage.routed_model || usage.model || '')
    const decision: RouterFxDecision = {
      tier,
      model: usage.routed_model || usage.model || '',
      source: usage.routing_source || 'none',
      confidence: typeof usage.routing_confidence === 'number' ? usage.routing_confidence : 0,
      fallback: usage.routing_source === 'fallback',
      routing_applied: usage.routing_applied !== false,
      rollout_phase: usage.rollout_phase || 'full',
    }
    const requestKind = routerFxRequestKindFromDecision(decision, opts.requestKind)
    return buildRouterFxElement(decision, {
      preSettled: true,
      seedKey: seedKey != null ? String(seedKey) : 'history:' + tier,
      requestKind,
    })
  }

  return {
    // live entry + history builder
    handleRouterDecision,
    buildRouterFxFromUsage,
    // pending-anchor cache (flushed by the history renderer once an anchor exists)
    cachePendingRouterDecision,
    flushPendingRouterDecisions,
    // stream-lifecycle hooks (composed into the controller — chat.js:6585/6907/…)
    settleForOutput,
    cancelPendingRouterFxScan,
    staticizeCompletedStrips,
    pauseScanTimers,
    resumeLiveStrip,
    clearRouterFxVisuals,
    // scan scheduling (driven by the chat send flow)
    scheduleBeginScan,
    beginScan,
    // compaction (Task 7)
    suppressForCompaction,
    isSuppressedForCompactionTurn: routerFxIsSuppressedForCompactionTurn,
    // strip queries + anchor lookups (park/restore in stream.ts)
    strips,
    currentSessionLiveRouterStrips: (key?: string): HTMLElement[] => {
      const k = key || sessionKey()
      return strips('.router-fx[data-live="true"]').filter(
        (el) => !el.dataset.sessionKey || el.dataset.sessionKey === k,
      )
    },
    lastUserMessage,
    userMessageForAssistant,
    // The legacy anchor/bubble args are ignored (dock mount). A 1-arg function
    // is assignable where the 3-arg dep shape is expected, so the stream
    // controller's park/restore call site stays uniform.
    insertLiveRouterStripForAnchor: (el: HTMLElement): void =>
      insertAnchored(el as RouterFxStripElement),
    // dock predicate for stream.ts's `if (routerFxDock())`
    hasDock: (): boolean => !!dock(),
    // one-shot animate (parity; retained)
    animateRouterFx,
    // build (exposed for tests / the history renderer)
    buildRouterFxElement,
  }
}

export type RouterFxRenderer = ReturnType<typeof createRouterFxRenderer>
