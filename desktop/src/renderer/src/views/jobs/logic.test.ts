import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { RawJob } from '@/views/cron/logic'
import {
  clockLabel,
  countdown,
  cronToNatural,
  describeSchedule,
  filterCounts,
  formatDurationMs,
  formatInterval,
  intervalToSeconds,
  isoToLocalInput,
  jobHealth,
  localInputToIso,
  matchesFilter,
  naturalToCron,
  orderJobs,
  runOutcomes,
  secondsToInterval,
  seedBuilder,
  timeZoneOptions,
  upcomingRuns,
  DEFAULT_NATURAL,
} from './logic'
import { EMPTY_CRON_FORM } from '@/views/cron/logic'

const NOW = new Date(2026, 8, 10, 12, 0, 0).getTime() // Thu 10 Sep 2026, local

function job(over: Partial<RawJob> = {}): RawJob {
  return { id: 'j1', name: 'Job', enabled: true, expression: '0 9 * * *', ...over }
}

describe('jobHealth', () => {
  it('reads paused, running, failing and active in that priority', () => {
    expect(jobHealth(job({ enabled: false, status: 'running' }))).toBe('paused')
    expect(jobHealth(job({ status: 'running', consecutive_errors: 3 }))).toBe('running')
    expect(jobHealth(job({ consecutive_errors: 1 }))).toBe('failing')
    expect(jobHealth(job({ last_status: 'error' }))).toBe('failing')
    expect(jobHealth(job({ lastResult: 'boom', last_run: '2026-09-10T00:00:00Z' }))).toBe('failing')
    expect(jobHealth(job())).toBe('active')
  })
})

describe('filters', () => {
  const jobs = [
    job({ id: 'a' }),
    job({ id: 'b', enabled: false }),
    job({ id: 'c', consecutive_errors: 2 }),
    job({ id: 'd', status: 'running' }),
  ]
  it('counts every bucket and always passes "all"', () => {
    expect(filterCounts(jobs)).toEqual({ all: 4, active: 2, paused: 1, failing: 1 })
    expect(jobs.filter((j) => matchesFilter(j, 'active')).map((j) => j.id)).toEqual(['a', 'd'])
    expect(jobs.filter((j) => matchesFilter(j, 'failing')).map((j) => j.id)).toEqual(['c'])
    expect(jobs.filter((j) => matchesFilter(j, 'paused')).map((j) => j.id)).toEqual(['b'])
  })
})

describe('orderJobs', () => {
  it('sorts by next run, missing after known, paused last, then by name', () => {
    const list = [
      job({ id: 'paused', name: 'A', enabled: false, next_run: '2026-09-10T13:00:00Z' }),
      job({ id: 'later', name: 'Z', next_run: '2026-09-11T09:00:00Z' }),
      job({ id: 'none-b', name: 'B' }),
      job({ id: 'soon', name: 'Y', next_run: '2026-09-10T13:00:00Z' }),
      job({ id: 'none-a', name: 'A' }),
    ]
    expect(orderJobs(list).map((j) => j.id)).toEqual([
      'soon',
      'later',
      'none-a',
      'none-b',
      'paused',
    ])
  })
})

describe('describeSchedule', () => {
  it('speaks in words for every schedule kind', () => {
    expect(describeSchedule(job({ expression: '0 9 * * 1-5' }))).toBe('Weekdays at 09:00')
    expect(describeSchedule(job({ scheduleKind: 'every', scheduleRaw: 300 }))).toBe('Every 5 min')
    expect(describeSchedule(job({ scheduleKind: 'every', scheduleRaw: 7200 }))).toBe('Every 2 h')
    const at = new Date(NOW + 24 * 3600 * 1000)
    at.setHours(15, 30, 0, 0)
    // The clock part follows the runtime locale (15:30 or 03:30 PM).
    expect(
      describeSchedule(job({ scheduleKind: 'at', scheduleRaw: at.toISOString() }), NOW),
    ).toMatch(/^Once, Tomorrow (15:30|03:30 PM)$/)
  })
  it('falls back to a generic label when the expression is unreadable', () => {
    expect(describeSchedule(job({ expression: 'nonsense' }))).toBe('Custom cadence')
  })
})

