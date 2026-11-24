// The chart library throws on unsorted, duplicate or non-finite points; the
// payload is cleaned before it gets there.
import { screen, waitFor } from '@testing-library/react'
import { createElement } from 'react'
import { beforeEach, describe, expect, it } from 'vitest'
import '@/test/web'
import { renderWeb } from '@/test/render-web'
import { useConnection } from '@/stores/connection'
import { tradingRpc } from '@/test/web-trading'
import { chartData, PriceChart } from './PriceChart'

describe('chartData', () => {
  it('sorts, floors to whole seconds and keeps the first of a duplicate', () => {
    expect(
      chartData([
        { t: 300.9, c: 3 },
        { t: 100, c: 1 },
        { t: 300.1, c: 4 },
        { t: 200, c: 2 },
      ]),
    ).toEqual([
      { time: 100, value: 1 },
      { time: 200, value: 2 },
      { time: 300, value: 3 },
    ])
  })

  it('drops points that are not finite', () => {
    expect(
      chartData([
        { t: Number.NaN, c: 1 },
        { t: 100, c: Number.NaN },
        { t: 200, c: Number.POSITIVE_INFINITY },
        { t: 300, c: 5 },
      ]),
    ).toEqual([{ time: 300, value: 5 }])
    expect(chartData([])).toEqual([])
  })
})

describe('PriceChart header', () => {
  beforeEach(() => useConnection.getState().setState('connected'))

  it("shows the engine's change over the range, not one worked out from thinned points", async () => {
    const rpc = tradingRpc().handle('trading.chart', {
      source: 'geckoterminal',
      range: '1d',
      points: [
        { t: 100, c: 10 },
        { t: 200, c: 20 },
      ],
      stats: {
        priceUsd: 20,
        priceNative: null,
        quoteSymbol: 'USDC',
        marketCapUsd: null,
        market: null,
        changePct: -12.5,
      },
    })
    renderWeb(createElement(PriceChart, { chainId: 8453, token: '0xabc', symbol: 'ETH' }), { rpc })
    const change = await screen.findByTitle(/change over the range/i)
    await waitFor(() => expect(change.textContent).toMatch(/12\.5/))
    expect(change.textContent).not.toMatch(/100/)
    expect(change.getAttribute('data-tone')).toBe('down')
  })
})
