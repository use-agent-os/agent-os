// Pure skills-view helpers ported 1:1 from the legacy view
// (src/agentos/gateway/static/js/views/skills.js). Each function below carries
// the legacy line range it mirrors so the parity matrix stays auditable. RPC
// calls, mutations, dialogs and rendering live in SkillsPage.tsx; this module
// owns the pure derivations (filtering, layer grouping/sort, stats, category
// derivation, registry filtering, install-action state, and small utilities).

// ── Types ────────────────────────────────────────────────────────────────────

/** An installed skill row from skills.list (all fields optional). */
export interface RawSkill {
  name?: string
  description?: string
  emoji?: string
  layer?: string
  status?: string
  status_detail?: string
  eligible?: boolean
  triggers?: string[]
  homepage?: string
  file_path?: string
  missing_bins?: string[]
  missing_env?: string[]
  install?: SkillInstallOption[]
  requirements?: SkillRequirements
  [key: string]: unknown
}

export interface SkillInstallOption {
  id?: string
  kind?: string
  label?: string
  bins?: string[]
}

export interface SkillRequirements {
  items?: SkillRequirementItem[]
}

export interface SkillRequirementItem {
  name?: string
  status?: string
  missing_bins?: string[]
  missing_env?: string[]
  requires_bins?: string[]
  requires_any_bins?: string[]
  requires_env?: string[]
}

/** A registry/catalog row from skills.search (bankr / community). */
export interface RegistryItem {
  name?: string
  identifier?: string
  provider?: string
  source?: string
  description?: string
  category?: string
  logo?: string
  emoji?: string
  homepage?: string
  trust_level?: string
  installed?: boolean
  setup?: string[]
  demo?: { code?: string; language?: string; title?: string }
  [key: string]: unknown
}

/** The four status-filter keys the metric pills toggle (skills.js:352-366). */
export type StatusFilter = 'all' | 'ready' | 'needs-setup' | 'not-declared'

// ── Constants (skills.js:36-58) ──────────────────────────────────────────────

export const LAYER_ORDER = [
  'workspace',
  'bundled',
  'managed',
  'personal',
  'project',
  'extra',
] as const

export const LAYER_LABEL: Record<string, string> = {
  workspace: 'Workspace',
  bundled: 'Bundled',
  managed: 'Managed',
  personal: 'Personal',
  project: 'Project',
  extra: 'Extra',
}

export const LAYER_HELP: Record<string, string> = {
  workspace: 'Workspace skills are local to the active workspace.',
  bundled: 'Bundled skills ship with AgentOS.',
  managed: 'Managed skills are locally installed into AgentOS state.',
  personal: 'Personal skills are local user installs, not bundled.',
  project: 'Project skills are local to the current project.',
  extra: 'Extra skills come from configured local directories.',
}

export const CAT_LABEL: Record<string, string> = {
  all: 'All',
  trading: 'Trading',
  defi: 'DeFi',
  wallet: 'Wallets',
  markets: 'Markets',
  social: 'Social',
  data: 'Data',
  nft: 'NFT',
  dev: 'Dev tools',
  infra: 'Infra',
  other: 'Other',
}

/** skills.js:210,220 — the registry-search debounce interval (ms). */
export const REGISTRY_SEARCH_DEBOUNCE_MS = 250

// ── Layer label/help (skills.js:1070-1076) ───────────────────────────────────

export function layerLabel(layer?: string): string {
  return (layer && LAYER_LABEL[layer]) || layer || 'Unknown'
}

export function layerHelp(layer?: string): string {
  return (layer && LAYER_HELP[layer]) || 'Configured local skill directory.'
}

// ── Installed stats (skills.js:342-367) ──────────────────────────────────────

export interface SkillStats {
  total: number
  ready: number
  needs: number
  notDeclared: number
}

export function skillStats(skills: RawSkill[]): SkillStats {
  return {
    total: skills.length,
    ready: skills.filter((s) => s.status === 'ready').length,
    needs: skills.filter((s) => s.status === 'needs_setup').length,
    notDeclared: skills.filter((s) => s.status === 'not_declared').length,
  }
}

// ── Installed filter (skills.js:374-388) ─────────────────────────────────────

/**
 * skills.js:374-388 — filter installed skills by the free-text filter (name /
 * description / triggers, case-insensitive) then the active status pill.
 * `filterText` is expected already-lowercased (legacy keeps `_filterText`
 * lowercased); we lowercase again defensively so the helper is order-safe.
 */
export function filterSkills(
  skills: RawSkill[],
  filterText: string,
  statusFilter: StatusFilter,
): RawSkill[] {
  const q = (filterText || '').toLowerCase()
  let out = skills
  if (q) {
    out = out.filter(
      (s) =>
        (s.name || '').toLowerCase().includes(q) ||
        (s.description || '').toLowerCase().includes(q) ||
        (s.triggers || []).some((t) => t.toLowerCase().includes(q)),
    )
  }
  if (statusFilter === 'ready') out = out.filter((s) => s.status === 'ready')
  else if (statusFilter === 'needs-setup') out = out.filter((s) => s.status === 'needs_setup')
  else if (statusFilter === 'not-declared') out = out.filter((s) => s.status === 'not_declared')
  return out
}

