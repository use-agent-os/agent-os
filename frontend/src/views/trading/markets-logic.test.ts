import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import {
  counterpartySymbol,
  dexLabel,
  feeLabel,
  formatPoolPrice,
  formatRatio,
  formatUsdShort,
  isNativeCounterparty,
  isStockToken,
  isStockTokenName,
  launcherRepeatsDex,
  formatPremiumPct,
  offersDeeper,
  offersReadAgain,
  pairParts,
  pickMarketsTarget,
  poolAge,
  poolSwapTokens,
  premiumTone,
  rankMatches,
  rowPair,
  showsCounterpartyName,
  tvlStepLabel,
} from './markets-logic'
import type { MarketsPayload, SearchToken } from './types'

const FIXTURE = 'src/views/trading/__fixtures__/markets/nvda.json'
const NVDA_MARKETS = JSON.parse(readFileSync(FIXTURE, 'utf8')) as MarketsPayload
const NVDA_ADDRESS = '0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec'

function found(extra: Partial<SearchToken>): SearchToken {
  return {
    chainId: 4663,
    address: NVDA_ADDRESS,
    symbol: 'NVDA',
    name: 'NVIDIA • Robinhood Token',
    decimals: 18,
    logoUrl: null,
    native: false,
    verified: true,
    stockToken: true,
    priceUsd: 240.3,
    liquidityUsd: null,
    ...extra,
  }
}

describe('markets-logic · words a row wears', () => {
  const now = Date.parse('2026-10-06T04:12:00Z')
  const ago = (s: number) => new Date(now - s * 1000).toISOString()

  it('says a pool’s age in minutes, hours, days, months, years', () => {
    expect(poolAge(ago(45 * 60), now)).toBe('45m')
    expect(poolAge(ago(5 * 3600), now)).toBe('5h')
    expect(poolAge(ago(3 * 86400), now)).toBe('3d')
    // Two weeks reads in days, not weeks: the card says "3d", "2mo".
    expect(poolAge(ago(14 * 86400), now)).toBe('14d')
    expect(poolAge(ago(73 * 86400), now)).toBe('2mo')
    expect(poolAge(ago(400 * 86400), now)).toBe('1y')
    expect(poolAge(null, now)).toBe('—')
    expect(poolAge('not a date', now)).toBe('—')
  })

  it('names the venue with its version, and falls back to the dex id', () => {
    expect(dexLabel({ id: 'uniswap-v4-robinhood', label: 'Uniswap', version: 'v4' })).toBe(
      'Uniswap v4',
    )
    expect(dexLabel({ id: 'bankr-robinhood', label: 'Bankr', version: null })).toBe('Bankr')
    expect(dexLabel({ id: 'ramses-v3-robinhood', label: '', version: 'v3' })).toBe('Ramses v3')
  })

  it('prints the fee tier, or nothing when the pool name had none', () => {
    expect(feeLabel(0.05)).toBe('0.05%')
    expect(feeLabel(0.025)).toBe('0.025%')
    expect(feeLabel(1)).toBe('1%')
    expect(feeLabel(null)).toBeNull()
  })

  it('labels the Min TVL steps', () => {
    expect([1_000, 10_000, 100_000].map(tvlStepLabel)).toEqual(['$1k', '$10k', '$100k'])
  })

  it('compacts TVL and volume past a thousand dollars', () => {
    expect(formatUsdShort(4_732_293)).toBe('$4.7M')
    expect(formatUsdShort(842_113)).toBe('$842.1K')
    expect(formatUsdShort(12_345)).toBe('$12.3K')
    // A round figure keeps no trailing zero on every Node: 22 used to print "$131.0K".
    expect(formatUsdShort(131_000)).toBe('$131K')
    expect(formatUsdShort(2_000_000)).toBe('$2M')
    expect(formatUsdShort(950)).toBe('$950.00')
    expect(formatUsdShort(null)).toBe('—')
  })

  it('keeps four significant digits on a sub-dollar pool price', () => {
    expect(formatPoolPrice(0.1131)).toBe('$0.1131')
    expect(formatPoolPrice(0.0042)).toBe('$0.0042')
    expect(formatPoolPrice(0.00001748)).toBe('$0.0₄1748')
    expect(formatPoolPrice(240.85)).toBe('$240.85')
    expect(formatPoolPrice(null)).toBe('—')
  })

  it('prints one token in another without a currency sign', () => {
    expect(formatRatio(0.000471)).toBe('0.0₃471')
    expect(formatRatio(0.1131)).toBe('0.1131')
    expect(formatRatio(0.0573)).toBe('0.0573')
    expect(formatRatio(240.85)).toBe('240.85')
    expect(formatRatio(2.78071)).toBe('2.7807')
    expect(formatRatio(1234.567)).toBe('1,234.57')
    expect(formatRatio(null)).toBe('—')
  })

  it('splits a pair into its two symbols', () => {
    expect(pairParts('AI/NVDA')).toEqual(['AI', 'NVDA'])
    expect(pairParts('NVDA / USDG')).toEqual(['NVDA', 'USDG'])
    expect(pairParts('ODD')).toEqual(['ODD', ''])
  })
})

