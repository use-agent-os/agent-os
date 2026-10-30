import { describe, expect, it } from 'vitest'
import { badgeText, estimateChipText, formatElapsed, formatMs, modelPriceHint, usd } from './price'
import { MODELS } from './test-fixtures'
import type { EstimateResult, Price } from './types'

const est = (total: Price, cached: string[] = []): EstimateResult => ({
  nodes: {},
  total,
  will_run: [],
  cached,
})

describe('estimate chip', () => {
  it('free', () => {
    expect(estimateChipText(est({ kind: 'free', approximate: false }))).toBe('This run · free')
  })

  it('usd, approximate or exact, with the cached count', () => {
    expect(estimateChipText(est({ kind: 'usd', usd: 0.29, approximate: true }, ['n2', 'n3']))).toBe(
      'This run ~$0.29 · 2 cached',
    )
    expect(estimateChipText(est({ kind: 'usd', usd: 1.5, approximate: false }))).toBe(
      'This run $1.50',
    )
    expect(estimateChipText(est({ kind: 'usd', usd: 0.004, approximate: true }))).toBe(
      'This run <$0.01',
    )
  })

  it('range', () => {
    expect(estimateChipText(est({ kind: 'range', min: 0.06, max: 0.24, approximate: true }))).toBe(
      'This run ~$0.06–$0.24',
    )
    expect(estimateChipText(est({ kind: 'range', min: 0.1, max: 0.1, approximate: false }))).toBe(
      'This run $0.10',
    )
  })

  it('unknown', () => {
    expect(estimateChipText(est({ kind: 'unknown', approximate: true }, ['n1']))).toBe(
      'This run · cost unknown · 1 cached',
    )
  })

  it('no estimate yet says nothing', () => {
    expect(estimateChipText(null)).toBe('')
  })
})

describe('approximate prices (one "~", never "~<$0.01")', () => {
  it('a single amount under a cent is "<$0.01", with no "~" in front', () => {
    expect(badgeText({ kind: 'usd', usd: 0.003, approximate: true })).toBe('<$0.01')
    expect(badgeText({ kind: 'range', min: 0.002, max: 0.004, approximate: true })).toBe('<$0.01')
  })

  it('a range starting under a cent shows the small end, not "<$0.01–"', () => {
    expect(badgeText({ kind: 'range', min: 0.003, max: 0.1, approximate: true })).toBe(
      '~$0.003–$0.10',
    )
  })

  it('the toolbar chip marks approximation the same way', () => {
    expect(estimateChipText(est({ kind: 'usd', usd: 0.27, approximate: true }))).toBe(
      'This run ~$0.27',
    )
  })
})

describe('badges and hints', () => {
  it('node badge is terse; free hides it', () => {
    expect(badgeText({ kind: 'usd', usd: 0.24, approximate: true })).toBe('~$0.24')
    expect(badgeText({ kind: 'usd', usd: 0.27, approximate: false })).toBe('$0.27')
    expect(badgeText({ kind: 'usd', usd: 0.002, approximate: true })).toBe('<$0.01')
    expect(badgeText({ kind: 'free', approximate: false })).toBeNull()
    expect(badgeText({ kind: 'unknown', approximate: true })).toBe('?')
    expect(badgeText(undefined)).toBeNull()
  })

  it('money', () => {
    expect(usd(0)).toBe('$0')
    expect(usd(0.039)).toBe('$0.04')
    expect(usd(123.4)).toBe('$123')
  })

  it('model picker shows the cheapest listed image, marked approximate', () => {
    expect(modelPriceHint(MODELS.models[0]!)).toBe('~$0.04')
    expect(modelPriceHint({ ...MODELS.models[0]!, price: null })).toBeNull()
  })

  it('durations', () => {
    expect(formatMs(450)).toBe('450ms')
    expect(formatMs(2100)).toBe('2.1s')
    expect(formatMs(83_000)).toBe('1m 23s')
    expect(formatMs(undefined)).toBeNull()
    expect(formatElapsed(47_400)).toBe('0:47')
    expect(formatElapsed(125_000)).toBe('2:05')
    expect(formatElapsed(3_723_000)).toBe('1:02:03')
  })
})
