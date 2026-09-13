// Desktop-only derivations for the Skills panel. Everything the web console
// already computes (buckets, grouping, registry filtering, install action
// state, …) is imported from `@/views/skills/logic`; this module adds what a
// three-column master/detail layout needs on top: the source rail, a stable
// selection, the brand-mark choice, and the composer prefill for "Use".
import { t as tw } from '@/i18n'
import '@/i18n/en/skills'
import {
  layerLabel,
  registryKey,
  skillPublisherId,
  type RawSkill,
  type RegistryItem,
  type SkillRequirementItem,
} from '@/views/skills/logic'

/** The rail entries, in the order they render. */
export type SkillSource = 'installed' | 'robinhood' | 'bankr' | 'aeon' | 'capminal' | 'community'

export const SKILL_SOURCES: readonly SkillSource[] = [
  'installed',
  'robinhood',
  'bankr',
  'aeon',
  'capminal',
  'community',
]

/** Sources whose rows come from `skills.search` rather than `skills.list`. */
export type RegistrySource = Extract<SkillSource, 'bankr' | 'aeon' | 'capminal' | 'community'>
export type PartnerBrand = Extract<SkillSource, 'robinhood' | 'bankr' | 'aeon' | 'capminal'>

export function isRegistrySource(source: SkillSource): source is RegistrySource {
  return source !== 'installed' && source !== 'robinhood'
}

export function isPartnerBrand(value: string): value is PartnerBrand {
  return value === 'robinhood' || value === 'bankr' || value === 'aeon' || value === 'capminal'
}

/** Brand names are proper nouns and stay literal, as on the web console. */
export const PARTNER_LABEL: Record<PartnerBrand, string> = {
  robinhood: 'Robinhood',
  bankr: 'Bankr',
  aeon: 'Aeon',
  capminal: 'Capminal',
}

/**
 * Partner catalogs that own a rail entry must not also appear under
 * Community, mirroring `hiddenRegistrySources` on the web.
 */
export const HIDDEN_COMMUNITY_SOURCES: ReadonlySet<string> = new Set(['bankr', 'capminal', 'aeon'])

/**
 * The brand mark an installed skill wears, keyed off `publisher.id` — the one
 * field the gateway resolved against its allowlist. Never derived from the
 * name or homepage.
 */
export function partnerBrandOf(skill: RawSkill): PartnerBrand | null {
  const id = skillPublisherId(skill)
  return isPartnerBrand(id) ? id : null
}

/** A row's key in the list: skills by name, catalog rows by identifier. */
export function skillRowKey(skill: RawSkill): string {
  return String(skill.name || '')
}

/**
 * Master/detail keeps something selected whenever there is something to
 * select: the current key if it still exists in the visible rows, else the
 * first row. `null` only when the list is empty.
 */
export function resolveSelection<T>(
  rows: readonly T[],
  keyOf: (row: T) => string,
  current: string | null,
): T | null {
  if (current) {
    const hit = rows.find((r) => keyOf(r) === current)
    if (hit) return hit
  }
  return rows[0] ?? null
}

/**
 * The text "Use in chat" drops into the composer. Naming the skill explicitly
 * is the documented way to pin the agent to it; the trailing newline leaves
 * the caret on a fresh line for the actual request. Nothing is sent.
 */
export function skillPrefillText(name: string): string {
  return `use skill ${name}\n`
}

/** Where the skill came from, as a short word for a chip. */
export function acquisitionSourceLabel(skill: RawSkill): string {
  const acq = skill.acquisition
  if (acq?.kind === 'hub') return acq.source_id || tw('skills.srcHub')
  if (acq?.kind === 'shipped') return tw('skills.srcShipped')
  if (acq?.kind === 'local') return tw('skills.srcLocal')
  return layerLabel(skill.layer).toLowerCase()
}

/** The hub + author line for a hub install; '' for shipped/local skills. */
export function originLine(skill: RawSkill): string {
  const acq = skill.acquisition
  if (acq?.kind !== 'hub') return ''
  const source = acq.source_id || 'hub'
  const author = (acq.author || '').trim()
  return author ? `${source} · ${author}` : source
}

export type ReqTone = 'ok' | 'warn' | undefined

export function reqStatusLabel(status: string): string {
  if (status === 'ready') return tw('skills.reqReady')
  if (status === 'needs_setup') return tw('skills.reqNeedsSetup')
  if (status === 'missing_skill') return tw('skills.reqMissingSkill')
  return tw('skills.reqNotDeclared')
}

export function reqStatusTone(status: string): ReqTone {
  if (status === 'ready') return 'ok'
  if (status === 'needs_setup' || status === 'missing_skill') return 'warn'
  return undefined
}

/**
 * One line under a requirement: what is missing, else what it declares,
 * else "no dependencies". Missing names come back as a list so the view can
 * set them in code.
 */
export function requirementDetail(item: SkillRequirementItem): {
  missing: string[]
  text: string
} {
  const missing = [...(item.missing_bins || []), ...(item.missing_env || [])]
  if (missing.length) return { missing, text: tw('skills.reqMissingLead') }
  const requires: string[] = [...(item.requires_bins || [])]
  if ((item.requires_any_bins || []).length) {
    requires.push(tw('skills.reqOneOf', { list: (item.requires_any_bins || []).join(' / ') }))
  }
  ;(item.requires_env || []).forEach((e) => requires.push(tw('skills.reqEnv', { name: e })))
  return { missing: [], text: requires.length ? requires.join(', ') : tw('skills.reqNoDeps') }
}

/** The catalog rows a source shows, given the session's own installs. */
export function sessionInstallsFor(
  source: RegistrySource,
  installs: RegistryItem[],
): RegistryItem[] {
  if (source === 'community')
    return installs.filter((r) => !HIDDEN_COMMUNITY_SOURCES.has(String(r.source || '')))
  return installs.filter((r) => r.source === source)
}

/** Drop a removed skill from the rows installed this session. */
export function forgetSessionInstall(
  installs: RegistryItem[],
  name: string,
  identifier: string,
): RegistryItem[] {
  return installs.filter((r) => r.name !== name && registryKey(r) !== identifier)
}

/** A short "3 of 12" style summary for the panel header. */
export function panelSubtitle(total: number, ready: number, needs: number): string {
  if (total === 0) return ''
  const parts = [`${total} ${total === 1 ? 'skill' : 'skills'}`, `${ready} ready`]
  if (needs > 0) parts.push(`${needs} need setup`)
  return parts.join(' · ')
}

/** Tone for the status light on a row and the chip in the detail head. */
export type StatusTone = 'ok' | 'warn' | 'dim'

/** The `scan_verdict` word as a chip tone; unknown verdicts stay neutral. */
export function verdictTone(verdict?: string): 'ok' | 'warn' | 'danger' | undefined {
  const v = (verdict || '').toLowerCase()
  if (v === 'clean' || v === 'safe' || v === 'trusted') return 'ok'
  if (v === 'suspicious' || v === 'warn' || v === 'warning') return 'warn'
  if (v === 'dangerous' || v === 'blocked') return 'danger'
  return undefined
}

/** ISO timestamp → a local date, or '' when unparsable. */
export function installedAtLabel(iso?: string): string {
  if (!iso) return ''
  const ts = new Date(iso).getTime()
  if (Number.isNaN(ts)) return ''
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' }).format(ts)
}
