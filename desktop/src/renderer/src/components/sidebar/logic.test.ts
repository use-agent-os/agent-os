import { describe, expect, it } from 'vitest'
import type { RawProject } from '@/views/projects/logic'
import type { SessionRow } from '~/stores/sessions'
import {
  agentIds,
  DEFAULT_VIEW,
  filterRows,
  hasActiveFilter,
  isDefaultView,
  matchesStatus,
  orderRows,
  rowAgentId,
  sectionRows,
  type SectionLabels,
  type SessionView,
} from './logic'

const DAY = 24 * 60 * 60 * 1000
const NOW = Date.parse('2026-09-12T12:00:00')

function row(
  key: string,
  extra: Partial<SessionRow> & { raw?: SessionRow['raw'] } = {},
): SessionRow {
  return {
    key,
    title: extra.title ?? key,
    updatedAt: extra.updatedAt ?? NOW,
    live: extra.live ?? false,
    raw: { key, ...(extra.raw ?? {}) },
  }
}

const labels: SectionLabels = {
  today: 'Today',
  yesterday: 'Yesterday',
  week: 'This week',
  pinned: 'Pinned',
  others: 'Others',
  noProject: 'No project',
  agent: (id) => id,
}

const none = { pinned: new Set<string>(), archived: new Set<string>(), unread: new Set<string>() }

describe('view defaults', () => {
  it('knows the default view and what counts as a filter', () => {
    expect(isDefaultView(DEFAULT_VIEW)).toBe(true)
    expect(isDefaultView({ ...DEFAULT_VIEW, ordering: 'name' })).toBe(false)
    expect(hasActiveFilter({ ...DEFAULT_VIEW, ordering: 'name' })).toBe(false)
    expect(hasActiveFilter({ ...DEFAULT_VIEW, status: 'running' })).toBe(true)
    expect(hasActiveFilter({ ...DEFAULT_VIEW, archived: true })).toBe(true)
  })
})

describe('rowAgentId', () => {
  it('reads the record, then the key prefix, then main', () => {
    expect(rowAgentId(row('agent:ops:webchat:a', { raw: { agent_id: 'research' } }))).toBe(
      'research',
    )
    expect(rowAgentId(row('agent:ops:webchat:a'))).toBe('ops')
    expect(rowAgentId(row('loose'))).toBe('main')
    expect(agentIds([row('agent:b:x:1'), row('agent:a:x:2'), row('agent:b:x:3')])).toEqual([
      'a',
      'b',
    ])
  })
})

describe('matchesStatus', () => {
  const running = row('r', { live: true, raw: { active_task: { status: 'running' } } })
  const failed = row('f', { raw: { last_task: { status: 'failed' } } })
  const idle = row('i')

  it('splits running, needs-attention and idle', () => {
    expect(matchesStatus(running, 'running')).toBe(true)
    expect(matchesStatus(failed, 'running')).toBe(false)
    expect(matchesStatus(failed, 'attention')).toBe(true)
    expect(matchesStatus(idle, 'attention')).toBe(false)
    expect(matchesStatus(idle, 'idle')).toBe(true)
    expect(matchesStatus(running, 'idle')).toBe(false)
  })

  it('counts a turn streaming in this window as running', () => {
    expect(matchesStatus(idle, 'running', true)).toBe(true)
  })
})

describe('filterRows', () => {
  const rows = [
    row('agent:main:webchat:a', { raw: { project_id: 'p1' } }),
    row('agent:ops:webchat:b'),
    row('agent:main:webchat:c'),
  ]

  it('hides archived rows unless the view shows them', () => {
    const marks = { archived: new Set(['agent:main:webchat:c']) }
    expect(filterRows(rows, DEFAULT_VIEW, marks).map((r) => r.key)).toEqual([
      'agent:main:webchat:a',
      'agent:ops:webchat:b',
    ])
    expect(filterRows(rows, { ...DEFAULT_VIEW, archived: true }, marks)).toHaveLength(3)
  })

  it('narrows by agent and by project, including "no project"', () => {
    expect(filterRows(rows, { ...DEFAULT_VIEW, agent: 'ops' }, none).map((r) => r.key)).toEqual([
      'agent:ops:webchat:b',
    ])
    expect(filterRows(rows, { ...DEFAULT_VIEW, project: 'p1' }, none).map((r) => r.key)).toEqual([
      'agent:main:webchat:a',
    ])
    expect(filterRows(rows, { ...DEFAULT_VIEW, project: '' }, none)).toHaveLength(2)
  })
})

