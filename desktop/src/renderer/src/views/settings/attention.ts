import type { SettingsSnapshot } from '@/views/settings/snapshot'
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
 * The rail's dots: a section is marked when the gateway says something it
 * owns needs action. Blocking items (no provider) read as danger, the rest as
 * a warning. Optional items that are merely unset are not marked.
 */
export function sectionAttention(
  snapshot: SettingsSnapshot | undefined,
): Partial<Record<SettingsSection, AttentionTone>> {
  const out: Partial<Record<SettingsSection, AttentionTone>> = {}
  const details = snapshot?.status?.sectionDetails ?? snapshot?.readiness?.sectionDetails ?? {}
  for (const [name, detail] of Object.entries(details)) {
    const section = SECTION_FOR_ONBOARDING[name]
    if (!section || !detail?.actionRequired) continue
    const tone: AttentionTone = detail.blocking ? 'danger' : 'warn'
    if (out[section] !== 'danger') out[section] = tone
  }
  return out
}

export function useSectionAttention(): Partial<Record<SettingsSection, AttentionTone>> {
  const { snapshot } = useConfigSnapshot()
  return sectionAttention(snapshot)
}
