import { ChevronRight, CircleAlert, Info, TriangleAlert } from 'lucide-react'
import type * as React from 'react'
import { cn } from '@/lib/utils'
import '@/web/styles/settings.css'

// Building blocks every settings section shares. The look lives in
// styles/settings.css (set-* classes); these keep the markup consistent.

/**
 * A designed disclosure: a native `<details>` (so keyboard and screen-reader
 * behaviour stay the browser's) with a rotating chevron and a body that rises
 * in when opened. `variant="card"` makes it its own card.
 */
export function Disclosure({
  summary,
  children,
  variant,
  className,
  bodyClassName,
  ...props
}: Omit<React.ComponentProps<'details'>, 'summary'> & {
  summary: React.ReactNode
  variant?: 'card'
  bodyClassName?: string
}) {
  return (
    <details data-variant={variant} className={cn('set-disclosure', className)} {...props}>
      <summary>
        <ChevronRight className="set-disclosure-chevron" aria-hidden />
        {summary}
      </summary>
      <div className={cn('set-disclosure-body pt-2', bodyClassName)}>{children}</div>
    </details>
  )
}

const NOTICE_ICONS = {
  info: Info,
  warn: TriangleAlert,
  danger: CircleAlert,
  ok: Info,
  accent: Info,
}

/** An inline banner: tinted by tone, icon in a soft tile, text, optional actions. */
export function Notice({
  tone = 'info',
  icon,
  actions,
  children,
  className,
  ...props
}: Omit<React.ComponentProps<'div'>, 'children'> & {
  tone?: keyof typeof NOTICE_ICONS
  /** Replaces the tone's default icon. */
  icon?: React.ReactNode
  actions?: React.ReactNode
  children: React.ReactNode
}) {
  const Icon = NOTICE_ICONS[tone]
  return (
    <div data-tone={tone} className={cn('set-notice', className)} {...props}>
      <span className="set-notice-icon" aria-hidden>
        {icon ?? <Icon data-status-icon />}
      </span>
      <div className="min-w-0 flex-1">{children}</div>
      {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
    </div>
  )
}

/** A titled settings card: heading, one line of description, actions on the right. */
export function SettingsCard({
  title,
  description,
  actions,
  icon,
  as: Heading = 'h2',
  headingId,
  children,
  className,
  ...props
}: Omit<React.ComponentProps<'section'>, 'title'> & {
  title?: React.ReactNode
  description?: React.ReactNode
  actions?: React.ReactNode
  icon?: React.ReactNode
  as?: 'h2' | 'h3'
  headingId?: string
}) {
  return (
    <section className={cn('set-card', className)} {...props}>
      {title ? (
        <header className="mb-1 flex flex-wrap items-start gap-x-3 gap-y-2">
          {icon ? (
            <span
              className="mt-0.5 grid size-8 shrink-0 place-items-center rounded-control bg-surface-2 text-text-muted shadow-highlight [&_svg]:size-4"
              aria-hidden
            >
              {icon}
            </span>
          ) : null}
          <div className="min-w-0 flex-1">
            <Heading id={headingId} className="set-card-title">
              {title}
            </Heading>
            {description ? (
              <p className="mt-0.5 text-small text-pretty text-text-muted">{description}</p>
            ) : null}
          </div>
          {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
        </header>
      ) : null}
      {children}
    </section>
  )
}
