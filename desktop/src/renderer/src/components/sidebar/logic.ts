// Pure helpers for the sidebar's session list: how the view menu's choices
// (grouping, ordering, filters) and the local marks (pinned, archived,
// unread) turn the gateway's rows into the sections on screen.

import { projectId, projectName, sessionProjectId, type RawProject } from '@/views/projects/logic'
import { agentIdFromKey, sessionRunStatus } from '@/views/sessions/logic'
import { dateGroup, groupKey } from '~/lib/relative-time'
import type { SessionMarks } from '~/stores/session-marks'
import type { SessionRow } from '~/stores/sessions'

export type Grouping = 'date' | 'agent' | 'project' | 'none'
export type Ordering = 'recent' | 'oldest' | 'name'
/** `attention`: the last run failed, timed out or was cut short. */
export type StatusFilter = 'all' | 'running' | 'attention' | 'idle'

export interface SessionView {
  grouping: Grouping
  ordering: Ordering
  /** Project folders above the list (only meaningful when grouping is not by project). */
  folders: boolean
  /** The age column on each row. */
  ages: boolean
  /** Mail-style: unread rows first in every section. */
  inbox: boolean
  status: StatusFilter
  /** An agent id, or null for every agent. */
  agent: string | null
  /** A project id, '' for "not in a project", or null for every project. */
  project: string | null
  /** Show archived sessions too. */
  archived: boolean
}

export const DEFAULT_VIEW: SessionView = {
  grouping: 'date',
  ordering: 'recent',
  folders: true,
  ages: true,
  inbox: false,
  status: 'all',
  agent: null,
  project: null,
  archived: false,
}

export const GROUPINGS: readonly Grouping[] = ['date', 'agent', 'project', 'none']
export const ORDERINGS: readonly Ordering[] = ['recent', 'oldest', 'name']
export const STATUS_FILTERS: readonly StatusFilter[] = ['all', 'running', 'attention', 'idle']

export function isDefaultView(v: SessionView): boolean {
  return (Object.keys(DEFAULT_VIEW) as (keyof SessionView)[]).every((k) => v[k] === DEFAULT_VIEW[k])
}

/** A filter is narrowing the list (as opposed to only arranging it). */
export function hasActiveFilter(v: SessionView): boolean {
  return v.status !== 'all' || v.agent !== null || v.project !== null || v.archived
}

/** The agent a row belongs to: the record's field, else the key's prefix, else main. */
export function rowAgentId(row: SessionRow): string {
  return String(row.raw.agent_id || row.raw.agentId || '') || agentIdFromKey(row.key) || 'main'
}

const ATTENTION = new Set(['failed', 'timeout', 'cancelled', 'interrupted'])

export function matchesStatus(row: SessionRow, status: StatusFilter, liveLocally = false): boolean {
  if (status === 'all') return true
  const run = sessionRunStatus(row.raw)
  const live = row.live || liveLocally
  switch (status) {
    case 'running':
      return live
    case 'attention':
      return !live && ATTENTION.has(run)
    case 'idle':
      return !live && !ATTENTION.has(run)
  }
}

/**
 * The rows the view lets through. Archived rows are out unless asked for;
 * the status, agent and project filters each narrow further.
 */
export function filterRows(
  rows: SessionRow[],
  view: SessionView,
  marks: Pick<SessionMarks, 'archived'>,
  liveIds: ReadonlySet<string> = new Set(),
): SessionRow[] {
  return rows.filter((row) => {
    if (!view.archived && marks.archived.has(row.key)) return false
    if (!matchesStatus(row, view.status, liveIds.has(row.key))) return false
    if (view.agent !== null && rowAgentId(row) !== view.agent) return false
    if (view.project !== null && sessionProjectId(row.raw) !== view.project) return false
    return true
  })
}

