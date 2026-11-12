import type { CSSProperties } from 'react'
import { cn } from '@/lib/utils'

// The Usage page's drawing pieces, all CSS + SVG and decorative: the figures
// they show are always in text beside them. Bars reveal left to right on
// mount and glide (clip-path) when their values change; the donut's arcs draw
// in. Styles live in styles/page-usage.css.

/** The categorical series of the page, in the order they are assigned. */
export const SERIES = ['s1', 's2', 's3', 's4', 's5', 's6'] as const
export type Series = (typeof SERIES)[number]

export function seriesAt(index: number): Series {
  return SERIES[index % SERIES.length]!
}

const clamp = (n: number) => (Number.isFinite(n) ? Math.min(100, Math.max(0, n)) : 0)

/**
 * A horizontal bar of segments laid end to end (percent of the track each).
 * `index` staggers its entrance among sibling bars.
 */
export function Bar({
  segments,
  index = 0,
  className,
}: {
  segments: readonly { pct: number; series: Series }[]
  index?: number
  className?: string
}) {
  // Each fill starts where the previous one ends.
  const starts = segments.map((_, i) => segments.slice(0, i).reduce((sum, s) => sum + s.pct, 0))
  return (
    <span
      aria-hidden
      className={cn('us-bar', className)}
      style={{ '--bar-i': Math.min(index, 8) } as CSSProperties}
    >
      <span className="us-bar-reveal">
        {segments.map((segment, i) => (
          <span
            key={i}
            className="us-fill"
            data-series={segment.series}
            style={
              {
                '--from': clamp(starts[i]!),
                '--to': clamp(starts[i]! + segment.pct),
              } as CSSProperties
            }
          />
        ))}
      </span>
    </span>
  )
}

/** A colour key square. */
export function Swatch({ series, className }: { series: Series; className?: string }) {
  return <span aria-hidden className={cn('us-swatch', className)} data-series={series} />
}

const R = 52
const C = 2 * Math.PI * R
/** The gap between arcs, in the circle's units. */
const GAP = 2.5

/**
 * A ring of shares (percent each, in order). Arcs draw in one after another;
 * a share too small to see keeps a minimum sliver so it still reads.
 */
export function Donut({
  shares,
  children,
  className,
}: {
  shares: readonly { pct: number; series: Series }[]
  children?: React.ReactNode
  className?: string
}) {
  const visible = shares.filter((s) => s.pct > 0)
  const gap = visible.length > 1 ? GAP : 0
  const offsets = visible.map((_, i) =>
    visible.slice(0, i).reduce((sum, s) => sum + (clamp(s.pct) / 100) * C, 0),
  )
  return (
    <div className={cn('us-donut', className)}>
      <svg viewBox="0 0 120 120" aria-hidden>
        <circle className="us-donut-track" cx="60" cy="60" r={R} />
        {visible.map((share, i) => {
          const len = Math.max(1.5, (clamp(share.pct) / 100) * C - gap)
          return (
            <circle
              key={i}
              className="us-donut-arc"
              data-series={share.series}
              cx="60"
              cy="60"
              r={R}
              style={
                {
                  '--len': len,
                  '--arc-i': Math.min(i, 6),
                  strokeDasharray: `${len} ${C}`,
                  strokeDashoffset: -offsets[i]!,
                } as CSSProperties
              }
            />
          )
        })}
      </svg>
      {children ? <div className="us-donut-center">{children}</div> : null}
    </div>
  )
}
