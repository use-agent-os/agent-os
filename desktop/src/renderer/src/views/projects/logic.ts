// Pure helpers for the desktop's project folders and project page. The
// project model itself (RawProject, sorting, id/name accessors) is the web
// console's, imported from @/views/projects/logic; this module owns what is
// specific to the desktop presentation: filing sessions into folders, the
// brief's autosave bookkeeping, and the sidebar's disclosure state.

import { projectId, sessionProjectId, type RawProject } from '@/views/projects/logic'
import type { SessionRow } from '~/stores/sessions'

/** Server-side ceiling on the brief (SessionManager.PROJECT_KNOWLEDGE_MAX_CHARS). */
export const KNOWLEDGE_MAX_CHARS = 24_000
/** Server-side ceiling on the name (SessionManager.PROJECT_NAME_MAX_CHARS). */
export const NAME_MAX_CHARS = 200

/** How many sessions a folder shows before collapsing the rest into "N more". */
export const FOLDER_PREVIEW = 6

/** MIME type carried by a sidebar session row while it is being dragged. */
export const SESSION_DRAG_TYPE = 'application/x-agentos-session'

export interface Filed {
  /** Sessions per project id, newest first. Every project has an entry. */
  byProject: Map<string, SessionRow[]>
  /** Sessions that belong to no project (or to one that no longer exists). */
  unfiled: SessionRow[]
}

/**
 * Split the sidebar rows into per-project folders and the loose list. A
 * session whose project id does not match a known project is treated as
 * unfiled rather than hidden, so nothing disappears if the list is stale.
 */
export function fileSessions(rows: SessionRow[], projects: RawProject[]): Filed {
  const byProject = new Map<string, SessionRow[]>()
  for (const p of projects) byProject.set(projectId(p), [])
  const unfiled: SessionRow[] = []
  for (const row of rows) {
    const pid = sessionProjectId(row.raw)
    const bucket = pid ? byProject.get(pid) : undefined
    if (bucket) bucket.push(row)
    else unfiled.push(row)
  }
  return { byProject, unfiled }
}

/** The rows a folder shows inline, and how many it hides. */
export function folderPreview(
  rows: SessionRow[],
  limit = FOLDER_PREVIEW,
): { shown: SessionRow[]; hidden: number } {
  if (rows.length <= limit) return { shown: rows, hidden: 0 }
  return { shown: rows.slice(0, limit), hidden: rows.length - limit }
}

/** Group a project's sessions by agent id, alphabetical, each newest first. */
export function groupByAgent(rows: SessionRow[]): Array<{ agentId: string; items: SessionRow[] }> {
  const buckets = new Map<string, SessionRow[]>()
  for (const row of rows) {
    const agentId = String(row.raw.agent_id || row.raw.agentId || '') || 'main'
    const bucket = buckets.get(agentId)
    if (bucket) bucket.push(row)
    else buckets.set(agentId, [row])
  }
  return [...buckets.entries()]
    .map(([agentId, items]) => ({
      agentId,
      items: [...items].sort((a, b) => b.updatedAt - a.updatedAt),
    }))
    .sort((a, b) => a.agentId.localeCompare(b.agentId))
}

/** Word and character counts for the brief's footer. */
export function briefStats(text: string): { chars: number; words: number } {
  const chars = text.length
  const words = text.trim() ? text.trim().split(/\s+/).length : 0
  return { chars, words }
}

/** Format a count with thousands separators, locale-independent. */
export function formatCount(n: number): string {
  return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ',')
}

export type SaveState = 'clean' | 'dirty' | 'saving' | 'saved' | 'error'

/**
 * Trim a project name the way the gateway will accept it; '' means invalid.
 * Whitespace-only names are rejected server-side, so they are rejected here
 * before a round trip.
 */
export function normalizeName(raw: string): string {
  return raw.trim().slice(0, NAME_MAX_CHARS)
}

/** True when the brief draft differs from what the server holds. */
export function isDirty(draft: string, saved: string): boolean {
  return draft !== saved
}

/** Reduce a project name to one or two initials for the folder glyph. */
export function initials(name: string): string {
  const words = name.trim().split(/\s+/).filter(Boolean)
  if (words.length === 0) return '?'
  if (words.length === 1) return words[0]!.slice(0, 2).toUpperCase()
  return (words[0]![0]! + words[1]![0]!).toUpperCase()
}

/** A short "when" for the metadata line: today's time, else a date. */
export function briefDate(epochMs: number, now = Date.now()): string {
  if (!epochMs) return ''
  const d = new Date(epochMs)
  const n = new Date(now)
  const sameDay =
    d.getFullYear() === n.getFullYear() &&
    d.getMonth() === n.getMonth() &&
    d.getDate() === n.getDate()
  if (sameDay) {
    return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })
  }
  const sameYear = d.getFullYear() === n.getFullYear()
  return d.toLocaleDateString(
    'en-US',
    sameYear
      ? { month: 'short', day: 'numeric' }
      : { month: 'short', day: 'numeric', year: 'numeric' },
  )
}