/** skills.js:391-399 — the empty-state message for the installed list. */
export function installedEmptyMessage(filterText: string, statusFilter: StatusFilter): string {
  if (filterText) return `No skills match ${filterText}.`
  if (statusFilter === 'ready') return 'No skills are ready. Install dependencies to enable them.'
  if (statusFilter === 'needs-setup') return 'No skills currently need setup.'
  if (statusFilter === 'not-declared') return 'No skills without declared dependencies.'
  return 'No skills installed.'
}

// ── Layer grouping + ready-first sort (skills.js:407-442) ─────────────────────

/** skills.js:407-411 — sort rank: ready(0) < not_declared(1) < needs_setup(2). */
export function skillRank(s: RawSkill): number {
  if (s.status === 'ready') return 0
  if (s.status === 'not_declared') return 1
  return 2
}

export interface SkillGroup {
  layer: string
  label: string
  help: string
  skills: RawSkill[]
}

/**
 * skills.js:413-442 — bucket the filtered skills by layer, sort each bucket
 * ready-first then name-asc, and emit groups in LAYER_ORDER (skipping empties).
 * An unknown layer buckets under 'extra' (legacy `s.layer || 'extra'`).
 */
export function groupSkillsByLayer(skills: RawSkill[]): SkillGroup[] {
  const groups: Record<string, RawSkill[]> = {}
  skills.forEach((s) => {
    const l = s.layer || 'extra'
    ;(groups[l] = groups[l] || []).push(s)
  })
  const sortByReady = (list: RawSkill[]) =>
    list.sort((a, b) => {
      const ra = skillRank(a)
      const rb = skillRank(b)
      if (ra !== rb) return ra - rb
      return (a.name || '').localeCompare(b.name || '')
    })
  Object.values(groups).forEach(sortByReady)
  const out: SkillGroup[] = []
  LAYER_ORDER.forEach((layer) => {
    const list = groups[layer]
    if (!list || list.length === 0) return
    out.push({ layer, label: layerLabel(layer), help: layerHelp(layer), skills: list })
  })
  return out
}

// ── Card status → tone/label (skills.js:447-465, 779-789) ─────────────────────

/** The card status dot class: ready / needs / unverified. */
export type SkillDot = 'is-ready' | 'is-needs' | 'is-unverified'

/** skills.js:448 — resolve a skill's effective status (falls back to eligible). */
export function skillStatus(skill: RawSkill): string {
  return skill.status || (skill.eligible ? 'ready' : 'needs_setup')
}

/** skills.js:449-452 — the status-dot class for a card. */
export function skillDotClass(skill: RawSkill): SkillDot {
  const status = skillStatus(skill)
  if (status === 'ready') return 'is-ready'
  if (status === 'needs_setup') return 'is-needs'
  return 'is-unverified'
}

/** skills.js:454 — the dot tooltip. */
export function skillDotTitle(skill: RawSkill): string {
  return skill.status_detail || (skill.eligible ? 'Ready' : 'Needs setup')
}

// ── Robinhood partner grouping (skills.js:469-497) ────────────────────────────

/**
 * skills.js:469-477 — partner grouping is a brand surface: only BUNDLED skills
 * whose name starts with `robinhood` or whose homepage mentions robinhood.com
 * qualify (a user-installed community skill can't wear the partner banner).
 */
export function isRobinhoodSkill(skill: RawSkill): boolean {
  if (skill.layer !== 'bundled') return false
  const name = (skill.name || '').toLowerCase()
  const home = (skill.homepage || '').toLowerCase()
  return name.startsWith('robinhood') || home.includes('robinhood.com')
}

/** skills.js:484-486 — the installed Robinhood-family skills, name-sorted. */
export function robinhoodSkills(skills: RawSkill[]): RawSkill[] {
  return skills.filter(isRobinhoodSkill).sort((a, b) => (a.name || '').localeCompare(b.name || ''))
}

// ── Registry (community / bankr) derivations ──────────────────────────────────

/**
 * skills.js:503-505 — when the dedicated Bankr tab is showing, Community
 * excludes source==='bankr' rows; otherwise Bankr falls through into Community.
 */
export function communityFilter(results: RegistryItem[], showBankr: boolean): RegistryItem[] {
  return showBankr ? results.filter((r) => r.source !== 'bankr') : results
}

/** skills.js:560-564 — category → count map over a registry list. */
export function categoriesFor(list: RegistryItem[]): Record<string, number> {
  const counts: Record<string, number> = {}
  list.forEach((r) => {
    const c = r.category || 'other'
    counts[c] = (counts[c] || 0) + 1
  })
  return counts
}

