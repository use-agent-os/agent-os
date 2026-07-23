import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  buildCsv,
  chartRows,
  costSourceBadge,
  csvFilename,
  formatCost,
  formatNum,
  formatRelTime,
  modelBreakdownGrid,
  modelDisplayLabel,
  normalizeRange,
  rangeCutoffMs,
  rangeHiddenHint,
  sessionExpandRows,
  sessionTimestamp,
  sortSessions,
  sourceCompositionHint,
  undatedHiddenCount,
  usageMetrics,
  visibleSessions,
  type UsageRow,
} from './logic'

describe('normalizeRange', () => {
  it('accepts all/7/14/30', () => {
    for (const r of ['all', '7', '14', '30']) expect(normalizeRange(r)).toBe(r)
  })
  it('defaults invalid / null to 7', () => {
    expect(normalizeRange(null)).toBe('7')
    expect(normalizeRange(undefined)).toBe('7')
    expect(normalizeRange('99')).toBe('7')
    expect(normalizeRange('')).toBe('7')
    expect(normalizeRange('garbage')).toBe('7')
  })
  it('coerces non-strings', () => {
    expect(normalizeRange(7 as unknown as string)).toBe('7')
  })
})

describe('formatCost', () => {
  it('null → em dash', () => {
    expect(formatCost(null)).toBe('—')
    expect(formatCost(undefined)).toBe('—')
  })
  it('defaults to 4 decimals', () => {
    expect(formatCost(1.23456)).toBe('$1.2346')
    expect(formatCost(0)).toBe('$0.0000')
  })
  it('honors a custom decimals option', () => {
    expect(formatCost(1.23456, { decimals: 6 })).toBe('$1.234560')
    expect(formatCost(2, { decimals: 2 })).toBe('$2.00')
  })
})

describe('formatNum', () => {
  it('null → em dash', () => {
    expect(formatNum(null)).toBe('—')
  })
  it('millions and thousands abbreviate', () => {
    expect(formatNum(2_500_000)).toBe('2.5M')
    expect(formatNum(1_500)).toBe('1.5K')
    expect(formatNum(999)).toBe('999')
    expect(formatNum(1_000_000)).toBe('1.0M')
    expect(formatNum(1_000)).toBe('1.0K')
  })
})

describe('formatRelTime', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'))
  })
  afterEach(() => vi.useRealTimers())

  it('numeric seconds epoch (< 1e10) is scaled to ms', () => {
    const tenMinAgoSec = Date.now() / 1000 - 600
    expect(formatRelTime(tenMinAgoSec)).toBe('10m ago')
  })
  it('numeric ms epoch passes through', () => {
    expect(formatRelTime(Date.now() - 2 * 3600 * 1000)).toBe('2h ago')
  })
  it('just now / minutes / hours / days buckets', () => {
    expect(formatRelTime(Date.now() - 30_000)).toBe('just now')
    expect(formatRelTime(Date.now() - 3 * 86_400 * 1000)).toBe('3d ago')
  })
  it('invalid → em dash', () => {
    expect(formatRelTime('not-a-date')).toBe('—')
  })
})

describe('sessionTimestamp', () => {
  it('prefers endedAt/ended_at over the rest, snake or camel', () => {
    expect(sessionTimestamp({ endedAt: 5, updated_at: 9 })).toBe(5)
    expect(sessionTimestamp({ ended_at: 5, updated_at: 9 })).toBe(5)
    expect(sessionTimestamp({ updated_at: 9, started_at: 3 })).toBe(9)
    expect(sessionTimestamp({ created_at: 1 })).toBe(1)
  })
  it('returns null when no timestamp field is numeric', () => {
    expect(sessionTimestamp({})).toBeNull()
    expect(sessionTimestamp({ updated_at: '' })).toBeNull()
    expect(sessionTimestamp({ updated_at: 'nope' })).toBeNull()
  })
})

