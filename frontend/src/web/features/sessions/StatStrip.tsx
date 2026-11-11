import { useEffect, useState, type ReactNode } from 'react'
import { cn } from '@/lib/utils'
import { Card } from '@/web/components/ui/card'
import { LoadingRegion, Skeleton } from '@/web/components/ui/states'
import { IconTile, STAT_FIGURE, type OverviewTone } from '@/web/features/home/overview-ui'
import '@/web/styles/work.css'

// The figure row at the top of the work pages (Sessions, Automations) and
// the Inbox: one raised card per figure, in the same family as the Home and
// Usage stat cards (icon tile + muted label, stat-size figure, small hint).
// Each card is a `dt` label, a `dd` figure and optional `dd` detail; tests
// read a figure as the label's next sibling, so keep that order.

export type StripTone = 'default' | 'ok' | 'warn' | 'danger' | 'accent'

export interface StripItem {
  label: string
  value: ReactNode
  icon?: ReactNode
  /** Colours the figure (only when it signals something, e.g. failures > 0). */
  tone?: StripTone
  /** Tints the icon tile (default: neutral). */
  iconTone?: OverviewTone
  /** Under the figure: a breakdown, a live status line. */
  detail?: ReactNode
  /** Spans two columns on wide layouts (the cell with a breakdown). */
  wide?: boolean
}

const TONE: Record<StripTone, string> = {
  default: '',
  ok: 'text-ok',
  warn: 'text-warn',
  danger: 'text-danger',
  accent: 'text-accent',
}

/** Grid columns per breakpoint, by the number of columns the items need. */
function columns(units: number): string {
  // Five cells, the first one wide: it gets 1.5 shares on one wide row.
  if (units >= 6)
    return 'grid-cols-2 sm:grid-cols-3 lg:grid-cols-[minmax(0,1.5fr)_repeat(4,minmax(0,1fr))]'
  if (units === 5) return 'grid-cols-2 sm:grid-cols-5'
  if (units === 3) return 'grid-cols-3'
  return 'grid-cols-2 sm:grid-cols-4'
}

/** One figure card's surface and rhythm, shared with the skeleton. */
const CELL = 'flex min-w-0 flex-col gap-2 p-4 sm:p-5'

export function StatStrip({
  items,
  label,
  className,
}: {
  items: StripItem[]
  /** Names the figure list (e.g. "Summary"). */
  label?: string
  className?: string
}) {
  const units = items.reduce((n, item) => n + (item.wide ? 2 : 1), 0)
  return (
    <dl
      aria-label={label}
      className={cn('anim-stagger grid gap-3 sm:gap-4', columns(units), className)}
    >
      {items.map((item) => (
        <Card
          key={item.label}
          padding="none"
          className={cn(CELL, item.wide && 'col-span-2 lg:col-span-1')}
        >
          {/* The label is the dt's own text (tests and getNodeText read it);
              it wraps beside the tile rather than cutting off. */}
          <dt
            title={item.label}
            className="flex min-h-8 min-w-0 items-center gap-2.5 text-label text-text-muted"
          >
            {item.icon ? (
              <IconTile tone={item.iconTone} className="max-sm:hidden">
                {item.icon}
              </IconTile>
            ) : null}
            {item.label}
          </dt>
          <dd className={cn(STAT_FIGURE, TONE[item.tone ?? 'default'])}>{item.value}</dd>
          {item.detail ? (
            <dd className="min-w-0 text-small text-text-muted">{item.detail}</dd>
          ) : null}
        </Card>
      ))}
    </dl>
  )
}

/** The strip's loading shape: same cells and heights, so nothing shifts. */
export function StatStripSkeleton({ cells, wide = false }: { cells: number; wide?: boolean }) {
  const units = cells + (wide ? 1 : 0)
  return (
    <div className={cn('grid gap-3 sm:gap-4', columns(units))}>
      {Array.from({ length: cells }, (_, i) => (
        <Card
          key={i}
          padding="none"
          className={cn(CELL, wide && i === 0 && 'col-span-2 lg:col-span-1')}
        >
          <span className="flex items-center gap-2.5">
            <Skeleton className="size-8 rounded-control max-sm:hidden" />
            <Skeleton className="h-3.5 w-20" />
          </span>
          <Skeleton className="h-8 w-12" />
          {wide && i === 0 ? (
            <>
              <Skeleton className="h-1.5 w-full" />
              <Skeleton className="h-3 w-3/4" />
            </>
          ) : null}
        </Card>
      ))}
    </div>
  )
}

/** A loading region with the strip on top and `children` (the list shape) below. */
export function WorkSkeleton({
  label,
  cells,
  wide,
  children,
}: {
  label: string
  cells: number
  wide?: boolean
  children: ReactNode
}) {
  return (
    <LoadingRegion label={label} className="flex flex-col gap-4">
      <StatStripSkeleton cells={cells} wide={wide} />
      {children}
    </LoadingRegion>
  )
}

/**
 * True until shortly after `ready` first turns true: put a one-time entrance
 * class (`anim-stagger`) on a list while it is, so rows rise in when the data
 * first arrives and never again on a refetch, a sort or a ticking value.
 */
export function useArrival(ready: boolean, ms = 700): boolean {
  const [settled, setSettled] = useState(false)
  useEffect(() => {
    if (!ready || settled) return
    const id = window.setTimeout(() => setSettled(true), ms)
    return () => window.clearTimeout(id)
  }, [ready, settled, ms])
  return !settled
}
