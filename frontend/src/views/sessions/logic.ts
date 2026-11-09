// Pure sessions-view helpers ported 1:1 from the legacy view
// (src/agentos/gateway/static/js/views/sessions.js) and the shared UI helpers
// it consumes (static/js/components.js sessionStatus*). Each function carries
// the legacy line range it mirrors so the parity matrix stays auditable. RPC
// calls, mutations, dialogs and rendering live in SessionsPage.tsx; this module
// owns the pure derivations (status folding, filtering, sorting, stats, orphan
// resolution).

/** A run-status task descriptor (active_task / last_task). */
interface RunTask {
  status?: string
}

/** A raw session row from sessions.list (all fields optional; both snake_case
 * and camelCase variants appear across backend/CLI payloads). */
import { t } from '@/i18n'
import '@/i18n/en/sessions'

export interface RawSession {
  key?: string
  status?: string
  model?: string
  message_count?: number
  updated_at?: string | number
  size_bytes?: number | null
  agent_id?: string
  agentId?: string
  display_name?: string
  displayName?: string
  subject?: string
  project_id?: string | null
  projectId?: string | null
  derived_title?: string
  derivedTitle?: string
  active_task?: RunTask | null
  activeTask?: RunTask | null
  last_task?: RunTask | null
  lastTask?: RunTask | null
  run_status?: string
  runStatus?: string
  terminal_status?: string
  terminalStatus?: string
  [key: string]: unknown
}

/** A registry agent entry (from agents.list) used for orphan detection. */
export interface AgentEntry {
  id?: string
  name?: string
}

/** --tone token names used across the console design system. */
export type Tone = 'ok' | 'warn' | 'danger' | 'info' | 'dim'

/** Page size the WebUI asks sessions.list for. Larger than the CLI default
 * (50) because the Sessions and Projects views render, filter and count the
 * whole list client-side and share the ['sessions'] query cache, so both
 * callers must request the same window or the cache flips between two
 * different truncations depending on which view loaded first. */
export const SESSIONS_LIST_LIMIT = 500

// ── Key parsing ──────────────────────────────────────────────────────────────

/** — pull the agent id from a key like
 * "agent:<id>:<kind>:<short>". Returns '' when the prefix doesn't match or the
 * input isn't a string. */
export function agentIdFromKey(key: unknown): string {
  if (typeof key !== 'string') return ''
  const m = /^agent:([^:]+):/.exec(key)
  return m ? (m[1] ?? '') : ''
}

// ── Run-status normalization / folding ───────────────────────────────────────

const KNOWN_RUN_STATUSES = ['queued', 'running', 'interrupted', 'failed', 'timeout', 'cancelled']

/** — normalize a raw run status: abandoned→interrupted,
 * succeeded/success/complete→idle, known tokens pass through lowercased, else
 * idle. */
export function normalizeRunStatus(status?: string | null): string {
  const value = String(status || '').toLowerCase()
  if (value === 'abandoned') return 'interrupted'
  if (value === 'succeeded' || value === 'success' || value === 'complete') return 'idle'
  if (KNOWN_RUN_STATUSES.includes(value)) return value
  return 'idle'
}

/** — the terminal run status of a row (from last_task /
 * terminal_status), but only when it is one of failed/timeout/cancelled/
 * interrupted; otherwise ''. */
export function terminalRunStatus(row: RawSession): string {
  const lastTask = row.last_task || row.lastTask || null
  const rawStatus = lastTask?.status || row.terminal_status || row.terminalStatus || ''
  const status = normalizeRunStatus(rawStatus)
  return ['failed', 'timeout', 'cancelled', 'interrupted'].includes(status) ? status : ''
}

/** — the effective run status of a session: a live
 * active queued/running task wins; else a terminal status; else the raw
 * run_status (normalized), defaulting to idle. */