describe('rangeCutoffMs / visibleSessions / undatedHiddenCount', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-30T00:00:00Z'))
  })
  afterEach(() => vi.useRealTimers())

  it('all → null cutoff', () => {
    expect(rangeCutoffMs('all')).toBeNull()
  })
  it('7 → now − 7d', () => {
    expect(rangeCutoffMs('7')).toBe(Date.now() - 7 * 86_400_000)
  })
  // Built lazily so the epochs derive from the FAKE clock set in beforeEach.
  const makeRows = (): UsageRow[] => [
    { session: 'recent', updated_at: Date.now() - 2 * 86_400_000 },
    { session: 'old', updated_at: Date.now() - 20 * 86_400_000 },
    { session: 'undated' },
  ]
  it('all keeps every row (including undated)', () => {
    expect(visibleSessions(makeRows(), 'all').map((r) => r.session)).toEqual([
      'recent',
      'old',
      'undated',
    ])
  })
  it('7d keeps only rows dated within window (undated dropped)', () => {
    expect(visibleSessions(makeRows(), '7').map((r) => r.session)).toEqual(['recent'])
  })
  it('undatedHiddenCount is 0 on all, counts undated otherwise', () => {
    expect(undatedHiddenCount(makeRows(), 'all')).toBe(0)
    expect(undatedHiddenCount(makeRows(), '7')).toBe(1)
  })
  it('rangeHiddenHint pluralizes', () => {
    expect(rangeHiddenHint(makeRows(), '7')).toBe('1 undated legacy session hidden')
    expect(rangeHiddenHint([{}, {}], '7')).toBe('2 undated legacy sessions hidden')
    expect(rangeHiddenHint(makeRows(), 'all')).toBe('')
  })
})

describe('usageMetrics', () => {
  it('sums input/output/cost/cache across rows (snake or camel)', () => {
    const rows: UsageRow[] = [
      { input_tokens: 100, output_tokens: 50, cost_usd: 1, cache_read_tokens: 10 },
      { inputTokens: 200, outputTokens: 25, costUsd: 2, cacheWriteTokens: 5 },
    ]
    const m = usageMetrics(rows)
    expect(m.input).toBe(300)
    expect(m.output).toBe(75)
    expect(m.totalTokens).toBe(375)
    expect(m.cost).toBe(3)
    expect(m.cacheRead).toBe(10)
    expect(m.cacheWrite).toBe(5)
    expect(m.sessions).toBe(2)
    expect(m.avgCost).toBe(1.5)
  })
  it('avgCost is null on an empty list', () => {
    expect(usageMetrics([]).avgCost).toBeNull()
  })
})

describe('costSourceBadge', () => {
  it('maps known sources to label/tooltip/class', () => {
    expect(costSourceBadge({ cost_source: 'provider_billed' })).toMatchObject({
      label: 'Actual',
      cls: 'provider_billed',
    })
    expect(costSourceBadge({ cost_source: 'provider_billed_prorated' })).toMatchObject({
      label: 'Actual',
      cls: 'provider_billed_prorated',
    })
    expect(costSourceBadge({ cost_source: 'agentos_estimate' }).label).toBe('Estimated')
    expect(costSourceBadge({ cost_source: 'mixed' }).label).toBe('Mixed')
    expect(costSourceBadge({ cost_source: 'unavailable' }).label).toBe('Unpriced')
    expect(costSourceBadge({}).label).toBe('None')
  })
  it('unknown source falls back to none class', () => {
    expect(costSourceBadge({ cost_source: 'weird' }).cls).toBe('none')
  })
  it('ephemeral overrides label + tooltip', () => {
    const b = costSourceBadge({ cost_source: 'provider_billed', cost_ephemeral: true })
    expect(b.label).toBe('Ephemeral')
    expect(b.ephemeral).toBe(true)
  })
  it('reads camelCase costSource', () => {
    expect(costSourceBadge({ costSource: 'agentos_estimate' }).label).toBe('Estimated')
  })
})