describe('countdown / clockLabel', () => {
  it('uses at most two units and flips to "ago" for the past', () => {
    expect(countdown(NOW + 500, NOW)).toBe('now')
    expect(countdown(NOW + 32_000, NOW)).toBe('in 32s')
    expect(countdown(NOW + 4 * 60_000 + 5_000, NOW)).toBe('in 4m 5s')
    expect(countdown(NOW + 3 * 3_600_000 + 12 * 60_000, NOW)).toBe('in 3h 12m')
    expect(countdown(NOW + 2 * 86_400_000 + 3_600_000, NOW)).toBe('in 2d 1h')
    expect(countdown(NOW - 3 * 60_000, NOW)).toBe('3m 0s ago')
  })
  it('names today, tomorrow and yesterday, else the weekday and date', () => {
    const d = new Date(NOW)
    d.setHours(9, 5)
    expect(clockLabel(d, NOW)).toMatch(/^Today 09:05/)
    d.setDate(d.getDate() + 1)
    expect(clockLabel(d, NOW)).toMatch(/^Tomorrow /)
    d.setDate(d.getDate() - 2)
    expect(clockLabel(d, NOW)).toMatch(/^Yesterday /)
    d.setDate(d.getDate() + 5)
    expect(clockLabel(d, NOW)).toMatch(/^\w{3}(,| \d)/)
  })
})

describe('upcomingRuns', () => {
  it('is empty for a paused job and steps an interval from next_run', () => {
    expect(upcomingRuns(job({ enabled: false }), 5, NOW)).toEqual([])
    const next = new Date(NOW + 60_000).toISOString()
    const runs = upcomingRuns(
      job({ scheduleKind: 'every', scheduleRaw: 600, next_run: next }),
      3,
      NOW,
    )
    expect(runs.map((d) => d.getTime())).toEqual([
      NOW + 60_000,
      NOW + 60_000 + 600_000,
      NOW + 60_000 + 1_200_000,
    ])
  })
  it('walks a cron expression and returns the single date of a one-time job', () => {
    const daily = upcomingRuns(job({ expression: '0 9 * * *' }), 2, NOW)
    expect(daily).toHaveLength(2)
    expect(daily[0]!.getHours()).toBe(9)
    expect(daily[1]!.getTime() - daily[0]!.getTime()).toBe(86_400_000)
    const once = new Date(NOW + 3_600_000).toISOString()
    expect(upcomingRuns(job({ scheduleKind: 'at', next_run: once }), 5, NOW)).toHaveLength(1)
    expect(
      upcomingRuns(job({ scheduleKind: 'at', next_run: new Date(NOW - 1).toISOString() }), 5, NOW),
    ).toEqual([])
  })
})

describe('natural schedule builder', () => {
  it('builds the expression for each cadence', () => {
    expect(naturalToCron({ ...DEFAULT_NATURAL, cadence: 'daily', time: '08:30' })).toBe(
      '30 8 * * *',
    )
    expect(
      naturalToCron({ ...DEFAULT_NATURAL, cadence: 'weekly', time: '09:00', days: [5, 1] }),
    ).toBe('0 9 * * 1,5')
    expect(naturalToCron({ ...DEFAULT_NATURAL, cadence: 'weekly', days: [] })).toBe('0 9 * * *')
    expect(naturalToCron({ ...DEFAULT_NATURAL, cadence: 'hourly', time: '00:15' })).toBe(
      '15 * * * *',
    )
    expect(naturalToCron({ ...DEFAULT_NATURAL, cadence: 'minutes', everyMinutes: 5 })).toBe(
      '*/5 * * * *',
    )
    expect(naturalToCron({ ...DEFAULT_NATURAL, cadence: 'minutes', everyMinutes: 1 })).toBe(
      '* * * * *',
    )
    expect(naturalToCron({ ...DEFAULT_NATURAL, cadence: 'custom' })).toBe('')
  })

  it('recognises what it built, and nothing it did not', () => {
    expect(cronToNatural('30 8 * * *')).toMatchObject({ cadence: 'daily', time: '08:30' })
    expect(cronToNatural('0 9 * * 1-5')).toMatchObject({
      cadence: 'weekly',
      time: '09:00',
      days: [1, 2, 3, 4, 5],
    })
    expect(cronToNatural('0 17 * * fri')).toMatchObject({ cadence: 'weekly', days: [5] })
    expect(cronToNatural('15 * * * *')).toMatchObject({ cadence: 'hourly', time: '00:15' })
    expect(cronToNatural('*/5 * * * *')).toMatchObject({ cadence: 'minutes', everyMinutes: 5 })
    expect(cronToNatural('* * * * *')).toMatchObject({ cadence: 'minutes', everyMinutes: 1 })
    // Day-of-month, month, irregular minutes, several hours: all custom.
    expect(cronToNatural('0 9 1 * *').cadence).toBe('custom')
    expect(cronToNatural('0 9 * jan *').cadence).toBe('custom')
    expect(cronToNatural('0,7 * * * *').cadence).toBe('custom')
    expect(cronToNatural('0 9,17 * * *').cadence).toBe('custom')
    expect(cronToNatural('*/5 9 * * *').cadence).toBe('custom')
    expect(cronToNatural('garbage').cadence).toBe('custom')
  })

  it('round-trips every non-custom cadence', () => {
    for (const natural of [
      { ...DEFAULT_NATURAL, cadence: 'daily' as const, time: '23:45' },
      { ...DEFAULT_NATURAL, cadence: 'weekly' as const, time: '06:00', days: [0, 6] },
      { ...DEFAULT_NATURAL, cadence: 'hourly' as const, time: '00:42' },
      { ...DEFAULT_NATURAL, cadence: 'minutes' as const, everyMinutes: 20 },
    ]) {
      expect(naturalToCron(cronToNatural(naturalToCron(natural)))).toBe(naturalToCron(natural))
    }
  })
})

