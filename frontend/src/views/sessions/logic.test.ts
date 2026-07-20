import { describe, expect, it } from 'vitest'
import {
  agentIdFromKey,
  agentSubline,
  buildDeleteParams,
  dotTone,
  parseBulkDeleteResult,
  parseSingleDeleteResult,
  relTimeLabel,
  filterSessions,
  normalizeRunStatus,
  runStatusBadge,
  sessionRunStatus,
  sessionStats,
  sessionStatusChip,
  sessionVisualStatus,
  sortSessions,
  terminalRunStatus,
  type RawSession,
} from './logic'

// ── agentIdFromKey (sessions.js:720-724) ─────────────────────────────────────
describe('agentIdFromKey', () => {
  it('pulls the agent id from an "agent:<id>:..." key', () => {
    expect(agentIdFromKey('agent:main:chat:abc')).toBe('main')
    expect(agentIdFromKey('agent:data-bot:xyz')).toBe('data-bot')
  })
  it('returns "" when the prefix does not match or input is not a string', () => {
    expect(agentIdFromKey('random-key')).toBe('')
    expect(agentIdFromKey('')).toBe('')
    expect(agentIdFromKey(undefined as unknown as string)).toBe('')
  })
})

// ── normalizeRunStatus (sessions.js:726-734) ─────────────────────────────────
describe('normalizeRunStatus', () => {
  it('maps abandoned → interrupted', () => {
    expect(normalizeRunStatus('abandoned')).toBe('interrupted')
  })
  it('maps succeeded/success/complete → idle', () => {
    expect(normalizeRunStatus('succeeded')).toBe('idle')
    expect(normalizeRunStatus('success')).toBe('idle')
    expect(normalizeRunStatus('complete')).toBe('idle')
  })
  it('passes known run statuses through (lowercased)', () => {
    for (const s of ['queued', 'running', 'interrupted', 'failed', 'timeout', 'cancelled']) {
      expect(normalizeRunStatus(s.toUpperCase())).toBe(s)
    }
  })
  it('falls back to idle for unknown / empty', () => {
    expect(normalizeRunStatus('')).toBe('idle')
    expect(normalizeRunStatus('weird')).toBe('idle')
    expect(normalizeRunStatus(null)).toBe('idle')
  })
})

// ── terminalRunStatus (sessions.js:748-754) ──────────────────────────────────
describe('terminalRunStatus', () => {
  it('returns the normalized status only when terminal', () => {
    expect(terminalRunStatus({ last_task: { status: 'failed' } })).toBe('failed')
    expect(terminalRunStatus({ terminal_status: 'abandoned' })).toBe('interrupted')
    expect(terminalRunStatus({ lastTask: { status: 'cancelled' } })).toBe('cancelled')
  })
  it('returns "" for non-terminal / missing', () => {
    expect(terminalRunStatus({ last_task: { status: 'running' } })).toBe('')
    expect(terminalRunStatus({})).toBe('')
  })
})

// ── sessionRunStatus (sessions.js:736-746) ───────────────────────────────────
describe('sessionRunStatus', () => {
  it('prefers an active queued/running task', () => {
    expect(sessionRunStatus({ active_task: { status: 'running' } })).toBe('running')
    expect(sessionRunStatus({ activeTask: { status: 'queued' } })).toBe('queued')
  })
  it('uses a terminal status when there is no live active task', () => {
    expect(sessionRunStatus({ last_task: { status: 'failed' } })).toBe('failed')
    expect(sessionRunStatus({ terminal_status: 'timeout' })).toBe('timeout')
  })
  it('falls back to run_status / idle', () => {
    expect(sessionRunStatus({ run_status: 'queued' })).toBe('queued')
    expect(sessionRunStatus({})).toBe('idle')
  })
})

// ── sessionVisualStatus (sessions.js:756-761) ────────────────────────────────
describe('sessionVisualStatus', () => {
  it('surfaces failed / timeout run statuses directly', () => {
    expect(sessionVisualStatus({ active_task: { status: 'failed' }, status: 'running' })).toBe(
      'failed',
    )
    expect(sessionVisualStatus({ last_task: { status: 'timeout' }, status: 'done' })).toBe(
      'timeout',
    )
  })
  it('maps cancelled / interrupted run statuses to killed', () => {
    expect(sessionVisualStatus({ last_task: { status: 'cancelled' }, status: 'done' })).toBe(
      'killed',
    )
    expect(sessionVisualStatus({ terminal_status: 'abandoned', status: 'done' })).toBe('killed')
  })
  it('otherwise uses the lifecycle status (lowercased, unknown fallback)', () => {
    expect(sessionVisualStatus({ status: 'Running' })).toBe('running')
    expect(sessionVisualStatus({})).toBe('unknown')
  })
})

