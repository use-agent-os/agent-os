import { AlertTriangle, History } from 'lucide-react'
import * as React from 'react'
import { t } from '@/i18n'
import '@/i18n/en/webCommon'
import { cn } from '@/lib/utils'
import { Button } from './button'

// The standard view states (design-system spec "Standard view states"):
// loading after 300 ms, empty with the primary action, error that keeps stale
// data visible, and a stale marker for snapshot data.

export const SKELETON_DELAY_MS = 300

/** True once `delay` ms have passed since mount. */
export function useDelayed(delay: number = SKELETON_DELAY_MS): boolean {
  const [ready, setReady] = React.useState(delay <= 0)
  React.useEffect(() => {
    if (delay <= 0) return
    const id = window.setTimeout(() => setReady(true), delay)
    return () => window.clearTimeout(id)
  }, [delay])
  return ready
}

/**
 * A placeholder block. It always occupies its box (so nothing shifts when data
 * arrives) but only becomes visible after `delay` ms, so fast loads never
 * flash a skeleton.
 */
export function Skeleton({
  className,
  delay = SKELETON_DELAY_MS,
  ...props
}: React.ComponentProps<'div'> & { delay?: number }) {
  const visible = useDelayed(delay)
  return (
    <div
      data-slot="skeleton"
      data-visible={visible || undefined}
      aria-hidden
      className={cn(
        'rounded-md bg-surface-3 transition-opacity',
        visible ? 'opacity-100' : 'opacity-0',
        className,
      )}
      {...props}
    />
  )
}

/** Announces a pending load to assistive technology and wraps skeletons. */
export function LoadingRegion({
  label,
  children,
  className,
}: {
  label?: string
  children: React.ReactNode
  className?: string
}) {
  return (
    <div
      role="status"
      aria-busy="true"
      aria-label={label ?? t('webCommon.loading')}
      className={className}
    >
      {children}
    </div>
  )
}

export function EmptyState({
  icon,
  title,
  description,
  action,
  className,
}: {
  icon?: React.ReactNode
  title: string
  description?: React.ReactNode
  action?: React.ReactNode
  className?: string
}) {
  return (
    <div
      data-slot="empty-state"
      className={cn(
        'mx-auto flex max-w-md flex-col items-center gap-3 px-6 py-12 text-center',
        className,
      )}
    >
      {icon ? (
        <div className="text-text-muted [&_svg]:size-8" aria-hidden>
          {icon}
        </div>
      ) : null}
      <h2 className="text-base font-semibold">{title}</h2>
      {description ? <p className="text-text-muted">{description}</p> : null}
      {action ? <div className="mt-1">{action}</div> : null}
    </div>
  )
}

/**
 * An error notice with Retry. Children (data already loaded) stay rendered
 * beneath it: a failed refresh never blanks what the user was reading.
 */
export function ErrorState({
  title,
  message,
  onRetry,
  retrying = false,
  children,
  className,
}: {
  title?: string
  message?: React.ReactNode
  onRetry?: () => void
  retrying?: boolean
  children?: React.ReactNode
  className?: string
}) {
  return (
    <div data-slot="error-state" className={className}>
      <div
        role="alert"
        className="flex items-start gap-3 rounded-lg border border-danger/40 bg-danger/10 px-4 py-3"
      >
        <AlertTriangle
          className="mt-0.5 size-4 shrink-0 text-danger"
          aria-hidden
          data-status-icon
        />
        <div className="min-w-0 flex-1">
          <p className="font-medium">{title ?? t('webCommon.errorTitle')}</p>
          {message ? <p className="break-words text-text-muted">{message}</p> : null}
        </div>
        {onRetry ? (
          <Button size="sm" variant="outline" onClick={onRetry} disabled={retrying}>
            {t('webCommon.retry')}
          </Button>
        ) : null}
      </div>
      {children ? <div className="mt-3">{children}</div> : null}
    </div>
  )
}

/** Marks data shown from a snapshot while offline. */
export function StaleBadge({ since, className }: { since?: string; className?: string }) {
  const label = since ? t('webCommon.staleSince', { time: since }) : t('webCommon.stale')
  return (
    <span
      data-slot="stale-badge"
      title={t('webCommon.staleTitle')}
      className={cn(
        'inline-flex items-center gap-1 rounded-full border border-warn/50 px-2 py-0.5 text-small text-warn',
        className,
      )}
    >
      <History className="size-3.5" aria-hidden data-status-icon />
      {label}
    </span>
  )
}
