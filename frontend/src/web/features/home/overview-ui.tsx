import { ChevronRight } from 'lucide-react'
import { useEffect, useState, type CSSProperties, type ReactNode } from 'react'
import { Link } from 'react-router'
import { cn } from '@/lib/utils'
import '@/web/styles/overview.css'

// Pieces shared by the overview pages (Home, Usage, Inbox): an icon in a
// soft tile, a meter, the "Details" link and the presence of a list whose
// items animate out. Styles live in styles/overview.css.

export type OverviewTone = 'neutral' | 'accent' | 'ok' | 'warn' | 'danger' | 'info'

/**
 * The one page container of the top-level pages (Home, Usage, Inbox,
 * Sessions, Projects, Automations): same max width, gutters and rhythm, so
 * page titles line up when moving between them.
 */
export const PAGE_CLASS = 'mx-auto flex w-full max-w-6xl flex-col gap-5 p-4 md:p-8'

// The stat family. Every figure card on these pages (Home, Usage, Inbox,
// Sessions, Automations, settings tiles) reads the same: an icon in a soft
// tile beside a muted label that wraps rather than clips, a stat-size
// tabular figure, then a small muted hint.

/** A figure card's label: wraps to two lines instead of cutting off. */
export const STAT_LABEL = 'line-clamp-2 min-w-0 flex-1 text-label text-text-muted'
/** A figure card's number. */
export const STAT_FIGURE = 'truncate text-stat tabular-nums'
/**
 * Brings a `StatCard` (components/ui) into the family: the shared padding
 * and gaps, and a label that wraps instead of truncating.
 */
export const STAT_CARD =
  'gap-2 p-4 sm:p-5 [&>header]:gap-2.5 [&>header>:is(h2,h3,h4,p)]:line-clamp-2 [&>header>:is(h2,h3,h4,p)]:whitespace-normal'
/** The same in a narrow column (a settings section): the tighter padding. */
export const STAT_CARD_DENSE =
  'gap-2 p-4 [&>header]:gap-2.5 [&>header>:is(h2,h3,h4,p)]:line-clamp-2 [&>header>:is(h2,h3,h4,p)]:whitespace-normal'

/** A lucide icon in a soft tile tinted with its tone. Decorative. */
export function IconTile({
  tone = 'neutral',
  size = 'md',
  className,
  children,
}: {
  tone?: OverviewTone
  size?: 'md' | 'lg'
  className?: string
  children: ReactNode
}) {
  return (
    <span aria-hidden data-ov-tone={tone} data-size={size} className={cn('ov-tile', className)}>
      {children}
    </span>
  )
}

/**
 * A figure card value that is a word, not a number ("Vector + full text",
 * "On"): body-size type, so it neither dwarfs the numbers beside it nor
 * wraps; the card keeps the figure's line height, so rows stay level.
 */
export function StatWord({ children }: { children: string }) {
  return (
    <span title={children} className="text-ui font-medium">
      {children}
    </span>
  )
}

/** Inline style carrying a stagger index (capped at 5, the entrance budget). */
export function staggerStyle(index: number, style?: CSSProperties): CSSProperties {
  return { ...style, '--stagger-i': Math.min(Math.max(index, 0), 5) } as CSSProperties
}

/** One clipped fill on a track: from `from`% to `to`% of its width. */
function fillStyle(from: number, to: number): CSSProperties {
  const clamp = (n: number) => (Number.isFinite(n) ? Math.min(100, Math.max(0, n)) : 0)
  return { '--from': clamp(from), '--to': clamp(to) } as CSSProperties
}

/**
 * A horizontal meter. Its fills reveal left to right on mount (staggered by
 * `index`) and glide when their values change. Decorative: the figure it
 * shows is always in text beside it.
 */
