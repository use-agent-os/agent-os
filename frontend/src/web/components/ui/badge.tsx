import { cva, type VariantProps } from 'class-variance-authority'
import * as React from 'react'
import { cn } from '@/lib/utils'

// Status pills and labels: a soft tinted fill with a hairline in the tone's
// colour. Meaning is always in the text too, never in the colour alone.

export const badgeVariants = cva(
  'inline-flex max-w-full items-center gap-1.5 rounded-full border px-2 py-0.5 text-small font-medium whitespace-nowrap [&_svg]:size-3 [&_svg]:shrink-0',
  {
    variants: {
      tone: {
        neutral: 'border-border-strong bg-surface-2 text-text-muted',
        accent: 'border-accent/30 bg-accent-tint text-accent',
        ok: 'border-ok/30 bg-ok/10 text-ok',
        warn: 'border-warn/30 bg-warn/10 text-warn',
        danger: 'border-danger/30 bg-danger/10 text-danger',
        info: 'border-info/30 bg-info/10 text-info',
      },
      variant: {
        soft: '',
        outline: 'bg-transparent',
      },
    },
    defaultVariants: { tone: 'neutral', variant: 'soft' },
  },
)

type Tone = NonNullable<VariantProps<typeof badgeVariants>['tone']>

export interface BadgeProps
  extends React.ComponentProps<'span'>, VariantProps<typeof badgeVariants> {
  /** Lead with a status dot in the tone's colour. */
  dot?: boolean
  /** The dot pings: something is running right now. */
  live?: boolean
}

export function Badge({ className, tone, variant, dot, live, children, ...props }: BadgeProps) {
  return (
    <span data-slot="badge" className={cn(badgeVariants({ tone, variant }), className)} {...props}>
      {dot || live ? <StatusDot live={live} className="size-1.5" /> : null}
      {children}
    </span>
  )
}

const DOT_TONE: Record<Tone, string> = {
  neutral: 'text-text-muted',
  accent: 'text-accent',
  ok: 'text-ok',
  warn: 'text-warn',
  danger: 'text-danger',
  info: 'text-info',
}

/**
 * A small status dot in currentColor (or a `tone`). `live` adds the ping
 * (base.css `.status-dot[data-live]`) and keeps a slow pulse under reduced
 * motion. Decorative: put the status in text next to it.
 */
export function StatusDot({
  tone,
  live = false,
  className,
}: {
  tone?: Tone
  live?: boolean
  className?: string
}) {
  return (
    <span
      aria-hidden
      data-live={live || undefined}
      className={cn(
        'status-dot size-2',
        live && 'motion-status',
        tone && DOT_TONE[tone],
        className,
      )}
    />
  )
}
