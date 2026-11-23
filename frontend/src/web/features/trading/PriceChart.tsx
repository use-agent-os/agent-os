import { X } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { t } from '@/i18n'
import '@/i18n/en/webTrading'
import { useTheme } from '@/stores/theme'
import { chartTheme } from '@/views/chat/transcript/chart'
import { errorText, formatPct, formatPrice, pnlTone } from '@/views/trading/logic'
import type { ChartRange } from '@/views/trading/types'
import { Button } from '@/web/components/ui/button'
import { Card, CardHeader } from '@/web/components/ui/card'
import { SegmentedControl } from '@/web/components/ui/segmented-control'
import { ErrorState, Skeleton } from '@/web/components/ui/states'
import { useChart } from './data'
import { clampSym } from './safety'

// A token's price line from `trading.chart`. The chart library loads on
// first use, inside this feature's chunk, never with the console's shell.

const RANGES: readonly ChartRange[] = ['1h', '6h', '1d', '1w', 'all']
const HEIGHT = 260

/**
 * Points as the library takes them: whole unix seconds, finite, strictly
 * ascending, one per second. It throws on anything else, and a payload is
 * not trusted to be any of that.
 */
export function chartData(points: readonly { t: number; c: number }[]) {
  const seen = new Set<number>()
  return points
    .filter((p) => Number.isFinite(p.t) && Number.isFinite(p.c))
    .map((p) => ({ time: Math.floor(p.t), value: p.c }))
    .sort((a, b) => a.time - b.time)
    .filter((p) => (seen.has(p.time) ? false : (seen.add(p.time), true)))
}

function Line({ points, label }: { points: { t: number; c: number }[]; label: string }) {
  const host = useRef<HTMLDivElement>(null)
  const mode = useTheme((s) => s.mode)
  useEffect(() => {
    const el = host.current
    if (!el) return
    let disposed = false
    let remove: (() => void) | null = null
    void import('lightweight-charts').then((lib) => {
      if (disposed || !host.current) return
      const theme = chartTheme(mode)
      let chart: ReturnType<typeof lib.createChart>
      try {
        chart = lib.createChart(el, {
          height: HEIGHT,
          autoSize: true,
          layout: { background: { color: theme.background }, textColor: theme.textColor },
          grid: { vertLines: { visible: false }, horzLines: { color: theme.gridColor } },
          rightPriceScale: { borderColor: theme.borderColor },
          timeScale: { borderColor: theme.borderColor, timeVisible: true },
          handleScroll: false,
          handleScale: false,
        })
      } catch {
        // No canvas (jsdom, headless): the line is decoration there.
        return
      }
      remove = () => chart.remove()
      const first = points[0]?.c ?? 0
      const last = points[points.length - 1]?.c ?? 0
      const color = last >= first ? theme.upColor : theme.downColor
      const series = chart.addSeries(lib.AreaSeries, {
        lineColor: color,
        topColor: `${color}55`,
        bottomColor: `${color}00`,
        lineWidth: 2,
        priceFormat: { type: 'custom', formatter: (p: number) => formatPrice(p) },
      })
      series.setData(chartData(points).map((p) => ({ ...p, time: p.time as never })))
      chart.timeScale().fitContent()
    })
    return () => {
      disposed = true
      remove?.()
    }
  }, [points, mode])
  return (
    <div
      ref={host}
      data-testid="trd-chart-canvas"
      style={{ height: HEIGHT }}
      role="img"
      aria-label={label}
    />
  )
}

export function PriceChart({
  chainId,
  token,
  symbol,
  onClose,
}: {
  chainId: number
  token: string
  symbol: string
  onClose?: () => void
}) {
  const [range, setRange] = useState<ChartRange>('1d')
  const chart = useChart(chainId, token, range)
  const shown = clampSym(symbol)
  const points = chart.data?.points ?? []
  const last = points[points.length - 1]?.c
  // The engine's change over the range, from its full series (the points are thinned).
  const change = chart.data?.stats?.changePct ?? null
  return (
    <Card asChild>
      <section
        aria-label={t('webTrading.chartOf', { symbol: shown })}
        className="flex flex-col gap-3"
      >
        <CardHeader
          title={t('webTrading.chartOf', { symbol: shown })}
          action={
            onClose ? (
              <Button
                variant="ghost"
                size="icon-sm"
                onClick={onClose}
                aria-label={t('webTrading.chartClose')}
              >
                <X aria-hidden />
              </Button>
            ) : null
          }
        />
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="flex items-baseline gap-2">
            <span className="trd-figure__value">
              {formatPrice(chart.data?.stats?.priceUsd ?? last)}
            </span>
            <span
              className="trd-num"
              data-tone={pnlTone(change)}
              title={t('webTrading.chartRangeChange')}
            >
              {formatPct(change, { signed: true })}
            </span>
          </p>
          <SegmentedControl
            size="sm"
            aria-label={t('webTrading.chartRange')}
            value={range}
            onChange={setRange}
            options={RANGES.map((r) => ({ value: r, label: r.toUpperCase() }))}
          />
        </div>
        {chart.isError ? (
          <ErrorState
            title={t('webTrading.chartFailed')}
            message={errorText(chart.error)}
            onRetry={() => void chart.refetch()}
          />
        ) : chart.isPending ? (
          <Skeleton className="rounded-[14px]" style={{ height: HEIGHT }} />
        ) : points.length < 2 ? (
          <p className="trd-note py-10 text-center">{t('webTrading.chartEmpty')}</p>
        ) : (
          <Line points={points} label={t('webTrading.chartOf', { symbol: shown })} />
        )}
      </section>
    </Card>
  )
}