export function sessionRunStatus(row: RawSession): string {
  const active = row.active_task || row.activeTask || null
  const activeStatus = active ? normalizeRunStatus(active.status) : ''
  const terminalStatus = terminalRunStatus(row)
  const rawStatus = row.run_status || row.runStatus || active?.status || terminalStatus || ''
  const runStatus = normalizeRunStatus(rawStatus)
  if (active && (activeStatus === 'queued' || activeStatus === 'running')) return activeStatus
  if (terminalStatus) return normalizeRunStatus(terminalStatus)
  return runStatus
}

/** — the status used to color the row: failed/timeout run
 * statuses surface directly; cancelled/interrupted → 'killed'; otherwise the
 * lifecycle status (lowercased, 'unknown' fallback). */
export function sessionVisualStatus(row: RawSession): string {
  const runStatus = sessionRunStatus(row)
  if (runStatus === 'failed' || runStatus === 'timeout') return runStatus
  if (runStatus === 'cancelled' || runStatus === 'interrupted') return 'killed'
  return String(row?.status || 'unknown').toLowerCase()
}

// ── Status → chip / dot tone mapping ──────────────────

const SESSION_STATUS_DOT: Record<string, string> = {
  running: 'ok',
  done: 'off',
  failed: 'err',
  killed: 'off',
  timeout: 'warn',
}
const SESSION_STATUS_CHIP: Record<string, Tone> = {
  running: 'ok',
  done: 'info',
  failed: 'danger',
  killed: 'dim',
  timeout: 'warn',
}
function sessionStatusLabels(): Record<string, string> {
  return {
    running: t('sessions.statusRunning'),
    done: t('sessions.statusDone'),
    failed: t('sessions.statusFailed'),
    killed: t('sessions.statusKilled'),
    timeout: t('sessions.statusTimeout'),
  }
}

/** — dot color variant ("ok"/"warn"/"err"/"off"). */
export function sessionStatusClass(status?: string | null): string {
  const k = String(status || '').toLowerCase()
  return SESSION_STATUS_DOT[k] || 'off'
}

/** — tooltip label; raw string else "Unknown" when empty. */
export function sessionStatusLabel(status?: string | null): string {
  const k = String(status || '').toLowerCase()
  return sessionStatusLabels()[k] || (status ? String(status) : t('sessions.statusUnknown'))
}

/** — chip color modifier mapped to a --tone token. The
 * legacy '' (muted) variant maps to the 'dim' tone; status color stays on the
 * tone primitive, never hardcoded. */
export function sessionStatusChip(status?: string | null): Tone {
  const k = String(status || '').toLowerCase()
  return SESSION_STATUS_CHIP[k] || 'dim'
}

/** components.js dot variant ("ok"/"warn"/"err"/"off") → --tone token name.
 * Accepts a raw session status (folds through sessionStatusClass first). */
export function dotTone(status?: string | null): Tone {
  const dot = sessionStatusClass(status)
  if (dot === 'ok') return 'ok'
  if (dot === 'warn') return 'warn'
  if (dot === 'err') return 'danger'
  return 'dim'
}

/** — relative time. Numeric input is treated as an epoch
 * (seconds when < 1e10, else millis); strings parse as ISO or a numeric epoch.
 * Invalid → "—". */
export function relTimeLabel(isoOrTs: string | number): string {
  const numeric =
    typeof isoOrTs === 'number'
      ? isoOrTs
      : typeof isoOrTs === 'string' && isoOrTs.trim() !== ''
        ? Number(isoOrTs)
        : NaN
  const d = Number.isFinite(numeric)
    ? new Date(Math.abs(numeric) < 10_000_000_000 ? numeric * 1000 : numeric)
    : new Date(isoOrTs)
  if (Number.isNaN(d.getTime())) return t('common.dash')
  const diff = (Date.now() - d.getTime()) / 1000
  if (diff < 60) return t('sessions.relJustNow')
  if (diff < 3600) return t('sessions.relMinutes', { count: Math.floor(diff / 60) })
  if (diff < 86_400) return t('sessions.relHours', { count: Math.floor(diff / 3600) })
  return t('sessions.relDays', { count: Math.floor(diff / 86_400) })
}

