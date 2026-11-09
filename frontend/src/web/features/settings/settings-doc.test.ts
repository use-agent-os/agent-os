// docs/web-ui.md's Settings map lists exactly the registry's sections (task 7.15).
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { t } from '@/i18n'
import { SETTINGS_GROUPS, SETTINGS_SECTIONS } from './registry'

describe('docs/web-ui.md Settings map', () => {
  it('documents every settings section, in registry order, under its group', () => {
    const doc = readFileSync('../docs/web-ui.md', 'utf8')
    const section = doc.slice(
      doc.indexOf('## Settings'),
      doc.indexOf('### Configuration write safety'),
    )
    const rows = section
      .split('\n')
      .filter((l) => l.startsWith('| ') && !l.startsWith('| Group') && !l.startsWith('| ---'))
      .map((l) =>
        l
          .split('|')
          .slice(1, 3)
          .map((c) => c.trim()),
      )
    const groupTitle = (id: string) => t(SETTINGS_GROUPS.find((g) => g.id === id)!.titleKey)
    expect(rows).toEqual(SETTINGS_SECTIONS.map((s) => [groupTitle(s.group), t(s.titleKey)]))
  })
})
