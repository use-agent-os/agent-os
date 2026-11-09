import { AlertTriangle } from 'lucide-react'
import { isRouteErrorResponse, useRouteError } from 'react-router'
import { t } from '@/i18n'
import '@/i18n/en/webShell'
import { Button } from '@/web/components/ui/button'
import { EmptyState } from '@/web/components/ui/states'

/** A route that threw: name the failure and offer a reload, inside the shell. */
export function RouteErrorBoundary() {
  const error = useRouteError()
  const detail = isRouteErrorResponse(error)
    ? `${error.status} ${error.statusText}`
    : error instanceof Error
      ? error.message
      : String(error)
  return (
    <div role="alert" className="flex h-full items-center justify-center">
      <EmptyState
        icon={<AlertTriangle />}
        title={t('webShell.routeErrorTitle')}
        description={detail}
        action={
          <Button variant="outline" onClick={() => window.location.reload()}>
            {t('webShell.routeErrorReload')}
          </Button>
        }
      />
    </div>
  )
}