// ── sessionStatusChip (components.js:256-281) ────────────────────────────────
describe('sessionStatusChip', () => {
  it('maps statuses to chip tone tokens', () => {
    expect(sessionStatusChip('running')).toBe('ok')
    expect(sessionStatusChip('done')).toBe('info')
    expect(sessionStatusChip('failed')).toBe('danger')
    expect(sessionStatusChip('timeout')).toBe('warn')
  })
  it('killed / unknown / empty → dim (the muted variant)', () => {
    expect(sessionStatusChip('killed')).toBe('dim')
    expect(sessionStatusChip('mystery')).toBe('dim')
    expect(sessionStatusChip('')).toBe('dim')
  })
})

// ── runStatusBadge (sessions.js:763-790) ─────────────────────────────────────
describe('runStatusBadge', () => {
  it('returns a label + chip tone for run-status states that carry a badge', () => {
    expect(runStatusBadge({ active_task: { status: 'running' } })).toEqual({
      label: 'Task running',
      tone: 'ok',
    })
    expect(runStatusBadge({ active_task: { status: 'queued' } })).toEqual({
      label: 'Task queued',
      tone: 'warn',
    })
    expect(runStatusBadge({ last_task: { status: 'failed' } })).toEqual({
      label: 'Last task failed',
      tone: 'danger',
    })
    expect(runStatusBadge({ terminal_status: 'abandoned' })).toEqual({
      label: 'Interrupted',
      tone: 'warn',
    })
  })
  it('returns null when the run status carries no badge (idle)', () => {
    expect(runStatusBadge({})).toBeNull()
    expect(runStatusBadge({ status: 'running' })).toBeNull()
  })
})

// ── dotTone (overview parity) ────────────────────────────────────────────────
describe('dotTone', () => {
  it('maps status dot variants to --tone tokens', () => {
    // running→ok, done→off→dim, failed→err→danger, timeout→warn, killed→off→dim
    expect(dotTone('running')).toBe('ok')
    expect(dotTone('done')).toBe('dim')
    expect(dotTone('failed')).toBe('danger')
    expect(dotTone('timeout')).toBe('warn')
    expect(dotTone('killed')).toBe('dim')
  })
})

// ── relTimeLabel (components.js:228-241) ─────────────────────────────────────
describe('relTimeLabel', () => {
  it('returns "just now" for a very recent time', () => {
    expect(relTimeLabel(Date.now())).toBe('just now')
  })
  it('formats minutes / hours / days ago', () => {
    expect(relTimeLabel(Date.now() - 5 * 60_000)).toBe('5m ago')
    expect(relTimeLabel(Date.now() - 3 * 3_600_000)).toBe('3h ago')
    expect(relTimeLabel(Date.now() - 2 * 86_400_000)).toBe('2d ago')
  })
  it('returns "—" for invalid input', () => {
    expect(relTimeLabel('not-a-date')).toBe('—')
  })
})

// ── filterSessions (sessions.js:162-176) ─────────────────────────────────────
const SESSIONS: RawSession[] = [
  { key: 'agent:main:chat:aaa', model: 'gpt-x', message_count: 3, updated_at: '3' },
  {
    key: 'agent:bot:chat:bbb',
    model: 'claude',
    display_name: 'Bug triage',
    message_count: 10,
    updated_at: '1',
  },
  { key: 'agent:main:chat:ccc', subject: 'Deploy plan', message_count: 1, updated_at: '2' },
]

describe('filterSessions', () => {
  it('returns a copy of all sessions when the query is empty', () => {
    const out = filterSessions(SESSIONS, '')
    expect(out).toHaveLength(3)
    expect(out).not.toBe(SESSIONS)
  })
  it('matches on key, model, display_name and subject (case-insensitive)', () => {
    expect(filterSessions(SESSIONS, 'bug').map((s) => s.key)).toEqual(['agent:bot:chat:bbb'])
    expect(filterSessions(SESSIONS, 'claude').map((s) => s.key)).toEqual(['agent:bot:chat:bbb'])
    expect(filterSessions(SESSIONS, 'deploy').map((s) => s.key)).toEqual(['agent:main:chat:ccc'])
    expect(filterSessions(SESSIONS, 'aaa').map((s) => s.key)).toEqual(['agent:main:chat:aaa'])
  })
  it('matches derived_title / derivedTitle', () => {
    const rows: RawSession[] = [{ key: 'k1', derived_title: 'Weekly report' }]
    expect(filterSessions(rows, 'weekly')).toHaveLength(1)
  })
})

// ── sortSessions (sessions.js:180-194) ───────────────────────────────────────
describe('sortSessions', () => {
  it('sorts updated_at numerically (desc by default semantics via asc flag)', () => {
    const asc = sortSessions(SESSIONS, 'updated_at', true).map((s) => s.updated_at)
    expect(asc).toEqual(['1', '2', '3'])
    const desc = sortSessions(SESSIONS, 'updated_at', false).map((s) => s.updated_at)
    expect(desc).toEqual(['3', '2', '1'])
  })
  it('sorts message_count numerically', () => {
    const desc = sortSessions(SESSIONS, 'message_count', false).map((s) => s.message_count)
    expect(desc).toEqual([10, 3, 1])
  })
  it('sorts key as a lowercased string', () => {
    const asc = sortSessions(SESSIONS, 'key', true).map((s) => s.key)
    expect(asc).toEqual(['agent:bot:chat:bbb', 'agent:main:chat:aaa', 'agent:main:chat:ccc'])
  })
  it('does not mutate the input', () => {
    const copy = [...SESSIONS]
    sortSessions(SESSIONS, 'key', true)
    expect(SESSIONS).toEqual(copy)
  })
})

