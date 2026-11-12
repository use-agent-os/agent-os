import { AlertTriangle, History } from 'lucide-react'
import * as React from 'react'
import { t } from '@/i18n'
import '@/i18n/en/webCommon'
import { cn } from '@/lib/utils'
import { Badge } from './badge'
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
        'skeleton-shimmer rounded-md bg-[color-mix(in_oklab,var(--text)_8%,transparent)] transition-opacity duration-[var(--duration-base)]',
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

/**
 * A designed empty state, composed on the canvas rather than framed: the icon
 * in a soft round mark with faint halo rings, a clear title, one line of help
 * and the primary action. Never a bordered box around emptiness. `size="sm"`
 * for a panel or list body.
 */
export function EmptyState({
  icon,
  title,
  description,
  action,
  size = 'md',
  className,
}: {
  icon?: React.ReactNode
  title: string
  description?: React.ReactNode
  action?: React.ReactNode
  size?: 'sm' | 'md'
  className?: string
}) {
  return (
    <div
      data-slot="empty-state"
      data-size={size}
      className={cn(
        'empty-state anim-enter mx-auto flex max-w-md flex-col items-center text-center',
        size === 'sm' ? 'px-4 py-8' : 'px-6 py-16',
        className,
      )}
    >
      {icon ? (
        <div className="empty-mark" aria-hidden>
          {icon}
        </div>
      ) : null}
      <h2 className="empty-title">{title}</h2>
      {description ? <p className="empty-description">{description}</p> : null}
      {action ? <div className="mt-5 flex flex-wrap justify-center gap-2">{action}</div> : null}
    </div>
  )
}

/**
 * An error notice with Retry: a soft danger-tinted strip (no outlined box),
 * the icon in a small round mark. Children (data already loaded) stay
 * rendered beneath it: a failed refresh never blanks what the user was
 * reading.
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
        className="error-strip anim-fade-in flex items-center gap-3 rounded-card py-2.5 pr-2.5 pl-3"
      >
        <span className="grid size-7 shrink-0 place-items-center self-start rounded-full bg-danger/12 text-danger">
          <AlertTriangle className="size-3.5" aria-hidden data-status-icon />
        </span>
        <div className="min-w-0 flex-1 self-center">
          <p className="font-medium">{title ?? t('webCommon.errorTitle')}</p>
          {message ? <p className="text-small break-words text-text-muted">{message}</p> : null}
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
    <Badge
      tone="warn"
      data-slot="stale-badge"
      title={t('webCommon.staleTitle')}
      className={cn('[&_svg]:size-3.5', className)}
    >
      <History aria-hidden data-status-icon />
      {label}
    </Badge>
  )
}