describe('sourceCompositionHint', () => {
  it('joins non-zero label counts, lowercased', () => {
    const rows: UsageRow[] = [
      { cost_source: 'provider_billed' },
      { cost_source: 'provider_billed' },
      { cost_source: 'agentos_estimate' },
      { cost_source: 'unavailable' },
    ]
    expect(sourceCompositionHint(rows)).toBe('actual 2 · estimated 1 · unpriced 1')
  })
  it('empty when nothing matches counted labels', () => {
    expect(sourceCompositionHint([])).toBe('')
  })
})

describe('sortSessions', () => {
  const rows: UsageRow[] = [
    { session: 'b', updated_at: 200, input_tokens: 5, cost_usd: 3 },
    { session: 'a', updated_at: 100, input_tokens: 9, cost_usd: 1 },
    { session: 'c', updated_at: 300, input_tokens: 1, cost_usd: 2 },
  ]
  it('numeric column ascending / descending', () => {
    expect(sortSessions(rows, 'input_tokens', true).map((r) => r.session)).toEqual(['c', 'b', 'a'])
    expect(sortSessions(rows, 'input_tokens', false).map((r) => r.session)).toEqual(['a', 'b', 'c'])
  })
  it('string column (session) lowercased', () => {
    expect(sortSessions(rows, 'session', true).map((r) => r.session)).toEqual(['a', 'b', 'c'])
  })
  it('updated_at sorts by derived timestamp', () => {
    expect(sortSessions(rows, 'updated_at', false).map((r) => r.session)).toEqual(['c', 'b', 'a'])
  })
  it('does not mutate the input', () => {
    const copy = [...rows]
    sortSessions(rows, 'cost_usd', true)
    expect(rows).toEqual(copy)
  })
})

describe('chartRows', () => {
  const rows: UsageRow[] = [
    { session: 'zero', input_tokens: 0, output_tokens: 0, cost_usd: 5 },
    { session: 'big', input_tokens: 800, output_tokens: 200, cost_usd: 1 },
    { session: 'small', input_tokens: 100, output_tokens: 100, cost_usd: 4 },
  ]
  it('drops zero-token rows and sorts by total tokens desc in tokens mode', () => {
    const c = chartRows(rows, 'tokens')
    expect(c.bars.map((b) => b.key)).toEqual(['big', 'small'])
    expect(c.max).toBe(1000)
  })
  it('input+output percentages scale to max', () => {
    const c = chartRows(rows, 'tokens')
    const big = c.bars[0]!
    expect(big.inputPct).toBeCloseTo(80)
    expect(big.outputPct).toBeCloseTo(20)
    expect(big.valueLabel).toBe('1.0K')
  })
  it('cost mode sorts by cost desc, single fill, cost value label', () => {
    const c = chartRows(rows, 'cost')
    expect(c.bars.map((b) => b.key)).toEqual(['small', 'big'])
    const small = c.bars[0]!
    expect(small.outputPct).toBe(0)
    expect(small.inputPct).toBeCloseTo(100)
    expect(small.valueLabel).toBe('$4.0000')
  })
  it('caps to top 20 and reports the pool size', () => {
    const many: UsageRow[] = Array.from({ length: 25 }, (_, i) => ({
      session: `s${i}`,
      input_tokens: i + 1,
      output_tokens: 0,
    }))
    const c = chartRows(many, 'tokens')
    expect(c.bars).toHaveLength(20)
    expect(c.poolSize).toBe(25)
    expect(c.shown).toBe(20)
  })
  it('truncates long labels to 24 chars + ellipsis', () => {
    const c = chartRows([{ session: 'x'.repeat(40), input_tokens: 5 }], 'tokens')
    expect(c.bars[0]!.label).toBe('x'.repeat(24) + '…')
  })
  it('empty pool → no bars, max defaults to 1', () => {
    const c = chartRows([{ input_tokens: 0, output_tokens: 0 }], 'tokens')
    expect(c.bars).toHaveLength(0)
    expect(c.max).toBe(1)
  })
})

