// Pure Sessions-list derivations (overhaul-control-ui D6, shell spec "Session
// list rows", "Child sessions nest", "Session grouping", "Session filters",
// "Pinned sessions"). Rendering and RPC live in SessionList.tsx.
import { sessionRunStatus, type RawSession } from '@/views/sessions/logic'
import type { SessionFilters } from './session-filters'
import { agentOfSessionKey } from './session-url'

export { NO_FILTERS, hasActiveFilters, type SessionFilters } from './session-filters'

export interface SessionRow extends RawSession {
  key: string
  channel?: string | null
  last_channel?: string | null
  lastChannel?: string | null
  parent_session_key?: string | null
  parentSessionKey?: string | null
  updatedAt?: string | number
  /** Set on a row this tab created that the gateway has not listed yet. */
  provisional?: boolean
}

// ── Row fields ───────────────────────────────────────────────────────────────

/** A readable label from the key alone: `agent:main:webchat:abc` -> `abc`. */
export function keyLabel(key: string): string {
  const parts = key.split(':')
  const rest = parts[0] === 'agent' ? parts.slice(2) : parts
  if (rest[0] === 'webchat' && rest.length > 1) rest.shift()
  return rest.join(' · ') || key
}

/** Title precedence: display name, then derived title, then the key label. */
export function sessionTitle(row: SessionRow): string {
  const display = String(row.display_name || row.displayName || '').trim()
  if (display) return display
  const derived = String(row.derived_title || row.derivedTitle || '').trim()
  if (derived) return derived
  return keyLabel(row.key)
}

export function sessionAgent(row: SessionRow): string {
  return String(row.agent_id || row.agentId || agentOfSessionKey(row.key) || '')
}

/** The source channel: the row's channel, else the key's channel segment. */
export function sessionChannel(row: SessionRow): string {
  const explicit = row.channel || row.last_channel || row.lastChannel
  if (explicit) return String(explicit).toLowerCase()
  const parts = row.key.split(':')
  const segment = parts[0] === 'agent' ? parts[2] : parts[0]
  return (segment || 'webchat').toLowerCase()
}

export function sessionProject(row: SessionRow): string | null {
  const project = row.project_id ?? row.projectId
  return project ? String(project) : null
}

export function sessionParent(row: SessionRow): string | null {
  const parent = row.parent_session_key ?? row.parentSessionKey
  return parent ? String(parent) : null
}

/** Updated time in epoch ms (sessions.list mixes seconds and ms). */
export function sessionUpdatedMs(row: SessionRow): number {
  const raw = row.updated_at ?? row.updatedAt
  const numeric = typeof raw === 'number' ? raw : raw ? Number(raw) : NaN
  if (Number.isFinite(numeric)) return Math.abs(numeric) < 10_000_000_000 ? numeric * 1000 : numeric
  const parsed = raw ? Date.parse(String(raw)) : NaN
  return Number.isFinite(parsed) ? parsed : 0
}

/** Running indicator, from the row's task-state summary. */
export function sessionIsRunning(row: SessionRow): boolean {
  const status = sessionRunStatus(row)
  return status === 'queued' || status === 'running'
}

// ── Filters ──────────────────────────────────────────────────────────────────

export function matchesFilters(row: SessionRow, filters: SessionFilters): boolean {
  const q = filters.text.trim().toLowerCase()
  if (q) {
    const haystack = [
      sessionTitle(row),
      String(row.derived_title || row.derivedTitle || ''),
      row.key,
    ]
    if (!haystack.some((value) => value.toLowerCase().includes(q))) return false
  }
  if (filters.agent && sessionAgent(row) !== filters.agent) return false
  if (filters.channel && sessionChannel(row) !== filters.channel) return false
  if (filters.project && sessionProject(row) !== filters.project) return false
  return true
}

// ── Nesting ──────────────────────────────────────────────────────────────────

export interface SessionNode {
  row: SessionRow
  children: SessionNode[]
  /** Descendants, at any depth. */
  childCount: number
  /** Whether any descendant is running. */
  childRunning: boolean
}

/** Nest each row under its listed parent; rows without one are roots. */
export function nestSessions(rows: readonly SessionRow[]): SessionNode[] {
  const byKey = new Map<string, SessionNode>()
  for (const row of rows) {
    byKey.set(row.key, { row, children: [], childCount: 0, childRunning: false })
  }
  const roots: SessionNode[] = []
  for (const node of byKey.values()) {
    const parentKey = sessionParent(node.row)
    const parent = parentKey && parentKey !== node.row.key ? byKey.get(parentKey) : undefined
    if (parent && !isAncestor(node, parent, byKey)) parent.children.push(node)
    else roots.push(node)
  }
  const finish = (node: SessionNode): void => {
    node.children.sort(byUpdatedDesc)
    for (const child of node.children) finish(child)
    node.childCount = node.children.reduce((n, c) => n + 1 + c.childCount, 0)
    node.childRunning = node.children.some((c) => sessionIsRunning(c.row) || c.childRunning)
  }
  for (const root of roots) finish(root)
  return roots.sort(byUpdatedDesc)
}

/** Guards parent cycles: is `node` an ancestor of `candidate`? */
function isAncestor(
  node: SessionNode,
  candidate: SessionNode,
  byKey: Map<string, SessionNode>,
): boolean {
  let current: SessionNode | undefined = candidate
  const seen = new Set<string>()
  while (current) {
    if (current === node) return true
    if (seen.has(current.row.key)) return true
    seen.add(current.row.key)
    const parentKey = sessionParent(current.row)
    current = parentKey ? byKey.get(parentKey) : undefined
  }
  return false
}

