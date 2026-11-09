import { Construction } from 'lucide-react'
import { Link, useLocation } from 'react-router'
import { t } from '@/i18n'
import '@/i18n/en/webShell'
import { EmptyState } from '@/web/components/ui/states'
import { buttonVariants } from '@/web/components/ui/button'

/** Stands in for a destination that is not rebuilt yet (D13). */
export function RebuildingPlaceholder({ name }: { name: string }) {
  return (
    <div data-placeholder="rebuilding" className="flex h-full items-center justify-center">
      <EmptyState
        icon={<Construction />}
        title={t('webShell.rebuildingTitle', { name })}
        description={t('webShell.rebuildingBody')}
      />
    </div>
  )
}

/**
 * Unknown route. The requested path is rendered as a text node — React
 * escapes it — so markup in the URL can never execute.
 */
export function NotFound() {
  const { pathname } = useLocation()
  return (
    <div className="flex h-full items-center justify-center">
      <EmptyState
        title={t('webShell.notFoundTitle')}
        description={
          <span data-testid="not-found-path">{t('webShell.notFoundBody', { path: pathname })}</span>
        }
        action={
          <div className="flex gap-2">
            <Link to="/home" className={buttonVariants({ variant: 'outline' })}>
              {t('webShell.notFoundHome')}
            </Link>
            <Link to="/chat" className={buttonVariants({ variant: 'primary' })}>
              {t('webShell.notFoundChat')}
            </Link>
          </div>
        }
      />
    </div>
  )
}

export function RoutePending() {
  return (
    <div role="status" aria-busy="true" className="p-8 text-text-muted">
      {t('webShell.routePending')}
    </div>
  )
}
