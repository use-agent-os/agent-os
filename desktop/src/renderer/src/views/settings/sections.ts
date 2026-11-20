/**
 * The settings rail. Two facts per section: its id and the
 * group it sits under. Copy lives in i18n (`settings.section.<id>`).
 */
export const SETTINGS_SECTIONS = [
  'overview',
  'providers',
  'router',
  'memory',
  'capabilities',
  'skills',
  'channels',
  'environment',
  'trading',
  'gateway',
  'appearance',
  'notifications',
  'security',
  'behaviour',
  'shortcuts',
  'advanced',
  'about',
] as const
export type SettingsSection = (typeof SETTINGS_SECTIONS)[number]

export const SETTINGS_GROUPS: readonly {
  /** `start` has no heading: it is the page Settings opens on. */
  id: 'start' | 'agent' | 'app' | 'more'
  sections: SettingsSection[]
}[] = [
  { id: 'start', sections: ['overview'] },
  {
    id: 'agent',
    sections: [
      'providers',
      'router',
      'memory',
      'capabilities',
      'skills',
      'channels',
      'environment',
      'trading',
    ],
  },
  {
    id: 'app',
    sections: ['gateway', 'appearance', 'notifications', 'security', 'behaviour', 'shortcuts'],
  },
  { id: 'more', sections: ['advanced', 'about'] },
]

export const DEFAULT_SECTION: SettingsSection = 'overview'

export function isSettingsSection(value: unknown): value is SettingsSection {
  return typeof value === 'string' && (SETTINGS_SECTIONS as readonly string[]).includes(value)
}

/**
 * Rail search: a section matches on its name or on one of its keywords
 * (`settings.section.<id>.keywords`, space-separated). Empty query keeps all.
 */
export function matchSections(
  query: string,
  describe: (id: SettingsSection) => { name: string; keywords: string },
): SettingsSection[] {
  const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean)
  const all = SETTINGS_GROUPS.flatMap((g) => g.sections)
  if (words.length === 0) return all
  return all.filter((id) => {
    const { name, keywords } = describe(id)
    const hay = `${name} ${keywords}`.toLowerCase()
    return words.every((w) => hay.includes(w))
  })
}
