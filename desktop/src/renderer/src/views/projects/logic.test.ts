import { describe, expect, it } from 'vitest'
import type { SessionRow } from '~/stores/sessions'
import {
  briefDate,
  briefStats,
  fileSessions,
  folderPreview,
  formatCount,
  groupByAgent,
  initials,
  isDirty,
  normalizeName,
} from './logic'

function row(key: string, extra: Record<string, unknown> = {}, updatedAt = 0): SessionRow {
  return { key, title: key, updatedAt, live: false, raw: { key, ...extra } }
}

describe('fileSessions', () => {
  const projects = [{ project_id: 'p1' }, { projectId: 'p2' }]

  it('files sessions under their project and leaves the rest loose', () => {
    const rows = [
      row('a', { project_id: 'p1' }),
      row('b', { projectId: 'p2' }),
      row('c'),
      row('d', { project_id: null }),
    ]
    const filed = fileSessions(rows, projects)
    expect(filed.byProject.get('p1')?.map((r) => r.key)).toEqual(['a'])
    expect(filed.byProject.get('p2')?.map((r) => r.key)).toEqual(['b'])
    expect(filed.unfiled.map((r) => r.key)).toEqual(['c', 'd'])
  })

  it('gives every project an entry even when empty', () => {
    const filed = fileSessions([], projects)
    expect(filed.byProject.get('p1')).toEqual([])
    expect(filed.byProject.get('p2')).toEqual([])
  })

  it('treats a session in an unknown project as unfiled rather than hiding it', () => {
    const filed = fileSessions([row('x', { project_id: 'gone' })], projects)
    expect(filed.unfiled.map((r) => r.key)).toEqual(['x'])
  })
})

describe('folderPreview', () => {
  it('shows everything under the limit', () => {
    const rows = [row('a'), row('b')]
    expect(folderPreview(rows, 6)).toEqual({ shown: rows, hidden: 0 })
  })
  it('caps at the limit and counts the rest', () => {
    const rows = Array.from({ length: 9 }, (_, i) => row(`s${i}`))
    const { shown, hidden } = folderPreview(rows, 6)
    expect(shown).toHaveLength(6)
    expect(hidden).toBe(3)
  })
})

describe('groupByAgent', () => {
  it('buckets by agent alphabetically, newest first inside each', () => {
    const rows = [
      row('a1', { agent_id: 'zeta' }, 10),
      row('a2', { agentId: 'alpha' }, 5),
      row('a3', { agent_id: 'alpha' }, 50),
      row('a4', {}, 1),
    ]
    const groups = groupByAgent(rows)
    expect(groups.map((g) => g.agentId)).toEqual(['alpha', 'main', 'zeta'])
    expect(groups[0]?.items.map((r) => r.key)).toEqual(['a3', 'a2'])
  })
})

describe('briefStats / formatCount', () => {
  it('counts words and characters', () => {
    expect(briefStats('')).toEqual({ chars: 0, words: 0 })
    expect(briefStats('  hello   world \n again ')).toEqual({ chars: 24, words: 3 })
  })
  it('separates thousands', () => {
    expect(formatCount(0)).toBe('0')
    expect(formatCount(999)).toBe('999')
    expect(formatCount(24000)).toBe('24,000')
    expect(formatCount(1234567)).toBe('1,234,567')
  })
})

describe('normalizeName / isDirty', () => {
  it('trims and rejects whitespace-only names', () => {
    expect(normalizeName('  Token launch ')).toBe('Token launch')
    expect(normalizeName('   ')).toBe('')
  })
  it('caps at the server limit', () => {
    expect(normalizeName('x'.repeat(300))).toHaveLength(200)
  })
  it('dirty is a plain inequality', () => {
    expect(isDirty('a', 'a')).toBe(false)
    expect(isDirty('a', 'b')).toBe(true)
  })
})

describe('initials', () => {
  it('takes one letter from each of the first two words', () => {
    expect(initials('Token launch research')).toBe('TL')
  })
  it('takes two letters from a single word', () => {
    expect(initials('robinhood')).toBe('RO')
  })
  it('falls back for an empty name', () => {
    expect(initials('   ')).toBe('?')
  })
})

describe('briefDate', () => {
  const now = new Date(2026, 8, 10, 15, 0).getTime()
  it('shows a time for today', () => {
    const at = new Date(2026, 8, 10, 9, 5).getTime()
    expect(briefDate(at, now)).toMatch(/9:05/)
  })
  it('shows month and day within the year', () => {
    const at = new Date(2026, 0, 3).getTime()
    expect(briefDate(at, now)).toBe('Jan 3')
  })
  it('adds the year outside it', () => {
    const at = new Date(2025, 11, 25).getTime()
    expect(briefDate(at, now)).toBe('Dec 25, 2025')
  })
  it('is empty for no timestamp', () => {
    expect(briefDate(0, now)).toBe('')
  })
})