describe('interval conversions', () => {
  it('picks the largest even unit and rebuilds seconds', () => {
    expect(secondsToInterval(3600)).toEqual({ value: 1, unit: 'hours' })
    expect(secondsToInterval(90)).toEqual({ value: 90, unit: 'seconds' })
    expect(secondsToInterval(172_800)).toEqual({ value: 2, unit: 'days' })
    expect(secondsToInterval(0)).toEqual({ value: 15, unit: 'minutes' })
    expect(intervalToSeconds(2, 'hours')).toBe(7200)
    expect(intervalToSeconds(0, 'hours')).toBe(0)
    expect(formatInterval(45)).toBe('45 s')
    expect(formatInterval(0)).toBe('')
  })
})

describe('datetime-local conversions', () => {
  it('keeps the wall-clock time and spells out the local offset', () => {
    const iso = localInputToIso('2026-09-10T15:30')
    expect(iso).toMatch(/^2026-09-10T15:30:00[+-]\d{2}:\d{2}$/)
    expect(new Date(iso).getHours()).toBe(15)
    expect(isoToLocalInput(iso)).toBe('2026-09-10T15:30')
    expect(localInputToIso('')).toBe('')
    expect(localInputToIso('not a date')).toBe('')
    expect(isoToLocalInput('junk')).toBe('')
  })
})

describe('timeZoneOptions', () => {
  it('lists the local zone first, then UTC, then the rest sorted', () => {
    const zones = timeZoneOptions('Asia/Ho_Chi_Minh')
    expect(zones[0]).toBe('Asia/Ho_Chi_Minh')
    expect(zones[1]).toBe('UTC')
    expect(zones.slice(2)).not.toContain('Asia/Ho_Chi_Minh')
    expect(zones.slice(2)).toEqual([...zones.slice(2)].sort())
  })
})

describe('seedBuilder', () => {
  it('starts a new job on the default cadence and opens an edit on its own', () => {
    expect(seedBuilder(EMPTY_CRON_FORM, false).natural).toEqual(DEFAULT_NATURAL)
    expect(seedBuilder({ ...EMPTY_CRON_FORM, cron: '0 9 * * 1-5' }, true).natural).toMatchObject({
      cadence: 'weekly',
      time: '09:00',
    })
    expect(seedBuilder(EMPTY_CRON_FORM, true).natural.cadence).toBe('custom')
    expect(
      seedBuilder({ ...EMPTY_CRON_FORM, scheduleKind: 'every', every: '900' }, true).interval,
    ).toEqual({ value: 15, unit: 'minutes' })
  })
})

describe('run history helpers', () => {
  it('orders outcomes oldest first and formats durations for people', () => {
    expect(
      runOutcomes([{ status: 'error' }, { status: 'ok' }, { success: true }, { status: 'ok' }], 3),
    ).toEqual(['ok', 'ok', 'error'])
    expect(formatDurationMs(null)).toBe('—')
    expect(formatDurationMs(840)).toBe('840ms')
    expect(formatDurationMs(1234)).toBe('1.2s')
    expect(formatDurationMs(42_000)).toBe('42s')
    expect(formatDurationMs(185_000)).toBe('3m 05s')
  })
})

describe('clock-dependent defaults', () => {
  beforeEach(() => vi.useFakeTimers({ now: NOW }))
  afterEach(() => vi.useRealTimers())
  it('countdown and describeSchedule default to the current time', () => {
    expect(countdown(NOW + 60_000)).toBe('in 1m 0s')
    const at = new Date(NOW + 3_600_000).toISOString()
    expect(describeSchedule(job({ scheduleKind: 'at', scheduleRaw: at }))).toMatch(/^Once, Today /)
  })
})
