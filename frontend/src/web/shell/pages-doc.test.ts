// Every page in the Pages registry is documented in docs/web-ui.md (task 8.7).
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { t } from '@/i18n'
import { SESSIONS_LIST_LIMIT } from '@/views/sessions/logic'
import { JOBS_POLL_MS } from '@/web/features/automations/AutomationsPage'
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

describe('docs/web-ui.md page facts', () => {
  const doc = readFileSync('../docs/web-ui.md', 'utf8')

  it('states what Home and Usage figures cover', () => {
    expect(doc).toContain('usage (all time, plus how many sessions were active today)')
    expect(doc).toContain('**Usage** lists the sessions active in the chosen range')
  })

  it('states the session list limit and the Automations poll', () => {
    expect(doc).toContain(`"Showing the newest ${SESSIONS_LIST_LIMIT} of N"`)
    expect(doc).toContain(`re-read every ${JOBS_POLL_MS / 1000} seconds`)
  })

  it('lists what the web console leaves out', () => {
    const start = doc.indexOf('## Not in the web console')
    expect(start).toBeGreaterThanOrEqual(0)
    const section = doc.slice(start, doc.indexOf('\n## ', start + 1))
    for (const item of [
      'read-only',
      'Automations covers scheduled jobs',
      'recent-sessions panel',
    ]) {
      expect(section).toContain(item)
    }
  })
})