describe('orderRows', () => {
  const a = row('a', { title: 'Zebra', updatedAt: NOW - 2 * DAY })
  const b = row('b', { title: 'apple', updatedAt: NOW })
  const c = row('c', { title: 'Mango', updatedAt: NOW - DAY })

  it('orders by recency, oldest first, or name (case-insensitive)', () => {
    const v = (ordering: SessionView['ordering']) => ({ ordering, inbox: false })
    expect(orderRows([a, b, c], v('recent'), none).map((r) => r.key)).toEqual(['b', 'c', 'a'])
    expect(orderRows([a, b, c], v('oldest'), none).map((r) => r.key)).toEqual(['a', 'c', 'b'])
    expect(orderRows([a, b, c], v('name'), none).map((r) => r.key)).toEqual(['b', 'c', 'a'])
  })

  it('lifts unread rows first in inbox style only', () => {
    const marks = { unread: new Set(['a']) }
    expect(
      orderRows([a, b, c], { ordering: 'recent', inbox: true }, marks).map((r) => r.key),
    ).toEqual(['a', 'b', 'c'])
    expect(
      orderRows([a, b, c], { ordering: 'recent', inbox: false }, marks).map((r) => r.key),
    ).toEqual(['b', 'c', 'a'])
  })
})

describe('sectionRows', () => {
  const today = row('t', { updatedAt: NOW - 1000 })
  const yesterday = row('y', { updatedAt: NOW - DAY })
  const lastMonth = row('m', { updatedAt: NOW - 40 * DAY })

  it('groups by date with an unlabelled Today section on top', () => {
    const s = sectionRows(
      [today, yesterday, lastMonth],
      { grouping: 'date' },
      none,
      labels,
      [],
      NOW,
    )
    expect(s.map((x) => [x.label, x.items.length])).toEqual([
      ['', 1],
      ['Yesterday', 1],
      ['August', 1],
    ])
  })

  it('puts pinned rows in their own first section and captions Today after it', () => {
    const marks = { pinned: new Set(['y']) }
    const s = sectionRows([today, yesterday], { grouping: 'date' }, marks, labels, [], NOW)
    expect(s.map((x) => [x.key, x.label])).toEqual([
      ['pinned', 'Pinned'],
      ['today', 'Today'],
    ])
  })

  it('groups by agent alphabetically and flat with "none"', () => {
    const rows = [row('agent:zed:x:1'), row('agent:amy:x:2'), row('agent:zed:x:3')]
    expect(
      sectionRows(rows, { grouping: 'agent' }, none, labels, [], NOW).map((x) => [
        x.label,
        x.items.length,
      ]),
    ).toEqual([
      ['amy', 1],
      ['zed', 2],
    ])
    const flat = sectionRows(rows, { grouping: 'none' }, none, labels, [], NOW)
    expect(flat).toHaveLength(1)
    expect(flat[0]!.label).toBe('')
    const withPin = sectionRows(
      rows,
      { grouping: 'none' },
      { pinned: new Set(['agent:amy:x:2']) },
      labels,
      [],
      NOW,
    )
    expect(withPin.map((x) => x.label)).toEqual(['Pinned', 'Others'])
  })

  it('groups by project in sidebar order, loose rows last, unknown projects loose', () => {
    const projects: RawProject[] = [
      { project_id: 'p2', name: 'Two' },
      { project_id: 'p1', name: 'One' },
    ]
    const rows = [
      row('a', { raw: { project_id: 'p1' } }),
      row('b'),
      row('c', { raw: { project_id: 'p2' } }),
      row('d', { raw: { project_id: 'gone' } }),
    ]
    const s = sectionRows(rows, { grouping: 'project' }, none, labels, projects, NOW)
    expect(s.map((x) => [x.label, x.items.map((r) => r.key)])).toEqual([
      ['Two', ['c']],
      ['One', ['a']],
      ['No project', ['b', 'd']],
    ])
  })
})
