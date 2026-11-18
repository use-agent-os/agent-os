import { describe, expect, it } from 'vitest'
import {
  filterByProtocol,
  formatRate,
  formatShare,
  hasClaimable,
  lpActions,
  lpStatusTone,
  positionTag,
  rangeMarker,
} from './liquidity-logic'
import type { LpAmount, LpPosition } from './types'

const zero: LpAmount = { raw: '0', human: '0', usd: 0 }

function position(over: Partial<LpPosition> = {}): LpPosition {
  return {
    id: '8453:uniswap_v3:1:0xabc',
    chain: { id: 8453, key: 'base', name: 'Base', explorer: 'https://basescan.org' },
    tokenId: '1',
    owner: { address: '0xabc', label: 'Main', inApp: true },
    token: { address: '0x1', symbol: 'WETH', decimals: 18, priceUsd: 2500 },
    quote: { address: '0x2', symbol: 'USDC', decimals: 6, priceUsd: 1 },
    pool: { poolId: '0xpool', feePct: '0.05%', tickSpacing: 10, tvlUsd: null, priceUsd: 2500 },
    range: { priceLower: 2000, priceUpper: 3125 },
    status: 'in-range',
    principal: { base: zero, quote: zero, usd: 0 },
    fees: { base: zero, quote: zero, usd: 0 },
    valueUsd: 0,
    distancePct: null,
    protocol: { key: 'uniswap_v3', name: 'Uniswap V3', kind: 'concentrated' },
    staked: false,
    gauge: null,
    rewards: null,
    share: null,
    ...over,
  }
}

describe('rangeMarker', () => {
  it('places the price on a log scale', () => {
    // 2500 is the geometric middle of 2000 and 3125.
    expect(rangeMarker(position())).toBeCloseTo(0.5, 6)
    const low = position({ token: { ...position().token, priceUsd: 2000 } })
    expect(rangeMarker(low)).toBeCloseTo(0, 6)
  })

  it('pins an out-of-range position to the end it left through', () => {
    expect(rangeMarker(position({ status: 'above-range' }))).toBe(1)
    expect(rangeMarker(position({ status: 'below-range' }))).toBe(0)
    const unpriced = position({
      status: 'above-range',
      token: { ...position().token, priceUsd: null },
    })
    expect(rangeMarker(unpriced)).toBe(1)
  })

  it('has no marker without a range or a price', () => {
    expect(rangeMarker(position({ range: null, status: 'full-range' }))).toBeNull()
    expect(rangeMarker(position({ quote: { ...position().quote, priceUsd: null } }))).toBeNull()
    expect(rangeMarker(position({ range: { priceLower: 5, priceUpper: 5 } }))).toBeNull()
  })

  it('clamps a price that drifted past a bound the status has not caught up with', () => {
    const drifted = position({ token: { ...position().token, priceUsd: 9000 } })
    expect(rangeMarker(drifted)).toBe(1)
  })
})

describe('formatRate', () => {
  it('reads at a glance at every magnitude', () => {
    expect(formatRate(2512.4567)).toBe('2,512')
    expect(formatRate(12.34567)).toBe('12.346')
    expect(formatRate(0.042137)).toBe('0.04214')
    expect(formatRate(0.0000001234)).toBe('0.0₆1234')
    expect(formatRate(3_400_000)).toBe('3.40M')
    expect(formatRate(7_250_000_000)).toBe('7.25B')
  })

  it('shows a dash for nothing', () => {
    expect(formatRate(null)).toBe('—')
    expect(formatRate(0)).toBe('—')
    expect(formatRate(Number.NaN)).toBe('—')
  })
})

describe('rows', () => {
  it('narrows by protocol without reordering', () => {
    const rows = [
      position({ id: 'a' }),
      position({ id: 'b', protocol: { key: 'aero_basic', name: 'Aero Basic', kind: 'pair' } }),
      position({ id: 'c' }),
    ]
    expect(filterByProtocol(rows, null).map((r) => r.id)).toEqual(['a', 'b', 'c'])
    expect(filterByProtocol(rows, 'uniswap_v3').map((r) => r.id)).toEqual(['a', 'c'])
    expect(filterByProtocol(rows, 'uniswap_v2')).toEqual([])
  })

  it('tags an NFT by id and a pair by nothing', () => {
    expect(positionTag(position({ tokenId: '48213' }))).toBe('#48213')
    expect(positionTag(position({ tokenId: null }))).toBeNull()
  })

  it('formats a pool share', () => {
    expect(formatShare(0.1234)).toBe('12.3%')
    expect(formatShare(0.0123)).toBe('1.23%')
    expect(formatShare(0.00001)).toBe('<0.01%')
    expect(formatShare(null)).toBeNull()
    expect(formatShare(0)).toBeNull()
  })

  it('knows when something is waiting to be claimed', () => {
    expect(hasClaimable(position())).toBe(false)
    expect(hasClaimable(position({ fees: null }))).toBe(false)
    const fee = { ...zero, raw: '5' }
    expect(hasClaimable(position({ fees: { base: fee, quote: zero, usd: null } }))).toBe(true)
    const reward = { token: position().token, amount: { ...zero, raw: '9' } }
    expect(hasClaimable(position({ fees: null, rewards: reward }))).toBe(true)
  })

  it('tones a status', () => {
    expect(lpStatusTone('in-range')).toBe('ok')
    expect(lpStatusTone('full-range')).toBe('ok')
    expect(lpStatusTone('above-range')).toBe('warn')
    expect(lpStatusTone('below-range')).toBe('warn')
    expect(lpStatusTone('closed')).toBe('dim')
  })
})

describe('lpActions', () => {
  const v4 = { key: 'uniswap_v4', name: 'Uniswap V4', kind: 'concentrated' } as const
  const owed: LpAmount = { raw: '5', human: '0.000005', usd: 0.01 }

  it('offers remove for a V4 position, and collect when fees are owed', () => {
    expect(lpActions(position({ protocol: v4 }))).toEqual(['remove'])
    const earning = position({ protocol: v4, fees: { base: zero, quote: owed, usd: 0.01 } })
    expect(lpActions(earning)).toEqual(['collect', 'remove'])
  })

  it('offers nothing where the engine has no write', () => {
    // Uniswap V3, Aero and pairs are listed, not managed.
    expect(lpActions(position({ fees: { base: owed, quote: owed, usd: 1 } }))).toEqual([])
    expect(lpActions(position({ protocol: v4, tokenId: null }))).toEqual([])
  })
})
