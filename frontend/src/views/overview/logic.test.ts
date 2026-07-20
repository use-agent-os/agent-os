import { describe, expect, it, vi, afterEach } from 'vitest'
import {
  readinessStatusLabel,
  formatUptime,
  sessionStatusClass,
  sessionStatusLabel,
  relTime,
  formatEventPayload,
  formatEventTs,
  sortRecentSessions,
  formatTokens,
  formatCost,
  type OverviewSession,
} from './logic'

describe('readinessStatusLabel', () => {
  it.each([
    ['ready', 'Ready'],
    ['degraded', 'Degraded'],
    ['action_required', 'Action required'],
    ['unavailable', 'Unavailable'],
    ['unknown', 'Unknown'],
  ])('maps known status %s -> %s (overview.js:352-365)', (status, label) => {
    expect(readinessStatusLabel(status)).toBe(label)
  })
  it('Title-cases an unknown status token splitting _/-', () => {
    expect(readinessStatusLabel('needs_setup')).toBe('Needs Setup')
  })
  it('falls back to Unknown for empty / nullish', () => {
    expect(readinessStatusLabel('')).toBe('Unknown')
    expect(readinessStatusLabel(undefined)).toBe('Unknown')
  })
})

describe('formatUptime', () => {
  it('formats uptime_ms as "Hh Mm Ss" (overview.js:234-242)', () => {
    // 1h 2m 3s => 3600000 + 120000 + 3000 = 3723000 ms
    expect(formatUptime(3723000)).toBe('1h 2m 3s')
  })
  it('zero uptime renders 0h 0m 0s', () => {
    expect(formatUptime(0)).toBe('0h 0m 0s')
  })
  it('returns "—" for null/undefined', () => {
    expect(formatUptime(null)).toBe('—')
    expect(formatUptime(undefined)).toBe('—')
  })
})

describe('sessionStatusClass (components.js:249-275)', () => {
  it.each([
    ['running', 'ok'],
    ['done', 'off'],
    ['failed', 'err'],
    ['killed', 'off'],
    ['timeout', 'warn'],
  ])('maps %s -> %s dot variant', (status, cls) => {
    expect(sessionStatusClass(status)).toBe(cls)
  })
  it('defaults unknown statuses to off', () => {
    expect(sessionStatusClass('weird')).toBe('off')
    expect(sessionStatusClass('')).toBe('off')
  })
})

describe('sessionStatusLabel (components.js:263-287)', () => {
  it.each([
    ['running', 'Running'],
    ['done', 'Completed'],
    ['failed', 'Failed'],
    ['killed', 'Aborted by operator'],
    ['timeout', 'Timed out'],
  ])('maps %s -> %s tooltip', (status, label) => {
    expect(sessionStatusLabel(status)).toBe(label)
  })
  it('falls back to the raw string, or Unknown when empty', () => {
    expect(sessionStatusLabel('mystery')).toBe('mystery')
    expect(sessionStatusLabel('')).toBe('Unknown')
  })
})

describe('relTime (components.js:228-241)', () => {
  afterEach(() => vi.useRealTimers())
  it('returns "just now" for < 60s', () => {
    const now = Date.now()
    vi.useFakeTimers().setSystemTime(now)
    expect(relTime(new Date(now - 5_000).toISOString())).toBe('just now')
  })
  it('formats minutes/hours/days ago', () => {
    const now = Date.now()
    vi.useFakeTimers().setSystemTime(now)
    expect(relTime(new Date(now - 5 * 60_000).toISOString())).toBe('5m ago')
    expect(relTime(new Date(now - 3 * 3_600_000).toISOString())).toBe('3h ago')
    expect(relTime(new Date(now - 2 * 86_400_000).toISOString())).toBe('2d ago')
  })
  it('accepts epoch seconds and epoch millis', () => {
    const now = 1_000_000_000_000 // ms
    vi.useFakeTimers().setSystemTime(now)
    // epoch seconds (< 1e10) get *1000
    expect(relTime(Math.floor(now / 1000) - 120)).toBe('2m ago')
    // epoch millis pass through
    expect(relTime(now - 120_000)).toBe('2m ago')
  })
  it('returns "—" for invalid input', () => {
    expect(relTime('not-a-date')).toBe('—')
  })
})

describe('formatEventPayload (overview.js:320-326)', () => {
  it('stringifies JSON and truncates over 80 chars with an ellipsis', () => {
    const big = { k: 'x'.repeat(200) }
    const out = formatEventPayload(big)
    expect(out.length).toBe(81) // 80 chars + ellipsis
    expect(out.endsWith('…')).toBe(true)
  })
  it('keeps short payloads intact', () => {
    expect(formatEventPayload({ a: 1 })).toBe('{"a":1}')
  })
  it('falls back to String() for non-serializable payloads', () => {
    const circular: Record<string, unknown> = {}
    circular.self = circular
    expect(typeof formatEventPayload(circular)).toBe('string')
  })
})

describe('formatEventTs (overview.js:318-319)', () => {
  it('slices a Date to HH:MM:SS', () => {
    const d = new Date('2026-01-02T13:45:07.000Z')
    // toTimeString is local; assert the shape rather than the exact zone.
    expect(formatEventTs(d)).toMatch(/^\d{2}:\d{2}:\d{2}$/)
  })
})

describe('sortRecentSessions (overview.js:274-281)', () => {
  const mk = (key: string, updated_at?: string): OverviewSession => ({ key, updated_at })
  it('sorts by updated_at descending and slices to 6', () => {
    const sessions = [
      mk('a', '2026-01-01T00:00:00Z'),
      mk('b', '2026-01-03T00:00:00Z'),
      mk('c', '2026-01-02T00:00:00Z'),
    ]
    expect(sortRecentSessions(sessions).map((s) => s.key)).toEqual(['b', 'c', 'a'])
  })
  it('treats a missing updated_at as epoch 0 (sorts last)', () => {
    const sessions = [mk('old'), mk('new', '2026-01-01T00:00:00Z')]
    expect(sortRecentSessions(sessions).map((s) => s.key)).toEqual(['new', 'old'])
  })
  it('caps the result at 6 rows', () => {
    const many = Array.from({ length: 10 }, (_, i) =>
      mk(`s${i}`, `2026-01-${String(i + 1).padStart(2, '0')}T00:00:00Z`),
    )
    expect(sortRecentSessions(many)).toHaveLength(6)
  })
  it('does not mutate the input array', () => {
    const input = [mk('a', '2026-01-01T00:00:00Z'), mk('b', '2026-01-02T00:00:00Z')]
    const snapshot = input.map((s) => s.key)
    sortRecentSessions(input)
    expect(input.map((s) => s.key)).toEqual(snapshot)
  })
})

describe('formatTokens (overview.js:263)', () => {
  it('localizes a token count', () => {
    expect(formatTokens(1234567)).toBe((1234567).toLocaleString())
  })
  it('returns "—" for null/undefined', () => {
    expect(formatTokens(null)).toBe('—')
    expect(formatTokens(undefined)).toBe('—')
  })
})

describe('formatCost (overview.js:266-268)', () => {
  it('formats as $ with 4 decimals', () => {
    expect(formatCost(1.2)).toBe('$1.2000')
  })
  it('returns "—" for null/undefined', () => {
    expect(formatCost(null)).toBe('—')
    expect(formatCost(undefined)).toBe('—')
  })
})