describe('markets-logic · Stock Tokens', () => {
  it('reads the suffix the way the engine does, truncation included', () => {
    expect(isStockTokenName('NVIDIA • Robinhood Token')).toBe(true)
    expect(isStockTokenName('International Business Machines • Robinhood Toke')).toBe(true)
    expect(isStockTokenName('Some Long Company Name • Robinhood T')).toBe(true)
    // No bullet, no Stock Token, however it is spelt.
    expect(isStockTokenName(' NVIDIA Robinhood Token ')).toBe(false)
    expect(isStockTokenName('memestock TSLA')).toBe(false)
    expect(isStockTokenName('Fake • Coin')).toBe(false)
  })

  it('trusts the engine’s flag, and the name only for a verified Robinhood token', () => {
    expect(isStockToken({ chainId: 8453, name: 'x', verified: false, stockToken: true })).toBe(true)
    expect(isStockToken({ chainId: 4663, name: 'Apple • Robinhood Token', verified: true })).toBe(
      true,
    )
    expect(isStockToken({ chainId: 4663, name: 'Apple • Robinhood Token', verified: false })).toBe(
      false,
    )
    expect(isStockToken({ chainId: 8453, name: 'Apple • Robinhood Token', verified: true })).toBe(
      false,
    )
  })
})