/** Sort a section's rows: the ordering, then unread first when inbox-style. */
export function orderRows(
  rows: SessionRow[],
  view: Pick<SessionView, 'ordering' | 'inbox'>,
  marks: Pick<SessionMarks, 'unread'>,
): SessionRow[] {
  const byOrder = (a: SessionRow, b: SessionRow): number => {
    switch (view.ordering) {
      case 'recent':
        return b.updatedAt - a.updatedAt
      case 'oldest':
        return a.updatedAt - b.updatedAt
      case 'name':
        return a.title.localeCompare(b.title, undefined, { sensitivity: 'base' })
    }
  }
  return [...rows].sort((a, b) => {
    if (view.inbox) {
      const ua = marks.unread.has(a.key) ? 0 : 1
      const ub = marks.unread.has(b.key) ? 0 : 1
      if (ua !== ub) return ua - ub
    }
    return byOrder(a, b)
  })
}

export interface Section {
  key: string
  /** Empty for the one unlabelled section (e.g. today's rows at the top). */
  label: string
  items: SessionRow[]
}

export interface SectionLabels {
  today: string
  yesterday: string
  week: string
  pinned: string
  /** The unpinned rows when nothing groups them and a pinned section sits above. */
  others: string
  noProject: string
  agent: (id: string) => string
}

/**
 * Split ordered rows into labelled sections. Pinned rows always form the
 * first section. By date the first section is unlabelled when it is
 * today's, as the list has always shown; a month carries its own name.
 */
export function sectionRows(
  rows: SessionRow[],
  view: Pick<SessionView, 'grouping'>,
  marks: Pick<SessionMarks, 'pinned'>,
  labels: SectionLabels,
  projects: RawProject[] = [],
  now = Date.now(),
): Section[] {
  const out: Section[] = []
  const pinned = rows.filter((r) => marks.pinned.has(r.key))
  const rest = rows.filter((r) => !marks.pinned.has(r.key))
  if (pinned.length > 0) out.push({ key: 'pinned', label: labels.pinned, items: pinned })

  if (view.grouping === 'none') {
    if (rest.length > 0) {
      out.push({ key: 'all', label: pinned.length ? labels.others : '', items: rest })
    }
    return out
  }

  const sections = new Map<string, Section>()
  const push = (key: string, label: string, row: SessionRow) => {
    const s = sections.get(key) ?? { key, label, items: [] }
    s.items.push(row)
    sections.set(key, s)
  }
  if (view.grouping === 'date') {
    for (const row of rest) {
      const g = row.updatedAt ? dateGroup(row.updatedAt, now) : { kind: 'today' as const }
      const label =
        g.kind === 'today'
          ? labels.today
          : g.kind === 'yesterday'
            ? labels.yesterday
            : g.kind === 'week'
              ? labels.week
              : g.label
      push(groupKey(g), label, row)
    }
    const list = [...sections.values()]
    // Today's section at the top needs no caption; the rows speak for it.
    if (pinned.length === 0 && list[0]?.key === 'today') list[0] = { ...list[0], label: '' }
    return out.concat(list)
  }
  if (view.grouping === 'agent') {
    for (const row of rest) {
      const id = rowAgentId(row)
      push(`agent:${id}`, labels.agent(id), row)
    }
    return out.concat([...sections.values()].sort((a, b) => a.label.localeCompare(b.label)))
  }
  // By project: known projects in their sidebar order, then the loose rows.
  const names = new Map(projects.map((p) => [projectId(p), projectName(p)]))
  for (const row of rest) {
    const pid = sessionProjectId(row.raw)
    const name = pid ? names.get(pid) : undefined
    if (pid && name !== undefined) push(`project:${pid}`, name, row)
    else push('project:', labels.noProject, row)
  }
  const ordered: Section[] = []
  for (const p of projects) {
    const s = sections.get(`project:${projectId(p)}`)
    if (s) ordered.push(s)
  }
  const loose = sections.get('project:')
  if (loose) ordered.push(loose)
  return out.concat(ordered)
}

/** Distinct agent ids across the rows, alphabetical, for the Agent filter. */
export function agentIds(rows: SessionRow[]): string[] {
  return [...new Set(rows.map(rowAgentId))].sort((a, b) => a.localeCompare(b))
}

/** How many rows the filters hide, for the "n hidden" hint under the list. */
export function hiddenCount(all: SessionRow[], shown: SessionRow[]): number {
  return Math.max(0, all.length - shown.length)
}
