import { describe, expect, it } from 'vitest'
import { dateGroup, shortAge } from './relative-time'

const NOW = new Date('2026-09-09T15:00:00').getTime()
const H = 3_600_000
const D = 24 * H

describe('shortAge', () => {
  it('scales units with distance', () => {
    expect(shortAge(NOW - 10_000, NOW)).toBe('now')
    expect(shortAge(NOW - 5 * 60_000, NOW)).toBe('5m')
    expect(shortAge(NOW - 3 * H, NOW)).toBe('3h')
    expect(shortAge(NOW - 4 * D, NOW)).toBe('4d')
    expect(shortAge(NOW - 16 * D, NOW)).toBe('2w')
    expect(shortAge(NOW - 70 * D, NOW)).toBe('2mo')
  })
  it('never goes negative for future stamps', () => {
    expect(shortAge(NOW + D, NOW)).toBe('now')
  })
})

describe('dateGroup', () => {
  it('buckets by calendar day, not 24h windows', () => {
    expect(dateGroup(NOW - 2 * H, NOW)).toEqual({ kind: 'today' })
    expect(dateGroup(NOW - 20 * H, NOW)).toEqual({ kind: 'yesterday' })
    expect(dateGroup(NOW - 4 * D, NOW)).toEqual({ kind: 'week' })
    expect(dateGroup(NOW - 20 * D, NOW)).toEqual({ kind: 'month', label: 'August' })
  })
  it('adds the year once it differs', () => {
    expect(dateGroup(NOW - 300 * D, NOW)).toEqual({ kind: 'month', label: 'November 2025' })
  })
})
