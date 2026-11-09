// Every page in the Pages registry is documented in docs/web-ui.md (task 8.7).
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { t } from '@/i18n'
import { PAGES } from './pages'

describe('docs/web-ui.md Main Areas', () => {
  it('documents every page of the Pages catalog with its route', () => {
    const doc = readFileSync('../docs/web-ui.md', 'utf8')
    const section = doc.slice(doc.indexOf('## Main Areas'), doc.indexOf('## Gateway connection'))
    for (const page of PAGES) {
      expect(section, page.id).toContain(`| ${t(page.titleKey)} | \`${page.path}\` |`)
    }
  })
})