describe('markets-logic · which token a query means', () => {
  const stock = found({})
  const lookalike = found({
    address: '0x1111111111111111111111111111111111111111',
    name: 'memestock NVDA',
    verified: false,
    stockToken: false,
  })
  const baseDegen = found({
    chainId: 8453,
    address: '0x4ed4e862860bed51a9570b96d89af5e1b0efefed',
    symbol: 'DEGEN',
    name: 'Degen',
    stockToken: false,
  })

  it('takes NVDA typed on Base to Robinhood Chain’s verified Stock Token', () => {
    const pick = pickMarketsTarget('nvda', [lookalike, stock], 8453, false)
    expect(pick).toMatchObject({ chainId: 4663, target: NVDA_ADDRESS })
    expect(pick.token?.symbol).toBe('NVDA')
  })

  it('keeps a chain the user chose, and hands the engine the symbol when it has no match there', () => {
    expect(pickMarketsTarget('NVDA', [stock], 8453, true)).toEqual({
      chainId: 8453,
      target: 'NVDA',
      token: null,
      candidates: [],
    })
  })

  it('falls back to a verified match on any chain, never to an unverified one', () => {
    expect(pickMarketsTarget('DEGEN', [baseDegen], 4663, false)).toMatchObject({
      chainId: 8453,
      target: baseDegen.address,
    })
    expect(pickMarketsTarget('NVDA', [lookalike], 4663, false)).toEqual({
      chainId: 4663,
      target: 'NVDA',
      token: null,
      candidates: [],
    })
  })

  it('takes an address as given, on the current chain', () => {
    expect(pickMarketsTarget(` ${NVDA_ADDRESS} `, [], 4663, false)).toEqual({
      chainId: 4663,
      target: NVDA_ADDRESS,
      token: null,
      candidates: [],
    })
  })

  // Live test 2026-10-06: "AI" took "AI PIN" (~$15k liquidity), the search's
  // first exact match, instead of Artificial Inu (~$131k).
  describe('several verified tokens share the symbol', () => {
    const pin = found({
      address: '0x5555555555555555555555555555555555555555',
      symbol: 'AI',
      name: 'AI PIN',
      stockToken: false,
      liquidityUsd: 15_000,
    })
    const inu = found({
      address: '0x2e8c4b1f0a3d6e7c9b5a4f3e2d1c0b9a8f7e1e18',
      symbol: 'AI',
      name: 'Artificial Inu',
      stockToken: false,
      liquidityUsd: 131_000,
    })
    const dry = found({
      address: '0x6666666666666666666666666666666666666666',
      symbol: 'AI',
      name: 'No Liquidity AI',
      stockToken: false,
      liquidityUsd: null,
    })

    it('takes the deepest one, not the first the search returned', () => {
      const pick = pickMarketsTarget('ai', [pin, dry, inu], 4663, false)
      expect(pick).toMatchObject({ chainId: 4663, target: inu.address })
      expect(pick.token?.name).toBe('Artificial Inu')
      // Every match stays on offer, the pick first, unknown liquidity last.
      expect(pick.candidates.map((c) => c.name)).toEqual([
        'Artificial Inu',
        'AI PIN',
        'No Liquidity AI',
      ])
      // Same answer on a chain the user chose.
      expect(pickMarketsTarget('AI', [pin, inu], 4663, true).target).toBe(inu.address)
    })

    it('prefers a Stock Token over a deeper community token of the same symbol', () => {
      const stockAi = found({
        address: '0x7777777777777777777777777777777777777777',
        symbol: 'AI',
        name: 'C3.ai • Robinhood Token',
        stockToken: true,
        liquidityUsd: 2_000,
      })
      expect(rankMatches([inu, stockAi, pin]).map((c) => c.name)).toEqual([
        'C3.ai • Robinhood Token',
        'Artificial Inu',
        'AI PIN',
      ])
      expect(pickMarketsTarget('AI', [inu, stockAi], 4663, true).target).toBe(stockAi.address)
    })

    it('lists only the pick’s chain, whichever chain answered first', () => {
      const baseAi = found({
        chainId: 8453,
        address: '0x8888888888888888888888888888888888888888',
        symbol: 'AI',
        name: 'Base AI',
        stockToken: false,
        liquidityUsd: 900_000,
      })
      const pick = pickMarketsTarget('AI', [baseAi, pin, inu], 4663, false)
      // The desk's chain wins over a deeper match elsewhere.
      expect(pick.target).toBe(inu.address)
      // Base's AI is not offered while Robinhood Chain's markets are read.
      expect(pick.candidates.map((c) => c.name)).toEqual(['Artificial Inu', 'AI PIN'])
      // A chosen chain lists only its own.
      expect(
        pickMarketsTarget('AI', [baseAi, pin, inu], 4663, true).candidates.map((c) => c.name),
      ).toEqual(['Artificial Inu', 'AI PIN'])
    })

    // Live test 2026-10-06 (second round): "AI" showed 4 matches, then 2 on
    // the next search — both chains' answers and debounce races mixed in.
    it('answers the same list for the same search: deduped, ranked, one chain', () => {
      const stockAi = found({
        address: '0x7777777777777777777777777777777777777777',
        symbol: 'AI',
        name: 'C3.ai • Robinhood Token',
        stockToken: true,
        liquidityUsd: 2_000,
      })
      const baseAi = found({
        chainId: 8453,
        address: '0x8888888888888888888888888888888888888888',
        symbol: 'AI',
        name: 'Base AI',
        stockToken: false,
        liquidityUsd: 900_000,
      })
      // The same token twice (two sources), once with a stale liquidity and
      // its address in another case.
      const inuAgain = {
        ...inu,
        address: inu.address.toUpperCase().replace('0X', '0x'),
        liquidityUsd: 90_000,
      }
      const twin = found({
        address: '0x4444444444444444444444444444444444444444',
        symbol: 'AI',
        name: 'AI Twin',
        stockToken: false,
        liquidityUsd: 15_000,
      })
      const rows = [baseAi, pin, inuAgain, dry, stockAi, inu, twin]
      const expected = [
        'C3.ai • Robinhood Token',
        'Artificial Inu',
        'AI Twin',
        'AI PIN',
        'No Liquidity AI',
      ]
      // Every arrival order, either chain first: one answer.
      for (const order of [
        rows,
        [...rows].reverse(),
        [inu, twin, stockAi, dry, pin, baseAi, inuAgain],
      ]) {
        for (const explicit of [false, true]) {
          const pick = pickMarketsTarget('AI', order, 4663, explicit)
          expect(
            pick.candidates.map((c) => c.name),
            String(explicit),
          ).toEqual(expected)
          expect(pick.target).toBe(stockAi.address)
        }
      }
      // The deeper duplicate is the one kept.
      const kept = pickMarketsTarget('AI', rows, 4663, true).candidates.find(
        (c) => c.name === 'Artificial Inu',
      )
      expect(kept?.liquidityUsd).toBe(131_000)
      // Equal liquidity ties break on the address, not on arrival.
      expect(rankMatches([twin, pin]).map((c) => c.name)).toEqual(['AI Twin', 'AI PIN'])
      expect(rankMatches([pin, twin]).map((c) => c.name)).toEqual(['AI Twin', 'AI PIN'])
      // Unverified and inexact rows never count.
      expect(
        pickMarketsTarget(
          'AI',
          [
            { ...pin, verified: false },
            { ...inu, symbol: 'AIX' },
          ],
          4663,
          true,
        ).candidates,
      ).toEqual([])
    })
  })
})