export interface CategoryChip {
  cat: string
  label: string
  count: number
  active: boolean
}

/**
 * skills.js:567-587 — chips derive from the FULL snapshot only (never change on
 * keystrokes). No chips when there are no items, or only the 'other' category.
 * 'all' leads, then categories sorted by count desc.
 */
export function categoryChips(snapshot: RegistryItem[], activeCat: string): CategoryChip[] {
  const counts = categoriesFor(snapshot)
  const keys = Object.keys(counts)
  const hasCats = keys.some((c) => c && c !== 'other') || keys.length > 1
  if (!hasCats || !snapshot.length) return []
  const cats = ['all', ...keys.sort((a, b) => (counts[b] ?? 0) - (counts[a] ?? 0))]
  return cats.map((c) => ({
    cat: c,
    label: CAT_LABEL[c] || c,
    count: c === 'all' ? snapshot.length : (counts[c] ?? 0),
    active: activeCat === c,
  }))
}

/**
 * skills.js:610-620 — apply the category filter then the case-insensitive text
 * filter (name / provider / description) to a registry list. `query` is trimmed
 * + lowercased here (legacy trims/lowercases inline).
 */
export function filterRegistry(
  items: RegistryItem[],
  category: string,
  query: string,
): RegistryItem[] {
  let out = items
  const cat = category || 'all'
  if (cat !== 'all') out = out.filter((r) => (r.category || 'other') === cat)
  const q = (query || '').trim().toLowerCase()
  if (q) {
    out = out.filter(
      (r) =>
        (r.name || '').toLowerCase().includes(q) ||
        (r.provider || '').toLowerCase().includes(q) ||
        (r.description || '').toLowerCase().includes(q),
    )
  }
  return out
}

/** skills.js:622-626 — the empty message for a registry group + query. */
export function registryEmptyMessage(group: 'bankr' | 'community', query: string): string {
  const q = (query || '').trim()
  if (q) return `No skills match ${q}.`
  return group === 'bankr'
    ? 'No Bankr skills available right now.'
    : 'No community skills available right now.'
}

/** skills.js:662,715,283 — the stable identifier key for a registry row. */
export function registryKey(r: RegistryItem): string {
  return r.identifier || r.name || ''
}

// ── Install-action state (skills.js:633-641) ──────────────────────────────────

export type InstallActionKind = 'installed' | 'force' | 'install'

/**
 * skills.js:633-641 — the install button's state for a registry row: already
 * installed → a static badge; force-armed (post security-block) → a danger
 * force-install; otherwise a normal install.
 */
export function installAction(r: RegistryItem, forceArmed: Set<string>): InstallActionKind {
  if (r.installed) return 'installed'
  const key = registryKey(r)
  if (forceArmed.has(key)) return 'force'
  return 'install'
}

/** skills.js:254,640 — the source to install from (default 'clawhub'). */
export function installSource(r: RegistryItem): string {
  return r.source || 'clawhub'
}

// ── deps.install still-missing (skills.js:894-896) ────────────────────────────

export interface DepsInstallResult {
  success?: boolean
  message?: string
  missing_still?: { bins?: string[]; env?: string[] }
}

/** skills.js:894-896 — count of deps still missing after a deps.install. */
export function stillMissingCount(res: DepsInstallResult): number {
  const still = res.missing_still || {}
  return (still.bins || []).length + (still.env || []).length
}

// ── update result unwrap (skills.js:1000-1007) ────────────────────────────────

export interface UpdateResult {
  results?: Array<{ success?: boolean; message?: string }>
  message?: string
}

/** skills.js:1000 — skills.update returns a results[] array; take the first. */
export function firstUpdateResult(res: UpdateResult): { success?: boolean; message?: string } {
  return (res.results || [])[0] || {}
}

// ── Small utilities ──────────────────────────────────────────────────────────

/** skills.js:1019-1023 — provider/name initials for a logo fallback. */
export function initials(text?: string): string {
  const words = (text || '').trim().split(/\s+/).filter(Boolean)
  const first = words[0]
  if (!first) return '?'
  const second = words[1]
  return ((first[0] ?? '') + (second ? (second[0] ?? '') : '')).toUpperCase()
}

/** skills.js:1030-1033 — allow only http(s) URLs from remote catalogs. */
export function safeUrl(url?: string): string {
  const u = String(url || '').trim()
  return /^https?:\/\//i.test(u) ? u : ''
}

/**
 * skills.js:911-924 — flip `installed` in-place on rows matching by identifier
 * or name across the cached registry lists. Returns a NEW array (React-friendly)
 * rather than mutating, but preserves the legacy match semantics.
 */
export function markInstalled(
  list: RegistryItem[],
  identifier: string,
  name: string,
  installed: boolean,
): RegistryItem[] {
  return list.map((r) => {
    const key = registryKey(r)
    if ((identifier && key === identifier) || (name && r.name === name)) {
      return { ...r, installed }
    }
    return r
  })
}
