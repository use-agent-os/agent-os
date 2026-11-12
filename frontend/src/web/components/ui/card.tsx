import { Slot } from '@radix-ui/react-slot'
import { cva, type VariantProps } from 'class-variance-authority'
import { ArrowDownRight, ArrowUpRight, Minus } from 'lucide-react'
import * as React from 'react'
import { cn } from '@/lib/utils'
import { LoadingRegion, Skeleton } from './states'
import { useCountUp } from './use-count-up'

// Cards read as cards through their surface step, a hairline and (raised) a
// soft shadow plus the dark-theme top highlight, not through a heavy outline.

export const cardVariants = cva('min-w-0 rounded-card border border-border text-text', {
  variants: {
    variant: {
      /** A flat panel on the page: sections, lists, tables. */
      plain: 'bg-surface-1',
      /** The default tile: figures, summaries, anything that sits above the page. */
      raised: 'bg-surface-1 shadow-card',
      /** A clickable card (render a Link/button via asChild): lifts on hover. */
      interactive:
        'lift block cursor-pointer bg-surface-1 shadow-card hover:bg-[color-mix(in_oklab,var(--surface-1),var(--text)_2%)]',
      /** A sunk well inside a card: logs, code, previews. */
      inset: 'bg-surface-inset shadow-inset',
    },
    padding: {
      none: '',
      sm: 'p-3',
      md: 'p-4',
      lg: 'p-6',
    },
  },
  defaultVariants: { variant: 'raised', padding: 'md' },
})

export interface CardProps extends React.ComponentProps<'div'>, VariantProps<typeof cardVariants> {
  /** Render the child element (a `section`, `Link`, `button`) with card styling. */
  asChild?: boolean
}

export function Card({ className, variant, padding, asChild = false, ...props }: CardProps) {
  const Comp = asChild ? Slot : 'div'
  return (
    <Comp
      data-slot="card"
      data-variant={variant ?? 'raised'}
      className={cn(cardVariants({ variant, padding }), className)}
      {...props}
    />
  )
}

/** The small label row at the top of a card: icon, muted title, optional action. */
export function CardHeader({
  icon,
  title,
  action,
  as: Heading = 'h2',
  className,
}: {
  icon?: React.ReactNode
  title: React.ReactNode
  action?: React.ReactNode
  as?: 'h2' | 'h3' | 'h4' | 'p'
  className?: string
}) {
  return (
    <header className={cn('flex min-w-0 items-center gap-2', className)}>
      {icon ? (
        <span className="text-text-muted [&_svg]:size-4" aria-hidden>
          {icon}
        </span>
      ) : null}
      <Heading className="min-w-0 flex-1 truncate text-label text-text-muted">{title}</Heading>
      {action}
    </header>
  )
}

export type Trend = {
  direction: 'up' | 'down' | 'flat'
  /** Visible text, e.g. "+12% vs last week". */
  label: string
  /** Whether this direction is good news (colours it ok/danger). Default: up is good. */
  good?: boolean
}

const TONE_TEXT = {
  default: '',
  accent: 'text-accent',
  ok: 'text-ok',
  warn: 'text-warn',
  danger: 'text-danger',
} as const

/**
 * A figure tile: label, big tabular figure, hint, and optionally an icon, a
 * header action (e.g. a "Details" link), a trend chip and a value tone.
 * Renders a raised Card as a `section` named by its label. A numeric `value`
 * counts up on arrival (reduced-motion aware) and is shown through `format`.
 */
export function StatCard({
  label,
  value,
  format = (n) => n.toLocaleString(),
  hint,
  icon,
  action,
  trend,
  tone = 'default',
  loading = false,
  loadingLabel,
  headingAs = 'h2',
  children,
  className,
}: {
  label: string
  value: React.ReactNode
  /** Formats a numeric `value` (default: locale grouping). */
  format?: (value: number) => string
  hint?: React.ReactNode
  icon?: React.ReactNode
  action?: React.ReactNode
  trend?: Trend
  tone?: keyof typeof TONE_TEXT
  /** Show a skeleton figure instead of the value. */
  loading?: boolean
  loadingLabel?: string
  headingAs?: 'h2' | 'h3' | 'h4' | 'p'
  /** Extra content under the hint (a sparkline, a list). */
  children?: React.ReactNode
  className?: string
}) {
  const counted = useCountUp(typeof value === 'number' ? value : Number.NaN)
  const shown = typeof value === 'number' ? format(counted) : value
  return (
    <Card asChild className={cn('flex flex-col gap-1.5', className)}>
      <section aria-label={label} data-slot="stat-card">
        <CardHeader icon={icon} title={label} action={action} as={headingAs} />
        {loading ? (
          <LoadingRegion label={loadingLabel} className="flex flex-col gap-1.5">
            <Skeleton className="h-8 w-28" />
            <Skeleton className="h-4 w-36" />
          </LoadingRegion>
        ) : (
          <>
            <p
              title={typeof value === 'string' ? value : undefined}
              className={cn('mt-0.5 truncate text-stat tabular-nums', TONE_TEXT[tone])}
            >
              {shown}
            </p>
            {hint || trend ? (
              <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-small text-text-muted">
                {trend ? <TrendChip trend={trend} /> : null}
                {hint ? (
                  <p
                    title={typeof hint === 'string' ? hint : undefined}
                    className="line-clamp-2 min-w-0 break-words"
                  >
                    {hint}
                  </p>
                ) : null}
              </div>
            ) : null}
          </>
        )}
        {children}
      </section>
    </Card>
  )
}

function TrendChip({ trend }: { trend: Trend }) {
  const good = trend.good ?? trend.direction === 'up'
  const Icon =
    trend.direction === 'up' ? ArrowUpRight : trend.direction === 'down' ? ArrowDownRight : Minus
  return (
    <span
      className={cn(
        'inline-flex items-center gap-0.5 font-medium tabular-nums',
        trend.direction === 'flat' ? 'text-text-muted' : good ? 'text-ok' : 'text-danger',
      )}
    >
      <Icon className="size-3.5" aria-hidden data-status-icon />
      {trend.label}
    </span>
  )
}
