import type { SettingsSnapshot } from '@/views/settings/snapshot'
import { useKeyFacts } from './capabilities/key-context'
import { capabilityStatus, xSearchDef, type KeyContext } from './capabilities/logic'
import type { SettingsSection } from './sections'
import { useConfigSnapshot } from './use-snapshot'

export type AttentionTone = 'warn' | 'danger'

/** Which Settings section owns each onboarding section the gateway reports. */
export const SECTION_FOR_ONBOARDING: Record<string, SettingsSection> = {
  llm: 'providers',
  router: 'router',
  memory_embedding: 'memory',
  search: 'capabilities',
  image_generation: 'capabilities',
  audio: 'capabilities',
  channels: 'channels',
}

/**
 * The rail's dots: a section is marked when something it owns is switched
 * on but cannot work. Mostly the gateway says so (its onboarding sections);
 * X search has no onboarding section, so it is judged here from the config,
 * the environment and the xAI login (`keys`, when known). Blocking items (no
 * provider) read as danger, the rest as a warning. Optional items that are
 * merely unset or off are not marked. Overview carries the worst of them, so
 * the page Settings opens on says "something here" even when the section
 * that owns it is scrolled out of the rail.
 */
export function sectionAttention(
  snapshot: SettingsSnapshot | undefined,
  keys?: KeyContext,
): Partial<Record<SettingsSection, AttentionTone>> {
  const out: Partial<Record<SettingsSection, AttentionTone>> = {}
  const mark = (section: SettingsSection, tone: AttentionTone) => {
    if (out[section] !== 'danger') out[section] = tone
    if (out.overview !== 'danger') out.overview = tone
  }
  const details = snapshot?.status?.sectionDetails ?? snapshot?.readiness?.sectionDetails ?? {}
  for (const [name, detail] of Object.entries(details)) {
    const section = SECTION_FOR_ONBOARDING[name]
    if (!section || !detail?.actionRequired) continue
    mark(section, detail.blocking ? 'danger' : 'warn')
  }
  if (snapshot && keys && xSearchDef.specs(snapshot).length > 0) {
    if (capabilityStatus(xSearchDef, snapshot, keys).state === 'key') mark('capabilities', 'warn')
  }
  return out
}

export function useSectionAttention(): Partial<Record<SettingsSection, AttentionTone>> {
  const { connected, snapshot } = useConfigSnapshot()
  const keys = useKeyFacts(connected)
  return sectionAttention(snapshot, keys.ctx)
}
