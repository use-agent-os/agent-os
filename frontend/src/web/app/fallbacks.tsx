import { Link, useLocation } from 'react-router'
import { t } from '@/i18n'
import '@/i18n/en/webShell'
import { Search } from 'lucide-react'
import { EmptyState } from '@/web/components/ui/states'
import { buttonVariants } from '@/web/components/ui/button'

/**
 * Unknown route. The requested path is rendered as a text node — React
 * escapes it — so markup in the URL can never execute.
 */
export function NotFound() {
  const { pathname } = useLocation()
  return (
    <div className="bg-atmosphere flex h-full items-center justify-center">
      <EmptyState
        icon={<Search />}
        title={t('webShell.notFoundTitle')}
        description={
          <span data-testid="not-found-path">{t('webShell.notFoundBody', { path: pathname })}</span>
        }
        action={
          <div className="flex gap-2">
            <Link to="/home" viewTransition className={buttonVariants({ variant: 'secondary' })}>
              {t('webShell.notFoundHome')}
            </Link>
            <Link to="/chat" viewTransition className={buttonVariants({ variant: 'primary' })}>
              {t('webShell.notFoundChat')}
            </Link>
          </div>
        }
      />
    </div>
  )
}

/** A route chunk still loading: the page skeleton, drawn in CSS (shell.css). */
export function RoutePending() {
  return (
    <div role="status" aria-busy="true" className="p-6">
      <span className="sr-only">{t('webShell.routePending')}</span>
      <div aria-hidden className="route-skeleton" />
    </div>
  )
}
