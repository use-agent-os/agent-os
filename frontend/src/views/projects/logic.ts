// Pure projects helpers. RPC calls, mutations, dialogs and rendering live in
// the callers (the web console's Projects page, the desktop's sidebar and
// project view); this module owns the pure derivations (sorting,
// filtering, per-project session grouping). Rows tolerate both snake_case and
// camelCase field variants, mirroring RawSession in views/sessions/logic.ts.

import type { RawSession } from '@/views/sessions/logic'

/** A raw project row from projects.list / projects.get. */
export interface RawProject {
  project_id?: string
  projectId?: string
  agent_id?: string
  agentId?: string
  name?: string
  knowledge?: string
  created_at?: number
  createdAt?: number
  updated_at?: number
  updatedAt?: number
  session_count?: number
  sessionCount?: number
  [key: string]: unknown
}

/** The canonical id of a project row ('' when absent). */
export function projectId(p: RawProject): string {
  return String(p.project_id || p.projectId || '')
}

/** Display name of a project row, falling back to a short id. */
export function projectName(p: RawProject): string {
  const name = String(p.name || '')
  if (name) return name
  return projectId(p).slice(0, 8)
}

/** The owning agent id of a project row. */
export function projectAgentId(p: RawProject): string {
  return String(p.agent_id || p.agentId || '')
}

/** The session count of a project row (0 when absent). */
export function projectSessionCount(p: RawProject): number {
  return Number(p.session_count ?? p.sessionCount ?? 0) || 0
}

/** The project id a session row belongs to ('' when project-less). */
export function sessionProjectId(s: RawSession): string {
  return String(s.project_id || s.projectId || '')
}

/** Sort a copy of the projects by updated_at desc, name as tiebreak. */
export function sortProjects(projects: RawProject[]): RawProject[] {
  return [...projects].sort((a, b) => {
    const ua = Number(a.updated_at ?? a.updatedAt ?? 0) || 0
    const ub = Number(b.updated_at ?? b.updatedAt ?? 0) || 0
    if (ua !== ub) return ub - ua
    return projectName(a).toLowerCase() < projectName(b).toLowerCase() ? -1 : 1
  })
}

/** Filter projects by a lowercased query across name and knowledge. */
export function filterProjects(projects: RawProject[], query: string): RawProject[] {
  const q = query.trim().toLowerCase()
  if (!q) return [...projects]
  return projects.filter(
    (p) =>
      projectName(p).toLowerCase().includes(q) ||
      String(p.knowledge || '')
        .toLowerCase()
        .includes(q),
  )
}

/** Sessions belonging to a given project, newest first. */
export function sessionsInProject(sessions: RawSession[], id: string): RawSession[] {
  return sessions
    .filter((s) => sessionProjectId(s) === id)
    .sort((a, b) => (Number(b.updated_at ?? 0) || 0) - (Number(a.updated_at ?? 0) || 0))
}

/** Group a project's sessions by their agent id (alphabetical), each bucket
 *  newest first. Projects are cross-agent, so the detail panel renders the
 *  Project → Agents → Sessions tree from this. */
export function groupProjectSessionsByAgent(
  sessions: RawSession[],
): Array<{ agentId: string; items: RawSession[] }> {
  const buckets = new Map<string, RawSession[]>()
  for (const s of sessions) {
    const agentId = String(s.agent_id || s.agentId || '') || 'main'
    const bucket = buckets.get(agentId)
    if (bucket) bucket.push(s)
    else buckets.set(agentId, [s])
  }
  return [...buckets.entries()]
    .map(([agentId, items]) => ({
      agentId,
      items: [...items].sort(
        (a, b) => (Number(b.updated_at ?? 0) || 0) - (Number(a.updated_at ?? 0) || 0),
      ),
    }))
    .sort((a, b) => a.agentId.localeCompare(b.agentId))
}

/** Filter sessions by project: 'all' passes everything, 'none' passes
 *  project-less rows, any other value matches that project id. */
export function filterSessionsByProject(
  sessions: RawSession[],
  projectFilter: string,
): RawSession[] {
  if (!projectFilter || projectFilter === 'all') return [...sessions]
  if (projectFilter === 'none') return sessions.filter((s) => !sessionProjectId(s))
  return sessions.filter((s) => sessionProjectId(s) === projectFilter)
}

/** A single-line excerpt of the knowledge text for list rows. */
export function knowledgeExcerpt(text: string, maxChars = 96): string {
  const flattened = text.split(/\s+/).filter(Boolean).join(' ')
  if (flattened.length <= maxChars) return flattened
  return flattened.slice(0, Math.max(0, maxChars - 1)) + '…'
}
