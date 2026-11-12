import * as React from 'react'
import { cn } from '@/lib/utils'

/**
 * The title row of a section inside a page: title (with an optional icon),
 * one line of description, actions on the right. `size="sm"` is the small
 * uppercase group label (settings nav groups, list sections).
 */
export function SectionHeader({
  title,
  description,
  actions,
  icon,
  as: Heading = 'h2',
  id,
  size = 'md',
  className,
}: {
  title: React.ReactNode
  description?: React.ReactNode
  actions?: React.ReactNode
  icon?: React.ReactNode
  as?: 'h2' | 'h3' | 'h4'
  /** Id for the heading, to name a region with aria-labelledby. */
  id?: string
  size?: 'sm' | 'md'
  className?: string
}) {
  return (
    <div
      data-slot="section-header"
      className={cn('flex flex-wrap items-end justify-between gap-x-4 gap-y-2', className)}
    >
      <div className="min-w-0">
        <Heading
          id={id}
          className={cn(
            'flex items-center gap-2',
            size === 'sm'
              ? 'text-label tracking-[0.06em] text-text-muted uppercase'
              : 'text-section',
            '[&_svg]:size-4 [&_svg]:text-text-muted',
          )}
        >
          {icon ? <span aria-hidden>{icon}</span> : null}
          {title}
        </Heading>
        {description ? (
          <p className="mt-0.5 text-small text-pretty text-text-muted">{description}</p>
        ) : null}
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
    </div>
  )
}