// ── Run-status badge ───────────────────────────────────

function runStatusLabels(): Record<string, string> {
  return {
    queued: t('sessions.runQueued'),
    running: t('sessions.runRunning'),
    interrupted: t('sessions.runInterrupted'),
    failed: t('sessions.runFailed'),
    timeout: t('sessions.runTimeout'),
    cancelled: t('sessions.runCancelled'),
  }
}
const RUN_STATUS_CHIP: Record<string, Tone> = {
  queued: 'warn',
  running: 'ok',
  interrupted: 'warn',
  failed: 'danger',
  timeout: 'warn',
}

export interface RunBadge {
  label: string
  tone: Tone
}

/** — the run-status badge for a row, or null when the run
 * status carries no badge (e.g. idle, or cancelled which has a label but no
 * chip class in the legacy map → still shown, tone falls back to dim). */
export function runStatusBadge(row: RawSession): RunBadge | null {
  const runStatus = sessionRunStatus(row)
  const label = runStatusLabels()[runStatus]
  if (!label) return null
  return { label, tone: RUN_STATUS_CHIP[runStatus] || 'dim' }
}

// ── Naming ───────────────────────────────────────────────────────────────────

/** The user-set session name, or '' when the session has never been renamed.
 *
 * Deliberately does NOT fall back to `derived_title`: the gateway derives that
 * from the short session id when no name is set, and prefilling the rename
 * input with an id would make "clear the name" impossible to express. Search
 * still scores `derived_title` — see `filterSessions`. */
export function sessionName(s: RawSession): string {
  return String(s.display_name || s.displayName || '')
}

// ── Filtering ──────────────────────────────────────────

/** — filter sessions by a lowercased query across
 * key/model/display_name/subject/derived_title. Empty query returns a copy of
 * the whole list (never mutates the input). */
export function filterSessions(sessions: RawSession[], query: string): RawSession[] {
  const q = query.trim().toLowerCase()
  if (!q) return [...sessions]
  return sessions.filter(
    (s) =>
      String(s.key || '')
        .toLowerCase()
        .includes(q) ||
      String(s.model || '')
        .toLowerCase()
        .includes(q) ||
      String(s.display_name || s.displayName || '')
        .toLowerCase()
        .includes(q) ||
      String(s.subject || '')
        .toLowerCase()
        .includes(q) ||
      String(s.derived_title || s.derivedTitle || '')
        .toLowerCase()
        .includes(q),
  )
}

// ── Sorting ────────────────────────────────────────────

export type SortColumn = 'key' | 'updated_at' | 'message_count'

/** — sort a copy of the sessions by a column. message_count
 * / updated_at compare numerically; other columns compare as lowercased
 * strings. `asc` toggles direction. Never mutates the input. */
export function sortSessions(
  sessions: RawSession[],
  column: SortColumn,
  asc: boolean,
): RawSession[] {
  const numeric = column === 'message_count' || column === 'updated_at'
  return [...sessions].sort((a, b) => {
    let va: number | string
    let vb: number | string
    if (numeric) {
      va = Number(a[column] ?? '') || 0
      vb = Number(b[column] ?? '') || 0
    } else {
      va = String(a[column] ?? '').toLowerCase()
      vb = String(b[column] ?? '').toLowerCase()
    }
    const cmp = va < vb ? -1 : va > vb ? 1 : 0
    return asc ? cmp : -cmp
  })
}

// ── Stat row ───────────────────────────────────────────

export interface SessionStats {
  total: number
  lifecycleOpen: number
  activeRuns: number
  done: number
  failedOrTimedOut: number
  aborted: number
  totalMessages: number
  agents: number
}

/** — the stat-row aggregates: total sessions, lifecycle
 * 'running' count, executing runs (run status queued/running), visual done /
 * failed-or-timeout / killed buckets, total messages, and the count of
 * distinct agents derived from the key prefix. */