describe('markets-logic · what a row says', () => {
  const ai = NVDA_MARKETS.sections.quote[0]!
  const usdg = NVDA_MARKETS.sections.base[0]!
  const ZERO = '0x0000000000000000000000000000000000000000'

  it('names the native coin ETH, never the zero address', () => {
    const eth = {
      ...usdg,
      pair: 'NVDA/0x0000…0000',
      counterparty: { ...usdg.counterparty, address: ZERO, symbol: '0x0000…0000', native: true },
    }
    expect(isNativeCounterparty(eth.counterparty)).toBe(true)
    expect(counterpartySymbol(eth.counterparty)).toBe('ETH')
    expect(rowPair(eth, 'NVDA')).toEqual(['NVDA', 'ETH'])
    expect(rowPair({ ...eth, side: 'quote' }, 'NVDA')).toEqual(['ETH', 'NVDA'])
    // The zero address alone is enough, for an engine that sends no flag.
    const bare = { ...eth.counterparty, native: undefined }
    expect(counterpartySymbol(bare)).toBe('ETH')
    // The ticket gets ETH as the native coin.
    const legs = poolSwapTokens(NVDA_MARKETS, {
      ...eth,
      swap: { chainId: 4663, tokenIn: NVDA_ADDRESS, tokenOut: ZERO },
    })
    expect(legs.tokenOut).toMatchObject({ symbol: 'ETH', native: true, verified: true })
    // Anything else keeps the engine's pair.
    expect(rowPair(ai, 'NVDA')).toEqual(['AI', 'NVDA'])
  })

  it('names the counterparty after the pair for a lookalike or a same-symbol token', () => {
    expect(showsCounterpartyName(ai, 'NVDA')).toBe(false)
    const lookalike = { ...ai, counterparty: { ...ai.counterparty, lookalike: true } }
    expect(showsCounterpartyName(lookalike, 'NVDA')).toBe(true)
    const twin = { ...ai, counterparty: { ...ai.counterparty, symbol: 'nvda', name: 'Nvda Inu' } }
    expect(showsCounterpartyName(twin, 'NVDA')).toBe(true)
    // Nothing to show without a name.
    expect(
      showsCounterpartyName({ ...twin, counterparty: { ...twin.counterparty, name: '' } }, 'NVDA'),
    ).toBe(false)
  })

  it('drops the launcher pill when it would repeat the venue', () => {
    expect(launcherRepeatsDex(ai)).toBe(true)
    expect(launcherRepeatsDex({ ...ai, launcher: 'bankr' })).toBe(true)
    expect(
      launcherRepeatsDex({
        ...ai,
        dex: { id: 'uniswap-v4-robinhood', label: 'Uniswap', version: 'v4' },
      }),
    ).toBe(false)
    expect(launcherRepeatsDex({ ...ai, launcher: null })).toBe(false)
  })

  it('offers Deeper only when the page cap ended a read with more pools, and not twice', () => {
    expect(offersDeeper({ pageCapHit: true }, false)).toBe(true)
    expect(offersDeeper({ pageCapHit: true }, true)).toBe(false)
    // A limit-cut read: a deeper read finds nothing more.
    expect(offersDeeper({ pageCapHit: false }, false)).toBe(false)
    // An older engine says nothing: no promise it cannot keep.
    expect(offersDeeper({}, false)).toBe(false)
  })

  it('offers Deeper again after a 429, and Read again when the read was already deep', () => {
    expect(offersDeeper({ pageCapHit: false, rateLimited: true }, false)).toBe(true)
    expect(offersDeeper({ pageCapHit: false, rateLimited: true }, true)).toBe(false)
    expect(offersReadAgain({ rateLimited: true }, true)).toBe(true)
    expect(offersReadAgain({ rateLimited: true }, false)).toBe(false)
    expect(offersReadAgain({ rateLimited: false }, true)).toBe(false)
    expect(offersReadAgain({}, true)).toBe(false)
  })

  it('prints a premium under 0.05 % either way as a flat 0.0%, never −0.0%', () => {
    for (const pct of [0, -0.0, -0.004, 0.004, -0.049, 0.049]) {
      expect(formatPremiumPct(pct), String(pct)).toBe('0.0%')
      expect(premiumTone(pct), String(pct)).toBe('flat')
    }
    expect(formatPremiumPct(0.4)).toBe('+0.4%')
    expect(premiumTone(0.4)).toBe('up')
    expect(formatPremiumPct(-0.06)).toBe('−0.1%')
    expect(premiumTone(-0.06)).toBe('down')
    expect(formatPremiumPct(null)).toBe('—')
  })
})