export function Meter({
  segments,
  index = 0,
  className,
}: {
  /** Percent widths laid end to end; `series` picks the colour. */
  segments: readonly { pct: number; series?: 'input' | 'output' }[]
  index?: number
  className?: string
}) {
  // Each fill starts where the previous one ends. Fills stay mounted at 0%
  // so a value that grows from nothing glides in too.
  const starts = segments.map((_, i) =>
    segments.slice(0, i).reduce((sum, segment) => sum + segment.pct, 0),
  )
  return (
    <span aria-hidden className={cn('ov-track', className)} style={staggerStyle(index)}>
      <span className="ov-reveal">
        {segments.map((segment, i) => (
          <span
            key={i}
            className="ov-fill"
            data-series={segment.series}
            style={fillStyle(starts[i]!, starts[i]! + segment.pct)}
          />
        ))}
      </span>
    </span>
  )
}

/**
 * A figure's "Details" link: a pill-shaped target whose chevron nudges on
 * hover. `stretched` lets the whole card (a `relative group` parent) take the
 * click, so the card can lift as one clickable tile.
 */
export function DetailsLink({
  to,
  label,
  children,
  stretched = false,
}: {
  to: string
  /** Accessible name; it starts with the visible text (WCAG 2.5.3). */
  label: string
  children: ReactNode
  stretched?: boolean
}) {
  return (
    <Link
      to={to}
      viewTransition
      aria-label={label}
      className={cn(
        'group/details target -mr-1.5 inline-flex h-6 shrink-0 items-center gap-0.5 rounded-full pr-1 pl-2 text-small font-medium text-accent',
        'transition-colors duration-[var(--duration-fast)] ease-[var(--ease-out)] hover:bg-accent-tint',
        stretched && "after:absolute after:inset-0 after:rounded-[inherit] after:content-['']",
      )}
    >
      {children}
      <ChevronRight
        aria-hidden
        className="size-3.5 transition-transform duration-[var(--duration-fast)] ease-[var(--ease-out)] group-hover:translate-x-0.5 group-hover/details:translate-x-0.5"
      />
    </Link>
  )
}

const reducedMotion = () =>
  typeof window !== 'undefined' &&
  typeof window.matchMedia === 'function' &&
  window.matchMedia('(prefers-reduced-motion: reduce)').matches

/** How long a removed item may linger if its exit animation never ends. */
const LEAVE_FALLBACK_MS = 1000

export interface Presence<T> {
  item: T
  /** The item is gone from the source list and is playing its exit. */
  leaving: boolean
}

/**
 * Keeps items that dropped out of `items` for the length of their exit
 * animation, at the place they left, so a resolved card fades and the list
 * closes up instead of the card vanishing. Call `done(id)` from the leaving
 * element's `animationend`. Under reduced motion items leave at once.
 */
export function usePresence<T>(
  items: readonly T[],
  idOf: (item: T) => string,
): { entries: Presence<T>[]; done: (id: string) => void } {
  const [state, setState] = useState<{
    prev: readonly T[]
    leaving: { item: T; index: number }[]
  }>({ prev: items, leaving: [] })

  let leaving = state.leaving
  if (state.prev !== items) {
    // Derived during render (not in an effect) so a removed item never
    // disappears for a frame before its exit starts.
    const ids = new Set(items.map(idOf))
    const kept = leaving.filter((l) => !ids.has(idOf(l.item)))
    const gone = reducedMotion()
      ? []
      : state.prev.map((item, index) => ({ item, index })).filter((l) => !ids.has(idOf(l.item)))
    leaving = [...kept, ...gone]
    setState({ prev: items, leaving })
  }

  const done = (id: string) =>
    setState((s) => ({ ...s, leaving: s.leaving.filter((l) => idOf(l.item) !== id) }))

  // A fallback in case the exit animation never reports its end (a hidden
  // tab, animations disabled by the browser).
  const pending = leaving.length
  useEffect(() => {
    if (pending === 0) return
    const timer = window.setTimeout(
      () => setState((s) => ({ ...s, leaving: [] })),
      LEAVE_FALLBACK_MS,
    )
    return () => window.clearTimeout(timer)
  }, [pending])

  const entries: Presence<T>[] = items.map((item) => ({ item, leaving: false }))
  for (const l of [...leaving].sort((a, b) => a.index - b.index)) {
    entries.splice(Math.min(l.index, entries.length), 0, { item: l.item, leaving: true })
  }
  return { entries, done }
}