export function sessionStats(sessions: RawSession[]): SessionStats {
  const total = sessions.length
  const lifecycleOpen = sessions.filter((s) => s.status === 'running').length
  const activeRuns = sessions.filter((s) => {
    const runStatus = sessionRunStatus(s)
    return runStatus === 'queued' || runStatus === 'running'
  }).length
  const done = sessions.filter((s) => sessionVisualStatus(s) === 'done').length
  const failedOrTimedOut = sessions.filter((s) => {
    const status = sessionVisualStatus(s)
    return status === 'failed' || status === 'timeout'
  }).length
  const aborted = sessions.filter((s) => sessionVisualStatus(s) === 'killed').length
  const totalMessages = sessions.reduce((acc, s) => acc + (Number(s.message_count) || 0), 0)
  const agents = new Set<string>()
  sessions.forEach((s) => {
    const m = /^agent:([^:]+):/.exec(s.key || '')
    if (m?.[1]) agents.add(m[1])
  })
  return {
    total,
    lifecycleOpen,
    activeRuns,
    done,
    failedOrTimedOut,
    aborted,
    totalMessages,
    agents: agents.size,
  }
}

// ── Orphan-agent subline ───────────────────────────────

export interface AgentSubline {
  /** Agent display name (or the raw id when unknown). */
  name: string
  /** True when the agent id is missing from a loaded registry. */
  orphan: boolean
}

/** — resolve the per-row agent subline. Blank id or the
 * built-in `main` return null (no noise). A known agent → its name. An unknown
 * agent → orphaned, but ONLY once the registry has actually loaded; before
 * first load the raw id is shown as plain text (orphan:false) so a transient
 * agents.list failure doesn't flood the table with false warnings. */
export function agentSubline(
  agentId: string,
  agentsById: Map<string, AgentEntry>,
  agentsLoaded: boolean,
): AgentSubline | null {
  if (!agentId) return null
  const entry = agentsById.get(agentId)
  if (entry) {
    if (agentId === 'main') return null
    return { name: entry.name || agentId, orphan: false }
  }
  if (agentId === 'main') return null
  if (!agentsLoaded) return { name: agentId, orphan: false }
  return { name: agentId, orphan: true }
}

// ── Delete params ──────────────────────────────────────

/** — the sessions.delete param shape: a single key uses
 * {key}; multiple keys use the batch {keys:[…]} form (partial-failure aware). */
export function buildDeleteParams(keys: string[]): { key: string } | { keys: string[] } {
  return keys.length === 1 ? { key: keys[0]! } : { keys }
}

// ── Delete result parsing ──────────────────────

export interface DeleteResult {
  deleted?: unknown[]
  errors?: unknown[]
}

export interface DeleteOutcome {
  okCount: number
  errCount: number
}

/** — parse a batch sessions.delete response into ok/err
 * counts. okCount falls back to (requested - errCount) when the backend omits
 * the deleted[] array. */
export function parseBulkDeleteResult(res: DeleteResult | null, requested: number): DeleteOutcome {
  const errCount = Array.isArray(res?.errors) ? res!.errors.length : 0
  const okCount = Array.isArray(res?.deleted) ? res!.deleted.length : requested - errCount
  return { okCount, errCount }
}

/** — decide whether a single-key delete succeeded, and the
 * reason string when it did not. Success requires no errors AND the key present
 * in deleted[]. */
export function parseSingleDeleteResult(
  res: DeleteResult | null,
  key: string,
): { ok: true } | { ok: false; reason: string } {
  const errors = Array.isArray(res?.errors) ? res!.errors : []
  const deleted = Array.isArray(res?.deleted) ? res!.deleted : []
  if (errors.length === 0 && deleted.includes(key)) return { ok: true }
  const first = errors[0]
  const reason =
    typeof first === 'string'
      ? first
      : ((first as { message?: string; error?: string; reason?: string } | undefined)?.message ??
        (first as { error?: string } | undefined)?.error ??
        (first as { reason?: string } | undefined)?.reason ??
        'session was not deleted')
  return { ok: false, reason }
}