describe('markets-logic · a row’s Swap', () => {
  it('sells the token for the counterparty, as full Token objects for the ticket', () => {
    const ai = NVDA_MARKETS.sections.quote[0]!
    const legs = poolSwapTokens(NVDA_MARKETS, ai)
    expect(legs.chainId).toBe(4663)
    expect(legs.tokenIn).toEqual({
      chainId: 4663,
      address: NVDA_ADDRESS,
      symbol: 'NVDA',
      name: 'NVIDIA • Robinhood Token',
      decimals: 18,
      logoUrl: null,
      native: false,
      verified: true,
      stockToken: true,
    })
    expect(legs.tokenOut).toMatchObject({ symbol: 'AI', name: 'Artificial Inu', verified: false })
    expect(legs.tokenOut.address).toBe(ai.counterparty.address)
  })

  it('follows the direction `swap` names, whichever side the token is on', () => {
    const usdg = NVDA_MARKETS.sections.base[0]!
    const reversed = {
      ...usdg,
      swap: { chainId: 4663, tokenIn: usdg.counterparty.address, tokenOut: NVDA_ADDRESS },
    }
    const legs = poolSwapTokens(NVDA_MARKETS, reversed)
    expect(legs.tokenIn.symbol).toBe('USDG')
    expect(legs.tokenIn.decimals).toBe(6)
    expect(legs.tokenOut.symbol).toBe('NVDA')
  })
})
