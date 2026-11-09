import { describe, expect, it } from 'vitest'
import {
  NO_FILTERS,
  dateBucket,
  groupSessions,
  hasActiveFilters,
  keyLabel,
  matchesFilters,
  movePin,
  nestSessions,
  parsePins,
  reorderPins,
  sessionChannel,
  sessionIsRunning,
  sessionTitle,
  togglePin,
  type SessionRow,
} from './session-list'

const NOW = new Date(2026, 10, 9, 15, 0, 0).getTime()
const HOUR = 3_600_000

function row(key: string, extra: Partial<SessionRow> = {}): SessionRow {
  return { key, updated_at: NOW - HOUR, ...extra }
}

describe('row fields', () => {
  it('prefers display name, then derived title, then a key label', () => {
    expect(
      sessionTitle(
        row('agent:main:webchat:a', {
          display_name: 'Weekly DCA',
          derived_title: 'Set up a DCA for ETH',
        }),
      ),
    ).toBe('Weekly DCA')
    expect(
      sessionTitle(row('agent:main:webchat:a', { derived_title: 'Set up a DCA for ETH' })),
    ).toBe('Set up a DCA for ETH')
    expect(sessionTitle(row('agent:main:webchat:abc'))).toBe('abc')
    expect(keyLabel('agent:main:telegram:dm:42')).toBe('telegram · dm · 42')
    expect(keyLabel('cron:job')).toBe('cron · job')
  })

  it('reads the channel from the row, falling back to the key', () => {
    expect(sessionChannel(row('agent:main:webchat:a', { channel: 'Telegram' }))).toBe('telegram')
    expect(sessionChannel(row('agent:main:discord:x'))).toBe('discord')
  })

  it('shows the running indicator only for queued or running tasks', () => {
    expect(sessionIsRunning(row('k', { active_task: { status: 'running' } }))).toBe(true)
    expect(sessionIsRunning(row('k', { active_task: { status: 'queued' } }))).toBe(true)
    expect(
      sessionIsRunning(row('k', { active_task: null, last_task: { status: 'succeeded' } })),
    ).toBe(false)
  })
})

describe('filters', () => {
  const rows = [
    row('agent:main:webchat:a', { display_name: 'Weekly DCA' }),
    row('agent:main:webchat:b', { derived_title: 'Plan the dca ladder' }),
    row('agent:research:webchat:dca-key'),
    row('agent:research:telegram:x', { project_id: 'p1' }),
  ]

  it('matches title, derived title or key, case-insensitively', () => {
    const f = { ...NO_FILTERS, text: 'DCA' }
    expect(rows.filter((r) => matchesFilters(r, f)).map((r) => r.key)).toEqual([
      'agent:main:webchat:a',
      'agent:main:webchat:b',
      'agent:research:webchat:dca-key',
    ])
  })

  it('filters by agent, channel and project', () => {
    expect(
      rows.filter((r) => matchesFilters(r, { ...NO_FILTERS, agent: 'research' })),
    ).toHaveLength(2)
    expect(
      rows.filter((r) => matchesFilters(r, { ...NO_FILTERS, channel: 'telegram' })),
    ).toHaveLength(1)
    expect(rows.filter((r) => matchesFilters(r, { ...NO_FILTERS, project: 'p1' }))).toHaveLength(1)
    expect(hasActiveFilters(NO_FILTERS)).toBe(false)
    expect(hasActiveFilters({ ...NO_FILTERS, channel: 'x' })).toBe(true)
  })
})

describe('nesting', () => {
  it('nests a child under its listed parent with counts', () => {
    const [a] = nestSessions([
      row('A'),
      row('B', { parentSessionKey: 'A', active_task: { status: 'running' } }),
    ])
    expect(a!.row.key).toBe('A')
    expect(a!.childCount).toBe(1)
    expect(a!.childRunning).toBe(true)
    expect(a!.children.map((c) => c.row.key)).toEqual(['B'])
  })

  it('keeps a child at the top level when its parent is not listed', () => {
    expect(nestSessions([row('B', { parent_session_key: 'gone' })]).map((n) => n.row.key)).toEqual([
      'B',
    ])
  })

  it('counts grandchildren and survives parent cycles', () => {
    const [a] = nestSessions([
      row('A'),
      row('B', { parentSessionKey: 'A' }),
      row('C', { parentSessionKey: 'B' }),
    ])
    expect(a!.childCount).toBe(2)
    const cyclic = nestSessions([
      row('X', { parentSessionKey: 'Y' }),
      row('Y', { parentSessionKey: 'X' }),
    ])
    expect(cyclic.length).toBeGreaterThanOrEqual(1)
    const total = (nodes: typeof cyclic): number =>
      nodes.reduce((n, node) => n + 1 + total(node.children), 0)
    expect(total(cyclic)).toBe(2)
  })
})

describe('grouping', () => {
  it('groups by date: Today before Older, empty sections hidden', () => {
    const nodes = nestSessions([
      row('t', { updated_at: NOW - HOUR }),
      row('o', { updated_at: NOW - 40 * 24 * HOUR }),
    ])
    expect(groupSessions(nodes, { grouping: 'date', pins: [], now: NOW }).map((s) => s.id)).toEqual(
      ['today', 'older'],
    )
  })

  it('places yesterday and the previous seven days', () => {
    expect(dateBucket(NOW - 20 * HOUR, NOW)).toBe('yesterday')
    expect(dateBucket(NOW - 4 * 24 * HOUR, NOW)).toBe('previous7')
  })

  it('groups by project with No project last', () => {
    const nodes = nestSessions([
      row('a', { project_id: 'p2' }),
      row('b', { project_id: 'p1' }),
      row('c'),
    ])
    const sections = groupSessions(nodes, {
      grouping: 'project',
      pins: [],
      projects: new Map([
        ['p1', 'Zeta'],
        ['p2', 'Alpha'],
      ]),
    })
    expect(sections.map((s) => [s.id, s.label])).toEqual([
      ['project:p2', 'Alpha'],
      ['project:p1', 'Zeta'],
      ['no-project', undefined],
    ])
  })

  it('puts pins first in the user order and hides missing pins', () => {
    const nodes = nestSessions([row('A'), row('B'), row('C')])
    const sections = groupSessions(nodes, {
      grouping: 'date',
      pins: ['B', 'missing', 'A'],
      now: NOW,
    })
    expect(sections[0]!.id).toBe('pinned')
    expect(sections[0]!.nodes.map((n) => n.row.key)).toEqual(['B', 'A'])
    expect(sections[1]!.nodes.map((n) => n.row.key)).toEqual(['C'])
  })
})

describe('pins', () => {
  it('pins A then B and moves B above A', () => {
    let pins = togglePin(togglePin([], 'A'), 'B')
    expect(pins).toEqual(['A', 'B'])
    pins = movePin(pins, 'B', -1)
    expect(pins).toEqual(['B', 'A'])
    expect(movePin(pins, 'B', -1)).toEqual(['B', 'A'])
    expect(reorderPins(['a', 'b', 'c'], 0, 2)).toEqual(['b', 'c', 'a'])
    expect(togglePin(pins, 'A')).toEqual(['B'])
  })

  it('parses stored pins defensively', () => {
    expect(parsePins('["a",1,"b"]')).toEqual(['a', 'b'])
    expect(parsePins('{')).toEqual([])
    expect(parsePins(null)).toEqual([])
  })
})