// ── sessionStats (sessions.js:196-241) ───────────────────────────────────────
describe('sessionStats', () => {
  it('counts totals, lifecycle buckets, executing runs, messages and distinct agents', () => {
    const rows: RawSession[] = [
      { key: 'agent:main:chat:a', status: 'running', message_count: 5 },
      { key: 'agent:bot:chat:b', status: 'done', message_count: 2 },
      {
        key: 'agent:bot:chat:c',
        status: 'done',
        last_task: { status: 'failed' },
        message_count: 1,
      },
      {
        key: 'agent:x:chat:d',
        status: 'done',
        active_task: { status: 'queued' },
        message_count: 4,
      },
      { key: 'agent:x:chat:e', status: 'done', terminal_status: 'timeout' },
      { key: 'agent:x:chat:f', status: 'done', terminal_status: 'cancelled' },
    ]
    const s = sessionStats(rows)
    expect(s.total).toBe(6)
    expect(s.lifecycleOpen).toBe(1) // one 'running'
    expect(s.activeRuns).toBe(1) // the queued one
    expect(s.done).toBe(2) // b (done) + d (done + active queued → visual still 'done')
    expect(s.failedOrTimedOut).toBe(2) // c failed + e timeout
    expect(s.aborted).toBe(1) // f cancelled → killed
    expect(s.totalMessages).toBe(12)
    expect(s.agents).toBe(3) // main, bot, x
  })
  it('handles an empty list', () => {
    const s = sessionStats([])
    expect(s).toMatchObject({ total: 0, activeRuns: 0, totalMessages: 0, agents: 0 })
  })
})

// ── agentSubline (sessions.js:796-824) ───────────────────────────────────────
describe('agentSubline', () => {
  const agents = new Map([['bot', { id: 'bot', name: 'Support Bot' }]])
  it('returns null for blank agent id', () => {
    expect(agentSubline('', agents, true)).toBeNull()
  })
  it('returns null for the built-in main agent (no noise)', () => {
    expect(agentSubline('main', agents, true)).toBeNull()
    expect(agentSubline('main', new Map(), true)).toBeNull()
  })
  it('returns the display name when the agent is known', () => {
    expect(agentSubline('bot', agents, true)).toEqual({ name: 'Support Bot', orphan: false })
  })
  it('marks an unknown agent orphaned only after the registry has loaded', () => {
    expect(agentSubline('ghost', agents, true)).toEqual({ name: 'ghost', orphan: true })
    // before load: plain id, not orphaned
    expect(agentSubline('ghost', agents, false)).toEqual({ name: 'ghost', orphan: false })
  })
})

// ── buildDeleteParams (sessions.js:509,538) ──────────────────────────────────
describe('buildDeleteParams', () => {
  it('builds a single-key delete param', () => {
    expect(buildDeleteParams(['k1'])).toEqual({ key: 'k1' })
  })
  it('builds a batch delete param for multiple keys', () => {
    expect(buildDeleteParams(['k1', 'k2'])).toEqual({ keys: ['k1', 'k2'] })
  })
})

// ── parseBulkDeleteResult (sessions.js:510-516) ──────────────────────────────
describe('parseBulkDeleteResult', () => {
  it('reads ok/err counts from deleted[]/errors[]', () => {
    expect(parseBulkDeleteResult({ deleted: ['a', 'b'], errors: ['c'] }, 3)).toEqual({
      okCount: 2,
      errCount: 1,
    })
  })
  it('falls back to (requested - errCount) when deleted[] is missing', () => {
    expect(parseBulkDeleteResult({ errors: ['x'] }, 4)).toEqual({ okCount: 3, errCount: 1 })
    expect(parseBulkDeleteResult(null, 2)).toEqual({ okCount: 2, errCount: 0 })
  })
})

// ── parseSingleDeleteResult (sessions.js:539-549) ────────────────────────────
describe('parseSingleDeleteResult', () => {
  it('is ok when errors is empty and the key is in deleted[]', () => {
    expect(parseSingleDeleteResult({ deleted: ['k1'], errors: [] }, 'k1')).toEqual({ ok: true })
  })
  it('is not-ok with a reason when the key was not deleted', () => {
    expect(parseSingleDeleteResult({ deleted: [], errors: ['nope'] }, 'k1')).toEqual({
      ok: false,
      reason: 'nope',
    })
    expect(parseSingleDeleteResult({ deleted: [], errors: [{ message: 'boom' }] }, 'k1')).toEqual({
      ok: false,
      reason: 'boom',
    })
    expect(parseSingleDeleteResult({ deleted: [] }, 'k1')).toEqual({
      ok: false,
      reason: 'session was not deleted',
    })
  })
})