function byUpdatedDesc(a: SessionNode, b: SessionNode): number {
  return sessionUpdatedMs(b.row) - sessionUpdatedMs(a.row)
}

// ── Pins ─────────────────────────────────────────────────────────────────────

export const PINS_KEY = 'agentos.ui.pinnedSessions'

export function togglePin(pins: readonly string[], key: string): string[] {
  return pins.includes(key) ? pins.filter((k) => k !== key) : [...pins, key]
}

/** Move a pinned key by `delta` places (keyboard reordering). */
export function movePin(pins: readonly string[], key: string, delta: number): string[] {
  const from = pins.indexOf(key)
  if (from < 0) return [...pins]
  const to = Math.max(0, Math.min(pins.length - 1, from + delta))
  return reorderPins(pins, from, to)
}

/** Move the pin at `from` to index `to` (drag reordering). */
export function reorderPins(pins: readonly string[], from: number, to: number): string[] {
  const next = [...pins]
  const [moved] = next.splice(from, 1)
  if (moved === undefined) return [...pins]
  next.splice(Math.max(0, Math.min(next.length, to)), 0, moved)
  return next
}

export function parsePins(raw: string | null): string[] {
  if (!raw) return []
  try {
    const parsed: unknown = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed.filter((k): k is string => typeof k === 'string') : []
  } catch {
    return []
  }
}

// ── Grouping ─────────────────────────────────────────────────────────────────

export type SessionGrouping = 'date' | 'project'
export const GROUPING_KEY = 'agentos.ui.sessionGrouping'

export type SectionId = 'pinned' | 'today' | 'yesterday' | 'previous7' | 'older' | 'no-project'

export interface SessionSection {
  /** A fixed section id, or `project:<id>`. */
  id: SectionId | `project:${string}`
  /** Project name for project sections; fixed sections are labelled by id. */
  label?: string
  nodes: SessionNode[]
}

const DAY_MS = 86_400_000

function startOfDay(ms: number): number {
  const d = new Date(ms)
  d.setHours(0, 0, 0, 0)
  return d.getTime()
}

export function dateBucket(updatedMs: number, now: number): SectionId {
  const today = startOfDay(now)
  if (updatedMs >= today) return 'today'
  if (updatedMs >= today - DAY_MS) return 'yesterday'
  if (updatedMs >= today - 7 * DAY_MS) return 'previous7'
  return 'older'
}

export interface GroupOptions {
  grouping: SessionGrouping
  pins: readonly string[]
  /** Project id -> name, for project sections. */
  projects?: ReadonlyMap<string, string>
  now?: number
}

/**
 * Sections in display order, empty ones dropped. Pinned roots come first in
 * the user's order (pins naming a missing session are skipped, not removed);
 * every other root lands in exactly one section.
 */
export function groupSessions(
  nodes: readonly SessionNode[],
  options: GroupOptions,
): SessionSection[] {
  const now = options.now ?? Date.now()
  const byKey = new Map(nodes.map((n) => [n.row.key, n]))
  const pinned = options.pins.map((k) => byKey.get(k)).filter((n): n is SessionNode => !!n)
  const pinnedKeys = new Set(pinned.map((n) => n.row.key))
  const rest = nodes.filter((n) => !pinnedKeys.has(n.row.key))
  const sections: SessionSection[] = [{ id: 'pinned', nodes: pinned }]

  if (options.grouping === 'date') {
    const buckets: Record<string, SessionNode[]> = {
      today: [],
      yesterday: [],
      previous7: [],
      older: [],
    }
    for (const node of rest) buckets[dateBucket(sessionUpdatedMs(node.row), now)]!.push(node)
    for (const id of ['today', 'yesterday', 'previous7', 'older'] as const) {
      sections.push({ id, nodes: buckets[id]! })
    }
  } else {
    const byProject = new Map<string, SessionNode[]>()
    const none: SessionNode[] = []
    for (const node of rest) {
      const project = sessionProject(node.row)
      if (!project) none.push(node)
      else byProject.set(project, [...(byProject.get(project) ?? []), node])
    }
    const name = (id: string) => options.projects?.get(id) ?? id
    const projectIds = [...byProject.keys()].sort((a, b) => name(a).localeCompare(name(b)))
    for (const id of projectIds) {
      sections.push({ id: `project:${id}`, label: name(id), nodes: byProject.get(id)! })
    }
    sections.push({ id: 'no-project', nodes: none })
  }
  return sections.filter((s) => s.nodes.length > 0)
}

/** Distinct values for the filter menu, from the loaded rows. */
export function filterOptions(rows: readonly SessionRow[]): {
  agents: string[]
  channels: string[]
  projects: string[]
} {
  const agents = new Set<string>()
  const channels = new Set<string>()
  const projects = new Set<string>()
  for (const row of rows) {
    const agent = sessionAgent(row)
    if (agent) agents.add(agent)
    channels.add(sessionChannel(row))
    const project = sessionProject(row)
    if (project) projects.add(project)
  }
  const sorted = (s: Set<string>) => [...s].sort()
  return { agents: sorted(agents), channels: sorted(channels), projects: sorted(projects) }
}
