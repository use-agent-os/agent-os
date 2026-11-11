import { useEffect, useState, type ReactNode } from 'react'
import { cn } from '@/lib/utils'
import { Card } from '@/web/components/ui/card'
import { LoadingRegion, Skeleton } from '@/web/components/ui/states'
import '@/web/styles/work.css'

// The compact figure strip at the top of the work pages (Sessions,
// Automations): one raised card split into cells by hairlines, each a
// `dt` label, a `dd` figure and optional `dd` detail. Tests read a figure
// as the label's next sibling, so keep that order.

export type StripTone = 'default' | 'ok' | 'warn' | 'danger' | 'accent'

export interface StripItem {
  label: string
  value: ReactNode
  icon?: ReactNode
  /** Colours the figure (only when it signals something, e.g. failures > 0). */
  tone?: StripTone
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
  return 'grid-cols-2 sm:grid-cols-4'
}

export function StatStrip({ items, className }: { items: StripItem[]; className?: string }) {
  const units = items.reduce((n, item) => n + (item.wide ? 2 : 1), 0)
  return (
    <Card padding="none" className={cn('anim-enter overflow-hidden', className)}>
      <dl className={cn('work-strip', columns(units))}>
        {items.map((item) => (
          <div
            key={item.label}
            className={cn(
              'flex flex-col gap-1 px-3.5 py-3',
              item.wide && 'col-span-2 lg:col-span-1',
            )}
          >
            <dt
              title={item.label}
              className="truncate text-label text-text-muted [&_svg]:mr-1.5 [&_svg]:inline [&_svg]:size-3.5 [&_svg]:align-[-2px]"
            >
              {item.icon}
              {item.label}
            </dt>
            <dd className={cn('text-title tabular-nums', TONE[item.tone ?? 'default'])}>
              {item.value}
            </dd>
            {item.detail ? (
              <dd className="min-w-0 text-small text-text-muted">{item.detail}</dd>
            ) : null}
          </div>
        ))}
      </dl>
    </Card>
  )
}

/** The strip's loading shape: same cells and heights, so nothing shifts. */
export function StatStripSkeleton({ cells, wide = false }: { cells: number; wide?: boolean }) {
  const units = cells + (wide ? 1 : 0)
  return (
    <Card padding="none" className="overflow-hidden">
      <div className={cn('work-strip', columns(units))}>
        {Array.from({ length: cells }, (_, i) => (
          <div
            key={i}
            className={cn(
              'flex flex-col gap-2 px-3.5 py-3',
              wide && i === 0 && 'col-span-2 lg:col-span-1',
            )}
          >
            <Skeleton className="h-3.5 w-20" />
            <Skeleton className="h-6 w-12" />
            {wide && i === 0 ? (
              <>
                <Skeleton className="h-1.5 w-full" />
                <Skeleton className="h-3 w-3/4" />
                <Skeleton className="h-3 w-1/2" />
              </>
            ) : null}
          </div>
        ))}
      </div>
    </Card>
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
