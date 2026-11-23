// The chart library throws on unsorted, duplicate or non-finite points; the
// payload is cleaned before it gets there.
import { describe, expect, it } from 'vitest'
import { chartData } from './PriceChart'

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
