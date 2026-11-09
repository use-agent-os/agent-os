import { t } from '@/i18n'
import '@/i18n/en/webSettings'
import { ALL_SETTINGS_SECTIONS, type SettingsSectionMeta } from './registry'
import type { RegisteredField } from './fields'

export interface SettingsSearchResult {
  section: SettingsSectionMeta
  /** A field inside the section, when the match is a field label. */
  field?: RegisteredField
  label: string
}

/** Match section names, descriptions, keywords and registered field labels. */
export function searchSettings(
  query: string,
  fields: Record<string, RegisteredField[]>,
): SettingsSearchResult[] {
  const q = query.trim().toLowerCase()
  if (!q) return []
  const results: SettingsSearchResult[] = []
  for (const section of ALL_SETTINGS_SECTIONS) {
    const title = t(section.titleKey)
    const haystack = [title, t(section.descriptionKey), ...section.keywords].join(' ').toLowerCase()
    if (haystack.includes(q)) results.push({ section, label: title })
  }
  for (const section of ALL_SETTINGS_SECTIONS) {
    for (const field of fields[section.id] ?? []) {
      if (field.label.toLowerCase().includes(q) || field.id.toLowerCase().includes(q)) {
        results.push({
          section,
          field,
          label: t('webSettings.searchField', { field: field.label, section: t(section.titleKey) }),
        })
      }
    }
  }
  return results.slice(0, 30)
}
