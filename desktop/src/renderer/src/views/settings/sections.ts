/**
 * The settings rail. Two facts per section: its id and the
 * group it sits under. Copy lives in i18n (`settings.section.<id>`).
 */
export const SETTINGS_SECTIONS = [
  'providers',
  'router',
  'skills',
  'gateway',
  'appearance',
  'notifications',
  'behaviour',
  'shortcuts',
  'advanced',
  'about',
] as const
export type SettingsSection = (typeof SETTINGS_SECTIONS)[number]

export const SETTINGS_GROUPS: readonly {
  id: 'agent' | 'app' | 'more'
  sections: SettingsSection[]
}[] = [
  { id: 'agent', sections: ['providers', 'router', 'skills'] },
  { id: 'app', sections: ['gateway', 'appearance', 'notifications', 'behaviour', 'shortcuts'] },
  { id: 'more', sections: ['advanced', 'about'] },
]

export const DEFAULT_SECTION: SettingsSection = 'providers'

export function isSettingsSection(value: unknown): value is SettingsSection {
  return typeof value === 'string' && (SETTINGS_SECTIONS as readonly string[]).includes(value)
}
