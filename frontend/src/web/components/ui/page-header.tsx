import type { ReactNode } from 'react'

// The title row of a top-level page. Phones stack it (title and description
// full width, actions on their own row, left-aligned) so the actions never
// squeeze the description into a narrow column; from `sm` up the actions sit
// on the right.

export function PageHeader({
  title,
  badge,
  description,
  actions,
  note,
  footer,
}: {
  title: ReactNode
  /** Beside the title, e.g. a stale marker. */
  badge?: ReactNode
  description?: ReactNode
  actions?: ReactNode
  /** Under the actions, e.g. why they are disabled. */
  note?: ReactNode
  /** A full-width line under the whole header. */
  footer?: ReactNode
}) {
  return (
    <header
      data-page-header
      className="flex flex-col gap-3 sm:flex-row sm:flex-wrap sm:items-start"
    >
      <div className="min-w-0 sm:flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-xl font-semibold">{title}</h1>
          {badge}
        </div>
        {description ? (
          <p data-page-header-description className="text-text-muted">
            {description}
          </p>
        ) : null}
      </div>
      {actions ? (
        <div className="flex flex-col items-start gap-1 sm:items-end">
          <div className="flex flex-wrap items-center gap-2">{actions}</div>
          {note}
        </div>
      ) : null}
      {footer ? <div className="w-full">{footer}</div> : null}
    </header>
  )
}