describe('modelDisplayLabel', () => {
  it('single-model breakdown → that model', () => {
    expect(modelDisplayLabel({ modelBreakdown: [{ model: 'openai/gpt' }] })).toBe('openai/gpt')
  })
  it('multi-model breakdown → auto · N models', () => {
    expect(modelDisplayLabel({ modelBreakdown: [{ model: 'a' }, { model: 'b' }] })).toBe(
      'auto · 2 models',
    )
  })
  it('no breakdown → row.model, else em dash', () => {
    expect(modelDisplayLabel({ model: 'x/y' })).toBe('x/y')
    expect(modelDisplayLabel({})).toBe('—')
  })
})

describe('sessionExpandRows', () => {
  it('only builds when breakdown has >1 models; splits provider/name; computes share', () => {
    const row: UsageRow = {
      modelBreakdown: [
        { model: 'openai/gpt-4', inputTokens: 100, outputTokens: 0, costUsd: 3 },
        { model: 'anthropic/claude', inputTokens: 50, outputTokens: 50, costUsd: 1 },
      ],
    }
    const ex = sessionExpandRows(row)
    expect(ex.totalCost).toBe(4)
    expect(ex.totalTokens).toBe(200)
    expect(ex.anyProrated).toBe(false)
    expect(ex.rows[0]).toMatchObject({ provider: 'openai', name: 'gpt-4', tokens: 100, cost: 3 })
    expect(ex.rows[0]!.sharePct).toBeCloseTo(75)
  })
  it('flags prorated when any model is provider_billed_prorated', () => {
    const ex = sessionExpandRows({
      modelBreakdown: [{ model: 'a/b', costSource: 'provider_billed_prorated' }, { model: 'c/d' }],
    })
    expect(ex.anyProrated).toBe(true)
  })
})

describe('modelBreakdownGrid', () => {
  it('groups by per-model usage, distinct-session count, sorts by cost desc', () => {
    const rows: UsageRow[] = [
      {
        session: 's1',
        modelBreakdown: [
          { model: 'openai/gpt', inputTokens: 100, outputTokens: 0, costUsd: 1 },
          { model: 'x/y', inputTokens: 10, outputTokens: 0, costUsd: 5 },
        ],
      },
      { session: 's2', model: 'openai/gpt', input_tokens: 50, output_tokens: 0, cost_usd: 2 },
    ]
    const grid = modelBreakdownGrid(rows)
    // x/y (cost 5) first, then openai/gpt (cost 3)
    expect(grid.models.map((m) => m.model)).toEqual(['x/y', 'openai/gpt'])
    const gpt = grid.models.find((m) => m.model === 'openai/gpt')!
    expect(gpt.inputTokens).toBe(150)
    expect(gpt.costUsd).toBe(3)
    expect(gpt.sessions).toBe(2)
    expect(grid.totalCost).toBe(8)
    expect(gpt.provider).toBe('openai')
    expect(gpt.name).toBe('gpt')
    expect(gpt.sharePct).toBeCloseTo((3 / 8) * 100)
  })
  it('empty rows → empty grid', () => {
    expect(modelBreakdownGrid([]).models).toEqual([])
  })
})

describe('buildCsv / csvFilename', () => {
  it('emits the header row and quote-escaped fields with 6-decimal costs', () => {
    const rows: UsageRow[] = [
      {
        session: 'agent:main:chat:a"b',
        input_tokens: 100,
        output_tokens: 50,
        cost_usd: 1.5,
        cost_source: 'provider_billed',
        model: 'openai/gpt',
      },
    ]
    const csv = buildCsv(rows)
    const lines = csv.split('\n')
    expect(lines[0]).toContain('"session"')
    expect(lines[0]).toContain('"cost_usd"')
    // quote-escaped embedded quote
    expect(lines[1]).toContain('"agent:main:chat:a""b"')
    expect(lines[1]).toContain('"1.500000"')
  })
  it('filename reflects the range', () => {
    expect(csvFilename('all')).toBe('agentos-usage-all.csv')
    expect(csvFilename('7')).toBe('agentos-usage-7d.csv')
  })
})
